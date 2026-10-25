// Package mask 是把个人数据转成展示文本的唯一位置。
//
// # 功能
// 掩码手机号、邮箱、设备号，以及自由文本中的手机号与邮箱。
// 本包**不**脱敏姓名、学号或凭据，**不**做加解密，**不**写日志。
//
// # 归属
// core 层纯领域包。审计、日志与界面展示都取本包结果。
// 调用方**不得自行复写掩码规则**。
//
// # 复用
// MaskPhone、MaskEmail、MaskDevice、MaskText、EmailLabel。
//
// # 口径
// 所有函数幂等：对已掩码的值再掩码，返回原值。
// 手机号保留前 3 后 4，邮箱保留首字符与域名，设备保留前 2 后 2。
package mask

import (
	"regexp"
	"strings"
)

var (
	phoneInText = regexp.MustCompile(`\b1\d{10}\b`)
	emailInText = regexp.MustCompile(`[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}`)
)

// MaskPhone 保留手机号前 3 位与后 4 位。
// 非 11 位、非 1 开头或已含掩码的输入原样返回。
func MaskPhone(s string) string {
	if strings.Contains(s, "****") || len(s) != 11 || s[0] != '1' {
		return s
	}
	for i := 0; i < len(s); i++ {
		if s[i] < '0' || s[i] > '9' {
			return s
		}
	}
	return s[:3] + "****" + s[7:]
}

// MaskEmail 保留邮箱首字符与域名。
func MaskEmail(s string) string {
	at := strings.LastIndexByte(s, '@')
	if at <= 0 {
		return s
	}
	local := s[:at]
	if strings.Contains(local, "***") {
		return s
	}
	return local[:1] + "***" + s[at:]
}

// MaskDevice 保留设备号前 2 位与后 2 位。
// 长度不超过 4 时整串掩掉，**不泄露任何字符**。
func MaskDevice(s string) string {
	if strings.Contains(s, "****") {
		return s
	}
	if len(s) <= 4 {
		return "****"
	}
	return s[:2] + "****" + s[len(s)-2:]
}

// MaskText 掩码自由文本中的手机号与邮箱。
func MaskText(s string) string {
	s = emailInText.ReplaceAllStringFunc(s, MaskEmail)
	return phoneInText.ReplaceAllStringFunc(s, MaskPhone)
}

// EmailLabel 返回审计 actor 标签。
//
// **无法掩码的输入一律返回 "unknown"**：畸形串可能是手机号或任意文本，
// 原样写入审计会把未掩码的个人数据带进链。
func EmailLabel(s string) string {
	normalized := strings.ToLower(strings.TrimSpace(s))
	masked := MaskEmail(normalized)
	if normalized == "" || masked == normalized {
		return "unknown"
	}
	return masked
}
