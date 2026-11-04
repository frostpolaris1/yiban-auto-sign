package audit

// 本文件定义审计主体。**它不含凭据，也不含明文邮箱**。
//
// 归属：app 层共用取值。入口层从会话构造一次，
// 三个用例域原样透传，避免各域复写掩码规则。

import (
	"strings"

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

// FilterLabel 把列表筛选的 actor 原值变成可匹配的标签。
//
// 完整邮箱先归一为小写再掩码：写入口径存储的邮箱已由注册归一而小写，
// 故大小写不同的查询也能命中。已是掩码标签（含 ***）或非邮箱形状
// （如 unknown）原样返回——它们本身就是存储里的标签值。
// 空串表示不筛选。
func FilterLabel(raw string) string {
	value := strings.ToLower(strings.TrimSpace(raw))
	if value == "" {
		return ""
	}
	masked := mask.MaskEmail(value)
	if masked == value {
		return value
	}
	return masked
}
