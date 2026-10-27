package main

// 容器照片目录可写性的 e2e（派单 F4）。
//
// # 功能
// 模拟真实部署动作：构建镜像 → 用命名卷挂 /data/photos →
// 以镜像自带用户跑 `yiban-cli doctor --photo-dir /data/photos --service-user nonroot`。
// 断言 fs.photo_dir 与 fs.photo_owner 都通过：distroless nonroot(65532)
// 既能写卷，属主也是服务用户。
// 镜像若未预建并 chown 该目录，命名卷以 root 属主挂载，两项都会 FAIL。
//
// # 口径
// 构建镜像耗时且需要 daemon，默认跳过；置 YIBAN_DOCKER_E2E=1 才跑。
// server/scripts/verify.sh 的容器段默认带该开关跑（docker 可用时）。
// 跳过行显性打印（AGENTS §30）。卷先删后建，用毕删除。

import (
	"bytes"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// dockerRun 跑一条 docker 命令并返回输出与退出码。
func dockerRun(t *testing.T, args ...string) (string, int) {
	t.Helper()
	cmd := exec.Command("docker", args...)
	var buf bytes.Buffer
	cmd.Stdout = &buf
	cmd.Stderr = &buf
	err := cmd.Run()
	if err != nil {
		if exit, ok := err.(*exec.ExitError); ok {
			return buf.String(), exit.ExitCode()
		}
		t.Fatalf("docker %s: %v", strings.Join(args, " "), err)
	}
	return buf.String(), 0
}

func TestContainerPhotoDirWritableByNonroot(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("SKIP: 容器 e2e 需要 POSIX 路径与 bash 构建上下文")
	}
	if os.Getenv("YIBAN_DOCKER_E2E") != "1" {
		t.Skip("SKIP: 容器 e2e 需 YIBAN_DOCKER_E2E=1（构建镜像耗时，不进默认套件）")
	}
	if _, err := exec.LookPath("docker"); err != nil {
		t.Skip("SKIP: 本机无 docker CLI，容器判据未实跑")
	}
	if out, rc := dockerRun(t, "info"); rc != 0 {
		t.Skipf("SKIP: docker daemon 不可用，容器判据未实跑: %s", out)
	}

	root, err := filepath.Abs(filepath.Join("..", "..", ".."))
	if err != nil {
		t.Fatalf("resolve repo root: %v", err)
	}
	const image = "yiban-p7-photo-test:local"
	buildArgs := []string{"build", "-f", filepath.Join(root, "deploy", "docker", "Dockerfile"), "-t", image}
	// 受限网络可给 YIBAN_DOCKER_GOPROXY 覆盖构建段代理；
	// 缺省不注入，与 Dockerfile 的口径一致。
	if proxy := os.Getenv("YIBAN_DOCKER_GOPROXY"); proxy != "" {
		buildArgs = append(buildArgs, "--build-arg", "GOPROXY="+proxy)
	}
	out, rc := dockerRun(t, append(buildArgs, root)...)
	if rc != 0 {
		t.Fatalf("docker build rc=%d:\n%s", rc, out)
	}

	vol := "yiban-p7-photo-vol"
	dockerRun(t, "volume", "rm", "-f", vol)
	if out, rc := dockerRun(t, "volume", "create", vol); rc != 0 {
		t.Fatalf("docker volume create rc=%d:\n%s", rc, out)
	}
	t.Cleanup(func() { dockerRun(t, "volume", "rm", "-f", vol) })

	// 以镜像缺省用户（nonroot）跑 doctor 的文件系统面。
	// --service-user nonroot 显式钉住服务身份：镜像里没有 systemd 单元，
	// 缺省虽已解析为当前用户，e2e 不靠缺省值证明容器语义。
	out, rc = dockerRun(t, "run", "--rm", "--entrypoint", "/usr/local/bin/yiban-cli",
		"-v", vol+":/data/photos", image,
		"doctor", "--photo-dir", "/data/photos", "--service-user", "nonroot")
	for _, name := range []string{"fs.photo_dir", "fs.photo_owner"} {
		if !strings.Contains(out, name) {
			t.Fatalf("doctor 输出缺 %s 行:\n%s", name, out)
		}
		if strings.Contains(out, "[FAIL] "+name) {
			t.Fatalf("%s 失败：nonroot 不能写 /data/photos 或属主不是 nonroot:\n%s", name, out)
		}
		if !strings.Contains(out, "[ OK ] "+name) {
			t.Fatalf("%s 未判 OK（rc=%d）:\n%s", name, rc, out)
		}
	}
}
