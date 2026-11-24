package router_test

// 注册状态回包必须带站点密钥：前端只认这一个字段，
// 漏掉它人机验证控件就永远不出现（服务端却仍在 on 档拦注册）。

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app/auth"
)

func TestRegistrationStatusCarriesTurnstileSiteKey(t *testing.T) {
	engine := newAuthEngine(t, newStubIssuer(), stubAuth{
		registration: auth.RegistrationStatus{
			Enabled:           true,
			TermsVersion:      2,
			PrivacyVersion:    3,
			TurnstileSiteKey:  "site-key-from-config",
			TurnstileEnforced: true,
		},
	})
	rec := serve(engine, httptest.NewRequest(http.MethodGet, "/api/auth/registration", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %q", rec.Code, rec.Body.String())
	}
	var got struct {
		Enabled           bool   `json:"enabled"`
		TermsVersion      int    `json:"terms_version"`
		PrivacyVersion    int    `json:"privacy_version"`
		TurnstileSiteKey  string `json:"turnstile_site_key"`
		TurnstileEnforced bool   `json:"turnstile_enforced"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if got.TurnstileSiteKey != "site-key-from-config" {
		t.Fatalf("turnstile_site_key = %q, want %q", got.TurnstileSiteKey, "site-key-from-config")
	}
	if !got.TurnstileEnforced {
		t.Fatal("turnstile_enforced = false, want the service value")
	}
	if !got.Enabled || got.TermsVersion != 2 || got.PrivacyVersion != 3 {
		t.Fatalf("status = %+v, want the service values", got)
	}
}
