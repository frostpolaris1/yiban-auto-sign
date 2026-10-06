#!/bin/bash
# 备份哨兵 + 审计锚点离机外发（cron 每天 08:05 执行一次）。
#
# 本脚本只是薄包装：检查、告警与锚点外发的唯一实现在
# `scripts/backup_sentinel.py`。它防的是**静默失败**：`scripts/backup.sh` 的加密配置
# 一旦失效会 fail-closed 不产出任何归档，cron 拿不到信号，于是"连续几天没有备份"在
# 页面上看不出来（生产上真静默失败过 4 天）。
#
# 它同时是审计链头哈希的**离机留痕出口**（M28）：备份包把锚点与库同包同盘恢复，
# 本机自检无法证明"没被回滚到更早的旧批次"；这里每天把当日链头哈希外发一封邮件，
# 那封邮件才是外部参照（恢复后与该包日期当天的哈希比对即可）。
# 锚点外发**每天必发**（失败时 stderr 留一行，不改退出码）；失败告警仍只在异常时发。
#
# 安装（08:05 排期在 02:00 备份之后，留足打包与异机同步的时间）：
#   sudo install -m 0700 -o root -g root scripts/yiban-backup-sentinel.sh \
#       /usr/local/sbin/yiban-backup-sentinel.sh
#   sudo crontab -e
#   5 8 * * * APP_DIR=/opt/yiban-auto-sign /usr/local/sbin/yiban-backup-sentinel.sh >> /var/log/yiban/backup.log 2>&1
#   # 退出码 0 = 检查完成（含"缺失但告警已发出"、含"锚点外发没成功"）；1 = 检查本身失败或告警发不出去。
#   ⚠ 上面这行把 stderr 并进了 backup.log，所以 cron 不会发报错邮件（全仓无 MAILTO
#   设置）：退出码 1 只在有人翻日志时才看得见，别把它当成告警。要它自己响，得去掉
#   `2>&1` 并配 `MAILTO`，或把退出码接进监控。日志与 backup.sh 同文件，排查时按时间挨着看。
#
# 环境变量（与 backup.sh / run.sh 同口径）：
#   BACKUP_DIR（默认 /var/backups）、APP_DIR（默认 /opt/yiban-auto-sign）、
#   YIBAN_BACKUP_INSTALLED（默认 /usr/local/sbin/yiban-backup.sh，防漂移比对对象）、
#   YIBAN_ENV_FILE（默认 <APP_DIR>/.env，由本脚本导出）、
#   YIBAN_DB_FILE（本脚本**不设**这枚键。库路径由 store 侧 `resolve_path` 三档解析：
#     进程环境 → .env → 默认 "yiban.db"。默认值按 cwd 解析，而本脚本已 cd 到
#     APP_DIR，所以缺省档就是 <APP_DIR>/yiban.db。本脚本插一手就会顶掉 .env 的声明）、
#   YIBAN_STATE_DIR（默认 .env 所在目录，即 <APP_DIR>：节流表落
#   <APP_DIR>/notify-throttle.json）——后两个给收件人算法与节流表定位；
#   未配置推送/邮件时告警只落日志（退出码 1），锚点外发同样落 stderr。
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
# 库路径**不在这里给值**（工单 ba-p02-02 订正）。库路径的唯一解析器在 Python 侧。
# `env_io.resolve_path` 的三档是：进程环境 → .env → 默认值。第一档压过第二档。
# 这里原先写的是 `export YIBAN_DB_FILE="${YIBAN_DB_FILE:-$APP_DIR/yiban.db}"`。
# 部署方没设进程环境时，它会造出一个值，并把该值放进第一档。
# 于是 `.env` 里声明的自定义库路径被顶掉——哨兵读到另一份（通常不存在的）库。
# 头注释当时写的理由是"免得锚点悄悄读到一个空库"。这与代码的实际行为相反，
# 属假担保，同批订正。
# 不设这行之后，缺省档仍落在 <APP_DIR>/yiban.db：本脚本已 cd 到 APP_DIR，
# 默认值 "yiban.db" 按 cwd 解析就是它（与上面 cd 段的口径同一条）。

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
