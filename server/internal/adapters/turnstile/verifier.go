// Package turnstile 校验 Cloudflare Turnstile 的人机验证令牌。
//
// # 功能
// 把前端拿到的 response 令牌连同客户端 IP 提交给 siteverify，回答通过、
// 拒绝或服务不可达。拒绝原因是 Cloudflare 的 error-codes，供日志使用。
// 本包**不**决定「要不要拦」：off/log/on 档位由组合根与 app/auth 判定，
// 本包只回答「这一枚令牌过没过」。也**不**记录、回显或存储令牌本身。
//
// # 归属
// adapters 层设施包。app/auth 的 HumanVerifier 端口实现。
//
// # 复用
// New、Verifier.Verify、DefaultEndpoint、VerifyTimeout。
//
// # 口径
// 超时 5s、不重试：注册是交互请求，重试只会放大用户等待。
// 判定三分：令牌无效回 (false, codes, nil)；网络故障、非 2xx、回执不是
// JSON、Cloudflare 自报 internal-error 回 error（可重试的暂时故障）。
// 可选的令牌绑定：期望主机名/动作非空时，回执必须匹配，不匹配按
// 「令牌不合格」回 (false, [{hostname,action}-mismatch], nil)，不是 error。
// 错误文本只含状态码与原因，不含令牌与私钥。
package turnstile

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// DefaultEndpoint 是 siteverify 的固定地址。
const DefaultEndpoint = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

// VerifyTimeout 是单次校验的上限。注册请求不应被验证服务拖过它。
const VerifyTimeout = 5 * time.Second

// maxAnswerBytes 限制回执读取量：正常回执不足 1KB，超出的部分是异常。
const maxAnswerBytes = 8 << 10

// Verifier 校验 Turnstile 令牌。零值不可用；请用 New 构造。
type Verifier struct {
	// Endpoint 是 siteverify 地址。生产恒为 DefaultEndpoint；
	// 只有测试会替换它，故不提供环境变量入口。
	Endpoint string
	secret   string
	// expectedHostname/expectedAction 是可选绑定：非空时回执必须匹配。
	// 空表示不校验——未配置绑定时的行为与引入绑定前完全一致。
	expectedHostname string
	expectedAction   string
	client           *http.Client
}

// New 构造校验器。私钥为空即失败关闭：没有私钥的校验器只会永远拒绝。
// expectedHostname 与 expectedAction 为空表示不校验该维度；期望值只是
// 校验条件，仍需私钥才能问出结果，故私钥为空时期望值无意义（由配置层拒绝）。
func New(secret, expectedHostname, expectedAction string) (*Verifier, error) {
	secret = strings.TrimSpace(secret)
	if secret == "" {
		return nil, errors.New("turnstile: secret must not be empty")
	}
	return &Verifier{
		Endpoint:         DefaultEndpoint,
		secret:           secret,
		expectedHostname: strings.TrimSpace(expectedHostname),
		expectedAction:   strings.TrimSpace(expectedAction),
		client:           &http.Client{Timeout: VerifyTimeout},
	}, nil
}

// Verify 提交一枚令牌。
//
// 第二个返回值是 Cloudflare 的 error-codes（通过时为空），调用方只把它
// 写进日志；第三个返回值非 nil 表示「没问出结果」，与「问出来了但没过」
// 严格区分：前者不能当成用户作弊。
func (v *Verifier) Verify(ctx context.Context, token, remoteIP string) (bool, []string, error) {
	if v == nil || v.secret == "" || v.Endpoint == "" {
		return false, nil, errors.New("turnstile: verifier is not configured")
	}
	token = strings.TrimSpace(token)
	if token == "" {
		return false, nil, errors.New("turnstile: token must not be empty")
	}
	form := url.Values{"secret": {v.secret}, "response": {token}}
	if remoteIP = strings.TrimSpace(remoteIP); remoteIP != "" {
		form.Set("remoteip", remoteIP)
	}
	ctx, cancel := context.WithTimeout(ctx, VerifyTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, v.Endpoint,
		strings.NewReader(form.Encode()))
	if err != nil {
		return false, nil, errors.New("turnstile: cannot build the siteverify request")
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	resp, err := v.client.Do(req)
	if err != nil {
		// 不回显底层错误原文：它可能带完整 URL，而 URL 里只有地址、没有密钥；
		// 但保持一条可诊断的原因足够，故保留 err 供日志归类。
		return false, nil, fmt.Errorf("turnstile: siteverify request failed: %w", err)
	}
	defer func() {
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, maxAnswerBytes))
		_ = resp.Body.Close()
	}()
	if resp.StatusCode < http.StatusOK || resp.StatusCode >= http.StatusMultipleChoices {
		return false, nil, fmt.Errorf("turnstile: siteverify answered status %d", resp.StatusCode)
	}
	var answer struct {
		Success    bool     `json:"success"`
		ErrorCodes []string `json:"error-codes"`
		Hostname   string   `json:"hostname"`
		Action     string   `json:"action"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, maxAnswerBytes)).Decode(&answer); err != nil {
		return false, nil, errors.New("turnstile: siteverify answer is not JSON")
	}
	if answer.Success {
		// 令牌绑定：回执带 siteverify 判定的 hostname 与 action。期望非空
		// 而回执不匹配，说明这枚令牌不是本站/本动作签发的：按「令牌不合格」
		// 回 codes（on 档 400），不能报成 error（on 档会变 502）。
		if v.expectedHostname != "" &&
			!strings.EqualFold(strings.TrimSpace(answer.Hostname), v.expectedHostname) {
			return false, []string{"hostname-mismatch"}, nil
		}
		if v.expectedAction != "" && answer.Action != v.expectedAction {
			return false, []string{"action-mismatch"}, nil
		}
		return true, nil, nil
	}
	// 这几档都是服务端故障或配置错，不是令牌问题：按不可达上报，
	// 免得把验证服务自己的抖动或密钥轮换笔误算成用户作弊（on 档会变成一片 400）。
	for _, code := range answer.ErrorCodes {
		switch code {
		case "internal-error", "missing-input-secret", "invalid-input-secret", "bad-request":
			return false, answer.ErrorCodes, fmt.Errorf("turnstile: siteverify rejected the request: %s", code)
		}
	}
	return false, answer.ErrorCodes, nil
}
