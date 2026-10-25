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
// 归一＝去首尾空白后转小写。应用层产出小写地址，库内 ck_users_email_lower 兜底。
// 形状＝3 至 254 **字节**（len 的单位是字节）、恰好一个 @、两侧非空、
// 无 Unicode 空白、无 *、无 Unicode Cf 格式字符。
// 应用层比库内 CHECK 更严：库内的 [[:space:]] 随 LC_CTYPE 变化，且不拦 Cf。
// 库内 CHECK 兜底空白与非空侧；Cf 只由本层拦截，直写库可绕过。
// 发布前须查目标库是否已有 Cf 行（见 README 的 Release checks）。
// 库内 CHECK 不是 Cf 的最后一道。
// 应用层先拒，错误码才是 validation_failed。
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

// ValidEmail 报告已归一的邮箱是否满足应用层的最严形状。
//
// 判定顺序固定：长度、非法字符、@ 个数、两侧非空。
// 空白与 Cf 一并拒绝：任何 Unicode 空白（含 \v、U+00A0）都不允许；
// 零宽与双向控制等 Cf 字符让两个账号渲染相同，构成仿冒面。
// 代价是 ZWNJ/ZWJ 等在某些文字中合法的字符也被拒。
// 本部署的中文与拉丁地址不需要它们；若将来支持这类文字，须重新裁决。
// RFC 5321 允许无点域名，故本函数不要求域名含点（裁决 P3-5）。
func ValidEmail(email string) bool {
	if len(email) < 3 || len(email) > 254 {
		return false
	}
	if strings.ContainsAny(email, "*") {
		return false
	}
	if strings.IndexFunc(email, func(r rune) bool {
		return unicode.IsSpace(r) || unicode.Is(unicode.Cf, r)
	}) >= 0 {
		return false
	}
	if strings.Count(email, "@") != 1 {
		return false
	}
	at := strings.IndexByte(email, '@')
	return at > 0 && at < len(email)-1
}
