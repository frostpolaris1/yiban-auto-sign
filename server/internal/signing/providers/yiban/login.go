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
	if protocol.LooksLikeChallenge(string(body)) {
		return contract.Profile{}, outcome(contract.OutcomeChallenge, "challenge page")
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
	// SPEC §2.6：观测流程未携带 scope。参考实现的空 scope 是等价写法；
	// 本实现不发送该字段。
	form := protocol.BuildUsersureForm(cred.Phone, encrypted, oauthClientID, redirectURI, "authorize", nil)
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
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return contract.Profile{}, outcome(contract.OutcomeTransient,
			fmt.Sprintf("usersure status %d", resp.StatusCode))
	}
	if protocol.LooksLikeChallenge(string(body)) {
		return contract.Profile{}, outcome(contract.OutcomeChallenge, "challenge page")
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
	if protocol.LooksLikeChallenge(string(body)) {
		return contract.Profile{}, outcome(contract.OutcomeChallenge, "challenge page")
	}
	token, ok := protocol.ExtractVerifyRequest(resp.Header.Get("Location"))
	if !ok {
		return contract.Profile{}, outcome(contract.OutcomeUnknown, "verify_request is missing")
	}

	// 4. 最终认证：手动跟跳最多 5 层，只允许落到配置的 API 主机。
	authURL := withQuery(c.endpoints.APIAuth, url.Values{
		"verifyRequest": {token},
		"CSRF":          {csrf},
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
	if protocol.LooksLikeChallenge(string(body)) {
		return contract.Profile{}, outcome(contract.OutcomeChallenge, "challenge page")
	}
	envelope, err := protocol.ParseAPIEnvelope(body)
	if err != nil {
		return contract.Profile{}, parseOutcome(err)
	}
	if !protocol.CodeIsOK(envelope.Code) {
		return contract.Profile{}, outcome(contract.OutcomeUnknown,
			fmt.Sprintf("auth code %v", envelope.Code))
	}
	data, ok := envelope.Data.(map[string]any)
	if !ok {
		return contract.Profile{}, outcome(contract.OutcomeUnknown, "auth data is not an object")
	}
	identity, err := protocol.ParseIdentity(data)
	if err != nil {
		return contract.Profile{}, parseOutcome(err)
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
