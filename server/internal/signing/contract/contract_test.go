package contract

import "testing"

func TestOutcomeValid(t *testing.T) {
	all := []Outcome{
		OutcomeSuccess, OutcomeAlreadyDone, OutcomePendingUpstream,
		OutcomeNoTarget, OutcomeInvalidTarget, OutcomeCredentialBad,
		OutcomeDeviceUnbound, OutcomeChallenge, OutcomeTransient,
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
