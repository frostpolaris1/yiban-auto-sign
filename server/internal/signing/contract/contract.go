// Package contract is the fixed v1 interface between the host framework and a
// platform provider. Providers must not import the host, touch storage or log
// credentials.
package contract

import (
	"context"
	"time"
)

// Provider is the only platform-specific seam.
type Provider interface {
	ID() string
	Capabilities() Capabilities
	DiscoverWindow(ctx context.Context, req DiscoverRequest) (Window, error)
	DiscoverTargets(ctx context.Context, req DiscoverRequest) ([]Target, error)
	Verify(ctx context.Context, req VerifyRequest) (Profile, error)
	Sign(ctx context.Context, req SignRequest) (Result, error)
}

// AccountRef is a host-owned account identity.
type AccountRef struct {
	ID       string
	SchoolID string
	Platform string
}

// Credential is valid for one call only. Providers must not retain or log it.
type Credential struct {
	Phone      string
	Password   string
	DeviceCode string
	PhoneModel string
}

// WindowSource records where a window came from.
type WindowSource string

const (
	WindowPlatform WindowSource = "platform"
	WindowCache    WindowSource = "cache"
	WindowFallback WindowSource = "fallback"
)

// Window is one sign-in window.
type Window struct {
	Start  time.Time
	End    time.Time
	Source WindowSource
	Raw    map[string]any
}

// Point is a WGS84 coordinate.
type Point struct {
	Lng float64
	Lat float64
}

// Target is one signable location.
type Target struct {
	ID             string
	Title          string
	Address        string
	Polygon        []Point
	Center         Point
	RadiusM        int
	RequiresPhoto  bool
	AttachmentName string
	Raw            map[string]any
}

// LocationMode selects automatic sampling or a fixed point.
type LocationMode string

const (
	LocationAuto  LocationMode = "auto"
	LocationFixed LocationMode = "fixed"
)

// Location is the host's location policy for one attempt.
type Location struct {
	Mode   LocationMode
	Lng    float64
	Lat    float64
	Reason string
}

// ImageRef is a prepared image. The host rotates, crops and re-encodes before
// passing it; the provider only uploads and submits it.
type ImageRef struct {
	ID     string
	Mime   string
	Width  int
	Height int
	SHA256 string
	Bytes  []byte
}

// DiscoverRequest asks a provider for the window or targets of one account.
type DiscoverRequest struct {
	Account    AccountRef
	Credential Credential
}

// VerifyRequest asks a provider to validate one credential set.
type VerifyRequest struct {
	Account    AccountRef
	Credential Credential
}

// Profile is the non-secret identity a provider may return.
type Profile struct {
	DisplayName string
	SchoolName  string
	StudentID   string
	Raw         map[string]any
}

// SignRequest is one attempt.
type SignRequest struct {
	AttemptID  string
	Account    AccountRef
	Credential Credential
	Window     Window
	Target     Target
	Location   Location
	Image      *ImageRef
	Options    map[string]string
}

// Outcome is the neutral result vocabulary.
type Outcome string

const (
	OutcomeSuccess         Outcome = "success"
	OutcomeAlreadyDone     Outcome = "already_done"
	OutcomePendingUpstream Outcome = "pending_upstream"
	OutcomeNoTarget        Outcome = "no_target"
	OutcomeInvalidTarget   Outcome = "invalid_target"
	OutcomeCredentialBad   Outcome = "credential_invalid"
	OutcomeDeviceUnbound   Outcome = "device_unbound"
	OutcomeChallenge       Outcome = "challenge"
	OutcomeTransient       Outcome = "transient"
	OutcomeRejected        Outcome = "rejected"
	OutcomePhotoMissing    Outcome = "photo_missing"
	OutcomePhotoRejected   Outcome = "photo_rejected"
	OutcomeUnknown         Outcome = "unknown"
)

// Valid reports whether o is a known outcome.
func (o Outcome) Valid() bool {
	switch o {
	case OutcomeSuccess, OutcomeAlreadyDone, OutcomePendingUpstream,
		OutcomeNoTarget, OutcomeInvalidTarget, OutcomeCredentialBad,
		OutcomeDeviceUnbound, OutcomeChallenge, OutcomeTransient,
		OutcomeRejected, OutcomePhotoMissing, OutcomePhotoRejected,
		OutcomeUnknown:
		return true
	default:
		return false
	}
}

// Evidence is the audit-safe result detail. Raw must already be masked.
type Evidence struct {
	Rank        *int
	SignedAt    *time.Time
	Receipt     string
	PhotoID     string
	PhotoSHA256 string
	Raw         map[string]any
}

// RetryAdvice tells the host whether and when to retry.
type RetryAdvice struct {
	Retryable    bool
	After        time.Duration
	MaxAttempts  int
	ClearSession bool
}

// Result is one attempt's outcome.
type Result struct {
	Outcome  Outcome
	Evidence Evidence
	Retry    RetryAdvice
	Message  string
}

// Capabilities lets the host adapt its UI and scheduling.
type Capabilities struct {
	WindowDiscovery bool
	TargetDiscovery bool
	ImageRequired   bool
	ManualCaptcha   bool
	DeviceBinding   bool
	FixedLocation   bool
	Rank            bool
}
