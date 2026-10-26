package yiban

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/base64"
	"encoding/pem"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
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
	server *httptest.Server
	key    *rsa.PrivateKey
	public string

	challenge    bool
	usersureCode string
	authBody     []byte
	authFixture  []byte
	missingToken bool

	mu           sync.Mutex
	codeQuery    url.Values
	usersureForm url.Values
	decrypted    string
	authQuery    url.Values
	authCookie   string
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
	up := &upstream{
		key:         key,
		public:      public,
		authFixture: readAuthFixture(t),
		headers:     make(map[string]http.Header),
		counts:      make(map[string]int),
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
			up.mu.Lock()
			up.authQuery = r.URL.Query()
			if cookie, err := r.Cookie("csrf_token"); err == nil {
				up.authCookie = cookie.Value
			}
			body := up.authBody
			up.mu.Unlock()
			w.Header().Set("Content-Type", "text/html")
			if body != nil {
				w.Write(body)
				return
			}
			w.Write(up.authFixture)
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(up.server.Close)
	return up
}

func (u *upstream) endpoints() Endpoints {
	return Endpoints{
		CodeHTML:    u.server.URL + "/code/html",
		Usersure:    u.server.URL + "/code/usersure",
		IframeIndex: u.server.URL + "/iframe/index",
		APIAuth:     u.server.URL + "/base/c/auth/yiban",
	}
}

func (u *upstream) count(path string) int {
	u.mu.Lock()
	defer u.mu.Unlock()
	return u.counts[path]
}

func readAuthFixture(t *testing.T) []byte {
	t.Helper()
	body, err := os.ReadFile("../../protocol/testdata/auth_response.json")
	if err != nil {
		t.Fatalf("read auth fixture: %v", err)
	}
	return body
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
	var parse *protocol.ParseError
	if !errors.As(err, &parse) {
		t.Fatalf("error = %v, want ParseError", err)
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
