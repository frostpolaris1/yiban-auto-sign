package account_test

// 批量动作端点的入口层判据。
//
// # 功能
// 钉住二次口令门形：缺失 400、错误 403、正确 200 且用例收到
// 动作、ids、理由与主体；未接线 500 失败关闭；失败零调用。
// 另钉响应形状：items 与请求 ids 同序，失败条带稳定码与消息。
//
// # 口径
// 用例替身不执行业务；真库与真 HTTP 行为由 cmd/server 的 e2e 负责。

import (
	"context"
	"encoding/json"
	"net/http"
	"testing"

	appaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/account"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	accounthttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/account"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
)

// batchFake 记录一次 Batch 调用的全部入参，并复述预设结果或错误。
type batchFake struct {
	fakeService
	calls   int
	scope   corescope.Scope
	action  string
	ids     []string
	reason  string
	actor   appaudit.Actor
	results []appaccount.BatchItemResult
	err     error
}

func (f *batchFake) Batch(_ context.Context, scope corescope.Scope, action string, ids []string, reason string, actor appaudit.Actor) ([]appaccount.BatchItemResult, error) {
	f.calls++
	f.scope = scope
	f.action = action
	f.ids = append([]string(nil), ids...)
	f.reason = reason
	f.actor = actor
	if f.err != nil {
		return nil, f.err
	}
	return f.results, nil
}

// invokeBatch 在测试上下文里调用批量处理器，回状态码与响应体。
func invokeBatch(t *testing.T, h *accounthttp.Handler, body string) (int, []byte) {
	t.Helper()
	c, rec := newContext(t, http.MethodPost, "/api/accounts/batch", body)
	h.Batch(c, oapi.AccountBatchParams{})
	return c.Writer.Status(), rec.Body.Bytes()
}

// 缺 current_password：400，用例不被调用。
func TestBatchRequiresCurrentPassword(t *testing.T) {
	svc := &batchFake{}
	h := accounthttp.NewHandler(svc, fakeAuthorizer{}).WithReauth(&recordingReauth{})
	code, _ := invokeBatch(t, h, `{"action":"pause","ids":["a1"]}`)
	if code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", code)
	}
	if svc.calls != 0 {
		t.Fatal("a missing password must not reach the use case")
	}
}

// 错误口令：403，用例不被调用。
func TestBatchRejectsWrongPassword(t *testing.T) {
	svc := &batchFake{}
	r := &recordingReauth{err: errs.New(errs.CodeForbidden, "reauthentication failed")}
	h := accounthttp.NewHandler(svc, fakeAuthorizer{}).WithReauth(r)
	code, _ := invokeBatch(t, h, `{"action":"pause","ids":["a1"],"current_password":"wrong"}`)
	if code != http.StatusForbidden {
		t.Fatalf("status = %d, want 403", code)
	}
	if svc.calls != 0 {
		t.Fatal("a rejected reauth must not reach the use case")
	}
}

// 未注入 reauth：500 失败关闭，用例不被调用。
func TestBatchWithoutWiredReauthFailsClosed(t *testing.T) {
	svc := &batchFake{}
	h := accounthttp.NewHandler(svc, fakeAuthorizer{})
	code, _ := invokeBatch(t, h, `{"action":"pause","ids":["a1"],"current_password":"pw"}`)
	if code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500", code)
	}
	if svc.calls != 0 {
		t.Fatal("unwired reauth must not reach the use case")
	}
}

// 正确口令：200，用例收到动作、ids、理由与会话主体。
func TestBatchPassesWithRightPassword(t *testing.T) {
	svc := &batchFake{results: []appaccount.BatchItemResult{{ID: "a1"}}}
	h := accounthttp.NewHandler(svc, fakeAuthorizer{}).WithReauth(&recordingReauth{})
	code, _ := invokeBatch(t, h,
		`{"action":"review_reject","ids":["a1","a2"],"reason":"未绑定设备","current_password":"pw"}`)
	if code != http.StatusOK {
		t.Fatalf("status = %d, want 200", code)
	}
	if svc.calls != 1 {
		t.Fatalf("use case calls = %d, want 1", svc.calls)
	}
	if svc.action != "review_reject" || len(svc.ids) != 2 || svc.ids[0] != "a1" || svc.ids[1] != "a2" {
		t.Fatalf("use case got action=%q ids=%v", svc.action, svc.ids)
	}
	if svc.reason != "未绑定设备" {
		t.Fatalf("use case got reason=%q", svc.reason)
	}
	if svc.actor.UserID != "u1" {
		t.Fatalf("use case got actor=%+v, want the session principal", svc.actor)
	}
}

// 请求级校验错误按码映射：validation_failed 回 400。
func TestBatchTopLevelValidationError(t *testing.T) {
	svc := &batchFake{err: errs.New(errs.CodeValidation, "ids must not repeat an entry")}
	h := accounthttp.NewHandler(svc, fakeAuthorizer{}).WithReauth(&recordingReauth{})
	code, _ := invokeBatch(t, h, `{"action":"pause","ids":["a1","a1"],"current_password":"pw"}`)
	if code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", code)
	}
}

// 响应形状：items 与请求 ids 同序；ok 与 error 各按其形状输出。
func TestBatchResponseShape(t *testing.T) {
	svc := &batchFake{results: []appaccount.BatchItemResult{
		{ID: "a1"},
		{ID: "a2", Code: errs.CodeConflict, Message: "account status does not allow this"},
		{ID: "a3"},
	}}
	h := accounthttp.NewHandler(svc, fakeAuthorizer{}).WithReauth(&recordingReauth{})
	code, payload := invokeBatch(t, h, `{"action":"pause","ids":["a1","a2","a3"],"current_password":"pw"}`)
	if code != http.StatusOK {
		t.Fatalf("status = %d, want 200", code)
	}
	var body struct {
		Items []struct {
			ID      string  `json:"id"`
			Status  string  `json:"status"`
			Code    *string `json:"code"`
			Message *string `json:"message"`
		} `json:"items"`
	}
	if err := json.Unmarshal(payload, &body); err != nil {
		t.Fatalf("decode response: %v; body=%q", err, payload)
	}
	if len(body.Items) != 3 {
		t.Fatalf("items = %+v, want 3 entries", body.Items)
	}
	if body.Items[0].ID != "a1" || body.Items[0].Status != "ok" || body.Items[0].Code != nil {
		t.Fatalf("items[0] = %+v, want ok without code", body.Items[0])
	}
	if body.Items[1].ID != "a2" || body.Items[1].Status != "error" {
		t.Fatalf("items[1] = %+v, want error", body.Items[1])
	}
	if body.Items[1].Code == nil || *body.Items[1].Code != "conflict" {
		t.Fatalf("items[1] code = %+v, want conflict", body.Items[1].Code)
	}
	if body.Items[1].Message == nil || *body.Items[1].Message == "" {
		t.Fatalf("items[1] message = %+v, want user-safe text", body.Items[1].Message)
	}
	if body.Items[2].ID != "a3" || body.Items[2].Status != "ok" {
		t.Fatalf("items[2] = %+v, want ok", body.Items[2])
	}
}
