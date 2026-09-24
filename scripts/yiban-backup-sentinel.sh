#!/bin/bash
# 备份失败哨兵（cron 每天 08:05 执行一次）。
#
# 本脚本只是薄包装：检查与告警的唯一实现在 `scripts/backup_sentinel.py`。它防的是
# **静默失败**：`scripts/backup.sh` 的加密配置一旦失效会 fail-closed 不产出任何归档，
# cron 拿不到信号，于是"连续几天没有备份"在页面上看不出来（生产上真静默失败过 4 天）。
#
# 安装（08:05 排期在 02:00 备份之后，留足打包与异机同步的时间）：
#   sudo install -m 0700 -o root -g root scripts/yiban-backup-sentinel.sh \
#       /usr/local/sbin/yiban-backup-sentinel.sh
#   sudo crontab -e
#   5 8 * * * APP_DIR=/opt/yiban-auto-sign /usr/local/sbin/yiban-backup-sentinel.sh >> /var/log/yiban/backup.log 2>&1
#   # 退出码 0 = 检查完成（含"缺失但告警已发出"）；1 = 告警发不出去，此时 cron 自己
#   # 的报错邮件是最后一道声音。日志与 backup.sh 同文件，排查时按时间挨着看。
#
# 环境变量（与 backup.sh / run.sh 同口径）：
#   BACKUP_DIR（默认 /var/backups）、APP_DIR（默认 /opt/yiban-auto-sign）、
#   YIBAN_BACKUP_INSTALLED（默认 /usr/local/sbin/yiban-backup.sh，防漂移比对对象）、
#   YIBAN_ENV_FILE（默认 <APP_DIR>/.env，由本脚本导出）、
#   YIBAN_STATE_DIR（默认 .env 所在目录，即 <APP_DIR>：节流表落
#   <APP_DIR>/notify-throttle.json）——后两个给收件人算法与节流表定位；
#   未配置推送/邮件时告警只落日志（退出码 1）。
#
# 工作目录：本脚本自己 cd 到 APP_DIR，**这是哨兵能出声的前提**。cron 的 cwd 是
# $HOME，而哨兵侧所有 cwd 相对的回落（`yiban/infra/env_io.py` 的 env_path() 默认
# ".env"、`yiban/store/connection.py` 的 DB_DEFAULT "yiban.db"）都以 cwd 为基准：
# 不 cd 就读不到 .env，收件人解析为空 ⇒ 告警发不出去（退出码 1，恰是本脚本要消灭的
# 那种静默），并在 $HOME 就地新建一份野 yiban.db。
# 部署：以 yiban 用户运行（与 run.sh 同属主），日志**不要**写 sign-*.log。
# 解释器：优先项目虚拟环境（与 run.sh 同一套解析），缺失时回退系统 python3。
umask 077

APP_DIR="${APP_DIR:-/opt/yiban-auto-sign}"
# 从仓库里直接跑（未安装）时，没有 APP_DIR 也能定位到仓库根：本脚本就在 scripts/ 下
if [ ! -f "$APP_DIR/scripts/backup_sentinel.py" ]; then
    APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
fi
export APP_DIR
cd "$APP_DIR" || { echo "致命: 无法进入应用目录 $APP_DIR" >&2; exit 1; }
# .env 路径显式导出：哨兵的收件人算法与节流表都按 YIBAN_ENV_FILE 定位 .env（缺省
# 回落 cwd 相对 ".env"，那正是上面 cd 要治的病症）；已由部署方给出时沿用其值。
export YIBAN_ENV_FILE="${YIBAN_ENV_FILE:-$APP_DIR/.env}"

if [ -x "$APP_DIR/.venv/bin/python3" ]; then
    PY="$APP_DIR/.venv/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
    PY=python3
elif [ -x /usr/bin/python3 ]; then
    # cron 的 PATH 可能极简（/usr/bin:/bin），command -v 也未命中时兜底绝对路径
    PY=/usr/bin/python3
else
    echo "找不到 python3，无法执行备份哨兵（实现在 scripts/backup_sentinel.py）" >&2
    exit 1
fi

exec "$PY" "$APP_DIR/scripts/backup_sentinel.py" "$@"
