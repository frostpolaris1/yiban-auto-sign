// Package respond 是写错误响应体的唯一位置。
//
// # 功能
// 把 core/errs 错误编码为固定 JSON 信封，并把错误码映射为 HTTP 状态码。
// 本包**不**鉴权、**不**写业务数据、**不**记日志。
//
// # 归属
// entry/http 入口层。路由、中间件与处理器都经本包回错误。
//
// # 复用
// Error、NotImplemented、JSON；Envelope、Body；RequestIDHeader。
//
// # 口径
// 信封四键恒在：code、message、details、trace_id。
// details 缺省发 {}，trace_id 缺省发 ""，**形状不随场景变化**。
package respond

import (
	"encoding/json"
	"net/http"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
)

// RequestIDHeader 双向携带 trace id：请求头与响应头同名。
const RequestIDHeader = "X-Request-Id"

// Envelope 是错误响应的外形。
type Envelope struct {
	Error Body `json:"error"`
}

// Body 是错误明细。
// **四键恒在**：消费方依赖稳定形状，
// 故无 details 时发 {}，无 trace id 时发 ""。
type Body struct {
	Code    string         `json:"code"`
	Message string         `json:"message"`
	Details map[string]any `json:"details"`
	TraceID string         `json:"trace_id"`
}

// Error 把 err 写成稳定 JSON 信封。
func Error(w http.ResponseWriter, r *http.Request, err error) {
	e := errs.From(err)
	if e == nil {
		e = errs.New(errs.CodeInternal, "internal error")
	}
	writeJSON(w, statusFor(e.Code), Envelope{Error: bodyOf(e, r)})
}

// NotImplemented 为「契约保留、本阶段未实现」的端点
// 写 internal_error 信封与 501。
func NotImplemented(w http.ResponseWriter, r *http.Request) {
	e := errs.New(errs.CodeInternal, "not implemented")
	writeJSON(w, http.StatusNotImplemented, Envelope{Error: bodyOf(e, r)})
}

// JSON 写成功响应体。
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
