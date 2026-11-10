// Package system 内的配额设置端点（批 r4r）。
//
// 功能：
// GET /api/admin/quota-settings（master/admin 只读两键生效值与来源）、
// PUT /api/admin/quota-settings（master 部分补丁写回两键）。
// 本包**不**写 SQL、**不**做域校验（域在用例层单源）。
//
// 归属：entry/http 入口层。用例来自 app/settings；
// 读门 master/admin（与名册一致），写门 master（与调度写回一致）。
//
// 口径：部分补丁语义与 PUT /api/admin/schedule-settings 同形：
// 缺字段=不动该键、显式 null=删覆盖行回缺省、值=upsert；
// 未知字段在这里显式 400，不被解码器静默忽略。
// 响应只含两键的生效值与来源，不回显覆盖行原文。
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

// QuotaSettings 是配额设置读写端口（r4r）。
// 未注入时两个端点回 501：写面缺失必须显性。
type QuotaSettings interface {
	QuotaSettings(ctx context.Context, scope corescope.Scope) (appsettings.QuotaReport, error)
	SetQuota(ctx context.Context, scope corescope.Scope, actor appaudit.Actor, changes []appsettings.QuotaChange) error
}

// WithQuotaSettings 注入配额设置用例。
// 未注入时端点回 501：生产装配必须注入，缺失必须显性。
func (h *Handler) WithQuotaSettings(q QuotaSettings) *Handler {
	h.quotaSettings = q
	return h
}

// AdminQuotaSettings 实现 GET /api/admin/quota-settings。master/admin 可读。
func (h *Handler) AdminQuotaSettings(c *gin.Context) {
	session, ok := requester(c)
	if !ok {
		return
	}
	if !h.allowAdmin(c, session) {
		return
	}
	if h.quotaSettings == nil {
		respond.NotImplemented(c.Writer, c.Request)
		return
	}
	report, err := h.quotaSettings.QuotaSettings(c.Request.Context(), scopeOf(session.User))
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusOK, quotaReportResponse(report))
}

// AdminQuotaSettingsSet 实现 PUT /api/admin/quota-settings。仅 master。
//
// 部分补丁语义：缺字段=不动该键；显式 null=删覆盖行回缺省；值=upsert。
// 键存在性只能在解码原始 JSON 时判定（与 AdminScheduleSettingsSet 同因），
// 未知字段在这里显式拒绝（400），不被解码器静默忽略。
// 域校验与写入都在用例层：任何拒绝零写入零审计。
func (h *Handler) AdminQuotaSettingsSet(c *gin.Context, _ oapi.AdminQuotaSettingsSetParams) {
	session, ok := requester(c)
	if !ok {
		return
	}
	if session.User.Role != coreauth.RoleMaster {
		fail(c, errs.New(errs.CodeForbidden, "master role is required"))
		return
	}
	if h.quotaSettings == nil {
		respond.NotImplemented(c.Writer, c.Request)
		return
	}
	var body map[string]json.RawMessage
	if err := json.NewDecoder(c.Request.Body).Decode(&body); err != nil {
		respond.BindError(c.Writer, c.Request, err)
		return
	}
	changes, err := quotaChangesFromJSON(body)
	if err != nil {
		fail(c, err)
		return
	}
	if err := h.quotaSettings.SetQuota(c.Request.Context(), scopeOf(session.User),
		appaudit.NewActor(session.User), changes); err != nil {
		fail(c, err)
		return
	}
	report, err := h.quotaSettings.QuotaSettings(c.Request.Context(), scopeOf(session.User))
	if err != nil {
		fail(c, err)
		return
	}
	respond.JSON(c.Writer, http.StatusOK, quotaReportResponse(report))
}

// quotaBodyKeys 把 PUT 请求体字段名映射到 settings 表键名。
// 表外字段一律未知（400）。
var quotaBodyKeys = map[string]string{
	"max_users":    appsettings.KeyQuotaMaxUsers,
	"max_accounts": appsettings.KeyQuotaMaxAccounts,
}

// quotaChangesFromJSON 把原始 JSON 体折成写意图。
// 未知字段、JSON 类型错、空更新都在这里 400；域校验在用例层。
func quotaChangesFromJSON(body map[string]json.RawMessage) ([]appsettings.QuotaChange, error) {
	changes := make([]appsettings.QuotaChange, 0, len(body))
	for field, raw := range body {
		key, known := quotaBodyKeys[field]
		if !known {
			return nil, errs.New(errs.CodeValidation, "unknown quota setting key "+field)
		}
		if string(raw) == "null" {
			changes = append(changes, appsettings.QuotaChange{Key: key, Op: appsettings.OpClear})
			continue
		}
		var value int
		if err := json.Unmarshal(raw, &value); err != nil {
			return nil, errs.New(errs.CodeValidation, field+" requires an integer")
		}
		changes = append(changes, appsettings.QuotaChange{Key: key, Op: appsettings.OpSet, Value: value})
	}
	if len(changes) == 0 {
		return nil, errs.New(errs.CodeValidation, "quota settings update is empty")
	}
	return changes, nil
}

// quotaReportResponse 把用例报告转成传输形状。
func quotaReportResponse(report appsettings.QuotaReport) oapi.QuotaReport {
	return oapi.QuotaReport{
		MaxUsers: oapi.QuotaSetting{
			Value:  report.MaxUsers.Value,
			Source: oapi.QuotaSettingSource(report.MaxUsers.Source),
		},
		MaxAccounts: oapi.QuotaSetting{
			Value:  report.MaxAccounts.Value,
			Source: oapi.QuotaSettingSource(report.MaxAccounts.Source),
		},
	}
}
