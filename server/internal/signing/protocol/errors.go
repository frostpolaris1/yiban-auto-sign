// Package protocol 是易班平台协议的解析与构造实现。
//
// # 功能
// 解析授权页、重定向令牌、API 信封、身份、签到点与设备登记；
// 构造 usersure 表单与签到体；用 RSA 公钥加密口令。
// 本包**不**发请求、**不**持会话、**不**写日志。
//
// # 归属
// signing 层的纯函数子包。provider 调用本包，宿主不直接调用。
// 本包只依赖标准库。
//
// # 复用
// ParseAuthorizePage、ExtractVerifyRequest、ParseAPIEnvelope、
// ParseUsersureResponse、LooksLikeChallenge、ParseIdentity、
// ParseSignPosition、BuildUsersureForm、BuildSignInBody、
// ParseDeviceState、EncryptPassword。
//
// # 口径
// 解析不 speculate：结构未知即 ParseError，带字段名与 ≤120 字符片段。
// 唯一例外是信封 code=999，返回 SessionExpired。
// 数值字段按双态收敛；字面量 "None" 视作无值。
// IsNeedPhoto 原样保留，不臆断为布尔。
// 所有函数无 I/O、无全局状态；测试不触网。
package protocol

import (
	"fmt"
	"strings"
)

const fragmentLimit = 120

// ParseError 表示输入不符合已知上游形态。
type ParseError struct {
	Field    string
	Fragment string
}

func (e *ParseError) Error() string {
	if e.Fragment == "" {
		return fmt.Sprintf("cannot parse %q", e.Field)
	}
	return fmt.Sprintf("cannot parse %q (%s)", e.Field, e.Fragment)
}

// SessionExpired 表示信封 code=999：会话失效，调用方必须重登。
type SessionExpired struct {
	Msg string
}

func (e *SessionExpired) Error() string {
	if e.Msg == "" {
		return "session expired"
	}
	return "session expired: " + e.Msg
}

// summarize 把值渲染成单行片段，并按字符截断。
func summarize(v any) string {
	text, ok := v.(string)
	if !ok {
		text = fmt.Sprintf("%v", v)
	}
	text = strings.ReplaceAll(text, "\r", `\r`)
	text = strings.ReplaceAll(text, "\n", `\n`)
	runes := []rune(text)
	if len(runes) > fragmentLimit {
		return string(runes[:fragmentLimit]) + "…"
	}
	return text
}

func parseError(field string, v any) error {
	return &ParseError{Field: field, Fragment: summarize(v)}
}
