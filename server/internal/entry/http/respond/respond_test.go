package respond

import (
	"encoding/json"
	"errors"
	"net/http/httptest"
	"testing"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
)

func decode(t *testing.T, body []byte) Envelope {
	t.Helper()
	var env Envelope
	if err := json.Unmarshal(body, &env); err != nil {
		t.Fatalf("decode: %v", err)
	}
	return env
}

func TestErrorNotFound(t *testing.T) {
	rec := httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/x", nil)
	req.Header.Set("X-Request-Id", "trace-1")
	Error(rec, req, errs.ErrNotFound)
	if rec.Code != 404 {
		t.Fatalf("status = %d, want 404", rec.Code)
	}
	env := decode(t, rec.Body.Bytes())
	if env.Error.Code != "not_found" || env.Error.TraceID != "trace-1" {
		t.Fatalf("body = %#v", env)
	}
}

func TestErrorUnknownHidesRawMessage(t *testing.T) {
	rec := httptest.NewRecorder()
	Error(rec, nil, errors.New("secret database detail"))
	if rec.Code != 500 {
		t.Fatalf("status = %d, want 500", rec.Code)
	}
	env := decode(t, rec.Body.Bytes())
	if env.Error.Code != "internal_error" || env.Error.Message != "internal error" {
		t.Fatalf("body = %#v", env)
	}
}

func TestErrorValidation(t *testing.T) {
	rec := httptest.NewRecorder()
	Error(rec, nil, errs.New(errs.CodeValidation, "bad input"))
	if rec.Code != 400 {
		t.Fatalf("status = %d, want 400", rec.Code)
	}
}
