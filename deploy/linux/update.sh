#!/usr/bin/env bash
# ============================================================
# deploy/linux/update.sh — Go 服务端升级器。
#
# 顺序（派单 P7）：放置新二进制 → 停服务 → 跑迁移 → 换二进制 →
# 起服务 → /healthz；迁移失败回滚为「旧件继续运行」；
# 健康检查失败回滚旧二进制并报错。回滚后仍不健康退出 2。
#
# 用法：
#   sudo bash deploy/linux/update.sh --server-bin NEW [--cli-bin NEW]
#   （--cli-bin 与服务二进制同版本发布；只给 --server-bin 时
#     迁移用已安装的 yiban-cli。）
#
# 干跑钩子：YIBAN_INSTALL_ROOT=前缀 SYSTEMCTL=命令覆盖（与 install.sh 同义）。
#
# 退出码：0 成功；1 迁移失败或回滚成功（旧件在跑）；2 回滚后仍不健康。
# 已知残余风险：迁移一旦应用不会随二进制回滚（没有 down 迁移）；
# 旧二进制须能向前兼容新 schema。迁移都按向后兼容发布。
# ============================================================
set -euo pipefail

ROOT="${YIBAN_INSTALL_ROOT:-}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
UNIT="yiban.service"

SERVER_BIN=""
CLI_BIN=""
while [ $# -gt 0 ]; do
    case "$1" in
        --server-bin) SERVER_BIN="${2:?}"; shift 2 ;;
        --cli-bin) CLI_BIN="${2:?}"; shift 2 ;;
        *) echo "update: unknown argument: $1" >&2; exit 2 ;;
    esac
done

[ -n "$SERVER_BIN" ] || { echo "update: --server-bin is required" >&2; exit 2; }
[ -x "$SERVER_BIN" ] || { echo "update: new server binary missing: $SERVER_BIN" >&2; exit 2; }

dest() { printf '%s%s' "$ROOT" "$1"; }
SERVER_DEST="$(dest /usr/local/bin/yiban-server)"
CLI_DEST="$(dest /usr/local/bin/yiban-cli)"
SERVER_NEW="$SERVER_DEST.new"
BAK="$SERVER_DEST.bak"

[ -x "$SERVER_DEST" ] || { echo "update: no installed server at $SERVER_DEST; run install.sh first" >&2; exit 2; }

# 迁移要用新版的嵌入迁移集：给了 --cli-bin 就暂存新 cli，否则用已装的。
STAGED_CLI=""
if [ -n "$CLI_BIN" ]; then
    [ -x "$CLI_BIN" ] || { echo "update: new cli binary missing: $CLI_BIN" >&2; exit 2; }
    install -m 0755 "$CLI_BIN" "$CLI_DEST.new"
    STAGED_CLI="$CLI_DEST.new"
fi
install -m 0755 "$SERVER_BIN" "$SERVER_NEW"

# 载入 /etc/yiban/.env 的 KEY=VALUE（systemd 同源），DSN 由此而来。
# **不** source：DSN 含 `&` 或 `#` 时 shell 会切开赋值，变量落进子 shell
# 不外泄，迁移收到空 DSN。本函数只做四件事：跳过空行与 `#` 注释、
# 取第一个 `=` 分割键值、去掉值两端的成对单/双引号、只导出合法键名。
load_env_file() {
    local file="$1" line key value
    [ -f "$file" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%$'\r'}"
        case "$line" in
            ''|'#'*) continue ;;
        esac
        case "$line" in
            *=*) ;;
            *) continue ;;
        esac
        key="${line%%=*}"
        value="${line#*=}"
        # 键名必须匹配 ^[A-Za-z_][A-Za-z0-9_]*$；其余行不是配置项。
        case "$key" in
            ''|[0-9]*|*[!A-Za-z0-9_]*) continue ;;
        esac
        case "$value" in
            \"*\") value="${value:1:${#value}-2}" ;;
            \'*\') value="${value:1:${#value}-2}" ;;
        esac
        export "$key=$value"
    done < "$file"
}

ENV_FILE="$(dest /etc/yiban/.env)"
load_env_file "$ENV_FILE"

echo "update: stopping $UNIT"
"$SYSTEMCTL" stop "$UNIT"

MIGRATE_BIN="${STAGED_CLI:-$CLI_DEST}"
if [ ! -x "$MIGRATE_BIN" ]; then
    echo "update: no cli to run migrate at $MIGRATE_BIN; restore service" >&2
    "$SYSTEMCTL" start "$UNIT"
    exit 1
fi
if ! "$MIGRATE_BIN" migrate; then
    echo "update: migrate FAILED; keeping the old binary and starting the service back" >&2
    "$SYSTEMCTL" start "$UNIT"
    exit 1
fi

# 换二进制：旧件留 .bak 供回滚。
cp -p "$SERVER_DEST" "$BAK"
mv "$SERVER_NEW" "$SERVER_DEST"
if [ -n "$STAGED_CLI" ]; then
    mv "$CLI_DEST.new" "$CLI_DEST"
fi

echo "update: starting $UNIT"
"$SYSTEMCTL" start "$UNIT"

# 健康检查；失败回滚旧二进制再试一次。
ADDR="${YIBAN_HTTP_ADDR:-127.0.0.1:47183}"
RETRIES="${YIBAN_HEALTH_RETRIES:-10}"
INTERVAL="${YIBAN_HEALTH_RETRY_INTERVAL:-1}"

probe() {
    local i=1
    while [ "$i" -le "$RETRIES" ]; do
        if "$CLI_DEST" healthcheck --addr "$ADDR" --timeout 2s; then
            return 0
        fi
        sleep "$INTERVAL"
        i=$((i + 1))
    done
    return 1
}

if probe; then
    echo "update complete: healthz ok at $ADDR (old binary kept at $BAK)"
    exit 0
fi

echo "update: healthz failed after swap; rolling back to $BAK" >&2
# 必须先 stop：旧进程仍持有新二进制，对 active 单元 start 是 no-op，
# 只换文件不重启则回滚后的「旧件在跑」是假的。
"$SYSTEMCTL" stop "$UNIT"
mv "$BAK" "$SERVER_DEST"
"$SYSTEMCTL" start "$UNIT"
if probe; then
    echo "update: rolled back; old binary is serving again" >&2
    exit 1
fi
echo "update: rollback did not restore health; manual action required" >&2
exit 2
