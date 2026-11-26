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
// 审核竞态由 pending→active 的状态 CAS 处理：人工已动过的行不覆盖。
// 凭据竞态由 SetStatusIfCredentialVersion 的版本 CAS 处理：核验期间
// 凭据被替换（ReplaceCredential 会 version+1）时零行，账号留 pending。
// 两条 CAS 在同一条件 UPDATE 里，激活与「用的是哪一版凭据」不可分。
// P3-14 通知归属：Create 判「armed＝开关开且端口齐」时**不发** pending
// 通知，把唯一一条通知交给核验收尾——通过发 auto_approved，
// 留在 pending 的每一条早退路径（含开关中途关闭与竞写失败）补发
// pending；CAS 冲突（人工已定）不发。未 armed 时 pending 通知照旧由
// Create 发，核验成功不再补发（开关中途翻开的窗口取「一条通知」
// 而不是最终态双发）。

import (
	"context"
	"time"

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
	// autoCheckCredentialChanged 表示核验通过时凭据已被替换：
	// 平台结论属于旧凭据，不能激活；账号留 pending 等人工。
	autoCheckCredentialChanged = "credential_changed"
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
// ownsAlert 标记「唯一一条通知归核验面收尾」（P3-14，由 Create 判定传入）。
func (s *Service) startAutoApprove(record Record, ownsAlert bool) {
	if s.deps.Settings == nil || s.deps.Identity == nil {
		return
	}
	s.bg.Add(1)
	go func() {
		defer s.bg.Done()
		ctx, cancel := context.WithTimeout(s.baseCtx, autoApproveTimeout)
		defer cancel()
		s.runAutoApprove(ctx, record.ID, record.SchoolID, ownsAlert)
	}()
}

// runAutoApprove 执行单步核验。
// 顺序：开关 → 存储凭据 → 学校绑定 → 平台身份 → CAS 审核通过。
// 失败路径只追加一条结果词审计；成功路径在状态迁移事务内追加审计，
// 提交成功后才发管理员通知。ownsAlert 时每条「留在 pending」的早退
// 都补发 pending 通知（P3-14：armed 建号只发最终态一条）。
func (s *Service) runAutoApprove(ctx context.Context, accountID, schoolID string, ownsAlert bool) {
	enabled, err := s.deps.Settings.AutoApproveEnabled(ctx)
	if err != nil {
		s.log.Warn("account: auto approve switch read failed",
			logfield.KeyEvent, "auto_approve.switch_failed",
			logfield.KeyAccountID, accountID,
			logfield.KeyReason, "store_unavailable")
		s.alertStaysPending(ctx, accountID, schoolID, ownsAlert)
		return
	}
	if !enabled {
		s.alertStaysPending(ctx, accountID, schoolID, ownsAlert)
		return
	}
	if ctx.Err() != nil {
		s.alertStaysPending(ctx, accountID, schoolID, ownsAlert)
		return
	}

	credential, err := s.SigningCredential(ctx, corescope.Master(), accountID)
	if err != nil {
		s.recordAutoCheckFailed(ctx, accountID, schoolID, autoCheckCredentialMissing)
		s.alertStaysPending(ctx, accountID, schoolID, ownsAlert)
		return
	}
	school, err := s.deps.Schools.Get(ctx, schoolID)
	if err != nil {
		s.recordAutoCheckFailed(ctx, accountID, schoolID, autoCheckSchoolMissing)
		s.alertStaysPending(ctx, accountID, schoolID, ownsAlert)
		return
	}
	if school.Status != coreschool.StatusActive {
		s.recordAutoCheckFailed(ctx, accountID, schoolID, autoCheckSchoolDisabled)
		s.alertStaysPending(ctx, accountID, schoolID, ownsAlert)
		return
	}
	if school.PlatformUniversityID == "" {
		s.recordAutoCheckFailed(ctx, accountID, schoolID, autoCheckSchoolUnbound)
		s.alertStaysPending(ctx, accountID, schoolID, ownsAlert)
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
		s.alertStaysPending(ctx, accountID, schoolID, ownsAlert)
		return
	}
	if profile.UniversityID == "" {
		s.recordAutoCheckFailed(ctx, accountID, schoolID, autoCheckIdentityMissing)
		s.alertStaysPending(ctx, accountID, schoolID, ownsAlert)
		return
	}
	if profile.UniversityID != school.PlatformUniversityID {
		s.recordAutoCheckFailed(ctx, accountID, schoolID, autoCheckIdentityMismatch)
		s.alertStaysPending(ctx, accountID, schoolID, ownsAlert)
		return
	}

	var approved Record
	err = s.deps.Tx.InTx(ctx, func(ctx context.Context) error {
		var err error
		// 激活必须与「核验用的是哪一版凭据」同条件：凭据在核验期间
		// 被替换时行不匹配，账号保持 pending。
		approved, err = s.deps.Accounts.SetStatusIfCredentialVersion(ctx, corescope.Master(), accountID,
			StatusPending, StatusActive, credential.CredentialVersion, s.deps.Clock.Now().UTC(), "")
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
		// CAS 冲突有两类，靠事务外的行状态区分：
		//   - 人工已动过状态（行不再是 pending）：静默让位，状态已由
		//     人工事实决定，本面不得替它发通知；
		//   - 行仍 pending 且可见：只剩「凭据版本已变」一种原因，记结果词
		//     并按 P3-14 补发 pending 通知（armed 时唯一一条最终态）。
		if errs.From(err).Code == errs.CodeConflict {
			record, getErr := s.deps.Accounts.Get(ctx, corescope.Master(), accountID)
			if getErr != nil {
				// 冲突后读不到行只影响「要不要补结果词」，不改返回值；
				// 取消是常态（Shutdown 先取消），不刷告警。
				if ctx.Err() == nil {
					s.log.Warn("account: auto approve conflict lookup failed",
						logfield.KeyEvent, "auto_approve.conflict_lookup_failed",
						logfield.KeyAccountID, accountID,
						logfield.KeyError, getErr.Error())
				}
				return
			}
			if record.Status != StatusPending {
				return
			}
			// 通知跟「行仍 pending」这个结论走，不跟审计是否落地走：
			// recordAutoCheckFailed 会再复核一次，人工已改终态时返回 false；
			// 审计写失败只 Warn，行已确认 pending 仍返回 true。
			if s.recordAutoCheckFailed(ctx, accountID, schoolID, autoCheckCredentialChanged) {
				s.alertStaysPending(ctx, accountID, schoolID, ownsAlert)
			}
			return
		}
		s.log.Warn("account: auto approve write failed",
			logfield.KeyEvent, "auto_approve.write_failed",
			logfield.KeyAccountID, accountID,
			logfield.KeyError, err.Error())
		s.alertStaysPending(ctx, accountID, schoolID, ownsAlert)
		return
	}
	// P3-14：未 armed 时 pending 通知已由 Create 发出，成功不再补发
	// （开关在 Create 与核验之间翻开的窄窗口取「一条通知」）。
	if !ownsAlert {
		return
	}
	s.alertAutoApproved(ctx, approved)
}

// alertStaysPending 在 armed 核验把账号留在 pending 时补发唯一一条待审核通知。
// 未 armed（pending 通知已由 Create 发）时静默；record 用 id+school 拼最小形状，
// alertReviewPending 只读这两个字段。
func (s *Service) alertStaysPending(ctx context.Context, accountID, schoolID string, ownsAlert bool) {
	if !ownsAlert {
		return
	}
	s.alertReviewPending(ctx, Record{ID: accountID, SchoolID: schoolID})
}

// recordAutoCheckFailed 在账号仍 pending 时追加失败审计，并返回「是否仍应通知」。
// 返回值只回答行是否仍 pending，不表示审计已经落地：复核发现读不到行、
// 或已被人工改成终态才返回 false；审计写失败只 Warn，调用方仍应补发通知。
// 返回值把「通知」钉在行状态上：调用方第一次读到的 pending 可能在这里
// 第二次读之前已被人工改成终态，此时跳过审计也不得补发待审通知。
func (s *Service) recordAutoCheckFailed(ctx context.Context, accountID, schoolID, result string) bool {
	if ctx.Err() != nil {
		return false
	}
	record, err := s.deps.Accounts.Get(ctx, corescope.Master(), accountID)
	if err != nil || record.Status != StatusPending {
		return false
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
		// 审计失败不改变「行仍 pending、用户仍欠一条通知」的事实。
		return true
	}
	return true
}

// alertAutoApproved 在核验通过后通知该校管理员。
// 复用 review_pending 类别与 0v8 投递体系；AutoApproved 让文案
// 显示「已自动通过」而不是「待审核」。去重键同样＝账号+日（P3-14：
// 通知归属唯一，同账号同日不产生第二行）。失败只记日志。
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
	day := reviewAlertDay(s.deps.Clock.Now())
	if err := s.deps.ReviewAlerts.Dispatch(ctx, corenotify.Event{
		Category:     corenotify.CategoryReviewPending,
		SchoolID:     record.SchoolID,
		AccountID:    record.ID,
		Day:          day,
		AutoApproved: true,
		DedupeKey:    "account_auto_approve/" + record.ID + "/" + day,
	}); err != nil {
		s.log.Warn("account: auto approve alert dispatch failed",
			logfield.KeyEvent, "notify.dispatch_failed",
			logfield.KeyAccountID, record.ID,
			logfield.KeyError, err.Error())
	}
}
