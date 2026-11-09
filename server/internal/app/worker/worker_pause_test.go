package worker_test

// 全局暂停门（批 3a）的守卫单测。
//
// 判据：paused=true 时 Step 整轮不动（不认领，返回 0）；暂停查询故障
// 上抛且本轮不认领；未暂停时认领照常（缺行行为保持）；装配缺暂停门
// 是构造错误（失败关闭）。

import (
	"context"
	"errors"
	"testing"
	"time"

	appworker "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/worker"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
)

// claimCountStore 包装 fakeStore，只为数清 ClaimDue 次数：
// 「暂停时不触认领」的判据直接钉在调用点上。
type claimCountStore struct {
	*fakeStore
	claims int
}

func (c *claimCountStore) ClaimDue(ctx context.Context, now time.Time, limit int) ([]coreschedule.TaskRow, error) {
	c.claims++
	return c.fakeStore.ClaimDue(ctx, now, limit)
}

// newPauseGateService 构造带可切换暂停门的执行服务。
func newPauseGateService(t *testing.T, st *claimCountStore, gate func(context.Context) (bool, error)) *appworker.Service {
	t.Helper()
	svc, err := appworker.NewService(appworker.Deps{
		Store: st, Creds: fakeCreds{}, Signer: defaultSigner(), Egress: fakeEgress{},
		Limiter: fakeLimiter{allow: true}, Clock: coreclock.Fixed{T: testNow()}, Location: loc,
		Paused: gate,
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc
}

// 暂停生效：Step 返回 (0, nil)，认领一次都不发生。
func TestStepPausedSkipsClaim(t *testing.T) {
	st := &claimCountStore{fakeStore: newStore(baseTask())}
	st.windows["st-1|2026-10-29"] = openWindow()
	calls := 0
	svc := newPauseGateService(t, st, func(context.Context) (bool, error) {
		calls++
		return true, nil
	})
	for i := 0; i < 3; i++ {
		n, err := svc.Step(context.Background(), 10)
		if err != nil || n != 0 {
			t.Fatalf("Step %d = (%d, %v), want (0, nil)", i, n, err)
		}
	}
	if calls != 3 {
		t.Fatalf("暂停门被查询 %d 次, want 3（每轮一次）", calls)
	}
	if st.claims != 0 {
		t.Fatalf("暂停期间认领了 %d 次, want 0", st.claims)
	}
}

// 暂停查询故障上抛：本轮不认领。故障不是「未暂停」，不得误放行。
func TestStepPauseQueryErrorPropagates(t *testing.T) {
	st := &claimCountStore{fakeStore: newStore(baseTask())}
	st.windows["st-1|2026-10-29"] = openWindow()
	gateErr := errors.New("settings store down")
	svc := newPauseGateService(t, st, func(context.Context) (bool, error) {
		return false, gateErr
	})
	n, err := svc.Step(context.Background(), 10)
	if !errors.Is(err, gateErr) {
		t.Fatalf("Step err = %v, want the gate error", err)
	}
	if n != 0 {
		t.Fatalf("Step n = %d, want 0", n)
	}
	if st.claims != 0 {
		t.Fatalf("故障轮认领了 %d 次, want 0", st.claims)
	}
}

// 未暂停（含缺行读回 false）时认领照常：现行行为保持，不放宽也不收紧。
func TestStepNotPausedClaimsNormally(t *testing.T) {
	st := &claimCountStore{fakeStore: newStore(baseTask())}
	st.windows["st-1|2026-10-29"] = openWindow()
	svc := newPauseGateService(t, st, func(context.Context) (bool, error) {
		return false, nil
	})
	n, err := svc.Step(context.Background(), 10)
	if err != nil || n != 1 {
		t.Fatalf("Step = (%d, %v), want (1, nil)", n, err)
	}
	if st.claims != 1 {
		t.Fatalf("认领次数 = %d, want 1", st.claims)
	}
}

// 装配缺暂停门是编程错误：构造即失败，不得静默当「永不暂停」。
func TestNewServiceRequiresPauseGate(t *testing.T) {
	_, err := appworker.NewService(appworker.Deps{
		Store: newStore(), Creds: fakeCreds{}, Signer: defaultSigner(),
		Egress: fakeEgress{}, Limiter: fakeLimiter{allow: true},
		Clock: coreclock.Fixed{T: testNow()}, Location: loc,
	})
	if err == nil {
		t.Fatal("nil pause gate must fail construction")
	}
}
