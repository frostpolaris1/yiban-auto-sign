package pg_test

// 审计列表可见面 e2e（B8b）：admin 只见绑定院校、school_id IS NULL 行对 admin
// 不可见、admin 空绑定是空页而不是全量；master 看全部。计数与列表同口径，
// 仓储层对 user scope 直接 403（纵深防御）。

import (
	"context"
	"testing"
	"time"

	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// appendSchoolAuditAt 追加一行带院校归属的审计；schoolID 为空串落 NULL。
func appendSchoolAuditAt(t *testing.T, e *testEnv, ts time.Time, schoolID, label string) coreaudit.Row {
	t.Helper()
	var row coreaudit.Row
	err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		var err error
		row, err = e.audits.Append(ctx, coreaudit.Entry{
			TS:         ts,
			SchoolID:   schoolID,
			ActorLabel: label,
			Action:     coreaudit.ActionUserCreate,
			TargetType: "user",
			TargetID:   "target-1",
			Detail:     map[string]any{"field": "role"},
		})
		return err
	})
	if err != nil {
		t.Fatalf("append school audit: %v", err)
	}
	return row
}

func TestAuditLogListSchoolScopeE2E(t *testing.T) {
	e := newEnv(t)
	schoolA := e.createSchool("scope-a").ID
	schoolB := e.createSchool("scope-b").ID

	base := time.Date(2026, 11, 4, 9, 0, 0, 0, time.UTC)
	rowsA := []coreaudit.Row{
		appendSchoolAuditAt(t, e, base, schoolA, "a***@example.com"),
		appendSchoolAuditAt(t, e, base.Add(time.Minute), schoolA, "a***@example.com"),
		appendSchoolAuditAt(t, e, base.Add(2*time.Minute), schoolA, "a***@example.com"),
	}
	rowB := appendSchoolAuditAt(t, e, base.Add(3*time.Minute), schoolB, "b***@example.com")
	rowNull := appendSchoolAuditAt(t, e, base.Add(4*time.Minute), "", "n***@example.com")

	repo := e.audits
	master := corescope.Master()
	all, _, err := repo.ListLogs(e.ctx, coreaudit.LogQuery{Scope: master, Limit: 50})
	if err != nil {
		t.Fatalf("master ListLogs: %v", err)
	}
	if len(all) != 5 {
		t.Fatalf("master rows = %d, want 5", len(all))
	}
	if total, err := repo.CountLogs(e.ctx, coreaudit.LogQuery{Scope: master}); err != nil || total != 5 {
		t.Fatalf("master CountLogs = (%d, %v), want (5, nil)", total, err)
	}

	// admin 绑定 A：只读 A 的三行，B 与 NULL 不可见。
	adminA := corescope.Admin([]string{schoolA})
	bound, next, err := repo.ListLogs(e.ctx, coreaudit.LogQuery{Scope: adminA, Limit: 2})
	if err != nil {
		t.Fatalf("admin ListLogs: %v", err)
	}
	if len(bound) != 2 || next == "" {
		t.Fatalf("admin first page = %d rows next=%q, want 2 with cursor", len(bound), next)
	}
	if bound[0].ID != rowsA[2].ID || bound[1].ID != rowsA[1].ID {
		t.Fatalf("admin first page ids = %d,%d, want %d,%d", bound[0].ID, bound[1].ID, rowsA[2].ID, rowsA[1].ID)
	}
	rest, next2, err := repo.ListLogs(e.ctx, coreaudit.LogQuery{Scope: adminA, Limit: 2, Cursor: next})
	if err != nil {
		t.Fatalf("admin second page: %v", err)
	}
	if len(rest) != 1 || rest[0].ID != rowsA[0].ID || next2 != "" {
		t.Fatalf("admin second page = %+v next=%q, want row %d and no cursor", rest, next2, rowsA[0].ID)
	}
	seen := make([]coreaudit.LogRow, 0, len(bound)+len(rest))
	seen = append(seen, bound...)
	seen = append(seen, rest...)
	for _, row := range seen {
		if row.ID == rowB.ID || row.ID == rowNull.ID {
			t.Fatalf("admin saw out-of-scope row %d", row.ID)
		}
	}
	if total, err := repo.CountLogs(e.ctx, coreaudit.LogQuery{Scope: adminA}); err != nil || total != 3 {
		t.Fatalf("admin CountLogs = (%d, %v), want (3, nil)", total, err)
	}

	// admin 空绑定：空页、零计数，绝不是全量。
	emptyScope := corescope.Admin(nil)
	empty, emptyNext, err := repo.ListLogs(e.ctx, coreaudit.LogQuery{Scope: emptyScope, Limit: 50})
	if err != nil {
		t.Fatalf("admin empty ListLogs: %v", err)
	}
	if len(empty) != 0 || emptyNext != "" {
		t.Fatalf("admin empty binding = %d rows next=%q, want empty page", len(empty), emptyNext)
	}
	if total, err := repo.CountLogs(e.ctx, coreaudit.LogQuery{Scope: emptyScope}); err != nil || total != 0 {
		t.Fatalf("admin empty CountLogs = (%d, %v), want (0, nil)", total, err)
	}

	// 纵深防御：user scope 不得拼 owner_user_id，直接 403。
	userScope := corescope.User("11111111-1111-1111-1111-111111111111")
	if _, _, err := repo.ListLogs(e.ctx, coreaudit.LogQuery{Scope: userScope, Limit: 50}); codeOf(t, err) != errs.CodeForbidden {
		t.Fatalf("user ListLogs code = %v, want forbidden", codeOf(t, err))
	}
	if _, err := repo.CountLogs(e.ctx, coreaudit.LogQuery{Scope: userScope}); codeOf(t, err) != errs.CodeForbidden {
		t.Fatalf("user CountLogs code = %v, want forbidden", codeOf(t, err))
	}
	// 无效 scope 是 500（不是静默全量）。
	if _, _, err := repo.ListLogs(e.ctx, coreaudit.LogQuery{Limit: 50}); codeOf(t, err) != errs.CodeInternal {
		t.Fatalf("invalid scope ListLogs code = %v, want internal_error", codeOf(t, err))
	}
}

// appendScopedAudit 追加一行可指定动作的审计；schoolID 为空串落 NULL。
func appendScopedAudit(t *testing.T, e *testEnv, ts time.Time, schoolID, label string, action coreaudit.Action) coreaudit.Row {
	t.Helper()
	var row coreaudit.Row
	err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		var err error
		row, err = e.audits.Append(ctx, coreaudit.Entry{
			TS:         ts,
			SchoolID:   schoolID,
			ActorLabel: label,
			Action:     action,
			TargetType: "user",
			TargetID:   "target-1",
			Detail:     map[string]any{"field": "role"},
		})
		return err
	})
	if err != nil {
		t.Fatalf("append scoped audit: %v", err)
	}
	return row
}

func TestAuditLogListScopedFilterE2E(t *testing.T) {
	e := newEnv(t)
	schoolA := e.createSchool("filter-a").ID
	schoolB := e.createSchool("filter-b").ID

	base := time.Date(2026, 11, 5, 9, 0, 0, 0, time.UTC)
	a := "s***@example.com"
	b := "t***@example.com"
	rowA1 := appendScopedAudit(t, e, base, schoolA, a, coreaudit.ActionUserCreate)
	appendScopedAudit(t, e, base.Add(time.Minute), schoolA, a, coreaudit.ActionUserDisable)
	rowA2 := appendScopedAudit(t, e, base.Add(2*time.Minute), schoolA, a, coreaudit.ActionUserCreate)
	appendScopedAudit(t, e, base.Add(3*time.Minute), schoolA, b, coreaudit.ActionUserCreate)
	rowA3 := appendScopedAudit(t, e, base.Add(4*time.Minute), schoolA, a, coreaudit.ActionUserCreate)
	rowB := appendScopedAudit(t, e, base.Add(time.Minute), schoolB, a, coreaudit.ActionUserCreate)
	rowNull := appendScopedAudit(t, e, base.Add(2*time.Minute), "", a, coreaudit.ActionUserCreate)

	repo := e.audits
	adminA := corescope.Admin([]string{schoolA})
	from := base.Add(2 * time.Minute)
	filtered := coreaudit.LogQuery{
		Scope:  adminA,
		Action: string(coreaudit.ActionUserCreate),
		Actor:  a,
		From:   &from,
		Limit:  1,
	}
	first, cursor, err := repo.ListLogs(e.ctx, filtered)
	if err != nil {
		t.Fatalf("admin filtered first page: %v", err)
	}
	if len(first) != 1 || first[0].ID != rowA3.ID || cursor == "" {
		t.Fatalf("admin filtered first page = %+v cursor=%q, want row %d with cursor", first, cursor, rowA3.ID)
	}
	second, next, err := repo.ListLogs(e.ctx, coreaudit.LogQuery{
		Scope:  adminA,
		Action: string(coreaudit.ActionUserCreate),
		Actor:  a,
		From:   &from,
		Limit:  1,
		Cursor: cursor,
	})
	if err != nil {
		t.Fatalf("admin filtered second page: %v", err)
	}
	if len(second) != 1 || second[0].ID != rowA2.ID || next != "" {
		t.Fatalf("admin filtered second page = %+v next=%q, want row %d and no cursor", second, next, rowA2.ID)
	}
	for _, row := range append(first, second...) {
		if row.ID == rowA1.ID || row.ID == rowB.ID || row.ID == rowNull.ID {
			t.Fatalf("admin filtered saw out-of-scope row %d", row.ID)
		}
	}
	countQuery := filtered
	countQuery.Cursor = ""
	if total, err := repo.CountLogs(e.ctx, countQuery); err != nil || total != 2 {
		t.Fatalf("admin filtered CountLogs = (%d, %v), want (2, nil)", total, err)
	}
	// master 用同一组筛选能看到 NULL 行，但仍看不到动作/操作者/时间范围之外的行。
	master := filtered
	master.Scope = corescope.Master()
	master.Limit = 10
	all, _, err := repo.ListLogs(e.ctx, master)
	if err != nil {
		t.Fatalf("master filtered ListLogs: %v", err)
	}
	if len(all) != 3 {
		t.Fatalf("master filtered rows = %d, want 3", len(all))
	}
	if total, err := repo.CountLogs(e.ctx, master); err != nil || total != 3 {
		t.Fatalf("master filtered CountLogs = (%d, %v), want (3, nil)", total, err)
	}
}
