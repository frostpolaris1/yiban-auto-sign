package image

// 解码闸：限制同时解码的图片张数。
//
// 单张上限 40 MP 的 RGBA 约 160 MB；十张并发就是 1.6 GB。
// 闸门按**整个进程**计：装配层构造一个实例，传给唯一的图片入口。

import "context"

// DecodeConcurrency 是同时解码的上限。图片解码是 CPU 与内存双密集，
// 两个并发足够喂满一张照片的预处理链，又把峰值内存压在 320 MB 以内。
const DecodeConcurrency = 2

// Gate 是计数信号量。零值不可用，用 NewGate 构造；nil 表示不限制。
type Gate struct{ slots chan struct{} }

// NewGate 构造容量为 n 的闸门。n < 1 夹到 1。
func NewGate(n int) *Gate {
	if n < 1 {
		n = 1
	}
	return &Gate{slots: make(chan struct{}, n)}
}

// acquire 取一个槽位。ctx 取消时放弃，返回 ctx 的错误。
func (g *Gate) acquire(ctx context.Context) error {
	if g == nil {
		return nil
	}
	select {
	case g.slots <- struct{}{}:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// release 归还槽位。
func (g *Gate) release() {
	if g != nil {
		<-g.slots
	}
}
