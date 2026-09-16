#!/bin/bash
# 兜底常驻执行体（cron 每条窗口开始时拉起一次）。
#
# 本脚本只是**薄包装**：真正的逻辑唯一在 `yiban/engine/workers.py` 的
# `run_fallback_worker`（窗口内反复扫"还没了结"的账号并接手，窗口关闭即自己退出）。
# 这里只做两件事：**判开关**、**把日志重定向到当日签到日志**。
#
# 为什么要有它（而不是让部署者自己写 cron 命令）：
#   1. 开关 `YIBAN_FALLBACK_ENABLE` 由**网页**写进 `.env`，而 cron 环境里没有这个
#      变量，且 cron 也不能安全地加载 `.env`（不能 source，见 run.sh 的注入说明）——
#      这段"先读 .env 再判开关"的样板不该由每个部署者各写一份；
#   2. 开关关闭时必须**静默退出**：cron 每 5 分钟一次，任何输出都会变成周期性邮件；
#   3. 日志要与 run.sh 同源（`sign-<业务日>.log`），否则运维要在两个地方找日志。
#
# 环境变量（与 run.sh 同口径）：
#   YIBAN_ENV_FILE（默认 <APP_DIR>/.env）—— 开关与状态/日志目录都从这里读
#   YIBAN_STATE_DIR（默认 /var/log/yiban）、YIBAN_LOG_FILE（默认 <state>/sign.log）
#   YIBAN_FALLBACK_ENABLE —— 真值（1/true/on/yes，大小写不敏感）才起进程；
#     未设/0/false —— 静默 exit 0（不写日志、不取锁、不启进程）
#   **环境变量优先于 .env**：cron 行里显式写 `YIBAN_FALLBACK_ENABLE=1` 可临时绕过 .env
#
# cron 模板（放在签到窗口**开始时**；窗口 06:30 开始则 06:05 起挂上就够）：
#   5 6 * * * yiban /bin/bash /opt/yiban-auto-sign/scripts/yiban-fallback.sh
# 兜底进程会一直跑到窗口关闭（引擎自己判退），故**不需要**额外的 timeout 包裹。
# 与 run.sh 并存是安全的：它自己持独立锁文件（signin-run.lock.fallback），
# 与定时全量/手动签到的分工交给数据库里的领取池。
# 部署：以 yiban 用户运行（与 run.sh 同属主，才能写签到日志与状态目录）。
# 解释器：优先项目虚拟环境（与 run.sh 同一套解析），缺失时回退系统 python3。
umask 077

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"

ENV_PATH="${YIBAN_ENV_FILE:-$APP_DIR/.env}"

# 加载 `.env`：**只在变量未被设置时取用**，绝不覆盖已有环境变量。
# 安全说明与 run.sh 同源：绝不能使用 `source`/`.` 加载 .env——网页可写公告等文本，
# 若含 `; $(...)` 等 shell 元字符，source 会把文本当命令执行（命令注入）。
# 这里只做 key=value 赋值导出，值不会再次被 shell 求值；且只导出 YIBAN_* 键。
# 不打印任何告警：本脚本默认（开关关闭）每 5 分钟跑一次，向 stderr 输出会在 cron 下
# 变成周期性邮件噪声；非 YIBAN_ 键或非法键名一律**静默跳过**。
if [ -r "$ENV_PATH" ]; then
    while IFS='=' read -r key value || [ -n "$key$value" ]; do
        # 兼容 CRLF 编辑产生的行尾 CR
        value=${value%$'\r'}
        # 兼容 UTF-8 BOM 开头（Windows 记事本保存常见）
        key="${key#$'\xEF\xBB\xBF'}"
        # 两侧空白去除（与 env_io.parse_env_file 同口径）
        key="${key#"${key%%[![:space:]]*}"}"
        key="${key%"${key##*[![:space:]]}"}"
        value="${value#"${value%%[![:space:]]*}"}"
        value="${value%"${value##*[![:space:]]}"}"
        [ -z "$key" ] && continue
        case "$key" in \#*) continue ;; esac
        [[ "$key" =~ ^YIBAN_ && "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
        # 已在环境里设过的键不改（环境变量优先；cron 行里的显式赋值即由此生效）
        [ -n "${!key+x}" ] && continue
        export "$key=$value"
    done < "$ENV_PATH"
fi

# 真值判定（与 run.sh 的 `_is_truthy` 同一套字面量）
_is_truthy() {
    case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on) return 0 ;;
        *) return 1 ;;
    esac
}

if ! _is_truthy "${YIBAN_FALLBACK_ENABLE:-0}"; then
    # 开关关：静默退出（不写日志、不取锁、不启进程）
    exit 0
fi

# 状态/日志目录：与 run.sh 同口径，日志按天分文件（web 端按日期直接读取对应文件）
STATE_DIR="${YIBAN_STATE_DIR:-/var/log/yiban}"
mkdir -p "$STATE_DIR" 2>/dev/null || true
LOG_FILE="${YIBAN_LOG_FILE:-$STATE_DIR/sign.log}"
LOG_FILE="$(dirname "$LOG_FILE")/sign-$(date +%Y-%m-%d).log"

if [ -x "$APP_DIR/.venv/bin/python3" ]; then
    PY="$APP_DIR/.venv/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
    PY=python3
elif [ -x /usr/bin/python3 ]; then
    # cron 的 PATH 可能极简（/usr/bin:/bin），command -v 也未命中时兜底绝对路径
    PY=/usr/bin/python3
else
    echo "找不到 python3，无法启动兜底执行体（实现见 yiban/engine/workers.py）" >&2
    exit 1
fi

# 窗口关闭时引擎自己退出，故无需额外 timeout；本行之后进程被 exec 替换
exec "$PY" -m yiban.cli sign --fallback >> "$LOG_FILE" 2>&1
