package pg_test

// 站内信与投递账本的真库 e2e。
//
// 覆盖：本人隔离、键集翻页（同 created_at 按 id 倒序）、未读数只算本人、
// 越权 id 静默忽略、read-all 幂等、30 天边界删除、用户与通知级联删除、
// 两表唯一键、投递 due 扫描与终态记账。
// 所有断言走真实 SQL 与真实约束，不用进程内替身。

import (
	"context"
	"sort"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corenotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/notify"
)

// insertNotification 写入一条站内信并断言成功。
func insertNotification(t *testing.T, repo *pg.NotificationRepo,
	userID, dedupe, title string, createdAt time.Time) corenotify.Notification {
	t.Helper()
	item, err := repo.Insert(context.Background(), corenotify.NotificationInsert{
		UserID:    userID,
		Category:  corenotify.CategoryReviewPending,
		DedupeKey: dedupe,
		Title:     title,
		Body:      "body",
		CreatedAt: createdAt,
	})
	if err != nil {
		t.Fatalf("insert notification %q: %v", dedupe, err)
	}
	return item
}

// 本人隔离与键集翻页：无重无漏，同 created_at 时按 id 倒序。
func TestNotificationListIsolatesUsersAndPages(t *testing.T) {
	e := newEnv(t)
	repo := pg.NewNotificationRepo(e.db)
	alice := e.createUser("notify-alice@example.com")
	bob := e.createUser("notify-bob@example.com")

	base := time.Date(2026, 10, 1, 9, 0, 0, 0, time.UTC)
	var aliceIDs []string
	for i := 0; i < 5; i++ {
		item := insertNotification(t, repo, alice.ID, "page/"+string(rune('a'+i)),
			"通知", base.Add(time.Duration(i)*time.Minute))
		aliceIDs = append(aliceIDs, item.ID)
	}
	bobItem := insertNotification(t, repo, bob.ID, "page/bob", "bob 的通知", base.Add(time.Hour))

	// 三个同一时刻的行：排序必须由 id DESC 决定，不能依赖插入顺序。
	tieAt := base.Add(24 * time.Hour)
	var tieIDs []string
	for i := 0; i < 3; i++ {
		item := insertNotification(t, repo, alice.ID, "tie/"+string(rune('a'+i)), "同刻", tieAt)
		tieIDs = append(tieIDs, item.ID)
	}
	sort.Strings(tieIDs)
	wantTie := []string{tieIDs[2], tieIDs[1], tieIDs[0]}

	var pageIDs []string
	cursor := ""
	for page := 0; page < 3; page++ {
		items, next, err := repo.List(context.Background(), corenotify.NotificationQuery{
			UserID: alice.ID, Cursor: cursor, Limit: 4,
		})
		if err != nil {
			t.Fatalf("list page %d: %v", page, err)
		}
		for _, item := range items {
			if item.ID == bobItem.ID {
				t.Fatalf("alice page contains bob's row %s", item.ID)
			}
			pageIDs = append(pageIDs, item.ID)
		}
		cursor = next
		if cursor == "" {
			break
		}
	}
	if len(pageIDs) != 8 {
		t.Fatalf("alice rows = %d, want 8 (no duplicates, no gaps)", len(pageIDs))
	}
	seen := map[string]bool{}
	for _, id := range pageIDs {
		if seen[id] {
			t.Fatalf("duplicate row %s across pages", id)
		}
		seen[id] = true
	}
	for _, id := range aliceIDs {
		if !seen[id] {
			t.Fatalf("missing row %s", id)
		}
	}
	if got := pageIDs[:3]; got[0] != wantTie[0] || got[1] != wantTie[1] || got[2] != wantTie[2] {
		t.Fatalf("same-timestamp order = %v, want %v", got, wantTie)
	}

	bobItems, next, err := repo.List(context.Background(), corenotify.NotificationQuery{
		UserID: bob.ID, Limit: 20,
	})
	if err != nil {
		t.Fatalf("list bob: %v", err)
	}
	if len(bobItems) != 1 || bobItems[0].ID != bobItem.ID || next != "" {
		t.Fatalf("bob rows = %+v next=%q", bobItems, next)
	}

	// 非法游标与越界 limit 都是校验失败。
	if _, _, err := repo.List(context.Background(), corenotify.NotificationQuery{
		UserID: alice.ID, Cursor: "not-base64", Limit: 20,
	}); errs.From(err).Code != errs.CodeValidation {
		t.Fatalf("invalid cursor code = %v", errs.From(err).Code)
	}
	for _, limit := range []int{0, 101} {
		if _, _, err := repo.List(context.Background(), corenotify.NotificationQuery{
			UserID: alice.ID, Limit: limit,
		}); errs.From(err).Code != errs.CodeValidation {
			t.Fatalf("limit %d code = %v", limit, errs.From(err).Code)
		}
	}
}

// 未读数、标记已读与全部已读都以本人为作用域；越权 id 静默忽略。
func TestNotificationReadOwnershipAndIdempotency(t *testing.T) {
	e := newEnv(t)
	repo := pg.NewNotificationRepo(e.db)
	alice := e.createUser("read-alice@example.com")
	bob := e.createUser("read-bob@example.com")

	base := time.Date(2026, 10, 1, 9, 0, 0, 0, time.UTC)
	a1 := insertNotification(t, repo, alice.ID, "read/a1", "a1", base)
	insertNotification(t, repo, alice.ID, "read/a2", "a2", base.Add(time.Minute))
	b1 := insertNotification(t, repo, bob.ID, "read/b1", "b1", base)

	ctx := context.Background()
	if n, err := repo.UnreadCount(ctx, alice.ID); err != nil || n != 2 {
		t.Fatalf("alice unread = %d, %v; want 2", n, err)
	}
	if n, err := repo.UnreadCount(ctx, bob.ID); err != nil || n != 1 {
		t.Fatalf("bob unread = %d, %v; want 1", n, err)
	}

	firstRead := base.Add(2 * time.Hour)
	// 越权 b1 与不存在的 id 都不得命中；只有 a1 被更新。
	affected, err := repo.MarkRead(ctx, alice.ID,
		[]string{a1.ID, b1.ID, "018f4a4e-0000-7000-8000-00000000ffff"}, firstRead)
	if err != nil {
		t.Fatalf("mark read: %v", err)
	}
	if affected != 1 {
		t.Fatalf("mark read affected %d rows, want 1", affected)
	}
	if n, _ := repo.UnreadCount(ctx, alice.ID); n != 1 {
		t.Fatalf("alice unread after mark = %d, want 1", n)
	}
	if n, _ := repo.UnreadCount(ctx, bob.ID); n != 1 {
		t.Fatalf("bob unread changed to %d", n)
	}
	stored, err := e.db.QueryString(ctx, "SELECT read_at::text FROM notifications WHERE id = $1::uuid", a1.ID)
	if err != nil {
		t.Fatalf("read a1 read_at: %v", err)
	}
	// 重复标记已读不重写首次时刻。
	if affected, err := repo.MarkRead(ctx, alice.ID, []string{a1.ID}, firstRead.Add(time.Hour)); err != nil || affected != 0 {
		t.Fatalf("second mark = %d, %v; want 0", affected, err)
	}
	again, err := e.db.QueryString(ctx, "SELECT read_at::text FROM notifications WHERE id = $1::uuid", a1.ID)
	if err != nil {
		t.Fatalf("read a1 read_at again: %v", err)
	}
	if stored != again {
		t.Fatalf("first read timestamp was rewritten: %q -> %q", stored, again)
	}

	if affected, err := repo.MarkAllRead(ctx, bob.ID, base.Add(3*time.Hour)); err != nil || affected != 1 {
		t.Fatalf("mark all = %d, %v; want 1", affected, err)
	}
	if affected, err := repo.MarkAllRead(ctx, bob.ID, base.Add(4*time.Hour)); err != nil || affected != 0 {
		t.Fatalf("second mark all = %d, %v; want 0 (idempotent)", affected, err)
	}
	if n, _ := repo.UnreadCount(ctx, bob.ID); n != 0 {
		t.Fatalf("bob unread after mark-all = %d", n)
	}
	if n, _ := repo.UnreadCount(ctx, alice.ID); n != 1 {
		t.Fatalf("alice unread after bob mark-all = %d, want 1", n)
	}
}

// Insert 以 (user_id, dedupe_key) 幂等；类别词表在写入前校验。
func TestNotificationInsertDedupe(t *testing.T) {
	e := newEnv(t)
	repo := pg.NewNotificationRepo(e.db)
	user := e.createUser("dedupe@example.com")
	at := time.Date(2026, 10, 1, 9, 0, 0, 0, time.UTC)

	first := insertNotification(t, repo, user.ID, "review_pending/acct-1/evt-1", "第一次", at)
	second := insertNotification(t, repo, user.ID, "review_pending/acct-1/evt-1", "重复", at.Add(time.Minute))
	if first.ID != second.ID {
		t.Fatalf("dedupe produced two rows: %s vs %s", first.ID, second.ID)
	}
	if got := e.scalarInt("SELECT count(*) FROM notifications WHERE user_id = $1::uuid", user.ID); got != 1 {
		t.Fatalf("rows = %d, want 1", got)
	}
	// 同一 dedupe_key 在不同用户下是两行：唯一键含 user_id。
	other := e.createUser("dedupe-other@example.com")
	otherRow := insertNotification(t, repo, other.ID, "review_pending/acct-1/evt-1", "他人", at)
	if otherRow.ID == first.ID {
		t.Fatal("dedupe key collided across users")
	}

	if _, err := repo.Insert(context.Background(), corenotify.NotificationInsert{
		UserID: user.ID, Category: "weekly_digest", DedupeKey: "bad/category",
		Title: "x", CreatedAt: at,
	}); errs.From(err).Code != errs.CodeValidation {
		t.Fatalf("unknown category code = %v", errs.From(err).Code)
	}
}

// 30 天边界：cutoff 之前删、cutoff 当刻与之后留；投递账本随通知级联。
func TestNotificationRetentionBoundaryAndCascade(t *testing.T) {
	e := newEnv(t)
	notifyRepo := pg.NewNotificationRepo(e.db)
	deliveryRepo := pg.NewNotificationDeliveryRepo(e.db)
	user := e.createUser("retention-notify@example.com")

	now := time.Date(2026, 10, 31, 9, 0, 0, 0, time.UTC)
	cutoff := now.AddDate(0, 0, -30)
	old := insertNotification(t, notifyRepo, user.ID, "retain/old", "旧", cutoff.Add(-time.Second))
	exact := insertNotification(t, notifyRepo, user.ID, "retain/exact", "边界", cutoff)
	fresh := insertNotification(t, notifyRepo, user.ID, "retain/fresh", "新", cutoff.Add(time.Second))
	for _, item := range []corenotify.Notification{old, exact, fresh} {
		if _, err := deliveryRepo.InsertPending(context.Background(), corenotify.DeliveryInsert{
			NotificationID: item.ID, UserID: user.ID, Channel: corenotify.ChannelEmail,
		}); err != nil {
			t.Fatalf("insert delivery for %s: %v", item.ID, err)
		}
	}

	deleted, err := pg.NewMaintenanceRepo(e.db).DeleteNotificationsBefore(
		context.Background(), cutoff, 1000)
	if err != nil {
		t.Fatalf("retention delete: %v", err)
	}
	if deleted != 1 {
		t.Fatalf("deleted = %d, want 1 (only created_at < cutoff)", deleted)
	}
	if got := e.scalarInt("SELECT count(*) FROM notifications WHERE id = $1::uuid", old.ID); got != 0 {
		t.Fatalf("old row survived")
	}
	for _, item := range []corenotify.Notification{exact, fresh} {
		if got := e.scalarInt("SELECT count(*) FROM notifications WHERE id = $1::uuid", item.ID); got != 1 {
			t.Fatalf("row %s was deleted at or after the boundary", item.ID)
		}
	}
	if got := e.scalarInt("SELECT count(*) FROM notification_deliveries WHERE notification_id = $1::uuid", old.ID); got != 0 {
		t.Fatalf("deliveries of the deleted notification survived: %d", got)
	}
	if got := e.scalarInt("SELECT count(*) FROM notification_deliveries WHERE notification_id = $1::uuid", fresh.ID); got != 1 {
		t.Fatalf("delivery of a retained notification was deleted")
	}

	// 用户硬删级联到通知与账本。
	doomed := e.createUser("cascade-notify@example.com")
	item := insertNotification(t, notifyRepo, doomed.ID, "cascade/1", "随用户删", now)
	if _, err := deliveryRepo.InsertPending(context.Background(), corenotify.DeliveryInsert{
		NotificationID: item.ID, UserID: doomed.ID, Channel: corenotify.ChannelEmail,
	}); err != nil {
		t.Fatalf("insert cascade delivery: %v", err)
	}
	if _, err := e.db.Exec(context.Background(), "DELETE FROM users WHERE id = $1::uuid", doomed.ID); err != nil {
		t.Fatalf("hard delete user: %v", err)
	}
	if got := e.scalarInt("SELECT count(*) FROM notifications WHERE user_id = $1::uuid", doomed.ID); got != 0 {
		t.Fatalf("notifications survived the user cascade: %d", got)
	}
	if got := e.scalarInt("SELECT count(*) FROM notification_deliveries WHERE user_id = $1::uuid", doomed.ID); got != 0 {
		t.Fatalf("deliveries survived the user cascade: %d", got)
	}
}

// 投递账本：唯一键、due 扫描、失败重试与终态。
func TestNotificationDeliveryLedger(t *testing.T) {
	e := newEnv(t)
	notifyRepo := pg.NewNotificationRepo(e.db)
	deliveryRepo := pg.NewNotificationDeliveryRepo(e.db)
	user := e.createUser("delivery@example.com")
	ctx := context.Background()
	now := time.Date(2026, 10, 31, 9, 0, 0, 0, time.UTC)

	n1 := insertNotification(t, notifyRepo, user.ID, "delivery/1", "n1", now)
	n2 := insertNotification(t, notifyRepo, user.ID, "delivery/2", "n2", now.Add(time.Second))
	n3 := insertNotification(t, notifyRepo, user.ID, "delivery/3", "n3", now.Add(2*time.Second))

	due := now.Add(-time.Minute)
	first, err := deliveryRepo.InsertPending(ctx, corenotify.DeliveryInsert{
		NotificationID: n1.ID, UserID: user.ID, Channel: corenotify.ChannelEmail, NextRetryAt: &due,
	})
	if err != nil {
		t.Fatalf("insert pending: %v", err)
	}
	again, err := deliveryRepo.InsertPending(ctx, corenotify.DeliveryInsert{
		NotificationID: n1.ID, UserID: user.ID, Channel: corenotify.ChannelEmail, NextRetryAt: &due,
	})
	if err != nil {
		t.Fatalf("insert pending again: %v", err)
	}
	if first.ID != again.ID {
		t.Fatalf("delivery dedupe produced two rows: %s vs %s", first.ID, again.ID)
	}
	if got := e.scalarInt("SELECT count(*) FROM notification_deliveries WHERE notification_id = $1::uuid", n1.ID); got != 1 {
		t.Fatalf("delivery rows = %d, want 1", got)
	}
	future := now.Add(time.Hour)
	futureRow, err := deliveryRepo.InsertPending(ctx, corenotify.DeliveryInsert{
		NotificationID: n2.ID, UserID: user.ID, Channel: corenotify.ChannelEmail, NextRetryAt: &future,
	})
	if err != nil {
		t.Fatalf("insert future delivery: %v", err)
	}

	listed, err := deliveryRepo.ListDue(ctx, now, 10)
	if err != nil {
		t.Fatalf("list due: %v", err)
	}
	if len(listed) != 1 || listed[0].ID != first.ID {
		t.Fatalf("due = %+v, want only %s", listed, first.ID)
	}

	retryAt := now.Add(5 * time.Minute)
	if err := deliveryRepo.MarkFailed(ctx, first.ID, "connect", &retryAt); err != nil {
		t.Fatalf("mark failed retry: %v", err)
	}
	status, attempts, kind, skip, next := deliveryState(t, e, first.ID)
	if status != corenotify.DeliveryStatusPending || attempts != 1 || kind != "connect" ||
		skip != "" || next == "" {
		t.Fatalf("retry state = %s/%d/%q/%q/%q", status, attempts, kind, skip, next)
	}
	if listed, _ := deliveryRepo.ListDue(ctx, now, 10); len(listed) != 0 {
		t.Fatalf("retry row is due before its next_retry_at: %+v", listed)
	}
	listed, err = deliveryRepo.ListDue(ctx, now.Add(10*time.Minute), 10)
	if err != nil || len(listed) != 1 || listed[0].ID != first.ID {
		t.Fatalf("retry due = %+v, %v", listed, err)
	}
	if err := deliveryRepo.MarkSent(ctx, first.ID); err != nil {
		t.Fatalf("mark sent: %v", err)
	}
	status, attempts, kind, skip, next = deliveryState(t, e, first.ID)
	if status != corenotify.DeliveryStatusSent || attempts != 2 || kind != "" ||
		skip != "" || next != "" {
		t.Fatalf("sent state = %s/%d/%q/%q/%q", status, attempts, kind, skip, next)
	}
	if err := deliveryRepo.MarkSent(ctx, first.ID); err != nil {
		t.Fatalf("mark sent again: %v", err)
	}
	if _, attempts, _, _, _ = deliveryState(t, e, first.ID); attempts != 2 {
		t.Fatalf("terminal row was rewritten: attempts = %d", attempts)
	}

	if err := deliveryRepo.MarkFailed(ctx, futureRow.ID, "auth", nil); err != nil {
		t.Fatalf("mark failed terminal: %v", err)
	}
	status, attempts, kind, skip, next = deliveryState(t, e, futureRow.ID)
	if status != corenotify.DeliveryStatusFailed || attempts != 1 || kind != "auth" ||
		skip != "" || next != "" {
		t.Fatalf("failed state = %s/%d/%q/%q/%q", status, attempts, kind, skip, next)
	}

	skipped, err := deliveryRepo.InsertPending(ctx, corenotify.DeliveryInsert{
		NotificationID: n3.ID, UserID: user.ID, Channel: corenotify.ChannelEmail, NextRetryAt: &due,
	})
	if err != nil {
		t.Fatalf("insert skipped row: %v", err)
	}
	if err := deliveryRepo.MarkSkipped(ctx, skipped.ID, "smtp_not_configured"); err != nil {
		t.Fatalf("mark skipped: %v", err)
	}
	status, attempts, kind, skip, next = deliveryState(t, e, skipped.ID)
	if status != corenotify.DeliveryStatusSkipped || attempts != 0 || kind != "" ||
		skip != "smtp_not_configured" || next != "" {
		t.Fatalf("skipped state = %s/%d/%q/%q/%q", status, attempts, kind, skip, next)
	}

	// 唯一键在数据库层也生效：绕过仓储直接插入必须被拒。
	_, err = e.db.Exec(ctx, `
		INSERT INTO notification_deliveries (notification_id, user_id, channel)
		VALUES ($1::uuid, $2::uuid, 'email')`, n1.ID, user.ID)
	if err == nil {
		t.Fatal("duplicate delivery insert was accepted")
	}
	if code := errs.From(err).Code; code != errs.CodeConflict {
		t.Fatalf("duplicate delivery code = %v, want conflict", code)
	}

	// 父通知不存在时入队是 not_found，不是冲突。
	if _, err := deliveryRepo.InsertPending(ctx, corenotify.DeliveryInsert{
		NotificationID: "018f4a4e-0000-7000-8000-00000000ffff",
		UserID:         user.ID, Channel: corenotify.ChannelEmail,
	}); errs.From(err).Code != errs.CodeNotFound {
		t.Fatalf("missing parent code = %v, want not_found", errs.From(err).Code)
	}

	// 通知级联删除账本行。
	if _, err := e.db.Exec(ctx, "DELETE FROM notifications WHERE id = $1::uuid", n2.ID); err != nil {
		t.Fatalf("delete notification: %v", err)
	}
	if got := e.scalarInt("SELECT count(*) FROM notification_deliveries WHERE notification_id = $1::uuid", n2.ID); got != 0 {
		t.Fatalf("delivery survived the notification cascade: %d", got)
	}
}

// mustInsertNotificationRow 写入一条站内信并断言成功；返回落库行。
func mustInsertNotificationRow(t *testing.T, ctx context.Context, repo *pg.NotificationRepo,
	in corenotify.NotificationInsert) corenotify.Notification {
	t.Helper()
	item, err := repo.Insert(ctx, in)
	if err != nil {
		t.Fatalf("insert %s/%s: %v", in.Category, in.DedupeKey, err)
	}
	return item
}

// P2-3：读面按订阅矩阵 in_app 过滤。
// 显式覆盖 (credential_invalid, in_app)=false → 列表与未读数都不含该类行
// （账本锚行仍在库里，只是不出读面）；
// 默认表 sign_failed_daily in_app=false → 无覆盖时同样被隐藏；
// 显式覆盖 (sign_failed_daily, in_app)=true → 回到列表。
func TestNotificationListAppliesInAppFilter(t *testing.T) {
	e := newEnv(t)
	repo := pg.NewNotificationRepo(e.db)
	prefs := pg.NewNotificationPrefsRepo(e.db)
	alice := e.createUser("filter-alice@example.com")
	bob := e.createUser("filter-bob@example.com")
	ctx := context.Background()

	base := time.Date(2026, 11, 1, 9, 0, 0, 0, time.UTC)
	cred := mustInsertNotificationRow(t, ctx, repo, corenotify.NotificationInsert{
		UserID: alice.ID, Category: corenotify.CategoryCredentialInvalid,
		DedupeKey: "filter/cred", Title: "凭据", Body: "b", CreatedAt: base,
	})
	mustInsertNotificationRow(t, ctx, repo, corenotify.NotificationInsert{
		UserID: alice.ID, Category: corenotify.CategoryReviewPending,
		DedupeKey: "filter/review", Title: "待审", Body: "b", CreatedAt: base,
	})
	mustInsertNotificationRow(t, ctx, repo, corenotify.NotificationInsert{
		UserID: alice.ID, Category: corenotify.CategorySignFailedDaily,
		DedupeKey: "filter/digest", Title: "摘要", Body: "b", CreatedAt: base,
	})
	// 锚行语义：账本锚仍落库；过滤只发生在读面，不删行。
	if _, err := pg.NewNotificationDeliveryRepo(e.db).InsertPending(ctx,
		corenotify.DeliveryInsert{NotificationID: cred.ID, UserID: alice.ID,
			Channel: corenotify.ChannelEmail}); err != nil {
		t.Fatalf("anchor delivery: %v", err)
	}

	// 默认表口径：credential_invalid/review_pending 可见，sign_failed_daily 隐藏。
	items, _, err := repo.List(ctx, corenotify.NotificationQuery{UserID: alice.ID, Limit: 20})
	if err != nil {
		t.Fatalf("list default: %v", err)
	}
	if len(items) != 2 {
		t.Fatalf("默认可见 = %d, want 2（sign_failed_daily 默认 in_app=false）", len(items))
	}
	if n, err := repo.UnreadCount(ctx, alice.ID); err != nil || n != 2 {
		t.Fatalf("默认未读 = %d, %v; want 2", n, err)
	}

	// 显式关 credential_invalid：列表与未读都不含；锚行仍在库。
	if err := prefs.Replace(ctx, alice.ID, []corenotify.Pref{
		{Category: corenotify.CategoryCredentialInvalid, Channel: corenotify.ChannelInApp, Enabled: false},
	}); err != nil {
		t.Fatalf("replace prefs: %v", err)
	}
	items, _, err = repo.List(ctx, corenotify.NotificationQuery{UserID: alice.ID, Limit: 20})
	if err != nil {
		t.Fatalf("list filtered: %v", err)
	}
	for _, item := range items {
		if item.ID == cred.ID {
			t.Fatalf("in_app=false 的类别仍出现在列表: %+v", item)
		}
	}
	if len(items) != 1 || items[0].Category != corenotify.CategoryReviewPending {
		t.Fatalf("过滤后 = %+v, want 只剩 review_pending", items)
	}
	if n, _ := repo.UnreadCount(ctx, alice.ID); n != 1 {
		t.Fatalf("过滤后未读 = %d, want 1", n)
	}
	if got := e.scalarInt(`SELECT count(*) FROM notifications WHERE id = $1::uuid`, cred.ID); got != 1 {
		t.Fatalf("锚行被删除（读面过滤不得删行）: %d", got)
	}
	// 未覆盖用户不受影响：bob 的读面按默认表。
	if _, _, err := repo.List(ctx, corenotify.NotificationQuery{UserID: bob.ID, Limit: 20}); err != nil {
		t.Fatalf("list bob: %v", err)
	}

	// 显式开 sign_failed_daily：默认隐藏翻转回可见。
	// Replace 是全量替换语义：一并保留 credential_invalid=false，
	// 否则它会回到默认 in_app=true 而重新出现（这本身也是替换语义的守卫）。
	if err := prefs.Replace(ctx, alice.ID, []corenotify.Pref{
		{Category: corenotify.CategorySignFailedDaily, Channel: corenotify.ChannelInApp, Enabled: true},
		{Category: corenotify.CategoryCredentialInvalid, Channel: corenotify.ChannelInApp, Enabled: false},
	}); err != nil {
		t.Fatalf("replace prefs 2: %v", err)
	}
	items, _, err = repo.List(ctx, corenotify.NotificationQuery{UserID: alice.ID, Limit: 20})
	if err != nil {
		t.Fatalf("list reopened: %v", err)
	}
	if len(items) != 2 {
		t.Fatalf("显式开启后 = %d, want 2（review_pending + sign_failed_daily）", len(items))
	}
	for _, item := range items {
		if item.ID == cred.ID {
			t.Fatalf("credential_invalid 覆盖被替换丢失：%+v", items)
		}
	}
}

// deliveryState 读一行的 (status, attempts, last_error_kind, next_retry_at)。
// deliveryState 读一行的
// (status, attempts, last_error_kind, skip_reason, next_retry_at)。
func deliveryState(t *testing.T, e *testEnv, id string) (string, int64, string, string, string) {
	t.Helper()
	status := e.scalarString(
		"SELECT status FROM notification_deliveries WHERE id = $1::uuid", id)
	attempts := e.scalarInt(
		"SELECT attempts FROM notification_deliveries WHERE id = $1::uuid", id)
	kind := e.scalarString(
		"SELECT last_error_kind FROM notification_deliveries WHERE id = $1::uuid", id)
	skip := e.scalarString(
		"SELECT skip_reason FROM notification_deliveries WHERE id = $1::uuid", id)
	next := e.scalarString(
		"SELECT coalesce(next_retry_at::text, '') FROM notification_deliveries WHERE id = $1::uuid", id)
	return status, attempts, kind, skip, next
}
