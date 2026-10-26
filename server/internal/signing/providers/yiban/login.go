package yiban

import (
	"context"
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
	client, jar := c.newHTTPClient()
	codeURL, err := url.Parse(c.endpoints.CodeHTML)
	if err != nil {
		return contract.Profile{}, err
	}
	jar.SetCookies(codeURL, []*http.Cookie{{Name: "csrf_token", Value: csrf, Path: "/"}})

	// 1. 授权页：取 page_use 与 RSA 公钥。
	req, err := newRequest(ctx, http.MethodGet, withQuery(c.endpoints.CodeHTML, url.Values{
		"client_id":    {oauthClientID},
		"redirect_uri": {redirectURI},
	}), nil)
	if err != nil {
		return contract.Profile{}, err
	}
	setLoginHeaders(req)
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
		return contract.Profile{}, err
	}

	// 2. usersure：提交加密口令；头不得带 Origin/Referer。
	encrypted, err := protocol.EncryptPassword(cred.Password, page.PublicKeyPEM)
	if err != nil {
		return contract.Profile{}, err
	}
	form := protocol.BuildUsersureForm(cred.Phone, encrypted, oauthClientID, redirectURI, "authorize", nil)
	req, err = newRequest(ctx, http.MethodPost,
		withQuery(c.endpoints.Usersure, url.Values{"ajax_sign": {page.PageUse}}),
		strings.NewReader(form))
	if err != nil {
		return contract.Profile{}, err
	}
	setUsersureHeaders(req)
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
		return contract.Profile{}, err
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

	// 4. 最终认证：成功不只看 code=0，还要求 data 是身份对象。
	req, err = newRequest(ctx, http.MethodGet, withQuery(c.endpoints.APIAuth, url.Values{
		"verifyRequest": {token},
		"CSRF":          {csrf},
	}), nil)
	if err != nil {
		return contract.Profile{}, err
	}
	setLoginHeaders(req)
	resp, body, err = c.do(client, req)
	if err != nil {
		return contract.Profile{}, err
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
		return contract.Profile{}, err
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
		return contract.Profile{}, err
	}

	profile := contract.Profile{
		DisplayName: identity.PersonName,
		SchoolName:  identity.UniversityName,
	}
	entry.http = client
	entry.csrf = csrf
	entry.expires = c.clock.Now().Add(c.ttl)
	return profile, nil
}
