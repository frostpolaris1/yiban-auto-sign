package main

// 租约双跑 e2e（量化实验）：一个账号、一行计划、两个执行体。
//
// # 要证明的事
// 认领租约只能回答"谁在跑"，回答不了"上一次认领的执行体还活着吗"。租约到期
// 就回收，是把"可能还活着"当成"已经死了"：回收把行改回 pending 的那一刻，
// 库层唯一的门（uq_sign_tasks_one_running，只盖 state='running'）就消失了。
// 第二个执行体随即认领同一账号，两次真实提交同时在空中——平台侧看到的是
// 同一账号的两条签到链，学生侧看到的是"签到成功"后面又跟着一次失败。
//
// 两次提交之间**没有**任何幂等保证可用：平台每次登录签发新的 attempt id，
// 服务端不会把第二次当成第一次的重放。所以这条路径只能靠纵深两层防：
//
//	A. 一次尝试的总截止时间（YIBAN_ATTEMPT_BUDGET_SEC）：预算 < 租约，
//	   本次尝试必然先于租约到期自我了结，回收器根本等不到过期行；
//	B. 回收冷却（YIBAN_RECLAIM_GRACE_SEC）：租约到期后不立刻重排，
//	   给慢提交留一段落回终态的时间。
//
// A 依赖提供方尊重 ctx（HTTP 客户端取消）；B 不依赖任何东西，只推迟重排。
// 两者互补，本文件把 5 种组合各跑一遍，用同一套判据量一次：
//
//	重复提交次数 = Sign 调用总数 - 1（第二次起都是重复）
//	峰值并发     = 同时在空中的 Sign 数
//	撞栅栏次数   = 结算/重排被 claim_seq 拒掉的行数（旧持有者的迟到结算）
//
// # 口径
// 缺 DSN 时整包跳过（TestMain 已打横幅）。时钟用真时钟：本实验的判据是
// 真实时间上的并发，冻结时钟会自己消灭被观察的现象。

import (
	"context"
	"errors"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	appegress "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/egress"
	appworker "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/worker"
	coreaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/account"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corerate "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/rate"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	coreschool "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/school"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// labSigner 是本次实验的被观察对象：Sign 睡 hold 时长，尊重 ctx（ignoreCtx=false）
// 或完全不看 ctx（ignoreCtx=true）。后者模拟"提供方自带的 HTTP 重试把取消吞掉"，
// 即预算切不断的真实存在的那一类客户端。
type labSigner struct {
	hold      time.Duration
	ignoreCtx bool

	entered chan string // 每次进入 Sign 发一个 attempt id（容量足够）

	mu         sync.Mutex
	calls      int
	active     int
	peak       int
	attemptIDs map[string]int
	order      []string
}

func newLabSigner(hold time.Duration, ignoreCtx bool) *labSigner {
	return &labSigner{
		hold: hold, ignoreCtx: ignoreCtx,
		entered:    make(chan string, 16),
		attemptIDs: map[string]int{},
	}
}

func (s *labSigner) DiscoverTargets(context.Context, contract.DiscoverRequest) ([]contract.Target, error) {
	return []contract.Target{{ID: "pos-1", Title: "东门"}}, nil
}

func (s *labSigner) Sign(ctx context.Context, req contract.SignRequest) (contract.Result, error) {
	s.mu.Lock()
	s.calls++
	s.active++
	if s.active > s.peak {
		s.peak = s.active
	}
	s.attemptIDs[req.AttemptID]++
	s.order = append(s.order, req.AttemptID)
	s.mu.Unlock()

	s.entered <- req.AttemptID

	defer func() {
		s.mu.Lock()
		s.active--
		s.mu.Unlock()
	}()

	if s.ignoreCtx {
		time.Sleep(s.hold)
		return contract.Result{Outcome: contract.OutcomeSuccess}, nil
	}
	select {
	case <-time.After(s.hold):
		return contract.Result{Outcome: contract.OutcomeSuccess}, nil
	case <-ctx.Done():
		return contract.Result{}, ctx.Err()
	}
}

// labCreds 是固定凭据替身：协议面不是本实验的变量。
type labCreds struct{}

func (labCreds) Credential(context.Context, string) (contract.Credential, error) {
	return contract.Credential{Phone: "13800000001", Password: "x", DeviceCode: "d", PhoneModel: "m"}, nil
}

func (labCreds) KID() string { return "" }

// labCell 是矩阵的一格。
type labCell struct {
	name      string
	lease     time.Duration // 认领租约
	grace     time.Duration // 回收冷却（B）
	budget    time.Duration // 一次尝试总截止（A）
	ignoreCtx bool          // true = 提供方吞掉取消（A 切不断）
	// wantDup 是期望的重复提交次数（Sign 次数 - 1）。
	wantDup int
	// 其余量纲逐格钉死：矩阵表里的数字若没有断言支撑，
	// 文档写的"峰值并发 2 / 回收 1 / 栅栏拒写 1"就只是日志。
	// -1 表示该格不产生这个量纲（例如没有回收就没有栅栏拒写）。
	wantPeak     int
	wantDistinct int
	wantReclaims int
	wantFences   int
	wantAttempts int
	// wantLateSigns 是第二幕的期望：冷却一过，行会被重新认领并再提交一次。
	// 0 表示本格不跑第二幕（它要额外等满冷却 + 一次 hold，属于慢格）；
	// 只有"仅 B 冷却"一格需要它，因为"延迟重复"是 B 单独用时的核心结论。
	wantLateSigns int
	// secondAt 是第二个执行体开跑的时刻（相对本格起点）。
	// 零值 = lease+100ms（"下一个 tick"在租约到期之后）；
	// 需要证明"租约没过期就没人能领"的格子必须把它压到租约之内。
	secondAt time.Duration
}

// labObservation 是一格的全部量化结果。
type labObservation struct {
	name       string
	signs      int
	dup        int
	peak       int
	distinct   int
	reclaims   int
	fences     int
	attempts   int
	resultWord string
	state      string
	elapsed    time.Duration
	// lateSigns / lateAttempts / lateState 是第二幕（冷却过去之后再跑一轮）的结果。
	// -1 = 本格没跑第二幕。
	lateSigns    int
	lateAttempts int
	lateState    string
}

// 7 格矩阵：基线 / 仅 A / 仅 B / A+B / A 遇上吞取消的提供方。
//
// 每格的 signs = wantDup+1（由 wantDup 断言）；peak / distinct / reclaims /
// fences / attempts 逐格钉死，-1 表示该格不应出现该量纲。
//
// 租约必须大于 attemptLeaseReserve（settlementTimeout+200ms=1s），否则
// effectiveAttemptBudget 会在认领后直接判 leaseSpent，执行体 1 连提交都
// 进不去。这里用 1.5s/3s：仍远小于生产 120s，但足够覆盖结算余量。
//
// 基线/仅 B 格的 budget=0 是正控支点：0 不封顶，才会出现回收后的第二次
// 提交（dup=1）。生产配置 0 会派生 0.8×租约，不会走这条正控路径。
var labMatrix = []labCell{
	// wantDistinct=2：回收后的第二次认领会 bump claim_seq，幂等键随之换新
	// （attemptID 带 claim_seq，见 issue #403）。两次提交不同键，说明
	// "接管"对提供方是另一次执行，不会读回第一次的缓存。
	{name: "基线（无 A 无 B）", lease: 1500 * time.Millisecond, wantDup: 1,
		wantPeak: 2, wantDistinct: 2, wantReclaims: 1, wantFences: 1, wantAttempts: 1},
	// 这一格故意在租约之内再跑一轮：证明"长租约"本身就是一道门，
	// 而不是靠回收/冷却拦住第二次认领。
	{name: "仅长租约 3s", lease: 3 * time.Second, wantDup: 0,
		wantPeak: 1, wantDistinct: 1, wantReclaims: 0, wantFences: 0, wantAttempts: 1,
		secondAt: 1500 * time.Millisecond},
	{name: "仅 A 预算 120ms", lease: 1500 * time.Millisecond, budget: 120 * time.Millisecond, wantDup: 0,
		wantPeak: 1, wantDistinct: 1, wantReclaims: 0, wantFences: 0, wantAttempts: 1},
	// 预算超过租约：运行面把它封顶到“租约终点-结算余量”，所以不会出现
	// 基线里的重复提交；这正是新护栏要证明的行为。
	{name: "A 预算 3s 超过租约（被封顶）", lease: 1500 * time.Millisecond, budget: 3 * time.Second, wantDup: 0,
		wantPeak: 1, wantDistinct: 1, wantReclaims: 0, wantFences: 0, wantAttempts: 1},
	// 冷却挡住的是"立刻重领"，不是"重复提交本身"：第二幕等冷却过去，
	// 同一行会被重新认领、再提交一次（wantLateSigns=2）。
	{name: "仅 B 冷却 5s", lease: 1500 * time.Millisecond, grace: 5 * time.Second, wantDup: 0,
		wantPeak: 1, wantDistinct: 1, wantReclaims: 1, wantFences: 1, wantAttempts: 0,
		wantLateSigns: 2},
	{name: "A+B", lease: 1500 * time.Millisecond, grace: 5 * time.Second, budget: 120 * time.Millisecond, wantDup: 0,
		wantPeak: 1, wantDistinct: 1, wantReclaims: 0, wantFences: 0, wantAttempts: 1},
	{name: "仅 A，提供方吞取消", lease: 1500 * time.Millisecond, budget: 120 * time.Millisecond, ignoreCtx: true, wantDup: 1,
		wantPeak: 2, wantDistinct: 2, wantReclaims: 1, wantFences: 1, wantAttempts: 1},
}

func TestLeaseDoubleRunMatrix(t *testing.T) {
	base := strings.TrimSpace(os.Getenv(authDSNEnv))
	if base == "" {
		t.Skip("SKIP: " + authDSNEnv + " is not set. Lease double-run experiment did NOT run.")
	}
	var obs []labObservation
	for _, c := range labMatrix {
		c := c
		t.Run(c.name, func(t *testing.T) {
			o := runLeaseLab(t, base, c)
			// 每一格把矩阵表的数字都钉住：wantDup 之外若不断言，
			// 文档里的"峰值并发 2 / 回收 1 / 栅栏拒写 1"就没有守护。
			for _, want := range []struct {
				name string
				got  int
				want int
			}{
				{"dup", o.dup, c.wantDup},
				{"signs", o.signs, c.wantDup + 1},
				{"peak", o.peak, c.wantPeak},
				{"distinct attemptID", o.distinct, c.wantDistinct},
				{"reclaims", o.reclaims, c.wantReclaims},
				{"fence rejections", o.fences, c.wantFences},
				{"attempts", o.attempts, c.wantAttempts},
				{"late signs", o.lateSigns, c.wantLateSigns},
			} {
				if want.want < 0 {
					continue
				}
				if want.got != want.want {
					t.Fatalf("%s = %d, want %d（Sign 共 %d 次, attempt ids %d 个, 峰值并发 %d）",
						want.name, want.got, want.want, o.signs, o.distinct, o.peak)
				}
			}
			obs = append(obs, o)
		})
	}
	// 一行一格的量化表：报告直接取这段输出。
	t.Logf("\n%-24s %6s %5s %5s %8s %8s %7s %9s %6s %10s", "cell", "signs", "dup", "peak", "distinct", "reclaim", "fence", "attempts", "late", "elapsed")
	for _, o := range obs {
		t.Logf("%-24s %6d %5d %5d %8d %8d %7d %9d %6d %10s",
			o.name, o.signs, o.dup, o.peak, o.distinct, o.reclaims, o.fences, o.attempts, o.lateSigns, o.elapsed.Round(time.Millisecond))
	}
}

// runLeaseLab 在独立 schema 里跑一格：两个执行体、同一个账号、同一行计划。
func runLeaseLab(t *testing.T, base string, c labCell) labObservation {
	t.Helper()
	ctx := context.Background()
	schema := "leaselab_" + randomHex(8)

	admin, err := pg.Open(ctx, pg.Options{DSN: base, MaxConns: 1, StatementTimeout: 30 * time.Second})
	if err != nil {
		t.Fatalf("open admin: %v", err)
	}
	if _, err := admin.Exec(ctx, "CREATE SCHEMA "+schema); err != nil {
		admin.Close()
		t.Fatalf("create schema: %v", err)
	}
	dsn := withSearchPath(base, schema)
	db, err := pg.Open(ctx, pg.Options{DSN: dsn, MaxConns: 8, StatementTimeout: 10 * time.Second})
	if err != nil {
		admin.Close()
		t.Fatalf("open schema db: %v", err)
	}
	t.Cleanup(func() {
		db.Close()
		_, _ = admin.Exec(context.Background(), "DROP SCHEMA IF EXISTS "+schema+" CASCADE")
		admin.Close()
	})
	if _, err := pg.Migrate(ctx, dsn); err != nil {
		t.Fatalf("migrate: %v", err)
	}

	// 种子：用户→学校→账号（审核激活）→出口→启用任务→当日窗口→一行计划。
	users := pg.NewUserRepo(db)
	var owner, reviewer coreuser.Record
	err = db.InTx(ctx, func(ctx context.Context) error {
		var txErr error
		if owner, txErr = users.Create(ctx, coreuser.New{
			Email: "lab-owner@example.com", PasswordHash: "argon2id$test", Role: coreauth.RoleUser,
		}, 10000); txErr != nil {
			return txErr
		}
		reviewer, txErr = users.Create(ctx, coreuser.New{
			Email: "lab-reviewer@example.com", PasswordHash: "argon2id$test", Role: coreauth.RoleMaster,
		}, 10000)
		return txErr
	})
	if err != nil {
		t.Fatalf("create users: %v", err)
	}
	school, err := pg.NewSchoolRepo(db).Create(ctx, coreschool.New{Code: "labschool", Name: "Lab School"})
	if err != nil {
		t.Fatalf("create school: %v", err)
	}
	accts := pg.NewAccountRepo(db)
	var acct coreaccount.Record
	err = db.InTx(ctx, func(ctx context.Context) error {
		var txErr error
		acct, txErr = accts.Create(ctx, coreaccount.New{
			SchoolID: school.ID, OwnerUserID: owner.ID, Phone: "13800000001",
		}, 10000)
		return txErr
	})
	if err != nil {
		t.Fatalf("create account: %v", err)
	}
	if _, err := accts.Review(ctx, corescope.Master(), acct.ID, true, "", reviewer.ID, (clock.System{}).Now()); err != nil {
		t.Fatalf("activate account: %v", err)
	}
	if _, err := db.Exec(ctx, "INSERT INTO egresses (identity) VALUES ($1)", "https://egress-leaselab"); err != nil {
		t.Fatalf("seed egress: %v", err)
	}
	sched := pg.NewScheduleRepo(db)
	schoolTaskID, err := sched.CreateSchoolTask(ctx, school.ID, "晨签")
	if err != nil {
		t.Fatalf("create school task: %v", err)
	}
	now := (clock.System{}).Now()
	day := clock.BusinessDay(now, time.UTC)
	if _, err := sched.PutWindow(ctx, coreschedule.WindowRecord{
		SchoolTaskID: schoolTaskID, Day: day,
		Start: now.Add(-10 * time.Minute), End: now.Add(10 * time.Minute),
		Source: "platform",
	}, 0); err != nil {
		t.Fatalf("put window: %v", err)
	}
	if _, err := sched.InsertPlan(ctx, []coreschedule.PlanRow{{
		AccountID: acct.ID, SchoolTaskID: schoolTaskID, Day: day, RunAt: now.Add(-time.Minute),
	}}); err != nil {
		t.Fatalf("insert plan: %v", err)
	}

	audits, err := pg.NewAuditRepo(db, []byte("leaselab-audit-key-0123456789"))
	if err != nil {
		t.Fatalf("audit repo: %v", err)
	}
	// 两个执行体各有自己的仓储实例：WithLease/WithReclaimGrace 是实例级配置，
	// 生产里就是两个进程各自的装配结果。
	newRepo := func() *pg.ScheduleRepo {
		return pg.NewScheduleRepoWithAudit(db, audits).WithLease(c.lease).WithReclaimGrace(c.grace)
	}
	egressSvc, err := appegress.NewService(appegress.Deps{
		Store: pg.NewEgressRepo(db), Clock: clock.System{},
	})
	if err != nil {
		t.Fatalf("egress service: %v", err)
	}
	// Sign 的占位时长由租约派生：第二个执行体在 lease+100ms 认领，
	// 第一个执行体在 lease+800ms 才拿到结果。两者之间留 700ms 余量，
	// 让"W2 的 Step 被调度推迟"不会退化成"W1 先结算、于是没有重复提交"。
	hold := c.lease + 800*time.Millisecond
	signer := newLabSigner(hold, c.ignoreCtx)
	newWorker := func() *appworker.Service {
		svc, err := appworker.NewService(appworker.Deps{
			Store: newRepo(), Creds: labCreds{}, Signer: signer,
			Egress:  egressSvc,
			Limiter: corerate.NewLimiter(corerate.DefaultRate, 6),
			Clock:   clock.System{}, Location: time.UTC,
			Paused:        func(context.Context) (bool, error) { return false, nil },
			AttemptBudget: c.budget,
		})
		if err != nil {
			t.Fatalf("worker service: %v", err)
		}
		return svc
	}

	type stepResult struct {
		processed int
		err       error
	}
	// 时刻从 clock 端口取：判据仍是真实时间上的并发（System 即真时钟），
	// 但 canonical 的 clock.now 门要求测试不直呼 time.Now。
	clk := clock.System{}
	start := clk.Now()
	w1 := newWorker()
	w2 := newWorker()
	ch1 := make(chan stepResult, 1)
	go func() {
		p, err := w1.Step(ctx, 10)
		ch1 <- stepResult{p, err}
	}()

	// 等第一个执行体真正进入提交（不是认领完成——认领后还有窗口/出口/凭据）。
	select {
	case <-signer.entered:
	case <-time.After(5 * time.Second):
		t.Fatal("执行体 1 未进入提交")
	}
	// 让第二个执行体跑一轮：缺省在租约过期之后（lease+100ms），
	// 这正是生产里"下一个 tick"相对"上一次认领"的时序；
	// secondAt 非零的格子自己指定（证明租约内无人能领）。
	secondAt := c.secondAt
	if secondAt <= 0 {
		secondAt = c.lease + 100*time.Millisecond
	}
	if wait := secondAt - time.Since(start); wait > 0 {
		time.Sleep(wait)
	}
	p2, err2 := w2.Step(ctx, 10)
	var r1 stepResult
	select {
	case r1 = <-ch1:
	case <-time.After(10 * time.Second):
		t.Fatal("执行体 1 未返回")
	}
	elapsed := time.Since(start)

	fences := 0
	for _, e := range []error{r1.err, err2} {
		if e != nil && coreerrs.From(e).Code == coreerrs.CodeConflict {
			fences++
		}
	}
	// 结算都已返回：等行落到终态（或退避后的 pending）。
	var state, resultWord string
	deadline := start.Add(5 * time.Second)
	for {
		state, resultWord, _ = labRow(ctx, db, acct.ID)
		if state != string(corestatus.TaskRunning) || clk.Now().After(deadline) {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	reclaims := queryInt(t, db, "SELECT count(*) FROM audit_logs WHERE action = 'task.reclaim'")
	attempts := queryInt(t, db, "SELECT attempts FROM sign_tasks WHERE account_id = $1", acct.ID)

	signer.mu.Lock()
	signs, peak := signer.calls, signer.peak
	distinct := len(signer.attemptIDs)
	signer.mu.Unlock()

	// 第二幕（只有显式声明 wantLateSigns 的格子跑）：冷却一过，同一行会被
	// 重新认领并**再提交一次**。这是"B 单独用不是修复"的直接证据——
	// 冷却只是把并发重复推迟成串行重复。
	lateSigns, lateAttempts, lateState := 0, -1, ""
	if c.wantLateSigns > 0 {
		lateDeadline := start.Add(secondAt + c.grace + 10*time.Second)
		for clk.Now().Before(lateDeadline) {
			if _, err := w2.Step(ctx, 10); err != nil {
				t.Fatalf("第二幕 Step: %v", err)
			}
			signer.mu.Lock()
			n := signer.calls
			signer.mu.Unlock()
			if n >= c.wantLateSigns {
				break
			}
			time.Sleep(100 * time.Millisecond)
		}
		settleBy := clk.Now().Add(10 * time.Second)
		for {
			lateState, _, _ = labRow(ctx, db, acct.ID)
			if lateState != string(corestatus.TaskRunning) || clk.Now().After(settleBy) {
				break
			}
			time.Sleep(20 * time.Millisecond)
		}
		signer.mu.Lock()
		lateSigns = signer.calls
		signer.mu.Unlock()
		lateAttempts = queryInt(t, db, "SELECT attempts FROM sign_tasks WHERE account_id = $1", acct.ID)
	}

	o := labObservation{
		name: c.name, signs: signs, dup: signs - 1, peak: peak, distinct: distinct,
		reclaims: reclaims, fences: fences, attempts: attempts,
		resultWord: resultWord, state: state, elapsed: elapsed,
		lateSigns: lateSigns, lateAttempts: lateAttempts, lateState: lateState,
	}
	t.Logf("cell=%s signs=%d dup=%d peak=%d distinct=%d reclaims=%d fences=%d attempts=%d state=%s result=%s late=%d lateAttempts=%d lateState=%s err1=%v err2=%v",
		o.name, o.signs, o.dup, o.peak, o.distinct, o.reclaims, o.fences, o.attempts, o.state, o.resultWord,
		o.lateSigns, o.lateAttempts, o.lateState, trimErr(r1.err), trimErr(err2))
	if p2 == 0 && err2 == nil {
		// 第二轮的"没活干"是两种不同的事：认领 0 行（被 A/B 挡住）
		// 还是认领了 1 行又结算掉了。前者才是我们要的证据。
		t.Logf("执行体 2 处理 0 行（认领被判据挡住）")
	}
	return o
}

// labRow 读回行状态与结论词。
func labRow(ctx context.Context, db *pg.DB, accountID string) (state, result string, err error) {
	state, err = db.QueryString(ctx,
		"SELECT state::text FROM sign_tasks WHERE account_id = $1", accountID)
	if err != nil {
		return "", "", err
	}
	result, err = db.QueryString(ctx,
		"SELECT coalesce(result, '') FROM sign_tasks WHERE account_id = $1", accountID)
	return state, result, err
}

func queryInt(t *testing.T, db *pg.DB, sql string, args ...any) int {
	t.Helper()
	n, err := db.QueryInt64(context.Background(), sql, args...)
	if err != nil {
		t.Fatalf("query int %q: %v", sql, err)
	}
	return int(n)
}

func trimErr(err error) string {
	if err == nil {
		return "-"
	}
	if errors.Is(err, context.Canceled) {
		return "canceled"
	}
	return err.Error()
}
