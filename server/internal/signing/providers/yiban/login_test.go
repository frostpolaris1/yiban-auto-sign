package yiban

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/protocol"
)

const (
	testPhone    = "13800000000"
	testPassword = "secret"
	testPageUse  = "a1b2c3d4e5f60718"
	testVerify   = "abcdef0123456789"
)

type manualClock struct {
	mu  sync.Mutex
	now time.Time
}

func (c *manualClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

func (c *manualClock) Advance(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.now = c.now.Add(d)
}

type upstream struct {
	server     *httptest.Server
	authServer *httptest.Server
	key        *rsa.PrivateKey
	public     string

	challenge    bool
	usersureCode string
	authBody     []byte
	authFixture  []byte
	authRedirect string
	authLoop     bool
	missingToken bool

	signPositionFixture []byte
	signPositionBody    []byte
	deviceBody          []byte
	signInBody          []byte
	signInStatus        int

	mu           sync.Mutex
	codeQuery    url.Values
	usersureForm url.Values
	decrypted    string
	authQuery    url.Values
	authCookie   string
	signForm     url.Values
	signQuery    url.Values
	signCookie   string
	headers      map[string]http.Header
	counts       map[string]int
}

func newUpstream(t *testing.T) *upstream {
	t.Helper()
	key, err := rsa.GenerateKey(rand.Reader, 1024)
	if err != nil {
		t.Fatalf("GenerateKey: %v", err)
	}
	der, err := x509.MarshalPKIXPublicKey(&key.PublicKey)
	if err != nil {
		t.Fatalf("MarshalPKIXPublicKey: %v", err)
	}
	public := string(pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: der}))
	signPosition := readProtocolFixture(t, "sign_position.json")
	up := &upstream{
		key:                 key,
		public:              public,
		authFixture:         readProtocolFixture(t, "auth_response.json"),
		signPositionFixture: signPosition,
		signPositionBody:    signPosition,
		deviceBody:          readProtocolFixture(t, "device_state.json"),
		signInBody:          readProtocolFixture(t, "sign_in_response.json"),
		headers:             make(map[string]http.Header),
		counts:              make(map[string]int),
	}
	up.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		up.mu.Lock()
		up.counts[r.URL.Path]++
		up.headers[r.URL.Path] = r.Header.Clone()
		up.mu.Unlock()

		switch r.URL.Path {
		case "/code/html":
			up.mu.Lock()
			up.codeQuery = r.URL.Query()
			challenge := up.challenge
			up.mu.Unlock()
			if challenge {
				w.Write([]byte("<html><body>ydclearance=abc</body></html>"))
				return
			}
			w.Write([]byte("<html><script>var page_use = '" + testPageUse +
				"';</script><input type=\"test\" id=\"key\" value=\"" + up.public + "\"></html>"))
		case "/code/usersure":
			if err := r.ParseForm(); err != nil {
				w.WriteHeader(http.StatusBadRequest)
				return
			}
			up.mu.Lock()
			up.usersureForm = r.PostForm
			code := up.usersureCode
			up.mu.Unlock()
			if got := r.URL.Query().Get("ajax_sign"); got != testPageUse {
				w.WriteHeader(http.StatusBadRequest)
				return
			}
			ciphertext, err := base64.StdEncoding.DecodeString(r.PostForm.Get("oauth_upwd"))
			if err != nil {
				w.WriteHeader(http.StatusBadRequest)
				return
			}
			plain, err := rsa.DecryptPKCS1v15(rand.Reader, up.key, ciphertext)
			if err != nil {
				w.WriteHeader(http.StatusBadRequest)
				return
			}
			up.mu.Lock()
			up.decrypted = string(plain)
			up.mu.Unlock()
			if code != "" {
				fmt.Fprintf(w, `{"code":%q,"msg":"denied"}`, code)
				return
			}
			w.Write([]byte(`{"code":"s200","reUrl":"https://f.yiban.cn/iapp7463"}`))
		case "/iframe/index":
			up.mu.Lock()
			missing := up.missingToken
			up.mu.Unlock()
			if missing {
				w.Header().Set("Location", up.server.URL+"/app#/?yb_uid=1")
			} else {
				w.Header().Set("Location", up.server.URL+"/app#/?verify_request="+testVerify+"&yb_uid=1")
			}
			w.WriteHeader(http.StatusFound)
		case "/base/c/auth/yiban":
			up.handleAuth(w, r)
		case "/base/c/auth/yiban/final":
			up.handleAuth(w, r)
		case "/nightAttendance/student/index/signPosition":
			up.mu.Lock()
			body := up.signPositionBody
			up.mu.Unlock()
			w.Write(body)
		case "/device/student/index/getState":
			up.mu.Lock()
			body := up.deviceBody
			up.mu.Unlock()
			w.Write(body)
		case "/nightAttendance/student/index/signIn":
			if err := r.ParseForm(); err != nil {
				w.WriteHeader(http.StatusBadRequest)
				return
			}
			up.mu.Lock()
			up.signForm = r.PostForm
			up.signQuery = r.URL.Query()
			if cookie, err := r.Cookie("csrf_token"); err == nil {
				up.signCookie = cookie.Value
			}
			body := up.signInBody
			status := up.signInStatus
			up.mu.Unlock()
			if status != 0 {
				w.WriteHeader(status)
				return
			}
			w.Write(body)
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(up.server.Close)
	return up
}

// handleAuth 回放认证响应；可配置一次 302。
func (u *upstream) handleAuth(w http.ResponseWriter, r *http.Request) {
	u.mu.Lock()
	u.authQuery = r.URL.Query()
	if cookie, err := r.Cookie("csrf_token"); err == nil {
		u.authCookie = cookie.Value
	}
	body := u.authBody
	redirect := u.authRedirect
	loop := u.authLoop
	u.mu.Unlock()
	if loop && r.URL.Path == "/base/c/auth/yiban" {
		w.Header().Set("Location", "/base/c/auth/yiban")
		w.WriteHeader(http.StatusFound)
		return
	}
	if redirect != "" && r.URL.Path == "/base/c/auth/yiban" {
		w.Header().Set("Location", redirect)
		w.WriteHeader(http.StatusFound)
		return
	}
	w.Header().Set("Content-Type", "text/html")
	if body != nil {
		w.Write(body)
		return
	}
	w.Write(u.authFixture)
}

// startSeparateAuth 让认证跳落到另一台主机，用于验证 CSRF 跨主机。
func (u *upstream) startSeparateAuth(t *testing.T) {
	t.Helper()
	u.authServer = httptest.NewServer(http.HandlerFunc(u.handleAuth))
	t.Cleanup(u.authServer.Close)
}

func (u *upstream) endpoints() Endpoints {
	auth := u.server.URL + "/base/c/auth/yiban"
	if u.authServer != nil {
		auth = u.authServer.URL + "/base/c/auth/yiban"
		// 用不同主机名（不是同主机不同端口）：cookiejar 忽略端口，
		// 同主机无法暴露「cookie 被绑定」的回退。
		auth = strings.Replace(auth, "127.0.0.1", "localhost", 1)
	}
	return Endpoints{
		CodeHTML:     u.server.URL + "/code/html",
		Usersure:     u.server.URL + "/code/usersure",
		IframeIndex:  u.server.URL + "/iframe/index",
		APIAuth:      auth,
		SignPosition: u.server.URL + "/nightAttendance/student/index/signPosition",
		SignIn:       u.server.URL + "/nightAttendance/student/index/signIn",
		DeviceState:  u.server.URL + "/device/student/index/getState",
	}
}

// setSignPositionState 改写 signPosition 的 State。
func (u *upstream) setSignPositionState(t *testing.T, state int) {
	t.Helper()
	var obj map[string]any
	if err := json.Unmarshal(u.signPositionFixture, &obj); err != nil {
		t.Fatalf("unmarshal signPosition: %v", err)
	}
	data, ok := obj["data"].(map[string]any)
	if !ok {
		t.Fatal("signPosition data is not an object")
	}
	data["State"] = state
	body, err := json.Marshal(obj)
	if err != nil {
		t.Fatalf("marshal signPosition: %v", err)
	}
	u.mu.Lock()
	u.signPositionBody = body
	u.mu.Unlock()
}

// removeSignPositionRange 删除时间窗，用于无窗口分支。
func (u *upstream) removeSignPositionRange(t *testing.T) {
	t.Helper()
	var obj map[string]any
	if err := json.Unmarshal(u.signPositionFixture, &obj); err != nil {
		t.Fatalf("unmarshal signPosition: %v", err)
	}
	data, ok := obj["data"].(map[string]any)
	if !ok {
		t.Fatal("signPosition data is not an object")
	}
	delete(data, "Range")
	body, err := json.Marshal(obj)
	if err != nil {
		t.Fatalf("marshal signPosition: %v", err)
	}
	u.mu.Lock()
	u.signPositionBody = body
	u.mu.Unlock()
}

// emptySignPosition 让 Position 为空数组。
func (u *upstream) emptySignPosition(t *testing.T) {
	t.Helper()
	var obj map[string]any
	if err := json.Unmarshal(u.signPositionFixture, &obj); err != nil {
		t.Fatalf("unmarshal signPosition: %v", err)
	}
	data, ok := obj["data"].(map[string]any)
	if !ok {
		t.Fatal("signPosition data is not an object")
	}
	data["Position"] = []any{}
	body, err := json.Marshal(obj)
	if err != nil {
		t.Fatalf("marshal signPosition: %v", err)
	}
	u.mu.Lock()
	u.signPositionBody = body
	u.mu.Unlock()
}

func (u *upstream) count(path string) int {
	u.mu.Lock()
	defer u.mu.Unlock()
	return u.counts[path]
}

func readAuthFixture(t *testing.T) []byte {
	t.Helper()
	return readProtocolFixture(t, "auth_response.json")
}

// readProtocolFixture 只允许读 captured 夹具。
// 一致性套件把这里的假上游当作成功场景。
// synthetic 与 unknown 夹具不得支撑该场景。
func readProtocolFixture(t *testing.T, name string) []byte {
	t.Helper()
	if provenance := protocolFixtureProvenance(t, name); provenance != "captured" {
		t.Fatalf("fixture %s is %s; provider tests may only use captured fixtures", name, provenance)
	}
	body, err := os.ReadFile("../../protocol/testdata/" + name)
	if err != nil {
		t.Fatalf("read %s: %v", name, err)
	}
	return body
}

// protocolFixtureProvenance 从登记表读取夹具来源。
func protocolFixtureProvenance(t *testing.T, name string) string {
	t.Helper()
	raw, err := os.ReadFile("../../protocol/testdata/provenance.json")
	if err != nil {
		t.Fatalf("read provenance registry: %v", err)
	}
	var registry struct {
		Files map[string]struct {
			Provenance string `json:"provenance"`
		} `json:"files"`
	}
	if err := json.Unmarshal(raw, &registry); err != nil {
		t.Fatalf("parse provenance registry: %v", err)
	}
	entry, ok := registry.Files[name]
	if !ok {
		t.Fatalf("fixture %s has no provenance entry", name)
	}
	return entry.Provenance
}

func testCredential() contract.Credential {
	return contract.Credential{Phone: testPhone, Password: testPassword}
}

func TestVerifyLoginChainSuccess(t *testing.T) {
	up := newUpstream(t)
	client := New(Config{Endpoints: up.endpoints()})
	profile, err := client.Verify(context.Background(), "acct-1", testCredential())
	if err != nil {
		t.Fatalf("Verify: %v", err)
	}
	if profile.DisplayName != "张三" || profile.SchoolName != "示例大学" {
		t.Fatalf("profile = %+v", profile)
	}

	up.mu.Lock()
	form, decrypted, codeQuery, authQuery, authCookie :=
		up.usersureForm, up.decrypted, up.codeQuery, up.authQuery, up.authCookie
	headers := up.headers
	up.mu.Unlock()

	if decrypted != testPassword {
		t.Fatalf("decrypted password = %q", decrypted)
	}
	if form.Get("oauth_uname") != testPhone ||
		form.Get("client_id") != oauthClientID ||
		form.Get("redirect_uri") != redirectURI ||
		form.Get("display") != "authorize" {
		t.Fatalf("usersure form = %v", form)
	}
	if codeQuery.Get("client_id") != oauthClientID ||
		codeQuery.Get("redirect_uri") != redirectURI {
		t.Fatalf("code query = %v", codeQuery)
	}
	if authQuery.Get("verifyRequest") != testVerify ||
		authQuery.Get("CSRF") == "" ||
		authCookie != authQuery.Get("CSRF") {
		t.Fatalf("auth query=%v cookie=%q", authQuery, authCookie)
	}

	if got := headers["/code/usersure"].Get("Origin"); got != "" {
		t.Fatalf("usersure Origin = %q, want empty", got)
	}
	if got := headers["/code/usersure"].Get("Referer"); got != "" {
		t.Fatalf("usersure Referer = %q, want empty", got)
	}
	for _, path := range []string{"/code/html", "/iframe/index", "/base/c/auth/yiban"} {
		if got := headers[path].Get("Origin"); got != loginOrigin {
			t.Fatalf("%s Origin = %q, want %q", path, got, loginOrigin)
		}
		if got := headers[path].Get("AppVersion"); got != appVersion {
			t.Fatalf("%s AppVersion = %q", path, got)
		}
	}
}

func TestVerifyBadCredentials(t *testing.T) {
	up := newUpstream(t)
	up.usersureCode = "e001"
	client := New(Config{Endpoints: up.endpoints()})
	_, err := client.Verify(context.Background(), "acct-1", testCredential())
	var outcomeErr *contract.OutcomeError
	if !errors.As(err, &outcomeErr) || outcomeErr.Outcome != contract.OutcomeCredentialBad {
		t.Fatalf("error = %v, want OutcomeCredentialBad", err)
	}
}

func TestVerifyChallenge(t *testing.T) {
	up := newUpstream(t)
	up.challenge = true
	client := New(Config{Endpoints: up.endpoints()})
	_, err := client.Verify(context.Background(), "acct-1", testCredential())
	var outcomeErr *contract.OutcomeError
	if !errors.As(err, &outcomeErr) || outcomeErr.Outcome != contract.OutcomeChallenge {
		t.Fatalf("error = %v, want OutcomeChallenge", err)
	}
}

func TestVerifySessionExpired(t *testing.T) {
	up := newUpstream(t)
	up.authBody = []byte(`{"code":999,"msg":"expired"}`)
	client := New(Config{Endpoints: up.endpoints()})
	_, err := client.Verify(context.Background(), "acct-1", testCredential())
	var expired *protocol.SessionExpired
	if !errors.As(err, &expired) {
		t.Fatalf("error = %v, want SessionExpired", err)
	}
}

func TestVerifyBrokenAuth(t *testing.T) {
	up := newUpstream(t)
	up.authBody = []byte(`{"code":0`)
	client := New(Config{Endpoints: up.endpoints()})
	_, err := client.Verify(context.Background(), "acct-1", testCredential())
	var outcomeErr *contract.OutcomeError
	if !errors.As(err, &outcomeErr) || outcomeErr.Outcome != contract.OutcomeUnknown {
		t.Fatalf("error = %v, want OutcomeUnknown", err)
	}
}

func TestVerifyAuthDataNotObject(t *testing.T) {
	up := newUpstream(t)
	up.authBody = []byte(`{"code":0,"data":[]}`)
	client := New(Config{Endpoints: up.endpoints()})
	_, err := client.Verify(context.Background(), "acct-1", testCredential())
	var outcomeErr *contract.OutcomeError
	if !errors.As(err, &outcomeErr) || outcomeErr.Outcome != contract.OutcomeUnknown {
		t.Fatalf("error = %v, want OutcomeUnknown", err)
	}
	if outcomeErr.Message != "auth data is not an object" {
		t.Fatalf("message = %q", outcomeErr.Message)
	}
}

// 解析错误不得带身份字段：Fragment 会含 PersonName/PersonId。
func TestVerifyParseErrorHidesIdentity(t *testing.T) {
	up := newUpstream(t)
	var obj map[string]any
	if err := json.Unmarshal(up.authFixture, &obj); err != nil {
		t.Fatalf("unmarshal fixture: %v", err)
	}
	data, ok := obj["data"].(map[string]any)
	if !ok {
		t.Fatal("fixture data is not an object")
	}
	delete(data, "PersonName")
	body, err := json.Marshal(obj)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	up.authBody = body
	client := New(Config{Endpoints: up.endpoints()})
	_, err = client.Verify(context.Background(), "acct-1", testCredential())
	var outcomeErr *contract.OutcomeError
	if !errors.As(err, &outcomeErr) || outcomeErr.Outcome != contract.OutcomeUnknown {
		t.Fatalf("error = %v, want OutcomeUnknown", err)
	}
	if strings.Contains(outcomeErr.Message, "张三") || strings.Contains(outcomeErr.Message, "ff4640") {
		t.Fatalf("error message leaked identity: %q", outcomeErr.Message)
	}
}

// CSRF cookie 必须跨主机发送：生产第 4 跳在 api.uyiban.com。
func TestVerifySendsCSRSAcrossHosts(t *testing.T) {
	up := newUpstream(t)
	up.startSeparateAuth(t)
	client := New(Config{Endpoints: up.endpoints()})
	if _, err := client.Verify(context.Background(), "acct-1", testCredential()); err != nil {
		t.Fatalf("Verify: %v", err)
	}
	up.mu.Lock()
	cookie, query := up.authCookie, up.authQuery.Get("CSRF")
	up.mu.Unlock()
	if cookie == "" || cookie != query {
		t.Fatalf("csrf cookie = %q, query = %q", cookie, query)
	}
}

// 认证跳可能 302：手动跟一跳。
func TestVerifyFollowsAuthRedirect(t *testing.T) {
	up := newUpstream(t)
	up.authRedirect = up.server.URL + "/base/c/auth/yiban/final"
	client := New(Config{Endpoints: up.endpoints()})
	profile, err := client.Verify(context.Background(), "acct-1", testCredential())
	if err != nil {
		t.Fatalf("Verify: %v", err)
	}
	if profile.DisplayName != "张三" {
		t.Fatalf("profile = %+v", profile)
	}
	if got := up.count("/base/c/auth/yiban/final"); got != 1 {
		t.Fatalf("final auth calls = %d, want 1", got)
	}
}

// 认证跳落到未知主机：拒绝，不发请求。
func TestVerifyRejectsUntrustedAuthRedirect(t *testing.T) {
	up := newUpstream(t)
	up.authRedirect = "http://127.0.0.1:1/steal"
	client := New(Config{Endpoints: up.endpoints()})
	_, err := client.Verify(context.Background(), "acct-1", testCredential())
	var outcomeErr *contract.OutcomeError
	if !errors.As(err, &outcomeErr) || outcomeErr.Outcome != contract.OutcomeUnknown {
		t.Fatalf("error = %v, want OutcomeUnknown", err)
	}
}

// 连续 302：最多跟 5 层，第 6 次请求后报错。
func TestVerifyRejectsAuthRedirectLoop(t *testing.T) {
	up := newUpstream(t)
	up.authLoop = true
	client := New(Config{Endpoints: up.endpoints()})
	_, err := client.Verify(context.Background(), "acct-1", testCredential())
	var outcomeErr *contract.OutcomeError
	if !errors.As(err, &outcomeErr) ||
		outcomeErr.Outcome != contract.OutcomeUnknown ||
		outcomeErr.Message != "auth redirect limit exceeded" {
		t.Fatalf("error = %v", err)
	}
	if got := up.count("/base/c/auth/yiban"); got != 6 {
		t.Fatalf("auth calls = %d, want 6", got)
	}
}

func TestTrustedAuthHost(t *testing.T) {
	// 配置端点用测试主机，才能同时钉住「配置主机」与「api.uyiban.com」两条分支。
	client := New(Config{Endpoints: Endpoints{
		APIAuth: "http://127.0.0.1:1/base/c/auth/yiban",
	}})
	cases := []struct {
		raw  string
		want bool
	}{
		{"http://127.0.0.1:1/x", true},
		{"https://127.0.0.1:1/x", false},
		{"https://api.uyiban.com/x", true},
		{"http://api.uyiban.com/x", false},
		{"https://example.com/x", false},
		{"https://api.uyiban.com.evil/x", false},
	}
	for _, tc := range cases {
		target, err := url.Parse(tc.raw)
		if err != nil {
			t.Fatalf("parse %q: %v", tc.raw, err)
		}
		if got := client.trustedAuthHost(target); got != tc.want {
			t.Fatalf("trustedAuthHost(%q) = %t, want %t", tc.raw, got, tc.want)
		}
	}
}

func TestVerifyMissingVerifyRequest(t *testing.T) {
	up := newUpstream(t)
	up.missingToken = true
	client := New(Config{Endpoints: up.endpoints()})
	_, err := client.Verify(context.Background(), "acct-1", testCredential())
	var outcomeErr *contract.OutcomeError
	if !errors.As(err, &outcomeErr) || outcomeErr.Outcome != contract.OutcomeUnknown {
		t.Fatalf("error = %v, want OutcomeUnknown", err)
	}
}

func TestSessionReuseAndExpiry(t *testing.T) {
	up := newUpstream(t)
	clock := &manualClock{now: time.Date(2026, 10, 26, 6, 0, 0, 0, time.UTC)}
	client := New(Config{Endpoints: up.endpoints(), Clock: clock})
	call := func() error {
		return client.withSession(context.Background(), "acct-1", testCredential(),
			func(context.Context, *http.Client, string) error { return nil })
	}
	if err := call(); err != nil {
		t.Fatalf("first call: %v", err)
	}
	if got := up.count("/code/html"); got != 1 {
		t.Fatalf("login calls = %d, want 1", got)
	}
	if err := call(); err != nil {
		t.Fatalf("second call: %v", err)
	}
	if got := up.count("/code/html"); got != 1 {
		t.Fatalf("cached login calls = %d, want 1", got)
	}
	clock.Advance(defaultSessionTTL + time.Minute)
	if err := call(); err != nil {
		t.Fatalf("expired call: %v", err)
	}
	if got := up.count("/code/html"); got != 2 {
		t.Fatalf("expired login calls = %d, want 2", got)
	}
}

func TestSessionTTLSlides(t *testing.T) {
	up := newUpstream(t)
	clock := &manualClock{now: time.Date(2026, 10, 26, 6, 0, 0, 0, time.UTC)}
	client := New(Config{Endpoints: up.endpoints(), Clock: clock})
	call := func() error {
		return client.withSession(context.Background(), "acct-1", testCredential(),
			func(context.Context, *http.Client, string) error { return nil })
	}
	if err := call(); err != nil {
		t.Fatalf("first call: %v", err)
	}
	clock.Advance(20 * time.Minute)
	if err := call(); err != nil {
		t.Fatalf("20m call: %v", err)
	}
	clock.Advance(20 * time.Minute)
	if err := call(); err != nil {
		t.Fatalf("40m call: %v", err)
	}
	if got := up.count("/code/html"); got != 1 {
		t.Fatalf("sliding TTL login calls = %d, want 1", got)
	}
	clock.Advance(31 * time.Minute)
	if err := call(); err != nil {
		t.Fatalf("71m call: %v", err)
	}
	if got := up.count("/code/html"); got != 2 {
		t.Fatalf("expired login calls = %d, want 2", got)
	}
}

func TestSessionExpiredRetriesOnce(t *testing.T) {
	up := newUpstream(t)
	client := New(Config{Endpoints: up.endpoints()})
	attempts := 0
	err := client.withSession(context.Background(), "acct-1", testCredential(),
		func(context.Context, *http.Client, string) error {
			attempts++
			return &protocol.SessionExpired{}
		})
	var expired *protocol.SessionExpired
	if !errors.As(err, &expired) {
		t.Fatalf("error = %v, want SessionExpired", err)
	}
	if attempts != 2 {
		t.Fatalf("attempts = %d, want 2", attempts)
	}
	if got := up.count("/code/html"); got != 2 {
		t.Fatalf("login calls = %d, want 2", got)
	}
}

func TestSessionExpiredRelogin(t *testing.T) {
	up := newUpstream(t)
	client := New(Config{Endpoints: up.endpoints()})
	attempts := 0
	err := client.withSession(context.Background(), "acct-1", testCredential(),
		func(context.Context, *http.Client, string) error {
			attempts++
			if attempts == 1 {
				return &protocol.SessionExpired{}
			}
			return nil
		})
	if err != nil {
		t.Fatalf("withSession: %v", err)
	}
	if attempts != 2 {
		t.Fatalf("attempts = %d, want 2", attempts)
	}
	if got := up.count("/code/html"); got != 2 {
		t.Fatalf("login calls = %d, want 2", got)
	}
}
