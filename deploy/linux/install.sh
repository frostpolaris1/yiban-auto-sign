#!/usr/bin/env bash
# ============================================================
# deploy/linux/install.sh — Go 服务端裸机安装器（systemd 宿主）。
#
# 前提：PostgreSQL 18 已就绪且可连（建库不在本脚本职责内；
# 容器形态走 deploy/docker，不用本脚本）。
#
# 用法：
#   sudo bash deploy/linux/install.sh \
#     --server-bin ./dist/yiban-server --cli-bin ./dist/yiban-cli \
#     [--database-url postgres://...]
#   DSN 缺省读环境变量 YIBAN_DATABASE_URL。
#
# 干跑钩子（e2e 用，两个都给才是完整干跑）：
#   YIBAN_INSTALL_ROOT=/tmp/prefix   所有文件路径加该前缀；
#                                    同时跳过服务用户与 chown（无特权）。
#   SYSTEMCTL=/path/fake-systemctl   覆盖 systemctl 调用（记录用）。
#
# 步骤：依赖检查 → 服务用户 → 二进制落位 → /etc/yiban/.env
#       （缺则生成随机 pepper/credential key 并打印一次）
#       → systemd 单元 → enable --now → /healthz 重试探测。
#
# 退出码：0 成功；1 健康检查超时（文件已落位，可查日志定位）；
#         2 用法错误或依赖缺失。
# ============================================================
set -euo pipefail

ROOT="${YIBAN_INSTALL_ROOT:-}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
UNIT="yiban.service"
SERVICE_USER="yiban"
SERVICE_HOME="/var/lib/yiban"

SERVER_BIN=""
CLI_BIN=""
DSN_FLAG=""
while [ $# -gt 0 ]; do
    case "$1" in
        --server-bin) SERVER_BIN="${2:?}"; shift 2 ;;
        --cli-bin) CLI_BIN="${2:?}"; shift 2 ;;
        --database-url) DSN_FLAG="${2:?}"; shift 2 ;;
        *) echo "install: unknown argument: $1" >&2; exit 2 ;;
    esac
done

[ -n "$SERVER_BIN" ] || { echo "install: --server-bin is required" >&2; exit 2; }
[ -n "$CLI_BIN" ] || { echo "install: --cli-bin is required" >&2; exit 2; }
[ -x "$SERVER_BIN" ] || { echo "install: server binary missing or not executable: $SERVER_BIN" >&2; exit 2; }
[ -x "$CLI_BIN" ] || { echo "install: cli binary missing or not executable: $CLI_BIN" >&2; exit 2; }
DATABASE_URL="${DSN_FLAG:-${YIBAN_DATABASE_URL:-}}"
[ -n "$DATABASE_URL" ] || { echo "install: --database-url or YIBAN_DATABASE_URL is required" >&2; exit 2; }
command -v openssl >/dev/null 2>&1 || { echo "install: openssl is required" >&2; exit 2; }

# dest 把系统绝对路径映射到干跑前缀下。
dest() { printf '%s%s' "$ROOT" "$1"; }

# 1) 服务用户与运行目录。干跑（有前缀）时跳过：本机可能没有 yiban 用户。
if [ -z "$ROOT" ]; then
    id "$SERVICE_USER" >/dev/null 2>&1 \
        || useradd --system --home "$SERVICE_HOME" --shell /usr/sbin/nologin "$SERVICE_USER"
    mkdir -p "$SERVICE_HOME/photos"
    chown -R "$SERVICE_USER:$SERVICE_USER" "$SERVICE_HOME"
else
    echo "install: dry-run (YIBAN_INSTALL_ROOT set): skip service user and chown"
    mkdir -p "$(dest "$SERVICE_HOME/photos")"
fi

mkdir -p "$(dest /usr/local/bin)" "$(dest /etc/yiban)" "$(dest /etc/systemd/system)"

# 2) 二进制落位。
install -m 0755 "$SERVER_BIN" "$(dest /usr/local/bin/yiban-server)"
install -m 0755 "$CLI_BIN" "$(dest /usr/local/bin/yiban-cli)"

# 3) /etc/yiban/.env：缺则生成。0600 root:root —— systemd 以 root 读它，
#    服务进程经 EnvironmentFile 拿到值，yiban 用户无需可读。
ENV_FILE="$(dest /etc/yiban/.env)"
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
YIBAN_HASH_PEPPER=$(openssl rand -hex 32)
YIBAN_CREDENTIAL_KEY=$(openssl rand -base64 32)
EOF
else
    echo "install: $ENV_FILE exists; keeping it (secrets are never regenerated in place)"
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
cat > "$(dest "/etc/systemd/system/$UNIT")" <<EOF
[Unit]
Description=yiban Go server (auto sign backend)
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
EnvironmentFile=/etc/yiban/.env
ExecStart=/usr/local/bin/yiban-server
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=15

[Install]
WantedBy=multi-user.target
EOF

# 5) 启用并启动。
"$SYSTEMCTL" daemon-reload
"$SYSTEMCTL" enable --now "$UNIT"

# 6) /healthz 重试探测。间隔与次数可用环境变量缩放（e2e 用小值）。
ENV_BODY="$(dest /etc/yiban/.env)"
ADDR="$(grep -E '^YIBAN_HTTP_ADDR=' "$ENV_BODY" | head -n 1 | cut -d= -f2-)"
ADDR="${ADDR:-127.0.0.1:47183}"
RETRIES="${YIBAN_HEALTH_RETRIES:-10}"
INTERVAL="${YIBAN_HEALTH_RETRY_INTERVAL:-1}"
i=1
while [ "$i" -le "$RETRIES" ]; do
    if "$(dest /usr/local/bin/yiban-cli)" healthcheck --addr "$ADDR" --timeout 2s; then
        echo "install complete: healthz ok at $ADDR (attempt $i/$RETRIES)"
        # 干净库还没有可登录账号：建第一个 master 是部署的最后一步。
        echo "next: create the first master:"
        echo "  yiban-cli user create-master --email <addr> --password <pass>"
        exit 0
    fi
    sleep "$INTERVAL"
    i=$((i + 1))
done
echo "install: healthcheck failed after $RETRIES attempts at $ADDR; check journalctl -u $UNIT" >&2
exit 1
