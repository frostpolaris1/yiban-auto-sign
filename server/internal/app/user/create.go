// 创建用户用例。
//
// # 功能
// 只实现 POST /api/users 对应的建号动作：校验输入、算口令哈希、写行与审计。
// 本包**不**做 HTTP 编码、**不**签发会话、**不**发通知。
//
// # 归属
// app 层用例。端口见 repo.go，实现见 adapters/pg。
// 入口层从 entry/http/user 调用本包。
//
// # 复用
// Service、Deps、CreateInput；Record 与 New 来自 repo.go。
//
// # 口径
// 只有 master 可以建号（裁决 12.4）。其余角色一律 403。
// 邮箱先归一再校验，口径取自 core/user。
// 口令在开事务**之前**算哈希：argon2id 校验耗时见 password.go 参数（m=19456KiB, t=2），不进事务、不持锁。
// 写行与 user.create 审计同事务。

package user

import (
	"context"
	"errors"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
)

// CreateInput 是建号输入。
// Password 是明文，只在本层变成哈希，**不写日志、不写响应**。
type CreateInput struct {
	Email    string
	Password string
	Role     coreauth.Role
}

// Deps 是用户用例的依赖。全部必填，缺一即构造失败。
// Limit 是活跃用户上限（裁决 12.11）。
type Deps struct {
	Users Repo
	Audit appaudit.Repo
	Tx    app.TxManager
	Clock clock.Clock
	// KDF 是口令哈希端口。生产必须传限并发实现（coreauth.Bounded）。
	KDF   coreauth.Hasher
	Limit int
}

// Service 承载用户管理用例。
type Service struct {
	deps Deps
}

// NewService 构造用户用例。缺依赖即失败关闭。
func NewService(deps Deps) (*Service, error) {
	switch {
	case deps.Users == nil:
		return nil, errors.New("user: nil user repo")
	case deps.Audit == nil:
		return nil, errors.New("user: nil audit repo")
	case deps.Tx == nil:
		return nil, errors.New("user: nil transaction manager")
	case deps.Clock == nil:
		return nil, errors.New("user: nil clock")
	case deps.KDF == nil:
		return nil, errors.New("user: nil KDF")
	case deps.Limit <= 0:
		return nil, errors.New("user: limit must be positive")
	}
	return &Service{deps: deps}, nil
}

// Create 建一个用户。**只有 master 可以建号**。
//
// 角色白名单是三值，其余一律 validation_failed。
// 重复邮箱由部分唯一索引 uq_users_email_live 报 conflict。
// 写行与 user.create 审计同事务：审计写失败即整体回滚。
func (s *Service) Create(ctx context.Context, scope corescope.Scope, in CreateInput, actor appaudit.Actor) (Record, error) {
	if err := app.RequireScope(scope); err != nil {
		return Record{}, err
	}
	if scope.Kind() != corescope.KindMaster {
		return Record{}, errs.New(errs.CodeForbidden, "master role is required")
	}
	email := coreuser.NormalizeEmail(in.Email)
	if !coreuser.ValidEmail(email) {
		return Record{}, errs.New(errs.CodeValidation, "invalid email")
	}
	if in.Password == "" {
		return Record{}, errs.New(errs.CodeValidation, "password is required")
	}
	if !in.Role.Valid() {
		return Record{}, errs.New(errs.CodeValidation, "role must be master, admin or user")
	}

	hash, err := s.deps.KDF.Hash(ctx, in.Password)
	if err != nil {
		return Record{}, err
	}

	var record Record
	err = s.deps.Tx.InTx(ctx, func(ctx context.Context) error {
		var err error
		record, err = s.deps.Users.Create(ctx, New{
			Email:        email,
			PasswordHash: hash,
			Role:         in.Role,
		}, s.deps.Limit)
		if err != nil {
			return err
		}
		// detail 只放角色：邮箱是 PII，**不得**进审计。
		_, err = s.deps.Audit.Append(ctx, coreaudit.Entry{
			TS:          s.deps.Clock.Now().UTC(),
			ActorUserID: actor.UserID,
			ActorRole:   actor.Role,
			ActorLabel:  actor.Label,
			Action:      coreaudit.ActionUserCreate,
			TargetType:  "user",
			TargetID:    record.ID,
			Detail:      map[string]any{"field": "role", "to": string(record.Role)},
		})
		return err
	})
	if err != nil {
		return Record{}, err
	}
	return record, nil
}
