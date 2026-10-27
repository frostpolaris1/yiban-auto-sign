package legacy

// 对账哈希与 Python 兼容序列化。
//
// 口径必须与 scripts/db_export.py 的 _recon_hash / _dump_line 逐字节一致：
//   - 掩码：core/mask.MaskPhone（与 yiban.core.masking.mask_phone 同规则）。
//   - 规范串：table + 0x1f + key + 0x1f + 0x1e 连接的 "k=v"（键按字典序，
//     phone 字段先掩码）；字符串值原样，非字符串值按 sort_keys 紧凑 JSON。
//   - 行序列化：Python json.dumps(sort_keys=True) 的默认分隔符——冒号与
//     逗号后各一个空格；字符串转义按 ensure_ascii=False 的规则
//     （"、\、\b、\f、\n、\r、\t 带名，其余 <0x20 用 \u00xx 小写十六进制）。
//
// 黄金值钉在 canonical_test.go（Python 侧现算），漂移即红。

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"sort"
	"strconv"
	"strings"
	"unicode/utf16"

	coremask "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mask"
)

// JSONNumber 是 json.Decoder.UseNumber 产出的数字类型别名。
// 对账哈希要求数字按源文件字面回放，禁止 float64 往返。
type JSONNumber = json.Number

const (
	unitSep = "\x1f"
	itemSep = "\x1e"
)

// MaskPhone 是掩码规则的转发点（canonical.tsv mask.phone 门）。
func MaskPhone(phone string) string { return coremask.MaskPhone(phone) }

// CanonicalHash 按导出器同一口径计算一行的对账哈希。
func CanonicalHash(table, key string, row map[string]any) string {
	keys := sortedKeys(row)
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		v := row[k]
		if k == "phone" {
			v = MaskPhone(valueString(v))
		}
		parts = append(parts, k+"="+canonicalValue(v))
	}
	sum := sha256.Sum256([]byte(table + unitSep + key + unitSep + strings.Join(parts, itemSep)))
	return hex.EncodeToString(sum[:])
}

// CanonicalLine 按 Python json.dumps(sort_keys=True) 的字节口径序列化一行。
func CanonicalLine(row map[string]any) string {
	keys := sortedKeys(row)
	var sb strings.Builder
	sb.WriteByte('{')
	for i, k := range keys {
		if i > 0 {
			sb.WriteString(", ")
		}
		sb.WriteString(quotePy(k))
		sb.WriteString(": ")
		sb.WriteString(pyValue(row[k]))
	}
	sb.WriteByte('}')
	return sb.String()
}

func sortedKeys(row map[string]any) []string {
	keys := make([]string, 0, len(row))
	for k := range row {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

// canonicalValue 渲染哈希规范串里的一个值：字符串原样，其余按行序列化口径。
func canonicalValue(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	return pyValue(v)
}

// pyValue 渲染一个值（递归；数字按字面回放，字符串按 Python 转义加引号）。
func pyValue(v any) string {
	switch t := v.(type) {
	case nil:
		return "null"
	case string:
		return quotePy(t)
	case bool:
		return strconv.FormatBool(t)
	case JSONNumber:
		return t.String()
	case map[string]any:
		keys := sortedKeys(t)
		var sb strings.Builder
		sb.WriteByte('{')
		for i, k := range keys {
			if i > 0 {
				sb.WriteString(", ")
			}
			sb.WriteString(quotePy(k))
			sb.WriteString(": ")
			sb.WriteString(pyValue(t[k]))
		}
		sb.WriteByte('}')
		return sb.String()
	case []any:
		var sb strings.Builder
		sb.WriteByte('[')
		for i, item := range t {
			if i > 0 {
				sb.WriteString(", ")
			}
			sb.WriteString(pyValue(item))
		}
		sb.WriteByte(']')
		return sb.String()
	default:
		// 导出物只有上列形状；未知形状按 Python repr 不可得，
		// 回退 fmt.Sprint 并靠黄金值门暴露差异。
		return fmt.Sprint(t)
	}
}

// quotePy 按 Python ensure_ascii=False 的转义规则写一个 JSON 字符串。
func quotePy(s string) string {
	var sb strings.Builder
	sb.WriteByte('"')
	for _, r := range s {
		switch r {
		case '"':
			sb.WriteString(`\"`)
		case '\\':
			sb.WriteString(`\\`)
		case '\n':
			sb.WriteString(`\n`)
		case '\r':
			sb.WriteString(`\r`)
		case '\t':
			sb.WriteString(`\t`)
		case '\b':
			sb.WriteString(`\b`)
		case '\f':
			sb.WriteString(`\f`)
		default:
			if r < 0x20 {
				sb.WriteString(fmt.Sprintf(`\u%04x`, r))
				continue
			}
			if r > 0xffff {
				// 代理对写法：Python 的 ensure_ascii=False 不在此分支
				//（它直接写 UTF-8 原字），这里同样直写。
				sb.WriteRune(r)
				continue
			}
			if utf16.IsSurrogate(rune(r)) {
				sb.WriteString(fmt.Sprintf(`\u%04x`, r))
				continue
			}
			sb.WriteRune(r)
		}
	}
	sb.WriteByte('"')
	return sb.String()
}

// valueString 把导出值转成字符串（手机号/键/文本列）。
func valueString(v any) string {
	switch t := v.(type) {
	case nil:
		return ""
	case string:
		return t
	case JSONNumber:
		return t.String()
	default:
		return fmt.Sprint(t)
	}
}

// valueInt 把导出值转成 int64（deleted / user_paused / 旧 id）。
func valueInt(v any) (int64, bool) {
	switch t := v.(type) {
	case JSONNumber:
		n, err := t.Int64()
		return n, err == nil
	case string:
		n, err := strconv.ParseInt(strings.TrimSpace(t), 10, 64)
		return n, err == nil
	default:
		return 0, false
	}
}
