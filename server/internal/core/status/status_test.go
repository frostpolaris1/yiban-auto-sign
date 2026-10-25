package status

import "testing"

func TestTaskState(t *testing.T) {
	for _, s := range []TaskState{TaskPending, TaskRunning, TaskSuccess, TaskFailed, TaskSkipped} {
		if !s.Valid() {
			t.Fatalf("%q should be valid", s)
		}
	}
	if TaskState("done").Valid() {
		t.Fatal("unknown state must be invalid")
	}
	if TaskPending.Terminal() || TaskRunning.Terminal() {
		t.Fatal("pending/running must not be terminal")
	}
	for _, s := range []TaskState{TaskSuccess, TaskFailed, TaskSkipped} {
		if !s.Terminal() {
			t.Fatalf("%q must be terminal", s)
		}
	}
}
