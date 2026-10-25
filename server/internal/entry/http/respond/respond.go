// Package respond is the only place that writes an error response body.
package respond

import (
	"encoding/json"
	"net/http"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
)

// RequestIDHeader carries the trace id in both directions.
const RequestIDHeader = "X-Request-Id"

// Envelope is the error response shape.
type Envelope struct {
	Error Body `json:"error"`
}

// Body is the error detail. Every key is always present: consumers rely on a
// stable shape, so an unused details map is emitted as {} and a missing trace
// id as "".
type Body struct {
	Code    string         `json:"code"`
	Message string         `json:"message"`
	Details map[string]any `json:"details"`
	TraceID string         `json:"trace_id"`
}

// Error writes err as the stable JSON envelope.
func Error(w http.ResponseWriter, r *http.Request, err error) {
	e := errs.From(err)
	if e == nil {
		e = errs.New(errs.CodeInternal, "internal error")
	}
	writeJSON(w, statusFor(e.Code), Envelope{Error: bodyOf(e, r)})
}

// NotImplemented writes the internal_error envelope with 501 for an endpoint
// that the contract reserves but this phase does not implement.
func NotImplemented(w http.ResponseWriter, r *http.Request) {
	e := errs.New(errs.CodeInternal, "not implemented")
	writeJSON(w, http.StatusNotImplemented, Envelope{Error: bodyOf(e, r)})
}

// JSON writes a success body.
func JSON(w http.ResponseWriter, status int, v any) {
	writeJSON(w, status, v)
}

func bodyOf(e *errs.Error, r *http.Request) Body {
	details := e.Details
	if details == nil {
		details = map[string]any{}
	}
	traceID := e.TraceID
	if traceID == "" && r != nil {
		traceID = r.Header.Get(RequestIDHeader)
	}
	return Body{
		Code:    string(e.Code),
		Message: e.Message,
		Details: details,
		TraceID: traceID,
	}
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
