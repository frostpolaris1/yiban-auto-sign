package turnstile_test

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/turnstile"
)

// newVerifier 起一个假 siteverify，返回按需替换 Endpoint 的校验器。
func newVerifier(t *testing.T, handler http.HandlerFunc) *turnstile.Verifier {
	return newVerifierWithExpectations(t, handler, "", "")
}

// newVerifierWithExpectations 同上，并带上可选的令牌绑定期望（主机名/动作）。
func newVerifierWithExpectations(t *testing.T, handler http.HandlerFunc, hostname, action string) *turnstile.Verifier {
	t.Helper()
	srv := httptest.NewServer(handler)
	t.Cleanup(srv.Close)
	v, err := turnstile.New("test-secret", hostname, action)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	v.Endpoint = srv.URL
	return v
}

func TestNewRejectsEmptySecret(t *testing.T) {
	for _, secret := range []string{"", "   "} {
		if _, err := turnstile.New(secret, "", ""); err == nil {
			t.Fatalf("New(%q) = nil error, want failure", secret)
		}
	}
}

func TestVerifySuccessSendsTheTokenAndClientIP(t *testing.T) {
	var got map[string]string
	v := newVerifier(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("method = %s, want POST", r.Method)
		}
		if ct := r.Header.Get("Content-Type"); ct != "application/x-www-form-urlencoded" {
			t.Errorf("content-type = %q", ct)
		}
		if err := r.ParseForm(); err != nil {
			t.Fatalf("ParseForm: %v", err)
		}
		got = map[string]string{
			"secret":   r.PostForm.Get("secret"),
			"response": r.PostForm.Get("response"),
			"remoteip": r.PostForm.Get("remoteip"),
		}
		_, _ = w.Write([]byte(`{"success":true}`))
	})
	ok, codes, err := v.Verify(context.Background(), "token-abc", "203.0.113.7")
	if err != nil || !ok {
		t.Fatalf("Verify = (%v, %v, %v), want success", ok, codes, err)
	}
	if len(codes) != 0 {
		t.Fatalf("codes = %v, want empty", codes)
	}
	want := map[string]string{"secret": "test-secret", "response": "token-abc", "remoteip": "203.0.113.7"}
	for k, v := range want {
		if got[k] != v {
			t.Errorf("form[%s] = %q, want %q", k, got[k], v)
		}
	}
}

func TestVerifyRejectedReportsCodesWithoutError(t *testing.T) {
	v := newVerifier(t, func(w http.ResponseWriter, _ *http.Request) {
		// 回包按 Cloudflare 的 JSON 键名写：原始串（backtick）不参与状态字面量
		// 判据——键名恰好与任务状态词同值，用 map 键写会被 status.literal 误判。
		_, _ = w.Write([]byte(`{"success":false,"error-codes":["invalid-input-response"]}`))
	})
	ok, codes, err := v.Verify(context.Background(), "spent-token", "")
	if err != nil {
		t.Fatalf("rejection must not be an error: %v", err)
	}
	if ok {
		t.Fatal("ok = true, want false")
	}
	if len(codes) != 1 || codes[0] != "invalid-input-response" {
		t.Fatalf("codes = %v", codes)
	}
}

func TestVerifyInternalErrorIsUnavailable(t *testing.T) {
	v := newVerifier(t, func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"success":false,"error-codes":["internal-error"]}`))
	})
	if ok, _, err := v.Verify(context.Background(), "token", ""); err == nil {
		t.Fatalf("Verify = (%v, _, nil), want an availability error", ok)
	}
}

func TestVerifyServerSideErrorCodesAreUnavailable(t *testing.T) {
	// 这几档是服务端故障或配置错：当成用户拒绝会让 on 档一片 400，
	// 事后看像机器人潮，实际是密钥轮换写错了。
	for _, code := range []string{"internal-error", "missing-input-secret", "invalid-input-secret", "bad-request"} {
		code := code
		t.Run(code, func(t *testing.T) {
			v := newVerifier(t, func(w http.ResponseWriter, _ *http.Request) {
				_, _ = w.Write([]byte(`{"success":false,"error-codes":["` + code + `"]}`))
			})
			ok, codes, err := v.Verify(context.Background(), "token", "")
			if err == nil {
				t.Fatalf("Verify = (%v, %v, nil), want an availability error", ok, codes)
			}
			if !strings.Contains(err.Error(), code) {
				t.Fatalf("error %q does not name the code %q", err, code)
			}
		})
	}
}

func TestVerifyNon2xxIsUnavailableAndHidesTheToken(t *testing.T) {
	v := newVerifier(t, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusBadGateway)
	})
	_, _, err := v.Verify(context.Background(), "secret-token-value", "")
	if err == nil {
		t.Fatal("want an error")
	}
	if strings.Contains(err.Error(), "secret-token-value") {
		t.Fatalf("error leaks the token: %v", err)
	}
}

func TestVerifyMalformedAnswerIsUnavailable(t *testing.T) {
	v := newVerifier(t, func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("not json"))
	})
	if _, _, err := v.Verify(context.Background(), "token", ""); err == nil {
		t.Fatal("want an error")
	}
}

func TestVerifyTimeoutIsUnavailable(t *testing.T) {
	v := newVerifier(t, func(w http.ResponseWriter, r *http.Request) {
		select {
		case <-r.Context().Done():
		case <-time.After(3 * time.Second):
		}
		_, _ = w.Write([]byte(`{"success":true}`))
	})
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	if _, _, err := v.Verify(ctx, "token", ""); err == nil {
		t.Fatal("want an error")
	}
}

func TestVerifyEmptyTokenNeverCallsTheEndpoint(t *testing.T) {
	v := newVerifier(t, func(w http.ResponseWriter, _ *http.Request) {
		t.Error("endpoint must not be called for an empty token")
	})
	if _, _, err := v.Verify(context.Background(), "  ", ""); err == nil {
		t.Fatal("want an error")
	}
}

// 令牌绑定：期望非空时回执里的主机名/动作必须匹配，不匹配是「令牌不合格」，
// 不是验证服务不可达——调用方 on 档回 400、log 档放行，不走 502。
func TestVerifyHonorsExpectedTokenBindings(t *testing.T) {
	cases := []struct {
		name     string
		answer   string
		hostname string
		action   string
		want     bool
		wantCode string
	}{
		{
			name:   "no expectation accepts any binding",
			answer: `{"success":true,"hostname":"any.example","action":"any"}`,
			want:   true,
		},
		{
			name:     "hostname match",
			answer:   `{"success":true,"hostname":"reg.example.com"}`,
			hostname: "reg.example.com",
			want:     true,
		},
		{
			name:     "hostname match ignores case and padding",
			answer:   `{"success":true,"hostname":"REG.Example.COM"}`,
			hostname: "  reg.example.com  ",
			want:     true,
		},
		{
			name:     "hostname mismatch",
			answer:   `{"success":true,"hostname":"evil.example.com"}`,
			hostname: "reg.example.com",
			wantCode: "hostname-mismatch",
		},
		{
			name:     "missing hostname is a mismatch",
			answer:   `{"success":true}`,
			hostname: "reg.example.com",
			wantCode: "hostname-mismatch",
		},
		{
			name:   "action match",
			answer: `{"success":true,"action":"register"}`,
			action: "register",
			want:   true,
		},
		{
			name:     "action mismatch is case sensitive",
			answer:   `{"success":true,"action":"Register"}`,
			action:   "register",
			wantCode: "action-mismatch",
		},
		{
			name:     "both match",
			answer:   `{"success":true,"hostname":"reg.example.com","action":"register"}`,
			hostname: "reg.example.com",
			action:   "register",
			want:     true,
		},
		{
			name:     "hostname mismatch is reported before action mismatch",
			answer:   `{"success":true,"hostname":"evil.example.com","action":"other"}`,
			hostname: "reg.example.com",
			action:   "register",
			wantCode: "hostname-mismatch",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			v := newVerifierWithExpectations(t, func(w http.ResponseWriter, _ *http.Request) {
				_, _ = w.Write([]byte(tc.answer))
			}, tc.hostname, tc.action)

			ok, codes, err := v.Verify(context.Background(), "token", "")
			if err != nil {
				t.Fatalf("binding rejection must not be an availability error: %v", err)
			}
			if ok != tc.want {
				t.Fatalf("ok = %v, want %v (codes %v)", ok, tc.want, codes)
			}
			if tc.wantCode == "" {
				if len(codes) != 0 {
					t.Fatalf("codes = %v, want empty", codes)
				}
				return
			}
			if len(codes) != 1 || codes[0] != tc.wantCode {
				t.Fatalf("codes = %v, want [%s]", codes, tc.wantCode)
			}
		})
	}
}
