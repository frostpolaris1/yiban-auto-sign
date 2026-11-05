#!/usr/bin/env bash
# ============================================================
# scripts/e2e/deploy-baremetal-e2e.sh — 裸机部署路径的干净环境 e2e（工单 6jk）。
#
# 模拟的真实用户动作（AGENTS §8g）：
#   一台干净的 Ubuntu 24.04 主机按 deploy/baremetal/install.sh 的用法，
#   落位二进制与前端产物、生成 /etc/yiban/server.env、装 systemd 单元、
#   enable --now，然后访问首页与 /healthz、再停机。
#
# 为什么容器内仍是「真装」（AGENTS §32）：
#   被测性质逐条列出：install.sh 的落位/属主/权限、systemd 单元启动、
#   服务连通真 PostgreSQL 18、/healthz 应答、页面与深链可达、SIGTERM 后退出码 0。
#   本脚本用一次性 Ubuntu 24.04 容器 + 真 systemd（PID1）+ 真
#   useradd/chown/systemctl + 真 PostgreSQL 18 覆盖上述全部性质。
#   前端产物由 node 容器真跑 npm ci + npm run build，不在宿主装依赖。
#   不等价的一维：容器没有真实内核启动与硬件；network-online.target
#   由 systemd 直接满足。该维不影响被测性质，摘要里显式打印。
#   不使用假 systemctl、不使用 YIBAN_INSTALL_ROOT 干跑前缀。
#
# 步骤：
#   B1 前置与干净环境      B2 OS 镜像（systemd + dbus + openssl + curl）
#   B3 干净 PostgreSQL 18   B4 构建二进制（golang 容器，真编译）
#   B4c 构建前端产物（node 容器，真 npm run build）
#   B5 一次性 OS 容器       B6 install.sh 真装（含 --ui-dir）
#   B6b 单元加固            B6c 照片目录白名单负向
#   B6d 重装重启            B7 服务状态
#   B7d 加固沙箱写照片      B7b 页面与深链
#   B7c 反代信任链          B8 落位与权限
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
#   DEPLOY_E2E_NODE_IMAGE   前端构建镜像（默认 node:22-alpine）
#   DEPLOY_E2E_GOPROXY      Go 模块代理（受限网络用，如 https://goproxy.cn,direct）
#   DEPLOY_E2E_NPM_REGISTRY npm 源（受限网络用，如 https://registry.npmmirror.com）
#   DEPLOY_E2E_SERVER_BIN   复用已构建的服务二进制（排障用，跳过 B4 编译）
#   DEPLOY_E2E_CLI_BIN      同上，CLI 二进制
#   DEPLOY_E2E_UI_DIST      复用已构建的前端产物（排障用，跳过 B4c）
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
NODE_IMAGE="${DEPLOY_E2E_NODE_IMAGE:-node:22-alpine}"
GOPROXY_ARG="${DEPLOY_E2E_GOPROXY:-}"
NPM_REGISTRY_ARG="${DEPLOY_E2E_NPM_REGISTRY:-}"
SERVER_BIN_SRC="${DEPLOY_E2E_SERVER_BIN:-}"
CLI_BIN_SRC="${DEPLOY_E2E_CLI_BIN:-}"
UI_DIST_SRC="${DEPLOY_E2E_UI_DIST:-}"
LOG_DIR="${DEPLOY_E2E_LOG_DIR:-$REPO/work/deploy-e2e-baremetal}"
KEEP=0

NET="${PREFIX}-net"
PG="${PREFIX}-pg"
OS="${PREFIX}-os"
VOL="${PREFIX}-pgdata"
BIN_OUT=""
UI_OUT=""
HEALTH_ADDR="127.0.0.1:47183"
PG_PASSWORD=""
DSN=""
STARTED=0

while [ $# -gt 0 ]; do
    case "$1" in
        --keep) KEEP=1; shift ;;
        --log-dir) LOG_DIR="${2:?--log-dir 需要目录}"; shift 2 ;;
        --log-dir=*) LOG_DIR="${1#*=}"; shift ;;
        -h|--help) sed -n '2,55p' "$0"; exit 0 ;;
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

# ---- B2 OS 镜像（systemd + openssl + curl） -------------------------------
e2e_step "B2" "OS 镜像（systemd + openssl + curl）"
# 复用镜像也要验收工具齐备：旧缓存可能没有 curl（CLEAN-0 起页面断言要用）。
os_image_ready() {
    docker image inspect "$OS_IMAGE" >/dev/null 2>&1 \
        && docker run --rm "$OS_IMAGE" sh -c \
            'test -x /sbin/init && test -x /usr/bin/openssl && test -x /usr/bin/curl && test -x /usr/bin/dbus-daemon' \
            >/dev/null 2>&1
}
if os_image_ready; then
    e2e_ok "复用镜像 $OS_IMAGE"
else
    cat > "$LOG_DIR/Dockerfile.os" <<EOF
FROM $OS_BASE
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \\
 && apt-get install -y --no-install-recommends systemd systemd-sysv dbus openssl ca-certificates curl \\
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
if ! docker run --rm "$OS_IMAGE" sh -c 'test -x /sbin/init && test -x /usr/bin/openssl && test -x /usr/bin/curl' >>"$E2E_STEP_LOG" 2>&1; then
    e2e_die "OS 镜像缺 /sbin/init、openssl 或 curl"
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

# ---- B4c 构建前端产物（真 npm run build） ---------------------------------
e2e_step "B4c" "构建前端产物"
if [ -n "$UI_DIST_SRC" ]; then
    [ -f "$UI_DIST_SRC/index.html" ] \
        || e2e_die "DEPLOY_E2E_UI_DIST 里没有 index.html: $UI_DIST_SRC"
    UI_OUT="$(mktemp -d "$LOG_DIR/ui-dist.XXXXXX")" || e2e_die "创建 UI 输出目录失败"
    cp -R "$UI_DIST_SRC/." "$UI_OUT/"
    e2e_ok "复用外部前端产物（排障模式）"
else
    UI_OUT="$(mktemp -d "$LOG_DIR/ui-dist.XXXXXX")" || e2e_die "创建 UI 输出目录失败"
    NPM_ENV=()
    if [ -n "$NPM_REGISTRY_ARG" ]; then
        NPM_ENV=(-e "NPM_CONFIG_REGISTRY=$NPM_REGISTRY_ARG")
    fi
    # 源码经 stdin 送进容器：不把宿主 ui/node_modules 绑进去。
    # npm ci 会先删 node_modules；绑定宿主目录等于删宿主的依赖。
    if ! tar -C "$REPO/ui" \
        --exclude=./node_modules \
        --exclude=./dist \
        --exclude=./dist-showcase \
        --exclude=./test-results \
        --exclude=./playwright-report \
        --exclude=./scripts/api-codegen/node_modules \
        -cf - . \
        | docker run --rm -i -v "$UI_OUT:/out" -w /src \
            "${NPM_ENV[@]}" \
            "$NODE_IMAGE" sh -c \
            'tar -xf - && npm ci --no-audit --no-fund && npm run build && cp -a dist/. /out/' \
            >>"$E2E_STEP_LOG" 2>&1; then
        e2e_die "前端构建失败（受限网络请设 DEPLOY_E2E_NPM_REGISTRY）"
    fi
    e2e_ok "用 $NODE_IMAGE 构建前端产物"
fi
[ -f "$UI_OUT/index.html" ] || e2e_die "前端产物缺 index.html"
for f in "$UI_OUT"/assets/*.js; do
    [ -f "$f" ] || e2e_die "前端产物缺 assets/*.js"
    break
done

# ---- B4b 签名发布物（真二进制签发 → 验签） ---------------------------------
e2e_step "B4b" "签名发布物"
SIGN_DIR="$BIN_OUT/signing"
mkdir -p "$SIGN_DIR"
bash "$REPO/scripts/release/make-manifest.sh" "e2e-B4b"     "$BIN_OUT/yiban-server" "$BIN_OUT/yiban-cli" > "$SIGN_DIR/manifest.json"
openssl genpkey -algorithm ED25519 -out "$SIGN_DIR/key.pem" >>"$E2E_STEP_LOG" 2>&1     || e2e_die "openssl 生成 Ed25519 私钥失败"
openssl pkey -in "$SIGN_DIR/key.pem" -pubout -out "$SIGN_DIR/pub.pem" >>"$E2E_STEP_LOG" 2>&1     || e2e_die "openssl 导出公钥失败"
bash "$REPO/scripts/release/sign.sh" --key "$SIGN_DIR/key.pem"     --in "$SIGN_DIR/manifest.json" --out "$SIGN_DIR/manifest.sig" >>"$E2E_STEP_LOG" 2>&1     || e2e_die "sign.sh 签名失败"
if ! bash "$REPO/scripts/release/verify.sh" --pubkey "$SIGN_DIR/pub.pem"     --manifest "$SIGN_DIR/manifest.json" --signature "$SIGN_DIR/manifest.sig" >>"$E2E_STEP_LOG" 2>&1; then
    e2e_die "正路径验签失败"
fi
e2e_ok "真二进制清单签发与验签通过"

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
docker cp "$UI_OUT" "$OS:/opt/e2e-dist/ui" >>"$E2E_STEP_LOG" 2>&1 \
    || e2e_die "复制前端产物失败"
# YIBAN_HEALTH_RETRIES 放大到 60：容器内首次启动要连库并跑迁移。
# 它只放大等待，不放松判据（仍然必须拿到 200）。
if ! docker exec -e YIBAN_HEALTH_RETRIES=60 -e YIBAN_HEALTH_RETRY_INTERVAL=1 \
    "$OS" bash /opt/e2e-src/deploy/baremetal/install.sh \
    --server-bin /opt/e2e-dist/yiban-server \
    --cli-bin /opt/e2e-dist/yiban-cli \
    --ui-dir /opt/e2e-dist/ui \
    --database-url "$DSN" >>"$E2E_STEP_LOG" 2>&1; then
    os_exec journalctl -u yiban.service --no-pager >>"$E2E_STEP_LOG" 2>&1 || true
    e2e_die "install.sh 退出码非 0"
fi
if ! grep -q 'install complete: healthz ok' "$E2E_STEP_LOG"; then
    e2e_die "install.sh 未报告 install complete: healthz ok"
fi
e2e_ok "install.sh 退出码 0，healthz ok"

# ---- B6b systemd 单元加固 --------------------------------------------------
# 加固键必须真进单元，并被 systemd 接受（不是只写在安装器注释里）。
e2e_step "B6b" "systemd 单元加固"
if ! os_exec systemd-analyze verify /etc/systemd/system/yiban.service \
    >>"$E2E_STEP_LOG" 2>&1; then
    e2e_die "systemd-analyze verify /etc/systemd/system/yiban.service 失败"
fi
UNIT_TEXT="$(os_exec cat /etc/systemd/system/yiban.service 2>>"$E2E_STEP_LOG" || true)"
for key in \
    'NoNewPrivileges=true' \
    'PrivateTmp=true' \
    'UMask=0077' \
    'ProtectSystem=strict' \
    'ProtectHome=true' \
    'ReadWritePaths=-/var/lib/yiban/photos'; do
    printf '%s\n' "$UNIT_TEXT" | grep -Fxq "$key" \
        || e2e_die "systemd 单元缺 $key"
done
# systemd 载入后的属性值：文件里写对但被忽略（拼错键名）也会在这里红。
for prop in \
    'NoNewPrivileges=yes' \
    'PrivateTmp=yes' \
    'ProtectSystem=strict' \
    'ProtectHome=yes' \
    'UMask=0077'; do
    if ! SHOW="$(os_exec systemctl show -p "${prop%%=*}" yiban.service 2>>"$E2E_STEP_LOG")"; then
        e2e_die "systemctl show -p ${prop%%=*} 失败"
    fi
    printf '%s\n' "$SHOW" | grep -Fxq "$prop" \
        || e2e_die "systemd 未接受加固键 $prop"
done
# ReadWritePaths 是路径列表（不是布尔），单独查：漏配会让首次上传才失败。
SHOW_RW="$(os_exec systemctl show -p ReadWritePaths yiban.service 2>>"$E2E_STEP_LOG" || true)"
printf '%s\n' "$SHOW_RW" | grep -Fq '/var/lib/yiban/photos' \
    || e2e_die "systemd 的 ReadWritePaths 未包含照片目录"
e2e_ok "systemd-analyze verify 通过；加固键全部被 systemd 接受"

# ---- B6c 照片目录白名单负向（真 root） -------------------------------------
# 坏 YIBAN_PHOTO_DIR 必须在任何写入/chown 前退 2；判据是属主与单元字节不变。
# 所有可能失败的命令都放在 if 条件里：os_exec 内部失败会触发 ERR trap，
# `cmd || handler` 不足以抑制（set -E 会让 trap 进入函数体）。
e2e_step "B6c" "照片目录白名单负向"
if ! UNIT_SUM_BEFORE="$(os_exec sha256sum /etc/systemd/system/yiban.service)"; then
    e2e_die "读取 unit 校验和失败"
fi
UNIT_SUM_BEFORE="${UNIT_SUM_BEFORE%% *}"

run_bad_photo() {
    local value="$1" label="$2" probe="$3" symlink="${4:-0}"
    local owner_before="" owner_after="" unit_after="" pid_before="" pid_after="" rc=0
    if ! os_exec cp -a /etc/yiban/server.env /tmp/e2e-server.env.bak >>"$E2E_STEP_LOG" 2>&1; then
        e2e_die "$label: 备份 server.env 失败"
    fi
    if [ "$symlink" = "1" ]; then
        if ! os_exec ln -sfn /etc /var/lib/yiban/photos-probe >>"$E2E_STEP_LOG" 2>&1; then
            e2e_die "$label: 造软链失败"
        fi
    fi
    if ! os_exec sed -i "s|^YIBAN_PHOTO_DIR=.*|YIBAN_PHOTO_DIR=$value|" /etc/yiban/server.env \
        >>"$E2E_STEP_LOG" 2>&1; then
        e2e_die "$label: 改 server.env 失败"
    fi
    if ! owner_before="$(os_exec stat -c '%U:%G' "$probe")"; then
        e2e_die "$label: 读 $probe 属主失败"
    fi
    if ! pid_before="$(os_exec systemctl show -p MainPID --value yiban.service)"; then
        e2e_die "$label: 读 MainPID 失败"
    fi
    if os_exec env YIBAN_HEALTH_RETRIES=1 bash /opt/e2e-src/deploy/baremetal/install.sh \
        --server-bin /opt/e2e-dist/yiban-server \
        --cli-bin /opt/e2e-dist/yiban-cli \
        --database-url "$DSN" >>"$E2E_STEP_LOG" 2>&1; then
        rc=0
    else
        rc=$?
    fi
    [ "$rc" = "2" ] || e2e_die "$label: rc=$rc（期望 2）"
    if ! owner_after="$(os_exec stat -c '%U:%G' "$probe")"; then
        e2e_die "$label: 读回 $probe 属主失败"
    fi
    [ "$owner_before" = "$owner_after" ] \
        || e2e_die "$label: chown 越界（$probe $owner_before -> $owner_after）"
    if ! unit_after="$(os_exec sha256sum /etc/systemd/system/yiban.service)"; then
        e2e_die "$label: 读回 unit 校验和失败"
    fi
    unit_after="${unit_after%% *}"
    [ "$UNIT_SUM_BEFORE" = "$unit_after" ] || e2e_die "$label: 失败路径改写了 unit"
    if ! pid_after="$(os_exec systemctl show -p MainPID --value yiban.service)"; then
        e2e_die "$label: 读回 MainPID 失败"
    fi
    [ "$pid_before" = "$pid_after" ] || e2e_die "$label: 失败路径重启了服务"
    if ! os_exec mv /tmp/e2e-server.env.bak /etc/yiban/server.env >>"$E2E_STEP_LOG" 2>&1; then
        e2e_die "$label: 恢复 server.env 失败"
    fi
    if ! os_exec chmod 600 /etc/yiban/server.env >>"$E2E_STEP_LOG" 2>&1; then
        e2e_die "$label: 恢复 server.env 权限失败"
    fi
    if ! os_exec chown root:root /etc/yiban/server.env >>"$E2E_STEP_LOG" 2>&1; then
        e2e_die "$label: 恢复 server.env 属主失败"
    fi
    if [ "$symlink" = "1" ]; then
        if ! os_exec rm -f /var/lib/yiban/photos-probe >>"$E2E_STEP_LOG" 2>&1; then
            e2e_die "$label: 删除软链失败"
        fi
    fi
    e2e_say "  [PASS] $label：rc=2，属主/unit/MainPID 未变"
}
run_bad_photo "/var/lib/yiban/photos/../.." "dotdot-escape" "/var/lib"
run_bad_photo "/etc" "etc" "/etc"
run_bad_photo "/var/lib/yiban-updater" "updater-root" "/var/lib/yiban-updater"
run_bad_photo "/var/lib/yiban/photos-probe" "symlink-to-etc" "/etc" 1
e2e_ok "dotdot/裸目录/updater/软链全部退 2；无 chown、无 unit 改写、无重启"

# ---- B6d 重装重启 active unit ----------------------------------------------
# enable --now 对 active 单元是 no-op；restart 必须换 MainPID，否则加固不生效。
e2e_step "B6d" "重装重启 active unit"
PID_BEFORE="$(os_exec systemctl show -p MainPID --value yiban.service 2>>"$E2E_STEP_LOG" || true)"
if ! os_exec env YIBAN_HEALTH_RETRIES=60 bash /opt/e2e-src/deploy/baremetal/install.sh \
    --server-bin /opt/e2e-dist/yiban-server \
    --cli-bin /opt/e2e-dist/yiban-cli \
    --ui-dir /opt/e2e-dist/ui \
    --database-url "$DSN" >>"$E2E_STEP_LOG" 2>&1; then
    e2e_die "重装 install.sh 失败"
fi
sleep 1
PID_AFTER="$(os_exec systemctl show -p MainPID --value yiban.service 2>>"$E2E_STEP_LOG" || true)"
[ -n "$PID_AFTER" ] && [ "$PID_AFTER" != "0" ] || e2e_die "重装后 MainPID=$PID_AFTER"
[ "$PID_AFTER" != "$PID_BEFORE" ] \
    || e2e_die "重装未重启服务（MainPID 仍为 $PID_BEFORE）"
if ! os_exec /usr/local/bin/yiban-cli healthcheck --addr "$HEALTH_ADDR" --timeout 2s \
    >>"$E2E_STEP_LOG" 2>&1; then
    e2e_die "重装后 healthcheck 失败"
fi
e2e_ok "重装 rc=0；MainPID $PID_BEFORE -> $PID_AFTER；healthz ok"

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

# ---- B7d 加固沙箱真实写照片 ------------------------------------------------
# 用与单元相同的隔离属性起一次性 systemd 单元：服务用户必须能写照片目录；
# 去掉 ReadWritePaths 后同一写操作必须失败（证明该行不是装饰）。
e2e_step "B7d" "加固沙箱写照片"
# systemd-run 走 D-Bus；OS 镜像装 dbus 并显式起服务，避免“连不上 bus”误判。
if ! os_exec systemctl start dbus.service >>"$E2E_STEP_LOG" 2>&1; then
    e2e_die "启动 dbus.service 失败（systemd-run 需要 D-Bus）"
fi
if ! os_exec systemd-run --unit=yiban-photo-probe --wait --collect \
    -p User=yiban -p Group=yiban -p ProtectSystem=strict -p ProtectHome=true \
    -p PrivateTmp=true -p NoNewPrivileges=true \
    -p ReadWritePaths=-/var/lib/yiban/photos \
    /bin/sh -c 'printf probe > /var/lib/yiban/photos/e2e-photo-probe && chmod 600 /var/lib/yiban/photos/e2e-photo-probe' \
    >>"$E2E_STEP_LOG" 2>&1; then
    e2e_die "服务用户在加固沙箱内写照片失败（ReadWritePaths 漏配？）"
fi
PROBE_META="$(os_exec stat -c '%a:%U:%G' /var/lib/yiban/photos/e2e-photo-probe 2>>"$E2E_STEP_LOG" || true)"
[ "$PROBE_META" = "600:yiban:yiban" ] \
    || e2e_die "照片探针权限=$PROBE_META（期望 600:yiban:yiban）"
if os_exec systemd-run --unit=yiban-photo-deny --wait --collect \
    -p User=yiban -p Group=yiban -p ProtectSystem=strict -p ProtectHome=true \
    -p PrivateTmp=true -p NoNewPrivileges=true \
    /bin/sh -c 'printf denied > /var/lib/yiban/photos/e2e-photo-denied' \
    >>"$E2E_STEP_LOG" 2>&1; then
    e2e_die "无 ReadWritePaths 时仍能写照片（判据无牙）"
fi
os_exec test ! -e /var/lib/yiban/photos/e2e-photo-denied \
    >>"$E2E_STEP_LOG" 2>&1 || e2e_die "拒绝写入后仍留下探针文件"
os_exec rm -f /var/lib/yiban/photos/e2e-photo-probe >>"$E2E_STEP_LOG" 2>&1 || true
e2e_ok "服务用户可在加固沙箱写照片；去掉 ReadWritePaths 即拒绝"

# ---- B7b 页面与前端深链（CLEAN-0） ----------------------------------------
e2e_step "B7b" "页面与前端深链"
os_exec curl -fsS --max-time 5 "http://$HEALTH_ADDR/" -o /tmp/e2e-ui.html \
    >>"$E2E_STEP_LOG" 2>&1 || e2e_die "GET / 失败"
os_exec grep -q '<div id="root">' /tmp/e2e-ui.html >>"$E2E_STEP_LOG" 2>&1 \
    || e2e_die "GET / 不是前端首页"
os_exec curl -fsS --max-time 5 "http://$HEALTH_ADDR/accounts" -o /tmp/e2e-deep.html \
    >>"$E2E_STEP_LOG" 2>&1 || e2e_die "GET /accounts（前端深链）失败"
os_exec grep -q '<div id="root">' /tmp/e2e-deep.html >>"$E2E_STEP_LOG" 2>&1 \
    || e2e_die "GET /accounts 未回 index.html"
CODE="$(os_exec curl -sS --max-time 5 -o /tmp/e2e-api404.json -w '%{http_code}' \
    "http://$HEALTH_ADDR/api/definitely-not-a-route" 2>>"$E2E_STEP_LOG" || true)"
[ "$CODE" = "404" ] || e2e_die "未知 API HTTP $CODE（期望 404）"
os_exec grep -q '"code":"not_found"' /tmp/e2e-api404.json >>"$E2E_STEP_LOG" 2>&1 \
    || e2e_die "未知 API 未回 JSON 信封"
ASSET="$(os_exec sh -c 'grep -oE "/assets/[^\"]+\.js" /tmp/e2e-ui.html | head -n1' \
    2>>"$E2E_STEP_LOG" || true)"
[ -n "$ASSET" ] || e2e_die "index.html 里找不到 /assets/*.js"
os_exec curl -fsS --max-time 5 -o /dev/null -D /tmp/e2e-asset.headers \
    "http://$HEALTH_ADDR$ASSET" >>"$E2E_STEP_LOG" 2>&1 || e2e_die "GET $ASSET 失败"
os_exec grep -qi '^cache-control: public, max-age=31536000, immutable' \
    /tmp/e2e-asset.headers >>"$E2E_STEP_LOG" 2>&1 \
    || e2e_die "$ASSET 缺 immutable 长缓存"
e2e_ok "首页 200；/accounts 回首页；未知 API 为 JSON 404；资产长缓存"

# ---- B7c 反代信任链 --------------------------------------------------------
# install.sh 生成的 .env 必须让宿主反代的 X-Forwarded-Proto 生效。
# 判据：登录回 401（无效凭据），不得回 403（信任链断）。
e2e_step "B7c" "反代信任链"
LOGIN_CODE="$(os_exec curl -sS --max-time 5 -o /tmp/e2e-login.json -w '%{http_code}' \
    -X POST "http://$HEALTH_ADDR/api/auth/login" \
    -H 'Content-Type: application/json' \
    -H "Origin: https://$HEALTH_ADDR" \
    -H 'X-Forwarded-Proto: https' \
    -d '{"email":"nobody@example.com","password":"wrong-password"}' \
    2>>"$E2E_STEP_LOG" || true)"
if [ "$LOGIN_CODE" = "403" ]; then
    e2e_die "登录回 403：server.env 的 YIBAN_TRUSTED_PROXIES 未覆盖宿主回环"
fi
if [ "$LOGIN_CODE" != "401" ]; then
    e2e_die "登录 HTTP $LOGIN_CODE（期望 401 无效凭据；403 表示信任链断了）"
fi
e2e_ok "X-Forwarded-Proto=https 被采信，登录回 401 而非 403"

# ---- B8 落位与权限 ---------------------------------------------------------
e2e_step "B8" "落位与权限"
ENV_META="$(os_exec stat -c '%a:%U:%G' /etc/yiban/server.env 2>>"$E2E_STEP_LOG" || true)"
[ "$ENV_META" = "600:root:root" ] || e2e_die "/etc/yiban/server.env 权限=$ENV_META（期望 600:root:root）"
for b in yiban-server yiban-cli; do
    META="$(os_exec stat -c '%a' "/usr/local/bin/$b" 2>>"$E2E_STEP_LOG" || true)"
    [ "$META" = "755" ] || e2e_die "/usr/local/bin/$b 权限=$META（期望 755）"
done
os_exec test -f /etc/systemd/system/yiban.service >>"$E2E_STEP_LOG" 2>&1 \
    || e2e_die "缺 /etc/systemd/system/yiban.service"
os_exec id -u yiban >>"$E2E_STEP_LOG" 2>&1 || e2e_die "服务用户 yiban 不存在"
PHOTO_META="$(os_exec stat -c '%U:%G' /var/lib/yiban/photos 2>>"$E2E_STEP_LOG" || true)"
[ "$PHOTO_META" = "yiban:yiban" ] || e2e_die "photos 目录属主=$PHOTO_META（期望 yiban:yiban）"
os_exec grep -qE '^YIBAN_HASH_PEPPER=[0-9a-f]{64}$' /etc/yiban/server.env >>"$E2E_STEP_LOG" 2>&1 \
    || e2e_die "server.env 的 YIBAN_HASH_PEPPER 不是 64 位十六进制"
KEY_BYTES="$(os_exec sh -c "grep -E '^YIBAN_CREDENTIAL_KEY=' /etc/yiban/server.env | cut -d= -f2- | base64 -d | wc -c" 2>>"$E2E_STEP_LOG" | tr -d '[:space:]' || true)"
[ "$KEY_BYTES" = "32" ] || e2e_die "YIBAN_CREDENTIAL_KEY 解码后 $KEY_BYTES 字节（期望 32）"
os_exec grep -Fxq "YIBAN_DATABASE_URL=$DSN" /etc/yiban/server.env >>"$E2E_STEP_LOG" 2>&1 \
    || e2e_die "server.env 的 YIBAN_DATABASE_URL 与传入值不一致"
os_exec grep -Fxq 'YIBAN_UI_DIR=/var/lib/yiban/ui' /etc/yiban/server.env >>"$E2E_STEP_LOG" 2>&1 \
    || e2e_die "server.env 缺 YIBAN_UI_DIR=/var/lib/yiban/ui"
os_exec grep -Fxq 'YIBAN_TRUSTED_PROXIES=127.0.0.1' /etc/yiban/server.env >>"$E2E_STEP_LOG" 2>&1 \
    || e2e_die "server.env 缺 YIBAN_TRUSTED_PROXIES=127.0.0.1（宿主反代在回环）"
os_exec test -f /var/lib/yiban/ui/index.html >>"$E2E_STEP_LOG" 2>&1 \
    || e2e_die "前端产物未落到 /var/lib/yiban/ui"
UI_META="$(os_exec stat -c '%U:%G' /var/lib/yiban/ui 2>>"$E2E_STEP_LOG" || true)"
[ "$UI_META" = "yiban:yiban" ] || e2e_die "ui 目录属主=$UI_META（期望 yiban:yiban）"
HOST_EXAMPLE_SUM="$(e2e_sha256 "$REPO/deploy/baremetal/server.env.example" || true)"
GUEST_EXAMPLE_SUM="$(os_exec sha256sum /etc/yiban/server.env.example 2>>"$E2E_STEP_LOG" | cut -d' ' -f1 || true)"
[ -n "$HOST_EXAMPLE_SUM" ] && [ "$HOST_EXAMPLE_SUM" = "$GUEST_EXAMPLE_SUM" ] \
    || e2e_die "安装的 server.env.example 与 deploy/baremetal/server.env.example 不一致"
EXAMPLE_META="$(os_exec stat -c '%a:%U:%G' /etc/yiban/server.env.example 2>>"$E2E_STEP_LOG" || true)"
[ "$EXAMPLE_META" = "644:root:root" ] || e2e_die "/etc/yiban/server.env.example 权限=$EXAMPLE_META（期望 644:root:root）"
if os_exec test -e /etc/yiban/registry.json >>"$E2E_STEP_LOG" 2>&1; then
    e2e_die "安装位不得有 registry.json：名册随二进制嵌入"
fi
e2e_ok "server.env 600 root:root；模板 644 root:root；二进制 755；photos 与 ui 属 yiban；无名册文件"

# ---- B9 迁移版本与干净库 ---------------------------------------------------
e2e_step "B9" "迁移版本与干净库"
EXPECTED_SCHEMA="$(e2e_expected_schema_version "$REPO")"
if [ "$EXPECTED_SCHEMA" -le 0 ]; then
    e2e_die "server/migrations/postgres 里没有迁移文件"
fi
if ! os_exec bash -c 'set -a; . /etc/yiban/server.env; set +a; /usr/local/bin/yiban-cli migrate' \
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
