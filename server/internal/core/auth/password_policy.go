package auth

// 口令强度策略。
//
// # 功能
// 判定明文口令是否满足自助口令的最小强度，返回稳定英文原因。
// 本文件**不**哈希口令，**不**读写数据库。
//
// # 归属
// core 层纯领域规则。app/auth 的公开注册先接入；
// 其余口令写入口在口令策略批次里复用同一实现，避免两份口径。
//
// # 口径
// 按角色分档：user 至少 10 个字符、命中两类；master/admin 至少 12 个
// 字符、命中三类（旧线 parity）。类别按 ASCII 判定，与旧线正则
// [A-Z]、[a-z]、\d、[^A-Za-z0-9] 同序；非 ASCII 字符计为符号。
// 长度按 Unicode 字符数，不按字节。
// 上限 1024 字节与凭据列的上界一致：超长负载在哈希前拒绝。
// 注册与本站全部口令写入口共用同一档位；易班账号的 platform 凭据
// 不适用本策略。

import (
	"strconv"
	"unicode/utf8"
)

const (
	// PasswordMinChars 是普通用户口令的最小字符数。
	PasswordMinChars = 10
	// PasswordMinClasses 是普通用户口令最少命中的字符类别数。
	PasswordMinClasses = 2
	// PasswordMinCharsPrivileged 是 master/admin 口令的最小字符数。
	PasswordMinCharsPrivileged = 12
	// PasswordMinClassesPrivileged 是 master/admin 口令最少命中的类别数。
	PasswordMinClassesPrivileged = 3
	// PasswordMaxBytes 是明文口令的字节上限。
	PasswordMaxBytes = 1024
)

// PasswordPolicyError 返回口令不满足普通用户档策略的原因；通过时返回空串。
//
// 返回英文稳定文案，供 API 直接下发；界面可自行本地化。
// 判定顺序固定：先长度，再字节上限，再类别。
func PasswordPolicyError(password string) string {
	return passwordPolicyErrorFor(password, PasswordMinChars, PasswordMinClasses)
}

// PasswordPolicyErrorForRole 返回口令不满足目标角色档策略的原因；通过时返回空串。
//
// master/admin 走特权档（12 字符、三类），user 与未知角色走普通档（10 字符、
// 两类）。未知角色按普通档处理是安全方向的缺省。
func PasswordPolicyErrorForRole(password string, role Role) string {
	if role == RoleMaster || role == RoleAdmin {
		return passwordPolicyErrorFor(password, PasswordMinCharsPrivileged, PasswordMinClassesPrivileged)
	}
	return PasswordPolicyError(password)
}

// passwordPolicyErrorFor 是分档实现的唯一判定点。
// 类别文案用英文数词，与既有稳定文案逐字一致。
func passwordPolicyErrorFor(password string, minChars, minClasses int) string {
	if utf8.RuneCountInString(password) < minChars {
		return "password must be at least " + strconv.Itoa(minChars) + " characters"
	}
	if len(password) > PasswordMaxBytes {
		return "password is too long"
	}
	if PasswordClassCount(password) < minClasses {
		return "password must contain at least " + classWord(minClasses) +
			" of uppercase, lowercase, digits and symbols"
	}
	return ""
}

// classWord 返回类别数的英文数词。档位只有两类与三类。
func classWord(n int) string {
	if n == 3 {
		return "three"
	}
	return "two"
}

// PasswordClassCount 数口令命中的类别数，取值 0..4。
// 类别定义见包口径；空串返回 0。
func PasswordClassCount(password string) int {
	var upper, lower, digit, symbol bool
	for _, r := range password {
		switch {
		case r >= 'A' && r <= 'Z':
			upper = true
		case r >= 'a' && r <= 'z':
			lower = true
		case r >= '0' && r <= '9':
			digit = true
		default:
			symbol = true
		}
	}
	count := 0
	for _, hit := range []bool{upper, lower, digit, symbol} {
		if hit {
			count++
		}
	}
	return count
}
