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
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
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
	if len(got) != 12 {
		t.Fatalf("defaults = %d pairs, want 12", len(got))
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
	// 列表的排序与 actor 过滤必须有索引：缺索引时每页全表排序。
	for _, index := range []string{"idx_audit_ts_id", "idx_audit_actor_label_ts"} {
		if n := e.scalarInt(
			"SELECT count(*) FROM pg_indexes WHERE schemaname = current_schema() AND indexname = $1", index); n != 1 {
			t.Fatalf("index %s missing (count=%d)", index, n)
		}
	}
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

// sett2 写回访问器：存在性感知读取、int/bool 读写对、删行与坏行判定。
// 坏行（手工 SQL 写坏）必须报校验错而不是「缺行」：用例层据此回退 env 并出声。
func TestSettingsWritebackAccessorE2E(t *testing.T) {
	e := newEnv(t)
	user := e.createUser("settings-writeback@example.com")
	repo := pg.NewSettingsRepo(e.db)
	const edgeKey = "schedule.edge_front_sec"
	const boolKey = "schedule.allow_time_pref"

	// 缺行：found=false，零错误。
	if value, found, err := repo.IntOverride(e.ctx, edgeKey); err != nil || found || value != 0 {
		t.Fatalf("missing int row = (%d, %v, %v)", value, found, err)
	}
	if value, found, err := repo.BoolOverride(e.ctx, boolKey); err != nil || found || value {
		t.Fatalf("missing bool row = (%v, %v, %v)", value, found, err)
	}

	// upsert 写读回环：updated_by 记录操作者，updated_at 由触发器维护。
	if err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		return repo.SetInt(ctx, edgeKey, 120, user.ID)
	}); err != nil {
		t.Fatalf("SetInt: %v", err)
	}
	if value, found, err := repo.IntOverride(e.ctx, edgeKey); err != nil || !found || value != 120 {
		t.Fatalf("int roundtrip = (%d, %v, %v)", value, found, err)
	}
	if by := e.scalarString("SELECT updated_by::text FROM settings WHERE key = $1", edgeKey); by != user.ID {
		t.Fatalf("updated_by = %q, want %q", by, user.ID)
	}
	if err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		return repo.SetBool(ctx, boolKey, true, user.ID)
	}); err != nil {
		t.Fatalf("SetBool: %v", err)
	}
	if value, found, err := repo.BoolOverride(e.ctx, boolKey); err != nil || !found || !value {
		t.Fatalf("bool roundtrip = (%v, %v, %v)", value, found, err)
	}

	// 二次写是 upsert 更新，不是第二行。
	if err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		return repo.SetInt(ctx, edgeKey, 90, user.ID)
	}); err != nil {
		t.Fatalf("SetInt again: %v", err)
	}
	if rows := e.scalarInt("SELECT count(*) FROM settings WHERE key = $1", edgeKey); rows != 1 {
		t.Fatalf("rows after rewrite = %d, want 1", rows)
	}
	if value, _, _ := repo.IntOverride(e.ctx, edgeKey); value != 90 {
		t.Fatalf("int after rewrite = %d, want 90", value)
	}

	// 手工坏行：int 位塞非整数、bool 位塞非字面，都报校验错。
	if _, err := e.db.Exec(e.ctx, `UPDATE settings SET value = 'abc' WHERE key = $1`, edgeKey); err != nil {
		t.Fatalf("corrupt int row: %v", err)
	}
	if _, found, err := repo.IntOverride(e.ctx, edgeKey); err == nil {
		t.Fatalf("corrupt int row = found %v without error", found)
	} else {
		requireValidation(t, err)
	}
	if _, err := e.db.Exec(e.ctx, `UPDATE settings SET value = 'TRUE' WHERE key = $1`, boolKey); err != nil {
		t.Fatalf("corrupt bool row: %v", err)
	}
	if _, found, err := repo.BoolOverride(e.ctx, boolKey); err == nil {
		t.Fatalf("corrupt bool row = found %v without error", found)
	} else {
		requireValidation(t, err)
	}

	// 操作者为空是校验错：写入零行。
	if err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		return repo.SetInt(ctx, edgeKey, 60, "")
	}); err == nil {
		t.Fatal("empty actor must be rejected")
	} else {
		requireValidation(t, err)
	}

	// 删行：行消失；删不存在的行是幂等成功。
	if err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		return repo.ClearSetting(ctx, edgeKey)
	}); err != nil {
		t.Fatalf("ClearSetting: %v", err)
	}
	if rows := e.scalarInt("SELECT count(*) FROM settings WHERE key = $1", edgeKey); rows != 0 {
		t.Fatalf("rows after clear = %d, want 0", rows)
	}
	if err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		return repo.ClearSetting(ctx, edgeKey)
	}); err != nil {
		t.Fatalf("ClearSetting absent row: %v", err)
	}

	// 既有布尔开关行为不变：registration.enabled 缺行按关闭。
	if enabled, err := repo.RegistrationEnabled(e.ctx); err != nil || enabled {
		t.Fatalf("RegistrationEnabled missing row = (%v, %v)", enabled, err)
	}
}

// 全局暂停（3a）用例接真库：写入落行、审计恰一行同事务；
// 审计追加失败时覆盖行不落地（整体回滚）；跨服务实例读回（无进程态缓存）。
func TestEnginePauseServiceE2E(t *testing.T) {
	e := newEnv(t)
	master := e.createUser("engine-pause-master@example.com")
	repo := pg.NewSettingsRepo(e.db)
	resolver, err := appsettings.NewResolver(repo, map[string]appsettings.SettingDomain{
		appsettings.KeyScheduleEdgeFrontSec: {Min: 0, Max: 300},
		appsettings.KeyScheduleEdgeBackSec:  {Min: 0, Max: 300},
	}, nil)
	if err != nil {
		t.Fatalf("NewResolver: %v", err)
	}
	newSvc := func() *appsettings.Service {
		svc, err := appsettings.NewService(appsettings.Deps{
			Store: pg.NewAnnouncementRepo(e.db), Audit: e.audits, Tx: e.db,
			Clock:    coreclock.Fixed{T: time.Date(2026, 11, 10, 10, 0, 0, 0, time.UTC)},
			Schedule: repo, Resolver: resolver,
		})
		if err != nil {
			t.Fatalf("NewService: %v", err)
		}
		return svc
	}
	svc := newSvc()
	actor := appaudit.Actor{UserID: master.ID, Role: coreauth.RoleMaster, Label: "m***@example.com"}

	// 暂停：行 true、恰一行审计、detail 只含字段与目标状态。
	if err := svc.SetEnginePaused(e.ctx, corescope.Master(), actor, true); err != nil {
		t.Fatalf("SetEnginePaused(true): %v", err)
	}
	if value := e.scalarString("SELECT value FROM settings WHERE key = $1", appsettings.KeyEnginePaused); value != "true" {
		t.Fatalf("settings value = %q, want true", value)
	}
	if rows := e.scalarInt("SELECT count(*) FROM audit_logs WHERE action = 'settings.engine_paused'"); rows != 1 {
		t.Fatalf("audit rows = %d, want 1", rows)
	}
	detail := e.scalarString(
		"SELECT detail FROM audit_logs WHERE action = 'settings.engine_paused' ORDER BY id DESC LIMIT 1")
	if detail != `{"field":"engine_paused","to":"paused"}` {
		t.Fatalf("audit detail = %q, want field+to only", detail)
	}

	// 持久跨重启（谓词 7）：新构造的用例与仓储实例读回 true——
	// 值只活在 settings 行里，没有进程态缓存。
	if paused, err := newSvc().EnginePaused(e.ctx); err != nil || !paused {
		t.Fatalf("fresh service EnginePaused = (%v, %v), want (true, nil)", paused, err)
	}

	// 审计追加失败：覆盖行必须回滚，不得留下无审计的写入（谓词 6）。
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
	if err := svc.SetEnginePaused(e.ctx, corescope.Master(), actor, false); err == nil {
		t.Fatal("resume must fail when the audit append fails")
	}
	if value := e.scalarString("SELECT value FROM settings WHERE key = $1", appsettings.KeyEnginePaused); value != "true" {
		t.Fatalf("settings value after failed resume = %q, want the pre-failure true", value)
	}
	if rows := e.scalarInt("SELECT count(*) FROM audit_logs WHERE action = 'settings.engine_paused'"); rows != 1 {
		t.Fatalf("audit rows after failure = %d, want still 1", rows)
	}

	// 审计触发器撤掉后恢复成功：行 false（写行不删行），审计恰两行。
	if _, err := e.db.Exec(e.ctx, "DROP TRIGGER reject_audit_insert ON audit_logs"); err != nil {
		t.Fatalf("drop trigger: %v", err)
	}
	if err := svc.SetEnginePaused(e.ctx, corescope.Master(), actor, false); err != nil {
		t.Fatalf("SetEnginePaused(false): %v", err)
	}
	if value := e.scalarString("SELECT value FROM settings WHERE key = $1", appsettings.KeyEnginePaused); value != "false" {
		t.Fatalf("settings value after resume = %q, want false（写行不删行）", value)
	}
	if rows := e.scalarInt("SELECT count(*) FROM audit_logs WHERE action = 'settings.engine_paused'"); rows != 2 {
		t.Fatalf("audit rows after resume = %d, want 2（幂等重放每次恰一行）", rows)
	}
	if to := e.scalarString(
		"SELECT detail FROM audit_logs WHERE action = 'settings.engine_paused' ORDER BY id DESC LIMIT 1"); !strings.Contains(to, `"to":"resumed"`) {
		t.Fatalf("resume detail = %q, want to=resumed", to)
	}
}

// 调度设置写回（sett2）用例接真库：写入落行、审计同事务；
// 审计追加失败时覆盖行不落地（整体回滚）。
func TestScheduleSettingsServiceE2E(t *testing.T) {
	e := newEnv(t)
	master := e.createUser("schedule-writeback-master@example.com")
	resolver, err := appsettings.NewResolver(pg.NewSettingsRepo(e.db), map[string]appsettings.SettingDomain{
		appsettings.KeyScheduleEdgeFrontSec: {Min: 0, Max: 300},
		appsettings.KeyScheduleEdgeBackSec:  {Min: 0, Max: 300},
	}, nil)
	if err != nil {
		t.Fatalf("NewResolver: %v", err)
	}
	svc, err := appsettings.NewService(appsettings.Deps{
		Store:    pg.NewAnnouncementRepo(e.db),
		Audit:    e.audits,
		Tx:       e.db,
		Clock:    coreclock.Fixed{T: time.Date(2026, 11, 10, 10, 0, 0, 0, time.UTC)},
		Schedule: pg.NewSettingsRepo(e.db),
		Resolver: resolver,
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	actor := appaudit.Actor{UserID: master.ID, Role: coreauth.RoleMaster, Label: "m***@example.com"}

	// 成功：覆盖行与审计行同事务落库；detail 只含变更键旧→新。
	err = svc.SetSchedule(e.ctx, corescope.Master(), actor, []appsettings.ScheduleChange{
		{Key: appsettings.KeyScheduleEdgeFrontSec, Op: appsettings.ScheduleOpSet, Value: 120},
	})
	if err != nil {
		t.Fatalf("SetSchedule: %v", err)
	}
	if value := e.scalarString("SELECT value FROM settings WHERE key = $1", appsettings.KeyScheduleEdgeFrontSec); value != "120" {
		t.Fatalf("settings value = %q, want 120", value)
	}
	if rows := e.scalarInt("SELECT count(*) FROM audit_logs WHERE action = 'settings.schedule_writeback'"); rows != 1 {
		t.Fatalf("audit rows = %d, want 1", rows)
	}
	detail := e.scalarString(
		"SELECT detail FROM audit_logs WHERE action = 'settings.schedule_writeback' ORDER BY id DESC LIMIT 1")
	if !strings.Contains(detail, `"changes"`) || !strings.Contains(detail, "schedule.edge_front_sec") {
		t.Fatalf("audit detail = %q", detail)
	}

	// 审计追加失败：覆盖行必须回滚，不得留下无审计的写入。
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
	err = svc.SetSchedule(e.ctx, corescope.Master(), actor, []appsettings.ScheduleChange{
		{Key: appsettings.KeyScheduleEdgeFrontSec, Op: appsettings.ScheduleOpSet, Value: 90},
	})
	if err == nil {
		t.Fatal("SetSchedule must fail when the audit append fails")
	}
	if value := e.scalarString("SELECT value FROM settings WHERE key = $1", appsettings.KeyScheduleEdgeFrontSec); value != "120" {
		t.Fatalf("settings value after failed put = %q, want the pre-failure 120", value)
	}
}
