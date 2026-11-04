package main

// 版本注入门：真实链接 + 运行二进制的断言。
//
// # 功能
// 1. 用 -X 注入 version/commit/buildTime，跑 yiban-server --version 与
//    yiban-cli version，断言三值原样输出；
// 2. 不带 ldflags 构建，断言显性 dev/unknown/unknown；
// 3. 静态守卫 deploy/docker/Dockerfile 的 -X 符号与构建参数。
//
// # 口径
// 编译型用例在 -short 下跳过；Dockerfile 静态守卫始终运行。
// 构建时清空 GOFLAGS/CGO_ENABLED，避免外部 ldflags 污染默认档。

import (
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// buildinfoModule 是 -X 的包路径。
const buildinfoModule = "github.com/frostpolaris1/yiban-auto-sign/server/internal/buildinfo"

func TestDockerfileUsesBuildinfoSymbols(t *testing.T) {
	body, err := os.ReadFile(filepath.Join("..", "..", "..", "deploy", "docker", "Dockerfile"))
	if err != nil {
		t.Fatalf("read Dockerfile: %v", err)
	}
	compact := strings.Join(strings.Fields(string(body)), "")
	for _, want := range []string{
		"-X" + buildinfoModule + ".version=${BUILD_VERSION}",
		"-X" + buildinfoModule + ".commit=${BUILD_COMMIT}",
		"-X" + buildinfoModule + ".buildTime=${BUILD_TIME}",
	} {
		if !strings.Contains(compact, want) {
			t.Fatalf("Dockerfile 缺注入符号 %q；版本门必须覆盖镜像构建", want)
		}
	}
}

func TestVersionInjectionEndToEnd(t *testing.T) {
	if testing.Short() {
		t.Skip("SKIP: 需要编译两个二进制，-short 跳过。")
	}
	serverDir := filepath.Join("..", "..")
	tmp := t.TempDir()
	exe := ""
	if runtime.GOOS == "windows" {
		exe = ".exe"
	}
	build := func(pkg, out, ldflags string) {
		t.Helper()
		args := []string{"build", "-trimpath"}
		if ldflags != "" {
			args = append(args, "-ldflags", ldflags)
		}
		args = append(args, "-o", out, pkg)
		cmd := exec.Command("go", args...)
		cmd.Dir = serverDir
		cmd.Env = cleanBuildEnv()
		if combined, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("go build %s: %v\n%s", pkg, err, combined)
		}
	}
	output := func(bin string, args ...string) string {
		t.Helper()
		combined, err := exec.Command(bin, args...).CombinedOutput()
		if err != nil {
			t.Fatalf("%s %v: %v\n%s", filepath.Base(bin), args, err, combined)
		}
		return string(combined)
	}

	injected := "-X " + buildinfoModule + ".version=9.9.9-test" +
		" -X " + buildinfoModule + ".commit=deadbee" +
		" -X " + buildinfoModule + ".buildTime=2026-11-04T10:00:00Z"
	wantInjected := "version: 9.9.9-test\ncommit: deadbee\nbuild_time: 2026-11-04T10:00:00Z\n"

	serverInjected := filepath.Join(tmp, "yiban-server-injected"+exe)
	cliInjected := filepath.Join(tmp, "yiban-cli-injected"+exe)
	build("./cmd/server", serverInjected, injected)
	build("./cmd/cli", cliInjected, injected)
	if got := output(serverInjected, "--version"); got != wantInjected {
		t.Fatalf("yiban-server --version = %q, want %q", got, wantInjected)
	}
	if got := output(cliInjected, "version"); got != wantInjected {
		t.Fatalf("yiban-cli version = %q, want %q", got, wantInjected)
	}

	wantDefault := "version: dev\ncommit: unknown\nbuild_time: unknown\n"
	serverDefault := filepath.Join(tmp, "yiban-server-default"+exe)
	cliDefault := filepath.Join(tmp, "yiban-cli-default"+exe)
	build("./cmd/server", serverDefault, "")
	build("./cmd/cli", cliDefault, "")
	if got := output(serverDefault, "--version"); got != wantDefault {
		t.Fatalf("default yiban-server --version = %q, want %q", got, wantDefault)
	}
	if got := output(cliDefault, "version"); got != wantDefault {
		t.Fatalf("default yiban-cli version = %q, want %q", got, wantDefault)
	}
}

// cleanBuildEnv 去掉会改变构建结果的 GOFLAGS/CGO_ENABLED。
func cleanBuildEnv() []string {
	out := make([]string, 0, len(os.Environ())+2)
	for _, kv := range os.Environ() {
		if strings.HasPrefix(kv, "GOFLAGS=") || strings.HasPrefix(kv, "CGO_ENABLED=") {
			continue
		}
		out = append(out, kv)
	}
	return append(out, "GOFLAGS=", "CGO_ENABLED=0")
}
