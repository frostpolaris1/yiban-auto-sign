package account

// 本文件实现待审核通知的接线：账号进入或回到审核队列后发一条管理员事件。
//
// # 归属
// app 层用例。投递端口 ReviewAlerts 由 assembly 注入（app/notify.Dispatcher），
// 本文件不写 SQL、不拼文案。
//
// # 口径
// 事件只在事务提交成功后发：回滚的审核请求不得产生通知。
// 每次进出审核队列都是新事件，去重键带事件 uuid；
// 同一次请求重放不会产生第二个键。
// 投递失败只记日志，不改业务返回。

import (
	"context"
	"time"

	"github.com/google/uuid"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/logfield"
	corenotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/notify"
)

// reviewAlertTimeout 是一次投递的上限：HTTP 请求不得因通知而长时间挂起。
const reviewAlertTimeout = 2 * time.Second

// alertReviewPending 发一条待审核通知。
// 端口为 nil 时不投递；失败只记日志，panic 记一条不含 panic 值的警告。
func (s *Service) alertReviewPending(ctx context.Context, record Record) {
	if s.deps.ReviewAlerts == nil {
		return
	}
	defer func() {
		if r := recover(); r != nil {
			s.log.Warn("account: review alert dispatch panicked",
				logfield.KeyEvent, "notify.dispatch_panic",
				logfield.KeyAccountID, record.ID)
		}
	}()
	ctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), reviewAlertTimeout)
	defer cancel()
	if err := s.deps.ReviewAlerts.Dispatch(ctx, corenotify.Event{
		Category:  corenotify.CategoryReviewPending,
		SchoolID:  record.SchoolID,
		AccountID: record.ID,
		// 每次提交都是新事件：同账号重提必须产生第二行通知。
		DedupeKey: "review_pending/" + record.ID + "/" + uuid.NewString(),
	}); err != nil {
		s.log.Warn("account: review pending alert dispatch failed",
			logfield.KeyEvent, "notify.dispatch_failed",
			logfield.KeyAccountID, record.ID,
			logfield.KeyError, err.Error())
	}
}
