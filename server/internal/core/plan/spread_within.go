package plan

import "time"

// SpreadWithin 把 n 行铺进至多 nSlices 片、每片 slots 个槽的片阵，
// 返回各行相对窗口起点的偏移（按行序）。两种分布：
//   - front=true：顺序装满前段（行 i 落片 i/slots），最早完成；
//   - front=false：全窗分层（行 i 落片 i%nSlices），摊薄峰值。
//
// 片号恒在 [0, nSlices-1]：调用方按窗口跨度给 nSlices，
// 由此保证任何行都不越窗。确定性可重放。
func SpreadWithin(n, slots, nSlices int, front bool) []time.Duration {
	if n < 0 {
		n = 0
	}
	if slots < 1 {
		slots = 1
	}
	if nSlices < 1 {
		nSlices = 1
	}
	offs := make([]time.Duration, n)
	for i := 0; i < n; i++ {
		var k, slot int
		if front {
			k = i / slots
			if k >= nSlices {
				k = nSlices - 1
			}
			slot = i % slots
		} else {
			k = i % nSlices
			slot = (i / nSlices) % slots
		}
		offs[i] = span(k) + time.Duration(slot)*SlotSpan/time.Duration(slots)
	}
	return offs
}
