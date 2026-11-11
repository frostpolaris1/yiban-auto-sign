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
// Action 及全部动作常量；Entry、Row、Report、Status、RowSource；
// EncodeDetail、Payload、Hash、Verify、VerifyStream、ZeroHash；
// 错误值 ErrEmptyChain、ErrBrokenChain、ErrDetail*；
// 上限常量 MaxDetailBytes、MaxReasonChars、Domain。
//
// # 口径
// 摘要算法固定 HMAC-SHA256，密钥由调用方注入。
// 时间戳统一 UTC RFC3339Nano，跨进程字节稳定。
// detail 只允许白名单内的键（见 detailKeys），自由文本只有 reason，上限 200 字符。
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

// P1 动作词表。P4 补任务类与凭据状态类动作。
const (
	ActionAuthLogin       Action = "auth.login"
	ActionAuthLoginFailed Action = "auth.login_failed"
	// 危险操作二次口令失败：403（口令错/用户不可用）与 429（窗口内失败超限）
	// 共用本动作。detail 只记字段、状态词与失败计数，**绝无口令或邮箱**。
	ActionAuthReauthFailed  Action = "auth.reauth_failed"
	ActionAuthLogout        Action = "auth.logout"
	ActionAuthPasswordReset Action = "auth.password_reset"
	// 登录态自助改密：调用方已通过旧口令校验。
	ActionAuthPasswordChange Action = "auth.password_change"
	// 公开注册：建号、会话与审计同事务。detail 只记协议版本，不含邮箱。
	ActionAuthRegister Action = "auth.register"
	ActionUserCreate   Action = "user.create"
	ActionUserDisable  Action = "user.disable"
	ActionUserEnable   Action = "user.enable"
	ActionUserDelete   Action = "user.delete"
	// UX-1 用户管理动作。role_change 记角色迁移；
	// password_reset 记重置但**绝无口令值**；restore 记软删恢复。
	ActionUserRoleChange    Action = "user.role_change"
	ActionUserPasswordReset Action = "user.password_reset"
	ActionUserRestore       Action = "user.restore"
	// 物理清除已软删用户：detail 恒为空对象，不含邮箱。
	// 审计行不随对象删除，purge 行在对象行消失后仍是唯一事实。
	ActionUserPurge Action = "user.purge"
	// 自助面（5mx）：本人签到失败邮件开关。detail 只记字段与
	// 目标状态（on/off），**绝无邮箱或口令**。
	ActionUserMailNotify Action = "user.mail_notify"
	// 自助面（lac2）：本人注销登录账号与宽限期内撤销注销。
	// detail 只记 deleted_at 字段名，**绝无邮箱或口令**；连带软删的
	// 签到账号不再逐行写 account.* 审计（与管理员删用户同口径）。
	ActionUserSelfDelete  Action = "user.self_delete"
	ActionUserSelfRestore Action = "user.self_restore"
	ActionSchoolCreate    Action = "school.create"
	ActionSchoolUpdate    Action = "school.update"
	ActionSchoolDisable   Action = "school.disable"
	// 平台大学投影自动建校。detail 只含 university_id（平台院校标识），
	// 不含平台院校名、编码或任何用户凭据。
	ActionSchoolAutoCreate Action = "school.auto_create"
	ActionAdminGrant       Action = "admin.grant"
	ActionAdminRevoke      Action = "admin.revoke"
	// 全局设置写入：目前只有公开注册开关。
	// detail 只记字段与目标状态（enabled/disabled）。
	ActionSettingsRegistration Action = "settings.registration"
	// 全站公告写入或清空。detail 只记字段名，**绝无公告正文**。
	ActionSettingsAnnouncement Action = "settings.announcement"
	// 全局自动审批开关。detail 只记字段与目标状态（enabled/disabled）。
	ActionSettingsAutoApprove Action = "settings.auto_approve"
	// 设置页测试邮件。每次外呼都记，成功与失败同动作；
	// detail 只记结果与投递粗分类，**绝无** SMTP 应答、邮箱原文或口令。
	ActionSettingsMailTest Action = "settings.mail_test"
	// 设置页测试推送（webhook）。每次外呼都记，成功与失败同动作；
	// detail 只记结果与投递粗分类，**绝无**对端应答、状态码、URL 或密钥。
	ActionSettingsWebhookTest Action = "settings.webhook_test"
	// 调度设置写回（sett2）。一次 PUT 恰一行；detail 的 changes 是
	// 键→[旧,新]对：键是 settings 表键名，值只允许 nil/布尔/整数
	// （三键的域是 [0,300] 整数与 true/false 字面），无自由文本容量。
	ActionSettingsScheduleWriteback Action = "settings.schedule_writeback"
	// 全局执行暂停开关（3a）。一次 PUT 恰一行，幂等重放也记：
	// detail 只含字段与目标状态（paused/resumed），**绝无口令**；
	// 操作者在审计行主体列，不进 detail。
	ActionSettingsEnginePaused Action = "settings.engine_paused"
	// 配额写回（r4r）。一次 PUT 恰一行；detail 的 changes 是
	// 键→[旧,新]对：键是 settings 表键名（quota.max_users/
	// quota.max_accounts），值只允许 nil 或 [0,100000] 整数
	// （0=不限哨兵），无自由文本容量。
	ActionSettingsQuotaWriteback Action = "settings.quota_writeback"
	// SMTP 设置写回（smtp4）。一次 PUT 恰一行；detail 的 changes 是
	// 键→[旧,新]对：非敏感键（mail.smtp_host/mail.smtp_port/
	// mail.tls_mode）记字面值；敏感键（mail.from/mail.user/mail.pass）
	// 只记 set/cleared 状态词，**绝无明文或密文口令**。
	ActionSettingsMailWriteback Action = "settings.mail_writeback"
	ActionAccountCreate         Action = "account.create"
	ActionAccountUpdate         Action = "account.update"
	// 自动核验通过：pending→active，无人工审核者。detail 只记状态迁移。
	ActionAccountAutoApprove Action = "account.auto_approve"
	// 自动核验失败：账号留 pending。detail 只记结果词（如
	// identity_mismatch / platform_error），绝不记平台值或凭据。
	ActionAccountAutoCheckFailed Action = "account.auto_check_failed"
	// 被拒账号编辑或改绑手机号把账号送回 pending：重提进待审队列。
	// detail 只记状态迁移，不含手机号。
	ActionAccountResubmit     Action = "account.resubmit"
	ActionAccountReviewPass   Action = "account.review_approve"
	ActionAccountReviewReject Action = "account.review_reject"
	ActionAccountPause        Action = "account.pause"
	ActionAccountResume       Action = "account.resume"
	ActionAccountDelete       Action = "account.delete"
	ActionAccountRestore      Action = "account.restore"
	// 物理清除已软删账号：detail 恒为空对象，不含手机号。
	// 审计行不随对象删除，purge 行在对象行消失后仍是唯一事实。
	ActionAccountPurge Action = "account.purge"
	// 自助面（5mx）：本人注销与撤销注销。deleted_by 记本人；
	// detail 只记 deleted_at 字段名，不含手机号或邮箱。
	ActionAccountSelfDelete  Action = "account.self_delete"
	ActionAccountSelfRestore Action = "account.self_restore"
	// UX-2 设备刷新：只在平台读回成功且本事务提交时出现。
	// detail 只记 field=device_code，**绝无设备码全文或机型明文**。
	ActionAccountDeviceRefresh Action = "account.device_refresh"
	// 平台姓名懒加载揭示：只在平台认证成功且审计追加成功后响应姓名。
	// detail 只记 field=person_name，**绝无姓名本体**；揭示本身零业务写库。
	ActionAccountPersonNameReveal Action = "account.person_name_reveal"
	ActionCredentialUpdate        Action = "credential.update"
	ActionCredentialRekey         Action = "credential.rekey"
	// P4 引擎：明确登录失败把账号移出排期（active → credential_error）。
	ActionAccountCredentialError Action = "account.credential_error"
	// 口令或设备更新把 credential_error 账号放回排期（credential_error → active）。
	// 改绑手机号不走恢复：它回 pending 重审。
	ActionAccountCredentialRecovered Action = "account.credential_recovered"
	ActionTaskReclaim                Action = "task.reclaim"
	ActionTaskClaim                  Action = "task.claim"
	ActionTaskSettle                 Action = "task.settle"
	// 手动签到把当日 failed/skipped 行放回队列（C2）。detail 只记来源状态。
	ActionTaskRearm Action = "task.rearm"
	// P5-C：账号定位策略写入。detail 只记 mode 与 has_reason。
	ActionLocationSet Action = "location.set"
	// P5：图片入库。detail 只记四边裁切比例与输出摘要，
	// 无自由文本，无用户标识之外的主体信息。
	ActionPhotoUpload Action = "photo.upload"
	// P5：学校默认图登记。detail 只记新照片 id 与旧照片 id（可空）。
	ActionSchoolDefaultPhotoSet Action = "school.default_photo_set"
)

// Actor 是写审计行时的操作者。
// 定义在 core 而非用例层：适配器写"状态变更＋审计"同事务时
// 需要接收主体，而适配器不 import 用例层。
// Label 必须是掩码值；掩码口径见 app/audit.NewActor。
type Actor struct {
	UserID string
	Role   coreauth.Role
	Label  string
}

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

// LogRow 是只读列表使用的一行审计记录。
// 它不含 prev_hash/hash 与目标字段：列表不校验链、不导出。
// ActorLabel 是写入时固定的掩码标签，永不是原始地址。
type LogRow struct {
	ID         int64
	TS         time.Time
	Action     string
	ActorLabel string
	Detail     string
}

// LogQuery 是审计只读列表的已定型条件。Actor 是已掩码的标签。
// From 与 To 是闭区间时间界；nil 表示该侧不限。
type LogQuery struct {
	Action string
	Actor  string
	From   *time.Time
	To     *time.Time
	Cursor string
	Limit  int
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
// 扩充只随新动作同批进行，且必须带形状或限型：
// mode 是状态词（wordShape），has_reason 只接受布尔——
// 两者都无自由文本容量，不构成隐私面（P5-C location.set）。
var (
	// traceIDShape 匹配 entry/http/router 生成的 id：32 位十六进制，
	// 或 req-<计数> 的兜底形式（最多六位数字）。
	// 客户端自带的请求头不会到达审计层，故裸数字串（如手机号）在此被拒。
	// 计数器要到百万次 crypto-rand 失败后才可能超过六位；
	// 越过该点后审计写入直接失败，而不是存下一段读作手机号的数字。
	traceIDShape = regexp.MustCompile(`^([0-9a-f]{32}|req-[0-9]{1,6})$`)
	// kidShape 匹配凭据密钥 id：16 位小写十六进制。
	kidShape = regexp.MustCompile(`^[0-9a-f]{16}$`)
	// digestShape 匹配输出摘要：64 位小写十六进制（SHA-256 hex）。
	digestShape = regexp.MustCompile(`^[0-9a-f]{64}$`)
	// fieldShape 是列名或配置项名。
	fieldShape = regexp.MustCompile(`^[a-z][a-z0-9_]{0,63}$`)
	// wordShape 是状态或原因码。
	wordShape = regexp.MustCompile(`^[a-z][a-z0-9_]{0,31}$`)
	// universityIDShape 是平台院校标识：UUID 形状或纯数字/字母 ID，
	// 1..128 字符，无空格与自由文本标点。它是公开院校标识，不是 PII。
	universityIDShape = regexp.MustCompile(`^[0-9A-Za-z][0-9A-Za-z_-]{0,127}$`)
	// uuidShape 匹配小写 UUID。学校默认图只记照片 id。
	uuidShape = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)
	// settingsKeyShape 匹配 settings 表键名（对齐迁移 00021 的
	// ck_settings_key_shape：^[a-z][a-z0-9_.]*$，另加上限 63 字符）。
	// 只服务 settings.schedule_writeback / settings.quota_writeback 的 changes 键位。
	settingsKeyShape = regexp.MustCompile(`^[a-z][a-z0-9_.]{0,63}$`)
	// hostShape 匹配 SMTP 主机名或 IP 字面量：字母、数字、点、冒号、连字符。
	// 首字符允许冒号以容纳 IPv6 字面量（::1）；无空白、@、引号与路径分隔符，
	// 故不是自由文本通道。只服务 settings.mail_writeback 的 mail.smtp_host 字面值。
	hostShape = regexp.MustCompile(`^[A-Za-z0-9:][A-Za-z0-9.:-]{0,252}$`)
)

var detailKeys = map[string]bool{
	"field":      true,
	"from":       true,
	"to":         true,
	"reason":     true,
	"count":      true,
	"kid":        true,
	"rows":       true,
	"trace_id":   true,
	"mode":       true,
	"has_reason": true,
	// P5 图片入库审计：四边比例 0..10 整数；输出摘要 64 位小写十六进制。
	// 四者是数值与摘要，无自由文本容量，不构成隐私面。
	"crop_l": true,
	"crop_r": true,
	"crop_t": true,
	"crop_b": true,
	"sha256": true,
	// P5 学校默认图：新值与旧值都是照片 id 或 null。
	// UUID 形状无自由文本容量，不构成隐私面。
	"photo_id":          true,
	"previous_photo_id": true,
	// 注册时同意的协议版本：1..1000 的整数，无自由文本容量。
	"terms_version":   true,
	"privacy_version": true,
	// 测试邮件审计：result/kind 是状态词，retryable 是布尔。
	// 三者无自由文本容量，不构成隐私面。
	"result":    true,
	"kind":      true,
	"retryable": true,
	// 平台大学投影自动建校：值来自平台身份信封，是公开院校标识而非个人数据。
	"university_id": true,
	// 设置写回（sett2/3b/3c 调度、r4r 配额、smtp4 SMTP）：changes 是
	// 键→[旧,新]对。键限 settings 键形状；值限 nil/布尔/整数标量、
	// 状态词形状字符串（调度域 [0,300]/[0,1439]/true-false/front-uniform，
	// 配额域 [0,100000]，mail 敏感键 set/cleared），或 settings.mail_writeback
	// 的 mail.smtp_host 主机名形状。无自由文本容量，不构成隐私面。
	"changes": true,
}

// cropPercentKeys 是裁切比例 detail 键集。取值限 0..10 闭区间。
var cropPercentKeys = map[string]bool{
	"crop_l": true,
	"crop_r": true,
	"crop_t": true,
	"crop_b": true,
}

// versionKeys 是协议版本 detail 键集。取值限 1..1000：版本号是正整数，
// 上界挡住把任意大整数塞进链里的写法。
var versionKeys = map[string]bool{
	"terms_version":   true,
	"privacy_version": true,
}

// ZeroHash 是新链首行的 prev_hash：32 字节全零。
func ZeroHash() []byte { return make([]byte, sha256.Size) }

// EncodeDetail 校验 detail 值并返回规范 JSON 文本。
//
// 键白名单闭合：未知键即错误。值递归校验。
// 允许的类型只有 nil、string、bool、有符号与无符号整数、浮点数，
// 仅限 "rows" 键下的 []int，以及仅限 "changes" 键下的 map[string][2]any
// （键限 settings 键形状、元素限 nil/布尔/整数/状态词；sett2/3c/r4r 写回审计）。
// 其余 map、切片、结构体与非 nil 指针一律拒绝。
// encoding/json 对 map 键排序，同一 map 永远得到同一字节。
//
// 每个键有自己的形状。trace_id、kid、field、from、to 按固定字符类匹配，
// 故随机 32 位十六进制 trace id 通过，手机号不通过。
// 只有 reason 承载自由文本：它必须通过 mask 的检测口径
// （TextHasPhone/TextHasEmail；时间戳形态放行；
// 22 位以上的连续数字段按滑动窗口抓内嵌号码；12 至 21 位不判，已登记），
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
		return validateDetailNumberWithLimits(key, float64(typed))
	case int8:
		return validateDetailNumberWithLimits(key, float64(typed))
	case int16:
		return validateDetailNumberWithLimits(key, float64(typed))
	case int32:
		return validateDetailNumberWithLimits(key, float64(typed))
	case int64:
		return validateDetailNumberWithLimits(key, float64(typed))
	case uint:
		return validateDetailNumberWithLimits(key, float64(typed))
	case uint8:
		return validateDetailNumberWithLimits(key, float64(typed))
	case uint16:
		return validateDetailNumberWithLimits(key, float64(typed))
	case uint32:
		return validateDetailNumberWithLimits(key, float64(typed))
	case uint64:
		return validateDetailNumberWithLimits(key, float64(typed))
	case float32:
		return validateDetailNumberWithLimits(key, float64(typed))
	case float64:
		return validateDetailNumberWithLimits(key, typed)
	case map[string][2]any:
		// changes（sett2/3c/r4r）是唯一允许的键→[旧,新]对形态：
		// 键限 settings 键形状，元素限 nil/布尔/整数标量，无自由文本。
		// 3c：字符串枚举（schedule.sign_dist）只收状态词形状（wordShape）——
		// 数字串、大写、空白与邮箱/手机号形状都不通过，仍无自由文本容量。
		if key != "changes" {
			return fmt.Errorf("%w: %q must not be a map", ErrDetailType, key)
		}
		for name, pair := range typed {
			if !settingsKeyShape.MatchString(name) {
				return fmt.Errorf("%w: changes key %q is not a settings key shape", ErrDetailShape, name)
			}
			for _, item := range pair {
				switch value := item.(type) {
				case nil, bool, int, int64:
				case string:
					// 主机名形状只放行 mail.smtp_host 一个键位：
					// 其它 changes 键仍只收状态词，不因本批放宽。
					if !wordShape.MatchString(value) &&
						(name != "mail.smtp_host" || !hostShape.MatchString(value)) {
						return fmt.Errorf("%w: changes %q value has the wrong value shape", ErrDetailShape, name)
					}
				default:
					return fmt.Errorf("%w: changes %q value has type %T", ErrDetailType, name, item)
				}
			}
		}
		return nil
	}
	// nil 指针编码为 null，不携带数据。
	if rv := reflect.ValueOf(value); rv.Kind() == reflect.Pointer && rv.IsNil() {
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
	case "from", "to", "mode", "result", "kind":
		if !wordShape.MatchString(text) {
			return fmt.Errorf("%w: %q has the wrong value shape", ErrDetailShape, key)
		}
		return nil
	case "sha256":
		if !digestShape.MatchString(text) {
			return fmt.Errorf("%w: %q has the wrong digest shape", ErrDetailShape, key)
		}
		return nil
	case "photo_id", "previous_photo_id":
		if !uuidShape.MatchString(text) {
			return fmt.Errorf("%w: %q has the wrong uuid shape", ErrDetailShape, key)
		}
		return nil
	case "university_id":
		if !universityIDShape.MatchString(text) {
			return fmt.Errorf("%w: %q has the wrong university id shape", ErrDetailShape, key)
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
	// 检测口径宁缺勿冤：时间戳形态不判为手机号。本文件不另设正则，避免两套口径漂移。
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

// validateDetailNumberWithLimits 是数值族的公共入口：先按按键限幅（crop_* 限
// 0..10），再走通用数值规则。限幅挂键名而不是挂类型，同键异型不会绕过。
func validateDetailNumberWithLimits(key string, value float64) error {
	if cropPercentKeys[key] && (value < 0 || value > 10) {
		return fmt.Errorf("%w: %q must be within 0..10", ErrDetailShape, key)
	}
	if versionKeys[key] && (value < 1 || value > 1000) {
		return fmt.Errorf("%w: %q must be within 1..1000", ErrDetailShape, key)
	}
	return validateDetailNumber(value)
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

// Status 汇总一次分窗链校验的结论。
//
// Empty 为 true 表示链内零行：没有证明任何事，**不是通过**。
// BrokenAt 非零表示第一个不符行的行 id；CheckedRows 数到该行（含）。
// 其余情况是完整通过：CheckedRows 是总行数，LastHash 是最后一行存储哈希的副本。
type Status struct {
	CheckedRows int64
	LastHash    []byte
	BrokenAt    int64
	Empty       bool
}

// RowSource 按 id 升序返回 afterID 之后的一批行，至多 limit 行。
// 返回零行表示链已读完。游标必须严格前进。
type RowSource func(afterID int64, limit int) ([]Row, error)

// VerifyStream 分窗校验整条链。
//
// 每窗至多 batchSize 行，故内存占用是 O(batchSize)，不随链长增长。
// 窗与窗之间用上一窗末行的存储哈希作锚点：下一窗首行的 prev_hash
// 必须等于它。Verify 的单窗口径不变：窗首行的 prev_hash 只作窗内锚点。
//
// 空链返回 Status{Empty: true} 且 **err == nil**：空扫描不是通过，
// 但也不是断链。断链返回 Status{BrokenAt, CheckedRows} 且 err == nil：
// 巡检结论不是调用错误。只有数据源错误才上抛。
//
// 数据源必须让游标前进（后一窗首行 id 严格大于 afterID）且窗内 id 升序；
// 违反者是接线缺陷，返回错误而不是死循环。
// 扫描在读到不足一窗时结束：其后追加的行不在本次结论内，
// LastHash 是本次扫描到的链头，可作外部锚点。
func VerifyStream(key []byte, batchSize int, next RowSource) (Status, error) {
	if batchSize < 1 {
		return Status{}, fmt.Errorf("audit: batch size must be positive, got %d", batchSize)
	}
	if next == nil {
		return Status{}, errors.New("audit: nil row source")
	}
	var status Status
	var afterID int64
	var anchor []byte
	for {
		rows, err := next(afterID, batchSize)
		if err != nil {
			return Status{}, err
		}
		if len(rows) == 0 {
			status.Empty = status.CheckedRows == 0
			return status, nil
		}
		if len(rows) > batchSize {
			return Status{}, fmt.Errorf("audit: row source returned %d rows, want at most %d", len(rows), batchSize)
		}
		for i := 1; i < len(rows); i++ {
			if rows[i].ID <= rows[i-1].ID {
				return Status{}, fmt.Errorf("audit: row source returned ids out of order at %d", rows[i].ID)
			}
		}
		if rows[0].ID <= afterID {
			return Status{}, fmt.Errorf("audit: row source did not advance past %d", afterID)
		}
		if anchor != nil && !hmac.Equal(anchor, rows[0].PrevHash) {
			status.CheckedRows++
			status.BrokenAt = rows[0].ID
			return status, nil
		}
		if _, err := Verify(key, rows); err != nil {
			if errors.Is(err, ErrBrokenChain) {
				index := firstBrokenIndex(key, rows)
				// 断点之前同窗的行已通过复算：链头锚点取最后一行的哈希。
				if index > 0 {
					status.LastHash = append([]byte(nil), rows[index-1].Hash...)
				}
				status.CheckedRows += int64(index + 1)
				status.BrokenAt = rows[index].ID
				return status, nil
			}
			return Status{}, err
		}
		status.CheckedRows += int64(len(rows))
		last := append([]byte(nil), rows[len(rows)-1].Hash...)
		status.LastHash = last
		anchor = last
		afterID = rows[len(rows)-1].ID
		if len(rows) < batchSize {
			return status, nil
		}
	}
}

// firstBrokenIndex 返回窗前若干行里第一个不符行的下标。
// 调用方已确认整窗不是自洽的，故必然命中。
// 相邻两行组成一窗交给 Verify：首行的 prev_hash 是已验证过的窗内锚点，
// 复算仍会检查每一行的哈希与窗内链接，校验规则不另写一份。
func firstBrokenIndex(key []byte, rows []Row) int {
	if _, err := Verify(key, rows[:1]); err != nil {
		return 0
	}
	for i := 1; i < len(rows); i++ {
		if _, err := Verify(key, rows[i-1:i+1]); err != nil {
			return i
		}
	}
	return len(rows) - 1
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
