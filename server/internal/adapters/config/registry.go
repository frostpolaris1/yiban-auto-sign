// 本文件解析编译期嵌入的配置名册（internal/configdata/registry.json）。
//
// 功能：把名册解析成有序条目：键、类型、分组、敏感级别与缺省值。
// 本文件**不**读取任何 YIBAN_* 的值，**不**判断运行环境。
//
// 归属：adapters 层设施包。cmd/server 经组合根注入 app/settings；
// doctor 与本文件共用同一份嵌入名册。
//
// 口径：名册随二进制一起发布（go:embed），不存在安装位文件，
// 也没有"名册缺失即降级"的分支。名册损坏（空、非法缺省值）是构建缺陷，
// 一律报错，**不返回空名册冒充成功**。

package config

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"sort"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/configdata"
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

// RegistryJSON 返回嵌入名册的原文副本。
// 测试与门用它核对名册文件，不复制任何缺省值字面量。
func RegistryJSON() []byte { return configdata.JSON() }

// Registry 解析嵌入名册。返回按名册键排序的条目。
func Registry() ([]RegistryEntry, error) { return ParseRegistry(configdata.JSON()) }

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
