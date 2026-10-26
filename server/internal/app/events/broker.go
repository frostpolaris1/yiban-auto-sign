// Package events 实现进程内事件总线（P6）。
//
// # 功能
// Broker 维护一条全局环形缓冲与一组订阅；Publish 发事件、
// Subscribe 按院校作用域订阅并可选按 Last-Event-ID 回放。
// 事件词表是闭集常量，见本文件 Type 常量。
// 本包**不**碰数据库、**不**做心跳、**不**持久化。
//
// # 归属
// app 层。发布方是 app/worker 与 app/schedule 的可选 Notifier 端口；
// 订阅方是 entry/http/events 的 SSE 处理器。
//
// # 复用
// Broker、Event、Subscription、Type 常量。
//
// # 口径
// 单进程是明文设计不变量：跨重启不承诺续传，重启即新总线。
// 设计取舍（snapshot 的生成方）：Broker 无库访问，snapshot 由
// 订阅方（SSE 处理器）在收到 resync 后现算并直写流，不经总线、
// 不占环形缓冲。选它而非「发布方首次注入」是因为它不需要
// 发布方感知订阅生命周期，且单测只需假数据源。
package events

import (
	"context"
	"sync"
	"sync/atomic"
	"time"
)

// 事件词表（闭集）。新增词必须改这里并同步契约文档。
const (
	TypeSnapshot        = "snapshot"         // 当日前景，由订阅方在 resync 后生成
	TypeTaskProgress    = "task.progress"    // 任务重排（等待或退避）
	TypeTaskConcluded   = "task.concluded"   // 任务落终态
	TypeWindowChanged   = "window.changed"   // 窗口重算并落库
	TypeCapacityChanged = "capacity.changed" // 容量出现缺口
	TypeEgressHealth    = "egress.health"    // 出口健康（P6 无发布点，词表预留）
	TypeHeartbeat       = "heartbeat"        // SSE 层 15s 心跳，不经总线
	TypeResync          = "resync"           // 缺口过大，客户端需重取全景
	TypeEnd             = "end"              // SSE 层空闲关闭，不经总线
)

// ringCapacity 是全局环形缓冲条数。
// 回放覆盖 lastEventID ≥ head-ringCapacity+1 的缺口；更旧即 resync。
const ringCapacity = 1000

// subBuffer 是每订阅者通道深度。满即丢该订阅者的事件并累计 dropped。
const subBuffer = 64

// Event 是总线信封。ID 由 Broker 在 Publish 内分配，发布方不得预填。
// At 由发布方用自己的时钟填；Broker 生成的 resync 事件 At 为零值。
// Payload 只放 id 与数字/枚举词，不得放手机号、凭据、平台原文。
type Event struct {
	ID       uint64
	Type     string
	SchoolID string
	TaskID   string
	Day      string
	At       time.Time
	Payload  map[string]any
}

// Subscription 是一次订阅的读取端。
// C 关闭表示订阅已终止（cancel 或订阅 ctx 结束）。
type Subscription struct {
	C    <-chan Event
	drop *atomic.Uint64
}

// Dropped 返回本订阅因缓冲满被丢弃的事件数。
func (s Subscription) Dropped() uint64 {
	if s.drop == nil {
		return 0
	}
	return s.drop.Load()
}

// subscriber 是 Broker 内部的订阅记录。
// schools 为 nil 表示 master（收全部院校）；
// 非 nil（含空表）表示只收命中的院校——空表一概不收。
type subscriber struct {
	schools map[string]struct{}
	ch      chan Event
	dropped atomic.Uint64
}

// matchesScope 报告发布作用域是否命中本订阅。
// master 全量命中；admin 按交集命中。
func (s *subscriber) matchesScope(scope []string) bool {
	if s.schools == nil {
		return true
	}
	for _, id := range scope {
		if _, ok := s.schools[id]; ok {
			return true
		}
	}
	return false
}

// matchesSchool 报告单院校事件是否属于本订阅（回放过滤用）。
func (s *subscriber) matchesSchool(schoolID string) bool {
	if s.schools == nil {
		return true
	}
	_, ok := s.schools[schoolID]
	return ok
}

// send 非阻塞投递。满即丢并计数；发布方永不等待。
func (s *subscriber) send(evt Event) {
	select {
	case s.ch <- evt:
	default:
		s.dropped.Add(1)
	}
}

// Broker 是进程内 fan-out 总线。
// 全部状态由 mu 保护：发布、回放与退订互斥，
// 保证任一订阅者读到的事件按 ID 严格递增。
type Broker struct {
	mu    sync.Mutex
	seq   uint64
	buf   [ringCapacity]Event
	start int // 最旧元素下标
	n     int // 有效条数
	subs  map[*subscriber]struct{}
}

// NewBroker 构造空总线。
func NewBroker() *Broker {
	return &Broker{subs: make(map[*subscriber]struct{})}
}

// Publish 分配 ID、入环、按作用域扇出。
// scope 是事件涉及的院校 id 集合；空集合只对 master 可见。
// 本调用**永不阻塞**：慢订阅者的事件被丢弃并计入其 dropped。
func (b *Broker) Publish(scope []string, evt Event) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.seq++
	evt.ID = b.seq
	b.buf[(b.start+b.n)%ringCapacity] = evt
	if b.n < ringCapacity {
		b.n++
	} else {
		b.start = (b.start + 1) % ringCapacity
	}
	for sub := range b.subs {
		if sub.matchesScope(scope) {
			sub.send(evt)
		}
	}
}

// Subscribe 注册订阅并按需回放。
//
// schools 为 nil 表示 master（全部院校）；非 nil 空表一概不收
// （与院校域的「空集合不等于全部」同口径）。lastEventID 为 0 表示
// 新连接：不回放，当前状态由订阅方自行经 REST 获取。
// lastEventID 大于 0 且缺口在环形缓冲内 → 按序补发其后事件并按
// 院校过滤；缺口过大或指向未来 → 先发一条 resync（Payload.from 记
// 客户端的 lastEventID），不补发旧事件。
//
// 返回的 cancel 与 ctx 取消等效：都注销订阅并关闭订阅通道。
// cancel 同步生效：返回后发布不再投递给本订阅。
// 回放在注册锁内完成，与并发 Publish 严格保序；
// 回放量超过 subBuffer 时溢出部分计入 dropped。
func (b *Broker) Subscribe(ctx context.Context, schools []string, lastEventID uint64) (Subscription, context.CancelFunc) {
	sub := &subscriber{ch: make(chan Event, subBuffer)}
	if schools != nil {
		sub.schools = make(map[string]struct{}, len(schools))
		for _, id := range schools {
			sub.schools[id] = struct{}{}
		}
	}
	b.mu.Lock()
	b.subs[sub] = struct{}{}
	b.replay(sub, lastEventID)
	b.mu.Unlock()

	unregister := func() {
		b.mu.Lock()
		if _, ok := b.subs[sub]; ok {
			delete(b.subs, sub)
			close(sub.ch)
		}
		b.mu.Unlock()
	}
	cctx, cancel := context.WithCancel(ctx)
	go func() {
		<-cctx.Done()
		unregister()
	}()
	return Subscription{C: sub.ch, drop: &sub.dropped}, func() {
		cancel()
		unregister()
	}
}

// replay 按 lastEventID 补发或发 resync。调用方必须持有 mu。
func (b *Broker) replay(sub *subscriber, lastEventID uint64) {
	if lastEventID == 0 {
		return
	}
	head := b.seq
	if lastEventID <= head && lastEventID+1 >= head-uint64(b.n)+1 {
		// 缺口完整落在环内：最旧 ID = head-n+1 ≤ lastEventID+1。
		for k := 0; k < b.n; k++ {
			evt := b.buf[(b.start+k)%ringCapacity]
			if evt.ID <= lastEventID {
				continue
			}
			if !sub.matchesSchool(evt.SchoolID) {
				continue
			}
			sub.send(evt)
		}
		return
	}
	// 缺口过大或指向未来：resync，快照由订阅方生成。
	sub.send(Event{Type: TypeResync, Payload: map[string]any{"from": lastEventID}})
}
