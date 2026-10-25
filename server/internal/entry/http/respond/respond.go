// Package respond is the only place that writes an error response body.
package respond

import (
	"encoding/json"
	"net/http"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
)

// Envelope is the error response shape.
type Envelope struct {
	Error Body `json:"error"`
}

// Body is the error detail.
type Body struct {
	Code    string         `json:"code"`
	Message string         `json:"message"`
	Details map[string]any `json:"details,omitempty"`
	TraceID string         `json:"trace_id,omitempty"`
}

// Error writes err as the stable JSON envelope.
func Error(w http.ResponseWriter, r *http.Request, err error) {
	e := errs.From(err)
	if e == nil {
		e = errs.New(errs.CodeInternal, "internal error")
	}
	traceID := e.TraceID
	if traceID == "" && r != nil {
		traceID = r.Header.Get("X-Request-Id")
	}
	writeJSON(w, statusFor(e.Code), Envelope{Error: Body{
		Code:    string(e.Code),
		Message: e.Message,
		Details: e.Details,
		TraceID: traceID,
	}})
}

// JSON writes a success body.
func JSON(w http.ResponseWriter, status int, v any) {
	writeJSON(w, status, v)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func statusFor(code errs.Code) int {
	switch code {
	case errs.CodeValidation:
		return http.StatusBadRequest
	case errs.CodeUnauthorized:
		return http.StatusUnauthorized
	case errs.CodeForbidden:
		return http.StatusForbidden
	case errs.CodeNotFound:
		return http.StatusNotFound
	case errs.CodeConflict, errs.CodeAlreadySigned, errs.CodeSignRunning, errs.CodeWindowClosed:
		return http.StatusConflict
	case errs.CodeRateLimited, errs.CodeCapacity:
		return http.StatusTooManyRequests
	case errs.CodePhotoMissing, errs.CodePhotoInvalid, errs.CodePhotoRejected, errs.CodeLocationOutside:
		return http.StatusUnprocessableEntity
	case errs.CodeUpstream:
		return http.StatusBadGateway
	default:
		return http.StatusInternalServerError
	}
}
