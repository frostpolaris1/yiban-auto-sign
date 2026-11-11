package main

// 批量签到端点的端到端测试。真库、真 HTTP、真 cookie。
//
// # 功能
// 钉住 POST /api/accounts/batch/sign 的验收链：两个可签账号全 ok 且
// 任务行存在、混合成败按序给出 per-item 稳定码（already_signed /
// window_closed / conflict / not_found）、非 active per-item conflict、
// admin 绑定校 ok 跨校 not_found、非 UUID per-item validation_failed
// （与单条同口径）、账号键限流真触发 per-item rate_limited 且不拖垮
// 整批、请求级校验 400、user 整批 403。
// 本文件**不**复制入口层替身单测。
//
// # 归属
// cmd/server 的 e2e。复用 w2cEnv 的 schema 隔离、HTTP 客户端与
// p4_e2e_test.go 的建校/建账号/开窗 helpers。
//
// # 口径
// 缺 DSN 时整包跳过并打可见横幅。跳过不是绿。
// 手机号只取合成值（138 段测试专用号段）；窗口发现由 e2eDiscoverer
// 预置，不发平台请求。成功项只入队（202 语义），不等待执行。

import (
	"net/http"
	"strings"
	"testing"
	"time"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// p4BatchMissingID 是一个形状合法但不存在的账号 id（零前缀合成段）。
const p4BatchMissingID = "00000000-0000-4000-8000-0000000000b1"

// p4BatchItem 是批量签到响应里用到的字段。
type p4BatchItem struct {
	ID      string  `json:"id"`
	Status  string  `json:"status"`
	Code    *string `json:"code"`
	TaskID  *string `json:"task_id"`
	Message *string `json:"message"`
}

// p4BatchItemShape 回显条目的 id|status|code 形状，供失败信息使用。
func p4BatchItemShape(item p4BatchItem) string {
	shape := item.ID + "|" + item.Status
	if item.Code != nil {
		shape += "|" + *item.Code
	}
	if item.TaskID != nil {
		shape += "|task"
	}
	return shape
}

// p4BatchDo 发一次批量签到请求并解出 items；非 2xx 由调用方按码断言，
// 解码用的 items 仅在 200 响应上有意义。
func p4BatchDo(t *testing.T, e *w2cEnv, issued *coreauth.Issued, ids []string) (int, []p4BatchItem, string) {
	t.Helper()
	rec := e.do("POST", "/api/accounts/batch/sign", map[string]any{"ids": ids}, issued)
	var result struct {
		Items []p4BatchItem `json:"items"`
	}
	if rec.Code == http.StatusOK {
		e.decode(rec, &result)
	}
	return rec.Code, result.Items, rec.Body.String()
}

// p4BatchTaskState 读批量返回的任务行状态；行缺失会响亮失败。
func p4BatchTaskState(t *testing.T, e *w2cEnv, taskID string) string {
	t.Helper()
	return e.scalarString("SELECT state::text FROM sign_tasks WHERE id = $1::uuid", taskID)
}

// 批量签到的全链路结论：六个验收共用一个环境。
func TestP4BatchSign(t *testing.T) {
	e := w2cNewEnv(t)
	now := clock.System{}.Now()
	day := clock.BusinessDay(now, p4Location(t))
	master := e.user("p4-batch-master@example.com", coreauth.RoleMaster)
	masterIssued := e.login(master)
	admin := e.user("p4-batch-admin@example.com", coreauth.RoleAdmin)
	adminIssued := e.login(admin)
	user := e.user("p4-batch-user@example.com", coreauth.RoleUser)
	userIssued := e.login(user)

	// 开窗学校：三个可签账号 + 一个已成功账号 + 一个非 active 账号。
	openSchool := e.school("p4-batch-open")
	openTask := e.schoolTask(openSchool.ID, "晨签")
	openWin := coreschedule.WindowRecord{
		SchoolTaskID: openTask, Day: day,
		Start: now.Add(-10 * time.Minute), End: now.Add(10 * time.Minute),
		Source: string(contract.WindowPlatform),
	}
	if _, err := e.sched.PutWindow(e.ctx, openWin, 0); err != nil {
		t.Fatalf("put open window: %v", err)
	}
	okA := e.account(openSchool.ID, master.ID, "13800009601")
	okB := e.account(openSchool.ID, master.ID, "13800009602")
	okC := e.account(openSchool.ID, master.ID, "13800009607")
	e.activateAccount(okA)
	e.activateAccount(okB)
	e.activateAccount(okC)

	// 已成功账号：先单条入队，再认领落 success（模拟今天已签）。
	signedAcct := e.account(openSchool.ID, master.ID, "13800009603")
	e.activateAccount(signedAcct)
	rec := e.do("POST", "/api/accounts/"+signedAcct+"/sign", nil, masterIssued)
	if rec.Code != http.StatusAccepted {
		t.Fatalf("预置单条签到 status = %d: %s", rec.Code, rec.Body.String())
	}
	seq, err := e.sched.ClaimPending(e.ctx, signedAcct, openTask, day, now)
	if err != nil {
		t.Fatalf("claim preset row: %v", err)
	}
	if err := e.sched.Finish(e.ctx, signedAcct, openTask, day, corestatus.TaskSuccess,
		string(contract.OutcomeSuccess), seq, now); err != nil {
		t.Fatalf("finish preset row: %v", err)
	}

	// 非 active 账号：pending，未激活。
	inactiveAcct := e.account(openSchool.ID, master.ID, "13800009605")

	// 关窗学校：窗口已结束。
	closedSchool := e.school("p4-batch-closed")
	closedTask := e.schoolTask(closedSchool.ID, "晨签")
	closedAcct := e.account(closedSchool.ID, master.ID, "13800009604")
	e.activateAccount(closedAcct)
	if _, err := e.sched.PutWindow(e.ctx, coreschedule.WindowRecord{
		SchoolTaskID: closedTask, Day: day,
		Start: now.Add(-2 * time.Hour), End: now.Add(-time.Minute),
		Source: string(contract.WindowPlatform),
	}, 0); err != nil {
		t.Fatalf("put closed window: %v", err)
	}

	// admin 绑定校：绑定自己的学校并开窗。
	adminSchool := e.school("p4-batch-admin")
	adminTask := e.schoolTask(adminSchool.ID, "晨签")
	adminAcct := e.account(adminSchool.ID, master.ID, "13800009606")
	e.activateAccount(adminAcct)
	e.bind(admin.ID, adminSchool.ID)
	if _, err := e.sched.PutWindow(e.ctx, coreschedule.WindowRecord{
		SchoolTaskID: adminTask, Day: day,
		Start: now.Add(-10 * time.Minute), End: now.Add(10 * time.Minute),
		Source: string(contract.WindowPlatform),
	}, 0); err != nil {
		t.Fatalf("put admin window: %v", err)
	}

	// 验收⑥：user 整批 403，一条都不入队。
	code, items, body := p4BatchDo(t, e, userIssued, []string{okA})
	if code != http.StatusForbidden {
		t.Fatalf("user 批量 status = %d, want 403: %s", code, body)
	}
	if len(items) != 0 {
		t.Fatalf("403 响应不得携带 items")
	}
	if n := e.scalarInt("SELECT count(*) FROM sign_tasks WHERE account_id = $1::uuid", okA); n != 0 {
		t.Fatalf("user 批量落库 %d 行, want 0", n)
	}

	// 验收⑤：11 个不同 id、重复、空 → 400，一条都不跑。
	eleven := []string{okA, okB, okC, signedAcct, closedAcct, inactiveAcct, adminAcct,
		p4BatchMissingID, p4BatchMissingID + "2", p4BatchMissingID + "3", p4BatchMissingID + "4"}
	for name, ids := range map[string][]string{
		"11个":   eleven,
		"重复":    {okA, okB, okA},
		"大小写重复": {okA, strings.ToUpper(okA)},
		"空":     {},
	} {
		code, _, body := p4BatchDo(t, e, masterIssued, ids)
		if code != http.StatusBadRequest {
			t.Fatalf("%s: status = %d, want 400: %s", name, code, body)
		}
		if !strings.Contains(body, "validation_failed") {
			t.Fatalf("%s: body = %s, want validation_failed", name, body)
		}
	}
	if n := e.scalarInt("SELECT count(*) FROM sign_tasks WHERE account_id = ANY($1::uuid[])",
		[]string{okA, okB, okC}); n != 0 {
		t.Fatalf("校验失败落库 %d 行, want 0", n)
	}

	// 验收①：两个可签账号批量，两项 ok、task_id 非空，任务行存在。
	code, first, body := p4BatchDo(t, e, masterIssued, []string{okA, okB})
	if code != http.StatusOK {
		t.Fatalf("批量可签 status = %d: %s", code, body)
	}
	if len(first) != 2 {
		t.Fatalf("items = %d, want 2", len(first))
	}
	for i, item := range first {
		if item.Status != "ok" || item.TaskID == nil || *item.TaskID == "" {
			t.Fatalf("items[%d] = %s, want ok with task_id", i, p4BatchItemShape(item))
		}
		if item.Code != nil || item.Message != nil {
			t.Fatalf("成功项不得带 code/message: %s", p4BatchItemShape(item))
		}
		if first[0].TaskID != nil && first[1].TaskID != nil && *first[0].TaskID == *first[1].TaskID {
			t.Fatalf("两项不得共用任务行: %s", *first[0].TaskID)
		}
		if state := p4BatchTaskState(t, e, *item.TaskID); state != string(corestatus.TaskPending) {
			t.Fatalf("items[%d] 任务状态 = %q, want pending", i, state)
		}
	}

	// 验收②：混合批次按序给出逐项结论；正常账号任务行仍创建。
	code, mixed, body := p4BatchDo(t, e, masterIssued, []string{signedAcct, closedAcct, okC})
	if code != http.StatusOK {
		t.Fatalf("混合批量 status = %d: %s", code, body)
	}
	if len(mixed) != 3 {
		t.Fatalf("items = %d, want 3", len(mixed))
	}
	if mixed[0].Code == nil || *mixed[0].Code != "already_signed" || mixed[0].TaskID != nil {
		t.Fatalf("items[0] = %s, want error already_signed without task_id", p4BatchItemShape(mixed[0]))
	}
	if mixed[1].Code == nil || *mixed[1].Code != "window_closed" || mixed[1].TaskID != nil {
		t.Fatalf("items[1] = %s, want error window_closed without task_id", p4BatchItemShape(mixed[1]))
	}
	if mixed[2].Status != "ok" || mixed[2].TaskID == nil || *mixed[2].TaskID == "" {
		t.Fatalf("items[2] = %s, want ok with task_id", p4BatchItemShape(mixed[2]))
	}
	if state := p4BatchTaskState(t, e, *mixed[2].TaskID); state != string(corestatus.TaskPending) {
		t.Fatalf("正常账号任务状态 = %q, want pending", state)
	}

	// 验收③：非 active 账号 per-item conflict，任务行不落库。
	code, inactive, body := p4BatchDo(t, e, masterIssued, []string{inactiveAcct})
	if code != http.StatusOK {
		t.Fatalf("非 active 批量 status = %d: %s", code, body)
	}
	if len(inactive) != 1 {
		t.Fatalf("items = %d, want 1", len(inactive))
	}
	if inactive[0].Code == nil || *inactive[0].Code != "conflict" || inactive[0].TaskID != nil {
		t.Fatalf("items[0] = %s, want error conflict without task_id", p4BatchItemShape(inactive[0]))
	}
	if n := e.scalarInt("SELECT count(*) FROM sign_tasks WHERE account_id = $1::uuid", inactiveAcct); n != 0 {
		t.Fatalf("非 active 落库 %d 行, want 0", n)
	}

	// 验收④：admin 绑定校 ok、跨校 per-item not_found。
	code, adminItems, body := p4BatchDo(t, e, adminIssued, []string{adminAcct, okA})
	if code != http.StatusOK {
		t.Fatalf("admin 批量 status = %d: %s", code, body)
	}
	if len(adminItems) != 2 {
		t.Fatalf("items = %d, want 2", len(adminItems))
	}
	if adminItems[0].Status != "ok" || adminItems[0].TaskID == nil {
		t.Fatalf("items[0] = %s, want ok with task_id", p4BatchItemShape(adminItems[0]))
	}
	if adminItems[1].Code == nil || *adminItems[1].Code != "not_found" || adminItems[1].TaskID != nil {
		t.Fatalf("items[1] = %s, want error not_found without task_id", p4BatchItemShape(adminItems[1]))
	}
	if state := p4BatchTaskState(t, e, *adminItems[0].TaskID); state != string(corestatus.TaskPending) {
		t.Fatalf("admin 账号任务状态 = %q, want pending", state)
	}

	// 非 UUID id 与单条同口径：per-item validation_failed，整批仍 200。
	code, malformed, body := p4BatchDo(t, e, masterIssued, []string{"not-a-uuid"})
	if code != http.StatusOK {
		t.Fatalf("非 UUID 批量 status = %d: %s", code, body)
	}
	if len(malformed) != 1 || malformed[0].Code == nil || *malformed[0].Code != "validation_failed" {
		t.Fatalf("items = %v, want per-item validation_failed", malformed)
	}

	// 验收⑦：账号键限流真触发——同账号连打吃满 burst（5/min 档），
	// 第 6 次整批仍 200：命中项 rate_limited，同批另一账号照常入队。
	limitAcct := e.account(openSchool.ID, master.ID, "13800009608")
	e.activateAccount(limitAcct)
	for i := 0; i < 5; i++ {
		code, warm, body := p4BatchDo(t, e, masterIssued, []string{limitAcct})
		if code != http.StatusOK || len(warm) != 1 || warm[0].Status != "ok" || warm[0].TaskID == nil {
			t.Fatalf("限流预热第 %d 次 = %d %s", i+1, code, body)
		}
	}
	code, limited, body := p4BatchDo(t, e, masterIssued, []string{limitAcct, okA})
	if code != http.StatusOK {
		t.Fatalf("限流命中批 status = %d: %s", code, body)
	}
	if limited[0].Code == nil || *limited[0].Code != "rate_limited" || limited[0].TaskID != nil {
		t.Fatalf("items[0] = %s, want error rate_limited without task_id", p4BatchItemShape(limited[0]))
	}
	if limited[1].Status != "ok" || limited[1].TaskID == nil {
		t.Fatalf("items[1] = %s, want ok（限流命中不拖垮整批）", p4BatchItemShape(limited[1]))
	}
	if state := p4BatchTaskState(t, e, *limited[1].TaskID); state != string(corestatus.TaskPending) {
		t.Fatalf("同批账号任务状态 = %q, want pending", state)
	}
	// 命中项不入队：limitAcct 只有预热吃满前入队的那一行。
	if n := e.scalarInt("SELECT count(*) FROM sign_tasks WHERE account_id = $1::uuid", limitAcct); n != 1 {
		t.Fatalf("限流账号任务行 = %d, want 1", n)
	}
}
