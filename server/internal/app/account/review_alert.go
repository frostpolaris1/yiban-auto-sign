package account

// 本文件实现待审核通知的接线：账号进入或回到审核队列后发一条管理员事件。
//
// # 归属
// app 层用例。投递端口 ReviewAlerts 由 assembly 注入（app/notify.Dispatcher），
// 本文件不写 SQL、不拼文案。
//
// # 口径
// 事件只在事务提交成功后发：回滚的审核请求不得产生通知。
// 去重键＝账号+日（P3-14 同族、巡检新发现）：同日同账号折叠为同一行
// 站内信与同一封邮件；旧「键带事件 uuid」使 create→delete→create
// 每次都产生新事件、邮件面又无冷却，即邮件洪水。
// 日口径＝用例时钟所在日的 UTC 日期（同日折叠只治当日洪水；
// 跨日与窗口边缘由邮件面同题冷却兜底，见 app/notify）。
// 投递失败只记日志，不改业务返回。

import (
	"context"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/logfield"
	corenotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/notify"
)

// reviewAlertTimeout 是一次投递的上限：HTTP 请求不得因通知而长时间挂起。
const reviewAlertTimeout = 2 * time.Second

// reviewAlertDay 是待审核业务键的「同日」窗口：用例时钟所在日（UTC 日期）。
func reviewAlertDay(now time.Time) string {
	return now.UTC().Format("2006-01-02")
}

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
	day := reviewAlertDay(s.deps.Clock.Now())
	if err := s.deps.ReviewAlerts.Dispatch(ctx, corenotify.Event{
		Category:  corenotify.CategoryReviewPending,
		SchoolID:  record.SchoolID,
		AccountID: record.ID,
		Day:       day,
		// 同日同账号折叠：同一业务键的第二次提交命中存储去重键，
		// 不产生第二行站内信、第二封邮件与第二条 webhook 账本。
		DedupeKey: "review_pending/" + record.ID + "/" + day,
	}); err != nil {
		s.log.Warn("account: review pending alert dispatch failed",
			logfield.KeyEvent, "notify.dispatch_failed",
			logfield.KeyAccountID, record.ID,
			logfield.KeyError, err.Error())
	}
}
