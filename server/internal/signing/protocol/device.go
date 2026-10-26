package protocol

import (
	"encoding/json"
	"strings"
)

// DeviceState 是 getState 的 data 载荷。
//
// Code 为空串表示该账号没有登记设备；Authorized 为假表示登记了但未授权。
// 未登记设备是正常状态，不是畸形响应。
type DeviceState struct {
	Authorized bool
	Code       string
	PhoneModel string
	UpdateTime *int
}

// ParseDeviceState 解析设备登记载荷。
//
// 四个字段缺省即默认；只有字段存在但类型不符才返回 ParseError。
func ParseDeviceState(data any) (DeviceState, error) {
	m, ok := data.(map[string]any)
	if !ok {
		return DeviceState{}, parseError("data", data)
	}
	code, err := optionalDeviceString(m, "Code")
	if err != nil {
		return DeviceState{}, err
	}
	model, err := optionalDeviceString(m, "PhoneModel")
	if err != nil {
		return DeviceState{}, err
	}
	return DeviceState{
		Authorized: toBool(m["authorize"]),
		Code:       code,
		PhoneModel: model,
		UpdateTime: intPtr(m["UpdateTime"]),
	}, nil
}

// optionalDeviceString 取可选字符串：缺失/None → 空串；非字符串 → ParseError。
func optionalDeviceString(m map[string]any, key string) (string, error) {
	v, ok := m[key]
	if !ok || v == nil {
		return "", nil
	}
	s, ok := v.(string)
	if !ok {
		return "", parseError(key, v)
	}
	return s, nil
}

// toBool 把 authorize 收敛为 bool；缺失或不可判为 false。
func toBool(v any) bool {
	switch x := v.(type) {
	case bool:
		return x
	case nil:
		return false
	case int:
		return x != 0
	case int64:
		return x != 0
	case float64:
		return x != 0
	case json.Number:
		// decodeObject 用 UseNumber：真实解码路径的数字都是 json.Number。
		if n, err := x.Int64(); err == nil {
			return n != 0
		}
		if f, err := x.Float64(); err == nil {
			return f != 0
		}
		return false
	case string:
		text := strings.ToLower(strings.TrimSpace(x))
		return text == "1" || text == "true" || text == "on" || text == "yes"
	}
	return false
}
