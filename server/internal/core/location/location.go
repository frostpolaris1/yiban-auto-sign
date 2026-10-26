// Package location 提供签到围栏的纯几何判定。
//
// # 功能
// 点与多边形的包含判定（射线法）、两点球面距离（haversine）、
// 目标围栏的统一判定入口。
// 本包**不**读库、**不**发请求、**不**决定结论词。
//
// # 归属
// core 层纯函数包。app/worker 在提交签到前调用 Resolve。
//
// # 复用
// PointInPolygon、DistanceMeters、Resolve。
//
// # 口径
// 坐标是 WGS84 经纬度（度）。多边形按平面坐标解释，
// **不处理反子午线跨越**：实测点位在中国境内（经度 73–135），
// 跨线围栏不存在；距离计算的回绕由 haversine 的半角正弦天然处理。
// 边界语义全包一致：多边形顶点与边上的点判"在内"，
// 距圆心恰为 RadiusM 的点判"在内"——围栏线不算越界。
// auto 定位宿主没有点位，Resolve 恒判在内：范围判定只约束固定点。
package location

import (
	"math"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// epsDeg 是边界判定的距离容差（度）。
// 1e-9 度约 0.1 毫米，远小于任何真实定位误差；
// 只用于把"恰好落在围栏线上"的浮点噪声归到一侧。
const epsDeg = 1e-9

// earthRadiusM 是 haversine 用的地球平均半径（米）。
const earthRadiusM = 6371000.0

// PointInPolygon 报告点是否落在多边形内。
//
// 判据分两步：先按"点在任一边上"判内（顶点与边都算），
// 再按水平射线法数交点。少于三个顶点的输入没有围栏，恒判在外。
func PointInPolygon(lng, lat float64, poly []contract.Point) bool {
	if len(poly) < 3 {
		return false
	}
	for i := 0; i < len(poly); i++ {
		a := poly[i]
		b := poly[(i+1)%len(poly)]
		if onSegment(lng, lat, a, b) {
			return true
		}
	}
	inside := false
	j := len(poly) - 1
	for i := 0; i < len(poly); i++ {
		pi, pj := poly[i], poly[j]
		// 只统计两端跨过纬线的边；交点经度按线性插值求出，
		// 射线向经度增大方向发出，交点在点右侧（lng < x）才算穿越。
		if (pi.Lat > lat) != (pj.Lat > lat) {
			x := pj.Lng + (lat-pj.Lat)/(pi.Lat-pj.Lat)*(pi.Lng-pj.Lng)
			if lng < x {
				inside = !inside
			}
		}
		j = i
	}
	return inside
}

// onSegment 报告点是否落在段上（含端点）。
// 判据：到段的垂距不超过 epsDeg，且落在两端点的包围盒内。
func onSegment(lng, lat float64, a, b contract.Point) bool {
	dx, dy := b.Lng-a.Lng, b.Lat-a.Lat
	length := math.Hypot(dx, dy)
	if length <= epsDeg {
		// 退化段：两端重合，等价于点距判定。
		return math.Hypot(lng-a.Lng, lat-a.Lat) <= epsDeg
	}
	cross := dx*(lat-a.Lat) - dy*(lng-a.Lng)
	if math.Abs(cross)/length > epsDeg {
		return false
	}
	return math.Min(a.Lng, b.Lng) <= lng && lng <= math.Max(a.Lng, b.Lng) &&
		math.Min(a.Lat, b.Lat) <= lat && lat <= math.Max(a.Lat, b.Lat)
}

// DistanceMeters 返回两点的大圆距离（米）。
// haversine 公式对经度回绕天然成立：跨反子午线的两点得到短弧距离。
func DistanceMeters(a, b contract.Point) float64 {
	phi1, phi2 := a.Lat*math.Pi/180, b.Lat*math.Pi/180
	dphi := phi2 - phi1
	dlambda := (b.Lng - a.Lng) * math.Pi / 180
	h := math.Sin(dphi/2)*math.Sin(dphi/2) +
		math.Cos(phi1)*math.Cos(phi2)*math.Sin(dlambda/2)*math.Sin(dlambda/2)
	return 2 * earthRadiusM * math.Asin(math.Min(1, math.Sqrt(h)))
}

// Resolve 报告一次尝试的定位是否落在目标围栏内。
//
// 判据顺序：多边形优先；无多边形才用中心与半径。
// auto 模式没有宿主侧点位，恒判在内（见包注释口径）；
// fixed 模式取 Location 的经纬度参与判定。
func Resolve(target contract.Target, loc contract.Location) bool {
	if loc.Mode != contract.LocationFixed {
		return true
	}
	if len(target.Polygon) >= 3 {
		return PointInPolygon(loc.Lng, loc.Lat, target.Polygon)
	}
	return distanceWithin(float64(target.RadiusM),
		DistanceMeters(contract.Point{Lng: loc.Lng, Lat: loc.Lat}, target.Center))
}

// distanceWithin 报告距离是否仍在半径内：恰好等于半径按在内。
// 抽成小函数是为了让边界方向有直接的单元判据（P5-C 审查 P2-3）。
func distanceWithin(radius, d float64) bool { return d <= radius }
