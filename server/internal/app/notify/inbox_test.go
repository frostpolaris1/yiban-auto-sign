package notify_test

// 站内信读用例的判据：limit 单独定型、用户作用域逐调用传递、
// id 形状与批量上限在用例层拒绝。

import (
	"context"
	"testing"
	"time"

	appnotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/notify"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corenotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/notify"
)

type fakeInboxRepo struct {
	items      []corenotify.Notification
	next       string
	unread     int64
	listErr    error
	countErr   error
	readErr    error
	allErr     error
	listQuery  corenotify.NotificationQuery
	countUser  string
	readUser   string
	readIDs    []string
	readAt     time.Time
	allUser    string
	allReadAt  time.Time
	listCalled bool
}

func (f *fakeInboxRepo) List(_ context.Context, query corenotify.NotificationQuery) ([]corenotify.Notification, string, error) {
	f.listCalled = true
	f.listQuery = query
	if f.listErr != nil {
		return nil, "", f.listErr
	}
	return f.items, f.next, nil
}

func (f *fakeInboxRepo) UnreadCount(_ context.Context, userID string) (int64, error) {
	f.countUser = userID
	if f.countErr != nil {
		return 0, f.countErr
	}
	return f.unread, nil
}

func (f *fakeInboxRepo) MarkRead(_ context.Context, userID string, ids []string, now time.Time) (int64, error) {
	f.readUser = userID
	f.readIDs = append([]string(nil), ids...)
	f.readAt = now
	return int64(len(ids)), f.readErr
}

func (f *fakeInboxRepo) MarkAllRead(_ context.Context, userID string, now time.Time) (int64, error) {
	f.allUser = userID
	f.allReadAt = now
	return 1, f.allErr
}

var inboxNow = time.Date(2026, 11, 5, 10, 0, 0, 0, time.UTC)

func newInbox(t *testing.T, repo appnotify.InboxRepo) *appnotify.Inbox {
	t.Helper()
	svc, err := appnotify.NewInbox(appnotify.InboxDeps{
		Repo:  repo,
		Clock: coreclock.Fixed{T: inboxNow},
	})
	if err != nil {
		t.Fatalf("NewInbox: %v", err)
	}
	return svc
}

func TestInboxListPassesQuery(t *testing.T) {
	repo := &fakeInboxRepo{items: []corenotify.Notification{{ID: "n1"}}, next: "next"}
	page, err := newInbox(t, repo).List(context.Background(), "user-1", "cursor-1", 20)
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	if repo.listQuery.UserID != "user-1" || repo.listQuery.Cursor != "cursor-1" ||
		repo.listQuery.Limit != 20 {
		t.Fatalf("query = %+v, want user/cursor/limit 20", repo.listQuery)
	}
	if len(page.Items) != 1 || page.NextCursor != "next" {
		t.Fatalf("page = %+v, want one item and next cursor", page)
	}
}

func TestInboxListRejectsLimitOutsideRange(t *testing.T) {
	for _, limit := range []int{0, -1, 101} {
		repo := &fakeInboxRepo{}
		_, err := newInbox(t, repo).List(context.Background(), "user-1", "", limit)
		if codeOf(err) != errs.CodeValidation {
			t.Fatalf("limit %d: code = %v, want validation_failed", limit, codeOf(err))
		}
		if repo.listCalled {
			t.Fatalf("limit %d: repo was called despite the validation failure", limit)
		}
	}
	for _, limit := range []int{1, 100} {
		repo := &fakeInboxRepo{}
		if _, err := newInbox(t, repo).List(context.Background(), "user-1", "", limit); err != nil {
			t.Fatalf("limit %d: %v", limit, err)
		}
		if repo.listQuery.Limit != limit {
			t.Fatalf("limit %d: repo saw %d", limit, repo.listQuery.Limit)
		}
	}
}

func TestInboxUnreadCountUsesCaller(t *testing.T) {
	repo := &fakeInboxRepo{unread: 7}
	count, err := newInbox(t, repo).UnreadCount(context.Background(), "user-1")
	if err != nil {
		t.Fatalf("UnreadCount: %v", err)
	}
	if count != 7 || repo.countUser != "user-1" {
		t.Fatalf("count = %d user = %q, want 7 for user-1", count, repo.countUser)
	}
}

func TestInboxMarkReadValidatesAndPassesCaller(t *testing.T) {
	repo := &fakeInboxRepo{}
	svc := newInbox(t, repo)
	valid := "018f4a4e-0000-7000-8000-000000000001"

	for _, tc := range []struct {
		name string
		ids  []string
	}{
		{name: "empty", ids: []string{}},
		{name: "too many", ids: make([]string, 101)},
		{name: "not uuid", ids: []string{"not-a-uuid"}},
	} {
		if err := svc.MarkRead(context.Background(), "user-1", tc.ids); codeOf(err) != errs.CodeValidation {
			t.Fatalf("%s: code = %v, want validation_failed", tc.name, codeOf(err))
		}
	}
	if repo.readUser != "" {
		t.Fatalf("repo was called despite validation failures: %q", repo.readUser)
	}

	if err := svc.MarkRead(context.Background(), "user-1", []string{valid}); err != nil {
		t.Fatalf("MarkRead: %v", err)
	}
	if repo.readUser != "user-1" || len(repo.readIDs) != 1 || repo.readIDs[0] != valid {
		t.Fatalf("read call = user %q ids %v", repo.readUser, repo.readIDs)
	}
	if !repo.readAt.Equal(inboxNow) {
		t.Fatalf("read timestamp = %v, want %v", repo.readAt, inboxNow)
	}
}

func TestInboxMarkAllReadUsesCaller(t *testing.T) {
	repo := &fakeInboxRepo{}
	if err := newInbox(t, repo).MarkAllRead(context.Background(), "user-2"); err != nil {
		t.Fatalf("MarkAllRead: %v", err)
	}
	if repo.allUser != "user-2" || !repo.allReadAt.Equal(inboxNow) {
		t.Fatalf("mark-all call = user %q at %v", repo.allUser, repo.allReadAt)
	}
}

func TestInboxRejectsMissingUser(t *testing.T) {
	repo := &fakeInboxRepo{}
	svc := newInbox(t, repo)
	if _, err := svc.List(context.Background(), "", "", 20); codeOf(err) != errs.CodeInternal {
		t.Fatalf("List: code = %v, want internal_error", codeOf(err))
	}
	if _, err := svc.UnreadCount(context.Background(), ""); codeOf(err) != errs.CodeInternal {
		t.Fatalf("UnreadCount: code = %v, want internal_error", codeOf(err))
	}
	if err := svc.MarkRead(context.Background(), "", []string{"018f4a4e-0000-7000-8000-000000000001"}); codeOf(err) != errs.CodeInternal {
		t.Fatalf("MarkRead: code = %v, want internal_error", codeOf(err))
	}
	if err := svc.MarkAllRead(context.Background(), ""); codeOf(err) != errs.CodeInternal {
		t.Fatalf("MarkAllRead: code = %v, want internal_error", codeOf(err))
	}
}

func TestNewInboxRequiresDeps(t *testing.T) {
	if _, err := appnotify.NewInbox(appnotify.InboxDeps{Clock: coreclock.Fixed{}}); err == nil {
		t.Fatal("NewInbox accepted a nil repo")
	}
	if _, err := appnotify.NewInbox(appnotify.InboxDeps{Repo: &fakeInboxRepo{}}); err == nil {
		t.Fatal("NewInbox accepted a nil clock")
	}
}

// codeOf 返回稳定错误码；nil 直接失败，避免把断言错写成 panic。
func codeOf(err error) errs.Code {
	if err == nil {
		return ""
	}
	return errs.From(err).Code
}
