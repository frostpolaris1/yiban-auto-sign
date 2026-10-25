// Package audit declares the audit repository port.
//
// The chain encoding lives in core/audit so the PostgreSQL adapter never
// imports this package (canonical.tsv boundary.adapters).
package audit

import (
	"context"

	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
)

// Entry is the input of one append.
type Entry = coreaudit.Entry

// Row is one stored audit row.
type Row = coreaudit.Row

// Report summarizes a verified range.
type Report = coreaudit.Report

// Action is an audited operation name.
type Action = coreaudit.Action

// Repo is the audit repository port.
//
// Append must run inside a transaction. It takes the chain advisory lock,
// reads the current head and inserts the next row, so the chain is serialized
// and cannot fork. Append must be the last statement of a short transaction: a
// long transaction holds the chain lock and blocks every audited write.
//
// Verify checks link continuity and recomputes every hash. from and to are
// inclusive row ids; zero means unbounded. An empty range is an error, never a
// pass.
type Repo interface {
	Append(ctx context.Context, in Entry) (Row, error)
	Verify(ctx context.Context, from, to int64) (Report, error)
}
