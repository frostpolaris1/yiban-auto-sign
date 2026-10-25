package plan

import (
	"testing"
	"time"
)

func TestSlotOf(t *testing.T) {
	cases := []struct {
		in   time.Duration
		want int
	}{
		{-time.Second, 0},
		{0, 0},
		{4*time.Minute + 59*time.Second, 0},
		{5 * time.Minute, 1},
		{10*time.Minute + 30*time.Second, 2},
	}
	for _, c := range cases {
		if got := SlotOf(c.in); got != c.want {
			t.Fatalf("SlotOf(%v) = %d, want %d", c.in, got, c.want)
		}
	}
}
