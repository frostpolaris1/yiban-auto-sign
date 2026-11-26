package main

import (
	"net/url"
	"regexp"
	"strings"
)

// probeURLPattern 匹配错误串里的 http/https URL。空白、引号、尖括号与中文
// 句读是边界；中文正文直接粘在 URL 后仍按 URL 一并脱敏（宁可多隐藏，不外泄
// token）。尾随的 ASCII 句读由 redactProbeText 先 trim、脱敏后再原样拼回。
var probeURLPattern = regexp.MustCompile(`(?i)https?://[^\s"'<>，。、；：！？）」】》]*`)

// probeURLTrailingPunctuation 是 URL 外侧的 ASCII 句读。URL 自身可以合法包含
// 这些字符，但探针输出的是错误串，优先保留句子结构；脱敏结果本就不承诺字节保真。
const probeURLTrailingPunctuation = ",;:.)]}.!?"

// redactProbeText 对文本中的每个 http/https URL 脱敏：保留 scheme、host、
// path，查询串的值替换为 ***（参数名保留），fragment 清空。解析失败的
// URL 整段替换为 [redacted-url]；不含 URL 的文本原样返回。
func redactProbeText(text string) string {
	if text == "" {
		return text
	}
	return probeURLPattern.ReplaceAllStringFunc(text, func(raw string) string {
		urlPart := strings.TrimRight(raw, probeURLTrailingPunctuation)
		suffix := raw[len(urlPart):]
		return redactProbeURL(urlPart) + suffix
	})
}

// redactProbeURL 不是字节保真：userinfo 丢弃、fragment 丢弃，查询参数值无论
// 空否都渲染成 ***。抓取物保留原始末尾 ?；输出面文本经 redactProbeText 会把
// 末尾 ? 当句读保留，而单 URL 解析时空 query 的 ? 被丢弃。不要拿脱敏后的 URL
// 做线上形状断言。
func redactProbeURL(raw string) string {
	u, err := url.Parse(raw)
	if err != nil || u.Host == "" {
		return "[redacted-url]"
	}
	scheme := strings.ToLower(u.Scheme)
	if scheme != "http" && scheme != "https" {
		return "[redacted-url]"
	}
	var b strings.Builder
	b.WriteString(scheme)
	b.WriteString("://")
	b.WriteString(u.Host)
	b.WriteString(u.EscapedPath())
	if u.RawQuery != "" {
		b.WriteByte('?')
		for i, part := range strings.Split(u.RawQuery, "&") {
			if i > 0 {
				b.WriteByte('&')
			}
			key, _, _ := strings.Cut(part, "=")
			b.WriteString(key)
			b.WriteString("=***")
		}
	}
	return b.String()
}
