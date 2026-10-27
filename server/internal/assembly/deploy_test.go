package assembly

// 部署件的静态门禁（派单 F4、F5、P2-3、P2-4）。
//
// # 功能
// 1. docker-compose 的 services.app.stop_grace_period 必须大于
//    shutdownBudget：否则 docker stop 在优雅收尾完成前发 SIGKILL，
//    残余 running 行只能靠下次启动的 Recover 回收。
// 2. install.sh 写的 systemd 单元 TimeoutStopSec 必须大于同一预算：
//    两个入口同形状，缺一道门就有一半静默。
// 3. 镜像必须预建 /data/photos 并归 nonroot(65532)：
//    命名卷首次挂载继承镜像内目录的属主；不预建则 Docker 以 root
//    建空目录，distroless nonroot 不可写。
//
// # 口径
// 只读文件，不启容器：判据都在构建产物里可判。
// 真容器写文件另由 YIBAN_DOCKER_E2E=1 的 e2e 覆盖（cmd/cli）。

import (
	"os"
	"path/filepath"
	"regexp"
	"strconv"
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

// yamlKeyValue 拆一行 YAML 的 `key: value`。列表项返回 ok=false。
func yamlKeyValue(line string) (key, value string, ok bool) {
	trimmed := strings.TrimSpace(line)
	if strings.HasPrefix(trimmed, "- ") || trimmed == "-" {
		return "", "", false
	}
	k, v, found := strings.Cut(trimmed, ":")
	if !found || strings.TrimSpace(k) == "" {
		return "", "", false
	}
	return strings.TrimSpace(k), strings.Trim(strings.TrimSpace(v), `"'`), true
}

// leadingIndent 返回行首空格数。
func leadingIndent(line string) int {
	return len(line) - len(strings.TrimLeft(line, " "))
}

// composeBlockValue 取 `section.service.key` 的值。
// 按缩进入块：只认 services → app 块内的键。
// 把 stop_grace_period 挪到 db 服务下时这里取不到，门必红。
func composeBlockValue(body, section, service, key string) (string, bool) {
	inSection, inService := false, false
	sectionIndent, serviceIndent := -1, -1
	for _, line := range strings.Split(body, "\n") {
		trimmed := strings.TrimSpace(line)
		if trimmed == "" || strings.HasPrefix(trimmed, "#") {
			continue
		}
		indent := leadingIndent(line)
		k, v, ok := yamlKeyValue(trimmed)
		if !ok {
			continue
		}
		// 缩进回到块头层级即离开该块。
		if inService && indent <= serviceIndent {
			inService = false
		}
		if inSection && indent <= sectionIndent {
			inSection = false
		}
		switch {
		case !inSection:
			if k == section && v == "" {
				inSection = true
				sectionIndent = indent
			}
		case !inService:
			if k == service && v == "" {
				inService = true
				serviceIndent = indent
			}
		default:
			if k == key {
				return v, true
			}
		}
	}
	return "", false
}

// composeStopGrace 从 compose 文本里取 app 服务的 stop_grace_period。
func composeStopGrace(t *testing.T, body string) time.Duration {
	t.Helper()
	raw, ok := composeBlockValue(body, "services", "app", "stop_grace_period")
	if !ok {
		t.Fatalf("docker-compose.yml 的 services.app 未声明 stop_grace_period")
	}
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

// TestInstallUnitStopTimeoutExceedsShutdownBudget 钉住 systemd 单元的停机预算（派单 P2-4）。
// TimeoutStopSec 小于 shutdownBudget 时，systemd 在优雅收尾完成前发 SIGKILL。
func TestInstallUnitStopTimeoutExceedsShutdownBudget(t *testing.T) {
	body := deployFile(t, "linux/install.sh")
	re := regexp.MustCompile(`(?m)^TimeoutStopSec=(\d+)$`)
	m := re.FindStringSubmatch(body)
	if m == nil {
		t.Fatal("deploy/linux/install.sh 的 systemd 单元缺 TimeoutStopSec=<秒>")
	}
	secs, err := strconv.Atoi(m[1])
	if err != nil {
		t.Fatalf("TimeoutStopSec=%q 不是整数秒: %v", m[1], err)
	}
	if d := time.Duration(secs) * time.Second; d <= shutdownBudget {
		t.Fatalf("TimeoutStopSec=%ds 必须大于 shutdownBudget=%s；否则 SIGKILL 打断优雅收尾",
			secs, shutdownBudget)
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
