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
// Error、NotImplemented、JSON、JSONWithETag、IfNoneMatchHit；
// Envelope、Body；RequestIDHeader。
//
// # 口径
// 信封四键恒在：code、message、details、trace_id。
// details 缺省发 {}，trace_id 缺省发 ""，**形状不随场景变化**。
package respond

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/http"
	"strings"

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
// 急停（*errs.PausedError）映射 503：code 仍是既有 capacity_exceeded，
// 状态码单独抬成「暂时不可用」，调用方按重试处理。
func Error(w http.ResponseWriter, r *http.Request, err error) {
	e := errs.From(err)
	if e == nil {
		e = errs.New(errs.CodeInternal, "internal error")
	}
	status := statusFor(e.Code)
	var paused *errs.PausedError
	if errors.As(err, &paused) {
		status = http.StatusServiceUnavailable
	}
	writeJSON(w, status, Envelope{Error: bodyOf(e, r)})
}

// NotImplemented 为「契约保留、本阶段未实现」的端点
// 写 internal_error 信封与 501。
func NotImplemented(w http.ResponseWriter, r *http.Request) {
	e := errs.New(errs.CodeInternal, "not implemented")
	writeJSON(w, http.StatusNotImplemented, Envelope{Error: bodyOf(e, r)})
}

// BindError 把请求体解码失败写成信封。
//
// 请求体超过入口上界时回 413；其余仍是 400 validation_failed。
// 上界失败**不得**落到 400：调用方要能区分「格式错」与「体积超限」。
// 所有处理器共用本函数，故认证与三个业务域的口径只有一份。
func BindError(w http.ResponseWriter, r *http.Request, err error) {
	var tooLarge *http.MaxBytesError
	if errors.As(err, &tooLarge) {
		PayloadTooLarge(w, r)
		return
	}
	Error(w, r, errs.New(errs.CodeValidation, "invalid request"))
}

// PayloadTooLarge 写 413 信封。
// 请求体超过入口上界时用它；错误码沿用 validation_failed，
// 调用方凭状态码区分「格式错」与「体积超限」。
func PayloadTooLarge(w http.ResponseWriter, r *http.Request) {
	e := errs.New(errs.CodeValidation, "request body too large")
	writeJSON(w, http.StatusRequestEntityTooLarge, Envelope{Error: bodyOf(e, r)})
}

// JSON 写成功响应体。
func JSON(w http.ResponseWriter, status int, v any) {
	writeJSON(w, status, v)
}

// JSONWithETag 写成功响应体，并用**实际写出的字节**做 ETag。
//
// ETag 是响应体的 sha256（带引号 hex），故 200 与 304 的两个校验值
// 严格对应同一份字节。If-None-Match 命中时只写 ETag 与缓存指令，
// 回 304：不声明 Content-Type/Content-Length，也不写响应体。
// Marshal 失败走 Error 信封，不写半截成功响应。
func JSONWithETag(w http.ResponseWriter, r *http.Request, status int, inm string, v any) {
	raw, err := json.Marshal(v)
	if err != nil {
		Error(w, r, err)
		return
	}
	sum := sha256.Sum256(raw)
	etag := `"` + hex.EncodeToString(sum[:]) + `"`
	w.Header().Set("ETag", etag)
	// 入口中间件对 /api 统一发 no-store；列表需要每次复验而不是禁止缓存，
	// 故这里覆写为 private, max-age=0，与照片单图同口径。
	w.Header().Set("Cache-Control", "private, max-age=0")
	if IfNoneMatchHit(inm, etag) {
		w.WriteHeader(http.StatusNotModified)
		return
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_, _ = w.Write(raw)
}

// IfNoneMatchHit 按 RFC 9110 的 If-None-Match 语义判命中：
// 逗号列表、弱比较（W/ 前缀两侧等价）、`*` 匹配任一表示。
func IfNoneMatchHit(header, etag string) bool {
	for _, candidate := range strings.Split(header, ",") {
		candidate = strings.TrimSpace(candidate)
		if candidate == "*" {
			return true
		}
		if strings.TrimPrefix(candidate, "W/") == etag {
			return true
		}
	}
	return false
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
	case errs.CodeConflict, errs.CodeAlreadySigned, errs.CodeSignRunning, errs.CodeWindowClosed,
		errs.CodeCapacityShortfall, errs.CodeTaskAmbiguous:
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
