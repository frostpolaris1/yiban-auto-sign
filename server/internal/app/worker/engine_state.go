package worker

// EngineState 是执行循环的进程内时刻记录（UX-5 执行监控）。
//
// # 功能
// 记录三个时刻：循环启动、最近一轮 tick、最近一次认领。
// 由 Loop 在事件发生时写入，由 /api/admin/engine 的处理器读出。
//
// # 归属
// app/worker 用例层。Loop 是唯一写入方；入口层经只读端口消费。
//
// # 口径
// 进程时间不落库、不从库里补：进程重启即清零，未发生为 nil。
// 这三个时刻描述的是**本进程**，不是任务行——任务行的时间在
// sign_tasks（claimed_at、lease_expires_at），两条事实不许混。
// 写读都持同一把锁：Loop 的 goroutine 写，HTTP 的 goroutine 读。

import (
	"sync"
	"time"
)

// EngineState 记录执行循环的进程内时刻。零值可用。
type EngineState struct {
	mu          sync.Mutex
	startedAt   time.Time
	lastTickAt  time.Time
	lastClaimAt time.Time
}

// NewEngineState 构造状态。三个时刻都未发生。
func NewEngineState() *EngineState { return &EngineState{} }

// RecordStarted 记循环启动时刻。只在 Loop.Start 成功后调用一次。
func (s *EngineState) RecordStarted(at time.Time) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.startedAt = at
}

// RecordTick 记一轮 Step 结束的时刻。每轮一次，无论该轮是否认领到任务。
func (s *EngineState) RecordTick(at time.Time) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.lastTickAt = at
}

// RecordClaim 记最近一次认领到的时刻。Step 返回处理数大于 0 时调用。
func (s *EngineState) RecordClaim(at time.Time) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.lastClaimAt = at
}

// EngineTimes 返回三时刻的快照：启动、最近一轮 tick、最近一次认领。
// 未发生的时刻为 nil。返回值是副本，改写不得穿透内部状态。
func (s *EngineState) EngineTimes() (startedAt, lastTickAt, lastClaimAt *time.Time) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return timePtr(s.startedAt), timePtr(s.lastTickAt), timePtr(s.lastClaimAt)
}

// timePtr 把零值时刻折叠成 nil：零值表示「从未发生」。
func timePtr(at time.Time) *time.Time {
	if at.IsZero() {
		return nil
	}
	copied := at
	return &copied
}
