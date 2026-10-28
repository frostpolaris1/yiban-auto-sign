package yiban

// 本文件登录链夹具标注：四跳形状已由 2026-10-28 实拍逐跳命中
// （bd yiban-auto-sign-4sk 注释）；夹具值是合成值，不是抓包原文。
// 协议解析夹具（auth_response.json 等）沿用 protocol/testdata 的既有登记。
// 未实拍档位登记在 replay_test.go 与 testdata/recordings.json 的 unrecorded 段。

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
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

	// P5-D 上传播（逆向夹具，未经实拍）。
	uploadBody []byte
	putStatus  int

	usersureBody []byte

	mu           sync.Mutex
	codeQuery    url.Values
	usersureForm url.Values
	usersureRaw  []byte
	decrypted    string
	authQuery    url.Values
	authRawQuery string
	authCookie   string
	signForm     url.Values
	signQuery    url.Values
	signCookie   string
	signRaw      []byte
	uploadForm   url.Values
	uploadQuery  url.Values
	putHeaders   http.Header
	putBody      []byte
	putPath      string
	putMethod    string
	headers      map[string]http.Header
	counts       map[string]int
	recorded     []recordedHop
}

// recordedHop 是一次请求的快照，供录播链判据断言顺序与形状。
type recordedHop struct {
	Method   string
	Path     string
	RawQuery string
	Header   http.Header
	Cookie   map[string]string
}

// snapshotHop 抓取一次请求的可断言字段。
func snapshotHop(r *http.Request) recordedHop {
	cookies := make(map[string]string)
	for _, cookie := range r.Cookies() {
		cookies[cookie.Name] = cookie.Value
	}
	return recordedHop{
		Method:   r.Method,
		Path:     r.URL.Path,
		RawQuery: r.URL.RawQuery,
		Header:   r.Header.Clone(),
		Cookie:   cookies,
	}
}

// recordedHops 返回请求快照的副本。
func (u *upstream) recordedHops() []recordedHop {
	u.mu.Lock()
	defer u.mu.Unlock()
	return append([]recordedHop(nil), u.recorded...)
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
		up.recorded = append(up.recorded, snapshotHop(r))
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
			raw, _ := io.ReadAll(r.Body)
			r.Body = io.NopCloser(bytes.NewReader(raw))
			up.mu.Lock()
			up.usersureRaw = raw
			up.mu.Unlock()
			if err := r.ParseForm(); err != nil {
				w.WriteHeader(http.StatusBadRequest)
				return
			}
			up.mu.Lock()
			up.usersureForm = r.PostForm
			code := up.usersureCode
			custom := up.usersureBody
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
			if custom != nil {
				w.Write(custom)
				return
			}
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
			raw, _ := io.ReadAll(r.Body)
			r.Body = io.NopCloser(bytes.NewReader(raw))
			if err := r.ParseForm(); err != nil {
				w.WriteHeader(http.StatusBadRequest)
				return
			}
			up.mu.Lock()
			up.signRaw = raw
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
		case "/nightAttendance/student/index/uploadUri":
			// P5-D：逆向夹具（未经实拍）。回执键名取自逆向常量池。
			raw, _ := io.ReadAll(r.Body)
			r.Body = io.NopCloser(bytes.NewReader(raw))
			if err := r.ParseForm(); err != nil {
				w.WriteHeader(http.StatusBadRequest)
				return
			}
			up.mu.Lock()
			up.uploadForm = r.PostForm
			up.uploadQuery = r.URL.Query()
			body := up.uploadBody
			up.mu.Unlock()
			if body == nil {
				body = []byte(`{"code":0,"msg":"","data":{"AttachmentFileName":"` +
					"yiban_camera_1790000000.jpg" + `","signedUrl":"` +
					up.server.URL + "/upload/yiban_camera_1790000000.jpg" + `"}}`)
			}
			w.Write(body)
		default:
			if strings.HasPrefix(r.URL.Path, "/upload/") {
				// P5-D：签名地址的下行（PUT 图片字节）。
				raw, _ := io.ReadAll(r.Body)
				up.mu.Lock()
				up.putHeaders = r.Header.Clone()
				up.putBody = raw
				up.putPath = r.URL.Path
				up.putMethod = r.Method
				status := up.putStatus
				up.mu.Unlock()
				if status != 0 && status != http.StatusOK {
					if status >= 300 && status < 400 {
						// 带 Location 且目标 200：跟随重定向的实现会把 302 变成
						// 一次成功的 GET——用于给「PUT 禁跟随」立牙。
						w.Header().Set("Location", "/upload/redirected.jpg")
					}
					w.WriteHeader(status)
					return
				}
				w.WriteHeader(http.StatusOK)
				return
			}
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
	u.authRawQuery = r.URL.RawQuery
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
		UploadUri:    u.server.URL + "/nightAttendance/student/index/uploadUri",
	}
}

// setSignPositionState 改写 signPosition 的 State。
// **合成改动**：State=2/4/5 的响应从未录制；
// 只用于验证旧实现映射，不构成平台证据。
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

// authQueryRaw 取认证跳的原始 query 串（顺序敏感断言用）。
func authQueryRaw(t *testing.T, up *upstream) string {
	t.Helper()
	up.mu.Lock()
	defer up.mu.Unlock()
	return up.authRawQuery
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
	form, decrypted, codeQuery, authQuery, authCookie, usersureRaw :=
		up.usersureForm, up.decrypted, up.codeQuery, up.authQuery, up.authCookie, up.usersureRaw
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
	// 旧实现（ref develop@1d1c5ccf）传空 scope：线上体以 "&scope=" 空值字段收尾。
	if !form.Has("scope") || form.Get("scope") != "" {
		t.Fatalf("usersure form missing empty scope = %v", form)
	}
	if !strings.HasSuffix(string(usersureRaw), "&scope=") {
		t.Fatalf("usersure body must end with empty scope, got %q", string(usersureRaw))
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
	// 旧实现 query 顺序：verifyRequest 在前、CSRF 在后（ref develop@1d1c5ccf）。
	if i, j := strings.Index(authQueryRaw(t, up), "verifyRequest="),
		strings.Index(authQueryRaw(t, up), "CSRF="); i < 0 || j < 0 || i > j {
		t.Fatalf("auth query order = %q, want verifyRequest before CSRF", authQueryRaw(t, up))
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
	// 挑战页未实拍，未知：响应体是合成标志物，只验证识别分支。
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
	// code=999 未实拍，未知：响应体是合成值，只验证旧实现映射。
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
	// 截断体里带身份字段：解析错误消息不得带原始片段（ParseError.Fragment 外溢即泄漏）。
	up.authBody = []byte(`{"code":0,"PersonName":"张三"`)
	client := New(Config{Endpoints: up.endpoints()})
	_, err := client.Verify(context.Background(), "acct-1", testCredential())
	var outcomeErr *contract.OutcomeError
	if !errors.As(err, &outcomeErr) || outcomeErr.Outcome != contract.OutcomeUnknown {
		t.Fatalf("error = %v, want OutcomeUnknown", err)
	}
	if strings.Contains(outcomeErr.Message, "张三") {
		t.Fatalf("error message leaked identity: %q", outcomeErr.Message)
	}
}

// 旧实现回执判据（ref develop@1d1c5ccf）：code 成功后只要 data 非空且非
// {Action,Data} 入口步即成功；data 是数组也放行，身份字段只是展示面。
func TestVerifyAuthDataArrayIsReceipt(t *testing.T) {
	up := newUpstream(t)
	up.authBody = []byte(`{"code":0,"data":[]}`)
	client := New(Config{Endpoints: up.endpoints()})
	profile, err := client.Verify(context.Background(), "acct-1", testCredential())
	if err != nil {
		t.Fatalf("Verify: %v", err)
	}
	if profile.DisplayName != "" || profile.SchoolName != "" {
		t.Fatalf("profile = %+v, want empty", profile)
	}
}

// data={} 是旧实现录制到的真实成功形状，登录必须成功。
func TestVerifyEmptyReceiptObjectSucceeds(t *testing.T) {
	up := newUpstream(t)
	up.authBody = []byte(`{"code":0,"data":{}}`)
	client := New(Config{Endpoints: up.endpoints()})
	if _, err := client.Verify(context.Background(), "acct-1", testCredential()); err != nil {
		t.Fatalf("Verify: %v", err)
	}
}

// data 键缺失：code=0 但无签发回执，登录失败。
func TestVerifyMissingReceiptRejected(t *testing.T) {
	up := newUpstream(t)
	up.authBody = []byte(`{"code":0,"msg":"ok"}`)
	client := New(Config{Endpoints: up.endpoints()})
	_, err := client.Verify(context.Background(), "acct-1", testCredential())
	var outcomeErr *contract.OutcomeError
	if !errors.As(err, &outcomeErr) || outcomeErr.Outcome != contract.OutcomeUnknown {
		t.Fatalf("error = %v, want OutcomeUnknown", err)
	}
}

// data 是 {Action,Data} 入口步形状：未签发会话，登录失败。
func TestVerifyEntryStepPayloadRejected(t *testing.T) {
	up := newUpstream(t)
	up.authBody = []byte(`{"code":0,"data":{"Action":"login","Data":"https://oauth.yiban.cn/x"}}`)
	client := New(Config{Endpoints: up.endpoints()})
	_, err := client.Verify(context.Background(), "acct-1", testCredential())
	var outcomeErr *contract.OutcomeError
	if !errors.As(err, &outcomeErr) || outcomeErr.Outcome != contract.OutcomeUnknown {
		t.Fatalf("error = %v, want OutcomeUnknown", err)
	}
}

// WAF 短响应关键词拦截不得判成凭据错（旧实现 usersure 步先判 WAF 再解析，
// ref develop@1d1c5ccf is_waf_blocked；\uXXXX 转义需先解码）。
func TestVerifyUsersureWAFKeywordBlock(t *testing.T) {
	up := newUpstream(t)
	up.usersureBody = []byte(`{"code":"x001","msg":"\u98ce\u9669\u8bbf\u95ee"}`)
	client := New(Config{Endpoints: up.endpoints()})
	_, err := client.Verify(context.Background(), "acct-1", testCredential())
	var outcomeErr *contract.OutcomeError
	if !errors.As(err, &outcomeErr) || outcomeErr.Outcome != contract.OutcomeTransient {
		t.Fatalf("error = %v, want OutcomeTransient", err)
	}
}

// 长响应（>2000 字符）合法含"风控"字样：不判拦截，照常解析（防误伤口径）。
func TestVerifyUsersureWAFKeywordLongBodyPassesThrough(t *testing.T) {
	up := newUpstream(t)
	up.usersureCode = "e001"
	up.usersureBody = []byte(`{"code":"e001","msgCN":"` + strings.Repeat("说明文本", 600) + `风控说明` + `","msg":"x"}`)
	client := New(Config{Endpoints: up.endpoints()})
	_, err := client.Verify(context.Background(), "acct-1", testCredential())
	var outcomeErr *contract.OutcomeError
	if !errors.As(err, &outcomeErr) || outcomeErr.Outcome != contract.OutcomeCredentialBad {
		t.Fatalf("error = %v, want OutcomeCredentialBad", err)
	}
}

// 挑战页双 JS 特征（旧实现 challenge.py 强信号）命中即 Challenge。
func TestVerifyDoubleJSChallenge(t *testing.T) {
	up := newUpstream(t)
	up.usersureBody = []byte(`<html><script>window.onload=setTimeout;eval("qo=eval;qo(po);")</script></html>`)
	client := New(Config{Endpoints: up.endpoints()})
	_, err := client.Verify(context.Background(), "acct-1", testCredential())
	var outcomeErr *contract.OutcomeError
	if !errors.As(err, &outcomeErr) || outcomeErr.Outcome != contract.OutcomeChallenge {
		t.Fatalf("error = %v, want OutcomeChallenge", err)
	}
}

// 身份字段缺失不回退登录结论：旧实现回执判据只看 data 载荷存在性；
// 身份字段是展示面，缺字段以空 Profile 成功（ref develop@1d1c5ccf）。
func TestVerifyPartialIdentityStillSucceeds(t *testing.T) {
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
	profile, err := client.Verify(context.Background(), "acct-1", testCredential())
	if err != nil {
		t.Fatalf("Verify: %v", err)
	}
	if profile.DisplayName != "" {
		t.Fatalf("DisplayName = %q, want empty", profile.DisplayName)
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
