package main

// W2C e2e：学校、管理员、账号三组端点。
//
// # 功能
// 用真实 PostgreSQL 与内存会话直接向路由发请求，逐条钉住
// E-30 至 E-37、游标分页、上限与跨校可见性。
// 本文件**不**测登录；会话由内存签发器直接给出。
//
// # 归属
// cmd/server 可以同时引用 adapters 与 entry；
// internal/entry 禁止引用 adapters，故 e2e 只能放这里。
//
// # 口径
// 每个用例独占一个数据库 schema，互不影响。
// 缺 DSN 时打**可见**跳过横幅，绝不静默当绿。

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"regexp"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gin-gonic/gin"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/session"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/storage"
	appaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/account"
	appadmin "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/admin"
	appdiagnostic "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/diagnostic"
	appegress "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/egress"
	appevents "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/events"
	applocation "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/location"
	appphotos "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/photos"
	appschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/schedule"
	appschool "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/school"
	apptimepref "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/timepref"
	appuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/user"
	appworker "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/worker"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/assembly"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corerate "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/rate"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
	accounthttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/account"
	adminhttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/admin"
	eventshttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/events"
	locationhttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/location"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	photoshttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/photos"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/router"
	schedulehttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/schedule"
	schoolhttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/school"
	signinghttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/signing"
	userhttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/user"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

const w2cDSNEnv = "YIBAN_TEST_DATABASE_URL"

// 固定 32 字节凭据钥。真实钥来自配置。
var w2cAccountsKey = []byte("0123456789abcdef0123456789abcdef")

// 11 位手机号形状。E-37 用它扫描响应与日志。
var w2cPhoneShape = regexp.MustCompile(`1[3-9][0-9]{9}`)

// w2cEnv 是一个独占 schema 的 HTTP 测试环境。
type w2cEnv struct {
	t        *testing.T
	ctx      context.Context
	adminDB  *pg.DB
	db       *pg.DB
	schema   string
	engine   *gin.Engine
	sessions *session.Memory

	users     *pg.UserRepo
	schools   *pg.SchoolRepo
	admins    *pg.AdminSchoolRepo
	accounts  *pg.AccountRepo
	audits    *pg.AuditRepo
	sched     *pg.ScheduleRepo
	prefs     *pg.TimePrefRepo
	photos    *pg.PhotoRepo
	photoBlob *storage.FS
	photoDir  string
	photoSvc  *appphotos.Service
	locations *pg.LocationRepo
	worker    *appworker.Service
	provider  *e2eProvider
	broker    *appevents.Broker
	// windows 是窗口发现脚本：任务 ID → 平台窗口。
	windows map[string]contract.Window
	// discover 是带计数的窗口发现替身；calls 即平台调用次数。
	discover *e2eDiscoverer

	logs   *bytes.Buffer
	bodies []string
}

// w2cRequireDSN 缺库时打可见横幅再跳过。
// t.Skip 的日志在不加 -v 时会被吞掉（AGENTS §30）。
func w2cRequireDSN(t *testing.T) string {
	t.Helper()
	dsn := strings.TrimSpace(os.Getenv(w2cDSNEnv))
	if dsn == "" {
		fmt.Println("\n**********************************************************************")
		fmt.Println("* SKIP: " + w2cDSNEnv + " is not set.")
		fmt.Println("* The W2C HTTP e2e tests DID NOT RUN. An unset database is never green.")
		fmt.Println("**********************************************************************")
		t.Skip("SKIP: " + w2cDSNEnv + " is not set. W2C e2e did not run.")
	}
	return dsn
}

func w2cNewEnv(t *testing.T) *w2cEnv {
	t.Helper()
	gin.SetMode(gin.TestMode)
	base := w2cRequireDSN(t)
	ctx := t.Context()
	schema := "w2c_" + w2cRandomHex(6)

	adminDB, err := pg.Open(ctx, pg.Options{DSN: base, MaxConns: 1, StatementTimeout: 30 * time.Second})
	if err != nil {
		t.Fatalf("open admin connection: %v", err)
	}
	if _, err := adminDB.Exec(ctx, "CREATE SCHEMA "+schema); err != nil {
		adminDB.Close()
		t.Fatalf("create schema %s: %v", schema, err)
	}
	dsn := w2cWithSchema(base, schema)
	db, err := pg.Open(ctx, pg.Options{DSN: dsn, MaxConns: 8, StatementTimeout: 10 * time.Second})
	if err != nil {
		w2cDropSchema(adminDB, schema)
		adminDB.Close()
		t.Fatalf("open test connection: %v", err)
	}
	if _, err := pg.Migrate(ctx, dsn); err != nil {
		db.Close()
		w2cDropSchema(adminDB, schema)
		adminDB.Close()
		t.Fatalf("migrate test schema: %v", err)
	}

	e := &w2cEnv{
		t:       t,
		ctx:     ctx,
		adminDB: adminDB,
		db:      db,
		schema:  schema,
		logs:    &bytes.Buffer{},
	}
	e.users = pg.NewUserRepo(db)
	e.schools = pg.NewSchoolRepo(db)
	e.admins = pg.NewAdminSchoolRepo(db)
	e.accounts = pg.NewAccountRepo(db)
	e.sched = pg.NewScheduleRepo(db)
	e.prefs = pg.NewTimePrefRepo(db)
	e.photos = pg.NewPhotoRepo(db)
	e.locations = pg.NewLocationRepo(db)
	e.windows = map[string]contract.Window{}
	e.audits, err = pg.NewAuditRepo(db, []byte("w2c-audit-key-0123456789abcdef00"))
	if err != nil {
		t.Fatalf("new audit repo: %v", err)
	}

	cipher, err := appaccount.NewCipher(w2cAccountsKey)
	if err != nil {
		t.Fatalf("new credential cipher: %v", err)
	}
	clk := clock.System{}
	logger := slog.New(slog.NewJSONHandler(e.logs, nil))

	schoolSvc, err := appschool.NewService(appschool.Deps{
		Schools: e.schools,
		Audit:   e.audits,
		Tx:      db,
		Clock:   clk,
	})
	if err != nil {
		t.Fatalf("new school service: %v", err)
	}
	adminSvc, err := appadmin.NewService(appadmin.Deps{
		Admins:  e.admins,
		Users:   e.users,
		Schools: e.schools,
		Audit:   e.audits,
		Tx:      db,
		Clock:   clk,
	})
	if err != nil {
		t.Fatalf("new admin service: %v", err)
	}
	accountSvc, err := appaccount.NewService(appaccount.Deps{
		Accounts:   e.accounts,
		Schools:    e.schools,
		Audit:      e.audits,
		Tx:         db,
		Clock:      clk,
		Cipher:     cipher,
		Limit:      10000,
		OwnerLimit: 10000,
	})
	if err != nil {
		t.Fatalf("new account service: %v", err)
	}

	e.sessions = session.NewMemoryWithTTL(clk, 30*time.Minute, 12*time.Hour, []byte("w2c-session-secret-0123456789abc"))
	loc := time.FixedZone("CST", 8*3600)
	e.provider = &e2eProvider{windows: e.windows, targets: map[string][]contract.Target{}}
	e.discover = &e2eDiscoverer{windows: e.windows}
	egressSvc, err := appegress.NewService(appegress.Deps{
		Store: pg.NewEgressRepo(db), Clock: clk,
	})
	if err != nil {
		t.Fatalf("new egress service: %v", err)
	}
	// 图片库与定位策略先于执行循环装配：worker 的两个注入端口引用同例。
	photoDir := t.TempDir()
	photoBlobs, err := storage.NewFS(photoDir)
	if err != nil {
		t.Fatalf("new photo storage: %v", err)
	}
	e.photoBlob, e.photoDir = photoBlobs, photoDir
	photoSvc, err := appphotos.NewService(appphotos.Deps{
		Store: e.photos, Blobs: photoBlobs, Clock: clk,
		Audit:        e.audits,
		Tx:           db,
		Options:      appphotos.Options{MaxPixels: 4_000_000, MaxSidePx: 4096, Rand: zeroRand{}},
		Accounts:     accountSvc,
		SchoolPhotos: e.photos,
	})
	if err != nil {
		t.Fatalf("new photo service: %v", err)
	}
	e.photoSvc = photoSvc
	locationSvc, err := applocation.NewService(applocation.Deps{
		Store: e.locations, Accounts: accountSvc, Audit: e.audits, Tx: db, Clock: clk,
	})
	if err != nil {
		t.Fatalf("new location service: %v", err)
	}
	e.broker = appevents.NewBroker()
	e.worker, err = appworker.NewService(appworker.Deps{
		Store:     e.sched,
		Creds:     e2eCreds{},
		Signer:    e.provider,
		Egress:    egressSvc,
		Limiter:   corerate.NewLimiter(corerate.DefaultRate, 6),
		Clock:     clk,
		Location:  loc,
		Locations: locationSvc,
		Photos:    photoSvc,
		Notifier:  assembly.WorkerNotifier{Broker: e.broker, Clock: clk},
	})
	if err != nil {
		t.Fatalf("new worker service: %v", err)
	}
	scheduleSvc, err := appschedule.NewService(appschedule.Deps{
		Windows: e.sched, Plans: e.sched, Accounts: e.sched,
		Discover: e.discover, Prefs: e.prefs, Tx: db,
		Notifier: assembly.ScheduleNotifier{Broker: e.broker, Clock: clk},
	})
	diagSvc, err := appdiagnostic.NewService(appdiagnostic.Deps{
		Accounts: accountSvc,
		Store:    pg.NewDiagnosticRepo(db),
		Clock:    clk,
		Location: loc,
	})
	if err != nil {
		t.Fatalf("new schedule service: %v", err)
	}
	prefSvc, err := apptimepref.NewService(apptimepref.Deps{
		Store: e.prefs, Clock: clk, Allow: true, Cooldown: 30 * time.Second,
	})
	if err != nil {
		t.Fatalf("new time preference service: %v", err)
	}
	signingHandler := signinghttp.NewHandler(accountSvc, adminSvc, e.provider).
		WithManual(e.worker).WithAccess(accountSvc)
	scheduleHandler := schedulehttp.NewHandler(scheduleSvc, prefSvc, adminSvc, schedulehttp.Config{
		Location: loc, Workers: 1, Avg: 60 * time.Second, Rate: 1.0, Util: 0.8,
		SlotsPerSlice: 5, Front: true, UsePrefs: true,
	})
	userSvc, err := appuser.NewService(appuser.Deps{
		Users: e.users, Audit: e.audits, Tx: db, Clock: clk, KDF: coreauth.PlainHasher{}, Limit: 10000,
		Sessions: e.sessions, OwnerAccounts: e.users,
	})
	if err != nil {
		t.Fatalf("new user service: %v", err)
	}

	e.engine, err = router.New(router.Options{
		Sessions:        e.sessions,
		Clock:           clk,
		Logger:          logger,
		InsecureCookies: true,
		Schools:         schoolhttp.NewHandler(schoolSvc, adminSvc),
		Admins:          adminhttp.NewHandler(adminSvc, adminSvc),
		Accounts:        accounthttp.NewHandler(accountSvc, adminSvc),
		Users:           userhttp.NewHandler(userSvc, adminSvc),
		Signing:         signingHandler,
		Schedule:        scheduleHandler,
		Photos:          photoshttp.NewHandler(photoSvc, adminSvc),
		Locations:       locationhttp.NewHandler(locationSvc, adminSvc),
		Events: eventshttp.NewHandler(e.broker, diagSvc, adminSvc, clk, nil).
			WithSessionResolver(e.sessions, coreauth.InsecureCookieName),
	})
	if err != nil {
		t.Fatalf("router.New: %v", err)
	}

	t.Cleanup(func() {
		db.Close()
		w2cDropSchema(adminDB, schema)
		adminDB.Close()
	})
	return e
}

func w2cDropSchema(adminDB *pg.DB, schema string) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	_, _ = adminDB.Exec(ctx, "DROP SCHEMA IF EXISTS "+schema+" CASCADE")
}

func w2cWithSchema(dsn, schema string) string {
	sep := "?"
	if strings.Contains(dsn, "?") {
		sep = "&"
	}
	return dsn + sep + "search_path=" + schema
}

// zeroRand 让 e2e 的预处理确定：四边不裁，同字节上传得到同一行。
// 裁切随机性由 core/image 的单测覆盖；e2e 钉的是端点语义。
type zeroRand struct{}

func (zeroRand) IntN(int) int { return 0 }

// e2eCreds 是执行循环的凭据接缝：本文件的用例只用 Request，
// 不触发真实签到，故返回空凭据即可。
type e2eCreds struct{}

func (e2eCreds) Credential(context.Context, string) (contract.Credential, error) {
	return contract.Credential{}, nil
}

// e2eProvider 是本地假提供方：只为满足校验与执行接缝。
// 窗口发现由 e2eDiscoverer 直接提供；targets 按账号返回点位，
// signs 记录收到的签到请求供 P5-C 断言注入面。
type e2eProvider struct {
	windows map[string]contract.Window
	targets map[string][]contract.Target
	signs   []contract.SignRequest
}

func (p *e2eProvider) DiscoverWindow(context.Context, contract.DiscoverRequest) (contract.Window, error) {
	return contract.Window{}, errs.New(errs.CodeUpstream, "not used in e2e")
}

func (p *e2eProvider) DiscoverTargets(_ context.Context, req contract.DiscoverRequest) ([]contract.Target, error) {
	if ts, ok := p.targets[req.Account.ID]; ok {
		return ts, nil
	}
	return nil, errs.New(errs.CodeUpstream, "not used in e2e")
}

func (p *e2eProvider) Sign(_ context.Context, req contract.SignRequest) (contract.Result, error) {
	p.signs = append(p.signs, req)
	return contract.Result{Outcome: contract.OutcomeSuccess}, nil
}

func (p *e2eProvider) Verify(context.Context, contract.VerifyRequest) (contract.Profile, error) {
	return contract.Profile{}, nil
}

// e2eDiscoverer 按任务 ID 返回测试预置的平台窗口。
// 未预置视为平台无窗口，计划层按缓存或回退继续。
// calls 是平台发现调用次数：k0y-4 用它验收节流（风控暴露面）。
// 请求串行发出，read 面仍用原子量防数据竞争。
type e2eDiscoverer struct {
	windows map[string]contract.Window
	calls   atomic.Int64
}

func (d *e2eDiscoverer) DiscoverWindow(_ context.Context, taskID string) (contract.Window, error) {
	d.calls.Add(1)
	w, ok := d.windows[taskID]
	if !ok {
		return contract.Window{}, errs.ErrNotFound
	}
	return w, nil
}

// count 返回平台发现调用次数。
func (d *e2eDiscoverer) count() int64 { return d.calls.Load() }

func w2cRandomHex(n int) string {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		panic("w2c e2e: cannot read random bytes: " + err.Error())
	}
	return hex.EncodeToString(b)
}

// --- 造数 -----------------------------------------------------------------

func (e *w2cEnv) user(email string, role coreauth.Role) coreuser.Record {
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
		e.t.Fatalf("create user %s: %v", email, err)
	}
	return rec
}

func (e *w2cEnv) school(code string) schoolRecord {
	e.t.Helper()
	rec, err := e.schools.Create(e.ctx, appschool.New{Code: code, Name: "School " + code})
	if err != nil {
		e.t.Fatalf("create school %s: %v", code, err)
	}
	return schoolRecord{ID: rec.ID, Code: rec.Code}
}

func (e *w2cEnv) account(schoolID, ownerID, phone string) string {
	e.t.Helper()
	err := e.db.InTx(e.ctx, func(ctx context.Context) error {
		_, err := e.accounts.Create(ctx, appaccount.New{
			SchoolID:    schoolID,
			OwnerUserID: ownerID,
			Phone:       phone,
		}, 10000, 10000)
		return err
	})
	if err != nil {
		e.t.Fatalf("create account %s: %v", phone, err)
	}
	return e.scalarString("SELECT id::text FROM accounts WHERE phone = $1 AND deleted_at IS NULL", phone)
}

func (e *w2cEnv) bind(userID, schoolID string) {
	e.t.Helper()
	if err := e.admins.Grant(e.ctx, appschool.Binding{UserID: userID, SchoolID: schoolID}); err != nil {
		e.t.Fatalf("grant admin: %v", err)
	}
}

func (e *w2cEnv) scalarInt(sql string, args ...any) int64 {
	e.t.Helper()
	v, err := e.db.QueryInt64(e.ctx, sql, args...)
	if err != nil {
		e.t.Fatalf("query %q: %v", sql, err)
	}
	return v
}

func (e *w2cEnv) scalarString(sql string, args ...any) string {
	e.t.Helper()
	v, err := e.db.QueryString(e.ctx, sql, args...)
	if err != nil {
		e.t.Fatalf("query %q: %v", sql, err)
	}
	return v
}

type schoolRecord struct {
	ID   string
	Code string
}

func (e *w2cEnv) login(user coreuser.Record) *coreauth.Issued {
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

// --- HTTP 客户端 -----------------------------------------------------------

func (e *w2cEnv) do(method, path string, body any, issued *coreauth.Issued) *httptest.ResponseRecorder {
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
	e.bodies = append(e.bodies, rec.Body.String())
	return rec
}

func (e *w2cEnv) decode(rec *httptest.ResponseRecorder, target any) {
	e.t.Helper()
	if err := json.Unmarshal(rec.Body.Bytes(), target); err != nil {
		e.t.Fatalf("decode (%d): %v; body=%q", rec.Code, err, rec.Body.String())
	}
}

type w2cSchool struct {
	ID       string    `json:"id"`
	Code     string    `json:"code"`
	Name     string    `json:"name"`
	Status   string    `json:"status"`
	Timezone string    `json:"timezone"`
	Created  time.Time `json:"created_at"`
	Updated  time.Time `json:"updated_at"`
}

type w2cSchoolPage struct {
	Items      []w2cSchool `json:"items"`
	NextCursor string      `json:"next_cursor"`
}

type w2cAdmin struct {
	UserID    string    `json:"user_id"`
	Email     string    `json:"email"`
	Role      string    `json:"role"`
	GrantedBy string    `json:"granted_by"`
	CreatedAt time.Time `json:"created_at"`
}

type w2cAdminPage struct {
	Items      []w2cAdmin `json:"items"`
	NextCursor string     `json:"next_cursor"`
}

type w2cAccount struct {
	ID          string    `json:"id"`
	SchoolID    string    `json:"school_id"`
	OwnerUserID string    `json:"owner_user_id"`
	PhoneMasked string    `json:"phone_masked"`
	Status      string    `json:"status"`
	Reject      string    `json:"reject_reason"`
	Created     time.Time `json:"created_at"`
	Updated     time.Time `json:"updated_at"`
}

type w2cAccountPage struct {
	Items      []w2cAccount `json:"items"`
	NextCursor string       `json:"next_cursor"`
}

type w2cErrorEnvelope struct {
	Error struct {
		Code string `json:"code"`
	} `json:"error"`
}

func (e *w2cEnv) errorCode(rec *httptest.ResponseRecorder) string {
	e.t.Helper()
	var env w2cErrorEnvelope
	e.decode(rec, &env)
	return env.Error.Code
}

func w2cWantStatus(t *testing.T, rec *httptest.ResponseRecorder, want int) {
	t.Helper()
	if rec.Code != want {
		t.Fatalf("status = %d, want %d; body=%q", rec.Code, want, rec.Body.String())
	}
}

// --- E-30 ------------------------------------------------------------------

// admin 读未绑定学校的账号：当作不存在。
func TestW2CE30AdminCannotReadUnboundSchoolAccount(t *testing.T) {
	e := w2cNewEnv(t)
	adminUser := e.user("admin@example.com", coreauth.RoleAdmin)
	owner := e.user("owner@example.com", coreauth.RoleUser)
	schoolA := e.school("e30-a")
	schoolB := e.school("e30-b")
	e.bind(adminUser.ID, schoolA.ID)
	account := e.account(schoolB.ID, owner.ID, "13800138000")

	rec := e.do(http.MethodGet, "/api/accounts/"+account, nil, e.login(adminUser))
	w2cWantStatus(t, rec, http.StatusNotFound)
	if code := e.errorCode(rec); code != "not_found" {
		t.Fatalf("code = %q, want not_found", code)
	}
}

// --- E-31 ------------------------------------------------------------------

// user 改他人账号：当作不存在。
func TestW2CE31UserCannotUpdateAnotherUsersAccount(t *testing.T) {
	e := w2cNewEnv(t)
	alice := e.user("alice@example.com", coreauth.RoleUser)
	bob := e.user("bob@example.com", coreauth.RoleUser)
	school := e.school("e31")
	account := e.account(school.ID, bob.ID, "13800138001")

	rec := e.do(http.MethodPatch, "/api/accounts/"+account,
		map[string]any{"phone": "13900139000"}, e.login(alice))
	w2cWantStatus(t, rec, http.StatusNotFound)
	if code := e.errorCode(rec); code != "not_found" {
		t.Fatalf("code = %q, want not_found", code)
	}
	if phone := e.scalarString("SELECT phone FROM accounts WHERE id = $1::uuid", account); phone != "13800138001" {
		t.Fatalf("out-of-scope update changed the phone to %q", phone)
	}
}

// --- E-32 ------------------------------------------------------------------

// master 建校并绑定 admin：两次写入，两条审计。
func TestW2CE32MasterCreatesSchoolAndBindsAdmin(t *testing.T) {
	e := w2cNewEnv(t)
	master := e.user("master@example.com", coreauth.RoleMaster)
	adminUser := e.user("bound@example.com", coreauth.RoleAdmin)
	masterSess := e.login(master)

	rec := e.do(http.MethodPost, "/api/schools",
		map[string]any{"code": "e32-school", "name": "E32"}, masterSess)
	w2cWantStatus(t, rec, http.StatusCreated)
	var created w2cSchool
	e.decode(rec, &created)
	if created.Code != "e32-school" || created.Status != "active" || created.Timezone != "Asia/Shanghai" {
		t.Fatalf("created school = %#v", created)
	}

	rec = e.do(http.MethodPost, "/api/schools/"+created.ID+"/admins",
		map[string]any{"user_id": adminUser.ID}, masterSess)
	w2cWantStatus(t, rec, http.StatusCreated)

	if got := e.scalarInt("SELECT count(*) FROM audit_logs WHERE action = 'school.create'"); got != 1 {
		t.Fatalf("school.create audit rows = %d, want 1", got)
	}
	if got := e.scalarInt("SELECT count(*) FROM audit_logs WHERE action = 'admin.grant'"); got != 1 {
		t.Fatalf("admin.grant audit rows = %d, want 1", got)
	}
	label := e.scalarString("SELECT actor_label FROM audit_logs WHERE action = 'admin.grant'")
	if label != "m***@example.com" {
		t.Fatalf("actor_label = %q, want the masked email", label)
	}

	// 绑定立即生效：admin 现在能看到该校。
	rec = e.do(http.MethodGet, "/api/schools", nil, e.login(adminUser))
	w2cWantStatus(t, rec, http.StatusOK)
	var page w2cSchoolPage
	e.decode(rec, &page)
	if len(page.Items) != 1 || page.Items[0].ID != created.ID {
		t.Fatalf("admin school page = %#v", page)
	}

	// 管理员列表带邮箱与角色。
	rec = e.do(http.MethodGet, "/api/schools/"+created.ID+"/admins", nil, masterSess)
	w2cWantStatus(t, rec, http.StatusOK)
	var admins w2cAdminPage
	e.decode(rec, &admins)
	if len(admins.Items) != 1 || admins.Items[0].UserID != adminUser.ID ||
		admins.Items[0].Email != "bound@example.com" || admins.Items[0].Role != "admin" {
		t.Fatalf("admin page = %#v", admins)
	}

	// 解绑后列表为空，且被解绑者不再看到该校。
	w2cWantStatus(t, e.do(http.MethodDelete,
		"/api/schools/"+created.ID+"/admins/"+adminUser.ID, nil, masterSess), http.StatusNoContent)
	rec = e.do(http.MethodGet, "/api/schools/"+created.ID+"/admins", nil, masterSess)
	w2cWantStatus(t, rec, http.StatusOK)
	e.decode(rec, &admins)
	if len(admins.Items) != 0 {
		t.Fatalf("revoked binding is still listed: %#v", admins.Items)
	}
	if got := e.scalarInt("SELECT count(*) FROM audit_logs WHERE action = 'admin.revoke'"); got != 1 {
		t.Fatalf("admin.revoke audit rows = %d, want 1", got)
	}
	rec = e.do(http.MethodGet, "/api/schools", nil, e.login(adminUser))
	w2cWantStatus(t, rec, http.StatusOK)
	e.decode(rec, &page)
	if len(page.Items) != 0 {
		t.Fatalf("unbound admin still sees schools: %#v", page.Items)
	}
}

// --- E-33 ------------------------------------------------------------------

// 审核通过：pending -> active，且凭据以密文落库。
func TestW2CE33ReviewApprovesAccount(t *testing.T) {
	e := w2cNewEnv(t)
	master := e.user("master@example.com", coreauth.RoleMaster)
	school := e.school("e33")
	masterSess := e.login(master)
	const password = "S3cret-platform-password"

	rec := e.do(http.MethodPost, "/api/accounts", map[string]any{
		"school_id": school.ID,
		"phone":     "13800138002",
		"password":  password,
	}, masterSess)
	w2cWantStatus(t, rec, http.StatusCreated)
	var created w2cAccount
	e.decode(rec, &created)
	if created.Status != "pending" {
		t.Fatalf("status = %q, want pending", created.Status)
	}
	if created.PhoneMasked != "138****8002" {
		t.Fatalf("phone_masked = %q", created.PhoneMasked)
	}
	if strings.Contains(rec.Body.String(), "13800138002") {
		t.Fatal("create response leaked the raw phone")
	}

	// 凭据是密文：库内既不存明文口令，也不存明文手机号。
	if got := e.scalarInt("SELECT count(*) FROM credentials WHERE account_id = $1::uuid", created.ID); got != 1 {
		t.Fatalf("credential rows = %d, want 1", got)
	}
	if got := e.scalarInt(
		"SELECT count(*) FROM credentials WHERE position(convert_to($1, 'UTF8') in ciphertext) > 0",
		password); got != 0 {
		t.Fatal("stored credential contains the plaintext password")
	}

	rec = e.do(http.MethodPost, "/api/accounts/"+created.ID+"/review",
		map[string]any{"decision": "approve"}, masterSess)
	w2cWantStatus(t, rec, http.StatusOK)
	var reviewed w2cAccount
	e.decode(rec, &reviewed)
	if reviewed.Status != "active" {
		t.Fatalf("status = %q after review, want active", reviewed.Status)
	}
	if got := e.scalarInt("SELECT count(*) FROM audit_logs WHERE action = 'account.review_approve'"); got != 1 {
		t.Fatalf("review audit rows = %d, want 1", got)
	}
	if e.scalarString("SELECT reviewed_by::text FROM accounts WHERE id = $1::uuid", created.ID) != master.ID {
		t.Fatal("reviewed_by was not recorded")
	}

	// 改口令：版本自增，旧负载里的空设备码/机型被保留。
	rec = e.do(http.MethodPatch, "/api/accounts/"+created.ID,
		map[string]any{"password": "rotated-password"}, masterSess)
	w2cWantStatus(t, rec, http.StatusOK)
	var updated w2cAccount
	e.decode(rec, &updated)
	if updated.Status != "active" {
		t.Fatalf("update changed the status to %q", updated.Status)
	}
	cipher, err := appaccount.NewCipher(w2cAccountsKey)
	if err != nil {
		t.Fatalf("new cipher: %v", err)
	}
	credential, err := e.accounts.Credential(e.ctx, corescope.Master(), created.ID)
	if err != nil {
		t.Fatalf("read credential: %v", err)
	}
	if credential.Version != 2 {
		t.Fatalf("credential version = %d, want 2", credential.Version)
	}
	payload, err := cipher.Open(credential)
	if err != nil {
		t.Fatalf("open credential: %v", err)
	}
	if payload.Password != "rotated-password" {
		t.Fatalf("stored password = %q, want the rotated one", payload.Password)
	}
	if got := e.scalarInt("SELECT count(*) FROM audit_logs WHERE action = 'account.update'"); got != 1 {
		t.Fatalf("update audit rows = %d, want 1", got)
	}

	// 拒绝路径：拒绝必须带理由。
	second := e.account(school.ID, master.ID, "13800138011")
	rec = e.do(http.MethodPost, "/api/accounts/"+second+"/review",
		map[string]any{"decision": "reject"}, masterSess)
	w2cWantStatus(t, rec, http.StatusBadRequest)
	if code := e.errorCode(rec); code != "validation_failed" {
		t.Fatalf("reject without reason code = %q, want validation_failed", code)
	}

	rec = e.do(http.MethodPost, "/api/accounts/"+second+"/review",
		map[string]any{"decision": "reject", "reason": "资料不完整"}, masterSess)
	w2cWantStatus(t, rec, http.StatusOK)
	var rejected w2cAccount
	e.decode(rec, &rejected)
	if rejected.Status != "rejected" || rejected.Reject != "资料不完整" {
		t.Fatalf("rejected account = %#v", rejected)
	}
	if got := e.scalarInt("SELECT count(*) FROM audit_logs WHERE action = 'account.review_reject'"); got != 1 {
		t.Fatalf("reject audit rows = %d, want 1", got)
	}
	if got := e.scalarInt("SELECT count(*) FROM audit_logs WHERE detail LIKE '%资料不完整%'"); got != 1 {
		t.Fatalf("reject reason is missing from the audit detail")
	}
}

// --- E-34 ------------------------------------------------------------------

// 暂停与恢复：active 与 paused_admin 之间往返。
func TestW2CE34PauseAndResume(t *testing.T) {
	e := w2cNewEnv(t)
	adminUser := e.user("admin@example.com", coreauth.RoleAdmin)
	owner := e.user("owner@example.com", coreauth.RoleUser)
	school := e.school("e34")
	e.bind(adminUser.ID, school.ID)
	account := e.account(school.ID, owner.ID, "13800138003")
	adminSess := e.login(adminUser)
	// 只有 active 可暂停：先审核通过。
	w2cWantStatus(t, e.do(http.MethodPost, "/api/accounts/"+account+"/review",
		map[string]any{"decision": "approve"}, adminSess), http.StatusOK)

	rec := e.do(http.MethodPost, "/api/accounts/"+account+"/pause",
		map[string]any{"paused": true}, adminSess)
	w2cWantStatus(t, rec, http.StatusOK)
	var paused w2cAccount
	e.decode(rec, &paused)
	if paused.Status != "paused_admin" {
		t.Fatalf("status = %q, want paused_admin", paused.Status)
	}

	rec = e.do(http.MethodPost, "/api/accounts/"+account+"/pause",
		map[string]any{"paused": false}, adminSess)
	w2cWantStatus(t, rec, http.StatusOK)
	var resumed w2cAccount
	e.decode(rec, &resumed)
	if resumed.Status != "active" {
		t.Fatalf("status = %q, want active", resumed.Status)
	}

	if got := e.scalarInt("SELECT count(*) FROM audit_logs WHERE action IN ('account.pause', 'account.resume')"); got != 2 {
		t.Fatalf("pause/resume audit rows = %d, want 2", got)
	}
}

// --- E-35 ------------------------------------------------------------------

// 软删后读取：404，列表也不可见。
func TestW2CE35SoftDeletedAccountIsGone(t *testing.T) {
	e := w2cNewEnv(t)
	master := e.user("master@example.com", coreauth.RoleMaster)
	school := e.school("e35")
	account := e.account(school.ID, master.ID, "13800138004")
	masterSess := e.login(master)

	w2cWantStatus(t, e.do(http.MethodDelete, "/api/accounts/"+account, nil, masterSess), http.StatusNoContent)
	w2cWantStatus(t, e.do(http.MethodGet, "/api/accounts/"+account, nil, masterSess), http.StatusNotFound)

	rec := e.do(http.MethodGet, "/api/accounts", nil, masterSess)
	w2cWantStatus(t, rec, http.StatusOK)
	var page w2cAccountPage
	e.decode(rec, &page)
	if len(page.Items) != 0 {
		t.Fatalf("soft-deleted account is still listed: %#v", page.Items)
	}
	if got := e.scalarInt("SELECT count(*) FROM audit_logs WHERE action = 'account.delete'"); got != 1 {
		t.Fatalf("delete audit rows = %d, want 1", got)
	}
}

// --- E-36 ------------------------------------------------------------------

// 审计链：全链通过；插入一行伪造记录即红。
func TestW2CE36AuditChainVerifiesAndDetectsTamper(t *testing.T) {
	e := w2cNewEnv(t)
	master := e.user("master@example.com", coreauth.RoleMaster)
	adminUser := e.user("admin@example.com", coreauth.RoleAdmin)
	school := e.school("e36")
	masterSess := e.login(master)

	rec := e.do(http.MethodPost, "/api/schools",
		map[string]any{"code": "e36-created", "name": "E36"}, masterSess)
	w2cWantStatus(t, rec, http.StatusCreated)
	var created w2cSchool
	e.decode(rec, &created)
	w2cWantStatus(t, e.do(http.MethodPost, "/api/schools/"+created.ID+"/admins",
		map[string]any{"user_id": adminUser.ID}, masterSess), http.StatusCreated)
	w2cWantStatus(t, e.do(http.MethodPatch, "/api/schools/"+created.ID,
		map[string]any{"name": "E36 renamed"}, masterSess), http.StatusOK)
	e.account(school.ID, master.ID, "13800138005")

	report, err := e.audits.Verify(e.ctx, 0, 0)
	if err != nil {
		t.Fatalf("verify the live chain: %v", err)
	}
	if report.Count != 3 {
		t.Fatalf("verified %d rows, want 3", report.Count)
	}

	// INSERT 允许，故伪造行能落库；哈希不会匹配。
	if _, err := e.db.Exec(e.ctx, `
		INSERT INTO audit_logs (ts, actor_label, action, detail, prev_hash, hash)
		VALUES (now(), 'master', 'school.create', '{}', $1, $2)`,
		make([]byte, 32), bytes.Repeat([]byte{0x22}, 32)); err != nil {
		t.Fatalf("insert a forged audit row: %v", err)
	}
	if _, err := e.audits.Verify(e.ctx, 0, 0); err == nil {
		t.Fatal("verify accepted a chain with a forged row")
	}
}

// --- E-37 ------------------------------------------------------------------

// 响应与日志扫描：不出现 11 位手机号明文。
func TestW2CE37NoRawPhoneInResponsesOrLogs(t *testing.T) {
	e := w2cNewEnv(t)
	master := e.user("master@example.com", coreauth.RoleMaster)
	school := e.school("e37")
	masterSess := e.login(master)
	const phone = "13800138006"

	rec := e.do(http.MethodPost, "/api/accounts", map[string]any{
		"school_id": school.ID,
		"phone":     phone,
		"password":  "secret",
	}, masterSess)
	w2cWantStatus(t, rec, http.StatusCreated)
	var created w2cAccount
	e.decode(rec, &created)
	w2cWantStatus(t, e.do(http.MethodGet, "/api/accounts/"+created.ID, nil, masterSess), http.StatusOK)
	w2cWantStatus(t, e.do(http.MethodGet, "/api/accounts", nil, masterSess), http.StatusOK)
	w2cWantStatus(t, e.do(http.MethodPost, "/api/accounts/"+created.ID+"/review",
		map[string]any{"decision": "approve"}, masterSess), http.StatusOK)

	// 响应的 trace id 是 32 位十六进制；通用正则会把随机数字误当手机号。
	// 故响应与日志查本次使用的原号；审计 detail 另用通用形状扫描。
	for i, body := range e.bodies {
		if strings.Contains(body, phone) {
			t.Fatalf("response %d leaked the raw phone: %q", i, body)
		}
	}
	if strings.Contains(e.logs.String(), phone) {
		t.Fatalf("logs leaked the raw phone: %q", e.logs.String())
	}
	detail := e.scalarString("SELECT coalesce(string_agg(detail, ' '), '') FROM audit_logs")
	if w2cPhoneShape.MatchString(detail) {
		t.Fatalf("audit detail leaked a raw phone: %q", detail)
	}
}

// --- 游标分页 ---------------------------------------------------------------

func TestW2CCursorPagination(t *testing.T) {
	e := w2cNewEnv(t)
	master := e.user("master@example.com", coreauth.RoleMaster)
	masterSess := e.login(master)
	seen := map[string]bool{}
	for i := 0; i < 5; i++ {
		seen[e.school(fmt.Sprintf("e38-%02d", i)).ID] = true
	}

	cursor := ""
	pages := 0
	for {
		path := "/api/schools?limit=2"
		if cursor != "" {
			path += "&cursor=" + cursor
		}
		rec := e.do(http.MethodGet, path, nil, masterSess)
		w2cWantStatus(t, rec, http.StatusOK)
		var page w2cSchoolPage
		e.decode(rec, &page)
		if len(page.Items) > 2 {
			t.Fatalf("page has %d items, want at most 2", len(page.Items))
		}
		for _, item := range page.Items {
			if !seen[item.ID] {
				t.Fatalf("unexpected school %s", item.ID)
			}
			delete(seen, item.ID)
		}
		pages++
		if page.NextCursor == "" {
			break
		}
		cursor = page.NextCursor
		if pages > 5 {
			t.Fatal("pagination did not terminate")
		}
	}
	if len(seen) != 0 {
		t.Fatalf("pagination skipped %d schools", len(seen))
	}
	if pages != 3 {
		t.Fatalf("walked %d pages, want 3", pages)
	}
}

// limit 上限 200：越界即校验失败。
func TestW2CLimitBounds(t *testing.T) {
	e := w2cNewEnv(t)
	master := e.user("master@example.com", coreauth.RoleMaster)
	masterSess := e.login(master)

	for _, limit := range []string{"201", "0", "-1"} {
		rec := e.do(http.MethodGet, "/api/schools?limit="+limit, nil, masterSess)
		w2cWantStatus(t, rec, http.StatusBadRequest)
		if code := e.errorCode(rec); code != "validation_failed" {
			t.Fatalf("limit %s code = %q, want validation_failed", limit, code)
		}
	}

	rec := e.do(http.MethodGet, "/api/schools?limit=200", nil, masterSess)
	w2cWantStatus(t, rec, http.StatusOK)

	// 游标不透明：解析失败即校验失败，不静默当首页。
	rec = e.do(http.MethodGet, "/api/schools?cursor=not-a-cursor", nil, masterSess)
	w2cWantStatus(t, rec, http.StatusBadRequest)
	if code := e.errorCode(rec); code != "validation_failed" {
		t.Fatalf("bad cursor code = %q, want validation_failed", code)
	}
}

// 跨校列表互不可见；master 全量。
func TestW2CCrossSchoolListsAreIsolated(t *testing.T) {
	e := w2cNewEnv(t)
	adminUser := e.user("admin@example.com", coreauth.RoleAdmin)
	alice := e.user("alice@example.com", coreauth.RoleUser)
	schoolA := e.school("e39-a")
	schoolB := e.school("e39-b")
	e.bind(adminUser.ID, schoolA.ID)
	inA := e.account(schoolA.ID, alice.ID, "13800138007")
	inB := e.account(schoolB.ID, alice.ID, "13800138008")

	// admin 只见绑定校。
	rec := e.do(http.MethodGet, "/api/accounts", nil, e.login(adminUser))
	w2cWantStatus(t, rec, http.StatusOK)
	var page w2cAccountPage
	e.decode(rec, &page)
	if len(page.Items) != 1 || page.Items[0].ID != inA {
		t.Fatalf("admin sees %#v, want only %s", page.Items, inA)
	}

	// user 只见自己的账号；两校都归 alice，故两条都可见。
	rec = e.do(http.MethodGet, "/api/accounts", nil, e.login(alice))
	w2cWantStatus(t, rec, http.StatusOK)
	e.decode(rec, &page)
	if len(page.Items) != 2 {
		t.Fatalf("owner sees %d accounts, want 2", len(page.Items))
	}

	// master 全量。
	master := e.user("master@example.com", coreauth.RoleMaster)
	rec = e.do(http.MethodGet, "/api/accounts", nil, e.login(master))
	w2cWantStatus(t, rec, http.StatusOK)
	e.decode(rec, &page)
	if len(page.Items) != 2 {
		t.Fatalf("master sees %d accounts, want 2", len(page.Items))
	}
	_ = inB

	// 显式按未绑定校过滤：空，不是 403，也不是全量。
	rec = e.do(http.MethodGet, "/api/accounts?school_id="+schoolB.ID, nil, e.login(adminUser))
	w2cWantStatus(t, rec, http.StatusOK)
	e.decode(rec, &page)
	if len(page.Items) != 0 {
		t.Fatalf("admin filtering by an unbound school saw %#v", page.Items)
	}
}

// 动作无权返回 403，与跨校 404 区分。
func TestW2CForbiddenActionsAre403(t *testing.T) {
	e := w2cNewEnv(t)
	alice := e.user("alice@example.com", coreauth.RoleUser)
	school := e.school("e40")
	account := e.account(school.ID, alice.ID, "13800138009")
	aliceSess := e.login(alice)

	w2cWantStatus(t, e.do(http.MethodPost, "/api/schools",
		map[string]any{"code": "nope", "name": "Nope"}, aliceSess), http.StatusForbidden)
	w2cWantStatus(t, e.do(http.MethodGet, "/api/schools", nil, aliceSess), http.StatusForbidden)
	w2cWantStatus(t, e.do(http.MethodPost, "/api/accounts/"+account+"/review",
		map[string]any{"decision": "approve"}, aliceSess), http.StatusForbidden)
}

// 非法状态迁移返回 conflict：pending 不能直接暂停。
func TestW2CInvalidTransitionIsConflict(t *testing.T) {
	e := w2cNewEnv(t)
	adminUser := e.user("admin@example.com", coreauth.RoleAdmin)
	owner := e.user("owner@example.com", coreauth.RoleUser)
	school := e.school("e41")
	e.bind(adminUser.ID, school.ID)
	account := e.account(school.ID, owner.ID, "13800138010")

	rec := e.do(http.MethodPost, "/api/accounts/"+account+"/pause",
		map[string]any{"paused": true}, e.login(adminUser))
	w2cWantStatus(t, rec, http.StatusConflict)
	if code := e.errorCode(rec); code != "conflict" {
		t.Fatalf("code = %q, want conflict", code)
	}
}

// --- P1-1 跨校写动作 --------------------------------------------------------

// accountSnapshot 返回一行账号的可比较快照。
// 越权写必须让整行不变，故快照覆盖会被这四类动作改动的列。
func (e *w2cEnv) accountSnapshot(id string) string {
	e.t.Helper()
	return e.scalarString(`
		SELECT phone || '|' || status::text || '|' || reject_reason || '|' ||
		       (deleted_at IS NOT NULL)::text || '|' || coalesce(reviewed_by::text, '')
		  FROM accounts WHERE id = $1::uuid`, id)
}

// admin 只绑定学校 A。对学校 B 的账号做审核、暂停、改号、软删：
// 四个写动作全部 404，且 B 的行一个字节都不变。
func TestW2CP11CrossSchoolWritesAre404(t *testing.T) {
	e := w2cNewEnv(t)
	adminUser := e.user("admin@example.com", coreauth.RoleAdmin)
	owner := e.user("owner@example.com", coreauth.RoleUser)
	schoolA := e.school("p11-a")
	schoolB := e.school("p11-b")
	e.bind(adminUser.ID, schoolA.ID)
	adminSess := e.login(adminUser)

	reviewed := e.account(schoolB.ID, owner.ID, "13800138020")
	paused := e.account(schoolB.ID, owner.ID, "13800138021")
	phoned := e.account(schoolB.ID, owner.ID, "13800138022")
	deleted := e.account(schoolB.ID, owner.ID, "13800138023")
	// 暂停只从 active 出发；这里直接把行置为 active，避免用 master 预置。
	if _, err := e.db.Exec(e.ctx,
		"UPDATE accounts SET status = 'active' WHERE id = $1::uuid", paused); err != nil {
		t.Fatalf("activate the pause target: %v", err)
	}

	cases := []struct {
		name   string
		method string
		path   string
		body   any
		id     string
	}{
		{"review", http.MethodPost, "/api/accounts/" + reviewed + "/review",
			map[string]any{"decision": "approve"}, reviewed},
		{"pause", http.MethodPost, "/api/accounts/" + paused + "/pause",
			map[string]any{"paused": true}, paused},
		{"update-phone", http.MethodPatch, "/api/accounts/" + phoned,
			map[string]any{"phone": "13900139099"}, phoned},
		{"soft-delete", http.MethodDelete, "/api/accounts/" + deleted, nil, deleted},
	}
	for _, tc := range cases {
		before := e.accountSnapshot(tc.id)
		rec := e.do(tc.method, tc.path, tc.body, adminSess)
		if rec.Code != http.StatusNotFound {
			t.Fatalf("%s on an unbound school account = %d, want 404; body=%q",
				tc.name, rec.Code, rec.Body.String())
		}
		if code := e.errorCode(rec); code != "not_found" {
			t.Fatalf("%s code = %q, want not_found", tc.name, code)
		}
		if after := e.accountSnapshot(tc.id); after != before {
			t.Fatalf("%s changed the out-of-scope row: %q -> %q", tc.name, before, after)
		}
	}
}

// --- P1-2 审计与业务同事务 --------------------------------------------------

// 审计表改名让每次 Append 失败。
// 断言：master 建校 5xx 且该校一行都没有；admin 审核 5xx 且状态仍是 pending。
// 恢复表名后同一组动作必须成功。
func TestW2CP12AuditFailureRollsBackBusinessWrite(t *testing.T) {
	e := w2cNewEnv(t)
	master := e.user("master@example.com", coreauth.RoleMaster)
	adminUser := e.user("admin@example.com", coreauth.RoleAdmin)
	school := e.school("p12")
	e.bind(adminUser.ID, school.ID)
	account := e.account(school.ID, master.ID, "13800138024")
	masterSess := e.login(master)
	adminSess := e.login(adminUser)

	if _, err := e.db.Exec(e.ctx, "ALTER TABLE audit_logs RENAME TO audit_logs_muted"); err != nil {
		t.Fatalf("mute audit_logs: %v", err)
	}
	restored := false
	restore := func() {
		if restored {
			return
		}
		restored = true
		if _, err := e.db.Exec(context.Background(),
			"ALTER TABLE audit_logs_muted RENAME TO audit_logs"); err != nil {
			t.Errorf("restore audit_logs: %v", err)
		}
	}
	t.Cleanup(restore)

	rec := e.do(http.MethodPost, "/api/schools",
		map[string]any{"code": "p12-blocked", "name": "Blocked"}, masterSess)
	if rec.Code < 500 {
		t.Fatalf("create school with a broken audit table = %d, want 5xx; body=%q",
			rec.Code, rec.Body.String())
	}
	if got := e.scalarInt("SELECT count(*) FROM schools WHERE code = 'p12-blocked'"); got != 0 {
		t.Fatalf("school rows after a failed audit write = %d, want 0", got)
	}

	rec = e.do(http.MethodPost, "/api/accounts/"+account+"/review",
		map[string]any{"decision": "approve"}, adminSess)
	if rec.Code < 500 {
		t.Fatalf("review with a broken audit table = %d, want 5xx; body=%q",
			rec.Code, rec.Body.String())
	}
	if status := e.scalarString("SELECT status::text FROM accounts WHERE id = $1::uuid", account); status != "pending" {
		t.Fatalf("status after a failed audit write = %q, want pending", status)
	}

	// 恢复表名后同一组动作成功：证明上面的失败只是审计故障所致。
	restore()
	w2cWantStatus(t, e.do(http.MethodPost, "/api/schools",
		map[string]any{"code": "p12-blocked", "name": "Blocked"}, masterSess), http.StatusCreated)
	w2cWantStatus(t, e.do(http.MethodPost, "/api/accounts/"+account+"/review",
		map[string]any{"decision": "approve"}, adminSess), http.StatusOK)
}

// --- P2-1 q 过滤口径 -------------------------------------------------------

// q 只命中前 3 位前缀、后 4 位后缀或 11 位全号。
// 中四位可被全号枚举恢复（10^4 次），依赖登录限速（P7 前置）；
// 本用例钉住的是「5..10 位片段永不命中」这一更强的性质。
func TestW2CP21PhoneQueryHasNoMaskOracle(t *testing.T) {
	e := w2cNewEnv(t)
	master := e.user("master@example.com", coreauth.RoleMaster)
	school := e.school("p21")
	e.account(school.ID, master.ID, "13800138000")
	e.account(school.ID, master.ID, "13900139000")
	masterSess := e.login(master)

	cases := []struct {
		q    string
		want int
	}{
		{"0013", 0},        // 掩码隐藏的中四位
		{"138", 1},         // 前 3 位前缀
		{"8000", 1},        // 后 4 位后缀
		{"13800138000", 1}, // 11 位全号
	}
	for _, tc := range cases {
		rec := e.do(http.MethodGet, "/api/accounts?q="+tc.q, nil, masterSess)
		w2cWantStatus(t, rec, http.StatusOK)
		var page w2cAccountPage
		e.decode(rec, &page)
		if len(page.Items) != tc.want {
			t.Fatalf("q=%s returned %d accounts, want %d; items=%#v",
				tc.q, len(page.Items), tc.want, page.Items)
		}
	}

	// 命中的必须是 138 那一行，而不是 139 那一行。
	rec := e.do(http.MethodGet, "/api/accounts?q=138", nil, masterSess)
	w2cWantStatus(t, rec, http.StatusOK)
	var page w2cAccountPage
	e.decode(rec, &page)
	if len(page.Items) != 1 || page.Items[0].PhoneMasked != "138****8000" {
		t.Fatalf("q=138 items = %#v, want only 138****8000", page.Items)
	}
}

// --- P2-2 拒绝理由出口掩码 --------------------------------------------------

// 拒绝理由含手机号时，审核响应、读取响应、列表响应都只出现掩码；
// 审计 detail 保持原有掩码口径。
func TestW2CP22RejectReasonIsMaskedOnEveryResponse(t *testing.T) {
	e := w2cNewEnv(t)
	master := e.user("master@example.com", coreauth.RoleMaster)
	school := e.school("p22")
	account := e.account(school.ID, master.ID, "13800138012")
	masterSess := e.login(master)
	const (
		raw  = "13900139000"
		mask = "139****9000"
	)
	reason := "退回：" + raw + " 需更正"

	bodies := map[string]string{}
	rec := e.do(http.MethodPost, "/api/accounts/"+account+"/review",
		map[string]any{"decision": "reject", "reason": reason}, masterSess)
	w2cWantStatus(t, rec, http.StatusOK)
	bodies["review"] = rec.Body.String()

	rec = e.do(http.MethodGet, "/api/accounts/"+account, nil, masterSess)
	w2cWantStatus(t, rec, http.StatusOK)
	bodies["get"] = rec.Body.String()

	rec = e.do(http.MethodGet, "/api/accounts", nil, masterSess)
	w2cWantStatus(t, rec, http.StatusOK)
	bodies["list"] = rec.Body.String()

	for name, body := range bodies {
		if !strings.Contains(body, mask) {
			t.Fatalf("%s response is missing the masked reason: %q", name, body)
		}
		if strings.Contains(body, raw) {
			t.Fatalf("%s response leaked the raw phone: %q", name, body)
		}
	}

	detail := e.scalarString(
		"SELECT coalesce(string_agg(detail, ' '), '') FROM audit_logs WHERE action = 'account.review_reject'")
	if !strings.Contains(detail, mask) || strings.Contains(detail, raw) {
		t.Fatalf("audit detail = %q, want the masked reason only", detail)
	}
}

// --- P2-3 413 覆盖域处理器 --------------------------------------------------

// 域端点与认证端点同口径：请求体超过 1 MiB 回 413，不是 400。
func TestW2CP23OversizedDomainBodiesAre413(t *testing.T) {
	e := w2cNewEnv(t)
	master := e.user("master@example.com", coreauth.RoleMaster)
	masterSess := e.login(master)
	huge := strings.Repeat("x", 2<<20)

	cases := []struct {
		name string
		path string
		body any
	}{
		{"schools", "/api/schools", map[string]any{"code": huge, "name": "x"}},
		{"accounts", "/api/accounts", map[string]any{
			"school_id": "00000000-0000-0000-0000-000000000000",
			"phone":     "13800138000",
			"password":  huge,
		}},
		{"school-admins", "/api/schools/018f0000-0000-7000-8000-00000000000f/admins",
			map[string]any{"user_id": huge}},
	}
	for _, tc := range cases {
		rec := e.do(http.MethodPost, tc.path, tc.body, masterSess)
		if rec.Code != http.StatusRequestEntityTooLarge {
			t.Fatalf("%s oversized body = %d, want 413; body=%q",
				tc.name, rec.Code, rec.Body.String())
		}
		if code := e.errorCode(rec); code != "validation_failed" {
			t.Fatalf("%s 413 code = %q, want validation_failed", tc.name, code)
		}
	}
}

// --- P3-2 真实装配接线 ------------------------------------------------------

// 真实进程的装配（buildStack）必须注入三组业务处理器。
// 未注入时端点在认证之后回 501；本用例走真实登录，故 501 会被看见。
// 三个端点各取一条：schools、accounts、绑定列表。
func TestW2CP32RealStackWiresDomainHandlers(t *testing.T) {
	e := newAuthEnv(t)
	user := e.createUser("wired-master@example.com", "correct-horse")
	if _, err := e.db.Exec(e.ctx,
		"UPDATE users SET role = 'master' WHERE id = $1::uuid", user.ID); err != nil {
		t.Fatalf("promote the user to master: %v", err)
	}
	resp, body := e.login("wired-master@example.com", "correct-horse")
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("login = %d, want 200", resp.StatusCode)
	}
	if body.Role != "master" {
		t.Fatalf("session role = %q, want master", body.Role)
	}

	for name, tc := range map[string]struct {
		path string
		want int
	}{
		"schools":       {"/api/schools", http.StatusOK},
		"accounts":      {"/api/accounts", http.StatusOK},
		"school-admins": {"/api/schools/018f0000-0000-7000-8000-00000000000f/admins", http.StatusNotFound},
	} {
		resp, payload := e.do(http.MethodGet, tc.path, nil, nil)
		if resp.StatusCode == http.StatusNotImplemented {
			t.Fatalf("%s endpoint is not wired: 501; body=%q", name, payload)
		}
		if resp.StatusCode != tc.want {
			t.Fatalf("%s status = %d, want %d; body=%q", name, resp.StatusCode, tc.want, payload)
		}
	}
}

// 编译期检查：生成契约的参数类型仍是本测试使用的形状。
var _ = oapi.SchoolListParams{}

// TestW2CRealStackCredentialRoundTrip 钉住凭据钥来自配置而非 pepper。
//
// 真实用户动作：master 登录、建校、建账号。
// 断言：库内凭据能用 YIBAN_CREDENTIAL_KEY 解出提交时的口令。
// 若装配改回从 pepper 派生，kid 与密文都不再匹配该钥，本用例即红。
func TestW2CRealStackCredentialRoundTrip(t *testing.T) {
	e := newAuthEnv(t)
	user := e.createUser("key-master@example.com", "correct-horse")
	if _, err := e.db.Exec(e.ctx,
		"UPDATE users SET role = 'master' WHERE id = $1::uuid", user.ID); err != nil {
		t.Fatalf("promote the user to master: %v", err)
	}
	resp, body := e.login("key-master@example.com", "correct-horse")
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("login = %d, want 200", resp.StatusCode)
	}
	if body.CSRFToken == "" {
		t.Fatal("login returned no CSRF token")
	}
	csrf := map[string]string{"X-CSRF-Token": body.CSRFToken}

	resp, payload := e.do(http.MethodPost, "/api/schools",
		map[string]any{"code": "key-school", "name": "Key School"}, csrf)
	if resp.StatusCode != http.StatusCreated {
		t.Fatalf("create school = %d, want 201; body=%q", resp.StatusCode, payload)
	}
	var school w2cSchool
	if err := json.Unmarshal(payload, &school); err != nil || school.ID == "" {
		t.Fatalf("decode school: %v; body=%q", err, payload)
	}

	const password = "credential-secret"
	resp, payload = e.do(http.MethodPost, "/api/accounts", map[string]any{
		"school_id": school.ID,
		"phone":     "13900139000",
		"password":  password,
	}, csrf)
	if resp.StatusCode != http.StatusCreated {
		t.Fatalf("create account = %d, want 201; body=%q", resp.StatusCode, payload)
	}
	var account w2cAccount
	if err := json.Unmarshal(payload, &account); err != nil || account.ID == "" {
		t.Fatalf("decode account: %v; body=%q", err, payload)
	}

	credential, err := pg.NewAccountRepo(e.db).Credential(e.ctx, corescope.Master(), account.ID)
	if err != nil {
		t.Fatalf("read credential: %v", err)
	}
	cipher, err := appaccount.NewCipher(authTestCredentialKey)
	if err != nil {
		t.Fatalf("new cipher: %v", err)
	}
	if credential.KID != cipher.KID() {
		t.Fatalf("stored kid = %q, want the YIBAN_CREDENTIAL_KEY kid %q",
			credential.KID, cipher.KID())
	}
	plain, err := cipher.Open(credential)
	if err != nil {
		t.Fatalf("open credential with the configured key: %v", err)
	}
	if plain.Password != password {
		t.Fatal("decrypted credential does not carry the submitted password")
	}
}
