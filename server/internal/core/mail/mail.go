// Package mail 定义邮件投递端口与失败分类。
//
// # 功能
// 声明 Sender 端口、纯文本邮件 Message，以及投递失败的粗分类
// （可重试 / 不可重试）。本包**不**连 SMTP、**不**读配置、**不**记日志。
//
// # 归属
// core 层纯领域包。app/auth 依赖 Sender；adapters/mail 实现它。
// 分类词表住在这里，使用例层不导入任何传输实现。
//
// # 复用
// Sender、Message、Kind 及其常量、DeliveryError、Classify、
// ErrUnsupportedAddress、TLSMode 常量与 TLSModes。
//
// # 口径
// 失败一律是 *DeliveryError。Error() 只含分类与可重试标志，
// **不含**底层错误文本：日志面按分类聚合，凭据与目标细节不出传输层。
// 底层错误经 Unwrap 可达，供 errors.Is/As 使用。
// TLS 模式词表只在本包定义：config 的闭集与 adapters/mail 的解析同源。
package mail

import (
	"context"
	"errors"
	"fmt"
)

// Message 是一封纯文本邮件。
// To 是单个收件人地址；Subject 与 Text 由调用方给出。
type Message struct {
	To      string
	Subject string
	Text    string
}

// Sender 投递一封邮件。
//
// 返回 nil 表示 SMTP 服务器已接收（DATA 已被 ACK）。
// 返回错误时调用方可用 Classify 取粗分类；**不得**把错误文本写进日志。
type Sender interface {
	Send(ctx context.Context, msg Message) error
}

// Kind 是投递失败的粗分类。
// 新增词必须同步日志断言与运维手册；日志只许出现本词表。
type Kind string

const (
	// KindConnect 是连接、TLS 握手或连接中断失败。可重试。
	KindConnect Kind = "connect"
	// KindTimeout 是截止时间到。可重试。
	KindTimeout Kind = "timeout"
	// KindAuth 是认证被拒。不可重试：凭据错重试只会再失败。
	KindAuth Kind = "auth"
	// KindSender 是发件人被拒。4xx 可重试，5xx 不可重试。
	KindSender Kind = "sender"
	// KindRecipient 是收件人被拒。4xx 可重试，5xx 不可重试。
	KindRecipient Kind = "recipient"
	// KindMessage 是邮件内容被拒，或 DATA 已发出后的失败。
	// **不可重试**：重试可能造成双发。
	KindMessage Kind = "message"
	// KindProtocol 是对端应答不符合预期。默认不可重试。
	KindProtocol Kind = "protocol"
	// KindConfig 是本地配置错误（地址形状、TLS 证书不受信）。
	// **不可重试**：改配置之前重试没有意义。
	KindConfig Kind = "config"
	// KindPanic 是投递实现 panic 的兜底分类。
	// **不可重试**：panic 是实现缺陷，重投只会再 panic。
	// 本值不由 Classify 返回；调用方的 recover 分支直接用它记账。
	KindPanic Kind = "panic"
)

// TLS 模式是 YIBAN_MAIL_TLS_MODE 的闭集。
//
// 唯一定义点：config 用它生成启动期闭集，adapters/mail 用它解析档位。
// 新增模式必须同时改这两处的读取路径；adapters/mail 的测试钉住
// 「本组每个取值适配器都能解析」。
const (
	// TLSModeAuto 按端口选档：465 走隐式 TLS，其余走 STARTTLS。
	TLSModeAuto = "auto"
	// TLSModeImplicit 强制隐式 TLS（连上即握手）。
	TLSModeImplicit = "implicit"
	// TLSModeStartTLS 强制明文连接后升级。
	TLSModeStartTLS = "starttls"
)

// TLSModes 返回闭集，顺序固定。
// 每次调用返回新切片：调用方可以排序或改写而不影响其他读者。
func TLSModes() []string {
	return []string{TLSModeAuto, TLSModeImplicit, TLSModeStartTLS}
}

// ErrUnsupportedAddress 表示收件地址能通过注册口径，但投递路径无法表达。
//
// 两个来源：
//   - 含控制字符（< 0x20 或 0x7F）：net/mail 引用时会丢弃它们，
//     投递地址会与注册地址不同。
//   - 含非 ASCII 字符：net/smtp 不发 SMTPUTF8，出站必被服务端拒。
//
// 投递实现把它包进 DeliveryError（KindConfig、不可重试）。
// 调用方用 errors.Is 判定，并据此在日志里记显性原因。
var ErrUnsupportedAddress = errors.New("mail: recipient address is not supported by this delivery path")

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
// SMTP 应答里可能带目标主机与账号线索，凭据不得进日志。
func (e *DeliveryError) Error() string {
	if e == nil {
		return "<nil>"
	}
	return fmt.Sprintf("mail: delivery failed (kind=%s retryable=%t)", e.Kind, e.Retryable)
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
// nil 返回空分类；非 *DeliveryError 视为不可重试的协议错误——
// 未知错误不得被当成可重试，否则调用方可能反复重投。
func Classify(err error) (Kind, bool) {
	if err == nil {
		return "", false
	}
	var de *DeliveryError
	if errors.As(err, &de) {
		kind := de.Kind
		if kind == "" {
			kind = KindProtocol
		}
		return kind, de.Retryable
	}
	return KindProtocol, false
}
