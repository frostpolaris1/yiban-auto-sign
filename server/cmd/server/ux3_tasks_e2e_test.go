package main

// UX-3 运行页 e2e：任务检索与 CSV 导出。
//
// # 功能
// 覆盖 swh 验收的后端面：master/admin 作用域、day/state/school_id/q
// 过滤、游标翻页、手机号只出掩码、CSV 表头与行、导出 10000 行截断、
// 诊断端点对 master 开放（本批契约修订）。
// 走真实栈：真库、真登录、真 cookie、真 HTTP。
// 本文件**不**测前端，**不**复制仓储层用例。
//
// # 归属
// cmd/server 是唯一可同时引用 adapters、app、entry 的位置。
// 环境复用 auth_e2e_test.go 的 newAuthEnv；用户造数复用 w2dSeedUser。
//
// # 口径
// 缺 DSN 时整包跳过，并打印横幅。跳过不是绿。
// 每个测试独占一个 schema，互不依赖。
// 手机号只取登记的 138 合成段（tests/synthetic-fixtures.json）。
// 邮箱一律用保留域 example.com。状态词一律用 corestatus 常量，
// 不在测试里写状态字符串字面量。
// 时区固定 UTC（mutate 改写），业务日按 UTC 墙钟计算；
// 种子 run_at 一律取当日 08:00 UTC，跨日切点不进判定。
// 显式 day 的用例固定 2030-01-01，不依赖墙钟；
// 缺省日用例向服务端要时刻（/api/me/diagnostics 的 updated_at），
// 不在本文件读 time.Now（canonical 的 clock.now 判据）。

import (
	"context"
	"encoding/csv"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/config"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	appaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/account"
	appschool "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/school"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
)

// u3Day 是显式 day 用例的固定业务日：不随墙钟漂移。
const u3Day = "2030-01-01"

// u3TodayFromServer 向服务端要当前时刻并换算成服务端业务日。
// 时刻来源是 /api/me/diagnostics 的 updated_at（服务端 Clock.Now）。
func u3TodayFromServer(t *testing.T, e *authEnv, client *http.Client) string {
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
	return coreclock.BusinessDay(body.UpdatedAt, time.UTC)
}

// u3TaskRow 是契约 AdminTaskRow 的形状（仅取断言用到的字段）。
type u3TaskRow struct {
	ID                 string     `json:"id"`
	AccountID          string     `json:"account_id"`
	AccountPhoneMasked string     `json:"account_phone_masked"`
	SchoolID           string     `json:"school_id"`
	SchoolName         string     `json:"school_name"`
	TaskName           string     `json:"task_name"`
	State              string     `json:"state"`
	Attempts           int        `json:"attempts"`
	Result             string     `json:"result"`
	RunAt              time.Time  `json:"run_at"`
	ClaimedAt          *time.Time `json:"claimed_at"`
	UpdatedAt          time.Time  `json:"updated_at"`
}

// u3TaskPage 是契约 TaskPage 的形状。
type u3TaskPage struct {
	Items      []u3TaskRow `json:"items"`
	NextCursor string      `json:"next_cursor"`
}

// u3Get 以指定客户端发 GET 并返回响应与响应体。
func u3Get(e *authEnv, client *http.Client, path string) (*http.Response, []byte) {
	e.t.Helper()
	return u1Do(e, client, http.MethodGet, path, nil, "")
}

// u3List 以指定客户端请求任务列表并解码响应。
func u3List(e *authEnv, client *http.Client, query url.Values) (*http.Response, u3TaskPage) {
	e.t.Helper()
	resp, payload := u3Get(e, client, "/api/admin/tasks?"+query.Encode())
	var page u3TaskPage
	if resp.StatusCode == http.StatusOK {
		if err := json.Unmarshal(payload, &page); err != nil {
			e.t.Fatalf("decode task page: %v; body=%q", err, payload)
		}
	}
	return resp, page
}

// u3NewEnv 造一个上海时区的装配环境。
func u3NewEnv(t *testing.T) *authEnv {
	return newAuthEnvWith(t, func(cfg *config.Config) { cfg.Timezone = "UTC" }, nil)
}

// u3SeedSchool 造一所学校并返回记录。name 由调用方给，可含检定字符。
func u3SeedSchool(t *testing.T, e *authEnv, name string) appschool.Record {
	t.Helper()
	rec, err := pg.NewSchoolRepo(e.db).Create(e.ctx, appschool.New{
		Code: "u3-" + randomHex(4),
		Name: name,
	})
	if err != nil {
		t.Fatalf("seed school %s: %v", name, err)
	}
	return rec
}

// u3SeedTask 在学校下建一条学校任务，返回任务 ID。
func u3SeedTask(t *testing.T, e *authEnv, schoolID, name string) string {
	t.Helper()
	id, err := pg.NewScheduleRepo(e.db).CreateSchoolTask(e.ctx, schoolID, name)
	if err != nil {
		t.Fatalf("seed school task %s: %v", name, err)
	}
	return id
}

// u3SeedAccount 建一个归属 ownerID、挂在学校下的账号，返回账号 ID。
func u3SeedAccount(t *testing.T, e *authEnv, ownerID, schoolID, phone string) string {
	t.Helper()
	var id string
	err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		rec, err := pg.NewAccountRepo(e.db).Create(ctx, appaccount.New{
			SchoolID:    schoolID,
			OwnerUserID: ownerID,
			Phone:       phone,
		}, 10000, 10000)
		if err != nil {
			return err
		}
		id = rec.ID
		return nil
	})
	if err != nil {
		t.Fatalf("seed account %s: %v", phone, err)
	}
	return id
}

// u3SeedSignTask 铺一行排期任务并按 state/result/claimedAt 改写，
// 返回行 ID。attempts 由调用方给：终态行至少 1。
func u3SeedSignTask(t *testing.T, e *authEnv, taskID, accountID, day string, runAt time.Time,
	state status.TaskState, attempts int, result string, claimedAt *time.Time) string {
	t.Helper()
	repo := pg.NewScheduleRepo(e.db)
	if _, err := repo.InsertPlan(e.ctx, []coreschedule.PlanRow{{
		AccountID: accountID, SchoolTaskID: taskID, Day: day, RunAt: runAt,
	}}); err != nil {
		t.Fatalf("seed sign task: %v", err)
	}
	var claimed any
	if claimedAt != nil {
		claimed = *claimedAt
	}
	if _, err := e.db.Exec(e.ctx,
		`UPDATE sign_tasks SET state = $2::sign_task_state, result = $3,
		        attempts = $4, claimed_at = $5
		  WHERE account_id = $1::uuid AND school_task_id = $6::uuid AND day = $7::date`,
		accountID, string(state), result, attempts, claimed, taskID, day); err != nil {
		t.Fatalf("seed sign task state: %v", err)
	}
	return e.scalarString(
		`SELECT id::text FROM sign_tasks
		  WHERE account_id = $1::uuid AND school_task_id = $2::uuid AND day = $3::date`,
		accountID, taskID, day)
}

// u3At 把业务日 day 的 UTC 墙钟 hour 点转成时刻。
func u3At(day string, hour int) time.Time {
	at, err := time.ParseInLocation("2006-01-02 15", day+" "+fmt.Sprintf("%02d", hour), time.UTC)
	if err != nil {
		panic("parse day: " + err.Error())
	}
	return at
}

// u3DayShift 返回业务日 day 偏移 days 天后的业务日。
func u3DayShift(day string, days int) string {
	at, err := time.ParseInLocation("2006-01-02", day, time.UTC)
	if err != nil {
		panic("parse day: " + err.Error())
	}
	return at.AddDate(0, 0, days).Format("2006-01-02")
}

// u3LoginSeed 建一名指定角色用户并登录，返回记录与已登录客户端。
func u3LoginSeed(t *testing.T, e *authEnv, email, password string, role coreauth.Role) (coreuser.Record, *http.Client) {
	t.Helper()
	user := w2dSeedUser(t, e, email, password, role)
	client := u1FreshClient(t)
	u1LoginOn(e, client, email, password)
	return user, client
}

// u3Grant 把 admin 绑到学校。grantedBy 记为该 admin 自己（测试无第四者）。
func u3Grant(t *testing.T, e *authEnv, userID, schoolID string) {
	t.Helper()
	if err := pg.NewAdminSchoolRepo(e.db).Grant(e.ctx, appschool.Binding{
		UserID: userID, SchoolID: schoolID,
	}); err != nil {
		t.Fatalf("grant school %s: %v", schoolID, err)
	}
}

// u3IDs 按行序抽 ID 列。
func u3IDs(rows []u3TaskRow) []string {
	out := make([]string, len(rows))
	for i, r := range rows {
		out[i] = r.ID
	}
	return out
}

// --- 用例 ----------------------------------------------------------------

// master 不限学校：两所学校的行都出现，按 run_at 倒序，行字段完整。
func TestU3MasterListsTasksAcrossSchools(t *testing.T) {
	e := newAuthEnvWith(t, func(cfg *config.Config) { cfg.Timezone = "UTC" }, nil)
	_, client := u3LoginSeed(t, e, "u3-master@example.com", "master-pass", coreauth.RoleMaster)
	owner := w2dSeedUser(t, e, "u3-owner@example.com", "owner-pass", coreauth.RoleUser)

	day := u3Day
	schoolA := u3SeedSchool(t, e, "U3 甲学校")
	schoolB := u3SeedSchool(t, e, "U3 乙学校")
	taskA := u3SeedTask(t, e, schoolA.ID, "早签到")
	taskB := u3SeedTask(t, e, schoolB.ID, "晚签到")
	accA := u3SeedAccount(t, e, owner.ID, schoolA.ID, "13800138001")
	accB := u3SeedAccount(t, e, owner.ID, schoolB.ID, "13800138002")

	runA := u3At(day, 8)
	runB := u3At(day, 9)
	idA := u3SeedSignTask(t, e, taskA, accA, day, runA, status.TaskSuccess, 1, "ok", &runA)
	idB := u3SeedSignTask(t, e, taskB, accB, day, runB, status.TaskFailed, 2, "credential_invalid", &runB)

	resp, page := u3List(e, client, url.Values{"day": {day}})
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("master list status = %d, want 200", resp.StatusCode)
	}
	if got := u3IDs(page.Items); len(got) != 2 || got[0] != idB || got[1] != idA {
		t.Fatalf("rows = %v, want [%s %s] in run_at desc order", got, idB, idA)
	}
	first := page.Items[0]
	if first.SchoolName != schoolB.Name || first.TaskName != "晚签到" {
		t.Fatalf("school/task = %q/%q, want %q/晚签到", first.SchoolName, first.TaskName, schoolB.Name)
	}
	if first.AccountPhoneMasked != "138****8002" {
		t.Fatalf("masked phone = %q, want 138****8002", first.AccountPhoneMasked)
	}
	if first.State != string(status.TaskFailed) || first.Attempts != 2 || first.Result != "credential_invalid" {
		t.Fatalf("state/attempts/result = %q/%d/%q", first.State, first.Attempts, first.Result)
	}
	if first.ClaimedAt == nil || !first.ClaimedAt.Equal(runB) {
		t.Fatalf("claimed_at = %v, want %v", first.ClaimedAt, runB)
	}
	if first.SchoolID != schoolB.ID || first.AccountID != accB {
		t.Fatalf("school/account = %q/%q, want %q/%q", first.SchoolID, first.AccountID, schoolB.ID, accB)
	}
	if page.NextCursor != "" {
		t.Fatalf("next_cursor = %q, want empty on the last page", page.NextCursor)
	}
}

// admin 只见绑定学校；未绑定的 admin 看到空页；user 角色回 403。
func TestU3AdminScopeSeesOnlyBoundSchools(t *testing.T) {
	e := newAuthEnvWith(t, func(cfg *config.Config) { cfg.Timezone = "UTC" }, nil)
	owner := w2dSeedUser(t, e, "u3-owner@example.com", "owner-pass", coreauth.RoleUser)
	day := u3Day

	schoolA := u3SeedSchool(t, e, "U3 绑定校")
	schoolB := u3SeedSchool(t, e, "U3 别校")
	taskA := u3SeedTask(t, e, schoolA.ID, "早签到")
	taskB := u3SeedTask(t, e, schoolB.ID, "早签到")
	accA := u3SeedAccount(t, e, owner.ID, schoolA.ID, "13800138003")
	accB := u3SeedAccount(t, e, owner.ID, schoolB.ID, "13800138004")
	runA := u3At(day, 8)
	idA := u3SeedSignTask(t, e, taskA, accA, day, runA, status.TaskPending, 0, "", nil)
	u3SeedSignTask(t, e, taskB, accB, day, runA, status.TaskPending, 0, "", nil)

	admin, adminClient := u3LoginSeed(t, e, "u3-admin@example.com", "admin-pass", coreauth.RoleAdmin)
	u3Grant(t, e, admin.ID, schoolA.ID)

	resp, page := u3List(e, adminClient, url.Values{"day": {day}})
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("admin list status = %d, want 200", resp.StatusCode)
	}
	if got := u3IDs(page.Items); len(got) != 1 || got[0] != idA {
		t.Fatalf("bound admin rows = %v, want only [%s]", got, idA)
	}

	_, stranger := u3LoginSeed(t, e, "u3-stranger@example.com", "stranger-pass", coreauth.RoleAdmin)
	resp, page = u3List(e, stranger, url.Values{"day": {day}})
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("unbound admin status = %d, want 200", resp.StatusCode)
	}
	if len(page.Items) != 0 {
		t.Fatalf("unbound admin rows = %v, want none: an empty binding is not master scope", u3IDs(page.Items))
	}

	_, member := u3LoginSeed(t, e, "u3-member@example.com", "member-pass", coreauth.RoleUser)
	resp, _ = u3List(e, member, url.Values{"day": {day}})
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("user role status = %d, want 403", resp.StatusCode)
	}
}

// day 缺省＝业务日今天；显式 day 只回那一天的行。
func TestU3DayFilterDefaultsToBusinessDay(t *testing.T) {
	e := newAuthEnvWith(t, func(cfg *config.Config) { cfg.Timezone = "UTC" }, nil)
	owner := w2dSeedUser(t, e, "u3-owner@example.com", "owner-pass", coreauth.RoleUser)
	_, client := u3LoginSeed(t, e, "u3-master@example.com", "master-pass", coreauth.RoleMaster)

	today := u3TodayFromServer(t, e, client)
	yesterday := u3DayShift(today, -1)
	school := u3SeedSchool(t, e, "U3 日期校")
	task := u3SeedTask(t, e, school.ID, "早签到")
	acc := u3SeedAccount(t, e, owner.ID, school.ID, "13800138005")
	idToday := u3SeedSignTask(t, e, task, acc, today, u3At(today, 8), status.TaskPending, 0, "", nil)
	// 昨日行放另一个账号：同账号同任务同日唯一。
	accY := u3SeedAccount(t, e, owner.ID, school.ID, "13800138006")
	idY := u3SeedSignTask(t, e, task, accY, yesterday, u3At(yesterday, 8), status.TaskSuccess, 1, "ok", nil)

	resp, page := u3List(e, client, url.Values{})
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("default day status = %d, want 200", resp.StatusCode)
	}
	if got := u3IDs(page.Items); len(got) != 1 || got[0] != idToday {
		t.Fatalf("default day rows = %v, want only today [%s]", got, idToday)
	}

	resp, page = u3List(e, client, url.Values{"day": {yesterday}})
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("yesterday status = %d, want 200", resp.StatusCode)
	}
	if got := u3IDs(page.Items); len(got) != 1 || got[0] != idY {
		t.Fatalf("yesterday rows = %v, want only [%s]", got, idY)
	}
}

// state 与 school_id 过滤是**与**关系；非法 state 回 400。
func TestU3StateAndSchoolFilters(t *testing.T) {
	e := newAuthEnvWith(t, func(cfg *config.Config) { cfg.Timezone = "UTC" }, nil)
	owner := w2dSeedUser(t, e, "u3-owner@example.com", "owner-pass", coreauth.RoleUser)
	_, client := u3LoginSeed(t, e, "u3-master@example.com", "master-pass", coreauth.RoleMaster)

	day := u3Day
	schoolA := u3SeedSchool(t, e, "U3 状态校甲")
	schoolB := u3SeedSchool(t, e, "U3 状态校乙")
	taskA := u3SeedTask(t, e, schoolA.ID, "早签到")
	taskB := u3SeedTask(t, e, schoolB.ID, "早签到")
	accA1 := u3SeedAccount(t, e, owner.ID, schoolA.ID, "13800138007")
	accA2 := u3SeedAccount(t, e, owner.ID, schoolA.ID, "13800138008")
	accB1 := u3SeedAccount(t, e, owner.ID, schoolB.ID, "13800138009")
	runA := u3At(day, 8)
	idFail := u3SeedSignTask(t, e, taskA, accA1, day, runA, status.TaskFailed, 1, "credential_invalid", &runA)
	idDone := u3SeedSignTask(t, e, taskA, accA2, day, u3At(day, 9), status.TaskSuccess, 1, "ok", nil)
	idOther := u3SeedSignTask(t, e, taskB, accB1, day, u3At(day, 10), status.TaskFailed, 1, "network_error", nil)

	// state=failed：两校各一条失败行。
	resp, page := u3List(e, client, url.Values{"day": {day}, "state": {string(status.TaskFailed)}})
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("state filter status = %d, want 200", resp.StatusCode)
	}
	got := map[string]bool{}
	for _, r := range page.Items {
		got[r.ID] = true
	}
	if len(got) != 2 || !got[idFail] || !got[idOther] {
		t.Fatalf("state=failed rows = %v, want {%s, %s}", u3IDs(page.Items), idFail, idOther)
	}

	// school_id=A：只回 A 校两行。
	resp, page = u3List(e, client, url.Values{"day": {day}, "school_id": {schoolA.ID}})
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("school filter status = %d, want 200", resp.StatusCode)
	}
	if got := u3IDs(page.Items); len(got) != 2 {
		t.Fatalf("school A rows = %v, want 2 rows of school A", got)
	}

	// 组合：A 校且 failed 只剩一条。
	resp, page = u3List(e, client, url.Values{"day": {day}, "school_id": {schoolA.ID},
		"state": {string(status.TaskFailed)}})
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("combined filter status = %d, want 200", resp.StatusCode)
	}
	if got := u3IDs(page.Items); len(got) != 1 || got[0] != idFail {
		t.Fatalf("combined rows = %v, want [%s]", got, idFail)
	}

	// 组合：A 校且 success 只剩一条。
	resp, page = u3List(e, client, url.Values{"day": {day}, "school_id": {schoolA.ID},
		"state": {string(status.TaskSuccess)}})
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("combined success filter status = %d, want 200", resp.StatusCode)
	}
	if got := u3IDs(page.Items); len(got) != 1 || got[0] != idDone {
		t.Fatalf("combined success rows = %v, want [%s]", got, idDone)
	}

	// 非法 state 回 400，不静默当成不过滤。
	resp, _ = u3List(e, client, url.Values{"day": {day}, "state": {"done"}})
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("bad state status = %d, want 400", resp.StatusCode)
	}
}

// q 按手机号前缀或学校名子串匹配；LIKE 元字符按字面处理。
func TestU3QueryMatchesPhonePrefixAndSchoolName(t *testing.T) {
	e := newAuthEnvWith(t, func(cfg *config.Config) { cfg.Timezone = "UTC" }, nil)
	owner := w2dSeedUser(t, e, "u3-owner@example.com", "owner-pass", coreauth.RoleUser)
	_, client := u3LoginSeed(t, e, "u3-master@example.com", "master-pass", coreauth.RoleMaster)

	day := u3Day
	schoolA := u3SeedSchool(t, e, "U3 检索甲校")
	schoolB := u3SeedSchool(t, e, "U3 检索乙校 100%")
	taskA := u3SeedTask(t, e, schoolA.ID, "早签到")
	taskB := u3SeedTask(t, e, schoolB.ID, "早签到")
	accA := u3SeedAccount(t, e, owner.ID, schoolA.ID, "13800138010")
	accB := u3SeedAccount(t, e, owner.ID, schoolB.ID, "13800138020")
	runA := u3At(day, 8)
	idA := u3SeedSignTask(t, e, taskA, accA, day, runA, status.TaskPending, 0, "", nil)
	idB := u3SeedSignTask(t, e, taskB, accB, day, runA, status.TaskPending, 0, "", nil)

	// 手机号前缀命中甲校账号。
	resp, page := u3List(e, client, url.Values{"day": {day}, "q": {"13800138010"}})
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("phone query status = %d, want 200", resp.StatusCode)
	}
	if got := u3IDs(page.Items); len(got) != 1 || got[0] != idA {
		t.Fatalf("phone prefix rows = %v, want [%s]", got, idA)
	}

	// 手机号短前缀只命中乙校：1380013802 是 13800138020 的前缀，不是甲校的。
	resp, page = u3List(e, client, url.Values{"day": {day}, "q": {"1380013802"}})
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("short phone prefix status = %d, want 200", resp.StatusCode)
	}
	if got := u3IDs(page.Items); len(got) != 1 || got[0] != idB {
		t.Fatalf("short phone prefix rows = %v, want [%s]", got, idB)
	}

	// 学校名子串命中两校。
	resp, page = u3List(e, client, url.Values{"day": {day}, "q": {"检索"}})
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("school name query status = %d, want 200", resp.StatusCode)
	}
	if len(page.Items) != 2 {
		t.Fatalf("school substring rows = %v, want both", u3IDs(page.Items))
	}

	// LIKE 元字符按字面匹配：q="100%" 只命中乙校，不退化成全量。
	resp, page = u3List(e, client, url.Values{"day": {day}, "q": {"100%"}})
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("literal percent query status = %d, want 200", resp.StatusCode)
	}
	if got := u3IDs(page.Items); len(got) != 1 || got[0] != idB {
		t.Fatalf("literal percent rows = %v, want [%s] only", got, idB)
	}

	// 无命中是空页，不是错误。
	resp, page = u3List(e, client, url.Values{"day": {day}, "q": {"不存在的学校"}})
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("no-hit query status = %d, want 200", resp.StatusCode)
	}
	if len(page.Items) != 0 {
		t.Fatalf("no-hit rows = %v, want empty", u3IDs(page.Items))
	}
}

// 游标翻页不重不漏且保持 run_at 倒序；篡改游标回 400。
func TestU3CursorPaginationIsStable(t *testing.T) {
	e := newAuthEnvWith(t, func(cfg *config.Config) { cfg.Timezone = "UTC" }, nil)
	owner := w2dSeedUser(t, e, "u3-owner@example.com", "owner-pass", coreauth.RoleUser)
	_, client := u3LoginSeed(t, e, "u3-master@example.com", "master-pass", coreauth.RoleMaster)

	day := u3Day
	school := u3SeedSchool(t, e, "U3 游标校")
	task := u3SeedTask(t, e, school.ID, "早签到")
	var want []string
	for i := 0; i < 5; i++ {
		acc := u3SeedAccount(t, e, owner.ID, school.ID, fmt.Sprintf("138001380%02d", 20+i))
		id := u3SeedSignTask(t, e, task, acc, day, u3At(day, 8+i), status.TaskPending, 0, "", nil)
		want = append(want, id)
	}

	// 第一页 2 条。
	resp, page := u3List(e, client, url.Values{"day": {day}, "limit": {"2"}})
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("page 1 status = %d, want 200", resp.StatusCode)
	}
	if got := u3IDs(page.Items); len(got) != 2 || got[0] != want[4] || got[1] != want[3] {
		t.Fatalf("page 1 = %v, want [%s %s]", got, want[4], want[3])
	}
	if page.NextCursor == "" {
		t.Fatal("page 1 next_cursor is empty, want a cursor")
	}

	// 第二页 2 条。
	resp, page = u3List(e, client, url.Values{"day": {day}, "limit": {"2"}, "cursor": {page.NextCursor}})
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("page 2 status = %d, want 200", resp.StatusCode)
	}
	if got := u3IDs(page.Items); len(got) != 2 || got[0] != want[2] || got[1] != want[1] {
		t.Fatalf("page 2 = %v, want [%s %s]", got, want[2], want[1])
	}

	// 第三页 1 条且游标耗尽。
	resp, page = u3List(e, client, url.Values{"day": {day}, "limit": {"2"}, "cursor": {page.NextCursor}})
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("page 3 status = %d, want 200", resp.StatusCode)
	}
	if got := u3IDs(page.Items); len(got) != 1 || got[0] != want[0] || page.NextCursor != "" {
		t.Fatalf("page 3 = %v cursor=%q, want [%s] with empty cursor", got, page.NextCursor, want[0])
	}

	// 篡改游标回 400。
	resp, _ = u3List(e, client, url.Values{"day": {day}, "cursor": {"not-a-cursor"}})
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("bad cursor status = %d, want 400", resp.StatusCode)
	}
}

// 响应全文不出现完整手机号；掩码形态出现。
func TestU3PhoneNeverLeaks(t *testing.T) {
	e := newAuthEnvWith(t, func(cfg *config.Config) { cfg.Timezone = "UTC" }, nil)
	owner := w2dSeedUser(t, e, "u3-owner@example.com", "owner-pass", coreauth.RoleUser)
	_, client := u3LoginSeed(t, e, "u3-master@example.com", "master-pass", coreauth.RoleMaster)

	day := u3Day
	school := u3SeedSchool(t, e, "U3 掩码校")
	task := u3SeedTask(t, e, school.ID, "早签到")
	acc := u3SeedAccount(t, e, owner.ID, school.ID, "13800138030")
	runA := u3At(day, 8)
	u3SeedSignTask(t, e, task, acc, day, runA, status.TaskFailed, 1, "credential_invalid", &runA)

	resp, payload := u3Get(e, client, "/api/admin/tasks?"+url.Values{"day": {day}}.Encode())
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("list status = %d", resp.StatusCode)
	}
	if strings.Contains(string(payload), "13800138030") {
		t.Fatal("the full phone number leaked into the task list response")
	}
	if !strings.Contains(string(payload), "138****8030") {
		t.Fatalf("masked phone missing from response: %s", payload)
	}
}

// CSV 导出：表头与页面同口径、Content-Type 与 Content-Disposition 齐备、
// 行与列表一致且含引号转义；admin 作用域与筛选同样生效。
func TestU3ExportCSVHeadersRowsAndScope(t *testing.T) {
	e := newAuthEnvWith(t, func(cfg *config.Config) { cfg.Timezone = "UTC" }, nil)
	owner := w2dSeedUser(t, e, "u3-owner@example.com", "owner-pass", coreauth.RoleUser)
	_, masterClient := u3LoginSeed(t, e, "u3-master@example.com", "master-pass", coreauth.RoleMaster)

	day := u3Day
	schoolA := u3SeedSchool(t, e, "U3 导出甲校")
	schoolB := u3SeedSchool(t, e, "U3 导出乙校")
	taskA := u3SeedTask(t, e, schoolA.ID, "早签到")
	taskB := u3SeedTask(t, e, schoolB.ID, "早签到")
	accA := u3SeedAccount(t, e, owner.ID, schoolA.ID, "13800138031")
	accB := u3SeedAccount(t, e, owner.ID, schoolB.ID, "13800138000")
	runA := u3At(day, 8)
	idA := u3SeedSignTask(t, e, taskA, accA, day, runA, status.TaskFailed, 2, `带,逗号与"引号"`, &runA)
	u3SeedSignTask(t, e, taskB, accB, day, runA, status.TaskSuccess, 1, "ok", &runA)

	resp, body := u3Get(e, masterClient, "/api/admin/tasks/export?"+url.Values{"day": {day}}.Encode())
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("export status = %d; body=%s", resp.StatusCode, body)
	}
	if ct := resp.Header.Get("Content-Type"); !strings.HasPrefix(ct, "text/csv") {
		t.Fatalf("content-type = %q, want text/csv", ct)
	}
	disposition := resp.Header.Get("Content-Disposition")
	if !strings.Contains(disposition, "attachment") || !strings.Contains(disposition, ".csv") {
		t.Fatalf("content-disposition = %q, want an attachment with a .csv name", disposition)
	}
	if truncated := resp.Header.Get("X-Export-Truncated"); truncated != "" && truncated != "false" {
		t.Fatalf("small export must not be marked truncated, got %q", truncated)
	}

	table, err := csv.NewReader(strings.NewReader(string(body))).ReadAll()
	if err != nil {
		t.Fatalf("parse csv: %v", err)
	}
	wantHeader := []string{"id", "account_id", "account_phone_masked", "school_id",
		"school_name", "task_name", "state", "attempts", "result", "run_at",
		"claimed_at", "updated_at"}
	if len(table) == 0 || strings.Join(table[0], ",") != strings.Join(wantHeader, ",") {
		t.Fatalf("csv header = %v, want %v", table[0], wantHeader)
	}
	if len(table) != 3 {
		t.Fatalf("csv rows = %d (with header), want 3", len(table))
	}
	// 第一行是 run_at 较晚（同刻则 id 大）的行；这里两行同刻，按 id 序稳定。
	rowByID := map[string][]string{}
	for _, row := range table[1:] {
		rowByID[row[0]] = row
	}
	rowA := rowByID[idA]
	if rowA == nil {
		t.Fatalf("row %s missing from csv", idA)
	}
	if rowA[2] != "138****8031" || rowA[4] != schoolA.Name || rowA[5] != "早签到" ||
		rowA[6] != string(status.TaskFailed) || rowA[7] != "2" || rowA[8] != `带,逗号与"引号"` {
		t.Fatalf("row A = %v", rowA)
	}

	// admin 只导出绑定校。
	admin, adminClient := u3LoginSeed(t, e, "u3-admin@example.com", "admin-pass", coreauth.RoleAdmin)
	u3Grant(t, e, admin.ID, schoolA.ID)
	resp, body = u3Get(e, adminClient, "/api/admin/tasks/export?"+url.Values{"day": {day}}.Encode())
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("admin export status = %d", resp.StatusCode)
	}
	table, err = csv.NewReader(strings.NewReader(string(body))).ReadAll()
	if err != nil {
		t.Fatalf("parse admin csv: %v", err)
	}
	if len(table) != 2 {
		t.Fatalf("admin csv rows = %d (with header), want 2", len(table))
	}
	if table[1][0] != idA {
		t.Fatalf("admin csv row = %v, want only %s", table[1], idA)
	}
}

// 导出超 10000 行截断并显式标记；行数以响应头与实际行数双重核对。
func TestU3ExportTruncatesAt10000(t *testing.T) {
	e := newAuthEnvWith(t, func(cfg *config.Config) { cfg.Timezone = "UTC" }, nil)
	owner := w2dSeedUser(t, e, "u3-owner@example.com", "owner-pass", coreauth.RoleUser)
	_, client := u3LoginSeed(t, e, "u3-master@example.com", "master-pass", coreauth.RoleMaster)

	day := u3Day
	school := u3SeedSchool(t, e, "U3 截断校")
	acc := u3SeedAccount(t, e, owner.ID, school.ID, "13800138000")
	// 10001 条同日同账号任务：各挂一条独立学校任务，绕开
	// (account_id, school_task_id, day) 唯一约束。两条 set-return 语句完成。
	if _, err := e.db.Exec(e.ctx,
		`INSERT INTO school_tasks (school_id, name)
		 SELECT $1::uuid, 'bulk-' || i FROM generate_series(1, 10001) i`,
		school.ID); err != nil {
		t.Fatalf("bulk school tasks: %v", err)
	}
	if _, err := e.db.Exec(e.ctx,
		`INSERT INTO sign_tasks (account_id, school_task_id, day, run_at)
		 SELECT $1::uuid, st.id, $2::date, $3
		   FROM school_tasks st
		  WHERE st.school_id = $4::uuid AND st.name LIKE 'bulk-%'`,
		acc, day, u3At(day, 8), school.ID); err != nil {
		t.Fatalf("bulk sign tasks: %v", err)
	}
	if got := e.scalarInt(`SELECT count(*) FROM sign_tasks s
		JOIN school_tasks st ON st.id = s.school_task_id
		WHERE st.school_id = $1::uuid AND s.day = $2::date`, school.ID, day); got != 10001 {
		t.Fatalf("seeded rows = %d, want 10001", got)
	}

	resp, body := u3Get(e, client, "/api/admin/tasks/export?"+url.Values{"day": {day}}.Encode())
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("export status = %d", resp.StatusCode)
	}
	if truncated := resp.Header.Get("X-Export-Truncated"); truncated != "true" {
		t.Fatalf("X-Export-Truncated = %q, want true", truncated)
	}
	table, err := csv.NewReader(strings.NewReader(string(body))).ReadAll()
	if err != nil {
		t.Fatalf("parse csv: %v", err)
	}
	if len(table) != 10001 {
		t.Fatalf("csv rows = %d (with header), want 10001: header + capped 10000", len(table))
	}
	// 截断保留的是键序最前的 10000 行：最后一行仍是数据，不含截断说明行。
	if table[10000][0] == "" {
		t.Fatalf("last csv row is blank: %v", table[10000])
	}
}

// 校验错误面：limit 与 day 的边界在列表与导出两处同判。
func TestU3ValidationErrors(t *testing.T) {
	e := newAuthEnvWith(t, func(cfg *config.Config) { cfg.Timezone = "UTC" }, nil)
	_, client := u3LoginSeed(t, e, "u3-master@example.com", "master-pass", coreauth.RoleMaster)

	cases := []struct {
		name  string
		query url.Values
		path  string
		want  int
	}{
		{"limit zero", url.Values{"limit": {"0"}}, "/api/admin/tasks", 400},
		{"limit over", url.Values{"limit": {"201"}}, "/api/admin/tasks", 400},
		{"bad day", url.Values{"day": {"2026/10/31"}}, "/api/admin/tasks", 400},
		{"bad cursor", url.Values{"cursor": {"@@@"}}, "/api/admin/tasks", 400},
		{"export bad day", url.Values{"day": {"not-a-day"}}, "/api/admin/tasks/export", 400},
		{"export bad state", url.Values{"state": {"done"}}, "/api/admin/tasks/export", 400},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			resp, _ := u3Get(e, client, c.path+"?"+c.query.Encode())
			if resp.StatusCode != c.want {
				t.Fatalf("%s status = %d, want %d", c.name, resp.StatusCode, c.want)
			}
		})
	}
	// 缺 day 的导出走缺省业务日：不报错。
	resp, _ := u3Get(e, client, "/api/admin/tasks/export")
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("export default day status = %d, want 200", resp.StatusCode)
	}
	_, _ = io.Copy(io.Discard, resp.Body)
}

// 诊断端点对 master 开放（UX-3 契约修订）：master 200；admin 跨校 404 不变。
func TestU3DiagnosticsRoles(t *testing.T) {
	e := newAuthEnvWith(t, func(cfg *config.Config) { cfg.Timezone = "UTC" }, nil)
	owner := w2dSeedUser(t, e, "u3-owner@example.com", "owner-pass", coreauth.RoleUser)
	_, masterClient := u3LoginSeed(t, e, "u3-master@example.com", "master-pass", coreauth.RoleMaster)

	schoolA := u3SeedSchool(t, e, "U3 诊断甲校")
	schoolB := u3SeedSchool(t, e, "U3 诊断乙校")
	accA := u3SeedAccount(t, e, owner.ID, schoolA.ID, "13800138001")

	resp, payload := u3Get(e, masterClient, "/api/accounts/"+accA+"/diagnostics")
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("master diagnostics status = %d, want 200; body=%s", resp.StatusCode, payload)
	}

	admin, adminClient := u3LoginSeed(t, e, "u3-admin@example.com", "admin-pass", coreauth.RoleAdmin)
	u3Grant(t, e, admin.ID, schoolB.ID)
	resp, _ = u3Get(e, adminClient, "/api/accounts/"+accA+"/diagnostics")
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("cross-school admin diagnostics status = %d, want 404", resp.StatusCode)
	}
}
