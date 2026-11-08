package account

// 自动审批的单步异步核验（ykxf）。
//
// # 功能
// create 成功后，仅当开关开且客户端声明 confirm_defaults 时，
// 起一次有界后台核验：用存储凭据重登平台，复核平台 UniversityId
// 与账号学校绑定一致，全过则 pending→active 并通知管理员。
//
// # 归属
// app 层用例。平台认证经 IdentityVerifier；开关经 AutoApproveSettings；
// 通知复用 ReviewAlerts 端口。本文件不写 SQL、不拼文案。
//
// # 口径
// 单发不重试：任一不过或平台故障都留 pending，并写
// account.auto_check_failed（detail 只含结果词）。
// 核验在 Service 生命周期上下文内运行：Shutdown 先取消再等待。
// 审核竞态由 SetStatus 的 pending→active CAS 处理：人工已动过的行不覆盖。

import (
	"context"
	"time"

	"github.com/google/uuid"

	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/logfield"
	corenotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/notify"
	coreschool "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/school"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// AutoApproveSettings 读全局自动审批开关。
// 实现是 adapters/pg.SettingsRepo；nil 表示不核验。
type AutoApproveSettings interface {
	AutoApproveEnabled(ctx context.Context) (bool, error)
}

const (
	// autoApproveTimeout 是一次后台核验的总预算。
	// 平台登录链本身有单请求超时；这里给整次核验封顶，防泄漏。
	autoApproveTimeout = 30 * time.Second
	// autoApproveActor 是系统审计行的标签。与 engine 先例同形：
	// actor_user_id / actor_role 为空，label 标明子系统。
	autoApproveActor = "auto-approve"

	// 结果词是自由字符串，只表达分类，不含平台值或凭据。
	autoCheckPlatformError     = "platform_error"
	autoCheckCredentialMissing = "credential_unavailable"
	autoCheckSchoolMissing     = "school_unavailable"
	autoCheckSchoolDisabled    = "school_disabled"
	autoCheckSchoolUnbound     = "school_unbound"
	autoCheckIdentityMissing   = "identity_missing"
	autoCheckIdentityMismatch  = "identity_mismatch"
)

// Shutdown 取消在飞核验并等待它们退出，或上下文先到期。
// 可重复调用：cancel 幂等；等待者只等一次已启动的任务集。
func (s *Service) Shutdown(ctx context.Context) {
	s.cancel()
	done := make(chan struct{})
	go func() {
		s.bg.Wait()
		close(done)
	}()
	select {
	case <-done:
	case <-ctx.Done():
	}
}

// waitBackground 只供包内测试等待已启动的后台核验。
func (s *Service) waitBackground() {
	s.bg.Wait()
}

// startAutoApprove 在一次成功建号后启动后台核验。
// 缺开关或平台端口时不启动：自动审批是开关控制的可选面。
func (s *Service) startAutoApprove(record Record) {
	if s.deps.Settings == nil || s.deps.Identity == nil {
		return
	}
	s.bg.Add(1)
	go func() {
		defer s.bg.Done()
		ctx, cancel := context.WithTimeout(s.baseCtx, autoApproveTimeout)
		defer cancel()
		s.runAutoApprove(ctx, record.ID, record.SchoolID)
	}()
}

// runAutoApprove 执行单步核验。
// 顺序：开关 → 存储凭据 → 学校绑定 → 平台身份 → CAS 审核通过。
// 失败路径只追加一条结果词审计；成功路径在状态迁移事务内追加审计，
// 提交成功后才发管理员通知。
func (s *Service) runAutoApprove(ctx context.Context, accountID, schoolID string) {
	enabled, err := s.deps.Settings.AutoApproveEnabled(ctx)
	if err != nil {
		s.log.Warn("account: auto approve switch read failed",
			logfield.KeyEvent, "auto_approve.switch_failed",
			logfield.KeyAccountID, accountID,
			logfield.KeyReason, "store_unavailable")
		return
	}
	if !enabled {
		return
	}
	if ctx.Err() != nil {
		return
	}

	credential, err := s.SigningCredential(ctx, corescope.Master(), accountID)
	if err != nil {
		s.recordAutoCheckFailed(ctx, accountID, schoolID, autoCheckCredentialMissing)
		return
	}
	school, err := s.deps.Schools.Get(ctx, schoolID)
	if err != nil {
		s.recordAutoCheckFailed(ctx, accountID, schoolID, autoCheckSchoolMissing)
		return
	}
	if school.Status != coreschool.StatusActive {
		s.recordAutoCheckFailed(ctx, accountID, schoolID, autoCheckSchoolDisabled)
		return
	}
	if school.PlatformUniversityID == "" {
		s.recordAutoCheckFailed(ctx, accountID, schoolID, autoCheckSchoolUnbound)
		return
	}

	profile, err := s.deps.Identity.Verify(ctx, contract.VerifyRequest{
		Account: contract.AccountRef{
			ID:       accountID,
			SchoolID: schoolID,
			Platform: "yiban",
		},
		Credential: contract.Credential{
			Phone:      credential.Phone,
			Password:   credential.Password,
			DeviceCode: credential.DeviceCode,
			PhoneModel: credential.PhoneModel,
		},
	})
	if err != nil {
		s.recordAutoCheckFailed(ctx, accountID, schoolID, autoCheckPlatformError)
		return
	}
	if profile.UniversityID == "" {
		s.recordAutoCheckFailed(ctx, accountID, schoolID, autoCheckIdentityMissing)
		return
	}
	if profile.UniversityID != school.PlatformUniversityID {
		s.recordAutoCheckFailed(ctx, accountID, schoolID, autoCheckIdentityMismatch)
		return
	}

	var approved Record
	err = s.deps.Tx.InTx(ctx, func(ctx context.Context) error {
		var err error
		approved, err = s.deps.Accounts.SetStatus(ctx, corescope.Master(), accountID,
			StatusPending, StatusActive, s.deps.Clock.Now().UTC(), "")
		if err != nil {
			return err
		}
		_, err = s.deps.Audit.Append(ctx, coreaudit.Entry{
			TS:          s.deps.Clock.Now().UTC(),
			SchoolID:    approved.SchoolID,
			ActorUserID: "",
			ActorRole:   "",
			ActorLabel:  autoApproveActor,
			Action:      coreaudit.ActionAccountAutoApprove,
			TargetType:  "account",
			TargetID:    approved.ID,
			Detail: map[string]any{
				"field": "status",
				"from":  string(StatusPending),
				"to":    string(StatusActive),
			},
		})
		return err
	})
	if err != nil {
		// 人工审核已先一步改过状态：CAS 冲突不是故障，静默让位。
		if errs.From(err).Code == errs.CodeConflict {
			return
		}
		s.log.Warn("account: auto approve write failed",
			logfield.KeyEvent, "auto_approve.write_failed",
			logfield.KeyAccountID, accountID,
			logfield.KeyError, err.Error())
		return
	}
	s.alertAutoApproved(ctx, approved)
}

// recordAutoCheckFailed 在账号仍 pending 时追加失败审计。
// 已被人工审核或删除的行不再审计：结果已由人工事实决定。
func (s *Service) recordAutoCheckFailed(ctx context.Context, accountID, schoolID, result string) {
	if ctx.Err() != nil {
		return
	}
	record, err := s.deps.Accounts.Get(ctx, corescope.Master(), accountID)
	if err != nil || record.Status != StatusPending {
		return
	}
	err = s.deps.Tx.InTx(ctx, func(ctx context.Context) error {
		_, err := s.deps.Audit.Append(ctx, coreaudit.Entry{
			TS:          s.deps.Clock.Now().UTC(),
			SchoolID:    schoolID,
			ActorUserID: "",
			ActorRole:   "",
			ActorLabel:  autoApproveActor,
			Action:      coreaudit.ActionAccountAutoCheckFailed,
			TargetType:  "account",
			TargetID:    accountID,
			Detail:      map[string]any{"result": result},
		})
		return err
	})
	if err != nil {
		s.log.Warn("account: auto check failure audit failed",
			logfield.KeyEvent, "auto_approve.audit_failed",
			logfield.KeyAccountID, accountID,
			logfield.KeyError, err.Error())
	}
}

// alertAutoApproved 在核验通过后通知该校管理员。
// 复用 review_pending 类别与 0v8 投递体系；AutoApproved 让文案
// 显示「已自动通过」而不是「待审核」。失败只记日志。
func (s *Service) alertAutoApproved(ctx context.Context, record Record) {
	if s.deps.ReviewAlerts == nil {
		return
	}
	defer func() {
		if r := recover(); r != nil {
			s.log.Warn("account: auto approve alert dispatch panicked",
				logfield.KeyEvent, "notify.dispatch_panic",
				logfield.KeyAccountID, record.ID)
		}
	}()
	ctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), reviewAlertTimeout)
	defer cancel()
	if err := s.deps.ReviewAlerts.Dispatch(ctx, corenotify.Event{
		Category:     corenotify.CategoryReviewPending,
		SchoolID:     record.SchoolID,
		AccountID:    record.ID,
		AutoApproved: true,
		DedupeKey:    "account_auto_approve/" + record.ID + "/" + uuid.NewString(),
	}); err != nil {
		s.log.Warn("account: auto approve alert dispatch failed",
			logfield.KeyEvent, "notify.dispatch_failed",
			logfield.KeyAccountID, record.ID,
			logfield.KeyError, err.Error())
	}
}
