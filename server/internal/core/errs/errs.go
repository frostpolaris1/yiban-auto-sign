// Package errs 是稳定错误码的唯一定义。
//
// # 功能
// 定义机器可读错误码与 Error 结构，并把未知错误归一。
// 本包**不**写 HTTP 响应、**不**读 SQLSTATE。
// 状态码映射见 entry/http/respond；SQLSTATE 映射见 adapters/pg。
//
// # 归属
// core 层纯领域包，全仓共用。用例与入口只见本包错误。
//
// # 复用
// Code 及全部码常量；Error、New、From、ErrNotFound。
//
// # 口径
// Code 是契约面，**已发布的值不得改动**。
// From 把未知错误压成 internal_error，原始消息不进响应体。
package errs

import "errors"

// Code 是稳定的机器可读错误码。
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

// Error 携带稳定码、用户安全消息与可选详情。
type Error struct {
	Code    Code
	Message string
	Details map[string]any
	TraceID string
}

// New 构造 Error。
func New(code Code, message string) *Error {
	return &Error{Code: code, Message: message}
}

// Error 实现 error。
func (e *Error) Error() string {
	return string(e.Code) + ": " + e.Message
}

// From 返回 err 对应的稳定错误。
// 未知错误变为 internal_error，**原始实现错误不进响应体**。
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

// ErrNotFound 是共用的未找到错误。
var ErrNotFound = New(CodeNotFound, "not found")
