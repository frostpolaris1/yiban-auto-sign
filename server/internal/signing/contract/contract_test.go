package contract

import (
	"errors"
	"testing"
)

func TestOutcomeValid(t *testing.T) {
	all := []Outcome{
		OutcomeSuccess, OutcomeAlreadyDone, OutcomePendingUpstream,
		OutcomeNoTarget, OutcomeInvalidTarget, OutcomeCredentialBad,
		OutcomeDeviceUnbound, OutcomeChallenge, OutcomeBlocked, OutcomeTransient,
		OutcomeRejected, OutcomePhotoMissing, OutcomePhotoRejected,
		OutcomeUnknown,
	}
	for _, o := range all {
		if !o.Valid() {
			t.Fatalf("%q must be valid", o)
		}
	}
	if Outcome("done").Valid() {
		t.Fatal("unknown outcome must be invalid")
	}
}

func TestUnknownIsNotSuccess(t *testing.T) {
	if OutcomeUnknown == OutcomeSuccess {
		t.Fatal("unknown must stay distinct from success")
	}
}

func TestOutcomeError(t *testing.T) {
	withMessage := &OutcomeError{Outcome: OutcomeCredentialBad, Message: "bad credentials"}
	if withMessage.Error() != "bad credentials" {
		t.Fatalf("Error() = %q", withMessage.Error())
	}
	withoutMessage := &OutcomeError{Outcome: OutcomeChallenge}
	if withoutMessage.Error() != "provider: challenge" {
		t.Fatalf("Error() = %q", withoutMessage.Error())
	}
	var target *OutcomeError
	if !errors.As(error(withMessage), &target) || target != withMessage {
		t.Fatal("errors.As must recover the OutcomeError")
	}
}
