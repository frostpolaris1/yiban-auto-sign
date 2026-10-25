package pg

import (
	"errors"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
)

// mapError 把 PostgreSQL 失败转成稳定错误码。
//
// **这是全仓唯一读 SQLSTATE 的地方**。
// 用例与处理器只见 core/errs 码，不见驱动错误。
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

// mapInsertForeignKey 在插入点映射外键失败。
//
// mapError 保留 SQLSTATE 层面的视图：23503 是冲突。
// 但在插入处，23503 表示被引用的父行不存在，
// schema §2.4 要求报 not_found（HTTP 404）。
// 被拒的删除属 RESTRICT，PostgreSQL 在删除点报 23001，仍算冲突（409）。
// **本函数只用于插入子行的语句**。
func mapInsertForeignKey(err error) error {
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) && pgErr.Code == "23503" {
		details := map[string]any{}
		if pgErr.ConstraintName != "" {
			details["constraint"] = pgErr.ConstraintName
		}
		return &errs.Error{Code: errs.CodeNotFound, Message: "not found", Details: details}
	}
	return mapError(err)
}

// recordOrNotFound 返回值；行缺失映射为 errs.ErrNotFound，
// 其他失败映射为各自稳定码。
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

// coreerrNotFound 是共用未找到错误的简写。
func coreerrNotFound() error { return errs.ErrNotFound }

// validationError 构造校验失败错误。
func validationError(message string) error {
	return errs.New(errs.CodeValidation, message)
}

// conflictError 构造冲突失败错误。
func conflictError(message string) error {
	return errs.New(errs.CodeConflict, message)
}

// capacityError 构造容量失败错误。
// 语义：已达上限，**本次写入未生效**。
func capacityError() error {
	return errs.New(errs.CodeCapacity, "capacity exceeded")
}
