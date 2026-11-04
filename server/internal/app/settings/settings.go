// Package settings 实现系统设置只读名册、全站公告与测试邮件用例（2ws A / c3e）。
//
// # 功能
// Roster 把配置名册按组展开为只读报告：非敏感键给解析值，
// 敏感键只给 {set, masked, source}，永不回显原文。
// Service 读写全站公告：正文单行、去首尾空白、上限 200 字符；
// 写入与 settings.announcement 审计同事务。
// Service 还向当前管理员自己的邮箱发一封测试信：复用 core/mail 端口，
// 同步投递，失败按粗分类记账，并按管理员冷却限频。
// 本包**不**读环境变量、**不**读名册文件、**不**写 SQL、**不**实现 SMTP。
//
// # 归属
// app 层用例。名册由 adapters/config 解析后注入；公告端口见本文件，
// adapters/pg 实现；邮件端口由 core/mail 声明，adapters/mail 实现。
// 入口层只做角色门与传输映射。
//
// # 复用
// Roster、Entry、Report、Group、Item；Service、Announcement、Store、Deps；
// Cooldown、TestMailCooldown；MaxAnnouncementChars、Source 常量。
//
// # 口径
// source 只报告能跟踪到的事实：环境提供即 env，名册登记缺省即 default，
// 两者都没有即 unknown。file 保留给文件层，本服务永不产出。
// 敏感键的 masked 是固定 "****"，不含原值任何字符。
// 测试邮件收件人只取会话用户邮箱，**不接受**请求体指定；
// 失败只记 core/mail 粗分类与掩码邮箱，SMTP 应答与口令不进响应和日志。
package settings

import (
	"context"
	"errors"
	"log/slog"
	"math"
	"sort"
	"strings"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/logfield"
	coremail "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mail"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mask"
)

// Source 是配置值的来源分类。
type Source string

const (
	// SourceEnv 表示进程环境提供该键。
	SourceEnv Source = "env"
	// SourceFile 表示文件配置层提供该键；本服务永不产出。
	SourceFile Source = "file"
	// SourceDefault 表示环境未提供且名册登记了缺省值。
	SourceDefault Source = "default"
	// SourceUnknown 表示环境未提供且名册没有缺省值。
	SourceUnknown Source = "unknown"
)

// maskedValue 是敏感键的固定掩码。它不含原值任何字符。
const maskedValue = "****"

// ValueSource 读取配置值的最小环境面。实现是 adapters/config.Env。
type ValueSource interface {
	Lookup(key string) (string, bool)
}

// Entry 是名册里的一行。
// Default 为 nil 且 HasDefault 为假表示该键没有缺省值；
// 缺省值显式为 null 也按「无缺省」处理（名册口径）。
type Entry struct {
	Key        string
	Group      string
	Sensitive  bool
	Default    any
	HasDefault bool
}

// Sensitive 是敏感键的对外视图。
type Sensitive struct {
	Set    bool
	Masked string
	Source Source
}

// Item 是一个键的对外视图。
// 敏感键 Value 为 nil 且 Sensitive 非空；非敏感键相反。
type Item struct {
	Key       string
	Group     string
	Source    Source
	Value     any
	Sensitive *Sensitive
}

// Group 是一组设置。
type Group struct {
	Name  string
	Items []Item
}

// Report 是全部分组的只读视图。分组与组内条目都按键名排序，顺序稳定。
type Report struct {
	Groups []Group
}

// Roster 是只读配置名册。
type Roster struct {
	entries []Entry
	source  ValueSource
}

// NewRoster 构造名册报告器。名册为空、键重复或来源缺失即失败：
// 空名册不得当绿。
func NewRoster(entries []Entry, source ValueSource) (*Roster, error) {
	if source == nil {
		return nil, errors.New("settings: nil value source")
	}
	if len(entries) == 0 {
		return nil, errors.New("settings: empty configuration roster")
	}
	seen := make(map[string]bool, len(entries))
	copied := make([]Entry, 0, len(entries))
	for _, entry := range entries {
		if entry.Key == "" {
			return nil, errors.New("settings: roster key is required")
		}
		if seen[entry.Key] {
			return nil, errors.New("settings: duplicate roster key " + entry.Key)
		}
		seen[entry.Key] = true
		copied = append(copied, entry)
	}
	sort.Slice(copied, func(i, j int) bool { return copied[i].Key < copied[j].Key })
	return &Roster{entries: copied, source: source}, nil
}

// Report 展开全部键。敏感键只给掩码视图。
func (r *Roster) Report() Report {
	byGroup := map[string][]Item{}
	for _, entry := range r.entries {
		item := r.item(entry)
		byGroup[entry.Group] = append(byGroup[entry.Group], item)
	}
	names := make([]string, 0, len(byGroup))
	for name := range byGroup {
		names = append(names, name)
	}
	sort.Strings(names)
	groups := make([]Group, 0, len(names))
	for _, name := range names {
		groups = append(groups, Group{Name: name, Items: byGroup[name]})
	}
	return Report{Groups: groups}
}

// item 解析单个键的来源与值。环境值为空串按未设置处理：
// 与 config.Load 的取值口径一致，避免空值冒充已设置。
func (r *Roster) item(entry Entry) Item {
	raw, provided := r.source.Lookup(entry.Key)
	set := provided && raw != ""
	source := SourceUnknown
	switch {
	case set:
		source = SourceEnv
	case entry.HasDefault:
		source = SourceDefault
	}
	item := Item{Key: entry.Key, Group: entry.Group, Source: source}
	if entry.Sensitive {
		masked := ""
		if set {
			masked = maskedValue
		}
		item.Sensitive = &Sensitive{Set: set, Masked: masked, Source: source}
		return item
	}
	if set {
		item.Value = raw
	} else if entry.HasDefault {
		item.Value = entry.Default
	}
	return item
}

// MaxAnnouncementChars 是公告正文的字符上限，与旧部署一致。
const MaxAnnouncementChars = 200

// announcementLineBreaks 是公告正文中拒绝的换行与行分隔符。
// 字符集对齐旧线 env_io.has_line_break（Python str.splitlines）：
// CR、LF、VT、FF、U+001C..U+001E、NEL、U+2028、U+2029。
// 它们都会在单行存储里变成新的物理行。
const announcementLineBreaks = "\r\n\v\f\u001c\u001d\u001e\u0085\u2028\u2029"

// Announcement 是全站公告。UpdatedAt 为 nil 表示从未设置或已清空。
type Announcement struct {
	Text      string
	UpdatedAt *time.Time
}

// Store 是公告的持久化端口。空正文在存储侧删除行，
// 故清空后读到的 updatedAt 是 nil。返回值是原始列，不构造用例类型。
type Store interface {
	Announcement(ctx context.Context) (text string, updatedAt *time.Time, err error)
	SetAnnouncement(ctx context.Context, text, updatedBy string) (updatedAt *time.Time, err error)
}

// Deps 是公告与测试邮件用例的依赖。
type Deps struct {
	Store Store
	Audit appaudit.Repo
	Tx    app.TxManager
	Clock coreclock.Clock
	// Mail 投递测试邮件。为 nil 表示 SMTP 关闭：
	// 测试邮件端点回 409，**不**静默成功。
	Mail coremail.Sender
	// Cooldown 给测试邮件加每管理员冷却。为 nil 表示不限流：
	// 只用于测试与降级进程。实现是 entry/http/ratelimit.Throttle。
	Cooldown Cooldown
	// Logger 可选。缺省用 slog.Default。
	Logger *slog.Logger
}

// Cooldown 是按键的发送冷却端口。
// Allow 报告 key 本次是否可发送；被拒时给出建议等待。
type Cooldown interface {
	Allow(key string) (bool, time.Duration)
}

// Service 是公告与测试邮件用例服务。
type Service struct {
	deps Deps
	log  *slog.Logger
}

// NewService 构造公告服务。缺依赖即失败。
func NewService(deps Deps) (*Service, error) {
	if deps.Store == nil {
		return nil, errors.New("settings: nil announcement store")
	}
	if deps.Audit == nil {
		return nil, errors.New("settings: nil audit repo")
	}
	if deps.Tx == nil {
		return nil, errors.New("settings: nil transaction manager")
	}
	if deps.Clock == nil {
		return nil, errors.New("settings: nil clock")
	}
	logger := deps.Logger
	if logger == nil {
		logger = slog.Default()
	}
	return &Service{deps: deps, log: logger}, nil
}

// Get 返回当前公告。未设置或已清空时 Text 为空、UpdatedAt 为 nil。
func (s *Service) Get(ctx context.Context) (Announcement, error) {
	text, updatedAt, err := s.deps.Store.Announcement(ctx)
	if err != nil {
		return Announcement{}, err
	}
	return Announcement{Text: text, UpdatedAt: updatedAt}, nil
}

// Set 发布或清空公告。
// 正文去首尾空白后必须单行且不超过 200 字符；空正文即清空。
// 写入与 settings.announcement 审计同事务；审计 detail 只有字段名。
func (s *Service) Set(ctx context.Context, raw string, actor appaudit.Actor) (Announcement, error) {
	text := strings.TrimSpace(raw)
	if strings.ContainsAny(text, announcementLineBreaks) {
		return Announcement{}, coreerrs.New(coreerrs.CodeValidation, "announcement must be a single line")
	}
	if len([]rune(text)) > MaxAnnouncementChars {
		return Announcement{}, coreerrs.New(coreerrs.CodeValidation,
			"announcement must be at most 200 characters")
	}
	var announcement Announcement
	err := s.deps.Tx.InTx(ctx, func(ctx context.Context) error {
		updatedAt, err := s.deps.Store.SetAnnouncement(ctx, text, actor.UserID)
		if err != nil {
			return err
		}
		announcement = Announcement{Text: text, UpdatedAt: updatedAt}
		_, err = s.deps.Audit.Append(ctx, coreaudit.Entry{
			TS:          s.deps.Clock.Now().UTC(),
			ActorUserID: actor.UserID,
			ActorRole:   actor.Role,
			ActorLabel:  actor.Label,
			Action:      coreaudit.ActionSettingsAnnouncement,
			TargetType:  "settings",
			TargetID:    "announcement",
			Detail:      map[string]any{"field": "announcement"},
		})
		return err
	})
	if err != nil {
		return Announcement{}, err
	}
	return announcement, nil
}

// TestMailCooldown 是同一管理员两次测试邮件之间的最小间隔。
// 入口层用它给 429 的 Retry-After 兜底；真正的判定来自 Cooldown 端口。
const TestMailCooldown = time.Minute

const (
	// testMailSubject 是测试邮件的主题。
	testMailSubject = "测试邮件"
	// testMailBody 是测试邮件的纯文本正文。
	testMailBody = "这是一封来自 Yiban Auto Sign 的测试邮件，用来验证当前 SMTP 配置可以发信。\n\n" +
		"如果你没有请求发送测试邮件，可以忽略本邮件。\n"
	// testMailCooldownKeyPrefix 让测试邮件冷却与登录等账号限流共用桶实现时
	// 仍落在独立的键前缀上：同一管理员 id 在不同限流面互不占用额度。
	testMailCooldownKeyPrefix = "admin-mail-test:"
)

// SendTestMail 向 user 自己的邮箱发一封测试信。
//
// 收件人只取会话用户邮箱，**不接受**调用方指定地址。
// 同步投递：返回 nil 表示 SMTP 服务器已接收（DATA 已 ACK），
// 不代表邮箱已读；失败回 upstream_unavailable，错误文本不含 SMTP 应答。
// SMTP 关闭/未配置回 conflict（与无邮箱同状态、文案不同）；
// 会话邮箱为空回 conflict；冷却内重复请求回 rate_limited，
// details 带 retry_after_seconds。
// 不写审计：审计词表是安全动作的闭集，投递结果走结构化日志。
func (s *Service) SendTestMail(ctx context.Context, user coreauth.User) error {
	to := strings.TrimSpace(user.Email)
	if to == "" {
		return coreerrs.New(coreerrs.CodeConflict, "signed-in account has no email address")
	}
	if s.deps.Mail == nil {
		return coreerrs.New(coreerrs.CodeConflict, "mail delivery is disabled")
	}
	if s.deps.Cooldown != nil {
		ok, wait := s.deps.Cooldown.Allow(testMailCooldownKeyPrefix + user.ID)
		if !ok {
			seconds := int(math.Ceil(wait.Seconds()))
			if seconds < 1 {
				seconds = 1
			}
			return &coreerrs.Error{
				Code:    coreerrs.CodeRateLimited,
				Message: "a test email was sent recently; retry later",
				Details: map[string]any{"retry_after_seconds": seconds},
			}
		}
	}
	msg := coremail.Message{To: to, Subject: testMailSubject, Text: testMailBody}
	if err := s.deps.Mail.Send(ctx, msg); err != nil {
		kind, retryable := coremail.Classify(err)
		attrs := []any{
			logfield.KeyEvent, "mail_test.delivery_failed",
			logfield.KeyKind, string(kind),
			logfield.KeyRetryable, retryable,
			logfield.KeyEmail, mask.EmailLabel(to),
		}
		// 地址不可表达是配置类失败里最需要一眼区分的一种：
		// 重试与改凭据都无用，只能改账号邮箱。
		if errors.Is(err, coremail.ErrUnsupportedAddress) {
			attrs = append(attrs, logfield.KeyReason, "unsupported_address")
		}
		s.log.Warn("test mail delivery failed", attrs...)
		return coreerrs.New(coreerrs.CodeUpstream, "test email delivery failed")
	}
	s.log.Info("test mail delivered",
		logfield.KeyEvent, "mail_test.delivered",
		logfield.KeyEmail, mask.EmailLabel(to))
	return nil
}
