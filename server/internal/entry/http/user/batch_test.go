package user_test

// 用户批量动作端点的入口层判据。
//
// # 功能
// 钉住二次口令门形：缺失 400、错误 403、正确 200 且用例收到
// 动作、ids 与主体；未接线 500 失败关闭；失败零调用。
// 另钉响应形状：items 与请求 ids 同序，失败条带稳定码与消息，
// temp_password 只在 reset 成功项出现一次。
//
// # 口径
// 用例替身不执行业务；真库与真 HTTP 行为由 cmd/server 的 e2e 负责。
// 形状照 account 包的批量入口测试，两处互不引用。

import (
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/user"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	userhttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/user"
)

// batchFake 记录一次 Batch 调用的全部入参，并复述预设结果或错误。
type batchFake struct {
	fakeUserService
	calls   int
	scope   corescope.Scope
	action  string
	ids     []string
	actor   appaudit.Actor
	results []appuser.BatchItemResult
	err     error
}

func (f *batchFake) Batch(_ context.Context, scope corescope.Scope, action string, ids []string, actor appaudit.Actor) ([]appuser.BatchItemResult, error) {
	f.calls++
	f.scope = scope
	f.action = action
	f.ids = append([]string(nil), ids...)
	f.actor = actor
	if f.err != nil {
		return nil, f.err
	}
	return f.results, nil
}

// invokeBatch 在测试上下文里调用批量处理器，回状态码与响应体。
func invokeBatch(t *testing.T, h *userhttp.Handler, body string) (int, []byte) {
	t.Helper()
	code, raw, _ := callUserHandler(t, h, http.MethodPost, "/api/users/batch", body,
		func(h *userhttp.Handler, c *gin.Context) { h.Batch(c, oapi.UserBatchParams{}) })
	return code, []byte(raw)
}

// 缺 current_password：400，用例不被调用。
func TestUserBatchRequiresCurrentPassword(t *testing.T) {
	svc := &batchFake{}
	h := userhttp.NewHandler(svc, fakeUserAuthorizer{}).WithReauth(&recordingReauth{})
	code, _ := invokeBatch(t, h, `{"action":"disable","ids":["u1"]}`)
	if code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", code)
	}
	if svc.calls != 0 {
		t.Fatal("a missing password must not reach the use case")
	}
}

// 错误口令：403，用例不被调用。
func TestUserBatchRejectsWrongPassword(t *testing.T) {
	svc := &batchFake{}
	r := &recordingReauth{err: errs.New(errs.CodeForbidden, "reauthentication failed")}
	h := userhttp.NewHandler(svc, fakeUserAuthorizer{}).WithReauth(r)
	code, _ := invokeBatch(t, h, `{"action":"disable","ids":["u1"],"current_password":"wrong"}`)
	if code != http.StatusForbidden {
		t.Fatalf("status = %d, want 403", code)
	}
	if svc.calls != 0 {
		t.Fatal("a rejected reauth must not reach the use case")
	}
}

// 未注入 reauth：500 失败关闭，用例不被调用。
func TestUserBatchWithoutWiredReauthFailsClosed(t *testing.T) {
	svc := &batchFake{}
	h := userhttp.NewHandler(svc, fakeUserAuthorizer{})
	code, _ := invokeBatch(t, h, `{"action":"disable","ids":["u1"],"current_password":"pw"}`)
	if code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500", code)
	}
	if svc.calls != 0 {
		t.Fatal("unwired reauth must not reach the use case")
	}
}

// 正确口令：200，用例收到动作、ids 与会话主体。
func TestUserBatchPassesWithRightPassword(t *testing.T) {
	svc := &batchFake{results: []appuser.BatchItemResult{{ID: "u1"}}}
	h := userhttp.NewHandler(svc, fakeUserAuthorizer{}).WithReauth(&recordingReauth{})
	code, _ := invokeBatch(t, h,
		`{"action":"reset_password","ids":["u1","u2"],"current_password":"pw"}`)
	if code != http.StatusOK {
		t.Fatalf("status = %d, want 200", code)
	}
	if svc.calls != 1 {
		t.Fatalf("use case calls = %d, want 1", svc.calls)
	}
	if svc.action != "reset_password" || len(svc.ids) != 2 || svc.ids[0] != "u1" || svc.ids[1] != "u2" {
		t.Fatalf("use case got action=%q ids=%v", svc.action, svc.ids)
	}
	if svc.actor.UserID != "u1" {
		t.Fatalf("use case got actor=%+v, want the session principal", svc.actor)
	}
}

// 请求级校验错误按码映射：validation_failed 回 400。
func TestUserBatchTopLevelValidationError(t *testing.T) {
	svc := &batchFake{err: errs.New(errs.CodeValidation, "ids must not repeat an entry")}
	h := userhttp.NewHandler(svc, fakeUserAuthorizer{}).WithReauth(&recordingReauth{})
	code, _ := invokeBatch(t, h, `{"action":"disable","ids":["u1","u1"],"current_password":"pw"}`)
	if code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", code)
	}
}

// batchItemShape 回显条目的 id|status|code 形状，供失败信息使用：
// 不回显 temp_password 与 message，避免一次性口令进测试日志。
// 与 cmd/server e2e 的 userBatchItemShape 同口径，两处互不引用。
type batchItemShape struct {
	ID           string  `json:"id"`
	Status       string  `json:"status"`
	Code         *string `json:"code"`
	Message      *string `json:"message"`
	TempPassword *string `json:"temp_password"`
}

func (s batchItemShape) String() string {
	out := s.ID + "|" + s.Status
	if s.Code != nil {
		out += "|" + *s.Code
	}
	return out
}

// 响应形状：items 与请求 ids 同序；ok 与 error 各按其形状输出；
// temp_password 只在 reset 成功项出现一次。失败信息只回显 id|status|code。
func TestUserBatchResponseShape(t *testing.T) {
	svc := &batchFake{results: []appuser.BatchItemResult{
		{ID: "u1"},
		{ID: "u2", TempPassword: "one-time-temp-42"},
		{ID: "u3", Code: errs.CodeConflict, Message: "the last active master cannot be demoted or disabled"},
	}}
	h := userhttp.NewHandler(svc, fakeUserAuthorizer{}).WithReauth(&recordingReauth{})
	code, payload := invokeBatch(t, h, `{"action":"reset_password","ids":["u1","u2","u3"],"current_password":"pw"}`)
	if code != http.StatusOK {
		t.Fatalf("status = %d, want 200", code)
	}
	if got := strings.Count(string(payload), "temp_password"); got != 1 {
		t.Fatalf("the temp_password key appears %d times, want exactly once", got)
	}
	// 口令值本身也只出现一次（与 e2e 同口径）：值是唯一的一次性展示。
	var body struct {
		Items []batchItemShape `json:"items"`
	}
	if err := json.Unmarshal(payload, &body); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	if len(body.Items) != 3 {
		t.Fatalf("items = %d, want 3 entries", len(body.Items))
	}
	if body.Items[1].TempPassword == nil {
		t.Fatal("items[1] must carry a temp password")
	}
	if got := strings.Count(string(payload), *body.Items[1].TempPassword); got != 1 {
		t.Fatalf("the temp secret value appears %d times in the body, want exactly once", got)
	}
	if body.Items[0].ID != "u1" || body.Items[0].Status != "ok" || body.Items[0].Code != nil || body.Items[0].TempPassword != nil {
		t.Fatalf("items[0] = %s, want ok without code or temp password", body.Items[0])
	}
	if body.Items[1].ID != "u2" || body.Items[1].Status != "ok" {
		t.Fatalf("items[1] = %s, want ok", body.Items[1])
	}
	if body.Items[1].TempPassword == nil || *body.Items[1].TempPassword != "one-time-temp-42" {
		t.Fatalf("items[1] = %s, want a non-empty one-time secret", body.Items[1])
	}
	if body.Items[2].ID != "u3" || body.Items[2].Status != "error" {
		t.Fatalf("items[2] = %s, want error", body.Items[2])
	}
	if body.Items[2].Code == nil || *body.Items[2].Code != "conflict" {
		t.Fatalf("items[2] = %s, want conflict", body.Items[2])
	}
	if body.Items[2].Message == nil || *body.Items[2].Message == "" {
		t.Fatalf("items[2] = %s, want a user-safe message", body.Items[2])
	}
	if body.Items[2].TempPassword != nil {
		t.Fatal("an errored item must not carry a temp password")
	}
}
