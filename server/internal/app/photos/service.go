// Package photos 实现用户图片库用例（P5-A）。
//
// # 功能
// 上传、列表、读取、软删与保留期设置；上传前完成预处理与去重。
// 到期行清理与无引用 blob 回收由 RetentionLoop 每日执行。
// 处理顺序固定：预处理 → 写 blob → 写记录。
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
// admin 读取图片需要在 P5-B 建立「账号—图片」关系，本切片不开放。
// 去重键是 (owner_user_id, sha256)：同一用户重复上传返回同一行。
package photos

import (
	"context"
	"errors"
	"io"
	"time"

	appcore "github.com/frostpolaris1/yiban-auto-sign/server/internal/app"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/image"
	corephoto "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/photo"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// Options 是图片预处理参数。零值表示不限尺寸、不裁切、保持输入格式。
type Options struct {
	MaxPixels   int
	MaxSidePx   int
	CropPercent int
	TargetMime  string
}

// Page 是一页图片记录。
type Page struct {
	Items      []corephoto.Record
	NextCursor string
}

// Deps 是用例依赖。
type Deps struct {
	Store   Store
	Blobs   Blobs
	Clock   coreclock.Clock
	Options Options
}

// Service 是图片库用例服务。
type Service struct {
	deps Deps
}

// NewService 构造服务。缺依赖即失败。
func NewService(deps Deps) (*Service, error) {
	if deps.Store == nil {
		return nil, errors.New("photos: nil store")
	}
	if deps.Blobs == nil {
		return nil, errors.New("photos: nil blob store")
	}
	if deps.Clock == nil {
		return nil, errors.New("photos: nil clock")
	}
	return &Service{deps: deps}, nil
}

// Upload 预处理一张图片并入库。同一用户重复上传同一字节返回同一行。
func (s *Service) Upload(ctx context.Context, scope corescope.Scope, raw []byte) (corephoto.Record, error) {
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
		MaxPixels:   s.deps.Options.MaxPixels,
		MaxSidePx:   s.deps.Options.MaxSidePx,
		CropPercent: s.deps.Options.CropPercent,
		TargetMime:  s.deps.Options.TargetMime,
	})
	if err != nil {
		return corephoto.Record{}, err
	}
	// 先写 blob 再写行：行写失败只会留下无引用的内容寻址 blob，
	// 由后续 GC 回收；反过来会留下指向空 blob 的行。
	if err := s.deps.Blobs.Put(ctx, out.SHA256, out.Data); err != nil {
		return corephoto.Record{}, err
	}
	// 去重命中时 Put 不重写文件，修改时间仍是首次落盘的旧值。
	// GC 按修改时间判安全期；不刷新就可能删掉「刚重传、行还未提交」的文件。
	if err := s.deps.Blobs.Touch(ctx, out.SHA256, s.deps.Clock.Now()); err != nil {
		return corephoto.Record{}, err
	}
	return s.deps.Store.Create(ctx, corephoto.Record{
		OwnerUserID: scope.UserID(),
		SHA256:      out.SHA256,
		Mime:        out.Mime,
		Width:       out.Width,
		Height:      out.Height,
		Bytes:       int64(len(out.Data)),
		CreatedAt:   s.deps.Clock.Now(),
	})
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

// Get 在作用域内读一张图片。跨用户与已软删返回 404。
func (s *Service) Get(ctx context.Context, scope corescope.Scope, id string) (corephoto.Record, error) {
	if err := appcore.RequireScope(scope); err != nil {
		return corephoto.Record{}, err
	}
	return s.deps.Store.Get(ctx, scope, id)
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
		return 0, errs.New(errs.CodeValidation, "retention must be one of 30, 90, 180, 365")
	}
	if err := s.deps.Store.PutRetention(ctx, scope.UserID(), days, s.deps.Clock.Now()); err != nil {
		return 0, err
	}
	return days, nil
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
// 先取文件清单、再取引用集合：清单之后才落盘的上传不在清单里，
// 不会被本轮删除；清单之前落盘的上传已刷新修改时间，受安全期保护。
func (s *Service) CollectBlobs(ctx context.Context) (int, error) {
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
