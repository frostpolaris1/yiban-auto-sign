// Package audit owns the append-only audit chain.
//
// The chain is a sequence of HMAC-SHA256 hashes. Each row hashes the previous
// hash and a length-prefixed encoding of its own fields. Length prefixes make
// field boundaries unambiguous, so two different rows cannot produce the same
// payload.
//
// This package is pure. The database adapter supplies the rows and the
// serialization of the chain head.
package audit

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"regexp"
	"time"
	"unicode/utf8"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mask"
)

// Domain separates this chain from any other HMAC use.
const Domain = "yiban.audit.v1"

// MaxDetailBytes is the stored detail limit. A longer value is rejected, not
// truncated: truncation would make the stored bytes differ from the hashed
// bytes.
const MaxDetailBytes = 4096

// Action is an audited operation name. The vocabulary is the single source of
// truth for valid action values; the database CHECK validates the shape only.
type Action string

// P1 action vocabulary. P4 adds the task actions below.
const (
	ActionAuthLogin           Action = "auth.login"
	ActionAuthLoginFailed     Action = "auth.login_failed"
	ActionAuthLogout          Action = "auth.logout"
	ActionAuthPasswordReset   Action = "auth.password_reset"
	ActionUserCreate          Action = "user.create"
	ActionUserDisable         Action = "user.disable"
	ActionUserEnable          Action = "user.enable"
	ActionUserDelete          Action = "user.delete"
	ActionSchoolCreate        Action = "school.create"
	ActionSchoolUpdate        Action = "school.update"
	ActionSchoolDisable       Action = "school.disable"
	ActionAdminGrant          Action = "admin.grant"
	ActionAdminRevoke         Action = "admin.revoke"
	ActionAccountCreate       Action = "account.create"
	ActionAccountUpdate       Action = "account.update"
	ActionAccountReviewPass   Action = "account.review_approve"
	ActionAccountReviewReject Action = "account.review_reject"
	ActionAccountPause        Action = "account.pause"
	ActionAccountResume       Action = "account.resume"
	ActionAccountDelete       Action = "account.delete"
	ActionAccountRestore      Action = "account.restore"
	ActionCredentialUpdate    Action = "credential.update"
	ActionCredentialRekey     Action = "credential.rekey"
	ActionTaskReclaim         Action = "task.reclaim"
	ActionTaskClaim           Action = "task.claim"
	ActionTaskSettle          Action = "task.settle"
)

// Entry is the input of one append. Empty SchoolID, ActorUserID and ActorRole
// are stored as NULL. Detail is validated and encoded by the store.
type Entry struct {
	TS          time.Time
	SchoolID    string
	ActorUserID string
	ActorRole   coreauth.Role
	ActorLabel  string
	Action      Action
	TargetType  string
	TargetID    string
	Detail      map[string]any
}

// Row is one stored audit row. PrevHash and Hash are raw 32-byte values.
type Row struct {
	ID          int64
	TS          time.Time
	SchoolID    string
	ActorUserID string
	ActorRole   string
	ActorLabel  string
	Action      string
	TargetType  string
	TargetID    string
	Detail      string
	PrevHash    []byte
	Hash        []byte
}

// Report summarizes a verified range.
type Report struct {
	FirstID int64
	LastID  int64
	Count   int64
}

// ErrEmptyChain reports a verification over zero rows. An empty scan never
// passes: it proves nothing.
var ErrEmptyChain = errors.New("audit: no rows to verify")

// ErrBrokenChain reports a row whose stored hash or link does not match.
var ErrBrokenChain = errors.New("audit: chain is broken")

// ErrDetailKey reports a detail key outside the whitelist.
var ErrDetailKey = errors.New("audit: detail key is not allowed")

// ErrDetailPII reports a detail value that contains a phone number or an
// email address.
var ErrDetailPII = errors.New("audit: detail contains personal data")

// ErrDetailTooLarge reports a detail value over MaxDetailBytes.
var ErrDetailTooLarge = errors.New("audit: detail is too large")

// ErrDetailType reports a detail value outside the scalar whitelist.
var ErrDetailType = errors.New("audit: detail value type is not allowed")

// ErrDetailShape reports a string that does not match the shape of its key.
var ErrDetailShape = errors.New("audit: detail value shape is not allowed")

// ErrDetailReason reports a reason shorter than 1 or longer than
// MaxReasonChars runes.
var ErrDetailReason = errors.New("audit: detail reason is out of range")

// MaxReasonChars bounds the reason value. A free-text field is the only place
// where arbitrary words can reach the chain, so it gets the tightest limit.
const MaxReasonChars = 200

// Phone-shaped numbers in this inclusive range are rejected. A JSON number
// that equals an 11-digit mobile number cannot be told apart from a phone
// number, so the whole shape is refused.
const (
	phoneNumberMin = 10000000000
	phoneNumberMax = 14999999999
)

var (
	// phoneAnywhere matches an 11-digit mobile number without word
	// boundaries, so "user13800138000" and "13800138000abc" are caught too.
	phoneAnywhere = regexp.MustCompile(`1[3-9][0-9]{9}`)
	// emailAnywhere matches an email address without word boundaries.
	emailAnywhere = regexp.MustCompile(`[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}`)
)

// detailKeys is the only allowed set of detail keys.
var (
	// traceIDShape is the request id alphabet from entry/http/router.
	traceIDShape = regexp.MustCompile(`^[0-9A-Za-z._-]{1,64}$`)
	// kidShape matches the credential key id: 16 lowercase hex characters.
	kidShape = regexp.MustCompile(`^[0-9a-f]{16}$`)
	// fieldShape is a column or setting name.
	fieldShape = regexp.MustCompile(`^[a-z][a-z0-9_]{0,63}$`)
	// wordShape is a status or reason code.
	wordShape = regexp.MustCompile(`^[a-z][a-z0-9_]{0,31}$`)
)

var detailKeys = map[string]bool{
	"field":    true,
	"from":     true,
	"to":       true,
	"reason":   true,
	"count":    true,
	"kid":      true,
	"rows":     true,
	"trace_id": true,
}

// ZeroHash is the prev_hash of the first row of a new chain.
func ZeroHash() []byte { return make([]byte, sha256.Size) }

// EncodeDetail validates values and returns canonical JSON text.
//
// The key whitelist is closed: an unknown key is an error. Values are checked
// recursively. The only allowed types are nil, string, bool, signed and
// unsigned integers, floats, and []string under the "rows" key. Maps, other
// slices, structs and non-nil pointers are rejected. encoding/json sorts map
// keys, so the same map always yields the same bytes.
//
// Each key has its own shape. trace_id, kid, field, from and to are matched
// against fixed character classes, so a random 32-hex trace id is accepted and
// a phone number is not. Only reason carries free text: it must survive
// mask.MaskText, must not contain a mobile number or an email address, and is
// limited to MaxReasonChars runes. A number inside [10000000000,
// 14999999999] is rejected because it has the phone shape; rows accepts only
// integers for the same reason.
//
// Limitation: a password or a device code has no recognizable shape. This
// function cannot detect one. Callers must never pass a secret to detail. The
// key whitelist and code review are the guards; do not weaken either.
func EncodeDetail(values map[string]any) (string, error) {
	if len(values) == 0 {
		return "{}", nil
	}
	for key, value := range values {
		if !detailKeys[key] {
			return "", fmt.Errorf("%w: %q", ErrDetailKey, key)
		}
		if err := validateDetailValue(key, value); err != nil {
			return "", err
		}
	}
	encoded, err := json.Marshal(values)
	if err != nil {
		return "", fmt.Errorf("audit: encode detail: %w", err)
	}
	if len(encoded) > MaxDetailBytes {
		return "", fmt.Errorf("%w: %d bytes", ErrDetailTooLarge, len(encoded))
	}
	return string(encoded), nil
}

// validateDetailValue applies the type whitelist to one value.
func validateDetailValue(key string, value any) error {
	switch typed := value.(type) {
	case nil:
		return nil
	case string:
		return validateDetailString(key, typed)
	case bool:
		return nil
	case []int:
		if key != "rows" {
			return fmt.Errorf("%w: %q must not be a slice", ErrDetailType, key)
		}
		for _, item := range typed {
			if err := validateDetailNumber(float64(item)); err != nil {
				return err
			}
		}
		return nil
	case int:
		return validateDetailNumber(float64(typed))
	case int8:
		return validateDetailNumber(float64(typed))
	case int16:
		return validateDetailNumber(float64(typed))
	case int32:
		return validateDetailNumber(float64(typed))
	case int64:
		return validateDetailNumber(float64(typed))
	case uint:
		return validateDetailNumber(float64(typed))
	case uint8:
		return validateDetailNumber(float64(typed))
	case uint16:
		return validateDetailNumber(float64(typed))
	case uint32:
		return validateDetailNumber(float64(typed))
	case uint64:
		return validateDetailNumber(float64(typed))
	case float32:
		return validateDetailNumber(float64(typed))
	case float64:
		return validateDetailNumber(typed)
	}
	// A nil pointer encodes as null, which carries no data.
	if rv := reflect.ValueOf(value); rv.Kind() == reflect.Ptr && rv.IsNil() {
		return nil
	}
	return fmt.Errorf("%w: %q has type %T", ErrDetailType, key, value)
}

// validateDetailString applies the per-key shape or free-text rule.
func validateDetailString(key, text string) error {
	switch key {
	case "trace_id":
		if !traceIDShape.MatchString(text) {
			return fmt.Errorf("%w: %q has the wrong trace id shape", ErrDetailShape, key)
		}
		return nil
	case "kid":
		if !kidShape.MatchString(text) {
			return fmt.Errorf("%w: %q has the wrong kid shape", ErrDetailShape, key)
		}
		return nil
	case "field":
		if !fieldShape.MatchString(text) {
			return fmt.Errorf("%w: %q has the wrong field shape", ErrDetailShape, key)
		}
		return nil
	case "from", "to":
		if !wordShape.MatchString(text) {
			return fmt.Errorf("%w: %q has the wrong value shape", ErrDetailShape, key)
		}
		return nil
	case "reason":
		if utf8.RuneCountInString(text) > MaxReasonChars {
			return fmt.Errorf("%w: %q has %d characters", ErrDetailReason, key, utf8.RuneCountInString(text))
		}
	default:
		return fmt.Errorf("%w: %q has no string rule", ErrDetailKey, key)
	}
	if mask.MaskText(text) != text || phoneAnywhere.MatchString(text) || emailAnywhere.MatchString(text) {
		return fmt.Errorf("%w: %q", ErrDetailPII, key)
	}
	return nil
}

// validateDetailNumber rejects numbers that have the phone shape.
func validateDetailNumber(value float64) error {
	if value >= phoneNumberMin && value <= phoneNumberMax {
		return fmt.Errorf("%w: numeric phone shape", ErrDetailPII)
	}
	return nil
}

// Payload builds the bytes that Hash signs. The layout is frozen:
//
//	"yiban.audit.v1\0"
//	field(prev_hash) field(ts) field(school_id) field(actor_user_id)
//	field(actor_role) field(actor_label) field(action) field(target_type)
//	field(target_id) field(detail)
//	field(x) = uint32_be(len(x)) || x
func Payload(row Row) []byte {
	out := make([]byte, 0, 256)
	out = append(out, Domain...)
	out = append(out, 0)
	out = appendField(out, row.PrevHash)
	out = appendField(out, []byte(formatTime(row.TS)))
	out = appendField(out, []byte(row.SchoolID))
	out = appendField(out, []byte(row.ActorUserID))
	out = appendField(out, []byte(row.ActorRole))
	out = appendField(out, []byte(row.ActorLabel))
	out = appendField(out, []byte(row.Action))
	out = appendField(out, []byte(row.TargetType))
	out = appendField(out, []byte(row.TargetID))
	out = appendField(out, []byte(row.Detail))
	return out
}

// Hash computes the HMAC of the row payload. PrevHash and Hash fields are not
// part of the payload except prev_hash.
func Hash(key []byte, row Row) []byte {
	mac := hmac.New(sha256.New, key)
	mac.Write(Payload(row))
	return mac.Sum(nil)
}

// Verify checks link continuity and every hash in rows. The first row's
// prev_hash is the anchor and is not compared with anything: after cleanup an
// old chain legitimately starts at a non-zero anchor.
func Verify(key []byte, rows []Row) (Report, error) {
	if len(rows) == 0 {
		return Report{}, ErrEmptyChain
	}
	var prev []byte
	for i, row := range rows {
		if i > 0 && !hmac.Equal(prev, row.PrevHash) {
			return Report{}, fmt.Errorf("%w: row %d prev_hash does not match the previous hash", ErrBrokenChain, row.ID)
		}
		if !hmac.Equal(Hash(key, row), row.Hash) {
			return Report{}, fmt.Errorf("%w: row %d hash does not match its payload", ErrBrokenChain, row.ID)
		}
		prev = row.Hash
	}
	return Report{FirstID: rows[0].ID, LastID: rows[len(rows)-1].ID, Count: int64(len(rows))}, nil
}

func appendField(dst, value []byte) []byte {
	var size [4]byte
	binary.BigEndian.PutUint32(size[:], uint32(len(value)))
	dst = append(dst, size[:]...)
	return append(dst, value...)
}

// formatTime pins the timestamp form. UTC with nanoseconds and a trailing Z
// keeps the payload stable across processes.
func formatTime(ts time.Time) string {
	return ts.UTC().Format(time.RFC3339Nano)
}
