package pg

import (
	"context"
	"encoding/hex"
	"errors"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corephoto "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/photo"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// PhotoRepo 是图片记录与保留期的仓储。
//
// 作用域口径：owner 可见自己的照片；admin 可见绑定院校里账号属主的
// 照片（照片没有 school_id，院校面经 accounts 派生）；master 没有
// 照片作用域，入口层把它收敛为 owner 范围，只读自己的行。
// 学校默认图是本表的第二个读者：引用登记在 schools.default_photo_id。
type PhotoRepo struct {
	db *DB
}

// NewPhotoRepo 构造图片仓储。
func NewPhotoRepo(db *DB) *PhotoRepo { return &PhotoRepo{db: db} }

const photoColumns = `id::text, owner_user_id::text, sha256, mime, width, height, bytes, created_at, deleted_at`

// Create 幂等写入一张图片。同 (owner, sha256) 再次写入时恢复软删行，
// 并把 created_at 重置为本次上传时间（保留期从本次重算）。
func (r *PhotoRepo) Create(ctx context.Context, rec corephoto.Record) (corephoto.Record, error) {
	row := r.db.q(ctx).QueryRow(ctx,
		`INSERT INTO photos (owner_user_id, sha256, mime, width, height, bytes, created_at)
		 VALUES ($1::uuid, $2, $3, $4, $5, $6, $7)
		 ON CONFLICT (owner_user_id, sha256) DO UPDATE
		 SET deleted_at = NULL, created_at = EXCLUDED.created_at
		 RETURNING `+photoColumns,
		rec.OwnerUserID, rec.SHA256[:], rec.Mime, rec.Width, rec.Height, rec.Bytes, rec.CreatedAt)
	return scanPhoto(row)
}

// Get 按作用域读一张图片；跨用户、跨校、已软删与不存在都返回 not_found。
// master 没有照片作用域：入口层用 owner 范围读它自己的照片。
func (r *PhotoRepo) Get(ctx context.Context, scope corescope.Scope, id string) (corephoto.Record, error) {
	if scope.Kind() == corescope.KindMaster {
		return corephoto.Record{}, errs.ErrNotFound
	}
	// 前缀必须是 photos.：EXISTS 子查询里的裸列名会先解析到 a，
	// 不加前缀等于拿自身比自身（恒真）。
	clause, scopeArgs, err := photoScopePredicate(scope, 2, "photos.")
	if err != nil {
		return corephoto.Record{}, err
	}
	row := r.db.q(ctx).QueryRow(ctx,
		`SELECT `+photoColumns+` FROM photos
		 WHERE id = $1::uuid AND deleted_at IS NULL`+clause,
		append([]any{id}, scopeArgs...)...)
	return scanPhoto(row)
}

// PhotoForDefault 读一张未软删照片，供学校默认图写入校验。
// 只接受 admin 作用域：契约把 PUT default-photo 收窄为 [admin]。
// 院校范围由 SetSchoolDefault 判定。
func (r *PhotoRepo) PhotoForDefault(ctx context.Context, scope corescope.Scope, id string) (corephoto.Record, error) {
	clause, scopeArgs, err := photoScopePredicate(scope, 2, "photos.")
	if err != nil {
		return corephoto.Record{}, err
	}
	row := r.db.q(ctx).QueryRow(ctx,
		`SELECT `+photoColumns+` FROM photos
		 WHERE id = $1::uuid AND deleted_at IS NULL`+clause,
		append([]any{id}, scopeArgs...)...)
	return scanPhoto(row)
}

// SchoolDefault 读学校默认图引用。
// 学校不在作用域内与不存在都返回 not_found。
// 引用照片已软删时按未设置返回：字节已不可读，引用不再可用。
// etag 是引用照片的 sha256 hex；无引用时为空串。
func (r *PhotoRepo) SchoolDefault(ctx context.Context, scope corescope.Scope, schoolID string) (photoID, etag string, err error) {
	clause, scopeArgs, err := schoolDefaultPhotoPredicate(scope, 2, "s.")
	if err != nil {
		return "", "", err
	}
	row := r.db.q(ctx).QueryRow(ctx,
		`SELECT s.default_photo_id::text, p.sha256
		   FROM schools s
		   LEFT JOIN photos p ON p.id = s.default_photo_id AND p.deleted_at IS NULL
		  WHERE s.id = $1::uuid`+clause,
		append([]any{schoolID}, scopeArgs...)...)
	var id *string
	var sha []byte
	if err := row.Scan(&id, &sha); err != nil {
		return "", "", mapError(err)
	}
	if id == nil || sha == nil {
		// 无引用，或引用照片已软删（LEFT JOIN 无行）。
		return "", "", nil
	}
	if len(sha) != 32 {
		return "", "", errs.New(errs.CodeInternal, "photo digest has the wrong length")
	}
	return *id, hex.EncodeToString(sha), nil
}

// SchoolWritable 判定院校可写：admin 限绑定校。
// 写入入口在 app 层已收窄为 admin；越权与不存在都返回 not_found。
// 写入路径在照片校验之前调用它，使「院校不可见」先于「照片不可见」
// 判定，不借 400 泄漏照片存在性。
func (r *PhotoRepo) SchoolWritable(ctx context.Context, scope corescope.Scope, schoolID string) error {
	clause, scopeArgs, err := schoolScopePredicate(scope, 2, "")
	if err != nil {
		return err
	}
	var id string
	err = r.db.q(ctx).QueryRow(ctx,
		`SELECT id::text FROM schools WHERE id = $1::uuid`+clause,
		append([]any{schoolID}, scopeArgs...)...).Scan(&id)
	return mapError(err)
}

// SetSchoolDefault 覆盖写学校默认图并返回旧引用（nil 表示原先未设置）。
// **必须在事务内运行**：读旧值并持行锁，写与审计同事务。
// 院校范围与 schoolScopePredicate 同源；契约只允许 admin 写入。
func (r *PhotoRepo) SetSchoolDefault(ctx context.Context, scope corescope.Scope, schoolID string, photoID *string) (*string, error) {
	if !r.db.hasTx(ctx) {
		return nil, ErrNoTransaction
	}
	clause, scopeArgs, err := schoolScopePredicate(scope, 2, "")
	if err != nil {
		return nil, err
	}
	var previous *string
	err = r.db.q(ctx).QueryRow(ctx,
		`SELECT default_photo_id::text FROM schools
		  WHERE id = $1::uuid`+clause+` FOR UPDATE`,
		append([]any{schoolID}, scopeArgs...)...).Scan(&previous)
	if err != nil {
		return nil, mapError(err)
	}
	if _, err := r.db.q(ctx).Exec(ctx,
		`UPDATE schools SET default_photo_id = $2::uuid WHERE id = $1::uuid`,
		schoolID, photoID); err != nil {
		return nil, mapError(err)
	}
	return previous, nil
}

// List 按作用域列一页图片，游标语义与账号列表一致。
func (r *PhotoRepo) List(ctx context.Context, scope corescope.Scope, cursor string, limit int) ([]corephoto.Record, string, error) {
	if limit < 1 || limit > 200 {
		return nil, "", validationError("limit must be between 1 and 200")
	}
	if scope.Kind() != corescope.KindUser {
		return nil, "", errs.ErrNotFound
	}
	args := []any{scope.UserID()}
	query, args, err := appendKeyset(
		`SELECT `+photoColumns+` FROM photos WHERE deleted_at IS NULL AND owner_user_id = $1::uuid`,
		args, cursor, "created_at", "id")
	if err != nil {
		return nil, "", err
	}
	query += ` ORDER BY created_at DESC, id DESC`
	query, args = appendLimit(query, args, limit)
	rows, err := r.db.q(ctx).Query(ctx, query, args...)
	if err != nil {
		return nil, "", mapError(err)
	}
	defer rows.Close()
	items := make([]corephoto.Record, 0, limit)
	for rows.Next() {
		rec, err := scanPhoto(rows)
		if err != nil {
			return nil, "", err
		}
		items = append(items, rec)
	}
	if err := rows.Err(); err != nil {
		return nil, "", mapError(err)
	}
	more := len(items) > limit
	if more {
		items = items[:limit]
	}
	next := nextCursor(items, more, func(rec corephoto.Record) (time.Time, string) {
		return rec.CreatedAt, rec.ID
	})
	return items, next, nil
}

// SoftDelete 软删一张图片。已软删时返回原行，不报错。
func (r *PhotoRepo) SoftDelete(ctx context.Context, scope corescope.Scope, id string, now time.Time) (corephoto.Record, error) {
	if scope.Kind() != corescope.KindUser {
		return corephoto.Record{}, errs.ErrNotFound
	}
	row := r.db.q(ctx).QueryRow(ctx,
		`UPDATE photos SET deleted_at = COALESCE(deleted_at, $3)
		 WHERE id = $1::uuid AND owner_user_id = $2::uuid
		 RETURNING `+photoColumns,
		id, scope.UserID(), now)
	return scanPhoto(row)
}

// Retention 返回用户保留期；没有设置时返回 0，由用例补缺省。
func (r *PhotoRepo) Retention(ctx context.Context, userID string) (int, error) {
	var days int
	err := r.db.q(ctx).QueryRow(ctx,
		`SELECT days FROM photo_retention WHERE user_id = $1::uuid`, userID).Scan(&days)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return 0, nil
		}
		return 0, mapError(err)
	}
	return days, nil
}

// PutRetention 覆盖写用户保留期。
func (r *PhotoRepo) PutRetention(ctx context.Context, userID string, days int, now time.Time) error {
	_, err := r.db.q(ctx).Exec(ctx,
		`INSERT INTO photo_retention (user_id, days, updated_at) VALUES ($1::uuid, $2, $3)
		 ON CONFLICT (user_id) DO UPDATE SET days = EXCLUDED.days, updated_at = EXCLUDED.updated_at`,
		userID, days, now)
	return mapInsertForeignKey(err)
}

// PurgeExpired 物理删除按各用户保留期已到期的行，返回删除行数。
// 判据是 COALESCE(deleted_at, created_at)：软删行从删除时刻起算。
// 缺省保留期 90 天与 core/photo 一致。一次最多删 limit 行。
func (r *PhotoRepo) PurgeExpired(ctx context.Context, now time.Time, limit int) (int64, error) {
	if limit < 1 {
		limit = 1
	}
	ct, err := r.db.q(ctx).Exec(ctx,
		`DELETE FROM photos p
		 WHERE p.id IN (
		   SELECT p2.id FROM photos p2
		   LEFT JOIN photo_retention r ON r.user_id = p2.owner_user_id
		   WHERE COALESCE(p2.deleted_at, p2.created_at)
		         <= $1::timestamptz - make_interval(days => COALESCE(r.days, 90))
		   ORDER BY COALESCE(p2.deleted_at, p2.created_at), p2.id
		   LIMIT $2
		 )`,
		now, limit)
	if err != nil {
		return 0, mapError(err)
	}
	return ct.RowsAffected(), nil
}

// ReferencedDigests 返回 photos 表仍被任意行引用的摘要集合。
// 含软删行：未到期的软删行可恢复，其 blob 不得回收。
func (r *PhotoRepo) ReferencedDigests(ctx context.Context) (map[[32]byte]struct{}, error) {
	rows, err := r.db.q(ctx).Query(ctx, `SELECT DISTINCT sha256 FROM photos`)
	if err != nil {
		return nil, mapError(err)
	}
	defer rows.Close()
	out := make(map[[32]byte]struct{})
	for rows.Next() {
		var sha []byte
		if err := rows.Scan(&sha); err != nil {
			return nil, mapError(err)
		}
		if len(sha) != 32 {
			return nil, errs.New(errs.CodeInternal, "photo digest has the wrong length")
		}
		var key [32]byte
		copy(key[:], sha)
		out[key] = struct{}{}
	}
	if err := rows.Err(); err != nil {
		return nil, mapError(err)
	}
	return out, nil
}

// CountLibrary 报告属主未删图片数（P5-C 选图定界用）。
func (r *PhotoRepo) CountLibrary(ctx context.Context, ownerUserID string) (int64, error) {
	var n int64
	err := r.db.q(ctx).QueryRow(ctx,
		`SELECT count(*) FROM photos WHERE owner_user_id = $1::uuid AND deleted_at IS NULL`,
		ownerUserID).Scan(&n)
	if err != nil {
		return 0, mapError(err)
	}
	return n, nil
}

// LibraryAt 按 (created_at, id) 升序取第 offset 张（0 基）。
// 升序口径是选图确定性的存储侧判据：调用方已先 CountLibrary 定界，
// offset 越界与库空同形，返回 errs.ErrNotFound。
func (r *PhotoRepo) LibraryAt(ctx context.Context, ownerUserID string, offset int) (corephoto.Record, error) {
	if offset < 0 {
		return corephoto.Record{}, validationError("offset must not be negative")
	}
	row := r.db.q(ctx).QueryRow(ctx,
		`SELECT `+photoColumns+` FROM photos
		 WHERE owner_user_id = $1::uuid AND deleted_at IS NULL
		 ORDER BY created_at, id
		 OFFSET $2 LIMIT 1`,
		ownerUserID, offset)
	return scanPhoto(row)
}

// scanPhoto 把一行扫成记录。sha256 以 bytea 存取，读出后拷进定长数组。
func scanPhoto(row interface{ Scan(...any) error }) (corephoto.Record, error) {
	var rec corephoto.Record
	var sha []byte
	var deleted *time.Time
	if err := row.Scan(&rec.ID, &rec.OwnerUserID, &sha, &rec.Mime,
		&rec.Width, &rec.Height, &rec.Bytes, &rec.CreatedAt, &deleted); err != nil {
		return corephoto.Record{}, mapError(err)
	}
	if len(sha) != len(rec.SHA256) {
		return corephoto.Record{}, errs.New(errs.CodeInternal, "photo digest has the wrong length")
	}
	copy(rec.SHA256[:], sha)
	rec.DeletedAt = deleted
	return rec, nil
}
