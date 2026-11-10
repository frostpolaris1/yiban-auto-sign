// Package system 内的 SMTP 设置端点（批 smtp4）。
//
// 功能：
// GET /api/admin/mail-settings（master/admin 只读六键生效值与来源）、
// PUT /api/admin/mail-settings（master 部分补丁写回六键，二次口令复核）。
// 本包**不**写 SQL、**不**做域校验（域在用例层单源）、**不**回显敏感值。
//
// 归属：entry/http 入口层。用例来自 app/settings；
// 读门 master/admin（与名册一致），写门 master（与调度/配额写回一致）。
//
// 口径：部分补丁语义与 PUT /api/admin/schedule-settings 同形：
// 缺字段=不动该键、显式 null=删覆盖行回 env、值=upsert；
// 未知字段在这里显式 400，不被解码器静默忽略。
// **任一字段的变更都要求 current_password**：SMTP 凭据是出口配置，
// 旧线 risk_always 口径；缺失 400、错误 403，均零写入零审计。
// 响应只含生效值与来源：pass 只给 set，from/user 只给掩码，
// host/port/tls_mode 给真实值。口令明文与密文都不进响应。
package system

import (
	"context"
	"encoding/json"
	"net/http"

	"github.com/gin-gonic/gin"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appsettings "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/settings"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/respond"
)

// MailSettings 是 SMTP 设置读写端口（smtp4）。
// 未注入时两个端点回 501：写面缺失必须显性。
type MailSettings interface {
	MailSettings(ctx context.Context, scope corescope.Scope) (appsettings.MailReport, error)
	SetMail(ctx context.Context, scope corescope.Scope, actor appaudit.Actor, changes []appsettings.MailChange) error
}

// WithMailSettings 注入 SMTP 设置用例。
// 未注入时端点回 501：生产装配必须注入，缺失必须显性。
func (h *Handler) WithMailSettings(m MailSettings) *Handler {
	h.mailSettings = m
	return h
}

// AdminMailSettings 实现 GET /api/admin/mail-settings。master/admin 可读。
func (h *Handler) AdminMailSettings(c *gin.Context) {
	session, ok := requester(c)
	if !ok {
		return
	}
	if !h.allowAdmin(c, session) {
		return
	}
	if h.mailSettings == nil {
		respond.NotImplemented(c.Writer, c.Request)
		return
	}
	report, err := h.mailSettings.MailSettings(c.Request.Context(), scopeOf(session.User))
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusOK, mailReportResponse(report))
}

// AdminMailSettingsSet 实现 PUT /api/admin/mail-settings。仅 master。
//
// 顺序：会话 → 请求体 → 角色门 → 口令存在性/字段解析（400）→
// 二次口令（403/429）→ 用例（写与审计同事务）。
// 越权与缺口令都在写之前：被拒路径零写入零审计。
func (h *Handler) AdminMailSettingsSet(c *gin.Context, _ oapi.AdminMailSettingsSetParams) {
	session, ok := requester(c)
	if !ok {
		return
	}
	if session.User.Role != coreauth.RoleMaster {
		fail(c, errs.New(errs.CodeForbidden, "master role is required"))
		return
	}
	if h.mailSettings == nil {
		respond.NotImplemented(c.Writer, c.Request)
		return
	}
	var body map[string]json.RawMessage
	if err := json.NewDecoder(c.Request.Body).Decode(&body); err != nil {
		respond.BindError(c.Writer, c.Request, err)
		return
	}
	password, err := mailCurrentPassword(body)
	if err != nil {
		fail(c, err)
		return
	}
	delete(body, "current_password")
	changes, err := mailChangesFromJSON(body)
	if err != nil {
		fail(c, err)
		return
	}
	if !h.reauthenticate(c, session.User.ID, password) {
		return
	}
	if err := h.mailSettings.SetMail(c.Request.Context(), scopeOf(session.User),
		appaudit.NewActor(session.User), changes); err != nil {
		fail(c, err)
		return
	}
	report, err := h.mailSettings.MailSettings(c.Request.Context(), scopeOf(session.User))
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusOK, mailReportResponse(report))
}

// mailCurrentPassword 从原始 JSON 体取二次口令。
// 缺失、null、非字符串与空串都回 400：任一字段的变更都要求复核。
func mailCurrentPassword(body map[string]json.RawMessage) (string, error) {
	raw, ok := body["current_password"]
	if !ok || string(raw) == "null" {
		return "", errs.New(errs.CodeValidation, "current_password is required")
	}
	var password string
	if err := json.Unmarshal(raw, &password); err != nil {
		return "", errs.New(errs.CodeValidation, "current_password requires a string")
	}
	if password == "" {
		return "", errs.New(errs.CodeValidation, "current_password is required")
	}
	return password, nil
}

// mailBodyKeys 把 PUT 请求体字段名映射到 settings 表键名与值类型。
// 表外字段一律未知（400）；current_password 在调用前已摘除。
var mailBodyKeys = map[string]struct {
	key  string
	kind string
}{
	"host":     {key: appsettings.KeyMailSMTPHost, kind: "string"},
	"port":     {key: appsettings.KeyMailSMTPPort, kind: "int"},
	"tls_mode": {key: appsettings.KeyMailTLSMode, kind: "string"},
	"from":     {key: appsettings.KeyMailFrom, kind: "string"},
	"user":     {key: appsettings.KeyMailUser, kind: "string"},
	"pass":     {key: appsettings.KeyMailPass, kind: "string"},
}

// mailChangesFromJSON 把原始 JSON 体折成写意图。
// 未知字段、JSON 类型错、空更新都在这里 400；域校验与口令加密在用例层。
func mailChangesFromJSON(body map[string]json.RawMessage) ([]appsettings.MailChange, error) {
	changes := make([]appsettings.MailChange, 0, len(body))
	for field, raw := range body {
		spec, known := mailBodyKeys[field]
		if !known {
			return nil, errs.New(errs.CodeValidation, "unknown mail setting key "+field)
		}
		if string(raw) == "null" {
			changes = append(changes, appsettings.MailChange{Key: spec.key, Op: appsettings.OpClear})
			continue
		}
		if spec.kind == "int" {
			var value int
			if err := json.Unmarshal(raw, &value); err != nil {
				return nil, errs.New(errs.CodeValidation, field+" requires an integer")
			}
			changes = append(changes, appsettings.MailChange{Key: spec.key, Op: appsettings.OpSet, Value: value})
			continue
		}
		var value string
		if err := json.Unmarshal(raw, &value); err != nil {
			return nil, errs.New(errs.CodeValidation, field+" requires a string literal")
		}
		changes = append(changes, appsettings.MailChange{Key: spec.key, Op: appsettings.OpSet, Value: value})
	}
	if len(changes) == 0 {
		return nil, errs.New(errs.CodeValidation, "mail settings update is empty")
	}
	return changes, nil
}

// mailReportResponse 把用例报告转成传输形状。
// pass 无 masked 字段；from/user 的 masked 是固定 "****"。
func mailReportResponse(report appsettings.MailReport) oapi.MailSettingsReport {
	return oapi.MailSettingsReport{
		Host: oapi.MailSettingValue{
			Value:  report.Host.Value,
			Source: oapi.MailSettingSource(report.Host.Source),
		},
		Port: oapi.MailPortSetting{
			Value:  report.Port.Value,
			Source: oapi.MailSettingSource(report.Port.Source),
		},
		TlsMode: oapi.MailSettingValue{
			Value:  report.TLSMode.Value,
			Source: oapi.MailSettingSource(report.TLSMode.Source),
		},
		From: oapi.MailSensitiveSetting{
			Set:    report.From.Set,
			Masked: report.From.Masked,
			Source: oapi.MailSettingSource(report.From.Source),
		},
		User: oapi.MailSensitiveSetting{
			Set:    report.User.Set,
			Masked: report.User.Masked,
			Source: oapi.MailSettingSource(report.User.Source),
		},
		Pass: oapi.MailPasswordSetting{
			Set:    report.Pass.Set,
			Source: oapi.MailSettingSource(report.Pass.Source),
		},
	}
}
