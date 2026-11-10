package pg

// 全局运行时设置的 PostgreSQL 适配器。
//
// # 功能
// 读写公开注册开关、自动审批开关与可写回的调度设置行。
// 本文件**不**做权限判断，**不**做域校验，**不**写审计。
//
// # 归属
// adapters 层。app/auth 只依赖 RegistrationStore 端口，不见本类型。
//
// # 口径
// 存在性感知读取（getSetting）是唯一的取行底座：
// 行缺失返回 found=false，调用方决定缺省语义。
// 布尔开关（registration.enabled、auto_approve）在 found 之上保留
// 「缺行/非 true 一律 false」的既有对外行为：关闭是安全默认。
// 调度三键（sett2 写回）走存在性感知的 IntOverride/BoolOverride；
// 批 3c 的字符串枚举键走 StringOverride/SetString（词表校验在用例层）：
// 行缺失由用例层回退 env，值不符合类型口径报校验错（不是缺行）。
// 写入单语句 upsert（ON CONFLICT、updated_by、触发器维护 updated_at）；
// 删行走 ClearSetting；审计由用例层在同一事务内追加。

import (
	"context"
	"errors"
	"strconv"
	"strings"

	"github.com/jackc/pgx/v5"
)

// settingsRegistrationKey 是公开注册开关的键。
const settingsRegistrationKey = "registration.enabled"

// settingsAutoApproveKey 是全局自动审批开关的键。
const settingsAutoApproveKey = "auto_approve"

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
	return r.booleanSetting(ctx, settingsRegistrationKey)
}

// SetRegistrationEnabled 写入开关并记录操作者。
// 调用方必须在事务内调用，使本次写入与审计同存亡。
func (r *SettingsRepo) SetRegistrationEnabled(ctx context.Context, enabled bool, by string) error {
	return r.setBooleanSetting(ctx, settingsRegistrationKey, enabled, by)
}

// AutoApproveEnabled 报告自动审批是否开放。
// 行缺失、空值与非 true 值一律为 false，不返回 not_found。
func (r *SettingsRepo) AutoApproveEnabled(ctx context.Context) (bool, error) {
	return r.booleanSetting(ctx, settingsAutoApproveKey)
}

// SetAutoApproveEnabled 写入自动审批开关并记录操作者。
func (r *SettingsRepo) SetAutoApproveEnabled(ctx context.Context, enabled bool, by string) error {
	return r.setBooleanSetting(ctx, settingsAutoApproveKey, enabled, by)
}

// IntOverride 读整型覆盖行。found=false 表示行缺失。
// 行存在但值不是十进制整数时报校验错：坏行不是「缺行」，
// 用例层据此回退 env 并出声（fail-open 但留痕）。
func (r *SettingsRepo) IntOverride(ctx context.Context, key string) (int, bool, error) {
	raw, found, err := r.getSetting(ctx, key)
	if err != nil || !found {
		return 0, found, err
	}
	value, parseErr := strconv.Atoi(strings.TrimSpace(raw))
	if parseErr != nil {
		return 0, true, validationError("settings value is not an integer")
	}
	return value, true, nil
}

// SetInt 写整型覆盖行并记录操作者。
// 调用方必须在事务内调用，使本次写入与审计同存亡。
func (r *SettingsRepo) SetInt(ctx context.Context, key string, value int, by string) error {
	return r.setSetting(ctx, key, strconv.Itoa(value), by)
}

// BoolOverride 读布尔覆盖行。found=false 表示行缺失。
// 行存在但值不是小写 true/false 字面时报校验错：
// 「只认真小写字面」是覆盖行的读口径（00021 的 value 列无 CHECK）。
func (r *SettingsRepo) BoolOverride(ctx context.Context, key string) (bool, bool, error) {
	raw, found, err := r.getSetting(ctx, key)
	if err != nil || !found {
		return false, found, err
	}
	switch strings.TrimSpace(raw) {
	case "true":
		return true, true, nil
	case "false":
		return false, true, nil
	default:
		return false, true, validationError("settings value is not a boolean literal")
	}
}

// SetBool 写布尔覆盖行并记录操作者。
// 调用方必须在事务内调用，使本次写入与审计同存亡。
func (r *SettingsRepo) SetBool(ctx context.Context, key string, value bool, by string) error {
	return r.setSetting(ctx, key, strconv.FormatBool(value), by)
}

// StringOverride 读字符串覆盖行（批 3c 分布档）。found=false 表示行缺失。
// 返回裁剪两侧空白后的原始字符串（与 Int/Bool 的读口径一致）；
// 词表校验在用例层：adapter 不知道 front/uniform 的语义。
func (r *SettingsRepo) StringOverride(ctx context.Context, key string) (string, bool, error) {
	raw, found, err := r.getSetting(ctx, key)
	if err != nil || !found {
		return "", found, err
	}
	return strings.TrimSpace(raw), true, nil
}

// SetString 写字符串覆盖行并记录操作者。
// 值必须已过用例层词表校验；调用方必须在事务内调用。
func (r *SettingsRepo) SetString(ctx context.Context, key, value, by string) error {
	return r.setSetting(ctx, key, value, by)
}

// ClearSetting 删除覆盖行（PUT 显式 null 的语义：回退 env）。
// 行不存在是幂等成功。调用方必须在事务内调用。
func (r *SettingsRepo) ClearSetting(ctx context.Context, key string) error {
	_, err := r.db.q(ctx).Exec(ctx, "DELETE FROM settings WHERE key = $1", key)
	return mapError(err)
}

// getSetting 是存在性感知读取的唯一底座：
// 行缺失返回 ("", false, nil)；其他失败原样映射上抛。
func (r *SettingsRepo) getSetting(ctx context.Context, key string) (string, bool, error) {
	var value string
	err := r.db.q(ctx).QueryRow(ctx,
		"SELECT value FROM settings WHERE key = $1", key).Scan(&value)
	if errors.Is(err, pgx.ErrNoRows) {
		return "", false, nil
	}
	if err != nil {
		return "", false, mapError(err)
	}
	return value, true, nil
}

// booleanSetting 是布尔开关的唯一读取口径：
// 行缺失、空值与非 true 值一律为 false。它是 getSetting 之上的薄语义。
func (r *SettingsRepo) booleanSetting(ctx context.Context, key string) (bool, error) {
	raw, found, err := r.getSetting(ctx, key)
	if err != nil || !found {
		return false, err
	}
	return strings.TrimSpace(raw) == "true", nil
}

// setBooleanSetting 是布尔开关的唯一写入口径。
// 调用方必须在事务内调用，使本次写入与审计同存亡。
func (r *SettingsRepo) setBooleanSetting(ctx context.Context, key string, enabled bool, by string) error {
	return r.setSetting(ctx, key, strconv.FormatBool(enabled), by)
}

// setSetting 是覆盖行的唯一写入口径：单语句 upsert，
// ON CONFLICT 更新 value 与 updated_by；updated_at 由触发器维护。
// 调用方必须在事务内调用，使本次写入与审计同存亡。
func (r *SettingsRepo) setSetting(ctx context.Context, key, value, by string) error {
	if strings.TrimSpace(by) == "" {
		return validationError("actor user id is required")
	}
	_, err := r.db.q(ctx).Exec(ctx, `
		INSERT INTO settings (key, value, updated_by)
		VALUES ($1, $2, $3::uuid)
		ON CONFLICT (key) DO UPDATE
		   SET value = EXCLUDED.value,
		       updated_by = EXCLUDED.updated_by`,
		key, value, by)
	return mapError(err)
}
