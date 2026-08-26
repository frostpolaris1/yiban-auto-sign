#!/usr/bin/env bash
# ============================================================
# Docker 部署加密备份（2026-08-27 审查补缺 P2-11）
#
# 背景：容器部署的数据备份此前只有 README 的「裸 tar data/」路线，
# 无 backup.sh（宿主 systemd 部署）的默认加密能力，备份明文落盘。
#
# 用法（在 docker-compose.yml 所在目录执行）：
#   YIBAN_BACKUP_PASSPHRASE='你的口令' bash docker/backup-docker.sh
# 可选环境变量：DATA_DIR（默认 ./data）、BACKUP_DIR（默认 ./backups）、
#   RETAIN_DAYS（默认 30，超期自动删除）
#
# 恢复：
#   gpg --batch --yes --decrypt --passphrase '口令' 备份.tgz.gpg | tar -C 目标目录 -xzf -
#
# 依赖：宿主机 tar 与 gpg（数据经管道流式处理，不在磁盘留任何明文副本）。
# 口令丢失 = 备份不可解密；请与 ./data 分开存放口令（同 README 密钥分离承诺）。
# ============================================================
set -euo pipefail

DATA_DIR="${DATA_DIR:-./data}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
RETAIN_DAYS="${RETAIN_DAYS:-30}"
PASSPHRASE="${YIBAN_BACKUP_PASSPHRASE:-${BACKUP_GPG_PASSPHRASE:-}}"

if [ -z "$PASSPHRASE" ]; then
    echo "错误：未设置 YIBAN_BACKUP_PASSPHRASE（或兼容名 BACKUP_GPG_PASSPHRASE）。" >&2
    echo "本脚本拒绝生成明文备份——若确需明文请自行手动 tar，后果自负。" >&2
    exit 2
fi
if [ ! -d "$DATA_DIR" ]; then
    echo "错误：数据目录不存在: $DATA_DIR" >&2
    exit 1
fi

command -v gpg >/dev/null || { echo "错误：宿主机未安装 gpg" >&2; exit 3; }

mkdir -p "$BACKUP_DIR"
STAMP="$(date +%F)"
OUT="$BACKUP_DIR/yiban-data-$STAMP.tar.gz.gpg"

tar -C "$(dirname "$DATA_DIR")" -czf - "$(basename "$DATA_DIR")" \
    | gpg --batch --yes --symmetric --cipher-algo AES256 \
          --passphrase "$PASSPHRASE" -o "$OUT"
chmod 600 "$OUT"
sha256sum "$OUT" | awk '{print $1}' > "$OUT.sha256"

# 超期清理（按文件名日期粗筛即可）
find "$BACKUP_DIR" -name 'yiban-data-*.tar.gz.gpg' -mtime "+$RETAIN_DAYS" -delete 2>/dev/null || true
find "$BACKUP_DIR" -name '*.sha256' -mtime "+$RETAIN_DAYS" -delete 2>/dev/null || true

echo "备份完成: $OUT"
echo "校验值 : $(cat "$OUT.sha256")"
