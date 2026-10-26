package pg

import (
	"context"
	"errors"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"

	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
)

// ErrScheduleConflict 是 CAS 认领竞争失败的错误：
// 另一个 worker 已把该行推进到非 pending 态。
var ErrScheduleConflict = errors.New("pg: sign task already claimed")

// ErrWindowStale 是窗口版本竞争失败的错误：库里已有更新的版本。
var ErrWindowStale = errors.New("pg: window version conflict")

// ScheduleRepo 是排期四表的仓储。
// 唯一约束 (account_id, school_task_id, day) 是 CAS 判据的库级兜底：
// 应用层条件 UPDATE 之外，重复插行必然失败。
type ScheduleRepo struct {
	db *DB
}

// NewScheduleRepo 构造排期仓储。
func NewScheduleRepo(db *DB) *ScheduleRepo { return &ScheduleRepo{db: db} }

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
func (r *ScheduleRepo) ClaimPending(ctx context.Context, accountID, schoolTaskID, day string, now time.Time) error {
	ct, err := r.db.q(ctx).Exec(ctx,
		`UPDATE sign_tasks t SET state = 'running', claimed_at = $4
		 WHERE t.account_id = $1::uuid AND t.school_task_id = $2::uuid AND t.day = $3 AND t.state = 'pending'
		   AND EXISTS (SELECT 1 FROM accounts a
		               WHERE a.id = t.account_id AND a.status = 'active' AND a.deleted_at IS NULL)
		   AND EXISTS (SELECT 1 FROM school_tasks st
		               WHERE st.id = t.school_task_id AND st.enabled)`,
		accountID, schoolTaskID, day, now)
	if err != nil {
		var pgErr *pgconn.PgError
		if errors.As(err, &pgErr) && pgErr.Code == "23505" {
			return ErrScheduleConflict
		}
		return mapError(err)
	}
	if ct.RowsAffected() == 0 {
		return ErrScheduleConflict
	}
	return nil
}

// Finish 把 running 行落终态并记结果摘要。
// 只接受终态（success/failed/skipped）：非终态是调用方编舞错误，
// 放过去会让行回到可认领态、破坏 CAS 语义（P4-B 复审 P2-1）。
// 摘要经掩码后才允许入库（调用方负责；本层不二次校验形状）。
// 词表形状由库层 ENUM 兜底；非法值报 validation。
func (r *ScheduleRepo) Finish(ctx context.Context, accountID, schoolTaskID, day string, state corestatus.TaskState, result string, now time.Time) error {
	if !state.Terminal() {
		return validationError("finish requires a terminal state: " + string(state))
	}
	ct, err := r.db.q(ctx).Exec(ctx,
		`UPDATE sign_tasks SET state = $4, result = $5, attempts = attempts + 1
		 WHERE account_id = $1::uuid AND school_task_id = $2::uuid AND day = $3 AND state = 'running'`,
		accountID, schoolTaskID, day, string(state), result)
	if err != nil {
		return mapError(err)
	}
	if ct.RowsAffected() == 0 {
		return ErrScheduleConflict
	}
	return nil
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
