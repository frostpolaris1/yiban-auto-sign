package pg

import (
	"errors"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
)

// mapError turns a PostgreSQL failure into a stable error code.
//
// It is the only place that reads SQLSTATE. Use cases and handlers see
// core/errs codes and never a driver error.
func mapError(err error) error {
	if err == nil {
		return nil
	}
	if errors.Is(err, pgx.ErrNoRows) {
		return errs.ErrNotFound
	}
	var mapped *errs.Error
	if errors.As(err, &mapped) {
		return err
	}
	var pgErr *pgconn.PgError
	if !errors.As(err, &pgErr) {
		return err
	}
	details := map[string]any{}
	if pgErr.ConstraintName != "" {
		details["constraint"] = pgErr.ConstraintName
	}
	switch pgErr.Code {
	case "23505": // unique_violation
		return &errs.Error{Code: errs.CodeConflict, Message: "conflict", Details: details}
	case "23503", "23001": // foreign_key_violation, restrict_violation
		return &errs.Error{Code: errs.CodeConflict, Message: "conflict", Details: details}
	case "23502", "23514", "22P02", "22001", "22003": // not_null, check, bad text, too long, out of range
		return &errs.Error{Code: errs.CodeValidation, Message: "validation failed", Details: details}
	case "55P03", "57014", "40001", "40P01": // lock timeout, cancel, serialization, deadlock
		return &errs.Error{Code: errs.CodeUpstream, Message: "database is busy", Details: details}
	default:
		return err
	}
}

// recordOrNotFound returns value, or maps a missing row to errs.ErrNotFound
// and any other failure to its stable code.
func recordOrNotFound[T any](value T, err error) (T, error) {
	if err == nil {
		return value, nil
	}
	var zero T
	if errors.Is(err, pgx.ErrNoRows) {
		return zero, errs.ErrNotFound
	}
	return zero, mapError(err)
}

// coreerrNotFound is shorthand for the shared not-found error.
func coreerrNotFound() error { return errs.ErrNotFound }

// validationError builds a validation failure with an optional constraint name.
func validationError(message string) error {
	return errs.New(errs.CodeValidation, message)
}

// conflictError builds a conflict failure.
func conflictError(message string) error {
	return errs.New(errs.CodeConflict, message)
}

// capacityError builds the capacity failure. It means: the limit is reached,
// the write did nothing.
func capacityError() error {
	return errs.New(errs.CodeCapacity, "capacity exceeded")
}
