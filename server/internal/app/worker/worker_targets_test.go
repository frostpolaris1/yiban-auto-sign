package worker_test

// 换点策略判据（工单 mks）：
// 候选点按尝试序号轮转；轮转不改变总尝试次数。

import (
	"context"
	"testing"
	"time"

	appworker "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/worker"
	corelocation "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/location"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// squareAround 返回以 (lng,lat) 为左下角的 1×1 度方块围栏。
func squareAround(lng, lat float64) []contract.Point {
	return []contract.Point{
		{Lng: lng, Lat: lat}, {Lng: lng + 1, Lat: lat},
		{Lng: lng + 1, Lat: lat + 1}, {Lng: lng, Lat: lat + 1},
	}
}

// 用户动作：账号有 2 个候选点，前两次尝试返回瞬时失败并退避重排。
// 用户可见结果：三次提交依次用第 1、2、1 个候选点；仍按预算落 failed。
func TestRetryRotatesTargets(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := defaultSigner()
	signer.targets = []contract.Target{{ID: "pos-1"}, {ID: "pos-2"}}
	signer.outcome = contract.OutcomeTransient
	clk := &stepClock{t: testNow()}
	svc, err := appworker.NewService(appworker.Deps{
		Store: st, Creds: fakeCreds{}, Signer: signer, Egress: fakeEgress{},
		Limiter: fakeLimiter{allow: true}, Clock: clk, Location: loc,
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}

	for i := 0; i < 3; i++ {
		if _, err := svc.Step(context.Background(), 10); err != nil {
			t.Fatalf("Step %d: %v", i, err)
		}
		clk.t = clk.t.Add(10 * time.Minute)
	}

	want := []string{"pos-1", "pos-2", "pos-1"}
	if len(signer.requests) != len(want) {
		t.Fatalf("尝试次数 = %d, want %d（轮转不改变总尝试数）", len(signer.requests), len(want))
	}
	for i, id := range want {
		if got := signer.requests[i].Target.ID; got != id {
			t.Fatalf("第 %d 次尝试用 %q, want %q", i+1, got, id)
		}
	}
	if st.finished["t1"] != corestatus.TaskFailed {
		t.Fatalf("预算耗尽应 failed, got %v", st.finished["t1"])
	}
}

// 边界：固定点只落在第 2 个候选围栏内时，选点跳过第 1 个。
// 轮转不得因为选到落不到的围栏就截断当日（那会丢掉本来能签的候选）。
func TestFixedLocationSkipsTargetsThatExcludeIt(t *testing.T) {
	st := newStore(taskWithOwner())
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := defaultSigner()
	signer.targets = []contract.Target{
		{ID: "pos-1", Polygon: squareAround(100, 20)},
		{ID: "pos-2", Polygon: squareAround(105, 25)},
	}
	locs := &fakeLocations{byAccount: map[string]contract.Location{
		"acct-1": fixedLoc(105.5, 25.5, ""),
	}}
	svc := injectService(t, st, signer, locs, &fakePhotos{})

	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	if signer.signs != 1 {
		t.Fatalf("签到次数 = %d, want 1", signer.signs)
	}
	req := signer.requests[0]
	if req.Target.ID != "pos-2" {
		t.Fatalf("选中 %q, want pos-2", req.Target.ID)
	}
	if req.Location != fixedLoc(105.5, 25.5, "") {
		t.Fatalf("Location = %+v, want 固定点原值", req.Location)
	}
	if st.finished["t1"] != corestatus.TaskSuccess {
		t.Fatalf("终态 = %v, want success", st.finished["t1"])
	}
}

// 多候选 ＋ auto 采样：第 2 次尝试采到的点必须落在轮转后的候选围栏内。
// 突变守卫：把采样输入换成 targets[0].Polygon，本测试必须红。
func TestSamplingFollowsRotatedTarget(t *testing.T) {
	st := newStore(taskWithOwner())
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := defaultSigner()
	first := contract.Target{ID: "pos-1", Polygon: squareAround(100, 20)}
	second := contract.Target{ID: "pos-2", Polygon: squareAround(110, 30)}
	signer.targets = []contract.Target{first, second}
	signer.outcome = contract.OutcomeTransient // 第 1 次失败，触发第 2 次尝试
	clk := &stepClock{t: testNow()}
	svc, err := appworker.NewService(appworker.Deps{
		Store: st, Creds: fakeCreds{}, Signer: signer, Egress: fakeEgress{},
		Limiter: fakeLimiter{allow: true}, Clock: clk, Location: loc,
		Locations: &fakeLocations{}, Photos: &fakePhotos{},
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}

	for i := 0; i < 2; i++ {
		if _, err := svc.Step(context.Background(), 10); err != nil {
			t.Fatalf("Step %d: %v", i, err)
		}
		clk.t = clk.t.Add(10 * time.Minute)
	}
	if len(signer.requests) != 2 {
		t.Fatalf("尝试次数 = %d, want 2", len(signer.requests))
	}
	firstReq, secondReq := signer.requests[0], signer.requests[1]
	if secondReq.Target.ID != "pos-2" {
		t.Fatalf("第 2 次尝试用 %q, want pos-2", secondReq.Target.ID)
	}
	if !corelocation.PointInPolygon(firstReq.Location.Lng, firstReq.Location.Lat, first.Polygon) {
		t.Fatalf("第 1 次提交点不在第 1 个候选围栏内: %+v", firstReq.Location)
	}
	if !corelocation.PointInPolygon(secondReq.Location.Lng, secondReq.Location.Lat, second.Polygon) {
		t.Fatalf("第 2 次提交点不在第 2 个候选围栏内: %+v", secondReq.Location)
	}
	if corelocation.PointInPolygon(secondReq.Location.Lng, secondReq.Location.Lat, first.Polygon) {
		t.Fatalf("第 2 次提交点落在第 1 个候选围栏内：采样用了错的围栏: %+v", secondReq.Location)
	}
}

// 边界：单候选点时轮转恒取第一个（不与既有行为分叉）。
func TestSingleTargetStaysFirst(t *testing.T) {
	st := newStore(baseTask())
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := defaultSigner()
	signer.targets = []contract.Target{{ID: "pos-only"}}
	clk := &stepClock{t: testNow()}
	svc, err := appworker.NewService(appworker.Deps{
		Store: st, Creds: fakeCreds{}, Signer: signer, Egress: fakeEgress{},
		Limiter: fakeLimiter{allow: true}, Clock: clk, Location: loc,
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	for i := 0; i < 2; i++ {
		if _, err := svc.Step(context.Background(), 10); err != nil {
			t.Fatalf("Step %d: %v", i, err)
		}
		clk.t = clk.t.Add(10 * time.Minute)
	}
	for i, req := range signer.requests {
		if req.Target.ID != "pos-only" {
			t.Fatalf("第 %d 次尝试用 %q, want pos-only", i+1, req.Target.ID)
		}
	}
}
