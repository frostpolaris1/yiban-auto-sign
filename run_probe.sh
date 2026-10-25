#!/bin/bash
umask 077
# 易班自动签到 - 探针模式运行脚本（健康探测，v0.23.x）
# 用法：cron 建议高频轮询（如 */10 * * * *）调用，脚本内由 signin.py --probe
#       自行判断是否到触发时间/频率（含 once 单次执行后自动关闭）。
#       实际执行时刻由 YIBAN_PROBE_TIME 决定（每天该时刻后的第一个调度周期）；
#       在「系统设置」修改探针时间后无需再改动 cron。
# 应用目录：与 run.sh 同口径（唯一环境键 YIBAN_APP_DIR，未设时按检出反推，
# 见 `scripts/lib/host_paths.sh`）。
. "$(dirname "${BASH_SOURCE[0]}")/scripts/lib/host_paths.sh"
APP_DIR="$(yiban_app_dir)"
cd "$APP_DIR" || { echo "致命: 无法进入应用目录 $APP_DIR" >&2; exit 1; }

# 加载环境变量（安全逐行解析，仅导出 YIBAN_* 前缀键；与 run.sh 一致，绝不用 source）
# 优先级：非路径键 `.env` 优先（M27）；四个路径键进程环境优先、`.env` 只补缺
# （工单 71z7，与 `env_io.resolve_path` / `env_child.build_child_env` 同口径）。
# .env 在检出内的 data/ 下（工单 ybg8）：运行数据与代码分离，检出根才能属 root
# 且组/其他不可写（install.sh 的 M01 门）。data/ 由 install.sh 建立。
_ENV_PATH="$APP_DIR/data/.env"
_ENV_WARNINGS=""
if [ -r "$_ENV_PATH" ]; then
    while IFS='=' read -r key value || [ -n "$key$value" ]; do
        value=${value%$'\r'}
        # BOM 剥离：Windows 记事本保存的 .env 首行键名会带 BOM 前缀，被键名校验拒掉，
        # 导致与 run.sh（已剥离）对同一文件读出不同配置
        key="${key#$'\xEF\xBB\xBF'}"
        # key/value 首尾空白去除（与 run.sh 的 strip 及 env_io.parse_env_file 同口径；
        # 原先只剥 key，`KEY = v` / 值带尾随空格时两侧漂移）
        key="${key#"${key%%[![:space:]]*}"}"
        key="${key%"${key##*[![:space:]]}"}"
        value="${value#"${value%%[![:space:]]*}"}"
        value="${value%"${value##*[![:space:]]}"}"
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
        # 四个路径键的优先级例外（工单 71z7，与 env_io.resolve_path / env_child 同口径）：
        # 进程环境已有非空值时，.env 只补缺。环境文件指针固定 $APP_DIR/data/.env
        # （p12-13），不参与 .env 覆盖。
        case "$key" in
            YIBAN_DB_FILE|YIBAN_STATE_DIR|YIBAN_LOG_FILE)
                [ -n "${!key:-}" ] && continue
                ;;
        esac
        export "$key=$value"
    done < "$_ENV_PATH"
fi

# 指针与 run.sh 同源：子进程不得与脚本读到另一份配置（p12-13）。
export YIBAN_ENV_FILE="$_ENV_PATH"

# 探针未开启时完全静默退出：不产生任何日志、不获取锁、不调用签到程序。
# 开关真值判定与 run.sh 的 _is_truthy 同口径（字面量单一事实源在 yiban/adapters/config/env_io.py
# 的 ENV_TRUTHY_LITERALS）：去首尾空白 + 转小写后再比对，**大小写与两侧空白都不敏感**
# （旧实现 `=~(1|true|on|yes)` 大写敏感，`.env` 写 `YIBAN_PROBE_ENABLE=True`/`ON`/`TRUE`
# 时容器与引擎认为已开启、裸机这里却判未开启）。
_probe_flag="${YIBAN_PROBE_ENABLE:-0}"
_probe_flag="${_probe_flag#"${_probe_flag%%[![:space:]]*}"}"
_probe_flag="${_probe_flag%"${_probe_flag##*[![:space:]]}"}"
_probe_flag="$(printf '%s' "$_probe_flag" | tr '[:upper:]' '[:lower:]')"
case "$_probe_flag" in
    1|true|yes|on) ;;
    *) exit 0 ;;
esac

# 已确认开启：此刻才放行解析期告警（探针关闭时保持完全静默）
if [ -n "$_ENV_WARNINGS" ]; then
    printf '%s' "$_ENV_WARNINGS" >&2
fi

# Python 解释器：优先项目虚拟环境，缺失时回退系统 Python（须在按天文件名之前定好：
# 业务日取时经它走 yiban.core.clock，与引擎同源）
if [ -x "$APP_DIR/.venv/bin/python3" ]; then
    PY="$APP_DIR/.venv/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
    PY=python3
elif [ -x /usr/bin/python3 ]; then
    # cron 的 PATH 可能极简（/usr/bin:/bin），command -v 也未命中时兜底绝对路径
    PY=/usr/bin/python3
else
    echo "致命: 找不到 python3，无法运行探针" >&2
    exit 1
fi

# 业务日（`business_day`）与按天日志名（`yiban_day_log_file`）来自
# `scripts/lib/host_paths.sh`：宿主脚本的唯一 bash 定义点。探针按天写
# sign-<业务日>.log；取宿主 date（UTC 16:00-24:00 = 北京 00:00-08:00 窗口）会把结果
# 写进与引擎/web 不同名号的文件，web 日志页当天看到空文件。

# 状态/日志根目录（业务日与引擎同源）
STATE_DIR="${YIBAN_STATE_DIR:-/var/log/yiban}"
LOG_FILE="$(yiban_day_log_file "$STATE_DIR")"

# 与签到共用单实例锁：探针与签到进程互斥，防止并发操作同一批账号
# fail-closed 与 run.sh 同一纪律：建不成主锁目录就拒绝运行（rc=1）——旧回退分支把锁
# 静默挪进 /tmp 下的按 uid 命名目录，恰是锁目录加固注释点名的"可被预测/占用"位置，
# 且回退 mkdir 结果不检查；两把不同位置的锁=没有锁，探针与签到的互斥前提失效。
# 需要替代路径显式设 YIBAN_LOCK_DIR（与 run.sh 同键），属主/权限风险自担；对显式
# 路径同样执行"属主为本用户 + chmod 700 成功"的硬检查，不给第二套判法。
LOCK_DIR="${YIBAN_LOCK_DIR:-/var/lock/yiban}"
if [ ! -d "$LOCK_DIR" ]; then
    if ! mkdir -p "$LOCK_DIR" 2>/dev/null; then
        echo "致命: 无法创建锁目录 $LOCK_DIR，拒绝运行（如需替代路径请显式设置 YIBAN_LOCK_DIR）" >&2
        exit 1
    fi
fi
# M07：属主 + 700 硬检查对【已存在】锁目录同样执行——旧实现只在新建分支执行，
# 同机其他用户预建锁目录即可抢占锁使探针与签到长期静默跳过。
if ! { [ -O "$LOCK_DIR" ] && chmod 700 "$LOCK_DIR" 2>/dev/null; }; then
    echo "致命: 锁目录 $LOCK_DIR 不安全（非本用户属主或权限收紧失败），拒绝运行" >&2
    exit 1
fi
exec 9>"$LOCK_DIR/sign.lock"
flock -n 9 || {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] === 已有签到/探针进程在运行，本次跳过 ===" >> "$LOG_FILE"
    exit 0
}

# Python 解释器已在按天文件名之前解析（business_day 用它取业务日）。
"$PY" scripts/signin.py --probe >> "$LOG_FILE" 2>&1
