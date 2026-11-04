package settings_test

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appsettings "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/settings"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coremail "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mail"
)

// mapSource 是固定环境面。空串按未设置处理，与 config.Load 一致。
type mapSource map[string]string

func (m mapSource) Lookup(key string) (string, bool) {
	value, ok := m[key]
	return value, ok
}

// fakeStore 在内存里保存公告行。
type fakeStore struct {
	text      string
	updatedAt *time.Time
	by        string
	writes    int
	failWith  error
}

func (f *fakeStore) Announcement(context.Context) (string, *time.Time, error) {
	if f.failWith != nil {
		return "", nil, f.failWith
	}
	return f.text, f.updatedAt, nil
}

func (f *fakeStore) SetAnnouncement(_ context.Context, text, updatedBy string) (*time.Time, error) {
	if f.failWith != nil {
		return nil, f.failWith
	}
	f.writes++
	f.by = updatedBy
	if text == "" {
		f.text = ""
		f.updatedAt = nil
		return nil, nil
	}
	now := time.Date(2026, 11, 4, 12, 0, 0, 0, time.UTC)
	f.text = text
	f.updatedAt = &now
	return &now, nil
}

// fakeAudit 记录追加的条目。
type fakeAudit struct {
	entries []coreaudit.Entry
	err     error
}

func (f *fakeAudit) Append(_ context.Context, in coreaudit.Entry) (coreaudit.Row, error) {
	if f.err != nil {
		return coreaudit.Row{}, f.err
	}
	f.entries = append(f.entries, in)
	return coreaudit.Row{}, nil
}

func (f *fakeAudit) Verify(context.Context, int64, int64) (coreaudit.Report, error) {
	return coreaudit.Report{}, nil
}

// fakeTx 直接运行回调，并记录调用次数。
type fakeTx struct{ calls int }

func (f *fakeTx) InTx(ctx context.Context, fn func(context.Context) error) error {
	f.calls++
	return fn(ctx)
}

type fixedClock struct{}

func (fixedClock) Now() time.Time { return time.Date(2026, 11, 4, 9, 0, 0, 0, time.UTC) }

func newAnnouncementService(t *testing.T, store appsettings.Store, audits *fakeAudit) (*appsettings.Service, *fakeTx) {
	t.Helper()
	tx := &fakeTx{}
	svc, err := appsettings.NewService(appsettings.Deps{
		Store: store, Audit: audits, Tx: tx, Clock: fixedClock{},
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc, tx
}

// 名册按组返回、顺序稳定；环境值原样给出，缺省值按名册类型给出。
func TestRosterReportsValuesAndSources(t *testing.T) {
	roster, err := appsettings.NewRoster([]appsettings.Entry{
		{Key: "B_KEY", Group: "乙", Default: "fallback", HasDefault: true},
		{Key: "A_KEY", Group: "甲", Default: true, HasDefault: true},
		{Key: "C_KEY", Group: "甲"}, // 无缺省、环境未设置
	}, mapSource{"B_KEY": "from-env"})
	if err != nil {
		t.Fatalf("NewRoster: %v", err)
	}
	report := roster.Report()
	if len(report.Groups) != 2 || report.Groups[0].Name != "乙" || report.Groups[1].Name != "甲" {
		t.Fatalf("group order = %+v, want 乙 then 甲", report.Groups)
	}
	items := map[string]appsettings.Item{}
	for _, group := range report.Groups {
		for _, item := range group.Items {
			items[item.Key] = item
		}
	}
	if got := items["B_KEY"]; got.Value != "from-env" || got.Source != appsettings.SourceEnv {
		t.Fatalf("B_KEY = %+v, want env value", got)
	}
	if got := items["A_KEY"]; got.Value != true || got.Source != appsettings.SourceDefault {
		t.Fatalf("A_KEY = %+v, want default true", got)
	}
	if got := items["C_KEY"]; got.Value != nil || got.Source != appsettings.SourceUnknown {
		t.Fatalf("C_KEY = %+v, want unknown/null", got)
	}
}

// 环境值为空串按未设置处理：空值不得冒充 env 来源。
func TestRosterTreatsEmptyEnvAsUnset(t *testing.T) {
	roster, err := appsettings.NewRoster([]appsettings.Entry{
		{Key: "K", Group: "组", Default: "d", HasDefault: true},
	}, mapSource{"K": ""})
	if err != nil {
		t.Fatalf("NewRoster: %v", err)
	}
	item := roster.Report().Groups[0].Items[0]
	if item.Source != appsettings.SourceDefault || item.Value != "d" {
		t.Fatalf("item = %+v, want default", item)
	}
}

// 敏感键只给固定掩码与来源，绝不回显原值。
func TestRosterNeverEchoesSensitiveValues(t *testing.T) {
	const secret = "super-secret-token"
	roster, err := appsettings.NewRoster([]appsettings.Entry{
		{Key: "SECRET", Group: "安全", Sensitive: true},
		{Key: "SECRET_DEFAULT", Group: "安全", Sensitive: true, Default: "fallback-secret", HasDefault: true},
	}, mapSource{"SECRET": secret})
	if err != nil {
		t.Fatalf("NewRoster: %v", err)
	}
	report := roster.Report()
	seen := 0
	for _, group := range report.Groups {
		for _, item := range group.Items {
			if strings.Contains(item.Key, "SECRET") {
				seen++
				if item.Value != nil {
					t.Fatalf("%s carries a value %v; sensitive values must stay hidden", item.Key, item.Value)
				}
				if item.Sensitive == nil {
					t.Fatalf("%s lacks the sensitive view", item.Key)
				}
				if strings.Contains(item.Sensitive.Masked, secret) ||
					strings.Contains(item.Sensitive.Masked, "fallback-secret") {
					t.Fatalf("%s masked = %q leaks the stored value", item.Key, item.Sensitive.Masked)
				}
			}
		}
	}
	if seen != 2 {
		t.Fatalf("sensitive items = %d, want 2", seen)
	}
	first := report.Groups[0].Items[0]
	if first.Key != "SECRET" || !first.Sensitive.Set || first.Sensitive.Masked != "****" ||
		first.Sensitive.Source != appsettings.SourceEnv {
		t.Fatalf("set sensitive = %+v", first.Sensitive)
	}
	second := report.Groups[0].Items[1]
	if second.Key != "SECRET_DEFAULT" || second.Sensitive.Set || second.Sensitive.Masked != "" ||
		second.Sensitive.Source != appsettings.SourceDefault {
		t.Fatalf("default sensitive = %+v", second.Sensitive)
	}
}

// 空名册、重复键、空键都必须失败：扫空不得当绿。
func TestRosterRejectsInvalidRosters(t *testing.T) {
	if _, err := appsettings.NewRoster(nil, mapSource{}); err == nil {
		t.Fatal("empty roster must fail")
	}
	if _, err := appsettings.NewRoster(nil, nil); err == nil {
		t.Fatal("nil source must fail")
	}
	if _, err := appsettings.NewRoster([]appsettings.Entry{{Key: ""}}, mapSource{}); err == nil {
		t.Fatal("empty key must fail")
	}
	if _, err := appsettings.NewRoster([]appsettings.Entry{
		{Key: "K"}, {Key: "K"},
	}, mapSource{}); err == nil {
		t.Fatal("duplicate key must fail")
	}
}

var announcementActor = appaudit.Actor{
	UserID: "master-1",
	Role:   coreauth.RoleMaster,
	Label:  "m***@example.com",
}

// 发布公告：去首尾空白、写存储、追加 settings.announcement 且 detail 不含正文。
func TestAnnouncementSetWritesAndAudits(t *testing.T) {
	store := &fakeStore{}
	audits := &fakeAudit{}
	svc, tx := newAnnouncementService(t, store, audits)
	const body = "  本周六维护  "
	announcement, err := svc.Set(context.Background(), body, announcementActor)
	if err != nil {
		t.Fatalf("Set: %v", err)
	}
	if announcement.Text != "本周六维护" {
		t.Fatalf("text = %q, want trimmed", announcement.Text)
	}
	if store.text != "本周六维护" || store.by != "master-1" || store.writes != 1 {
		t.Fatalf("store = %+v", store)
	}
	if tx.calls != 1 {
		t.Fatalf("tx calls = %d, want 1 (write and audit share one transaction)", tx.calls)
	}
	if len(audits.entries) != 1 {
		t.Fatalf("audit entries = %d, want 1", len(audits.entries))
	}
	entry := audits.entries[0]
	if entry.Action != coreaudit.ActionSettingsAnnouncement {
		t.Fatalf("action = %q", entry.Action)
	}
	if entry.TargetType != "settings" || entry.TargetID != "announcement" {
		t.Fatalf("target = %s/%s", entry.TargetType, entry.TargetID)
	}
	if len(entry.Detail) != 1 || entry.Detail["field"] != "announcement" {
		t.Fatalf("detail = %v, want only the field name", entry.Detail)
	}
	for _, value := range entry.Detail {
		if text, ok := value.(string); ok && strings.Contains(text, body) {
			t.Fatalf("detail leaks the announcement text: %v", entry.Detail)
		}
	}
}

// 空正文清空公告：写空、时间归 nil、仍记审计。
func TestAnnouncementSetEmptyClears(t *testing.T) {
	store := &fakeStore{text: "旧公告"}
	audits := &fakeAudit{}
	svc, _ := newAnnouncementService(t, store, audits)
	announcement, err := svc.Set(context.Background(), "   ", announcementActor)
	if err != nil {
		t.Fatalf("Set: %v", err)
	}
	if announcement.Text != "" || announcement.UpdatedAt != nil {
		t.Fatalf("announcement = %+v, want cleared", announcement)
	}
	if store.text != "" || store.updatedAt != nil {
		t.Fatalf("store = %+v, want deleted", store)
	}
	if len(audits.entries) != 1 {
		t.Fatalf("audit entries = %d, want 1", len(audits.entries))
	}
}

// 负向：换行、行分隔符与超长正文都是 400，且不写存储、不写审计。
func TestAnnouncementRejectsInvalidText(t *testing.T) {
	cases := map[string]string{
		"line feed":      "第一行\n第二行",
		"carriage":       "第一行\r第二行",
		"vertical tab":   "第一行\v第二行",
		"form feed":      "第一行\f第二行",
		"file separator": "第一行\u001c第二行",
		"line separator": "第一行\u2028第二行",
		"paragraph":      "第一行\u2029第二行",
		"nel":            "第一行\u0085第二行",
		"too long":       strings.Repeat("字", 201),
	}
	for name, text := range cases {
		t.Run(name, func(t *testing.T) {
			store := &fakeStore{}
			audits := &fakeAudit{}
			svc, tx := newAnnouncementService(t, store, audits)
			_, err := svc.Set(context.Background(), text, announcementActor)
			if err == nil {
				t.Fatal("Set must fail")
			}
			var appErr *errs.Error
			if !errors.As(err, &appErr) || appErr.Code != errs.CodeValidation {
				t.Fatalf("error = %v, want validation_failed", err)
			}
			if store.writes != 0 || tx.calls != 0 || len(audits.entries) != 0 {
				t.Fatalf("invalid text reached storage: store=%+v tx=%d audit=%d",
					store, tx.calls, len(audits.entries))
			}
		})
	}
}

// 200 字符是闭区间上界：200 通过，201 已被上一条钉住。
func TestAnnouncementAcceptsMaxLength(t *testing.T) {
	store := &fakeStore{}
	audits := &fakeAudit{}
	svc, _ := newAnnouncementService(t, store, audits)
	text := strings.Repeat("字", appsettings.MaxAnnouncementChars)
	if _, err := svc.Set(context.Background(), text, announcementActor); err != nil {
		t.Fatalf("Set at max length: %v", err)
	}
}

// 审计失败必须回滚公告写入：两者同事务。
func TestAnnouncementAuditFailurePropagates(t *testing.T) {
	boom := errors.New("audit down")
	store := &fakeStore{}
	audits := &fakeAudit{err: boom}
	svc, _ := newAnnouncementService(t, store, audits)
	if _, err := svc.Set(context.Background(), "正文", announcementActor); !errors.Is(err, boom) {
		t.Fatalf("err = %v, want audit failure", err)
	}
}

// Get 透传存储状态。
func TestAnnouncementGet(t *testing.T) {
	now := time.Date(2026, 11, 4, 8, 0, 0, 0, time.UTC)
	store := &fakeStore{text: "公告", updatedAt: &now}
	svc, _ := newAnnouncementService(t, store, &fakeAudit{})
	announcement, err := svc.Get(context.Background())
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	if announcement.Text != "公告" || announcement.UpdatedAt == nil || !announcement.UpdatedAt.Equal(now) {
		t.Fatalf("announcement = %+v", announcement)
	}
}

// fakeMail 记录最后一次投递，并按需返回错误。
type fakeMail struct {
	msg   coremail.Message
	err   error
	sends int
}

func (f *fakeMail) Send(_ context.Context, msg coremail.Message) error {
	f.sends++
	f.msg = msg
	return f.err
}

// fakeCooldown 是固定判据的冷却替身。
type fakeCooldown struct {
	allowed bool
	wait    time.Duration
	keys    []string
}

func (f *fakeCooldown) Allow(key string) (bool, time.Duration) {
	f.keys = append(f.keys, key)
	return f.allowed, f.wait
}

func newMailService(t *testing.T, sender coremail.Sender, cooldown appsettings.Cooldown) *appsettings.Service {
	t.Helper()
	svc, err := appsettings.NewService(appsettings.Deps{
		Store: &fakeStore{}, Audit: &fakeAudit{}, Tx: &fakeTx{}, Clock: fixedClock{},
		Mail: sender, Cooldown: cooldown,
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc
}

// 收件人只取会话邮箱：签名里没有第二个地址来源。
func TestSendTestMailUsesSessionAddress(t *testing.T) {
	sender := &fakeMail{}
	cooldown := &fakeCooldown{allowed: true}
	svc := newMailService(t, sender, cooldown)
	user := coreauth.User{ID: "master-1", Email: "  owner@example.com  "}
	if err := svc.SendTestMail(context.Background(), user); err != nil {
		t.Fatalf("SendTestMail: %v", err)
	}
	if sender.sends != 1 {
		t.Fatalf("sends = %d, want 1", sender.sends)
	}
	if sender.msg.To != "owner@example.com" {
		t.Fatalf("recipient = %q, want trimmed session address", sender.msg.To)
	}
	if sender.msg.Subject == "" || sender.msg.Text == "" {
		t.Fatalf("message = %+v, want subject and body", sender.msg)
	}
	if len(cooldown.keys) != 1 || cooldown.keys[0] != "admin-mail-test:master-1" {
		t.Fatalf("cooldown keys = %v", cooldown.keys)
	}
}

// 会话邮箱为空：409，且不触达发送器。
func TestSendTestMailRejectsMissingMailbox(t *testing.T) {
	for _, email := range []string{"", "   "} {
		sender := &fakeMail{}
		svc := newMailService(t, sender, nil)
		err := svc.SendTestMail(context.Background(), coreauth.User{ID: "master-1", Email: email})
		if code := errs.From(err).Code; code != errs.CodeConflict {
			t.Fatalf("email %q: code = %s, want conflict", email, code)
		}
		if sender.sends != 0 {
			t.Fatal("a mailbox-less account reached the sender")
		}
	}
}

// SMTP 关闭：409，不静默成功。
func TestSendTestMailDisabled(t *testing.T) {
	svc := newMailService(t, nil, nil)
	err := svc.SendTestMail(context.Background(), coreauth.User{ID: "master-1", Email: "owner@example.com"})
	if code := errs.From(err).Code; code != errs.CodeConflict {
		t.Fatalf("code = %s, want conflict", code)
	}
}

// 投递失败：502，错误文本不含 SMTP 应答或口令。
func TestSendTestMailDeliveryFailureHidesTransportDetail(t *testing.T) {
	const secret = "smtp-secret-9f21"
	sender := &fakeMail{err: &coremail.DeliveryError{
		Kind: coremail.KindAuth, Err: errors.New("535 auth failed: " + secret),
	}}
	svc := newMailService(t, sender, nil)
	err := svc.SendTestMail(context.Background(), coreauth.User{ID: "master-1", Email: "owner@example.com"})
	if code := errs.From(err).Code; code != errs.CodeUpstream {
		t.Fatalf("code = %s, want upstream_unavailable", code)
	}
	if strings.Contains(err.Error(), secret) || strings.Contains(err.Error(), "535") {
		t.Fatalf("error text leaks transport detail: %v", err)
	}
}

// 冷却内的请求：429，带 retry_after_seconds，且不触达发送器。
func TestSendTestMailCooldown(t *testing.T) {
	sender := &fakeMail{}
	cooldown := &fakeCooldown{allowed: false, wait: 42 * time.Second}
	svc := newMailService(t, sender, cooldown)
	err := svc.SendTestMail(context.Background(), coreauth.User{ID: "master-1", Email: "owner@example.com"})
	if code := errs.From(err).Code; code != errs.CodeRateLimited {
		t.Fatalf("code = %s, want rate_limited", code)
	}
	var stable *errs.Error
	if !errors.As(err, &stable) {
		t.Fatalf("err = %T, want *errs.Error", err)
	}
	if got := stable.Details["retry_after_seconds"]; got != 42 {
		t.Fatalf("retry_after_seconds = %v, want 42", got)
	}
	if sender.sends != 0 {
		t.Fatal("a throttled request reached the sender")
	}
}
