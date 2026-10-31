package main

// UX-2 账号提交双形状 e2e：三种归属路径、owner 活跃校验、
// 设备刷新成功写回与失败零写、软删恢复、学校列表对 user 放宽。
//
// # 功能
// 覆盖 8v8 验收的 Go 侧与安全不变量：
// owner 必须是活跃用户（堵 FK 放行口子）、设备刷新失败零写库零审计、
// 设备码全文不出服务端、恢复保持删除前状态。
// 全栈用例复用 auth_e2e_test.go 的 newAuthEnv；
// 设备刷新用例需要可编程的 DeviceReader，全栈装配注入的是真实
// provider，故按 w2c 形状自建只挂学校与账号两个域的精简环境。
//
// # 归属
// cmd/server 是唯一可同时引用 adapters、app、entry 的位置。
//
// # 口径
// 缺 DSN 时整包跳过并打可见横幅。跳过不是绿。
// 每个测试独占一个 schema，互不依赖。
// 手机号只取登记的合成段（tests/synthetic-fixtures.json）。
// 邮箱一律用保留域 example.com。

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/cookiejar"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gin-gonic/gin"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/session"
	appaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/account"
	appadmin "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/admin"
	appschool "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/school"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coreschool "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/school"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
	accounthttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/account"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/router"
	schoolhttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/school"
)

// 本文件的合成手机号全部取自登记段（tests/synthetic-fixtures.json）。
const (
	u2PhoneA = "13800138020"
	u2PhoneB = "13800138021"
	u2PhoneC = "13800138022"
	u2PhoneD = "13800138023"
	u2PhoneE = "13800138024"
)

// 设备刷新用例各自的账号手机号，同样全部登记过。
const u2DevicePhoneOwn = "13800138031"

// u2AccountsKey 是本文件专用的 32 字节凭据钥，不与真实配置或其他测试共用。
var u2AccountsKey = []byte("ux2-accounts-key-0123456789abcde")

// u2Do 用指定客户端发一次 JSON 请求；csrf 非空时带 X-CSRF-Token。
func u2Do(e *authEnv, client *http.Client, method, path string, body any, csrf string) (*http.Response, []byte) {
	e.t.Helper()
	headers := map[string]string{}
	if csrf != "" {
		headers["X-CSRF-Token"] = csrf
	}
	return e.doAs(client, method, path, body, headers)
}

// u2FreshClient 造一个独立 cookie 客户端，代表另一台设备。
func u2FreshClient(t *testing.T) *http.Client {
	t.Helper()
	jar, err := cookiejar.New(nil)
	if err != nil {
		t.Fatalf("cookie jar: %v", err)
	}
	return &http.Client{Jar: jar, Timeout: 10 * time.Second}
}

// u2LoginOn 在指定客户端上真实登录（走 /api/auth/login）。
func u2LoginOn(e *authEnv, client *http.Client, email, password string) sessionUserBody {
	e.t.Helper()
	resp, payload := u2Do(e, client, http.MethodPost, "/api/auth/login",
		map[string]string{"email": email, "password": password}, "")
	if resp.StatusCode != http.StatusOK {
		e.t.Fatalf("login %s = %d, want 200; body=%q", email, resp.StatusCode, payload)
	}
	var body sessionUserBody
	if err := json.Unmarshal(payload, &body); err != nil {
		e.t.Fatalf("decode login body: %v; body=%q", err, payload)
	}
	return body
}

// u2SeedSchool 建一所合成学校并返回 ID。
func u2SeedSchool(t *testing.T, e *authEnv, code string) string {
	t.Helper()
	schools := pg.NewSchoolRepo(e.db)
	rec, err := schools.Create(e.ctx, appschool.New{Code: code, Name: "UX-2 School " + code})
	if err != nil {
		t.Fatalf("seed school %s: %v", code, err)
	}
	return rec.ID
}

// u2SeedUser 直接写库造一个可登录用户。
func u2SeedUser(t *testing.T, e *authEnv, email, password string, role coreauth.Role) coreuser.Record {
	t.Helper()
	return w2dSeedUser(t, e, email, password, role)
}

// u2BindSchool 把管理员绑定到学校。
func u2BindSchool(t *testing.T, e *authEnv, userID, schoolID string) {
	t.Helper()
	admins := pg.NewAdminSchoolRepo(e.db)
	if err := admins.Grant(e.ctx, appschool.Binding{UserID: userID, SchoolID: schoolID}); err != nil {
		t.Fatalf("grant admin binding: %v", err)
	}
}

// u2CreateBody 是 POST /api/accounts 的请求体。
type u2CreateBody struct {
	SchoolID    string `json:"school_id"`
	Phone       string `json:"phone"`
	Password    string `json:"password"`
	OwnerUserID string `json:"owner_user_id,omitempty"`
}

// u2Account 是响应里用到的账号字段。
type u2Account struct {
	ID          string `json:"id"`
	SchoolID    string `json:"school_id"`
	OwnerUserID string `json:"owner_user_id"`
	PhoneMasked string `json:"phone_masked"`
	Status      string `json:"status"`
}

// u2CreateAccount 发一次建号请求并返回状态码与响应体。
func u2CreateAccount(e *authEnv, client *http.Client, csrf string, in u2CreateBody) (*http.Response, []byte) {
	e.t.Helper()
	return u2Do(e, client, http.MethodPost, "/api/accounts", in, csrf)
}

// --- 验收①：三种归属路径（G8） -------------------------------------------

// 模拟三类真实动作：user 自建、admin 代建给指定活跃用户、
// master 指向不存在的用户。断言归属、状态与错误码。
func TestUX2G8OwnershipPaths(t *testing.T) {
	e := newAuthEnv(t)
	schoolID := u2SeedSchool(t, e, "u2-g8")

	u2SeedUser(t, e, "u2-g8-master@example.com", "master-pass", coreauth.RoleMaster)
	admin := u2SeedUser(t, e, "u2-g8-admin@example.com", "admin-pass", coreauth.RoleAdmin)
	member := u2SeedUser(t, e, "u2-g8-member@example.com", "member-pass", coreauth.RoleUser)
	u2BindSchool(t, e, admin.ID, schoolID)

	masterSession := u2LoginOn(e, e.client, "u2-g8-master@example.com", "master-pass")
	adminClient := u2FreshClient(t)
	adminSession := u2LoginOn(e, adminClient, "u2-g8-admin@example.com", "admin-pass")
	memberClient := u2FreshClient(t)
	memberSession := u2LoginOn(e, memberClient, "u2-g8-member@example.com", "member-pass")

	// 路径一：user 自建，归属自己，状态 pending。
	resp, payload := u2CreateAccount(e, memberClient, memberSession.CSRFToken, u2CreateBody{
		SchoolID: schoolID, Phone: u2PhoneA, Password: "member-pass-2",
	})
	if resp.StatusCode != http.StatusCreated {
		t.Fatalf("user self create = %d, want 201; body=%q", resp.StatusCode, payload)
	}
	var own u2Account
	if err := json.Unmarshal(payload, &own); err != nil {
		t.Fatalf("decode own create: %v; body=%q", err, payload)
	}
	if own.OwnerUserID != member.ID {
		t.Fatalf("self create owner = %q, want %q", own.OwnerUserID, member.ID)
	}
	if own.Status != string(appaccount.StatusPending) {
		t.Fatalf("self create status = %q, want pending", own.Status)
	}

	// 路径二：admin 指定活跃用户，归属该用户，进待审核。
	resp, payload = u2CreateAccount(e, adminClient, adminSession.CSRFToken, u2CreateBody{
		SchoolID: schoolID, Phone: u2PhoneB, Password: "admin-pass-2", OwnerUserID: member.ID,
	})
	if resp.StatusCode != http.StatusCreated {
		t.Fatalf("admin designated create = %d, want 201; body=%q", resp.StatusCode, payload)
	}
	var designated u2Account
	if err := json.Unmarshal(payload, &designated); err != nil {
		t.Fatalf("decode designated create: %v; body=%q", err, payload)
	}
	if designated.OwnerUserID != member.ID {
		t.Fatalf("designated owner = %q, want %q", designated.OwnerUserID, member.ID)
	}
	if designated.Status != string(appaccount.StatusPending) {
		t.Fatalf("designated status = %q, want pending", designated.Status)
	}

	// 路径三：master 指向不存在的用户 → 404，整单不建。
	resp, payload = u2CreateAccount(e, e.client, masterSession.CSRFToken, u2CreateBody{
		SchoolID: schoolID, Phone: u2PhoneC, Password: "master-pass-2", OwnerUserID: "00000000-0000-0000-0000-000000000001",
	})
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("missing owner create = %d, want 404; body=%q", resp.StatusCode, payload)
	}
	if code, _ := errorCode(t, payload); code != "not_found" {
		t.Fatalf("missing owner code = %q, want not_found", code)
	}
	if got := e.scalarInt("SELECT count(*) FROM accounts WHERE phone = $1", u2PhoneC); got != 0 {
		t.Fatal("a rejected create must not leave an account row")
	}
}

// --- 安全不变量：owner 必须是活跃用户（G11） -------------------------------

// 指向软删用户的 owner_user_id 必须 404：软删行仍在，
// 外键放行，口子只能由应用层堵上。
func TestUX2G11OwnerMustBeLiveUser(t *testing.T) {
	e := newAuthEnv(t)
	schoolID := u2SeedSchool(t, e, "u2-g11")

	u2SeedUser(t, e, "u2-g11-master@example.com", "master-pass", coreauth.RoleMaster)
	masterSession := u2LoginOn(e, e.client, "u2-g11-master@example.com", "master-pass")
	target := u2SeedUser(t, e, "u2-g11-target@example.com", "target-pass", coreauth.RoleUser)

	// 先建号证明活跃时可行，再软删用户后重试。
	resp, payload := u2CreateAccount(e, e.client, masterSession.CSRFToken, u2CreateBody{
		SchoolID: schoolID, Phone: u2PhoneD, Password: "master-pass-2", OwnerUserID: target.ID,
	})
	if resp.StatusCode != http.StatusCreated {
		t.Fatalf("live owner create = %d, want 201; body=%q", resp.StatusCode, payload)
	}
	var created u2Account
	if err := json.Unmarshal(payload, &created); err != nil {
		t.Fatalf("decode live create: %v", err)
	}
	// 让位：删掉第一个账号，避免手机号唯一约束挡住第二次建号。
	if _, err := e.db.Exec(e.ctx, "UPDATE accounts SET deleted_at = now() WHERE id = $1", created.ID); err != nil {
		t.Fatalf("hide first account: %v", err)
	}

	if err := e.users.SoftDelete(e.ctx, target.ID, masterSession.ID, clock.System{}.Now().UTC()); err != nil {
		t.Fatalf("soft delete owner: %v", err)
	}

	resp, payload = u2CreateAccount(e, e.client, masterSession.CSRFToken, u2CreateBody{
		SchoolID: schoolID, Phone: u2PhoneE, Password: "master-pass-2", OwnerUserID: target.ID,
	})
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("deleted owner create = %d, want 404; body=%q", resp.StatusCode, payload)
	}
	if code, _ := errorCode(t, payload); code != "not_found" {
		t.Fatalf("deleted owner code = %q, want not_found", code)
	}
	if got := e.scalarInt("SELECT count(*) FROM accounts WHERE phone = $1", u2PhoneE); got != 0 {
		t.Fatal("a create pointing at a deleted owner must not write a row")
	}
}

// --- 验收④：恢复后回到原状态（G10） ---------------------------------------

// 建号（pending）→ 软删 → 恢复 → status 保持 pending；
// 默认列表先无后有。user 调恢复 → 403；未删行再恢复 → 404。
func TestUX2G10RestoreRoundTrip(t *testing.T) {
	e := newAuthEnv(t)
	schoolID := u2SeedSchool(t, e, "u2-g10")

	u2SeedUser(t, e, "u2-g10-master@example.com", "master-pass", coreauth.RoleMaster)
	masterSession := u2LoginOn(e, e.client, "u2-g10-master@example.com", "master-pass")
	member := u2SeedUser(t, e, "u2-g10-member@example.com", "member-pass", coreauth.RoleUser)

	resp, payload := u2CreateAccount(e, e.client, masterSession.CSRFToken, u2CreateBody{
		SchoolID: schoolID, Phone: u2PhoneD, Password: "master-pass-2", OwnerUserID: member.ID,
	})
	if resp.StatusCode != http.StatusCreated {
		t.Fatalf("create = %d, want 201; body=%q", resp.StatusCode, payload)
	}
	var created u2Account
	if err := json.Unmarshal(payload, &created); err != nil {
		t.Fatalf("decode create: %v", err)
	}

	delResp, delPayload := u2Do(e, e.client, http.MethodDelete, "/api/accounts/"+created.ID, nil, masterSession.CSRFToken)
	if delResp.StatusCode != http.StatusNoContent {
		t.Fatalf("delete = %d, want 204; body=%q", delResp.StatusCode, delPayload)
	}
	if got := e.scalarInt("SELECT count(*) FROM accounts WHERE id = $1 AND deleted_at IS NULL", created.ID); got != 0 {
		t.Fatal("the deleted account is still live")
	}

	// user 恢复 → 403。
	memberClient := u2FreshClient(t)
	memberSession := u2LoginOn(e, memberClient, "u2-g10-member@example.com", "member-pass")
	userResp, userPayload := u2Do(e, memberClient, http.MethodPost,
		"/api/accounts/"+created.ID+"/restore", nil, memberSession.CSRFToken)
	if userResp.StatusCode != http.StatusForbidden {
		t.Fatalf("user restore = %d, want 403; body=%q", userResp.StatusCode, userPayload)
	}

	restoreResp, restorePayload := u2Do(e, e.client, http.MethodPost,
		"/api/accounts/"+created.ID+"/restore", nil, masterSession.CSRFToken)
	if restoreResp.StatusCode != http.StatusOK {
		t.Fatalf("restore = %d, want 200; body=%q", restoreResp.StatusCode, restorePayload)
	}
	var restored u2Account
	if err := json.Unmarshal(restorePayload, &restored); err != nil {
		t.Fatalf("decode restore: %v; body=%q", err, restorePayload)
	}
	if restored.Status != string(appaccount.StatusPending) {
		t.Fatalf("restored status = %q, want the pre-delete pending", restored.Status)
	}
	if got := e.scalarInt("SELECT count(*) FROM accounts WHERE id = $1 AND deleted_at IS NULL", created.ID); got != 1 {
		t.Fatal("the restored account is not live again")
	}

	// 未删行再恢复 → 409（恢复只认软删行；适配器口径）。
	againResp, againPayload := u2Do(e, e.client, http.MethodPost,
		"/api/accounts/"+created.ID+"/restore", nil, masterSession.CSRFToken)
	if againResp.StatusCode != http.StatusConflict {
		t.Fatalf("restore of a live account = %d, want 409; body=%q", againResp.StatusCode, againPayload)
	}
}

// --- 设备刷新：可编程 DeviceReader 的精简环境 ------------------------------

// u2DeviceOutcome 是一次读回的脚本项。
type u2DeviceOutcome struct {
	state appaccount.DeviceState
	err   error
}

// u2DeviceReader 是可编程的设备读回替身：按调用次序回放脚本，
// 脚本取完后重复最后一项。calls 记录触达次数供作用域断言。
type u2DeviceReader struct {
	mu     sync.Mutex
	calls  int
	script []u2DeviceOutcome
}

func (r *u2DeviceReader) DeviceState(context.Context, string, appaccount.SigningCredential) (appaccount.DeviceState, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.calls++
	at := r.calls - 1
	if at >= len(r.script) {
		at = len(r.script) - 1
	}
	out := r.script[at]
	return out.state, out.err
}

func (r *u2DeviceReader) count() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.calls
}

// u2Env 是只挂学校与账号两个域的精简 HTTP 环境。
type u2Env struct {
	t        *testing.T
	ctx      context.Context
	adminDB  *pg.DB
	db       *pg.DB
	schema   string
	engine   *gin.Engine
	sessions *session.Memory
	users    *pg.UserRepo
	schools  *pg.SchoolRepo
	accounts *pg.AccountRepo
	cipher   *appaccount.Cipher
	reader   *u2DeviceReader
}

func u2NewEnv(t *testing.T) *u2Env {
	t.Helper()
	gin.SetMode(gin.TestMode)
	// w2cRequireDSN 是豁免名单内的 DSN 读取入口：缺库打可见横幅再跳过。
	base := w2cRequireDSN(t)
	ctx := t.Context()
	schema := "ux2_" + randomHex(6)

	adminDB, err := pg.Open(ctx, pg.Options{DSN: base, MaxConns: 1, StatementTimeout: 30 * time.Second})
	if err != nil {
		t.Fatalf("open admin connection: %v", err)
	}
	if _, err := adminDB.Exec(ctx, "CREATE SCHEMA "+schema); err != nil {
		adminDB.Close()
		t.Fatalf("create schema %s: %v", schema, err)
	}
	dsn := withSearchPath(base, schema)
	db, err := pg.Open(ctx, pg.Options{DSN: dsn, MaxConns: 8, StatementTimeout: 10 * time.Second})
	if err != nil {
		u2DropSchema(adminDB, schema)
		adminDB.Close()
		t.Fatalf("open test connection: %v", err)
	}
	if _, err := pg.Migrate(ctx, dsn); err != nil {
		db.Close()
		u2DropSchema(adminDB, schema)
		adminDB.Close()
		t.Fatalf("migrate test schema: %v", err)
	}

	e := &u2Env{t: t, ctx: ctx, adminDB: adminDB, db: db, schema: schema}
	e.users = pg.NewUserRepo(db)
	e.schools = pg.NewSchoolRepo(db)
	e.accounts = pg.NewAccountRepo(db)
	e.cipher, err = appaccount.NewCipher(u2AccountsKey)
	if err != nil {
		t.Fatalf("new credential cipher: %v", err)
	}

	clk := clock.System{}
	audits, err := pg.NewAuditRepo(db, []byte("ux2-audit-key-0123456789abcdef00"))
	if err != nil {
		t.Fatalf("new audit repo: %v", err)
	}
	adminSvc, err := appadmin.NewService(appadmin.Deps{
		Admins:  pg.NewAdminSchoolRepo(db),
		Users:   e.users,
		Schools: e.schools,
		Audit:   audits,
		Tx:      db,
		Clock:   clk,
	})
	if err != nil {
		t.Fatalf("new admin service: %v", err)
	}
	e.reader = &u2DeviceReader{}
	accountSvc, err := appaccount.NewService(appaccount.Deps{
		Accounts:   e.accounts,
		Schools:    e.schools,
		Audit:      audits,
		Tx:         db,
		Clock:      clk,
		Cipher:     e.cipher,
		Limit:      10000,
		OwnerLimit: 10000,
		Owner:      e.users,
		Device:     e.reader,
	})
	if err != nil {
		t.Fatalf("new account service: %v", err)
	}
	schoolSvc, err := appschool.NewService(appschool.Deps{
		Schools: e.schools, Audit: audits, Tx: db, Clock: clk,
	})
	if err != nil {
		t.Fatalf("new school service: %v", err)
	}
	e.sessions = session.NewMemoryWithTTL(clk, 30*time.Minute, 12*time.Hour, []byte("ux2-session-secret-0123456789abc"))
	e.engine, err = router.New(router.Options{
		Sessions:        e.sessions,
		Clock:           clk,
		Logger:          slog.New(slog.NewTextHandler(io.Discard, nil)),
		InsecureCookies: true,
		Schools:         schoolhttp.NewHandler(schoolSvc, adminSvc),
		Accounts:        accounthttp.NewHandler(accountSvc, adminSvc),
	})
	if err != nil {
		t.Fatalf("router.New: %v", err)
	}

	t.Cleanup(func() {
		db.Close()
		u2DropSchema(adminDB, schema)
		adminDB.Close()
	})
	return e
}

func u2DropSchema(adminDB *pg.DB, schema string) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	_, _ = adminDB.Exec(ctx, "DROP SCHEMA IF EXISTS "+schema+" CASCADE")
}

// issue 直接签发一个用户的会话（本环境不测登录面）。
func (e *u2Env) issue(user coreuser.Record) *coreauth.Issued {
	e.t.Helper()
	issued, err := e.sessions.Issue(e.ctx, coreauth.User{
		ID:     user.ID,
		Email:  user.Email,
		Role:   user.Role,
		Status: coreauth.StatusActive,
	})
	if err != nil {
		e.t.Fatalf("issue session: %v", err)
	}
	return &issued
}

// do 向路由发一次 JSON 请求；issued 非空时带 cookie 与 CSRF 头。
func (e *u2Env) do(method, path string, body any, issued *coreauth.Issued) (int, []byte) {
	e.t.Helper()
	var reader io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			e.t.Fatalf("marshal request: %v", err)
		}
		reader = bytes.NewReader(raw)
	}
	req := httptest.NewRequest(method, path, reader)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if issued != nil {
		req.AddCookie(&http.Cookie{Name: coreauth.InsecureCookieName, Value: issued.Token})
		req.Header.Set("X-CSRF-Token", issued.CSRFToken)
	}
	rec := httptest.NewRecorder()
	e.engine.ServeHTTP(rec, req)
	return rec.Code, rec.Body.Bytes()
}

// seedSchool 建一所学校并返回 ID。
func (e *u2Env) seedSchool(label string) string {
	e.t.Helper()
	rec, err := e.schools.Create(e.ctx, appschool.New{Code: "u2-" + label + "-" + randomHex(4), Name: "UX-2 School " + label})
	if err != nil {
		e.t.Fatalf("seed school %s: %v", label, err)
	}
	return rec.ID
}

// seedUser 直接写库造一个用户。
func (e *u2Env) seedUser(email string, role coreauth.Role) coreuser.Record {
	e.t.Helper()
	var rec coreuser.Record
	err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		var err error
		rec, err = e.users.Create(ctx, coreuser.New{
			Email:        email,
			PasswordHash: "argon2id$test",
			Role:         role,
		}, 10000)
		return err
	})
	if err != nil {
		e.t.Fatalf("seed user %s: %v", email, err)
	}
	return rec
}

// seedAccount 造一个带凭据行的账号并返回 ID。
func (e *u2Env) seedAccount(schoolID, ownerID, phone, deviceCode, phoneModel string) string {
	e.t.Helper()
	var accountID string
	err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		rec, err := e.accounts.Create(ctx, appaccount.New{
			SchoolID:    schoolID,
			OwnerUserID: ownerID,
			Phone:       phone,
		}, 10000, 10000)
		if err != nil {
			return err
		}
		accountID = rec.ID
		cred, err := e.cipher.Seal(schoolID, rec.ID, appaccount.CredentialPayload{
			Password:   "seeded-password",
			DeviceCode: deviceCode,
			PhoneModel: phoneModel,
		})
		if err != nil {
			return err
		}
		return e.accounts.InsertCredential(ctx, cred)
	})
	if err != nil {
		e.t.Fatalf("seed account %s: %v", phone, err)
	}
	return accountID
}

// credentialVersion 读凭据行的乐观锁版本。
func (e *u2Env) credentialVersion(accountID string) int64 {
	e.t.Helper()
	v, err := e.db.QueryInt64(e.ctx,
		"SELECT version FROM credentials WHERE account_id = $1", accountID)
	if err != nil {
		e.t.Fatalf("read credential version: %v", err)
	}
	return v
}

// credentialPayload 解出凭据明文负载。
func (e *u2Env) credentialPayload(accountID string) appaccount.CredentialPayload {
	e.t.Helper()
	cred, err := e.accounts.Credential(e.ctx, corescope.Master(), accountID)
	if err != nil {
		e.t.Fatalf("load credential: %v", err)
	}
	payload, err := e.cipher.Open(cred)
	if err != nil {
		e.t.Fatalf("open credential: %v", err)
	}
	return payload
}

// auditCount 数某动作对某目标的审计行。
func (e *u2Env) auditCount(targetID, action string) int64 {
	e.t.Helper()
	v, err := e.db.QueryInt64(e.ctx,
		"SELECT count(*) FROM audit_logs WHERE target_id = $1 AND action = $2", targetID, action)
	if err != nil {
		e.t.Fatalf("count audit rows: %v", err)
	}
	return v
}

// lastAuditDetail 取某动作对某目标的最新一条 detail。
func (e *u2Env) lastAuditDetail(targetID, action string) string {
	e.t.Helper()
	v, err := e.db.QueryString(e.ctx,
		"SELECT detail FROM audit_logs WHERE target_id = $1 AND action = $2 ORDER BY id DESC LIMIT 1",
		targetID, action)
	if err != nil {
		e.t.Fatalf("read audit detail: %v", err)
	}
	return v
}

// --- 验收②：设备刷新成功写回与失败零写（G9） ------------------------------

// 成功 → 200 掩码响应、凭据 version+1、审计恰一行且无全文；
// 平台空字段不覆盖已有值且不空转写版本；失败 → 502 且零写零审计。
func TestUX2G9DeviceRefresh(t *testing.T) {
	e := u2NewEnv(t)
	schoolID := e.seedSchool("g9")
	master := e.seedUser("u2-g9-master@example.com", coreauth.RoleMaster)
	owner := e.seedUser("u2-g9-owner@example.com", coreauth.RoleUser)
	accountID := e.seedAccount(schoolID, owner.ID, u2DevicePhoneOwn, "SEEDDEV01", "SeedPhone 1")

	fullCode := "UX2DEVICECODE-1234"
	const maskedFull = "UX****34"

	masterIssued := e.issue(master)

	// 成功：平台回读非空 → 写回 + 审计。
	e.reader.script = []u2DeviceOutcome{{
		state: appaccount.DeviceState{Authorized: true, Code: fullCode, PhoneModel: "Mi 9 SE"},
	}}
	before := e.credentialVersion(accountID)
	code, payload := e.do(http.MethodPost, "/api/accounts/"+accountID+"/device-refresh", nil, masterIssued)
	if code != http.StatusOK {
		t.Fatalf("device refresh = %d, want 200; body=%q", code, payload)
	}
	var device struct {
		DeviceCodeMasked string `json:"device_code_masked"`
		PhoneModel       string `json:"phone_model"`
		Authorized       bool   `json:"authorized"`
	}
	if err := json.Unmarshal(payload, &device); err != nil {
		t.Fatalf("decode device refresh: %v; body=%q", err, payload)
	}
	if device.DeviceCodeMasked != maskedFull {
		t.Fatalf("masked code = %q, want %q", device.DeviceCodeMasked, maskedFull)
	}
	if device.PhoneModel != "Mi 9 SE" || !device.Authorized {
		t.Fatalf("device refresh body = %+v", device)
	}
	if strings.Contains(string(payload), fullCode) {
		t.Fatal("the response leaks the full device code")
	}
	if got := e.credentialVersion(accountID); got != before+1 {
		t.Fatalf("credential version = %d, want %d+1", got, before)
	}
	merged := e.credentialPayload(accountID)
	if merged.DeviceCode != fullCode || merged.PhoneModel != "Mi 9 SE" {
		t.Fatalf("merged payload = %+v", merged)
	}
	if merged.Password != "seeded-password" {
		t.Fatal("the refresh must not disturb the stored password")
	}
	if got := e.auditCount(accountID, "account.device_refresh"); got != 1 {
		t.Fatalf("device_refresh audit rows = %d, want 1", got)
	}
	detail := e.lastAuditDetail(accountID, "account.device_refresh")
	if strings.Contains(detail, fullCode) {
		t.Fatal("the audit detail leaks the full device code")
	}

	// 平台空字段不覆盖已有值：响应仍给合并后的现值，凭据明文与版本不变。
	e.reader.script = []u2DeviceOutcome{{
		state: appaccount.DeviceState{Authorized: true},
	}}
	before = e.credentialVersion(accountID)
	code, payload = e.do(http.MethodPost, "/api/accounts/"+accountID+"/device-refresh", nil, masterIssued)
	if code != http.StatusOK {
		t.Fatalf("empty-fields refresh = %d, want 200; body=%q", code, payload)
	}
	device = struct {
		DeviceCodeMasked string `json:"device_code_masked"`
		PhoneModel       string `json:"phone_model"`
		Authorized       bool   `json:"authorized"`
	}{}
	if err := json.Unmarshal(payload, &device); err != nil {
		t.Fatalf("decode empty-fields refresh: %v", err)
	}
	if device.DeviceCodeMasked != maskedFull || device.PhoneModel != "Mi 9 SE" {
		t.Fatalf("empty-fields body = %+v, want the stored values", device)
	}
	if got := e.credentialVersion(accountID); got != before {
		t.Fatalf("empty-fields refresh moved the version to %d, want %d", got, before)
	}
	if got := e.auditCount(accountID, "account.device_refresh"); got != 2 {
		t.Fatalf("device_refresh audit rows = %d, want 2", got)
	}

	// 失败：零写库零审计。
	e.reader.script = []u2DeviceOutcome{{err: fmt.Errorf("platform down")}}
	before = e.credentialVersion(accountID)
	code, payload = e.do(http.MethodPost, "/api/accounts/"+accountID+"/device-refresh", nil, masterIssued)
	if code != http.StatusBadGateway {
		t.Fatalf("failed refresh = %d, want 502; body=%q", code, payload)
	}
	if errCode, _ := errorCode(t, payload); errCode != "upstream_unavailable" {
		t.Fatalf("failed refresh code = %q, want upstream_unavailable", errCode)
	}
	if got := e.credentialVersion(accountID); got != before {
		t.Fatalf("a failed refresh moved the version to %d, want %d", got, before)
	}
	if got := e.auditCount(accountID, "account.device_refresh"); got != 2 {
		t.Fatalf("a failed refresh wrote audit rows: %d, want 2", got)
	}
}

// --- 安全不变量：设备刷新的作用域（G12） -----------------------------------

// user 刷新自己的账号可达成功路径；跨属主与跨校一律 404，
// 且被拒请求不得触达平台（reader 计数不增）。
func TestUX2G12DeviceRefreshScope(t *testing.T) {
	e := u2NewEnv(t)
	schoolX := e.seedSchool("x")
	schoolY := e.seedSchool("y")
	owner := e.seedUser("u2-g12-owner@example.com", coreauth.RoleUser)
	stranger := e.seedUser("u2-g12-stranger@example.com", coreauth.RoleUser)
	admin := e.seedUser("u2-g12-admin@example.com", coreauth.RoleAdmin)

	// admin 只绑 schoolY；own/other 都在 schoolX。
	admins := pg.NewAdminSchoolRepo(e.db)
	if err := admins.Grant(e.ctx, appschool.Binding{UserID: admin.ID, SchoolID: schoolY}); err != nil {
		t.Fatalf("grant admin: %v", err)
	}

	ownID := e.seedAccount(schoolX, owner.ID, u2DevicePhoneOwn, "SEEDDEV01", "")

	e.reader.script = []u2DeviceOutcome{{
		state: appaccount.DeviceState{Authorized: true, Code: "SCOPECODE-1", PhoneModel: "P"},
	}}

	// user 自己 → 200。
	ownIssued := e.issue(owner)
	code, payload := e.do(http.MethodPost, "/api/accounts/"+ownID+"/device-refresh", nil, ownIssued)
	if code != http.StatusOK {
		t.Fatalf("own refresh = %d, want 200; body=%q", code, payload)
	}

	// user 看别人的 → 404，且不触达平台。
	strangerIssued := e.issue(stranger)
	code, payload = e.do(http.MethodPost, "/api/accounts/"+ownID+"/device-refresh", nil, strangerIssued)
	if code != http.StatusNotFound {
		t.Fatalf("cross-owner refresh = %d, want 404; body=%q", code, payload)
	}

	// admin 对可见面之外的账号 → 404。
	adminIssued := e.issue(admin)
	code, payload = e.do(http.MethodPost, "/api/accounts/"+ownID+"/device-refresh", nil, adminIssued)
	if code != http.StatusNotFound {
		t.Fatalf("cross-school refresh = %d, want 404; body=%q", code, payload)
	}
	// 平台调用恰好 1 次：被拒请求在读凭据前就返回。
	if got := e.reader.count(); got != 1 {
		t.Fatalf("provider calls = %d, want 1 (denied requests must not reach the platform)", got)
	}
}

// --- 学校列表对 user 放宽 --------------------------------------------------

// user 列学校 → 200 且只见 active；master 见全量含停用。
func TestUX2SchoolsForUser(t *testing.T) {
	e := newAuthEnv(t)
	activeID := u2SeedSchool(t, e, "u2-sch-active")
	hiddenID := u2SeedSchool(t, e, "u2-sch-hidden")
	schools := pg.NewSchoolRepo(e.db)
	if _, err := schools.Update(e.ctx, hiddenID, coreschool.Update{
		Status: ptrSchoolStatus(coreschool.StatusDisabled),
	}); err != nil {
		t.Fatalf("disable school: %v", err)
	}

	u2SeedUser(t, e, "u2-sch-master@example.com", "master-pass", coreauth.RoleMaster)
	u2SeedUser(t, e, "u2-sch-member@example.com", "member-pass", coreauth.RoleUser)
	masterSession := u2LoginOn(e, e.client, "u2-sch-master@example.com", "master-pass")
	_ = masterSession

	memberClient := u2FreshClient(t)
	u2LoginOn(e, memberClient, "u2-sch-member@example.com", "member-pass")

	resp, payload := u2Do(e, memberClient, http.MethodGet, "/api/schools", nil, "")
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("user school list = %d, want 200; body=%q", resp.StatusCode, payload)
	}
	var page struct {
		Items []struct {
			ID     string `json:"id"`
			Status string `json:"status"`
		} `json:"items"`
	}
	if err := json.Unmarshal(payload, &page); err != nil {
		t.Fatalf("decode school page: %v; body=%q", err, payload)
	}
	if len(page.Items) != 1 || page.Items[0].ID != activeID {
		t.Fatalf("user sees %d schools (%+v), want only the active one", len(page.Items), page.Items)
	}

	masterResp, masterPayload := u2Do(e, e.client, http.MethodGet, "/api/schools", nil, "")
	if masterResp.StatusCode != http.StatusOK {
		t.Fatalf("master school list = %d; body=%q", masterResp.StatusCode, masterPayload)
	}
	var masterPage struct {
		Items []struct {
			ID string `json:"id"`
		} `json:"items"`
	}
	if err := json.Unmarshal(masterPayload, &masterPage); err != nil {
		t.Fatalf("decode master school page: %v", err)
	}
	seen := 0
	for _, item := range masterPage.Items {
		if item.ID == activeID || item.ID == hiddenID {
			seen++
		}
	}
	if seen != 2 {
		t.Fatalf("master sees %d of the two seeded schools, want 2", seen)
	}
}

// u2LookupRef 是 lookup 行的契约形状。
type u2LookupRef struct {
	ID          string `json:"id"`
	EmailMasked string `json:"email_masked"`
	Status      string `json:"status"`
}

// --- 安全不变量：exact lookup 不被前缀歧义绑架（F1） -----------------------

// 同首字符同域的两名用户：前缀 q=a. 回两条（email 升序），
// exact=true 只回全等邮箱的那一条。掩码把两者渲染成同一个字符串，
// 前缀语义＋取第一条会把账号绑到错误属主（审查 F1 实证）。
func TestUX2LookupExactBeatsPrefix(t *testing.T) {
	e := newAuthEnv(t)
	u2SeedUser(t, e, "u2-lk-master@example.com", "master-pass", coreauth.RoleMaster)
	one := u2SeedUser(t, e, "a.one@example.com", "one-pass", coreauth.RoleUser)
	two := u2SeedUser(t, e, "a.two@example.com", "two-pass", coreauth.RoleUser)
	u2LoginOn(e, e.client, "u2-lk-master@example.com", "master-pass")

	var prefixPage struct {
		Items []u2LookupRef `json:"items"`
	}
	prefixResp, prefixPayload := u2Do(e, e.client, http.MethodGet,
		"/api/users/lookup?q=a.&limit=200", nil, "")
	if prefixResp.StatusCode != http.StatusOK {
		t.Fatalf("prefix lookup = %d; body=%q", prefixResp.StatusCode, prefixPayload)
	}
	if err := json.Unmarshal(prefixPayload, &prefixPage); err != nil {
		t.Fatalf("decode prefix lookup: %v", err)
	}
	if len(prefixPage.Items) != 2 {
		t.Fatalf("prefix lookup rows = %d, want 2; body=%q", len(prefixPage.Items), prefixPayload)
	}
	if prefixPage.Items[0].ID != one.ID || prefixPage.Items[1].ID != two.ID {
		t.Fatalf("prefix order = [%s %s], want email asc [%s %s]",
			prefixPage.Items[0].ID, prefixPage.Items[1].ID, one.ID, two.ID)
	}

	var exactPage struct {
		Items []u2LookupRef `json:"items"`
	}
	exactResp, exactPayload := u2Do(e, e.client, http.MethodGet,
		"/api/users/lookup?q=a.two%40example.com&exact=true", nil, "")
	if exactResp.StatusCode != http.StatusOK {
		t.Fatalf("exact lookup = %d; body=%q", exactResp.StatusCode, exactPayload)
	}
	if err := json.Unmarshal(exactPayload, &exactPage); err != nil {
		t.Fatalf("decode exact lookup: %v", err)
	}
	if len(exactPage.Items) != 1 || exactPage.Items[0].ID != two.ID {
		t.Fatalf("exact lookup rows = %+v, want exactly %s", exactPage.Items, two.ID)
	}
}

// --- 安全不变量：include_deleted 的可见面（M6） ----------------------------

// 建号→软删→默认列表不含→include_deleted=true 含且 deleted_at 非空；
// user 打开同开关只见自己名下（含自己的已删行），他人的行不可见。
func TestUX2IncludeDeletedList(t *testing.T) {
	e := newAuthEnv(t)
	schoolID := u2SeedSchool(t, e, "u2-inc")

	u2SeedUser(t, e, "u2-inc-master@example.com", "master-pass", coreauth.RoleMaster)
	masterSession := u2LoginOn(e, e.client, "u2-inc-master@example.com", "master-pass")
	member := u2SeedUser(t, e, "u2-inc-member@example.com", "member-pass", coreauth.RoleUser)

	// 两条账号：member 一条（将软删）、master 自有一条（保持活跃）。
	resp, payload := u2CreateAccount(e, e.client, masterSession.CSRFToken, u2CreateBody{
		SchoolID: schoolID, Phone: u2PhoneD, Password: "master-pass-2", OwnerUserID: member.ID,
	})
	if resp.StatusCode != http.StatusCreated {
		t.Fatalf("create member account = %d, want 201; body=%q", resp.StatusCode, payload)
	}
	var memberAccount u2Account
	if err := json.Unmarshal(payload, &memberAccount); err != nil {
		t.Fatalf("decode member create: %v", err)
	}
	resp, payload = u2CreateAccount(e, e.client, masterSession.CSRFToken, u2CreateBody{
		SchoolID: schoolID, Phone: u2PhoneE, Password: "master-pass-2",
	})
	if resp.StatusCode != http.StatusCreated {
		t.Fatalf("create master account = %d, want 201; body=%q", resp.StatusCode, payload)
	}

	delResp, delPayload := u2Do(e, e.client, http.MethodDelete,
		"/api/accounts/"+memberAccount.ID, nil, masterSession.CSRFToken)
	if delResp.StatusCode != http.StatusNoContent {
		t.Fatalf("delete = %d, want 204; body=%q", delResp.StatusCode, delPayload)
	}

	// 默认列表：软删行不可见（扫全页）。
	var defaultPage struct {
		Items []u2Account `json:"items"`
	}
	defResp, defPayload := u2Do(e, e.client, http.MethodGet, "/api/accounts?limit=200", nil, "")
	if defResp.StatusCode != http.StatusOK {
		t.Fatalf("default list = %d; body=%q", defResp.StatusCode, defPayload)
	}
	if err := json.Unmarshal(defPayload, &defaultPage); err != nil {
		t.Fatalf("decode default list: %v", err)
	}
	for _, item := range defaultPage.Items {
		if item.ID == memberAccount.ID {
			t.Fatal("the deleted account appears in the default list")
		}
	}

	// include_deleted=true：行回来且 deleted_at 非空。
	var fullPage struct {
		Items []struct {
			ID        string     `json:"id"`
			DeletedAt *time.Time `json:"deleted_at"`
		} `json:"items"`
	}
	incResp, incPayload := u2Do(e, e.client, http.MethodGet,
		"/api/accounts?limit=200&include_deleted=true", nil, "")
	if incResp.StatusCode != http.StatusOK {
		t.Fatalf("include_deleted list = %d; body=%q", incResp.StatusCode, incPayload)
	}
	if err := json.Unmarshal(incPayload, &fullPage); err != nil {
		t.Fatalf("decode include_deleted list: %v", err)
	}
	var found *time.Time
	for i := range fullPage.Items {
		if fullPage.Items[i].ID == memberAccount.ID {
			found = fullPage.Items[i].DeletedAt
		}
	}
	if found == nil {
		t.Fatal("include_deleted=true did not return the deleted row with a timestamp")
	}

	// user 视角：同开关只见自己名下；member 看得到自己的已删行，看不到 master 的行。
	memberClient := u2FreshClient(t)
	memberSession := u2LoginOn(e, memberClient, "u2-inc-member@example.com", "member-pass")
	userResp, userPayload := u2Do(e, memberClient, http.MethodGet,
		"/api/accounts?limit=200&include_deleted=true", nil, memberSession.CSRFToken)
	if userResp.StatusCode != http.StatusOK {
		t.Fatalf("user include_deleted list = %d; body=%q", userResp.StatusCode, userPayload)
	}
	var userPage struct {
		Items []struct {
			ID          string  `json:"id"`
			OwnerUserID string  `json:"owner_user_id"`
			DeletedAt   *string `json:"deleted_at"`
		} `json:"items"`
	}
	if err := json.Unmarshal(userPayload, &userPage); err != nil {
		t.Fatalf("decode user list: %v", err)
	}
	sawOwnDeleted := false
	for _, item := range userPage.Items {
		if item.OwnerUserID != member.ID {
			t.Fatalf("the user list leaked account %s owned by %s", item.ID, item.OwnerUserID)
		}
		if item.ID == memberAccount.ID && item.DeletedAt != nil {
			sawOwnDeleted = true
		}
	}
	if !sawOwnDeleted {
		t.Fatal("the user's own deleted row is missing from include_deleted=true")
	}
}

func ptrSchoolStatus(s coreschool.Status) *coreschool.Status { return &s }
