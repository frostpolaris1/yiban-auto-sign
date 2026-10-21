#!/bin/bash
umask 077

# ---- stderr 出口：全部警告/致命统一带时间戳（工单 yiban-auto-sign-2k1i，2026-10-10）----
# 面向 stderr 的行落进 run-cron.log。原实现无时间戳，跨日累积的同类行（例如每日
# cron 各留 1 条）会被读成"同日被反复调用多次"，是 10-10 误判的直接原因。统一经本
# 函数出口，前缀与 _log 同格式（`%F %T` 即 `%Y-%m-%d %H:%M:%S`）。新增警告/致命
# 一律走 _err；不经 _err 的裸 stderr 写入由 tests/test_run_sh_stderr_timestamps.py 结构门拦下。
# 位置约束：本定义必须先于首次使用（下面 `cd "$APP_DIR"` 的失败分支即最先使用）。
_err() {
    echo "[$(date '+%F %T')] $*" >&2
}

# 易班自动签到运行脚本
#
# 应用目录：默认生产路径；可用 YIBAN_APP_DIR 覆盖。原实现把 /opt/yiban-auto-sign
# 硬编码在多处，导致本脚本无法在任意目录下被回归测试或本地演练——改为变量后
# 生产行为完全不变（默认值仍是 /opt/yiban-auto-sign）。
APP_DIR="${YIBAN_APP_DIR:-/opt/yiban-auto-sign}"
cd "$APP_DIR" 2>/dev/null || { _err "致命: 无法进入应用目录 $APP_DIR"; exit 1; }
# .env 在检出内的 data/ 下（工单 ybg8）：运行数据与代码分离，检出根才能属 root
# 且组/其他不可写（install.sh 的 M01 门要求）。data/ 由 install.sh 建立（属服务
# 账号、0700）——本脚本不自建：检出根变只读后 mkdir 会失败。
ENV_PATH="$APP_DIR/data/.env"

# Python 解释器：优先项目虚拟环境，缺失时回退系统 Python（须在按天文件名之前定好：
# 业务日取时经它走 yiban.core.clock，与引擎同源）
if [ -x "$APP_DIR/.venv/bin/python3" ]; then
    PY="$APP_DIR/.venv/bin/python3"
else
    PY=/usr/bin/python3
fi

# 业务日唯一来源（MF-109）：yiban.core.clock 的北京钟，与引擎/web 同一事实源。宿主时区
# ≠北京（UTC 16:00-24:00 窗口）时宿主 `date` 的"当天"与引擎业务日差一天——状态文件、
# 日志、触发标记、收尾标记与库内"当日事实"查询一律取这里。取不到 yiban.core.clock
# （python 缺失/损坏）时退化 `TZ=Asia/Shanghai date`（需宿主 tzdata），再退化宿主
# `date`（保持可运行的降级形态，此时与引擎业务日可能差一天）。
business_day() {
    # 只信任形如 YYYY-MM-DD 的输出：解释器在但打印为空/异常时（venv 损坏、版本不匹配）
    # 必须退到下一级——否则按天文件名会变成 `sign-.log` / `yiban-.tar.gz` 这种静默错位。
    local _d _tz
    _d="$("$PY" -c "import sys; sys.path.insert(0, sys.argv[1]); from yiban.core.clock import today as t; print(t())" "$APP_DIR" 2>/dev/null)"
    case "$_d" in
        [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) echo "$_d"; return 0 ;;
    esac
    # 二级降级的判据是「TZ 真的生效」，不是 date 的退出码：宿主缺 tzdata 时
    # GNU date 对未知时区按 UTC 处理并返回 0，旧写法让第三级永远不可达。
    _tz="$(TZ=Asia/Shanghai date +%z 2>/dev/null || true)"
    if [ "$_tz" = "+0800" ]; then
        TZ=Asia/Shanghai date +%F
        return 0
    fi
    _err "警告: 宿主缺 tzdata，TZ=Asia/Shanghai 不生效（date +%z=${_tz:-空}），业务日退回宿主 date——可能与引擎业务日差一天"
    date +%F
}

# 加载环境变量（逐行安全解析，显式 export；必须先于日志/状态路径计算：
# YIBAN_STATE_DIR / YIBAN_LOG_FILE 可能由 .env 提供）
#
# **优先级口径（工单 71z7，2026-10-20 裁定）：**
# · **非路径键**：`.env` 优先，进程环境只补缺（M27）。网页设置页写的是 `.env`，
#   读侧若反过来，页面显示关闭而排程照跑——两处同时为真没人说得清。
# · **四个路径键**（环境文件指针、库文件、状态目录、日志文件）：**进程环境优先，
#   `.env` 只补缺**（与 `env_io.resolve_path` / `scripts/child_env.build_child_env`
#   同一口径）。真实用途：测试/CI 用进程环境把数据目录重定向到临时目录，不该被
#   `.env` 里的旧路径拉回。
# · 环境文件指针固定 `$APP_DIR/data/.env`（下面显式 export）；它不参与 `.env` 覆盖
#   （p12-13：旧注释声称可由该指针环境变量覆盖，实现从未读它）。
#
# 安全说明：绝不能使用 `source`/`.` 加载 .env。Web 普通管理员可写入公告等文本，
# 若公告含 `; $(...)` 等 shell 元字符，source 会把文本当作 shell 命令执行（命令注入）。
# 这里只做 key=value 赋值导出，值不会再次被 shell 求值。
if [ -r "$ENV_PATH" ]; then
    while IFS='=' read -r key value || [ -n "$key$value" ]; do
        # 兼容 CRLF 编辑产生的行尾 CR
        value=${value%$'\r'}
        # 兼容 UTF-8 BOM 开头的 .env（Windows 记事本保存常见）：否则首行键名带 BOM
        # 前缀被键名校验拒掉，宿主 cron 与 web 侧（utf-8-sig，见 yiban/adapters/config/env_io.py）
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
            _err "警告: .env 含非法键名，已跳过: $key"
            continue
        fi
        # 2026-08-21 对抗性审查加固：仅导出 YIBAN_* 前缀键——防止 .env 被写入
        # PATH/LD_PRELOAD 等敏感变量覆盖后续命令解析（纵深防御；当前能写 .env 的
        # web 设置页只产 YIBAN_* 键，此白名单同时约束未来变更）
        if [[ ! "$key" =~ ^YIBAN_ ]]; then
            _err "警告: .env 含非 YIBAN_ 前缀键，已跳过导出: $key"
            continue
        fi
        # 四个路径键的优先级例外（与 env_io.resolve_path / child_env 同口径）：
        # 进程环境已有非空值时，.env 只补缺。环境文件指针固定 $APP_DIR/data/.env
        # （p12-13），不在覆盖范围（见下方显式 export）。
        case "$key" in
            YIBAN_DB_FILE|YIBAN_STATE_DIR|YIBAN_LOG_FILE)
                [ -n "${!key:-}" ] && continue
                ;;
        esac
        export "$key=$value"
    done < "$ENV_PATH"
else
    # MF-81④：.env 缺失/不可读过去完全静默——所有配置（STATE_DIR、通知、窗口时间）
    # 悄悄回落默认值，排障时无人知道"配置读没读到"。现在显式告警（下面 _log 可用后
    # 写入日志）。选择"告警后继续"而不是退出：现网可能以读不到 root-only .env 的身份跑
    # cron，直接退非 0 会天天红；"告警 + 继续用默认值 + 日志/stderr 留痕"是更安全的取舍。
    if [ -e "$ENV_PATH" ]; then
        ENV_WARNING=".env 存在但当前用户不可读，全部配置回落默认值: $ENV_PATH"
    else
        ENV_WARNING=".env 不存在，全部配置回落默认值: $ENV_PATH"
    fi
fi

# 指针与库路径**显式导出给子进程**（本脚本读的是上面那个固定 `$APP_DIR/data/.env`，
# 子进程不得与它分叉：一侧读 data/.env、另一侧读别处 = 引擎与 cron 用两份配置）。
# 位置约束：必须排在**上面 .env 装载之后**——装载循环把路径键排除在覆盖之外，
# 这里再无条件写一次，保证本脚本与子进程读同一份文件（p12-13）。
export YIBAN_ENV_FILE="$ENV_PATH"
STATE_DIR="${YIBAN_STATE_DIR:-/var/log/yiban}"
# MF-81：状态目录承载全部"跑/不跑"判定件（RUN_MARKER / 状态文件 / 收尾标记），
# 它的可用性必须显式校验，对齐下面锁目录的既有做法——旧实现 `mkdir -p || true`
# 吞掉一切失败：目录建不出来/不可写时，noclobber 标记写失败会被误判成
# "当日已触发过"（导出 YIBAN_SECOND_RUN=1 静默关掉进程内补签轮），或让收尾/状态
# 写入静默丢失（下一触发误判再跑整轮真实登录）。写失败 = fail-closed：不跑并告警。
# 此刻 _log/LOG_FILE 尚不可用，声音只能走 stderr + 非零退出码。
if [ ! -d "$STATE_DIR" ]; then
    if ! mkdir -p "$STATE_DIR" 2>/dev/null; then
        _err "致命: 无法创建状态目录 $STATE_DIR，无法完成跑/不跑判定，拒绝运行"
        exit 1
    fi
fi
# M07：属主 + 700 硬检查对【已存在】目录同样执行——旧实现把它放在 `if [ ! -d ]` 的
# 新建分支内，同机其他用户预建的目录（或早期版本留下的非本用户属主目录）完全不查，
# 可长期压住签到或伪造状态锚点。
if ! { [ -O "$STATE_DIR" ] && chmod 700 "$STATE_DIR" 2>/dev/null; }; then
    _err "致命: 状态目录 $STATE_DIR 不安全（非本用户属主或权限收紧失败），拒绝运行"
    exit 1
fi
if [ ! -w "$STATE_DIR" ]; then
    _err "致命: 状态目录 $STATE_DIR 不可写，无法完成跑/不跑判定，拒绝运行"
    exit 1
fi

# 日志按天分文件（2026-08-16）：sign-YYYY-MM-DD.log，web 端按日期直接读取对应文件；
# 保留 YIBAN_LOG_FILE 配置的目录语义（默认 $STATE_DIR/sign.log）。
LOG_FILE="${YIBAN_LOG_FILE:-$STATE_DIR/sign.log}"
LOG_FILE="$(dirname "$LOG_FILE")/sign-$(business_day).log"

# ---- 触发来源前缀与退出码留痕 ----
# 排程（cron / systemd / 容器调度）与手工执行共用同一个 sign-YYYY-MM-DD.log，光看时间
# 戳分不清哪一轮是谁触发的（"今天怎么跑了两轮"是排查的第一句）。判据：有控制终端 =
# 人在终端里执行；没有 = 排程。容器等场景可用 YIBAN_TRIGGER 显式覆盖。
# 判据取 **stdin**（fd 0）而不是 stdout/stderr：手工执行常把输出重定向进日志
# （`./run.sh > sign.log`），那两路就不是终端了、会被误判成排程；stdin 是不是终端与
# "人在不在终端里执行"这件事同步，重定向输出不影响它。
if [ -n "${YIBAN_TRIGGER:-}" ]; then
    TRIGGER_TAG="$YIBAN_TRIGGER"
elif [ -t 0 ]; then
    TRIGGER_TAG="手工"
else
    TRIGGER_TAG="排程"
fi

# 本脚本自己的日志出口：统一带上触发来源前缀（子进程的 stdout/stderr 仍直落同一文件）
_log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [$TRIGGER_TAG] $*" >> "$LOG_FILE"
}

# .env 缺失/不可读的告警在此补报（解析处 _log 尚未定义，见上面 ENV_WARNING 赋值处）。
# fail-open（继续用默认值跑）+ 双声音：stderr + 当日日志。
if [ -n "${ENV_WARNING:-}" ]; then
    _err "警告: $ENV_WARNING"
    _log "警告: $ENV_WARNING"
fi

# ---- 告警频次口径（2026-10-05 裁定）----
# 判一处告警是否合规前，先分事件性质。两类各按各的档位出声，不许由同一个理由得出两种结论。
#   一、状态突变类（配置面整体丢失）：每次响亮，不去重、不设进程内闩。
#       承载者 `scripts/child_env.py`：.env 读不到即全局配置失效。
#       理由：常驻调度器一旦"只喊一次"，退化状态可在其后数月的静默中持续。
#   二、反复可容忍类：允许静默或去重。
#       承载者 `scripts/yiban-fallback.sh`（cron 高频拉起，开关关闭时静默退出）
#       与本脚本逐轮的兜底接线核对。
#       理由：触发频繁，逐次出声会变成周期邮件噪声。
# 判据：问"这次失败会不会反复发生、且每次都值得知道"。不会 ⇒ 反复可容忍。

# 任何退出路径都落一行带退出码的日志。原先只有正常收尾那一行，于是 flock 跳过（0）、
# 当日已签到成功跳过（0）、当日已收尾跳过（0）、timeout 击杀（124）全都静默收场——
# 日志里看不出"这一轮跑过没有、为什么没签"。退出码契约 0/1/2/3/10 逐字不变：这里只多写
# 日志。收到 SIGTERM/SIGINT 也落一行（容器 stop / 宿主重启杀掉本进程时，日志是唯一
# 现场），退出码沿用 128+信号号——与"被信号杀死"在父进程看来本来就得到的码一致。
_on_exit() {
    local rc="$1"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [$TRIGGER_TAG] === run.sh 退出，退出码: $rc ===" >> "$LOG_FILE" || true
    echo "" >> "$LOG_FILE" || true
    return 0
}
trap '_on_exit $?' EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

# 识别「当日已触发过」标记。
# 06:31 与 07:12 是同一脚本的两次 cron 调用，仅靠 sign-status 状态文件无法区分——
# 状态文件只记"本次触发的结果"文本（**取值表只住一处**：见下方 `_write_status_from_exit`
# 上方），不记"第几轮"：首签轮被 timeout 击杀（exit 124）或异常失败（exit 1）后，07:12
# 那一轮读到的文本与首签轮无异（f7va 前这两种码根本不写文件；现在会落盘一份非 SUCCESS
# 文本，但仍不区分轮次）。
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
RUN_MARKER="$STATE_DIR/yiban-run-today-$(business_day).marker"
if ( set -o noclobber; : > "$RUN_MARKER" ) 2>/dev/null; then
    : # 今日首次触发：本轮按首签轮运行
elif [ -f "$RUN_MARKER" ]; then
    # 创建失败且文件确实在——才是真"当日已触发过"
    export YIBAN_SECOND_RUN=1
else
    # MF-81②：noclobber 失败但文件不存在 = 写失败（目录竞态被搬走、磁盘满、路径
    # 被占成目录等），不是"已触发过"。旧实现把两者混为一谈：静默导出
    # YIBAN_SECOND_RUN=1 → 进程内补签轮被关掉（漏签，而现网补签只剩这一条通道）。
    # fail-closed：无法判定首签/补签 ⇒ 不跑并告警（stderr + 日志 + 非零码）。
    _err "警告: 无法写入当日触发标记 $RUN_MARKER，无法判定首签/补签，拒绝运行"
    _log "警告: 当日触发标记写入失败（$RUN_MARKER 不存在且不可创建），拒绝运行本轮"
    exit 1
fi

# 单实例锁：自动错峰模式下 06:31 进程可能 sleep 等待时间点，
# 防止 07:12 的 cron 并发启动第二个进程（重复签到/并发竞争）
# 使用 /var/lock（仅 yiban 用户可写），避免 /tmp 下可被任意用户预测/占用导致 DoS。
# fail-closed：主锁目录创建失败即拒绝运行（rc=1），不回退共享临时目录——旧回退分支
# 与上面注释的威胁模型自相矛盾（/tmp 恰是"可被预测/占用"的位置），且回退 mkdir
# 结果未检查（建不成也在假锁路径上继续 flock）。需要替代路径的人显式设
# YIBAN_LOCK_DIR，该路径属主/权限风险自担；本脚本对显式路径同样执行
# "属主为本用户 + chmod 700 成功"的同一道硬检查，不给第二套判法。
LOCK_DIR="${YIBAN_LOCK_DIR:-/var/lock/yiban}"
if [ ! -d "$LOCK_DIR" ]; then
    if ! mkdir -p "$LOCK_DIR" 2>/dev/null; then
        _err "致命: 无法创建锁目录 $LOCK_DIR，拒绝运行（如需替代路径请显式设置 YIBAN_LOCK_DIR）"
        _log "致命: 无法创建锁目录 $LOCK_DIR，拒绝运行"
        exit 1
    fi
fi
# M07：属主 + 700 硬检查对【已存在】锁目录同样执行——旧实现只在新建分支执行，
# 同机其他用户预建锁目录（或符号链接）即可抢占锁使签到长期静默跳过。
if ! { [ -O "$LOCK_DIR" ] && chmod 700 "$LOCK_DIR" 2>/dev/null; }; then
    _err "致命: 锁目录 $LOCK_DIR 不安全（非本用户属主或权限收紧失败），拒绝运行"
    exit 1
fi
exec 9>"$LOCK_DIR/sign.lock"
flock -n 9 || {
    _log "=== 已有签到进程在运行，本次跳过 ==="
    exit 0
}

# 状态文件：记录今天的签到结果，避免重复执行
STATUS_FILE="$STATE_DIR/sign-status-$(business_day).txt"
# 当日收尾标记：本脚本（含进程内补签轮）已把当天该做的都做完（无论成败）。
# 作用：让 07:12 的 cron（兜底）在后继场景下不再多跑第三轮——
# 例如首轮 06:35 就结束、且已完成补签轮，此时 07:12 的 cron 会拿到锁，
# 没有本标记它会因为状态非 SUCCESS 再跑一轮（幂等但多一轮真实登录请求）。
SECOND_DONE_MARKER="$STATE_DIR/yiban-settled-$(business_day).marker"
# sign-status 采信的交叉核对源库（MF-82）：与引擎同一个键 YIBAN_DB_FILE、同一
# 默认值口径（引擎 resolve_path = 进程环境 → .env → env_io.DEFAULT_DB_FILE
# "data/yiban.db"；此处 .env 已 export，cwd 即 APP_DIR，与引擎子进程的相对路径
# 基准一致）。库与 .env 同住检出内的 data/（工单 ybg8）。
DB_FILE="${YIBAN_DB_FILE:-data/yiban.db}"
# 库路径显式导出：与上面的 .env 指针同理——子进程按同一份库取数，不靠默认值对齐。
export YIBAN_DB_FILE="$DB_FILE"

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
# 缺省值必须与 yiban/window.DEFAULT_RETRY_HM 一致（signin 的告警抑制按该值判断
# "是否还有下一轮兜底"；tests/test_retry_slot_time.py 会比对两处字面量）。
SECOND_HHMM="${YIBAN_SECOND_RUN_TIME:-07:12}"
# 导出给子进程：signin 从环境读同一键（见 yiban.core.window.retry_hm），
# 使"改了补签时刻"在所有取用点同时生效
export YIBAN_SECOND_RUN_TIME="$SECOND_HHMM"
# 逃生开关：显式置 0 可关闭进程内补签轮（仅调试/特殊运维场景用）
SECOND_ROUND_ENABLED="${YIBAN_HOST_SECOND_ROUND:-1}"

# 开关真值判定。字面量口径的**单一事实源**在 yiban/adapters/config/env_io.py 的
# ENV_TRUTHY_LITERALS（Python 侧 parse_env_flag 用它）；bash 无法 import，此处逐字
# 复刻同一份（1/true/yes/on，大小写不敏感、两侧空白忽略）。两边结论一致性由
# tests/test_pause_flag_truthiness_e2e.py 真跑本函数与 Python 逐值比对钉住。
# 勿在此另加/删字面量——改了它就必须同步改 env_io 的名册。
_is_truthy() {
    local v="${1:-}"
    # 去首尾空白（与 Python .strip() 同口径；run.sh 加载 .env 时已剥一次，这里兜住
    # "直接经进程环境传入且带空白"的调用方）
    v="${v#"${v%%[![:space:]]*}"}"
    v="${v%"${v##*[![:space:]]}"}"
    case "$(printf '%s' "$v" | tr '[:upper:]' '[:lower:]')" in
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
    tgt_s="$(TZ=Asia/Shanghai date -d "today $th:$tm" +%s 2>/dev/null)" || return 0
    [ -n "$tgt_s" ] || return 0
    if [ "$now_s" -lt "$tgt_s" ]; then
        _log "补签轮：等待至 $target 再执行（约 $(( (tgt_s - now_s) / 60 )) 分钟）"
        sleep $(( tgt_s - now_s ))
    fi
    return 0
}

# 补签轮判定（调用 signin.py --second-run-check）：退出码 10 = 需要补跑
# SEAL_CHECK_RC 记录**最近一次库内事实判定**的原始退出码（"" = 本轮还没判过）：
# 收尾标记的封存闸门与补签判定消费同一次判定结果，不各查各的（判定口径唯一：
# signin 的 need_second_run = 领取池 + 状态文件的库内事实，不是子执行体自报的 rc）。
SEAL_CHECK_RC=""
_need_second_round() {
    _is_truthy "$SECOND_ROUND_ENABLED" || return 1
    # MF-81⑤：YIBAN_SECOND_RUN 统一走 _is_truthy 一处解析（与 .env/脚本自身的
    # "1" 兼容，也接受 true/yes/on；不再存在"= 1 字面比较"的第二套取值域）
    _is_truthy "${YIBAN_SECOND_RUN:-0}" && return 1   # 本轮本身就是补签轮 → 不再评估
    # 全站暂停（管理员一键暂停）：signin 会立刻 exit 2，补跑没有任何意义，
    # 只会把锁多占一会儿（原逻辑下 07:12 的 cron 也会同样空跑一次，属既有行为）
    _is_truthy "${YIBAN_GLOBAL_PAUSE:-0}" && return 1
    "$PY" scripts/signin.py --second-run-check >> "$LOG_FILE" 2>&1
    SEAL_CHECK_RC=$?
    [ "$SEAL_CHECK_RC" -eq 10 ]
}

# 执行一轮签到；每轮按当前时刻重算超时（原实现只在脚本开头算一次，
# 补签轮复用它会让第二轮的可用时长被高估）
# 并行执行体数量的合法域：向 Python 取，本脚本不再自己抄一份。
# 唯一事实源是 `yiban/egress.py` 的 WORKERS_MIN/WORKERS_MAX（census P1-3）：旧写法把
# 2~64 硬编码在这里，而容器侧走 egress，两边对同一份 `.env` 给出不同的执行体数。
# 取不到 ⇒ 返回 1（解释器调用失败）或 2（打印的不是两个整数），由调用方响亮告警并按
# 单执行体执行；**绝不静默换成另一套缺省数**。方向取"少开"：每多一个执行体就多一路
# 真实登录（风控面），少开看得见，多开看不见。
# 成功时置 WORKERS_MIN_S/WORKERS_MAX_S，并把原始打印留在 WORKERS_DOMAIN_RAW 供告警点名。
WORKERS_MIN_S=""
WORKERS_MAX_S=""
WORKERS_DOMAIN_RAW=""
workers_domain() {
    local _out _re='^[0-9]+ [0-9]+$'
    WORKERS_MIN_S=""; WORKERS_MAX_S=""; WORKERS_DOMAIN_RAW=""
    # 解释器与业务日取时同一条回退链（$PY + 仓库根进 sys.path）；stderr 丢弃：
    # 失败的样子由返回码与下面的告警描述，不让 traceback 混进判定值。
    _out="$("$PY" -c "import sys; sys.path.insert(0, sys.argv[1]); from yiban.egress import WORKERS_MIN, WORKERS_MAX; print(WORKERS_MIN, WORKERS_MAX)" "$APP_DIR" 2>/dev/null)" || {
        return 1
    }
    # 兼容 CRLF 与多行打印：折成一行后仍要求整串是"整数 空格 整数"，否则算取不到
    _out="${_out%$'\r'}"
    _out="${_out//$'\n'/ }"
    _out="${_out#"${_out%%[![:space:]]*}"}"
    _out="${_out%"${_out##*[![:space:]]}"}"
    if [[ ! "$_out" =~ $_re ]]; then
        WORKERS_DOMAIN_RAW="$_out"
        return 2
    fi
    WORKERS_MIN_S="${_out%% *}"
    WORKERS_MAX_S="${_out##* }"
    return 0
}

_run_signin_round() {
    local end_hhmm="${YIBAN_SIGN_END:-07:50}" run_timeout end_ts now_ts raw
    if ! echo "$end_hhmm" | grep -qE '^([01]?[0-9]|2[0-3]):[0-5][0-9]$'; then
        _log "警告: YIBAN_SIGN_END=$end_hhmm 非法，回退默认 07:50"
        end_hhmm="07:50"
    fi
    # M25：窗口/时刻判定用北京钟（business_day 与引擎同口径），动态超时与补签等待
    # 也必须用北京钟——两处 `date -d "today HH:MM"` 若按宿主本地时区算，宿主非 UTC+8 时
    # end_ts - now_ts 可为负，被下面的下限夹到 600s，把窗口内的整轮腰斩成静默漏签。
    end_ts=$(TZ=Asia/Shanghai date -d "today $end_hhmm" +%s)
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
            _log "警告: YIBAN_RUN_TIMEOUT_SEC=$raw 非法（须为 ≥600 的整数），回退动态计算 ${run_timeout}s"
        fi
    fi
    _log "签到超时: ${run_timeout}s（窗口至 $end_hhmm）"
    # 多执行体（可选）：YIBAN_WORKERS>1 时由 signin 的监督模式拉起 N 个并行执行体，
    # 分工靠数据库里的领取池（账号不会被两个执行体同时登录）。默认 1 = 现状不变。
    # 非法值只告警并回退单执行体：绝不能因为一个配置笔误让当天不签到。
    # 合法域的上下限由 `workers_domain` 向 Python 取（见该函数），这里不写死数字。
    workers_args=()
    workers_raw="${YIBAN_WORKERS:-1}"
    if [ -n "$workers_raw" ] && [ "$workers_raw" != "1" ]; then
        workers_domain
        _wd=$?
        if [ "$_wd" -eq 0 ]; then
            if [[ "$workers_raw" =~ ^[0-9]+$ ]] &&
                [ "$workers_raw" -ge "$WORKERS_MIN_S" ] && [ "$workers_raw" -le "$WORKERS_MAX_S" ]; then
                workers_args=(--workers "$workers_raw")
            else
                _log "警告: YIBAN_WORKERS=$workers_raw 非法（须为 ${WORKERS_MIN_S}~${WORKERS_MAX_S} 的整数），按单执行体执行"
            fi
        elif [ "$_wd" -eq 2 ]; then
            _log "警告: 取不到 YIBAN_WORKERS 的合法域（$PY 打印的不是「MIN MAX」: '${WORKERS_DOMAIN_RAW}'），按单执行体执行"
        else
            _log "警告: 取不到 YIBAN_WORKERS 的合法域（$PY 读取 yiban.egress 失败），按单执行体执行"
        fi
    fi
    if [ ${#workers_args[@]} -gt 0 ]; then
        _log "多执行体: ${workers_raw} 个并行执行体"
    fi
    timeout "$run_timeout" "$PY" scripts/signin.py ${workers_args[@]+"${workers_args[@]}"} >> "$LOG_FILE" 2>&1
    return $?
}

# 状态文件记的是**"本次触发的结果"**，不是"当日最终结论"（工单 yiban-auto-sign-f7va，
# 2026-10-12）。它是审计/巡检的机器可读留痕。**唯一读者**是"当日已 SUCCESS ⇒ 跳过本轮"
# 的采信闸门（`_status_credible_success` 只认字面 SUCCESS）；**补签判定与封存判定都不
# 读它**——补签走 `signin --second-run-check`（读 sign-state json 与库内未了结计数），
# 封存走 `--second-run-check` 的判 0/10。故这里多写的文本不改变任何"跑/不跑/补签"判定。
#
# 取值表——**逐码派生**自 `yiban/engine/cli_support.py::_EXIT_KINDS`（码表本身"只增不改"
# 既有码含义）。**不许有 catch-all 把已知码悄悄归进"失败"**：
#   码   _EXIT_KINDS kind     状态文本            理由
#   0    ok                   SUCCESS            确实执行并成功
#   1    failure              FAILED             有真实失败（账号/网络/配置）
#   2    skipped              SKIPPED            全部跳过 / 存在窗口外未了结
#        （YIBAN_GLOBAL_PAUSE 时写 GLOBAL_PAUSED——人为暂停与普通跳过分开）
#   3    locked               BUSY               队列忙：本轮**没跑**（另有持有者持有运行锁，
#                                                或互斥不可得）。**不是失败**
#   4    schema_migration     SCHEMA_MIGRATION   迁移拒启：本轮没跑，需运维介入（不伪装成账号失败）
#   10   second_run_check     NEED_SECOND_RUN    判定码，**不是轮结果码**（只由
#                                                `--second-run-check` 产生）。正常轮不可达
#                                                （见下），出现即管道接错——响亮记日志
#   124  （不在码表内）         TIMEOUT            timeout(1) 击杀整轮（外部杀）
#   其余非零  unknown          FAILED             未知码：安全方向（见下），绝不写 SUCCESS
#
# rc=3/rc=4/rc=10 曾被 `else` 一并吞成 FAILED——那是**假失败/归族**：3 是"另有持有者正在跑"、
# 4 是"schema 半升级"、10 是"判定码漏进结果通道"，三者都不是账号签到失败（本项目 10-07
# 栽过"执行体领不到任务被上报成批假失败"那族缺陷，见记忆 fallback-gates-and-yield）。
# 故逐码分列，已知码**一律不许**被 catch-all 归族。
# rc=10 不可达证据（当前代码）：run.sh 的轮调用不带 `--second-run-check`（见 `_run_signin_round`
# 的 `"$PY" scripts/signin.py ${workers_args...}`）；监督进程给子进程的命令行只是剔掉
# `--workers`（workers.py 的 child_argv），故子进程也不带它；`SECOND_RUN_CHECK_NEED` 只在
# `runner.main` 的 `args.second_run_check` 分支返回。但**契约承认**"轮退出码 10"这一形态
# （监督进程汇总会原样透出子码 10，workers.py；用例 `([10,-9],10)` 钉住），故仍显式分列。
# 旧口径"非 0/2 不写文件"另让失败轮当天两件判据全缺（v0.6.2 生产实测 10-12：✅229/❌1、
# 两件产物都缺），同批改为"每个跑过一轮的码都落盘"。
# 退出码 2 同时覆盖「存在窗口外/缺失未了结账号」的混合场景（部分成功 + 部分
# skipped_window/norange）——signin.py 此时返回 2，这里写 SKIPPED 而非 SUCCESS，
# 补签才会重跑；窗口外账号不因"有账号成功"而失去当天兜底（容器调度器同语义）。
#
# 未知码为何落 FAILED（安全方向）：未知意味着"本轮没走完正常收场链路"。写 SUCCESS 会
# 谎报成功（不可逆地吃掉当天兜底）；写 FAILED 只多一条"未成功"留痕，且采信侧本就不认
# 非 SUCCESS ⇒ 方向偏安全。护栏测试显式钉住"未知码→FAILED"。
#
# 原子写：写临时文件 + mv，防止掉电/被杀时文件处于半写状态。
# MF-81：mktemp/echo/mv 三步全部判码——状态文件决定后续触发"跑/不跑"，任何一步
# 失败都必须显式失败返回（rc 1），由调用方升级退出码，绝不静默续跑。
_status_write() {
    local content="$1" tmp
    if ! tmp=$(mktemp "${STATUS_FILE}.tmp.XXXXXX" 2>/dev/null); then
        _err "警告: 状态文件临时件创建失败（mktemp ${STATUS_FILE}.tmp.*）"
        return 1
    fi
    if ! echo "$content" > "$tmp" 2>/dev/null; then
        _err "警告: 状态文件临时件写入失败: $tmp"
        rm -f "$tmp" 2>/dev/null
        return 1
    fi
    # 目标是已存在目录时 `mv -f` 会把临时件**搬进目录里**并返回 0（GNU 语义）——
    # 状态看似写成功、实际没写：这正是"写失败==已完成"的等价类，必须显式拒绝。
    if [ -d "$STATUS_FILE" ] || ! mv -f "$tmp" "$STATUS_FILE" 2>/dev/null; then
        _err "警告: 状态文件原子替换失败: $STATUS_FILE"
        rm -f "$tmp" 2>/dev/null
        return 1
    fi
    return 0
}

_write_status_from_exit() {
    local exit_code="$1" want=""
    # 逐码派生自 cli_support._EXIT_KINDS（见表与护栏测试）；无 catch-all 归族：
    # 3=locked、4=schema_migration 各有专属文本，不被"其余非零"吞进 FAILED。
    case "$exit_code" in
        0)   want="SUCCESS" ;;          # _EXIT_KINDS 0 = ok
        1)   want="FAILED" ;;           # _EXIT_KINDS 1 = failure
        2)                              # _EXIT_KINDS 2 = skipped
            # 语义区分（0.22.0 审查修复）：全局暂停（YIBAN_GLOBAL_PAUSE）写 GLOBAL_PAUSED，
            # 与窗口/配置导致的普通 SKIPPED 分开——运维/监控可区分"人为暂停"与"技术性跳过"。
            # MF-81⑤：与 _need_second_round 统一走 _is_truthy 一处解析，不再两套取值域。
            if _is_truthy "${YIBAN_GLOBAL_PAUSE:-0}"; then
                want="GLOBAL_PAUSED"
            else
                want="SKIPPED"
            fi ;;
        3)   want="BUSY" ;;             # _EXIT_KINDS 3 = locked（队列忙，本轮没跑，非失败）
        4)   want="SCHEMA_MIGRATION" ;; # _EXIT_KINDS 4 = schema_migration（迁移拒启，需运维）
        10)  # _EXIT_KINDS 10 = second_run_check：**判定码，不是轮结果码**。正常轮不可达
             # （见上方注释的不可达证据）；出现即"判定码漏进了结果通道"＝管道接错，
             # 不得伪装成账号失败，故独立文本 + 响亮留痕（stderr + 当日日志）。
            want="NEED_SECOND_RUN"
            _err "警告: 轮退出码 10（second_run_check）是 --second-run-check 的判定码，不是轮结果码；正常轮不该以它收场，出现即管道接错"
            _log "警告: 轮退出码 10（second_run_check）出现——判定码被当成轮结果码透出，请核管道/调用配置"
            ;;
        124) want="TIMEOUT" ;;          # _EXIT_KINDS 外：timeout(1) 击杀整轮
        *)   want="FAILED" ;;           # 未知非零：安全方向（唯一允许的兜底档）
    esac
    if ! _status_write "$want"; then
        _log "警告: 状态文件写入失败（本轮结果 $want 未落盘），本轮不得按已完成处理"
        return 1
    fi
    return 0
}

# ---- sign-status 采信（MF-82）----
# 状态文本单独不可信：伪造（或在磁盘满时"丢失又补回"）一个 SUCCESS，就能让当天
# 全部账号一次不签。修法三选定的交叉核对方式：只读 sqlite 查当日**库内了结事实**
# （sign_tasks 的 done/skipped，兼容 v17 写者 sign_claims 的 done）——数量 > 0 才
# 与 SUCCESS 相称。查询失败（python 缺失/库不存在/库损坏）与计数为 0 同样按
# "不一致"处理：拒绝采信 + 双声音告警 + 按未完成继续（fail-closed 的"不轻信"侧）。
_db_settled_today() {
    "$PY" - "$DB_FILE" "$(business_day)" 2>/dev/null <<'PYEOF'
import os
import sqlite3
import sys
from urllib.parse import quote

db, day = os.path.abspath(sys.argv[1]), sys.argv[2]
try:
    con = sqlite3.connect("file:" + quote(db) + "?mode=ro", uri=True, timeout=5)
except sqlite3.Error:
    sys.exit(1)
n = 0
try:
    for table, cond in (("sign_tasks", "state IN ('done','skipped')"),
                        ("sign_claims", "state = 'done'")):
        try:
            n += con.execute(
                "SELECT COUNT(*) FROM " + table + " WHERE day=? AND " + cond,
                (day,)).fetchone()[0]
        except sqlite3.Error:
            pass   # 表不存在（未跑过对应迁移）按 0 计
    print(int(n))
except sqlite3.Error:
    sys.exit(1)
finally:
    con.close()
PYEOF
}

_status_credible_success() {
    local content facts
    [ -f "$STATUS_FILE" ] || return 1
    content=$(cat "$STATUS_FILE" 2>/dev/null) || return 1
    [ "$content" = "SUCCESS" ] || return 1   # 非 SUCCESS 文本：不是"谎报成功"，静默不采信
    if ! facts=$(_db_settled_today); then
        _err "警告: sign-status 声称 SUCCESS，但当日库内事实查询失败（$DB_FILE），拒绝采信"
        _log "警告: sign-status 拒绝采信（库内事实查询失败: $DB_FILE），按未完成处理"
        return 1
    fi
    case "$facts" in ''|*[!0-9]*) facts=0 ;; esac
    if [ "$facts" -le 0 ]; then
        _err "警告: sign-status 声称 SUCCESS，但库内当日无已了结任务（疑似伪造/搬运 STATE_DIR），拒绝采信"
        _log "警告: sign-status 与库内当日事实不符（了结数=$facts），拒绝采信、按未完成处理"
        return 1
    fi
    return 0
}

# 当日收尾标记已存在 → 当天该做的都已做完（含补签轮），本次触发无需再跑。
# 仍然先看 SUCCESS：两者语义重叠但保留原判定，避免行为回归。
if [ -f "$SECOND_DONE_MARKER" ]; then
    _log "今天已完成签到收尾（含补签轮），跳过执行 ==="
    exit 0
fi

# 检查今天是否已经签到成功（MF-82：文本 SUCCESS 必须与库内当日事实交叉核对后才采信）
if _status_credible_success; then
    _log "今天已签到成功，跳过执行 ==="
    exit 0
fi

# 记录脚本开始执行
_log "=== run.sh 开始执行 ==="
_log "工作目录: $(pwd)"
_log "Python版本: $("$PY" --version 2>&1)"

# ---- 兜底常驻的接线核对：`YIBAN_FALLBACK_ENABLE=1` 而无进程 ⇒ 启动即告警并拉起 ----
# 旧语义：开关只是 .env 里的一个键，必须部署者再手工挂一条 cron（scripts/yiban-
# fallback.sh）才真有进程——网页把开关显示成"已启用"而机器上什么都没有。现在每次
# 拿到锁、确实要跑本轮时核对一次。存活判定复用唯一口径 `state_io.fallback_alive`
# （心跳文件新鲜度 ≤ 2×扫描间隔；以文件内时间戳而非"文件在不在"为准——被 kill -9
# 的进程不会清心跳）。不在跑 ⇒ 双声音告警 + 后台拉起与 cron 件**同一条入口**
# （`-m yiban.entry.cli sign --fallback`）。周末/暂停/窗口门不在此重复实现：引擎每轮自
# 重判并自行退出，误拉起一个"该休息"的进程只会立刻安静结束；独立锁
# `signin-run.lock.fallback` 保证撞上已在跑的兜底以退出码 3 结束，绝不叠进程。
# 子进程不得继承 fd 9（本轮持有的全局运行锁）：兜底与全量轮并存靠让位+领取池，
# 不是替全量轮继续持锁——否则本脚本退出后锁仍被兜底攥着，下一触发整天撞锁。
_check_fallback_wiring() {
    _is_truthy "${YIBAN_FALLBACK_ENABLE:-0}" || return 0
    if "$PY" - >> "$LOG_FILE" 2>&1 <<'PYEOF'
import os
import sys
sys.path.insert(0, os.getcwd())
from yiban.adapters.statefiles import state_io
_alive, _age = state_io.fallback_alive()
sys.exit(0 if _alive else 1)
PYEOF
    then
        return 0
    fi
    _err "警告: YIBAN_FALLBACK_ENABLE=1 但未检测到兜底执行体在跑（心跳缺失或已过期），本轮启动即拉起"
    _log "警告: 兜底开关置 1 而无兜底进程（心跳缺失/过期），拉起兜底常驻执行体（-m yiban.entry.cli sign --fallback）"
    nohup "$PY" -m yiban.entry.cli sign --fallback < /dev/null >> "$LOG_FILE" 2>&1 9<&- &
    return 0
}
_check_fallback_wiring

# ---- 第一轮（首签轮）----
_run_signin_round
EXIT_CODE=$?
if ! _write_status_from_exit "$EXIT_CODE"; then
    # MF-81：本轮结果未能落盘——"成功但没人记得"不得按成功收场（写失败 == 已完成
    # 的等价类禁止存在）。退出码升为 1 给监控声音；下一触发因无状态文件按未完成
    # 继续判定，这是带告警的降级，不是静默。
    if [ "$EXIT_CODE" -eq 0 ]; then EXIT_CODE=1; fi
fi

# ---- 第二轮（补签轮，进程内）----
# 首轮结束后仍持锁，任何其他触发（含 07:12 的 cron）都被 flock 挡在外面，
# 因此"判定 → 等待 → 补跑"整段是原子的，不存在两个进程同时补跑的窗口。
if _need_second_round; then
    _wait_until_hhmm "$SECOND_HHMM"
    # 等待期间其它进程进不来（锁在本进程手上）；若等待前状态已是 SUCCESS 则不必补跑
    # （MF-82：同样必须过库内事实交叉核对，缺省/伪造的 SUCCESS 不再短路补签轮）
    if _status_credible_success; then
        _log "补签轮：状态已是 SUCCESS，跳过"
    else
        _log "=== 开始补签轮（第二轮）==="
        export YIBAN_SECOND_RUN=1   # signin 据此判定 is_second_run（告警口径/剔除已成功账号）
        _run_signin_round
        EXIT_CODE=$?
        if ! _write_status_from_exit "$EXIT_CODE"; then
            # 同首签轮：补签轮结果未落盘，"成功"不得静默收场
            if [ "$EXIT_CODE" -eq 0 ]; then EXIT_CODE=1; fi
        fi
        _log "=== 补签轮结束，退出码: $EXIT_CODE ==="
    fi
else
    # P12-18：_need_second_round 的四条返回 1 的路径里，只有"判定码 != 10"跑过
    # --second-run-check（SEAL_CHECK_RC 有值）；开关关闭/本轮即补签轮/全站暂停三条
    # 根本没跑判定（SEAL_CHECK_RC 为空）。else 一律说"已收尾且无未了结"是把未判
    # 说成已判——恰是排查漏签时最需要区分的一种假象。
    case "${SEAL_CHECK_RC}" in
        "") _log "补签轮：无需补跑——未做库内事实判定（补签开关关闭、本轮即补签轮或全站暂停）" ;;
        10) _log "补签轮：无需补跑——库内判定为需要补跑但条件未成立（不应到达）" ;;
        *)  _log "补签轮：无需补跑——库内判定退出 ${SEAL_CHECK_RC}（无未了结账号）" ;;
    esac
fi

# ---- 当日收尾标记的封存：前置 = 库内事实"确实无未了结" ----
# 旧实现在这里**无条件**封存——封存的前置必须是"确实无未了结"，而"无未了结"以
# **库内查询**为准（--second-run-check：领取池 + 状态文件），不是子执行体的自报退出码：
# 整批没领、部分没轮到、被信号杀后半途而废，rc 都可能粉饰成"跑完了"，一封存就把
# 07:12 的当日恢复腿（flock 即存活判据，唯一"主进程消失后才动"的腿）永久弹开。
# 判定 0 ⇒ 封存；判定 10（仍有未了结）⇒ 不封存、把恢复留给后继触发——这是设计结果，
# 不是失败，不改退出码；判定不可得（其余码/判不了）⇒ 不封存 + 双声音，本轮若以成功
# 收场则退出码升 1，"无法判定"绝不静默等价于"做完了"。
# 真正封存时写失败仍须双声音留痕，本轮成功则升 1。
# 判定边界：封存依据是**判定时点**的库内事实——判定 0 到写标记之间，正在常驻的兜底
# 仍可能接手一个判定时尚未建行的账号，这一残竞有界（至多一次接手动作），由后继运行
# 的"状态 vs 库内"交叉核对兜住：状态 SUCCESS 不过库内判定，不得短路补签轮。
_seal_second_done_marker() {
    if [ "$SEAL_CHECK_RC" != "0" ]; then
        "$PY" scripts/signin.py --second-run-check >> "$LOG_FILE" 2>&1
        SEAL_CHECK_RC=$?
    fi
    if [ "$SEAL_CHECK_RC" = "0" ]; then
        # census-P2-settled-marker：`: > "$SECOND_DONE_MARKER"` 是裸覆盖（非 noclobber、
        # 非 tmp+mv），写到一半被杀会留下半截/空文件；权限只靠 umask 077，不显式。
        # 改 mktemp + mv（同目录原子 rename）+ 显式 0600。
        local _tmp_marker="" _marker_ok=0
        if [ -e "$SECOND_DONE_MARKER" ] && [ ! -f "$SECOND_DONE_MARKER" ]; then
            # 目标被占成目录/设备：mv 会把临时件挪进去且"看似成功"，先拒。
            _marker_ok=0
        elif _tmp_marker="$(mktemp "${SECOND_DONE_MARKER}.tmp.XXXXXX" 2>/dev/null)" \
            && : > "$_tmp_marker" && chmod 600 "$_tmp_marker" \
            && mv -f "$_tmp_marker" "$SECOND_DONE_MARKER" \
            && [ -f "$SECOND_DONE_MARKER" ]; then
            _marker_ok=1
        fi
        if [ "$_marker_ok" -eq 0 ]; then
            [ -n "$_tmp_marker" ] && rm -f "$_tmp_marker"
            _err "警告: 当日收尾标记写入失败（$SECOND_DONE_MARKER），下一触发可能重判未收尾而多跑一轮"
            _log "警告: 当日收尾标记写入失败，下一触发或重复整轮签到"
            if [ "$EXIT_CODE" -eq 0 ]; then EXIT_CODE=1; fi
        fi
        return 0
    fi
    if [ "$SEAL_CHECK_RC" = "10" ]; then
        _log "收尾标记：库内仍有未了结账号（判定 10），不封存，留给 07:12 兜底轮继续处理"
        return 0
    fi
    _err "警告: 收尾标记封存判定不可得（--second-run-check 退出 $SEAL_CHECK_RC），不封存；下一触发将重判"
    _log "警告: 收尾标记封存判定不可得（--second-run-check 退出 $SEAL_CHECK_RC），不封存"
    if [ "$EXIT_CODE" -eq 0 ]; then EXIT_CODE=1; fi
    return 0
}
_seal_second_done_marker

# 退出码与收场日志由 _on_exit（trap EXIT）统一收口：正常收尾、超时击杀、各处跳过
# 都在同一处留痕，不再各自 echo 一行、也不再漏掉任一条路径
exit $EXIT_CODE
