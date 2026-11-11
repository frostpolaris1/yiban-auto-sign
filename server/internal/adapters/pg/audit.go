package pg

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"

	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
)

// AuditRepo 是 PostgreSQL 审计仓储。
//
// 它是**链追加的唯一实现**。密钥来自配置；空密钥直接拒绝，
// 配置错误的进程无法写出无密钥的链。
type AuditRepo struct {
	db  *DB
	key []byte
}

// NewAuditRepo 构造审计仓储。key 是链的 HMAC 密钥。
func NewAuditRepo(db *DB, key []byte) (*AuditRepo, error) {
	if len(key) == 0 {
		return nil, errors.New("pg: audit key is required")
	}
	return &AuditRepo{db: db, key: append([]byte(nil), key...)}, nil
}

// Append 写入链的下一环。
//
// **必须在事务内运行**。它取 AuditChainLock、读链头、算哈希再插入，
// 故两个事务不可能追加同一前驱。
// 本行最后插入：长事务持有链锁，会阻塞每一次受审计写入。
// 无事务时报 ErrNoTransaction，**不得回落到连接池**：
// 回落到池连接会让审计行逃出调用方事务，链锁也随即释放。
func (r *AuditRepo) Append(ctx context.Context, in coreaudit.Entry) (coreaudit.Row, error) {
	q, err := r.db.txQueryer(ctx)
	if err != nil {
		return coreaudit.Row{}, err
	}
	if in.TS.IsZero() {
		return coreaudit.Row{}, validationError("audit timestamp is required")
	}
	if in.Action == "" {
		return coreaudit.Row{}, validationError("audit action is required")
	}
	if in.ActorLabel == "" {
		return coreaudit.Row{}, validationError("audit actor label is required")
	}
	detail, err := coreaudit.EncodeDetail(in.Detail)
	if err != nil {
		return coreaudit.Row{}, validationError(err.Error())
	}

	if _, err := q.Exec(ctx, "SELECT pg_advisory_xact_lock($1)", AuditChainLock); err != nil {
		return coreaudit.Row{}, mapError(err)
	}

	prev := coreaudit.ZeroHash()
	var head []byte
	if err := q.QueryRow(ctx,
		`SELECT hash FROM audit_logs ORDER BY id DESC LIMIT 1`).Scan(&head); err != nil {
		if !errors.Is(err, pgx.ErrNoRows) {
			return coreaudit.Row{}, mapError(err)
		}
	} else {
		prev = head
	}

	row := coreaudit.Row{
		TS:          in.TS.UTC().Truncate(time.Microsecond),
		SchoolID:    in.SchoolID,
		ActorUserID: in.ActorUserID,
		ActorRole:   string(in.ActorRole),
		ActorLabel:  in.ActorLabel,
		Action:      string(in.Action),
		TargetType:  in.TargetType,
		TargetID:    in.TargetID,
		Detail:      detail,
		PrevHash:    prev,
	}
	row.Hash = coreaudit.Hash(r.key, row)

	if err := q.QueryRow(ctx, `
		INSERT INTO audit_logs
		       (ts, school_id, actor_user_id, actor_role, actor_label, action,
		        target_type, target_id, detail, prev_hash, hash)
		VALUES ($1, NULLIF($2, '')::uuid, NULLIF($3, '')::uuid, NULLIF($4, '')::user_role,
		        $5, $6, $7, $8, $9, $10, $11)
		RETURNING id`,
		row.TS, row.SchoolID, row.ActorUserID, row.ActorRole, row.ActorLabel,
		row.Action, row.TargetType, row.TargetID, row.Detail, row.PrevHash, row.Hash).Scan(&row.ID); err != nil {
		return coreaudit.Row{}, mapError(err)
	}
	return row, nil
}

// Verify 在闭区间 id 范围内校验链。0 表示该侧不限。
// **空区间是错误**：什么都没看的校验不算通过。
func (r *AuditRepo) Verify(ctx context.Context, from, to int64) (coreaudit.Report, error) {
	rows, err := r.db.q(ctx).Query(ctx, `
		SELECT id, ts, coalesce(school_id::text, ''), coalesce(actor_user_id::text, ''),
		       coalesce(actor_role::text, ''), actor_label, action, target_type,
		       target_id, detail, prev_hash, hash
		  FROM audit_logs
		 WHERE ($1 = 0 OR id >= $1)
		   AND ($2 = 0 OR id <= $2)
		 ORDER BY id`, from, to)
	if err != nil {
		return coreaudit.Report{}, mapError(err)
	}
	defer rows.Close()

	entries, err := scanChainRows(rows)
	if err != nil {
		return coreaudit.Report{}, err
	}
	return coreaudit.Verify(r.key, entries)
}

// auditStatusBatchRows 是巡检每窗的行数上限。
//
// 内存上限：单窗最坏约为 2000 × (detail ≤ 4 KiB + 两条 32 字节哈希 + 字段)，
// 即个位数 MiB；链再长也按窗流式处理，内存不随链长增长。
// 代价是巡检耗时随行数线性增长；端点只对 master/admin 开放。
const auditStatusBatchRows = 2000

// VerifyStatus 分窗校验整条链，供巡检端点使用。
//
// 键集分页：每窗读 id 大于上一窗末行的一批行。窗内用 core 的 Verify
// 复算；窗与窗之间由 core 的 VerifyStream 比较首行 prev_hash 与上一窗
// 末行哈希。空链、断链都作为结论返回（err == nil），只有读库错误上抛。
// 读到不足一窗即结束：其后追加的行不在本次结论内。
// anchor 非 nil 时同时比对库外锚点（删前缀/截尾/清空/链尾重签）。
func (r *AuditRepo) VerifyStatus(ctx context.Context, anchor *coreaudit.Anchor) (coreaudit.Status, error) {
	return coreaudit.VerifyStreamWithAnchor(r.key, auditStatusBatchRows,
		func(afterID int64, limit int) ([]coreaudit.Row, error) {
			rows, err := r.db.q(ctx).Query(ctx, `
				SELECT id, ts, coalesce(school_id::text, ''), coalesce(actor_user_id::text, ''),
				       coalesce(actor_role::text, ''), actor_label, action, target_type,
				       target_id, detail, prev_hash, hash
				  FROM audit_logs
				 WHERE id > $1
				 ORDER BY id
				 LIMIT $2`, afterID, limit)
			if err != nil {
				return nil, mapError(err)
			}
			defer rows.Close()
			return scanChainRows(rows)
		}, anchor)
}

// Head 读取当前链头：最后一行 id、截至该行的行数与存储哈希。
//
// 单条语句取行与 count，二者来自同一快照：并发追加不会让锚点记下
// 指向不同行的 seq 与 count。空链返回 ok=false。
// 本方法不校验链，只服务 CLI 的 `audit anchor` 写锚点。
func (r *AuditRepo) Head(ctx context.Context) (coreaudit.Anchor, bool, error) {
	var seq, count int64
	var hash []byte
	err := r.db.q(ctx).QueryRow(ctx, `
		SELECT id, hash, (SELECT count(*) FROM audit_logs)
		  FROM audit_logs
		 ORDER BY id DESC
		 LIMIT 1`).Scan(&seq, &hash, &count)
	if errors.Is(err, pgx.ErrNoRows) {
		return coreaudit.Anchor{}, false, nil
	}
	if err != nil {
		return coreaudit.Anchor{}, false, mapError(err)
	}
	return coreaudit.Anchor{Seq: seq, Count: count, Hash: hash}, true, nil
}

// AnchorMeta 读取库内锚点高水位；无行返回 (nil, nil)。
// 高水位只用于检出锚点文件被截断/改写，不参与链内复算。
func (r *AuditRepo) AnchorMeta(ctx context.Context) (*coreaudit.AnchorMeta, error) {
	var seq int64
	var hash []byte
	err := r.db.q(ctx).QueryRow(ctx,
		`SELECT seq, head_hash FROM anchor_meta WHERE singleton`).Scan(&seq, &hash)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, mapError(err)
	}
	if len(hash) != 32 {
		return nil, fmt.Errorf("pg: anchor meta hash has the wrong length")
	}
	return &coreaudit.AnchorMeta{Seq: seq, HeadHash: hash}, nil
}

// RecordAnchorMeta 覆盖写单行高水位。
// 调用方负责保证 seq/head_hash 与锚点文件的最强行一致：
// 本方法只落库，不读锚点文件。
func (r *AuditRepo) RecordAnchorMeta(ctx context.Context, meta coreaudit.AnchorMeta) error {
	if meta.Seq < 1 || len(meta.HeadHash) != 32 {
		return validationError("invalid anchor meta")
	}
	_, err := r.db.q(ctx).Exec(ctx, `
		INSERT INTO anchor_meta (singleton, seq, head_hash, updated_at)
		VALUES (true, $1, $2, now())
		ON CONFLICT (singleton) DO UPDATE
		SET seq = EXCLUDED.seq,
		    head_hash = EXCLUDED.head_hash,
		    updated_at = EXCLUDED.updated_at`,
		meta.Seq, meta.HeadHash)
	return mapError(err)
}

// scanChainRows 把链行查询结果扫成 core 的 Row 切片。
func scanChainRows(rows pgx.Rows) ([]coreaudit.Row, error) {
	entries := make([]coreaudit.Row, 0, 64)
	for rows.Next() {
		var entry coreaudit.Row
		if err := rows.Scan(&entry.ID, &entry.TS, &entry.SchoolID, &entry.ActorUserID,
			&entry.ActorRole, &entry.ActorLabel, &entry.Action, &entry.TargetType,
			&entry.TargetID, &entry.Detail, &entry.PrevHash, &entry.Hash); err != nil {
			return nil, mapError(err)
		}
		entries = append(entries, entry)
	}
	if err := rows.Err(); err != nil {
		return nil, mapError(err)
	}
	return entries, nil
}

// ListLogs 读取审计只读列表，按 (ts DESC, id DESC) 键集分页。
// action、actor 与 target 是精确匹配；from/to 是闭区间时间界；
// actor 已由用例层掩码。本方法**不**校验链。
func (r *AuditRepo) ListLogs(ctx context.Context, query coreaudit.LogQuery) ([]coreaudit.LogRow, string, error) {
	if query.Limit < 1 || query.Limit > 200 {
		return nil, "", validationError("limit must be between 1 and 200")
	}
	where, args := auditListWhere(query)
	if query.Cursor != "" {
		at, id, err := decodeIntCursor(query.Cursor)
		if err != nil {
			return nil, "", err
		}
		args = append(args, at, id)
		where += fmt.Sprintf(" AND (ts, id) < ($%d, $%d)", len(args)-1, len(args))
	}
	sqlText := `SELECT id, ts, action, actor_label, target_id, detail FROM audit_logs` + where +
		` ORDER BY ts DESC, id DESC`
	sqlText, args = appendLimit(sqlText, args, query.Limit)

	rows, err := r.db.q(ctx).Query(ctx, sqlText, args...)
	if err != nil {
		return nil, "", mapError(err)
	}
	defer rows.Close()

	items := make([]coreaudit.LogRow, 0, query.Limit)
	for rows.Next() {
		var row coreaudit.LogRow
		if err := rows.Scan(&row.ID, &row.TS, &row.Action, &row.ActorLabel, &row.Target, &row.Detail); err != nil {
			return nil, "", mapError(err)
		}
		items = append(items, row)
	}
	if err := rows.Err(); err != nil {
		return nil, "", mapError(err)
	}
	more := len(items) > query.Limit
	if more {
		items = items[:query.Limit]
	}
	next := nextIntCursor(items, more, func(row coreaudit.LogRow) (time.Time, int64) {
		return row.TS, row.ID
	})
	return items, next, nil
}

// CountLogs 数同过滤条件下的总行数，忽略游标与 limit。
// 与 ListLogs 共用 auditListWhere：两处过滤口径不会漂移。
func (r *AuditRepo) CountLogs(ctx context.Context, query coreaudit.LogQuery) (int64, error) {
	where, args := auditListWhere(query)
	var total int64
	if err := r.db.q(ctx).QueryRow(ctx,
		`SELECT count(*) FROM audit_logs`+where, args...).Scan(&total); err != nil {
		return 0, mapError(err)
	}
	return total, nil
}

// auditListWhere 构造列表与计数共用的过滤子句。
// 返回的 clause 以 " WHERE TRUE" 开头；args 与占位符按出现顺序对应。
func auditListWhere(query coreaudit.LogQuery) (string, []any) {
	where := " WHERE TRUE"
	var args []any
	if query.Action != "" {
		args = append(args, query.Action)
		where += fmt.Sprintf(" AND action = $%d", len(args))
	}
	if query.Actor != "" {
		args = append(args, query.Actor)
		where += fmt.Sprintf(" AND actor_label = $%d", len(args))
	}
	if query.Target != "" {
		args = append(args, query.Target)
		where += fmt.Sprintf(" AND target_id = $%d", len(args))
	}
	if query.From != nil {
		args = append(args, query.From.UTC())
		where += fmt.Sprintf(" AND ts >= $%d", len(args))
	}
	if query.To != nil {
		args = append(args, query.To.UTC())
		where += fmt.Sprintf(" AND ts <= $%d", len(args))
	}
	return where, args
}
