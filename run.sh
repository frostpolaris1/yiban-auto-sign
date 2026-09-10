#!/bin/bash
umask 077
# 易班自动签到运行脚本
#
# 应用目录：默认生产路径；可用 YIBAN_APP_DIR 覆盖。原实现把 /opt/yiban-auto-sign
# 硬编码在多处，导致本脚本无法在任意目录下被回归测试或本地演练——改为变量后
# 生产行为完全不变（默认值仍是 /opt/yiban-auto-sign）。
APP_DIR="${YIBAN_APP_DIR:-/opt/yiban-auto-sign}"
cd "$APP_DIR" || { echo "致命: 无法进入应用目录 $APP_DIR" >&2; exit 1; }
ENV_PATH="$APP_DIR/.env"

# 加载环境变量（逐行安全解析，显式 export；必须先于日志/状态路径计算：
# YIBAN_STATE_DIR / YIBAN_LOG_FILE 可能由 .env 提供）
# 安全说明：绝不能使用 `source`/`.` 加载 .env。Web 普通管理员可写入公告等文本，
# 若公告含 `; $(...)` 等 shell 元字符，source 会把文本当作 shell 命令执行（命令注入）。
# 这里只做 key=value 赋值导出，值不会再次被 shell 求值。
if [ -r "$ENV_PATH" ]; then
    while IFS='=' read -r key value || [ -n "$key$value" ]; do
        # 兼容 CRLF 编辑产生的行尾 CR
        value=${value%$'\r'}
        # 兼容 UTF-8 BOM 开头的 .env（Windows 记事本保存常见）：否则首行键名带 BOM
        # 前缀被键名校验拒掉，宿主 cron 与 web 侧（utf-8-sig，见 scripts/env_io.py）
        # 对同一文件读出不同配置（2026-09-08）
        key="${key#$'\xEF\xBB\xBF'}"
        # key/value 首尾空白去除（与 env_io.parse_env_file 的两侧 strip 同口径；
        # 原实现 value 只剥尾部 CR，`KEY = v` / 值带尾随空格时两侧漂移）
        key="${key#"${key%%[![:space:]]*}"}"
        key="${key%"${key##*[![:space:]]}"}"
        value="${value#"${value%%[![:space:]]*}"}"
        value="${value%"${value##*[![:space:]]}"}"
        [ -z "$key" ] && continue
        case "$key" in \#*) continue ;; esac
        if [[ ! "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
            echo "警告: .env 含非法键名，已跳过: $key" >&2
            continue
        fi
        # 2026-08-21 对抗性审查加固：仅导出 YIBAN_* 前缀键——防止 .env 被写入
        # PATH/LD_PRELOAD 等敏感变量覆盖后续命令解析（纵深防御；当前能写 .env 的
        # web 设置页只产 YIBAN_* 键，此白名单同时约束未来变更）
        if [[ ! "$key" =~ ^YIBAN_ ]]; then
            echo "警告: .env 含非 YIBAN_ 前缀键，已跳过导出: $key" >&2
            continue
        fi
        export "$key=$value"
    done < "$ENV_PATH"
fi

# 状态/日志根目录：优先 YIBAN_STATE_DIR
STATE_DIR="${YIBAN_STATE_DIR:-/var/log/yiban}"
# 确保状态目录存在：否则下面 RUN_MARKER 的 noclobber 创建会失败，被误判成
# "当日已触发过"（导出 YIBAN_SECOND_RUN=1），在原生 run.sh 里只是告警口径偏差，
# 但在新逻辑下会**关掉进程内补签轮**——必须在写标记前建好目录。
mkdir -p "$STATE_DIR" 2>/dev/null || true

# 日志按天分文件（2026-08-16）：sign-YYYY-MM-DD.log，web 端按日期直接读取对应文件；
# 保留 YIBAN_LOG_FILE 配置的目录语义（默认 $STATE_DIR/sign.log）。
LOG_FILE="${YIBAN_LOG_FILE:-$STATE_DIR/sign.log}"
LOG_FILE="$(dirname "$LOG_FILE")/sign-$(date +%Y-%m-%d).log"

# 识别「当日已触发过」标记。
# 06:31 与 07:12 是同一脚本的两次 cron 调用，仅靠 sign-status 状态文件无法区分——
# 首签轮被 timeout 击杀（exit 124）或异常失败（exit 1）时不写状态文件，07:12 补签
# 轮读到的状态文件可能不存在，与首签轮无异。
# 标记语义（M6 修订）：「当日 run.sh 已触发过」（而非「首签轮已抢到锁」）——标记
# 写入前移到 flock 之前，06:31 触发被 flock 弹开（上一触发进程仍在运行）时同样
# 留痕；此后任意一次成功拿到锁的触发读到标记即以补签轮身份运行（导出
# YIBAN_SECOND_RUN=1，signin.py 的 _is_second_run 据此判定），解除「部分成功 +
# 窗口外」告警被首签轮身份压制的缺陷（_maybe_alert_zero_success：补签轮才告警，
# 而被弹开轮之后已无下一触发点）。因此 flock 弹开路径（exit 0）发生在标记写入
# 之后——这正是目的：被弹开的触发也计入「当日已触发过」。
# 标记按日期命名，跨日自动失效。本块在 flock 之前执行、无锁保护，故创建改用
# noclobber 原子测试创建：并发触发时仅一次创建成功，其余一律按补签轮处理
# （fail-safe 侧：宁可多告警、不可漏告警；实际执行仍由 flock 串行化）。
RUN_MARKER="$STATE_DIR/yiban-run-today-$(date +%Y-%m-%d).marker"
if ( set -o noclobber; : > "$RUN_MARKER" ) 2>/dev/null; then
    : # 今日首次触发：本轮按首签轮运行
else
    export YIBAN_SECOND_RUN=1
fi

# 单实例锁：自动错峰模式下 06:31 进程可能 sleep 等待时间点，
# 防止 07:12 的 cron 并发启动第二个进程（重复签到/并发竞争）
# 使用 /var/lock（仅 yiban 用户可写），避免 /tmp 下可被任意用户预测/占用导致 DoS
LOCK_DIR="${YIBAN_LOCK_DIR:-/var/lock/yiban}"
if [ ! -d "$LOCK_DIR" ]; then
    if ! mkdir -p "$LOCK_DIR" 2>/dev/null; then
        echo "警告: 无法创建 $LOCK_DIR，回退 /tmp" >&2
        LOCK_DIR="/tmp/yiban-sign-$(id -u)"
        mkdir -p "$LOCK_DIR"
    fi
    # 2026-08-21 对抗性审查加固：回退目录必须属主为本用户且 chmod 700 成功——
    # 否则同机其他用户可预建目录/符号链接截断文件或抢占锁使签到静默跳过
    if ! { [ -O "$LOCK_DIR" ] && chmod 700 "$LOCK_DIR" 2>/dev/null; }; then
        echo "致命: 锁目录 $LOCK_DIR 不安全（非本用户属主或权限收紧失败），拒绝运行" >&2
        exit 1
    fi
fi
exec 9>"$LOCK_DIR/sign.lock"
flock -n 9 || {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] === 已有签到进程在运行，本次跳过 ===" >> "$LOG_FILE"
    exit 0
}

# 状态文件：记录今天的签到结果，避免重复执行
STATUS_FILE="$STATE_DIR/sign-status-$(date +%Y-%m-%d).txt"
# 当日收尾标记：本脚本（含进程内补签轮）已把当天该做的都做完（无论成败）。
# 作用：让 07:12 的 cron（兜底）在后继场景下不再多跑第三轮——
# 例如首轮 06:35 就结束、且已完成补签轮，此时 07:12 的 cron 会拿到锁，
# 没有本标记它会因为状态非 SUCCESS 再跑一轮（幂等但多一轮真实登录请求）。
SECOND_DONE_MARKER="$STATE_DIR/yiban-settled-$(date +%Y-%m-%d).marker"

# Python 解释器：优先项目虚拟环境，缺失时回退系统 Python
if [ -x "$APP_DIR/.venv/bin/python3" ]; then
    PY="$APP_DIR/.venv/bin/python3"
else
    PY=/usr/bin/python3
fi

# ---------------------------------------------------------------------------
# 进程内补签轮（2026-09-10 批次20 B3，用户裁决方案一）
# ---------------------------------------------------------------------------
# 问题：补签原本由**第二个独立 cron**（07:12）承担，但 flock 由首签脚本持有至退出，
# 而首签要 sleep 到最晚自选时间片（生产实测 07:25）才结束 —— 07:12 的 cron 每天撞锁
# `exit 0`，补签轮从未真正执行（生产 sign-*.log 12/12 天实证）。容器形态反而正确：
# docker/scheduler.py 在同一循环里等首轮子进程返回后再判定 SECOND 闸门。
# 修法：宿主改为同语义——首轮结束后**在仍持锁的同一进程内**再判定一次是否需要补跑。
# 判定口径收敛在 signin.need_second_run()（容器同样复用它），不在此处重复实现。
# 注意：07:12 的 cron 保留不动，作为「06:31 进程被宿主杀死」的兜底；它拿到锁时会因
# YIBAN_SECOND_RUN=1 而不再评估第三轮，并在收尾写 SECOND_DONE_MARKER。
SECOND_HHMM="${YIBAN_SECOND_RUN_TIME:-07:12}"
# 逃生开关：显式置 0 可关闭进程内补签轮（仅调试/特殊运维场景用）
SECOND_ROUND_ENABLED="${YIBAN_HOST_SECOND_ROUND:-1}"

_is_truthy() {
    case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on) return 0 ;;
        *) return 1 ;;
    esac
}

# 等到指定 HH:MM（当天）；已过点或格式非法则立即返回（与容器 hm>=SECOND 同语义）
_wait_until_hhmm() {
    local target="$1" th tm now_s tgt_s
    [ -n "$target" ] || return 0
    case "$target" in *:*) ;; *) return 0 ;; esac
    th="${target%%:*}"
    tm="${target##*:}"
    now_s="$(date +%s)"
    tgt_s="$(date -d "today $th:$tm" +%s 2>/dev/null)" || return 0
    [ -n "$tgt_s" ] || return 0
    if [ "$now_s" -lt "$tgt_s" ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 补签轮：等待至 $target 再执行（约 $(( (tgt_s - now_s) / 60 )) 分钟）" >> "$LOG_FILE"
        sleep $(( tgt_s - now_s ))
    fi
    return 0
}

# 补签轮判定（调用 signin.py --second-run-check）：退出码 10 = 需要补跑
_need_second_round() {
    _is_truthy "$SECOND_ROUND_ENABLED" || return 1
    [ "${YIBAN_SECOND_RUN:-0}" = "1" ] && return 1   # 本轮本身就是补签轮 → 不再评估
    # 全站暂停（管理员一键暂停）：signin 会立刻 exit 2，补跑没有任何意义，
    # 只会把锁多占一会儿（原逻辑下 07:12 的 cron 也会同样空跑一次，属既有行为）
    _is_truthy "${YIBAN_GLOBAL_PAUSE:-0}" && return 1
    "$PY" scripts/signin.py --second-run-check >> "$LOG_FILE" 2>&1
    local rc=$?
    [ "$rc" -eq 10 ]
}

# 执行一轮签到；每轮按当前时刻重算超时（原实现只在脚本开头算一次，
# 补签轮复用它会让第二轮的可用时长被高估）
_run_signin_round() {
    local end_hhmm="${YIBAN_SIGN_END:-07:50}" run_timeout end_ts now_ts raw
    if ! echo "$end_hhmm" | grep -qE '^([01]?[0-9]|2[0-3]):[0-5][0-9]$'; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 警告: YIBAN_SIGN_END=$end_hhmm 非法，回退默认 07:50" >> "$LOG_FILE"
        end_hhmm="07:50"
    fi
    end_ts=$(date -d "today $end_hhmm" +%s)
    now_ts=$(date +%s)
    run_timeout=$(( end_ts - now_ts + 300 ))
    [ "$run_timeout" -lt 600 ] && run_timeout=600
    raw="${YIBAN_RUN_TIMEOUT_SEC:-}"
    # P3-2 钳位：先去首尾空白（与 .env 解析的值剥空白口径一致，兼容旧值带空格的存量部署）
    raw="${raw#"${raw%%[![:space:]]*}"}"
    raw="${raw%"${raw##*[![:space:]]}"}"
    if [ -n "$raw" ]; then
        if [[ "$raw" =~ ^[0-9]+$ ]] && [ "$raw" -ge 600 ] 2>/dev/null; then
            run_timeout="$raw"
        else
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] 警告: YIBAN_RUN_TIMEOUT_SEC=$raw 非法（须为 ≥600 的整数），回退动态计算 ${run_timeout}s" >> "$LOG_FILE"
        fi
    fi
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 签到超时: ${run_timeout}s（窗口至 $end_hhmm）" >> "$LOG_FILE"
    timeout "$run_timeout" "$PY" scripts/signin.py >> "$LOG_FILE" 2>&1
    return $?
}

# 状态文件只在"确实执行过签到"时写 SUCCESS（退出码 0）：
# 全部 skip（无实际执行，退出码 2）写 SKIPPED，避免把"没签到"记录成成功
# 从而吞掉后续任务；其他失败（退出码 1）不写状态文件
# 退出码 2 同时覆盖「存在窗口外/缺失未了结账号」的混合场景
# （部分成功 + 部分 skipped_window/norange）——signin.py 此时返回 2，
# 这里写 SKIPPED 而非 SUCCESS，补签才会重跑，
# 窗口外账号不因"有账号成功"而失去当天兜底（容器调度器同语义）。
# 原子写：写临时文件 + mv，防止掉电/被杀时文件处于半写状态
_status_write() {
    local content="$1"
    local tmp
    tmp=$(mktemp "${STATUS_FILE}.tmp.XXXXXX")
    echo "$content" > "$tmp"
    mv -f "$tmp" "$STATUS_FILE"
}

_write_status_from_exit() {
    local exit_code="$1"
    if [ "$exit_code" -eq 0 ]; then
        _status_write "SUCCESS"
    elif [ "$exit_code" -eq 2 ]; then
        # 语义区分（0.22.0 审查修复）：全局暂停（YIBAN_GLOBAL_PAUSE=1）写 GLOBAL_PAUSED，
        # 与窗口/配置导致的普通 SKIPPED 分开——运维/监控可直接区分"人为暂停"与"技术性跳过"
        if [ "${YIBAN_GLOBAL_PAUSE:-0}" = "1" ]; then
            _status_write "GLOBAL_PAUSED"
        else
            _status_write "SKIPPED"
        fi
    fi
}

# 当日收尾标记已存在 → 当天该做的都已做完（含补签轮），本次触发无需再跑。
# 仍然先看 SUCCESS：两者语义重叠但保留原判定，避免行为回归。
if [ -f "$SECOND_DONE_MARKER" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 今天已完成签到收尾（含补签轮），跳过执行 ===" >> "$LOG_FILE"
    exit 0
fi

# 检查今天是否已经签到成功
if [ -f "$STATUS_FILE" ]; then
    STATUS=$(cat "$STATUS_FILE")
    if [ "$STATUS" = "SUCCESS" ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 今天已签到成功，跳过执行 ===" >> "$LOG_FILE"
        exit 0
    fi
fi

# 记录脚本开始执行
echo "[$(date '+%Y-%m-%d %H:%M:%S')] === run.sh 开始执行 ===" >> "$LOG_FILE"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 工作目录: $(pwd)" >> "$LOG_FILE"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Python版本: $("$PY" --version 2>&1)" >> "$LOG_FILE"

# ---- 第一轮（首签轮）----
_run_signin_round
EXIT_CODE=$?
_write_status_from_exit "$EXIT_CODE"

# ---- 第二轮（补签轮，进程内）----
# 首轮结束后仍持锁，任何其他触发（含 07:12 的 cron）都被 flock 挡在外面，
# 因此"判定 → 等待 → 补跑"整段是原子的，不存在两个进程同时补跑的窗口。
if _need_second_round; then
    _wait_until_hhmm "$SECOND_HHMM"
    # 等待期间其它进程进不来（锁在本进程手上）；若等待前状态已是 SUCCESS 则不必补跑
    if [ -f "$STATUS_FILE" ] && [ "$(cat "$STATUS_FILE")" = "SUCCESS" ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 补签轮：状态已是 SUCCESS，跳过" >> "$LOG_FILE"
    else
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] === 开始补签轮（第二轮）===" >> "$LOG_FILE"
        export YIBAN_SECOND_RUN=1   # signin 据此判定 is_second_run（告警口径/剔除已成功账号）
        _run_signin_round
        EXIT_CODE=$?
        _write_status_from_exit "$EXIT_CODE"
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] === 补签轮结束，退出码: $EXIT_CODE ===" >> "$LOG_FILE"
    fi
else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 补签轮：无需补跑（当日已收尾且无未了结账号）" >> "$LOG_FILE"
fi

# 当日收尾：标记"该做的都做完了"，避免 07:12 的兜底 cron 再跑第三轮
: > "$SECOND_DONE_MARKER" 2>/dev/null || true

# 记录脚本执行结果
echo "[$(date '+%Y-%m-%d %H:%M:%S')] === run.sh 执行完成，退出码: $EXIT_CODE ===" >> "$LOG_FILE"
echo "" >> "$LOG_FILE"

exit $EXIT_CODE
