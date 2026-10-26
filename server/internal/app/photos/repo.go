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
	// PurgeExpired 删除按各用户保留期已到期的行，返回删除行数。
	PurgeExpired(ctx context.Context, now time.Time, limit int) (int64, error)
}

// Blobs 是内容寻址的字节存储。
//
// Put 必须幂等：同一 sha 重复写不报错。
// Open 对缺失键返回错误；错误不得吞成空流。
type Blobs interface {
	Put(ctx context.Context, sha [32]byte, data []byte) error
	Open(ctx context.Context, sha [32]byte) (io.ReadCloser, error)
}
