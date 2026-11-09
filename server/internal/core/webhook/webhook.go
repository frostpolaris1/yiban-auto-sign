// Package webhook 定义 webhook 推送端口与失败分类。
//
// # 功能
// 声明 Sender 端口、推送消息 Push，以及投递失败的粗分类
// （可重试 / 不可重试）。本包**不**发 HTTP、**不**读配置、**不**记日志。
//
// # 归属
// core 层纯领域包。app/notify 依赖 Sender；adapters/webhook 实现它。
// 分类词表住在这里，使用例层不导入任何传输实现。
//
// # 复用
// Sender、Push、Kind 及其常量、DeliveryError、Classify。
//
// # 口径
// 失败一律是 *DeliveryError。Error() 只含分类与可重试标志，
// **不含**底层错误文本：HTTP 状态码、响应体、URL 与 SendKey 都可能
// 藏在传输层错误里，凭据不得进日志。底层错误经 Unwrap 可达，
// 供 errors.Is/As 使用。
package webhook

import (
	"context"
	"errors"
	"fmt"
)

// Push 是一条 webhook 推送。
// Title 是推送标题；Content 是正文。两者都由调用方在落库前掩码，
// 出口层不再做脱敏：对端收到什么取决于调用方传了什么。
type Push struct {
	Title   string
	Content string
}

// Sender 投递一条 webhook 推送。
//
// 返回 nil 表示对端已受理（serverchan 的 code==0，custom 的 2xx 且
// 无业务码拒绝）。返回错误时调用方可用 Classify 取粗分类；
// **不得**把错误文本写进日志：URL 与 SendKey 都可能藏在其中。
type Sender interface {
	Send(ctx context.Context, push Push) error
}

// Kind 是投递失败的粗分类。
// 新增词必须同步日志断言与运维手册；日志只许出现本词表。
type Kind string

const (
	// KindConnect 是连接失败或连接中断。可重试。
	KindConnect Kind = "connect"
	// KindTimeout 是截止时间到。可重试。
	KindTimeout Kind = "timeout"
	// KindBadResponse 是对端应答不符合预期（非 JSON、JSON 非对象等）。
	// 可重试：网关错误页与半截响应常见，下一轮往往恢复。
	KindBadResponse Kind = "bad_response"
	// KindRefused 是对端拒收：非 2xx、业务码非 0、未知出口类型。
	// **不可重试**：拒收是确定性结果，重试只会再被拒。
	KindRefused Kind = "refused"
	// KindUnsafeURL 是本地安全防线拒发（SSRF/白名单）。
	// **不可重试**：改配置之前重试没有意义。
	KindUnsafeURL Kind = "unsafe_url"
	// KindPanic 是投递实现 panic 的兜底分类。
	// **不可重试**：panic 是实现缺陷，重投只会再 panic。
	// 本值不由 Classify 返回；调用方的 recover 分支直接用它记账。
	KindPanic Kind = "panic"
)

// DeliveryError 是 Sender.Send 的唯一失败类型。
//
// Kind 与 Retryable 是给调用方与日志用的粗分类；
// Err 保留原始错误链，但 Error() 不输出它。
type DeliveryError struct {
	Kind      Kind
	Retryable bool
	Err       error
}

// Error 只输出分类。**不输出**底层错误文本：
// serverchan 的 URL path 里拼着 SendKey，custom 的 URL 本身就是凭据，
// HTTP 状态码是对端端口探测指纹，都不得经错误串进日志。
func (e *DeliveryError) Error() string {
	if e == nil {
		return "<nil>"
	}
	return fmt.Sprintf("webhook: delivery failed (kind=%s retryable=%t)", e.Kind, e.Retryable)
}

// Unwrap 暴露原始错误，供 errors.Is/As 判断取消与超时。
func (e *DeliveryError) Unwrap() error {
	if e == nil {
		return nil
	}
	return e.Err
}

// Classify 把任意错误拆成粗分类。
//
// nil 返回空分类；非 *DeliveryError 视为不可重试的拒收——
// 未知错误不得被当成可重试，否则调用方可能反复重投。
func Classify(err error) (Kind, bool) {
	if err == nil {
		return "", false
	}
	var de *DeliveryError
	if errors.As(err, &de) {
		kind := de.Kind
		if kind == "" {
			kind = KindRefused
		}
		return kind, de.Retryable
	}
	return KindRefused, false
}
