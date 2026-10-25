package protocol

import (
	"errors"
	"testing"
)

func TestParseAPIEnvelopeFixture(t *testing.T) {
	env, err := ParseAPIEnvelope(readFixture(t, "auth_response.json"))
	if err != nil {
		t.Fatalf("ParseAPIEnvelope: %v", err)
	}
	if !CodeIsOK(env.Code) {
		t.Fatalf("code = %#v, want success", env.Code)
	}
	if _, ok := env.Data.(map[string]any); !ok {
		t.Fatalf("data = %T, want object", env.Data)
	}
	if env.Msg != "" {
		t.Fatalf("msg = %q", env.Msg)
	}
}

func TestParseAPIEnvelopeSessionExpired(t *testing.T) {
	for _, name := range []string{
		"synthetic_session_expired.json",
		"synthetic_session_expired_string_code.json",
	} {
		_, err := ParseAPIEnvelope(readFixture(t, name))
		var expired *SessionExpired
		if !errors.As(err, &expired) {
			t.Fatalf("%s: error = %v, want SessionExpired", name, err)
		}
	}
}

func TestParseAPIEnvelopeRejectsBrokenJSON(t *testing.T) {
	for _, body := range []string{`{"code":0`, `[]`, `"text"`, ``} {
		_, err := ParseAPIEnvelope([]byte(body))
		var parse *ParseError
		if !errors.As(err, &parse) || parse.Field != "envelope" {
			t.Fatalf("%q: error = %v, want ParseError(envelope)", body, err)
		}
	}
}

func TestParseUsersureResponseFixture(t *testing.T) {
	res, err := ParseUsersureResponse(readFixture(t, "usersure_response.json"))
	if err != nil {
		t.Fatalf("ParseUsersureResponse: %v", err)
	}
	if !res.OK || res.Code != "s200" {
		t.Fatalf("result = %+v, want s200", res)
	}
	if res.ReURL == nil || *res.ReURL != "https://f.yiban.cn/iapp7463" {
		t.Fatalf("reUrl = %v", res.ReURL)
	}
}

func TestParseUsersureResponseBusinessFailure(t *testing.T) {
	res, err := ParseUsersureResponse([]byte(`{"code":"e001","msg":"无效的应用端编号"}`))
	if err != nil {
		t.Fatalf("ParseUsersureResponse: %v", err)
	}
	if res.OK || res.Code != "e001" || res.ReURL != nil {
		t.Fatalf("result = %+v", res)
	}
	if _, err := ParseUsersureResponse([]byte("not json")); err == nil {
		t.Fatal("non-JSON usersure response must fail")
	}
}

func TestCodeIsOK(t *testing.T) {
	ok := []any{int(0), int64(0), float64(0), "0"}
	bad := []any{false, int64(999), "999", "1", nil, "s200"}
	for _, code := range ok {
		if !CodeIsOK(code) {
			t.Fatalf("CodeIsOK(%#v) = false, want true", code)
		}
	}
	for _, code := range bad {
		if CodeIsOK(code) {
			t.Fatalf("CodeIsOK(%#v) = true, want false", code)
		}
	}
}

func TestLooksLikeChallenge(t *testing.T) {
	normal := []string{
		"authorize_page.html",
		"auth_response.json",
		"usersure_response.json",
		"sign_in_response.json",
		"sign_position.json",
		"device_state.json",
		"sign_in_body.txt",
		"usersure_form.txt",
	}
	for _, name := range normal {
		if LooksLikeChallenge(string(readFixture(t, name))) {
			t.Fatalf("%s was classified as a challenge", name)
		}
	}
	synthetic := []string{
		"synthetic_challenge_ydclearance.html",
		"synthetic_challenge_fengkongcloud.html",
		"synthetic_challenge_captcha.html",
		"synthetic_challenge_acw_sc.html",
	}
	for _, name := range synthetic {
		if !LooksLikeChallenge(string(readFixture(t, name))) {
			t.Fatalf("%s was not classified as a challenge", name)
		}
	}
	if LooksLikeChallenge("captcha.js") {
		t.Fatal("a weak marker without HTML context must not count")
	}
}
