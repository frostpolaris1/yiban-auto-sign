package account

// 平台姓名揭示的审计用例单测。
// 判据：揭示零业务写库，审计行是唯一落盘事实；
// detail 只带 field 词（EncodeDetail 白名单键），绝无姓名本体。

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// capturingAuditRepo 捕获追加的审计行，供揭示审计断言。
type capturingAuditRepo struct {
	appaudit.Repo
	entries []coreaudit.Entry
	err     error
}

func (f *capturingAuditRepo) Append(_ context.Context, in coreaudit.Entry) (coreaudit.Row, error) {
	if f.err != nil {
		return coreaudit.Row{}, f.err
	}
	f.entries = append(f.entries, in)
	return coreaudit.Row{}, nil
}

// revealTxManager 直接执行事务体：单语句追加不需要真事务语义。
type revealTxManager struct{ app.TxManager }

func (revealTxManager) InTx(ctx context.Context, fn func(context.Context) error) error {
	return fn(ctx)
}

func newRevealService(t *testing.T, audit appaudit.Repo) *Service {
	t.Helper()
	svc, err := NewService(Deps{
		Accounts:   &fakeListRepo{},
		Schools:    fakeSchoolReader{},
		Audit:      audit,
		Tx:         revealTxManager{},
		Clock:      clock.Fixed{T: time.Date(2026, 11, 8, 7, 0, 0, 0, time.UTC)},
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

// 揭示审计：动作、目标与 detail 形状都钉死；detail 不含姓名本体。
func TestRecordPersonNameRevealAuditsWithoutName(t *testing.T) {
	audit := &capturingAuditRepo{}
	svc := newRevealService(t, audit)
	actor := appaudit.Actor{UserID: "u1", Role: "user", Label: "u***@example.com"}

	err := svc.RecordPersonNameReveal(context.Background(), corescope.User("u1"), "acc-1", "school-1", actor)
	if err != nil {
		t.Fatalf("RecordPersonNameReveal: %v", err)
	}
	if len(audit.entries) != 1 {
		t.Fatalf("audit rows = %d, want 1", len(audit.entries))
	}
	entry := audit.entries[0]
	if entry.Action != coreaudit.ActionAccountPersonNameReveal {
		t.Fatalf("action = %q", entry.Action)
	}
	if entry.TargetType != "account" || entry.TargetID != "acc-1" || entry.SchoolID != "school-1" {
		t.Fatalf("entry = %+v", entry)
	}
	if entry.ActorUserID != "u1" || entry.ActorLabel != "u***@example.com" {
		t.Fatalf("actor = %+v", entry)
	}
	raw, err := json.Marshal(entry.Detail)
	if err != nil {
		t.Fatalf("marshal detail: %v", err)
	}
	// detail 是 {"field":"person_name"}：只有字段名，没有姓名值。
	if string(raw) != `{"field":"person_name"}` {
		t.Fatalf("detail = %s", raw)
	}
}

// 零值作用域拒绝：与同包其余用例同门。
func TestRecordPersonNameRevealRejectsZeroScope(t *testing.T) {
	audit := &capturingAuditRepo{}
	svc := newRevealService(t, audit)
	err := svc.RecordPersonNameReveal(context.Background(), corescope.Scope{}, "acc-1", "school-1",
		appaudit.Actor{UserID: "u1"})
	if err == nil {
		t.Fatal("zero-value scope passed the reveal audit")
	}
	if len(audit.entries) != 0 {
		t.Fatalf("audit rows = %d, want 0", len(audit.entries))
	}
}

// 审计仓储失败原样上抛：入口层据此不给姓名出响应。
func TestRecordPersonNameRevealPassesStoreFailure(t *testing.T) {
	audit := &capturingAuditRepo{err: errors.New("audit insert failed")}
	svc := newRevealService(t, audit)
	err := svc.RecordPersonNameReveal(context.Background(), corescope.Master(), "acc-1", "school-1",
		appaudit.Actor{UserID: "u1"})
	if err == nil || !strings.Contains(err.Error(), "audit insert failed") {
		t.Fatalf("err = %v, want the store failure", err)
	}
}
