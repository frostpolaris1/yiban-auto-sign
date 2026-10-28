// Package mask 是把个人数据转成展示文本的唯一位置。
//
// # 功能
// 掩码手机号、邮箱、设备号，以及自由文本中的手机号与邮箱。
// 把错误换成可进日志的分类词：错误原文不回放。
// 本包**不**脱敏姓名、学号或凭据，**不**做加解密，**不**写日志。
//
// # 归属
// core 层纯领域包。审计、日志与界面展示都取本包结果。
// 调用方**不得自行复写掩码规则**。
//
// # 复用
// MaskPhone、MaskEmail、MaskDevice、MaskText、TextHasPhone、TextHasEmail、
// EmailLabel、SafeErrorText。
//
// # 口径
// 所有函数幂等：对已掩码的值再掩码，返回原值。
// 合法 UTF-8 输入经任何掩码函数后仍是合法 UTF-8；切分首尾一律按字符，不按字节。
// 手机号保留前 3 后 4，邮箱保留首字符与域名，设备保留前 2 后 2。
// 两套口径都在本包，判定只有一份实现：
// 掩码口径 MaskText（宁多勿漏，时间戳形态也会被改写）；
// 检测口径 TextHasPhone/TextHasEmail（宁缺勿冤，只认对齐窗口 + 1[3-9]）。
// 邮箱检测覆盖常见地址：本地部与域名含 Unicode 字母、数字与组合记号，
// 域名带点且 TLD 至少 2 个字母。
// 下列系统接受的形状不在检测口径内，属已登记限制：
// 无点域、TLD 不是至少 2 个字母、Punycode TLD、符号或 emoji 本地部。
// 审计守卫与符合性套件用检测口径。
// 段内滑动检测只对 22 位以上的数字段生效；11 至 21 位的整段数字
// 只认对齐窗口，避免秒/毫秒/微秒/纳秒时间戳与编号被冤成号码（R3-002）。
// 故 12 至 21 位数字串内嵌的号码不判，属已登记限制；掩码仍会处理。
// 下划线、逗号与全角数字不在分隔符或数字集合内，属同一类已登记限制。
// 多段窗口每段超过 4 位的分组不判（如 13800 138000）；掩码仍会处理。
package mask

import (
	"errors"
	"net"
	"regexp"
	"strings"
	"unicode/utf8"
)

// SafeErrorText 把错误换成可进日志的分类词。
//
// 传输错误的原文带完整 URL；查询串里有会话标识（CSRF 等）。
// 故本函数只给分类词，**不回放原文**。
// 需要错误细节时按任务标识回查，不靠日志。
func SafeErrorText(err error) string {
	if err == nil {
		return ""
	}
	var netErr net.Error
	if errors.As(err, &netErr) {
		if netErr.Timeout() {
			return "transport timeout"
		}
		return "transport error"
	}
	return "unclassified error"
}

var (
	// phoneInText 匹配一个候选数字游程。
	// 游程内的数字可被空格、点、连字符等分隔符打断，
	// 并允许 +86 国家码前缀（W2C-R2-05）。
	// 游程内的窗口扫描见 maskPhoneRun：与分段规则同源。
	phoneInText = regexp.MustCompile(`\+?[0-9]+(?:[\s.\-]+[0-9]+)*`)
	// emailInText 覆盖常见地址：本地部与域名含 Unicode 字母、数字与组合记号，
	// 域名带点且 TLD 至少 2 个字母。
	// 只认 ASCII 会让非 ASCII 邮箱躲过审计 PII 守卫（N5）。
	// \p{M} 收组合记号：分解形式的带音标字符由字母与记号组成。
	// 已登记不覆盖：无点域（a@localhost）、TLD 不是至少 2 个字母
	// （a@b.c、a@b.12、a@b.12c）、Punycode TLD（a@xn--fiqs8s）、
	// 符号或 emoji 本地部（😀@example.com）。
	emailInText = regexp.MustCompile(`[\p{L}\p{N}\p{M}._%+\-]+@[\p{L}\p{N}\p{M}.\-]+\.[\p{L}]{2,}`)
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

// MaskDevice 保留设备号前 2 个字符与后 2 个字符。
// 长度不超过 4 个字符时整串掩掉，**不泄露任何字符**。
// 按字符切，不按字节：多字节设备码不得产出非法 UTF-8（N1）。
func MaskDevice(s string) string {
	if strings.Contains(s, "****") {
		return s
	}
	runes := []rune(s)
	if len(runes) <= 4 {
		return "****"
	}
	return string(runes[:2]) + "****" + string(runes[len(runes)-2:])
}

// MaskText 掩码自由文本中的手机号与邮箱。
//
// 手机号形态：11 位且以 1 开头，可带 86 国家码（13 位），
// 数字之间允许空格、点、连字符等分隔符。
// 邮箱形态：本地部与域名可含 Unicode 字母、数字与组合记号。
// 判定做**两遍窗口扫描**：先取跨段的「对齐窗口」，再掩段内遗留窗口。
// 「139-0013-9000 100」与「2026-10-25 13900139000」都能正确处理：
// 手机号被掩，日期与数量保持原样。
// 段内遗留窗口扫描到**没有命中为止**：一个数字段里连写两个号码时
// 两个都要掩，只掩最左会把第二个号码明文留下。
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
// 这是**检测口径**：只认 1[3-9] 开头的 11 位，或 86+1[3-9] 开头的 13 位。
// 掩码口径（MaskText）宁多勿漏，会把时间戳形态也改写；
// 检测若沿用同一口径，纪元毫秒与编号会被冤成手机号（W2C-R2-02）。
//
// 窗口规则分两种：
// 多段（数字之间有分隔符）只认**对齐窗口**，且每段不超过 4 位；
// 单段（连续数字）在 22 位以上按**滑动窗口**判，抓段内连写的号码。
// 11 至 21 位的整段数字仍只认对齐窗口：这一长度覆盖秒、毫秒、微秒与纳秒
// 纪元时间戳（10/13/16/19 位），滑动会把时间戳冤成号码。
// 故 12 至 21 位数字串内嵌的号码不判，属已登记限制；
// 掩码仍会改写它，敏感面不因此残留明文。
// 审计守卫与符合性套件都用本函数做失败关闭判定。
func TextHasPhone(s string) bool {
	for _, run := range phoneInText.FindAllString(s, -1) {
		segs := splitSegments(run)
		// 检测窗口与掩码第一遍同构：从某段开头到某段结尾的**对齐窗口**。
		// 单段内部（i==j）即整段；跨段（i<j）要求窗口覆盖完整段。
		for i := 0; i < len(segs); i++ {
			for j := i; j < len(segs); j++ {
				if j > i && !segmentsFitPhone(segs[i:j+1]) {
					continue
				}
				if isPhoneDigits(digitsOnly(run[segs[i].start:segs[j].end])) {
					return true
				}
			}
		}
		// 段内滑动：只对长过 maxAlignedDigits 的段生效，见函数注释。
		for _, seg := range segs {
			if seg.end-seg.start <= maxAlignedDigits {
				continue
			}
			if hasEmbeddedPhone(run[seg.start:seg.end]) {
				return true
			}
		}
	}
	return false
}

// maxAlignedDigits 是仍按「整段对齐」判定的最大位数。
// 11 至 21 位的整段数字可能是时间戳（秒/毫秒/微秒/纳秒）或编号；
// 22 位以上只能是复合串（两个 11 位号码连写）。
const maxAlignedDigits = 21

// hasEmbeddedPhone 在连续数字串里滑动查找手机号形态。
// 只用于长过 maxAlignedDigits 的段；每个起点都用严格检测口径判。
func hasEmbeddedPhone(digits string) bool {
	for start := 0; start+11 <= len(digits); start++ {
		if isPhoneDigits(digits[start : start+11]) {
			return true
		}
	}
	for start := 0; start+13 <= len(digits); start++ {
		if isPhoneDigits(digits[start : start+13]) {
			return true
		}
	}
	return false
}

// isPhoneDigits 是检测口径的形状判定：11 位以 1[3-9] 开头，
// 或 13 位以 86+1[3-9] 开头。与掩码口径 phoneShape 刻意不同。
func isPhoneDigits(digits string) bool {
	switch len(digits) {
	case 11:
		return digits[0] == '1' && digits[1] >= '3' && digits[1] <= '9'
	case 13:
		return strings.HasPrefix(digits, "86") &&
			digits[2] == '1' && digits[3] >= '3' && digits[3] <= '9'
	}
	return false
}

// segmentsFitPhone 报告多段窗口是否符合手机号书写。
//
// 单段不限：11 位连写是常见形态。
// 多段每段不超过 4 位：时间戳的「秒.纳秒」会形成 2+9 段，
// 11 位数字形似手机号，但不是号码（R3-002 的同类假阳性）。
// 代价是 5 位以上的异常分组不判为手机号；掩码仍会处理，
// 检测口径宁缺勿冤。
func segmentsFitPhone(segs []segment) bool {
	for _, seg := range segs {
		if seg.end-seg.start > 4 {
			return false
		}
	}
	return true
}

// TextHasEmail 报告文本里是否还有未掩码的邮箱形态。
// 已掩码（含 ***）的邮箱不算。
// 覆盖非 ASCII 字母、数字与组合记号；符号更多的本地部不在此口径内。
// 无点域、纯数字 TLD 与 Punycode TLD 也不在；清单见 emailInText 注释。
// 检测口径宁缺勿冤，但不得对系统接受的字符类整体失明（N5）。
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
// 掩掉段内**每一个**匹配窗口，直到没有命中。
// 它处理「手机号与数字连写」的输入；只掩最左会让第二个号码明文留下。
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
		if m := maskAllInSegment(segText); m != segText {
			b.WriteString(run[consumed:segs[i].start])
			b.WriteString(m)
			consumed = segs[i].end
		}
		i++
	}
	b.WriteString(run[consumed:])
	return b.String()
}

// maskAllInSegment 掩掉段内全部手机号窗口。
// 段是无分隔符的纯数字；窗口取 11 位或 86 前缀的 13 位。
// 掩掉一个后从下一个偏移继续扫，直到没有命中：
// 一个段里连写两个号码时两个都掩。
func maskAllInSegment(seg string) string {
	if len(seg) < 11 {
		return seg
	}
	var b strings.Builder
	consumed := 0
	for start := 0; start < len(seg); {
		if start+11 <= len(seg) {
			if dd := seg[start : start+11]; phoneShape(dd) {
				b.WriteString(seg[consumed:start])
				b.WriteString(MaskPhone(dd))
				consumed = start + 11
				start += 11
				continue
			}
		}
		if start+13 <= len(seg) {
			if dd := seg[start : start+13]; strings.HasPrefix(dd, "86") && phoneShape(dd) {
				b.WriteString(seg[consumed:start])
				b.WriteString(MaskPhone(dd[2:]))
				consumed = start + 13
				start += 13
				continue
			}
		}
		start++
	}
	b.WriteString(seg[consumed:])
	return b.String()
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
