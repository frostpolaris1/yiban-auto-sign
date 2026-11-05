package pg

// 每日签到失败摘要的取数与收件人解析。
//
// 功能：某业务日 state='failed' 的任务明细（账号、学校、手机号、
// result、attempts、updated_at），以及管理员摘要收件人
// （master 全量 + admin 绑定校 + sign_failed_daily 覆盖订阅）。
// 本文件不按账号聚合、不判默认订阅、不拼文案、不投递邮件。
//
// 归属：adapters 层。app/notify 只依赖端口，不见本类型。
//
// 口径：失败明细只取未软删账号；账号状态不参与过滤——任务行已经
// 落 failed 是事实，软删才是收件人不可读。收件人只取 active 且未软删的
// master/admin；admin 无绑定校时 Schools 为空集合（不等于全量）。
// 手机号与邮箱只进读模型，由用例掩码后再落库/进日志。

import (
	"context"
	"time"

	corenotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/notify"
)

// NotificationDigestRepo 读取每日摘要的失败明细与收件人。
type NotificationDigestRepo struct{ db *DB }

// NewNotificationDigestRepo 构造摘要仓储。
func NewNotificationDigestRepo(db *DB) *NotificationDigestRepo {
	return &NotificationDigestRepo{db: db}
}

// failedAccountsSQL 读取某业务日的失败明细。
// 排序 (account, updated_at, id) 让用例的「同账号取最新」有稳定输入。
const failedAccountsSQL = `
	SELECT t.account_id::text, a.school_id::text, a.phone, t.result, t.attempts,
	       t.updated_at
	  FROM sign_tasks t
	  JOIN accounts a ON a.id = t.account_id
	 WHERE t.day = $1::date
	   AND t.state = 'failed'
	   AND a.deleted_at IS NULL
	 ORDER BY t.account_id, t.updated_at, t.id`

// FailedAccounts 返回某业务日 state='failed' 的任务明细。
// 只取未软删账号；同一账号可能返回多行（多任务），聚合由用例负责。
func (r *NotificationDigestRepo) FailedAccounts(ctx context.Context, day string) ([]corenotify.FailedTask, error) {
	if day == "" {
		return nil, validationError("business day is required")
	}
	if _, err := time.Parse("2006-01-02", day); err != nil {
		return nil, validationError("business day must be YYYY-MM-DD")
	}
	rows, err := r.db.q(ctx).Query(ctx, failedAccountsSQL, day)
	if err != nil {
		return nil, mapError(err)
	}
	defer rows.Close()

	items := make([]corenotify.FailedTask, 0, 16)
	for rows.Next() {
		var item corenotify.FailedTask
		if err := rows.Scan(&item.AccountID, &item.SchoolID, &item.Phone,
			&item.Result, &item.Attempts, &item.UpdatedAt); err != nil {
			return nil, mapError(err)
		}
		items = append(items, item)
	}
	if err := rows.Err(); err != nil {
		return nil, mapError(err)
	}
	return items, nil
}

// digestSubscribersSQL 返回管理员摘要收件人与绑定校、覆盖订阅。
//
// LEFT JOIN 让「无绑定校」「无覆盖行」也出现在结果里：前者是空 scope，
// 后者由用例按默认表解释。绑定校与订阅是两张表，故同一用户会展开成
// 多行；本方法按用户收敛，学校去重。
const digestSubscribersSQL = `
	SELECT u.id::text, u.email, u.role = 'master',
	       coalesce(a.school_id::text, ''),
	       coalesce(p.category, ''), coalesce(p.channel, ''), coalesce(p.enabled, false)
	  FROM users u
	  LEFT JOIN admin_schools a ON a.user_id = u.id
	  LEFT JOIN notification_prefs p ON p.user_id = u.id
	 WHERE u.role IN ('master', 'admin')
	   AND u.status = 'active'
	   AND u.deleted_at IS NULL
	 ORDER BY u.id, a.school_id, p.category, p.channel`

// DigestSubscribers 返回全部管理员摘要收件人。
// master 收全量；admin 的 Schools 是绑定校集合，空集合表示没有 scope。
func (r *NotificationDigestRepo) DigestSubscribers(ctx context.Context) ([]corenotify.DigestSubscriber, error) {
	rows, err := r.db.q(ctx).Query(ctx, digestSubscribersSQL)
	if err != nil {
		return nil, mapError(err)
	}
	defer rows.Close()

	var out []corenotify.DigestSubscriber
	for rows.Next() {
		var (
			userID, email, schoolID, category, channel string
			master, enabledValue                       bool
		)
		if err := rows.Scan(&userID, &email, &master, &schoolID,
			&category, &channel, &enabledValue); err != nil {
			return nil, mapError(err)
		}
		if len(out) == 0 || out[len(out)-1].UserID != userID {
			out = append(out, corenotify.DigestSubscriber{
				UserID:    userID,
				Email:     email,
				Master:    master,
				Overrides: map[corenotify.Pair]bool{},
			})
		}
		current := &out[len(out)-1]
		if schoolID != "" && !containsString(current.Schools, schoolID) {
			current.Schools = append(current.Schools, schoolID)
		}
		if category == "" {
			continue
		}
		pair := corenotify.Pair{
			Category: corenotify.Category(category),
			Channel:  corenotify.Channel(channel),
		}
		// 存储里出现词表外的值时报错，**不静默丢弃**：
		// 丢弃会让一条已存订阅被当成默认值解释。
		if !pair.Category.Valid() {
			return nil, validationError("unknown notification category in storage")
		}
		if !pair.Channel.Valid() {
			return nil, validationError("unknown notification channel in storage")
		}
		current.Overrides[pair] = enabledValue
	}
	if err := rows.Err(); err != nil {
		return nil, mapError(err)
	}
	return out, nil
}

// containsString 报告切片里是否已有该值。
// 绑定校数量是管理员可授范围，线性扫描足够。
func containsString(items []string, value string) bool {
	for _, item := range items {
		if item == value {
			return true
		}
	}
	return false
}
