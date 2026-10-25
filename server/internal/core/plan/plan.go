// Package plan is the only definition of time-slot geometry.
package plan

import "time"

// SlotSpan is the user-selectable slot width.
const SlotSpan = 5 * time.Minute

// SlotOf maps an offset from the window start to a slot index.
// Negative offsets clamp to slot zero.
func SlotOf(offset time.Duration) int {
	if offset < 0 {
		return 0
	}
	return int(offset / SlotSpan)
}
