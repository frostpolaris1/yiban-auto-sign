package respond

import (
	"encoding/json"
	"errors"
	"net/http"
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

// 错误码到状态码的映射是契约面：调用方按状态码分流，码值按业务分流。
// 这两层任何一层漂移都会让客户端走错分支，故逐码钉住。
// 422 是校验类语义（图片、定位），429 是限流与容量。
func TestStatusForEveryCode(t *testing.T) {
	want := map[errs.Code]int{
		errs.CodeValidation:      http.StatusBadRequest,
		errs.CodeUnauthorized:    http.StatusUnauthorized,
		errs.CodeForbidden:       http.StatusForbidden,
		errs.CodeNotFound:        http.StatusNotFound,
		errs.CodeConflict:        http.StatusConflict,
		errs.CodeAlreadySigned:   http.StatusConflict,
		errs.CodeSignRunning:     http.StatusConflict,
		errs.CodeWindowClosed:    http.StatusConflict,
		errs.CodeRateLimited:     http.StatusTooManyRequests,
		errs.CodeCapacity:        http.StatusTooManyRequests,
		errs.CodePhotoMissing:    http.StatusUnprocessableEntity,
		errs.CodePhotoInvalid:    http.StatusUnprocessableEntity,
		errs.CodePhotoRejected:   http.StatusUnprocessableEntity,
		errs.CodeLocationOutside: http.StatusUnprocessableEntity,
		errs.CodeUpstream:        http.StatusBadGateway,
		errs.CodeInternal:        http.StatusInternalServerError,
	}
	if len(want) != 16 {
		t.Fatalf("映射表录入 %d 个码，want 16", len(want))
	}
	for code, status := range want {
		if got := statusFor(code); got != status {
			t.Errorf("statusFor(%s) = %d, want %d", code, got, status)
		}
	}
}
