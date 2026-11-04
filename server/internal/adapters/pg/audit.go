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

	var entries []coreaudit.Row
	for rows.Next() {
		var entry coreaudit.Row
		if err := rows.Scan(&entry.ID, &entry.TS, &entry.SchoolID, &entry.ActorUserID,
			&entry.ActorRole, &entry.ActorLabel, &entry.Action, &entry.TargetType,
			&entry.TargetID, &entry.Detail, &entry.PrevHash, &entry.Hash); err != nil {
			return coreaudit.Report{}, mapError(err)
		}
		entries = append(entries, entry)
	}
	if err := rows.Err(); err != nil {
		return coreaudit.Report{}, mapError(err)
	}
	return coreaudit.Verify(r.key, entries)
}

// ListLogs 读取审计只读列表，按 (ts DESC, id DESC) 键集分页。
// action 与 actor 是精确匹配；from/to 是闭区间时间界；
// actor 已由用例层掩码。本方法**不**校验链、**不**返回目标字段。
func (r *AuditRepo) ListLogs(ctx context.Context, query coreaudit.LogQuery) ([]coreaudit.LogRow, string, error) {
	if query.Limit < 1 || query.Limit > 200 {
		return nil, "", validationError("limit must be between 1 and 200")
	}
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
	if query.From != nil {
		args = append(args, query.From.UTC())
		where += fmt.Sprintf(" AND ts >= $%d", len(args))
	}
	if query.To != nil {
		args = append(args, query.To.UTC())
		where += fmt.Sprintf(" AND ts <= $%d", len(args))
	}
	if query.Cursor != "" {
		at, id, err := decodeIntCursor(query.Cursor)
		if err != nil {
			return nil, "", err
		}
		args = append(args, at, id)
		where += fmt.Sprintf(" AND (ts, id) < ($%d, $%d)", len(args)-1, len(args))
	}
	sqlText := `SELECT id, ts, action, actor_label, detail FROM audit_logs` + where +
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
		if err := rows.Scan(&row.ID, &row.TS, &row.Action, &row.ActorLabel, &row.Detail); err != nil {
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
