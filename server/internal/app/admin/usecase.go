// Package admin 实现管理员绑定与作用域解析。
//
// # 功能
// 绑定、解绑、按院校列管理员；把会话主体解析成院校作用域。
// 本包**不**实现 SQL、**不**签发会话、**不**判院校状态。
//
// # 归属
// app 层用例。端口在本文件声明，实现见 adapters/pg。
// 入口层的三个域都用本包解析作用域，故范围口径只有一份。
//
// # 复用
// Service、Deps、Page、Authorize；Binding 转发 core/school。
//
// # 口径
// 只有 master 可以绑定或解绑管理员。
// admin 的作用域每次请求从 admin_schools 读，P1 不缓存。
// 绑定幂等；解绑不存在的绑定报 not_found。
package admin

import (
	"context"
	"errors"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coreschool "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/school"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
)

// Binding 是一行 admin_schools 记录，附带用户邮箱与角色。
type Binding = coreschool.AdminBinding

// Repo 是管理员绑定仓储端口。
type Repo interface {
	Grant(ctx context.Context, binding Binding) error
	Revoke(ctx context.Context, userID, schoolID string) error
	SchoolsForUser(ctx context.Context, userID string) ([]string, error)
	ListBySchool(ctx context.Context, schoolID, cursor string, limit int) ([]Binding, string, error)
	GetBinding(ctx context.Context, userID, schoolID string) (Binding, error)
}

// UserReader 读用户行。只用到按 id 取一条。
type UserReader interface {
	GetByID(ctx context.Context, id string) (coreuser.Record, error)
}

// SchoolReader 读院校行。只用到按 id 取一条。
type SchoolReader interface {
	Get(ctx context.Context, id string) (coreschool.Record, error)
}

// Deps 是管理员用例的依赖。
type Deps struct {
	Admins  Repo
	Users   UserReader
	Schools SchoolReader
	Audit   appaudit.Repo
	Tx      app.TxManager
	Clock   clock.Clock
}

// Service 是管理员用例服务。
type Service struct {
	deps Deps
}

// Page 是一页管理员绑定。
type Page struct {
	Items      []Binding
	NextCursor string
}

// NewService 构造管理员用例。缺依赖即失败。
func NewService(deps Deps) (*Service, error) {
	if deps.Admins == nil {
		return nil, errors.New("admin: nil binding repo")
	}
	if deps.Users == nil {
		return nil, errors.New("admin: nil user reader")
	}
	if deps.Schools == nil {
		return nil, errors.New("admin: nil school reader")
	}
	if deps.Audit == nil {
		return nil, errors.New("admin: nil audit repo")
	}
	if deps.Tx == nil {
		return nil, errors.New("admin: nil transaction manager")
	}
	if deps.Clock == nil {
		return nil, errors.New("admin: nil clock")
	}
	return &Service{deps: deps}, nil
}

// Authorize 把已认证主体解析成作用域与审计主体。
// admin 的绑定校**当场**读库；读失败即请求失败。
func (s *Service) Authorize(ctx context.Context, user coreauth.User) (corescope.Scope, appaudit.Actor, error) {
	actor := appaudit.NewActor(user)
	if user.Status != coreauth.StatusActive {
		return corescope.Scope{}, actor, errs.New(errs.CodeUnauthorized, "authentication required")
	}
	switch user.Role {
	case coreauth.RoleMaster:
		return corescope.Master(), actor, nil
	case coreauth.RoleAdmin:
		ids, err := s.deps.Admins.SchoolsForUser(ctx, user.ID)
		if err != nil {
			return corescope.Scope{}, actor, err
		}
		return corescope.Admin(ids), actor, nil
	case coreauth.RoleUser:
		return corescope.User(user.ID), actor, nil
	default:
		return corescope.Scope{}, actor, errs.New(errs.CodeForbidden, "unknown role")
	}
}

// List 列出绑定到某院校的管理员。**只有 master 可以读**。
func (s *Service) List(ctx context.Context, scope corescope.Scope, schoolID, cursor string, limit int) (Page, error) {
	if err := app.RequireScope(scope); err != nil {
		return Page{}, err
	}
	if scope.Kind() != corescope.KindMaster {
		return Page{}, errs.New(errs.CodeForbidden, "master role is required")
	}
	if _, err := s.deps.Schools.Get(ctx, schoolID); err != nil {
		return Page{}, err
	}
	limit, err := app.NormalizeLimit(limit)
	if err != nil {
		return Page{}, err
	}
	items, next, err := s.deps.Admins.ListBySchool(ctx, schoolID, cursor, limit)
	if err != nil {
		return Page{}, err
	}
	return Page{Items: items, NextCursor: next}, nil
}

// Grant 把一个已存在且角色为 admin 或 master 的用户绑到院校。
// 绑定幂等；写行与 admin.grant 审计同事务。
func (s *Service) Grant(ctx context.Context, scope corescope.Scope, schoolID, userID string, actor appaudit.Actor) (Binding, error) {
	if err := app.RequireScope(scope); err != nil {
		return Binding{}, err
	}
	if scope.Kind() != corescope.KindMaster {
		return Binding{}, errs.New(errs.CodeForbidden, "master role is required")
	}
	if _, err := s.deps.Schools.Get(ctx, schoolID); err != nil {
		return Binding{}, err
	}
	target, err := s.deps.Users.GetByID(ctx, userID)
	if err != nil {
		return Binding{}, err
	}
	if target.Role != coreauth.RoleAdmin && target.Role != coreauth.RoleMaster {
		return Binding{}, errs.New(errs.CodeValidation, "user role must be admin or master")
	}
	var binding Binding
	err = s.deps.Tx.InTx(ctx, func(ctx context.Context) error {
		if err := s.deps.Admins.Grant(ctx, Binding{
			UserID:    userID,
			SchoolID:  schoolID,
			GrantedBy: actor.UserID,
		}); err != nil {
			return err
		}
		var err error
		binding, err = s.deps.Admins.GetBinding(ctx, userID, schoolID)
		if err != nil {
			return err
		}
		_, err = s.deps.Audit.Append(ctx, coreaudit.Entry{
			TS:          s.deps.Clock.Now().UTC(),
			SchoolID:    schoolID,
			ActorUserID: actor.UserID,
			ActorRole:   actor.Role,
			ActorLabel:  actor.Label,
			Action:      coreaudit.ActionAdminGrant,
			TargetType:  "user",
			TargetID:    userID,
			Detail:      map[string]any{"field": "user_id"},
		})
		return err
	})
	if err != nil {
		return Binding{}, err
	}
	return binding, nil
}

// Revoke 解绑。无绑定可删时报 not_found。
// 删除与 admin.revoke 审计同事务。
func (s *Service) Revoke(ctx context.Context, scope corescope.Scope, schoolID, userID string, actor appaudit.Actor) error {
	if err := app.RequireScope(scope); err != nil {
		return err
	}
	if scope.Kind() != corescope.KindMaster {
		return errs.New(errs.CodeForbidden, "master role is required")
	}
	return s.deps.Tx.InTx(ctx, func(ctx context.Context) error {
		if err := s.deps.Admins.Revoke(ctx, userID, schoolID); err != nil {
			return err
		}
		_, err := s.deps.Audit.Append(ctx, coreaudit.Entry{
			TS:          s.deps.Clock.Now().UTC(),
			SchoolID:    schoolID,
			ActorUserID: actor.UserID,
			ActorRole:   actor.Role,
			ActorLabel:  actor.Label,
			Action:      coreaudit.ActionAdminRevoke,
			TargetType:  "user",
			TargetID:    userID,
			Detail:      map[string]any{"field": "user_id"},
		})
		return err
	})
}
