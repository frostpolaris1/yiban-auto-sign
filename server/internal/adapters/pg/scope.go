package pg

import (
	"fmt"

	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// scopePredicate returns an SQL condition and its arguments for a scope.
//
// The three scopes produce three different statements. They are never merged
// into "($n IS NULL OR school_id = ANY($n))": an empty set and "all rows" must
// stay distinguishable. prefix is the table alias plus a dot, or empty.
//
// A user scope filters by owner_user_id. Ownership is the user's whole
// visibility: there is no user-to-school binding in P1.
func scopePredicate(s corescope.Scope, argIndex int, prefix string) (string, []any, error) {
	if !s.Valid() {
		return "", nil, fmt.Errorf("pg: invalid scope kind %d", s.Kind())
	}
	switch s.Kind() {
	case corescope.KindMaster:
		return "", nil, nil
	case corescope.KindAdmin:
		// The cast runs on the parameter, not on the column, so the
		// school_id index stays usable.
		clause := fmt.Sprintf(" AND %sschool_id = ANY($%d::text[]::uuid[])", prefix, argIndex)
		return clause, []any{s.SchoolIDs()}, nil
	case corescope.KindUser:
		clause := fmt.Sprintf(" AND %sowner_user_id = $%d::uuid", prefix, argIndex)
		return clause, []any{s.UserID()}, nil
	default:
		return "", nil, fmt.Errorf("pg: unsupported scope kind %d", s.Kind())
	}
}
