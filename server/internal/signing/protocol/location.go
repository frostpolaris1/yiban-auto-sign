package protocol

import "regexp"

var verifyRequestRE = regexp.MustCompile(`verify_request=([^&]+)`)

// ExtractVerifyRequest 从 302 的 Location 头提取 verify_request。
//
// 令牌可能位于 query 末位（其后没有 &），正则因此不要求后随分隔符。
// 取不到返回 ("", false)，不抛异常；由调用方决定语义。
func ExtractVerifyRequest(location string) (string, bool) {
	m := verifyRequestRE.FindStringSubmatch(location)
	if m == nil || m[1] == "" {
		return "", false
	}
	return m[1], true
}
