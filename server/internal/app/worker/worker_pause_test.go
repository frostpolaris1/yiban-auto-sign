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

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appworker "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/worker"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
)

// claimCountStore 包装 fakeStore，数清 ClaimDue 与 ReclaimExpired 次数：
// 「暂停时不触认领」与「急停＝整轮不动（租约回收同停）」的判据
// 直接钉在调用点上——把暂停门挪到回收之后，本文件即红（批 3a 复审 P2-1）。
type claimCountStore struct {
	*fakeStore
	claims   int
	reclaims int
}

func (c *claimCountStore) ClaimDue(ctx context.Context, now time.Time, limit int) ([]coreschedule.TaskRow, error) {
	c.claims++
	return c.fakeStore.ClaimDue(ctx, now, limit)
}

func (c *claimCountStore) ReclaimExpired(ctx context.Context, now time.Time, limit int) (int64, error) {
	c.reclaims++
	return c.fakeStore.ReclaimExpired(ctx, now, limit)
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
	// 急停＝整轮不动：租约回收同被短路（判断 3 的门面）。
	// 若暂停门被移到 ReclaimExpired 之后，本断言转红。
	if st.reclaims != 0 {
		t.Fatalf("暂停期间租约回收了 %d 次, want 0（急停整轮不动）", st.reclaims)
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
	// 对侧钉住：未暂停轮回收照常执行——暂停门不得拦正常轮。
	if st.reclaims != 1 {
		t.Fatalf("未暂停轮租约回收次数 = %d, want 1", st.reclaims)
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

// 暂停时手动 Request 回 503 码（决策 D2）：不再静默排队。
// 判据咬在 Request 的第一道门：暂停下不读任务、不建行、不重开。
func TestRequestPausedReturnsPaused(t *testing.T) {
	st := &claimCountStore{fakeStore: newStore()}
	st.dayTasks = []string{"st-1"}
	svc := newPauseGateService(t, st, func(context.Context) (bool, error) {
		return true, nil
	})
	_, err := svc.Request(context.Background(), "acc-1", appaudit.Actor{UserID: "u-1"})
	if err == nil {
		t.Fatal("paused Request must fail")
	}
	var paused *coreerrs.PausedError
	if !errors.As(err, &paused) {
		t.Fatalf("paused Request err = %v, want PausedError", err)
	}
	if len(st.tasks) != 0 {
		t.Fatalf("paused Request created %d task rows, want 0", len(st.tasks))
	}
}

// 暂停查询故障上抛：故障不是「未暂停」，手动签到不得放行。
func TestRequestPauseQueryErrorPropagates(t *testing.T) {
	st := &claimCountStore{fakeStore: newStore()}
	st.dayTasks = []string{"st-1"}
	gateErr := errors.New("settings store down")
	svc := newPauseGateService(t, st, func(context.Context) (bool, error) {
		return false, gateErr
	})
	_, err := svc.Request(context.Background(), "acc-1", appaudit.Actor{UserID: "u-1"})
	if !errors.Is(err, gateErr) {
		t.Fatalf("Request err = %v, want the gate error", err)
	}
	if len(st.tasks) != 0 {
		t.Fatalf("故障轮创建了 %d 行, want 0", len(st.tasks))
	}
}
