package config

// 配置名册解析的进程内测试。
//
// 口径：解析只做形状校验，不读环境、不判运行环境。
// 空名册、非法缺省值与非标量缺省值必须失败：空名册不得当绿。

import (
	"testing"
)

func TestParseRegistry(t *testing.T) {
	raw := []byte(`{
	  "_meta": {"version": 1},
	  "B_KEY": {"type": "bool", "default": true, "group": ["发布"], "sensitive": false, "scope": "server"},
	  "A_KEY": {"type": "string", "default": "x", "group": ["部署", "备用"], "sensitive": true, "scope": "cli"},
	  "C_KEY": {"type": "string", "default": null, "group": ["部署"]},
	  "D_KEY": {"type": "int", "group": ["调度"], "scope": "server"}
	}`)
	entries, err := ParseRegistry(raw)
	if err != nil {
		t.Fatalf("ParseRegistry: %v", err)
	}
	if len(entries) != 4 {
		t.Fatalf("entries = %d, want 4 (meta excluded)", len(entries))
	}
	byKey := map[string]RegistryEntry{}
	for _, entry := range entries {
		byKey[entry.Key] = entry
	}
	if got := byKey["A_KEY"]; got.Group != "部署" || !got.Sensitive ||
		!got.HasDefault || got.Default != "x" || got.Scope != ScopeCLI {
		t.Fatalf("A_KEY = %+v", got)
	}
	if got := byKey["B_KEY"]; got.Default != true || !got.HasDefault || got.Scope != ScopeServer {
		t.Fatalf("B_KEY = %+v", got)
	}
	// scope 缺省按 server：显式字段是嵌入名册的门（见 parity），解析不强制。
	if got := byKey["C_KEY"]; got.HasDefault || got.Default != nil || got.Scope != ScopeServer {
		t.Fatalf("C_KEY = %+v, null default must be no default", got)
	}
	if got := byKey["D_KEY"]; got.HasDefault || got.Default != nil || got.Scope != ScopeServer {
		t.Fatalf("D_KEY = %+v, missing default must be no default", got)
	}
	// 排序稳定：A、B、C、D。
	for i, want := range []string{"A_KEY", "B_KEY", "C_KEY", "D_KEY"} {
		if entries[i].Key != want {
			t.Fatalf("entries[%d] = %q, want %q", i, entries[i].Key, want)
		}
	}
}

func TestParseRegistryRejectsInvalid(t *testing.T) {
	cases := map[string]string{
		"empty":          "",
		"empty object":   `{"_meta": {"version": 1}}`,
		"not json":       `{`,
		"array default":  `{"K": {"default": [1,2]}}`,
		"object default": `{"K": {"default": {"a": 1}}}`,
		"unknown scope":  `{"K": {"scope": "operator"}}`,
	}
	for name, raw := range cases {
		t.Run(name, func(t *testing.T) {
			if _, err := ParseRegistry([]byte(raw)); err == nil {
				t.Fatal("ParseRegistry must fail")
			}
		})
	}
}

// TestEmbeddedRegistryLoads 钉住嵌入名册本身可解析且非空。
// 名册随二进制发布：空名册或形状损坏是构建缺陷，必须当红。
func TestEmbeddedRegistryLoads(t *testing.T) {
	entries, err := Registry()
	if err != nil {
		t.Fatalf("Registry: %v", err)
	}
	if len(entries) < 30 {
		t.Fatalf("嵌入名册只有 %d 条——删空或截断即拒判", len(entries))
	}
	server, cli := 0, 0
	for _, entry := range entries {
		if entry.Key == "" || entry.Group == "" || entry.Type == "" {
			t.Fatalf("名册条目缺键/分组/类型: %+v", entry)
		}
		// scope 必须显式且在闭集内：settings 只列 server、doctor 按全集校验，
		// 两者的分界靠它，不能是解析缺省。
		switch entry.Scope {
		case ScopeServer:
			server++
		case ScopeCLI:
			cli++
		default:
			t.Fatalf("名册条目 %s 的 scope = %q，want 显式 server 或 cli", entry.Key, entry.Scope)
		}
	}
	if server == 0 || cli == 0 {
		t.Fatalf("scope 分区塌了：server %d / cli %d", server, cli)
	}
}

// TestRegistryJSONIsACopy 钉住调用方改不到嵌入字节。
func TestRegistryJSONIsACopy(t *testing.T) {
	first := RegistryJSON()
	if len(first) == 0 {
		t.Fatal("RegistryJSON is empty")
	}
	first[0] = 'X'
	if second := RegistryJSON(); len(second) == 0 || second[0] == 'X' {
		t.Fatal("RegistryJSON 返回了共享切片：嵌入名册被调用方改写")
	}
}
