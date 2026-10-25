// Package audit 定义只追加审计链的编码、校验与 detail 白名单。
//
// # 功能
// 链是一串 HMAC-SHA256 哈希。每行哈希「上一行哈希 + 本行字段的长度前缀编码」。
// 长度前缀让字段边界无歧义，两行不同内容不会得到同一 payload。
// 本包**不**读写数据库，**不**持有 HMAC 密钥，**不**判定账号当日终态。
//
// # 归属
// core 层纯领域包。adapters/pg 提供行数据与链头序列化；
// 追加顺序与锁由适配器保证（见 internal/app/audit）。
//
// # 复用
// Action 及全部动作常量；Entry、Row、Report；
// EncodeDetail、Payload、Hash、Verify、ZeroHash；
// 错误值 ErrEmptyChain、ErrBrokenChain、ErrDetail*；
// 上限常量 MaxDetailBytes、MaxReasonChars、Domain。
//
// # 口径
// 摘要算法固定 HMAC-SHA256，密钥由调用方注入。
// 时间戳统一 UTC RFC3339Nano，跨进程字节稳定。
// detail 只允许 8 个键，自由文本只有 reason，上限 200 字符。
package audit

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"regexp"
	"time"
	"unicode/utf8"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mask"
)

// Domain 把本链与其他 HMAC 用途隔开，参与 payload 首段。
const Domain = "yiban.audit.v1"

// MaxDetailBytes 是 detail 的存储上限 4096 字节。
// **超限拒绝，不截断**：截断会让存储字节与哈希字节不一致，
// 校验时无法复算出原摘要。
const MaxDetailBytes = 4096

// Action 是受审计的操作名。
// 本处词表是动作取值的唯一来源；数据库 CHECK 只校验形状，不校验集合。
type Action string

// P1 动作词表。P4 补任务类动作，见下方 task.* 三项。
const (
	ActionAuthLogin           Action = "auth.login"
	ActionAuthLoginFailed     Action = "auth.login_failed"
	ActionAuthLogout          Action = "auth.logout"
	ActionAuthPasswordReset   Action = "auth.password_reset"
	ActionUserCreate          Action = "user.create"
	ActionUserDisable         Action = "user.disable"
	ActionUserEnable          Action = "user.enable"
	ActionUserDelete          Action = "user.delete"
	ActionSchoolCreate        Action = "school.create"
	ActionSchoolUpdate        Action = "school.update"
	ActionSchoolDisable       Action = "school.disable"
	ActionAdminGrant          Action = "admin.grant"
	ActionAdminRevoke         Action = "admin.revoke"
	ActionAccountCreate       Action = "account.create"
	ActionAccountUpdate       Action = "account.update"
	ActionAccountReviewPass   Action = "account.review_approve"
	ActionAccountReviewReject Action = "account.review_reject"
	ActionAccountPause        Action = "account.pause"
	ActionAccountResume       Action = "account.resume"
	ActionAccountDelete       Action = "account.delete"
	ActionAccountRestore      Action = "account.restore"
	ActionCredentialUpdate    Action = "credential.update"
	ActionCredentialRekey     Action = "credential.rekey"
	ActionTaskReclaim         Action = "task.reclaim"
	ActionTaskClaim           Action = "task.claim"
	ActionTaskSettle          Action = "task.settle"
)

// Entry 是一次追加的输入。
// SchoolID、ActorUserID、ActorRole 为空时存储为 NULL。
// Detail 由存储侧校验并编码。
type Entry struct {
	TS          time.Time
	SchoolID    string
	ActorUserID string
	ActorRole   coreauth.Role
	ActorLabel  string
	Action      Action
	TargetType  string
	TargetID    string
	Detail      map[string]any
}

// Row 是一行已存储的审计记录。PrevHash 与 Hash 是 32 字节原值。
type Row struct {
	ID          int64
	TS          time.Time
	SchoolID    string
	ActorUserID string
	ActorRole   string
	ActorLabel  string
	Action      string
	TargetType  string
	TargetID    string
	Detail      string
	PrevHash    []byte
	Hash        []byte
}

// Report 汇总一次校验的区间。
type Report struct {
	FirstID int64
	LastID  int64
	Count   int64
}

// ErrEmptyChain 表示校验了零行。
// **空扫描永不通过**：它没有证明任何事。
var ErrEmptyChain = errors.New("audit: no rows to verify")

// ErrBrokenChain 表示某行存储哈希或链接与复算结果不符。
var ErrBrokenChain = errors.New("audit: chain is broken")

// ErrDetailKey 表示 detail 键不在白名单内。
var ErrDetailKey = errors.New("audit: detail key is not allowed")

// ErrDetailPII 表示 detail 值含手机号或邮箱。
var ErrDetailPII = errors.New("audit: detail contains personal data")

// ErrDetailTooLarge 表示 detail 值超过 MaxDetailBytes。
var ErrDetailTooLarge = errors.New("audit: detail is too large")

// ErrDetailType 表示 detail 值类型不在标量白名单内。
var ErrDetailType = errors.New("audit: detail value type is not allowed")

// ErrDetailShape 表示字符串不符合其键的形状。
var ErrDetailShape = errors.New("audit: detail value shape is not allowed")

// ErrDetailReason 表示 reason 超过 MaxReasonChars 个字符。
// 空 reason 放行：追加不强制带 reason。
// 本处只查上界；要求必填的调用方自行校验。
var ErrDetailReason = errors.New("audit: detail reason is out of range")

// MaxReasonChars 限制 reason 的字符数 200。
// 自由文本是任意词句进入链的唯一入口，故取最紧的上限。
const MaxReasonChars = 200

// 落在本闭区间内的数字按手机号形状拒绝。
// 11 位手机号与普通整数无法区分，故整段形状一律拒绝。
const (
	phoneNumberMin = 10000000000
	phoneNumberMax = 14999999999
)

// detailKeys 是允许的 detail 键全集，闭合不可扩展。
var (
	// traceIDShape 匹配 entry/http/router 生成的 id：32 位十六进制，
	// 或 req-<计数> 的兜底形式（最多六位数字）。
	// 客户端自带的请求头不会到达审计层，故裸数字串（如手机号）在此被拒。
	// 计数器要到百万次 crypto-rand 失败后才可能超过六位；
	// 越过该点后审计写入直接失败，而不是存下一段读作手机号的数字。
	traceIDShape = regexp.MustCompile(`^([0-9a-f]{32}|req-[0-9]{1,6})$`)
	// kidShape 匹配凭据密钥 id：16 位小写十六进制。
	kidShape = regexp.MustCompile(`^[0-9a-f]{16}$`)
	// fieldShape 是列名或配置项名。
	fieldShape = regexp.MustCompile(`^[a-z][a-z0-9_]{0,63}$`)
	// wordShape 是状态或原因码。
	wordShape = regexp.MustCompile(`^[a-z][a-z0-9_]{0,31}$`)
)

var detailKeys = map[string]bool{
	"field":    true,
	"from":     true,
	"to":       true,
	"reason":   true,
	"count":    true,
	"kid":      true,
	"rows":     true,
	"trace_id": true,
}

// ZeroHash 是新链首行的 prev_hash：32 字节全零。
func ZeroHash() []byte { return make([]byte, sha256.Size) }

// EncodeDetail 校验 detail 值并返回规范 JSON 文本。
//
// 键白名单闭合：未知键即错误。值递归校验。
// 允许的类型只有 nil、string、bool、有符号与无符号整数、浮点数，
// 以及仅限 "rows" 键下的 []int。
// map、其他切片、结构体与非 nil 指针一律拒绝。
// encoding/json 对 map 键排序，同一 map 永远得到同一字节。
//
// 每个键有自己的形状。trace_id、kid、field、from、to 按固定字符类匹配，
// 故随机 32 位十六进制 trace id 通过，手机号不通过。
// 只有 reason 承载自由文本：它必须通过 mask 的检测口径
// （TextHasPhone/TextHasEmail；时间戳形态放行，见 R3-02），
// 不得含手机号或邮箱，且不超过 MaxReasonChars 个字符。
// 落在 [10000000000, 14999999999] 的数字按手机号形状拒绝；
// rows 出于同一理由只接受整数。
//
// 局限：密码与设备码没有可识别的形状，本函数无法识别。
// **调用方绝不可把秘密值放进 detail**。守卫是键白名单与代码评审，
// 两者都不得放松。
func EncodeDetail(values map[string]any) (string, error) {
	if len(values) == 0 {
		return "{}", nil
	}
	for key, value := range values {
		if !detailKeys[key] {
			return "", fmt.Errorf("%w: %q", ErrDetailKey, key)
		}
		if err := validateDetailValue(key, value); err != nil {
			return "", err
		}
	}
	encoded, err := json.Marshal(values)
	if err != nil {
		return "", fmt.Errorf("audit: encode detail: %w", err)
	}
	if len(encoded) > MaxDetailBytes {
		return "", fmt.Errorf("%w: %d bytes", ErrDetailTooLarge, len(encoded))
	}
	return string(encoded), nil
}

// validateDetailValue 对单个值应用类型白名单。
func validateDetailValue(key string, value any) error {
	switch typed := value.(type) {
	case nil:
		return nil
	case string:
		return validateDetailString(key, typed)
	case bool:
		return nil
	case []int:
		if key != "rows" {
			return fmt.Errorf("%w: %q must not be a slice", ErrDetailType, key)
		}
		for _, item := range typed {
			if err := validateDetailNumber(float64(item)); err != nil {
				return err
			}
		}
		return nil
	case int:
		return validateDetailNumber(float64(typed))
	case int8:
		return validateDetailNumber(float64(typed))
	case int16:
		return validateDetailNumber(float64(typed))
	case int32:
		return validateDetailNumber(float64(typed))
	case int64:
		return validateDetailNumber(float64(typed))
	case uint:
		return validateDetailNumber(float64(typed))
	case uint8:
		return validateDetailNumber(float64(typed))
	case uint16:
		return validateDetailNumber(float64(typed))
	case uint32:
		return validateDetailNumber(float64(typed))
	case uint64:
		return validateDetailNumber(float64(typed))
	case float32:
		return validateDetailNumber(float64(typed))
	case float64:
		return validateDetailNumber(typed)
	}
	// nil 指针编码为 null，不携带数据。
	if rv := reflect.ValueOf(value); rv.Kind() == reflect.Ptr && rv.IsNil() {
		return nil
	}
	return fmt.Errorf("%w: %q has type %T", ErrDetailType, key, value)
}

// validateDetailString 应用按键的形状规则或自由文本规则。
func validateDetailString(key, text string) error {
	switch key {
	case "trace_id":
		if !traceIDShape.MatchString(text) {
			return fmt.Errorf("%w: %q has the wrong trace id shape", ErrDetailShape, key)
		}
		return nil
	case "kid":
		if !kidShape.MatchString(text) {
			return fmt.Errorf("%w: %q has the wrong kid shape", ErrDetailShape, key)
		}
		return nil
	case "field":
		if !fieldShape.MatchString(text) {
			return fmt.Errorf("%w: %q has the wrong field shape", ErrDetailShape, key)
		}
		return nil
	case "from", "to":
		if !wordShape.MatchString(text) {
			return fmt.Errorf("%w: %q has the wrong value shape", ErrDetailShape, key)
		}
		return nil
	case "reason":
		if utf8.RuneCountInString(text) > MaxReasonChars {
			return fmt.Errorf("%w: %q has %d characters", ErrDetailReason, key, utf8.RuneCountInString(text))
		}
	default:
		return fmt.Errorf("%w: %q has no string rule", ErrDetailKey, key)
	}
	// PII 判定单一来源是 core/mask 的检测入口：
	// TextHasPhone（对齐窗口，1[3-9]）与 TextHasEmail。
	// 检测口径宁缺勿冤：时间戳形态不判为手机号（W2C-R2-02）。本文件不另设正则，避免两套口径漂移。
	if mask.TextHasPhone(text) || mask.TextHasEmail(text) {
		return fmt.Errorf("%w: %q", ErrDetailPII, key)
	}
	return nil
}

// validateDetailNumber 拒绝具备手机号形状的数字。
func validateDetailNumber(value float64) error {
	if value >= phoneNumberMin && value <= phoneNumberMax {
		return fmt.Errorf("%w: numeric phone shape", ErrDetailPII)
	}
	return nil
}

// Payload 构造 Hash 签名的字节。布局冻结：
//
//	"yiban.audit.v1\0"
//	field(prev_hash) field(ts) field(school_id) field(actor_user_id)
//	field(actor_role) field(actor_label) field(action) field(target_type)
//	field(target_id) field(detail)
//	field(x) = uint32_be(len(x)) || x
//
// **改布局即改全部历史哈希**；调整必须配链迁移方案。
func Payload(row Row) []byte {
	out := make([]byte, 0, 256)
	out = append(out, Domain...)
	out = append(out, 0)
	out = appendField(out, row.PrevHash)
	out = appendField(out, []byte(formatTime(row.TS)))
	out = appendField(out, []byte(row.SchoolID))
	out = appendField(out, []byte(row.ActorUserID))
	out = appendField(out, []byte(row.ActorRole))
	out = appendField(out, []byte(row.ActorLabel))
	out = appendField(out, []byte(row.Action))
	out = appendField(out, []byte(row.TargetType))
	out = appendField(out, []byte(row.TargetID))
	out = appendField(out, []byte(row.Detail))
	return out
}

// Hash 计算本行 payload 的 HMAC。
// 参与签名的只有 payload 字段；Hash 自身不在其内。
func Hash(key []byte, row Row) []byte {
	mac := hmac.New(sha256.New, key)
	mac.Write(Payload(row))
	return mac.Sum(nil)
}

// Verify 校验 rows 内每一行的哈希与链接连续性。
// 首行的 prev_hash 作为锚点，**不与任何值比较**：
// 清理之后，旧链合法地从非零锚点开始。
// 零行返回 ErrEmptyChain。
func Verify(key []byte, rows []Row) (Report, error) {
	if len(rows) == 0 {
		return Report{}, ErrEmptyChain
	}
	var prev []byte
	for i, row := range rows {
		if i > 0 && !hmac.Equal(prev, row.PrevHash) {
			return Report{}, fmt.Errorf("%w: row %d prev_hash does not match the previous hash", ErrBrokenChain, row.ID)
		}
		if !hmac.Equal(Hash(key, row), row.Hash) {
			return Report{}, fmt.Errorf("%w: row %d hash does not match its payload", ErrBrokenChain, row.ID)
		}
		prev = row.Hash
	}
	return Report{FirstID: rows[0].ID, LastID: rows[len(rows)-1].ID, Count: int64(len(rows))}, nil
}

func appendField(dst, value []byte) []byte {
	var size [4]byte
	binary.BigEndian.PutUint32(size[:], uint32(len(value)))
	dst = append(dst, size[:]...)
	return append(dst, value...)
}

// formatTime 固定时间戳形式。
// UTC、纳秒精度、尾随 Z，保证跨进程 payload 稳定。
func formatTime(ts time.Time) string {
	return ts.UTC().Format(time.RFC3339Nano)
}
