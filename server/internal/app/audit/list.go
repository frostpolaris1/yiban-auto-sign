// 本文件实现审计只读列表用例（2ws A）。
//
// 功能：按 action/actor/from/to 筛选审计行，游标分页，出口脱敏。
// 归属：app 层用例；端口 Lister 由 adapters/pg 实现。
// 口径：actor 过滤值先按写入口径掩码再匹配；detail 出口过 mask.MaskText；
// 不校验链、不导出、不返回 target 字段。
// 旧线 parity：master 与 admin 看同一份全局列表，不按院校收窄。

package audit

import (
	"context"
	"errors"
	"strings"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mask"
)

// LogRow 是一行审计记录（core/audit 的只读视图）。
type LogRow = coreaudit.LogRow

// LogQuery 是交给仓储的已定型条件。Actor 已掩码。
type LogQuery = coreaudit.LogQuery

// ListFilter 是列表筛选条件。Actor 是客户端原值，服务层负责掩码。
// From 与 To 是闭区间时间界。
type ListFilter struct {
	Action string
	Actor  string
	From   *time.Time
	To     *time.Time
	Cursor string
	Limit  int
}

// LogPage 是一页审计记录。
type LogPage struct {
	Items      []LogRow
	NextCursor string
}

// Lister 是审计列表读取端口。
// 实现按 (ts DESC, id DESC) 排序，键集分页，返回末页游标（末页为空串）。
// from/to 闭区间，nil 表示不设该侧界。
type Lister interface {
	ListLogs(ctx context.Context, query LogQuery) ([]LogRow, string, error)
}

// Reader 是审计只读列表用例。
type Reader struct {
	lister Lister
}

// NewReader 构造读取器。缺端口即失败。
func NewReader(lister Lister) (*Reader, error) {
	if lister == nil {
		return nil, errors.New("audit: nil log lister")
	}
	return &Reader{lister: lister}, nil
}

// List 返回一页审计记录。
// action 只做长度上界；未知动作返回空页，不报错。
// actor 按写入口径掩码，故完整邮箱匹配其存储的掩码标签。
func (r *Reader) List(ctx context.Context, filter ListFilter) (LogPage, error) {
	limit, err := app.NormalizeLimit(filter.Limit)
	if err != nil {
		return LogPage{}, err
	}
	action := strings.TrimSpace(filter.Action)
	if len(action) > 128 {
		return LogPage{}, errs.New(errs.CodeValidation, "action is too long")
	}
	rawActor := strings.TrimSpace(filter.Actor)
	if len(rawActor) > 254 {
		return LogPage{}, errs.New(errs.CodeValidation, "actor is too long")
	}
	if filter.From != nil && filter.To != nil && filter.To.Before(*filter.From) {
		return LogPage{}, errs.New(errs.CodeValidation, "to must not be before from")
	}
	items, next, err := r.lister.ListLogs(ctx, LogQuery{
		Action: action,
		Actor:  FilterLabel(rawActor),
		From:   filter.From,
		To:     filter.To,
		Cursor: filter.Cursor,
		Limit:  limit,
	})
	if err != nil {
		return LogPage{}, err
	}
	// detail 在用例出口统一过掩码：写入时的白名单已挡 PII，
	// 这里防的是历史行与未来动作的漏网。
	for i := range items {
		items[i].Detail = mask.MaskText(items[i].Detail)
	}
	return LogPage{Items: items, NextCursor: next}, nil
}
