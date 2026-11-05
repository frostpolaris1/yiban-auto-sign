#!/usr/bin/env bash
# ============================================================
# scripts/e2e/update-rollback-e2e.sh — 一键更新（U2）与回滚演练 e2e。
#
# 模拟的真实用户动作（AGENTS §8g）：
#   操作员在一台已装好 yiban 的 Ubuntu 主机上跑 deploy/baremetal/update.sh。
#   容器内真跑 U2 的完整失败矩阵：
#   R2 签名错           → exit 1，不碰旧件、不停服；
#   R3 自检 FAIL        → exit 2，不停服、不写状态/备份；
#   R4 备份失败         → exit 2，不停服、不迁移；
#   R5 无 CREATEDB      → exit 2，不开始（未给 --no-restore-db）；
#   R6 迁移失败(不回库) → exit 1，旧二进制原位、服务活着、schema 不前进；
#   R7 健康失败+回库    → exit 1，二进制/UI 换回，数据库恢复到新库
#                         yiban_restore_<ts> 并切 .env 的 DSN，服务活着，
#                         新库 schema = 升级前，原库保留（schema 已前进）；
#   R8 正常升级         → exit 0，.bak 保存升级前旧件，schema +2，首次装页面生效；
#   R9 健康失败+UI 回滚 → exit 1，旧页面换回、指针只剩一行、库不动（migrate 空转）；
#   R8a UI 预置软链      → 安装/更新都不顺链写，mv -T 原子替换（受害者字节/权限不变）；
#   R8b install.sh 重跑  → 更新器 root/state/backups 仍是 root 0700（install.sh 只 chown photos/ui）；
#   R8c 状态目录软链    → exit 2，root 不顺着软链写状态/日志；
#   R9b 备份件被篡改    → exit 2，回滚拒绝执行备份里的 CLI（属主/权限复核）；
#   R10 恢复失败        → exit 2，打印 manual action（含真实目标库名），服务停住。
#
# 为什么容器内是真跑（AGENTS §32）：
#   被测性质：update.sh 的自检门、统一备份（pg_dump -Fc + pg_restore --list）、
#   真 systemctl 停起 unit、真 goose 迁移对真 PostgreSQL 18 的成败、
#   真 CREATE DATABASE / pg_restore 的数据库回滚、/healthz 探活与状态文件。
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
#   H3 PostgreSQL 18 与应用角色（含 CREATEDB）   H4 构建六组二进制
#   H5 演练容器与物料就位   H6 容器内演练（run 模式）
# 步骤（容器内 run）：
#   R1 基线安装（install.sh + v1）与自检门自证
#   R2 签名错  R3 自检 FAIL  R4 备份失败  R5 无 CREATEDB
#   R6 迁移失败（--no-restore-db）  R7 健康失败 + 数据库回滚
#   R8 正常升级（含 --ui-dir）  R8a UI 预置软链
#   R8b install.sh 重跑  R8c 状态目录软链
#   R9 健康失败 + UI 回滚
#   R9b 备份件被篡改（回滚拒绝执行）
#   R10 数据库恢复失败（manual action）
#
# 失败口径：退出码 1，打印 failed step: <ID> <标题>；每步日志在 --log-dir。
# 退出码 2 = 用法、环境错误，或守卫拒绝。
#
# 签名：除 R2（故意用坏签名）外，普通升级带 --allow-unsigned 显式逃生口；
#   真签名三件套路径由 deploy-baremetal-e2e B4b 覆盖。
#
# 坏件注入（编译期改源码副本，不改仓库）：
#   v2 = 源码 + MIG1（CREATE TABLE，R6 的迁移靶子）；
#   v3 = v2 + main() 起手 panic（R7 的健康靶子；迁移会真的前进一档）；
#   v4 = 源码 + MIG1 + MIG2（R8 的正常升级件）；
#   v5 = v4 + MIG3 + panic（R10：迁移前进、健康失败、恢复失败）；
#   v6 = v4 + panic（R9：迁移集与 v4 相同，migrate 空转 → 只测 UI/二进制回滚）。
#   MIG* 从仓库实测最大迁移号 +1 起，不写死。
#   R6 用 psql 对应用角色 REVOKE CREATE ON SCHEMA public 让真 goose 撞权限错误；
#   R4 用「一跑就失败」的 pg_dump 桩让备份步失败；
#   R10 用只放行 --list 的 pg_restore 包装器让「校验通过、恢复失败」。
#
# 发布门状态：本脚本在 CI 的 update-rollback-e2e job 里自动跑（docker
#   --privileged；跑不起来时该 job 显性打印 SKIP，不当绿）。release-check
#   项 7 仍是 POINTER；演练本身打印 MANUAL 行登记等价性边界。
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

usage() { sed -n '2,86p' "$SELF"; }

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

    # wait_pg <秒>：等到「能真正连上并跑查询」为止。
    # 只看 pg_isready 会撞上 postgres 镜像 entrypoint 的 initdb 临时实例：
    # 它先起一个临时 server（isready 通过），随后关掉再起正式 server，
    # 这中间跑 SQL 会得到 "the database system is shutting down"（实测偶发）。
    wait_pg() {
        local limit="$1"
        local end=$((SECONDS + limit))
        while [ "$SECONDS" -lt "$end" ]; do
            if docker exec "$PG" psql -U postgres -d yiban -tAc 'select 1' >/dev/null 2>&1; then
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
    # 应用角色不是超级用户也不是库属主：REVOKE CREATE 才咬得住（R6 的前提）。
    # CREATEDB 是 U2 数据库回滚的前提（R7 要建 yiban_restore_<ts>）。
    # R5 会临时 NOCREATEDB 再还回去。
    if ! docker run -d --name "$PG" --network "$NET" \
        -e POSTGRES_DB=yiban -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD="$PG_PASSWORD" \
        -v "$VOL:/var/lib/postgresql" "$PG_IMAGE" >>"$E2E_STEP_LOG" 2>&1; then
        e2e_die "启动 PostgreSQL 容器失败"
    fi
    if ! wait_pg 120; then
        docker logs --tail 60 "$PG" >>"$E2E_STEP_LOG" 2>&1 || true
        e2e_die "PostgreSQL 未在 120s 内就绪"
    fi
    # 应用角色开通：登录 + CREATEDB + public 模式 USAGE/CREATE（迁移要建表）。
    cat > "$LOG_DIR/sql-provision.sql" <<EOF
CREATE ROLE yiban LOGIN CREATEDB PASSWORD '$APP_PASSWORD';
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
    e2e_ok "PostgreSQL 18 就绪；应用角色 yiban（非属主）有 public CREATE 与 CREATEDB"

    # ---- H4 构建四组二进制 --------------------------------------------------
    e2e_step "H4" "构建六组二进制"
    BIN_OUT="$LOG_DIR/bin"
    rm -rf "$BIN_OUT"
    mkdir -p "$BIN_OUT"
    EXPECTED_SCHEMA="$(e2e_expected_schema_version "$REPO")"
    [ "$EXPECTED_SCHEMA" -gt 0 ] || e2e_die "server/migrations/postgres 里没有迁移文件"
    # 注入的迁移号必须大于仓库现有最大编号：goose 对重复版本号直接 panic。
    # 编号从仓库实测值推，不写死（新增迁移时演练自动跟上）。
    MIG1=$((EXPECTED_SCHEMA + 1))
    MIG2=$((EXPECTED_SCHEMA + 2))
    MIG3=$((EXPECTED_SCHEMA + 3))
    # v2 = 仓库源码 + MIG1（CREATE TABLE，R6 的迁移靶子）。
    # v3 = v2 + main() 起手 panic（R7 的健康靶子；迁移真的前进一档）。
    # v4 = 仓库源码 + MIG1 + MIG2（R8 的正常升级件）。
    # v5 = v4 + MIG3 + panic（R10：迁移前进、健康失败、恢复失败）。
    # v6 = v4 + panic（R9：迁移集与 v4 相同 → migrate 空转，只测换件后
    #      健康失败时的二进制/UI 回滚，不混测数据库回滚）。
    # 同一 --log-dir 复跑时旧树必须先清：cp -a 对已存在目标会把源嵌进
    # <目标>/server/，挂载的仍是旧源码。
    rm -rf "$LOG_DIR/srv_v2" "$LOG_DIR/srv_v3" "$LOG_DIR/srv_v4" "$LOG_DIR/srv_v5" "$LOG_DIR/srv_v6"
    cp -a "$REPO/server" "$LOG_DIR/srv_v2"
    cat > "$LOG_DIR/srv_v2/migrations/postgres/${MIG1}_e2e_rollback_probe.sql" <<'EOF'
-- +goose Up
-- E-2 回滚演练件：需要 public 模式 CREATE 权限，供 R6 注入 REVOKE 后
-- 走真 goose 路径失败。建出的表同时证明「迁移不随二进制回滚」这一
-- 已登记边界（update.sh 头注）。
CREATE TABLE e2e_rollback_probe (
    id integer PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- +goose Down
DROP TABLE IF EXISTS e2e_rollback_probe;
EOF
    # 注入“启动后不健康”：先睡过 systemctl start，再 panic。
    # Type=simple 的 start 在进程立即退出时会与健康探测竞争；CI 快机上
    # start 可能直接返回失败，R7/R9/R10 的 “healthz failed after swap”
    # 断言就永远等不到。1.5s 让 start 稳定返回 0，健康探测再失败。
    inject_unhealthy_main() {
        local file="$1" msg="$2"
        sed -i 's|^\t"syscall"$|\t"syscall"\n\t"time"|' "$file" \
            || e2e_die "注入 time import 失败"
        sed -i "s|^func main() {$|func main() {\n\ttime.Sleep(1500 * time.Millisecond)\n\tpanic(\"$msg\")|" "$file" \
            || e2e_die "注入 panic 失败"
        grep -q 'time.Sleep(1500 \* time.Millisecond)' "$file" \
            || e2e_die "源码注入未生效（main.go 形状与预期不符）"
    }
    cp -a "$LOG_DIR/srv_v2" "$LOG_DIR/srv_v3"
    inject_unhealthy_main "$LOG_DIR/srv_v3/cmd/server/main.go" \
        "e2e-rollback-e2e: injected unhealthy build"
    grep -q 'injected unhealthy build' "$LOG_DIR/srv_v3/cmd/server/main.go" \
        || e2e_die "v3 源码注入失败（main.go 形状与预期不符）"
    cp -a "$LOG_DIR/srv_v2" "$LOG_DIR/srv_v4"
    cat > "$LOG_DIR/srv_v4/migrations/postgres/${MIG2}_e2e_version_bump.sql" <<'EOF'
-- +goose Up
-- E-2 回滚演练件：R8 正常升级的可应用迁移（无 DDL，靠 SELECT 走通
-- goose 全流程并推高一档版本）。
SELECT 1;

-- +goose Down
SELECT 1;
EOF
    cp -a "$LOG_DIR/srv_v4" "$LOG_DIR/srv_v5"
    cat > "$LOG_DIR/srv_v5/migrations/postgres/${MIG3}_e2e_version_bump.sql" <<'EOF'
-- +goose Up
-- E-2 回滚演练件：R9 的可应用迁移（把版本再推一档，制造「库已前进」）。
SELECT 1;

-- +goose Down
SELECT 1;
EOF
    inject_unhealthy_main "$LOG_DIR/srv_v5/cmd/server/main.go" \
        "e2e-rollback-e2e: injected unhealthy build for restore failure"
    grep -q 'injected unhealthy build for restore failure' "$LOG_DIR/srv_v5/cmd/server/main.go" \
        || e2e_die "v5 源码注入失败（main.go 形状与预期不符）"
    cp -a "$LOG_DIR/srv_v4" "$LOG_DIR/srv_v6"
    inject_unhealthy_main "$LOG_DIR/srv_v6/cmd/server/main.go" \
        "e2e-rollback-e2e: injected unhealthy build for ui rollback"
    grep -q 'injected unhealthy build for ui rollback' "$LOG_DIR/srv_v6/cmd/server/main.go" \
        || e2e_die "v6 源码注入失败（main.go 形状与预期不符）"
    # 嵌套自检：拷贝后的 scratch 树顶层必须是源码根。
    # 挂载点对着顶层；嵌套一层时构建的「新版」就是旧源码（判据静默失效）。
    for d in srv_v2 srv_v3 srv_v4 srv_v5 srv_v6; do
        [ -f "$LOG_DIR/$d/go.mod" ] \
            || e2e_die "$d 顶层缺 go.mod（源码被嵌套拷贝，挂载的不是顶层源码）"
        [ ! -d "$LOG_DIR/$d/server" ] \
            || e2e_die "$d 内出现嵌套 server/ 目录（cp 目标已存在）"
    done
    [ -f "$LOG_DIR/srv_v2/migrations/postgres/${MIG1}_e2e_rollback_probe.sql" ] \
        || e2e_die "srv_v2 顶层缺 ${MIG1} 演练迁移"
    [ -f "$LOG_DIR/srv_v4/migrations/postgres/${MIG2}_e2e_version_bump.sql" ] \
        || e2e_die "srv_v4 顶层缺 ${MIG2} 演练迁移"
    [ -f "$LOG_DIR/srv_v5/migrations/postgres/${MIG3}_e2e_version_bump.sql" ] \
        || e2e_die "srv_v5 顶层缺 ${MIG3} 演练迁移"
    cat > "$LOG_DIR/build-all.sh" <<'EOF'
#!/bin/sh
# 五组二进制一次编译：共享 GOCACHE/GOPATH（挂载卷，跨次运行复用）。
set -eu
export CGO_ENABLED=0 GOCACHE=/tmp/gocache GOPATH=/tmp/gopath
if [ -n "${ROLLBACK_E2E_GOPROXY:-}" ]; then
    export GOPROXY="$ROLLBACK_E2E_GOPROXY"
fi
for v in v1 v2 v3 v4 v5 v6; do
    cd "/src/$v"
    go build -trimpath -ldflags="-s -w" -o "/out/yiban-server-$v" ./cmd/server
    go build -trimpath -ldflags="-s -w" -o "/out/yiban-cli-$v" ./cmd/cli
done
EOF
    if ! docker run --rm \
        -e "ROLLBACK_E2E_GOPROXY=$GOPROXY_ARG" \
        -v "$REPO/server:/src/v1:ro" \
        -v "$LOG_DIR/srv_v2:/src/v2" \
        -v "$LOG_DIR/srv_v3:/src/v3" \
        -v "$LOG_DIR/srv_v4:/src/v4" \
        -v "$LOG_DIR/srv_v5:/src/v5" \
        -v "$LOG_DIR/srv_v6:/src/v6" \
        -v "$BIN_OUT:/out" \
        -v "$LOG_DIR/build-all.sh:/build-all.sh:ro" \
        -v "$GO_CACHE_VOL:/tmp/gocache" \
        -v "$GO_MOD_VOL:/tmp/gopath" \
        -w / \
        "$GO_IMAGE" sh /build-all.sh >>"$E2E_STEP_LOG" 2>&1; then
        e2e_die "Go 构建失败（受限网络请设 ROLLBACK_E2E_GOPROXY；见 H4.log）"
    fi
    for b in yiban-server-v1 yiban-cli-v1 yiban-server-v2 yiban-cli-v2 \
             yiban-server-v3 yiban-cli-v3 yiban-server-v4 yiban-cli-v4 \
             yiban-server-v5 yiban-cli-v5 yiban-server-v6 yiban-cli-v6; do
        [ -x "$BIN_OUT/$b" ] || e2e_die "$b 未产出或不可执行"
    done
    SHA_V1="$(e2e_sha256 "$BIN_OUT/yiban-server-v1")"
    SHA_V2="$(e2e_sha256 "$BIN_OUT/yiban-server-v2")"
    SHA_V3="$(e2e_sha256 "$BIN_OUT/yiban-server-v3")"
    SHA_V4="$(e2e_sha256 "$BIN_OUT/yiban-server-v4")"
    SHA_V5="$(e2e_sha256 "$BIN_OUT/yiban-server-v5")"
    SHA_V6="$(e2e_sha256 "$BIN_OUT/yiban-server-v6")"
    # 六组两两不同：排序去重后的行数必须等于 6。
    UNIQ_SHAS="$(printf '%s\n' "$SHA_V1" "$SHA_V2" "$SHA_V3" "$SHA_V4" "$SHA_V5" "$SHA_V6" | sort -u | wc -l | tr -d '[:space:]')"
    [ "$UNIQ_SHAS" -eq 6 ] || e2e_die "六组 server 二进制 sha256 有重复（期望两两不同，实得 $UNIQ_SHAS）"
    e2e_ok "v1..v6 各产出 server+cli；sha256 两两不同"

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
    # R2 走真签名链：update.sh 从 <repo>/scripts/release/verify.sh 验签。
    docker exec "$OS" mkdir -p /opt/e2e-src/scripts >>"$E2E_STEP_LOG" 2>&1 || e2e_die "创建 scripts 目录失败"
    docker cp "$REPO/scripts/release" "$OS:/opt/e2e-src/scripts/release" >>"$E2E_STEP_LOG" 2>&1 \
        || e2e_die "复制 scripts/release 失败"
    docker cp "$SELF" "$OS:/opt/e2e/scripts/e2e/update-rollback-e2e.sh" >>"$E2E_STEP_LOG" 2>&1 \
        || e2e_die "复制演练脚本失败"
    docker cp "$SELF_DIR/lib/deploy-e2e.sh" "$OS:/opt/e2e/scripts/e2e/lib/deploy-e2e.sh" \
        >>"$E2E_STEP_LOG" 2>&1 || e2e_die "复制演练共用件失败"
    for b in yiban-server-v1 yiban-cli-v1 yiban-server-v2 yiban-cli-v2 \
             yiban-server-v3 yiban-cli-v3 yiban-server-v4 yiban-cli-v4 \
             yiban-server-v5 yiban-cli-v5 yiban-server-v6 yiban-cli-v6; do
        docker cp "$BIN_OUT/$b" "$OS:/opt/e2e-dist/$b" >>"$E2E_STEP_LOG" 2>&1 || e2e_die "复制 $b 失败"
    done
    # R5/R6 的权限注入 SQL：psql 在容器内执行（走 H5 提取的 PG 客户端）。
    cat > "$LOG_DIR/sql-revoke-create.sql" <<'EOF'
REVOKE CREATE ON SCHEMA public FROM yiban;
EOF
    cat > "$LOG_DIR/sql-grant-create.sql" <<'EOF'
GRANT CREATE ON SCHEMA public TO yiban;
EOF
    cat > "$LOG_DIR/sql-revoke-createdb.sql" <<'EOF'
ALTER ROLE yiban NOCREATEDB;
EOF
    cat > "$LOG_DIR/sql-grant-createdb.sql" <<'EOF'
ALTER ROLE yiban CREATEDB;
EOF
    for f in revoke-create grant-create revoke-createdb grant-createdb; do
        docker cp "$LOG_DIR/sql-$f.sql" "$OS:/opt/e2e/sql/$f.sql" >>"$E2E_STEP_LOG" 2>&1 \
            || e2e_die "复制 sql-$f.sql 失败"
    done
    # pg_dump/pg_restore/psql 从 postgres:18 镜像整包提取（tar 单文件中转）：
    # U2 的统一备份要求容器内有版本匹配的 pg_dump 与 pg_restore。
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
        usr/lib/postgresql/${PG_MAJOR}/bin/pg_dump usr/lib/postgresql/${PG_MAJOR}/bin/pg_restore \
        usr/lib/postgresql/${PG_MAJOR}/bin/psql $PG_LIBS" \
        > "$PGTOOLS_TAR"; then
        e2e_die "从 $PG_IMAGE 提取 pg 客户端失败（tar 中转见 $PGTOOLS_TAR）"
    fi
    PGTOOLS_COUNT="$(tar -tf "$PGTOOLS_TAR" | wc -l | tr -d '[:space:]')"
    PGTOOLS_EXPECTED=$((3 + $(printf '%s' "$PG_LIBS" | wc -w)))
    [ "$PGTOOLS_COUNT" -eq "$PGTOOLS_EXPECTED" ] \
        || e2e_die "pg 客户端 tar 应含 $PGTOOLS_EXPECTED 项（3 个客户端 + $((PGTOOLS_EXPECTED - 3)) 个库），实得 $PGTOOLS_COUNT"
    docker cp "$PGTOOLS_TAR" "$OS:/opt/pg18/tools.tar" >>"$E2E_STEP_LOG" 2>&1 || e2e_die "复制 pg 客户端 tar 失败"
    docker exec "$OS" tar -xf /opt/pg18/tools.tar -C /opt/pg18 >>"$E2E_STEP_LOG" 2>&1 \
        || e2e_die "容器内解包 pg 客户端失败"
    docker exec "$OS" chmod -R 755 /opt/pg18 >>"$E2E_STEP_LOG" 2>&1 || e2e_die "pg 客户端授权失败"
    # 包装器落 /usr/local/bin：update.sh 只要求 PATH 里有 pg_dump。
    cat > "$LOG_DIR/wrapper-pg_dump.sh" <<EOF
#!/bin/sh
# E-2 演练：PG ${PG_MAJOR} 客户端运行于 /opt/pg18，带私有库目录。
# argv 与 PGPASSFILE 落到演练日志目录（/opt/e2e-logs）：演练用它钉
# 「DSN 口令不进 argv、连接口令走 pgpass」。
printf 'pg_dump %s pgpass=%s\n' "\$*" "\${PGPASSFILE:-<unset>}" >> /opt/e2e-logs/pg-argv.log 2>/dev/null || true
exec env LD_LIBRARY_PATH=/opt/pg18/lib/x86_64-linux-gnu \
    /opt/pg18/usr/lib/postgresql/${PG_MAJOR}/bin/pg_dump "\$@"
EOF
    cat > "$LOG_DIR/wrapper-pg_restore.sh" <<EOF
#!/bin/sh
# E-2 演练：PG ${PG_MAJOR} 客户端运行于 /opt/pg18，带私有库目录。
printf 'pg_restore %s pgpass=%s\n' "\$*" "\${PGPASSFILE:-<unset>}" >> /opt/e2e-logs/pg-argv.log 2>/dev/null || true
exec env LD_LIBRARY_PATH=/opt/pg18/lib/x86_64-linux-gnu \
    /opt/pg18/usr/lib/postgresql/${PG_MAJOR}/bin/pg_restore "\$@"
EOF
    cat > "$LOG_DIR/wrapper-psql.sh" <<EOF
#!/bin/sh
# E-2 演练：PG ${PG_MAJOR} 客户端运行于 /opt/pg18，带私有库目录。
printf 'psql %s pgpass=%s\n' "\$*" "\${PGPASSFILE:-<unset>}" >> /opt/e2e-logs/pg-argv.log 2>/dev/null || true
exec env LD_LIBRARY_PATH=/opt/pg18/lib/x86_64-linux-gnu \
    /opt/pg18/usr/lib/postgresql/${PG_MAJOR}/bin/psql "\$@"
EOF
    docker cp "$LOG_DIR/wrapper-pg_dump.sh" "$OS:/usr/local/bin/pg_dump" >>"$E2E_STEP_LOG" 2>&1 \
        || e2e_die "安装 pg_dump 包装器失败"
    docker cp "$LOG_DIR/wrapper-pg_restore.sh" "$OS:/usr/local/bin/pg_restore" >>"$E2E_STEP_LOG" 2>&1 \
        || e2e_die "安装 pg_restore 包装器失败"
    docker cp "$LOG_DIR/wrapper-psql.sh" "$OS:/usr/local/bin/psql" >>"$E2E_STEP_LOG" 2>&1 \
        || e2e_die "安装 psql 包装器失败"
    docker exec "$OS" chmod 755 /usr/local/bin/pg_dump /usr/local/bin/pg_restore /usr/local/bin/psql \
        >>"$E2E_STEP_LOG" 2>&1 || e2e_die "包装器授权失败"
    docker exec "$OS" pg_dump --version >>"$E2E_STEP_LOG" 2>&1 || e2e_die "容器内 pg_dump 不可用"
    docker exec "$OS" pg_restore --version >>"$E2E_STEP_LOG" 2>&1 || e2e_die "容器内 pg_restore 不可用"
    docker exec "$OS" psql --version >>"$E2E_STEP_LOG" 2>&1 || e2e_die "容器内 psql 不可用"
    e2e_ok "systemd=$SYSTEMD_STATE；标记已写；物料就位；pg_dump/pg_restore/psql 可用"

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
    e2e_say "CI：deploy-gate.yml 的 update-rollback-e2e job 自动跑「干跑契约 + 本演练 R1..R10」；"
    e2e_say "环境起不来时该 job 显性 SKIP（写进 step summary，不算绿）。"
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

    UPDATE_SH="$SRC/deploy/baremetal/update.sh"
    INSTALL_SH="$SRC/deploy/baremetal/install.sh"
    ENV_EXAMPLE="$SRC/deploy/baremetal/server.env.example"
    SERVER_DEST="/usr/local/bin/yiban-server"
    CLI_DEST="/usr/local/bin/yiban-cli"
    UNIT="yiban.service"
    ENV_FILE="/etc/yiban/server.env"
    UPDATER_ROOT="/var/lib/yiban-updater"
    STATE_DIR="$UPDATER_ROOT/state"
    STATE_FILE="$STATE_DIR/update-state.json"
    # 更新器的 root 独占树：父目录 /var/lib 属 root ⇒ yiban 换不了它；
    # 与 /var/lib/yiban 同级（install.sh 只 chown photos/ui，不碰这里）。
    BACKUP_ROOT="$UPDATER_ROOT/backups"

    # sha <文件>：容器内 sha256sum（coreutils 必有）。
    sha() { sha256sum "$1" | cut -d' ' -f1; }
    # psql_admin <SQL>：超级用户对**原库**执行单条 SQL。
    psql_admin() { psql "$ADMIN_DSN" -v ON_ERROR_STOP=1 -tAc "$1"; }
    # current_app_dsn：当前生效的 DSN（数据库回滚会改 /etc/yiban/server.env）。
    current_app_dsn() {
        { grep -E '^YIBAN_DATABASE_URL=' "$ENV_FILE" || true; } | head -n 1 | cut -d= -f2-
    }
    # 演练自己的 psql 也不把应用角色口令放进 argv：口令进 0600 PGPASSFILE，
    # argv 用去掉口令的 DSN。否则「argv 无口令」的断言会被演练自己打红。
    APP_PW_ONLY="${APP_DSN#*://}"
    APP_PW_ONLY="${APP_PW_ONLY#*:}"
    APP_PW_ONLY="${APP_PW_ONLY%%@*}"
    ( umask 077; printf '*:*:*:*:%s\n' "$APP_PW_ONLY" > /opt/e2e/app.pgpass )
    chmod 600 /opt/e2e/app.pgpass
    export PGPASSFILE=/opt/e2e/app.pgpass
    # psql_app <SQL>：应用角色对**当前库**执行单条 SQL（argv 无口令）。
    psql_app() {
        local dsn rest userpass after user
        dsn="$(current_app_dsn)"
        rest="${dsn#*://}"
        userpass="${rest%%@*}"
        after="${rest#*@}"
        user="${userpass%%:*}"
        psql "postgres://${user}@${after}" -v ON_ERROR_STOP=1 -tAc "$1"
    }
    goose_version() {
        psql_app "select coalesce(max(version_id), 0) from goose_db_version where is_applied" \
            | tr -d '[:space:]'
    }
    # apply_sql <名字>：执行物料里的注入 SQL（超级用户，原库）。
    apply_sql() {
        psql -f "/opt/e2e/sql/$1.sql" "$ADMIN_DSN" >>"$E2E_STEP_LOG" 2>&1 \
            || e2e_die "执行 sql/$1.sql 失败"
    }
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
    # assert_grep <文件> <模式> <说明>：文件里必须出现该模式。
    assert_grep() {
        if ! grep -q -- "$2" "$1"; then
            e2e_tail "$1" 30 || true
            e2e_die "$3（$1 里没有「$2」）"
        fi
    }
    # state_str / state_num：读 update-state.json 的字段（格式由 update.sh 固定）。
    # 管道里的 `|| true` 只为兜住「文件不存在」：本脚本 set -E（errtrace），
    # 管道失败会让 ERR trap 在 $( ) 子 shell 里点火并中止整轮演练。
    state_str() {
        { sed -n "s/^  \"$1\": \"\([^\"]*\)\",\{0,1\}$/\1/p" "$STATE_FILE" 2>/dev/null || true; } | head -n 1
    }
    state_num() {
        { sed -n "s/^  \"$1\": \([0-9][0-9]*\),\{0,1\}$/\1/p" "$STATE_FILE" 2>/dev/null || true; } | head -n 1
    }
    # backup_sets：备份目录个数。
    backup_sets() { { ls -1d "$BACKUP_ROOT"/*/ 2>/dev/null || true; } | wc -l | tr -d '[:space:]'; }

    # ---- R1 基线安装与自检门自证 --------------------------------------------
    e2e_step "R1" "基线安装与自检门自证"
    [ -f /etc/yiban-e2e-container ] || e2e_die "容器标记丢失（守卫被判据钉住，此处为纵深）"
    for f in "$UPDATE_SH" "$INSTALL_SH" "$ENV_EXAMPLE"; do
        [ -f "$f" ] || e2e_die "物料缺失：$f"
    done
    for b in yiban-server-v1 yiban-cli-v1 yiban-server-v2 yiban-cli-v2 \
             yiban-server-v3 yiban-cli-v3 yiban-server-v4 yiban-cli-v4 \
             yiban-server-v5 yiban-cli-v5 yiban-server-v6 yiban-cli-v6; do
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
    HEALTH_ADDR="$(grep -E '^YIBAN_HTTP_ADDR=' /etc/yiban/server.env | head -n 1 | cut -d= -f2- || true)"
    HEALTH_ADDR="${HEALTH_ADDR:-127.0.0.1:47183}"
    SHA_V1="$(sha "$DIST/yiban-server-v1")"
    SHA_V2="$(sha "$DIST/yiban-server-v2")"
    SHA_V3="$(sha "$DIST/yiban-server-v3")"
    SHA_V4="$(sha "$DIST/yiban-server-v4")"
    SHA_V5="$(sha "$DIST/yiban-server-v5")"
    SHA_CLI_V1="$(sha "$DIST/yiban-cli-v1")"
    SHA_CLI_V4="$(sha "$DIST/yiban-cli-v4")"
    assert_eq "安装位 server sha256" "$(sha "$SERVER_DEST")" "$SHA_V1"
    assert_eq "unit 状态" "$(unit_active)" "active"
    if ! health_ok; then e2e_die "基线 healthz 探活失败（$HEALTH_ADDR）"; fi
    assert_eq "基线 schema 版本" "$(goose_version)" "$EXPECTED_SCHEMA"
    if psql_app "select to_regclass('public.e2e_rollback_probe') is not null" | grep -q t; then
        e2e_die "干净库已有 e2e_rollback_probe 表（基线不干净）"
    fi
    # 自检门自证：只读、exit 0、不创建状态目录。
    SELFCHECK_RC=0
    bash "$UPDATE_SH" --selfcheck-only >"$E2E_LOG_DIR/R1-selfcheck.log" 2>&1 || SELFCHECK_RC=$?
    assert_eq "selfcheck-only 退出码" "$SELFCHECK_RC" "0"
    assert_grep "$E2E_LOG_DIR/R1-selfcheck.log" 'selfcheck:' "自检必须打印人读摘要"
    [ ! -e "$STATE_DIR" ] || e2e_die "--selfcheck-only 不得创建状态目录 $STATE_DIR"
    # HIGH 3：老机上留着旧 update.sh 的 /var/lib/yiban/pre-migrate（root 0700）。
    # 自检的 fs.backup_dir 若仍判这个目录就会 FAIL，把所有升级挡在门外。
    install -d -m 0700 /var/lib/yiban/pre-migrate
    chown root:root /var/lib/yiban/pre-migrate
    assert_eq "老机 pre-migrate 存在" "$(stat -c '%a %U' /var/lib/yiban/pre-migrate)" "700 root"
    SELFCHECK_LEGACY_RC=0
    bash "$UPDATE_SH" --selfcheck-only >"$E2E_LOG_DIR/R1-selfcheck-legacy.log" 2>&1 || SELFCHECK_LEGACY_RC=$?
    assert_eq "老机 pre-migrate 在场时 selfcheck-only 退出码" "$SELFCHECK_LEGACY_RC" "0"
    # 再加一遍**不带 --backup-dir** 的明文自检：判的是 U1 的默认值
    # （selfcheck.DefaultBackupDir）。默认值若被改回 root 专属目录，
    # 这里会看到 [FAIL] fs.backup_dir —— 正是要挡的误红。
    # 直接调 CLI 要自己载入 server.env（update.sh 才替它做这件事）。
    SELFCHECK_TEXT_RC=0
    (
        set -a
        # shellcheck disable=SC1091
        . /etc/yiban/server.env
        set +a
        exec "$CLI_DEST" selfcheck
    ) >"$E2E_LOG_DIR/R1-selfcheck-legacy-text.log" 2>&1 || SELFCHECK_TEXT_RC=$?
    assert_eq "老机 pre-migrate 在场时明文自检退出码" "$SELFCHECK_TEXT_RC" "0"
    assert_grep "$E2E_LOG_DIR/R1-selfcheck-legacy-text.log" 'fs.backup_dir' "自检必须列出备份目录判据"
    if grep -q '^\[FAIL\] fs\.backup_dir' "$E2E_LOG_DIR/R1-selfcheck-legacy-text.log"; then
        e2e_die "老机的 /var/lib/yiban/pre-migrate（root 0700）让 fs.backup_dir 误红：备份目录默认值没跟上 U2"
    fi
    e2e_ok "install.sh rc=0；server=v1；unit active；healthz ok；schema=$EXPECTED_SCHEMA；selfcheck-only rc=0 且零写入；老机 pre-migrate 不误红"

    # ---- R2 签名错：验签失败不碰旧件 ----------------------------------------
    e2e_step "R2" "签名错：验签失败不碰旧件、不停服"
    R2_DIR="$E2E_LOG_DIR/r2-signing"
    mkdir -p "$R2_DIR"
    if ! openssl genpkey -algorithm ED25519 -out "$R2_DIR/key.pem" >>"$E2E_STEP_LOG" 2>&1 \
        || ! openssl pkey -in "$R2_DIR/key.pem" -pubout -out "$R2_DIR/pub.pem" >>"$E2E_STEP_LOG" 2>&1; then
        e2e_die "openssl 生成密钥失败"
    fi
    # 清单按基名对账：拷一份叫 yiban-server 的件再签发。
    cp "$DIST/yiban-server-v2" "$R2_DIR/yiban-server"
    chmod 755 "$R2_DIR/yiban-server"
    bash "$SRC/scripts/release/make-manifest.sh" 9.9.9 "$R2_DIR/yiban-server" \
        >"$R2_DIR/manifest.json" 2>>"$E2E_STEP_LOG" || e2e_die "make-manifest.sh 失败"
    bash "$SRC/scripts/release/sign.sh" --key "$R2_DIR/key.pem" \
        --in "$R2_DIR/manifest.json" --out "$R2_DIR/manifest.sig" >>"$E2E_STEP_LOG" 2>&1 \
        || e2e_die "sign.sh 失败"
    # 篡改一个字节：签名覆盖清单字节，验签必须红。
    sed -i 's/9\.9\.9/9.9.8/' "$R2_DIR/manifest.json"
    grep -q '9\.9\.8' "$R2_DIR/manifest.json" || e2e_die "篡改清单失败（版本字面量未命中）"
    R2_RC=0
    YIBAN_RELEASE_PUBKEY_SHA256="$(sha "$R2_DIR/pub.pem")" \
        bash "$UPDATE_SH" --server-bin "$DIST/yiban-server-v2" \
        --manifest "$R2_DIR/manifest.json" --signature "$R2_DIR/manifest.sig" \
        --pubkey "$R2_DIR/pub.pem" >"$E2E_LOG_DIR/R2-update.log" 2>&1 || R2_RC=$?
    assert_eq "R2 update.sh 退出码" "$R2_RC" "1"
    assert_grep "$E2E_LOG_DIR/R2-update.log" 'signing verification FAILED' "必须在验签处失败"
    assert_eq "R2 安装位仍是旧件" "$(sha "$SERVER_DEST")" "$SHA_V1"
    [ ! -e "$SERVER_DEST.bak" ] || e2e_die "R2：验签失败不应产生 .bak"
    [ ! -e "$SERVER_DEST.new" ] || e2e_die "R2：失败路径残留 .new 暂存件"
    [ ! -e "$STATE_DIR" ] || e2e_die "R2：验签失败不应创建状态目录"
    assert_eq "R2 unit 状态" "$(unit_active)" "active"
    e2e_ok "rc=1；安装位不变；无 .bak/.new；无状态目录；unit active"

    # ---- R3 自检 FAIL：不停服、不写状态 -------------------------------------
    e2e_step "R3" "自检 FAIL：不停服、不写状态/备份"
    # 把照片目录改成 root 专属：U1 的 config.photo_dir / fs.photo_dir 按服务用户判权 → FAIL。
    chown -R root:root /var/lib/yiban/photos
    chmod 700 /var/lib/yiban/photos
    R3_RC=0
    bash "$UPDATE_SH" --server-bin "$DIST/yiban-server-v2" --allow-unsigned \
        >"$E2E_LOG_DIR/R3-update.log" 2>&1 || R3_RC=$?
    chown -R yiban:yiban /var/lib/yiban/photos
    chmod 755 /var/lib/yiban/photos
    assert_eq "R3 update.sh 退出码" "$R3_RC" "2"
    assert_grep "$E2E_LOG_DIR/R3-update.log" 'selfcheck FAILED' "必须点名自检门"
    assert_grep "$E2E_LOG_DIR/R3-update.log" 'photo_dir' "必须点名失败的检查项"
    [ ! -e "$STATE_DIR" ] || e2e_die "R3：自检失败不应创建状态目录"
    assert_eq "R3 安装位仍是旧件" "$(sha "$SERVER_DEST")" "$SHA_V1"
    assert_eq "R3 unit 状态" "$(unit_active)" "active"
    if ! health_ok; then e2e_die "R3：服务必须继续可用"; fi
    e2e_ok "rc=2；FAIL 项点名；无状态目录；unit active；healthz ok"

    # ---- R4 备份失败：停服前中止 --------------------------------------------
    e2e_step "R4" "备份失败：停服前中止"
    # 注入口：pg_dump 在运行期失败（备份根已由自检判权，占位文件会先在门里红）。
    # tools.pg_dump 只做 LookPath，所以「找得到但一跑就失败」正好落在备份步。
    mv /usr/local/bin/pg_dump /usr/local/bin/pg_dump.ok
    printf '#!/bin/sh\necho "pg_dump: simulated failure" >&2\nexit 1\n' > /usr/local/bin/pg_dump
    chmod 755 /usr/local/bin/pg_dump
    R4_RC=0
    bash "$UPDATE_SH" --server-bin "$DIST/yiban-server-v2" --cli-bin "$DIST/yiban-cli-v2" \
        --allow-unsigned >"$E2E_LOG_DIR/R4-update.log" 2>&1 || R4_RC=$?
    mv /usr/local/bin/pg_dump.ok /usr/local/bin/pg_dump
    chmod 755 /usr/local/bin/pg_dump
    assert_eq "R4 update.sh 退出码" "$R4_RC" "2"
    assert_grep "$E2E_LOG_DIR/R4-update.log" 'backup FAILED' "必须点名备份失败"
    assert_eq "R4 状态 phase" "$(state_str phase)" "backup"
    assert_eq "R4 状态 result" "$(state_str result)" "aborted"
    grep -q 'service stopped' "$STATE_DIR/update.log" \
        && e2e_die "R4：备份失败不得停服（update.log 出现 service stopped）"
    assert_eq "R4 安装位仍是旧件" "$(sha "$SERVER_DEST")" "$SHA_V1"
    assert_eq "R4 unit 状态" "$(unit_active)" "active"
    assert_eq "R4 无备份目录" "$(backup_sets)" "0"
    e2e_ok "rc=2；phase=backup；未停服；无备份目录；unit active"

    # ---- R5 无 CREATEDB：预检拒绝 -------------------------------------------
    e2e_step "R5" "无 CREATEDB：预检拒绝，不开始"
    apply_sql revoke-createdb
    assert_eq "角色 CREATEDB 已撤销" "$(psql_admin "select rolcreatedb from pg_roles where rolname='yiban'")" "f"
    R5_RC=0
    bash "$UPDATE_SH" --server-bin "$DIST/yiban-server-v2" --cli-bin "$DIST/yiban-cli-v2" \
        --allow-unsigned >"$E2E_LOG_DIR/R5-update.log" 2>&1 || R5_RC=$?
    assert_eq "R5 update.sh 退出码" "$R5_RC" "2"
    assert_grep "$E2E_LOG_DIR/R5-update.log" 'CREATEDB' "必须点名 CREATEDB"
    assert_grep "$E2E_LOG_DIR/R5-update.log" '\-\-no-restore-db' "必须给出逃生口"
    assert_eq "R5 状态 phase" "$(state_str phase)" "preflight"
    assert_eq "R5 无备份目录" "$(backup_sets)" "0"
    assert_eq "R5 unit 状态" "$(unit_active)" "active"
    apply_sql grant-createdb
    assert_eq "角色 CREATEDB 已恢复" "$(psql_admin "select rolcreatedb from pg_roles where rolname='yiban'")" "t"
    e2e_ok "rc=2；phase=preflight；无备份；unit active；CREATEDB 已还原"

    # ---- R6 迁移失败（--no-restore-db）：旧件继续跑 --------------------------
    e2e_step "R6" "迁移失败（--no-restore-db）：旧件原位、库不前进"
    apply_sql revoke-create
    assert_eq "注入后 yiban 的 public CREATE" "$(psql_admin "select has_schema_privilege('yiban','public','CREATE')")" "f"
    PRE_DSN="$(current_app_dsn)"
    R6_RC=0
    bash "$UPDATE_SH" --server-bin "$DIST/yiban-server-v2" --cli-bin "$DIST/yiban-cli-v2" \
        --allow-unsigned --no-restore-db >"$E2E_LOG_DIR/R6-update.log" 2>&1 || R6_RC=$?
    assert_grep "$E2E_LOG_DIR/R6-update.log" 'migrate FAILED' "必须在迁移处失败"
    assert_grep "$E2E_LOG_DIR/R6-update.log" 'permission denied for schema public' \
        "失败原因必须是 schema 权限"
    assert_eq "R6 update.sh 退出码" "$R6_RC" "1"
    assert_eq "R6 安装位仍是旧件" "$(sha "$SERVER_DEST")" "$SHA_V1"
    [ ! -e "$SERVER_DEST.bak" ] || e2e_die "R6：迁移失败不应产生 .bak"
    [ ! -e "$SERVER_DEST.new" ] || e2e_die "R6：失败路径残留 .new 暂存件"
    assert_eq "R6 unit 状态" "$(unit_active)" "active"
    if ! health_ok; then e2e_die "R6：回滚后 healthz 探活失败"; fi
    assert_eq "R6 schema 版本不前进" "$(goose_version)" "$EXPECTED_SCHEMA"
    assert_eq "R6 DSN 不变" "$(current_app_dsn)" "$PRE_DSN"
    assert_eq "R6 状态 db_restore" "$(state_str db_restore)" "disabled"
    assert_eq "R6 状态 exit_code" "$(state_num exit_code)" "1"
    apply_sql grant-create
    assert_eq "归还后 yiban 的 public CREATE" "$(psql_admin "select has_schema_privilege('yiban','public','CREATE')")" "t"
    e2e_ok "rc=1；旧件原位；无 .bak/.new；unit active；schema 不变；DSN 不变"

    # ---- R7 健康失败 + 数据库回滚（默认开启） --------------------------------
    e2e_step "R7" "健康失败 + 数据库回滚：恢复到新库并切 DSN"
    PRE_DSN="$(current_app_dsn)"
    R7_RC=0
    bash "$UPDATE_SH" --server-bin "$DIST/yiban-server-v3" --cli-bin "$DIST/yiban-cli-v3" \
        --allow-unsigned >"$E2E_LOG_DIR/R7-update.log" 2>&1 || R7_RC=$?
    assert_grep "$E2E_LOG_DIR/R7-update.log" 'healthz failed after swap' "必须在健康检查处失败"
    assert_grep "$E2E_LOG_DIR/R7-update.log" 'db restore' "必须走数据库恢复"
    assert_grep "$E2E_LOG_DIR/R7-update.log" 'rolled back' "必须回滚旧件"
    assert_eq "R7 update.sh 退出码" "$R7_RC" "1"
    POST_DSN="$(current_app_dsn)"
    if [ "$POST_DSN" = "$PRE_DSN" ]; then
        e2e_die "R7：迁移后健康失败必须切换 DSN（仍是 $PRE_DSN）"
    fi
    case "$POST_DSN" in
        *yiban_restore_*) ;;
        *) e2e_die "R7：新 DSN 未指向 yiban_restore_<ts>：$POST_DSN" ;;
    esac
    assert_eq "R7 状态 db_restore" "$(state_str db_restore)" "restored"
    RESTORE_TARGET="$(state_str db_restore_target)"
    case "$RESTORE_TARGET" in
        yiban_restore_*) ;;
        *) e2e_die "R7：状态里的 db_restore_target 不对：$RESTORE_TARGET" ;;
    esac
    assert_eq "R7 当前库是新库" "$(psql_app 'select current_database()' | tr -d '[:space:]')" "$RESTORE_TARGET"
    assert_eq "R7 新库 schema = 升级前" "$(goose_version)" "$EXPECTED_SCHEMA"
    # 原库保留且已前进一档（v3 的 00021 真应用了）。
    assert_eq "R7 原库保留并前进一档" \
        "$(psql_admin "select coalesce(max(version_id),0) from goose_db_version where is_applied" | tr -d '[:space:]')" \
        "$((EXPECTED_SCHEMA + 1))"
    # 恢复件可用：业务表可读（不是只有版本表）。
    assert_eq "R7 恢复件 users 可读" "$(psql_app 'select count(*) from users' | tr -d '[:space:]')" "0"
    assert_eq "R7 安装位是旧件" "$(sha "$SERVER_DEST")" "$SHA_V1"
    assert_eq "R7 安装位 cli 回到旧件" "$(sha "$CLI_DEST")" "$SHA_CLI_V1"
    [ ! -e "$SERVER_DEST.bak" ] || e2e_die "R7：.bak 应已换回安装位"
    assert_eq "R7 unit 状态" "$(unit_active)" "active"
    if ! health_ok; then e2e_die "R7：回滚后 healthz 探活失败"; fi
    NEWEST_BACKUP="$({ ls -1d "$BACKUP_ROOT"/*/ 2>/dev/null || true; } | sort | tail -n 1)"
    NEWEST_BACKUP="${NEWEST_BACKUP%/}"
    [ -f "$NEWEST_BACKUP/db.dump" ] || e2e_die "R7：备份缺 db.dump"
    [ -f "$NEWEST_BACKUP/SHA256SUMS" ] || e2e_die "R7：备份缺 SHA256SUMS"
    assert_eq "R7 db.dump 权限" "$(stat -c '%a' "$NEWEST_BACKUP/db.dump")" "600"
    assert_eq "R7 备份目录权限" "$(stat -c '%a' "$NEWEST_BACKUP")" "700"
    e2e_ok "rc=1；DSN → $RESTORE_TARGET；新库 schema=$EXPECTED_SCHEMA；原库保留且 schema=$((EXPECTED_SCHEMA + 1))；unit active"

    # ---- R8 正常升级（在恢复后的新库上，首次装页面） -------------------------
    e2e_step "R8" "正常升级：rc=0、.bak 留旧件、schema 前进两档、首次装页面"
    UI_ONE="/opt/e2e/ui-one"
    install -d -m 0755 "$UI_ONE"
    printf '<div id="root">ui-one</div>\n' > "$UI_ONE/index.html"
    BACKUPS_BEFORE="$(backup_sets)"
    R8_RC=0
    bash "$UPDATE_SH" --server-bin "$DIST/yiban-server-v4" --cli-bin "$DIST/yiban-cli-v4" \
        --ui-dir "$UI_ONE" \
        --allow-unsigned >"$E2E_LOG_DIR/R8-update.log" 2>&1 || R8_RC=$?
    assert_grep "$E2E_LOG_DIR/R8-update.log" 'update complete: healthz ok' "必须报告升级完成"
    assert_eq "R8 update.sh 退出码" "$R8_RC" "0"
    assert_eq "R8 安装位是新件" "$(sha "$SERVER_DEST")" "$SHA_V4"
    assert_eq "R8 安装位 cli 是新件" "$(sha "$CLI_DEST")" "$SHA_CLI_V4"
    [ -e "$SERVER_DEST.bak" ] || e2e_die "R8：升级成功后 .bak 应保留"
    assert_eq "R8 .bak 是升级前旧件" "$(sha "$SERVER_DEST.bak")" "$SHA_V1"
    assert_eq "R8 unit 状态" "$(unit_active)" "active"
    if ! health_ok; then e2e_die "R8：升级后 healthz 探活失败"; fi
    assert_eq "R8 schema 前进两档" "$(goose_version)" "$((EXPECTED_SCHEMA + 2))"
    assert_eq "R8 迁移靶表存在" \
        "$(psql_app "select to_regclass('public.e2e_rollback_probe') is not null" | tr -d '[:space:]')" "t"
    assert_eq "R8 状态 result" "$(state_str result)" "success"
    assert_eq "R8 状态 exit_code" "$(state_num exit_code)" "0"
    assert_eq "R8 状态 db_restore" "$(state_str db_restore)" "not-needed"
    assert_eq "R8 新增一份备份" "$(backup_sets)" "$((BACKUPS_BEFORE + 1))"
    # 红线：状态/日志/自检报告里不得出现 DSN 口令。
    APP_PW="${APP_DSN#*://}"
    APP_PW="${APP_PW#*:}"
    APP_PW="${APP_PW%%@*}"
    if [ -n "$APP_PW" ]; then
        for f in "$STATE_FILE" "$STATE_DIR/update.log" "$STATE_DIR/selfcheck-latest.json" \
                 "$E2E_LOG_DIR"/R*-update.log; do
            [ -f "$f" ] || continue
            if grep -qF -- "$APP_PW" "$f"; then
                e2e_die "证据文件出现 DSN 口令：$f"
            fi
        done
    fi
    assert_eq "R8 状态文件权限" "$(stat -c '%a' "$STATE_FILE")" "600"
    # HIGH 4：pg 客户端 argv 里不得有口令；连接一律走 PGPASSFILE。
    if [ ! -f /opt/e2e-logs/pg-argv.log ]; then
        e2e_die "没有 pg argv 记录（包装器没装上？）"
    fi
    # 日志本来就在 $E2E_LOG_DIR 里（包装器直接写那里），无需再拷。
    grep -qF -- "$APP_PW" /opt/e2e-logs/pg-argv.log \
        && e2e_die "pg 客户端 argv 出现 DSN 口令（见 pg-argv.log）"
    # `--version` 只是探测、不连库，允许没有 PGPASSFILE；真实连接一律要有。
    if grep 'pgpass=<unset>' /opt/e2e-logs/pg-argv.log | grep -v -- ' --version' | grep -q .; then
        grep 'pgpass=<unset>' /opt/e2e-logs/pg-argv.log | grep -v -- ' --version' \
            | head -n 5 | sed 's/^/  /' >&2
        e2e_die "pg 客户端没拿到 PGPASSFILE（口令隔离没接线，见上）"
    fi
    # 备份落位：根与状态目录都在 root 独占父目录下（0700 root），本份也 0700 root。
    NEWEST_R8_BACKUP="$({ ls -1d "$BACKUP_ROOT"/*/ 2>/dev/null || true; } | sort | tail -n 1)"
    NEWEST_R8_BACKUP="${NEWEST_R8_BACKUP%/}"
    assert_eq "R8 更新器父目录权限与属主" "$(stat -c '%a %U' "$UPDATER_ROOT")" "700 root"
    assert_eq "R8 状态目录权限与属主" "$(stat -c '%a %U' "$STATE_DIR")" "700 root"
    assert_eq "R8 备份根权限与属主" "$(stat -c '%a %U' "$BACKUP_ROOT")" "700 root"
    assert_eq "R8 备份子目录属主" "$(stat -c '%U' "$NEWEST_R8_BACKUP")" "root"
    [ -f "$NEWEST_R8_BACKUP/env.backup" ] || e2e_die "R8：备份缺 env.backup"
    assert_eq "R8 env.backup 权限" "$(stat -c '%a' "$NEWEST_R8_BACKUP/env.backup")" "600"
    # 首次装页面：目录与指针都生效。
    grep -q 'ui-one' /var/lib/yiban/ui/index.html || e2e_die "R8：首次装页面没生效"
    assert_eq "R8 YIBAN_UI_DIR 唯一" "$(grep -c '^YIBAN_UI_DIR=' "$ENV_FILE")" "1"
    assert_grep "$ENV_FILE" '^YIBAN_UI_DIR=/var/lib/yiban/ui$' "指针必须指到 /var/lib/yiban/ui"
    e2e_ok "rc=0；.bak=升级前旧件；unit active；healthz ok；schema=$((EXPECTED_SCHEMA + 2))；首次装页面生效；argv 无口令"

    # ---- R8a UI 落位：预置软链不被跟随 ---------------------------------------
    e2e_step "R8a" "UI 落位：服务用户预置的软链不被跟随"
    # 复审 HIGH：服务用户能把 $SERVICE_HOME/ui 预置成软链；root 的 cp/chmod
    # 若顺链走，会把 /root 或旧备份写坏并改权限。落位必须 mv -T 原子替换。
    VICTIM="/opt/e2e/ui-victim"
    rm -rf "$VICTIM"
    # 受害目录对服务用户可读（否则自检的 config.ui_dir 会先把它判 FAIL，
    # 走不到 UI 落位逻辑）；真正要证明的是 cp/chmod 不顺链改文件与权限。
    install -d -m 0755 "$VICTIM"
    printf 'SECRET\n' > "$VICTIM/index.html"
    chmod 0600 "$VICTIM/index.html"
    install -d -m 0700 "$VICTIM/sub"
    printf 'KEEP\n' > "$VICTIM/sub/keep.txt"
    chmod 0600 "$VICTIM/sub/keep.txt"
    UI_THREE="/opt/e2e/ui-three"
    install -d -m 0755 "$UI_THREE"
    printf '<div id="root">ui-three</div>\n' > "$UI_THREE/index.html"
    mv /var/lib/yiban/ui /var/lib/yiban/ui.keep
    ln -s "$VICTIM" /var/lib/yiban/ui
    R8A_RC=0
    bash "$UPDATE_SH" --server-bin "$DIST/yiban-server-v4" --cli-bin "$DIST/yiban-cli-v4" \
        --ui-dir "$UI_THREE" --allow-unsigned >"$E2E_LOG_DIR/R8a-update.log" 2>&1 || R8A_RC=$?
    assert_eq "R8a update.sh 退出码" "$R8A_RC" "0"
    if [ -L /var/lib/yiban/ui ]; then
        e2e_die "R8a：安装位 ui 仍是软链"
    fi
    grep -q 'ui-three' /var/lib/yiban/ui/index.html || e2e_die "R8a：新页面没有落位"
    # 受害目录：字节与权限都不能被动过（cp/chmod 顺链的判据）。
    assert_eq "R8a 软链目标内容未变" "$(cat "$VICTIM/index.html")" "SECRET"
    assert_eq "R8a 软链目标文件权限未变" "$(stat -c '%a' "$VICTIM/index.html")" "600"
    assert_eq "R8a 软链目标子文件未变" "$(cat "$VICTIM/sub/keep.txt")" "KEEP"
    assert_eq "R8a 软链目标子目录权限未变" "$(stat -c '%a' "$VICTIM/sub")" "700"
    # 恢复 R8 装的那份页面，供 R9 的 UI 回滚断言使用。
    rm -rf /var/lib/yiban/ui
    mv /var/lib/yiban/ui.keep /var/lib/yiban/ui
    assert_eq "R8a 恢复后 unit 状态" "$(unit_active)" "active"
    if ! health_ok; then e2e_die "R8a：恢复页面后 healthz 应仍 OK"; fi
    e2e_ok "rc=0；软链目标字节/权限未变；安装位是真实目录；页面=ui-three"

    # ---- R8b install.sh 重跑：更新器根仍是 root 独占 -------------------------
    e2e_step "R8b" "install.sh 重跑：更新器根仍是 root 独占"
    # install.sh 只把 home 与 photos/ui 交给服务用户（不再递归整棵树）。
    # 更新器的 state/backups 与它同级、本就不可达，重装后仍是 root 0700。
    R8B_INSTALL_RC=0
    YIBAN_HEALTH_RETRIES=60 YIBAN_HEALTH_RETRY_INTERVAL=1 \
        bash "$INSTALL_SH" --server-bin "$DIST/yiban-server-v4" \
        --cli-bin "$DIST/yiban-cli-v4" --ui-dir "$UI_ONE" \
        --database-url "$(current_app_dsn)" \
        >"$E2E_LOG_DIR/R8b-install.log" 2>&1 || R8B_INSTALL_RC=$?
    if [ "$R8B_INSTALL_RC" -ne 0 ]; then
        tail -n 20 "$E2E_LOG_DIR/R8b-install.log" >>"$E2E_STEP_LOG" 2>&1 || true
        e2e_die "R8b：install.sh 重跑失败（rc=$R8B_INSTALL_RC）"
    fi
    assert_grep "$E2E_LOG_DIR/R8b-install.log" 'install complete: healthz ok' "重装后必须健康"
    # 同一 --ui-dir 连跑两次（R8 一次、R8b 一次）都要成功：目标已是非空
    # 真实目录时 mv -T 会 ENOTEMPTY，必须先换到 .bak（复审七轮 HIGH）。
    grep -q 'ui-one' /var/lib/yiban/ui/index.html || e2e_die "R8b：重装后页面不对"
    [ -e /var/lib/yiban/ui.bak/index.html ] || e2e_die "R8b：重装后应留下 ui.bak（上一份页面）"
    for d in "$UPDATER_ROOT" "$STATE_DIR" "$BACKUP_ROOT"; do
        assert_eq "R8b $(basename "$d") 权限与属主" "$(stat -c '%a %U' "$d")" "700 root"
    done
    assert_eq "R8b 服务用户写不了更新器根" \
        "$(setpriv --reuid=yiban --regid=yiban --clear-groups sh -c 'test -w /var/lib/yiban-updater && echo yes || echo no')" "no"
    assert_eq "R8b 备份件仍属 root" "$(stat -c '%U' "$NEWEST_R8_BACKUP/db.dump")" "root"
    # install.sh 只 chown photos/ui（必要时 home 目录本身），不再递归整棵树：
    # 旧备份目录与 U1 的密钥指纹状态必须保持 root 属主。
    assert_eq "R8b 旧 pre-migrate 仍 root 0700"         "$(stat -c '%a %U' /var/lib/yiban/pre-migrate)" "700 root"
    assert_eq "R8b 旧 pre-migrate 里无服务用户可读件"         "$(setpriv --reuid=yiban --regid=yiban --clear-groups sh -c 'test -r /var/lib/yiban/pre-migrate && echo yes || echo no')" "no"
    assert_eq "R8b U1 密钥状态仍属 root" "$(stat -c '%U' /var/lib/yiban/update-state.json)" "root"
    R8B_SELF_RC=0
    bash "$UPDATE_SH" --selfcheck-only >"$E2E_LOG_DIR/R8b-selfcheck.log" 2>&1 || R8B_SELF_RC=$?
    assert_eq "R8b 重装后 selfcheck-only 退出码" "$R8B_SELF_RC" "0"
    e2e_ok "install.sh 重跑 rc=0；更新器 root/state/backups 仍 0700 root；服务用户不可写；selfcheck-only rc=0"

    # ---- R8c 状态目录被换成软链：拒绝写入 -----------------------------------
    e2e_step "R8c" "状态目录被换成软链：写入前拒绝"
    EVIL_TARGET="/tmp/yiban-evil-state"
    rm -rf "$EVIL_TARGET"
    install -d -m 0755 "$EVIL_TARGET"
    mv "$STATE_DIR" "$UPDATER_ROOT/state.real"
    ln -s "$EVIL_TARGET" "$STATE_DIR"
    R8C_RC=0
    bash "$UPDATE_SH" --server-bin "$DIST/yiban-server-v4" --cli-bin "$DIST/yiban-cli-v4" \
        --allow-unsigned >"$E2E_LOG_DIR/R8c-update.log" 2>&1 || R8C_RC=$?
    rm -f "$STATE_DIR"
    mv "$UPDATER_ROOT/state.real" "$STATE_DIR"
    assert_eq "R8c update.sh 退出码" "$R8C_RC" "2"
    assert_grep "$E2E_LOG_DIR/R8c-update.log" 'symlinked updater directory' "必须点名软链"
    if [ -e "$EVIL_TARGET/update.log" ] || [ -e "$EVIL_TARGET/update-state.json" ]; then
        e2e_die "R8c：root 顺着软链写了状态文件（$EVIL_TARGET）"
    fi
    assert_eq "R8c 修复后 unit 状态" "$(unit_active)" "active"
    if ! health_ok; then e2e_die "R8c：服务必须继续可用"; fi
    R8C_SELF_RC=0
    bash "$UPDATE_SH" --selfcheck-only >"$E2E_LOG_DIR/R8c-selfcheck.log" 2>&1 || R8C_SELF_RC=$?
    assert_eq "R8c 修复后 selfcheck-only 退出码" "$R8C_SELF_RC" "0"
    e2e_ok "rc=2；未顺着软链写；修复后 unit active、healthz ok、selfcheck-only rc=0"

    # ---- R9 健康失败 + UI 回滚：旧页面换回 -----------------------------------
    e2e_step "R9" "健康失败 + UI 回滚：旧页面换回、.bak 不残留"
    UI_TWO="/opt/e2e/ui-two"
    install -d -m 0755 "$UI_TWO"
    printf '<div id="root">ui-two</div>\n' > "$UI_TWO/index.html"
    PRE_DSN="$(current_app_dsn)"
    R9_RC=0
    bash "$UPDATE_SH" --server-bin "$DIST/yiban-server-v6" --cli-bin "$DIST/yiban-cli-v6" \
        --ui-dir "$UI_TWO" \
        --allow-unsigned >"$E2E_LOG_DIR/R9-update.log" 2>&1 || R9_RC=$?
    assert_grep "$E2E_LOG_DIR/R9-update.log" 'healthz failed after swap' "必须在健康检查处失败"
    assert_grep "$E2E_LOG_DIR/R9-update.log" 'rolled back' "必须回滚旧件"
    assert_eq "R9 update.sh 退出码" "$R9_RC" "1"
    assert_eq "R9 安装位回到旧件" "$(sha "$SERVER_DEST")" "$SHA_V4"
    [ ! -e "$SERVER_DEST.bak" ] || e2e_die "R9：.bak 应已换回安装位"
    [ ! -e /var/lib/yiban/ui.bak ] || e2e_die "R9：ui.bak 应已换回安装位"
    grep -q 'ui-one' /var/lib/yiban/ui/index.html \
        || e2e_die "R9：回滚后页面必须是 ui-one（旧件配新页面是禁止的混装）"
    assert_eq "R9 YIBAN_UI_DIR 唯一" "$(grep -c '^YIBAN_UI_DIR=' "$ENV_FILE")" "1"
    assert_grep "$ENV_FILE" '^YIBAN_UI_DIR=/var/lib/yiban/ui$' "指针必须指回 /var/lib/yiban/ui"
    assert_eq "R9 DSN 不变（migrate 空转，无需回库）" "$(current_app_dsn)" "$PRE_DSN"
    assert_eq "R9 状态 db_restore" "$(state_str db_restore)" "not-needed"
    assert_eq "R9 状态 exit_code" "$(state_num exit_code)" "1"
    assert_eq "R9 unit 状态" "$(unit_active)" "active"
    if ! health_ok; then e2e_die "R9：回滚后 healthz 探活失败"; fi
    e2e_ok "rc=1；UI 回到 ui-one；无 ui.bak；unit active；healthz ok"

    # ---- R9b 备份件被篡改：回滚拒绝执行备份里的 CLI -------------------------
    e2e_step "R9b" "备份件被篡改：回滚拒绝执行备份里的 CLI"
    # 复审 CRITICAL：备份根已 root:root 0700，这里再演一层「万一权限被放开」。
    # 在健康探活窗口里反复把本轮备份内的 CLI 改成组/其他可写：回滚必须拒绝安装它。
    # 用 watcher 而不是单次 chmod：进程可能在备份件刚出现时就已进入回滚，
    # 单次篡改会漏；watcher 一直把权限按在 0666，直到本轮 update 结束。
    R9B_LOG="$E2E_LOG_DIR/R9b-update.log"
    R9B_BEFORE="$({ ls -1d "$BACKUP_ROOT"/*/ 2>/dev/null || true; })"
    (
        YIBAN_HEALTH_RETRIES=8 YIBAN_HEALTH_RETRY_INTERVAL=1             bash "$UPDATE_SH" --server-bin "$DIST/yiban-server-v6"             --cli-bin "$DIST/yiban-cli-v6" --allow-unsigned >"$R9B_LOG" 2>&1
    ) &
    R9B_PID=$!
    R9B_TAMPER_LOG="$E2E_LOG_DIR/R9b-tamper.log"
    : >"$R9B_TAMPER_LOG"
    (
        for _ in $(seq 1 300); do
            for d in "$BACKUP_ROOT"/*/; do
                [ -d "$d" ] || continue
                case $'
'"$R9B_BEFORE"$'
' in
                    *$'
'"$d"$'
'*) continue ;;
                esac
                if [ -f "${d}bin/yiban-cli" ]; then
                    chmod 0666 "${d}bin/yiban-cli" 2>/dev/null || true
                    printf '%s
' "${d}bin/yiban-cli" >>"$R9B_TAMPER_LOG"
                    break
                fi
            done
            sleep 0.1
        done
    ) &
    R9B_WATCH_PID=$!
    R9B_RC=0
    wait "$R9B_PID" || R9B_RC=$?
    kill "$R9B_WATCH_PID" 2>/dev/null || true
    wait "$R9B_WATCH_PID" 2>/dev/null || true
    if [ ! -s "$R9B_TAMPER_LOG" ]; then
        e2e_die "R9b：等不到备份里的 CLI（$BACKUP_ROOT/*/bin/yiban-cli）"
    fi
    TAMPER_TARGET="$(head -n 1 "$R9B_TAMPER_LOG")"
    e2e_say "R9b: tampered ${TAMPER_TARGET} ($(stat -c '%a %U' "$TAMPER_TARGET" 2>/dev/null || echo gone))"
    assert_eq "R9b update.sh 退出码" "$R9B_RC" "2"
    assert_grep "$R9B_LOG" 'refusing to install the backup cli' "必须拒绝安装被篡改的备份 CLI"
    assert_grep "$R9B_LOG" 'writable by group or others' "拒绝原因必须点名权限被放开"
    assert_grep "$R9B_LOG" 'manual action required' "必须打印 manual action"
    # 被篡改的那份没有被装进安装位：安装位 CLI 仍是本轮换上的 v6 件。
    assert_eq "R9b 安装位 CLI 未被篡改件替换" \
        "$(sha "$CLI_DEST")" "$(sha "$DIST/yiban-cli-v6")"
    assert_eq "R9b 安装位 server 已换回旧件" "$(sha "$SERVER_DEST")" "$SHA_V4"
    assert_eq "R9b 状态 exit_code" "$(state_num exit_code)" "2"
    # MEDIUM-1 起：CLI 校验失败不提前退出，回滚继续走完（DB/UI/起旧件），
    # 旧二进制在跑但 CLI 没回来 ⇒ 仍按 exit 2 + manual action 收口。
    assert_eq "R9b 旧件已起（CLI 未回滚）" "$(unit_active)" "active"
    if ! health_ok; then e2e_die "R9b：旧件必须健康（只有 CLI 没回滚）"; fi
    assert_grep "$R9B_LOG" 'backup cli was NOT restored' "必须点明 CLI 未回滚"
    e2e_ok "rc=2；拒绝执行被篡改的备份 CLI；旧件在跑、CLI 未回滚（manual action）"

    # ---- R10 数据库恢复失败：manual action、不启动旧件 -----------------------
    e2e_step "R10" "数据库恢复失败：exit 2 + manual action，服务停住"
    # R9b 的回滚会继续走完（旧件已起，只是 CLI 没换回）。这里仍先确保服务在跑，
    # 让 R10 的门（service.yiban）有健康的升级前状态。
    systemctl start "$UNIT" >/dev/null 2>&1 || e2e_die "R10：起服务失败"
    assert_eq "R10 前置 unit 状态" "$(unit_active)" "active"
    # 只放行 --list 的 pg_restore 包装器：备份校验通过、真恢复必失败。
    REAL_RESTORE="$(ls /opt/pg18/usr/lib/postgresql/*/bin/pg_restore | head -n 1)"
    [ -x "$REAL_RESTORE" ] || e2e_die "找不到真实 pg_restore（$REAL_RESTORE）"
    cat > /usr/local/bin/pg_restore <<EOF
#!/bin/sh
# E-2 演练：--list 走真件（备份校验），真恢复一律失败（模拟磁盘满/转储损坏）。
for a in "\$@"; do
    case "\$a" in --list) exec env LD_LIBRARY_PATH=/opt/pg18/lib/x86_64-linux-gnu "$REAL_RESTORE" "\$@" ;; esac
done
echo "pg_restore: error: simulated restore failure" >&2
exit 1
EOF
    chmod 755 /usr/local/bin/pg_restore
    PRE_DSN="$(current_app_dsn)"
    R10_RC=0
    bash "$UPDATE_SH" --server-bin "$DIST/yiban-server-v5" --cli-bin "$DIST/yiban-cli-v5" \
        --allow-unsigned >"$E2E_LOG_DIR/R10-update.log" 2>&1 || R10_RC=$?
    assert_eq "R10 update.sh 退出码" "$R10_RC" "2"
    assert_grep "$E2E_LOG_DIR/R10-update.log" 'db restore FAILED' "必须点名恢复失败"
    assert_grep "$E2E_LOG_DIR/R10-update.log" 'manual action required' "必须打印 manual action"
    assert_eq "R10 状态 db_restore" "$(state_str db_restore)" "failed"
    assert_eq "R10 状态 exit_code" "$(state_num exit_code)" "2"
    # MEDIUM 6：CREATE DATABASE 已成功 → 目标名必须记下，manual action 给真名。
    R10_TARGET="$(state_str db_restore_target)"
    case "$R10_TARGET" in
        yiban_restore_*) ;;
        *) e2e_die "R10：db_restore_target 必须是真库名，got '$R10_TARGET'" ;;
    esac
    assert_grep "$E2E_LOG_DIR/R10-update.log" "$R10_TARGET" "manual action 必须点出真实目标库"
    assert_eq "R10 DSN 未改" "$(current_app_dsn)" "$PRE_DSN"
    assert_eq "R10 二进制已换回旧件" "$(sha "$SERVER_DEST")" "$SHA_V4"
    # 库状态未知：不自动起旧件（stop 之后不得再有 start）。
    assert_eq "R10 unit 未启动" "$(unit_active)" "inactive"
    journalctl -u "$UNIT" --no-pager -n 20 >>"$E2E_STEP_LOG" 2>&1 || true
    e2e_ok "rc=2；db_restore=failed（target=$R10_TARGET）；manual action 给真名；DSN 不变；unit inactive（人工处置）"

    e2e_say ""
    e2e_say "等价性登记：真 systemd + 真 PostgreSQL 18 + 真 goose 迁移 + 真 update.sh 自检/备份/回滚；"
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
