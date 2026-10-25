package protocol

import (
	"regexp"
	"strings"
)

// AuthorizePage 是 OAuth 授权页提取出的值。
type AuthorizePage struct {
	// PageUse 是页面一次性令牌，后续作为 usersure 的 ajax_sign。
	PageUse string
	// PublicKeyPEM 是登录口令加密公钥（RSA-1024，PEM 文本）。
	PublicKeyPEM string
}

var (
	// pageUseRE 只允许字母数字，单双引号都容。
	pageUseRE = regexp.MustCompile(`var\s+page_use\s*=\s*['"]([A-Za-z0-9]+)['"]`)
	// keyInputRE 靠 id="key" 定位 input；上游把 type 写成 "test"，不得依赖 type。
	keyInputRE = regexp.MustCompile(`(?is)<input\b[^>]*\bid\s*=\s*['"]key['"][^>]*>`)
	// valueAttrRE 在标签文本内取 value；PEM 内部无引号。
	valueAttrRE = regexp.MustCompile(`(?is)\bvalue\s*=\s*['"](.*?)['"]`)
)

// ParseAuthorizePage 解析授权页，取出 page_use 与 RSA 公钥。
//
// 找不到令牌或公钥返回 ParseError。
func ParseAuthorizePage(html string) (AuthorizePage, error) {
	use := pageUseRE.FindStringSubmatch(html)
	if use == nil || use[1] == "" {
		return AuthorizePage{}, parseError("page_use", html)
	}

	tag := keyInputRE.FindString(html)
	if tag == "" {
		return AuthorizePage{}, parseError("public_key", html)
	}
	value := valueAttrRE.FindStringSubmatch(tag)
	if value == nil {
		return AuthorizePage{}, parseError("public_key", tag)
	}

	pem := normalizePEM(value[1])
	if !strings.Contains(pem, "-----BEGIN PUBLIC KEY-----") ||
		!strings.Contains(pem, "-----END PUBLIC KEY-----") {
		return AuthorizePage{}, parseError("public_key", pem)
	}
	return AuthorizePage{PageUse: use[1], PublicKeyPEM: pem}, nil
}

// normalizePEM 把嵌在 HTML 属性里的 PEM 归一化。
// 保留头尾行，中间 base64 行以 \n 连接，去掉首尾空白与 CRLF。
func normalizePEM(raw string) string {
	raw = strings.ReplaceAll(raw, "\r\n", "\n")
	raw = strings.ReplaceAll(raw, "\r", "\n")
	lines := strings.Split(raw, "\n")
	kept := make([]string, 0, len(lines))
	for _, line := range lines {
		line = strings.TrimSpace(line)
		if line != "" {
			kept = append(kept, line)
		}
	}
	return strings.Join(kept, "\n")
}
