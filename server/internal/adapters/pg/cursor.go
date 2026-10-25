package pg

// 本文件实现游标分页的编解码。
//
// 口径：游标是不透明串，内容为 RFC3339Nano 时刻与行 id。
// 客户端只允许原样回传；解析失败即 validation_failed。
// 排序键固定为 (created_at DESC, id DESC)，与游标内容一一对应。

import (
	"encoding/base64"
	"fmt"
	"strings"
	"time"

	"github.com/google/uuid"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
)

// cursorSep 分隔时刻与 id。时刻本身不含该字符。
const cursorSep = "|"

// encodeCursor 把一行的排序键编成游标。
func encodeCursor(at time.Time, id string) string {
	raw := at.UTC().Format(time.RFC3339Nano) + cursorSep + id
	return base64.RawURLEncoding.EncodeToString([]byte(raw))
}

// decodeCursor 解析游标。空串表示首页。
func decodeCursor(cursor string) (time.Time, string, error) {
	if cursor == "" {
		return time.Time{}, "", nil
	}
	raw, err := base64.RawURLEncoding.DecodeString(cursor)
	if err != nil {
		return time.Time{}, "", errs.New(errs.CodeValidation, "invalid cursor")
	}
	parts := strings.SplitN(string(raw), cursorSep, 2)
	if len(parts) != 2 {
		return time.Time{}, "", errs.New(errs.CodeValidation, "invalid cursor")
	}
	at, err := time.Parse(time.RFC3339Nano, parts[0])
	if err != nil {
		return time.Time{}, "", errs.New(errs.CodeValidation, "invalid cursor")
	}
	if _, err := uuid.Parse(parts[1]); err != nil {
		return time.Time{}, "", errs.New(errs.CodeValidation, "invalid cursor")
	}
	return at, parts[1], nil
}

// nextCursor 返回本页末行的游标；末页返回空串。
// rows 必须已经截到 limit 条，more 报告仓储是否还有下一行。
func nextCursor[T any](rows []T, more bool, key func(T) (time.Time, string)) string {
	if !more || len(rows) == 0 {
		return ""
	}
	at, id := key(rows[len(rows)-1])
	return encodeCursor(at, id)
}

// appendKeyset 追加 (时间列, id 列) 的键集条件。
// 列名由调用方给出且**只能是本包常量**，不接受外部输入。
func appendKeyset(where string, args []any, cursor, timeColumn, idColumn string) (string, []any, error) {
	if cursor == "" {
		return where, args, nil
	}
	at, id, err := decodeCursor(cursor)
	if err != nil {
		return "", nil, err
	}
	args = append(args, at, id)
	where += fmt.Sprintf(" AND (%s, %s) < ($%d, $%d::uuid)",
		timeColumn, idColumn, len(args)-1, len(args))
	return where, args, nil
}

// appendLimit 追加 LIMIT。多取一行用于判断是否末页。
func appendLimit(query string, args []any, limit int) (string, []any) {
	args = append(args, limit+1)
	return fmt.Sprintf("%s LIMIT $%d", query, len(args)), args
}
