#!/usr/bin/env bash
# ============================================================
# scripts/e2e/deploy-baremetal-e2e.sh — 裸机部署路径的干净环境 e2e（工单 6jk）。
#
# 模拟的真实用户动作（AGENTS §8g）：
#   一台干净的 Ubuntu 24.04 主机按 deploy/linux/install.sh 的用法，
#   落位二进制、生成 /etc/yiban/.env、装 systemd 单元、enable --now，
#   然后访问 /healthz、再停机。
#
# 为什么容器内仍是「真装」（AGENTS §32）：
#   被测性质逐条列出：install.sh 的落位/属主/权限、systemd 单元启动、
#   服务连通真 PostgreSQL 18、/healthz 应答、SIGTERM 后退出码 0。
#   本脚本用一次性 Ubuntu 24.04 容器 + 真 systemd（PID1）+ 真
#   useradd/chown/systemctl + 真 PostgreSQL 18 覆盖上述全部性质。
#   不等价的一维：容器没有真实内核启动与硬件；network-online.target
#   由 systemd 直接满足。该维不影响被测性质，摘要里显式打印。
#   不使用假 systemctl、不使用 YIBAN_INSTALL_ROOT 干跑前缀。
#
# 步骤：
#   B1 前置与干净环境      B2 OS 镜像（systemd + openssl）
#   B3 干净 PostgreSQL 18   B4 构建二进制（golang 容器，真编译）
#   B5 一次性 OS 容器       B6 install.sh 真装
#   B7 服务状态             B8 落位与权限
#   B9 迁移版本与干净库     B10 SIGTERM 优雅停机
#   B11 资源清理
# 失败口径：退出码 1，并打印 failed step: <ID> <标题>；每步日志在 --log-dir。
# 退出码 2 = 用法或环境错误（缺 docker 等）。
#
# 用法（仓库根目录，WSL/Linux；需要 docker）：
#   bash scripts/e2e/deploy-baremetal-e2e.sh
#   bash scripts/e2e/deploy-baremetal-e2e.sh --keep --log-dir /root/dvlogs/6jk-baremetal
#
# 环境变量：
#   DEPLOY_E2E_PREFIX       资源名前缀（默认 yiban-e2e-baremetal）
#   DEPLOY_E2E_OS_IMAGE     一次性 OS 镜像（默认 yiban-e2e-os:ubuntu24.04）
#   DEPLOY_E2E_OS_BASE      构建 OS 镜像的基底（默认 ubuntu:24.04）
#   DEPLOY_E2E_PG_IMAGE     PostgreSQL 镜像（默认 postgres:18）
#   DEPLOY_E2E_GO_IMAGE     编译用镜像（默认 golang:1.27-alpine）
#   DEPLOY_E2E_GOPROXY      Go 模块代理（受限网络用，如 https://goproxy.cn,direct）
#   DEPLOY_E2E_SERVER_BIN   复用已构建的服务二进制（排障用，跳过 B4 编译）
#   DEPLOY_E2E_CLI_BIN      同上，CLI 二进制
#
# 发布门状态：发布前手跑项（MANUAL）。本脚本需要 docker 的 --privileged
#   与 host cgroup，尚未在 GitHub 托管 runner 上实测；故不进自动轨。
#   加入 CI 前须先在托管 runner 上跑绿一次（见 docs/dev/deploy-e2e.md）。
#
# 隔离：独立容器名/网络/卷（前缀由 DEPLOY_E2E_PREFIX 决定），
#   不碰本机既有容器（如 test47 上的 yiban-go-test）。
# ============================================================
set -Eeuo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SELF_DIR/../.." && pwd)"
# shellcheck source=lib/deploy-e2e.sh
. "$SELF_DIR/lib/deploy-e2e.sh"

PREFIX="${DEPLOY_E2E_PREFIX:-yiban-e2e-baremetal}"
OS_IMAGE="${DEPLOY_E2E_OS_IMAGE:-yiban-e2e-os:ubuntu24.04}"
OS_BASE="${DEPLOY_E2E_OS_BASE:-ubuntu:24.04}"
PG_IMAGE="${DEPLOY_E2E_PG_IMAGE:-postgres:18}"
GO_IMAGE="${DEPLOY_E2E_GO_IMAGE:-golang:1.27-alpine}"
GOPROXY_ARG="${DEPLOY_E2E_GOPROXY:-}"
SERVER_BIN_SRC="${DEPLOY_E2E_SERVER_BIN:-}"
CLI_BIN_SRC="${DEPLOY_E2E_CLI_BIN:-}"
LOG_DIR="${DEPLOY_E2E_LOG_DIR:-$REPO/work/deploy-e2e-baremetal}"
KEEP=0

NET="${PREFIX}-net"
PG="${PREFIX}-pg"
OS="${PREFIX}-os"
VOL="${PREFIX}-pgdata"
BIN_OUT=""
HEALTH_ADDR="127.0.0.1:47183"
PG_PASSWORD=""
DSN=""
STARTED=0

while [ $# -gt 0 ]; do
    case "$1" in
        --keep) KEEP=1; shift ;;
        --log-dir) LOG_DIR="${2:?--log-dir 需要目录}"; shift 2 ;;
        --log-dir=*) LOG_DIR="${1#*=}"; shift ;;
        -h|--help) sed -n '2,56p' "$0"; exit 0 ;;
        *) echo "未知参数：$1" >&2; exit 2 ;;
    esac
done

e2e_init "baremetal" "$LOG_DIR"

cleanup() {
    local rc=$?
    # 先摘掉 ERR/EXIT，防清理命令的失败再次触发 trap（会造成重复汇总）。
    trap - ERR EXIT
    e2e_exit_note "$rc"
    if [ "$STARTED" -eq 1 ]; then
        if [ "$KEEP" -eq 1 ]; then
            e2e_say "(--keep) 保留容器 $OS、$PG，网络 $NET，卷 $VOL"
            e2e_say "  手动清理：docker rm -f $OS $PG; docker network rm $NET; docker volume rm $VOL"
        else
            docker rm -f "$OS" >/dev/null 2>&1 || true
            docker rm -f "$PG" >/dev/null 2>&1 || true
            docker network rm "$NET" >/dev/null 2>&1 || true
            docker volume rm "$VOL" >/dev/null 2>&1 || true
        fi
    fi
    exit "$rc"
}
trap cleanup EXIT

wait_systemd() {
    local limit="$1"
    local end=$((SECONDS + limit))
    local st
    while [ "$SECONDS" -lt "$end" ]; do
        st="$(docker exec "$OS" systemctl is-system-running 2>/dev/null || true)"
        case "$st" in
            running|degraded) echo "$st"; return 0 ;;
        esac
        sleep 1
    done
    return 1
}

wait_pg() {
    local limit="$1"
    local end=$((SECONDS + limit))
    while [ "$SECONDS" -lt "$end" ]; do
        if docker exec "$PG" pg_isready -U yiban -d yiban >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done
    return 1
}

os_exec() {
    docker exec "$OS" "$@"
}

# ---- B1 前置与干净环境 -----------------------------------------------------
e2e_step "B1" "前置与干净环境"
e2e_require docker
if ! docker info >"$E2E_STEP_LOG" 2>&1; then
    e2e_die "docker daemon 不可达（docker info 失败）"
fi
# 只清本前缀的资源；其他项目（含 test47 上的既有容器）不碰。
if docker inspect "$OS" >/dev/null 2>&1 || docker inspect "$PG" >/dev/null 2>&1; then
    e2e_say "  [WARN] 发现上次残留（前缀 $PREFIX），先清理"
fi
docker rm -f "$OS" >/dev/null 2>&1 || true
docker rm -f "$PG" >/dev/null 2>&1 || true
docker network rm "$NET" >/dev/null 2>&1 || true
docker volume rm "$VOL" >/dev/null 2>&1 || true
if docker inspect "$OS" >/dev/null 2>&1 || docker inspect "$PG" >/dev/null 2>&1; then
    e2e_die "清理后仍存在同名容器"
fi
STARTED=1
BIN_OUT="$LOG_DIR/bin"
rm -rf "$BIN_OUT"
mkdir -p "$BIN_OUT"
docker version --format '{{.Server.Version}}' >>"$E2E_STEP_LOG" 2>&1 || true
git -C "$REPO" rev-parse HEAD >>"$E2E_STEP_LOG" 2>&1 || true
e2e_ok "前缀 $PREFIX，资源名未占用"

# ---- B2 OS 镜像（systemd + openssl） --------------------------------------
e2e_step "B2" "OS 镜像（systemd + openssl）"
if docker image inspect "$OS_IMAGE" >/dev/null 2>&1; then
    e2e_ok "复用镜像 $OS_IMAGE"
else
    cat > "$LOG_DIR/Dockerfile.os" <<EOF
FROM $OS_BASE
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \\
 && apt-get install -y --no-install-recommends systemd systemd-sysv openssl ca-certificates \\
 && rm -rf /var/lib/apt/lists/*
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
EOF
    if ! docker build -f "$LOG_DIR/Dockerfile.os" -t "$OS_IMAGE" "$LOG_DIR" \
        >>"$E2E_STEP_LOG" 2>&1; then
        e2e_die "构建 OS 镜像失败（需要访问 apt 源；见 B2.log）"
    fi
    e2e_ok "构建 $OS_IMAGE（基 $OS_BASE）"
fi
if ! os_image_ok="$(docker run --rm "$OS_IMAGE" sh -c 'test -x /sbin/init && test -x /usr/bin/openssl' 2>>"$E2E_STEP_LOG")"; then
    e2e_die "OS 镜像缺 /sbin/init 或 openssl"
fi

# ---- B3 干净 PostgreSQL 18 -------------------------------------------------
e2e_step "B3" "干净 PostgreSQL 18"
if ! docker network create "$NET" >>"$E2E_STEP_LOG" 2>&1; then
    e2e_die "创建网络 $NET 失败"
fi
PG_PASSWORD="$(e2e_secret_hex 16)"
DSN="postgres://yiban:${PG_PASSWORD}@${PG}:5432/yiban?sslmode=disable"
if ! docker volume create "$VOL" >>"$E2E_STEP_LOG" 2>&1; then
    e2e_die "创建卷 $VOL 失败"
fi
if ! docker run -d --name "$PG" --network "$NET" \
    -e POSTGRES_DB=yiban -e POSTGRES_USER=yiban -e POSTGRES_PASSWORD="$PG_PASSWORD" \
    -v "$VOL:/var/lib/postgresql" "$PG_IMAGE" >>"$E2E_STEP_LOG" 2>&1; then
    e2e_die "启动 PostgreSQL 容器失败"
fi
if ! wait_pg 120; then
    docker logs --tail 60 "$PG" >>"$E2E_STEP_LOG" 2>&1 || true
    e2e_die "PostgreSQL 未在 120s 内就绪"
fi
if ! TABLES="$(docker exec "$PG" psql -U yiban -d yiban -tAc \
    "select count(*) from information_schema.tables where table_schema='public'" 2>>"$E2E_STEP_LOG" | tr -d '[:space:]')"; then
    e2e_die "查询干净库表数失败"
fi
if [ "$TABLES" != "0" ]; then
    e2e_die "干净库已有 $TABLES 张表（期望 0）"
fi
e2e_ok "卷 $VOL 新建，public 表数=0"

# ---- B4 构建二进制 ---------------------------------------------------------
e2e_step "B4" "构建二进制"
if [ -n "$SERVER_BIN_SRC" ] || [ -n "$CLI_BIN_SRC" ]; then
    [ -n "$SERVER_BIN_SRC" ] && [ -n "$CLI_BIN_SRC" ] \
        || e2e_die "DEPLOY_E2E_SERVER_BIN 与 DEPLOY_E2E_CLI_BIN 必须同时给"
    cp "$SERVER_BIN_SRC" "$BIN_OUT/yiban-server"
    cp "$CLI_BIN_SRC" "$BIN_OUT/yiban-cli"
    e2e_ok "复用外部二进制（排障模式）"
else
    GO_ENV=(-e CGO_ENABLED=0 -e GOCACHE=/tmp/gocache -e GOPATH=/tmp/gopath)
    if [ -n "$GOPROXY_ARG" ]; then
        GO_ENV+=(-e "GOPROXY=$GOPROXY_ARG")
    fi
    if ! docker run --rm \
        -v "$REPO/server:/src:ro" -v "$BIN_OUT:/out" -w /src \
        "${GO_ENV[@]}" \
        "$GO_IMAGE" sh -c \
        'go build -trimpath -ldflags="-s -w" -o /out/yiban-server ./cmd/server \
         && go build -trimpath -ldflags="-s -w" -o /out/yiban-cli ./cmd/cli' \
        >>"$E2E_STEP_LOG" 2>&1; then
        e2e_die "Go 构建失败（受限网络请设 DEPLOY_E2E_GOPROXY）"
    fi
    e2e_ok "用 $GO_IMAGE 编译 server 与 cli"
fi
for b in yiban-server yiban-cli; do
    [ -x "$BIN_OUT/$b" ] || e2e_die "$b 未产出或不可执行"
done

# ---- B5 一次性 OS 容器 -----------------------------------------------------
e2e_step "B5" "一次性 OS 容器（真 systemd）"
if ! docker run -d --name "$OS" --network "$NET" --hostname e2e-baremetal \
    --privileged --cgroupns=host \
    -v /sys/fs/cgroup:/sys/fs/cgroup:rw --tmpfs /run --tmpfs /run/lock \
    "$OS_IMAGE" >>"$E2E_STEP_LOG" 2>&1; then
    e2e_die "启动 OS 容器失败"
fi
if ! SYSTEMD_STATE="$(wait_systemd 60)"; then
    docker logs --tail 60 "$OS" >>"$E2E_STEP_LOG" 2>&1 || true
    e2e_die "systemd 未在 60s 内就绪"
fi
# 干净环境：安装前这几处必须不存在，否则「从零装成」是空话。
os_exec sh -c 'test ! -e /etc/yiban' >>"$E2E_STEP_LOG" 2>&1 \
    || e2e_die "OS 容器里已有 /etc/yiban（不是干净环境）"
os_exec sh -c '! id yiban >/dev/null 2>&1' >>"$E2E_STEP_LOG" 2>&1 \
    || e2e_die "OS 容器里已有 yiban 用户（不是干净环境）"
os_exec sh -c 'test ! -e /usr/local/bin/yiban-server' >>"$E2E_STEP_LOG" 2>&1 \
    || e2e_die "OS 容器里已有服务二进制（不是干净环境）"
e2e_ok "systemd=$SYSTEMD_STATE；/etc/yiban、yiban 用户、二进制均不存在"

# ---- B6 install.sh 真装 ----------------------------------------------------
e2e_step "B6" "install.sh 真装"
os_exec mkdir -p /opt/e2e-src /opt/e2e-dist >>"$E2E_STEP_LOG" 2>&1 \
    || e2e_die "创建 /opt/e2e-src 失败"
docker cp "$REPO/deploy" "$OS:/opt/e2e-src/deploy" >>"$E2E_STEP_LOG" 2>&1 \
    || e2e_die "复制 deploy/ 失败"
docker cp "$REPO/config" "$OS:/opt/e2e-src/config" >>"$E2E_STEP_LOG" 2>&1 \
    || e2e_die "复制 config/ 失败"
docker cp "$BIN_OUT/yiban-server" "$OS:/opt/e2e-dist/yiban-server" >>"$E2E_STEP_LOG" 2>&1 \
    || e2e_die "复制 server 二进制失败"
docker cp "$BIN_OUT/yiban-cli" "$OS:/opt/e2e-dist/yiban-cli" >>"$E2E_STEP_LOG" 2>&1 \
    || e2e_die "复制 cli 二进制失败"
# YIBAN_HEALTH_RETRIES 放大到 60：容器内首次启动要连库并跑迁移。
# 它只放大等待，不放松判据（仍然必须拿到 200）。
if ! docker exec -e YIBAN_HEALTH_RETRIES=60 -e YIBAN_HEALTH_RETRY_INTERVAL=1 \
    "$OS" bash /opt/e2e-src/deploy/linux/install.sh \
    --server-bin /opt/e2e-dist/yiban-server \
    --cli-bin /opt/e2e-dist/yiban-cli \
    --database-url "$DSN" >>"$E2E_STEP_LOG" 2>&1; then
    os_exec journalctl -u yiban.service --no-pager >>"$E2E_STEP_LOG" 2>&1 || true
    e2e_die "install.sh 退出码非 0"
fi
if ! grep -q 'install complete: healthz ok' "$E2E_STEP_LOG"; then
    e2e_die "install.sh 未报告 install complete: healthz ok"
fi
e2e_ok "install.sh 退出码 0，healthz ok"

# ---- B7 服务状态 -----------------------------------------------------------
e2e_step "B7" "服务状态"
if ! ACTIVE="$(os_exec systemctl is-active yiban.service 2>>"$E2E_STEP_LOG")"; then
    e2e_die "yiban.service 不是 active（$ACTIVE）"
fi
if [ "$ACTIVE" != "active" ]; then
    e2e_die "yiban.service 状态=$ACTIVE（期望 active）"
fi
if ! ENABLED="$(os_exec systemctl is-enabled yiban.service 2>>"$E2E_STEP_LOG")"; then
    e2e_die "yiban.service 未 enable"
fi
if [ "$ENABLED" != "enabled" ]; then
    e2e_die "yiban.service is-enabled=$ENABLED（期望 enabled）"
fi
# 独立探活：不复用 install.sh 的重试循环结论。
if ! os_exec /usr/local/bin/yiban-cli healthcheck --addr "$HEALTH_ADDR" --timeout 2s \
    >>"$E2E_STEP_LOG" 2>&1; then
    e2e_die "独立 healthcheck 失败（$HEALTH_ADDR）"
fi
e2e_ok "active + enabled；独立 healthcheck 通过"

# ---- B8 落位与权限 ---------------------------------------------------------
e2e_step "B8" "落位与权限"
ENV_META="$(os_exec stat -c '%a:%U:%G' /etc/yiban/.env 2>>"$E2E_STEP_LOG" || true)"
[ "$ENV_META" = "600:root:root" ] || e2e_die "/etc/yiban/.env 权限=$ENV_META（期望 600:root:root）"
for b in yiban-server yiban-cli; do
    META="$(os_exec stat -c '%a' "/usr/local/bin/$b" 2>>"$E2E_STEP_LOG" || true)"
    [ "$META" = "755" ] || e2e_die "/usr/local/bin/$b 权限=$META（期望 755）"
done
os_exec test -f /etc/systemd/system/yiban.service >>"$E2E_STEP_LOG" 2>&1 \
    || e2e_die "缺 /etc/systemd/system/yiban.service"
os_exec id -u yiban >>"$E2E_STEP_LOG" 2>&1 || e2e_die "服务用户 yiban 不存在"
PHOTO_META="$(os_exec stat -c '%U:%G' /var/lib/yiban/photos 2>>"$E2E_STEP_LOG" || true)"
[ "$PHOTO_META" = "yiban:yiban" ] || e2e_die "photos 目录属主=$PHOTO_META（期望 yiban:yiban）"
os_exec grep -qE '^YIBAN_HASH_PEPPER=[0-9a-f]{64}$' /etc/yiban/.env >>"$E2E_STEP_LOG" 2>&1 \
    || e2e_die ".env 的 YIBAN_HASH_PEPPER 不是 64 位十六进制"
KEY_BYTES="$(os_exec sh -c "grep -E '^YIBAN_CREDENTIAL_KEY=' /etc/yiban/.env | cut -d= -f2- | base64 -d | wc -c" 2>>"$E2E_STEP_LOG" | tr -d '[:space:]' || true)"
[ "$KEY_BYTES" = "32" ] || e2e_die "YIBAN_CREDENTIAL_KEY 解码后 $KEY_BYTES 字节（期望 32）"
os_exec grep -Fxq "YIBAN_DATABASE_URL=$DSN" /etc/yiban/.env >>"$E2E_STEP_LOG" 2>&1 \
    || e2e_die ".env 的 YIBAN_DATABASE_URL 与传入值不一致"
HOST_REG_SUM="$(e2e_sha256 "$REPO/config/registry.json" || true)"
GUEST_REG_SUM="$(os_exec sha256sum /etc/yiban/registry.json 2>>"$E2E_STEP_LOG" | cut -d' ' -f1 || true)"
[ -n "$HOST_REG_SUM" ] && [ "$HOST_REG_SUM" = "$GUEST_REG_SUM" ] \
    || e2e_die "安装的 registry.json 与 config/registry.json 不一致"
e2e_ok ".env 600 root:root；二进制 755；photos 属 yiban；名册逐字节一致"

# ---- B9 迁移版本与干净库 ---------------------------------------------------
e2e_step "B9" "迁移版本与干净库"
EXPECTED_SCHEMA="$(e2e_expected_schema_version "$REPO")"
if [ "$EXPECTED_SCHEMA" -le 0 ]; then
    e2e_die "server/migrations/postgres 里没有迁移文件"
fi
if ! os_exec bash -c 'set -a; . /etc/yiban/.env; set +a; /usr/local/bin/yiban-cli migrate' \
    >"$LOG_DIR/B9-migrate.log" 2>&1; then
    cat "$LOG_DIR/B9-migrate.log" >>"$E2E_STEP_LOG" 2>/dev/null || true
    e2e_die "已装 yiban-cli migrate 失败"
fi
if ! grep -q "schema version $EXPECTED_SCHEMA" "$LOG_DIR/B9-migrate.log"; then
    cat "$LOG_DIR/B9-migrate.log" >>"$E2E_STEP_LOG" 2>/dev/null || true
    e2e_die "迁移版本不是 $EXPECTED_SCHEMA"
fi
if ! USERS="$(docker exec "$PG" psql -U yiban -d yiban -tAc 'select count(*) from users' 2>>"$E2E_STEP_LOG" | tr -d '[:space:]')"; then
    e2e_die "users 计数查询失败"
fi
[ "$USERS" = "0" ] || e2e_die "干净库 users=$USERS（期望 0）"
e2e_ok "schema version $EXPECTED_SCHEMA；users=0"

# ---- B10 SIGTERM 优雅停机 --------------------------------------------------
e2e_step "B10" "SIGTERM 优雅停机"
if ! os_exec systemctl stop yiban.service >>"$E2E_STEP_LOG" 2>&1; then
    e2e_die "systemctl stop yiban.service 失败"
fi
MAIN_STATUS="$(os_exec systemctl show -p ExecMainStatus yiban.service 2>>"$E2E_STEP_LOG" || true)"
RESULT="$(os_exec systemctl show -p Result yiban.service 2>>"$E2E_STEP_LOG" || true)"
STATE_AFTER="$(os_exec systemctl is-active yiban.service 2>/dev/null || true)"
printf '%s\n%s\nis-active=%s\n' "$MAIN_STATUS" "$RESULT" "$STATE_AFTER" >>"$E2E_STEP_LOG"
[ "$MAIN_STATUS" = "ExecMainStatus=0" ] \
    || e2e_die "停机后 $MAIN_STATUS（期望 ExecMainStatus=0；137 表示被 SIGKILL）"
[ "$RESULT" = "Result=success" ] || e2e_die "停机后 $RESULT（期望 Result=success）"
[ "$STATE_AFTER" = "inactive" ] || e2e_die "停机后状态=$STATE_AFTER（期望 inactive）"
if os_exec /usr/local/bin/yiban-cli healthcheck --addr "$HEALTH_ADDR" --timeout 2s \
    >>"$E2E_STEP_LOG" 2>&1; then
    e2e_die "停机后 healthcheck 仍通过（说明先前的通过不具判别力）"
fi
e2e_ok "ExecMainStatus=0，Result=success，healthcheck 已失败"

# ---- B11 资源清理 ----------------------------------------------------------
e2e_step "B11" "资源清理"
if [ "$KEEP" -eq 1 ]; then
    e2e_ok "(--keep) 保留容器与卷"
else
    docker rm -f "$OS" >>"$E2E_STEP_LOG" 2>&1 || e2e_die "删除 OS 容器失败"
    docker rm -f "$PG" >>"$E2E_STEP_LOG" 2>&1 || e2e_die "删除 PG 容器失败"
    docker network rm "$NET" >>"$E2E_STEP_LOG" 2>&1 || e2e_die "删除网络失败"
    docker volume rm "$VOL" >>"$E2E_STEP_LOG" 2>&1 || e2e_die "删除卷失败"
    if docker inspect "$OS" >/dev/null 2>&1 || docker inspect "$PG" >/dev/null 2>&1; then
        e2e_die "清理后仍有同名容器"
    fi
    e2e_ok "容器/网络/卷均已删除"
fi

e2e_say ""
e2e_say "MANUAL（发布前手跑项）：本脚本不在自动轨。加入 CI 前须在托管 runner 上跑绿一次。"
e2e_say "等价性登记：真 systemd + 真 PostgreSQL + 真 useradd/chown；不等价的一维是没有真实内核启动与硬件。"
e2e_say "日志目录：$LOG_DIR"
if e2e_summary; then
    exit 0
fi
exit 1
