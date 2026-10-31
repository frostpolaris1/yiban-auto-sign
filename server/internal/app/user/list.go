// 列出用户用例。
//
// # 功能
// 只实现 GET /api/users 对应的读取动作：校验过滤条件、转交仓储、返回一页。
// 本包**不**做 HTTP 编码、**不**写审计、**不**改任何行。
//
// # 归属
// app 层用例。端口见 repo.go，实现见 adapters/pg。
// 入口层从 entry/http/user 调用本包。
//
// # 复用
// Service、ListFilter、Page；分页口径来自 app.PageLimit 与 app.NormalizeLimit。
//
// # 口径
// 只有 master 可以读用户列表（契约 x-roles: [master]）。其余角色一律 403。
// 过滤条件是**与**关系；空串表示不过滤。
// q 按邮箱前缀匹配，**不**按姓名：users 表没有姓名字段，响应也不含姓名。
// 已软删的行不出现在任何一页。

package user

import (
	"context"
	"unicode/utf8"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// maxQueryChars 限制邮箱前缀的长度。
// 254 是邮箱地址上限；更长的前缀不可能命中任何行。
const maxQueryChars = 254

// ListFilter 是列表的可选过滤条件。
// 空串表示不过滤；它们与软删过滤是**与**关系。
type ListFilter struct {
	Role   coreauth.Role
	Status coreauth.Status
	Query  string
	// IncludeDeleted 置真时软删行也入选。缺省 false，行为不变。
	IncludeDeleted bool
}

// Page 是一页用户。
type Page struct {
	Items      []Record
	NextCursor string
}

// List 列一页用户。**只有 master 可以调用**。
//
// 非法角色或状态是 validation_failed，不静默夹到默认值。
// 游标由仓储解析；格式错误同样报 validation_failed。
// 默认滤掉软删行；IncludeDeleted 置真时软删行也回。
func (s *Service) List(ctx context.Context, scope corescope.Scope, filter ListFilter, cursor string, limit int) (Page, error) {
	if err := app.RequireScope(scope); err != nil {
		return Page{}, err
	}
	if scope.Kind() != corescope.KindMaster {
		return Page{}, errs.New(errs.CodeForbidden, "master role is required")
	}
	if filter.Role != "" && !filter.Role.Valid() {
		return Page{}, errs.New(errs.CodeValidation, "role must be master, admin or user")
	}
	if filter.Status != "" && !filter.Status.Valid() {
		return Page{}, errs.New(errs.CodeValidation, "status must be active or disabled")
	}
	if utf8.RuneCountInString(filter.Query) > maxQueryChars {
		return Page{}, errs.New(errs.CodeValidation, "q is too long")
	}
	limit, err := app.NormalizeLimit(limit)
	if err != nil {
		return Page{}, err
	}
	items, next, err := s.deps.Users.List(ctx, filter.Role, filter.Status, filter.Query, cursor, limit, filter.IncludeDeleted)
	if err != nil {
		return Page{}, err
	}
	return Page{Items: items, NextCursor: next}, nil
}
