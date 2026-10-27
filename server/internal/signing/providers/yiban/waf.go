package yiban

import (
	"regexp"
	"strconv"
	"strings"
	"unicode/utf8"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/protocol"
)

// WAF/挑战判定，复刻旧实现 `yiban/security.py::is_waf_blocked` 与
// `yiban/challenge.py::looks_like_challenge`（ref develop@1d1c5ccf）。
// 判据分两族：
//   - 形态（挑战页特征）：不受长度限制，挑战/拦截页的形状就是判据本身；
//   - 关键词：只在 ≤2000 字符的短响应里找，防把合法含"风控""拦截"的
//     正常长文误判成拦截页。
//
// 两族判据与旧实现同源；Set-Cookie 的 https_ydclearance 信号旧实现只在
// legacy 流程检查、killyiban 链不查，此处同批不复刻（待实拍定夺）。

// 挑战页双 JS 特征：旧实现 challenge.py 的强信号。
const (
	doubleJSSetup = "window.onload=setTimeout"
	doubleJSEval  = `eval("qo=eval;qo(po);")`
)

// maxWAFKeywordBodyRunes 是关键词判据的响应长度上界（字符数，与旧实现
// Python `len(text)` 同口径）。
const maxWAFKeywordBodyRunes = 2000

// challengeHitBody 判定响应体是否命中挑战页形态。误报不可接受，漏报可接受。
// 判据 = 协议库特征（ydclearance/fengkongcloud 强信号；acw_sc/captcha 弱信号
// 须 HTML 上下文）∪ 双 JS 特征。
func challengeHitBody(body string) bool {
	if strings.Contains(body, doubleJSSetup) && strings.Contains(body, doubleJSEval) {
		return true
	}
	return protocol.LooksLikeChallenge(body)
}

// wafBlocked 判定响应是否被 WAF 风控拦截（is_waf_blocked 复刻）。
func wafBlocked(body string) bool {
	if challengeHitBody(body) {
		return true
	}
	if utf8.RuneCountInString(body) > maxWAFKeywordBodyRunes {
		return false
	}
	return matchesWAFKeywords(body) || matchesWAFKeywords(decodeUnicodeEscapes(body))
}

// waf 关键词名册（旧实现 security.WAF_KEYWORDS 唯一真值源的 Go 侧复刻）：
// 中文词元在 base64 与 \uXXXX 转义文本里造不出来，按子串匹配；
// ASCII 词元 WAF 必须两侧都不是字母数字（aWAFb 不是拦截页写法）。
var wafChineseKeywords = []string{"风险访问", "风控", "访问服务禁用", "拦截"}

// matchesWAFKeywords 判定文本是否含 WAF 词元（无长度上界）。
func matchesWAFKeywords(text string) bool {
	if text == "" {
		return false
	}
	for _, keyword := range wafChineseKeywords {
		if strings.Contains(text, keyword) {
			return true
		}
	}
	return containsWAFToken(text)
}

// containsWAFToken 匹配 ASCII 词元 WAF，两侧必须不是 ASCII 字母数字。
func containsWAFToken(text string) bool {
	for start := 0; ; {
		offset := strings.Index(text[start:], "WAF")
		if offset < 0 {
			return false
		}
		i := start + offset
		before := byte(' ') // 起始位置视作边界
		if i > 0 {
			before = text[i-1]
		}
		j := i + len("WAF")
		after := byte(' ') // 末尾位置视作边界
		if j < len(text) {
			after = text[j]
		}
		if !isAlnumASCII(before) && !isAlnumASCII(after) {
			return true
		}
		start = i + 1
	}
}

func isAlnumASCII(b byte) bool {
	return b >= '0' && b <= '9' || b >= 'a' && b <= 'z' || b >= 'A' && b <= 'Z'
}

var unicodeEscapeRE = regexp.MustCompile(`\\u([0-9a-fA-F]{4})`)

// decodeUnicodeEscapes 把 \uXXXX 转还原字符：易班 JSON 响应的中文在转义里，
// 匹配前必须先解（旧实现 security._decode_unicode_escapes 同口径）。
func decodeUnicodeEscapes(text string) string {
	return unicodeEscapeRE.ReplaceAllStringFunc(text, func(match string) string {
		value, err := strconv.ParseUint(match[2:], 16, 32)
		if err != nil {
			return match
		}
		return string(rune(value))
	})
}

// blockedOutcome 把 WAF/挑战命中压成结论错误：
// 挑战形态 → OutcomeChallenge（不可重试，对应旧实现 ydclearance 硬失败档）；
// 仅关键词命中 → OutcomeTransient（可重试，对应旧实现风控档）。
func blockedOutcome(body string) error {
	if challengeHitBody(body) {
		return outcome(contract.OutcomeChallenge, "challenge page")
	}
	return outcome(contract.OutcomeTransient, "WAF blocked response")
}
