package location

// 围栏判定与采样使用的自有类型。
//
// 这些类型只承载几何判定实际消费的字段，不是平台协议形状；
// signing/contract 的同名类型在调用侧转换（app/worker），
// core 不依赖 signing 包（canonical 的 boundary.core.contract）。

// Point 是 WGS84 坐标。
type Point struct {
	Lng float64
	Lat float64
}

// Target 是围栏判定的目标子集：多边形、中心与半径。
// 标题、地址、图片等非几何字段不进入 core。
type Target struct {
	Polygon []Point
	Center  Point
	RadiusM int
}

// LocationMode 选择自动采样还是固定点。
type LocationMode string

const (
	LocationAuto  LocationMode = "auto"
	LocationFixed LocationMode = "fixed"
)

// Location 是一次尝试的定位策略。
// Reason 不在此：它是 app 层"跳过围栏过滤"的用例判据，不是几何输入。
type Location struct {
	Mode LocationMode
	Lng  float64
	Lat  float64
}
