package diagnostic

// SuggestionFor 的表驱动判据（P6）。
// 词表封闭：wait|retry_now|re_arm|contact_admin；
// 未知状态必须仍落在词表内，不得外溢。

import (
	"testing"
	"time"

	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

var now = time.Date(2026, 10, 29, 7, 0, 0, 0, time.FixedZone("CST", 8*3600))

func openWin() *WindowInfo {
	return &WindowInfo{Start: now.Add(-time.Hour), End: now.Add(time.Hour)}
}

func closedWin() *WindowInfo {
	return &WindowInfo{Start: now.Add(-3 * time.Hour), End: now.Add(-time.Hour)}
}

func TestSuggestionVocabulary(t *testing.T) {
	cases := []struct {
		state  string
		result string
		win    *WindowInfo
		want   string
	}{
		{string(corestatus.TaskFailed), "credential_invalid", openWin(), SuggestContactAdmin},
		{string(corestatus.TaskFailed), "credential_invalid", nil, SuggestContactAdmin},
		{string(corestatus.TaskFailed), "rejected", openWin(), SuggestRetryNow},
		{string(corestatus.TaskFailed), "rejected", closedWin(), SuggestReArm},
		{string(corestatus.TaskFailed), "rejected", nil, SuggestReArm},
		{string(corestatus.TaskPending), "wait_window_open", openWin(), SuggestWait},
		// 状态先于结论词：排队中的行可能带着上次失败的结论词
		//（RequeueRetry 不擦 result），建议仍是 wait。
		{string(corestatus.TaskPending), "credential_invalid", openWin(), SuggestWait},
		{string(corestatus.TaskPending), string(contract.OutcomeRejected), closedWin(), SuggestWait},
		{string(corestatus.TaskRunning), "", openWin(), SuggestWait},
		{string(corestatus.TaskSuccess), string(contract.OutcomeSuccess), openWin(), SuggestWait},
		{string(corestatus.TaskSkipped), "window_closed", closedWin(), SuggestWait},
	}
	for _, c := range cases {
		got := SuggestionFor(c.state, c.result, c.win, now)
		if got != c.want {
			t.Fatalf("SuggestionFor(%s,%s,%v) = %q, want %q", c.state, c.result, c.win, got, c.want)
		}
	}
}

// 未知状态不是合法输入：映射必须给出词表内的词而不是空串或任意词。
func TestSuggestionUnknownStateStillInVocabulary(t *testing.T) {
	vocab := map[string]bool{
		SuggestWait: true, SuggestRetryNow: true,
		SuggestReArm: true, SuggestContactAdmin: true,
	}
	for _, state := range []string{"", "unknown", "PENDING"} {
		if got := SuggestionFor(state, "", nil, now); !vocab[got] {
			t.Fatalf("state %q 得到词表外的 %q", state, got)
		}
	}
}
