package protocol

import (
	"bytes"
	"encoding/json"
	"net/url"
	"strconv"
	"strings"
)

// SignInBody 是签到请求的输入。
type SignInBody struct {
	LngLat             Point
	Address            string
	OutState           string
	Reason             string
	AttachmentFileName string
	Code               string
	PhoneModel         string
}

// BuildUsersureForm 构造 usersure 的 POST 请求体。
//
// 字段顺序固定：oauth_uname、oauth_upwd、client_id、redirect_uri、display；
// scope 仅在非 nil 时追加。编码为 form-urlencoded（空格→+）。
// display 为空时取 "authorize"：与 SPEC §2.6 的默认值一致。
func BuildUsersureForm(phone, encryptedPassword, clientID, redirectURI, display string, scope *string) string {
	if display == "" {
		display = "authorize"
	}
	pairs := [][2]string{
		{"oauth_uname", phone},
		{"oauth_upwd", encryptedPassword},
		{"client_id", clientID},
		{"redirect_uri", redirectURI},
		{"display", display},
	}
	if scope != nil {
		pairs = append(pairs, [2]string{"scope", *scope})
	}
	return encodePairs(pairs)
}

// BuildSignInBody 构造 nightAttendance 签到 POST 请求体。
//
// 顶层五字段顺序对齐 H5 SPA：AttachmentFileName、OutState、Code、
// PhoneModel、SignInfo。SignInfo 的键序固定，JSON 用 ", " 与 ": " 分隔。
func BuildSignInBody(in SignInBody) string {
	outState := in.OutState
	if outState == "" {
		outState = "1"
	}
	lngLat := formatFloat(in.LngLat.Lng) + "," + formatFloat(in.LngLat.Lat)
	signInfo := `{"Reason": ` + quoteJSON(in.Reason) +
		`, "AttachmentFileName": ` + quoteJSON(in.AttachmentFileName) +
		`, "LngLat": ` + quoteJSON(lngLat) +
		`, "Address": ` + quoteJSON(in.Address) + `}`
	return encodePairs([][2]string{
		{"AttachmentFileName", in.AttachmentFileName},
		{"OutState", outState},
		{"Code", in.Code},
		{"PhoneModel", in.PhoneModel},
		{"SignInfo", signInfo},
	})
}

// encodePairs 按顺序拼接 form-urlencoded 字符串。
func encodePairs(pairs [][2]string) string {
	var b strings.Builder
	for i, pair := range pairs {
		if i > 0 {
			b.WriteByte('&')
		}
		b.WriteString(url.QueryEscape(pair[0]))
		b.WriteByte('=')
		b.WriteString(url.QueryEscape(pair[1]))
	}
	return b.String()
}

// formatFloat 用最短十进制表示坐标；不用科学计数法。
func formatFloat(v float64) string {
	return strconv.FormatFloat(v, 'f', -1, 64)
}

// quoteJSON 生成 JSON 字符串字面量。
// 用 encoding/json 编码：控制字符输出 \u00XX，不产出非法 JSON。
// 关闭 HTML 转义：地址里的 < > & 保持原样，与 Python 的 ensure_ascii=False 同形。
func quoteJSON(s string) string {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(s); err != nil {
		return `""`
	}
	return strings.TrimSuffix(buf.String(), "\n")
}
