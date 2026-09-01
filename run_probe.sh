#!/bin/bash
umask 077
# 易班自动签到 - 探针模式运行脚本（健康探测，v0.23.x）
# 用法：cron 建议高频轮询（如 */10 * * * *）调用，脚本内由 signin.py --probe
#       自行判断是否到触发时间/频率（含 once 单次执行后自动关闭）。
#       实际执行时刻由 YIBAN_PROBE_TIME 决定（每天该时刻后的第一个调度周期）；
#       在「系统设置」修改探针时间后无需再改动 cron。
cd /opt/yiban-auto-sign

# 加载环境变量（安全逐行解析，仅导出 YIBAN_* 前缀键；与 run.sh 一致，绝不用 source）
_ENV_WARNINGS=""
if [ -r /opt/yiban-auto-sign/.env ]; then
    while IFS='=' read -r key value || [ -n "$key$value" ]; do
        value=${value%$'\r'}
        key="${key#"${key%%[![:space:]]*}"}"
        key="${key%"${key##*[![:space:]]}"}"
        [ -z "$key" ] && continue
        case "$key" in \#*) continue ;; esac
        if [[ ! "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
            _ENV_WARNINGS="${_ENV_WARNINGS}警告: .env 含非法键名，已跳过: $key"$'
'
            continue
        fi
        if [[ ! "$key" =~ ^YIBAN_ ]]; then
            _ENV_WARNINGS="${_ENV_WARNINGS}警告: .env 含非 YIBAN_ 前缀键，已跳过导出: $key"$'
'
            continue
        fi
        export "$key=$value"
    done < /opt/yiban-auto-sign/.env
fi

# 状态/日志根目录
STATE_DIR="${YIBAN_STATE_DIR:-/var/log/yiban}"
LOG_FILE="${YIBAN_LOG_FILE:-$STATE_DIR/sign.log}"
LOG_FILE="$(dirname "$LOG_FILE")/sign-$(date +%Y-%m-%d).log"

# 探针未开启时完全静默退出：不产生任何日志、不获取锁、不调用签到程序
if [[ ! "${YIBAN_PROBE_ENABLE:-0}" =~ ^(1|true|on|yes)$ ]]; then
    exit 0
fi

# 已确认开启：此刻才放行解析期告警（探针关闭时保持完全静默）
if [ -n "$_ENV_WARNINGS" ]; then
    printf '%s' "$_ENV_WARNINGS" >&2
fi

# 与签到共用单实例锁：探针与签到进程互斥，防止并发操作同一批账号
LOCK_DIR="/var/lock/yiban"
if [ ! -d "$LOCK_DIR" ]; then
    if ! mkdir -p "$LOCK_DIR" 2>/dev/null; then
        LOCK_DIR="/tmp/yiban-sign-$(id -u)"
        mkdir -p "$LOCK_DIR"
    fi
    if ! { [ -O "$LOCK_DIR" ] && chmod 700 "$LOCK_DIR" 2>/dev/null; }; then
        echo "致命: 锁目录 $LOCK_DIR 不安全（非本用户属主或权限收紧失败），拒绝运行" >&2
        exit 1
    fi
fi
exec 9>"$LOCK_DIR/sign.lock"
flock -n 9 || {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] === 已有签到/探针进程在运行，本次跳过 ===" >> "$LOG_FILE"
    exit 0
}

# Python 解释器：优先项目虚拟环境，缺失时回退系统 Python
if [ -x /opt/yiban-auto-sign/.venv/bin/python3 ]; then
    PY=/opt/yiban-auto-sign/.venv/bin/python3
else
    PY=/usr/bin/python3
fi

"$PY" scripts/signin.py --probe >> "$LOG_FILE" 2>&1
