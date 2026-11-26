package schedule_test

// 人工覆盖的审计链（P2-3）。
//
// 判据：每次真正写 window_overrides 都在**同一事务末尾**追加恰好一条
// schedule.window_override；旧值取自事务内 PutOverride 之前的读取；
// 周末门关闭与非法 reason 零写入零审计；版本竞争重试只留一条。
// 回滚本身由 pg e2e 钉住（假仓储不模拟事务回滚）。

import (
	"context"
	"errors"
	"reflect"
	"strings"
	"testing"
	"time"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/schedule"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// fakeAuditRepo 记录追加的审计行。
// tx 指向本次用例的事务替身：Append 若发生在事务外会被计数，
// 因为 P2-3 要求审计是事务的最后一条语句。
type fakeAuditRepo struct {
	entries   []appaudit.Entry
	appendErr error
	tx        *fakeTx
	outsideTx int
}

func (f *fakeAuditRepo) Append(_ context.Context, in appaudit.Entry) (appaudit.Row, error) {
	if f.tx == nil || f.tx.depth == 0 {
		f.outsideTx++
	}
	f.entries = append(f.entries, in)
	if f.appendErr != nil {
		return appaudit.Row{}, f.appendErr
	}
	return appaudit.Row{}, nil
}

func (f *fakeAuditRepo) Verify(context.Context, int64, int64) (appaudit.Report, error) {
	return appaudit.Report{}, nil
}

// testActor 是入口会话主体的等价物：Label 必须已掩码。
func testActor(id string) coreaudit.Actor {
	return appaudit.NewActor(coreauth.User{
		ID: id, Email: id + "@example.com", Role: coreauth.RoleMaster,
	})
}

// newServiceWithAudit 装配可观察审计端的服务；事务替身同时交给审计端，
// 用于断言 Append 发生在事务内。
func newServiceWithAudit(t *testing.T, w *fakeWindowStore, p *fakePlanStore,
	a fakeAccounts, d *fakeDiscover) (*appschedule.Service, *fakeAuditRepo) {
	t.Helper()
	tx := &fakeTx{}
	audit := &fakeAuditRepo{tx: tx}
	svc, err := appschedule.NewService(appschedule.Deps{
		Windows: w, Plans: p, Accounts: a, Discover: d, Tx: tx, Audit: audit,
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc, audit
}

// 首次覆盖：旧值为 null，新值为请求窗口的 Unix 秒，主体与目标逐项对齐。
func TestOverrideAppendsAuditWithNilOldWindow(t *testing.T) {
	w := &fakeWindowStore{task: enabledTask()}
	p := &fakePlanStore{}
	d := &fakeDiscover{window: platformWindow(6, 30, 7, 50)}
	svc, audit := newServiceWithAudit(t, w, p, fakeAccounts{ids: []string{"a1"}}, d)
	start := time.Date(2026, 10, 29, 9, 0, 0, 0, shanghai)
	end := start.Add(30 * time.Minute)
	actor := testActor("user-1")

	if _, err := svc.Override(context.Background(), corescope.Master(), baseInput(),
		start, end, "平台窗口错误", actor); err != nil {
		t.Fatalf("Override: %v", err)
	}
	if len(audit.entries) != 1 {
		t.Fatalf("审计行数 = %d, want 1", len(audit.entries))
	}
	if audit.outsideTx != 0 {
		t.Fatalf("Append 发生在事务外 %d 次：审计必须是事务最后一条语句", audit.outsideTx)
	}
	got := audit.entries[0]
	if got.Action != coreaudit.ActionScheduleWindowOverride {
		t.Fatalf("action = %q, want %q", got.Action, coreaudit.ActionScheduleWindowOverride)
	}
	if got.SchoolID != "school-1" || got.TargetType != "school_task" || got.TargetID != "task-1" {
		t.Fatalf("目标 = school %q type %q id %q", got.SchoolID, got.TargetType, got.TargetID)
	}
	if got.ActorUserID != actor.UserID || got.ActorRole != actor.Role || got.ActorLabel != actor.Label {
		t.Fatalf("主体 = %+v, want %+v", got, actor)
	}
	if strings.Contains(got.ActorLabel, "@") && !strings.Contains(got.ActorLabel, "***") {
		t.Fatalf("actor_label %q 未掩码", got.ActorLabel)
	}
	want := map[string]any{
		"changes": map[string][2]any{
			"window.start": {nil, start.Unix()},
			"window.end":   {nil, end.Unix()},
		},
		"reason": "平台窗口错误",
	}
	if !reflect.DeepEqual(got.Detail, want) {
		t.Fatalf("detail = %#v, want %#v", got.Detail, want)
	}
	if got.TS.IsZero() {
		t.Fatal("审计时间戳不得为零值")
	}
}

// 二次覆盖：旧值来自事务内 PutOverride 之前的库内读取，不是请求里的新值。
func TestOverrideAuditOldValuesComeFromStoredOverride(t *testing.T) {
	oldStart := time.Date(2026, 10, 29, 8, 0, 0, 0, shanghai)
	oldEnd := oldStart.Add(20 * time.Minute)
	w := &fakeWindowStore{
		task: enabledTask(),
		override: coreschedule.WindowOverride{
			SchoolTaskID: "task-1", Day: "2026-10-29",
			Start: oldStart, End: oldEnd, Reason: "旧理由", CreatedBy: "other-user",
		},
		overrideOK: true,
	}
	svc, audit := newServiceWithAudit(t, w, &fakePlanStore{},
		fakeAccounts{ids: []string{"a1"}}, &fakeDiscover{window: platformWindow(6, 30, 7, 50)})
	newStart := time.Date(2026, 10, 29, 10, 0, 0, 0, shanghai)
	newEnd := newStart.Add(20 * time.Minute)

	if _, err := svc.Override(context.Background(), corescope.Master(), baseInput(),
		newStart, newEnd, "平台窗口再次订正", testActor("user-2")); err != nil {
		t.Fatalf("Override: %v", err)
	}
	if len(audit.entries) != 1 {
		t.Fatalf("审计行数 = %d, want 1", len(audit.entries))
	}
	want := map[string]any{
		"changes": map[string][2]any{
			"window.start": {oldStart.Unix(), newStart.Unix()},
			"window.end":   {oldEnd.Unix(), newEnd.Unix()},
		},
		"reason": "平台窗口再次订正",
	}
	if got := audit.entries[0].Detail; !reflect.DeepEqual(got, want) {
		t.Fatalf("detail = %#v, want %#v（旧值必须是库内旧覆盖）", got, want)
	}
	if !w.override.Start.Equal(newStart) || !w.override.End.Equal(newEnd) {
		t.Fatalf("落库覆盖 = %v~%v, want %v~%v", w.override.Start, w.override.End, newStart, newEnd)
	}
}

// 审计失败：Override 必须整体失败，不得把写入当成成功返回预览。
// 真正的回滚（覆盖/缓存/计划行随事务消失）由 pg e2e 钉住。
func TestOverrideFailsClosedWhenAuditAppendFails(t *testing.T) {
	w := &fakeWindowStore{task: enabledTask()}
	svc, audit := newServiceWithAudit(t, w, &fakePlanStore{},
		fakeAccounts{ids: []string{"a1"}}, &fakeDiscover{window: platformWindow(6, 30, 7, 50)})
	audit.appendErr = errors.New("audit chain unavailable")
	start := time.Date(2026, 10, 29, 9, 0, 0, 0, shanghai)
	end := start.Add(30 * time.Minute)

	got, err := svc.Override(context.Background(), corescope.Master(), baseInput(),
		start, end, "平台窗口错误", testActor("user-1"))
	if err == nil {
		t.Fatal("审计失败必须上抛，不得返回成功预览")
	}
	if code := coreerrs.From(err).Code; code != coreerrs.CodeInternal {
		t.Fatalf("错误码 = %q, want %q", code, coreerrs.CodeInternal)
	}
	if len(got.Rows) != 0 || got.Source != "" {
		t.Fatalf("失败仍返回预览 = %+v", got)
	}
	if len(audit.entries) != 1 {
		t.Fatalf("Append 尝试次数 = %d, want 1", len(audit.entries))
	}
}

// 周末门关闭：不写覆盖，也不写审计。
func TestOverrideWeekendClosedWritesNoAudit(t *testing.T) {
	w := &fakeWindowStore{task: enabledTask()}
	svc, audit := newServiceWithAudit(t, w, &fakePlanStore{},
		fakeAccounts{ids: []string{"a1"}}, &fakeDiscover{})
	in := baseInput()
	in.Day = "2026-10-31"
	in.WeekendClosed = true
	start := time.Date(2026, 10, 31, 9, 0, 0, 0, shanghai)
	end := start.Add(30 * time.Minute)

	p, err := svc.Override(context.Background(), corescope.Master(), in,
		start, end, "调整", testActor("user-1"))
	if err != nil {
		t.Fatalf("Override: %v", err)
	}
	if len(audit.entries) != 0 {
		t.Fatalf("门关写了 %d 条审计, want 0", len(audit.entries))
	}
	if w.overrideOK {
		t.Fatal("门关不得写覆盖")
	}
	if len(p.Rows) != 0 || p.Source != contract.WindowWeekendClosed || p.Note != appschedule.WeekendGateNote {
		t.Fatalf("门关回执 = %+v", p)
	}
}

// 版本竞争：第一次事务回滚，最终只留一条审计。
// 假仓储不模拟回滚，故这里只断言追加次数；旧值语义由 pg e2e 钉住。
func TestOverrideStaleRetryAppendsOneAudit(t *testing.T) {
	w := &fakeWindowStore{task: enabledTask(), staleOnce: true}
	svc, audit := newServiceWithAudit(t, w, &fakePlanStore{},
		fakeAccounts{ids: []string{"a1"}}, &fakeDiscover{window: platformWindow(6, 30, 7, 50)})
	start := time.Date(2026, 10, 29, 9, 0, 0, 0, shanghai)
	end := start.Add(30 * time.Minute)

	if _, err := svc.Override(context.Background(), corescope.Master(), baseInput(),
		start, end, "平台窗口错误", testActor("user-1")); err != nil {
		t.Fatalf("Override: %v", err)
	}
	if len(audit.entries) != 1 {
		t.Fatalf("重试后审计行数 = %d, want 1", len(audit.entries))
	}
	if w.saves != 1 {
		t.Fatalf("窗口写次数 = %d, want 1（重试只成功一次）", w.saves)
	}
}

// reason 含手机号/邮箱：审计链拒收，必须在校验阶段 400 且零写入零审计。
func TestOverrideRejectsReasonWithPersonalData(t *testing.T) {
	for _, reason := range []string{"联系 13800138000", "邮箱 boss@example.com"} {
		t.Run(reason, func(t *testing.T) {
			w := &fakeWindowStore{task: enabledTask()}
			p := &fakePlanStore{}
			svc, audit := newServiceWithAudit(t, w, p,
				fakeAccounts{ids: []string{"a1"}}, &fakeDiscover{window: platformWindow(6, 30, 7, 50)})
			start := time.Date(2026, 10, 29, 9, 0, 0, 0, shanghai)
			end := start.Add(30 * time.Minute)

			_, err := svc.Override(context.Background(), corescope.Master(), baseInput(),
				start, end, reason, testActor("user-1"))
			if err == nil {
				t.Fatal("含个人数据的理由应 400")
			}
			codeErr := coreerrs.From(err)
			if codeErr.Code != coreerrs.CodeValidation {
				t.Fatalf("错误码 = %q, want %q", codeErr.Code, coreerrs.CodeValidation)
			}
			if !strings.Contains(codeErr.Message, "reason must not contain phone numbers or email addresses") {
				t.Fatalf("消息 = %q", codeErr.Message)
			}
			if w.overrideOK || len(p.inserted) != 0 {
				t.Fatalf("校验失败仍写库: overrideOK=%v inserted=%d", w.overrideOK, len(p.inserted))
			}
			if len(audit.entries) != 0 {
				t.Fatalf("校验失败仍写审计: %d 条", len(audit.entries))
			}
		})
	}
}
