package schedule

// 执行监控快照用例的单测：替身端口，不打真库。
//
// # 口径
// 只测用例自身的判据：作用域拒绝、业务日传递、出口掩码。
// SQL 行为（四段查询、作用域谓词、排序）由 cmd/server 的 UX-5 e2e
// 对真库覆盖，这里不重复。

import (
	"context"
	"testing"
	"time"

	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// fakeEngineSource 记录入参并回放固定数据。
type fakeEngineSource struct {
	gotScope corescope.Scope
	gotDay   string
	gotNow   time.Time
	calls    int
	data     coreschedule.EngineData
}

func (f *fakeEngineSource) EngineSnapshot(ctx context.Context, scope corescope.Scope,
	day string, now time.Time) (coreschedule.EngineData, error) {
	f.calls++
	f.gotScope = scope
	f.gotDay = day
	f.gotNow = now
	return f.data, nil
}

// user 范围没有执行可见面：拒绝，不把空集合当「自己的执行面」。
func TestEngineQueryRejectsUserScope(t *testing.T) {
	src := &fakeEngineSource{}
	q := NewEngineQuery(src, fakeClockAt("2026-10-31T10:00:00+08:00"), mustCST())
	if _, err := q.Snapshot(context.Background(), corescope.User("u1")); err == nil {
		t.Fatal("user scope must be rejected, not silently narrowed")
	}
	if src.calls != 0 {
		t.Fatalf("source called %d times, want 0", src.calls)
	}
}

// 业务日与时钟都取注入值，原样传给端口。
func TestEngineQueryPassesDayAndNow(t *testing.T) {
	src := &fakeEngineSource{}
	q := NewEngineQuery(src, fakeClockAt("2026-10-31T10:00:00+08:00"), mustCST())
	if _, err := q.Snapshot(context.Background(), corescope.Master()); err != nil {
		t.Fatalf("snapshot: %v", err)
	}
	if src.gotDay != "2026-10-31" {
		t.Fatalf("day = %q, want the clock's business day", src.gotDay)
	}
	if src.gotNow.IsZero() {
		t.Fatal("now must come from the injected clock")
	}
}

// 出口掩码：明文手机号不进视图；result 过自由文本掩码。
func TestEngineQueryMasksAtTheBoundary(t *testing.T) {
	run := time.Date(2026, 10, 31, 8, 0, 0, 0, time.UTC)
	src := &fakeEngineSource{data: coreschedule.EngineData{
		Inflight: []coreschedule.EngineInflightRow{{
			AccountID: "a1", Phone: "13800138000", SchoolID: "s1", SchoolName: "学校甲",
			ClaimedAt: run, LeaseExpiresAt: run.Add(2 * time.Minute),
		}},
		DueNow:       3,
		TodaySuccess: 5,
		TodayFailed:  2,
		Retrying: []coreschedule.EngineRetryRow{{
			AccountID: "a2", Phone: "13900139000", SchoolID: "s1", SchoolName: "学校甲",
			Attempts: 2, RunAt: run, Result: "failed: 13800138000 in text",
		}},
	}}
	q := NewEngineQuery(src, fakeClockAt("2026-10-31T10:00:00+08:00"), mustCST())

	snap, err := q.Snapshot(context.Background(), corescope.Master())
	if err != nil {
		t.Fatalf("snapshot: %v", err)
	}
	if snap.Inflight[0].PhoneMasked != "138****8000" {
		t.Fatalf("inflight phone = %q, want masked", snap.Inflight[0].PhoneMasked)
	}
	if snap.Retrying[0].PhoneMasked != "139****9000" {
		t.Fatalf("retrying phone = %q, want masked", snap.Retrying[0].PhoneMasked)
	}
	if snap.Retrying[0].Result == "" || snap.Retrying[0].Result == "failed: 13800138000 in text" {
		t.Fatalf("retrying result = %q, want the masked text", snap.Retrying[0].Result)
	}
	if snap.DueNow != 3 || snap.TodaySuccess != 5 || snap.TodayFailed != 2 {
		t.Fatalf("counts = %d/%d/%d, want 3/5/2", snap.DueNow, snap.TodaySuccess, snap.TodayFailed)
	}
}
