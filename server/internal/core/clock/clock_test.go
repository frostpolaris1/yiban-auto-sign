package clock

import (
	"testing"
	"time"
)

func TestFixed(t *testing.T) {
	want := time.Date(2026, 10, 25, 0, 0, 0, 0, time.UTC)
	if got := (Fixed{T: want}).Now(); !got.Equal(want) {
		t.Fatalf("Fixed.Now() = %v, want %v", got, want)
	}
}

func TestBusinessDay(t *testing.T) {
	loc, err := LoadLocation("Asia/Shanghai")
	if err != nil {
		t.Fatalf("LoadLocation: %v", err)
	}
	// 2026-10-24 16:30 UTC 即上海 2026-10-25 00:30。
	now := time.Date(2026, 10, 24, 16, 30, 0, 0, time.UTC)
	if got, want := BusinessDay(now, loc), "2026-10-25"; got != want {
		t.Fatalf("BusinessDay = %q, want %q", got, want)
	}
	if got, want := BusinessDay(now, nil), "2026-10-24"; got != want {
		t.Fatalf("BusinessDay(nil) = %q, want %q", got, want)
	}
}

func TestFromUnix(t *testing.T) {
	loc, err := LoadLocation("Asia/Shanghai")
	if err != nil {
		t.Fatalf("LoadLocation: %v", err)
	}
	got := FromUnix(0, loc)
	if want := time.Date(1970, 1, 1, 8, 0, 0, 0, loc); !got.Equal(want) {
		t.Fatalf("FromUnix(0) = %v, want %v", got, want)
	}
}
