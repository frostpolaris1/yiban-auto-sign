package main

// build-release.sh 接线门（2026-11-05 版本批）。
//
// # 功能
// 真跑仓库根的 server/scripts/build-release.sh 到 t.TempDir()，再执行产出的
// yiban-server 与 yiban-cli，断言两者自报 version == 仓库根 VERSION，且
// commit/build_time 不是占位符（证明 -ldflags -X 注入真的发生，不是回落
// dev/unknown）。Python 侧 tests/test_build_version_wiring.py 钉静态接线；
// 本门证执行面，两者并存防漂移。
//
// # 口径
// - -short 显性跳过（本门真编译两个二进制）；CI 的 server/scripts/verify.sh
//   不传 -short，门在 CI 真跑。
// - 缺 bash/git/go 时显性 Skip 并说明（Windows 可跑 Git Bash）。
// - 产物命名规则与 build-release.sh 一致：Windows 宿主带 .exe 后缀
//   （Go 的 os/exec 在 Windows 不认无后缀 PE）。
// - 传给脚本的目录一律转成正斜杠：Windows 上 t.TempDir() 的反斜杠路径
//   在 MSYS bash 里不是可靠路径写法。
// - 预算：模块缓存热时两个 go build 约数秒（实测本机 <30s）。

import (
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
	"testing"
)

func TestBuildReleaseScriptWiresVersion(t *testing.T) {
	if testing.Short() {
		t.Skip("SKIP: build-release.sh 真编译两个二进制；-short 跳过（CI 不传 -short，门在 CI 真跑）。")
	}
	for _, tool := range []string{"bash", "git", "go"} {
		if _, err := exec.LookPath(tool); err != nil {
			t.Skipf("SKIP: %s not in PATH; build-release.sh cannot run (Windows: run under Git Bash).", tool)
		}
	}

	root, err := filepath.Abs(filepath.Join("..", "..", ".."))
	if err != nil {
		t.Fatalf("locate repo root: %v", err)
	}
	script := filepath.Join(root, "server", "scripts", "build-release.sh")
	if _, err := os.Stat(script); err != nil {
		t.Fatalf("build-release.sh missing: %v", err)
	}
	verRaw, err := os.ReadFile(filepath.Join(root, "VERSION"))
	if err != nil {
		t.Fatalf("read VERSION: %v", err)
	}
	ver := strings.TrimSpace(string(verRaw))
	if ver == "" {
		t.Fatalf("VERSION is empty; Go line must carry a single X.Y.Z line")
	}

	out := t.TempDir()
	cmd := exec.Command("bash", script, filepath.ToSlash(out))
	cmd.Dir = root
	combined, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("build-release.sh: %v\n%s", err, combined)
	}

	exe := ""
	if runtime.GOOS == "windows" {
		exe = ".exe"
	}
	var serverBin, cliBin string
	for _, name := range []string{"yiban-server", "yiban-cli"} {
		p := filepath.Join(out, name+exe)
		if _, err := os.Stat(p); err != nil {
			t.Fatalf("build-release.sh did not produce %s: %v\n%s", name+exe, err, combined)
		}
		if name == "yiban-server" {
			serverBin = p
		} else {
			cliBin = p
		}
	}

	// line 抽取只认构建包自己的稳定输出（buildinfo.Info.String）：
	// version: X / commit: Y / build_time: Z。
	line := func(output, key, bin string) string {
		t.Helper()
		re := regexp.MustCompile(`(?m)^` + key + `: (.+)$`)
		m := re.FindStringSubmatch(output)
		if m == nil {
			t.Fatalf("%s output has no %q line:\n%s", filepath.Base(bin), key, output)
		}
		return strings.TrimSpace(m[1])
	}
	selfReport := func(bin string, args ...string) string {
		t.Helper()
		b, err := exec.Command(bin, args...).CombinedOutput()
		if err != nil {
			t.Fatalf("%s %v: %v\n%s", filepath.Base(bin), args, err, b)
		}
		return string(b)
	}

	checks := []struct {
		name string
		bin  string
		args []string
	}{
		{"yiban-server", serverBin, []string{"--version"}},
		{"yiban-cli", cliBin, []string{"version"}},
	}
	for _, c := range checks {
		output := selfReport(c.bin, c.args...)
		if got := line(output, "version", c.bin); got != ver {
			t.Fatalf("%s self-reports version %q, want %q (repo root VERSION)", c.name, got, ver)
		}
		if got := line(output, "commit", c.bin); got == "unknown" || got == "" {
			t.Fatalf("%s commit is %q; build-release.sh must inject a real commit", c.name, got)
		}
		if got := line(output, "build_time", c.bin); got == "unknown" || got == "" {
			t.Fatalf("%s build_time is %q; build-release.sh must inject the build time", c.name, got)
		}
	}
}
