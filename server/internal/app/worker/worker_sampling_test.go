package worker_test

// 围栏采样注入判据（工单 mks）：
// auto 定位在多边形围栏内取确定点；采样不成立时保留零坐标，
// 由提供方回退上游中心点。

import (
	"context"
	"testing"

	corelocation "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/location"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// 用户动作：auto 定位账号在带多边形围栏的目标上执行一次签到。
// 用户可见结果：提交点落在围栏内，且等于按 AttemptID 复算的采样点。
func TestAutoLocationSamplesInsideFence(t *testing.T) {
	st := newStore(taskWithOwner())
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := defaultSigner()
	target := fencedTarget()
	signer.targets = []contract.Target{target}
	svc := injectService(t, st, signer, &fakeLocations{}, &fakePhotos{})

	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	if signer.signs != 1 {
		t.Fatalf("签到次数 = %d, want 1", signer.signs)
	}
	loc := signer.requests[0].Location
	if loc.Mode != contract.LocationAuto {
		t.Fatalf("Mode = %q, want auto", loc.Mode)
	}
	if !corelocation.PointInPolygon(loc.Lng, loc.Lat, target.Polygon) {
		t.Fatalf("提交点在围栏外: %+v", loc)
	}
	want, ok := corelocation.SamplePoint(target.Polygon, signer.requests[0].AttemptID)
	if !ok || loc.Lng != want.Lng || loc.Lat != want.Lat {
		t.Fatalf("提交点 = (%v,%v)，按 AttemptID 复算 = (%v,%v,%v)",
			loc.Lng, loc.Lat, want.Lng, want.Lat, ok)
	}
}

// 无有效多边形：不采样，Location 留零坐标，提供方回退上游中心点。
func TestAutoLocationFallsBackWithoutPolygon(t *testing.T) {
	cases := []struct {
		name    string
		polygon []contract.Point
	}{
		{"无多边形", nil},
		{"两点退化", []contract.Point{{Lng: 100, Lat: 20}, {Lng: 110, Lat: 30}}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			st := newStore(taskWithOwner())
			st.windows["st-1|2026-10-29"] = openWindow()
			signer := defaultSigner()
			signer.targets = []contract.Target{{
				ID:      "pos-1",
				Polygon: c.polygon,
				Center:  contract.Point{Lng: 105, Lat: 25},
			}}
			svc := injectService(t, st, signer, &fakeLocations{}, &fakePhotos{})

			if _, err := svc.Step(context.Background(), 10); err != nil {
				t.Fatalf("Step: %v", err)
			}
			loc := signer.requests[0].Location
			if loc.Lng != 0 || loc.Lat != 0 {
				t.Fatalf("采样应回退，实际带坐标: %+v", loc)
			}
		})
	}
}
