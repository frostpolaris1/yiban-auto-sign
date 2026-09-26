#!/bin/bash
# ============================================================
# yiban-audit-witness.sh —— 审计锚点独立见证的 root 侧入口
#
# 仓库来源：deploy/prod/yiban-audit-witness.sh
# 安装位：  /usr/local/sbin/yiban-audit-witness.sh（0700 root，install.sh 落位）
# 排期：    /etc/cron.d/yiban-audit-witness 每 10 分钟一次（root）
#
# 为什么必须 root：审计锚点文件与其库内指纹都由应用身份（yiban）读写，拿到该身份
# 写权限的人可同时改写两者把"最近若干条审计被删"伪装成自洽。独立见证文件写在
# /var/lib/yiban-audit（install.sh 创建，root 属主），只有 root 能改写——这样
# "双写掩盖"才会在逐日校验里留下不一致。以 yiban 身份跑本脚本等于没跑。
#
# 实现在 scripts/audit_anchor_witness.py（薄包装，与 backup sentinel 同构）。
# 解释器优先项目虚拟环境；cwd 必须是 APP_DIR，否则 .env 的相对回落读不到，
# 锚点/状态目录会按默认值解析到别处。
# ============================================================
set -uo pipefail
umask 077

APP_DIR="${YIBAN_APP_DIR:-/opt/yiban-auto-sign}"
if [ ! -f "$APP_DIR/scripts/audit_anchor_witness.py" ]; then
    APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
fi
export APP_DIR
cd "$APP_DIR" || { echo "致命: 无法进入应用目录 $APP_DIR" >&2; exit 1; }
export YIBAN_ENV_FILE="${YIBAN_ENV_FILE:-$APP_DIR/.env}"

if [ -x "$APP_DIR/.venv/bin/python3" ]; then
    PY="$APP_DIR/.venv/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
    PY=python3
elif [ -x /usr/bin/python3 ]; then
    PY=/usr/bin/python3
else
    echo "找不到 python3，无法执行审计锚点见证（实现在 scripts/audit_anchor_witness.py）" >&2
    exit 1
fi

exec "$PY" "$APP_DIR/scripts/audit_anchor_witness.py" "$@"
