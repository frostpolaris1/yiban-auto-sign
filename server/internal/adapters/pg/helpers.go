package pg

import (
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgtype"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
)

// newID returns a UUIDv7. The application supplies ids explicitly so a row id
// is known before the insert; the database default only serves manual
// operations.
func newID() (string, error) {
	id, err := uuid.NewV7()
	if err != nil {
		return "", fmt.Errorf("pg: generate uuid: %w", err)
	}
	return id.String(), nil
}

// tokenHashFromBytes converts a scanned bytea into a TokenHash. The column
// CHECK guarantees 32 bytes; a different length means the schema and the code
// disagree, so it is an error, not a silent zero.
func tokenHashFromBytes(raw []byte) (coreauth.TokenHash, error) {
	if len(raw) != 32 {
		return coreauth.TokenHash{}, fmt.Errorf("pg: token hash is %d bytes, want 32", len(raw))
	}
	var hash coreauth.TokenHash
	copy(hash[:], raw)
	return hash, nil
}

// timePtr converts a nullable timestamp column into an optional value.
func timePtr(value pgtype.Timestamptz) *time.Time {
	if !value.Valid {
		return nil
	}
	out := value.Time
	return &out
}
