package retry

import (
	"testing"
	"time"
)

func TestNewClamps(t *testing.T) {
	if got := New(0, time.Second).MaxAttempts; got != 1 {
		t.Fatalf("MaxAttempts = %d, want 1", got)
	}
	if got := New(3, 2*time.Second); got.MaxAttempts != 3 || got.After != 2*time.Second {
		t.Fatalf("policy = %#v", got)
	}
}
