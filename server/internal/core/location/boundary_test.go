package location

import "testing"

// 半径恰好按在内：这是 Resolve 的文档口径。
// 抽出 distanceWithin 后，边界方向有了直接判据；
// 改 <= 为 < 时本测试必红（P5-C 审查 P2-3）。
func TestDistanceWithinBoundaryInclusive(t *testing.T) {
	if !distanceWithin(300, 300) {
		t.Fatal("恰好等于半径应判在内")
	}
	if distanceWithin(300, 300.000001) {
		t.Fatal("超出半径应判在外")
	}
	if distanceWithin(300, 299.999999) != true {
		t.Fatal("半径内应判在内")
	}
}
