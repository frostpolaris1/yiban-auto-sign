package user

// 批量动作用例的守卫单测。
//
// # 功能
// 钉住五种动作到既有单条用例的映射、混合成败按序返回、
// 失败后继续、请求级校验（动作未知、ids 空/超限/重复）、
// temp_password 只随 reset 成功项出现，以及 master 角色门槛。
//
// # 口径
// 仓储替身只记录调用形状；真库与真 HTTP 行为由 cmd/server 的 e2e 负责。
// 单条失败按 id 注入：观察该条之外的条目仍被处理。
// 最后 master 保护沿用 fake 仓储里的 active-only 复算口径。

import (
	"context"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
)

// batchUserIDs 是本文件的合成用户 id（零前缀合成段）。
var batchUserIDs = []string{
	"00000000-0000-4000-8000-0000000000b1",
	"00000000-0000-4000-8000-0000000000b2",
	"00000000-0000-4000-8000-0000000000b3",
}

// batchMasterID 是批量测试的 master actor id，
// 也是 batchSeedRecords 铺下的第三个合成用户（唯一 master）。
const batchMasterID = "00000000-0000-4000-8000-0000000000b3"

// batchUserRepo 是只服务批量用例的用户仓储替身。
// errs 按 id 注入单条失败；records 服务各用例的先读。
// GetByID 与真仓储同口径：只回未删行，已删与不存在同判 404。
type batchUserRepo struct {
	Repo
	records map[string]coreuser.Record
	errs    map[string]error

	setStatus   []string // "id|to"
	passwords   []string // id
	softDeleted []string // id
	restores    []string // id
}

func (f *batchUserRepo) failFor(id string) error { return f.errs[id] }

func (f *batchUserRepo) GetByID(_ context.Context, id string) (Record, error) {
	if err := f.failFor(id); err != nil {
		return Record{}, err
	}
	rec, ok := f.records[id]
	if !ok || rec.DeletedAt != nil {
		return Record{}, errs.ErrNotFound
	}
	return rec, nil
}

func (f *batchUserRepo) SetStatus(_ context.Context, id string, to coreauth.Status, _ time.Time) error {
	if err := f.failFor(id); err != nil {
		return err
	}
	rec, ok := f.records[id]
	if !ok || rec.DeletedAt != nil {
		return errs.ErrNotFound
	}
	rec.Status = to
	f.records[id] = rec
	f.setStatus = append(f.setStatus, id+"|"+string(to))
	return nil
}

func (f *batchUserRepo) UpdatePassword(_ context.Context, id, _ string, _ bool, _ time.Time) error {
	f.passwords = append(f.passwords, id)
	return f.failFor(id)
}

func (f *batchUserRepo) SoftDelete(_ context.Context, id, _ string, at time.Time) error {
	if err := f.failFor(id); err != nil {
		return err
	}
	rec, ok := f.records[id]
	if !ok || rec.DeletedAt != nil {
		return errs.ErrNotFound
	}
	rec.DeletedAt = &at
	f.records[id] = rec
	f.softDeleted = append(f.softDeleted, id)
	return nil
}

func (f *batchUserRepo) Restore(_ context.Context, id string, _ int) (Record, error) {
	if err := f.failFor(id); err != nil {
		return Record{}, err
	}
	rec, ok := f.records[id]
	if !ok || rec.DeletedAt == nil {
		return Record{}, errs.ErrNotFound
	}
	rec.DeletedAt = nil
	f.records[id] = rec
	f.restores = append(f.restores, id)
	return rec, nil
}

// CountLiveMasters 按判据 role='master' AND status='active' AND deleted_at IS NULL
// 复算，排除 excludeID。
func (f *batchUserRepo) CountLiveMasters(_ context.Context, excludeID string) (int, error) {
	n := 0
	for id, rec := range f.records {
		if id == excludeID {
			continue
		}
		if rec.Role == coreauth.RoleMaster && rec.Status == coreauth.StatusActive && rec.DeletedAt == nil {
			n++
		}
	}
	return n, nil
}

func (f *batchUserRepo) OwnerCounts(context.Context, string) (coreuser.OwnerCounts, error) {
	return coreuser.OwnerCounts{}, nil
}

// batchIssuer 记录会话撤销调用。
type batchIssuer struct{ revoked []string }

func (b *batchIssuer) Issue(context.Context, coreauth.User) (coreauth.Issued, error) {
	return coreauth.Issued{}, errs.New(errs.CodeInternal, "not used")
}

func (b *batchIssuer) Resolve(context.Context, string) (coreauth.Session, error) {
	return coreauth.Session{}, coreauth.ErrNoSession
}

func (b *batchIssuer) Revoke(context.Context, string) error { return nil }

func (b *batchIssuer) RevokeAllForUser(_ context.Context, userID string) error {
	b.revoked = append(b.revoked, userID)
	return nil
}

// newBatchService 构造只服务批量用例的服务。
func newBatchService(t *testing.T, repo *batchUserRepo, audits *fakeAudit, issuer *batchIssuer) *Service {
	t.Helper()
	svc, err := NewService(Deps{
		Users:         repo,
		Audit:         audits,
		Tx:            fakeTx{},
		Clock:         clock.System{},
		KDF:           coreauth.PlainHasher{},
		Limit:         10000,
		Sessions:      issuer,
		OwnerAccounts: fakeCounter{},
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc
}

// batchActor 构造 master 主体。
func batchActor() audit.Actor {
	return audit.Actor{UserID: batchMasterID, Role: coreauth.RoleMaster}
}

// batchSeedRecords 以合成 id 铺三个用户：u1/u2 普通用户、u3 master。
func batchSeedRecords() map[string]coreuser.Record {
	out := map[string]coreuser.Record{}
	for i, id := range batchUserIDs {
		role := coreauth.RoleUser
		if i == 2 {
			role = coreauth.RoleMaster
		}
		out[id] = coreuser.Record{ID: id, Email: "batch-user-" + id + "@example.test", Role: role, Status: coreauth.StatusActive}
	}
	return out
}

// batchSeedDeletedRecords 铺三个已软删用户，供 restore 批的成功项。
func batchSeedDeletedRecords() map[string]coreuser.Record {
	deleted := time.Date(2026, 11, 7, 6, 0, 0, 0, time.UTC)
	out := batchSeedRecords()
	for id, rec := range out {
		rec.DeletedAt = &deleted
		out[id] = rec
	}
	return out
}

// batchAuditCount 数某 target 的审计行。
func batchAuditCount(audits *fakeAudit, targetID string) int {
	count := 0
	for _, entry := range audits.entries {
		if entry.TargetID == targetID {
			count++
		}
	}
	return count
}

// 五种动作各映射到正确的既有用例：停用与启用走 Update 的状态分支、
// 重置口令走 PasswordReset（写哈希、撤会话）、软删与恢复走各自路径。
func TestBatchMapsActionsToExistingUseCases(t *testing.T) {
	cases := []struct {
		name      string
		action    string
		target    string
		check     func(t *testing.T, repo *batchUserRepo, issuer *batchIssuer)
		wantAudit coreaudit.Action
	}{
		{
			name:   "disable",
			action: BatchDisable,
			target: batchUserIDs[0],
			check: func(t *testing.T, repo *batchUserRepo, _ *batchIssuer) {
				t.Helper()
				if len(repo.setStatus) != 1 || repo.setStatus[0] != batchUserIDs[0]+"|disabled" {
					t.Fatalf("setStatus = %v, want [%s|disabled]", repo.setStatus, batchUserIDs[0])
				}
				if len(repo.passwords)+len(repo.softDeleted)+len(repo.restores) != 0 {
					t.Fatal("disable must not touch the other use cases")
				}
			},
			wantAudit: coreaudit.ActionUserDisable,
		},
		{
			name:   "enable",
			action: BatchEnable,
			target: batchUserIDs[0],
			check: func(t *testing.T, repo *batchUserRepo, _ *batchIssuer) {
				t.Helper()
				if len(repo.setStatus) != 1 || repo.setStatus[0] != batchUserIDs[0]+"|active" {
					t.Fatalf("setStatus = %v, want [%s|active]", repo.setStatus, batchUserIDs[0])
				}
			},
			wantAudit: coreaudit.ActionUserEnable,
		},
		{
			name:   "reset_password",
			action: BatchResetPassword,
			target: batchUserIDs[0],
			check: func(t *testing.T, repo *batchUserRepo, issuer *batchIssuer) {
				t.Helper()
				if len(repo.passwords) != 1 || repo.passwords[0] != batchUserIDs[0] {
					t.Fatalf("passwords = %v, want one for %s", repo.passwords, batchUserIDs[0])
				}
				if len(issuer.revoked) != 1 || issuer.revoked[0] != batchUserIDs[0] {
					t.Fatalf("revoked = %v, want one for %s", issuer.revoked, batchUserIDs[0])
				}
			},
			wantAudit: coreaudit.ActionUserPasswordReset,
		},
		{
			name:   "delete",
			action: BatchDelete,
			target: batchUserIDs[0],
			check: func(t *testing.T, repo *batchUserRepo, _ *batchIssuer) {
				t.Helper()
				if len(repo.softDeleted) != 1 || repo.softDeleted[0] != batchUserIDs[0] {
					t.Fatalf("softDeleted = %v, want one for %s", repo.softDeleted, batchUserIDs[0])
				}
			},
			wantAudit: coreaudit.ActionUserDelete,
		},
		{
			name:   "restore",
			action: BatchRestore,
			target: batchUserIDs[0],
			check: func(t *testing.T, repo *batchUserRepo, _ *batchIssuer) {
				t.Helper()
				if len(repo.restores) != 1 || repo.restores[0] != batchUserIDs[0] {
					t.Fatalf("restores = %v, want one for %s", repo.restores, batchUserIDs[0])
				}
			},
			wantAudit: coreaudit.ActionUserRestore,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			records := batchSeedRecords()
			// 目标先行落到动作可成功的形状：enable 要一个已停用行，
			// restore 要一个已软删行，否则单条用例判无变化或 404。
			target := records[tc.target]
			switch tc.action {
			case BatchEnable:
				target.Status = coreauth.StatusDisabled
				records[tc.target] = target
			case BatchRestore:
				deleted := time.Date(2026, 11, 7, 6, 0, 0, 0, time.UTC)
				target.DeletedAt = &deleted
				records[tc.target] = target
			}
			repo := &batchUserRepo{records: records}
			audits := &fakeAudit{}
			issuer := &batchIssuer{}
			svc := newBatchService(t, repo, audits, issuer)
			actor := batchActor()

			results, err := svc.Batch(context.Background(), corescope.Master(),
				tc.action, []string{tc.target}, actor)
			if err != nil {
				t.Fatalf("Batch(%s): %v", tc.action, err)
			}
			if len(results) != 1 || results[0].ID != tc.target || !results[0].OK() {
				t.Fatalf("results = %+v, want one ok item for %s", results, tc.target)
			}
			if tc.action != BatchResetPassword && results[0].TempPassword != "" {
				t.Fatalf("%s must not carry a temp password, got %q", tc.action, results[0].TempPassword)
			}
			if tc.action == BatchResetPassword && results[0].TempPassword == "" {
				t.Fatal("a reset item must carry the one-time temp password")
			}
			tc.check(t, repo, issuer)
			if got := batchAuditCount(audits, tc.target); got != 1 {
				t.Fatalf("audit rows for %s = %d, want 1", tc.target, got)
			}
			if audits.entries[0].Action != tc.wantAudit {
				t.Fatalf("audit action = %q, want %q", audits.entries[0].Action, tc.wantAudit)
			}
		})
	}

	// 校验全集与派发同集：batchActions 里每个动作都必须命中既有用例；
	// 漏配派发分支的动作落进 default，即 internal_error，本断言红。
	// 用例自身按替身状态回的 404 等业务错误不算派发失败。
	for _, action := range batchActions {
		repo := &batchUserRepo{records: batchSeedRecords()}
		svc := newBatchService(t, repo, &fakeAudit{}, &batchIssuer{})
		_, err := svc.batchApply(context.Background(), corescope.Master(),
			action, batchUserIDs[0], batchActor())
		if err != nil && errs.From(err).Code == errs.CodeInternal {
			t.Fatalf("action %q hit the dispatch default branch: %v", action, err)
		}
	}
}

// 混合成败按序返回：失败只落该条，后续条目继续处理。
// 最后 active master 的自停用命中单条用例的 conflict 判据。
func TestBatchContinuesAfterFailureAndKeepsOrder(t *testing.T) {
	repo := &batchUserRepo{records: batchSeedRecords()}
	audits := &fakeAudit{}
	issuer := &batchIssuer{}
	svc := newBatchService(t, repo, audits, issuer)

	results, err := svc.Batch(context.Background(), corescope.Master(),
		BatchDisable, []string{batchMasterID, batchUserIDs[0], batchUserIDs[1]}, batchActor())
	if err != nil {
		t.Fatalf("Batch: %v", err)
	}
	if len(results) != 3 {
		t.Fatalf("results len = %d, want 3", len(results))
	}
	// 第一条：actor 是唯一 active master，停用自己被最后 master 判据拒绝。
	if results[0].OK() || results[0].ID != batchMasterID || results[0].Code != errs.CodeConflict {
		t.Fatalf("results[0] = %+v, want error conflict for the last active master", results[0])
	}
	if results[0].Message == "" {
		t.Fatal("an errored item must carry a message")
	}
	if !results[1].OK() || results[1].ID != batchUserIDs[0] {
		t.Fatalf("results[1] = %+v, want ok for %s", results[1], batchUserIDs[0])
	}
	if !results[2].OK() || results[2].ID != batchUserIDs[1] {
		t.Fatalf("results[2] = %+v, want ok for %s", results[2], batchUserIDs[1])
	}
	// 失败后的条目仍被处理：两条普通用户的停用都落到仓储。
	if len(repo.setStatus) != 2 {
		t.Fatalf("setStatus calls = %d, want 2", len(repo.setStatus))
	}
	// 逐条审计：失败项零条，成功项各一条。
	if got := batchAuditCount(audits, batchMasterID); got != 0 {
		t.Fatalf("audit rows for the failed item = %d, want 0", got)
	}
	for _, id := range batchUserIDs[:2] {
		if got := batchAuditCount(audits, id); got != 1 {
			t.Fatalf("audit rows for %s = %d, want 1", id, got)
		}
	}
}

// 重复判据按 UUID 规范形归一化：同一用户的小写与全大写两种写法
// 两次入批即顶层 400；非 UUID 字面量按原文比较。
func TestBatchDuplicateDetectsCanonicalForms(t *testing.T) {
	repo := &batchUserRepo{records: batchSeedRecords()}
	audits := &fakeAudit{}
	svc := newBatchService(t, repo, audits, &batchIssuer{})

	upper := strings.ToUpper(batchUserIDs[0])
	if upper == batchUserIDs[0] {
		t.Skip("the fixture id has no case variant")
	}
	results, err := svc.Batch(context.Background(), corescope.Master(),
		BatchDisable, []string{batchUserIDs[0], upper}, batchActor())
	if err == nil {
		t.Fatalf("Batch = %+v, want a validation error for the canonical duplicate", results)
	}
	if code := errs.From(err).Code; code != errs.CodeValidation {
		t.Fatalf("code = %q, want validation_failed", code)
	}
	if results != nil {
		t.Fatalf("results = %+v, want nil", results)
	}
	if len(repo.setStatus)+len(repo.passwords)+len(repo.softDeleted)+len(repo.restores) != 0 {
		t.Fatal("a rejected request must not touch any use case")
	}
	if len(audits.entries) != 0 {
		t.Fatal("a rejected request must not write any audit row")
	}
}

// 幂等无变化与单条端点同口径：对已停用用户再停用、对活跃用户再启用，
// item 仍 ok，但不写行、不写审计。
func TestBatchIdempotentNoChangeWritesNoAudit(t *testing.T) {
	// 已停用用户再 disable：ok、零新增审计、零状态写。
	records := batchSeedRecords()
	records[batchUserIDs[0]] = coreuser.Record{
		ID: batchUserIDs[0], Email: "batch-user-" + batchUserIDs[0] + "@example.test",
		Role: coreauth.RoleUser, Status: coreauth.StatusDisabled,
	}
	repo := &batchUserRepo{records: records}
	audits := &fakeAudit{}
	svc := newBatchService(t, repo, audits, &batchIssuer{})

	results, err := svc.Batch(context.Background(), corescope.Master(),
		BatchDisable, []string{batchUserIDs[0]}, batchActor())
	if err != nil {
		t.Fatalf("disable on a disabled user: %v", err)
	}
	if len(results) != 1 || results[0].ID != batchUserIDs[0] || !results[0].OK() {
		t.Fatalf("results = %+v, want one ok item", results)
	}
	if len(repo.setStatus) != 0 {
		t.Fatalf("setStatus calls = %v, want none for a no-change item", repo.setStatus)
	}
	if len(audits.entries) != 0 {
		t.Fatalf("audit rows = %d, want 0 for a no-change item", len(audits.entries))
	}

	// 活跃用户再 enable：同口径。
	repo2 := &batchUserRepo{records: batchSeedRecords()}
	audits2 := &fakeAudit{}
	svc2 := newBatchService(t, repo2, audits2, &batchIssuer{})
	results2, err := svc2.Batch(context.Background(), corescope.Master(),
		BatchEnable, []string{batchUserIDs[0]}, batchActor())
	if err != nil {
		t.Fatalf("enable on an active user: %v", err)
	}
	if len(results2) != 1 || !results2[0].OK() {
		t.Fatalf("results = %+v, want one ok item", results2)
	}
	if len(repo2.setStatus) != 0 || len(audits2.entries) != 0 {
		t.Fatalf("a no-change enable wrote status=%v audit=%d, want none",
			repo2.setStatus, len(audits2.entries))
	}
}

// 注入错误与不存在 id 都按 per-item 处理，后续条目继续。
func TestBatchPerItemErrorCarriesStableCode(t *testing.T) {
	repo := &batchUserRepo{
		records: batchSeedRecords(),
		errs:    map[string]error{batchUserIDs[0]: errs.New(errs.CodeRateLimited, "injected")},
	}
	svc := newBatchService(t, repo, &fakeAudit{}, &batchIssuer{})

	results, err := svc.Batch(context.Background(), corescope.Master(),
		BatchEnable, []string{batchUserIDs[0], "not-a-uuid", batchUserIDs[1]}, batchActor())
	if err != nil {
		t.Fatalf("Batch: %v", err)
	}
	if results[0].OK() || results[0].Code != errs.CodeRateLimited {
		t.Fatalf("results[0] = %+v, want the injected error code", results[0])
	}
	if results[1].OK() || results[1].Code != errs.CodeNotFound {
		t.Fatalf("results[1] = %+v, want not_found for the non-UUID id", results[1])
	}
	if !results[2].OK() {
		t.Fatalf("results[2] = %+v, want ok after two failures", results[2])
	}
}

// batchActions 与五个动作常量逐值相等：运行时接受集不得多出契约外
// 字面量，也不得漏值或换序；漂移即红。契约侧同集守卫在 entry/http/oapi。
func TestBatchActionsSliceMatchesConstants(t *testing.T) {
	want := []string{
		BatchDisable,
		BatchEnable,
		BatchResetPassword,
		BatchDelete,
		BatchRestore,
	}
	if !slices.Equal(batchActions, want) {
		t.Fatalf("batchActions = %v, want %v", batchActions, want)
	}
}

// 派发 default 分支：未知动作记 internal_error，不静默 ok。
func TestBatchApplyUnknownActionIsInternalError(t *testing.T) {
	repo := &batchUserRepo{records: batchSeedRecords()}
	svc := newBatchService(t, repo, &fakeAudit{}, &batchIssuer{})

	_, err := svc.batchApply(context.Background(), corescope.Master(),
		"freeze", batchUserIDs[0], batchActor())
	if err == nil {
		t.Fatal("an unknown action must not read as a success")
	}
	if code := errs.From(err).Code; code != errs.CodeInternal {
		t.Fatalf("code = %q, want internal_error", code)
	}
	if len(repo.setStatus)+len(repo.passwords)+len(repo.softDeleted)+len(repo.restores) != 0 {
		t.Fatal("an unknown action must not touch any use case")
	}
}

// 请求级校验：动作未知、ids 空、超限、重复都整批拒绝，零调用零审计。
func TestBatchValidatesActionAndIds(t *testing.T) {
	repo := &batchUserRepo{}
	audits := &fakeAudit{}
	svc := newBatchService(t, repo, audits, &batchIssuer{})
	ctx := context.Background()

	cases := []struct {
		name   string
		action string
		ids    []string
	}{
		{"unknown action", "freeze", []string{batchUserIDs[0]}},
		{"empty ids", BatchDisable, nil},
		{"eleven ids", BatchDisable, []string{
			"00000000-0000-4000-8000-0000000000c1", "00000000-0000-4000-8000-0000000000c2",
			"00000000-0000-4000-8000-0000000000c3", "00000000-0000-4000-8000-0000000000c4",
			"00000000-0000-4000-8000-0000000000c5", "00000000-0000-4000-8000-0000000000c6",
			"00000000-0000-4000-8000-0000000000c7", "00000000-0000-4000-8000-0000000000c8",
			"00000000-0000-4000-8000-0000000000c9", "00000000-0000-4000-8000-0000000000ca",
			"00000000-0000-4000-8000-0000000000cb",
		}},
		{"duplicate ids", BatchDisable, []string{batchUserIDs[0], batchUserIDs[0]}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			results, err := svc.Batch(ctx, corescope.Master(), tc.action, tc.ids, batchActor())
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
	if len(repo.setStatus)+len(repo.passwords)+len(repo.softDeleted)+len(repo.restores) != 0 {
		t.Fatal("a rejected request must not touch any use case")
	}
	if len(audits.entries) != 0 {
		t.Fatal("a rejected request must not write any audit row")
	}
}

// temp_password 只随 reset 成功项出现：reset 批每条都带且互不相同，
// 其余动作的成功项该字段恒空。
func TestBatchTempPasswordOnlyOnResetItems(t *testing.T) {
	repo := &batchUserRepo{records: batchSeedRecords()}
	svc := newBatchService(t, repo, &fakeAudit{}, &batchIssuer{})

	results, err := svc.Batch(context.Background(), corescope.Master(),
		BatchResetPassword, batchUserIDs[:2], batchActor())
	if err != nil {
		t.Fatalf("reset batch: %v", err)
	}
	for i, result := range results {
		if !result.OK() {
			t.Fatalf("results[%d] = %+v, want ok", i, result)
		}
		if result.TempPassword == "" {
			t.Fatalf("results[%d] must carry a temp password", i)
		}
	}
	if results[0].TempPassword == results[1].TempPassword {
		t.Fatal("two resets must not share a temp password")
	}

	repo2 := &batchUserRepo{records: batchSeedRecords()}
	svc2 := newBatchService(t, repo2, &fakeAudit{}, &batchIssuer{})
	for _, action := range []string{BatchDisable, BatchEnable, BatchDelete, BatchRestore} {
		if action == BatchRestore {
			// restore 的目标先落软删行，否则该批全部判 404、断言落空。
			repo2 = &batchUserRepo{records: batchSeedDeletedRecords()}
			svc2 = newBatchService(t, repo2, &fakeAudit{}, &batchIssuer{})
		}
		results, err := svc2.Batch(context.Background(), corescope.Master(),
			action, batchUserIDs[:2], batchActor())
		if err != nil {
			t.Fatalf("%s batch: %v", action, err)
		}
		for i, result := range results {
			if result.OK() && result.TempPassword != "" {
				t.Fatalf("%s results[%d] carries a temp password %q", action, i, result.TempPassword)
			}
		}
	}
}

// 角色门槛：admin 与 user 一律 403，整批拒绝、零调用零审计。
func TestBatchRequiresMasterRole(t *testing.T) {
	repo := &batchUserRepo{}
	audits := &fakeAudit{}
	svc := newBatchService(t, repo, audits, &batchIssuer{})

	for name, scope := range map[string]corescope.Scope{
		"admin": corescope.Admin([]string{"00000000-0000-4000-8000-0000000000d1"}),
		"user":  corescope.User("00000000-0000-4000-8000-0000000000d2"),
	} {
		t.Run(name, func(t *testing.T) {
			actor := audit.Actor{UserID: batchMasterID, Role: coreauth.Role(name)}
			_, err := svc.Batch(context.Background(), scope,
				BatchDisable, []string{batchUserIDs[0]}, actor)
			if err == nil {
				t.Fatalf("%s must not run user batch actions", name)
			}
			if code := errs.From(err).Code; code != errs.CodeForbidden {
				t.Fatalf("code = %q, want forbidden", code)
			}
		})
	}
	if len(repo.setStatus)+len(repo.passwords)+len(repo.softDeleted)+len(repo.restores) != 0 {
		t.Fatal("a forbidden request must not touch any use case")
	}
	if len(audits.entries) != 0 {
		t.Fatal("a forbidden request must not write any audit row")
	}
}
