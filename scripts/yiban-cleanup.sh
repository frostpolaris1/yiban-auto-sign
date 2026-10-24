#!/bin/bash
# 按天数据清理（cron 每天执行一次，如 `0 3 * * * yiban /bin/bash /opt/yiban-auto-sign/scripts/yiban-cleanup.sh`）。
#
# 本脚本只是薄包装：清理**策略与实现**唯一在 `yiban/adapters/statefiles/state_gc.py`
# （清单 = 按天签到日志、按日结构化状态、旧版按日状态、用户失败提醒额度账本、
#  当日全量收尾标记、容器时段闩锁标记、调度快照；另含写盘中断留下的半成品
#  与空的熔断状态文件）。原先规则写在本脚本里、容器侧另写一份且只覆盖 3 个模式——
#  新增一类按日文件时没有机制提醒补规则，这正是状态目录无界增长（目录条目随天数
#  线性膨胀、web 日历按前缀扫描整目录变慢）的成因。
#
# 环境变量（与 run.sh 同口径，见 scripts/state_gc.py）：
#   YIBAN_STATE_DIR（默认 /var/log/yiban）、YIBAN_LOG_FILE（默认 <state>/sign.log）
#   YIBAN_RETENTION_DAYS（默认 365）、YIBAN_SNAPSHOT_RETENTION_DAYS（默认 7）
# 部署：以 yiban 用户运行（与 run.sh/签到日志同属主）；清理结果写入 <state>/cleanup.log，
#       绝不写 sign-*.log——root 预创建当日签到日志（umask 077）会让 run.sh 全部重定向
#       失败、signin 静默不执行（2026-08-17 事故：当天自动签到整体丢失）。
# 解释器：优先项目虚拟环境（与 run.sh 同一套解析），缺失时回退系统 python3。
umask 077

# 应用目录：唯一环境键 YIBAN_APP_DIR；未设时按本脚本所在检出反推
# （`scripts/lib/host_paths.sh`，与 run.sh / 兜底脚本同一算法，census-P2-repo-root）。
. "$(dirname "${BASH_SOURCE[0]}")/lib/host_paths.sh"
APP_DIR="$(yiban_app_dir)"
# 工作目录与 run.sh 同形：相对路径配置（YIBAN_STATE_DIR 写成相对值）按检出解析。
# cron 的 cwd 是 $HOME，不 cd 会让同一个 `.env` 在 run.sh 与清理之间指向两处。
cd "$APP_DIR" 2>/dev/null || { echo "致命: 无法进入应用目录 $APP_DIR" >&2; exit 1; }

# `.env` 指针：状态目录与日志文件可以只写在 `.env` 里。
# 为什么必须传：不传时 `scripts/state_cleanup.py` 经 `env_io` 回落默认 `/var/log/yiban`
# ——真实状态目录永不清理（磁盘持续增长），默认目录反被误清（R3-019）。
# 解析实现**只此一份**：`state_cleanup.py` 的 `state_dir_from_env` / `log_dir_from_env`
# 就是 `env_io` 的同一函数（引擎、web、容器调度共用）；本脚本只交指针，不抄解析。
# 优先级因此与 run.sh 同口径：进程环境 → `.env` → 默认值。
ENV_PATH="$APP_DIR/data/.env"
export YIBAN_ENV_FILE="$ENV_PATH"
# 告警走带时间戳的 `_warn`：cron 的 stderr 落 `<state>/cleanup.log`，跨日累积时
# 无时间戳的同类行会被读成"同一天被反复调用"。
_warn() { echo "[$(date '+%F %T')] $*" >&2; }
if [ ! -e "$ENV_PATH" ]; then
    _warn "警告: .env 不存在，状态目录回落默认值: $ENV_PATH"
elif [ ! -r "$ENV_PATH" ]; then
    _warn "警告: .env 存在但当前用户不可读，状态目录回落默认值: $ENV_PATH"
fi

if [ -x "$APP_DIR/.venv/bin/python3" ]; then
    PY="$APP_DIR/.venv/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
    PY=python3
elif [ -x /usr/bin/python3 ]; then
    # cron 的 PATH 可能极简（/usr/bin:/bin），command -v 也未命中时兜底绝对路径
    PY=/usr/bin/python3
else
    echo "找不到 python3，无法执行清理（策略实现在 yiban/adapters/statefiles/state_gc.py）" >&2
    exit 1
fi

exec "$PY" "$APP_DIR/scripts/state_cleanup.py" "$@"
