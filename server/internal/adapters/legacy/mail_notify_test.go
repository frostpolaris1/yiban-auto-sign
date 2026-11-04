package legacy

// 旧 users.mail_notify → 新布尔列的映射判据。
//
// # 口径
// 旧列是 INTEGER DEFAULT 1，旧读取口径只把 "1"/"true"/"on"/"yes"
// 当开启。缺键或 null 取 true；其余（"0"、false、空串）保持 opt-out。
// 本文件**不**连库；真库导入的端到端断言在 cmd/cli 的 migrate-legacy e2e。

import "testing"

func TestLegacyMailNotify(t *testing.T) {
	cases := []struct {
		name string
		row  map[string]any
		want bool
	}{
		{"missing defaults true", map[string]any{}, true},
		{"null defaults true", map[string]any{"mail_notify": nil}, true},
		{"int one is on", map[string]any{"mail_notify": JSONNumber("1")}, true},
		{"int zero is off", map[string]any{"mail_notify": JSONNumber("0")}, false},
		{"string one is on", map[string]any{"mail_notify": "1"}, true},
		{"string on is on", map[string]any{"mail_notify": " ON "}, true},
		{"string yes is on", map[string]any{"mail_notify": "yes"}, true},
		{"string off is off", map[string]any{"mail_notify": "off"}, false},
		{"bool true is on", map[string]any{"mail_notify": true}, true},
		{"bool false is off", map[string]any{"mail_notify": false}, false},
		{"empty string is off like the old filter", map[string]any{"mail_notify": ""}, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := legacyMailNotify(tc.row); got != tc.want {
				t.Fatalf("legacyMailNotify(%v) = %v, want %v", tc.row, got, tc.want)
			}
		})
	}
}
