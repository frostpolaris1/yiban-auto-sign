package main

// 失败归因聚合端点 GET /api/sign-stats/failures 的 HTTP + PostgreSQL e2e。
//
// # 功能
// 钉住任务书 07 的五条用户可见不变式：
//   1. 按任务行计数（同账号当日多任务失败逐行计入），只计当日终态 failed 项，
//      排除 pending/running/success/skipped、跨日行与已软删账号；保留 credential_error 账号的失败行。
//   2. 空 result 归入 unknown 并与字面 unknown 合并；词表外 result 原样返回；sum(items.count) == total；
//      空日返回 items=[] 与 total=0。
//   3. 作用域：master 全量；admin 只看绑定校；跨校或不存在 school_id/task_id 返回 404（不泄露存在性）；
//      同域内 school_id 与 task_id 不配套返回 400。
//   4. 负向：未认证 401；user 403；非法 day/school_id/task_id 400。
//   5. 缺省 day 与空查询串（?day=&school_id=&task_id=）使用服务端业务日今天。
//
// # 归属
// cmd/server e2e，使用真实路由与真实 PostgreSQL。

import (
	"encoding/json"
	"net/http"
	"strings"
	"testing"

	coreaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/account"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

func failInsertTask(t *testing.T, e *w2cEnv, accountID, taskID, day string, state corestatus.TaskState, result string) {
	t.Helper()
	now := coreclock.System{}.Now().UTC()
	if _, err := e.db.Exec(e.ctx, `
		INSERT INTO sign_tasks (account_id, school_task_id, day, run_at, state, result, updated_at)
		VALUES ($1::uuid, $2::uuid, $3::date, $4, $5::sign_task_state, $6, $4)`,
		accountID, taskID, day, now, string(state), result); err != nil {
		t.Fatalf("insert sign_task (%s/%s): %v", state, result, err)
	}
}

func failGet(t *testing.T, e *w2cEnv, path string, issued *coreauth.Issued, want int) (oapi.SignFailuresReport, string) {
	t.Helper()
	rec := e.do(http.MethodGet, path, nil, issued)
	w2cWantStatus(t, rec, want)
	var out oapi.SignFailuresReport
	if want == http.StatusOK {
		e.decode(rec, &out)
	}
	return out, rec.Body.String()
}

func failErrorParts(t *testing.T, raw string) (string, string) {
	t.Helper()
	var env struct {
		Error struct {
			Code    string `json:"code"`
			Message string `json:"message"`
		} `json:"error"`
	}
	if err := json.Unmarshal([]byte(raw), &env); err != nil {
		t.Fatalf("decode error envelope %q: %v", raw, err)
	}
	return env.Error.Code, env.Error.Message
}

func userAccountOrExtra(t *testing.T, e *w2cEnv, schoolID, userID string) string {
	t.Helper()
	return e.account(schoolID, userID, "13800000071")
}

func failBucketMap(items []oapi.SignFailureBucket) map[string]int {
	out := make(map[string]int, len(items))
	for _, item := range items {
		out[item.Result] = item.Count
	}
	return out
}

func failBucketSum(items []oapi.SignFailureBucket) int {
	total := 0
	for _, item := range items {
		total += item.Count
	}
	return total
}

func TestSignStatsFailuresScopeAndCounting(t *testing.T) {
	e := w2cNewEnv(t)
	master := e.user("fail-master@example.com", coreauth.RoleMaster)
	adminA := e.user("fail-admin-a@example.com", coreauth.RoleAdmin)
	adminBoth := e.user("fail-admin-both@example.com", coreauth.RoleAdmin)
	adminEmpty := e.user("fail-admin-empty@example.com", coreauth.RoleAdmin)
	user := e.user("fail-user@example.com", coreauth.RoleUser)

	schoolA := e.school("fail-a")
	schoolB := e.school("fail-b")
	e.bind(adminA.ID, schoolA.ID)
	e.bind(adminBoth.ID, schoolA.ID)
	e.bind(adminBoth.ID, schoolB.ID)

	a1 := e.account(schoolA.ID, master.ID, "13800000040")
	a2 := e.account(schoolA.ID, adminA.ID, "13800000041")
	a3 := e.account(schoolA.ID, adminBoth.ID, "13800000060")
	aDeleted := e.account(schoolA.ID, user.ID, "13800000070")
	b1 := e.account(schoolB.ID, master.ID, "13800000050")
	// 凭据错误账号的当日失败行必须计入；软删账号的遗留行必须排除。
	if _, err := e.db.Exec(e.ctx,
		`UPDATE accounts SET status = $2::account_status WHERE id = $1::uuid`,
		a1, string(coreaccount.StatusCredentialError)); err != nil {
		t.Fatalf("set credential_error status: %v", err)
	}
	if _, err := e.db.Exec(e.ctx,
		`UPDATE accounts SET deleted_at = now() WHERE id = $1::uuid`, aDeleted); err != nil {
		t.Fatalf("soft-delete account: %v", err)
	}

	taskA1, err := e.sched.CreateSchoolTask(e.ctx, schoolA.ID, "fail-task-a1")
	if err != nil {
		t.Fatalf("create task A1: %v", err)
	}
	taskA2, err := e.sched.CreateSchoolTask(e.ctx, schoolA.ID, "fail-task-a2")
	if err != nil {
		t.Fatalf("create task A2: %v", err)
	}
	taskB1, err := e.sched.CreateSchoolTask(e.ctx, schoolB.ID, "fail-task-b1")
	if err != nil {
		t.Fatalf("create task B1: %v", err)
	}

	today := m8cDay(0)
	yesterday := m8cDay(-1)
	emptyDay := m8cDay(-5)

	// 同账号 a1 当日两项任务均失败：按任务行计数应得 2 行（非账号去重）。
	failInsertTask(t, e, a1, taskA1, today, corestatus.TaskFailed, string(contract.OutcomeCredentialBad))
	failInsertTask(t, e, a1, taskA2, today, corestatus.TaskFailed, "")
	// a2/a3：字面 unknown 与空串合并为 unknown=2；pending/running/success/skipped 均不算终态失败。
	failInsertTask(t, e, a2, taskA1, today, corestatus.TaskFailed, string(contract.OutcomeUnknown))
	failInsertTask(t, e, a2, taskA2, today, corestatus.TaskPending, string(contract.OutcomeTransient))
	failInsertTask(t, e, a3, taskA1, today, corestatus.TaskRunning, "")
	failInsertTask(t, e, a3, taskA2, today, corestatus.TaskSuccess, string(contract.OutcomeSuccess))
	failInsertTask(t, e, userAccountOrExtra(t, e, schoolA.ID, user.ID), taskA2, today, corestatus.TaskSkipped, string(contract.OutcomeNoTarget))
	// 软删账号的 failed 行不得计入；跨日行不得计入。
	failInsertTask(t, e, aDeleted, taskA1, today, corestatus.TaskFailed, string(contract.OutcomeRejected))
	failInsertTask(t, e, a1, taskA1, yesterday, corestatus.TaskFailed, string(contract.OutcomeRejected))
	// B 校：词表外 drift 结果必须原样返回（漂移可见）。
	failInsertTask(t, e, b1, taskB1, today, corestatus.TaskFailed, "upstream_teapot")

	masterSess := e.login(master)
	adminASess := e.login(adminA)
	adminBothSess := e.login(adminBoth)
	adminEmptySess := e.login(adminEmpty)
	userSess := e.login(user)

	// 1. master 全量：A 校 3 行 + B 校 1 行 = 4。
	got, _ := failGet(t, e, "/api/sign-stats/failures?day="+today, masterSess, http.StatusOK)
	if got.Day.Format("2006-01-02") != today {
		t.Fatalf("day = %s, want %s", got.Day.Format("2006-01-02"), today)
	}
	if got.Total != 4 || failBucketSum(got.Items) != got.Total {
		t.Fatalf("master total=%d sum=%d items=%+v, want 4", got.Total, failBucketSum(got.Items), got.Items)
	}
	wantMaster := map[string]int{
		string(contract.OutcomeUnknown):       2,
		string(contract.OutcomeCredentialBad): 1,
		"upstream_teapot":                     1,
	}
	gotMap := failBucketMap(got.Items)
	if len(gotMap) != len(wantMaster) {
		t.Fatalf("master buckets = %v, want %v", gotMap, wantMaster)
	}
	for k, w := range wantMaster {
		if gotMap[k] != w {
			t.Fatalf("master bucket[%q] = %d, want %d", k, gotMap[k], w)
		}
	}
	if got.Scope.SchoolId != nil || got.Scope.TaskId != nil {
		t.Fatalf("master unfiltered scope = %+v, want empty", got.Scope)
	}

	// 2. master 按 school_id 与 task_id 过滤。
	gotB, _ := failGet(t, e, "/api/sign-stats/failures?day="+today+"&school_id="+schoolB.ID, masterSess, http.StatusOK)
	if gotB.Total != 1 || len(gotB.Items) != 1 || gotB.Items[0].Result != "upstream_teapot" || gotB.Items[0].Count != 1 {
		t.Fatalf("master schoolB = %+v, want upstream_teapot=1", gotB)
	}
	if gotB.Scope.SchoolId == nil || *gotB.Scope.SchoolId != schoolB.ID || gotB.Scope.TaskId != nil {
		t.Fatalf("master schoolB scope = %+v, want school_id=%s", gotB.Scope, schoolB.ID)
	}
	gotTaskA1, _ := failGet(t, e, "/api/sign-stats/failures?day="+today+"&school_id="+schoolA.ID+"&task_id="+taskA1, masterSess, http.StatusOK)
	if gotTaskA1.Total != 2 || failBucketSum(gotTaskA1.Items) != 2 {
		t.Fatalf("master taskA1 = %+v, want total 2", gotTaskA1)
	}
	if gotTaskA1.Scope.SchoolId == nil || *gotTaskA1.Scope.SchoolId != schoolA.ID ||
		gotTaskA1.Scope.TaskId == nil || *gotTaskA1.Scope.TaskId != taskA1 {
		t.Fatalf("master taskA1 scope = %+v", gotTaskA1.Scope)
	}
	gotOnlyTaskB1, _ := failGet(t, e, "/api/sign-stats/failures?day="+today+"&task_id="+taskB1, masterSess, http.StatusOK)
	if gotOnlyTaskB1.Total != 1 || gotOnlyTaskB1.Scope.SchoolId != nil ||
		gotOnlyTaskB1.Scope.TaskId == nil || *gotOnlyTaskB1.Scope.TaskId != taskB1 {
		t.Fatalf("master only taskB1 = %+v, want total=1 and scope.task_id only", gotOnlyTaskB1)
	}

	// 3. 空日必须返回 items=[]（非 null）与 total=0。
	gotEmpty, rawEmpty := failGet(t, e, "/api/sign-stats/failures?day="+emptyDay, masterSess, http.StatusOK)
	if gotEmpty.Total != 0 || len(gotEmpty.Items) != 0 {
		t.Fatalf("empty day = %+v, want total=0 items=[]", gotEmpty)
	}
	var rawMap map[string]json.RawMessage
	if err := json.Unmarshal([]byte(rawEmpty), &rawMap); err != nil {
		t.Fatalf("unmarshal raw empty: %v", err)
	}
	if strings.TrimSpace(string(rawMap["items"])) != "[]" {
		t.Fatalf("empty day raw items = %s, want []", rawMap["items"])
	}

	// 4. 缺省 day 与空查询串（?day=&school_id=&task_id=）使用今天并正常返回。
	gotDefault, _ := failGet(t, e, "/api/sign-stats/failures", masterSess, http.StatusOK)
	if gotDefault.Day.Format("2006-01-02") != today || gotDefault.Total != 4 {
		t.Fatalf("default day = %+v, want day=%s total=4", gotDefault, today)
	}
	gotEmptyParams, _ := failGet(t, e, "/api/sign-stats/failures?day=&school_id=&task_id=", masterSess, http.StatusOK)
	if gotEmptyParams.Day.Format("2006-01-02") != today || gotEmptyParams.Total != 4 {
		t.Fatalf("empty query params = %+v, want day=%s total=4", gotEmptyParams, today)
	}

	// 5. admin 只统计绑定学校；跨校 school_id/task_id 返回 404，不泄露存在性。
	gotAdminA, _ := failGet(t, e, "/api/sign-stats/failures?day="+today, adminASess, http.StatusOK)
	if gotAdminA.Total != 3 || failBucketSum(gotAdminA.Items) != 3 {
		t.Fatalf("adminA unfiltered = %+v, want total=3 (only school A)", gotAdminA)
	}
	if m := failBucketMap(gotAdminA.Items); m["upstream_teapot"] != 0 {
		t.Fatalf("adminA leaked school B bucket: %v", m)
	}
	gotAdminASchool, _ := failGet(t, e, "/api/sign-stats/failures?day="+today+"&school_id="+schoolA.ID, adminASess, http.StatusOK)
	if gotAdminASchool.Total != 3 {
		t.Fatalf("adminA schoolA = %+v, want total=3", gotAdminASchool)
	}
	_, crossSchoolBody := failGet(t, e, "/api/sign-stats/failures?day="+today+"&school_id="+schoolB.ID, adminASess, http.StatusNotFound)
	_, crossTaskBody := failGet(t, e, "/api/sign-stats/failures?day="+today+"&task_id="+taskB1, adminASess, http.StatusNotFound)
	_, crossSchoolOwnTaskBody := failGet(t, e, "/api/sign-stats/failures?day="+today+"&school_id="+schoolB.ID+"&task_id="+taskA1, adminASess, http.StatusNotFound)
	_, ownSchoolCrossTaskBody := failGet(t, e, "/api/sign-stats/failures?day="+today+"&school_id="+schoolA.ID+"&task_id="+taskB1, adminASess, http.StatusNotFound)

	// 无绑定 admin：不传过滤时 200 空集，指定任意学校 404。
	gotAdminEmpty, _ := failGet(t, e, "/api/sign-stats/failures?day="+today, adminEmptySess, http.StatusOK)
	if gotAdminEmpty.Total != 0 || len(gotAdminEmpty.Items) != 0 {
		t.Fatalf("unbound admin = %+v, want empty", gotAdminEmpty)
	}
	failGet(t, e, "/api/sign-stats/failures?day="+today+"&school_id="+schoolA.ID, adminEmptySess, http.StatusNotFound)

	// 同作用域内 school_id 与 task_id 不配套：400；不存在的 UUID：404 且与跨校 404 文案同形。
	failGet(t, e, "/api/sign-stats/failures?day="+today+"&school_id="+schoolA.ID+"&task_id="+taskB1, adminBothSess, http.StatusBadRequest)
	_, missingSchoolBody := failGet(t, e, "/api/sign-stats/failures?day="+today+"&school_id=01900000-0000-7000-8000-000000000999", masterSess, http.StatusNotFound)
	_, missingTaskBody := failGet(t, e, "/api/sign-stats/failures?day="+today+"&task_id=01900000-0000-7000-8000-000000000999", masterSess, http.StatusNotFound)
	csCode, csMsg := failErrorParts(t, crossSchoolBody)
	csotCode, csotMsg := failErrorParts(t, crossSchoolOwnTaskBody)
	msCode, msMsg := failErrorParts(t, missingSchoolBody)
	if csCode != msCode || csMsg != msMsg || csotCode != msCode || csotMsg != msMsg {
		t.Fatalf("school 404 shape mismatch: cross=(%s,%q) cross+ownTask=(%s,%q) missing=(%s,%q)",
			csCode, csMsg, csotCode, csotMsg, msCode, msMsg)
	}
	ctCode, ctMsg := failErrorParts(t, crossTaskBody)
	osctCode, osctMsg := failErrorParts(t, ownSchoolCrossTaskBody)
	mtCode, mtMsg := failErrorParts(t, missingTaskBody)
	if ctCode != mtCode || ctMsg != mtMsg || osctCode != mtCode || osctMsg != mtMsg {
		t.Fatalf("task 404 shape mismatch: cross=(%s,%q) own+crossTask=(%s,%q) missing=(%s,%q)",
			ctCode, ctMsg, osctCode, osctMsg, mtCode, mtMsg)
	}

	// 6. 负向用例：401 / 403 / 400。
	failGet(t, e, "/api/sign-stats/failures?day="+today, nil, http.StatusUnauthorized)
	failGet(t, e, "/api/sign-stats/failures?day="+today, userSess, http.StatusForbidden)
	failGet(t, e, "/api/sign-stats/failures?day=not-a-day", masterSess, http.StatusBadRequest)
	failGet(t, e, "/api/sign-stats/failures?day=2026-02-30", masterSess, http.StatusBadRequest)
	failGet(t, e, "/api/sign-stats/failures?school_id=not-a-uuid", masterSess, http.StatusBadRequest)
	failGet(t, e, "/api/sign-stats/failures?task_id=not-a-uuid", masterSess, http.StatusBadRequest)
}
