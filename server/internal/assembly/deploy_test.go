package assembly

// 部署件的静态门禁。
//
// # 功能
// 1. docker-compose 的 services.app.stop_grace_period 必须大于
//    shutdownBudget：否则 docker stop 在优雅收尾完成前发 SIGKILL，
//    残余 running 行的租约到期后由回收器回收（启动 Recover 清无租约或已过期行）。
// 2. install.sh 写的 systemd 单元 TimeoutStopSec 必须大于同一预算：
//    两个入口同形状，缺一道门就有一半静默。
// 3. 镜像必须预建 /data/photos 并归 nonroot(65532)：
//    命名卷首次挂载继承镜像内目录的属主；不预建则 Docker 以 root
//    建空目录，distroless nonroot 不可写。
// 4. install.sh 生成的 systemd 单元必须带加固键：键名拼错或丢失时服务仍能
//    启动，隔离面却静默消失。
// 5. 容器备份 profile 必须同时覆盖数据库与照片，且脚本校验转储可读；
//    只备一半会在恢复时才发现。
// 6. install.sh 的 YIBAN_PHOTO_DIR 白名单：realpath 归一化后只允许
//    /var/lib/yiban/ 下的真实子目录，软链与 .. 逃逸在 chown 前拒绝。
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
	body := deployFile(t, "container/docker-compose.yml")
	grace := composeStopGrace(t, body)
	if grace <= shutdownBudget {
		t.Fatalf("stop_grace_period=%s 必须大于 shutdownBudget=%s；否则 SIGKILL 打断优雅收尾",
			grace, shutdownBudget)
	}
}

// TestInstallUnitStopTimeoutExceedsShutdownBudget 钉住 systemd 单元的停机预算。
// TimeoutStopSec 小于 shutdownBudget 时，systemd 在优雅收尾完成前发 SIGKILL。
func TestInstallUnitStopTimeoutExceedsShutdownBudget(t *testing.T) {
	body := deployFile(t, "baremetal/install.sh")
	re := regexp.MustCompile(`(?m)^TimeoutStopSec=(\d+)$`)
	m := re.FindStringSubmatch(body)
	if m == nil {
		t.Fatal("deploy/baremetal/install.sh 的 systemd 单元缺 TimeoutStopSec=<秒>")
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

// installUnitHardeningMissing 返回缺失的加固行（空 = 全在）。
// 键名拼错或丢失时服务仍能启动，但隔离面静默消失；安装器在运行时生成单元，
// 所以这里按模板行形状判（注释行以 # 开头，不会误命中）。
func installUnitHardeningMissing(body string) []string {
	wants := []string{
		"User=$SERVICE_USER",
		"Group=$SERVICE_USER",
		"UMask=0077",
		"NoNewPrivileges=true",
		"PrivateTmp=true",
		"ProtectSystem=strict",
		"ProtectHome=true",
		"ReadWritePaths=-$PHOTO_DIR",
	}
	var missing []string
	for _, want := range wants {
		re := regexp.MustCompile(`(?m)^` + regexp.QuoteMeta(want) + `$`)
		if !re.MatchString(body) {
			missing = append(missing, want)
		}
	}
	return missing
}

// TestInstallUnitHardening 钉住 systemd 单元的加固键。
// 行为面由 scripts/e2e/deploy-baremetal-e2e.sh 的 B6b 用 systemd-analyze verify
// 与 systemctl show 再验一遍。
func TestInstallUnitHardening(t *testing.T) {
	body := deployFile(t, "baremetal/install.sh")
	if missing := installUnitHardeningMissing(body); len(missing) > 0 {
		t.Fatalf("install.sh 生成的 systemd 单元缺 %q（加固面会静默丢失）", missing)
	}
	// 突变：删掉 ReadWritePaths 后判据必须变红（防“门只测 helper”）。
	mutant := strings.Replace(body, "\nReadWritePaths=-$PHOTO_DIR\n", "\n", 1)
	if mutant == body {
		t.Fatal("突变未生效：找不到 ReadWritePaths 行")
	}
	if missing := installUnitHardeningMissing(mutant); len(missing) == 0 {
		t.Fatal("删掉 ReadWritePaths 后判据仍绿")
	}
}

// installPhotoDirGuardMissing 返回照片目录白名单/软链判据的缺失片段。
func installPhotoDirGuardMissing(body string) []string {
	wants := []string{
		`realpath -m -- "$raw"`,
		`[ -L "$raw" ]`,
		`"$PHOTO_ROOT"/*) ;;`,
		`realpath -e -- "$PHOTO_DIR"`,
		`chown -h "$SERVICE_USER:$SERVICE_USER" "$PHOTO_DIR"`,
	}
	var missing []string
	for _, want := range wants {
		if !strings.Contains(body, want) {
			missing = append(missing, want)
		}
	}
	return missing
}

// TestInstallPhotoDirGuard 钉住 P1-1 的白名单：realpath 归一化、只允许
// /var/lib/yiban/ 下的真实子目录、软链判据、chown 前复核与非递归 chown。
// 每条判据都配一次内存突变：删掉任一条，判据必须变红。
func TestInstallPhotoDirGuard(t *testing.T) {
	body := deployFile(t, "baremetal/install.sh")
	if missing := installPhotoDirGuardMissing(body); len(missing) > 0 {
		t.Fatalf("install.sh 缺照片目录判据 %q", missing)
	}
	mutations := []struct {
		name string
		old  string
	}{
		{"whitelist", "        \"$PHOTO_ROOT\"/*) ;;\n"},
		{"realpath-m", `realpath -m -- "$raw"`},
		{"symlink-source", `[ -L "$raw" ]`},
		{"realpath-e", `realpath -e -- "$PHOTO_DIR"`},
		{"chown-h", `chown -h "$SERVICE_USER:$SERVICE_USER" "$PHOTO_DIR"`},
	}
	for _, m := range mutations {
		mutant := strings.Replace(body, m.old, "", 1)
		if mutant == body {
			t.Fatalf("突变未生效：找不到 %s 判据", m.name)
		}
		if missing := installPhotoDirGuardMissing(mutant); len(missing) == 0 {
			t.Fatalf("删掉 %s 后判据仍绿", m.name)
		}
	}
}

func TestDockerfilePhotoDirOwnedByNonroot(t *testing.T) {
	// distroless/static-debian12:nonroot 的运行用户。
	const nonroot = "65532"
	body := deployFile(t, "container/Dockerfile")
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
	body := deployFile(t, "container/docker-compose.yml")
	if !strings.Contains(body, ":/data/photos") {
		t.Fatal("docker-compose.yml 未把照片卷挂在 /data/photos")
	}
}

// TestContainerBackupProfileCoversDBAndPhotos 钉住容器备份的接线：
// 任务只在 backup profile 下出现，脚本被挂进容器，且同时导出数据库与照片。
// 判据缺失时备份会退化成只有数据库（或只有文档），恢复时才发现照片丢了。
func TestContainerBackupProfileCoversDBAndPhotos(t *testing.T) {
	compose := deployFile(t, "container/docker-compose.yml")
	for _, want := range []string{
		`profiles: ["backup"]`,
		"./backup.sh:/usr/local/bin/yiban-backup:ro",
		"${YIBAN_BACKUP_HOST_DIR:-./backups}:/backups",
		"photos:/photos",
	} {
		if !strings.Contains(compose, want) {
			t.Errorf("docker-compose.yml 缺 %q（容器备份路径不完整）", want)
		}
	}
	script, err := readRepoFile("deploy/container/backup.sh")
	if err != nil {
		t.Fatalf("read deploy/container/backup.sh: %v", err)
	}
	for _, want := range []string{
		"pg_dump -Fc",
		"pg_restore --list",
		"photos.tar.gz",
		"sha256sum -c SHA256SUMS",
	} {
		if !strings.Contains(string(script), want) {
			t.Errorf("backup.sh 缺 %q（备份未覆盖数据库或照片，或没做可读性校验）", want)
		}
	}
}
