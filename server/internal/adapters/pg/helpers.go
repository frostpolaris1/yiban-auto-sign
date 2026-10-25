package pg

import (
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgtype"

	coreauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
)

// newID 返回 UUIDv7。
// id 由应用显式给出，插入前即可知；数据库默认值只服务手工操作。
func newID() (string, error) {
	id, err := uuid.NewV7()
	if err != nil {
		return "", fmt.Errorf("pg: generate uuid: %w", err)
	}
	return id.String(), nil
}

// tokenHashFromBytes 把扫出的 bytea 转成 TokenHash。
// 列 CHECK 保证 32 字节；长度不符说明 schema 与代码不一致，
// 故报错，**不静默返回零值**。
func tokenHashFromBytes(raw []byte) (coreauth.TokenHash, error) {
	if len(raw) != 32 {
		return coreauth.TokenHash{}, fmt.Errorf("pg: token hash is %d bytes, want 32", len(raw))
	}
	var hash coreauth.TokenHash
	copy(hash[:], raw)
	return hash, nil
}

// timePtr 把可空时间戳列转成可选值。
func timePtr(value pgtype.Timestamptz) *time.Time {
	if !value.Valid {
		return nil
	}
	out := value.Time
	return &out
}
