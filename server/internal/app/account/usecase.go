// Package account 实现账号用例。
//
// # 功能
// 建、查、改、审核、暂停、软删、列表。
// 本包**不**实现 SQL、**不**做 HTTP 编码、**不**签发会话。
//
// # 归属
// app 层用例。端口见 repo.go，实现见 adapters/pg。
// 入口层从 entry/http/account 调用本包。
//
// # 复用
// Service、Deps、Page、ListFilter、CreateInput、UpdateInput；
// Cipher、CredentialPayload；状态常量与 Record 来自 repo.go。
//
// # 口径
// 每次调用的第一个参数是作用域，漏传即编译失败。
// 跨校读取一律 404；动作无权 403；非法状态迁移 409。
// 写动作与审计行同事务；审计追加是事务最后一条语句。
// 明文手机号与口令只经 SigningCredential 交给签到链；
// 本层不记录、不回显，入口层负责掩码。
package account

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
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mask"
	coreschool "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/school"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// phonePattern 与 ck_accounts_phone_shape 同形。
var phonePattern = regexp.MustCompile(`^1[0-9]{10}$`)

// maxPasswordBytes 限制明文口令长度，避免超长负载撞密文列上限。
const maxPasswordBytes = 1024

// maxQueryChars 限制列表搜索串长度。
const maxQueryChars = 64

// maxCredentialFieldChars 与契约的 maxLength: 64 同值。
// 设备码与机型会进入凭据密文；不设上界时只受 1 MiB 体上界约束。
const maxCredentialFieldChars = 64

// SchoolReader 读院校行。只用到按 id 取一条。
type SchoolReader interface {
	Get(ctx context.Context, id string) (coreschool.Record, error)
}

// Deps 是账号用例的依赖。
// Cipher 可为 nil；此时任何写入口令/设备码的请求都失败（fail-closed）。
type Deps struct {
	Accounts Repo
	Schools  SchoolReader
	Audit    appaudit.Repo
	Tx       app.TxManager
	Clock    clock.Clock
	Cipher   *Cipher
	// Limit 是活跃账号上限。
	Limit int
}

// Service 是账号用例服务。
type Service struct {
	deps Deps
}

// Page 是一页账号。
type Page struct {
	Items      []Record
	NextCursor string
}

// ListFilter 是列表的可选过滤条件。
// 空串表示不过滤；它们与作用域是**与**关系。
type ListFilter struct {
	SchoolID string
	Status   Status
	Query    string
}

// CreateInput 是建账号的输入。口令必填。
type CreateInput struct {
	SchoolID    string
	OwnerUserID string
	Phone       string
	Password    string
	DeviceCode  string
	PhoneModel  string
}

// UpdateInput 携带可选的列变更。nil 字段不改。
// SchoolID 只用于拒绝换校，P1 不支持迁移。
type UpdateInput struct {
	SchoolID   *string
	Phone      *string
	Password   *string
	DeviceCode *string
	PhoneModel *string
}

// SigningCredential 是签到链需要的明文凭据。
// 只在本进程内交给 provider；入口层不得记录或回显。
type SigningCredential struct {
	SchoolID   string
	Phone      string
	Password   string
	DeviceCode string
	PhoneModel string
}

// NewService 构造账号用例。缺依赖即失败。
func NewService(deps Deps) (*Service, error) {
	if deps.Accounts == nil {
		return nil, errors.New("account: nil account repo")
	}
	if deps.Schools == nil {
		return nil, errors.New("account: nil school reader")
	}
	if deps.Audit == nil {
		return nil, errors.New("account: nil audit repo")
	}
	if deps.Tx == nil {
		return nil, errors.New("account: nil transaction manager")
	}
	if deps.Clock == nil {
		return nil, errors.New("account: nil clock")
	}
	if deps.Limit <= 0 {
		return nil, errors.New("account: capacity limit must be positive")
	}
	return &Service{deps: deps}, nil
}

// List 按作用域列账号。
// master 全量；admin 只见绑定校；user 只见自己的。
func (s *Service) List(ctx context.Context, scope corescope.Scope, filter ListFilter, cursor string, limit int) (Page, error) {
	if err := app.RequireScope(scope); err != nil {
		return Page{}, err
	}
	if filter.Status != "" && !filter.Status.Valid() {
		return Page{}, errs.New(errs.CodeValidation, "unknown account status")
	}
	if utf8.RuneCountInString(filter.Query) > maxQueryChars {
		return Page{}, errs.New(errs.CodeValidation, "q is too long")
	}
	limit, err := app.NormalizeLimit(limit)
	if err != nil {
		return Page{}, err
	}
	items, next, err := s.deps.Accounts.List(ctx, scope, filter.SchoolID, filter.Status, filter.Query, cursor, limit)
	if err != nil {
		return Page{}, err
	}
	return Page{Items: items, NextCursor: next}, nil
}

// Get 在作用域内读一个账号。跨校或已软删返回 404。
func (s *Service) Get(ctx context.Context, scope corescope.Scope, id string) (Record, error) {
	if err := app.RequireScope(scope); err != nil {
		return Record{}, err
	}
	return s.deps.Accounts.Get(ctx, scope, id)
}

// SigningCredential 读取并解密一个账号的凭据。
// 作用域外与已软删返回 404；未配置 Cipher 即失败（fail-closed）。
// 明文只交给签到链，本层不记录、不回显。
func (s *Service) SigningCredential(ctx context.Context, scope corescope.Scope, id string) (SigningCredential, error) {
	if err := app.RequireScope(scope); err != nil {
		return SigningCredential{}, err
	}
	record, err := s.deps.Accounts.Get(ctx, scope, id)
	if err != nil {
		return SigningCredential{}, err
	}
	if s.deps.Cipher == nil {
		return SigningCredential{}, errors.New("account: credential cipher is not configured")
	}
	credential, err := s.deps.Accounts.Credential(ctx, scope, id)
	if err != nil {
		return SigningCredential{}, err
	}
	payload, err := s.deps.Cipher.Open(credential)
	if err != nil {
		return SigningCredential{}, err
	}
	return SigningCredential{
		SchoolID:   record.SchoolID,
		Phone:      record.Phone,
		Password:   payload.Password,
		DeviceCode: payload.DeviceCode,
		PhoneModel: payload.PhoneModel,
	}, nil
}

// Create 建账号。状态固定为 pending。
// 账号行、凭据行与 account.create 审计同事务。
func (s *Service) Create(ctx context.Context, scope corescope.Scope, in CreateInput, actor appaudit.Actor) (Record, error) {
	if err := app.RequireScope(scope); err != nil {
		return Record{}, err
	}
	owner, err := resolveOwner(scope, actor, in.OwnerUserID)
	if err != nil {
		return Record{}, err
	}
	if err := s.requireSchool(ctx, scope, in.SchoolID); err != nil {
		return Record{}, err
	}
	if !phonePattern.MatchString(in.Phone) {
		return Record{}, errs.New(errs.CodeValidation, "phone must match ^1[0-9]{10}$")
	}
	if err := validatePassword(in.Password); err != nil {
		return Record{}, err
	}
	if err := validateCredentialText("device_code", in.DeviceCode); err != nil {
		return Record{}, err
	}
	if err := validateCredentialText("phone_model", in.PhoneModel); err != nil {
		return Record{}, err
	}
	if s.deps.Cipher == nil {
		return Record{}, errs.New(errs.CodeInternal, "credential cipher is not configured")
	}

	var record Record
	err = s.deps.Tx.InTx(ctx, func(ctx context.Context) error {
		var err error
		record, err = s.deps.Accounts.Create(ctx, New{
			SchoolID:    in.SchoolID,
			OwnerUserID: owner,
			Phone:       in.Phone,
		}, s.deps.Limit)
		if err != nil {
			return err
		}
		credential, err := s.deps.Cipher.Seal(in.SchoolID, record.ID, CredentialPayload{
			DeviceCode: in.DeviceCode,
			Password:   in.Password,
			PhoneModel: in.PhoneModel,
		})
		if err != nil {
			return err
		}
		if err := s.deps.Accounts.InsertCredential(ctx, credential); err != nil {
			return err
		}
		_, err = s.deps.Audit.Append(ctx, coreaudit.Entry{
			TS:          s.deps.Clock.Now().UTC(),
			SchoolID:    record.SchoolID,
			ActorUserID: actor.UserID,
			ActorRole:   actor.Role,
			ActorLabel:  actor.Label,
			Action:      coreaudit.ActionAccountCreate,
			TargetType:  "account",
			TargetID:    record.ID,
			Detail:      map[string]any{"field": "status", "to": string(StatusPending)},
		})
		return err
	})
	if err != nil {
		return Record{}, err
	}
	return record, nil
}

// Update 改账号字段。**从不改状态**；换校返回 conflict。
// 账号行、凭据行与 account.update 审计同事务。
func (s *Service) Update(ctx context.Context, scope corescope.Scope, id string, in UpdateInput, actor appaudit.Actor) (Record, error) {
	if err := app.RequireScope(scope); err != nil {
		return Record{}, err
	}
	current, err := s.deps.Accounts.Get(ctx, scope, id)
	if err != nil {
		return Record{}, err
	}
	if in.SchoolID != nil && *in.SchoolID != "" && *in.SchoolID != current.SchoolID {
		return Record{}, errs.New(errs.CodeConflict, "changing school is not supported")
	}
	field, err := changedField(in)
	if err != nil {
		return Record{}, err
	}
	if in.Phone != nil && !phonePattern.MatchString(*in.Phone) {
		return Record{}, errs.New(errs.CodeValidation, "phone must match ^1[0-9]{10}$")
	}
	if in.Password != nil {
		if err := validatePassword(*in.Password); err != nil {
			return Record{}, err
		}
	}
	if in.DeviceCode != nil {
		if err := validateCredentialText("device_code", *in.DeviceCode); err != nil {
			return Record{}, err
		}
	}
	if in.PhoneModel != nil {
		if err := validateCredentialText("phone_model", *in.PhoneModel); err != nil {
			return Record{}, err
		}
	}
	needCredential := in.Password != nil || in.DeviceCode != nil || in.PhoneModel != nil
	if needCredential && s.deps.Cipher == nil {
		return Record{}, errs.New(errs.CodeInternal, "credential cipher is not configured")
	}

	record := current
	err = s.deps.Tx.InTx(ctx, func(ctx context.Context) error {
		now := s.deps.Clock.Now().UTC()
		if in.Phone != nil {
			var err error
			record, err = s.deps.Accounts.UpdatePhone(ctx, scope, id, *in.Phone, now)
			if err != nil {
				return err
			}
		}
		if needCredential {
			if err := s.rewriteCredential(ctx, scope, record, in); err != nil {
				return err
			}
			// 凭据写回不更新 accounts.updated_at；重读一次让响应带真实时间。
			refreshed, err := s.deps.Accounts.Get(ctx, scope, id)
			if err != nil {
				return err
			}
			record = refreshed
		}
		_, err := s.deps.Audit.Append(ctx, coreaudit.Entry{
			TS:          now,
			SchoolID:    record.SchoolID,
			ActorUserID: actor.UserID,
			ActorRole:   actor.Role,
			ActorLabel:  actor.Label,
			Action:      coreaudit.ActionAccountUpdate,
			TargetType:  "account",
			TargetID:    record.ID,
			Detail:      map[string]any{"field": field},
		})
		return err
	})
	if err != nil {
		return Record{}, err
	}
	return record, nil
}

// Review 审核 pending 账号。拒绝必须带理由。
// **只有 master 与 admin 可以审核**。
func (s *Service) Review(ctx context.Context, scope corescope.Scope, id string, approve bool, reason string, actor appaudit.Actor) (Record, error) {
	if err := app.RequireScope(scope); err != nil {
		return Record{}, err
	}
	if scope.Kind() != corescope.KindMaster && scope.Kind() != corescope.KindAdmin {
		return Record{}, errs.New(errs.CodeForbidden, "role cannot review accounts")
	}
	reason = strings.TrimSpace(reason)
	if !approve {
		if count := utf8.RuneCountInString(reason); count < 1 || count > coreaudit.MaxReasonChars {
			return Record{}, errs.New(errs.CodeValidation, "reject reason must be 1 to 200 characters")
		}
	}
	var record Record
	err := s.deps.Tx.InTx(ctx, func(ctx context.Context) error {
		var err error
		record, err = s.deps.Accounts.Review(ctx, scope, id, approve, reason, actor.UserID, s.deps.Clock.Now().UTC())
		if err != nil {
			return err
		}
		action := coreaudit.ActionAccountReviewPass
		detail := map[string]any{
			"field": "status",
			"from":  string(StatusPending),
			"to":    string(StatusActive),
		}
		if !approve {
			action = coreaudit.ActionAccountReviewReject
			detail["to"] = string(StatusRejected)
			// 自由文本先掩码：审计 detail 不得出现手机号或邮箱。
			detail["reason"] = mask.MaskText(reason)
		}
		_, err = s.deps.Audit.Append(ctx, coreaudit.Entry{
			TS:          s.deps.Clock.Now().UTC(),
			SchoolID:    record.SchoolID,
			ActorUserID: actor.UserID,
			ActorRole:   actor.Role,
			ActorLabel:  actor.Label,
			Action:      action,
			TargetType:  "account",
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

// Pause 暂停或恢复账号。
// admin/master 用 paused_admin；账号属主用 paused_user。
func (s *Service) Pause(ctx context.Context, scope corescope.Scope, id string, paused bool, actor appaudit.Actor) (Record, error) {
	if err := app.RequireScope(scope); err != nil {
		return Record{}, err
	}
	from := StatusPausedAdmin
	if scope.Kind() == corescope.KindUser {
		from = StatusPausedUser
	}
	action := coreaudit.ActionAccountPause
	target := from
	detail := map[string]any{
		"field": "status",
		"from":  string(StatusActive),
		"to":    string(from),
	}
	if !paused {
		action = coreaudit.ActionAccountResume
		target = StatusActive
		detail = map[string]any{
			"field": "status",
			"from":  string(from),
			"to":    string(StatusActive),
		}
		return s.transition(ctx, scope, id, from, target, action, detail, actor)
	}
	return s.transition(ctx, scope, id, StatusActive, target, action, detail, actor)
}

// Delete 软删账号。行保留，读取立即 404。
func (s *Service) Delete(ctx context.Context, scope corescope.Scope, id string, actor appaudit.Actor) error {
	if err := app.RequireScope(scope); err != nil {
		return err
	}
	return s.deps.Tx.InTx(ctx, func(ctx context.Context) error {
		now := s.deps.Clock.Now().UTC()
		// 先读一次是为了拿 school_id 写审计；删除语句本身仍带作用域与 deleted_at 判据。
		record, err := s.deps.Accounts.Get(ctx, scope, id)
		if err != nil {
			return err
		}
		if err := s.deps.Accounts.SoftDelete(ctx, scope, id, actor.UserID, now); err != nil {
			return err
		}
		_, err = s.deps.Audit.Append(ctx, coreaudit.Entry{
			TS:          now,
			SchoolID:    record.SchoolID,
			ActorUserID: actor.UserID,
			ActorRole:   actor.Role,
			ActorLabel:  actor.Label,
			Action:      coreaudit.ActionAccountDelete,
			TargetType:  "account",
			TargetID:    record.ID,
			Detail:      map[string]any{"field": "deleted_at"},
		})
		return err
	})
}

// transition 执行一次状态比较并交换，并写对应审计。
func (s *Service) transition(ctx context.Context, scope corescope.Scope, id string, from, to Status, action coreaudit.Action, detail map[string]any, actor appaudit.Actor) (Record, error) {
	var record Record
	err := s.deps.Tx.InTx(ctx, func(ctx context.Context) error {
		var err error
		record, err = s.deps.Accounts.SetStatus(ctx, scope, id, from, to, s.deps.Clock.Now().UTC(), "")
		if err != nil {
			return err
		}
		_, err = s.deps.Audit.Append(ctx, coreaudit.Entry{
			TS:          s.deps.Clock.Now().UTC(),
			SchoolID:    record.SchoolID,
			ActorUserID: actor.UserID,
			ActorRole:   actor.Role,
			ActorLabel:  actor.Label,
			Action:      action,
			TargetType:  "account",
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

// rewriteCredential 解密现有负载、合并入参、再加密写回。
// 无既有行时用当前入参新建一行。
func (s *Service) rewriteCredential(ctx context.Context, scope corescope.Scope, record Record, in UpdateInput) error {
	payload := CredentialPayload{}
	version := int32(0)
	credential, err := s.deps.Accounts.Credential(ctx, scope, record.ID)
	switch {
	case err == nil:
		payload, err = s.deps.Cipher.Open(credential)
		if err != nil {
			return err
		}
		version = credential.Version
	case errs.From(err).Code == errs.CodeNotFound:
		// 无既有凭据：按新建处理。
	default:
		return err
	}
	if in.Password != nil {
		payload.Password = *in.Password
	}
	if in.DeviceCode != nil {
		payload.DeviceCode = *in.DeviceCode
	}
	if in.PhoneModel != nil {
		payload.PhoneModel = *in.PhoneModel
	}
	sealed, err := s.deps.Cipher.Seal(record.SchoolID, record.ID, payload)
	if err != nil {
		return err
	}
	if version == 0 {
		return s.deps.Accounts.InsertCredential(ctx, sealed)
	}
	return s.deps.Accounts.ReplaceCredential(ctx, sealed, version)
}

// requireSchool 确认院校存在且落在作用域内。
// admin 只允许自己绑定的院校；跨校一律 404。
func (s *Service) requireSchool(ctx context.Context, scope corescope.Scope, schoolID string) error {
	if strings.TrimSpace(schoolID) == "" {
		return errs.New(errs.CodeValidation, "school_id is required")
	}
	if _, err := s.deps.Schools.Get(ctx, schoolID); err != nil {
		return err
	}
	if scope.Kind() == corescope.KindAdmin {
		for _, id := range scope.SchoolIDs() {
			if id == schoolID {
				return nil
			}
		}
		return errs.ErrNotFound
	}
	return nil
}

// resolveOwner 决定账号属主。
// 只有 master 与 admin 可以把账号挂在别人名下。
func resolveOwner(scope corescope.Scope, actor appaudit.Actor, requested string) (string, error) {
	if scope.Kind() == corescope.KindUser {
		if requested != "" && requested != actor.UserID {
			return "", errs.New(errs.CodeForbidden, "user cannot set another owner")
		}
		return actor.UserID, nil
	}
	if requested == "" {
		return actor.UserID, nil
	}
	return requested, nil
}

// validateCredentialText 限制会进入凭据密文的自由文本长度。
// 空串合法：设备码与机型都是可选项。
func validateCredentialText(field, value string) error {
	if utf8.RuneCountInString(value) > maxCredentialFieldChars {
		return errs.New(errs.CodeValidation, field+" must be at most 64 characters")
	}
	return nil
}

func validatePassword(password string) error {
	if password == "" {
		return errs.New(errs.CodeValidation, "password is required")
	}
	if len(password) > maxPasswordBytes {
		return errs.New(errs.CodeValidation, "password is too long")
	}
	return nil
}

// changedField 返回本次改动的代表字段，并拒绝空变更。
func changedField(in UpdateInput) (string, error) {
	switch {
	case in.Phone != nil:
		return "phone", nil
	case in.Password != nil:
		return "password", nil
	case in.DeviceCode != nil:
		return "device_code", nil
	case in.PhoneModel != nil:
		return "phone_model", nil
	case in.SchoolID != nil:
		return "school_id", nil
	default:
		return "", errs.New(errs.CodeValidation, "at least one field is required")
	}
}
