#!/usr/bin/env bash
# ============================================================
# scripts/e2e/update-rollback-e2e.sh — 升级回滚演练 e2e（工单 9f6 的 E-2）。
#
# 模拟的真实用户动作（AGENTS §8g）：
#   操作员在一台已装好 yiban 的 Ubuntu 主机上跑 deploy/linux/update.sh。
#   三条真实路径各演一遍：
#   U3 新版迁移在库上失败 → update.sh 必须退 1、旧二进制原位、服务活着；
#   U4 换件后健康检查失败 → 退 1、.bak 换回旧件、服务活着；
#   U5 正常升级 → 退 0、.bak 保存升级前旧件、服务健康。
#
# 为什么容器内是真跑（AGENTS §32）：
#   被测性质：update.sh 的文件替换与回滚、真 systemctl 停起 unit、
#   真 goose 迁移对真 PostgreSQL 18 的成败、pg_dump 快照、/healthz 探活。
#   本脚本复用 deploy-baremetal-e2e.sh 的一次性 Ubuntu 容器壳
#   （真 systemd PID1、--privileged、host cgroup、真 PostgreSQL 18、
#   独立前缀/网络/卷）。不等价的一维：容器没有真实内核启动与硬件；
#   该维不影响上述任一性质，摘要里显式打印。
#
# 安全守卫（硬要求，上一版事故的对症门）：
#   演练步骤（run 子命令）只准在演练容器内执行。守卫要求
#   /etc/yiban-e2e-container 标记文件存在且容器主机名与标记内容同值；
#   标记内容由驱动在创建容器时写入（＝主机名配置值），判据与配置同源。
#   容器外跑 run 一律 exit 2 且零写入
#   （守卫在 e2e_init 建目录之前执行）。update.sh 只在容器内被调用，
#   驱动模式永不执行它。驱动 H1 步每次现演这条负向用例：
#   在宿主上跑 run → 必须退 2、安装位元数据快照一致（名称/类型/大小/mtime）。
#
# 模式：
#   update-rollback-e2e.sh host --log-dir DIR [--keep]   宿主驱动（缺省子命令）
#   update-rollback-e2e.sh --log-dir DIR                同上（--log-dir 开头＝host）
#   update-rollback-e2e.sh run --log-dir DIR --dist D --src S ...
#                                                       容器内演练（有守卫）
#   update-rollback-e2e.sh                              打印用法，exit 2，零写入
#
# 步骤（宿主驱动）：
#   H1 前置与负向守卫      H2 OS 镜像（systemd + openssl）
#   H3 PostgreSQL 18 与应用角色   H4 构建四组二进制
#   H5 演练容器与物料就位   H6 容器内演练（run 模式）
# 步骤（容器内 run）：
#   R1 基线安装（install.sh + v1）与守卫自证
#   R2 U3 迁移失败回滚     R3 U4 健康失败回滚     R4 U5 正常升级
#
# 失败口径：退出码 1，打印 failed step: <ID> <标题>；每步日志在 --log-dir。
# 退出码 2 = 用法、环境错误，或守卫拒绝。
#
# E-3 之后普通升级必须显式逃生口：本演练三次 update.sh 全部带
#   --allow-unsigned（真签名三件套路径由 deploy-baremetal-e2e B4b 覆盖）。
#
# 坏迁移注入：迁移 SQL 嵌在二进制里改不了。演练在编译时给「新版」
#   源码副本加一条真迁移（00021，CREATE TABLE），升级时先用 psql 对
#   应用角色 REVOKE CREATE ON SCHEMA public，让 goose 走真迁移路径
#   撞权限错误。U4 前再把权限还回去。
#
# 发布门状态：发布前手跑项（MANUAL）。需要 docker --privileged 与
#   host cgroup；CI 托管 runner 可用性未实测，故不进自动轨。
#
# 隔离：独立容器名/网络/卷（前缀 ROLLBACK_E2E_PREFIX，默认
#   yiban-e2e-rollback），不碰本机既有容器。
# ============================================================
set -Eeuo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="$SELF_DIR/$(basename "${BASH_SOURCE[0]}")"
REPO="$(cd "$SELF_DIR/../.." && pwd)"
# shellcheck source=lib/deploy-e2e.sh
. "$SELF_DIR/lib/deploy-e2e.sh"

usage() { sed -n '2,59p' "$SELF"; }

die_usage() {
    echo "update-rollback-e2e: $1" >&2
    usage >&2
    exit 2
}

# ---- 容器守卫（run 模式专用，先于一切写入） --------------------------------
# 判据两件同时成立：标记文件存在 + 容器主机名与标记内容同值。
# 标记内容由驱动在创建容器时写入（＝ROLLBACK_E2E_HOSTNAME 配置值），
# 判据与配置同源；容器内无需重复解析配置。任一不成立 → exit 2。
# 本函数不得创建任何文件或目录：容器外零写入靠它排在 e2e_init 之前。
e2e_guard_container() {
    local reason="" expected=""
    if [ ! -f /etc/yiban-e2e-container ]; then
        reason="marker file /etc/yiban-e2e-container is missing"
    else
        expected="$(head -n1 /etc/yiban-e2e-container 2>/dev/null | tr -d '[:space:]')"
        if [ -z "$expected" ]; then
            reason="marker file is empty or unreadable"
        elif [ "$(hostname)" != "$expected" ]; then
            reason="hostname $(hostname) does not match the marker hostname ($expected)"
        fi
    fi
    if [ -n "$reason" ]; then
        echo "update-rollback-e2e: REFUSING to run the drill outside the e2e container: $reason" >&2
        echo "update-rollback-e2e: run 'bash $SELF host --log-dir <dir>' on the host; the driver creates the container itself" >&2
        exit 2
    fi
}

# ---- 宿主安装位快照（负向用例的判据材料） ----------------------------------
# 覆盖上次事故的全部落点：安装位、配置、数据目录、pg_wrapper。
host_install_snapshot() {
    local p
    for p in /usr/local/bin/yiban-server /usr/local/bin/yiban-cli \
             /etc/yiban /var/lib/yiban /usr/lib/postgresql-common/pg_wrapper; do
        if [ -e "$p" ]; then
            stat -L -c '%n %F %s %Y' -- "$p" 2>/dev/null || printf 'stat-failed %s\n' "$p"
        else
            printf 'absent %s\n' "$p"
        fi
    done
}

host_mode() {
    local LOG_DIR HOST_LOG_DIR="" KEEP=0 STARTED=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --log-dir) HOST_LOG_DIR="${2:?--log-dir 需要目录}"; shift 2 ;;
            --log-dir=*) HOST_LOG_DIR="${1#*=}"; shift ;;
            --keep) KEEP=1; shift ;;
            -h|--help) usage; exit 0 ;;
            *) die_usage "未知参数：$1" ;;
        esac
    done
    [ -n "$HOST_LOG_DIR" ] || die_usage "host 模式需要 --log-dir"

    PREFIX="${ROLLBACK_E2E_PREFIX:-yiban-e2e-rollback}"
    OS_IMAGE="${ROLLBACK_E2E_OS_IMAGE:-yiban-e2e-os:ubuntu24.04}"
    OS_BASE="${ROLLBACK_E2E_OS_BASE:-ubuntu:24.04}"
    PG_IMAGE="${ROLLBACK_E2E_PG_IMAGE:-postgres:18}"
    GO_IMAGE="${ROLLBACK_E2E_GO_IMAGE:-golang:1.27-alpine}"
    GOPROXY_ARG="${ROLLBACK_E2E_GOPROXY:-${DEPLOY_E2E_GOPROXY:-}}"
    PG_MAJOR="${ROLLBACK_E2E_PG_MAJOR:-18}"
    OS_HOSTNAME="${ROLLBACK_E2E_HOSTNAME:-e2e-rollback}"
    NET="${PREFIX}-net"; PG="${PREFIX}-pg"; OS="${PREFIX}-os"
    VOL="${PREFIX}-pgdata"; GO_CACHE_VOL="${PREFIX}-gocache"; GO_MOD_VOL="${PREFIX}-gomod"
    LOG_DIR="$HOST_LOG_DIR"

    mkdir -p "$LOG_DIR"
    e2e_init "update-rollback" "$LOG_DIR"

    cleanup() {
        local rc=$?
        trap - ERR EXIT
        # 退出码落盘：调用方跨 wsl.exe 取 $? 不可靠，证据以本文件为准。
        printf '%s\n' "$rc" > "$LOG_DIR/exit-code"
        e2e_exit_note "$rc"
        # 日志回拷先行：容器还在时把容器内演练日志搬回宿主。
        if docker inspect "$OS" >/dev/null 2>&1; then
            mkdir -p "$LOG_DIR/container-run"
            docker cp "$OS:/opt/e2e-logs/." "$LOG_DIR/container-run/" >>"$LOG_DIR/cleanup.log" 2>&1 || true
        fi
        if [ "$STARTED" -eq 1 ]; then
            if [ "$KEEP" -eq 1 ]; then
                e2e_say "(--keep) 保留容器 $OS、$PG，网络 $NET，卷 $VOL"
                e2e_say "  手动清理：docker rm -f $OS $PG; docker network rm $NET; docker volume rm $VOL"
            else
                docker rm -f "$OS" >>"$LOG_DIR/cleanup.log" 2>&1 || true
                docker rm -f "$PG" >>"$LOG_DIR/cleanup.log" 2>&1 || true
                docker network rm "$NET" >>"$LOG_DIR/cleanup.log" 2>&1 || true
                docker volume rm "$VOL" >>"$LOG_DIR/cleanup.log" 2>&1 || true
            fi
        fi
        # Go 构建缓存卷保留：跨次运行复用，不携带被测语义。
        if [ "$KEEP" -eq 1 ]; then
            e2e_say "(--keep) 构建缓存卷 $GO_CACHE_VOL、$GO_MOD_VOL 一并保留"
        fi
        exit "$rc"
    }
    trap cleanup EXIT

    wait_pg() {
        local limit="$1"
        local end=$((SECONDS + limit))
        while [ "$SECONDS" -lt "$end" ]; do
            if docker exec "$PG" pg_isready -U postgres -d yiban >/dev/null 2>&1; then
                return 0
            fi
            sleep 2
        done
        return 1
    }

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

    # ---- H1 前置与负向守卫 --------------------------------------------------
    e2e_step "H1" "前置与负向守卫"
    e2e_require docker
    docker info >>"$E2E_STEP_LOG" 2>&1 || e2e_die "docker daemon 不可达"
    {
        docker version --format 'server={{.Server.Version}}'
        git -C "$REPO" rev-parse HEAD
    } >>"$E2E_STEP_LOG" 2>&1 || true
    # 清理本前缀残留；别的项目不碰。
    docker rm -f "$OS" >/dev/null 2>&1 || true
    docker rm -f "$PG" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
    docker volume rm "$VOL" >/dev/null 2>&1 || true
    if docker inspect "$OS" >/dev/null 2>&1 || docker inspect "$PG" >/dev/null 2>&1; then
        e2e_die "清理后仍存在同名容器"
    fi
    STARTED=1
    # 负向用例：宿主上直接跑演练步骤必须被守卫拒绝（exit 2、零写入、
    # 安装位元数据快照一致：名称/类型/大小/mtime）。
    # 上次事故（YIBAN_INSTALL_ROOT 为空写穿真实
    # /etc/yiban、/usr/local/bin、pg_wrapper）由此门钉死。
    local snap_before guard_rc probe_dir
    snap_before="$(host_install_snapshot)"
    probe_dir="$LOG_DIR/guard-probe"
    rm -rf "$probe_dir"
    guard_rc=0
    bash "$SELF" run --log-dir "$probe_dir" >"$LOG_DIR/H1-guard-refusal.log" 2>&1 || guard_rc=$?
    [ "$guard_rc" -eq 2 ] || e2e_die "守卫自检：宿主跑 run 退出码 $guard_rc（期望 2）；见 H1-guard-refusal.log"
    grep -q 'REFUSING to run the drill outside the e2e container' "$LOG_DIR/H1-guard-refusal.log" \
        || e2e_die "守卫自检：拒绝输出不含守卫话术（退出码 2 可能来自别的检查）"
    [ ! -e "$probe_dir" ] || e2e_die "守卫自检：被拒的 run 在宿主创建了 $probe_dir（零写入被破坏）"
    [ "$(host_install_snapshot)" = "$snap_before" ] \
        || e2e_die "守卫自检：宿主安装位快照在被拒 run 前后不一致"
    e2e_ok "docker 可达；宿主跑 run 被拒（rc=2），安装位元数据快照一致"

    # ---- H2 OS 镜像 ---------------------------------------------------------
    e2e_step "H2" "OS 镜像（systemd + openssl）"
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
            e2e_die "构建 OS 镜像失败（需要访问 apt 源；见 H2.log）"
        fi
        e2e_ok "构建 $OS_IMAGE（基 $OS_BASE）"
    fi
    if ! docker run --rm "$OS_IMAGE" sh -c 'test -x /sbin/init && test -x /usr/bin/openssl' \
        >>"$E2E_STEP_LOG" 2>&1; then
        e2e_die "OS 镜像缺 /sbin/init 或 openssl"
    fi

    # ---- H3 PostgreSQL 18 与应用角色 ----------------------------------------
    e2e_step "H3" "PostgreSQL 18 与应用角色"
    if ! docker network create "$NET" >>"$E2E_STEP_LOG" 2>&1; then
        e2e_die "创建网络 $NET 失败"
    fi
    PG_PASSWORD="$(e2e_secret_hex 16)"
    APP_PASSWORD="$(e2e_secret_hex 16)"
    if ! docker volume create "$VOL" >>"$E2E_STEP_LOG" 2>&1; then
        e2e_die "创建卷 $VOL 失败"
    fi
    # 应用角色不是超级用户也不是库属主：REVOKE CREATE 才咬得住（U3 的前提）。
    if ! docker run -d --name "$PG" --network "$NET" \
        -e POSTGRES_DB=yiban -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD="$PG_PASSWORD" \
        -v "$VOL:/var/lib/postgresql" "$PG_IMAGE" >>"$E2E_STEP_LOG" 2>&1; then
        e2e_die "启动 PostgreSQL 容器失败"
    fi
    if ! wait_pg 120; then
        docker logs --tail 60 "$PG" >>"$E2E_STEP_LOG" 2>&1 || true
        e2e_die "PostgreSQL 未在 120s 内就绪"
    fi
    # 应用角色开通：登录 + public 模式 USAGE/CREATE（迁移要建表）。
    cat > "$LOG_DIR/sql-provision.sql" <<EOF
CREATE ROLE yiban LOGIN PASSWORD '$APP_PASSWORD';
GRANT CONNECT ON DATABASE yiban TO yiban;
GRANT USAGE, CREATE ON SCHEMA public TO yiban;
EOF
    docker cp "$LOG_DIR/sql-provision.sql" "$PG:/tmp/provision.sql" >>"$E2E_STEP_LOG" 2>&1 \
        || e2e_die "复制 provision.sql 失败"
    if ! docker exec "$PG" psql -U postgres -d yiban -v ON_ERROR_STOP=1 \
        -f /tmp/provision.sql >>"$E2E_STEP_LOG" 2>&1; then
        e2e_die "应用角色开通失败"
    fi
    APP_DSN="postgres://yiban:${APP_PASSWORD}@${PG}:5432/yiban?sslmode=disable"
    ADMIN_DSN="postgres://postgres:${PG_PASSWORD}@${PG}:5432/yiban?sslmode=disable"
    GRANTED="$(docker exec "$PG" psql -U postgres -d yiban -tAc \
        "select has_schema_privilege('yiban','public','CREATE')" 2>>"$E2E_STEP_LOG" | tr -d '[:space:]')"
    [ "$GRANTED" = "t" ] || e2e_die "应用角色 CREATE 权限=$GRANTED（期望 t）"
    e2e_ok "PostgreSQL 18 就绪；应用角色 yiban（非属主）有 public CREATE"

    # ---- H4 构建四组二进制 --------------------------------------------------
    e2e_step "H4" "构建四组二进制"
    BIN_OUT="$LOG_DIR/bin"
    rm -rf "$BIN_OUT"
    mkdir -p "$BIN_OUT"
    EXPECTED_SCHEMA="$(e2e_expected_schema_version "$REPO")"
    [ "$EXPECTED_SCHEMA" -gt 0 ] || e2e_die "server/migrations/postgres 里没有迁移文件"
    # v2 = 仓库源码 + 00021（CREATE TABLE，U3 的迁移靶子）。
    # v3 = 仓库源码 + main() 起手 panic（U4 的健康靶子）。
    # v4 = 仓库源码 + 00021 + 00022（U5 的正常升级件）。
    # 同一 --log-dir 复跑时旧树必须先清：cp -a 对已存在目标会把源嵌进
    # <目标>/server/，挂载的仍是旧源码。
    rm -rf "$LOG_DIR/srv-v2" "$LOG_DIR/srv-v3" "$LOG_DIR/srv-v4"
    cp -a "$REPO/server" "$LOG_DIR/srv-v2"
    cat > "$LOG_DIR/srv-v2/migrations/postgres/00021_e2e_rollback_probe.sql" <<'EOF'
-- +goose Up
-- E-2 回滚演练件：需要 public 模式 CREATE 权限，供 U3 注入 REVOKE 后
-- 走真 goose 路径失败。建出的表同时证明「迁移不随二进制回滚」这一
-- 已登记边界（update.sh 头注）。
CREATE TABLE e2e_rollback_probe (
    id integer PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- +goose Down
DROP TABLE IF EXISTS e2e_rollback_probe;
EOF
    cp -a "$REPO/server" "$LOG_DIR/srv-v3"
    sed -i 's|^func main() {$|func main() {\n\tpanic("e2e-rollback-e2e: injected unhealthy build for U4")|' \
        "$LOG_DIR/srv-v3/cmd/server/main.go"
    grep -q 'injected unhealthy build for U4' "$LOG_DIR/srv-v3/cmd/server/main.go" \
        || e2e_die "v3 源码注入失败（main.go 形状与预期不符）"
    cp -a "$LOG_DIR/srv-v2" "$LOG_DIR/srv-v4"
    cat > "$LOG_DIR/srv-v4/migrations/postgres/00022_e2e_version_bump.sql" <<'EOF'
-- +goose Up
-- E-2 回滚演练件：U5 正常升级的可应用迁移（无 DDL，靠 SELECT 走通
-- goose 全流程并把版本推到 22）。
SELECT 1;

-- +goose Down
SELECT 1;
EOF
    # 嵌套自检：拷贝后的 scratch 树顶层必须是源码根。
    # 挂载点对着顶层；嵌套一层时构建的「新版」就是旧源码（判据静默失效）。
    for d in srv-v2 srv-v3 srv-v4; do
        [ -f "$LOG_DIR/$d/go.mod" ] \
            || e2e_die "$d 顶层缺 go.mod（源码被嵌套拷贝，挂载的不是顶层源码）"
        [ ! -d "$LOG_DIR/$d/server" ] \
            || e2e_die "$d 内出现嵌套 server/ 目录（cp 目标已存在）"
    done
    [ -f "$LOG_DIR/srv-v2/migrations/postgres/00021_e2e_rollback_probe.sql" ] \
        || e2e_die "srv-v2 顶层缺 00021 演练迁移"
    [ -f "$LOG_DIR/srv-v4/migrations/postgres/00022_e2e_version_bump.sql" ] \
        || e2e_die "srv-v4 顶层缺 00022 演练迁移"
    cat > "$LOG_DIR/build-all.sh" <<'EOF'
#!/bin/sh
# 四组二进制一次编译：共享 GOCACHE/GOPATH（挂载卷，跨次运行复用）。
set -eu
export CGO_ENABLED=0 GOCACHE=/tmp/gocache GOPATH=/tmp/gopath
if [ -n "${ROLLBACK_E2E_GOPROXY:-}" ]; then
    export GOPROXY="$ROLLBACK_E2E_GOPROXY"
fi
for v in v1 v2 v3 v4; do
    cd "/src/$v"
    go build -trimpath -ldflags="-s -w" -o "/out/yiban-server-$v" ./cmd/server
    go build -trimpath -ldflags="-s -w" -o "/out/yiban-cli-$v" ./cmd/cli
done
EOF
    if ! docker run --rm \
        -e "ROLLBACK_E2E_GOPROXY=$GOPROXY_ARG" \
        -v "$REPO/server:/src/v1:ro" \
        -v "$LOG_DIR/srv-v2:/src/v2" \
        -v "$LOG_DIR/srv-v3:/src/v3" \
        -v "$LOG_DIR/srv-v4:/src/v4" \
        -v "$BIN_OUT:/out" \
        -v "$LOG_DIR/build-all.sh:/build-all.sh:ro" \
        -v "$GO_CACHE_VOL:/tmp/gocache" \
        -v "$GO_MOD_VOL:/tmp/gopath" \
        -w / \
        "$GO_IMAGE" sh /build-all.sh >>"$E2E_STEP_LOG" 2>&1; then
        e2e_die "Go 构建失败（受限网络请设 ROLLBACK_E2E_GOPROXY；见 H4.log）"
    fi
    for b in yiban-server-v1 yiban-cli-v1 yiban-server-v2 yiban-cli-v2 \
             yiban-server-v3 yiban-cli-v3 yiban-server-v4 yiban-cli-v4; do
        [ -x "$BIN_OUT/$b" ] || e2e_die "$b 未产出或不可执行"
    done
    SHA_V1="$(e2e_sha256 "$BIN_OUT/yiban-server-v1")"
    SHA_V2="$(e2e_sha256 "$BIN_OUT/yiban-server-v2")"
    SHA_V3="$(e2e_sha256 "$BIN_OUT/yiban-server-v3")"
    SHA_V4="$(e2e_sha256 "$BIN_OUT/yiban-server-v4")"
    [ "$SHA_V1" != "$SHA_V2" ] && [ "$SHA_V1" != "$SHA_V3" ] && [ "$SHA_V1" != "$SHA_V4" ] \
        && [ "$SHA_V2" != "$SHA_V3" ] && [ "$SHA_V2" != "$SHA_V4" ] && [ "$SHA_V3" != "$SHA_V4" ] \
        || e2e_die "四组 server 二进制 sha256 有重复（期望两两不同）"
    e2e_ok "v1..v4 各产出 server+cli；sha256 两两不同"

    # ---- H5 演练容器与物料就位 ----------------------------------------------
    e2e_step "H5" "演练容器与物料就位"
    if ! docker run -d --name "$OS" --network "$NET" --hostname "$OS_HOSTNAME" \
        --privileged --cgroupns=host \
        -v /sys/fs/cgroup:/sys/fs/cgroup:rw --tmpfs /run --tmpfs /run/lock \
        "$OS_IMAGE" >>"$E2E_STEP_LOG" 2>&1; then
        e2e_die "启动 OS 容器失败"
    fi
    # 标记先行：任何后续 exec 之前，守卫判据已成立。
    # 标记文件内容＝演练主机名（ROLLBACK_E2E_HOSTNAME 配置值）：守卫判据与配置同源。
    printf '%s\n' "$OS_HOSTNAME" > "$LOG_DIR/container-marker"
    docker cp "$LOG_DIR/container-marker" "$OS:/etc/yiban-e2e-container" >>"$E2E_STEP_LOG" 2>&1 \
        || e2e_die "写容器标记失败"
    docker exec "$OS" test -f /etc/yiban-e2e-container >>"$E2E_STEP_LOG" 2>&1 \
        || e2e_die "容器标记不存在"
    if ! SYSTEMD_STATE="$(wait_systemd 60)"; then
        docker logs --tail 60 "$OS" >>"$E2E_STEP_LOG" 2>&1 || true
        e2e_die "systemd 未在 60s 内就绪"
    fi
    docker exec "$OS" mkdir -p /opt/e2e-src /opt/e2e-dist /opt/e2e/sql /opt/e2e/scripts/e2e/lib /opt/pg18 \
        >>"$E2E_STEP_LOG" 2>&1 || e2e_die "容器内目录创建失败"
    docker cp "$REPO/deploy" "$OS:/opt/e2e-src/deploy" >>"$E2E_STEP_LOG" 2>&1 || e2e_die "复制 deploy/ 失败"
    docker cp "$REPO/config" "$OS:/opt/e2e-src/config" >>"$E2E_STEP_LOG" 2>&1 || e2e_die "复制 config/ 失败"
    docker cp "$SELF" "$OS:/opt/e2e/scripts/e2e/update-rollback-e2e.sh" >>"$E2E_STEP_LOG" 2>&1 \
        || e2e_die "复制演练脚本失败"
    docker cp "$SELF_DIR/lib/deploy-e2e.sh" "$OS:/opt/e2e/scripts/e2e/lib/deploy-e2e.sh" \
        >>"$E2E_STEP_LOG" 2>&1 || e2e_die "复制演练共用件失败"
    for b in yiban-server-v1 yiban-cli-v1 yiban-server-v2 yiban-cli-v2 \
             yiban-server-v3 yiban-cli-v3 yiban-server-v4 yiban-cli-v4; do
        docker cp "$BIN_OUT/$b" "$OS:/opt/e2e-dist/$b" >>"$E2E_STEP_LOG" 2>&1 || e2e_die "复制 $b 失败"
    done
    # U3/U4 的权限注入 SQL：psql 在容器内执行（走 H5 提取的 PG 客户端）。
    cat > "$LOG_DIR/sql-u3-revoke.sql" <<'EOF'
REVOKE CREATE ON SCHEMA public FROM yiban;
EOF
    cat > "$LOG_DIR/sql-u4-grant.sql" <<'EOF'
GRANT CREATE ON SCHEMA public TO yiban;
EOF
    docker cp "$LOG_DIR/sql-u3-revoke.sql" "$OS:/opt/e2e/sql/u3-revoke.sql" >>"$E2E_STEP_LOG" 2>&1 \
        || e2e_die "复制 u3-revoke.sql 失败"
    docker cp "$LOG_DIR/sql-u4-grant.sql" "$OS:/opt/e2e/sql/u4-grant.sql" >>"$E2E_STEP_LOG" 2>&1 \
        || e2e_die "复制 u4-grant.sql 失败"
    # pg_dump/psql 从 postgres:18 镜像整包提取（tar 单文件中转）：
    # update.sh 的迁移前快照要求容器内有版本匹配的 pg_dump。
    # tar -ch 解引用符号链接：成员按 DT_NEEDED 名（.so.N）落成真件，
    # 不依赖镜像里的 .so.N.M 真名（随镜像版本漂移）。
    PGTOOLS_TAR="$LOG_DIR/pg18tools.tar"
    PG_LIBS="lib/x86_64-linux-gnu/libpq.so.5 lib/x86_64-linux-gnu/libpq.so.5.${PG_MAJOR}"
    PG_LIBS="$PG_LIBS lib/x86_64-linux-gnu/libgssapi_krb5.so.2 lib/x86_64-linux-gnu/libkrb5.so.3"
    PG_LIBS="$PG_LIBS lib/x86_64-linux-gnu/libk5crypto.so.3 lib/x86_64-linux-gnu/libkrb5support.so.0"
    PG_LIBS="$PG_LIBS lib/x86_64-linux-gnu/libcom_err.so.2 lib/x86_64-linux-gnu/libkeyutils.so.1"
    PG_LIBS="$PG_LIBS lib/x86_64-linux-gnu/libreadline.so.8 lib/x86_64-linux-gnu/libtinfo.so.6"
    PG_LIBS="$PG_LIBS lib/x86_64-linux-gnu/libsasl2.so.2 lib/x86_64-linux-gnu/libldap.so.2"
    PG_LIBS="$PG_LIBS lib/x86_64-linux-gnu/liblber.so.2 lib/x86_64-linux-gnu/libzstd.so.1"
    PG_LIBS="$PG_LIBS lib/x86_64-linux-gnu/liblz4.so.1 lib/x86_64-linux-gnu/libxxhash.so.0"
    PG_LIBS="$PG_LIBS lib/x86_64-linux-gnu/libssl.so.3 lib/x86_64-linux-gnu/libcrypto.so.3"
    if ! docker run --rm "$PG_IMAGE" sh -c "cd / && tar -chf - \
        usr/lib/postgresql/${PG_MAJOR}/bin/pg_dump usr/lib/postgresql/${PG_MAJOR}/bin/psql $PG_LIBS" \
        > "$PGTOOLS_TAR"; then
        e2e_die "从 $PG_IMAGE 提取 pg 客户端失败（tar 中转见 $PGTOOLS_TAR）"
    fi
    PGTOOLS_COUNT="$(tar -tf "$PGTOOLS_TAR" | wc -l | tr -d '[:space:]')"
    PGTOOLS_EXPECTED=$((2 + $(printf '%s' "$PG_LIBS" | wc -w)))
    [ "$PGTOOLS_COUNT" -eq "$PGTOOLS_EXPECTED" ] \
        || e2e_die "pg 客户端 tar 应含 $PGTOOLS_EXPECTED 项（2 个客户端 + $((PGTOOLS_EXPECTED - 2)) 个库），实得 $PGTOOLS_COUNT"
    docker cp "$PGTOOLS_TAR" "$OS:/opt/pg18/tools.tar" >>"$E2E_STEP_LOG" 2>&1 || e2e_die "复制 pg 客户端 tar 失败"
    docker exec "$OS" tar -xf /opt/pg18/tools.tar -C /opt/pg18 >>"$E2E_STEP_LOG" 2>&1 \
        || e2e_die "容器内解包 pg 客户端失败"
    docker exec "$OS" chmod -R 755 /opt/pg18 >>"$E2E_STEP_LOG" 2>&1 || e2e_die "pg 客户端授权失败"
    # 包装器落 /usr/local/bin：update.sh 只要求 PATH 里有 pg_dump。
    cat > "$LOG_DIR/wrapper-pg_dump.sh" <<EOF
#!/bin/sh
# E-2 演练：PG ${PG_MAJOR} 客户端运行于 /opt/pg18，带私有库目录。
exec env LD_LIBRARY_PATH=/opt/pg18/lib/x86_64-linux-gnu \
    /opt/pg18/usr/lib/postgresql/${PG_MAJOR}/bin/pg_dump "\$@"
EOF
    cat > "$LOG_DIR/wrapper-psql.sh" <<EOF
#!/bin/sh
# E-2 演练：PG ${PG_MAJOR} 客户端运行于 /opt/pg18，带私有库目录。
exec env LD_LIBRARY_PATH=/opt/pg18/lib/x86_64-linux-gnu \
    /opt/pg18/usr/lib/postgresql/${PG_MAJOR}/bin/psql "\$@"
EOF
    docker cp "$LOG_DIR/wrapper-pg_dump.sh" "$OS:/usr/local/bin/pg_dump" >>"$E2E_STEP_LOG" 2>&1 \
        || e2e_die "安装 pg_dump 包装器失败"
    docker cp "$LOG_DIR/wrapper-psql.sh" "$OS:/usr/local/bin/psql" >>"$E2E_STEP_LOG" 2>&1 \
        || e2e_die "安装 psql 包装器失败"
    docker exec "$OS" chmod 755 /usr/local/bin/pg_dump /usr/local/bin/psql >>"$E2E_STEP_LOG" 2>&1 \
        || e2e_die "包装器授权失败"
    docker exec "$OS" pg_dump --version >>"$E2E_STEP_LOG" 2>&1 || e2e_die "容器内 pg_dump 不可用"
    docker exec "$OS" psql --version >>"$E2E_STEP_LOG" 2>&1 || e2e_die "容器内 psql 不可用"
    e2e_ok "systemd=$SYSTEMD_STATE；标记已写；物料就位；pg_dump/psql 可用"

    # ---- H6 容器内演练 ------------------------------------------------------
    e2e_step "H6" "容器内演练（run 模式）"
    DRILL_RC=0
    docker exec "$OS" bash /opt/e2e/scripts/e2e/update-rollback-e2e.sh run \
        --log-dir /opt/e2e-logs \
        --dist /opt/e2e-dist --src /opt/e2e-src \
        --pg-host "$PG" --app-dsn "$APP_DSN" --admin-dsn "$ADMIN_DSN" \
        --expected-schema "$EXPECTED_SCHEMA" \
        >"$E2E_STEP_LOG" 2>&1 || DRILL_RC=$?
    if [ "$DRILL_RC" -ne 0 ]; then
        e2e_fail "容器内演练 rc=$DRILL_RC；明细见 container-run/run.log"
        # 失败也要落 result=FAIL：文档口径是 run.log 末尾必有 PASS/FAIL 结论。
        e2e_summary || true
        exit "$DRILL_RC"
    fi
    e2e_ok "容器内演练 rc=0"

    e2e_say ""
    e2e_say "MANUAL（发布前手跑项）：本脚本不在自动轨（--privileged 未在托管 runner 实测）。"
    e2e_say "等价性登记：真 systemd + 真 PostgreSQL 18 + 真 goose 迁移 + 真 update.sh 回滚；"
    e2e_say "不等价的一维是没有真实内核启动与硬件。"
    e2e_say "日志目录：$LOG_DIR（容器内演练明细在 container-run/）"
    e2e_summary || exit 1
    exit 0
}

# ============================================================================
# run 模式：容器内演练。守卫最先，任何写入都在守卫之后。
# ============================================================================
run_mode() {
    local RUN_LOG_DIR="" DIST="" SRC="" PG_HOST="" APP_DSN="" ADMIN_DSN="" EXPECTED_SCHEMA=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --log-dir) RUN_LOG_DIR="${2:?}"; shift 2 ;;
            --log-dir=*) RUN_LOG_DIR="${1#*=}"; shift ;;
            --dist) DIST="${2:?}"; shift 2 ;;
            --src) SRC="${2:?}"; shift 2 ;;
            --pg-host) PG_HOST="${2:?}"; shift 2 ;;
            --app-dsn) APP_DSN="${2:?}"; shift 2 ;;
            --admin-dsn) ADMIN_DSN="${2:?}"; shift 2 ;;
            --expected-schema) EXPECTED_SCHEMA="${2:?}"; shift 2 ;;
            -h|--help) usage; exit 0 ;;
            *) die_usage "run 模式未知参数：$1" ;;
        esac
    done

    # 守卫：先于 e2e_init（e2e_init 会 mkdir 日志目录）。
    e2e_guard_container
    [ -n "$RUN_LOG_DIR" ] || die_usage "run 模式需要 --log-dir"
    [ -n "$DIST" ] && [ -n "$SRC" ] && [ -n "$PG_HOST" ] \
        && [ -n "$APP_DSN" ] && [ -n "$ADMIN_DSN" ] && [ -n "$EXPECTED_SCHEMA" ] \
        || die_usage "run 模式需要 --dist/--src/--pg-host/--app-dsn/--admin-dsn/--expected-schema"

    mkdir -p "$RUN_LOG_DIR"
    e2e_init "update-rollback-drill" "$RUN_LOG_DIR"

    cleanup() {
        local rc=$?
        trap - ERR EXIT
        e2e_exit_note "$rc"
        exit "$rc"
    }
    trap cleanup EXIT

    UPDATE_SH="$SRC/deploy/linux/update.sh"
    INSTALL_SH="$SRC/deploy/linux/install.sh"
    REGISTRY="$SRC/config/registry.json"
    SERVER_DEST="/usr/local/bin/yiban-server"
    CLI_DEST="/usr/local/bin/yiban-cli"
    UNIT="yiban.service"

    # sha <文件>：容器内 sha256sum（coreutils 必有）。
    sha() { sha256sum "$1" | cut -d' ' -f1; }
    # psql_admin <SQL>：超级用户执行单条 SQL。
    psql_admin() { psql "$ADMIN_DSN" -v ON_ERROR_STOP=1 -tAc "$1"; }
    psql_app() { psql "$APP_DSN" -v ON_ERROR_STOP=1 -tAc "$1"; }
    goose_version() { psql_app "select coalesce(max(version_id), 0) from goose_db_version where is_applied" | tr -d '[:space:]'; }
    unit_active() { systemctl is-active "$UNIT" 2>/dev/null || true; }
    # 单次探活有假红窗口：Type=simple 的 start 在 bind 前返回。
    # 有界重试 10 次 × 1s，重试耗尽才红（与 update.sh 自身探活同级）。
    health_ok() {
        local i=1
        while [ "$i" -le 10 ]; do
            if "$CLI_DEST" healthcheck --addr "$HEALTH_ADDR" --timeout 2s >/dev/null 2>&1; then
                return 0
            fi
            sleep 1
            i=$((i + 1))
        done
        return 1
    }
    # assert_eq <说明> <got> <want>：不等即点名失败步骤并退出。
    assert_eq() {
        if [ "$2" != "$3" ]; then
            e2e_die "$1：got '$2' want '$3'"
        fi
    }

    # ---- R1 基线安装与守卫自证 ----------------------------------------------
    e2e_step "R1" "基线安装与守卫自证"
    [ -f /etc/yiban-e2e-container ] || e2e_die "容器标记丢失（守卫被判据钉住，此处为纵深）"
    for f in "$UPDATE_SH" "$INSTALL_SH" "$REGISTRY"; do
        [ -f "$f" ] || e2e_die "物料缺失：$f"
    done
    for b in yiban-server-v1 yiban-cli-v1 yiban-server-v2 yiban-cli-v2 \
             yiban-server-v3 yiban-cli-v3 yiban-server-v4 yiban-cli-v4; do
        [ -x "$DIST/$b" ] || e2e_die "二进制缺失：$DIST/$b"
    done
    if ! psql_admin "select 1" >/dev/null 2>&1; then
        e2e_die "超级用户 psql 连库失败（$PG_HOST）"
    fi
    # 基线安装：install.sh 真装 v1（服务起手会以应用角色跑迁移）。
    if ! YIBAN_HEALTH_RETRIES=60 YIBAN_HEALTH_RETRY_INTERVAL=1 \
        bash "$INSTALL_SH" \
        --server-bin "$DIST/yiban-server-v1" \
        --cli-bin "$DIST/yiban-cli-v1" \
        --database-url "$APP_DSN" >"$E2E_LOG_DIR/R1-install.log" 2>&1; then
        cat "$E2E_LOG_DIR/R1-install.log" >>"$E2E_STEP_LOG" 2>/dev/null || true
        journalctl -u "$UNIT" --no-pager >>"$E2E_STEP_LOG" 2>&1 || true
        e2e_die "install.sh 退出码非 0"
    fi
    grep -q 'install complete: healthz ok' "$E2E_LOG_DIR/R1-install.log" \
        || e2e_die "install.sh 未报告 install complete: healthz ok"
    HEALTH_ADDR="$(grep -E '^YIBAN_HTTP_ADDR=' /etc/yiban/.env | head -n 1 | cut -d= -f2- || true)"
    HEALTH_ADDR="${HEALTH_ADDR:-127.0.0.1:47183}"
    SHA_V1="$(sha "$DIST/yiban-server-v1")"
    SHA_V2="$(sha "$DIST/yiban-server-v2")"
    SHA_V3="$(sha "$DIST/yiban-server-v3")"
    SHA_V4="$(sha "$DIST/yiban-server-v4")"
    assert_eq "安装位 server sha256" "$(sha "$SERVER_DEST")" "$SHA_V1"
    assert_eq "unit 状态" "$(unit_active)" "active"
    if ! health_ok; then e2e_die "基线 healthz 探活失败（$HEALTH_ADDR）"; fi
    assert_eq "基线 schema 版本" "$(goose_version)" "$EXPECTED_SCHEMA"
    if psql_app "select to_regclass('public.e2e_rollback_probe') is not null" | grep -q t; then
        e2e_die "干净库已有 e2e_rollback_probe 表（基线不干净）"
    fi
    e2e_ok "install.sh rc=0；server=v1；unit active；healthz ok；schema=$EXPECTED_SCHEMA"

    # ---- R2 U3 迁移失败回滚 -------------------------------------------------
    e2e_step "R2" "U3 迁移失败回滚（REVOKE CREATE 注入）"
    if ! psql -f /opt/e2e/sql/u3-revoke.sql "$ADMIN_DSN" >>"$E2E_STEP_LOG" 2>&1; then
        e2e_die "注入 REVOKE CREATE 失败"
    fi
    assert_eq "注入后 yiban 的 public CREATE" "$(psql_admin "select has_schema_privilege('yiban','public','CREATE')")" "f"
    # E-3：默认拒未签名；演练显式给逃生口 --allow-unsigned。
    U3_RC=0
    bash "$UPDATE_SH" --server-bin "$DIST/yiban-server-v2" --cli-bin "$DIST/yiban-cli-v2" \
        --allow-unsigned >"$E2E_LOG_DIR/U3-update.log" 2>&1 || U3_RC=$?
    grep -q 'migrate FAILED' "$E2E_LOG_DIR/U3-update.log" \
        || e2e_die "U3：update.sh 未在迁移处失败（日志无 migrate FAILED）；见 U3-update.log"
    grep -q 'permission denied for schema public' "$E2E_LOG_DIR/U3-update.log" \
        || e2e_die "U3：迁移失败原因不是 schema 权限（缺 permission denied for schema public）；见 U3-update.log"
    assert_eq "U3 update.sh 退出码" "$U3_RC" "1"
    assert_eq "U3 安装位仍是旧二进制" "$(sha "$SERVER_DEST")" "$SHA_V1"
    [ ! -e "$SERVER_DEST.bak" ] || e2e_die "U3：迁移失败不应产生 .bak"
    [ ! -e "$SERVER_DEST.new" ] || e2e_die "U3：失败路径残留 .new 暂存件"
    assert_eq "U3 unit 状态" "$(unit_active)" "active"
    if ! health_ok; then e2e_die "U3：回滚后 healthz 探活失败"; fi
    assert_eq "U3 schema 版本不前进" "$(goose_version)" "$EXPECTED_SCHEMA"
    e2e_ok "rc=1；旧件原位；无 .bak/.new；unit active；healthz ok"

    # ---- R3 U4 健康检查失败回滚 ---------------------------------------------
    e2e_step "R3" "U4 健康检查失败回滚（换件后探活失败）"
    if ! psql -f /opt/e2e/sql/u4-grant.sql "$ADMIN_DSN" >>"$E2E_STEP_LOG" 2>&1; then
        e2e_die "归还 GRANT CREATE 失败"
    fi
    assert_eq "归还后 yiban 的 public CREATE" "$(psql_admin "select has_schema_privilege('yiban','public','CREATE')")" "t"
    # v3：迁移集与 v1 相同（不推进 schema——schema 前进会让回滚件撞
    # ErrDatabaseAhead，那是另一条已知边界，不在本步混测）；
    # main() 起手 panic → 换件后健康检查必失败。
    U4_RC=0
    bash "$UPDATE_SH" --server-bin "$DIST/yiban-server-v3" \
        --allow-unsigned >"$E2E_LOG_DIR/U4-update.log" 2>&1 || U4_RC=$?
    grep -q 'healthz failed after swap' "$E2E_LOG_DIR/U4-update.log" \
        || e2e_die "U4：update.sh 未在健康检查处失败；见 U4-update.log"
    grep -q 'rolled back' "$E2E_LOG_DIR/U4-update.log" \
        || e2e_die "U4：update.sh 未执行 .bak 换回；见 U4-update.log"
    assert_eq "U4 update.sh 退出码" "$U4_RC" "1"
    assert_eq "U4 换回后安装位是旧二进制" "$(sha "$SERVER_DEST")" "$SHA_V1"
    [ ! -e "$SERVER_DEST.bak" ] || e2e_die "U4：.bak 应已换回安装位（文件不应残留）"
    assert_eq "U4 unit 状态" "$(unit_active)" "active"
    if ! health_ok; then e2e_die "U4：回滚后 healthz 探活失败"; fi
    assert_eq "U4 schema 版本" "$(goose_version)" "$EXPECTED_SCHEMA"
    e2e_ok "rc=1；.bak 换回旧件；unit active；healthz ok"

    # ---- R4 U5 正常升级 -----------------------------------------------------
    e2e_step "R4" "U5 正常升级（含新迁移）"
    U5_RC=0
    bash "$UPDATE_SH" --server-bin "$DIST/yiban-server-v4" --cli-bin "$DIST/yiban-cli-v4" \
        --allow-unsigned >"$E2E_LOG_DIR/U5-update.log" 2>&1 || U5_RC=$?
    grep -q 'update complete: healthz ok' "$E2E_LOG_DIR/U5-update.log" \
        || e2e_die "U5：update.sh 未报告升级完成；见 U5-update.log"
    assert_eq "U5 update.sh 退出码" "$U5_RC" "0"
    assert_eq "U5 安装位是新二进制" "$(sha "$SERVER_DEST")" "$SHA_V4"
    assert_eq "U5 安装位 cli 是新件" "$(sha "$CLI_DEST")" "$(sha "$DIST/yiban-cli-v4")"
    [ -e "$SERVER_DEST.bak" ] || e2e_die "U5：升级成功后 .bak 应保留"
    assert_eq "U5 .bak 是升级前旧件" "$(sha "$SERVER_DEST.bak")" "$SHA_V1"
    assert_eq "U5 unit 状态" "$(unit_active)" "active"
    if ! health_ok; then e2e_die "U5：升级后 healthz 探活失败"; fi
    assert_eq "U5 schema 版本推进两档" "$(goose_version)" "$((EXPECTED_SCHEMA + 2))"
    assert_eq "U5 迁移靶表存在" "$(psql_app "select to_regclass('public.e2e_rollback_probe') is not null" | tr -d '[:space:]')" "t"
    e2e_ok "rc=0；.bak=升级前旧件；unit active；healthz ok；schema=$((EXPECTED_SCHEMA + 2))"

    e2e_say ""
    e2e_say "等价性登记：真 systemd + 真 PostgreSQL 18 + 真 goose 迁移 + 真 update.sh 回滚；"
    e2e_say "不等价的一维是没有真实内核启动与硬件。"
    e2e_summary || exit 1
    exit 0
}

# ============================================================================
# 入口：第一个参数是子命令；缺省（或以 - 开头）按 host 驱动处理。
# ============================================================================
MODE="${1:-}"
case "$MODE" in
    host) shift; host_mode "$@" ;;
    run) shift; run_mode "$@" ;;
    -h|--help) usage; exit 0 ;;
    '') die_usage "缺少子命令（host 或 run）" ;;
    -*) host_mode "$@" ;;
    *) die_usage "未知子命令：$MODE" ;;
esac
