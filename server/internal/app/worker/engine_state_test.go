package worker

// EngineState 的判据：进程内三时刻的记录与快照。
//
// # 口径
// 未发生为 nil；快照是副本，外部改写不得穿透；
// Loop 集成只判「事件发生过」，不钉调用顺序（测不变式）。
// 时刻值取 clock.Fixed：断言用同一注入值，不比墙钟。

import (
	"context"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
)

// TestEngineTimesNilBeforeEvents：零值状态的三时刻都是 nil。
func TestEngineTimesNilBeforeEvents(t *testing.T) {
	var state EngineState
	started, lastTick, lastClaim := state.EngineTimes()
	if started != nil || lastTick != nil || lastClaim != nil {
		t.Fatalf("fresh state must have nil times: %v %v %v", started, lastTick, lastClaim)
	}
}

// TestEngineStateRecordsTimes：三个记录方法各自落位；快照是副本。
func TestEngineStateRecordsTimes(t *testing.T) {
	at := clock.System{}.Now()
	state := NewEngineState()

	state.RecordStarted(at)
	started, lastTick, lastClaim := state.EngineTimes()
	if started == nil || !started.Equal(at) {
		t.Fatalf("StartedAt = %v, want %v", started, at)
	}
	if lastTick != nil || lastClaim != nil {
		t.Fatalf("unticked state must keep nil: %v %v", lastTick, lastClaim)
	}

	tick := at.Add(time.Second)
	claim := at.Add(2 * time.Second)
	state.RecordTick(tick)
	state.RecordClaim(claim)
	started, lastTick, lastClaim = state.EngineTimes()
	if lastTick == nil || !lastTick.Equal(tick) {
		t.Fatalf("LastTickAt = %v, want %v", lastTick, tick)
	}
	if lastClaim == nil || !lastClaim.Equal(claim) {
		t.Fatalf("LastClaimAt = %v, want %v", lastClaim, claim)
	}

	// 快照是副本：改写返回值不得穿透内部状态。
	*lastTick = at
	_, tickAgain, _ := state.EngineTimes()
	if !tickAgain.Equal(tick) {
		t.Fatalf("snapshot is aliased: %v, want %v", tickAgain, tick)
	}
}

// TestLoopRecordsEngineTimes：Start 记 started；完成一轮记 tick；
// 处理数大于 0 记 claim。先等一轮进入再放行，保证「有一轮已完成」：
// Stop 与首轮的先后不是本测试的判据（不测编舞）。
func TestLoopRecordsEngineTimes(t *testing.T) {
	at := clock.System{}.Now()
	clk := clock.Fixed{T: at}
	state := NewEngineState()
	begun := make(chan struct{})
	release := make(chan struct{})
	loop := NewLoop(&fakeStepper{begun: begun, release: release}, &fakeCounter{}, clk, state,
		5*time.Millisecond, 1)
	if err := loop.Start(); err != nil {
		t.Fatalf("Start: %v", err)
	}
	select {
	case <-begun:
	case <-time.After(2 * time.Second):
		t.Fatal("Step 未被启动")
	}
	close(release) // 放行在飞：该轮将完成且处理数大于 0
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if _, err := loop.Stop(ctx); err != nil {
		t.Fatalf("Stop: %v", err)
	}

	started, lastTick, lastClaim := state.EngineTimes()
	if started == nil || !started.Equal(at) {
		t.Fatalf("StartedAt = %v, want the injected clock value %v", started, at)
	}
	if lastTick == nil {
		t.Fatal("LastTickAt must be set after a completed step")
	}
	if lastClaim == nil {
		t.Fatal("LastClaimAt must be set when a step processed rows")
	}
}

// TestLoopWithoutStateRuns：state 为 nil 时循环照常执行，不记录。
func TestLoopWithoutStateRuns(t *testing.T) {
	release := make(chan struct{})
	close(release)
	loop := NewLoop(&fakeStepper{release: release}, &fakeCounter{}, clock.System{}, nil,
		5*time.Millisecond, 1)
	if err := loop.Start(); err != nil {
		t.Fatalf("Start: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if _, err := loop.Stop(ctx); err != nil {
		t.Fatalf("Stop: %v", err)
	}
}
