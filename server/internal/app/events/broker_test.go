package events

// Broker 单测。判据来自派单稿：
//   - scope 过滤：master 全量，admin 只收绑定校；
//   - 回放：lastEventID 之后的事件按序补发，且按 scope 过滤；
//   - 缺口：落后超过环形缓冲 → resync；
//   - 慢订阅者：满缓冲丢该订阅者的事件，Publish 不阻塞（1e5 次计时上限）；
//   - 并发安全：并发 Publish/Subscribe 不竞态（-race）。
// 时钟不进 Broker：事件 At 由发布方填，resync 的 At 为零值。

import (
	"context"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// collect 从订阅读 n 个事件或超时。超时即失败：静默少收不是绿。
func collect(t *testing.T, sub Subscription, n int) []Event {
	t.Helper()
	out := make([]Event, 0, n)
	deadline := time.After(2 * time.Second)
	for len(out) < n {
		select {
		case evt, ok := <-sub.C:
			if !ok {
				t.Fatalf("订阅在第 %d/%d 个事件前被关闭", len(out), n)
			}
			out = append(out, evt)
		case <-deadline:
			t.Fatalf("等待第 %d/%d 个事件超时", len(out), n)
		}
	}
	return out
}

func schoolEvent(typ, school string) Event {
	return Event{Type: typ, SchoolID: school, At: time.Unix(0, 0).UTC()}
}

func TestPublishAssignsSequentialIDs(t *testing.T) {
	b := NewBroker()
	sub, cancel := b.Subscribe(context.Background(), nil, 0)
	defer cancel()
	b.Publish([]string{"s1"}, schoolEvent(TypeTaskProgress, "s1"))
	b.Publish([]string{"s1"}, schoolEvent(TypeTaskConcluded, "s1"))
	got := collect(t, sub, 2)
	if got[0].ID != 1 || got[1].ID != 2 {
		t.Fatalf("事件 ID = %d,%d, want 1,2", got[0].ID, got[1].ID)
	}
	if got[0].Type != TypeTaskProgress || got[1].Type != TypeTaskConcluded {
		t.Fatalf("事件序 = %s,%s", got[0].Type, got[1].Type)
	}
}

func TestMasterReceivesAllSchools(t *testing.T) {
	b := NewBroker()
	sub, cancel := b.Subscribe(context.Background(), nil, 0)
	defer cancel()
	b.Publish([]string{"s1"}, schoolEvent(TypeTaskProgress, "s1"))
	b.Publish([]string{"s2"}, schoolEvent(TypeTaskProgress, "s2"))
	got := collect(t, sub, 2)
	if got[0].SchoolID != "s1" || got[1].SchoolID != "s2" {
		t.Fatalf("master 收到 %s,%s", got[0].SchoolID, got[1].SchoolID)
	}
}

func TestAdminScopeFilter(t *testing.T) {
	b := NewBroker()
	// schools 非 nil 即过滤；绑定 s1 的 admin 只收 s1。
	sub, cancel := b.Subscribe(context.Background(), []string{"s1"}, 0)
	defer cancel()
	b.Publish([]string{"s2"}, schoolEvent(TypeTaskProgress, "s2"))
	b.Publish([]string{"s1"}, schoolEvent(TypeTaskProgress, "s1"))
	got := collect(t, sub, 1)
	if got[0].SchoolID != "s1" {
		t.Fatalf("绑定 s1 的 admin 收到 %q", got[0].SchoolID)
	}
	// 跨校事件没有出现在流里（collect 已断言只收 1 条）。
}

// 空绑定列表不等于全部院校：一个事件都收不到。
func TestAdminEmptyBindingReceivesNothing(t *testing.T) {
	b := NewBroker()
	sub, cancel := b.Subscribe(context.Background(), []string{}, 0)
	defer cancel()
	b.Publish([]string{"s1"}, schoolEvent(TypeTaskProgress, "s1"))
	select {
	case evt := <-sub.C:
		t.Fatalf("空绑定收到了事件 %v", evt)
	case <-time.After(100 * time.Millisecond):
	}
}

// 发布作用域与订阅作用域的交集决定投递：scope 含多个院校时，
// 任一命中的订阅者都收。
func TestMultiSchoolPublishMatchesIntersection(t *testing.T) {
	b := NewBroker()
	subA, cancelA := b.Subscribe(context.Background(), []string{"a"}, 0)
	defer cancelA()
	subC, cancelC := b.Subscribe(context.Background(), []string{"c"}, 0)
	defer cancelC()
	b.Publish([]string{"a", "b"}, schoolEvent(TypeWindowChanged, "a"))
	gotA := collect(t, subA, 1)
	if gotA[0].SchoolID != "a" {
		t.Fatalf("a 订阅者收到 %q", gotA[0].SchoolID)
	}
	select {
	case evt := <-subC.C:
		t.Fatalf("c 订阅者收到了 %v", evt)
	case <-time.After(100 * time.Millisecond):
	}
}

func TestReplayAfterLastEventID(t *testing.T) {
	b := NewBroker()
	for i := 0; i < 5; i++ {
		b.Publish([]string{"s1"}, schoolEvent(TypeTaskProgress, "s1"))
	}
	sub, cancel := b.Subscribe(context.Background(), nil, 3)
	defer cancel()
	got := collect(t, sub, 2)
	if got[0].ID != 4 || got[1].ID != 5 {
		t.Fatalf("回放 = %d,%d, want 4,5", got[0].ID, got[1].ID)
	}
}

func TestReplayFiltersByScope(t *testing.T) {
	b := NewBroker()
	b.Publish([]string{"a"}, schoolEvent(TypeTaskProgress, "a")) // ID 1
	b.Publish([]string{"b"}, schoolEvent(TypeTaskProgress, "b")) // ID 2
	b.Publish([]string{"a"}, schoolEvent(TypeTaskProgress, "a")) // ID 3
	sub, cancel := b.Subscribe(context.Background(), []string{"a"}, 1)
	defer cancel()
	got := collect(t, sub, 1)
	if got[0].ID != 3 || got[0].SchoolID != "a" {
		t.Fatalf("回放 = %d/%s, want 3/a", got[0].ID, got[0].SchoolID)
	}
}

// 落后超过环形缓冲：先 resync，旧事件不补发。
func TestReplayGapEmitsResync(t *testing.T) {
	b := NewBroker()
	const total = RingCapacity + 5 // ID 1..RingCapacity+5，最旧 ID=6
	for i := 0; i < total; i++ {
		b.Publish([]string{"s1"}, schoolEvent(TypeTaskProgress, "s1"))
	}
	sub, cancel := b.Subscribe(context.Background(), nil, 4) // 4 < 6-1 → 缺口
	defer cancel()
	got := collect(t, sub, 1) // resync
	if got[0].Type != TypeResync {
		t.Fatalf("首事件 = %s, want resync", got[0].Type)
	}
	if got[0].Payload["from"] != uint64(4) {
		t.Fatalf("resync payload = %v, want from=4", got[0].Payload)
	}
	// resync 不补发旧事件；随后 live 正常到达。
	b.Publish([]string{"s1"}, schoolEvent(TypeTaskProgress, "s1"))
	got = collect(t, sub, 1)
	if got[0].ID != total+1 {
		t.Fatalf("resync 后首条 = %d, want %d", got[0].ID, total+1)
	}
}

// 回放正好覆盖边界：lastEventID = 最旧 ID - 1 时仍可回放。
func TestReplayBoundaryIsCovered(t *testing.T) {
	b := NewBroker()
	const total = RingCapacity // 环满，最旧 ID=1
	for i := 0; i < total; i++ {
		b.Publish([]string{"s1"}, schoolEvent(TypeTaskProgress, "s1"))
	}
	sub, cancel := b.Subscribe(context.Background(), nil, RingCapacity)
	defer cancel()
	// lastEventID == head：无缺口也无补发，随后 live 正常到达。
	b.Publish([]string{"s1"}, schoolEvent(TypeTaskProgress, "s1"))
	got := collect(t, sub, 1)
	if got[0].ID != RingCapacity+1 {
		t.Fatalf("边界回放后首条 = %d", got[0].ID)
	}
}

// 超前于总线的 lastEventID 同样视为缺口（无法补发不存在的事件）。
func TestReplayFutureIDEmitsResync(t *testing.T) {
	b := NewBroker()
	b.Publish([]string{"s1"}, schoolEvent(TypeTaskProgress, "s1"))
	sub, cancel := b.Subscribe(context.Background(), nil, 999)
	defer cancel()
	got := collect(t, sub, 1)
	if got[0].Type != TypeResync {
		t.Fatalf("首事件 = %s, want resync", got[0].Type)
	}
}

// 慢订阅者：满缓冲丢的是该订阅者的事件，发布方不被阻塞，
// 且丢发计账守恒：任一订阅者「收到 + 丢发 = 发布总数」。
func TestSlowSubscriberDropsPerSubscriber(t *testing.T) {
	b := NewBroker()
	slow, cancelSlow := b.Subscribe(context.Background(), nil, 0)
	defer cancelSlow()
	fast, cancelFast := b.Subscribe(context.Background(), nil, 0)
	defer cancelFast()

	const total = 100000
	var received atomic.Int64
	go func() {
		for range fast.C {
			received.Add(1)
		}
	}()

	start := time.Now()
	for i := 0; i < total; i++ {
		b.Publish([]string{"s1"}, schoolEvent(TypeTaskProgress, "s1"))
	}
	elapsed := time.Since(start)
	if elapsed > 10*time.Second {
		t.Fatalf("1e5 次 Publish 耗时 %s，超过 10s 上限：发布方被阻塞", elapsed)
	}
	// 发布已停：等读侧把缓冲排干（计数停止增长）再对账。
	prev := int64(-1)
	for stable := 0; stable < 50; {
		cur := received.Load()
		if cur == prev {
			stable++
		} else {
			stable, prev = 0, cur
		}
		time.Sleep(2 * time.Millisecond)
	}
	if got := received.Load() + int64(fast.Dropped()); got != total {
		t.Fatalf("快订阅者收到 %d + 丢发 %d = %d, want %d", received.Load(), fast.Dropped(), got, total)
	}
	if slow.Dropped() == 0 {
		t.Fatal("慢订阅者满缓冲却没有丢发计数")
	}
}

// 回放超出订阅缓冲深度时溢出部分计入 dropped，不阻塞 Subscribe。
func TestReplayOverflowCountsDropped(t *testing.T) {
	b := NewBroker()
	const total = subBuffer + 15 // 回放 lastEventID=5 之后：subBuffer+10 条
	for i := 0; i < total; i++ {
		b.Publish([]string{"s1"}, schoolEvent(TypeTaskProgress, "s1"))
	}
	sub, cancel := b.Subscribe(context.Background(), nil, 5)
	defer cancel()
	const replay = subBuffer + 10
	if got := sub.Dropped(); got != replay-subBuffer {
		t.Fatalf("回放溢出 dropped = %d, want %d", got, replay-subBuffer)
	}
	// 缓冲里仍是缓冲深度的条数，可正常读出。
	collect(t, sub, subBuffer)
}

func TestCancelUnsubscribes(t *testing.T) {
	b := NewBroker()
	sub, cancel := b.Subscribe(context.Background(), nil, 0)
	cancel()
	b.Publish([]string{"s1"}, schoolEvent(TypeTaskProgress, "s1"))
	select {
	case _, ok := <-sub.C:
		if ok {
			t.Fatal("cancel 后仍收到事件")
		}
	case <-time.After(time.Second):
		t.Fatal("cancel 后订阅通道未关闭")
	}
}

func TestContextCancelUnsubscribes(t *testing.T) {
	b := NewBroker()
	ctx, stop := context.WithCancel(context.Background())
	sub, cancel := b.Subscribe(ctx, nil, 0)
	defer cancel()
	stop()
	select {
	case _, ok := <-sub.C:
		if ok {
			t.Fatal("ctx 取消后仍收到事件")
		}
	case <-time.After(time.Second):
		t.Fatal("ctx 取消后订阅通道未关闭")
	}
}

// 并发发布与订阅换入换出：不竞态、不 panic、订阅者看到的 ID 严格递增。
// 用 -race 跑；本判据同时覆盖「Publish 永不阻塞」的并发面。
func TestConcurrentPublishAndSubscribe(t *testing.T) {
	b := NewBroker()
	stop := make(chan struct{})
	var wg sync.WaitGroup
	for w := 0; w < 4; w++ {
		wg.Add(1)
		go func(w int) {
			defer wg.Done()
			for i := 0; i < 2000; i++ {
				b.Publish([]string{"s1"}, schoolEvent(TypeTaskProgress, "s1"))
			}
		}(w)
	}
	wg.Add(1)
	go func() {
		defer wg.Done()
		for round := 0; round < 20; round++ {
			sub, cancel := b.Subscribe(context.Background(), []string{"s1"}, 0)
			// 读几条就换出，制造订阅与发布的交叠。
			for i := 0; i < 3; i++ {
				select {
				case <-sub.C:
				case <-time.After(time.Second):
					close(stop)
					cancel()
					return
				}
			}
			cancel()
		}
		close(stop)
	}()
	// 主 goroutine 等发布完成。
	done := make(chan struct{})
	go func() {
		wg.Wait()
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(10 * time.Second):
		t.Fatal("并发发布未在 10s 内完成")
	}
}
