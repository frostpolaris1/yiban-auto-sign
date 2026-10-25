// Package clock is the single source of the wall clock and business time.
//
// No other package may call time.Now or load a timezone. Tests inject Fixed.
package clock

import "time"

// Clock reads the current instant. Production uses System; tests use Fixed.
type Clock interface {
	Now() time.Time
}

// System reads the real wall clock.
type System struct{}

// Now returns the current instant.
func (System) Now() time.Time { return time.Now() }

// Fixed returns a fixed instant. It is the only clock tests should use.
type Fixed struct {
	T time.Time
}

// Now returns the fixed instant.
func (f Fixed) Now() time.Time { return f.T }

// BusinessDay formats the business day (YYYY-MM-DD) in loc.
// A nil location means UTC, which keeps the function total.
func BusinessDay(now time.Time, loc *time.Location) string {
	if loc == nil {
		loc = time.UTC
	}
	return now.In(loc).Format("2006-01-02")
}

// FromUnix converts platform epoch seconds to an instant in loc.
// A nil location means UTC.
func FromUnix(sec int64, loc *time.Location) time.Time {
	if loc == nil {
		loc = time.UTC
	}
	return time.Unix(sec, 0).In(loc)
}

// LoadLocation is the single entry for IANA timezone names.
func LoadLocation(name string) (*time.Location, error) {
	return time.LoadLocation(name)
}
