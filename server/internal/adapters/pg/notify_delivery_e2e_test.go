package pg_test

// 0v8 B 收件人解析与到期行的真库 e2e。
//
// 覆盖：master 全量 / admin 只收绑定校 / 无绑定、停用、软删都不收；
// 手机号与属主读取的 not_found 面；到期行读取带通知正文与收件地址；
// 账本唯一键与 skipped 行不进入到期扫描。
// 投递用例链路的 e2e 在 cmd/server：adapters 不得导入 app（boundary.adapters）。

import (
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corenotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/notify"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
)

// adminUser 建一个指定角色的用户，并按需写入绑定校。
func (e *testEnv) adminUser(email string, role coreauth.Role, schools ...string) coreuser.Record {
	e.t.Helper()
	rec := e.createUser(email)
	if role != coreauth.RoleUser {
		if _, err := e.db.Exec(e.ctx,
			"UPDATE users SET role = $2 WHERE id = $1::uuid", rec.ID, string(role)); err != nil {
			e.t.Fatalf("set role %s: %v", role, err)
		}
	}
	for _, schoolID := range schools {
		if _, err := e.db.Exec(e.ctx,
			"INSERT INTO admin_schools (user_id, school_id) VALUES ($1::uuid, $2::uuid)",
			rec.ID, schoolID); err != nil {
			e.t.Fatalf("bind school %s: %v", schoolID, err)
		}
	}
	return rec
}

// setPrefs 用真实的订阅仓储写入覆盖集。
func (e *testEnv) setPrefs(userID string, prefs ...corenotify.Pref) {
	e.t.Helper()
	if err := pg.NewNotificationPrefsRepo(e.db).Replace(e.ctx, userID, prefs); err != nil {
		e.t.Fatalf("replace prefs: %v", err)
	}
}

// 收件人解析：master 全量；admin 只收绑定校；停用、软删、无绑定都不收。
func TestNotificationAudienceScopesBySchool(t *testing.T) {
	e := newEnv(t)
	schoolA := e.createSchool("aud-a")
	schoolB := e.createSchool("aud-b")
	master := e.adminUser("aud-master@example.com", coreauth.RoleMaster)
	adminA := e.adminUser("aud-admin-a@example.com", coreauth.RoleAdmin, schoolA.ID)
	adminB := e.adminUser("aud-admin-b@example.com", coreauth.RoleAdmin, schoolB.ID)
	e.adminUser("aud-unbound@example.com", coreauth.RoleAdmin)
	disabled := e.adminUser("aud-disabled@example.com", coreauth.RoleAdmin, schoolA.ID)
	if _, err := e.db.Exec(e.ctx, "UPDATE users SET status = 'disabled' WHERE id = $1::uuid", disabled.ID); err != nil {
		t.Fatalf("disable admin: %v", err)
	}
	deleted := e.adminUser("aud-deleted@example.com", coreauth.RoleAdmin, schoolA.ID)
	if _, err := e.db.Exec(e.ctx, "UPDATE users SET deleted_at = now() WHERE id = $1::uuid", deleted.ID); err != nil {
		t.Fatalf("soft delete admin: %v", err)
	}
	// 普通用户永不进管理员收件人集合。
	e.createUser("aud-plain@example.com")
	// 显式覆盖行要随收件人一起读出。
	e.setPrefs(adminA.ID, corenotify.Pref{
		Category: corenotify.CategoryCredentialInvalid, Channel: corenotify.ChannelEmail, Enabled: true,
	})

	audience := pg.NewNotificationAudienceRepo(e.db)
	for _, tc := range []struct {
		schoolID string
		want     []string
	}{
		{schoolA.ID, []string{master.ID, adminA.ID}},
		{schoolB.ID, []string{master.ID, adminB.ID}},
		// 无院校事件只回 master：没有可匹配的绑定校。
		{"", []string{master.ID}},
	} {
		subscribers, err := audience.AdminSubscribers(e.ctx, tc.schoolID)
		if err != nil {
			t.Fatalf("AdminSubscribers(%q): %v", tc.schoolID, err)
		}
		if len(subscribers) != len(tc.want) {
			t.Fatalf("subscribers(%q) = %d, want %d（%+v）", tc.schoolID, len(subscribers), len(tc.want), subscribers)
		}
		for i, want := range tc.want {
			if subscribers[i].UserID != want {
				t.Fatalf("subscribers(%q)[%d] = %s, want %s", tc.schoolID, i, subscribers[i].UserID, want)
			}
		}
	}
	subscribers, err := audience.AdminSubscribers(e.ctx, schoolA.ID)
	if err != nil {
		t.Fatalf("AdminSubscribers: %v", err)
	}
	override := subscribers[1].Overrides[corenotify.Pair{
		Category: corenotify.CategoryCredentialInvalid, Channel: corenotify.ChannelEmail,
	}]
	if !override {
		t.Fatalf("显式覆盖未读出：%+v", subscribers[1].Overrides)
	}
	// master 无覆盖行：空集合由用例按默认表解释。
	if len(subscribers[0].Overrides) != 0 {
		t.Fatalf("master 覆盖集 = %+v, want 空", subscribers[0].Overrides)
	}
}

// 属主与手机号读取：账号软删与停用属主都回 not_found。
func TestNotificationAudienceAccountReads(t *testing.T) {
	e := newEnv(t)
	school := e.createSchool("acct-read")
	owner := e.createUser("acct-read-owner@example.com")
	account := e.createAccount(school.ID, owner.ID, "13800000001")

	audience := pg.NewNotificationAudienceRepo(e.db)
	phone, err := audience.AccountPhone(e.ctx, account.ID)
	if err != nil || phone != "13800000001" {
		t.Fatalf("AccountPhone = (%q,%v)", phone, err)
	}
	if _, err := audience.AccountPhone(e.ctx, "018f4a4e-0000-7000-8000-00000000ffff"); errs.From(err).Code != errs.CodeNotFound {
		t.Fatalf("缺账号 code = %v", errs.From(err).Code)
	}

	if err := e.users.SetMailNotify(e.ctx, owner.ID, true); err != nil {
		t.Fatalf("set mail notify: %v", err)
	}
	got, err := audience.FailureOwner(e.ctx, account.ID)
	if err != nil || got.UserID != owner.ID || !got.MailNotify {
		t.Fatalf("FailureOwner = (%+v,%v)", got, err)
	}

	// 停用属主：仍不是可投收件人。
	if _, err := e.db.Exec(e.ctx, "UPDATE users SET status = 'disabled' WHERE id = $1::uuid", owner.ID); err != nil {
		t.Fatalf("disable owner: %v", err)
	}
	if _, err := audience.FailureOwner(e.ctx, account.ID); errs.From(err).Code != errs.CodeNotFound {
		t.Fatalf("停用属主 code = %v", errs.From(err).Code)
	}
	// 账号软删：属主与手机号都不可读。
	if _, err := e.db.Exec(e.ctx, "UPDATE accounts SET deleted_at = now() WHERE id = $1::uuid", account.ID); err != nil {
		t.Fatalf("soft delete account: %v", err)
	}
	if _, err := audience.AccountPhone(e.ctx, account.ID); errs.From(err).Code != errs.CodeNotFound {
		t.Fatalf("软删账号 code = %v", errs.From(err).Code)
	}
}

// 到期行读取：带通知正文与收件地址；唯一键吸收重复入队；
// 未来行与 skipped 行都不进扫描。
func TestNotificationDeliveryDueRowsCarryText(t *testing.T) {
	e := newEnv(t)
	user := e.createUser("due-owner@example.com")
	notes := pg.NewNotificationRepo(e.db)
	ledger := pg.NewNotificationDeliveryRepo(e.db)
	now := time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)

	note, err := notes.Insert(e.ctx, corenotify.NotificationInsert{
		UserID: user.ID, Category: corenotify.CategoryReviewPending, DedupeKey: "due/1",
		Title: "账号待审核", Body: "账号 138****0001 已提交审核。", CreatedAt: now,
	})
	if err != nil {
		t.Fatalf("insert notification: %v", err)
	}
	past := now.Add(-time.Minute)
	delivery, err := ledger.InsertPending(e.ctx, corenotify.DeliveryInsert{
		NotificationID: note.ID, UserID: user.ID,
		Channel: corenotify.ChannelEmail, NextRetryAt: &past,
	})
	if err != nil {
		t.Fatalf("insert pending: %v", err)
	}
	// 同一 (notification, channel, user) 重复入队：返回同一行。
	again, err := ledger.InsertPending(e.ctx, corenotify.DeliveryInsert{
		NotificationID: note.ID, UserID: user.ID,
		Channel: corenotify.ChannelEmail, NextRetryAt: &past,
	})
	if err != nil || again.ID != delivery.ID {
		t.Fatalf("重复入队 = (%+v,%v)，want 同一行 %s", again, err, delivery.ID)
	}
	if got := e.scalarInt(
		`SELECT count(*) FROM notification_deliveries WHERE notification_id = $1::uuid`,
		note.ID); got != 1 {
		t.Fatalf("账本行 = %d, want 1", got)
	}

	due, err := ledger.ListDueForSend(e.ctx, now, 10)
	if err != nil {
		t.Fatalf("ListDueForSend: %v", err)
	}
	if len(due) != 1 {
		t.Fatalf("到期行 = %d, want 1", len(due))
	}
	if due[0].Title != "账号待审核" || due[0].Body != "账号 138****0001 已提交审核。" ||
		due[0].Email != user.Email || due[0].Attempts != 0 {
		t.Fatalf("到期行 = %+v", due[0])
	}

	// 未来行不进本轮到扫描。
	future := now.Add(time.Hour)
	futureNote, err := notes.Insert(e.ctx, corenotify.NotificationInsert{
		UserID: user.ID, Category: corenotify.CategoryReviewPending, DedupeKey: "due/2",
		Title: "账号待审核", Body: "body", CreatedAt: now,
	})
	if err != nil {
		t.Fatalf("insert notification: %v", err)
	}
	if _, err := ledger.InsertPending(e.ctx, corenotify.DeliveryInsert{
		NotificationID: futureNote.ID, UserID: user.ID,
		Channel: corenotify.ChannelEmail, NextRetryAt: &future,
	}); err != nil {
		t.Fatalf("insert pending: %v", err)
	}
	if due, err = ledger.ListDueForSend(e.ctx, now, 10); err != nil || len(due) != 1 {
		t.Fatalf("未来行进了扫描：%d (%v)", len(due), err)
	}

	// skipped 行退出扫描。
	if err := ledger.MarkSkipped(e.ctx, delivery.ID, "smtp_not_configured"); err != nil {
		t.Fatalf("mark skipped: %v", err)
	}
	if due, err = ledger.ListDueForSend(e.ctx, now.Add(2*time.Hour), 10); err != nil || len(due) != 1 {
		t.Fatalf("skipped 后扫描 = %d (%v), want 1（只剩未来行）", len(due), err)
	}
	status, attempts, kind, skip, _ := deliveryState(t, e, delivery.ID)
	if status != corenotify.DeliveryStatusSkipped || attempts != 0 || kind != "" ||
		skip != "smtp_not_configured" {
		t.Fatalf("skipped 状态 = %s/%d/%q/%q", status, attempts, kind, skip)
	}
}
