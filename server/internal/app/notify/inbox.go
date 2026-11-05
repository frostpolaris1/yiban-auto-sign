package notify

// 本文件实现站内信读用例：列表、未读数、标记已读、全部已读。
//
// 归属：app 层用例。端口 InboxRepo 由 adapters/pg 实现；
// 本文件不写 SQL、不发 HTTP、不发邮件。
//
// 口径：所有查询都按会话用户收敛，**不存在**跨用户读取或写入的入参。
// 越权或已删除的 id 在存储层命中 0 行，即幂等忽略。
// 列表 limit 缺省 20、上限 100，与通用分页的 50/200 不同：
// 通知面是高频轮询，页大小单独定型。

import (
	"context"
	"errors"
	"time"

	"github.com/google/uuid"

	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corenotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/notify"
)

// 站内信分页与批量上限。
const (
	// DefaultNotificationPageSize 是未给 limit 时的页大小。
	DefaultNotificationPageSize = 20
	// MaxNotificationPageSize 是允许的最大页大小。
	MaxNotificationPageSize = 100
	// MaxMarkReadIDs 是单次标记已读的 id 上限。
	MaxMarkReadIDs = 100
)

// InboxRepo 是站内信读模型的持久化端口。
// 全部方法都以 userID 为作用域：实现不得返回或更新其他用户的行。
type InboxRepo interface {
	List(ctx context.Context, query corenotify.NotificationQuery) ([]corenotify.Notification, string, error)
	UnreadCount(ctx context.Context, userID string) (int64, error)
	MarkRead(ctx context.Context, userID string, ids []string, now time.Time) (int64, error)
	MarkAllRead(ctx context.Context, userID string, now time.Time) (int64, error)
}

// InboxDeps 是站内信读服务的依赖。
type InboxDeps struct {
	Repo  InboxRepo
	Clock coreclock.Clock
}

// Notification 是站内信读模型，转发自 core/notify。
type Notification = corenotify.Notification

// NotificationPage 是一页站内信。NextCursor 为空串表示末页。
type NotificationPage struct {
	Items      []corenotify.Notification
	NextCursor string
}

// Inbox 是站内信读用例服务。
type Inbox struct {
	repo  InboxRepo
	clock coreclock.Clock
}

// NewInbox 构造站内信读服务。缺端口或缺时钟即失败。
func NewInbox(deps InboxDeps) (*Inbox, error) {
	if deps.Repo == nil {
		return nil, errors.New("notify: nil inbox repo")
	}
	if deps.Clock == nil {
		return nil, errors.New("notify: nil clock")
	}
	return &Inbox{repo: deps.Repo, clock: deps.Clock}, nil
}

// List 返回 userID 的一页站内信。
// limit 必须显式落在 1..100；缺省 20 由入口层在 limit 缺失时填入。
// 越界一律校验失败，**不静默夹到边界**，也不把显式 0 当成「没给」。
func (s *Inbox) List(ctx context.Context, userID, cursor string, limit int) (NotificationPage, error) {
	if userID == "" {
		return NotificationPage{}, errs.New(errs.CodeInternal, "notifications require a user")
	}
	if limit < 1 || limit > MaxNotificationPageSize {
		return NotificationPage{}, errs.New(errs.CodeValidation,
			"limit must be between 1 and 100")
	}
	items, next, err := s.repo.List(ctx, corenotify.NotificationQuery{
		UserID: userID,
		Cursor: cursor,
		Limit:  limit,
	})
	if err != nil {
		return NotificationPage{}, err
	}
	return NotificationPage{Items: items, NextCursor: next}, nil
}

// UnreadCount 返回 userID 的未读数。
func (s *Inbox) UnreadCount(ctx context.Context, userID string) (int64, error) {
	if userID == "" {
		return 0, errs.New(errs.CodeInternal, "notifications require a user")
	}
	return s.repo.UnreadCount(ctx, userID)
}

// MarkRead 把 userID 的指定站内信标为已读。
// ids 必须非空、至多 100 条、全部是 uuid；越权或已删除的 id 由存储层忽略。
func (s *Inbox) MarkRead(ctx context.Context, userID string, ids []string) error {
	if userID == "" {
		return errs.New(errs.CodeInternal, "notifications require a user")
	}
	if len(ids) == 0 {
		return errs.New(errs.CodeValidation, "ids is required")
	}
	if len(ids) > MaxMarkReadIDs {
		return errs.New(errs.CodeValidation, "ids must contain at most 100 entries")
	}
	for _, id := range ids {
		if _, err := uuid.Parse(id); err != nil {
			return errs.New(errs.CodeValidation, "invalid notification id")
		}
	}
	_, err := s.repo.MarkRead(ctx, userID, ids, s.clock.Now())
	return err
}

// MarkAllRead 把 userID 的全部未读站内信标为已读。
// 已全部已读时是 no-op，重复调用幂等。
func (s *Inbox) MarkAllRead(ctx context.Context, userID string) error {
	if userID == "" {
		return errs.New(errs.CodeInternal, "notifications require a user")
	}
	_, err := s.repo.MarkAllRead(ctx, userID, s.clock.Now())
	return err
}
