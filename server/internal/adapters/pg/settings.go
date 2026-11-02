package pg

// 全局运行时设置的 PostgreSQL 适配器。
//
// # 功能
// 读写公开注册开关。本文件**不**做权限判断，**不**写审计。
//
// # 归属
// adapters 层。app/auth 只依赖 RegistrationStore 端口，不见本类型。
//
// # 口径
// 键缺失或值不是小写 true 都按关闭处理：关闭是安全默认。
// 开关写入单语句 upsert；审计由用例层在同一事务内追加。

import (
	"context"
	"errors"
	"strconv"
	"strings"

	"github.com/jackc/pgx/v5"
)

// settingsRegistrationKey 是公开注册开关的键。
const settingsRegistrationKey = "registration.enabled"

// SettingsRepo 读写 settings 表。
type SettingsRepo struct {
	db *DB
}

// NewSettingsRepo 构造设置仓储。
func NewSettingsRepo(db *DB) *SettingsRepo {
	return &SettingsRepo{db: db}
}

// RegistrationEnabled 报告公开注册是否开放。
// 行缺失、空值与非 true 值一律为 false，不返回 not_found。
func (r *SettingsRepo) RegistrationEnabled(ctx context.Context) (bool, error) {
	var value string
	err := r.db.q(ctx).QueryRow(ctx,
		"SELECT value FROM settings WHERE key = $1", settingsRegistrationKey).Scan(&value)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, mapError(err)
	}
	return strings.TrimSpace(value) == "true", nil
}

// SetRegistrationEnabled 写入开关并记录操作者。
// 调用方必须在事务内调用，使本次写入与审计同存亡。
func (r *SettingsRepo) SetRegistrationEnabled(ctx context.Context, enabled bool, by string) error {
	if strings.TrimSpace(by) == "" {
		return validationError("actor user id is required")
	}
	_, err := r.db.q(ctx).Exec(ctx, `
		INSERT INTO settings (key, value, updated_by)
		VALUES ($1, $2, $3::uuid)
		ON CONFLICT (key) DO UPDATE
		   SET value = EXCLUDED.value,
		       updated_by = EXCLUDED.updated_by`,
		settingsRegistrationKey, strconv.FormatBool(enabled), by)
	return mapError(err)
}
