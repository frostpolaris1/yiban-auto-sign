// Package location 实现账号定位策略用例（P5-C）。
//
// # 功能
// 读改账号的定位偏好（auto/fixed），fixed 行带经纬度与原因说明。
// 本包**不**做围栏几何（core/location）、**不**写 SQL、**不**处理 HTTP。
//
// # 归属
// app 层用例。端口见 repo.go：Store 接 adapters/pg，
// Accounts 复用 app/account 的可见面判据。
//
// # 复用
// Service、Deps、Input、Row、Pref；端口 Store、Accounts。
//
// # 口径
// 可见面判据与账号域一致：user 只能改自己的账号，admin 只能改
// 绑定学校的账号，跨校一律 404（403 会泄漏存在性）。
// 写入角色按契约 x-roles 收紧为 user 与 admin；master 回 403。
// 审计只记 mode 与 has_reason：原因原文可能含任意词句，
// 进审计链必须先过掩码口径，本切片不存原文。
package location

import (
	"context"
	"errors"
	"math"
	"strings"
	"unicode/utf8"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/account"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// maxReasonChars 限制原因说明长度。与审计自由文本上限同值：
// 两处上限同源，放宽一处就是放宽两处。
const maxReasonChars = 200

// Accounts 是账号可见面判据端口（实现为 app/account.Service）。
// 跨校、跨用户与已软删都返回 errs.ErrNotFound，判据只有一份。
type Accounts interface {
	Get(ctx context.Context, scope corescope.Scope, id string) (coreaccount.Record, error)
}

// Deps 是用例依赖。
type Deps struct {
	Store    Store
	Accounts Accounts
	Audit    appaudit.Repo
	Tx       app.TxManager
	Clock    coreclock.Clock
}

// Service 是定位策略用例服务。
type Service struct {
	deps Deps
}

// NewService 构造服务。缺依赖即失败。
func NewService(deps Deps) (*Service, error) {
	if deps.Store == nil {
		return nil, errors.New("location: nil store")
	}
	if deps.Accounts == nil {
		return nil, errors.New("location: nil accounts reader")
	}
	if deps.Audit == nil {
		return nil, errors.New("location: nil audit repo")
	}
	if deps.Tx == nil {
		return nil, errors.New("location: nil transaction manager")
	}
	if deps.Clock == nil {
		return nil, errors.New("location: nil clock")
	}
	return &Service{deps: deps}, nil
}

// Input 是一次定位策略写入。Lng/Lat 仅 fixed 需要。
type Input struct {
	Mode   string
	Lng    *float64
	Lat    *float64
	Reason string
}

// Set 校验可见面与参数，覆盖写定位策略并记 location.set 审计。
// 跨校、跨用户与已软删账号一律 404。
func (s *Service) Set(ctx context.Context, scope corescope.Scope, accountID string, in Input, actor appaudit.Actor) (contract.Location, error) {
	if err := requireWriter(scope); err != nil {
		return contract.Location{}, err
	}
	record, err := s.deps.Accounts.Get(ctx, scope, accountID)
	if err != nil {
		return contract.Location{}, err
	}
	reason := strings.TrimSpace(in.Reason)
	if utf8.RuneCountInString(reason) > maxReasonChars {
		return contract.Location{}, errs.New(errs.CodeValidation, "reason must be at most 200 characters")
	}
	var point *contract.Point
	switch in.Mode {
	case "auto":
		// auto 行不带坐标：旧点清空，避免留下读得到但用不上的残留。
	case "fixed":
		if in.Lng == nil || in.Lat == nil {
			return contract.Location{}, errs.New(errs.CodeValidation, "fixed mode requires lng and lat")
		}
		if err := validatePoint(*in.Lng, *in.Lat); err != nil {
			return contract.Location{}, err
		}
		point = &contract.Point{Lng: *in.Lng, Lat: *in.Lat}
	default:
		return contract.Location{}, errs.New(errs.CodeValidation, "mode must be auto or fixed")
	}

	err = s.deps.Tx.InTx(ctx, func(ctx context.Context) error {
		if err := s.deps.Store.Set(ctx, Pref{
			AccountID: record.ID, Mode: in.Mode, Point: point,
			Reason: reason, UpdatedBy: actor.UserID, Now: s.deps.Clock.Now().UTC(),
		}); err != nil {
			return err
		}
		_, err := s.deps.Audit.Append(ctx, coreaudit.Entry{
			TS:          s.deps.Clock.Now().UTC(),
			SchoolID:    record.SchoolID,
			ActorUserID: actor.UserID,
			ActorRole:   actor.Role,
			ActorLabel:  actor.Label,
			Action:      coreaudit.ActionLocationSet,
			TargetType:  "account",
			TargetID:    record.ID,
			Detail:      map[string]any{"mode": in.Mode, "has_reason": reason != ""},
		})
		return err
	})
	if err != nil {
		return contract.Location{}, err
	}
	// 回显完整策略：HTTP 200 的响应体就是本返回值。
	out := contract.Location{Mode: contract.LocationMode(in.Mode), Reason: reason}
	if point != nil {
		out.Lng, out.Lat = point.Lng, point.Lat
	}
	return out, nil
}

// Resolve 返回账号的定位策略，供执行循环注入 SignRequest。
// 无行或 auto 行返回 LocationAuto；fixed 行带经纬度与原因。
// 本方法服务于系统动作（worker），不取作用域。
func (s *Service) Resolve(ctx context.Context, accountID string) (contract.Location, error) {
	row, err := s.deps.Store.Get(ctx, accountID)
	if err != nil {
		if errs.From(err).Code == errs.CodeNotFound {
			return contract.Location{Mode: contract.LocationAuto}, nil
		}
		return contract.Location{}, err
	}
	if row.Mode != "fixed" {
		return contract.Location{Mode: contract.LocationAuto}, nil
	}
	return contract.Location{
		Mode: contract.LocationFixed, Lng: row.Lng, Lat: row.Lat, Reason: row.Reason,
	}, nil
}

// requireWriter 限定定位写入只给 user 与 admin。
// 契约 x-roles 是 [user, admin]：master 不得经本端点改定位。
// 形状与 app/photos 的 requireUser 相同，只是多放行 admin。
func requireWriter(scope corescope.Scope) error {
	if err := app.RequireScope(scope); err != nil {
		return err
	}
	switch scope.Kind() {
	case corescope.KindUser, corescope.KindAdmin:
		return nil
	}
	return errs.New(errs.CodeForbidden, "role cannot change the account location")
}

// validatePoint 拒绝非有限值与越界经纬度。
// JSON 解码挡不住 NaN/Inf，这里统一兜底。
func validatePoint(lng, lat float64) error {
	if math.IsNaN(lng) || math.IsInf(lng, 0) || math.IsNaN(lat) || math.IsInf(lat, 0) {
		return errs.New(errs.CodeValidation, "coordinates must be finite numbers")
	}
	if lng < -180 || lng > 180 || lat < -90 || lat > 90 {
		return errs.New(errs.CodeValidation, "coordinates are out of the WGS84 range")
	}
	return nil
}
