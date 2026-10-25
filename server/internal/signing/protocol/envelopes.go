package protocol

import (
	"fmt"
	"strings"
)

// Envelope 是 api.uyiban.com 系列的通用响应信封。
type Envelope struct {
	// Code 保留上游原形态：int64、float64 或 string。
	Code any
	Msg  string
	Data any
	Raw  map[string]any
}

// UsersureResult 是 usersure 端点的扁平响应。
type UsersureResult struct {
	OK    bool
	ReURL *string
	Code  string
}

var (
	challengeMarkers = []string{"ydclearance", "fengkongcloud"}
	htmlMarkers      = []string{"<html", "<!doctype", "<script"}
	weakHTMLMarkers  = []string{"captcha"}
)

// ParseAPIEnvelope 解析 api.uyiban.com 的 JSON 信封。
//
// **不看 Content-Type**：上游会用 text/html 下发 JSON。
// JSON 不是对象 → ParseError；code=999 → SessionExpired。
func ParseAPIEnvelope(body []byte) (Envelope, error) {
	raw, err := decodeObject(body, "envelope")
	if err != nil {
		return Envelope{}, err
	}
	code := normalizeCode(raw["code"])
	if codeIsSessionExpired(code) {
		return Envelope{}, &SessionExpired{Msg: msgsOf(raw["msg"])}
	}
	return Envelope{Code: code, Msg: msgsOf(raw["msg"]), Data: raw["data"], Raw: raw}, nil
}

// ParseUsersureResponse 解析 usersure 响应。
// 业务失败码作为数据返回；只有结构性失败才返回 ParseError。
func ParseUsersureResponse(body []byte) (UsersureResult, error) {
	raw, err := decodeObject(body, "usersure")
	if err != nil {
		return UsersureResult{}, err
	}
	code := codeString(raw["code"])
	var reURL *string
	if s, ok := raw["reUrl"].(string); ok && s != "" {
		reURL = &s
	}
	return UsersureResult{OK: code == "s200", ReURL: reURL, Code: code}, nil
}

// LooksLikeChallenge 只检测 WAF/风控挑战页，不求解。
//
// 刻意保守：误报不可接受，漏报可接受。所有正常夹具必须返回 false。
// 强信号命中即真；弱信号 captcha 与 acw_sc 必须带 HTML 上下文。
func LooksLikeChallenge(body string) bool {
	lowered := strings.ToLower(body)
	for _, marker := range challengeMarkers {
		if strings.Contains(lowered, marker) {
			return true
		}
	}
	html := false
	for _, marker := range htmlMarkers {
		if strings.Contains(lowered, marker) {
			html = true
			break
		}
	}
	if !html {
		return false
	}
	if strings.Contains(lowered, "acw_sc") {
		return true
	}
	for _, marker := range weakHTMLMarkers {
		if strings.Contains(lowered, marker) {
			return true
		}
	}
	return false
}

// CodeIsOK 判定信封成功：数字 0 与字符串 "0" 都算。
// bool 不是成功码：false 与 0 等值会误判。
func CodeIsOK(code any) bool {
	return codeIsOK(code)
}

// codeString 把 code 收敛为字符串，供 usersure 与日志使用。
func codeString(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	if v == nil {
		return ""
	}
	return fmt.Sprint(normalizeCode(v))
}
