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
	  "B_KEY": {"type": "bool", "default": true, "group": ["发布"], "sensitive": false},
	  "A_KEY": {"type": "string", "default": "x", "group": ["部署", "备用"], "sensitive": true},
	  "C_KEY": {"type": "string", "default": null, "group": ["部署"]},
	  "D_KEY": {"type": "int", "group": ["调度"]}
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
		!got.HasDefault || got.Default != "x" {
		t.Fatalf("A_KEY = %+v", got)
	}
	if got := byKey["B_KEY"]; got.Default != true || !got.HasDefault {
		t.Fatalf("B_KEY = %+v", got)
	}
	if got := byKey["C_KEY"]; got.HasDefault || got.Default != nil {
		t.Fatalf("C_KEY = %+v, null default must be no default", got)
	}
	if got := byKey["D_KEY"]; got.HasDefault || got.Default != nil {
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
	for _, entry := range entries {
		if entry.Key == "" || entry.Group == "" || entry.Type == "" {
			t.Fatalf("名册条目缺键/分组/类型: %+v", entry)
		}
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
