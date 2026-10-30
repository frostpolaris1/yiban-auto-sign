package yiban

import (
	"context"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/protocol"
)

// positionTTL 是 signPosition 响应的短缓存。
// 宿主可能先取窗口再取目标；缓存避免同一账号连打两次平台。
const positionTTL = 5 * time.Second

// SignPosition 读取签到任务，按账号缓存 5 秒。
func (c *Client) SignPosition(ctx context.Context, accountID string, cred contract.Credential) (protocol.SignPositionConfig, error) {
	c.posMu.Lock()
	if entry, ok := c.positions[accountID]; ok && c.clock.Now().Sub(entry.at) < positionTTL {
		cfg := entry.cfg
		c.posMu.Unlock()
		return cfg, nil
	}
	c.posMu.Unlock()

	var cfg protocol.SignPositionConfig
	err := c.withSession(ctx, accountID, cred, func(ctx context.Context, client *http.Client, csrf string) error {
		req, err := newRequest(ctx, http.MethodGet,
			withQuery(c.endpoints.SignPosition, url.Values{"CSRF": {csrf}}), nil)
		if err != nil {
			return err
		}
		setAppHeaders(req)
		addCSRF(req, csrf)
		resp, body, err := c.do(client, req)
		if err != nil {
			return err
		}
		// WAF 判定先于状态码：拦截页可能带 4xx，按状态归 transient 只会重发同一请求。
		if wafBlocked(string(body)) {
			return blockedOutcome(string(body))
		}
		if resp.StatusCode < 200 || resp.StatusCode >= 300 {
			return outcome(contract.OutcomeTransient,
				fmt.Sprintf("signPosition status %d", resp.StatusCode))
		}
		envelope, err := protocol.ParseAPIEnvelope(body)
		if err != nil {
			return parseOutcome(err)
		}
		if !protocol.CodeIsOK(envelope.Code) {
			return outcome(contract.OutcomeUnknown,
				fmt.Sprintf("signPosition code %v", envelope.Code))
		}
		data, ok := envelope.Data.(map[string]any)
		if !ok {
			return outcome(contract.OutcomeUnknown, "signPosition data is not an object")
		}
		parsed, err := protocol.ParseSignPosition(data)
		if err != nil {
			return parseOutcome(err)
		}
		cfg = parsed
		c.posMu.Lock()
		c.positions[accountID] = positionEntry{at: c.clock.Now(), cfg: parsed}
		c.posMu.Unlock()
		return nil
	})
	return cfg, err
}

// DeviceState 读回服务端登记的设备。
func (c *Client) DeviceState(ctx context.Context, accountID string, cred contract.Credential) (protocol.DeviceState, error) {
	var state protocol.DeviceState
	err := c.withSession(ctx, accountID, cred, func(ctx context.Context, client *http.Client, csrf string) error {
		req, err := newRequest(ctx, http.MethodGet,
			withQuery(c.endpoints.DeviceState, url.Values{"CSRF": {csrf}}), nil)
		if err != nil {
			return err
		}
		setAppHeaders(req)
		addCSRF(req, csrf)
		resp, body, err := c.do(client, req)
		if err != nil {
			return err
		}
		if wafBlocked(string(body)) {
			return blockedOutcome(string(body))
		}
		if resp.StatusCode < 200 || resp.StatusCode >= 300 {
			return outcome(contract.OutcomeTransient,
				fmt.Sprintf("device status %d", resp.StatusCode))
		}
		envelope, err := protocol.ParseAPIEnvelope(body)
		if err != nil {
			return parseOutcome(err)
		}
		if !protocol.CodeIsOK(envelope.Code) {
			return outcome(contract.OutcomeUnknown,
				fmt.Sprintf("device code %v", envelope.Code))
		}
		parsed, err := protocol.ParseDeviceState(envelope.Data)
		if err != nil {
			return parseOutcome(err)
		}
		state = parsed
		return nil
	})
	return state, err
}

// SubmitSignIn 提交一次签到。
// **不自动重试**：POST 非幂等；999 由调用方按 AttemptID 去重。
func (c *Client) SubmitSignIn(ctx context.Context, accountID string, cred contract.Credential, body string) (protocol.Envelope, error) {
	var envelope protocol.Envelope
	err := c.withSessionNoRetry(ctx, accountID, cred, func(ctx context.Context, client *http.Client, csrf string) error {
		req, err := newRequest(ctx, http.MethodPost,
			withQuery(c.endpoints.SignIn, url.Values{"CSRF": {csrf}}), strings.NewReader(body))
		if err != nil {
			return err
		}
		setAppHeaders(req)
		addCSRF(req, csrf)
		req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
		resp, data, err := c.do(client, req)
		if err != nil {
			return err
		}
		if wafBlocked(string(data)) {
			return blockedOutcome(string(data))
		}
		if resp.StatusCode < 200 || resp.StatusCode >= 300 {
			return outcome(contract.OutcomeTransient,
				fmt.Sprintf("signIn status %d", resp.StatusCode))
		}
		parsed, err := protocol.ParseAPIEnvelope(data)
		if err != nil {
			return parseOutcome(err)
		}
		envelope = parsed
		return nil
	})
	c.clearPositions(accountID)
	return envelope, err
}
