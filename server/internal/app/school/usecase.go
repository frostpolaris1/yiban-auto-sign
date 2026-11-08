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
	"crypto/sha256"
	"encoding/hex"
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

// maxNameChars 与 ck_schools_name_len 及契约的 maxLength: 128 同值。
// 自动建校的平台名可能超限：截到该上限，不把平台异常变成整次探测失败。
const maxNameChars = 128

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
// master 见全量；admin 只见绑定校；user 只见 active 行（8v8①：
// 建号表单第 1 页需要学校下拉）。
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

// EnsurePlatformSchool 把平台院校标识投影到本地学校（ykxf）。
//
// 活跃绑定命中直接复用；disabled 绑定视同未命中并原样返回 disabled 行，
// 调用方据此不再建校；未命中则按平台院校名自动建校：
// 编码由 university_id 哈希生成，status 固定 active，
// school.auto_create 审计与建校同事务，detail 只含 university_id。
// 并发建校由部分唯一索引拦下（409），随后按 university_id 重读并复用赢家。
// 本方法不接受作用域：它只服务平台投影，调用方必须是已完成鉴权的用例。
func (s *Service) EnsurePlatformSchool(ctx context.Context, universityID, name string) (Record, error) {
	universityID = strings.TrimSpace(universityID)
	if universityID == "" || utf8.RuneCountInString(universityID) > 128 {
		return Record{}, errs.New(errs.CodeValidation,
			"university_id is required and must be at most 128 characters")
	}
	name = strings.TrimSpace(name)
	if name == "" {
		return Record{}, errs.New(errs.CodeValidation, "university name is required")
	}
	if utf8.RuneCountInString(name) > maxNameChars {
		name = string([]rune(name)[:maxNameChars])
	}

	record, err := s.deps.Schools.GetByPlatformUniversityID(ctx, universityID)
	if err == nil {
		return record, nil
	}
	if errs.From(err).Code != errs.CodeNotFound {
		return Record{}, err
	}

	newSchool := New{
		Code:                 autoSchoolCode(universityID),
		Name:                 name,
		PlatformUniversityID: universityID,
	}
	var created Record
	err = s.deps.Tx.InTx(ctx, func(ctx context.Context) error {
		var err error
		created, err = s.deps.Schools.Create(ctx, newSchool)
		if err != nil {
			return err
		}
		_, err = s.deps.Audit.Append(ctx, coreaudit.Entry{
			TS:          s.deps.Clock.Now().UTC(),
			SchoolID:    created.ID,
			ActorUserID: "",
			ActorRole:   "",
			ActorLabel:  "auto-detect",
			Action:      coreaudit.ActionSchoolAutoCreate,
			TargetType:  "school",
			TargetID:    created.ID,
			Detail:      map[string]any{"university_id": universityID},
		})
		return err
	})
	if err == nil {
		return created, nil
	}
	// 并发建校：另一个请求先提交同一 platform_university_id。
	// 回落重读并按活跃/停用语义返回；重读失败时保留原冲突错误。
	if errs.From(err).Code == errs.CodeConflict {
		if existing, lookupErr := s.deps.Schools.GetByPlatformUniversityID(ctx, universityID); lookupErr == nil {
			return existing, nil
		}
	}
	return Record{}, err
}

// autoSchoolCode 由平台院校标识生成稳定编码。
// 形状满足 ck_schools_code_shape（auto- + 12 位小写十六进制）。
// 同一 university_id 得到同一编码，并发插入因此撞上同一个唯一键。
func autoSchoolCode(universityID string) string {
	sum := sha256.Sum256([]byte(universityID))
	return "auto-" + hex.EncodeToString(sum[:6])
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

// requireSchoolReader 放行三类读者，拒绝其余作用域。
// UX-2 起 user 也可列学校，只见 active 行（8v8①）。
func requireSchoolReader(scope corescope.Scope) error {
	switch scope.Kind() {
	case corescope.KindMaster, corescope.KindAdmin, corescope.KindUser:
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
	if err := validatePlatformUniversityID(in.PlatformUniversityID); err != nil {
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
	if in.PlatformUniversityID != nil {
		if err := validatePlatformUniversityID(*in.PlatformUniversityID); err != nil {
			return "", err
		}
		changed++
		field = "platform_university_id"
	}
	if changed == 0 {
		return "", errs.New(errs.CodeValidation, "at least one field is required")
	}
	return field, nil
}

// validatePlatformUniversityID 与契约的 maxLength 128 同口径。
// 空串合法：表示清空或不写绑定。
func validatePlatformUniversityID(value string) error {
	if utf8.RuneCountInString(value) > 128 {
		return errs.New(errs.CodeValidation, "platform_university_id must be at most 128 characters")
	}
	return nil
}

func validateName(name string) error {
	name = strings.TrimSpace(name)
	if utf8.RuneCountInString(name) < 1 || utf8.RuneCountInString(name) > maxNameChars {
		return errs.New(errs.CodeValidation, "name must be 1 to 128 characters")
	}
	return nil
}
