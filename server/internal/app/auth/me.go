// 会话主体用例。

package auth

import (
	"context"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
)

// Me 返回请求上下文携带的会话。
//
// 入口中间件已验证令牌，故本函数**不**再查库。
// 缺会话返回 unauthorized，不返回 not_found：未登录不是资源缺失。
func Me(ctx context.Context) (coreauth.Session, error) {
	sess, ok := coreauth.FromContext(ctx)
	if !ok {
		return coreauth.Session{}, errs.New(errs.CodeUnauthorized, "authentication required")
	}
	return sess, nil
}
