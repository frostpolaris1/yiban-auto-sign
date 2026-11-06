package legacy

// parseLegacyTime 的格式判据。旧生产 users.created_at 是空格分隔、无时区的
// `YYYY-MM-DD HH:MM:SS`；三种合法形状都必须落到同一 UTC 时刻，
// 非法形状仍报错，不退化成任意字符串解析。

import (
	"testing"
	"time"
)

func TestParseLegacyTime(t *testing.T) {
	now := time.Date(2026, 11, 6, 12, 0, 0, 0, time.UTC)
	// 旧栈本地时区（legacyTZ，+08:00）的 08:00 就是 UTC 00:00。
	wantUTC := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	cases := []struct {
		name string
		in   string
	}{
		{"rfc3339 with zone", "2026-01-01T08:00:00+08:00"},
		{"t separator no zone", "2026-01-01T08:00:00"},
		{"space separator no zone", "2026-01-01 08:00:00"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := parseLegacyTime(tc.in, now)
			if err != nil {
				t.Fatalf("parseLegacyTime(%q): %v", tc.in, err)
			}
			if !got.Equal(wantUTC) {
				t.Fatalf("parseLegacyTime(%q) = %s, want %s", tc.in, got, wantUTC)
			}
			if got.Location() != time.UTC {
				t.Fatalf("parseLegacyTime(%q) location = %s, want UTC", tc.in, got.Location())
			}
		})
	}
}

func TestParseLegacyTimeEmptyUsesNow(t *testing.T) {
	now := time.Date(2026, 11, 6, 12, 0, 0, 0, time.UTC)
	got, err := parseLegacyTime("   ", now)
	if err != nil {
		t.Fatalf("empty timestamp must use now, got error: %v", err)
	}
	if !got.Equal(now) {
		t.Fatalf("empty timestamp = %s, want now %s", got, now)
	}
}

func TestParseLegacyTimeInvalid(t *testing.T) {
	now := time.Date(2026, 11, 6, 12, 0, 0, 0, time.UTC)
	for _, in := range []string{
		"2026-01-01",          // 只有日期：不接受
		"2026-01-01 08:00",    // 缺秒：不接受
		"2026-13-01 08:00:00", // 月份越界
		"not-a-timestamp",     // 任意字符串
	} {
		if _, err := parseLegacyTime(in, now); err == nil {
			t.Fatalf("parseLegacyTime(%q) accepted an invalid timestamp", in)
		}
	}
}
