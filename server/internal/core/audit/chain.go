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
	"time"

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

// detailKeys is the only allowed set of detail keys.
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
// The whitelist is closed: an unknown key is an error. A string value that
// changes under mask.MaskText contains a phone number or an email address and
// is rejected. encoding/json sorts map keys, so the same map always yields
// the same bytes.
func EncodeDetail(values map[string]any) (string, error) {
	if len(values) == 0 {
		return "{}", nil
	}
	for key, value := range values {
		if !detailKeys[key] {
			return "", fmt.Errorf("%w: %q", ErrDetailKey, key)
		}
		if text, ok := value.(string); ok && mask.MaskText(text) != text {
			return "", fmt.Errorf("%w: %q", ErrDetailPII, key)
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
