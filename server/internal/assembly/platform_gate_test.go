package assembly

// 交互面急停门与出口闸的守卫单测（批 abs R1 / F2）。
//
// 判据：
//   - paused=true 时三类平台调用一次都不落到 inner（出站连接计数 0）；
//   - 令牌不足时先等待再调用，不直连；等待可取消且取消后不调用；
//   - detect 的合成会话键走匿名出口，真实账号走粘性绑定；
//   - 暂停查询故障与出口缺失原样上抛，不吞成平台故障。

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/google/uuid"

	coreegress "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/egress"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corerate "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/rate"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/protocol"
)

// countingPlatform 数三类交互面平台调用；被调用即视为一次出站连接。
type countingPlatform struct {
	verify int
	device int
	window int
}

func (c *countingPlatform) Verify(context.Context, contract.VerifyRequest) (contract.Profile, error) {
	c.verify++
	return contract.Profile{}, nil
}

func (c *countingPlatform) DeviceStateOf(context.Context, string, contract.Credential) (protocol.DeviceState, error) {
	c.device++
	return protocol.DeviceState{}, nil
}

func (c *countingPlatform) DiscoverWindow(context.Context, contract.DiscoverRequest) (contract.Window, error) {
	c.window++
	return contract.Window{}, nil
}

func (c *countingPlatform) calls() int { return c.verify + c.device + c.window }

// fakeEgressPicker 记录出口选择；record/err 是返回给闸门的结论。
type fakeEgressPicker struct {
	assigned  []string
	anonymous []string
	record    coreegress.Record
	err       error
}

func (f *fakeEgressPicker) Assign(_ context.Context, accountID string) (coreegress.Record, error) {
	f.assigned = append(f.assigned, accountID)
	if f.err != nil {
		return coreegress.Record{}, f.err
	}
	return f.record, nil
}

func (f *fakeEgressPicker) AssignAnonymous(_ context.Context, key string) (coreegress.Record, error) {
	f.anonymous = append(f.anonymous, key)
	if f.err != nil {
		return coreegress.Record{}, f.err
	}
	return f.record, nil
}

// stepClock 是可推进的测试时钟。
type stepClock struct{ t time.Time }

func (c *stepClock) Now() time.Time { return c.t }

// recordingWait 记录等待并把测试时钟推进同样的时长。
func recordingWait(clk *stepClock, waits *[]time.Duration) func(context.Context, time.Duration) error {
	return func(_ context.Context, d time.Duration) error {
		*waits = append(*waits, d)
		clk.t = clk.t.Add(d)
		return nil
	}
}

func newTestGate(t *testing.T, paused func(context.Context) (bool, error),
	picker egressPicker, limiter *corerate.Limiter, clk *stepClock,
	wait func(context.Context, time.Duration) error) *egressGate {
	t.Helper()
	gate := newEgressGate(picker, limiter, clk, paused)
	if gate == nil {
		t.Fatal("newEgressGate returned nil")
	}
	if wait != nil {
		gate.wait = wait
	}
	return gate
}

// isPausedErr 报告错误是否为急停类型（线缆码沿用 capacity_exceeded）。
func isPausedErr(err error) bool {
	var paused *coreerrs.PausedError
	return errors.As(err, &paused)
}

// paused=true：三类调用都回 paused 码，inner 一次都没被调用。
func TestGatedProviderPausedMakesNoPlatformCall(t *testing.T) {
	inner := &countingPlatform{}
	picker := &fakeEgressPicker{record: coreegress.Record{ID: "eg-1"}}
	clk := &stepClock{t: time.Unix(1_000_000, 0)}
	gate := newTestGate(t, func(context.Context) (bool, error) { return true, nil },
		picker, corerate.NewLimiter(1, 1), clk, nil)
	p := gatedProvider{inner: inner, gate: gate}
	ctx := context.Background()

	if _, err := p.Verify(ctx, contract.VerifyRequest{
		Account: contract.AccountRef{ID: uuid.NewString()},
	}); !isPausedErr(err) {
		t.Fatalf("Verify err = %v, want paused", err)
	}
	if _, err := p.DeviceStateOf(ctx, uuid.NewString(), contract.Credential{}); !isPausedErr(err) {
		t.Fatalf("DeviceStateOf err = %v, want paused", err)
	}
	if _, err := p.DiscoverWindow(ctx, contract.DiscoverRequest{
		Account: contract.AccountRef{ID: uuid.NewString()},
	}); !isPausedErr(err) {
		t.Fatalf("DiscoverWindow err = %v, want paused", err)
	}
	if inner.calls() != 0 {
		t.Fatalf("paused 下平台调用 %d 次, want 0（不得出站）", inner.calls())
	}
	if len(picker.assigned)+len(picker.anonymous) != 0 {
		t.Fatal("paused 下不应选择出口")
	}
}

// 令牌不足：先等待再调用；等待时长来自 GCRA 的 RetryAfter。
func TestGatedProviderWaitsForEgressToken(t *testing.T) {
	inner := &countingPlatform{}
	picker := &fakeEgressPicker{record: coreegress.Record{ID: "eg-1"}}
	clk := &stepClock{t: time.Unix(1_000_000, 0)}
	var waits []time.Duration
	// rate=0.2/s、burst=1：首次放行后每 5s 一枚。
	gate := newTestGate(t, func(context.Context) (bool, error) { return false, nil },
		picker, corerate.NewLimiter(0.2, 1), clk, recordingWait(clk, &waits))
	p := gatedProvider{inner: inner, gate: gate}
	ctx := context.Background()
	req := contract.VerifyRequest{Account: contract.AccountRef{ID: uuid.NewString()}}

	if _, err := p.Verify(ctx, req); err != nil {
		t.Fatalf("first Verify: %v", err)
	}
	if inner.calls() != 1 || len(waits) != 0 {
		t.Fatalf("first call: inner=%d waits=%v, want 1/0", inner.calls(), waits)
	}
	if _, err := p.Verify(ctx, req); err != nil {
		t.Fatalf("second Verify: %v", err)
	}
	if len(waits) != 1 || waits[0] != 5*time.Second {
		t.Fatalf("waits = %v, want [5s]", waits)
	}
	if inner.calls() != 2 {
		t.Fatalf("second call inner=%d, want 2（等待后放行）", inner.calls())
	}
}

// 等待被取消：不调用 inner，错误原样上抛。
func TestGatedProviderCanceledWaitDoesNotCall(t *testing.T) {
	inner := &countingPlatform{}
	picker := &fakeEgressPicker{record: coreegress.Record{ID: "eg-1"}}
	clk := &stepClock{t: time.Unix(1_000_000, 0)}
	limiter := corerate.NewLimiter(0.2, 1)
	gate := newTestGate(t, func(context.Context) (bool, error) { return false, nil },
		picker, limiter, clk, func(context.Context, time.Duration) error {
			return context.Canceled
		})
	p := gatedProvider{inner: inner, gate: gate}
	ctx := context.Background()
	req := contract.VerifyRequest{Account: contract.AccountRef{ID: uuid.NewString()}}
	// 消耗掉唯一的突发额度。
	if _, err := p.Verify(ctx, req); err != nil {
		t.Fatalf("first Verify: %v", err)
	}
	if _, err := p.Verify(ctx, req); !errors.Is(err, context.Canceled) {
		t.Fatalf("second Verify err = %v, want context.Canceled", err)
	}
	if inner.calls() != 1 {
		t.Fatalf("取消后 inner 调用 %d 次, want 1", inner.calls())
	}
}

// detect 的合成会话键没有账号行：走匿名出口；真实 UUID 走粘性绑定。
func TestGatedProviderPicksEgressByAccountShape(t *testing.T) {
	inner := &countingPlatform{}
	picker := &fakeEgressPicker{record: coreegress.Record{ID: "eg-1"}}
	clk := &stepClock{t: time.Unix(1_000_000, 0)}
	gate := newTestGate(t, func(context.Context) (bool, error) { return false, nil },
		picker, corerate.NewLimiter(4, 6), clk, nil)
	p := gatedProvider{inner: inner, gate: gate}
	ctx := context.Background()

	if _, err := p.Verify(ctx, contract.VerifyRequest{
		Account: contract.AccountRef{ID: "detect:abc123"},
	}); err != nil {
		t.Fatalf("detect Verify: %v", err)
	}
	if len(picker.anonymous) != 1 || picker.anonymous[0] != "detect:abc123" {
		t.Fatalf("anonymous = %v, want [detect:abc123]", picker.anonymous)
	}
	if len(picker.assigned) != 0 {
		t.Fatalf("detect 不应走账号绑定: %v", picker.assigned)
	}

	accountID := uuid.NewString()
	if _, err := p.Verify(ctx, contract.VerifyRequest{
		Account: contract.AccountRef{ID: accountID},
	}); err != nil {
		t.Fatalf("account Verify: %v", err)
	}
	if len(picker.assigned) != 1 || picker.assigned[0] != accountID {
		t.Fatalf("assigned = %v, want [%s]", picker.assigned, accountID)
	}
}

// 暂停查询故障与出口缺失原样上抛：不吞成平台故障，也不直连。
func TestGatedProviderPropagatesGateFailures(t *testing.T) {
	inner := &countingPlatform{}
	clk := &stepClock{t: time.Unix(1_000_000, 0)}
	ctx := context.Background()
	req := contract.VerifyRequest{Account: contract.AccountRef{ID: uuid.NewString()}}

	storeErr := errors.New("settings store down")
	gate := newTestGate(t, func(context.Context) (bool, error) { return false, storeErr },
		&fakeEgressPicker{record: coreegress.Record{ID: "eg-1"}},
		corerate.NewLimiter(1, 1), clk, nil)
	if _, err := (gatedProvider{inner: inner, gate: gate}).Verify(ctx, req); !errors.Is(err, storeErr) {
		t.Fatalf("pause query err = %v, want store error", err)
	}

	noEgress := coreerrs.New(coreerrs.CodeCapacity, "no enabled egress")
	gate = newTestGate(t, func(context.Context) (bool, error) { return false, nil },
		&fakeEgressPicker{err: noEgress}, corerate.NewLimiter(1, 1), clk, nil)
	if _, err := (gatedProvider{inner: inner, gate: gate}).Verify(ctx, req); coreerrs.From(err).Code != coreerrs.CodeCapacity {
		t.Fatalf("egress err = %v, want capacity", err)
	}
	if inner.calls() != 0 {
		t.Fatalf("门失败后 inner 调用 %d 次, want 0", inner.calls())
	}
}
