package photos

import (
	"context"
	"io"
	"time"

	corephoto "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/photo"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// Store 读写图片记录。
//
// admin 作用域的读取需要「账号—图片」关系，本切片尚未建立；
// 在这之前 admin 范围的实现一律返回 errs.ErrNotFound，不放行全量。
//
// 去重键是 (owner_user_id, sha256)：重复上传返回既有行。
// 已软删的同键行在再次上传时恢复（DeletedAt 清空），避免用户
// 删掉后重新上传得到一行不可见记录。
type Store interface {
	Create(ctx context.Context, rec corephoto.Record) (corephoto.Record, error)
	// Get 按作用域读一张；跨用户、已软删与不存在都返回 errs.ErrNotFound。
	Get(ctx context.Context, scope corescope.Scope, id string) (corephoto.Record, error)
	// List 按作用域列一页，游标语义与账号列表一致。
	List(ctx context.Context, scope corescope.Scope, cursor string, limit int) ([]corephoto.Record, string, error)
	// SoftDelete 软删一张；已软删返回同一行，不报错。
	SoftDelete(ctx context.Context, scope corescope.Scope, id string, now time.Time) (corephoto.Record, error)
	// Retention 返回用户保留期；没有设置时返回 0，由用例补缺省。
	Retention(ctx context.Context, userID string) (int, error)
	// PutRetention 覆盖写用户保留期。
	PutRetention(ctx context.Context, userID string, days int, now time.Time) error
	// PurgeExpired 物理删除按各用户保留期已到期的行，返回删除行数。
	// 到期判据从 COALESCE(deleted_at, created_at) 起算。
	// 一次最多删除 limit 行；调用方循环到返回 0。
	PurgeExpired(ctx context.Context, now time.Time, limit int) (int64, error)
	// ReferencedDigests 返回 photos 表仍被任意行引用的摘要集合。
	// 含软删行：未到期的软删行可恢复，其 blob 不得回收。
	ReferencedDigests(ctx context.Context) (map[[32]byte]struct{}, error)
	// CountLibrary 报告属主未删图片数（P5-C 选图用）。
	CountLibrary(ctx context.Context, ownerUserID string) (int64, error)
	// LibraryAt 按 (created_at, id) 升序取第 offset 张（0 基）。
	// 调用方先 CountLibrary 定界；offset 越界或库空返回 errs.ErrNotFound。
	LibraryAt(ctx context.Context, ownerUserID string, offset int) (corephoto.Record, error)
}

// Blobs 是内容寻址的字节存储。
//
// Put 必须幂等：同一 sha 重复写不报错。
// Open 对缺失键返回错误；错误不得吞成空流。
// List 列出全部已落盘 blob 的摘要与修改时间；回收判据需要时间戳。
// Touch 把一份已有 blob 的修改时间刷到 at；去重命中后重新起算回收安全期。
// Remove 删除一份 blob，返回是否真的删掉了文件；缺失返回 false、不报错。
type Blobs interface {
	Put(ctx context.Context, sha [32]byte, data []byte) error
	Open(ctx context.Context, sha [32]byte) (io.ReadCloser, error)
	List(ctx context.Context) ([]corephoto.Blob, error)
	Touch(ctx context.Context, sha [32]byte, at time.Time) error
	Remove(ctx context.Context, sha [32]byte) (bool, error)
}
