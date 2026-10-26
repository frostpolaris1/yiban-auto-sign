package yiban

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/protocol"
)

// Provider 是易班平台的 contract.Provider 实现。
type Provider struct {
	client *Client
	cache  *resultCache
}

var _ contract.Provider = (*Provider)(nil)

// NewProvider 用已配置的客户端构造提供方。
func NewProvider(client *Client) *Provider {
	return &Provider{client: client, cache: newResultCache()}
}

func (p *Provider) ID() string { return "yiban" }

// Capabilities 声明本实现的能力。
// 图片上传属 P5，故 ImageRequired 为假；验证码只检测不求解。
func (p *Provider) Capabilities() contract.Capabilities {
	return contract.Capabilities{
		WindowDiscovery: true,
		TargetDiscovery: true,
		DeviceBinding:   true,
		FixedLocation:   true,
	}
}

// Verify 执行一次登录并返回非秘密身份。
func (p *Provider) Verify(ctx context.Context, req contract.VerifyRequest) (contract.Profile, error) {
	return p.client.Verify(ctx, req.Account.ID, req.Credential)
}

// DiscoverWindow 读取签到时间窗。
// 平台给出「无需签到 / 已签到 / 补签中」时返回 OutcomeError，
// 由宿主映射成 skipped 或 success。
func (p *Provider) DiscoverWindow(ctx context.Context, req contract.DiscoverRequest) (contract.Window, error) {
	cfg, err := p.client.SignPosition(ctx, req.Account.ID, req.Credential)
	if err != nil {
		return contract.Window{}, err
	}
	if outcome, ok := stateOutcome(cfg.State); ok {
		return contract.Window{}, &contract.OutcomeError{Outcome: outcome, Message: stateMessage(cfg.State)}
	}
	if cfg.TimeWindow == nil {
		return contract.Window{}, &contract.OutcomeError{
			Outcome: contract.OutcomeNoTarget, Message: "sign window is missing"}
	}
	return contract.Window{
		Start:  time.Unix(int64(cfg.TimeWindow.StartTime), 0),
		End:    time.Unix(int64(cfg.TimeWindow.EndTime), 0),
		Source: contract.WindowPlatform,
	}, nil
}

// DiscoverTargets 读取候选签到点。
// IsNeedPhoto 语义未定，不据此置 RequiresPhoto；原值进 Raw。
func (p *Provider) DiscoverTargets(ctx context.Context, req contract.DiscoverRequest) ([]contract.Target, error) {
	cfg, err := p.client.SignPosition(ctx, req.Account.ID, req.Credential)
	if err != nil {
		return nil, err
	}
	if outcome, ok := stateOutcome(cfg.State); ok {
		return nil, &contract.OutcomeError{Outcome: outcome, Message: stateMessage(cfg.State)}
	}
	if len(cfg.Positions) == 0 {
		return nil, &contract.OutcomeError{
			Outcome: contract.OutcomeNoTarget, Message: "signPosition returned no positions"}
	}
	targets := make([]contract.Target, 0, len(cfg.Positions))
	for _, position := range cfg.Positions {
		polygon := make([]contract.Point, 0, len(position.Points))
		for _, point := range position.Points {
			polygon = append(polygon, contract.Point{Lng: point.Lng, Lat: point.Lat})
		}
		radius := 0
		if position.RangeM != nil && *position.RangeM > 0 {
			radius = *position.RangeM
		}
		attachment := ""
		if cfg.AttachmentFileName != nil {
			attachment = *cfg.AttachmentFileName
		}
		raw := map[string]any{}
		if cfg.IsNeedPhoto != nil {
			raw["is_need_photo"] = *cfg.IsNeedPhoto
		}
		if position.MapType != nil {
			raw["map_type"] = *position.MapType
		}
		targets = append(targets, contract.Target{
			ID:             position.ID,
			Title:          position.Title,
			Address:        position.Address,
			Polygon:        polygon,
			Center:         contract.Point{Lng: position.LngLat.Lng, Lat: position.LngLat.Lat},
			RadiusM:        radius,
			RequiresPhoto:  false,
			AttachmentName: attachment,
			Raw:            raw,
		})
	}
	return targets, nil
}

// Sign 执行一次签到尝试。
// 同一 AttemptID 的结果会缓存；并发的同键调用串行，只提交一次。
func (p *Provider) Sign(ctx context.Context, req contract.SignRequest) (contract.Result, error) {
	if req.AttemptID == "" {
		return p.sign(ctx, req)
	}
	key := req.Account.ID + "\x00" + req.AttemptID
	lock := p.cache.lockFor(key)
	lock.Lock()
	defer lock.Unlock()
	if cached, ok := p.cache.get(key); ok {
		return cached, nil
	}
	result, err := p.sign(ctx, req)
	if err != nil {
		return contract.Result{}, err
	}
	p.cache.put(key, result)
	return result, nil
}

func (p *Provider) sign(ctx context.Context, req contract.SignRequest) (contract.Result, error) {
	cfg, err := p.client.SignPosition(ctx, req.Account.ID, req.Credential)
	if err != nil {
		return p.resultForError(req.Account.ID, err, "signPosition")
	}
	if outcome, ok := stateOutcome(cfg.State); ok {
		return resultFor(outcome, stateMessage(cfg.State)), nil
	}
	if len(cfg.Positions) == 0 {
		return resultFor(contract.OutcomeNoTarget, "signPosition returned no positions"), nil
	}

	deviceCode := req.Credential.DeviceCode
	deviceModel := req.Credential.PhoneModel
	warning := ""
	if deviceCode == "" {
		state, err := p.client.DeviceState(ctx, req.Account.ID, req.Credential)
		if err != nil {
			return p.resultForError(req.Account.ID, err, "device")
		}
		deviceCode = state.Code
		if deviceModel == "" {
			deviceModel = state.PhoneModel
		}
		if deviceCode == "" {
			return resultFor(contract.OutcomeDeviceUnbound,
				"no registered device; authorize this device in the Yiban app"), nil
		}
		if !state.Authorized {
			warning = "device is registered but not authorized; the platform may reject the sign"
		}
	}

	point := protocol.Point{Lng: req.Location.Lng, Lat: req.Location.Lat}
	if point.Lng == 0 && point.Lat == 0 {
		point = protocol.Point{Lng: req.Target.Center.Lng, Lat: req.Target.Center.Lat}
	}
	if point.Lng == 0 && point.Lat == 0 {
		return resultFor(contract.OutcomeInvalidTarget, "no location for the sign"), nil
	}

	// P5-D 待办（P5-C 登记，行为现状不改）：
	//  1. req.Image != nil 时本实现不上传图片，也不伪造请求——图片被静默
	//     丢弃后提交。P5-D 接 uploadUri→上传→回填；接上之前，宿主侧的
	//     photo_missing 短路保证只有带图目标会走到这里带图提交。
	//  2. AttachmentFileName 语义疑点：现填 Target.AttachmentName，但参考
	//     作者公开博客与 F2 抓包显示该字段的正确语义是「上传回执返回的
	//     文件名」。P5-D 接上传时必须改成回执值，不得沿用本行的猜测值。
	//  3. req.Location.Reason 现在只到宿主为止，本实现不把它写进 SignInfo；
	//     P5-D 接上传时一并送平台（协议形状 protocol/forms.go 已支持）。
	// 以实拍为准（AGENTS §26：平台协议形状不采信第三方实现）。
	body := protocol.BuildSignInBody(protocol.SignInBody{
		LngLat:             point,
		Address:            req.Target.Address,
		OutState:           "1",
		Code:               deviceCode,
		PhoneModel:         deviceModel,
		AttachmentFileName: req.Target.AttachmentName,
	})
	envelope, err := p.client.SubmitSignIn(ctx, req.Account.ID, req.Credential, body)
	if err != nil {
		return p.resultForError(req.Account.ID, err, "signIn")
	}
	return p.signResult(envelope, warning), nil
}

// resultForError 把会话/结论/原生错误分成三类。
// 取消与网络错误保持原生，交宿主处理。
func (p *Provider) resultForError(accountID string, err error, stage string) (contract.Result, error) {
	var expired *protocol.SessionExpired
	if errors.As(err, &expired) {
		p.client.clearSession(accountID)
		return transientResult(stage+" session expired", true), nil
	}
	var outcomeErr *contract.OutcomeError
	if errors.As(err, &outcomeErr) && outcomeErr != nil {
		return resultFor(outcomeErr.Outcome, outcomeErr.Message), nil
	}
	return contract.Result{}, err
}

func (p *Provider) signResult(envelope protocol.Envelope, warning string) contract.Result {
	if !protocol.CodeIsOK(envelope.Code) {
		message := safeMessage(envelope.Msg)
		if strings.Contains(message, "授权设备") {
			return resultFor(contract.OutcomeDeviceUnbound, message)
		}
		return resultFor(contract.OutcomeRejected, message)
	}
	receipt, ok := receiptOf(envelope.Data)
	if !ok {
		return resultFor(contract.OutcomeUnknown, "signIn returned no receipt")
	}
	now := p.client.clock.Now()
	return contract.Result{
		Outcome:  contract.OutcomeSuccess,
		Evidence: contract.Evidence{Receipt: receipt, SignedAt: &now},
		Message:  safeMessage(warning),
	}
}

// receiptOf 只认两种成功回执：布尔 true 与带正整数 Id 的对象。
// 数值 Id 必须 > 0：0、负数、浮点都算无回执（参考实现的 R3-002 判据）。
func receiptOf(data any) (string, bool) {
	switch value := data.(type) {
	case bool:
		if value {
			return "true", true
		}
	case map[string]any:
		switch id := value["Id"].(type) {
		case string:
			if strings.TrimSpace(id) != "" {
				return id, true
			}
		case json.Number:
			if n, err := id.Int64(); err == nil && n > 0 {
				return id.String(), true
			}
		}
	}
	return "", false
}

func stateOutcome(state *int) (contract.Outcome, bool) {
	if state == nil {
		return "", false
	}
	switch *state {
	case 2:
		return contract.OutcomeNoTarget, true
	case 3, 4:
		return contract.OutcomeAlreadyDone, true
	case 5:
		return contract.OutcomePendingUpstream, true
	}
	return "", false
}

func stateMessage(state *int) string {
	if state == nil {
		return ""
	}
	switch *state {
	case 2:
		return "今日无需签到"
	case 3:
		return "今日已签到"
	case 4:
		return "今日已更改（班委补签）"
	case 5:
		return "补签中"
	}
	return fmt.Sprintf("platform state %d", *state)
}

func resultFor(outcome contract.Outcome, message string) contract.Result {
	return contract.Result{
		Outcome: outcome,
		Message: safeMessage(message),
		Retry:   retryFor(outcome),
	}
}

func transientResult(message string, clearSession bool) contract.Result {
	return contract.Result{
		Outcome: contract.OutcomeTransient,
		Message: safeMessage(message),
		Retry: contract.RetryAdvice{
			Retryable:    true,
			MaxAttempts:  3,
			ClearSession: clearSession,
		},
	}
}

func retryFor(outcome contract.Outcome) contract.RetryAdvice {
	if outcome == contract.OutcomeTransient {
		return contract.RetryAdvice{Retryable: true, MaxAttempts: 3}
	}
	return contract.RetryAdvice{}
}
