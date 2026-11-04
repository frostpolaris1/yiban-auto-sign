package config

// 配置名册解析的进程内测试。
//
// 口径：解析只做形状校验，不读环境、不判运行环境。
// 空名册、非法缺省值与非标量缺省值必须失败：空名册不得当绿。

import (
	"errors"
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

func TestRegistryCandidatesInstallPathFirst(t *testing.T) {
	candidates := RegistryCandidates()
	if len(candidates) < 2 {
		t.Fatalf("candidates = %v", candidates)
	}
	if candidates[0] != "/etc/yiban/registry.json" {
		t.Fatalf("candidates[0] = %q, want the installed path first", candidates[0])
	}
}

func TestLoadRegistryMissingIsTyped(t *testing.T) {
	_, err := LoadRegistry(t.TempDir() + "/absent.json")
	if !errors.Is(err, ErrRegistryNotFound) {
		t.Fatalf("err = %v, want ErrRegistryNotFound for an explicit missing path", err)
	}
}
