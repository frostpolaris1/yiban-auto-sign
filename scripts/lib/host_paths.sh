#!/bin/bash
# ============================================================
# scripts/lib/host_paths.sh —— 宿主 bash 脚本共用的路径事实（**bash 侧唯一定义点**）
#
# 被 `run.sh`、`run_probe.sh`、`scripts/yiban-fallback.sh` 直接 source
# （`scripts/backup.sh` 从 `$APP_DIR` 下 source）。它们过去各自抄一份
# `business_day()` 与 `dirname "$LOG_FILE"/sign-<day>.log`：
# 改一次业务日来源或日志名，只有被顺手改到的那一份跟得上。
#
# 为什么 bash 不直接调 Python 取这两件事：
#   宿主入口必须在 venv 损坏时仍能写日志、跑完收尾（`run.sh` 的降级链就为这个存在）。
#   故本文件与 `yiban/adapters/config/env_io.py` 各持一份定义：
#     · Python 侧：`env_io.day_log_name` / `LOG_BASENAME`（引擎、web、清理名册）
#     · bash 侧：本文件（宿主入口）
#   两者逐字相同由 `tests/test_path_single_definition.py::CrossLanguageShapeTest`
#   钉住；任一改动而未改另一侧 ⇒ 该用例红。
#
# 依赖：调用方需在使用 `business_day` 之前定义 `PY` 与 `APP_DIR`
#       （两者都在脚本开头定好；函数体在调用时求值，故 source 顺序不受限）。
# ============================================================

# 仓库/部署根（census-P2-repo-root）：**唯一环境键 YIBAN_APP_DIR**。
# `APP_DIR` / `YIBAN_APP_ROOT` 只作旧部署别名（优先级低于 YIBAN_APP_DIR）；
# 都没设时按本文件位置反推（<root>/scripts/lib/host_paths.sh → 上两级）。
# `run.sh` / `run_probe.sh` 的旧默认是硬编码 `/opt/yiban-auto-sign`，只要它们在检出内
# 执行，反推值与硬编码值相同；检出不在 /opt 时反推仍可用（旧写法会直接失败）。
_yiban_lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
HOST_PATHS_ROOT="$(cd "$_yiban_lib_dir/../.." && pwd -P)"

yiban_app_dir() {
    printf '%s\n' "${YIBAN_APP_DIR:-${APP_DIR:-${YIBAN_APP_ROOT:-$HOST_PATHS_ROOT}}}"
}

# 加载 `.env`（逐行安全解析 + export）。**绝不 source**：网页可写公告等文本，
# 含 `; $(...)` 时 source 会把文本当命令执行（命令注入）。
#
# 用法：yiban_load_env <env 文件> [告警函数名]
#   告警函数收到一个参数（完整告警文本）。不给时非法键静默跳过——
#   cron 每 5 分钟一次，向 stderr 输出会变成周期性邮件噪声。
# 优先级（唯一口径，与 `env_io.resolve_path` / `scripts/child_env.build_child_env` 同形）：
#   · 非路径键：`.env` 优先，进程环境只补缺（M27）。网页设置页写的是 `.env`。
#   · 路径键 YIBAN_DB_FILE / YIBAN_STATE_DIR / YIBAN_LOG_FILE：进程环境优先，
#     `.env` 只补缺（测试/CI 用进程环境把数据目录重定向到临时目录）。
#   · 只导出 `YIBAN_*` 前缀键：防 `.env` 注入 PATH / LD_PRELOAD 等敏感变量。
# 返回：0 = 已读；1 = 存在但不可读；2 = 不存在。
# 读者：run.sh（宿主签到）、scripts/yiban-cleanup.sh（清理 cron）——同一个解析，
#   否则「run.sh 按 .env 找状态目录、清理按默认目录删」两处同时为真。
yiban_load_env() {
    local env_path="$1" warn_fn="${2:-}"
    local key value
    [ -e "$env_path" ] || return 2
    [ -r "$env_path" ] || return 1
    while IFS='=' read -r key value || [ -n "$key$value" ]; do
        # 兼容 CRLF 编辑产生的行尾 CR
        value=${value%$'\r'}
        # 兼容 UTF-8 BOM 开头的 .env（Windows 记事本保存常见）；否则首行键名带 BOM
        # 前缀被键名校验拒掉，宿主 cron 与 web 侧对同一文件读出不同配置（2026-09-08）
        key="${key#$'\xEF\xBB\xBF'}"
        # key/value 首尾空白去除（与 env_io.parse_env_file 的两侧 strip 同口径）
        key="${key#"${key%%[![:space:]]*}"}"
        key="${key%"${key##*[![:space:]]}"}"
        value="${value#"${value%%[![:space:]]*}"}"
        value="${value%"${value##*[![:space:]]}"}"
        [ -z "$key" ] && continue
        case "$key" in \#*) continue ;; esac
        if [[ ! "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
            [ -n "$warn_fn" ] && "$warn_fn" "警告: .env 含非法键名，已跳过: $key"
            continue
        fi
        if [[ ! "$key" =~ ^YIBAN_ ]]; then
            [ -n "$warn_fn" ] && "$warn_fn" "警告: .env 含非 YIBAN_ 前缀键，已跳过导出: $key"
            continue
        fi
        # 四个路径键的优先级例外：进程环境已有非空值时，`.env` 只补缺。
        case "$key" in
            YIBAN_DB_FILE|YIBAN_STATE_DIR|YIBAN_LOG_FILE)
                [ -n "${!key:-}" ] && continue
                ;;
        esac
        export "$key=$value"
    done < "$env_path"
    return 0
}

# 业务日唯一来源（MF-109）：yiban.core.clock 的北京钟，与引擎/web 同一事实源。
# 宿主时区 ≠ 北京（UTC 16:00-24:00 窗口）时宿主 `date` 的"当天"与引擎业务日差一天——
# 状态文件、日志、触发标记、收尾标记与库内"当日事实"查询一律取这里。取不到
# yiban.core.clock（解释器缺失/损坏）时退化 `TZ=Asia/Shanghai date`（需宿主 tzdata），
# 再退化宿主 `date`（保持可运行的降级形态，此时与引擎业务日可能差一天）。
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
    echo "[$(date '+%F %T')] 警告：宿主缺 tzdata，TZ=Asia/Shanghai 不生效（date +%z=${_tz:-空}），业务日退回宿主 date——可能与引擎业务日差一天" >&2
    date +%F
}

# 当日签到日志：`dirname(YIBAN_LOG_FILE)` / `sign-<业务日>.log`。
# `$1` = 状态目录（缺省 `$STATE_DIR`，再缺省裸机默认）。
# 保留 `YIBAN_LOG_FILE` 配置的**目录**语义（默认 `<状态目录>/sign.log`）。
yiban_day_log_file() {
    local _state_dir="${1:-${STATE_DIR:-/var/log/yiban}}"
    local _log_base="${YIBAN_LOG_FILE:-${_state_dir}/sign.log}"
    printf '%s/sign-%s.log\n' "$(dirname "$_log_base")" "$(business_day)"
}
