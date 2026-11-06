// Package account 的设备刷新用例（8v8②）。
//
// # 功能
// 读存量凭据 → 平台 getState → 成功且字段非空才写回凭据密文。
// 失败零写库零审计。
//
// # 归属
// app 层用例。DeviceReader 端口在本包声明，
// 实现由 assembly 用 signing/providers/yiban 注入：
// providers 不得 import 宿主，adapters 不得 import providers，
// 故适配器放组合根（先例：assembly 的 workerCredentials）。
//
// # 口径
// 明文凭据只经 SigningCredential 交给端口；端口返回值不含凭据。
// 平台返回的空字段**不覆盖**手填值；合并后无变化的成功不空转写版本，
// 也不推进账号 updated_at。
// 审计与凭据写回同事务，审计追加是事务最后一条语句。
// 明文设备码只走到入口层的掩码点，不落日志、不落审计、不进响应。
package account

import (
	"context"
	"errors"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// DeviceState 是平台登记设备的回读切片。
// 只含刷新需要的字段；协议类型留在 signing 层，不出端口。
type DeviceState struct {
	Authorized bool
	Code       string
	PhoneModel string
}

// DeviceReader 从平台读回一个账号登记的设备状态。
// 输入是已解密的使用中凭据与账号 ID（平台会话按账号缓存）。
// 实现必须把平台侧错误原样上抛；**不得**在实现里落库或写审计。
type DeviceReader interface {
	DeviceState(ctx context.Context, accountID string, cred SigningCredential) (DeviceState, error)
}

// DeviceRefreshResult 是一次成功刷新的合并结果。
// DeviceCode 是明文，只供入口层做掩码；**不得**记录或原样回显。
type DeviceRefreshResult struct {
	DeviceCode string
	PhoneModel string
	Authorized bool
}

// DeviceRefresh 读回平台登记的设备字段并写回凭据。
//
// 顺序：先取使用中凭据（作用域外与已软删一律 404），
// 再在事务外调平台；平台失败返回 upstream_unavailable，
// 此时凭据版本与审计行都是失败前的原样（安全不变量 8）。
// 成功后事务内合并写回：平台空字段不覆盖手填值，
// 无变化不空转写版本；设备字段真正变化时先锁账号行推进 updated_at，
// 再写凭据，使携带刷新前 expected_updated_at 的 PATCH 得到 409。
// 锁序与 Update（PATCH）一致，同一账号并发不会 40P01。
// 审计 account.device_refresh 与写回同事务。
func (s *Service) DeviceRefresh(ctx context.Context, scope corescope.Scope, id string, actor appaudit.Actor) (DeviceRefreshResult, error) {
	if err := app.RequireScope(scope); err != nil {
		return DeviceRefreshResult{}, err
	}
	credential, err := s.SigningCredential(ctx, scope, id)
	if err != nil {
		return DeviceRefreshResult{}, err
	}
	state, err := s.deps.Device.DeviceState(ctx, id, credential)
	if err != nil {
		// 平台侧细节（WAF 文案、状态码）不出本层：统一一个信封。
		return DeviceRefreshResult{}, errs.New(errs.CodeUpstream, "device refresh failed")
	}
	effective := DeviceRefreshResult{
		DeviceCode: credential.DeviceCode,
		PhoneModel: credential.PhoneModel,
		Authorized: state.Authorized,
	}
	if state.Code != "" {
		effective.DeviceCode = state.Code
	}
	if state.PhoneModel != "" {
		effective.PhoneModel = state.PhoneModel
	}
	// 平台回读也是写入路径：按契约上限截断，不让超长机型进密文或响应。
	effective.PhoneModel = truncateCredentialText(effective.PhoneModel)
	// 合并结果与刷新前的快照一致：不写凭据、不 touch，只追加审计。
	changed := effective.DeviceCode != credential.DeviceCode || effective.PhoneModel != credential.PhoneModel
	err = s.deps.Tx.InTx(ctx, func(ctx context.Context) error {
		now := s.deps.Clock.Now().UTC()
		if changed {
			// 锁序与 Update（PATCH）一致：先账号行、后凭据行。
			// 反过来先 ReplaceCredential 后 Touch，与 PATCH 并发时
			// PATCH 持账号锁等凭据、本事务持凭据等账号，会触发 40P01。
			// Touch 失败即整体回滚，凭据写回与审计都不留。
			if _, err := s.deps.Accounts.Touch(ctx, scope, id, now); err != nil {
				return err
			}
			if err := s.writeBackDevice(ctx, scope, id, effective); err != nil {
				return err
			}
		}
		_, err := s.deps.Audit.Append(ctx, coreaudit.Entry{
			TS:          now,
			SchoolID:    credential.SchoolID,
			ActorUserID: actor.UserID,
			ActorRole:   actor.Role,
			ActorLabel:  actor.Label,
			Action:      coreaudit.ActionAccountDeviceRefresh,
			TargetType:  "account",
			TargetID:    id,
			Detail:      map[string]any{"field": "device_code"},
		})
		return err
	})
	if err != nil {
		return DeviceRefreshResult{}, err
	}
	return effective, nil
}

// writeBackDevice 在事务内把合并后的设备字段写回凭据密文。
// 调用方必须已完成「确实变化」判定，并先持有账号行锁（Touch）：
// 锁序固定 accounts → credentials，与 Update（PATCH）一致。
// 凭据行在本事务内重读：与外层读之间若有并发改写，乐观锁冲突回滚，
// 审计一并作废——失败零写库的形状因此对并发也成立。
func (s *Service) writeBackDevice(ctx context.Context, scope corescope.Scope, id string, effective DeviceRefreshResult) error {
	if s.deps.Cipher == nil {
		return errors.New("account: credential cipher is not configured")
	}
	row, err := s.deps.Accounts.Credential(ctx, scope, id)
	if err != nil {
		return err
	}
	payload, err := s.deps.Cipher.Open(row)
	if err != nil {
		return err
	}
	payload.DeviceCode = effective.DeviceCode
	payload.PhoneModel = effective.PhoneModel
	sealed, err := s.deps.Cipher.Seal(row.SchoolID, id, payload)
	if err != nil {
		return err
	}
	return s.deps.Accounts.ReplaceCredential(ctx, sealed, row.Version)
}
