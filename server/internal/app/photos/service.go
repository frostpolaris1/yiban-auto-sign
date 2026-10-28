// Package photos 实现用户图片库用例（P5-A）。
//
// # 功能
// 上传、列表、读取、软删与保留期设置；上传前完成预处理与去重。
// 上传即随机安全裁切（每边 0–10%，固定不可调），并写 photo.upload
// 审计（四边比例与输出摘要）。到期行清理与无引用 blob 回收由
// RetentionLoop 每日执行。
// 处理顺序固定：预处理 → 写 blob → 事务内写记录与审计。
// 本包**不**发 HTTP 请求、**不**写 SQL、**不**生成公开 URL。
//
// # 归属
// app 层用例。端口见 repo.go：Store 接 adapters/pg，Blobs 接 adapters/storage。
//
// # 复用
// Service、Deps、Page；端口 Store、Blobs；Options。
//
// # 口径
// 上传只允许 user 角色，所有者取 scope.UserID()。
// admin 读取照片要求照片属主在绑定院校有未删除账号；越权与不存在统一 404。
// 去重键是 (owner_user_id, sha256)：同一用户重复上传返回同一行。
// 学校默认图只登记照片引用；本包不提交图片字节给平台（P5-D）。
package photos

import (
	"context"
	"encoding/hex"
	"errors"
	"io"
	"strconv"
	"strings"
	"sync"
	"time"

	appcore "github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/image"
	corephoto "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/photo"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/google/uuid"
)

// Options 是图片预处理参数。零值表示不限尺寸、保持输入格式。
// 裁切是固定随机口径（每边 0–10%），不是配置项：无配置键、
// 无环境变量、无管理员面。Rand 只服务测试注入。
type Options struct {
	MaxPixels  int
	MaxSidePx  int
	TargetMime string
	Rand       image.Rand
}

// Page 是一页图片记录。
type Page struct {
	Items      []corephoto.Record
	NextCursor string
}

// SchoolDefaultPhoto 是学校默认图引用。
// PhotoID 为空表示未设置；ETag 是所引照片的 sha256 hex，无引用时为空。
type SchoolDefaultPhoto struct {
	PhotoID string
	ETag    string
}

// Deps 是用例依赖。
type Deps struct {
	Store   Store
	Blobs   Blobs
	Audit   appaudit.Repo
	Tx      appcore.TxManager
	Clock   coreclock.Clock
	Options Options
	// Accounts 提供账号可见面判据（按账号列照片用）。
	Accounts Accounts
	// SchoolPhotos 提供学校默认图的存储面。
	SchoolPhotos SchoolPhotos
}

// Service 是图片库用例服务。
//
// mu 把「上传落盘」与「回收扫描」互斥。单进程是设计不变量（AGENTS §26），
// 因此一把进程内读写锁就能结构性排除这个交错：
// 回收按 List 快照的 mtime 判安全期，而上传的去重 Touch 可能发生在快照之后；
// 没有互斥时，回收会用过期快照删掉「刚刷新、行刚提交」的文件。
type Service struct {
	deps Deps
	mu   sync.RWMutex
}

// NewService 构造服务。缺依赖即失败。
func NewService(deps Deps) (*Service, error) {
	if deps.Store == nil {
		return nil, errors.New("photos: nil store")
	}
	if deps.Blobs == nil {
		return nil, errors.New("photos: nil blob store")
	}
	if deps.Audit == nil {
		return nil, errors.New("photos: nil audit repo")
	}
	if deps.Tx == nil {
		return nil, errors.New("photos: nil tx manager")
	}
	if deps.Clock == nil {
		return nil, errors.New("photos: nil clock")
	}
	if deps.Accounts == nil {
		return nil, errors.New("photos: nil account reader")
	}
	if deps.SchoolPhotos == nil {
		return nil, errors.New("photos: nil school photo store")
	}
	return &Service{deps: deps}, nil
}

// Upload 预处理一张图片并入库。同一用户重复上传同一字节返回同一行。
// 每次上传写一行 photo.upload 审计：TargetID 是落库行 id，
// detail 只含四边裁切比例与输出摘要（无 PII，无自由文本）。
// 追加与落行同事务；审计失败则上传失败，不静默放行。
func (s *Service) Upload(ctx context.Context, scope corescope.Scope, actor appaudit.Actor, raw []byte) (corephoto.Record, error) {
	if err := requireUser(scope); err != nil {
		return corephoto.Record{}, err
	}
	if len(raw) == 0 {
		return corephoto.Record{}, errs.New(errs.CodeValidation, "image is empty")
	}
	if len(raw) > corephoto.MaxBytes {
		return corephoto.Record{}, errs.New(errs.CodeValidation, "image is too large")
	}
	out, err := image.Prepare(raw, image.Options{
		MaxPixels:  s.deps.Options.MaxPixels,
		MaxSidePx:  s.deps.Options.MaxSidePx,
		TargetMime: s.deps.Options.TargetMime,
		Rand:       s.deps.Options.Rand,
	})
	if err != nil {
		return corephoto.Record{}, err
	}
	// 先写 blob 再写行：行写失败只会留下无引用的内容寻址 blob，
	// 由后续 GC 回收；反过来会留下指向空 blob 的行。
	// 读锁挡住回收：Put→Touch→Create 全程不得与回收扫描交错。
	s.mu.RLock()
	defer s.mu.RUnlock()
	if err := s.deps.Blobs.Put(ctx, out.SHA256, out.Data); err != nil {
		return corephoto.Record{}, err
	}
	// 去重命中时 Put 不重写文件，修改时间仍是首次落盘的旧值。
	// GC 按修改时间判安全期；不刷新就可能删掉「刚重传、行还未提交」的文件。
	if err := s.deps.Blobs.Touch(ctx, out.SHA256, s.deps.Clock.Now()); err != nil {
		return corephoto.Record{}, err
	}
	var created corephoto.Record
	err = s.deps.Tx.InTx(ctx, func(ctx context.Context) error {
		rec, err := s.deps.Store.Create(ctx, corephoto.Record{
			OwnerUserID: scope.UserID(),
			SHA256:      out.SHA256,
			Mime:        out.Mime,
			Width:       out.Width,
			Height:      out.Height,
			Bytes:       int64(len(out.Data)),
			CreatedAt:   s.deps.Clock.Now(),
		})
		if err != nil {
			return err
		}
		// detail 只承载比例与摘要：EncodeDetail 的键白名单与形状
		// 规则兜底；本调用点不得加自由文本键。
		if _, err := s.deps.Audit.Append(ctx, coreaudit.Entry{
			TS:          s.deps.Clock.Now().UTC(),
			ActorUserID: actor.UserID,
			ActorRole:   actor.Role,
			ActorLabel:  actor.Label,
			Action:      coreaudit.ActionPhotoUpload,
			TargetType:  "photo",
			TargetID:    rec.ID,
			Detail: map[string]any{
				"crop_l": out.Crop.Left,
				"crop_r": out.Crop.Right,
				"crop_t": out.Crop.Top,
				"crop_b": out.Crop.Bottom,
				"sha256": hex.EncodeToString(out.SHA256[:]),
			},
		}); err != nil {
			return err
		}
		created = rec
		return nil
	})
	if err != nil {
		return corephoto.Record{}, err
	}
	return created, nil
}

// List 按作用域列一页图片。limit 为 0 时取默认页大小。
// 契约把 /api/me/photos 定为 user 端点，admin 在这里就被拒绝。
func (s *Service) List(ctx context.Context, scope corescope.Scope, cursor string, limit int) (Page, error) {
	if err := requireUser(scope); err != nil {
		return Page{}, err
	}
	limit, err := appcore.NormalizeLimit(limit)
	if err != nil {
		return Page{}, err
	}
	items, next, err := s.deps.Store.List(ctx, scope, cursor, limit)
	if err != nil {
		return Page{}, err
	}
	return Page{Items: items, NextCursor: next}, nil
}

// Get 在作用域内读一张图片。跨用户、跨校与已软删返回 404。
// admin 面由属主的绑定院校账号派生；master 不在契约内，仍 404。
func (s *Service) Get(ctx context.Context, scope corescope.Scope, id string) (corephoto.Record, error) {
	if err := appcore.RequireScope(scope); err != nil {
		return corephoto.Record{}, err
	}
	return s.deps.Store.Get(ctx, scope, id)
}

// ListAccount 按账号列一页图片。
// 可见面判据与 /api/accounts/{id}/location 同源：复用 app/account 的 Get，
// 即 user 限自己账号、admin 限绑定学校。跨用户、跨校与已软删账号一律 404。
// 账号可见面判定后，图片面就是该账号属主的图片库；复用同一列表查询。
// 契约 x-roles 是 [user, admin]：master 回 403。
func (s *Service) ListAccount(ctx context.Context, scope corescope.Scope, accountID, cursor string, limit int) (Page, error) {
	if err := requireAccountReader(scope); err != nil {
		return Page{}, err
	}
	limit, err := appcore.NormalizeLimit(limit)
	if err != nil {
		return Page{}, err
	}
	record, err := s.deps.Accounts.Get(ctx, scope, accountID)
	if err != nil {
		return Page{}, err
	}
	items, next, err := s.deps.Store.List(ctx, corescope.User(record.OwnerUserID), cursor, limit)
	if err != nil {
		return Page{}, err
	}
	return Page{Items: items, NextCursor: next}, nil
}

// GetSchoolDefault 读学校默认图。
// 可见面由存储侧按作用域判定：master 不限、admin 限绑定校、
// user 限自己账号所在校。越权与不存在都 404。
func (s *Service) GetSchoolDefault(ctx context.Context, scope corescope.Scope, schoolID string) (SchoolDefaultPhoto, error) {
	if err := appcore.RequireScope(scope); err != nil {
		return SchoolDefaultPhoto{}, err
	}
	id, etag, err := s.deps.SchoolPhotos.SchoolDefault(ctx, scope, schoolID)
	if err != nil {
		return SchoolDefaultPhoto{}, err
	}
	return SchoolDefaultPhoto{PhotoID: id, ETag: etag}, nil
}

// SetSchoolDefault 覆盖写学校默认图并记 school.default_photo_set 审计。
// photo_id 必须引用调用者可见的未软删照片；不可见与不存在回 400
// （契约把照片引用列为请求体校验）。null 清除；重复 PUT 同值幂等。
// 写与审计同事务；detail 只含 photo_id 与 previous_photo_id，无 PII。
func (s *Service) SetSchoolDefault(ctx context.Context, scope corescope.Scope, schoolID string, photoID *string, actor appaudit.Actor) (SchoolDefaultPhoto, error) {
	if err := requireSchoolWriter(scope); err != nil {
		return SchoolDefaultPhoto{}, err
	}
	// 审计 TargetID 与库内形状统一小写：路径里的 UUID 大小写不敏感。
	if u, err := uuid.Parse(schoolID); err == nil {
		schoolID = u.String()
	}
	// 院校面先判：跨校 admin 与不存在的学校一律 404，
	// 不借照片校验的 400 泄漏照片可见性。
	if err := s.deps.SchoolPhotos.SchoolWritable(ctx, scope, schoolID); err != nil {
		return SchoolDefaultPhoto{}, err
	}
	var nextID *string
	out := SchoolDefaultPhoto{}
	if photoID != nil {
		rec, err := s.deps.SchoolPhotos.PhotoForDefault(ctx, scope, *photoID)
		if err != nil {
			if errs.From(err).Code == errs.CodeNotFound {
				return SchoolDefaultPhoto{}, errs.New(errs.CodeValidation, "photo must reference an existing visible photo")
			}
			return SchoolDefaultPhoto{}, err
		}
		// 存库与审计都用库里读回的形状（小写 UUID）。
		nextID = &rec.ID
		out = SchoolDefaultPhoto{PhotoID: rec.ID, ETag: hex.EncodeToString(rec.SHA256[:])}
	}
	err := s.deps.Tx.InTx(ctx, func(ctx context.Context) error {
		previous, err := s.deps.SchoolPhotos.SetSchoolDefault(ctx, scope, schoolID, nextID)
		if err != nil {
			return err
		}
		var prevValue, nextValue any
		if previous != nil {
			prevValue = *previous
		}
		if nextID != nil {
			nextValue = *nextID
		}
		// detail 只承载前后照片 id；键白名单与 UUID 形状由 core/audit 兜底。
		_, err = s.deps.Audit.Append(ctx, coreaudit.Entry{
			TS:          s.deps.Clock.Now().UTC(),
			SchoolID:    schoolID,
			ActorUserID: actor.UserID,
			ActorRole:   actor.Role,
			ActorLabel:  actor.Label,
			Action:      coreaudit.ActionSchoolDefaultPhotoSet,
			TargetType:  "school",
			TargetID:    schoolID,
			Detail: map[string]any{
				"photo_id":          nextValue,
				"previous_photo_id": prevValue,
			},
		})
		return err
	})
	if err != nil {
		return SchoolDefaultPhoto{}, err
	}
	return out, nil
}

// Open 打开一张图片的字节流。作用域判据与 Get 相同。
// 关闭动作交给调用方：HTTP 层用 defer body.Close()。
func (s *Service) Open(ctx context.Context, scope corescope.Scope, id string) (corephoto.Record, io.ReadCloser, error) {
	rec, err := s.Get(ctx, scope, id)
	if err != nil {
		return corephoto.Record{}, nil, err
	}
	body, err := s.deps.Blobs.Open(ctx, rec.SHA256)
	if err != nil {
		return corephoto.Record{}, nil, err
	}
	return rec, body, nil
}

// Delete 软删一张图片。重复删除返回同一行，不报错。
// 契约把 /api/me/photos/{id} 的删除定为 user 端点。
func (s *Service) Delete(ctx context.Context, scope corescope.Scope, id string) (corephoto.Record, error) {
	if err := requireUser(scope); err != nil {
		return corephoto.Record{}, err
	}
	return s.deps.Store.SoftDelete(ctx, scope, id, s.deps.Clock.Now())
}

// Retention 返回用户的保留期天数；未设置时返回缺省值。
func (s *Service) Retention(ctx context.Context, scope corescope.Scope) (int, error) {
	if err := requireUser(scope); err != nil {
		return 0, err
	}
	days, err := s.deps.Store.Retention(ctx, scope.UserID())
	if err != nil {
		return 0, err
	}
	if days <= 0 {
		return corephoto.DefaultRetentionDays, nil
	}
	return days, nil
}

// SetRetention 设置用户的保留期。只接受闭集值。
func (s *Service) SetRetention(ctx context.Context, scope corescope.Scope, days int) (int, error) {
	if err := requireUser(scope); err != nil {
		return 0, err
	}
	if !corephoto.ValidRetention(days) {
		return 0, errs.New(errs.CodeValidation, "retention must be one of "+retentionChoices())
	}
	if err := s.deps.Store.PutRetention(ctx, scope.UserID(), days, s.deps.Clock.Now()); err != nil {
		return 0, err
	}
	return days, nil
}

// retentionChoices 从闭集生成错误文案。
// 文案里的天数表只许由 core/photo.RetentionDays 派生：复写一份就是第二个定义点。
func retentionChoices() string {
	days := corephoto.RetentionDays()
	parts := make([]string, len(days))
	for i, d := range days {
		parts[i] = strconv.Itoa(d)
	}
	return strings.Join(parts, ", ")
}

// PurgeResult 是一轮保留期清理的结果。
type PurgeResult struct {
	RowsDeleted  int64
	BlobsDeleted int
}

// blobSafetyWindow 是 blob 回收的安全期。
// 上传先写 blob 再写行；行事务未提交的窗口内不得删文件。
// 一小时远大于任何提交延迟，也远小于一天的清理周期。
const blobSafetyWindow = time.Hour

// Purge 循环删除到期行，直到一次调用返回 0 行。
// limit 是单次删除上限：单条大事务会长时间持锁。
func (s *Service) Purge(ctx context.Context, limit int) (int64, error) {
	limit, err := appcore.NormalizeLimit(limit)
	if err != nil {
		return 0, err
	}
	var total int64
	for {
		n, err := s.deps.Store.PurgeExpired(ctx, s.deps.Clock.Now(), limit)
		if err != nil {
			return total, err
		}
		total += n
		if n == 0 {
			return total, nil
		}
		if err := ctx.Err(); err != nil {
			return total, err
		}
	}
}

// CollectBlobs 回收无行引用的 blob，返回真实删除数。
//
// 只删修改时间早于 now-blobSafetyWindow 的文件：
// 刚落盘或刚去重上传（Touch 刷新）的文件必须保留。
// 引用集合含软删行，故可恢复的行不会指向空文件。
// 全程持写锁：与 Upload 的 Put→Touch→Create 互斥。
// 没有这把锁，List 快照之后发生的去重 Touch 不会被本轮看到，
// 回收会按过期 mtime 删掉「刚提交行」的文件（复审 P1）。
func (s *Service) CollectBlobs(ctx context.Context) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	files, err := s.deps.Blobs.List(ctx)
	if err != nil {
		return 0, err
	}
	referenced, err := s.deps.Store.ReferencedDigests(ctx)
	if err != nil {
		return 0, err
	}
	cutoff := s.deps.Clock.Now().Add(-blobSafetyWindow)
	removed := 0
	for _, file := range files {
		if err := ctx.Err(); err != nil {
			return removed, err
		}
		if !file.ModTime.Before(cutoff) {
			continue
		}
		if _, ok := referenced[file.Digest]; ok {
			continue
		}
		reallyRemoved, err := s.deps.Blobs.Remove(ctx, file.Digest)
		if err != nil {
			return removed, err
		}
		if reallyRemoved {
			removed++
		}
	}
	return removed, nil
}

// Sweep 执行一轮保留期清理：先删到期行，再回收无引用 blob。
// 返回部分结果与首个错误，调用方可记录已完成的数目。
func (s *Service) Sweep(ctx context.Context, batch int) (PurgeResult, error) {
	rows, err := s.Purge(ctx, batch)
	result := PurgeResult{RowsDeleted: rows}
	if err != nil {
		return result, err
	}
	blobs, err := s.CollectBlobs(ctx)
	result.BlobsDeleted = blobs
	return result, err
}

// requireUser 限定图片库端点只给 user 角色。
func requireUser(scope corescope.Scope) error {
	if err := appcore.RequireScope(scope); err != nil {
		return err
	}
	if scope.Kind() != corescope.KindUser {
		return errs.New(errs.CodeForbidden, "photo library is for user accounts")
	}
	return nil
}

// requireAccountReader 限定按账号列照片只给 user 与 admin。
// 契约 x-roles 是 [user, admin]：master 不得经本端点列照片。
func requireAccountReader(scope corescope.Scope) error {
	if err := appcore.RequireScope(scope); err != nil {
		return err
	}
	switch scope.Kind() {
	case corescope.KindUser, corescope.KindAdmin:
		return nil
	}
	return errs.New(errs.CodeForbidden, "role cannot list account photos")
}

// requireSchoolWriter 限定默认图写入只给 master 与 admin。
// 契约 x-roles 是 [master, admin]：user 不得写学校默认图。
func requireSchoolWriter(scope corescope.Scope) error {
	if err := appcore.RequireScope(scope); err != nil {
		return err
	}
	switch scope.Kind() {
	case corescope.KindMaster, corescope.KindAdmin:
		return nil
	}
	return errs.New(errs.CodeForbidden, "role cannot set the school default photo")
}
