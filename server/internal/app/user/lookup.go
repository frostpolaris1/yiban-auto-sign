// 用户受限查询用例（归属选择器）。
//
// # 功能
// 只实现 GET /api/users/lookup 对应的读取动作：校验 q 与 limit、
// 转交仓储、掩码邮箱、返回引用列表。本包**不**做 HTTP 编码、
// **不**写审计（读端点与 List 同口径不写审计）。
//
// # 归属
// app 层用例。端口见 repo.go，实现见 adapters/pg。
//
// # 口径
// master 与 admin 可调；user 403。q 必填，1..254 字符；
// 空串与全空白 400。只回未删行，按 email 升序取前 limit 条。
// 出口只给 id、掩码邮箱（core/mask.MaskEmail）与状态；
// 完整邮箱、角色与时间戳不出本用例。

package user

import (
	"context"
	"strings"
	"unicode/utf8"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mask"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// LookupDefaultLimit 是未给 limit 时的页大小。
// 选择器场景翻页无意义，页比 List 的缺省 50 更小。
const LookupDefaultLimit = 20

// LookupRef 是归属选择器的一行引用。
type LookupRef struct {
	ID          string
	EmailMasked string
	Status      coreauth.Status
}

// Lookup 查一页归属候选。**只有 master 与 admin 可以调用**。
func (s *Service) Lookup(ctx context.Context, scope corescope.Scope, query string, limit int) ([]LookupRef, error) {
	if err := app.RequireScope(scope); err != nil {
		return nil, err
	}
	if scope.Kind() != corescope.KindMaster && scope.Kind() != corescope.KindAdmin {
		return nil, errs.New(errs.CodeForbidden, "master or admin role is required")
	}
	trimmed := strings.TrimSpace(query)
	if trimmed == "" {
		return nil, errs.New(errs.CodeValidation, "q is required")
	}
	if utf8.RuneCountInString(query) > maxQueryChars {
		return nil, errs.New(errs.CodeValidation, "q is too long")
	}
	if limit == 0 {
		limit = LookupDefaultLimit
	}
	limit, err := app.NormalizeLimit(limit)
	if err != nil {
		return nil, err
	}
	records, err := s.deps.Users.Lookup(ctx, query, limit)
	if err != nil {
		return nil, err
	}
	refs := make([]LookupRef, 0, len(records))
	for _, record := range records {
		refs = append(refs, LookupRef{
			ID:          record.ID,
			EmailMasked: mask.MaskEmail(record.Email),
			Status:      record.Status,
		})
	}
	return refs, nil
}
