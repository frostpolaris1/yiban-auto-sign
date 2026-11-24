package auth

// 公开注册的人机验证（Cloudflare Turnstile）。
//
// # 功能
// 校验注册请求携带的令牌，并按档位决定拦不拦。
// 本文件**不**发 HTTP、**不**解析响应格式：端点由 adapters/turnstile
// 实现 HumanVerifier，用例只消费「过没过」与「问没问出来」两件事。
//
// # 归属
// app 层用例。端口在本包声明（消费者侧），实现由组合根注入。
//
// # 口径
// 三档：human 为 nil 即 off；humanEnforce=false 即 log（只记不拦，
// 用于灰测通过率）；true 即 on。on 档下令牌缺失或无效回 400，
// 验证服务不可达回 502（CodeUpstream），与「用户没通过」严格区分。
// 日志只记 outcome 与 error-codes，**不**记令牌、邮箱或 IP。
import (
	"context"
	"strings"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/logfield"
)

// HumanVerifier 是验证服务端口。第二个返回值是服务给出的拒绝原因，
// 只供日志；第三个返回值表示「没问出结果」。
type HumanVerifier interface {
	Verify(ctx context.Context, token, remoteIP string) (bool, []string, error)
}

// verifyHuman 按档位校验注册令牌。
// off 档不调用端口；log 档任何失败都只记日志并放行。
func (s *Service) verifyHuman(ctx context.Context, token, remoteIP string) error {
	if s.human == nil {
		return nil
	}
	token = strings.TrimSpace(token)
	if token == "" {
		s.log.Warn("auth: registration without a human verification token",
			logfield.KeyReason, "turnstile_token_missing")
		if s.humanEnforce {
			return errs.New(errs.CodeHumanVerification, "human verification is required")
		}
		return nil
	}
	ok, codes, err := s.human.Verify(ctx, token, remoteIP)
	if err != nil {
		// 错误文本由适配器保证不含令牌与私钥，带上原因才排得动线上故障。
		s.log.Warn("auth: human verification is unavailable",
			logfield.KeyReason, "turnstile_unavailable",
			logfield.KeyError, err.Error())
		if s.humanEnforce {
			return errs.New(errs.CodeUpstream, "human verification is temporarily unavailable")
		}
		return nil
	}
	if ok {
		return nil
	}
	reason := strings.Join(codes, ",")
	if s.humanEnforce {
		s.log.Warn("auth: human verification rejected",
			logfield.KeyReason, "turnstile_rejected",
			logfield.KeyError, reason)
		return errs.New(errs.CodeHumanVerification, "human verification failed")
	}
	s.log.Warn("auth: human verification rejected; registration continues in log mode",
		logfield.KeyReason, "turnstile_rejected",
		logfield.KeyError, reason)
	return nil
}
