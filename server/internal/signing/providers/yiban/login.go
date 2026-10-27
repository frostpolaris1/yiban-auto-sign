package yiban

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strings"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/protocol"
)

// loginLocked 执行四跳登录链，并把会话写进 entry。
// 调用方必须持有 entry.mu。
//
// # 线上形状清单（复刻旧实现 yiban/platform.py::login_killyiban，ref develop@1d1c5ccf）
//
// 夹具与判据以旧实现行为为底本，标注「旧实现复刻」，未经真实抓包验证；
// 实拍对齐是 4sk 后续生产探针步骤，本实现不 claim 真实性。
//
//  1. GET oauth.yiban.cn/code/html?client_id=…&redirect_uri=…，不跟跳。
//     头：UA "Yiban"、AppVersion 5.2.3、Origin/Referer c.uyiban.com、Connection: close；
//     cookie csrf_token（域名空，随每跳显式携带）。302 落 f.yiban.cn/iapp7463
//     在旧实现是会话缓存探针命中；新栈会话用 TTL 判活，此处一律按失败处置。
//  2. POST oauth.yiban.cn/code/usersure?ajax_sign=page_use，不跟跳。
//     头不带 Origin/Referer（带了得 e001）；体字段序固定
//     oauth_uname、oauth_upwd（RSA-1024 PKCS1v1.5 密文）、client_id、redirect_uri、
//     display=authorize、scope=（空值，末位）。响应先判 WAF/挑战再解析；
//     code=="s200" 为成功。
//  3. GET f.yiban.cn/iframe/index?act=iapp7463，不跟跳；
//     从 Location 的 query 提取 verify_request。
//  4. GET api.uyiban.com/base/c/auth/yiban?verifyRequest=…&CSRF=…（该顺序），
//     手动跟跳至多 5 层：每跳先判 WAF/挑战，再校验落点主机白名单
//     （新栈只认 API 主机，比旧实现的 yiban.cn/uyiban.com 宽白名单更窄，fail-closed）。
//     落点 JSON 信封：code∈{0,"0"} 且 data 非空且非 {Action,Data} 入口步 →
//     登录成功（签发回执判据，本项目自有判定）；data={} 是录制到的真实成功形状；
//     身份字段只作展示面，缺失不回退登录结论。
func (c *Client) loginLocked(ctx context.Context, entry *sessionEntry, cred contract.Credential) (contract.Profile, error) {
	if strings.TrimSpace(cred.Phone) == "" || cred.Password == "" {
		return contract.Profile{}, outcome(contract.OutcomeCredentialBad, "phone and password are required")
	}
	csrf, err := randomHex(c.random, 16)
	if err != nil {
		return contract.Profile{}, err
	}
	client := c.newHTTPClient()

	// 1. 授权页：取 page_use 与 RSA 公钥。
	req, err := newRequest(ctx, http.MethodGet, withQuery(c.endpoints.CodeHTML, url.Values{
		"client_id":    {oauthClientID},
		"redirect_uri": {redirectURI},
	}), nil)
	if err != nil {
		return contract.Profile{}, err
	}
	setLoginHeaders(req)
	addCSRF(req, csrf)
	resp, body, err := c.do(client, req)
	if err != nil {
		return contract.Profile{}, err
	}
	if resp.StatusCode >= 300 && resp.StatusCode < 400 {
		return contract.Profile{}, outcome(contract.OutcomeTransient,
			"login page redirected while no session was cached")
	}
	if resp.StatusCode != http.StatusOK {
		return contract.Profile{}, outcome(contract.OutcomeTransient,
			fmt.Sprintf("login page status %d", resp.StatusCode))
	}
	if wafBlocked(string(body)) {
		return contract.Profile{}, blockedOutcome(string(body))
	}
	page, err := protocol.ParseAuthorizePage(string(body))
	if err != nil {
		return contract.Profile{}, parseOutcome(err)
	}

	// 2. usersure：提交加密口令；头不得带 Origin/Referer。
	encrypted, err := protocol.EncryptPassword(cred.Password, page.PublicKeyPEM)
	if err != nil {
		return contract.Profile{}, err
	}
	// 旧实现传空 scope（ref develop@1d1c5ccf）：线上体以 "&scope=" 空值收尾。
	scope := ""
	form := protocol.BuildUsersureForm(cred.Phone, encrypted, oauthClientID, redirectURI, "authorize", &scope)
	req, err = newRequest(ctx, http.MethodPost,
		withQuery(c.endpoints.Usersure, url.Values{"ajax_sign": {page.PageUse}}),
		strings.NewReader(form))
	if err != nil {
		return contract.Profile{}, err
	}
	setUsersureHeaders(req)
	addCSRF(req, csrf)
	resp, body, err = c.do(client, req)
	if err != nil {
		return contract.Profile{}, err
	}
	// 旧实现先判 WAF/挑战再做 JSON 解析（ref develop@1d1c5ccf usersure 步）。
	if wafBlocked(string(body)) {
		return contract.Profile{}, blockedOutcome(string(body))
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return contract.Profile{}, outcome(contract.OutcomeTransient,
			fmt.Sprintf("usersure status %d", resp.StatusCode))
	}
	usersure, err := protocol.ParseUsersureResponse(body)
	if err != nil {
		return contract.Profile{}, parseOutcome(err)
	}
	if !usersure.OK {
		return contract.Profile{}, outcome(contract.OutcomeCredentialBad, "usersure code "+usersure.Code)
	}

	// 3. iframe：从 302 的 Location 取 verify_request。
	req, err = newRequest(ctx, http.MethodGet,
		withQuery(c.endpoints.IframeIndex, url.Values{"act": {iframeAct}}), nil)
	if err != nil {
		return contract.Profile{}, err
	}
	setLoginHeaders(req)
	addCSRF(req, csrf)
	resp, body, err = c.do(client, req)
	if err != nil {
		return contract.Profile{}, err
	}
	if resp.StatusCode < 300 || resp.StatusCode >= 400 {
		return contract.Profile{}, outcome(contract.OutcomeTransient,
			fmt.Sprintf("iframe status %d", resp.StatusCode))
	}
	if wafBlocked(string(body)) {
		return contract.Profile{}, blockedOutcome(string(body))
	}
	token, ok := protocol.ExtractVerifyRequest(resp.Header.Get("Location"))
	if !ok {
		return contract.Profile{}, outcome(contract.OutcomeUnknown, "verify_request is missing")
	}

	// 4. 最终认证：手动跟跳最多 5 层，只允许落到配置的 API 主机。
	// query 顺序与旧实现一致：verifyRequest 在前、CSRF 在后
	// （url.Values.Encode 按字母序会排成 CSRF 在前，与旧实现线上形状不符）。
	authURL := withQueryOrdered(c.endpoints.APIAuth, [][2]string{
		{"verifyRequest", token},
		{"CSRF", csrf},
	})
	for redirects := 0; ; redirects++ {
		req, err = newRequest(ctx, http.MethodGet, authURL, nil)
		if err != nil {
			return contract.Profile{}, err
		}
		setLoginHeaders(req)
		addCSRF(req, csrf)
		resp, body, err = c.do(client, req)
		if err != nil {
			return contract.Profile{}, err
		}
		// 旧实现每跳响应都先判 WAF/挑战（require_not_blocked）。
		if wafBlocked(string(body)) {
			return contract.Profile{}, blockedOutcome(string(body))
		}
		if resp.StatusCode < 300 || resp.StatusCode >= 400 {
			break
		}
		if redirects >= maxAuthRedirects {
			return contract.Profile{}, outcome(contract.OutcomeUnknown, "auth redirect limit exceeded")
		}
		next, err := req.URL.Parse(resp.Header.Get("Location"))
		if err != nil || !c.trustedAuthHost(next) {
			return contract.Profile{}, outcome(contract.OutcomeUnknown, "untrusted auth redirect")
		}
		authURL = next.String()
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return contract.Profile{}, outcome(contract.OutcomeTransient,
			fmt.Sprintf("auth status %d", resp.StatusCode))
	}
	envelope, err := protocol.ParseAPIEnvelope(body)
	if err != nil {
		return contract.Profile{}, parseOutcome(err)
	}
	if !protocol.CodeIsOK(envelope.Code) {
		return contract.Profile{}, outcome(contract.OutcomeUnknown,
			fmt.Sprintf("auth code %v", envelope.Code))
	}
	// 签发回执判据（本项目自有判定，复刻旧实现，ref develop@1d1c5ccf）：
	// code==0 只代表"签发方没有否认"。data 缺失是无回执形状；data 带
	// Action/Data 键是未认证入口步回退。两者都不得当登录成功。
	data := envelope.Data
	if data == nil {
		return contract.Profile{}, outcome(contract.OutcomeUnknown,
			"auth returned no issuer receipt (code ok but data missing)")
	}
	if payload, ok := data.(map[string]any); ok {
		if _, has := payload["Action"]; has {
			return contract.Profile{}, outcome(contract.OutcomeUnknown,
				"auth returned entry-step payload (no issuer receipt)")
		}
		if _, has := payload["Data"]; has {
			return contract.Profile{}, outcome(contract.OutcomeUnknown,
				"auth returned entry-step payload (no issuer receipt)")
		}
	}
	// 回执已成立，身份字段只作展示面：缺失不回退登录结论（data={} 是旧实现
	// 录制到的真实成功形状，ref develop@1d1c5ccf）。
	identity, err := protocol.ParseIdentity(data)
	if err != nil {
		identity = protocol.Identity{}
	}

	// 不映射 PersonId：它是平台内部 ID，不是学号；Raw 也不放身份数据。
	profile := contract.Profile{
		DisplayName: identity.PersonName,
		SchoolName:  identity.UniversityName,
	}
	entry.http = client
	entry.csrf = csrf
	entry.expires = c.clock.Now().Add(c.ttl)
	return profile, nil
}

// parseOutcome 把协议解析错误压成不含原始片段的结论错误。
// ParseError.Fragment 可能含身份字段，不能直接外溢。
func parseOutcome(err error) error {
	var parse *protocol.ParseError
	if errors.As(err, &parse) {
		return outcome(contract.OutcomeUnknown, "malformed "+parse.Field+" payload")
	}
	return err
}
