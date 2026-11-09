package settings

// 测试推送用例：整镜像 SendTestMail 的形态（settings.go）。
//
// # 口径
// 目标只取配置的 webhook 出口（装配时构造的 core/webhook.Sender），
// **不接受**调用方指定地址；出口未配置回 conflict（与邮件未配置同状态、
// 文案不同）。同步投递：返回 nil 表示出口受理了推送（serverchan 的
// code==0，custom 的 2xx 且无业务码拒绝），不代表手机已收到。
// 失败回 upstream_unavailable，错误文本只含固定文案，**不含**对端应答、
// 状态码、URL 或 SendKey。
// 测试推送**跳过**普通 webhook 投递的同题节流与当日额度（force 语义）；
// 但同一 master 的测试推送之间有 60s 冷却（process-local，重启清零），
// 冷却内重复请求回 rate_limited，details 带 retry_after_seconds。
// 每次真正外呼都写 settings.webhook_test 审计，成功与失败同动作；
// detail 只含 result 与投递粗分类。审计写失败时回 internal：
// 审计完整性优先于本轮响应。

import (
	"context"
	"math"

	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/logfield"
	corewebhook "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/webhook"
)

// WebhookTestCooldown 是同一管理员两次测试推送之间的最小间隔。
// 与测试邮件同值：两者都是「确认配置能通」的人工动作，没有更高频的理由。
const WebhookTestCooldown = TestMailCooldown

const (
	// webhookTestTitle 是测试推送的标题。
	webhookTestTitle = "消息推送测试"
	// webhookTestBody 是测试推送的正文。
	webhookTestBody = "这是一条来自 Yiban Auto Sign 的测试推送，收到即表示 webhook 出口配置正常。"
	// webhookTestCooldownKeyPrefix 让测试推送冷却与测试邮件冷却共用桶实现时
	// 仍落在独立的键前缀上：同一管理员 id 在不同限流面互不占用额度。
	webhookTestCooldownKeyPrefix = "admin-webhook-test:"
)

// SendTestWebhook 向配置的 webhook 出口发一条测试推送。
//
// 出口未配置回 conflict；冷却内重复请求回 rate_limited（带
// retry_after_seconds）；发送失败回 upstream_unavailable，错误文本只含
// 固定文案。每次外呼都写 settings.webhook_test 审计。
func (s *Service) SendTestWebhook(ctx context.Context, user coreauth.User) error {
	if s.deps.Webhook == nil {
		return coreerrs.New(coreerrs.CodeConflict, "webhook delivery is disabled")
	}
	if s.deps.WebhookCooldown != nil {
		ok, wait := s.deps.WebhookCooldown.Allow(webhookTestCooldownKeyPrefix + user.ID)
		if !ok {
			seconds := int(math.Ceil(wait.Seconds()))
			if seconds < 1 {
				seconds = 1
			}
			return &coreerrs.Error{
				Code:    coreerrs.CodeRateLimited,
				Message: "a test webhook was attempted recently; retry later",
				Details: map[string]any{"retry_after_seconds": seconds},
			}
		}
	}
	push := corewebhook.Push{Title: webhookTestTitle, Content: webhookTestBody}
	if err := s.deps.Webhook.Send(ctx, push); err != nil {
		kind, retryable := corewebhook.Classify(err)
		// 日志只记分类与可重试标志：URL 与 SendKey 在错误链里，
		// 但 Error() 不输出它们，这里也不展开底层错误文本。
		s.log.Warn("test webhook delivery failed",
			logfield.KeyEvent, "webhook_test.delivery_failed",
			logfield.KeyKind, string(kind),
			logfield.KeyRetryable, retryable)
		if auditErr := s.recordTestWebhookAudit(ctx, user, map[string]any{
			"result":    "error",
			"kind":      string(kind),
			"retryable": retryable,
		}); auditErr != nil {
			s.log.Error("test webhook audit failed",
				logfield.KeyEvent, "webhook_test.audit_failed",
				logfield.KeyReason, "audit_write_failed")
			return coreerrs.New(coreerrs.CodeInternal, "internal error")
		}
		return coreerrs.New(coreerrs.CodeUpstream, "test webhook delivery failed")
	}
	if auditErr := s.recordTestWebhookAudit(ctx, user, map[string]any{"result": "sent"}); auditErr != nil {
		s.log.Error("test webhook audit failed",
			logfield.KeyEvent, "webhook_test.audit_failed",
			logfield.KeyReason, "audit_write_failed")
		return coreerrs.New(coreerrs.CodeInternal, "internal error")
	}
	s.log.Info("test webhook delivered",
		logfield.KeyEvent, "webhook_test.delivered")
	return nil
}

// recordTestWebhookAudit 写一条测试推送审计。
// detail 调用方只传结果词与粗分类；写入口径由 core/audit 的白名单兜底。
func (s *Service) recordTestWebhookAudit(ctx context.Context, user coreauth.User, detail map[string]any) error {
	actor := appaudit.NewActor(user)
	return s.deps.Tx.InTx(ctx, func(ctx context.Context) error {
		_, err := s.deps.Audit.Append(ctx, coreaudit.Entry{
			TS:          s.deps.Clock.Now().UTC(),
			ActorUserID: actor.UserID,
			ActorRole:   actor.Role,
			ActorLabel:  actor.Label,
			Action:      coreaudit.ActionSettingsWebhookTest,
			TargetType:  "settings",
			TargetID:    "webhook",
			Detail:      detail,
		})
		return err
	})
}
