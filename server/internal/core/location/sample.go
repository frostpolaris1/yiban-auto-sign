package location

// 围栏内确定性采样（工单 mks）。
//
// 采样点是 (多边形, 种子) 的纯函数：同一 AttemptID 恒得同一点。
// 做法是包围盒内均匀投点，再做多边形包含判定（拒绝采样）。
// 采样不成立时返回 false，调用方回退上游中心点。

import (
	"hash/fnv"
	"math"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// maxSampleTries 是一次采样的最大投点数。
// 退化围栏（细长条）可能用尽次数，此时返回 false，由调用方回退。
const maxSampleTries = 256

// SamplePoint 在围栏内取一个确定点。
//
// 前置：poly 至少 3 点且坐标有限；不满足即返回 false。
// 返回 true 时点必然满足 PointInPolygon，边界语义与判定一致。
func SamplePoint(poly []contract.Point, seed string) (contract.Point, bool) {
	if len(poly) < 3 {
		return contract.Point{}, false
	}
	minLng, maxLng := poly[0].Lng, poly[0].Lng
	minLat, maxLat := poly[0].Lat, poly[0].Lat
	for _, p := range poly {
		if !finite(p.Lng) || !finite(p.Lat) {
			return contract.Point{}, false
		}
		minLng = math.Min(minLng, p.Lng)
		maxLng = math.Max(maxLng, p.Lng)
		minLat = math.Min(minLat, p.Lat)
		maxLat = math.Max(maxLat, p.Lat)
	}
	// 零面积包围盒采不到内部点：直接拒绝，不浪费投点。
	if maxLng <= minLng || maxLat <= minLat {
		return contract.Point{}, false
	}
	g := &splitmix64{state: seedHash(seed)}
	for i := 0; i < maxSampleTries; i++ {
		lng := minLng + (maxLng-minLng)*g.unit()
		lat := minLat + (maxLat-minLat)*g.unit()
		if PointInPolygon(lng, lat, poly) {
			return contract.Point{Lng: lng, Lat: lat}, true
		}
	}
	return contract.Point{}, false
}

// finite 报告 v 是有限数。
func finite(v float64) bool { return !math.IsNaN(v) && !math.IsInf(v, 0) }

// seedHash 把种子字符串散列成 64 位状态。
// FNV-1a 的实现固定，不随工具链版本漂移。
func seedHash(seed string) uint64 {
	h := fnv.New64a()
	_, _ = h.Write([]byte(seed))
	return h.Sum64()
}

// splitmix64 是确定性伪随机发生器：同一状态恒出同一序列。
// 自实现而不用 math/rand：输出不随 Go 版本变化，可复现性不依赖工具链。
type splitmix64 struct{ state uint64 }

func (g *splitmix64) next() uint64 {
	g.state += 0x9e3779b97f4a7c15
	z := g.state
	z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9
	z = (z ^ (z >> 27)) * 0x94d049bb133111eb
	return z ^ (z >> 31)
}

// unit 返回 [0,1) 的均匀值：取高 53 位，正好是 float64 的尾数宽度。
func (g *splitmix64) unit() float64 {
	return float64(g.next()>>11) / (1 << 53)
}
