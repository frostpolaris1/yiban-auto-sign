package events_test

// 假时钟、假定时器与它们的判据。
// 定时器替身按截止时刻到期，与真计时器同语义。
// 截止时刻 = 注册时的假时钟读数 + d。
// 未到截止时刻不投递。
// 逻辑时间已越过截止时刻时，注册即触发。
// 未取走的触发值不因重新注册丢失。

import (
	"sync"
	"testing"
	"time"
)

// fakeClock 是可手动推进的时钟。
type fakeClock struct {
	mu sync.Mutex
	t  time.Time
}

func newFakeClock(t time.Time) *fakeClock { return &fakeClock{t: t} }

func (c *fakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.t
}

func (c *fakeClock) Advance(d time.Duration) {
	c.mu.Lock()
	c.t = c.t.Add(d)
	c.mu.Unlock()
}

// fakeTimer 是按假时钟到期的定时器替身。
// 截止时刻在注册时算出：注册时的假时钟读数 + d。
// 每次注册投一个令牌：测试等令牌后再推进时钟。
// 触发时刻记成水位：fireAt 见过的最晚时刻。
// 处理器注册晚于触发时，按水位补触发。
type fakeTimer struct {
	mu        sync.Mutex
	clock     *fakeClock
	ch        chan time.Time
	due       time.Time
	watermark time.Time
	regs      chan struct{}
}

func newFakeTimer(c *fakeClock) *fakeTimer {
	return &fakeTimer{clock: c, regs: make(chan struct{}, 8)}
}

// After 注册定时器：截止时刻 = 当前假时钟读数 + d。
// 水位已到或越过截止时刻时，返回的通道立即带一个触发值。
// 通道只建一次并复用：替换通道会丢掉处理器未取走的触发值。
func (f *fakeTimer) After(d time.Duration) <-chan time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.ch == nil {
		f.ch = make(chan time.Time, 1)
	}
	f.due = f.clock.Now().Add(d)
	if len(f.ch) == 0 && !f.watermark.Before(f.due) {
		f.ch <- f.watermark
	}
	select {
	case f.regs <- struct{}{}:
	default: // 测试未取令牌时不得阻塞处理器
	}
	return f.ch
}

// fireAt 报告逻辑时间推进到 t。
// 已注册且 t 不早于截止时刻时投递，返回 true。
// 未到截止时刻时不投递，返回 false：注册未到期，不得提前触发。
// 无注册或无空位时只记水位，之后注册的定时器补触发。
func (f *fakeTimer) fireAt(t time.Time) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.watermark.Before(t) {
		f.watermark = t
	}
	if f.ch == nil || t.Before(f.due) {
		return false
	}
	select {
	case f.ch <- t:
		return true
	default:
		return false
	}
}

// waitArmed 等处理器注册首个定时器，即流循环已开始。
// 推进时钟必须在此之后：处理器按流开始时的假时钟算截止时刻。
func (f *fakeTimer) waitArmed(t *testing.T) {
	t.Helper()
	f.waitRegistered(t, "首个")
}

// waitRegistered 等处理器完成一次定时器注册。
// 推进时钟必须在注册之后：截止时刻由注册时的假时钟读数算出。
// 处理器重注册晚于时钟推进时，截止时刻会被算到推进后的时间。
func (f *fakeTimer) waitRegistered(t *testing.T, what string) {
	t.Helper()
	select {
	case <-f.regs:
	case <-time.After(5 * time.Second):
		t.Fatalf("等定时器注册超时：%s", what)
	}
}

// 水位补触发：先推进逻辑时间，再注册已过截止时刻的定时器，必须立即触发。
func TestFakeTimerFiresRegistrationBehindWatermark(t *testing.T) {
	clk := newFakeClock(diagNowTime)
	timer := newFakeTimer(clk)
	_ = timer.fireAt(clk.Now().Add(15 * time.Second))
	ch := timer.After(15 * time.Second)
	select {
	case <-ch:
	case <-time.After(5 * time.Second):
		t.Fatal("截止时刻已过的定时器未立即触发")
	}
}

// 重新注册不丢触发值：处理器未取走就换定时器，值仍要送达。
func TestFakeTimerKeepsUnreadFireAcrossRegistration(t *testing.T) {
	clk := newFakeClock(diagNowTime)
	timer := newFakeTimer(clk)
	timer.After(15 * time.Second)
	_ = timer.fireAt(clk.Now().Add(15 * time.Second))
	ch := timer.After(30 * time.Second)
	select {
	case <-ch:
	case <-time.After(5 * time.Second):
		t.Fatal("重新注册后触发值丢失")
	}
}
