package legacy

// 对账哈希与序列化的判据（P8-B 派单 2026-10-27）。
//
// 黄金值由 P8-A 导出器的同一份 Python 实现现算得出
//（scripts/db_export.py 的 _recon_hash / _dump_line，2026-10-27）。
// 两侧实现不共享代码；黄金值不齐即本门红。
// 掩码规则与 core/mask.MaskPhone 同一实现（canonical.tsv mask.phone 门）。

import (
	"testing"
)

func TestCanonicalHashGolden(t *testing.T) {
	// 行字段与黄金值一一对应；改任何一侧都必须重算黄金值。
	usersRow := map[string]any{
		"id":            JSONNumber("1"),
		"email":         "user0000@example.invalid",
		"role":          "admin",
		"password_hash": "scrypt:32768:8:1$0123456789abcdef$9b9e",
		"created_at":    "2026-01-01T00:00:00",
		"deleted":       JSONNumber("0"),
	}
	accountRow := map[string]any{
		"id":            JSONNumber("1"),
		"phone":         "13800000001",
		"name":          "acct-a",
		"sort_order":    JSONNumber("0"),
		"password":      map[string]any{"v": JSONNumber("2"), "kid": "0000000000000000", "nonce": "000000000000000000000000", "ct": "abababababababababab", "tag": "cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd"},
		"phone_code":    "",
		"phone_model":   "model-0000",
		"owner":         "admin",
		"status":        "active",
		"reject_reason": "",
		"deleted":       JSONNumber("0"),
		"user_paused":   JSONNumber("0"),
	}
	prefRow := map[string]any{
		"phone":      "13800000001",
		"slot_min":   JSONNumber("30"),
		"updated_at": "2026-01-01T00:00:00",
	}
	cases := []struct {
		name  string
		table string
		key   string
		row   map[string]any
		want  string
	}{
		{"users", "users", "1", usersRow,
			"da2db35cc78441eebdb70ad48fe85712fd2f1b67540be0868c24fd175321d016"},
		{"accounts envelope row", "accounts", "1", accountRow,
			"d6d3e6cd26a98a1ceea324b7d5416c7e908a998b4019039f71783bd253b460bc"},
		{"time_prefs masked key", "time_prefs", "138****0001", prefRow,
			"3ef92e7a03368e21457b338a80b6b7128ed260b34b3d82f2e3290b51b3a7b783"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := CanonicalHash(tc.table, tc.key, tc.row)
			if got != tc.want {
				t.Fatalf("hash mismatch:\n got %s\nwant %s", got, tc.want)
			}
		})
	}
}

func TestCanonicalLineFormat(t *testing.T) {
	// 行序列化与 Python json.dumps(sort_keys=True) 默认分隔符逐字节一致：
	// 冒号与逗号后带空格；键按字典序。
	got := CanonicalLine(map[string]any{
		"table": "accounts", "key": "1",
		"hash": "d6d3e6cd26a98a1ceea324b7d5416c7e908a998b4019039f71783bd253b460bc",
	})
	want := `{"hash": "d6d3e6cd26a98a1ceea324b7d5416c7e908a998b4019039f71783bd253b460bc", "key": "1", "table": "accounts"}`
	if got != want {
		t.Fatalf("line mismatch:\n got %s\nwant %s", got, want)
	}
}

func TestMaskPhoneRule(t *testing.T) {
	cases := []struct{ in, want string }{
		{"13800000001", "138****0001"},
		{"138****0001", "138****0001"}, // 已掩码原样
		{"not-a-phone", "not-a-phone"}, // 非手机号原样
		{"23800000001", "23800000001"}, // 非 1 开头原样
	}
	for _, tc := range cases {
		if got := MaskPhone(tc.in); got != tc.want {
			t.Fatalf("MaskPhone(%q) = %q, want %q", tc.in, got, tc.want)
		}
	}
}
