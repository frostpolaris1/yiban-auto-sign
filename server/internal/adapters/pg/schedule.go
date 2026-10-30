package pg

import (
	"context"
	"errors"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"

	coreaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/account"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
)

// ErrScheduleConflict 是 CAS 认领竞争失败的错误：
// 另一个 worker 已把该行推进到非 pending 态。
var ErrScheduleConflict = coreerrs.New(coreerrs.CodeConflict, "pg: sign task already claimed")

// ErrWindowStale 是窗口版本竞争失败的错误：库里已有更新的版本。
// 哨兵在 core/schedule：用例层与适配器按同一对象判定。
var ErrWindowStale = coreschedule.ErrWindowStale

// ErrScheduleAuditMissing 表示一次写账号状态的结算没有审计仓储。
// **缺依赖即报错**：引擎写状态必须带审计，不得静默跳过。
var ErrScheduleAuditMissing = errors.New("pg: schedule repo has no audit repo")

// credentialInvalidResult 是凭据错的结算结果词。
// 与 signing/contract.OutcomeCredentialBad 同词：本层不导入 signing 包，
// 故按字面复写，并由结算 e2e 逐字钉住。
const credentialInvalidResult = "credential_invalid"

// ScheduleRepo 是排期四表的仓储。
// 唯一约束 (account_id, school_task_id, day) 是 CAS 判据的库级兜底：
// 应用层条件 UPDATE 之外，重复插行必然失败。
type ScheduleRepo struct {
	db     *DB
	audits *AuditRepo
	// lease 是每次认领写入的租约时长。零值取 2 分钟。
	lease time.Duration
}

// NewScheduleRepo 构造排期仓储。未注入审计仓储时，
// 任何写账号状态的结算都返回 ErrScheduleAuditMissing。
func NewScheduleRepo(db *DB) *ScheduleRepo {
	return &ScheduleRepo{db: db, lease: 2 * time.Minute}
}

// NewScheduleRepoWithAudit 构造带审计仓储的排期仓储。
// 引擎写账号状态的结算必须用它装配。
func NewScheduleRepoWithAudit(db *DB, audits *AuditRepo) *ScheduleRepo {
	return &ScheduleRepo{db: db, audits: audits, lease: 2 * time.Minute}
}

// WithLease 覆盖认领租约时长。d<=0 时保持缺省 2 分钟。
// 返回同一实例：装配层在构造后立刻调用，之后只读。
func (r *ScheduleRepo) WithLease(d time.Duration) *ScheduleRepo {
	if d > 0 {
		r.lease = d
	}
	return r
}

// CreateSchoolTask 建一条学校任务。同名任务返回既有行（幂等）。
func (r *ScheduleRepo) CreateSchoolTask(ctx context.Context, schoolID, name string) (string, error) {
	var id string
	err := r.db.q(ctx).QueryRow(ctx,
		`INSERT INTO school_tasks (school_id, name) VALUES ($1::uuid, $2)
		 ON CONFLICT (school_id, name) DO UPDATE SET name = EXCLUDED.name
		 WHERE school_tasks.name = EXCLUDED.name
		 RETURNING id`,
		schoolID, name).Scan(&id)
	if err != nil {
		return "", mapInsertForeignKey(err)
	}
	return id, nil
}

// GetSchoolTask 按学校与任务名取任务 ID；不存在返回 errs.ErrNotFound。
func (r *ScheduleRepo) GetSchoolTask(ctx context.Context, schoolID, name string) (string, error) {
	var id string
	err := r.db.q(ctx).QueryRow(ctx,
		`SELECT id FROM school_tasks WHERE school_id = $1::uuid AND name = $2`,
		schoolID, name).Scan(&id)
	if err != nil {
		return "", mapError(err)
	}
	return id, nil
}

// PutWindow 写窗口缓存。expectedVersion 为 0 时首写（冲突则版本 +1 覆盖）；
// 大于 0 时做乐观锁：版本对不上即 ErrWindowStale。
// 版本重排（平台窗口变更）由此推进：新版本覆盖旧值，计划层据此重排。
func (r *ScheduleRepo) PutWindow(ctx context.Context, w coreschedule.WindowRecord, expectedVersion int) (int, error) {
	var version int
	var err error
	if expectedVersion > 0 {
		err = r.db.q(ctx).QueryRow(ctx,
			`UPDATE window_cache SET start_at = $3, end_at = $4, source = $5, version = version + 1
			 WHERE school_task_id = $1::uuid AND day = $2 AND version = $6
			 RETURNING version`,
			w.SchoolTaskID, w.Day, w.Start, w.End, w.Source, expectedVersion).Scan(&version)
		if errors.Is(err, pgx.ErrNoRows) {
			return 0, ErrWindowStale
		}
	} else {
		err = r.db.q(ctx).QueryRow(ctx,
			`INSERT INTO window_cache (school_task_id, day, start_at, end_at, source, version)
			 VALUES ($1::uuid, $2, $3, $4, $5, 1)
			 ON CONFLICT (school_task_id, day) DO UPDATE
			 SET start_at = EXCLUDED.start_at, end_at = EXCLUDED.end_at,
			     source = EXCLUDED.source, version = window_cache.version + 1
			 RETURNING version`,
			w.SchoolTaskID, w.Day, w.Start, w.End, w.Source).Scan(&version)
	}
	if err != nil {
		return 0, mapError(err)
	}
	return version, nil
}

// GetWindow 取某任务某日的窗口；不存在返回 errs.ErrNotFound。
// day 列是 date 型：经 time.Time 中转再格式化，pgx 不把 date 扫进 string。
func (r *ScheduleRepo) GetWindow(ctx context.Context, schoolTaskID, day string) (coreschedule.WindowRecord, error) {
	var w coreschedule.WindowRecord
	var start, end, dayAt time.Time
	err := r.db.q(ctx).QueryRow(ctx,
		`SELECT school_task_id, day, start_at, end_at, source, version
		 FROM window_cache WHERE school_task_id = $1::uuid AND day = $2`,
		schoolTaskID, day).Scan(&w.SchoolTaskID, &dayAt, &start, &end, &w.Source, &w.Version)
	if err != nil {
		return coreschedule.WindowRecord{}, mapError(err)
	}
	w.Day = dayAt.Format("2006-01-02")
	w.Start, w.End = start, end
	return w, nil
}

// InsertPlan 幂等落一批计划行。唯一约束挡重放：已存在的行不覆盖。
// 返回实际写入行数。计划批次小（单校账号量级），逐行 Exec 足够；
// 事务内调用复用 ctx 携带的连接。
func (r *ScheduleRepo) InsertPlan(ctx context.Context, rows []coreschedule.PlanRow) (int64, error) {
	q := r.db.q(ctx)
	var written int64
	for _, row := range rows {
		ct, err := q.Exec(ctx,
			`INSERT INTO sign_tasks (account_id, school_task_id, day, run_at)
			 VALUES ($1::uuid, $2::uuid, $3, $4)
			 ON CONFLICT (account_id, school_task_id, day) DO NOTHING`,
			row.AccountID, row.SchoolTaskID, row.Day, row.RunAt)
		if err != nil {
			return written, mapInsertForeignKey(err)
		}
		written += ct.RowsAffected()
	}
	return written, nil
}

// ClaimPending 是 CAS 认领：把一行从 pending 推到 running。
// 判据全部在 WHERE 里（单语句 CAS，无 SELECT-UPDATE 窗口）：
// state = 'pending' 是唯一放行条件；
// 账号必须活跃且未软删、学校任务仍启用——已停的面不领活（P4-B 复审 P2-2）。
// uq_sign_tasks_one_running 在库层保证同账号同时至多一条 running；
// 撞该索引（23505）与竞争失败同义：返回 ErrScheduleConflict。
// 返回 0 行即 ErrScheduleConflict。
// ClaimPending 把指定行推进到 running 并返回本次认领的 claim_seq。
// 当前只有 e2e 使用；结算必须带返回的序号。
func (r *ScheduleRepo) ClaimPending(ctx context.Context, accountID, schoolTaskID, day string, now time.Time) (int, error) {
	var seq int
	err := r.db.q(ctx).QueryRow(ctx,
		`UPDATE sign_tasks t SET state = 'running', claimed_at = $4,
		        lease_expires_at = $4::timestamptz + make_interval(secs => $5),
		        claim_seq = t.claim_seq + 1
		 WHERE t.account_id = $1::uuid AND t.school_task_id = $2::uuid AND t.day = $3 AND t.state = 'pending'
		   AND EXISTS (SELECT 1 FROM accounts a
		               WHERE a.id = t.account_id AND a.status = 'active' AND a.deleted_at IS NULL)
		   AND EXISTS (SELECT 1 FROM school_tasks st
		               WHERE st.id = t.school_task_id AND st.enabled)
		 RETURNING t.claim_seq`,
		accountID, schoolTaskID, day, now, r.lease.Seconds()).Scan(&seq)
	if err != nil {
		var pgErr *pgconn.PgError
		if errors.As(err, &pgErr) && pgErr.Code == "23505" {
			return 0, ErrScheduleConflict
		}
		if errors.Is(err, pgx.ErrNoRows) {
			return 0, ErrScheduleConflict
		}
		return 0, mapError(err)
	}
	return seq, nil
}

// Finish 把 running 行落终态并记结果摘要。
// 只接受终态（success/failed/skipped）：非终态是调用方编舞错误，
// 放过去会让行回到可认领态、破坏 CAS 语义（P4-B 复审 P2-1）。
// 摘要经掩码后才允许入库（调用方负责；本层不二次校验形状）。
// 词表形状由库层 ENUM 兜底；非法值报 validation。
func (r *ScheduleRepo) Finish(ctx context.Context, accountID, schoolTaskID, day string, state corestatus.TaskState, result string, claimSeq int, now time.Time) error {
	if !state.Terminal() {
		return validationError("finish requires a terminal state: " + string(state))
	}
	ct, err := r.db.q(ctx).Exec(ctx,
		`UPDATE sign_tasks SET state = $4, result = $5, attempts = attempts + 1, lease_expires_at = NULL
		 WHERE account_id = $1::uuid AND school_task_id = $2::uuid AND day = $3
		   AND state = 'running' AND claim_seq = $6`,
		accountID, schoolTaskID, day, string(state), result, claimSeq)
	if err != nil {
		return mapError(err)
	}
	if ct.RowsAffected() == 0 {
		return ErrScheduleConflict
	}
	return nil
}

// FinishCredentialInvalid 结算一次凭据错：任务落 failed(credential_invalid)，
// 账号从 active 改为 credential_error，并写引擎审计。三件事同一事务。
//
// 判据：
//   - 任务行必须是 running；零行报 ErrScheduleConflict（不猜、不补写）。
//   - 账号 CAS 只认 active；零行不算错误——账号已被暂停/删除/标记时保持原状。
//     状态没变就不写审计：detail 声明 from=active 时，那必须是事实。
//   - 审计必须同事务：审计失败则任务与账号一并回滚，结算重试时不漏状态。
//
// schoolID 取自任务行（worker 认领时带回），审计据此落院校面。
func (r *ScheduleRepo) FinishCredentialInvalid(ctx context.Context, schoolID, accountID, schoolTaskID, day string, claimSeq int, now time.Time) error {
	if r.audits == nil {
		return ErrScheduleAuditMissing
	}
	return r.db.InTx(ctx, func(ctx context.Context) error {
		q := r.db.q(ctx)
		ct, err := q.Exec(ctx,
			`UPDATE sign_tasks SET state = $4, result = $5, attempts = attempts + 1, lease_expires_at = NULL
			 WHERE account_id = $1::uuid AND school_task_id = $2::uuid AND day = $3
			   AND state = 'running' AND claim_seq = $6`,
			accountID, schoolTaskID, day, string(corestatus.TaskFailed), credentialInvalidResult, claimSeq)
		if err != nil {
			return mapError(err)
		}
		if ct.RowsAffected() == 0 {
			return ErrScheduleConflict
		}
		ct, err = q.Exec(ctx,
			`UPDATE accounts SET status = $2::account_status, updated_at = $3
			 WHERE id = $1::uuid AND status = 'active'::account_status
			   AND deleted_at IS NULL`,
			accountID, string(coreaccount.StatusCredentialError), now)
		if err != nil {
			return mapError(err)
		}
		if ct.RowsAffected() == 0 {
			return nil
		}
		_, err = r.audits.Append(ctx, coreaudit.Entry{
			TS:          now.UTC(),
			SchoolID:    schoolID,
			ActorUserID: "",
			ActorRole:   "",
			ActorLabel:  "engine",
			Action:      coreaudit.ActionAccountCredentialError,
			TargetType:  "account",
			TargetID:    accountID,
			Detail: map[string]any{
				"field": "status",
				"from":  string(coreaccount.StatusActive),
				"to":    string(coreaccount.StatusCredentialError),
			},
		})
		return err
	})
}

// CountDone 报告某账号某日的已了结行数（success/skipped 计入）。
// 「同账号同任务同日只签一次」的查询面判据。
func (r *ScheduleRepo) CountDone(ctx context.Context, accountID, day string) (int64, error) {
	var n int64
	err := r.db.q(ctx).QueryRow(ctx,
		`SELECT count(*) FROM sign_tasks
		 WHERE account_id = $1::uuid AND day = $2 AND state IN ('success', 'skipped')`,
		accountID, day).Scan(&n)
	if err != nil {
		return 0, mapError(err)
	}
	return n, nil
}

// RearrangePlan 重排未执行行：只更新 state = 'pending' 的 run_at。
// 运行中与终态行一律不动——当日结论比计划新（重放安全的同一条判据）。
// 返回更新行数。
func (r *ScheduleRepo) RearrangePlan(ctx context.Context, schoolTaskID, day string, rows []coreschedule.PlanRow) (int64, error) {
	q := r.db.q(ctx)
	var updated int64
	for _, row := range rows {
		ct, err := q.Exec(ctx,
			`UPDATE sign_tasks SET run_at = $4
			 WHERE account_id = $1::uuid AND school_task_id = $2::uuid AND day = $3 AND state = 'pending'`,
			row.AccountID, schoolTaskID, day, row.RunAt)
		if err != nil {
			return updated, mapError(err)
		}
		updated += ct.RowsAffected()
	}
	return updated, nil
}

// ActiveAccounts 返回某校的活跃账号及其所有者，按手机号升序
// （稳定顺序即铺点行序号）。只取 active 且未软删的行：
// 暂停/待审账号不占计划位。
func (r *ScheduleRepo) ActiveAccounts(ctx context.Context, schoolID string) ([]coreschedule.AccountRef, error) {
	rows, err := r.db.q(ctx).Query(ctx,
		`SELECT id::text, owner_user_id::text FROM accounts
		 WHERE school_id = $1::uuid AND status = 'active' AND deleted_at IS NULL
		 ORDER BY phone, id`,
		schoolID)
	if err != nil {
		return nil, mapError(err)
	}
	defer rows.Close()
	var out []coreschedule.AccountRef
	for rows.Next() {
		var acct coreschedule.AccountRef
		if err := rows.Scan(&acct.ID, &acct.OwnerUserID); err != nil {
			return nil, mapError(err)
		}
		out = append(out, acct)
	}
	return out, mapError(rows.Err())
}

// GetOverride 取某任务某日的人工覆盖；不存在返回 errs.ErrNotFound。
func (r *ScheduleRepo) GetOverride(ctx context.Context, schoolTaskID, day string) (coreschedule.WindowOverride, error) {
	var ov coreschedule.WindowOverride
	err := r.db.q(ctx).QueryRow(ctx,
		`SELECT school_task_id::text, day::text, start_at, end_at, reason, created_by::text
		 FROM window_overrides WHERE school_task_id = $1::uuid AND day = $2::date`,
		schoolTaskID, day).Scan(&ov.SchoolTaskID, &ov.Day, &ov.Start, &ov.End, &ov.Reason, &ov.CreatedBy)
	if err != nil {
		return coreschedule.WindowOverride{}, mapError(err)
	}
	return ov, nil
}

// PutOverride 覆盖写某任务某日的人工覆盖：同键只留最后一条。
func (r *ScheduleRepo) PutOverride(ctx context.Context, ov coreschedule.WindowOverride) error {
	_, err := r.db.q(ctx).Exec(ctx,
		`INSERT INTO window_overrides (school_task_id, day, start_at, end_at, reason, created_by)
		 VALUES ($1::uuid, $2::date, $3, $4, $5, $6::uuid)
		 ON CONFLICT (school_task_id, day) DO UPDATE
		 SET start_at = EXCLUDED.start_at, end_at = EXCLUDED.end_at,
		     reason = EXCLUDED.reason, created_by = EXCLUDED.created_by, created_at = now()`,
		ov.SchoolTaskID, ov.Day, ov.Start, ov.End, ov.Reason, ov.CreatedBy)
	if err != nil {
		return mapInsertForeignKey(err)
	}
	return nil
}

// ListWindows 返回作用域内任务与当日窗口缓存。
// LEFT JOIN 保留"尚未发现窗口"的任务：管理面要看到这一行。
func (r *ScheduleRepo) ListWindows(ctx context.Context, scope corescope.Scope, day string) ([]coreschedule.WindowView, error) {
	clause, args, err := schoolScopePredicate(scope, 2, "sc.")
	if err != nil {
		return nil, err
	}
	query := `SELECT sc.id::text, sc.name, st.id::text, st.name, st.enabled,
	                 wc.start_at, wc.end_at, wc.source, wc.version
	          FROM school_tasks st
	          JOIN schools sc ON sc.id = st.school_id
	          LEFT JOIN window_cache wc ON wc.school_task_id = st.id AND wc.day = $1::date
	          WHERE true` + clause + `
	          ORDER BY sc.name, st.name, st.id`
	rows, err := r.db.q(ctx).Query(ctx, query, append([]any{day}, args...)...)
	if err != nil {
		return nil, mapError(err)
	}
	defer rows.Close()
	var out []coreschedule.WindowView
	for rows.Next() {
		var v coreschedule.WindowView
		var start, end *time.Time
		var source *string
		var version *int
		if err := rows.Scan(&v.SchoolID, &v.SchoolName, &v.TaskID, &v.TaskName, &v.Enabled,
			&start, &end, &source, &version); err != nil {
			return nil, mapError(err)
		}
		v.Day = day
		if start != nil && end != nil {
			v.Start, v.End, v.Cached = *start, *end, true
		}
		if source != nil {
			v.Source = *source
		}
		if version != nil {
			v.Version = *version
		}
		out = append(out, v)
	}
	return out, mapError(rows.Err())
}

// Task 读任务元数据；不存在返回 errs.ErrNotFound。
func (r *ScheduleRepo) Task(ctx context.Context, taskID string) (coreschedule.TaskInfo, error) {
	var t coreschedule.TaskInfo
	err := r.db.q(ctx).QueryRow(ctx,
		`SELECT id::text, school_id::text, name, enabled FROM school_tasks WHERE id = $1::uuid`,
		taskID).Scan(&t.ID, &t.SchoolID, &t.Name, &t.Enabled)
	if err != nil {
		return coreschedule.TaskInfo{}, mapError(err)
	}
	return t, nil
}

// ClaimDue 认领到期任务：单条 UPDATE 批量领取。
// 判据全在 SQL：pending、run_at 已到、账号活跃未删、任务启用、
// 同账号当前无 running（I-7 的查询面，库层唯一索引仍是最终兜底）。
// FOR UPDATE SKIP LOCKED 让多 worker 不互等。
// 外层 state='pending' 是第二道闸：READ COMMITTED 下 CTE 快照与行锁之间
// 存在窗口，并发语句可能在快照后改状态；该谓词把这类行排除。
func (r *ScheduleRepo) ClaimDue(ctx context.Context, now time.Time, limit int) ([]coreschedule.TaskRow, error) {
	if limit < 1 {
		limit = 1
	}
	rows, err := r.db.q(ctx).Query(ctx,
		`WITH picked AS (
		   SELECT DISTINCT ON (s.account_id) s.id
		   FROM sign_tasks s
		   WHERE s.state = 'pending' AND s.run_at <= $1
		     AND NOT EXISTS (
		       SELECT 1 FROM sign_tasks r
		       WHERE r.account_id = s.account_id AND r.state = 'running')
		     AND EXISTS (
		       SELECT 1 FROM accounts a
		       WHERE a.id = s.account_id AND a.status = 'active' AND a.deleted_at IS NULL)
		     AND EXISTS (
		       SELECT 1 FROM school_tasks st
		       WHERE st.id = s.school_task_id AND st.enabled)
		   ORDER BY s.account_id, s.run_at, s.id
		   LIMIT $2
		 ),
		locked AS (
		   SELECT id FROM sign_tasks WHERE id IN (SELECT id FROM picked) FOR UPDATE SKIP LOCKED
		 )
		 UPDATE sign_tasks t SET state = 'running', claimed_at = $1,
		        lease_expires_at = $1::timestamptz + make_interval(secs => $3),
		        claim_seq = t.claim_seq + 1
		 WHERE t.id IN (SELECT id FROM locked) AND t.state = 'pending'
		 RETURNING t.id::text, t.account_id::text, t.school_task_id::text,
		           (SELECT st.school_id::text FROM school_tasks st WHERE st.id = t.school_task_id),
		           t.day::text, t.run_at, t.state::text, t.attempts, t.claim_seq,
		           (SELECT a.owner_user_id::text FROM accounts a WHERE a.id = t.account_id)`,
		now, limit, r.lease.Seconds())
	if err != nil {
		return nil, mapError(err)
	}
	defer rows.Close()
	var out []coreschedule.TaskRow
	for rows.Next() {
		var t coreschedule.TaskRow
		if err := rows.Scan(&t.ID, &t.AccountID, &t.SchoolTaskID, &t.SchoolID, &t.Day, &t.RunAt, &t.State, &t.Attempts, &t.ClaimSeq, &t.OwnerUserID); err != nil {
			return nil, mapError(err)
		}
		out = append(out, t)
	}
	return out, mapError(rows.Err())
}

// RequeueWait 把 running 退回 pending 并移到新落点，**不计尝试数**。
//
// 适用场景：窗口未开、出口限速、出口暂不可用、批处理中途中止。
// 这些等待不是"试过了没成"，若计进业务预算，一个账号可能一整天
// 只真正打过一次平台就被判 failed（P4-E 审查 P2-1）。
//
// result 写的是内部等待原因（wait_*），不是 signing 结论词。
func (r *ScheduleRepo) RequeueWait(ctx context.Context, taskID string, runAt time.Time, result string, claimSeq int) error {
	ct, err := r.db.q(ctx).Exec(ctx,
		`UPDATE sign_tasks SET state = 'pending', run_at = $2, result = $3,
		        claimed_at = NULL, lease_expires_at = NULL
		 WHERE id = $1::uuid AND state = 'running' AND claim_seq = $4`,
		taskID, runAt, result, claimSeq)
	if err != nil {
		return mapError(err)
	}
	if ct.RowsAffected() == 0 {
		return ErrScheduleConflict
	}
	return nil
}

// RequeueRetry 是业务失败后的退避：退回 pending、移到新落点、**计一次尝试**。
// 预算比较在 app 层（core/retry），这里只负责如实记账。
func (r *ScheduleRepo) RequeueRetry(ctx context.Context, taskID string, runAt time.Time, result string, claimSeq int) error {
	ct, err := r.db.q(ctx).Exec(ctx,
		`UPDATE sign_tasks SET state = 'pending', run_at = $2, result = $3,
		        attempts = attempts + 1, claimed_at = NULL, lease_expires_at = NULL
		 WHERE id = $1::uuid AND state = 'running' AND claim_seq = $4`,
		taskID, runAt, result, claimSeq)
	if err != nil {
		return mapError(err)
	}
	if ct.RowsAffected() == 0 {
		return ErrScheduleConflict
	}
	return nil
}

// Rearm 把 failed 行重新放回队列（手动签到的重试入口）。
//
// 为什么单独一条而不是复用 Requeue*：Requeue* 只接受 running 行，
// 而手动签到遇到的是当日已落 failed 的终态行。重排不覆盖 result
// 之外的历史，attempts 保留（预算仍是当日的真实消耗）。
func (r *ScheduleRepo) Rearm(ctx context.Context, taskID string, runAt time.Time) error {
	ct, err := r.db.q(ctx).Exec(ctx,
		`UPDATE sign_tasks SET state = 'pending', run_at = $2, claimed_at = NULL,
		        lease_expires_at = NULL, result = ''
		 WHERE id = $1::uuid AND state = 'failed'`,
		taskID, runAt)
	if err != nil {
		return mapError(err)
	}
	if ct.RowsAffected() == 0 {
		return ErrScheduleConflict
	}
	return nil
}

// CountRunning 数当前 running 行。优雅停机与 doctor 复用；
// 词表经参数传入，不在 SQL 里写状态字面量。
func (r *ScheduleRepo) CountRunning(ctx context.Context) (int64, error) {
	return r.db.QueryInt64(ctx,
		"SELECT count(*) FROM sign_tasks WHERE state = $1", string(corestatus.TaskRunning))
}

// ReclaimExpired 把租约过期的 running 行放回 pending，并写 task.reclaim 审计。
// 每行一条审计；UPDATE 与审计同事务。上限 limit 防止一次回收过多行占住锁。
// 没有审计仓储时有行可回收即回滚；无行可回收时不需要审计。
func (r *ScheduleRepo) ReclaimExpired(ctx context.Context, now time.Time, limit int) (int64, error) {
	if limit < 1 {
		limit = 1
	}
	var n int64
	err := r.db.InTx(ctx, func(ctx context.Context) error {
		rows, err := r.db.q(ctx).Query(ctx, `
			WITH expired AS (
			  SELECT id FROM sign_tasks
			  WHERE state = 'running'
			    AND lease_expires_at IS NOT NULL
			    AND lease_expires_at <= $1
			  ORDER BY lease_expires_at
			  LIMIT $2
			  FOR UPDATE SKIP LOCKED
			)
			UPDATE sign_tasks t
			   SET state = 'pending', claimed_at = NULL,
			       lease_expires_at = NULL, run_at = $1
			 WHERE t.id IN (SELECT id FROM expired)
			RETURNING t.id::text,
			          (SELECT st.school_id::text FROM school_tasks st
			            WHERE st.id = t.school_task_id)`, now, limit)
		if err != nil {
			return mapError(err)
		}
		type expiredRow struct{ id, schoolID string }
		var out []expiredRow
		for rows.Next() {
			var it expiredRow
			if err := rows.Scan(&it.id, &it.schoolID); err != nil {
				rows.Close()
				return mapError(err)
			}
			out = append(out, it)
		}
		if err := rows.Err(); err != nil {
			rows.Close()
			return mapError(err)
		}
		rows.Close()
		if len(out) > 0 && r.audits == nil {
			// 没有审计仓储就不能回收：回滚，保持行原状。
			return ErrScheduleAuditMissing
		}
		for _, it := range out {
			if _, err := r.audits.Append(ctx, coreaudit.Entry{
				TS:         now.UTC(),
				SchoolID:   it.schoolID,
				ActorLabel: "engine",
				Action:     coreaudit.ActionTaskReclaim,
				TargetType: "task",
				TargetID:   it.id,
				Detail:     map[string]any{"reason": "lease_expired"},
			}); err != nil {
				return err
			}
		}
		n = int64(len(out))
		return nil
	})
	return n, err
}

// EnabledTasks 列出全部启用任务，供日切构建。
func (r *ScheduleRepo) EnabledTasks(ctx context.Context) ([]coreschedule.TaskInfo, error) {
	rows, err := r.db.q(ctx).Query(ctx,
		`SELECT id::text, school_id::text, name, enabled
		   FROM school_tasks WHERE enabled ORDER BY school_id, id`)
	if err != nil {
		return nil, mapError(err)
	}
	defer rows.Close()
	var out []coreschedule.TaskInfo
	for rows.Next() {
		var t coreschedule.TaskInfo
		if err := rows.Scan(&t.ID, &t.SchoolID, &t.Name, &t.Enabled); err != nil {
			return nil, mapError(err)
		}
		out = append(out, t)
	}
	return out, mapError(rows.Err())
}

// ResetRunning 回收无租约或租约已过期的 running 行（启动时调用）：
// 租约为空的行来自上次崩溃；已过期的行不会再被原持有者结算。落点改为 now，立即可执行。
// 回收前取 pg_try_advisory_xact_lock：多实例同时启动时只有一个执行。
func (r *ScheduleRepo) ResetRunning(ctx context.Context) (int64, error) {
	var n int64
	err := r.db.InTx(ctx, func(ctx context.Context) error {
		var ok bool
		if err := r.db.q(ctx).QueryRow(ctx,
			"SELECT pg_try_advisory_xact_lock($1)", ResetRunningLockKey).Scan(&ok); err != nil {
			return mapError(err)
		}
		if !ok {
			// 另一实例正在回收；本次视为已完成，不重复改行。
			return nil
		}
		ct, err := r.db.q(ctx).Exec(ctx,
			`UPDATE sign_tasks SET state = 'pending', claimed_at = NULL,
			        lease_expires_at = NULL, run_at = now()
			 WHERE state = 'running'
			   AND (lease_expires_at IS NULL OR lease_expires_at <= now())`)
		if err != nil {
			return mapError(err)
		}
		n = ct.RowsAffected()
		return nil
	})
	return n, err
}

// ResetRunningLockKey 是启动回收的 advisory lock 键（ASCII "yiba"）。
// 它只防多实例同时回收；单进程是当前部署不变量。
// 导出供 e2e 验证锁确实生效；生产代码不应另用该键。
const ResetRunningLockKey int64 = 0x79696261

// EnsureTask 为手动签到建或取当日任务行。
// created=true 表示本次插入；(xmax = 0) 是 PostgreSQL 区分插入与冲突更新的惯用判据。
func (r *ScheduleRepo) EnsureTask(ctx context.Context, accountID, schoolTaskID, day string, runAt time.Time) (coreschedule.TaskRow, bool, error) {
	var t coreschedule.TaskRow
	var created bool
	err := r.db.q(ctx).QueryRow(ctx,
		`INSERT INTO sign_tasks (account_id, school_task_id, day, run_at)
		 VALUES ($1::uuid, $2::uuid, $3, $4)
		 ON CONFLICT (account_id, school_task_id, day) DO UPDATE SET run_at = sign_tasks.run_at
		 RETURNING id::text, account_id::text, school_task_id::text,
		           (SELECT st.school_id::text FROM school_tasks st WHERE st.id = sign_tasks.school_task_id),
		           day::text, run_at, state::text, attempts,
		           (SELECT a.owner_user_id::text FROM accounts a WHERE a.id = sign_tasks.account_id),
		           (xmax = 0) AS created`,
		accountID, schoolTaskID, day, runAt).
		Scan(&t.ID, &t.AccountID, &t.SchoolTaskID, &t.SchoolID, &t.Day, &t.RunAt, &t.State, &t.Attempts, &t.OwnerUserID, &created)
	if err != nil {
		return coreschedule.TaskRow{}, false, mapInsertForeignKey(err)
	}
	return t, created, nil
}

// TodayTask 返回账号所属学校今天启用的第一个任务（按名称序）。
// 账号必须活跃且未软删——暂停账号入队后 ClaimDue 永不认领，
// 只会留下一条永远 pending 的行（P4-E 审查 P3-5）。
// 无启用任务或账号不可用返回 errs.ErrNotFound。
func (r *ScheduleRepo) TodayTask(ctx context.Context, accountID string) (string, error) {
	var id string
	err := r.db.q(ctx).QueryRow(ctx,
		`SELECT st.id::text FROM accounts a
		 JOIN school_tasks st ON st.school_id = a.school_id AND st.enabled
		 WHERE a.id = $1::uuid AND a.status = 'active' AND a.deleted_at IS NULL
		 ORDER BY st.name, st.id
		 LIMIT 1`,
		accountID).Scan(&id)
	if err != nil {
		return "", mapError(err)
	}
	return id, nil
}
