package pg_test

// 通知订阅、公告与审计只读列表的真库 e2e。
//
// 断言：覆盖集整体替换、缺行回落默认、公告清空删除行、
// 公告写入与审计行同事务、审计列表的筛选与游标边界。
// 本文件把 app 用例接到真实 PostgreSQL 适配器上（见 canonical boundary allow）。

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appnotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/notify"
	appsettings "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/settings"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corenotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/notify"
)

func requireValidation(t *testing.T, err error) {
	t.Helper()
	if err == nil {
		t.Fatal("want a validation error, got nil")
	}
	var appErr *errs.Error
	if !errors.As(err, &appErr) || appErr.Code != errs.CodeValidation {
		t.Fatalf("err = %v, want validation_failed", err)
	}
}

func notifyPrefs(page appnotify.Page) map[corenotify.Pair]bool {
	out := make(map[corenotify.Pair]bool, len(page.Items))
	for _, item := range page.Items {
		out[corenotify.Pair{Category: item.Category, Channel: item.Channel}] = item.Enabled
	}
	return out
}

// 缺行按默认解释；PUT 整体替换；空列表回到全默认。
func TestNotificationPrefsServiceE2E(t *testing.T) {
	e := newEnv(t)
	user := e.createUser("notify-svc@example.com")
	repo := pg.NewNotificationPrefsRepo(e.db)
	svc, err := appnotify.NewService(repo)
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}

	page, err := svc.Get(e.ctx, user.ID)
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	got := notifyPrefs(page)
	if len(got) != 8 {
		t.Fatalf("defaults = %d pairs, want 8", len(got))
	}
	if !got[corenotify.Pair{Category: corenotify.CategoryReviewPending, Channel: corenotify.ChannelEmail}] {
		t.Fatal("review_pending/email must default to true")
	}
	if got[corenotify.Pair{Category: corenotify.CategorySignFailedDaily, Channel: corenotify.ChannelInApp}] {
		t.Fatal("sign_failed_daily/in_app must default to false")
	}

	if _, err := svc.Set(e.ctx, user.ID, []corenotify.Pref{
		{Category: corenotify.CategoryReviewPending, Channel: corenotify.ChannelEmail, Enabled: false},
	}); err != nil {
		t.Fatalf("Set: %v", err)
	}
	if rows := e.scalarInt("SELECT count(*) FROM notification_prefs WHERE user_id = $1::uuid", user.ID); rows != 1 {
		t.Fatalf("stored rows = %d, want 1", rows)
	}
	page, err = svc.Get(e.ctx, user.ID)
	if err != nil {
		t.Fatalf("Get after Set: %v", err)
	}
	got = notifyPrefs(page)
	if got[corenotify.Pair{Category: corenotify.CategoryReviewPending, Channel: corenotify.ChannelEmail}] {
		t.Fatal("stored override false was not read back")
	}
	if !got[corenotify.Pair{Category: corenotify.CategoryCapacityRejected, Channel: corenotify.ChannelInApp}] {
		t.Fatal("unlisted pair did not fall back to its default")
	}

	if _, err := svc.Set(e.ctx, user.ID, []corenotify.Pref{}); err != nil {
		t.Fatalf("Set empty: %v", err)
	}
	if rows := e.scalarInt("SELECT count(*) FROM notification_prefs WHERE user_id = $1::uuid", user.ID); rows != 0 {
		t.Fatalf("stored rows after reset = %d, want 0", rows)
	}
	page, err = svc.Get(e.ctx, user.ID)
	if err != nil {
		t.Fatalf("Get after reset: %v", err)
	}
	if !notifyPrefs(page)[corenotify.Pair{Category: corenotify.CategoryReviewPending, Channel: corenotify.ChannelEmail}] {
		t.Fatal("reset did not restore defaults")
	}
}

// 存储里出现词表外的值必须报错，不得静默丢弃。
func TestNotificationPrefsRejectsCorruptRows(t *testing.T) {
	e := newEnv(t)
	user := e.createUser("notify-corrupt@example.com")
	repo := pg.NewNotificationPrefsRepo(e.db)
	if _, err := e.db.Exec(e.ctx, `
		INSERT INTO notification_prefs (user_id, category, channel, enabled)
		VALUES ($1::uuid, 'weekly_digest', 'in_app', true)`, user.ID); err != nil {
		t.Fatalf("insert corrupt row: %v", err)
	}
	if _, err := repo.Overrides(e.ctx, user.ID); err == nil {
		t.Fatal("unknown category in storage must fail closed")
	}
}

// 公告生命周期：缺失读空；写入 upsert；清空删行；updated_by 记录操作者。
func TestAnnouncementRepoE2E(t *testing.T) {
	e := newEnv(t)
	user := e.createUser("announcement@example.com")
	repo := pg.NewAnnouncementRepo(e.db)

	text, updatedAt, err := repo.Announcement(e.ctx)
	if err != nil {
		t.Fatalf("Announcement: %v", err)
	}
	if text != "" || updatedAt != nil {
		t.Fatalf("empty announcement = %q/%v", text, updatedAt)
	}
	if err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		_, err := repo.SetAnnouncement(ctx, "维护通知", user.ID)
		return err
	}); err != nil {
		t.Fatalf("SetAnnouncement: %v", err)
	}
	text, updatedAt, err = repo.Announcement(e.ctx)
	if err != nil {
		t.Fatalf("Announcement after set: %v", err)
	}
	if text != "维护通知" || updatedAt == nil {
		t.Fatalf("announcement = %q/%v", text, updatedAt)
	}
	if by := e.scalarString("SELECT updated_by::text FROM settings WHERE key = 'announcement.text'"); by != user.ID {
		t.Fatalf("updated_by = %q, want %q", by, user.ID)
	}

	if err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		_, err := repo.SetAnnouncement(ctx, "", user.ID)
		return err
	}); err != nil {
		t.Fatalf("clear: %v", err)
	}
	if rows := e.scalarInt("SELECT count(*) FROM settings WHERE key = 'announcement.text'"); rows != 0 {
		t.Fatalf("rows after clear = %d, want 0", rows)
	}
	text, updatedAt, err = repo.Announcement(e.ctx)
	if err != nil {
		t.Fatalf("Announcement after clear: %v", err)
	}
	if text != "" || updatedAt != nil {
		t.Fatalf("cleared announcement = %q/%v", text, updatedAt)
	}
}

// 公告写入与 settings.announcement 审计同事务；审计失败时公告行不落地。
func TestAnnouncementServiceE2E(t *testing.T) {
	e := newEnv(t)
	author := e.createUser("announcement-author@example.com")
	svc, err := appsettings.NewService(appsettings.Deps{
		Store: pg.NewAnnouncementRepo(e.db),
		Audit: e.audits,
		Tx:    e.db,
		Clock: coreclock.Fixed{T: time.Date(2026, 11, 4, 10, 0, 0, 0, time.UTC)},
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	actor := appaudit.Actor{UserID: author.ID, Role: coreauth.RoleMaster, Label: "a***@example.com"}

	announcement, err := svc.Set(e.ctx, "  全站维护  ", actor)
	if err != nil {
		t.Fatalf("Set: %v", err)
	}
	if announcement.Text != "全站维护" || announcement.UpdatedAt == nil {
		t.Fatalf("announcement = %+v", announcement)
	}
	if detail := e.scalarString(
		"SELECT detail FROM audit_logs WHERE action = 'settings.announcement' ORDER BY id DESC LIMIT 1"); detail != `{"field":"announcement"}` {
		t.Fatalf("audit detail = %q, want only the field name", detail)
	}
	if target := e.scalarString(
		"SELECT target_type || '/' || target_id FROM audit_logs WHERE action = 'settings.announcement' ORDER BY id DESC LIMIT 1"); target != "settings/announcement" {
		t.Fatalf("audit target = %q", target)
	}

	// 审计追加失败：公告写必须回滚。
	if _, err := e.db.Exec(e.ctx, `
		CREATE FUNCTION reject_audit() RETURNS trigger AS $$
		BEGIN
		    RAISE EXCEPTION 'audit rejected';
		END;
		$$ LANGUAGE plpgsql`); err != nil {
		t.Fatalf("create trigger function: %v", err)
	}
	if _, err := e.db.Exec(e.ctx, `
		CREATE TRIGGER reject_audit_insert BEFORE INSERT ON audit_logs
		FOR EACH ROW EXECUTE FUNCTION reject_audit()`); err != nil {
		t.Fatalf("create trigger: %v", err)
	}
	if _, err := svc.Set(e.ctx, "不应落地", actor); err == nil {
		t.Fatal("Set must fail when the audit append fails")
	}
	if value := e.scalarString(
		"SELECT value FROM settings WHERE key = 'announcement.text'"); value != "全站维护" {
		t.Fatalf("settings value = %q, want the pre-failure text", value)
	}
}

// appendAuditAt 以指定时刻与标签追加一行审计。
func appendAuditAt(t *testing.T, e *testEnv, ts time.Time, action coreaudit.Action, label string) coreaudit.Row {
	t.Helper()
	var row coreaudit.Row
	err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		var err error
		row, err = e.audits.Append(ctx, coreaudit.Entry{
			TS:         ts,
			ActorLabel: label,
			Action:     action,
			TargetType: "user",
			TargetID:   "target-1",
			Detail:     map[string]any{"field": "role"},
		})
		return err
	})
	if err != nil {
		t.Fatalf("append audit: %v", err)
	}
	return row
}

// 审计列表：最新在前、键集游标、action/actor/from/to 筛选。
func TestAuditLogListE2E(t *testing.T) {
	e := newEnv(t)
	base := time.Date(2026, 11, 4, 8, 0, 0, 0, time.UTC)
	specs := []struct {
		action coreaudit.Action
		label  string
	}{
		{coreaudit.ActionUserCreate, "a***@example.com"},
		{coreaudit.ActionAuthLogin, "b***@example.com"},
		{coreaudit.ActionUserCreate, "a***@example.com"},
		{coreaudit.ActionUserCreate, "a***@example.com"},
		{coreaudit.ActionAuthLogin, "a***@example.com"},
	}
	rows := make([]coreaudit.Row, 0, len(specs))
	for i, spec := range specs {
		rows = append(rows, appendAuditAt(t, e, base.Add(time.Duration(i)*time.Minute), spec.action, spec.label))
	}

	repo := e.audits
	first, next, err := repo.ListLogs(e.ctx, coreaudit.LogQuery{Limit: 2})
	if err != nil {
		t.Fatalf("ListLogs: %v", err)
	}
	if len(first) != 2 || next == "" {
		t.Fatalf("first page = %d rows, next = %q", len(first), next)
	}
	if first[0].ID != rows[4].ID || first[1].ID != rows[3].ID {
		t.Fatalf("first page ids = %d,%d, want %d,%d", first[0].ID, first[1].ID, rows[4].ID, rows[3].ID)
	}

	second, next2, err := repo.ListLogs(e.ctx, coreaudit.LogQuery{Limit: 2, Cursor: next})
	if err != nil {
		t.Fatalf("ListLogs second: %v", err)
	}
	if len(second) != 2 || second[0].ID != rows[2].ID || second[1].ID != rows[1].ID || next2 == "" {
		t.Fatalf("second page = %+v next=%q", second, next2)
	}
	third, next3, err := repo.ListLogs(e.ctx, coreaudit.LogQuery{Limit: 2, Cursor: next2})
	if err != nil {
		t.Fatalf("ListLogs third: %v", err)
	}
	if len(third) != 1 || third[0].ID != rows[0].ID || next3 != "" {
		t.Fatalf("third page = %+v next=%q", third, next3)
	}

	// action 精确匹配。
	matching, _, err := repo.ListLogs(e.ctx, coreaudit.LogQuery{Action: "auth.login", Limit: 50})
	if err != nil {
		t.Fatalf("ListLogs action: %v", err)
	}
	if len(matching) != 2 {
		t.Fatalf("auth.login rows = %d, want 2", len(matching))
	}
	// actor 精确匹配。
	byActor, _, err := repo.ListLogs(e.ctx, coreaudit.LogQuery{Actor: "a***@example.com", Limit: 50})
	if err != nil {
		t.Fatalf("ListLogs actor: %v", err)
	}
	if len(byActor) != 4 {
		t.Fatalf("actor rows = %d, want 4", len(byActor))
	}
	// from/to 闭区间。
	from := base.Add(time.Minute)
	to := base.Add(3 * time.Minute)
	bounded, _, err := repo.ListLogs(e.ctx, coreaudit.LogQuery{From: &from, To: &to, Limit: 50})
	if err != nil {
		t.Fatalf("ListLogs bounds: %v", err)
	}
	if len(bounded) != 3 {
		t.Fatalf("bounded rows = %d, want 3 (inclusive)", len(bounded))
	}

	// 负向：未知游标与越界 limit 都是 400。
	_, _, err = repo.ListLogs(e.ctx, coreaudit.LogQuery{Cursor: "not-a-cursor", Limit: 50})
	requireValidation(t, err)
	for _, limit := range []int{0, 201} {
		_, _, err = repo.ListLogs(e.ctx, coreaudit.LogQuery{Limit: limit})
		requireValidation(t, err)
	}
	// 未知 action 是空页，不是错误。
	empty, _, err := repo.ListLogs(e.ctx, coreaudit.LogQuery{Action: "auth.nope", Limit: 50})
	if err != nil {
		t.Fatalf("unknown action: %v", err)
	}
	if len(empty) != 0 {
		t.Fatalf("unknown action rows = %d, want 0", len(empty))
	}
	// detail 原样返回（脱敏在用例出口，见 app/audit 单测）。
	if !strings.Contains(first[0].Detail, `"field"`) {
		t.Fatalf("detail = %q", first[0].Detail)
	}
}
