package pg

import (
	"context"

	coreegress "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/egress"
)

// EgressRepo 是出口与粘性绑定的仓储。
type EgressRepo struct {
	db *DB
}

// NewEgressRepo 构造出口仓储。
func NewEgressRepo(db *DB) *EgressRepo { return &EgressRepo{db: db} }

// ListEnabled 返回启用出口，按 ID 升序（顺序参与确定性取模）。
func (r *EgressRepo) ListEnabled(ctx context.Context) ([]coreegress.Record, error) {
	rows, err := r.db.q(ctx).Query(ctx,
		`SELECT id::text, identity FROM egresses WHERE enabled ORDER BY id`)
	if err != nil {
		return nil, mapError(err)
	}
	defer rows.Close()
	var out []coreegress.Record
	for rows.Next() {
		var e coreegress.Record
		if err := rows.Scan(&e.ID, &e.Identity); err != nil {
			return nil, mapError(err)
		}
		out = append(out, e)
	}
	return out, mapError(rows.Err())
}

// GetAssignment 返回账号已绑定的出口 ID；无绑定返回 errs.ErrNotFound。
func (r *EgressRepo) GetAssignment(ctx context.Context, accountID string) (string, error) {
	var id string
	err := r.db.q(ctx).QueryRow(ctx,
		`SELECT egress_id::text FROM account_egress WHERE account_id = $1::uuid`,
		accountID).Scan(&id)
	if err != nil {
		return "", mapError(err)
	}
	return id, nil
}

// AssignOrGet 原子写入首选绑定并返回最终绑定：
// 单语句 upsert。冲突分支是**空更新**，只为了让语句必定返回既有行——
// `DO NOTHING` + 子查询在 READ COMMITTED 下会读不到并发刚提交的行
// （语句快照先于对方提交），表现为偶发 not_found。
func (r *EgressRepo) AssignOrGet(ctx context.Context, accountID, preferredEgressID string) (string, error) {
	var id string
	err := r.db.q(ctx).QueryRow(ctx,
		`INSERT INTO account_egress (account_id, egress_id)
		 VALUES ($1::uuid, $2::uuid)
		 ON CONFLICT (account_id) DO UPDATE SET egress_id = account_egress.egress_id
		 RETURNING egress_id::text`,
		accountID, preferredEgressID).Scan(&id)
	if err != nil {
		return "", mapInsertForeignKey(err)
	}
	return id, nil
}
