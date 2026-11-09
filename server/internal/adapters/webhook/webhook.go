// Package webhook 实现 webhook 推送的 HTTP 出口：Server酱（serverchan）
// 与自定义地址（custom）两条出口。
//
// # 功能
// 实现 core/webhook.Sender：按出口类型发一条推送，失败一律包成
// *corewebhook.DeliveryError（粗分类 + 可重试标志）。custom 出口在发送前
// 跑 SSRF 白名单（ssrf.go），复核通过后直连已复核 IP（闭合 DNS rebinding
// 的 TOCTOU）。本包**不**读配置、**不**记日志、**不**记账。
//
// # 归属
// adapters 层。app/notify 与 app/settings 只依赖 core/webhook.Sender。
//
// # 口径
// 出口凭据（serverchan 的 SendKey 拼在 URL path、custom 的 URL 本身）
// **只用于发请求**：错误 Error() 只含分类（core/webhook 口径），日志面
// 需要目标信息时用 MaskedHost 取 scheme+host 打码形态。响应体读入限长，
// 防止对端回超大响应耗尽内存。两条出口都不跟随重定向：3xx 意味着信没有
// 投递到对端处理逻辑，跳转目标也从未经过白名单。
package webhook

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"time"

	corewebhook "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/webhook"
)

// DefaultTimeout 是一次推送的总预算（连接 + 请求 + 读响应体），
// 与 app/notify 的投递预算同值。
const DefaultTimeout = 10 * time.Second

// maxResponseBody 是响应体的读入上限：回执是几 KB 的 JSON，
// 上限只防对端（或中间设备）回超大响应耗尽内存。
const maxResponseBody = 64 << 10

// maxTitleChars 是 Server酱服务端对 title 的长度上限，超了会被拒。
const maxTitleChars = 32

// serverchanHost 是 Server酱 Turbo 的固定出口主机。
// 失败日志只记这个常量 host 与 title，不记带 SendKey 的完整 URL。
const serverchanHost = "sctapi.ftqq.com"

// outletType 是出口类型词表：webhook 渠道内部的部署侧出口，
// 不是独立渠道。
type outletType string

const (
	outletServerchan outletType = "serverchan"
	outletCustom     outletType = "custom"
)

// Config 是出口构造参数。Type 是出口类型词表（serverchan/custom），
// Secret 是出口凭据：serverchan 传 SendKey，custom 传完整 webhook URL。
// Client 与 Lookup 是**测试注入点**（仅测试构造路径使用）：
// Client 换掉 HTTP 客户端以打假 transport；Lookup 换掉域名解析以测
// SSRF 复核。生产装配两者都留 nil。
type Config struct {
	Type   string
	Secret string
	Client *http.Client
	Lookup lookupIP
}

// Sender 是 core/webhook.Sender 的 HTTP 实现，按构造时的出口类型分流。
type Sender struct {
	outlet outletType
	secret string
	client *http.Client
	lookup lookupIP
}

// New 构造出口。Type 必须在词表内、Secret 必须非空，否则报错：
// 出口不存在时宁可启动失败，也不把凭据送往未预期的对端。
func New(cfg Config) (*Sender, error) {
	secret := strings.TrimSpace(cfg.Secret)
	if secret == "" {
		return nil, errors.New("webhook: secret is required")
	}
	outlet := outletType(strings.ToLower(strings.TrimSpace(cfg.Type)))
	if outlet == "" {
		// 与旧线兼容口径一致：未设类型但有 URL 时按 custom 发送。
		outlet = outletCustom
	}
	switch outlet {
	case outletServerchan, outletCustom:
	default:
		return nil, fmt.Errorf("webhook: type must be one of %s, %s",
			outletServerchan, outletCustom)
	}
	client := cfg.Client
	if client == nil {
		client = &http.Client{Timeout: DefaultTimeout}
	}
	return &Sender{
		outlet: outlet,
		secret: secret,
		client: client,
		lookup: cfg.Lookup,
	}, nil
}

// noRedirect 把客户端包成「不跟随重定向」的形态。两条出口都不跟随：
// 跳转目标从未经过白名单（custom），或跳转后的回执不再是本出口协议
// （serverchan）。对测试注入的 Client 同样生效——「不跟随」是出口行为，
// 不随注入面漂移。浅拷贝足够：Client 的字段都是值语义。
func noRedirect(c *http.Client) *http.Client {
	clone := *c
	clone.CheckRedirect = func(*http.Request, []*http.Request) error {
		return http.ErrUseLastResponse
	}
	return &clone
}

// Send 投递一条推送。失败一律是 *corewebhook.DeliveryError，
// 分类与可重试标志见 core/webhook 的词表。
func (s *Sender) Send(ctx context.Context, push corewebhook.Push) error {
	switch s.outlet {
	case outletServerchan:
		return s.sendServerchan(ctx, push)
	case outletCustom:
		return s.sendCustom(ctx, push)
	default:
		// 词表外出口宁可拒发，也不猜一条出口——那会把凭据送去未预期的对端。
		return &corewebhook.DeliveryError{
			Kind: corewebhook.KindRefused,
			Err:  errors.New("webhook: unknown outlet type"),
		}
	}
}

// sendCtx 把总预算收紧到 DefaultTimeout；调用方给更早的 deadline 时取更严者。
func sendCtx(ctx context.Context) (context.Context, context.CancelFunc) {
	return context.WithTimeout(ctx, DefaultTimeout)
}

// classifyTransport 把传输层错误拆成可重试的粗分类：
// 超时归 timeout，其余（含连接被拒、TLS 失败、连接中断）归 connect。
func classifyTransport(err error) *corewebhook.DeliveryError {
	kind := corewebhook.KindConnect
	if isDeadline(err) {
		kind = corewebhook.KindTimeout
	}
	return &corewebhook.DeliveryError{Kind: kind, Retryable: true, Err: err}
}

// isDeadline 报告错误是否为超时/截止类（含 context deadline）。
func isDeadline(err error) bool {
	type deadliner interface{ Timeout() bool }
	var d deadliner
	if errors.As(err, &d) {
		return d.Timeout()
	}
	return errors.Is(err, context.DeadlineExceeded)
}

// normalizeTitle 把推送标题规整成 Server酱可接受的形态：
// 去 CR/LF（服务端拒含换行的 title）、去首尾空白、按字符截 32。
// 多字节文本按 rune 截断，截断后仍是合法 UTF-8。
func normalizeTitle(title string) string {
	title = strings.ReplaceAll(title, "\r", " ")
	title = strings.ReplaceAll(title, "\n", " ")
	title = strings.TrimSpace(title)
	runes := []rune(title)
	if len(runes) > maxTitleChars {
		return string(runes[:maxTitleChars])
	}
	return title
}

// MaskedHost 返回 URL 的打码形态（scheme + host，去 path/query/userinfo），
// 供运维日志在需要目标信息时使用：完整 URL 本身就是凭据（custom）或
// path 里拼着 SendKey（serverchan），不得进日志。
func MaskedHost(raw string) string {
	parsed, err := url.Parse(raw)
	if err != nil {
		return "unparsed"
	}
	if parsed.Host == "" {
		return parsed.Scheme
	}
	return parsed.Scheme + "://" + parsed.Host
}
