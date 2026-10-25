// 邮箱的归一与形状校验。
//
// # 功能
// 提供 NormalizeEmail 与 ValidEmail。两者是全仓唯一实现。
// 本文件**不**查库，**不**判唯一性，**不**发邮件。
//
// # 归属
// core 层纯领域包。app/auth 的登录与忘记口令、app/user 的建号
// 都调用本文件。**不得**再写第二份口径。
//
// # 复用
// NormalizeEmail、ValidEmail。
//
// # 口径
// 归一＝去首尾空白后转小写，与 ck_users_email_lower 同口径。
// 形状＝3 至 254 字符、恰好一个 @、两侧非空、无空白与 *，
// 与 ck_users_email_shape 同口径。
// 应用层先拒，错误码才是 validation_failed；
// 库内 CHECK 是最后一道，不是第一道。
package user

import (
	"strings"
	"unicode"
)

// NormalizeEmail 去首尾空白并转小写。
// 每次比较与存储前都要先归一，否则同一地址会有两种行形态。
func NormalizeEmail(email string) string {
	return strings.ToLower(strings.TrimSpace(email))
}

// ValidEmail 报告已归一的邮箱是否满足库内 CHECK 的形状。
//
// 判定顺序固定：长度、非法字符、@ 个数、两侧非空。
// RFC 5321 允许无点域名，故本函数不要求域名含点（裁决 P3-5）。
func ValidEmail(email string) bool {
	if len(email) < 3 || len(email) > 254 {
		return false
	}
	if strings.ContainsAny(email, "*") {
		return false
	}
	// 任何 Unicode 空白（含 \v、U+00A0）一律拒绝：
	// 库内 CHECK 的 [[:space:]] 随排序规则变化，应用层先按最严口径拦。
	if strings.IndexFunc(email, unicode.IsSpace) >= 0 {
		return false
	}
	if strings.Count(email, "@") != 1 {
		return false
	}
	at := strings.IndexByte(email, '@')
	return at > 0 && at < len(email)-1
}
