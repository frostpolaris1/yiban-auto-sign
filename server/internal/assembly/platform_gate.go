package assembly

// 交互面平台调用的急停门与出口闸（批 abs R1 / F2）。
//
// # 功能
// gatedProvider 装饰平台 provider：detect、verify、person-name、
// device-refresh、auto-approve 后台核验与 schedule 窗口发现都经它调用。
// 每次平台调用前先过两道门：
//  1. engine.paused（坏行 fail-closed）→ 暂停回 PausedError（入口映射 503）；
//  2. 账号粘性出口的 GCRA 令牌（无账号的 detect 用确定性匿名出口）。
// 令牌不足时排队等待，不直连；Limiter 与 worker 共用同一实例与预算。
//
// # 归属
// 组合根内部件。app/account 与 entry/http/signing 只见各自的窄接口，
// 不认识本装饰层；worker 仍用裸 provider（它已有自己的 Step 门与限速）。
//
// # 口径
// 一次平台方法调用取一枚令牌（与 worker 的一次账号尝试同闸）。
// 暂停/容量错误是宿主自产 *errs.Error，用例层原样上抛，入口回 503/429。
// 等待可被请求取消；取消原样上抛，不吞成平台故障。

import (
	"context"
	"time"

	"github.com/google/uuid"

	appaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/account"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coreegress "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/egress"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corerate "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/rate"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/protocol"
)

// platformCaller 是交互面用到的平台方法最小集。
// 裸 provider 与测试替身都实现它；worker 的 Sign/DiscoverTargets 不在内。
type platformCaller interface {
	Verify(ctx context.Context, req contract.VerifyRequest) (contract.Profile, error)
	DeviceStateOf(ctx context.Context, accountID string, cred contract.Credential) (protocol.DeviceState, error)
	DiscoverWindow(ctx context.Context, req contract.DiscoverRequest) (contract.Window, error)
}

// deviceStateReader 是账号用例设备端口的装配接缝。
type deviceStateReader interface {
	DeviceStateOf(ctx context.Context, accountID string, cred contract.Credential) (protocol.DeviceState, error)
}

// windowDiscoverer 是计划用例窗口发现端口的装配接缝。
type windowDiscoverer interface {
	DiscoverWindow(ctx context.Context, req contract.DiscoverRequest) (contract.Window, error)
}

// egressPicker 是出口选择端口：账号走粘性绑定，无账号走确定性匿名选择。
type egressPicker interface {
	Assign(ctx context.Context, accountID string) (coreegress.Record, error)
	AssignAnonymous(ctx context.Context, key string) (coreegress.Record, error)
}

// egressGate 是交互面的急停门＋出口令牌闸。
type egressGate struct {
	paused  func(context.Context) (bool, error)
	egress  egressPicker
	limiter *corerate.Limiter
	clock   coreclock.Clock
	// wait 注入等待实现：生产是真定时器，测试用假时钟推进。
	wait func(context.Context, time.Duration) error
}

// newEgressGate 构造交互面闸门。依赖缺失时返回 nil，由调用方装配失败关闭。
func newEgressGate(egress egressPicker, limiter *corerate.Limiter, clk coreclock.Clock,
	paused func(context.Context) (bool, error)) *egressGate {
	if egress == nil || limiter == nil || clk == nil || paused == nil {
		return nil
	}
	return &egressGate{
		paused: paused, egress: egress, limiter: limiter, clock: clk,
		wait: waitForTimer,
	}
}

// acquire 取得一次平台调用许可：急停检查 → 出口选择 → 令牌等待。
func (g *egressGate) acquire(ctx context.Context, accountID string) error {
	if g == nil {
		// 装配缺失不得静默直连：交互面平台调用必须显性失败。
		return coreerrs.New(coreerrs.CodeInternal, "platform egress gate is not wired")
	}
	paused, err := g.paused(ctx)
	if err != nil {
		return err
	}
	if paused {
		return coreerrs.NewPaused()
	}
	eg, err := g.pickEgress(ctx, accountID)
	if err != nil {
		return err
	}
	for {
		now := g.clock.Now()
		if g.limiter.Admit(eg.ID, now) {
			return nil
		}
		wait := g.limiter.RetryAfter(eg.ID, now)
		if wait <= 0 {
			wait = time.Second // 桶拒绝但等待量为零：兜底 1s，防忙等
		}
		if err := g.wait(ctx, wait); err != nil {
			return err
		}
	}
}

// pickEgress 为一次平台调用选出口。
// 真实账号（UUID）走粘性绑定；detect 的合成会话键没有账号行，
// 用确定性匿名选择，仍落在某个真实出口的同一令牌桶上。
func (g *egressGate) pickEgress(ctx context.Context, accountID string) (coreegress.Record, error) {
	if _, err := uuid.Parse(accountID); err == nil {
		return g.egress.Assign(ctx, accountID)
	}
	return g.egress.AssignAnonymous(ctx, accountID)
}

// waitForTimer 是生产等待：可被请求取消。
func waitForTimer(ctx context.Context, d time.Duration) error {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

// gatedProvider 在每次平台调用前过急停门与出口闸。
type gatedProvider struct {
	inner platformCaller
	gate  *egressGate
}

// 编译期钉住三个消费面：账号认证、设备读回、窗口发现。
var (
	_ appaccount.IdentityVerifier = gatedProvider{}
	_ deviceStateReader           = gatedProvider{}
	_ windowDiscoverer            = gatedProvider{}
)

// Verify 实现 app/account.IdentityVerifier 与 signing.Verifier。
func (p gatedProvider) Verify(ctx context.Context, req contract.VerifyRequest) (contract.Profile, error) {
	if err := p.gate.acquire(ctx, req.Account.ID); err != nil {
		return contract.Profile{}, err
	}
	return p.inner.Verify(ctx, req)
}

// DeviceStateOf 实现设备读回端口。
func (p gatedProvider) DeviceStateOf(ctx context.Context, accountID string, cred contract.Credential) (protocol.DeviceState, error) {
	if err := p.gate.acquire(ctx, accountID); err != nil {
		return protocol.DeviceState{}, err
	}
	return p.inner.DeviceStateOf(ctx, accountID, cred)
}

// DiscoverWindow 实现计划用例的窗口发现端口。
func (p gatedProvider) DiscoverWindow(ctx context.Context, req contract.DiscoverRequest) (contract.Window, error) {
	if err := p.gate.acquire(ctx, req.Account.ID); err != nil {
		return contract.Window{}, err
	}
	return p.inner.DiscoverWindow(ctx, req)
}
