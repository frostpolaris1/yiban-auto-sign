package main

// UX-5 执行监控 e2e：GET /api/admin/engine 单进程快照。
//
// # 功能
// 覆盖 aqn 验收的后端面：在飞 0 与非 0、租约字段、due_now 计数、
// 当日 success/failed 计数（按账号终态去重）、待重试行
// （pending 且 attempts>0）、admin 只见绑定校、空绑定全 0、
// 手机号只出掩码、进程内时刻未启动为 null、user 无可见面（403）、
// 未登录 401。
// 走真实栈：真库、真登录、真 cookie、真 HTTP。
//
// # 归属
// cmd/server 是唯一可同时引用 adapters、app、entry 的位置。
// 环境复用 auth_e2e_test.go 的 newAuthEnvWith；造数复用 ux3 的种子助手。
//
// # 口径
// 缺 DSN 时整包跳过（包 TestMain 横幅），跳过不是绿。
// 每个测试独占一个 schema，互不依赖。
// 手机号只取登记的 138 合成段（tests/synthetic-fixtures.json）。
// 邮箱一律用保留域 example.com。状态词一律用 corestatus 常量。
// 本文件不读本地墙钟：当前时刻与业务日都从服务端
// /api/me/diagnostics 的 updated_at 取（canonical 的 clock.now 判据）。

import (
	"context"
	"encoding/json"
	"net/http"
	"net/url"
	"strings"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	appworker "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/worker"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
)

// u5Inflight 是契约 EngineInflightRow 的形状（仅取断言用到的字段）。
type u5Inflight struct {
	AccountID          string    `json:"account_id"`
	AccountPhoneMasked string    `json:"account_phone_masked"`
	SchoolID           string    `json:"school_id"`
	SchoolName         string    `json:"school_name"`
	ClaimedAt          time.Time `json:"claimed_at"`
	LeaseExpiresAt     time.Time `json:"lease_expires_at"`
}

// u5Retry 是契约 EngineRetryRow 的形状。
type u5Retry struct {
	TaskID             string    `json:"task_id"`
	AccountID          string    `json:"account_id"`
	AccountPhoneMasked string    `json:"account_phone_masked"`
	SchoolID           string    `json:"school_id"`
	SchoolName         string    `json:"school_name"`
	TaskName           string    `json:"task_name"`
	Attempts           int       `json:"attempts"`
	RunAt              time.Time `json:"run_at"`
	Result             string    `json:"result"`
}

// u5Engine 是契约 EngineSnapshot 的形状。
type u5Engine struct {
	Inflight          []u5Inflight `json:"inflight"`
	DueNow            int          `json:"due_now"`
	TodaySuccess      int          `json:"today_success"`
	TodayFailed       int          `json:"today_failed"`
	Retrying          []u5Retry    `json:"retrying"`
	RetryingTruncated bool         `json:"retrying_truncated"`
	LastClaimAt       *time.Time   `json:"last_claim_at"`
	LastTickAt        *time.Time   `json:"last_tick_at"`
	WorkerStartedAt   *time.Time   `json:"worker_started_at"`
}

// u5NowFromServer 向服务端要当前时刻（/api/me/diagnostics 的 updated_at）。
// 业务日与 run_at 的基准都用它，不在测试里读本地墙钟。
func u5NowFromServer(t *testing.T, e *authEnv, client *http.Client) time.Time {
	t.Helper()
	resp, payload := u3Get(e, client, "/api/me/diagnostics")
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("server time probe status = %d, want 200", resp.StatusCode)
	}
	var body struct {
		UpdatedAt time.Time `json:"updated_at"`
	}
	if err := json.Unmarshal(payload, &body); err != nil {
		t.Fatalf("decode diagnostics: %v", err)
	}
	// 库内 timestamptz 只有微秒精度：种子与断言都按微秒取整，
	// 不让纳秒尾巴造成假差异。
	return body.UpdatedAt.Truncate(time.Microsecond)
}

// u5EngineGet 请求执行监控快照并解码。
func u5EngineGet(e *authEnv, client *http.Client) (*http.Response, u5Engine, []byte) {
	e.t.Helper()
	resp, payload := u3Get(e, client, "/api/admin/engine")
	var snap u5Engine
	if resp.StatusCode == http.StatusOK {
		if err := json.Unmarshal(payload, &snap); err != nil {
			e.t.Fatalf("decode engine snapshot: %v; body=%q", err, payload)
		}
	}
	return resp, snap, payload
}

// u5SeedRunning 铺一行 running（认领时刻 now-1min，租约 2 分钟）并返回行 ID。
func u5SeedRunning(t *testing.T, e *authEnv, taskID, accountID, day string, now time.Time) string {
	t.Helper()
	claimed := now.Add(-time.Minute)
	rowID := u3SeedSignTask(t, e, taskID, accountID, day, now.Add(-time.Hour),
		status.TaskRunning, 1, "", &claimed)
	if _, err := e.db.Exec(e.ctx,
		`UPDATE sign_tasks SET lease_expires_at = claimed_at + interval '2 minutes' WHERE id = $1::uuid`,
		rowID); err != nil {
		t.Fatalf("seed lease: %v", err)
	}
	return rowID
}

// u5SeedTerminalTask 直接铺一行带指定 updated_at 的任务行：
// 同账号同日多任务时用它钉住「最后更新」的次序
// （u3SeedSignTask 的改写路径会刷新 updated_at，钉不住先后）。
// day 显式给出，与 updated_at 无关：行挂在哪个业务日只看 day 列。
func u5SeedTerminalTask(t *testing.T, e *authEnv, taskID, accountID, day string, at time.Time, state status.TaskState) {
	t.Helper()
	if _, err := e.db.Exec(e.ctx, `
		INSERT INTO sign_tasks (account_id, school_task_id, day, run_at, state, updated_at)
		VALUES ($1::uuid, $2::uuid, $3::date, $4, $5::sign_task_state, $4)`,
		accountID, taskID, day, at, string(state)); err != nil {
		t.Fatalf("seed terminal task at %v: %v", at, err)
	}
}

// --- 用例 ----------------------------------------------------------------

// user 没有执行监控可见面：403。未登录：401。
func TestU5EngineRequiresAdminRole(t *testing.T) {
	e := u3NewEnv(t)
	_, userClient := u3LoginSeed(t, e, "u5-user@example.com", "user-pass", coreauth.RoleUser)
	resp, _, _ := u5EngineGet(e, userClient)
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("user status = %d, want 403", resp.StatusCode)
	}
	resp, _, _ = u5EngineGet(e, u1FreshClient(t))
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("anonymous status = %d, want 401", resp.StatusCode)
	}
}

// 空库快照：四个计数为 0、两列表为空、进程时刻为 null（循环未启动）。
func TestU5EngineEmptySnapshot(t *testing.T) {
	e := u3NewEnv(t)
	_, client := u3LoginSeed(t, e, "u5-master@example.com", "master-pass", coreauth.RoleMaster)
	resp, snap, _ := u5EngineGet(e, client)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}
	if len(snap.Inflight) != 0 {
		t.Fatalf("inflight = %d rows, want 0", len(snap.Inflight))
	}
	if len(snap.Retrying) != 0 {
		t.Fatalf("retrying = %d rows, want 0", len(snap.Retrying))
	}
	if snap.DueNow != 0 || snap.TodaySuccess != 0 || snap.TodayFailed != 0 {
		t.Fatalf("counts = due %d success %d failed %d, want all 0",
			snap.DueNow, snap.TodaySuccess, snap.TodayFailed)
	}
	if snap.WorkerStartedAt != nil || snap.LastTickAt != nil || snap.LastClaimAt != nil {
		t.Fatalf("process times must be null before the loop starts: %+v", snap)
	}
}

// 在飞行完整：掩码号、学校名、认领时刻与租约到期（认领 + 2 分钟）；
// due_now 只数已到期 pending；响应体不含完整手机号。
func TestU5EngineInflightAndDue(t *testing.T) {
	e := u3NewEnv(t)
	_, client := u3LoginSeed(t, e, "u5-master@example.com", "master-pass", coreauth.RoleMaster)
	owner := w2dSeedUser(t, e, "u5-owner@example.com", "owner-pass", coreauth.RoleUser)

	now := u5NowFromServer(t, e, client)
	day := now.UTC().Format("2006-01-02")
	school := u3SeedSchool(t, e, "U5 执行学校")
	task := u3SeedTask(t, e, school.ID, "早签到")
	acc := u3SeedAccount(t, e, owner.ID, school.ID, "13800138001")
	u5SeedRunning(t, e, task, acc, day, now)
	// 到期 pending 与未到期 pending 各一行（第二个账号，避开每账号单行约束）。
	acc2 := u3SeedAccount(t, e, owner.ID, school.ID, "13800138002")
	u3SeedSignTask(t, e, task, acc2, day, now.Add(-time.Hour), status.TaskPending, 0, "", nil)
	acc3 := u3SeedAccount(t, e, owner.ID, school.ID, "13800138003")
	u3SeedSignTask(t, e, task, acc3, day, now.Add(time.Hour), status.TaskPending, 0, "", nil)

	resp, snap, body := u5EngineGet(e, client)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}
	if len(snap.Inflight) != 1 {
		t.Fatalf("inflight = %d rows, want 1; body=%s", len(snap.Inflight), body)
	}
	row := snap.Inflight[0]
	if row.AccountPhoneMasked != "138****8001" {
		t.Fatalf("masked phone = %q, want 138****8001", row.AccountPhoneMasked)
	}
	if row.SchoolID != school.ID || row.SchoolName != school.Name {
		t.Fatalf("school = %s/%s, want %s/%s", row.SchoolID, row.SchoolName, school.ID, school.Name)
	}
	if row.SchoolName != school.Name || !strings.Contains(string(body), school.Name) {
		t.Fatalf("school name missing from body")
	}
	lease := row.LeaseExpiresAt.Sub(row.ClaimedAt)
	if lease != 2*time.Minute {
		t.Fatalf("lease span = %v, want 2m (claimed %v expires %v)", lease, row.ClaimedAt, row.LeaseExpiresAt)
	}
	if snap.DueNow != 1 {
		t.Fatalf("due_now = %d, want 1 (one due pending; the future row and the running row are not due)", snap.DueNow)
	}
	if strings.Contains(string(body), "13800138001") {
		t.Fatal("the full phone number must never leave the server")
	}
}

// 当日计数：success 与 failed 分开计；skipped 不进两者；非当日不计。
func TestU5EngineTodayCounts(t *testing.T) {
	e := u3NewEnv(t)
	_, client := u3LoginSeed(t, e, "u5-master@example.com", "master-pass", coreauth.RoleMaster)
	owner := w2dSeedUser(t, e, "u5-owner@example.com", "owner-pass", coreauth.RoleUser)

	now := u5NowFromServer(t, e, client)
	day := now.UTC().Format("2006-01-02")
	yesterday := now.UTC().AddDate(0, 0, -1).Format("2006-01-02")
	school := u3SeedSchool(t, e, "U5 计数学校")
	task := u3SeedTask(t, e, school.ID, "早签到")

	accS := u3SeedAccount(t, e, owner.ID, school.ID, "13800138011")
	u3SeedSignTask(t, e, task, accS, day, now, status.TaskSuccess, 1, "ok", &now)
	accF := u3SeedAccount(t, e, owner.ID, school.ID, "13800138012")
	u3SeedSignTask(t, e, task, accF, day, now, status.TaskFailed, 2, "rejected", &now)
	accK := u3SeedAccount(t, e, owner.ID, school.ID, "13800138013")
	u3SeedSignTask(t, e, task, accK, day, now, status.TaskSkipped, 0, "window_closed", nil)
	accY := u3SeedAccount(t, e, owner.ID, school.ID, "13800138014")
	u3SeedSignTask(t, e, task, accY, yesterday, now, status.TaskFailed, 1, "rejected", nil)

	resp, snap, _ := u5EngineGet(e, client)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}
	if snap.TodaySuccess != 1 {
		t.Fatalf("today_success = %d, want 1", snap.TodaySuccess)
	}
	if snap.TodayFailed != 1 {
		t.Fatalf("today_failed = %d, want 1 (skipped is not a failure; yesterday's row is another day)", snap.TodayFailed)
	}
}

// 当日终态按账号去重：每账号只取最后一条任务行（updated_at DESC, id DESC）。
// success 在前 failed 在后 → 只计 failed；反过来 → 只计 success；
// 两账号合计 1/1，不得按任务行重复计。
func TestU5EngineTodayTerminalPicksLatestRow(t *testing.T) {
	e := u3NewEnv(t)
	_, client := u3LoginSeed(t, e, "u5-master@example.com", "master-pass", coreauth.RoleMaster)
	owner := w2dSeedUser(t, e, "u5-owner@example.com", "owner-pass", coreauth.RoleUser)

	now := u5NowFromServer(t, e, client)
	day := now.UTC().Format("2006-01-02")
	school := u3SeedSchool(t, e, "U5 终态学校")
	taskA := u3SeedTask(t, e, school.ID, "早签到")
	taskB := u3SeedTask(t, e, school.ID, "晚自习签到")

	early := now.Add(-2 * time.Hour)
	late := now.Add(-time.Hour)
	accF := u3SeedAccount(t, e, owner.ID, school.ID, "13800138051")
	u5SeedTerminalTask(t, e, taskA, accF, day, early, status.TaskSuccess)
	u5SeedTerminalTask(t, e, taskB, accF, day, late, status.TaskFailed)
	accS := u3SeedAccount(t, e, owner.ID, school.ID, "13800138052")
	u5SeedTerminalTask(t, e, taskA, accS, day, early, status.TaskFailed)
	u5SeedTerminalTask(t, e, taskB, accS, day, late, status.TaskSuccess)

	resp, snap, body := u5EngineGet(e, client)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}
	if snap.TodaySuccess != 1 || snap.TodayFailed != 1 {
		t.Fatalf("today counts = success %d failed %d, want 1/1 "+
			"(only each account's latest row counts, not every task row); body=%s",
			snap.TodaySuccess, snap.TodayFailed, body)
	}
}

// updated_at 并列时按 id DESC 决胜：较大 id 的行胜出。
// 同账号同刻两行（不同学校任务），胜者的 state 决定 KPI 落桶。
// 胜者按库内现查的 id 大小计算，不赌 uuidv7 的生成次序；
// 决胜方向反转（ASC）时计数值必然对调，本用例即红。
func TestU5EngineTodayTerminalTieBreaksByID(t *testing.T) {
	e := u3NewEnv(t)
	_, client := u3LoginSeed(t, e, "u5-master@example.com", "master-pass", coreauth.RoleMaster)
	owner := w2dSeedUser(t, e, "u5-owner@example.com", "owner-pass", coreauth.RoleUser)

	now := u5NowFromServer(t, e, client)
	day := now.UTC().Format("2006-01-02")
	school := u3SeedSchool(t, e, "U5 并列学校")
	taskA := u3SeedTask(t, e, school.ID, "早签到")
	taskB := u3SeedTask(t, e, school.ID, "晚自习签到")

	acc := u3SeedAccount(t, e, owner.ID, school.ID, "13800138058")
	sameAt := now.Add(-time.Hour)
	u5SeedTerminalTask(t, e, taskA, acc, day, sameAt, status.TaskSuccess)
	u5SeedTerminalTask(t, e, taskB, acc, day, sameAt, status.TaskFailed)
	successID := e.scalarString(
		`SELECT id::text FROM sign_tasks
		  WHERE account_id = $1::uuid AND school_task_id = $2::uuid AND day = $3::date`,
		acc, taskA, day)
	failedID := e.scalarString(
		`SELECT id::text FROM sign_tasks
		  WHERE account_id = $1::uuid AND school_task_id = $2::uuid AND day = $3::date`,
		acc, taskB, day)

	resp, snap, body := u5EngineGet(e, client)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}
	wantSuccess, wantFailed := 1, 0
	if failedID > successID {
		wantSuccess, wantFailed = 0, 1
	}
	if snap.TodaySuccess != wantSuccess || snap.TodayFailed != wantFailed {
		t.Fatalf("tied updated_at: counts = success %d failed %d, want %d/%d "+
			"(the larger id must win the tie: success %s vs failed %s); body=%s",
			snap.TodaySuccess, snap.TodayFailed, wantSuccess, wantFailed,
			successID, failedID, body)
	}
}

// 终态不认 skipped：最后一条是 skipped 的账号两个计数都不进，
// 即使更早还有一条 success——被 skipped 掩盖的旧结果不能漏回 KPI。
func TestU5EngineTodayTerminalSkippedNotCounted(t *testing.T) {
	e := u3NewEnv(t)
	_, client := u3LoginSeed(t, e, "u5-master@example.com", "master-pass", coreauth.RoleMaster)
	owner := w2dSeedUser(t, e, "u5-owner@example.com", "owner-pass", coreauth.RoleUser)

	now := u5NowFromServer(t, e, client)
	day := now.UTC().Format("2006-01-02")
	school := u3SeedSchool(t, e, "U5 跳过学校")
	taskA := u3SeedTask(t, e, school.ID, "早签到")
	taskB := u3SeedTask(t, e, school.ID, "晚自习签到")

	accK := u3SeedAccount(t, e, owner.ID, school.ID, "13800138053")
	u5SeedTerminalTask(t, e, taskA, accK, day, now.Add(-2*time.Hour), status.TaskSuccess)
	u5SeedTerminalTask(t, e, taskB, accK, day, now.Add(-time.Hour), status.TaskSkipped)
	accOnlyK := u3SeedAccount(t, e, owner.ID, school.ID, "13800138054")
	u5SeedTerminalTask(t, e, taskA, accOnlyK, day, now.Add(-time.Hour), status.TaskSkipped)

	resp, snap, body := u5EngineGet(e, client)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}
	if snap.TodaySuccess != 0 || snap.TodayFailed != 0 {
		t.Fatalf("today counts = success %d failed %d, want 0/0 "+
			"(a skipped latest row hides the account from both KPIs); body=%s",
			snap.TodaySuccess, snap.TodayFailed, body)
	}
}

// 软删账号整段排除：deleted_at 非空的账号当日行不计；
// 未删账号照常计数，排除不能把活账号一起抹掉。
func TestU5EngineTodayTerminalExcludesSoftDeleted(t *testing.T) {
	e := u3NewEnv(t)
	_, client := u3LoginSeed(t, e, "u5-master@example.com", "master-pass", coreauth.RoleMaster)
	owner := w2dSeedUser(t, e, "u5-owner@example.com", "owner-pass", coreauth.RoleUser)

	now := u5NowFromServer(t, e, client)
	day := now.UTC().Format("2006-01-02")
	school := u3SeedSchool(t, e, "U5 软删学校")
	task := u3SeedTask(t, e, school.ID, "早签到")

	accGone := u3SeedAccount(t, e, owner.ID, school.ID, "13800138055")
	u5SeedTerminalTask(t, e, task, accGone, day, now.Add(-time.Hour), status.TaskFailed)
	if err := pg.NewAccountRepo(e.db).SoftDelete(e.ctx, corescope.Master(), accGone, owner.ID, now); err != nil {
		t.Fatalf("soft delete account: %v", err)
	}
	accLive := u3SeedAccount(t, e, owner.ID, school.ID, "13800138056")
	u5SeedTerminalTask(t, e, task, accLive, day, now.Add(-time.Hour), status.TaskSuccess)

	resp, snap, body := u5EngineGet(e, client)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}
	if snap.TodaySuccess != 1 || snap.TodayFailed != 0 {
		t.Fatalf("today counts = success %d failed %d, want 1/0 "+
			"(the soft-deleted account's failed row must not count); body=%s",
			snap.TodaySuccess, snap.TodayFailed, body)
	}
}

// admin 无绑定校：当日终态全 0，绝不退化成全量；
// 同一份数据 master 照常看到。
func TestU5EngineAdminEmptyBinding(t *testing.T) {
	e := u3NewEnv(t)
	_, adminClient := u3LoginSeed(t, e, "u5-nobind-admin@example.com", "admin-pass", coreauth.RoleAdmin)
	_, masterClient := u3LoginSeed(t, e, "u5-nobind-master@example.com", "master-pass", coreauth.RoleMaster)
	owner := w2dSeedUser(t, e, "u5-nobind-owner@example.com", "owner-pass", coreauth.RoleUser)

	now := u5NowFromServer(t, e, adminClient)
	day := now.UTC().Format("2006-01-02")
	school := u3SeedSchool(t, e, "U5 无绑定学校")
	task := u3SeedTask(t, e, school.ID, "早签到")
	acc := u3SeedAccount(t, e, owner.ID, school.ID, "13800138057")
	u5SeedTerminalTask(t, e, task, acc, day, now.Add(-time.Hour), status.TaskFailed)

	resp, adminSnap, _ := u5EngineGet(e, adminClient)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("admin status = %d, want 200", resp.StatusCode)
	}
	if adminSnap.TodaySuccess != 0 || adminSnap.TodayFailed != 0 {
		t.Fatalf("admin counts = success %d failed %d, want 0/0 "+
			"(an admin with no school binding must not fall back to the full set)",
			adminSnap.TodaySuccess, adminSnap.TodayFailed)
	}
	resp, masterSnap, _ := u5EngineGet(e, masterClient)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("master status = %d, want 200", resp.StatusCode)
	}
	if masterSnap.TodayFailed != 1 {
		t.Fatalf("master today_failed = %d, want 1 (the same row is in master's scope)", masterSnap.TodayFailed)
	}
}

// 待重试：pending 且 attempts>0 才出现；attempts、下次 run_at、result、
// 任务名齐；按 run_at 升序；attempts=0 与终态行不出现；
// 同账号两条待重试（同日两个学校任务）都出现且 task_id 互不相同。
func TestU5EngineRetryingRows(t *testing.T) {
	e := u3NewEnv(t)
	_, client := u3LoginSeed(t, e, "u5-master@example.com", "master-pass", coreauth.RoleMaster)
	owner := w2dSeedUser(t, e, "u5-owner@example.com", "owner-pass", coreauth.RoleUser)

	now := u5NowFromServer(t, e, client)
	day := now.UTC().Format("2006-01-02")
	school := u3SeedSchool(t, e, "U5 重试学校")
	task := u3SeedTask(t, e, school.ID, "早签到")
	taskLate := u3SeedTask(t, e, school.ID, "晚自习签到")

	soon := now.Add(30 * time.Minute)
	late := now.Add(2 * time.Hour)
	acc1 := u3SeedAccount(t, e, owner.ID, school.ID, "13800138021")
	earlyID := u3SeedSignTask(t, e, task, acc1, day, soon, status.TaskPending, 1, "rejected", nil)
	lateID := u3SeedSignTask(t, e, taskLate, acc1, day, late, status.TaskPending, 2, "transient", nil)
	acc2 := u3SeedAccount(t, e, owner.ID, school.ID, "13800138022")
	u3SeedSignTask(t, e, task, acc2, day, soon, status.TaskPending, 0, "", nil)
	acc3 := u3SeedAccount(t, e, owner.ID, school.ID, "13800138023")
	u3SeedSignTask(t, e, task, acc3, day, soon, status.TaskFailed, 3, "blocked", nil)

	resp, snap, body := u5EngineGet(e, client)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}
	if len(snap.Retrying) != 2 {
		t.Fatalf("retrying = %d rows, want 2; body=%s", len(snap.Retrying), body)
	}
	if snap.Retrying[0].RunAt.After(snap.Retrying[1].RunAt) {
		t.Fatal("retrying rows must be ordered by run_at ascending")
	}
	first := snap.Retrying[0]
	if first.AccountPhoneMasked != "138****8021" || first.Attempts != 1 || first.Result != "rejected" {
		t.Fatalf("first retry = %+v, want 138****8021 attempts 1 rejected", first)
	}
	if first.TaskID != earlyID || first.TaskName != "早签到" {
		t.Fatalf("first retry identity = %s/%s, want %s/早签到", first.TaskID, first.TaskName, earlyID)
	}
	second := snap.Retrying[1]
	if second.TaskID != lateID || second.TaskName != "晚自习签到" || second.Attempts != 2 || !second.RunAt.Equal(late) {
		t.Fatalf("second retry = %+v, want task %s 晚自习签到 attempts 2 at %v", second, lateID, late)
	}
	if first.TaskID == second.TaskID {
		t.Fatal("two retries of one account must carry distinct task ids")
	}
	if snap.RetryingTruncated {
		t.Fatal("two retry rows must not read as truncated")
	}
	if strings.Contains(string(body), "13800138021") {
		t.Fatal("the full phone number must never leave the server")
	}
}

// 待重试超过 200 条：只回最先到期的 200 条，retrying_truncated 如实为真。
func TestU5EngineRetryingTruncated(t *testing.T) {
	e := u3NewEnv(t)
	_, client := u3LoginSeed(t, e, "u5-master@example.com", "master-pass", coreauth.RoleMaster)
	owner := w2dSeedUser(t, e, "u5-owner@example.com", "owner-pass", coreauth.RoleUser)

	now := u5NowFromServer(t, e, client)
	school := u3SeedSchool(t, e, "U5 截断学校")
	task := u3SeedTask(t, e, school.ID, "早签到")
	acc := u3SeedAccount(t, e, owner.ID, school.ID, "13800000041")

	// 同账号同任务、跨 201 个业务日各一条：超出 EngineRetryLimit 一行。
	const total = 201
	rows := make([]coreschedule.PlanRow, 0, total)
	for i := 0; i < total; i++ {
		rows = append(rows, coreschedule.PlanRow{
			AccountID: acc, SchoolTaskID: task,
			Day:   now.UTC().AddDate(0, 0, -total+i).Format("2006-01-02"),
			RunAt: now.Add(time.Duration(i-total) * time.Minute),
		})
	}
	repo := pg.NewScheduleRepo(e.db)
	if _, err := repo.InsertPlan(e.ctx, rows); err != nil {
		t.Fatalf("insert plan: %v", err)
	}
	if _, err := e.db.Exec(e.ctx,
		`UPDATE sign_tasks SET state = 'pending', attempts = 2, result = 'transient'
		  WHERE account_id = $1::uuid AND school_task_id = $2::uuid`,
		acc, task); err != nil {
		t.Fatalf("seed retry states: %v", err)
	}

	resp, snap, _ := u5EngineGet(e, client)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}
	if len(snap.Retrying) != 200 {
		t.Fatalf("retrying = %d rows, want the 200-row cap", len(snap.Retrying))
	}
	if !snap.RetryingTruncated {
		t.Fatal("retrying_truncated must be true when the cap cuts rows")
	}
	for i := 1; i < len(snap.Retrying); i++ {
		if snap.Retrying[i].RunAt.Before(snap.Retrying[i-1].RunAt) {
			t.Fatal("capped rows must stay ordered by run_at ascending")
		}
	}
	// 保留的必须是最先到期的 200 条：被截掉的只能是落点最晚的那行。
	dropped := rows[total-1].RunAt
	if !snap.Retrying[len(snap.Retrying)-1].RunAt.Before(dropped) {
		t.Fatalf("the kept rows must stop before %v; the cap must cut the latest run_at first", dropped)
	}
}

// admin 只见绑定校：在飞、due、当日失败与待重试都收窄；master 全量。
func TestU5EngineAdminScope(t *testing.T) {
	e := u3NewEnv(t)
	adminUser, adminClient := u3LoginSeed(t, e, "u5-admin@example.com", "admin-pass", coreauth.RoleAdmin)
	_, masterClient := u3LoginSeed(t, e, "u5-master@example.com", "master-pass", coreauth.RoleMaster)
	owner := w2dSeedUser(t, e, "u5-owner@example.com", "owner-pass", coreauth.RoleUser)

	now := u5NowFromServer(t, e, adminClient)
	day := now.UTC().Format("2006-01-02")
	schoolA := u3SeedSchool(t, e, "U5 绑定学校")
	schoolB := u3SeedSchool(t, e, "U5 局外学校")
	taskA := u3SeedTask(t, e, schoolA.ID, "早签到")
	taskB := u3SeedTask(t, e, schoolB.ID, "晚签到")

	accA := u3SeedAccount(t, e, owner.ID, schoolA.ID, "13800000031")
	u5SeedRunning(t, e, taskA, accA, day, now)
	accB := u3SeedAccount(t, e, owner.ID, schoolB.ID, "13800000032")
	u5SeedRunning(t, e, taskB, accB, day, now)
	accB2 := u3SeedAccount(t, e, owner.ID, schoolB.ID, "13800000030")
	u3SeedSignTask(t, e, taskB, accB2, day, now.Add(-time.Hour), status.TaskPending, 2, "transient", nil)
	accB3 := u3SeedAccount(t, e, owner.ID, schoolB.ID, "13800000040")
	u3SeedSignTask(t, e, taskB, accB3, day, now, status.TaskFailed, 1, "blocked", nil)
	u3Grant(t, e, adminUser.ID, schoolA.ID)

	resp, adminSnap, _ := u5EngineGet(e, adminClient)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("admin status = %d, want 200", resp.StatusCode)
	}
	if len(adminSnap.Inflight) != 1 || adminSnap.Inflight[0].SchoolID != schoolA.ID {
		t.Fatalf("admin inflight = %+v, want only school A", adminSnap.Inflight)
	}
	if adminSnap.DueNow != 0 {
		t.Fatalf("admin due_now = %d, want 0 (the due row belongs to school B)", adminSnap.DueNow)
	}
	if adminSnap.TodayFailed != 0 {
		t.Fatalf("admin today_failed = %d, want 0 (the failed row belongs to school B)", adminSnap.TodayFailed)
	}
	if len(adminSnap.Retrying) != 0 {
		t.Fatalf("admin retrying = %+v, want empty (school B is out of scope)", adminSnap.Retrying)
	}

	resp, masterSnap, _ := u5EngineGet(e, masterClient)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("master status = %d, want 200", resp.StatusCode)
	}
	if len(masterSnap.Inflight) != 2 {
		t.Fatalf("master inflight = %d rows, want 2", len(masterSnap.Inflight))
	}
	if masterSnap.DueNow != 1 || masterSnap.TodayFailed != 1 || len(masterSnap.Retrying) != 1 {
		t.Fatalf("master counts = due %d failed %d retrying %d, want 1/1/1",
			masterSnap.DueNow, masterSnap.TodayFailed, len(masterSnap.Retrying))
	}
}

// u5 无翻页参数：查询串恒空（契约没有游标）。
func TestU5EngineIgnoresQuery(t *testing.T) {
	e := u3NewEnv(t)
	_, client := u3LoginSeed(t, e, "u5-master@example.com", "master-pass", coreauth.RoleMaster)
	resp, _ := u3Get(e, client, "/api/admin/engine?"+url.Values{"limit": {"5"}}.Encode())
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200 (unknown query keys are not an error)", resp.StatusCode)
	}
}

// 循环与端点必须共享同一 EngineState（装配级接线判据）。
// 若装配给了两个实例，端点永远报「未启动」，常规快照用例照样全绿：
// 本用例起真循环、跑出 tick，再用 HTTP 读回非空时刻。
func TestU5EngineSharesLoopState(t *testing.T) {
	e := u3NewEnv(t)
	_, client := u3LoginSeed(t, e, "u5-master@example.com", "master-pass", coreauth.RoleMaster)
	if e.stack.Worker == nil || e.stack.EngineState == nil {
		t.Fatal("the assembled stack must carry a worker and its engine state")
	}

	loop := appworker.NewLoop(e.stack.Worker, e.stack.Worker, coreclock.System{},
		e.stack.EngineState, 5*time.Millisecond, 1)
	if err := loop.Start(); err != nil {
		t.Fatalf("start loop: %v", err)
	}
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_, _ = loop.Stop(ctx)
	})

	// 轮询端点直到最近一轮非空：循环与端点共享实例时这必然发生。
	timeout := time.After(10 * time.Second)
	for {
		select {
		case <-timeout:
			t.Fatal("the endpoint never saw a loop tick; the handler and the loop hold different states")
		default:
		}
		resp, snap, _ := u5EngineGet(e, client)
		if resp.StatusCode != http.StatusOK {
			t.Fatalf("status = %d, want 200", resp.StatusCode)
		}
		if snap.WorkerStartedAt != nil && snap.LastTickAt != nil {
			break
		}
		time.Sleep(50 * time.Millisecond)
	}
}
