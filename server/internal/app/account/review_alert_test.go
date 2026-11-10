package account

// 0v8 B 接线判据：账号进入或回到审核队列后必须把事件交给通知端口。
//
// 覆盖：Create 提交成功发一条待审核事件（业务键带事件 uuid）；
// 两次提交产生两个不同业务键；Update 只在重提面发；
// 只改备注名、凭据恢复与普通编辑不发；
// 端口失败或 panic 不改建号/更新结果。

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corenotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/notify"
	coreschool "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/school"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
)

// reviewFakeRepo 是建号/更新接线用的最小仓储。
// 未实现的方法留在嵌入的 nil 接口上：调用到即 panic，测试立刻暴露越界。
type reviewFakeRepo struct {
	Repo
	current   Record
	created   int
	inserted  []Credential
	setStatus int
}

func (f *reviewFakeRepo) Create(_ context.Context, in New, _ int) (Record, error) {
	f.created++
	f.current = Record{
		ID:          fmt.Sprintf("acct-%d", f.created),
		SchoolID:    in.SchoolID,
		OwnerUserID: in.OwnerUserID,
		Phone:       in.Phone,
		Name:        in.Name,
		Status:      StatusPending,
	}
	return f.current, nil
}

func (f *reviewFakeRepo) InsertCredential(_ context.Context, in Credential) error {
	f.inserted = append(f.inserted, in)
	return nil
}

func (f *reviewFakeRepo) Get(context.Context, corescope.Scope, string) (Record, error) {
	return f.current, nil
}

func (f *reviewFakeRepo) GetForUpdate(context.Context, corescope.Scope, string) (Record, error) {
	return f.current, nil
}

func (f *reviewFakeRepo) UpdateName(_ context.Context, _ corescope.Scope, _ string, name string, _ time.Time) (Record, error) {
	f.current.Name = name
	return f.current, nil
}

func (f *reviewFakeRepo) SetStatus(_ context.Context, _ corescope.Scope, _ string, from, to Status, _ time.Time, _ string) (Record, error) {
	if f.current.Status != from {
		return Record{}, errs.New(errs.CodeConflict, "status changed")
	}
	f.setStatus++
	f.current.Status = to
	return f.current, nil
}

func (f *reviewFakeRepo) Credential(context.Context, corescope.Scope, string) (Credential, error) {
	// 无既有凭据：rewriteCredential 按新建处理，attachPhoneModel 静默跳过。
	return Credential{}, errs.ErrNotFound
}

// reviewFakeSchools 命中一所院校。
type reviewFakeSchools struct{ SchoolReader }

func (reviewFakeSchools) Get(_ context.Context, id string) (coreschool.Record, error) {
	return coreschool.Record{ID: id, Code: "school", Name: "School"}, nil
}

// reviewFakeOwners 命中一个活跃属主。
type reviewFakeOwners struct{ OwnerReader }

func (reviewFakeOwners) GetByID(_ context.Context, id string) (coreuser.Record, error) {
	return coreuser.Record{ID: id, Email: "review-alert-owner@example.com"}, nil
}

// reviewFakeTx 直接执行事务体。
type reviewFakeTx struct{ app.TxManager }

// reviewFailingTx 让事务整体失败：事务体不执行，用于回滚路径。
type reviewFailingTx struct{ app.TxManager }

func (reviewFailingTx) InTx(context.Context, func(context.Context) error) error {
	return errors.New("transaction rolled back")
}

func (reviewFakeTx) InTx(ctx context.Context, fn func(context.Context) error) error { return fn(ctx) }

// reviewFakeAudit 只记录追加，不校验链。
type reviewFakeAudit struct {
	appaudit.Repo
	entries []coreaudit.Entry
}

func (f *reviewFakeAudit) Append(_ context.Context, in coreaudit.Entry) (coreaudit.Row, error) {
	f.entries = append(f.entries, in)
	return coreaudit.Row{}, nil
}

// recordingReviewAlerts 记录待审核事件。可注入错误或 panic。
type recordingReviewAlerts struct {
	events   []corenotify.Event
	err      error
	panicNow bool
}

func (a *recordingReviewAlerts) Dispatch(_ context.Context, event corenotify.Event) error {
	if a.panicNow {
		panic("review alert panic")
	}
	a.events = append(a.events, event)
	return a.err
}

func newReviewAlertService(t *testing.T, repo Repo, alerts ReviewAlerts) *Service {
	t.Helper()
	return newReviewAlertServiceWith(t, repo, alerts, reviewFakeTx{}, nil)
}

// newReviewAlertServiceWith 允许用例替换事务管理与 logger。
func newReviewAlertServiceWith(t *testing.T, repo Repo, alerts ReviewAlerts,
	tx app.TxManager, logger *slog.Logger) *Service {
	t.Helper()
	cipher, err := NewCipher([]byte("0123456789abcdef0123456789abcdef"))
	if err != nil {
		t.Fatalf("NewCipher: %v", err)
	}
	svc, err := NewService(Deps{
		Accounts:     repo,
		Schools:      reviewFakeSchools{},
		Audit:        &reviewFakeAudit{},
		Tx:           tx,
		Clock:        clock.Fixed{T: time.Date(2026, 10, 29, 6, 0, 0, 0, time.UTC)},
		Cipher:       cipher,
		Owner:        reviewFakeOwners{},
		Device:       &fakeDeviceReader{},
		Quota:        fixedQuota(10),
		ReviewAlerts: alerts,
		Logger:       logger,
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc
}

func createInput(phone string) CreateInput {
	return CreateInput{
		SchoolID:    "school-1",
		OwnerUserID: "owner-1",
		Phone:       phone,
		Password:    "test-password",
	}
}

// Create 提交成功即发待审核事件；每次提交都是新业务键。
func TestCreateDispatchesReviewPending(t *testing.T) {
	repo := &reviewFakeRepo{}
	alerts := &recordingReviewAlerts{}
	svc := newReviewAlertService(t, repo, alerts)

	first, err := svc.Create(context.Background(), corescope.Master(), createInput("13800000001"),
		appaudit.Actor{UserID: "master-1", Role: "master"})
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	second, err := svc.Create(context.Background(), corescope.Master(), createInput("13800000002"),
		appaudit.Actor{UserID: "master-1", Role: "master"})
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	if len(alerts.events) != 2 {
		t.Fatalf("待审核事件 = %d, want 2", len(alerts.events))
	}
	event := alerts.events[0]
	if event.Category != corenotify.CategoryReviewPending ||
		event.SchoolID != "school-1" || event.AccountID != first.ID ||
		!strings.HasPrefix(event.DedupeKey, "review_pending/"+first.ID+"/") {
		t.Fatalf("事件 = %+v", event)
	}
	if alerts.events[1].DedupeKey == event.DedupeKey {
		t.Fatalf("两次提交得到同一业务键 %q", event.DedupeKey)
	}
	if alerts.events[1].AccountID != second.ID {
		t.Fatalf("第二条事件账号 = %q, want %q", alerts.events[1].AccountID, second.ID)
	}
}

// Update 只在重提面发：被拒账号改凭据回到 pending。
func TestUpdateResubmitDispatchesReviewPending(t *testing.T) {
	repo := &reviewFakeRepo{current: Record{
		ID: "acct-1", SchoolID: "school-1", OwnerUserID: "owner-1",
		Phone: "13800000001", Status: StatusRejected,
	}}
	alerts := &recordingReviewAlerts{}
	svc := newReviewAlertService(t, repo, alerts)

	password := "new-password"
	if _, err := svc.Update(context.Background(), corescope.Master(), "acct-1",
		UpdateInput{Password: &password}, appaudit.Actor{UserID: "master-1", Role: "master"}); err != nil {
		t.Fatalf("Update: %v", err)
	}
	if repo.setStatus != 1 || repo.current.Status != StatusPending {
		t.Fatalf("状态 = %q（迁移次数 %d），want pending", repo.current.Status, repo.setStatus)
	}
	if len(alerts.events) != 1 {
		t.Fatalf("待审核事件 = %d, want 1", len(alerts.events))
	}
	if event := alerts.events[0]; event.AccountID != "acct-1" ||
		event.Category != corenotify.CategoryReviewPending ||
		!strings.HasPrefix(event.DedupeKey, "review_pending/acct-1/") {
		t.Fatalf("事件 = %+v", event)
	}
}

// 普通编辑与凭据恢复都不发：只有状态回到 pending 才发。
func TestUpdateWithoutResubmitDoesNotDispatch(t *testing.T) {
	cases := []struct {
		name    string
		current Record
		in      UpdateInput
	}{
		{
			name: "只改备注名",
			current: Record{ID: "acct-1", SchoolID: "school-1", OwnerUserID: "owner-1",
				Phone: "13800000001", Status: StatusActive},
			in: UpdateInput{Name: ptr("新的备注名")},
		},
		{
			name: "凭据恢复",
			current: Record{ID: "acct-1", SchoolID: "school-1", OwnerUserID: "owner-1",
				Phone: "13800000001", Status: StatusCredentialError},
			in: UpdateInput{Password: ptr("new-password")},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			repo := &reviewFakeRepo{current: tc.current}
			alerts := &recordingReviewAlerts{}
			svc := newReviewAlertService(t, repo, alerts)

			if _, err := svc.Update(context.Background(), corescope.Master(), "acct-1", tc.in,
				appaudit.Actor{UserID: "master-1", Role: "master"}); err != nil {
				t.Fatalf("Update: %v", err)
			}
			if len(alerts.events) != 0 {
				t.Fatalf("非重提路径仍发事件：%+v", alerts.events)
			}
		})
	}
}

// 端口失败或 panic 不改建号与更新结果。
func TestReviewAlertFailureDoesNotChangeResult(t *testing.T) {
	for _, tc := range []struct {
		name   string
		alerts *recordingReviewAlerts
	}{
		{"错误", &recordingReviewAlerts{err: errors.New("dispatch failed")}},
		{"panic", &recordingReviewAlerts{panicNow: true}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			repo := &reviewFakeRepo{}
			svc := newReviewAlertService(t, repo, tc.alerts)

			if _, err := svc.Create(context.Background(), corescope.Master(), createInput("13800000001"),
				appaudit.Actor{UserID: "master-1", Role: "master"}); err != nil {
				t.Fatalf("Create 不得因投递失败返回错误: %v", err)
			}
			rejected := &reviewFakeRepo{current: Record{
				ID: "acct-1", SchoolID: "school-1", OwnerUserID: "owner-1",
				Phone: "13800000001", Status: StatusRejected,
			}}
			svc = newReviewAlertService(t, rejected, tc.alerts)
			if _, err := svc.Update(context.Background(), corescope.Master(), "acct-1",
				UpdateInput{Password: ptr("new-password")},
				appaudit.Actor{UserID: "master-1", Role: "master"}); err != nil {
				t.Fatalf("Update 不得因投递失败返回错误: %v", err)
			}
		})
	}
}

// 事务回滚不发通知：Create 与 Update 都只在提交成功后投递。
func TestReviewAlertNotDispatchedOnRollback(t *testing.T) {
	alerts := &recordingReviewAlerts{}
	svc := newReviewAlertServiceWith(t, &reviewFakeRepo{}, alerts, reviewFailingTx{}, nil)

	_, err := svc.Create(context.Background(), corescope.Master(), createInput("13800000001"),
		appaudit.Actor{UserID: "master-1", Role: "master"})
	if err == nil {
		t.Fatal("事务失败时 Create 必须返回错误")
	}
	if len(alerts.events) != 0 {
		t.Fatalf("回滚的 Create 仍发事件：%+v", alerts.events)
	}

	rejected := &reviewFakeRepo{current: Record{
		ID: "acct-1", SchoolID: "school-1", OwnerUserID: "owner-1",
		Phone: "13800000001", Status: StatusRejected,
	}}
	svc = newReviewAlertServiceWith(t, rejected, alerts, reviewFailingTx{}, nil)
	if _, err := svc.Update(context.Background(), corescope.Master(), "acct-1",
		UpdateInput{Password: ptr("new-password")},
		appaudit.Actor{UserID: "master-1", Role: "master"}); err == nil {
		t.Fatal("事务失败时 Update 必须返回错误")
	}
	if len(alerts.events) != 0 {
		t.Fatalf("回滚的 Update 仍发事件：%+v", alerts.events)
	}
	if rejected.current.Status != StatusRejected {
		t.Fatalf("回滚后状态 = %q, want rejected", rejected.current.Status)
	}
}

// 投递端口 panic 要留一条不含 panic 值的显性告警。
func TestReviewAlertPanicIsLogged(t *testing.T) {
	var logs bytes.Buffer
	svc := newReviewAlertServiceWith(t, &reviewFakeRepo{}, &recordingReviewAlerts{panicNow: true},
		reviewFakeTx{}, slog.New(slog.NewTextHandler(&logs, nil)))

	if _, err := svc.Create(context.Background(), corescope.Master(), createInput("13800000001"),
		appaudit.Actor{UserID: "master-1", Role: "master"}); err != nil {
		t.Fatalf("Create 不得因投递 panic 返回错误: %v", err)
	}
	logged := logs.String()
	if !strings.Contains(logged, "notify.dispatch_panic") {
		t.Fatalf("缺 notify.dispatch_panic 告警：%s", logged)
	}
	if strings.Contains(logged, "review alert panic") {
		t.Fatalf("日志复述了 panic 值：%s", logged)
	}
}

// ptr 返回值的地址，供可选字段使用。
func ptr[T any](value T) *T { return &value }
