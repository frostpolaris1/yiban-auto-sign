package main

// R4B-CBL e2e：入口与契约偏差。
//
// # 功能
// 用真实 PostgreSQL 与路由直接发请求，钉住六条用户可见结果：
// 自选时间片缺 offset_min 回 400 且不清空已存值；可空字段输出 null 键；
// 超大请求体回 413；列表 limit=0 回 400；换校回 409；
// 未知字段按豁免忽略。
//
// # 归属
// cmd/server 的 e2e。复用 w2cEnv 的 schema 隔离与 HTTP 客户端。
//
// # 口径
// 模拟学生设定自选时间片与定位、翻页、上传超大图片；
// 其余条目模拟管理员读列表与改账号。
// 缺 DSN 时打可见横幅再跳过。

import (
	"bytes"
	"encoding/json"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
)

// r4bJSON 解出响应体对象。
func r4bJSON(t *testing.T, body []byte) map[string]json.RawMessage {
	t.Helper()
	var obj map[string]json.RawMessage
	if err := json.Unmarshal(body, &obj); err != nil {
		t.Fatalf("decode object: %v; body=%q", err, body)
	}
	return obj
}

// r4bWantNull 断言这些键存在且值为 null。
func r4bWantNull(t *testing.T, obj map[string]json.RawMessage, keys ...string) {
	t.Helper()
	for _, key := range keys {
		raw, ok := obj[key]
		if !ok {
			names := make([]string, 0, len(obj))
			for name := range obj {
				names = append(names, name)
			}
			t.Fatalf("响应缺键 %q；实际键=%v", key, names)
		}
		if got := strings.TrimSpace(string(raw)); got != "null" {
			t.Fatalf("键 %q = %s, want null", key, got)
		}
	}
}

// 自选时间片：offset_min 是必填键。缺键与显式 null 是两种语义，
// 缺键不得当成「清除」；冷却期内也必须是 400（验键先于用例）。
func TestR4BCblTimePrefRequiresOffsetKey(t *testing.T) {
	e := w2cNewEnv(t)
	user := e.user("r4b-pref@example.com", coreauth.RoleUser)
	issued := e.login(user)

	rec := e.do(http.MethodPut, "/api/me/time-pref", map[string]any{"offset_min": 30}, issued)
	w2cWantStatus(t, rec, http.StatusOK)

	// 缺键：400 validation_failed。
	rec = e.do(http.MethodPut, "/api/me/time-pref", map[string]any{}, issued)
	w2cWantStatus(t, rec, http.StatusBadRequest)
	if code := e.errorCode(rec); code != "validation_failed" {
		t.Fatalf("缺键 code = %q, want validation_failed", code)
	}

	// 已存值不得被清除。
	rec = e.do(http.MethodGet, "/api/me/time-pref", nil, issued)
	w2cWantStatus(t, rec, http.StatusOK)
	var pref oapi.TimePref
	e.decode(rec, &pref)
	if pref.OffsetMin == nil || *pref.OffsetMin != 30 {
		t.Fatalf("缺键请求改动了已存值: %+v", pref)
	}

	// 显式 null 合法：清除偏好。
	other := e.user("r4b-pref-null@example.com", coreauth.RoleUser)
	otherIssued := e.login(other)
	rec = e.do(http.MethodPut, "/api/me/time-pref", map[string]any{"offset_min": nil}, otherIssued)
	w2cWantStatus(t, rec, http.StatusOK)
	e.decode(rec, &pref)
	if pref.OffsetMin != nil {
		t.Fatalf("显式 null 后 offset = %v, want nil", *pref.OffsetMin)
	}
}

// 契约把可空字段写成 null。缺键与 null 对客户端是两种形状：
// 生成物靠 required 去掉 omitempty，故这里逐条检查真实响应。
func TestR4BCblNullKeysPresent(t *testing.T) {
	e := w2cNewEnv(t)
	master := e.user("r4b-null-master@example.com", coreauth.RoleMaster)
	masterIssued := e.login(master)
	owner := e.user("r4b-null-owner@example.com", coreauth.RoleUser)
	ownerIssued := e.login(owner)

	school := e.school("r4b-null")
	e.schoolTask(school.ID, "晨签")
	acct := e.account(school.ID, owner.ID, "13800071001")
	e.activateAccount(acct)

	// 定位 auto：lng/lat 输出 null。
	rec := e.do(http.MethodPut, "/api/accounts/"+acct+"/location",
		map[string]any{"mode": "auto"}, ownerIssued)
	w2cWantStatus(t, rec, http.StatusOK)
	r4bWantNull(t, r4bJSON(t, rec.Body.Bytes()), "lng", "lat")

	// 未缓存窗口：start/end/source 输出 null。
	rec = e.do(http.MethodGet, "/api/schedule/windows", nil, masterIssued)
	w2cWantStatus(t, rec, http.StatusOK)
	var page map[string]json.RawMessage
	if err := json.Unmarshal(rec.Body.Bytes(), &page); err != nil {
		t.Fatalf("decode window page: %v", err)
	}
	var windows []map[string]json.RawMessage
	if err := json.Unmarshal(page["items"], &windows); err != nil {
		t.Fatalf("decode window items: %v", err)
	}
	if len(windows) != 1 || windows[0]["task_id"] == nil {
		t.Fatalf("window items = %v", windows)
	}
	r4bWantNull(t, windows[0], "start", "end", "source")

	// 诊断：无当日任务行时 state/run_at/next_retry_at 输出 null。
	rec = e.do(http.MethodGet, "/api/me/diagnostics", nil, ownerIssued)
	w2cWantStatus(t, rec, http.StatusOK)
	var report struct {
		Items []map[string]json.RawMessage `json:"items"`
	}
	e.decode(rec, &report)
	if len(report.Items) != 1 {
		t.Fatalf("diagnostics items = %d, want 1", len(report.Items))
	}
	r4bWantNull(t, report.Items[0], "state", "run_at", "next_retry_at")

	// 单账号详版：window/egress 输出 null。
	rec = e.do(http.MethodGet, "/api/accounts/"+acct+"/diagnostics", nil, ownerIssued)
	w2cWantStatus(t, rec, http.StatusOK)
	r4bWantNull(t, r4bJSON(t, rec.Body.Bytes()), "window", "egress")

	// 轮询状态：state/run_at 输出 null。
	rec = e.do(http.MethodGet, "/api/me/sign-status", nil, ownerIssued)
	w2cWantStatus(t, rec, http.StatusOK)
	var status struct {
		Items []map[string]json.RawMessage `json:"items"`
	}
	e.decode(rec, &status)
	if len(status.Items) != 1 {
		t.Fatalf("sign-status items = %d, want 1", len(status.Items))
	}
	r4bWantNull(t, status.Items[0], "state", "run_at")

	// 账号：未审核时 reviewed_at/reviewed_by 输出 null。
	rec = e.do(http.MethodGet, "/api/accounts/"+acct, nil, ownerIssued)
	w2cWantStatus(t, rec, http.StatusOK)
	r4bWantNull(t, r4bJSON(t, rec.Body.Bytes()), "reviewed_at", "reviewed_by")

	// 用户：从未登录时 last_login_at 输出 null。
	rec = e.do(http.MethodPost, "/api/users", map[string]any{
		"email": "r4b-null-new@example.com", "password": "correct-horse", "role": "user",
	}, masterIssued)
	w2cWantStatus(t, rec, http.StatusCreated)
	r4bWantNull(t, r4bJSON(t, rec.Body.Bytes()), "last_login_at")
}

// doRaw 发原始请求体，服务 multipart 用例。
func (e *w2cEnv) doRaw(method, path string, body []byte, contentType string,
	issued *coreauth.Issued) *httptest.ResponseRecorder {
	e.t.Helper()
	req := httptest.NewRequest(method, path, bytes.NewReader(body))
	req.Header.Set("Content-Type", contentType)
	if issued != nil {
		req.AddCookie(&http.Cookie{Name: coreauth.InsecureCookieName, Value: issued.Token})
		req.Header.Set("X-CSRF-Token", issued.CSRFToken)
	}
	rec := httptest.NewRecorder()
	e.engine.ServeHTTP(rec, req)
	e.bodies = append(e.bodies, rec.Body.String())
	return rec
}

// 超大请求体一律 413：绑定失败要能区分「格式错」与「体积超界」。
// 四个 JSON 端点与 multipart 上传共用同一口径。
func TestR4BCblOversizedBodiesAre413(t *testing.T) {
	e := w2cNewEnv(t)
	master := e.user("r4b-big-master@example.com", coreauth.RoleMaster)
	masterIssued := e.login(master)
	owner := e.user("r4b-big-owner@example.com", coreauth.RoleUser)
	ownerIssued := e.login(owner)
	huge := strings.Repeat("x", 2<<20)

	cases := []struct {
		name   string
		method string
		path   string
		body   any
		issued *coreauth.Issued
	}{
		{"photo-retention", http.MethodPut, "/api/me/photo-retention",
			map[string]any{"days": huge}, ownerIssued},
		{"schedule-plan", http.MethodPost, "/api/schedule/plan",
			map[string]any{"day": huge, "school_id": "x", "task_id": "y"}, masterIssued},
		{"schedule-override", http.MethodPost, "/api/schedule/override",
			map[string]any{"day": huge, "school_id": "x", "task_id": "y",
				"start": "2026-01-01T00:00:00Z", "end": "2026-01-01T01:00:00Z",
				"reason": "r"}, masterIssued},
		{"time-pref", http.MethodPut, "/api/me/time-pref",
			map[string]any{"offset_min": huge}, ownerIssued},
	}
	for _, tc := range cases {
		rec := e.do(tc.method, tc.path, tc.body, tc.issued)
		if rec.Code != http.StatusRequestEntityTooLarge {
			t.Fatalf("%s 超大请求体 = %d, want 413; body=%q",
				tc.name, rec.Code, rec.Body.String())
		}
		if code := e.errorCode(rec); code != "validation_failed" {
			t.Fatalf("%s 413 code = %q, want validation_failed", tc.name, code)
		}
	}

	// multipart 上传：请求体超过路由上限（11 MiB）回 413。
	var buf bytes.Buffer
	writer := multipart.NewWriter(&buf)
	part, err := writer.CreateFormFile("image", "big.jpg")
	if err != nil {
		t.Fatalf("create form file: %v", err)
	}
	if _, err := part.Write(bytes.Repeat([]byte("x"), 12<<20)); err != nil {
		t.Fatalf("write form file: %v", err)
	}
	if err := writer.Close(); err != nil {
		t.Fatalf("close multipart writer: %v", err)
	}
	rec := e.doRaw(http.MethodPost, "/api/me/photos", buf.Bytes(), writer.FormDataContentType(), ownerIssued)
	if rec.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("超大上传 = %d, want 413; body=%q", rec.Code, rec.Body.String())
	}
}

// limit 的下界是 1。0 不得静默取缺省：同一参数在四个列表上必须同口径。
func TestR4BCblListLimitZeroIs400(t *testing.T) {
	e := w2cNewEnv(t)
	master := e.user("r4b-limit-master@example.com", coreauth.RoleMaster)
	masterIssued := e.login(master)
	owner := e.user("r4b-limit-owner@example.com", coreauth.RoleUser)
	ownerIssued := e.login(owner)
	school := e.school("r4b-limit")

	cases := []struct {
		name   string
		path   string
		issued *coreauth.Issued
	}{
		{"accounts", "/api/accounts?limit=0", masterIssued},
		{"school-admins", "/api/schools/" + school.ID + "/admins?limit=0", masterIssued},
		{"photos", "/api/me/photos?limit=0", ownerIssued},
	}
	for _, tc := range cases {
		rec := e.do(http.MethodGet, tc.path, nil, tc.issued)
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("%s limit=0 = %d, want 400; body=%q", tc.name, rec.Code, rec.Body.String())
		}
		if code := e.errorCode(rec); code != "validation_failed" {
			t.Fatalf("%s limit=0 code = %q, want validation_failed", tc.name, code)
		}
	}

	// 合法上界仍可用。
	rec := e.do(http.MethodGet, "/api/me/photos?limit=200", nil, ownerIssued)
	w2cWantStatus(t, rec, http.StatusOK)
}

// 换校在 P1 不支持。声明了 409，就必须真的可达：
// 客户端带不同的 school_id 时不得静默忽略。
func TestR4BCblSchoolChangeIs409(t *testing.T) {
	e := w2cNewEnv(t)
	master := e.user("r4b-school-master@example.com", coreauth.RoleMaster)
	issued := e.login(master)
	owner := e.user("r4b-school-owner@example.com", coreauth.RoleUser)
	schoolA := e.school("r4b-school-a")
	schoolB := e.school("r4b-school-b")
	acct := e.account(schoolA.ID, owner.ID, "13800073001")

	rec := e.do(http.MethodPatch, "/api/accounts/"+acct, map[string]any{"school_id": schoolB.ID}, issued)
	if rec.Code != http.StatusConflict {
		t.Fatalf("换校 = %d, want 409; body=%q", rec.Code, rec.Body.String())
	}
	if code := e.errorCode(rec); code != "conflict" {
		t.Fatalf("换校 code = %q, want conflict", code)
	}
	// 换校失败不得改动账号。
	if got := e.scalarString("SELECT school_id::text FROM accounts WHERE id = $1::uuid", acct); got != schoolA.ID {
		t.Fatalf("换校失败后 school_id = %q, want %q", got, schoolA.ID)
	}

	// 同校是空变更，允许 200。
	rec = e.do(http.MethodPatch, "/api/accounts/"+acct, map[string]any{"school_id": schoolA.ID}, issued)
	w2cWantStatus(t, rec, http.StatusOK)
}

// additionalProperties: false 未在运行时执行，本批显式豁免。
// 本用例钉住豁免事实：未知字段被忽略，请求成功。
// 豁免登记见 contracts/README.md 与 oapi 包的契约对账门。
func TestR4BCblUnknownFieldsAreIgnored(t *testing.T) {
	e := w2cNewEnv(t)
	master := e.user("r4b-extra-master@example.com", coreauth.RoleMaster)
	issued := e.login(master)

	rec := e.do(http.MethodPost, "/api/schools", map[string]any{
		"code": "r4b-extra", "name": "Extra School", "unknown_field": "ignored",
	}, issued)
	w2cWantStatus(t, rec, http.StatusCreated)
}
