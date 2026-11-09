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

// Scope 是名册条目的读者面。
// server 键由服务进程（internal/adapters/config）读取，进设置页与三份模板；
// cli 键只被运维 CLI（backup/migrate-legacy/bench）读取，不进设置页与模板。
type Scope string

const (
	// ScopeServer 是服务进程读取的键。
	ScopeServer Scope = "server"
	// ScopeCLI 是运维 CLI 读取的键。doctor 的未登记键体检按全集校验：
	// 误报会诱导运维删掉 backup 的口令，退回明文备份。
	ScopeCLI Scope = "cli"
)

// RegistryEntry 是名册里的一行。
// Default 只在 HasDefault 为真时有意义；显式 null 按无缺省处理。
type RegistryEntry struct {
	Key        string
	Type       string
	Group      string
	Scope      Scope
	Sensitive  bool
	Default    any
	HasDefault bool
	// Domain 是数值键的合法域 [min,max]（名册 domain 字段的数值二元组）；
	// 布尔与字符串键为 nil。sett2 写回面的域校验从这里注入。
	Domain []float64
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
			Scope     string          `json:"scope"`
			Sensitive bool            `json:"sensitive"`
			Default   json.RawMessage `json:"default"`
			Domain    json.RawMessage `json:"domain"`
		}
		if err := json.Unmarshal(document[key], &item); err != nil {
			return nil, fmt.Errorf("config: registry key %s: %w", key, err)
		}
		// scope 缺省按 server：名册的绝大多数条目是服务端键；
		// 嵌入名册的每条都显式写 scope，由门核对，不靠缺省。
		scope := Scope(item.Scope)
		switch scope {
		case "":
			scope = ScopeServer
		case ScopeServer, ScopeCLI:
		default:
			return nil, fmt.Errorf("config: registry key %s has unknown scope %q (want server or cli)", key, item.Scope)
		}
		entry := RegistryEntry{Key: key, Type: item.Type, Scope: scope, Sensitive: item.Sensitive}
		if len(item.Group) > 0 {
			entry.Group = item.Group[0]
		}
		if len(item.Domain) > 0 {
			// domain 有两种形状：数值二元组 [min,max]（int/float 键）
			// 与字符串闭集（enum 键，如 YIBAN_ENV）。写回面只消费
			// 数值域；枚举域由各自的读取代码持有，这里不展开。
			var domain []any
			if err := json.Unmarshal(item.Domain, &domain); err != nil {
				return nil, fmt.Errorf("config: registry key %s domain: %w", key, err)
			}
			if len(domain) == 2 {
				min, minOK := domain[0].(float64)
				max, maxOK := domain[1].(float64)
				if minOK && maxOK {
					if min > max {
						return nil, fmt.Errorf("config: registry key %s domain must be [min,max]", key)
					}
					entry.Domain = []float64{min, max}
				}
			}
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
