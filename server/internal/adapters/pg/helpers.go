package pg

import (
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgtype"
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

// timePtr converts a nullable timestamp column into an optional value.
func timePtr(value pgtype.Timestamptz) *time.Time {
	if !value.Valid {
		return nil
	}
	out := value.Time
	return &out
}
