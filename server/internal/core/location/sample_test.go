package location_test

// 围栏采样判据（工单 mks）：
// 采样点是 (多边形, 种子) 的纯函数；无有效多边形时拒绝采样，
// 由调用方回退上游中心点。

import (
	"math"
	"strconv"
	"testing"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/location"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// 13 点合成围栏：形状明显，非真实点位。
// 顺带覆盖"点多于三"与凹凸边，采样仍须保持包含判定。
var syntheticFence = []contract.Point{
	{Lng: 100.0, Lat: 20.0}, {Lng: 101.5, Lat: 20.2}, {Lng: 102.4, Lat: 20.9},
	{Lng: 102.1, Lat: 21.6}, {Lng: 101.2, Lat: 21.9}, {Lng: 100.6, Lat: 21.5},
	{Lng: 100.2, Lat: 22.0}, {Lng: 99.4, Lat: 21.7}, {Lng: 99.1, Lat: 21.0},
	{Lng: 99.6, Lat: 20.6}, {Lng: 99.8, Lat: 20.2}, {Lng: 100.3, Lat: 20.1},
	{Lng: 100.6, Lat: 20.4},
}

// 任何种子采出的点都必须落在多边形内。
func TestSamplePointStaysInsidePolygon(t *testing.T) {
	for _, poly := range [][]contract.Point{square, syntheticFence} {
		for i := 0; i < 200; i++ {
			point, ok := location.SamplePoint(poly, "attempt-"+strconv.Itoa(i))
			if !ok {
				t.Fatalf("SamplePoint(%d 点围栏, 种子 %d) 采样失败", len(poly), i)
			}
			if !location.PointInPolygon(point.Lng, point.Lat, poly) {
				t.Fatalf("采样点在围栏外: %+v（种子 %d）", point, i)
			}
		}
	}
}

// 同一 AttemptID 恒得同一点；不同尝试必须能取到不同点。
func TestSamplePointIsReproducible(t *testing.T) {
	first, ok := location.SamplePoint(syntheticFence, "task-1#0")
	if !ok {
		t.Fatal("采样失败")
	}
	again, ok := location.SamplePoint(syntheticFence, "task-1#0")
	if !ok || again != first {
		t.Fatalf("同一 AttemptID 得到不同点: %+v vs %+v", first, again)
	}
	second, ok := location.SamplePoint(syntheticFence, "task-1#1")
	if !ok {
		t.Fatal("第二次尝试采样失败")
	}
	if second == first {
		t.Fatalf("不同尝试得到同一点: %+v", first)
	}
}

// 无有效多边形一律拒绝采样，调用方据此回退上游中心点。
func TestSamplePointRejectsInvalidPolygon(t *testing.T) {
	cases := []struct {
		name string
		poly []contract.Point
	}{
		{"无多边形", nil},
		{"两点退化", []contract.Point{{Lng: 100, Lat: 20}, {Lng: 110, Lat: 30}}},
		{"零面积重合点", []contract.Point{{Lng: 100, Lat: 20}, {Lng: 100, Lat: 20}, {Lng: 100, Lat: 20}}},
		{"经度非有限", []contract.Point{{Lng: math.Inf(1), Lat: 20}, {Lng: 110, Lat: 20}, {Lng: 110, Lat: 30}}},
		{"纬度非有限", []contract.Point{{Lng: 100, Lat: math.NaN()}, {Lng: 110, Lat: 20}, {Lng: 110, Lat: 30}}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if point, ok := location.SamplePoint(c.poly, "task-1#0"); ok {
				t.Fatalf("非法多边形仍采样: %+v", point)
			}
		})
	}
}
