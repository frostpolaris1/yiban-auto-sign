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
// MaskPhone、MaskEmail、MaskDevice、MaskText、TextHasPhone、TextHasEmail、EmailLabel。
//
// # 口径
// 所有函数幂等：对已掩码的值再掩码，返回原值。
// 手机号保留前 3 后 4，邮箱保留首字符与域名，设备保留前 2 后 2。
// 两套口径都在本包，判定只有一份实现：
// 掩码口径 MaskText（宁多勿漏，时间戳形态也会被改写）；
// 检测口径 TextHasPhone/TextHasEmail（宁缺勿冤，只认对齐窗口 + 1[3-9]）。
// 审计守卫与符合性套件用检测口径。连写进更长数字串的手机号检测不到，
// 属已登记限制；掩码仍会处理它。
package mask

import (
	"regexp"
	"strings"
	"unicode/utf8"
)

var (
	// phoneInText 匹配一个候选数字游程。
	// 游程内的数字可被空格、点、连字符等分隔符打断，
	// 并允许 +86 国家码前缀（W2C-R2-05）。
	// 游程内的窗口扫描见 maskPhoneRun：与分段规则同源。
	phoneInText = regexp.MustCompile(`\+?[0-9]+(?:[\s.\-]+[0-9]+)*`)
	emailInText = regexp.MustCompile(`[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}`)
)

// isDigitSep 报告 c 是否为号码书写中的分隔符。
// 游程正则的字符类是 [\s.\-]，Go 的 \s 为 [\t\n\f\r ]，**不含 \v**。
// 故 \v 分支是防御性的：正常输入走不到，保留它只为对齐书写习惯。
// 加号**不是**分隔符：+86 国家码留在段内，随窗口一起消费。
func isDigitSep(c byte) bool {
	switch c {
	case ' ', '\t', '\n', '\r', '\f', '\v', '.', '-':
		return true
	}
	return false
}

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
	// 按**字符**而非字节取首字符：邮箱本地部可以是多字节字符，
	// 按字节切会产出非法 UTF-8，落库即报错（W2D 审查 P2）。
	_, size := utf8.DecodeRuneInString(local)
	return local[:size] + "***" + s[at:]
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
//
// 手机号形态：11 位且以 1 开头，可带 86 国家码（13 位），
// 数字之间允许空格、点、连字符等分隔符。
// 判定做**两遍窗口扫描**：先取跨段的「对齐窗口」，再掩段内遗留窗口。
// 「139-0013-9000 100」与「2026-10-25 13900139000」都能正确处理：
// 手机号被掩，日期与数量保持原样。
// 改写非号码的数字会破坏原文，故窗口之外的数字一律不动。
//
// **调用方判「是否含 PII」用下面两个检测入口，不看本函数返回值**：
// 掩码口径宁多勿漏（时间戳形态也会被改写），
// 检测口径宁缺勿冤（只认 1[3-9] 开头，时间戳不判为手机号）。
// 两套口径刻意不同，理由见各自注释。
func MaskText(s string) string {
	s = emailInText.ReplaceAllStringFunc(s, MaskEmail)
	return phoneInText.ReplaceAllStringFunc(s, maskPhoneRun)
}

// TextHasPhone 报告文本里是否还有未掩码的手机号形态。
//
// 这是**检测口径**：只认**对齐窗口**（数字段开头到段结尾）且
// 11 位以 1[3-9] 开头、13 位以 86+1[3-9] 开头。
// 掩码口径（MaskText）宁多勿漏，会把时间戳形态也改写；
// 检测若沿用同一口径，纪元毫秒与编号会被冤成手机号（W2C-R2-02）。
// 融进更长数字串的手机号检测不到，属已登记限制。
// 审计守卫与符合性套件都用本函数做失败关闭判定。
func TextHasPhone(s string) bool {
	for _, run := range phoneInText.FindAllString(s, -1) {
		segs := splitSegments(run)
		// 检测窗口与掩码第一遍同构：从某段开头到某段结尾的**对齐窗口**。
		// 单段内部（i==j）即整段；跨段（i<j）要求窗口覆盖完整段。
		for i := 0; i < len(segs); i++ {
			for j := i; j < len(segs); j++ {
				dd := digitsOnly(run[segs[i].start:segs[j].end])
				switch {
				case len(dd) == 11 && dd[0] == '1' && dd[1] >= '3' && dd[1] <= '9':
					return true
				case len(dd) == 13 && strings.HasPrefix(dd, "86") && dd[2] == '1' && dd[3] >= '3' && dd[3] <= '9':
					return true
				}
			}
		}
	}
	return false
}

// TextHasEmail 报告文本里是否还有未掩码的邮箱形态。
// 已掩码（含 ***）的邮箱不算。
func TextHasEmail(s string) bool {
	for _, m := range emailInText.FindAllString(s, -1) {
		if MaskEmail(m) != m {
			return true
		}
	}
	return false
}

// segment 是游程内一段无分隔符的数字，start 与 end 是字节偏移（含头不含尾）。
type segment struct {
	start, end int
}

// splitSegments 按分隔符把游程切成数字段。
func splitSegments(run string) []segment {
	var out []segment
	start := -1
	for i := 0; i < len(run); i++ {
		if isDigitSep(run[i]) {
			if start >= 0 {
				out = append(out, segment{start, i})
				start = -1
			}
			continue
		}
		if start < 0 {
			start = i
		}
	}
	if start >= 0 {
		out = append(out, segment{start, len(run)})
	}
	return out
}

// maskPhoneRun 在游程内做两遍窗口扫描（W2C-R4-01 及其复审）。
//
// 第一遍：**对齐窗口**。窗口必须从某段的开头到某段的结尾，
// 总位数 11（1 开头）或 13（86 开头）。它修掉分隔号码，
// 又不会把「日期 + 手机号」的日期尾巴并进窗口。
//
// 第二遍：**段内遗留**。对第一遍没动过、位数仍 ≥11 的段，
// 掩掉段内最左的匹配窗口。它处理「手机号与数字连写」的输入；
// 对纯日期段（位数不足 11）无影响。
//
// 顺序固定：先对齐、后遗留；两遍都只消费未掩码的原文偏移。
func maskPhoneRun(run string) string {
	segs := splitSegments(run)
	if len(segs) == 0 {
		return run
	}
	var b strings.Builder
	consumed := 0
	i := 0
	for i < len(segs) {
		matched := false
		for j := i; j < len(segs); j++ {
			dd := digitsOnly(run[segs[i].start:segs[j].end])
			if len(dd) != 11 && len(dd) != 13 {
				continue
			}
			if !phoneShape(dd) {
				continue
			}
			b.WriteString(run[consumed:segs[i].start])
			b.WriteString(maskedSpan(run[segs[i].start:segs[j].end], len(dd)))
			consumed = segs[j].end
			i = j + 1
			matched = true
			break
		}
		if matched {
			continue
		}
		segText := run[segs[i].start:segs[i].end]
		if m := maskLeftmostInSegment(segText); m != segText {
			b.WriteString(run[consumed:segs[i].start])
			b.WriteString(m)
			consumed = segs[i].end
		}
		i++
	}
	b.WriteString(run[consumed:])
	return b.String()
}

// maskLeftmostInSegment 掩掉段内最左的手机号窗口。
// 段是无分隔符的纯数字；窗口取 11 位或 86 前缀的 13 位。
func maskLeftmostInSegment(seg string) string {
	for start := 0; start+11 <= len(seg); start++ {
		if dd := seg[start : start+11]; phoneShape(dd) {
			return seg[:start] + MaskPhone(dd) + seg[start+11:]
		}
		if start+13 <= len(seg) {
			if dd := seg[start : start+13]; strings.HasPrefix(dd, "86") && phoneShape(dd) {
				return seg[:start] + MaskPhone(dd[2:]) + seg[start+13:]
			}
		}
	}
	return seg
}

// phoneShape 判定数字串是否为手机号形态：11 位以 1 开头，
// 或 13 位以 86 国家码开头且第 3 位是 1。与 MaskPhone 的口径一致。
func phoneShape(digits string) bool {
	switch len(digits) {
	case 11:
		return digits[0] == '1'
	case 13:
		return strings.HasPrefix(digits, "86") && digits[2] == '1'
	}
	return false
}

// maskedSpan 返回对齐窗口的掩码文本：前 3 后 4，去掉分隔符与国家码。
func maskedSpan(seg string, n int) string {
	dd := digitsOnly(seg)
	if n == 13 {
		dd = dd[2:]
	}
	return dd[:3] + "****" + dd[7:]
}

// digitsOnly 返回 s 中的十进制数字。
func digitsOnly(s string) string {
	var b strings.Builder
	b.Grow(len(s))
	for i := 0; i < len(s); i++ {
		if s[i] >= '0' && s[i] <= '9' {
			b.WriteByte(s[i])
		}
	}
	return b.String()
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
