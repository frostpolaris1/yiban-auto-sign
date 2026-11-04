// 通知订阅覆盖表的 PostgreSQL 适配器。
//
// 功能：读一个用户的覆盖行；原子替换覆盖集。
// 本文件**不**判角色、**不**补默认、**不**投递通知。
//
// 归属：adapters 层。app/notify 只依赖 Repo 端口，不见本类型。
//
// 口径：表只存覆盖；缺行由用例按 core/notify 的默认表解释。
// Replace 在单事务内先删后插：PUT 的语义是完整替换。
// 入库的类别/渠道都经过用例词表校验；本层不再加 CHECK
// （同一不变式只留一道守卫，见迁移 00023）。

package pg

import (
	"context"
	"fmt"

	corenotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/notify"
)

// NotificationPrefsRepo 读写 notification_prefs 表。
type NotificationPrefsRepo struct {
	db *DB
}

// NewNotificationPrefsRepo 构造订阅仓储。
func NewNotificationPrefsRepo(db *DB) *NotificationPrefsRepo {
	return &NotificationPrefsRepo{db: db}
}

// Overrides 返回一个用户的显式覆盖行。
// 存储里出现词表外的类别/渠道时报错，**不静默丢弃**：
// 丢弃会让界面把一条已存订阅显示成默认值。
func (r *NotificationPrefsRepo) Overrides(ctx context.Context, userID string) (map[corenotify.Pair]bool, error) {
	rows, err := r.db.q(ctx).Query(ctx,
		`SELECT category, channel, enabled FROM notification_prefs WHERE user_id = $1::uuid`, userID)
	if err != nil {
		return nil, mapError(err)
	}
	defer rows.Close()

	overrides := map[corenotify.Pair]bool{}
	for rows.Next() {
		var category, channel string
		var enabled bool
		if err := rows.Scan(&category, &channel, &enabled); err != nil {
			return nil, mapError(err)
		}
		pair := corenotify.Pair{
			Category: corenotify.Category(category),
			Channel:  corenotify.Channel(channel),
		}
		if !pair.Category.Valid() {
			return nil, fmt.Errorf("pg: unknown notification category %q in storage", category)
		}
		if !pair.Channel.Valid() {
			return nil, fmt.Errorf("pg: unknown notification channel %q in storage", channel)
		}
		overrides[pair] = enabled
	}
	if err := rows.Err(); err != nil {
		return nil, mapError(err)
	}
	return overrides, nil
}

// Replace 原子替换一个用户的全部覆盖行。
// 空列表只删不插：用户回到全默认。
func (r *NotificationPrefsRepo) Replace(ctx context.Context, userID string, prefs []corenotify.Pref) error {
	if userID == "" {
		return validationError("user id is required")
	}
	return r.db.InTx(ctx, func(ctx context.Context) error {
		if _, err := r.db.q(ctx).Exec(ctx,
			`DELETE FROM notification_prefs WHERE user_id = $1::uuid`, userID); err != nil {
			return mapError(err)
		}
		if len(prefs) == 0 {
			return nil
		}
		categories := make([]string, 0, len(prefs))
		channels := make([]string, 0, len(prefs))
		enabled := make([]bool, 0, len(prefs))
		for _, pref := range prefs {
			categories = append(categories, string(pref.Category))
			channels = append(channels, string(pref.Channel))
			enabled = append(enabled, pref.Enabled)
		}
		_, err := r.db.q(ctx).Exec(ctx, `
			INSERT INTO notification_prefs (user_id, category, channel, enabled)
			SELECT $1::uuid, c, ch, e
			  FROM unnest($2::text[], $3::text[], $4::boolean[]) AS t(c, ch, e)`,
			userID, categories, channels, enabled)
		return mapError(err)
	})
}
