package buildinfo

// 版本身份的三条判据：缺省显性 unknown、注入值原样、空白回落占位符。
//
// # 口径
// 测试直接改包内变量再恢复：版本注入只发生在链接期，运行期没有设置接口。

import "testing"

func setVars(t *testing.T, v, c, b string) {
	t.Helper()
	oldV, oldC, oldB := version, commit, buildTime
	version, commit, buildTime = v, c, b
	t.Cleanup(func() { version, commit, buildTime = oldV, oldC, oldB })
}

func TestCurrentDefaultsAreExplicit(t *testing.T) {
	setVars(t, "dev", "unknown", "unknown")
	got := Current()
	if got.Version != "dev" || got.Commit != "unknown" || got.BuildTime != "unknown" {
		t.Fatalf("Current() = %+v, want dev/unknown/unknown", got)
	}
}

func TestCurrentInjected(t *testing.T) {
	setVars(t, "0.7.0", "abc1234", "2026-11-04T10:00:00Z")
	got := Current()
	if got.Version != "0.7.0" || got.Commit != "abc1234" || got.BuildTime != "2026-11-04T10:00:00Z" {
		t.Fatalf("Current() = %+v", got)
	}
}

func TestCurrentBlankFallsBack(t *testing.T) {
	setVars(t, "  ", "", "\t")
	got := Current()
	if got.Version != "dev" || got.Commit != "unknown" || got.BuildTime != "unknown" {
		t.Fatalf("Current() = %+v, want fallback placeholders", got)
	}
}

func TestCurrentTrimsInjectedValues(t *testing.T) {
	setVars(t, " 0.7.0 ", " abc1234 ", " 2026-11-04T10:00:00Z ")
	got := Current()
	want := Info{Version: "0.7.0", Commit: "abc1234", BuildTime: "2026-11-04T10:00:00Z"}
	if got != want {
		t.Fatalf("Current() = %+v, want %+v", got, want)
	}
}

func TestStringFormat(t *testing.T) {
	got := Info{Version: "0.7.0", Commit: "abc1234", BuildTime: "2026-11-04T10:00:00Z"}.String()
	want := "version: 0.7.0\ncommit: abc1234\nbuild_time: 2026-11-04T10:00:00Z\n"
	if got != want {
		t.Fatalf("String() = %q, want %q", got, want)
	}
}
