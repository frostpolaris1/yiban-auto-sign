// Package photos 实现用户图片库用例（P5-A）。
//
// # 功能
// 上传、列表、读取、软删与保留期设置；上传前完成预处理与去重。
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

// Purge 删除到期的图片行。返回删除行数。blob 回收留后续批次。
func (s *Service) Purge(ctx context.Context, limit int) (int64, error) {
	limit, err := appcore.NormalizeLimit(limit)
	if err != nil {
		return 0, err
	}
	return s.deps.Store.PurgeExpired(ctx, s.deps.Clock.Now(), limit)
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
