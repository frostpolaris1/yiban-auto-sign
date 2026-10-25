// Package school 实现院校用例。
//
// # 功能
// 建校、改校、列表。列表对 master 返回全量，对 admin 只返回绑定校。
// 本包**不**实现 SQL、**不**做 HTTP 编码、**不**判管理员绑定。
//
// # 归属
// app 层用例。端口见 repo.go，实现见 adapters/pg。
// 入口层从 entry/http/school 调用本包。
//
// # 复用
// Service、Deps、Page；New、Update、Record 与状态常量来自 repo.go。
//
// # 口径
// 写动作与审计行同事务；审计追加是事务最后一条语句。
// 每次调用的第一个参数是作用域，漏传即编译失败。
// 跨校对象一律 404；动作无权 403。
package school

import (
	"context"
	"errors"
	"regexp"
	"strings"
	"unicode/utf8"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// codePattern 与数据库 ck_schools_code_shape 同形。
// 应用层先拒，错误码是 validation_failed 而不是约束名。
var codePattern = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{1,31}$`)

// Deps 是院校用例的依赖。
type Deps struct {
	Schools Repo
	Audit   appaudit.Repo
	Tx      app.TxManager
	Clock   clock.Clock
}

// Service 是院校用例服务。
type Service struct {
	deps Deps
}

// Page 是一页院校。
type Page struct {
	Items      []Record
	NextCursor string
}

// NewService 构造院校用例。缺依赖即失败，**不静默降级**。
func NewService(deps Deps) (*Service, error) {
	if deps.Schools == nil {
		return nil, errors.New("school: nil school repo")
	}
	if deps.Audit == nil {
		return nil, errors.New("school: nil audit repo")
	}
	if deps.Tx == nil {
		return nil, errors.New("school: nil transaction manager")
	}
	if deps.Clock == nil {
		return nil, errors.New("school: nil clock")
	}
	return &Service{deps: deps}, nil
}

// List 按作用域列院校。
// master 见全量；admin 只见绑定校；user 无权（403）。
func (s *Service) List(ctx context.Context, scope corescope.Scope, cursor string, limit int) (Page, error) {
	if err := app.RequireScope(scope); err != nil {
		return Page{}, err
	}
	if err := requireSchoolReader(scope); err != nil {
		return Page{}, err
	}
	limit, err := app.NormalizeLimit(limit)
	if err != nil {
		return Page{}, err
	}
	items, next, err := s.deps.Schools.List(ctx, scope, cursor, limit)
	if err != nil {
		return Page{}, err
	}
	return Page{Items: items, NextCursor: next}, nil
}

// Create 建校。**只有 master 可以建校**。
// 写行与 school.create 审计同事务。
func (s *Service) Create(ctx context.Context, scope corescope.Scope, in New, actor appaudit.Actor) (Record, error) {
	if err := app.RequireScope(scope); err != nil {
		return Record{}, err
	}
	if scope.Kind() != corescope.KindMaster {
		return Record{}, errs.New(errs.CodeForbidden, "master role is required")
	}
	if err := validateNew(in); err != nil {
		return Record{}, err
	}
	var record Record
	err := s.deps.Tx.InTx(ctx, func(ctx context.Context) error {
		var err error
		record, err = s.deps.Schools.Create(ctx, in)
		if err != nil {
			return err
		}
		_, err = s.deps.Audit.Append(ctx, coreaudit.Entry{
			TS:          s.deps.Clock.Now().UTC(),
			SchoolID:    record.ID,
			ActorUserID: actor.UserID,
			ActorRole:   actor.Role,
			ActorLabel:  actor.Label,
			Action:      coreaudit.ActionSchoolCreate,
			TargetType:  "school",
			TargetID:    record.ID,
			Detail:      map[string]any{"field": "code"},
		})
		return err
	})
	if err != nil {
		return Record{}, err
	}
	return record, nil
}

// Update 改校。**只有 master 可以改校**；院校编码不可改。
// 写行与 school.update / school.disable 审计同事务。
func (s *Service) Update(ctx context.Context, scope corescope.Scope, id string, in Update, actor appaudit.Actor) (Record, error) {
	if err := app.RequireScope(scope); err != nil {
		return Record{}, err
	}
	if scope.Kind() != corescope.KindMaster {
		return Record{}, errs.New(errs.CodeForbidden, "master role is required")
	}
	field, err := validateUpdate(in)
	if err != nil {
		return Record{}, err
	}
	var record Record
	err = s.deps.Tx.InTx(ctx, func(ctx context.Context) error {
		var err error
		record, err = s.deps.Schools.Update(ctx, id, in)
		if err != nil {
			return err
		}
		action := coreaudit.ActionSchoolUpdate
		detail := map[string]any{"field": field}
		if in.Status != nil {
			action = coreaudit.ActionSchoolUpdate
			if *in.Status == StatusDisabled {
				action = coreaudit.ActionSchoolDisable
			}
			detail = map[string]any{"field": "status", "to": string(*in.Status)}
		}
		_, err = s.deps.Audit.Append(ctx, coreaudit.Entry{
			TS:          s.deps.Clock.Now().UTC(),
			SchoolID:    record.ID,
			ActorUserID: actor.UserID,
			ActorRole:   actor.Role,
			ActorLabel:  actor.Label,
			Action:      action,
			TargetType:  "school",
			TargetID:    record.ID,
			Detail:      detail,
		})
		return err
	})
	if err != nil {
		return Record{}, err
	}
	return record, nil
}

// requireSchoolReader 拒绝 user 作用域。
func requireSchoolReader(scope corescope.Scope) error {
	switch scope.Kind() {
	case corescope.KindMaster, corescope.KindAdmin:
		return nil
	default:
		return errs.New(errs.CodeForbidden, "role cannot list schools")
	}
}

func validateNew(in New) error {
	if !codePattern.MatchString(in.Code) {
		return errs.New(errs.CodeValidation, "code must match ^[a-z0-9][a-z0-9-]{1,31}$")
	}
	if err := validateName(in.Name); err != nil {
		return err
	}
	// 时区闭集由 ck_schools_time_zone 兜底，映射为 validation_failed。
	// 用例层不复制该集合：单一事实源在迁移里。
	return nil
}

func validateUpdate(in Update) (string, error) {
	changed := 0
	field := ""
	if in.Name != nil {
		if err := validateName(*in.Name); err != nil {
			return "", err
		}
		changed++
		field = "name"
	}
	if in.Status != nil {
		if !in.Status.Valid() {
			return "", errs.New(errs.CodeValidation, "unknown school status")
		}
		changed++
		field = "status"
	}
	if in.TimeZone != nil {
		if strings.TrimSpace(*in.TimeZone) == "" {
			return "", errs.New(errs.CodeValidation, "timezone is required")
		}
		changed++
		field = "timezone"
	}
	if changed == 0 {
		return "", errs.New(errs.CodeValidation, "at least one field is required")
	}
	return field, nil
}

func validateName(name string) error {
	name = strings.TrimSpace(name)
	if utf8.RuneCountInString(name) < 1 || utf8.RuneCountInString(name) > 128 {
		return errs.New(errs.CodeValidation, "name must be 1 to 128 characters")
	}
	return nil
}
