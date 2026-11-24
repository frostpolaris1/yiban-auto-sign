package turnstile_test

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/turnstile"
)

// newVerifier 起一个假 siteverify，返回按需替换 Endpoint 的校验器。
func newVerifier(t *testing.T, handler http.HandlerFunc) *turnstile.Verifier {
	t.Helper()
	srv := httptest.NewServer(handler)
	t.Cleanup(srv.Close)
	v, err := turnstile.New("test-secret")
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	v.Endpoint = srv.URL
	return v
}

func TestNewRejectsEmptySecret(t *testing.T) {
	for _, secret := range []string{"", "   "} {
		if _, err := turnstile.New(secret); err == nil {
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
		_ = json.NewEncoder(w).Encode(map[string]any{
			"success":     false,
			"error-codes": []string{"invalid-input-response"},
		})
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
		_ = json.NewEncoder(w).Encode(map[string]any{
			"success":     false,
			"error-codes": []string{"internal-error"},
		})
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
				_ = json.NewEncoder(w).Encode(map[string]any{
					"success":     false,
					"error-codes": []string{code},
				})
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
