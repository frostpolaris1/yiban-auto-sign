// Package errs is the single definition of stable error codes.
package errs

import "errors"

// Code is a stable, machine-readable error code.
type Code string

const (
	CodeValidation      Code = "validation_failed"
	CodeUnauthorized    Code = "unauthorized"
	CodeForbidden       Code = "forbidden"
	CodeNotFound        Code = "not_found"
	CodeConflict        Code = "conflict"
	CodeRateLimited     Code = "rate_limited"
	CodeCapacity        Code = "capacity_exceeded"
	CodeWindowClosed    Code = "window_closed"
	CodeAlreadySigned   Code = "already_signed"
	CodeSignRunning     Code = "sign_running"
	CodePhotoMissing    Code = "photo_missing"
	CodePhotoInvalid    Code = "photo_invalid"
	CodePhotoRejected   Code = "photo_rejected"
	CodeLocationOutside Code = "location_out_of_range"
	CodeUpstream        Code = "upstream_unavailable"
	CodeInternal        Code = "internal_error"
)

// Error carries a stable code, a user-safe message and optional details.
type Error struct {
	Code    Code
	Message string
	Details map[string]any
	TraceID string
}

// New builds an Error.
func New(code Code, message string) *Error {
	return &Error{Code: code, Message: message}
}

// Error implements error.
func (e *Error) Error() string {
	return string(e.Code) + ": " + e.Message
}

// From returns the stable error for err. Unknown errors become internal
// errors, so a raw implementation error never reaches a response body.
func From(err error) *Error {
	if err == nil {
		return nil
	}
	var e *Error
	if errors.As(err, &e) {
		return e
	}
	return New(CodeInternal, "internal error")
}

// ErrNotFound is the shared not-found error.
var ErrNotFound = New(CodeNotFound, "not found")
