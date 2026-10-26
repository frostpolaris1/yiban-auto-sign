package protocol

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"strconv"
	"strings"
	"unicode/utf8"
)

// decodeObject 把 JSON 响应体解码为对象。
// **不看 Content-Type**：上游会用 text/html 下发 JSON。
// 数字保留为 json.Number，避免大整数丢精度。
// 非法 UTF-8 与尾部多余数据都拒绝：Python json.loads 同口径。
func decodeObject(body []byte, field string) (map[string]any, error) {
	if !utf8.Valid(body) {
		return nil, parseError(field, "<undecodable bytes>")
	}
	dec := json.NewDecoder(bytes.NewReader(body))
	dec.UseNumber()
	var v any
	if err := dec.Decode(&v); err != nil {
		return nil, parseError(field, string(body))
	}
	var extra any
	if err := dec.Decode(&extra); err != io.EOF {
		return nil, parseError(field, string(body))
	}
	m, ok := v.(map[string]any)
	if !ok {
		return nil, parseError(field, string(body))
	}
	return m, nil
}

// toInt 把上游双态数字（数字或数字字符串）收敛为 int。
// 缺失、布尔、不可解析 → false。
func toInt(v any) (int, bool) {
	switch x := v.(type) {
	case nil, bool:
		return 0, false
	case json.Number:
		if n, err := x.Int64(); err == nil {
			return int(n), true
		}
		if f, err := x.Float64(); err == nil {
			return int(f), true
		}
		return 0, false
	case float64:
		return int(x), true
	case int:
		return x, true
	case int64:
		return int(x), true
	case string:
		text := strings.TrimSpace(x)
		if text == "" {
			return 0, false
		}
		if n, err := strconv.Atoi(text); err == nil {
			return n, true
		}
		if f, err := strconv.ParseFloat(text, 64); err == nil {
			return int(f), true
		}
		return 0, false
	}
	return 0, false
}

// optionalString 取可选字符串：缺失/None → nil，其余保持或转字符串。
func optionalString(v any) *string {
	if v == nil {
		return nil
	}
	if s, ok := v.(string); ok {
		return &s
	}
	s := fmt.Sprint(v)
	return &s
}

// noneLike 把字面量 "None" 与空串视作无值。
func noneLike(v any) *string {
	if v == nil {
		return nil
	}
	if s, ok := v.(string); ok {
		text := strings.TrimSpace(s)
		if text == "" || text == "None" {
			return nil
		}
		return &s
	}
	s := fmt.Sprint(v)
	return &s
}

// requiredString 取必填字符串；缺失、nil 或非字符串 → ParseError。
func requiredString(m map[string]any, key string) (string, error) {
	v, ok := m[key]
	if !ok || v == nil {
		return "", parseError(key, m)
	}
	s, ok := v.(string)
	if !ok {
		return "", parseError(key, v)
	}
	return s, nil
}

// normalizeCode 把信封 code 收敛为 int64、float64 或 string。
// 上游同一字段可能发数字也可能发字符串。
func normalizeCode(v any) any {
	switch x := v.(type) {
	case json.Number:
		if n, err := x.Int64(); err == nil {
			return n
		}
		if f, err := x.Float64(); err == nil {
			return f
		}
		return x.String()
	case float64:
		if x == float64(int64(x)) {
			return int64(x)
		}
		return x
	case string:
		return x
	case nil:
		return nil
	}
	return v
}

// codeIsOK 判定信封成功：数字 0 与字符串 "0" 都算。
// bool 不是成功码：false 与 0 等值会误判。
func codeIsOK(v any) bool {
	switch x := v.(type) {
	case int:
		return x == 0
	case int64:
		return x == 0
	case float64:
		return x == 0
	case string:
		return x == "0"
	case json.Number:
		// Raw["code"] 保留 json.Number；导出入口必须与归一后的 Code 同判。
		if n, err := x.Int64(); err == nil {
			return n == 0
		}
		if f, err := x.Float64(); err == nil {
			return f == 0
		}
		return false
	}
	return false
}

// codeIsSessionExpired 判定 999：数字与字符串两种形态都算。
func codeIsSessionExpired(v any) bool {
	switch x := v.(type) {
	case int:
		return x == 999
	case int64:
		return x == 999
	case float64:
		return x == 999
	case string:
		return x == "999"
	case json.Number:
		if n, err := x.Int64(); err == nil {
			return n == 999
		}
		if f, err := x.Float64(); err == nil {
			return f == 999
		}
		return false
	}
	return false
}

// msgsOf 把可选 msg 字段收敛为字符串。
func msgsOf(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	if v == nil {
		return ""
	}
	return fmt.Sprint(v)
}
