package errs

import (
	"errors"
	"fmt"
	"testing"
)

func TestFrom(t *testing.T) {
	if From(nil) != nil {
		t.Fatal("From(nil) must be nil")
	}
	if got := From(ErrNotFound); got != ErrNotFound {
		t.Fatalf("From(ErrNotFound) = %p, want the same pointer", got)
	}
	wrapped := fmt.Errorf("wrap: %w", New(CodeValidation, "bad"))
	if got := From(wrapped); got.Code != CodeValidation {
		t.Fatalf("From(wrapped).Code = %q", got.Code)
	}
	if got := From(errors.New("boom")); got.Code != CodeInternal || got.Message != "internal error" {
		t.Fatalf("From(unknown) = %#v, want internal error without the raw message", got)
	}
}

func TestErrorMessage(t *testing.T) {
	if got, want := New(CodeConflict, "busy").Error(), "conflict: busy"; got != want {
		t.Fatalf("Error() = %q, want %q", got, want)
	}
}
