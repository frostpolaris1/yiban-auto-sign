package audit

// 本文件定义审计主体。**它不含凭据，也不含明文邮箱**。
//
// 归属：app 层共用取值。入口层从会话构造一次，
// 三个用例域原样透传，避免各域复写掩码规则。

import (
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mask"
)

// Actor 是写审计行时的操作者。
// 类型定义在 core/audit（适配器也要接收主体），本包是它的别名。
// Label 必须是掩码邮箱；掩码失败时取 unknown，**绝不回退为原地址**。
type Actor = coreaudit.Actor

// NewActor 从会话主体构造审计主体。
// 邮箱经 mask.MaskEmail 处理；结果与原值相同说明形状不符，
// 此时记 unknown（审计列不得出现完整邮箱）。
func NewActor(user coreauth.User) Actor {
	label := mask.MaskEmail(user.Email)
	if label == "" || label == user.Email {
		label = "unknown"
	}
	return Actor{UserID: user.ID, Role: user.Role, Label: label}
}
