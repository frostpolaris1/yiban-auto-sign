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
#   **`.env` 优先，进程环境只补缺**（M27，与 run.sh / docker-compose / 引擎
#   `build_child_env` 同一口径）：`.env` 里写了 `YIBAN_FALLBACK_ENABLE=0`，即便 cron
#   行上写 `YIBAN_FALLBACK_ENABLE=1` 也不起进程。想临时绕过开关，去网页设置页改
#   `.env`（或临时把 `.env` 那行挪掉），**不要**指望 cron 行里的前缀。
#   口径反转过一次（过去的头注释写「环境变量优先于 .env」，实现却也确实是那样），
#   现已统一成 `.env` 优先：开关的事实源必须是网页写进 `.env` 的那份，否则会出现
#   「页面显示关闭、cron 却天天拉起兜底」这种两处同时为真的状态。
#
# cron 模板（放在签到窗口**开始时**；窗口 06:30 开始则 06:05 起挂上就够）：
#   5 6 * * * yiban /bin/bash /opt/yiban-auto-sign/scripts/yiban-fallback.sh
# 兜底进程会一直跑到窗口关闭（引擎自己判退），故**不需要**额外的 timeout 包裹。
# 与 run.sh 并存是安全的：引擎侧持独立锁 `signin-run.lock.fallback`（本脚本不取锁——
# 锁由下面那条 `--fallback` 入口自己取，撞上已在跑的兜底会以退出码 3 结束，不叠进程）；
# 与定时全量/手动签到的账号级分工交给数据库里的领取池。
# 部署：以 yiban 用户运行（与 run.sh 同属主，才能写签到日志与状态目录）。
# 解释器：优先项目虚拟环境（与 run.sh 同一套解析），缺失时回退系统 python3。
umask 077

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"

ENV_PATH="${YIBAN_ENV_FILE:-$APP_DIR/.env}"

# 加载 `.env`：**`.env` 覆盖进程环境**，进程环境只补 `.env` 里没有的键（M27）。
# 安全说明与 run.sh 同源：绝不能使用 `source`/`.` 加载 .env——网页可写公告等文本，
# 若含 `; $(...)` 等 shell 元字符，source 会把文本当命令执行（命令注入）。
# 这里只做 key=value 赋值导出，值不会再次被 shell 求值；且只导出 YIBAN_* 键。
# 行为变更（2026-10-01，M27）：过去这里是「已在环境里设过的键不改」（环境变量优先），
# 现与 run.sh / 容器调度器 / 引擎 `build_child_env` 统一为 `.env` 优先。
# 谁受影响：cron 行上带 `YIBAN_FALLBACK_ENABLE=1` 前缀、而 `.env` 里是 0（或没写）的
# 部署——改后这些部署**不再**起兜底进程。怎么回退：把 `.env` 里那一行删掉（键不存在
# ⇒ 进程环境补缺 ⇒ 前缀重新生效），或在 cron 行上显式 `env -u YIBAN_FALLBACK_ENABLE`。
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
        # `.env` 优先：无条件覆盖。进程环境只在 `.env` **没有这个键**时补缺。
        export "$key=$value"
    done < "$ENV_PATH"
fi

# 真值判定。字面量口径的**单一事实源**在 yiban/infra/env_io.py 的
# ENV_TRUTHY_LITERALS（Python 侧 parse_env_flag 用它）；bash 无法 import，此处逐字
# 复刻同一份（1/true/yes/on，大小写不敏感、两侧空白忽略）。两边结论一致性由
# tests/test_pause_flag_truthiness_e2e.py 真跑本函数与 Python 逐值比对钉住。
# 勿在此另加/删字面量——改了它就必须同步改 env_io 的名册；本副本与 run.sh 的
# `_is_truthy` 必须逐字同口径，否则同一个 YIBAN_FALLBACK_ENABLE 在两个 bash 读者
# 下会得出相反结论（本脚本判关静默退出、run.sh 判开每轮告警并尝试拉起）。
_is_truthy() {
    local v="${1:-}"
    # 去首尾空白（与 Python .strip() 同口径；本脚本加载 .env 时已剥一次，这里兜住
    # "直接经进程环境传入且带空白"的调用方，与 run.sh 同一口径）
    v="${v#"${v%%[![:space:]]*}"}"
    v="${v%"${v##*[![:space:]]}"}"
    case "$(printf '%s' "$v" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on) return 0 ;;
        *) return 1 ;;
    esac
}

if ! _is_truthy "${YIBAN_FALLBACK_ENABLE:-0}"; then
    # 开关关：静默退出（不写日志、不取锁、不启进程）
    exit 0
fi

# 状态/日志目录：与 run.sh 同口径，日志按天分文件（web 端按日期直接读取对应文件）；
# 按天名取 business_day（业务日，与引擎同源），解释器先行解析供它取时用。
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

business_day() {
    # 只信任形如 YYYY-MM-DD 的输出：解释器在但打印为空/异常时（venv 损坏、版本不匹配）
    # 必须退到下一级——否则按天文件名会变成 `sign-.log` / `yiban-.tar.gz` 这种静默错位。
    local _d
    _d="$("$PY" -c "import sys; sys.path.insert(0, sys.argv[1]); from yiban.clock import today as t; print(t())" "$APP_DIR" 2>/dev/null)"
    case "$_d" in
        [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) echo "$_d"; return 0 ;;
    esac
    TZ=Asia/Shanghai date +%F 2>/dev/null && return 0
    date +%F
}
STATE_DIR="${YIBAN_STATE_DIR:-/var/log/yiban}"
mkdir -p "$STATE_DIR" 2>/dev/null || true
# M07：状态目录属主 + 700 硬检查（与 run.sh 同一判据）——已存在目录同样校验：
# 非本用户属主时可被同机其他用户预占/伪造签到日志与审计锚点。收紧失败即拒绝启动。
if ! { [ -O "$STATE_DIR" ] && chmod 700 "$STATE_DIR" 2>/dev/null; }; then
    echo "致命: 状态目录 $STATE_DIR 不安全（非本用户属主或权限收紧失败），拒绝启动兜底执行体" >&2
    exit 1
fi
LOG_FILE="${YIBAN_LOG_FILE:-$STATE_DIR/sign.log}"
LOG_FILE="$(dirname "$LOG_FILE")/sign-$(business_day).log"

# 窗口关闭时引擎自己退出，故无需额外 timeout；本行之后进程被 exec 替换
exec "$PY" -m yiban.cli sign --fallback >> "$LOG_FILE" 2>&1
