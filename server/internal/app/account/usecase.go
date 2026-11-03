// Package account 实现账号用例。
//
// # 功能
// 建、查、改、审核、暂停、软删、恢复、设备刷新、列表。
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
	coreuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/user"
)

// phonePattern 与 ck_accounts_phone_shape 同形。
var phonePattern = regexp.MustCompile(`^1[0-9]{10}$`)

// maxPasswordBytes 限制明文口令长度，避免超长负载撞密文列上限。
const maxPasswordBytes = 1024

// maxQueryChars 限制列表搜索串长度。
// 完整邮箱最长 254 字符，搜索必须能装下一个完整地址。
const maxQueryChars = 254

// maxCredentialFieldChars 与契约的 maxLength: 64 同值。
// 设备码与机型会进入凭据密文；不设上界时只受 1 MiB 体上界约束。
const maxCredentialFieldChars = 64

// maxNameChars 与契约的 maxLength: 50 及 ck_accounts_name_length 同值。
const maxNameChars = 50

// SchoolReader 读院校行。只用到按 id 取一条。
type SchoolReader interface {
	Get(ctx context.Context, id string) (coreschool.Record, error)
}

// OwnerReader 按 id 读属主用户行，供建号的活跃校验（8v8⑦）。
// 实现是 pg.UserRepo；GetByID 只回未删行，
// 软删与不存在的 owner 在这里同样落到 not_found。
// 返回类型留在 core/user：适配器无须导入本包（boundary.adapters）。
type OwnerReader interface {
	GetByID(ctx context.Context, id string) (coreuser.Record, error)
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
	// Owner 校验 master/admin 指定的属主是活跃用户（8v8⑦）。
	Owner OwnerReader
	// Device 从平台读回设备登记状态；实现见 assembly（8v8⑧）。
	Device DeviceReader
	// Limit 是活跃账号上限。
	Limit int
	// OwnerLimit 是每个属主的活跃账号上限。
	OwnerLimit int
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
// IncludeDeleted 置真时软删行也入选；缺省假，行为不变。
type ListFilter struct {
	SchoolID       string
	Status         Status
	Query          string
	IncludeDeleted bool
}

// CreateInput 是建账号的输入。口令必填，备注名可选。
type CreateInput struct {
	SchoolID    string
	OwnerUserID string
	Phone       string
	Name        string
	Password    string
	DeviceCode  string
	PhoneModel  string
}

// UpdateInput 携带可选的列变更。nil 字段不改。
// SchoolID 只用于拒绝换校，P1 不支持迁移。
type UpdateInput struct {
	SchoolID   *string
	Phone      *string
	Name       *string
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
	if deps.OwnerLimit <= 0 {
		return nil, errors.New("account: owner limit must be positive")
	}
	if deps.Owner == nil {
		return nil, errors.New("account: nil owner reader")
	}
	if deps.Device == nil {
		return nil, errors.New("account: nil device reader")
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
	items, next, err := s.deps.Accounts.List(ctx, scope, filter.SchoolID, filter.Status, filter.Query, cursor, limit, filter.IncludeDeleted)
	if err != nil {
		return Page{}, err
	}
	if err := s.attachPhoneModels(ctx, scope, items); err != nil {
		return Page{}, err
	}
	return Page{Items: items, NextCursor: next}, nil
}

// Get 在作用域内读一个账号。跨校或已软删返回 404。
func (s *Service) Get(ctx context.Context, scope corescope.Scope, id string) (Record, error) {
	if err := app.RequireScope(scope); err != nil {
		return Record{}, err
	}
	record, err := s.deps.Accounts.Get(ctx, scope, id)
	if err != nil {
		return Record{}, err
	}
	s.attachPhoneModel(ctx, scope, &record)
	return record, nil
}

// attachPhoneModels 为一页账号批量填机型。
// 只读未软删行；凭据缺失或解密失败的行留空，列表仍成功。
// 批量读库本身失败才返回错误。
func (s *Service) attachPhoneModels(ctx context.Context, scope corescope.Scope, items []Record) error {
	if s.deps.Cipher == nil {
		return nil
	}
	ids := make([]string, 0, len(items))
	for _, item := range items {
		if item.DeletedAt == nil {
			ids = append(ids, item.ID)
		}
	}
	if len(ids) == 0 {
		return nil
	}
	credentials, err := s.deps.Accounts.Credentials(ctx, scope, ids)
	if err != nil {
		return err
	}
	for i := range items {
		credential, ok := credentials[items[i].ID]
		if !ok {
			continue
		}
		payload, err := s.deps.Cipher.Open(credential)
		if err != nil {
			continue
		}
		items[i].PhoneModel = payload.PhoneModel
	}
	return nil
}

// attachPhoneModel 为单个账号填机型。凭据缺失或不可读时留空，详情不失败。
func (s *Service) attachPhoneModel(ctx context.Context, scope corescope.Scope, record *Record) {
	if s.deps.Cipher == nil || record.DeletedAt != nil {
		return
	}
	credential, err := s.deps.Accounts.Credential(ctx, scope, record.ID)
	if err != nil {
		return
	}
	payload, err := s.deps.Cipher.Open(credential)
	if err != nil {
		return
	}
	record.PhoneModel = payload.PhoneModel
}

// Summary 汇总作用域内待处理的账号数：pending 与 rejected，均只数未软删行。
// master 统计全校；admin 只统计绑定校。**只有 master 与 admin 可以读**；
// user 一律 403。只读，不写审计。
func (s *Service) Summary(ctx context.Context, scope corescope.Scope) (StatusCounts, error) {
	if err := app.RequireScope(scope); err != nil {
		return StatusCounts{}, err
	}
	if scope.Kind() != corescope.KindMaster && scope.Kind() != corescope.KindAdmin {
		return StatusCounts{}, errs.New(errs.CodeForbidden, "role cannot read the account summary")
	}
	return s.deps.Accounts.StatusCounts(ctx, scope)
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
//
// 设备码是平台设备身份，user 不得自填，只能走 device-refresh；
// 机型是非凭据标签，user 可以设置。master 与 admin 不受此限。
func (s *Service) Create(ctx context.Context, scope corescope.Scope, in CreateInput, actor appaudit.Actor) (Record, error) {
	if err := app.RequireScope(scope); err != nil {
		return Record{}, err
	}
	if scope.Kind() == corescope.KindUser && in.DeviceCode != "" {
		return Record{}, errs.New(errs.CodeForbidden, "users cannot set the device code")
	}
	owner, err := s.resolveOwner(ctx, scope, actor, in.OwnerUserID)
	if err != nil {
		return Record{}, err
	}
	if err := s.requireSchool(ctx, scope, in.SchoolID); err != nil {
		return Record{}, err
	}
	if !phonePattern.MatchString(in.Phone) {
		return Record{}, errs.New(errs.CodeValidation, "phone must match ^1[0-9]{10}$")
	}
	name, err := normalizeName(in.Name)
	if err != nil {
		return Record{}, err
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
			Name:        name,
		}, s.deps.Limit, s.deps.OwnerLimit)
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
	// 新行的凭据刚写入；响应直接回入参，不必再解密一次。
	record.PhoneModel = in.PhoneModel
	return record, nil
}

// Update 改账号字段。换校返回 conflict。
//
// 状态副作用按优先级：
//   - 被拒账号改凭据（手机号、口令、设备码、机型）：回 pending 重新提交，
//     清空驳回理由与审核戳。审核只收 pending，这是被拒账号唯一的再入队路径。
//     只改备注名不算重提：备注不参与审核判定，被拒账号保持拒绝。
//   - 改绑手机号：非 pending 账号回 pending 重审，清空理由与审核戳。
//     手机号即登录凭据，审核结论绑定旧号；不改状态等于免审换号。
//   - 其余凭据变更：credential_error 恢复为 active。
//
// 判定在事务内、行锁下读取，避免漏掉引擎刚提交的状态。
// 账号行、凭据行与审计同事务；状态迁移时 account.resubmit 或
// account.credential_recovered 与 account.update 相邻追加。
//
// 设备码是平台设备身份，user 不得改，只能走 device-refresh；
// 机型是非凭据标签，user 可以改。master 与 admin 不受此限。
func (s *Service) Update(ctx context.Context, scope corescope.Scope, id string, in UpdateInput, actor appaudit.Actor) (Record, error) {
	if err := app.RequireScope(scope); err != nil {
		return Record{}, err
	}
	if scope.Kind() == corescope.KindUser && in.DeviceCode != nil {
		return Record{}, errs.New(errs.CodeForbidden, "users cannot set the device code")
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
	name := ""
	if in.Name != nil {
		name, err = normalizeName(*in.Name)
		if err != nil {
			return Record{}, err
		}
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
		// 状态判定必须在事务内、行锁下读：事务外的快照会把引擎刚提交的
		// credential_error 或并发审核结果看丢。
		locked, err := s.deps.Accounts.GetForUpdate(ctx, scope, id)
		if err != nil {
			return err
		}
		record = locked
		rebind := in.Phone != nil && *in.Phone != locked.Phone
		// 凭据编辑才是重提：备注名是标签，只改它不把被拒账号送回队列。
		// 同值手机号不是编辑，不单独触发重提。
		credentialEdit := rebind || in.Password != nil ||
			in.DeviceCode != nil || in.PhoneModel != nil
		// 改绑把非 pending 账号送回队列；pending 行改绑仍停在 pending，
		// 不写 from=pending 的重提审计。
		needsReview := (locked.Status == StatusRejected && credentialEdit) ||
			(rebind && locked.Status != StatusPending)
		if in.Name != nil {
			record, err = s.deps.Accounts.UpdateName(ctx, scope, id, name, now)
			if err != nil {
				return err
			}
		}
		if in.Phone != nil {
			record, err = s.deps.Accounts.UpdatePhone(ctx, scope, id, *in.Phone, now)
			if err != nil {
				return err
			}
		}
		if needCredential {
			if err := s.rewriteCredential(ctx, scope, record, in); err != nil {
				return err
			}
		}
		// 改绑优先于凭据恢复：换号后必须重审，不能借恢复路径免审回 active。
		// 同值手机号不是凭据变更，不触发恢复。
		recovered := !needsReview && needCredential && locked.Status == StatusCredentialError
		switch {
		case needsReview:
			// 行锁已在本事务内，CAS 必然命中；并发状态写已被锁挡住。
			record, err = s.deps.Accounts.SetStatus(ctx, scope, id,
				locked.Status, StatusPending, now, "")
		case recovered:
			record, err = s.deps.Accounts.SetStatus(ctx, scope, id,
				StatusCredentialError, StatusActive, now, "")
		case needCredential:
			// 凭据写回不更新 accounts.updated_at；重读一次让响应带真实时间。
			record, err = s.deps.Accounts.Get(ctx, scope, id)
		}
		if err != nil {
			return err
		}
		if _, err := s.deps.Audit.Append(ctx, coreaudit.Entry{
			TS:          now,
			SchoolID:    record.SchoolID,
			ActorUserID: actor.UserID,
			ActorRole:   actor.Role,
			ActorLabel:  actor.Label,
			Action:      coreaudit.ActionAccountUpdate,
			TargetType:  "account",
			TargetID:    record.ID,
			Detail:      map[string]any{"field": field},
		}); err != nil {
			return err
		}
		if needsReview {
			// 两条审计相邻追加：中间不做任何工作，链锁只多持一次插入。
			_, err := s.deps.Audit.Append(ctx, coreaudit.Entry{
				TS:          now,
				SchoolID:    record.SchoolID,
				ActorUserID: actor.UserID,
				ActorRole:   actor.Role,
				ActorLabel:  actor.Label,
				Action:      coreaudit.ActionAccountResubmit,
				TargetType:  "account",
				TargetID:    record.ID,
				Detail: map[string]any{
					"field": "status",
					"from":  string(locked.Status),
					"to":    string(StatusPending),
				},
			})
			return err
		}
		if !recovered {
			return nil
		}
		_, err = s.deps.Audit.Append(ctx, coreaudit.Entry{
			TS:          now,
			SchoolID:    record.SchoolID,
			ActorUserID: actor.UserID,
			ActorRole:   actor.Role,
			ActorLabel:  actor.Label,
			Action:      coreaudit.ActionAccountCredentialRecovered,
			TargetType:  "account",
			TargetID:    record.ID,
			Detail: map[string]any{
				"field": "status",
				"from":  string(StatusCredentialError),
				"to":    string(StatusActive),
			},
		})
		return err
	})
	if err != nil {
		return Record{}, err
	}
	// 凭据写回不保证响应带最新机型：只在本次显式给了新值时回填。
	// 其余情况列表与详情会再读凭据。
	if in.PhoneModel != nil {
		record.PhoneModel = *in.PhoneModel
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
// 只有 master 与 admin 可以把账号挂在别人名下；
// 指定的属主必须是活跃（未删）用户：软删行仍在、外键放行，
// 这个口子只能由应用层堵上（8v8⑦，堵 FK 放行）。
func (s *Service) resolveOwner(ctx context.Context, scope corescope.Scope, actor appaudit.Actor, requested string) (string, error) {
	if scope.Kind() == corescope.KindUser {
		if requested != "" && requested != actor.UserID {
			return "", errs.New(errs.CodeForbidden, "user cannot set another owner")
		}
		return actor.UserID, nil
	}
	if requested == "" {
		return actor.UserID, nil
	}
	if _, err := s.deps.Owner.GetByID(ctx, requested); err != nil {
		// 只有「查无此人/已删」映射 404；读库失败等其余错误原样上抛，
		// 不许把基础设施故障伪装成资源缺失。
		if errs.From(err).Code == errs.CodeNotFound {
			return "", errs.New(errs.CodeNotFound, "owner user does not exist or is deleted")
		}
		return "", err
	}
	return requested, nil
}

// normalizeName 去首尾空白并限制 50 个字符。
// 空串合法：表示未命名或清空备注。
func normalizeName(value string) (string, error) {
	name := strings.TrimSpace(value)
	if utf8.RuneCountInString(name) > maxNameChars {
		return "", errs.New(errs.CodeValidation, "name must be at most 50 characters")
	}
	return name, nil
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
	case in.Name != nil:
		return "name", nil
	default:
		return "", errs.New(errs.CodeValidation, "at least one field is required")
	}
}
