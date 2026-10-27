package main

// bench 核心逻辑：造数、四项测量、可机读输出。
// 测量口径见 main.go 的包注释；本文件只放实现。

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"net/url"
	"os"
	"runtime"
	"strconv"
	"strings"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	appevents "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/events"
	appschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/schedule"
	coreaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/account"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coreschool "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/school"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// memoryLimitBytes 是内存峰值判据（P8 派单 §3）。
const memoryLimitBytes = 1 << 30

// Config 是一次压测的输入。
type Config struct {
	DSN        string
	Accounts   int
	Schools    int
	ClaimBatch int
	KeepSchema bool
}

// Result 是一轮压测的全部原始测量。
// 派生量（吞吐）由 Result 的方法给出，避免两处算不一致。
type Result struct {
	Accounts int
	Schools  int
	Shape    string // single | multi

	SeedRows    int
	SeedSeconds float64

	ClaimRows    int
	ClaimSeconds float64
	ClaimBatch   int

	BuildCount   int
	BuildSeconds float64

	SSESubscribers   int
	SSESubSeconds    float64
	SSEFanoutSeconds float64

	HeapPeakBytes uint64
	SysBytes      uint64

	CPUModel string
	Cores    int
	GoVer    string
	GOOS     string

	// DSNRef 是去凭据后的库地址（scheme://host/db），随结果走，不用全局量。
	DSNRef string
}

// ClaimRowsPerSec 返回认领吞吐（行/秒）。
func (r Result) ClaimRowsPerSec() float64 {
	if r.ClaimSeconds <= 0 {
		return 0
	}
	return float64(r.ClaimRows) / r.ClaimSeconds
}

// SeedRowsPerSec 返回造数吞吐（行/秒）。
func (r Result) SeedRowsPerSec() float64 {
	if r.SeedSeconds <= 0 {
		return 0
	}
	return float64(r.SeedRows) / r.SeedSeconds
}

// BuildSecondsPerBuild 返回单次计划构建耗时（秒）。
func (r Result) BuildSecondsPerBuild() float64 {
	if r.BuildCount <= 0 {
		return 0
	}
	return r.BuildSeconds / float64(r.BuildCount)
}

// benchPhone 按登记块拼合成手机号（运行期拼号，源代码无字面量）。
// 块＝"138"+8 位零填充序号，已登记 tests/synthetic-fixtures.json。
func benchPhone(i int) string { return "138" + fmt.Sprintf("%08d", i) }

// benchEmail 拼合成邮箱。example.com 是保留域。
func benchEmail(i int) string { return fmt.Sprintf("bench-%07d@example.com", i) }

// fixedWindow 是 Discoverer 的固定窗口替身。
// 测的是构建与落库路径；平台发现不在测量面。
type fixedWindow struct{ start, end time.Time }

func (f fixedWindow) DiscoverWindow(context.Context, string) (contract.Window, error) {
	return contract.Window{
		Start:  f.start,
		End:    f.end,
		Source: contract.WindowPlatform,
		Raw:    map[string]any{"bench": "fixed-window-stub"},
	}, nil
}

// Run 跑一轮完整压测：建 schema→迁移→造数→构建→认领→扇出→采样。
func Run(ctx context.Context, cfg Config) (Result, error) {
	if cfg.Accounts < 1 {
		return Result{}, fmt.Errorf("bench: accounts must be >= 1, got %d", cfg.Accounts)
	}
	if cfg.Schools < 1 {
		return Result{}, fmt.Errorf("bench: schools must be >= 1, got %d", cfg.Schools)
	}
	if cfg.ClaimBatch < 1 {
		cfg.ClaimBatch = 500
	}

	res := Result{
		Accounts: cfg.Accounts,
		Schools:  cfg.Schools,
		Shape:    "multi",
		CPUModel: cpuModel(),
		Cores:    runtime.NumCPU(),
		GoVer:    runtime.Version(),
		GOOS:     runtime.GOOS + "/" + runtime.GOARCH,
		DSNRef:   sanitizeDSN(cfg.DSN),
	}
	if cfg.Schools == 1 {
		res.Shape = "single"
	}

	// 独占临时 schema，与 pg e2e 的 testEnv 同形。
	schema := "bench_" + randomHex(8)
	admin, err := pg.Open(ctx, pg.Options{DSN: cfg.DSN, MaxConns: 1, StatementTimeout: 30 * time.Second})
	if err != nil {
		return res, fmt.Errorf("bench: open admin connection: %w", err)
	}
	if _, err := admin.Exec(ctx, "CREATE SCHEMA "+schema); err != nil {
		admin.Close()
		return res, fmt.Errorf("bench: create schema %s: %w", schema, err)
	}
	dropCtx, dropCancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer dropCancel()
	defer func() {
		if !cfg.KeepSchema {
			if _, err := admin.Exec(dropCtx, "DROP SCHEMA IF EXISTS "+schema+" CASCADE"); err != nil {
				// 清理失败只报一行 stderr，退出语义不变：压测结果已产出。
				fmt.Fprintf(os.Stderr, "bench: drop schema %s: %v\n", schema, err)
			}
		}
		admin.Close()
	}()

	dsn := withSchema(cfg.DSN, schema)
	if _, err := pg.Migrate(ctx, dsn); err != nil {
		return res, fmt.Errorf("bench: migrate schema %s: %w", schema, err)
	}
	db, err := pg.Open(ctx, pg.Options{DSN: dsn, MaxConns: 8, StatementTimeout: 120 * time.Second})
	if err != nil {
		return res, fmt.Errorf("bench: open bench connection: %w", err)
	}
	defer db.Close()

	users := pg.NewUserRepo(db)
	schools := pg.NewSchoolRepo(db)
	accts := pg.NewAccountRepo(db)
	sched := pg.NewScheduleRepo(db)

	schoolIDs, taskIDs, err := seedSchools(ctx, schools, sched, cfg.Schools)
	if err != nil {
		return res, err
	}

	memSample := func() {
		var ms runtime.MemStats
		runtime.ReadMemStats(&ms)
		if ms.HeapAlloc > res.HeapPeakBytes {
			res.HeapPeakBytes = ms.HeapAlloc
		}
		res.SysBytes = ms.Sys
	}

	// 造数：用户＋账号按批同事务；批后统一激活。
	start := coreclock.System{}.Now()
	if err := seedAccounts(ctx, db, users, accts, schoolIDs, cfg.Accounts); err != nil {
		return res, err
	}
	if _, err := db.Exec(ctx, "UPDATE accounts SET status = 'active' WHERE deleted_at IS NULL"); err != nil {
		return res, fmt.Errorf("bench: activate accounts: %w", err)
	}
	res.SeedSeconds = time.Since(start).Seconds()
	res.SeedRows = cfg.Accounts
	memSample()

	// 计划构建：窗口用固定替身，业务日取 UTC 当天。
	day := coreclock.System{}.Now().UTC().Format("2006-01-02")
	dayStart, err := time.ParseInLocation("2006-01-02", day, time.UTC)
	if err != nil {
		return res, fmt.Errorf("bench: parse day: %w", err)
	}
	winStart := dayStart.Add(30 * time.Minute)
	winEnd := winStart.Add(80 * time.Minute) // 80 分钟窗口是设计不变量

	svc, err := appschedule.NewService(appschedule.Deps{
		Windows:  sched,
		Plans:    sched,
		Accounts: sched,
		Discover: fixedWindow{start: winStart, end: winEnd},
		Tx:       db,
	})
	if err != nil {
		return res, fmt.Errorf("bench: build schedule service: %w", err)
	}

	start = coreclock.System{}.Now()
	for k, taskID := range taskIDs {
		_, err := svc.Build(ctx, appschedule.Input{
			SchoolID: schoolIDs[k], SchoolTaskID: taskID, Day: day, Location: time.UTC,
			Workers: 8, Avg: 500 * time.Millisecond, Rate: 50, Util: 0.8,
			SlotsPerSlice: 100, Front: true, UsePrefs: false,
		})
		if err != nil {
			return res, fmt.Errorf("bench: build plan for task %s: %w", taskID, err)
		}
	}
	res.BuildSeconds = time.Since(start).Seconds()
	res.BuildCount = len(taskIDs)
	memSample()

	// 认领吞吐：now 取窗口结束之后，全部计划行一次性到期。
	start = coreclock.System{}.Now()
	claimed := 0
	claimNow := winEnd.Add(time.Hour)
	for {
		rows, err := sched.ClaimDue(ctx, claimNow, cfg.ClaimBatch)
		if err != nil {
			return res, fmt.Errorf("bench: claim due: %w", err)
		}
		claimed += len(rows)
		if len(rows) < cfg.ClaimBatch {
			break
		}
	}
	res.ClaimSeconds = time.Since(start).Seconds()
	res.ClaimRows = claimed
	res.ClaimBatch = cfg.ClaimBatch
	memSample()

	// SSE 扇出：不依赖库，只测进程内总线。
	subSec, fanoutSec, sseSubs, err := runSSE(1000)
	if err != nil {
		return res, err
	}
	res.SSESubscribers = sseSubs
	res.SSESubSeconds = subSec
	res.SSEFanoutSeconds = fanoutSec

	memSample()
	return res, nil
}

// seedSchools 建校与每校一个启用任务，返回 ID 列表。
func seedSchools(ctx context.Context, schools *pg.SchoolRepo, sched *pg.ScheduleRepo, n int) ([]string, []string, error) {
	schoolIDs := make([]string, 0, n)
	taskIDs := make([]string, 0, n)
	for k := 0; k < n; k++ {
		code := fmt.Sprintf("bench-school-%02d", k+1)
		rec, err := schools.Create(ctx, coreschool.New{Code: code, Name: "Bench School " + fmt.Sprintf("%02d", k+1)})
		if err != nil {
			return nil, nil, fmt.Errorf("bench: create school %s: %w", code, err)
		}
		taskID, err := sched.CreateSchoolTask(ctx, rec.ID, "bench-task")
		if err != nil {
			return nil, nil, fmt.Errorf("bench: create school task for %s: %w", code, err)
		}
		schoolIDs = append(schoolIDs, rec.ID)
		taskIDs = append(taskIDs, taskID)
	}
	return schoolIDs, taskIDs, nil
}

// seedAccounts 造用户＋账号，按批同事务提交。
// 容量上限传 accounts+100：容量检查按存量计数，须大于造数总量。
func seedAccounts(ctx context.Context, db *pg.DB, users *pg.UserRepo, accts *pg.AccountRepo, schoolIDs []string, n int) error {
	const batch = 250
	limit := n + 100
	for base := 0; base < n; base += batch {
		end := base + batch
		if end > n {
			end = n
		}
		err := db.InTx(ctx, func(ctx context.Context) error {
			for i := base; i < end; i++ {
				u, err := users.Create(ctx, coreuser.New{
					Email:        benchEmail(i),
					PasswordHash: "argon2id$bench",
					Role:         coreauth.RoleUser,
				}, limit)
				if err != nil {
					return fmt.Errorf("create user %d: %w", i, err)
				}
				if _, err := accts.Create(ctx, coreaccount.New{
					SchoolID:    schoolIDs[i%len(schoolIDs)],
					OwnerUserID: u.ID,
					Phone:       benchPhone(i),
				}, limit); err != nil {
					return fmt.Errorf("create account %d: %w", i, err)
				}
			}
			return nil
		})
		if err != nil {
			return fmt.Errorf("bench: seed batch [%d,%d): %w", base, end, err)
		}
	}
	return nil
}

// runSSE 测订阅与扇出：订阅 N 端，发布一条，等全部订阅端收到。
func runSSE(n int) (subSec, fanoutSec float64, subscribers int, err error) {
	b := appevents.NewBroker()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	cancels := make([]context.CancelFunc, 0, n)
	subs := make([]appevents.Subscription, 0, n)
	start := coreclock.System{}.Now()
	for i := 0; i < n; i++ {
		s, c := b.Subscribe(ctx, nil, 0)
		subs = append(subs, s)
		cancels = append(cancels, c)
	}
	subSec = time.Since(start).Seconds()

	start = coreclock.System{}.Now()
	b.Publish(nil, appevents.Event{
		Type: appevents.TypeTaskProgress, SchoolID: "bench-school-01", Day: "2000-01-01", At: coreclock.System{}.Now(),
	})
	for i := range subs {
		select {
		case <-subs[i].C:
		case <-time.After(10 * time.Second):
			return subSec, 0, n, fmt.Errorf("bench: sse subscriber %d timed out", i)
		}
	}
	fanoutSec = time.Since(start).Seconds()
	for _, c := range cancels {
		c()
	}
	return subSec, fanoutSec, n, nil
}

// Format 产出可机读输出：一行一个指标，键值对带口径字段。
func Format(r Result) []string {
	return []string{
		fmt.Sprintf("bench_metric=seed accounts=%d schools=%d shape=%s rows=%d seconds=%s rows_per_sec=%s",
			r.Accounts, r.Schools, r.Shape, r.SeedRows, sec(r.SeedSeconds), num(r.SeedRowsPerSec())),
		fmt.Sprintf("bench_metric=claim_due accounts=%d schools=%d shape=%s rows=%d workers=1 batch=%d seconds=%s rows_per_sec=%s",
			r.Accounts, r.Schools, r.Shape, r.ClaimRows, r.ClaimBatch, sec(r.ClaimSeconds), num(r.ClaimRowsPerSec())),
		fmt.Sprintf("bench_metric=plan_build accounts=%d schools=%d shape=%s builds=%d seconds=%s seconds_per_build=%s",
			r.Accounts, r.Schools, r.Shape, r.BuildCount, sec(r.BuildSeconds), num(r.BuildSecondsPerBuild())),
		fmt.Sprintf("bench_metric=sse_subscribe accounts=%d schools=%d shape=%s subscribers=%d seconds=%s ops_per_sec=%s",
			r.Accounts, r.Schools, r.Shape, r.SSESubscribers, sec(r.SSESubSeconds), num(rate(r.SSESubscribers, r.SSESubSeconds))),
		fmt.Sprintf("bench_metric=sse_fanout accounts=%d schools=%d shape=%s subscribers=%d events=1 seconds=%s",
			r.Accounts, r.Schools, r.Shape, r.SSESubscribers, sec6(r.SSEFanoutSeconds)),
		fmt.Sprintf("bench_metric=memory accounts=%d schools=%d shape=%s heap_peak_bytes=%d sys_bytes=%d limit_bytes=%d",
			r.Accounts, r.Schools, r.Shape, r.HeapPeakBytes, r.SysBytes, uint64(memoryLimitBytes)),
		fmt.Sprintf("bench_meta=run cpu=%q cores=%d go=%q os=%q accounts=%d schools=%d shape=%s dsn=%q",
			r.CPUModel, r.Cores, r.GoVer, r.GOOS, r.Accounts, r.Schools, r.Shape, r.DSNRef),
	}
}

func sec(v float64) string { return strconv.FormatFloat(v, 'f', 3, 64) }

// sec6 用于亚毫秒级测量：3 位小数会吞成 0.000。
func sec6(v float64) string { return strconv.FormatFloat(v, 'f', 6, 64) }
func num(v float64) string { return strconv.FormatFloat(v, 'f', 1, 64) }
func rate(n int, s float64) float64 {
	if s <= 0 {
		return 0
	}
	return float64(n) / s
}

// cpuModel 读 CPU 型号作机器口径。空格换下划线：保住 key=value 单令牌形状。
// 非 Linux 取不到时如实报 unknown。
func cpuModel() string {
	b, err := os.ReadFile("/proc/cpuinfo")
	if err != nil {
		return "unknown"
	}
	for _, line := range strings.Split(string(b), "\n") {
		if name, ok := strings.CutPrefix(line, "model name"); ok {
			if _, v, found := strings.Cut(name, ":"); found {
				return strings.ReplaceAll(strings.TrimSpace(v), " ", "_")
			}
		}
	}
	return "unknown"
}

// sanitizeDSN 去掉用户信息：口令不得进输出。
func sanitizeDSN(dsn string) string {
	u, err := url.Parse(dsn)
	if err != nil {
		return "unparsed"
	}
	return u.Scheme + "://" + u.Host + u.Path
}

// randomHex 读随机字节作 schema 名。
func randomHex(n int) string {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		panic("bench: cannot read random bytes: " + err.Error())
	}
	return hex.EncodeToString(b)
}

// withSchema 给 DSN 追加 search_path；URL 形与空格形都支持。
// schema 名由 randomHex 生成（bench_<hex>），非外部输入，拼接无注入面。
func withSchema(dsn, schema string) string {
	if strings.Contains(dsn, "://") {
		sep := "?"
		if strings.Contains(dsn, "?") {
			sep = "&"
		}
		return dsn + sep + "search_path=" + schema
	}
	return dsn + " search_path=" + schema
}
