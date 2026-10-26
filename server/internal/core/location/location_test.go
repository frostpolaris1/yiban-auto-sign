package location_test

import (
	"math"
	"testing"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/location"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// 判据口径：点按 WGS84 经纬度解释。
// 多边形围栏按平面坐标做射线法，**不处理反子午线跨越**：
// 实测点位在中国境内（经度 73–135），跨线围栏不存在（见包注释）。
// 边界值语义：多边形顶点与边上的点按"在内"处理；
// 圆形围栏半径恰好等于 RadiusM 的点也按"在内"处理。
// 两处边界语义一致：围栏线不算越界。

// square 是 10×10 的经纬度方块：lng 0..10，lat 0..10。
var square = []contract.Point{
	{Lng: 0, Lat: 0}, {Lng: 10, Lat: 0}, {Lng: 10, Lat: 10}, {Lng: 0, Lat: 10},
}

func TestPointInPolygon(t *testing.T) {
	cases := []struct {
		name string
		lng  float64
		lat  float64
		poly []contract.Point
		want bool
	}{
		{"内部点", 5, 5, square, true},
		{"外部点", 15, 5, square, false},
		{"顶点按在内", 0, 0, square, true},
		{"边中点按在内", 0, 5, square, true},
		{"边外侧紧邻", -0.001, 5, square, false},
		{"退化多边形恒在外", 5, 5, square[:2], false},
		{"空多边形恒在外", 5, 5, nil, false},
		{"凹多边形内凹处在外", 0.5, 5, []contract.Point{
			{Lng: 0, Lat: 0}, {Lng: 10, Lat: 0}, {Lng: 10, Lat: 10},
			{Lng: 0, Lat: 10}, {Lng: 0, Lat: 6}, {Lng: 1, Lat: 5}, {Lng: 0, Lat: 4},
		}, false},
		{"凹多边形缺口外在内", 2, 5, []contract.Point{
			{Lng: 0, Lat: 0}, {Lng: 10, Lat: 0}, {Lng: 10, Lat: 10},
			{Lng: 0, Lat: 10}, {Lng: 0, Lat: 6}, {Lng: 1, Lat: 5}, {Lng: 0, Lat: 4},
		}, true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := location.PointInPolygon(c.lng, c.lat, c.poly); got != c.want {
				t.Fatalf("PointInPolygon(%v,%v) = %v, want %v", c.lng, c.lat, got, c.want)
			}
		})
	}
}

// DistanceMeters 用已知距离核对：纬度 1 度在大圆上约 111.195 km。
func TestDistanceMeters(t *testing.T) {
	cases := []struct {
		name string
		a    contract.Point
		b    contract.Point
		want float64 // 期望值；tol 为绝对容差
		tol  float64
	}{
		{"同点为零", contract.Point{Lng: 116.4, Lat: 39.9}, contract.Point{Lng: 116.4, Lat: 39.9}, 0, 1e-6},
		{"纬度一度", contract.Point{Lng: 116, Lat: 40}, contract.Point{Lng: 116, Lat: 41}, 111194.93, 1},
		{"赤道经度一度", contract.Point{Lng: 116, Lat: 0}, contract.Point{Lng: 117, Lat: 0}, 111194.93, 1},
		{"对称抵消", contract.Point{Lng: 116, Lat: 10}, contract.Point{Lng: 116, Lat: -10}, 2 * 111194.93 * 10 / 1, 10},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := location.DistanceMeters(c.a, c.b)
			if math.Abs(got-c.want) > c.tol {
				t.Fatalf("DistanceMeters = %.3f, want %.3f±%v", got, c.want, c.tol)
			}
			// 对称性：换序结果一致。
			back := location.DistanceMeters(c.b, c.a)
			if math.Abs(back-got) > 1e-6 {
				t.Fatalf("换序不对称: %.6f vs %.6f", back, got)
			}
		})
	}
}

// 跨经度：反子午线两侧相距 0.0002 度的点相距约 22 米，不是半地球。
// haversine 的 sin(Δλ/2) 天然处理回绕，不需要特判。
func TestDistanceMetersCrossesAntimeridian(t *testing.T) {
	got := location.DistanceMeters(
		contract.Point{Lng: 179.9999, Lat: 0},
		contract.Point{Lng: -179.9999, Lat: 0},
	)
	want := 111194.93 * 0.0002
	if math.Abs(got-want) > 0.1 {
		t.Fatalf("跨线距离 = %.3f, want ≈ %.3f", got, want)
	}
}

// fencedTarget 返回一个以 square 为围栏的目标。
func fencedTarget() contract.Target {
	return contract.Target{
		Polygon: square,
		Center:  contract.Point{Lng: 5, Lat: 5},
		RadiusM: 0,
	}
}

// 多边形优先：中心与半径只在缺多边形时生效。
func TestResolveUsesPolygonFirst(t *testing.T) {
	target := fencedTarget()
	fixed := contract.Location{
		Mode: contract.LocationFixed, Lng: 5, Lat: 5,
	}
	if !location.Resolve(target, fixed) {
		t.Fatal("多边形内的固定点应判在内")
	}
	outside := fixed
	outside.Lng, outside.Lat = 15, 5
	if location.Resolve(target, outside) {
		t.Fatal("多边形外的固定点应判在外")
	}
	// 多边形优先：中心与半径给出矛盾结论时仍以多边形为准。
	target.Center = contract.Point{Lng: 15, Lat: 5}
	target.RadiusM = 10_000_000
	if location.Resolve(target, outside) {
		t.Fatal("有多边形时不得退回中心半径")
	}
}

// 无多边形用中心半径；半径恰好按在内（与多边形边界同语义）。
func TestResolveFallsBackToRadius(t *testing.T) {
	target := contract.Target{
		Center:  contract.Point{Lng: 116.4, Lat: 39.9},
		RadiusM: 500,
	}
	// 纬度 0.0045 度 ≈ 500.4 m。4e-3 度 ≈ 444.8 m 在内。
	inside := contract.Location{Mode: contract.LocationFixed, Lng: 116.4, Lat: 39.9040}
	if !location.Resolve(target, inside) {
		t.Fatal("半径内固定点应判在内")
	}
	outside := contract.Location{Mode: contract.LocationFixed, Lng: 116.4, Lat: 39.9100}
	if location.Resolve(target, outside) {
		t.Fatal("半径外固定点应判在外")
	}
}

// 半径恰好：取恰好一个半径长度的纬度偏移，判定为在内。
func TestResolveExactlyAtRadius(t *testing.T) {
	const radius = 500.0
	target := contract.Target{
		Center:  contract.Point{Lng: 116.4, Lat: 39.9},
		RadiusM: int(radius),
	}
	deg := radius / 111194.93
	point := contract.Location{
		Mode: contract.LocationFixed, Lng: 116.4, Lat: 39.9 + deg,
	}
	if !location.Resolve(target, point) {
		t.Fatal("半径恰好的点应判在内")
	}
}

// auto 模式宿主没有点位，恒判在内：范围判定只约束固定点。
func TestResolveAutoAlwaysInside(t *testing.T) {
	target := fencedTarget()
	auto := contract.Location{Mode: contract.LocationAuto}
	if !location.Resolve(target, auto) {
		t.Fatal("auto 定位不得被判在外")
	}
}

// 非轴对齐多边形：顶点按在内，斜边内外各有判据。
// 删掉 onSegment 前置时本测试必红（P5-C 审查 P3-1）。
func TestPointInPolygonNonAxisAligned(t *testing.T) {
	poly := []contract.Point{
		{Lng: 0, Lat: 0}, {Lng: 4, Lat: 0}, {Lng: 2, Lat: 2},
	}
	cases := []struct {
		lng, lat float64
		want     bool
		note     string
	}{
		{2, 0.5, true, "三角形内"},
		{2, 2.5, false, "顶点上方"},
		{4, 0, true, "顶点按在内"},
		{5, 1, false, "右外侧"},
	}
	for _, c := range cases {
		if got := location.PointInPolygon(c.lng, c.lat, poly); got != c.want {
			t.Fatalf("(%v,%v)=%v, want %v（%s）", c.lng, c.lat, got, c.want, c.note)
		}
	}
}

// 不对称多边形：经纬度互换（射线换轴）必须改变判定结果，
// 否则换轴突变静默存活（P5-C 审查 P3-2）。
func TestPointInPolygonAsymmetric(t *testing.T) {
	poly := []contract.Point{
		{Lng: 100, Lat: 20}, {Lng: 110, Lat: 21}, {Lng: 109, Lat: 29}, {Lng: 101, Lat: 30},
	}
	if !location.PointInPolygon(105, 25, poly) {
		t.Fatal("(105,25) 应在多边形内")
	}
	if location.PointInPolygon(105, 35, poly) {
		t.Fatal("(105,35) 应在多边形外")
	}
	if location.PointInPolygon(90, 25, poly) {
		t.Fatal("(90,25) 应在多边形外")
	}
}
