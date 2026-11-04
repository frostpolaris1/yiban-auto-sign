// 本文件读取配置名册（config/registry.json）。
//
// 功能：把名册解析成有序条目：键、类型、分组、敏感级别与缺省值。
// 本文件**不**读取任何 YIBAN_* 的值，**不**判断运行环境。
//
// 归属：adapters 层设施包。cmd/server 经组合根注入 app/settings；
// doctor 与本文件共用 RegistryCandidates 的查找顺序。
//
// 口径：安装位优先（/etc/yiban/registry.json），其次仓库根
// config/registry.json。文件缺失、空名册或非法缺省值一律报错，
// **不返回空名册冒充成功**。

package config

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"sort"
)

// RegistryEntry 是名册里的一行。
// Default 只在 HasDefault 为真时有意义；显式 null 按无缺省处理。
type RegistryEntry struct {
	Key        string
	Type       string
	Group      string
	Sensitive  bool
	Default    any
	HasDefault bool
}

// RegistryCandidates 是名册的缺省查找顺序：安装位优先。
// install.sh 把 config/registry.json 装到 /etc/yiban/registry.json。
func RegistryCandidates() []string {
	return []string{"/etc/yiban/registry.json", "config/registry.json"}
}

// ErrRegistryNotFound 表示按缺省顺序找不到名册文件。
// 调用方据此区分「未安装名册」（可降级）与「名册损坏」（应失败关闭）。
var ErrRegistryNotFound = errors.New("config: configuration registry not found")

// LoadRegistry 读取名册。explicit 非空时只读该路径；
// 为空时按 RegistryCandidates 顺序取首个可读文件。
func LoadRegistry(explicit string) ([]RegistryEntry, error) {
	path := explicit
	if path == "" {
		for _, candidate := range RegistryCandidates() {
			if info, err := os.Stat(candidate); err == nil && !info.IsDir() {
				path = candidate
				break
			}
		}
	}
	if path == "" {
		return nil, fmt.Errorf("%w (tried %v)", ErrRegistryNotFound, RegistryCandidates())
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, fmt.Errorf("%w (%s)", ErrRegistryNotFound, path)
		}
		return nil, fmt.Errorf("config: read configuration registry %s: %w", path, err)
	}
	return ParseRegistry(raw)
}

// ParseRegistry 解析名册字节。
// 逐键校验形状；出现未知的缺省值类型即失败，不静默丢弃。
func ParseRegistry(raw []byte) ([]RegistryEntry, error) {
	if len(bytes.TrimSpace(raw)) == 0 {
		return nil, errors.New("config: configuration registry is empty")
	}
	var document map[string]json.RawMessage
	if err := json.Unmarshal(raw, &document); err != nil {
		return nil, fmt.Errorf("config: parse configuration registry: %w", err)
	}
	keys := make([]string, 0, len(document))
	for key := range document {
		if key == "_meta" {
			continue
		}
		keys = append(keys, key)
	}
	sort.Strings(keys)
	if len(keys) == 0 {
		return nil, errors.New("config: configuration registry has no keys")
	}
	entries := make([]RegistryEntry, 0, len(keys))
	for _, key := range keys {
		var item struct {
			Type      string          `json:"type"`
			Group     []string        `json:"group"`
			Sensitive bool            `json:"sensitive"`
			Default   json.RawMessage `json:"default"`
		}
		if err := json.Unmarshal(document[key], &item); err != nil {
			return nil, fmt.Errorf("config: registry key %s: %w", key, err)
		}
		entry := RegistryEntry{Key: key, Type: item.Type, Sensitive: item.Sensitive}
		if len(item.Group) > 0 {
			entry.Group = item.Group[0]
		}
		if len(item.Default) > 0 && string(item.Default) != "null" {
			var value any
			if err := json.Unmarshal(item.Default, &value); err != nil {
				return nil, fmt.Errorf("config: registry key %s default: %w", key, err)
			}
			switch value.(type) {
			case string, bool, float64:
				entry.Default = value
				entry.HasDefault = true
			default:
				return nil, fmt.Errorf("config: registry key %s has a non-scalar default", key)
			}
		}
		entries = append(entries, entry)
	}
	return entries, nil
}
