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
// **不把空集合当全量**。prefix 是表别名加点，或空串。
func schoolScopePredicate(s corescope.Scope, argIndex int, prefix string) (string, []any, error) {
	if !s.Valid() {
		return "", nil, errs.New(errs.CodeInternal, "invalid scope")
	}
	switch s.Kind() {
	case corescope.KindMaster:
		return "", nil, nil
	case corescope.KindAdmin:
		clause := fmt.Sprintf(" AND %sid = ANY($%d::text[]::uuid[])", prefix, argIndex)
		return clause, []any{s.SchoolIDs()}, nil
	default:
		return "", nil, errs.New(errs.CodeForbidden, "role cannot list schools")
	}
}

// photoScopePredicate 是 photos 表的可见面，供照片读取与默认图校验共用。
//
// owner 按 owner_user_id；admin 按账号归属：照片属主在该 admin 的绑定
// 院校里有未删除账号。照片没有 school_id 列，院校面只能经 accounts
// 派生；这与 scopePredicate 的 user 面同源（所有权即可见面）。
// master 没有本谓词：入口层把 master 收敛为 owner 作用域，只读自己的
// 照片；学校默认图写入也收窄为 admin（见 PhotoRepo.PhotoForDefault）。
// prefix 是表别名加点，或空串。
func photoScopePredicate(s corescope.Scope, argIndex int, prefix string) (string, []any, error) {
	if !s.Valid() {
		return "", nil, errs.New(errs.CodeInternal, "invalid scope")
	}
	switch s.Kind() {
	case corescope.KindUser:
		clause := fmt.Sprintf(" AND %sowner_user_id = $%d::uuid", prefix, argIndex)
		return clause, []any{s.UserID()}, nil
	case corescope.KindAdmin:
		clause := fmt.Sprintf(` AND EXISTS (
			SELECT 1 FROM accounts a
			WHERE a.owner_user_id = %sowner_user_id
			  AND a.deleted_at IS NULL
			  AND a.school_id = ANY($%d::text[]::uuid[]))`, prefix, argIndex)
		return clause, []any{s.SchoolIDs()}, nil
	default:
		return "", nil, errs.New(errs.CodeInternal, "photo scope is user or admin")
	}
}

// schoolDefaultPhotoPredicate 是学校默认图读取的院校可见面。
//
// master 不限；admin 限绑定院校；user 限自己拥有未删除账号的院校。
// user 没有院校绑定表，故院校面由 accounts.owner_user_id 派生。
// **空集合仍是空集合**：admin 无绑定即看不到任何学校。
// prefix 是表别名加点，或空串。
func schoolDefaultPhotoPredicate(s corescope.Scope, argIndex int, prefix string) (string, []any, error) {
	if !s.Valid() {
		return "", nil, errs.New(errs.CodeInternal, "invalid scope")
	}
	switch s.Kind() {
	case corescope.KindMaster:
		return "", nil, nil
	case corescope.KindAdmin:
		clause := fmt.Sprintf(" AND %sid = ANY($%d::text[]::uuid[])", prefix, argIndex)
		return clause, []any{s.SchoolIDs()}, nil
	case corescope.KindUser:
		clause := fmt.Sprintf(` AND EXISTS (
			SELECT 1 FROM accounts a
			WHERE a.school_id = %sid
			  AND a.owner_user_id = $%d::uuid
			  AND a.deleted_at IS NULL)`, prefix, argIndex)
		return clause, []any{s.UserID()}, nil
	default:
		return "", nil, errs.New(errs.CodeInternal, "unsupported scope kind")
	}
}
