// Package retry is the only definition of retry budgets.
package retry

import "time"

// Policy is one retry budget.
type Policy struct {
	MaxAttempts  int
	After        time.Duration
	ClearSession bool
}

// New builds a policy, clamping the attempt count to at least one.
func New(max int, after time.Duration) Policy {
	if max < 1 {
		max = 1
	}
	return Policy{MaxAttempts: max, After: after}
}
