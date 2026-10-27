package assembly

// 部署件的静态门禁（派单 F4、F5）。
//
// # 功能
// 1. docker-compose 的 stop_grace_period 必须大于 shutdownBudget：
//    否则 docker stop 在优雅收尾完成前发 SIGKILL，残余 running 行
//    只能靠下次启动的 Recover 回收。
// 2. 镜像必须预建 /data/photos 并归 nonroot(65532)：
//    命名卷首次挂载继承镜像内目录的属主；不预建则 Docker 以 root
//    建空目录，distroless nonroot 不可写。
//
// # 口径
// 只读文件，不启容器：两个判据都在构建产物里可判。
// 真容器写文件另由 YIBAN_DOCKER_E2E=1 的 e2e 覆盖（cmd/cli）。

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
	"time"
)

// repoRoot 是 server/internal/assembly 向上三级（本包的测试工作目录）。
func repoRoot() string { return filepath.Join("..", "..", "..") }

// readRepoFile 读仓库内相对路径的文件。
func readRepoFile(rel string) ([]byte, error) {
	return os.ReadFile(filepath.Join(repoRoot(), filepath.FromSlash(rel)))
}

// deployFile 读仓库内 deploy/ 下的文件。
func deployFile(t *testing.T, rel string) string {
	t.Helper()
	body, err := readRepoFile("deploy/" + rel)
	if err != nil {
		t.Fatalf("read %s: %v", rel, err)
	}
	return string(body)
}

// composeStopGrace 从 compose 文本里取 app 服务的 stop_grace_period。
func composeStopGrace(t *testing.T, body string) time.Duration {
	t.Helper()
	// 取最后一个 stop_grace_period：本文件只在 app 服务下声明。
	re := regexp.MustCompile(`(?m)^\s*stop_grace_period:\s*"?([0-9]+[smh][0-9smh]*)"?\s*$`)
	all := re.FindAllStringSubmatch(body, -1)
	if len(all) == 0 {
		t.Fatalf("docker-compose.yml 未声明 stop_grace_period")
	}
	raw := all[len(all)-1][1]
	d, err := time.ParseDuration(raw)
	if err != nil {
		t.Fatalf("stop_grace_period %q 不是合法时长: %v", raw, err)
	}
	return d
}

func TestComposeStopGraceExceedsShutdownBudget(t *testing.T) {
	body := deployFile(t, "docker/docker-compose.yml")
	grace := composeStopGrace(t, body)
	if grace <= shutdownBudget {
		t.Fatalf("stop_grace_period=%s 必须大于 shutdownBudget=%s；否则 SIGKILL 打断优雅收尾",
			grace, shutdownBudget)
	}
}

func TestDockerfilePhotoDirOwnedByNonroot(t *testing.T) {
	// distroless/static-debian12:nonroot 的运行用户。
	const nonroot = "65532"
	body := deployFile(t, "docker/Dockerfile")
	for _, line := range strings.Split(body, "\n") {
		trimmed := strings.TrimSpace(line)
		if strings.HasPrefix(trimmed, "#") {
			continue
		}
		if !strings.Contains(trimmed, "/data/photos") {
			continue
		}
		if !strings.HasPrefix(strings.ToUpper(trimmed), "COPY") {
			continue
		}
		if !strings.Contains(trimmed, "--chown="+nonroot+":"+nonroot) {
			t.Fatalf("COPY /data/photos 必须带 --chown=%s:%s：%s", nonroot, nonroot, trimmed)
		}
		return
	}
	t.Fatalf("Dockerfile 未预建 /data/photos（命名卷会以 root 属主挂载，nonroot 不可写）")
}

// TestComposePhotoVolumeTarget 钉住卷挂载点与镜像预建目录一致。
func TestComposePhotoVolumeTarget(t *testing.T) {
	body := deployFile(t, "docker/docker-compose.yml")
	if !strings.Contains(body, ":/data/photos") {
		t.Fatal("docker-compose.yml 未把照片卷挂在 /data/photos")
	}
}
