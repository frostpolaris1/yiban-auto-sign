#!/usr/bin/env bash
# ============================================================
# deploy/baremetal/install.sh — Go 服务端＋React 前端的裸机安装器（systemd 宿主）。
#
# 前提：PostgreSQL 18 已就绪且可连（建库不在本脚本职责内；
# 容器形态走 deploy/container，不用本脚本）。
#
# 用法：
#   sudo bash deploy/baremetal/install.sh \
#     --server-bin ./dist/yiban-server --cli-bin ./dist/yiban-cli \
#     --ui-dir ./ui/dist \
#     [--database-url postgres://...]
#   DSN 缺省读环境变量 YIBAN_DATABASE_URL。
#   --ui-dir 给前端构建产物目录（ui 目录执行 npm run build 得到 dist）。
#   不给时只装 API：首页不可用，安装器会显式提示。
#
# 干跑钩子（e2e 用，两个都给才是完整干跑）：
#   YIBAN_INSTALL_ROOT=/tmp/prefix   所有文件路径加该前缀；
#                                    同时跳过服务用户与 chown（无特权）。
#   SYSTEMCTL=/path/fake-systemctl   覆盖 systemctl 调用（记录用）。
#
# 步骤：依赖检查 → 旧布局保护（仅存旧 /etc/yiban/.env 时 exit 2，给迁移步骤）
#       → 服务用户 → 二进制落位 → /etc/yiban/server.env
#       （缺则生成随机 pepper/credential key 并打印一次）
#       → /etc/yiban/server.env.example（0644，无密钥的运行模板）
#       → 前端产物落位（给了 --ui-dir 时）
#       → systemd 单元（加固：只开照片目录可写）→ enable --now → /healthz 重试探测。
#
# 退出码：0 成功；1 健康检查超时或前端产物落位失败（可查日志定位）；
#         2 用法错误或依赖缺失（含旧布局保护：仅存 /etc/yiban/.env 时拒装）。
# ============================================================
set -euo pipefail

ROOT="${YIBAN_INSTALL_ROOT:-}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
UNIT="yiban.service"
SERVICE_USER="yiban"
SERVICE_HOME="/var/lib/yiban"

# 仓库根由脚本自身位置推出：运行配置模板取 <repo>/deploy/baremetal/server.env.example。
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

SERVER_BIN=""
CLI_BIN=""
DSN_FLAG=""
UI_DIR=""
while [ $# -gt 0 ]; do
    case "$1" in
        --server-bin) SERVER_BIN="${2:?}"; shift 2 ;;
        --cli-bin) CLI_BIN="${2:?}"; shift 2 ;;
        --database-url) DSN_FLAG="${2:?}"; shift 2 ;;
        --ui-dir) UI_DIR="${2:?}"; shift 2 ;;
        *) echo "install: unknown argument: $1" >&2; exit 2 ;;
    esac
done

[ -n "$SERVER_BIN" ] || { echo "install: --server-bin is required" >&2; exit 2; }
[ -n "$CLI_BIN" ] || { echo "install: --cli-bin is required" >&2; exit 2; }
[ -x "$SERVER_BIN" ] || { echo "install: server binary missing or not executable: $SERVER_BIN" >&2; exit 2; }
[ -x "$CLI_BIN" ] || { echo "install: cli binary missing or not executable: $CLI_BIN" >&2; exit 2; }
ENV_EXAMPLE="${ENV_EXAMPLE:-$REPO_ROOT/deploy/baremetal/server.env.example}"
[ -f "$ENV_EXAMPLE" ] || { echo "install: server.env template missing: $ENV_EXAMPLE" >&2; exit 2; }
DATABASE_URL="${DSN_FLAG:-${YIBAN_DATABASE_URL:-}}"
[ -n "$DATABASE_URL" ] || { echo "install: --database-url or YIBAN_DATABASE_URL is required" >&2; exit 2; }
command -v openssl >/dev/null 2>&1 || { echo "install: openssl is required" >&2; exit 2; }
if [ -n "$UI_DIR" ]; then
    [ -d "$UI_DIR" ] || { echo "install: --ui-dir is not a directory: $UI_DIR" >&2; exit 2; }
    [ -f "$UI_DIR/index.html" ] \
        || { echo "install: --ui-dir has no index.html: $UI_DIR" >&2; exit 2; }
fi

# dest 把系统绝对路径映射到干跑前缀下。
dest() { printf '%s%s' "$ROOT" "$1"; }

# 0) 旧布局保护：只有旧名 /etc/yiban/.env、没有 server.env 时拒绝安装。
#    此时静默生成 server.env 会产生**新的** YIBAN_HASH_PEPPER / YIBAN_CREDENTIAL_KEY：
#    旧钥加密的凭据将永远无法解密（数据丢失）。fail-closed 并给出迁移步骤；
#    密钥只复制、不重生成，老部署升级后照常启动。
#    server.env 已存在时不算旧布局（新栈已装），照常走幂等安装。
LEGACY_ENV="$(dest /etc/yiban/.env)"
NEW_ENV="$(dest /etc/yiban/server.env)"
if [ -f "$LEGACY_ENV" ] && [ ! -f "$NEW_ENV" ]; then
    echo "install: found legacy $LEGACY_ENV without $NEW_ENV" >&2
    echo "install: migrate the old runtime file first (secrets are preserved, NOT regenerated):" >&2
    echo "install:   sudo install -m 0600 -o root -g root /etc/yiban/.env /etc/yiban/server.env" >&2
    echo "install:   sudo rm -f /etc/yiban/.env" >&2
    echo "install:   then re-run install.sh (it keeps an existing server.env in place)" >&2
    echo "install: refusing: generating a fresh server.env here would create NEW pepper/credential keys" >&2
    echo "install:          and make existing credentials permanently undecryptable" >&2
    exit 2
fi

# 1) 服务用户与运行目录。干跑（有前缀）时跳过：本机可能没有 yiban 用户。
if [ -z "$ROOT" ]; then
    id "$SERVICE_USER" >/dev/null 2>&1 \
        || useradd --system --home "$SERVICE_HOME" --shell /usr/sbin/nologin "$SERVICE_USER"
    mkdir -p "$SERVICE_HOME/photos"
    # 只把服务用户真正要写的子树交给它：photos 与（下面步骤里的）ui。
    # **不**做 chown -R "$SERVICE_HOME"：那里还可能有旧版本留下的
    # pre-migrate/backups/updates 备份（含凭据密文）与 U1 的密钥指纹状态，
    # 递归 chown 会把它们改成服务用户可读（复审 MEDIUM-1）。
    chown "$SERVICE_USER:$SERVICE_USER" "$SERVICE_HOME"
    chown -R "$SERVICE_USER:$SERVICE_USER" "$SERVICE_HOME/photos"
    # 更新器的状态/备份根（/var/lib/yiban-updater/{state,backups}）与
    # $SERVICE_HOME 同级，**不**在任何 chown 的范围内：那里是 root 独占目录
    # （备份含凭据密文，回滚还会执行备份里的 CLI）。重装时若它已存在，
    # 顺手把属主钉回 root——防有人手工 chown -R 到服务用户后更新器拒绝运行。
    UPDATER_ROOT="/var/lib/yiban-updater"
    if [ -d "$UPDATER_ROOT" ]; then
        chown root:root "$UPDATER_ROOT"
        chmod 700 "$UPDATER_ROOT"
    fi
else
    echo "install: dry-run (YIBAN_INSTALL_ROOT set): skip service user and chown"
    mkdir -p "$(dest "$SERVICE_HOME/photos")"
fi

mkdir -p "$(dest /usr/local/bin)" "$(dest /etc/yiban)" "$(dest /etc/systemd/system)"

# 2) 二进制落位。
install -m 0755 "$SERVER_BIN" "$(dest /usr/local/bin/yiban-server)"
install -m 0755 "$CLI_BIN" "$(dest /usr/local/bin/yiban-cli)"

# 3) /etc/yiban/server.env：缺则生成。0600 root:root —— systemd 以 root 读它，
#    服务进程经 EnvironmentFile 拿到值，yiban 用户无需可读。
#    同时落一份无密钥模板 server.env.example（0644）：运维照键面补配置，
#    不必猜键名。模板不含任何真实值。
install -m 0644 "$ENV_EXAMPLE" "$(dest /etc/yiban/server.env.example)"
if [ -z "$ROOT" ]; then
    chown root:root "$(dest /etc/yiban/server.env.example)"
fi
ENV_FILE="$(dest /etc/yiban/server.env)"
GENERATED=0
if [ ! -f "$ENV_FILE" ]; then
    GENERATED=1
    umask 077
    cat > "$ENV_FILE" <<EOF
YIBAN_ENV=production
YIBAN_DATABASE_URL=$DATABASE_URL
YIBAN_HTTP_ADDR=127.0.0.1:47183
YIBAN_TZ=Asia/Shanghai
YIBAN_PHOTO_DIR=$SERVICE_HOME/photos
YIBAN_TRUSTED_PROXIES=127.0.0.1
YIBAN_HASH_PEPPER=$(openssl rand -hex 32)
YIBAN_CREDENTIAL_KEY=$(openssl rand -base64 32)
EOF
else
    echo "install: $ENV_FILE exists; keeping it (secrets are never regenerated in place)"
fi

# 已有 server.env 也必须收紧：密钥只给 root 读。
# 干跑（有前缀）时跳过 chown：非特权用户改不了属主。
chmod 600 "$ENV_FILE"
if [ -z "$ROOT" ]; then
    chown root:root "$ENV_FILE"
else
    echo "install: dry-run: skip chown root:root on $ENV_FILE"
fi

# 3b) 前端产物落位（给了 --ui-dir 时）。
# 目标是运行期真实路径；干跑只把文件放到前缀下，server.env 写的仍是真实路径，
# 与 systemd 单元同一口径。
# 落位方式：在 root 独占的更新器根里造好完整目录，再 mv -T 原子替换目标。
# 不能就地 cp -R/chmod：$SERVICE_HOME/ui 可能是服务用户预置的软链，root 会
# 顺链写到 /root 或旧备份并改坏其权限（复审 HIGH）。mv -T 替换的是链接本身。
UI_TARGET="$(dest "$SERVICE_HOME/ui")"
UI_PARENT="$(dest "$SERVICE_HOME")"
UI_ROOT="$(dest /var/lib/yiban-updater)"
if [ -n "$UI_DIR" ]; then
    if [ -L "$UI_PARENT" ]; then
        echo "install: refusing symlinked $UI_PARENT" >&2
        exit 1
    fi
    install -d -m 0700 "$UI_ROOT"
    if [ -z "$ROOT" ]; then
        chown root:root "$UI_ROOT"
        chmod 0700 "$UI_ROOT"
    fi
    UI_STAGE="$(mktemp -d "$UI_ROOT/ui.XXXXXX")" || {
        echo "install: cannot create a private ui staging directory" >&2
        exit 1
    }
    chmod 0700 "$UI_STAGE"
    if ! cp -R "$UI_DIR/." "$UI_STAGE/"; then
        rm -rf -- "$UI_STAGE"
        echo "install: cannot copy --ui-dir into the staging directory" >&2
        exit 1
    fi
    chmod -R a+rX "$UI_STAGE"
    if [ -z "$ROOT" ]; then
        chown -R "$SERVICE_USER:$SERVICE_USER" "$UI_STAGE"
    fi
    # 预置的软链不是合法页面目录：先摘链接（mv -T 不覆盖软链）。
    if [ -L "$UI_TARGET" ]; then
        if ! rm -f -- "$UI_TARGET"; then
            rm -rf -- "$UI_STAGE"
            echo "install: cannot remove the planted ui symlink: $UI_TARGET" >&2
            exit 1
        fi
    fi
    # 目标已存在（重装）时 mv -T 不会覆盖非空目录（ENOTEMPTY）：
    # 与 update.sh 同序——先清旧 .bak，把旧目录换过去，再换入新目录；
    # 换入失败就把 .bak 换回来（旧页面继续服务）。
    UI_BAK="$UI_TARGET.bak"
    if ! rm -rf -- "$UI_BAK"; then
        rm -rf -- "$UI_STAGE"
        echo "install: cannot clear $UI_BAK" >&2
        exit 1
    fi
    UI_OLD_MOVED=0
    if [ -e "$UI_TARGET" ]; then
        if ! mv -T "$UI_TARGET" "$UI_BAK"; then
            rm -rf -- "$UI_STAGE"
            echo "install: cannot move the current ui aside: $UI_TARGET" >&2
            exit 1
        fi
        UI_OLD_MOVED=1
    fi
    if ! mv -T "$UI_STAGE" "$UI_TARGET"; then
        rm -rf -- "$UI_STAGE"
        if [ "$UI_OLD_MOVED" -eq 1 ]; then
            # 目标可能被非空目录占住（ENOTEMPTY）：先清再换回，别让 restore
            # 静默失败（复审 M3）。换回失败必须点名 manual action。
            rm -rf -- "$UI_TARGET" 2>/dev/null || true
            if mv -T "$UI_BAK" "$UI_TARGET"; then
                echo "install: cannot activate the new ui; restored the previous directory" >&2
            else
                echo "install: MANUAL ACTION — cannot activate the new ui and restoring failed; the previous page is at $UI_BAK (mv -T $UI_BAK $UI_TARGET)" >&2
            fi
        else
            echo "install: cannot activate the ui at $UI_TARGET (no previous page to restore)" >&2
        fi
        exit 1
    fi
    # 幂等写键：删旧行再追加，不重排 server.env 的其余行。
    # server.env 刚创建或已收紧权限，读不到即中止：不能把它截断成只剩一行。
    # umask 077 让临时文件从创建那一刻就是 0600：密钥没有可读窗口。
    [ -r "$ENV_FILE" ] || { echo "install: cannot read $ENV_FILE" >&2; exit 1; }
    TMP_ENV="$ENV_FILE.tmp.$$"
    (
        umask 077
        sed '/^YIBAN_UI_DIR=/d' "$ENV_FILE" > "$TMP_ENV"
        printf 'YIBAN_UI_DIR=%s\n' "$SERVICE_HOME/ui" >> "$TMP_ENV"
    )
    chmod 600 "$TMP_ENV"
    if [ -z "$ROOT" ]; then
        chown root:root "$TMP_ENV"
    fi
    mv "$TMP_ENV" "$ENV_FILE"
    echo "install: ui installed at $SERVICE_HOME/ui"
else
    echo "install: --ui-dir not given: API only (no page at /)"
fi

# 生成的密钥只在这里打印一次；丢了就只能重新生成并承担轮换口径。
if [ "$GENERATED" -eq 1 ]; then
    echo "install: generated secrets (shown ONCE, store them elsewhere):"
    grep -E '^YIBAN_(HASH_PEPPER|CREDENTIAL_KEY)=' "$ENV_FILE"
fi

# 4) systemd 单元。TimeoutStopSec 覆盖优雅停机预算（先停 worker 领取，
#    再等在飞结算，10s 上限）并留出余量。
# 单元内容里的路径永远是真实运行路径：干跑只改文件的落位，
# 不改单元声明的路径（否则干跑产物与真件不一致，检查就失去意义）。
#
# 4a) 加固用的可写路径：服务只需要写照片目录（数据库在 PostgreSQL，
#     状态/日志/备份都在服务用户之外）。值以 server.env 为准（与进程读到的
#     是同一个），缺省回落到安装器的运行目录。以下形态直接拒绝：
#     相对路径、含空白（systemd 指令按空白切参数）、被 ProtectHome 遮住的
#     /home|/root|/run/user、被 PrivateTmp 私有化的 /tmp|/var/tmp|/dev/shm，
#     以及服务主目录本身或其父目录（会把 ui 与旧备份一并开放写）。
PHOTO_DIR="$(grep -E '^YIBAN_PHOTO_DIR=' "$ENV_FILE" 2>/dev/null | tail -n 1 | cut -d= -f2- || true)"
PHOTO_DIR="${PHOTO_DIR:-$SERVICE_HOME/photos}"
# 去掉尾斜杠：/var/lib/yiban/ 与 /var/lib/yiban 必须是同一条判据。
while [ "${#PHOTO_DIR}" -gt 1 ] && [ "${PHOTO_DIR%/}" != "$PHOTO_DIR" ]; do
    PHOTO_DIR="${PHOTO_DIR%/}"
done
case "$PHOTO_DIR" in
    /*) ;;
    *) echo "install: YIBAN_PHOTO_DIR must be an absolute path: $PHOTO_DIR" >&2; exit 2 ;;
esac
case "$PHOTO_DIR" in
    *[[:space:]]*)
        echo "install: YIBAN_PHOTO_DIR must not contain whitespace: $PHOTO_DIR" >&2; exit 2 ;;
    /home/*|/root/*|/run/user/*)
        echo "install: YIBAN_PHOTO_DIR=$PHOTO_DIR is hidden by ProtectHome=true; use a path outside /home, /root and /run/user" >&2; exit 2 ;;
    /tmp/*|/var/tmp/*|/dev/shm/*)
        echo "install: YIBAN_PHOTO_DIR=$PHOTO_DIR would be private to the unit (PrivateTmp=true); photos would vanish with it" >&2; exit 2 ;;
    /)
        echo "install: YIBAN_PHOTO_DIR must not be /" >&2; exit 2 ;;
esac
if [ "$PHOTO_DIR" = "$SERVICE_HOME" ]; then
    echo "install: YIBAN_PHOTO_DIR must be a subdirectory of $SERVICE_HOME, not $SERVICE_HOME itself" >&2
    exit 2
fi
case "$SERVICE_HOME/" in
    "$PHOTO_DIR"/*)
        echo "install: YIBAN_PHOTO_DIR=$PHOTO_DIR is a parent of $SERVICE_HOME; refusing to expose ui and old backups" >&2
        exit 2 ;;
esac
# ProtectSystem=strict 下服务无法自己创建目录：安装器先把照片根建好并交给
# 服务用户。非递归 chown：只保证能落新照片，既有文件的权限不被改写。
mkdir -p "$(dest "$PHOTO_DIR")"
if [ -z "$ROOT" ]; then
    chown "$SERVICE_USER:$SERVICE_USER" "$(dest "$PHOTO_DIR")"
fi

cat > "$(dest "/etc/systemd/system/$UNIT")" <<EOF
[Unit]
Description=yiban Go server (auto sign backend)
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
UMask=0077
EnvironmentFile=/etc/yiban/server.env
ExecStart=/usr/local/bin/yiban-server
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=15
# 加固：除照片目录外整个文件系统只读；/home、/root、/run/user 不可见；
# 进程有私有 /tmp；禁止提权；新建文件默认 0600。
# ReadWritePaths 的 "-" 前缀只表示「路径不存在时不要拒绝启动」，
# 不改变写权限范围：安装器在启动前已建好照片目录。
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=-$PHOTO_DIR

[Install]
WantedBy=multi-user.target
EOF

# 5) 启用并启动。
"$SYSTEMCTL" daemon-reload
"$SYSTEMCTL" enable --now "$UNIT"

# 6) /healthz 重试探测。间隔与次数可用环境变量缩放（e2e 用小值）。
ENV_BODY="$(dest /etc/yiban/server.env)"
# 键缺失不是错误：手写的 server.env 可能没有该行，按缺省地址探活。
ADDR="$(grep -E '^YIBAN_HTTP_ADDR=' "$ENV_BODY" | head -n 1 | cut -d= -f2- || true)"
ADDR="${ADDR:-127.0.0.1:47183}"
RETRIES="${YIBAN_HEALTH_RETRIES:-10}"
INTERVAL="${YIBAN_HEALTH_RETRY_INTERVAL:-1}"
i=1
while [ "$i" -le "$RETRIES" ]; do
    if "$(dest /usr/local/bin/yiban-cli)" healthcheck --addr "$ADDR" --timeout 2s; then
        echo "install complete: healthz ok at $ADDR (attempt $i/$RETRIES)"
        # 干净库还没有可登录账号：建第一个 master 是部署的最后一步。
        # server.env 是 0600 root-only，新 shell 里 YIBAN_* 未设置：
        # 先载入环境，再用管道喂口令（口令不进 argv）。
        echo "next: create the first master:"
        echo "  set -a; . /etc/yiban/server.env; set +a"
        echo '  printf "%s\n" "$PASS" | yiban-cli user create-master --email <addr> --password-stdin'
        exit 0
    fi
    sleep "$INTERVAL"
    i=$((i + 1))
done
echo "install: healthcheck failed after $RETRIES attempts at $ADDR; check journalctl -u $UNIT" >&2
exit 1
