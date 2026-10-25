package pg

import (
	"fmt"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// scopePredicate 返回范围对应的 SQL 条件与参数。
//
// 三种范围产出三条不同语句，**绝不合并**为
// "($n IS NULL OR school_id = ANY($n))"：
// 空集合与「全部行」必须始终可分辨。prefix 是表别名加点，或空串。
//
// user 范围按 owner_user_id 过滤。
// P1 没有用户到院校的绑定，故所有权就是用户的全部可见面。
func scopePredicate(s corescope.Scope, argIndex int, prefix string) (string, []any, error) {
	if !s.Valid() {
		return "", nil, fmt.Errorf("pg: invalid scope kind %d", s.Kind())
	}
	switch s.Kind() {
	case corescope.KindMaster:
		return "", nil, nil
	case corescope.KindAdmin:
		// 类型转换作用在参数上而不是列上，
		// 这样 school_id 索引仍可用。
		clause := fmt.Sprintf(" AND %sschool_id = ANY($%d::text[]::uuid[])", prefix, argIndex)
		return clause, []any{s.SchoolIDs()}, nil
	case corescope.KindUser:
		clause := fmt.Sprintf(" AND %sowner_user_id = $%d::uuid", prefix, argIndex)
		return clause, []any{s.UserID()}, nil
	default:
		return "", nil, fmt.Errorf("pg: unsupported scope kind %d", s.Kind())
	}
}

// schoolScopePredicate 是 scopePredicate 的院校表版本。
//
// schools 表的作用域列是 id 本身，不是 school_id。
// 只接受 master 与 admin：user 作用域没有院校可见面，
// **不把空集合当全量**。
func schoolScopePredicate(s corescope.Scope, argIndex int) (string, []any, error) {
	if !s.Valid() {
		return "", nil, errs.New(errs.CodeInternal, "invalid scope")
	}
	switch s.Kind() {
	case corescope.KindMaster:
		return "", nil, nil
	case corescope.KindAdmin:
		clause := fmt.Sprintf(" AND id = ANY($%d::text[]::uuid[])", argIndex)
		return clause, []any{s.SchoolIDs()}, nil
	default:
		return "", nil, errs.New(errs.CodeForbidden, "role cannot list schools")
	}
}
