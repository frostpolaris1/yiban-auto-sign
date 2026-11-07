package account

// 批量动作用例的守卫单测。
//
// # 功能
// 钉住六种动作到既有单条用例的映射、混合成败按序返回、
// 失败后继续、请求级校验（动作未知、ids 空/超限/重复）、
// reason 只进驳回，以及 master/admin 角色门槛。
//
// # 口径
// 仓储替身只记录调用形状；真库与真 HTTP 行为由 cmd/server 的 e2e 负责。
// 单条失败按 id 注入：观察该条之外的条目仍被处理。

import (
	"context"
	"slices"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// batchTxManager 直接执行事务体：替身不模拟引擎的原子性。
type batchTxManager struct{ app.TxManager }

func (batchTxManager) InTx(ctx context.Context, fn func(ctx context.Context) error) error {
	return fn(ctx)
}

// recordingAuditRepo 记录审计行，供按 target_id 计数。
type recordingAuditRepo struct {
	appaudit.Repo
	entries []coreaudit.Entry
}

func (r *recordingAuditRepo) Append(_ context.Context, in coreaudit.Entry) (coreaudit.Row, error) {
	r.entries = append(r.entries, in)
	return coreaudit.Row{}, nil
}

// batchRepo 是只服务批量用例的账号仓储替身。
// errs 按 id 注入单条失败；records 服务 Delete 用例的先读。
type batchRepo struct {
	Repo
	records map[string]Record
	errs    map[string]error

	reviews    []string // "id|approve|reason"
	setStatus  []string // "id|from|to"
	softDelete []string // id
	restores   []string // id
}

func (f *batchRepo) failFor(id string) error { return f.errs[id] }

func (f *batchRepo) Get(_ context.Context, _ corescope.Scope, id string) (Record, error) {
	if err := f.failFor(id); err != nil {
		return Record{}, err
	}
	rec, ok := f.records[id]
	if !ok {
		return Record{}, errs.New(errs.CodeNotFound, "not found")
	}
	return rec, nil
}

func (f *batchRepo) Review(_ context.Context, _ corescope.Scope, id string, approve bool, reason, _ string, _ time.Time) (Record, error) {
	f.reviews = append(f.reviews, id+"|"+boolText(approve)+"|"+reason)
	return Record{ID: id}, f.failFor(id)
}

func (f *batchRepo) SetStatus(_ context.Context, _ corescope.Scope, id string, from, to Status, _ time.Time, _ string) (Record, error) {
	f.setStatus = append(f.setStatus, id+"|"+string(from)+"|"+string(to))
	return Record{ID: id}, f.failFor(id)
}

func (f *batchRepo) SoftDelete(_ context.Context, _ corescope.Scope, id, _ string, _ time.Time) error {
	f.softDelete = append(f.softDelete, id)
	return f.failFor(id)
}

func (f *batchRepo) Restore(_ context.Context, _ corescope.Scope, id string, _, _ int) (Record, error) {
	f.restores = append(f.restores, id)
	return Record{ID: id}, f.failFor(id)
}

func boolText(v bool) string {
	if v {
		return "true"
	}
	return "false"
}

// newBatchService 构造只服务批量用例的服务。
func newBatchService(t *testing.T, repo *batchRepo, audit *recordingAuditRepo) *Service {
	t.Helper()
	svc, err := NewService(Deps{
		Accounts:   repo,
		Schools:    fakeSchoolReader{},
		Audit:      audit,
		Tx:         batchTxManager{},
		Clock:      clock.Fixed{T: time.Date(2026, 11, 7, 6, 0, 0, 0, time.UTC)},
		Owner:      fakeOwnerReader{},
		Device:     &fakeDeviceReader{},
		Limit:      10,
		OwnerLimit: 10,
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc
}

func batchAuditCount(audit *recordingAuditRepo, targetID string) int {
	count := 0
	for _, entry := range audit.entries {
		if entry.TargetID == targetID {
			count++
		}
	}
	return count
}

// 六种动作各映射到正确的既有用例：审核带决定与理由、暂停按
// paused_admin 形状比较交换、软删与恢复走各自路径。
func TestBatchMapsActionsToExistingUseCases(t *testing.T) {
	cases := []struct {
		name      string
		action    string
		check     func(t *testing.T, repo *batchRepo)
		wantAudit coreaudit.Action // 成功项应写出的审计动作；空串表示不检查
	}{
		{
			name:   "review_approve",
			action: BatchReviewApprove,
			check: func(t *testing.T, repo *batchRepo) {
				if len(repo.reviews) != 1 || repo.reviews[0] != "a1|true|" {
					t.Fatalf("reviews = %v, want [a1|true|]", repo.reviews)
				}
			},
			wantAudit: coreaudit.ActionAccountReviewPass,
		},
		{
			name:   "review_reject",
			action: BatchReviewReject,
			check: func(t *testing.T, repo *batchRepo) {
				if len(repo.reviews) != 1 || repo.reviews[0] != "a1|false|合成驳回理由" {
					t.Fatalf("reviews = %v, want [a1|false|合成驳回理由]", repo.reviews)
				}
			},
			wantAudit: coreaudit.ActionAccountReviewReject,
		},
		{
			name:   "pause",
			action: BatchPause,
			check: func(t *testing.T, repo *batchRepo) {
				if len(repo.setStatus) != 1 || repo.setStatus[0] != "a1|active|paused_admin" {
					t.Fatalf("setStatus = %v, want [a1|active|paused_admin]", repo.setStatus)
				}
			},
			wantAudit: coreaudit.ActionAccountPause,
		},
		{
			name:   "resume",
			action: BatchResume,
			check: func(t *testing.T, repo *batchRepo) {
				if len(repo.setStatus) != 1 || repo.setStatus[0] != "a1|paused_admin|active" {
					t.Fatalf("setStatus = %v, want [a1|paused_admin|active]", repo.setStatus)
				}
			},
			wantAudit: coreaudit.ActionAccountResume,
		},
		{
			name:   "delete",
			action: BatchDelete,
			check: func(t *testing.T, repo *batchRepo) {
				if len(repo.softDelete) != 1 || repo.softDelete[0] != "a1" {
					t.Fatalf("softDelete = %v, want [a1]", repo.softDelete)
				}
			},
			wantAudit: coreaudit.ActionAccountDelete,
		},
		{
			name:   "restore",
			action: BatchRestore,
			check: func(t *testing.T, repo *batchRepo) {
				if len(repo.restores) != 1 || repo.restores[0] != "a1" {
					t.Fatalf("restores = %v, want [a1]", repo.restores)
				}
			},
			wantAudit: coreaudit.ActionAccountRestore,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			repo := &batchRepo{records: map[string]Record{"a1": {ID: "a1"}}}
			audit := &recordingAuditRepo{}
			svc := newBatchService(t, repo, audit)
			actor := appaudit.Actor{UserID: "boss-1", Role: "master"}

			results, err := svc.Batch(context.Background(), corescope.Master(),
				tc.action, []string{"a1"}, "合成驳回理由", actor)
			if err != nil {
				t.Fatalf("Batch(%s): %v", tc.action, err)
			}
			if len(results) != 1 || results[0].ID != "a1" || !results[0].OK() {
				t.Fatalf("results = %+v, want one ok item for a1", results)
			}
			tc.check(t, repo)
			if got := batchAuditCount(audit, "a1"); got != 1 {
				t.Fatalf("audit rows for a1 = %d, want 1", got)
			}
			if tc.wantAudit != "" && audit.entries[0].Action != tc.wantAudit {
				t.Fatalf("audit action = %q, want %q", audit.entries[0].Action, tc.wantAudit)
			}
		})
	}

	// 校验全集与派发同集：batchActions 里每个动作都必须命中仓储；
	// 漏配派发分支的动作落进 default，即 internal_error，本断言红。
	actor := appaudit.Actor{UserID: "boss-1", Role: "master"}
	for _, action := range batchActions {
		repo := &batchRepo{records: map[string]Record{"a1": {ID: "a1"}}}
		svc := newBatchService(t, repo, &recordingAuditRepo{})
		if err := svc.batchApply(context.Background(), corescope.Master(),
			action, "a1", "驳回", actor); err != nil {
			t.Fatalf("action %q hit the dispatch default branch: %v", action, err)
		}
	}
}

// 混合成败按序返回：失败只落该条，后续条目继续处理。
func TestBatchContinuesAfterFailureAndKeepsOrder(t *testing.T) {
	repo := &batchRepo{
		records: map[string]Record{"a1": {ID: "a1"}, "a3": {ID: "a3"}},
		errs:    map[string]error{"a2": errs.New(errs.CodeNotFound, "not found")},
	}
	audit := &recordingAuditRepo{}
	svc := newBatchService(t, repo, audit)
	actor := appaudit.Actor{UserID: "boss-1", Role: "master"}

	results, err := svc.Batch(context.Background(), corescope.Master(),
		BatchPause, []string{"a1", "a2", "a3"}, "", actor)
	if err != nil {
		t.Fatalf("Batch: %v", err)
	}
	if len(results) != 3 {
		t.Fatalf("results len = %d, want 3", len(results))
	}
	if !results[0].OK() || results[0].ID != "a1" {
		t.Fatalf("results[0] = %+v, want ok a1", results[0])
	}
	if results[1].OK() || results[1].ID != "a2" || results[1].Code != errs.CodeNotFound {
		t.Fatalf("results[1] = %+v, want error a2 not_found", results[1])
	}
	if results[1].Message == "" {
		t.Fatal("an errored item must carry a message")
	}
	if !results[2].OK() || results[2].ID != "a3" {
		t.Fatalf("results[2] = %+v, want ok a3", results[2])
	}
	// 失败后的条目仍被处理：仓储收到了 a3 的调用（a2 自己的尝试也留痕）。
	if len(repo.setStatus) != 3 {
		t.Fatalf("setStatus calls = %d, want 3 (a1, a2 attempted, a3)", len(repo.setStatus))
	}
	if repo.setStatus[2] != "a3|active|paused_admin" {
		t.Fatalf("setStatus[2] = %q, want the a3 entry after the failure", repo.setStatus[2])
	}
	// 逐条审计：成功项各一条，失败项零条。
	for id, want := range map[string]int{"a1": 1, "a2": 0, "a3": 1} {
		if got := batchAuditCount(audit, id); got != want {
			t.Fatalf("audit rows for %s = %d, want %d", id, got, want)
		}
	}
}

// batchActions 与六个动作常量逐值相等：运行时接受集不得多出契约外
// 字面量，也不得漏值或换序；漂移即红。契约侧同集守卫在 entry/http/oapi。
func TestBatchActionsSliceMatchesConstants(t *testing.T) {
	want := []string{
		BatchReviewApprove,
		BatchReviewReject,
		BatchPause,
		BatchResume,
		BatchDelete,
		BatchRestore,
	}
	if !slices.Equal(batchActions, want) {
		t.Fatalf("batchActions = %v, want %v", batchActions, want)
	}
}

// 派发 default 分支：未知动作记 internal_error，不静默 ok。
func TestBatchApplyUnknownActionIsInternalError(t *testing.T) {
	repo := &batchRepo{records: map[string]Record{"a1": {ID: "a1"}}}
	svc := newBatchService(t, repo, &recordingAuditRepo{})
	actor := appaudit.Actor{UserID: "boss-1", Role: "master"}

	err := svc.batchApply(context.Background(), corescope.Master(),
		"freeze", "a1", "", actor)
	if err == nil {
		t.Fatal("an unknown action must not read as a success")
	}
	if code := errs.From(err).Code; code != errs.CodeInternal {
		t.Fatalf("code = %q, want internal_error", code)
	}
	if len(repo.reviews)+len(repo.setStatus)+len(repo.softDelete)+len(repo.restores) != 0 {
		t.Fatal("an unknown action must not touch any use case")
	}
}

// 请求级校验：动作未知、ids 空、超限、重复都整批拒绝，零调用。
func TestBatchValidatesActionAndIds(t *testing.T) {
	repo := &batchRepo{}
	audit := &recordingAuditRepo{}
	svc := newBatchService(t, repo, audit)
	actor := appaudit.Actor{UserID: "boss-1", Role: "master"}
	ctx := context.Background()

	cases := []struct {
		name   string
		action string
		ids    []string
	}{
		{"unknown action", "freeze", []string{"a1"}},
		{"empty ids", BatchPause, nil},
		{"eleven ids", BatchPause, []string{"a1", "a2", "a3", "a4", "a5", "a6", "a7", "a8", "a9", "a10", "a11"}},
		{"duplicate ids", BatchPause, []string{"a1", "a1"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			results, err := svc.Batch(ctx, corescope.Master(), tc.action, tc.ids, "", actor)
			if err == nil {
				t.Fatalf("Batch(%s) = %+v, want a validation error", tc.name, results)
			}
			if code := errs.From(err).Code; code != errs.CodeValidation {
				t.Fatalf("code = %q, want validation_failed", code)
			}
			if results != nil {
				t.Fatalf("results = %+v, want nil", results)
			}
		})
	}
	if len(repo.reviews)+len(repo.setStatus)+len(repo.softDelete)+len(repo.restores) != 0 {
		t.Fatal("a rejected request must not touch any use case")
	}
	if len(audit.entries) != 0 {
		t.Fatal("a rejected request must not write any audit row")
	}
}

// reason 只进驳回：其余动作带着非空 reason 也必须忽略它。
func TestBatchReasonOnlyFeedsReject(t *testing.T) {
	repo := &batchRepo{records: map[string]Record{"a1": {ID: "a1"}}}
	audit := &recordingAuditRepo{}
	svc := newBatchService(t, repo, audit)
	actor := appaudit.Actor{UserID: "boss-1", Role: "master"}

	if _, err := svc.Batch(context.Background(), corescope.Master(),
		BatchPause, []string{"a1"}, "应被忽略的理由", actor); err != nil {
		t.Fatalf("pause with a reason: %v", err)
	}
	if len(repo.reviews) != 0 {
		t.Fatalf("pause must not call Review: %v", repo.reviews)
	}
	if len(repo.setStatus) != 1 {
		t.Fatalf("setStatus calls = %d, want 1", len(repo.setStatus))
	}

	repo2 := &batchRepo{records: map[string]Record{"a1": {ID: "a1"}}}
	svc2 := newBatchService(t, repo2, &recordingAuditRepo{})
	if _, err := svc2.Batch(context.Background(), corescope.Master(),
		BatchReviewApprove, []string{"a1"}, "应被忽略的理由", actor); err != nil {
		t.Fatalf("approve with a reason: %v", err)
	}
	if len(repo2.reviews) != 1 || repo2.reviews[0] != "a1|true|" {
		t.Fatalf("approve reviews = %v, want the reason dropped", repo2.reviews)
	}
}

// 角色门槛：user 一律 403，整批拒绝、零调用零审计。
func TestBatchRequiresManagerRole(t *testing.T) {
	repo := &batchRepo{}
	audit := &recordingAuditRepo{}
	svc := newBatchService(t, repo, audit)
	actor := appaudit.Actor{UserID: "u1", Role: "user"}

	_, err := svc.Batch(context.Background(), corescope.User("u1"),
		BatchPause, []string{"a1"}, "", actor)
	if err == nil {
		t.Fatal("a user must not run batch actions")
	}
	if code := errs.From(err).Code; code != errs.CodeForbidden {
		t.Fatalf("code = %q, want forbidden", code)
	}
	if len(repo.setStatus) != 0 || len(audit.entries) != 0 {
		t.Fatal("a forbidden request must not touch the repo or the audit log")
	}
}
