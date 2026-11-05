#!/bin/sh
# ============================================================
# deploy/container/backup.sh — 容器形态的一次性备份任务。
#
# 由 compose 的 backup profile 调用（postgres:18 镜像里的 /bin/sh）：
#   docker compose --env-file deploy/container/compose.env \
#     -f deploy/container/docker-compose.yml --profile backup run --rm backup
#
# 产物落 /backups/<UTC 时间戳>/（宿主目录 = ${YIBAN_BACKUP_HOST_DIR:-./backups}）：
#   db.dump        pg_dump -Fc（含凭据密文与审计行）
#   db.list        pg_restore --list（证明转储可读）
#   photos.tar.gz  照片卷快照（照片不进数据库备份）
#   SHA256SUMS     以上文件的 sha256（写入后立即 -c 校验）
#
# 口径：
#   - 数据库口令只经 PGPASSWORD 环境交给 libpq，不进 argv，也不打印。
#   - 目录 0700、文件 0600：备份含凭据密文，等同于裸机备份的敏感级。
#   - 任何一步失败即退出非 0；半成品目录保留给排障（不会伪装成完整备份）。
#   - 本脚本不加密；需要静态加密时在宿主对目录内文件跑 gpg（README 的容器备份一节）。
#
# 退出码：0 成功；1 备份步骤失败（pg_dump/tar/校验）；2 用法或环境缺失。
# ============================================================
set -eu

: "${PGHOST:?PGHOST is required}"
: "${PGUSER:?PGUSER is required}"
: "${PGDATABASE:?PGDATABASE is required}"
: "${PGPASSWORD:?PGPASSWORD is required}"

command -v pg_dump >/dev/null 2>&1 || { echo "backup: pg_dump not found" >&2; exit 2; }
command -v pg_restore >/dev/null 2>&1 || { echo "backup: pg_restore not found" >&2; exit 2; }
command -v tar >/dev/null 2>&1 || { echo "backup: tar not found" >&2; exit 2; }

[ -d /backups ] || { echo "backup: /backups is not a directory (mount YIBAN_BACKUP_HOST_DIR)" >&2; exit 2; }
[ -d /photos ] || { echo "backup: /photos is not a directory (photos volume missing)" >&2; exit 2; }

TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="/backups/$TS"

umask 077
mkdir -p "$OUT"

echo "backup: dumping $PGDATABASE from $PGHOST into $OUT"
pg_dump -Fc --no-owner --no-privileges -f "$OUT/db.dump"
# 转储可读性是备份成立的判据：读不了等同于没有备份。
pg_restore --list "$OUT/db.dump" > "$OUT/db.list"

echo "backup: archiving photos"
tar -C /photos -czf "$OUT/photos.tar.gz" .

(
    cd "$OUT"
    sha256sum db.dump db.list photos.tar.gz > SHA256SUMS
    sha256sum -c SHA256SUMS >/dev/null
)

echo "backup: wrote $OUT"
echo "backup: restore database (run from the repo root; swap the host path if"
echo "backup: YIBAN_BACKUP_HOST_DIR is set; stop app first):"
echo "  docker compose --env-file deploy/container/compose.env \\"
echo "    -f deploy/container/docker-compose.yml exec -T db \\"
echo "    pg_restore -U yiban -d yiban --clean --if-exists --no-owner --no-privileges \\"
echo "    < deploy/container/backups/$TS/db.dump"
echo "backup: restore photos with:"
echo "  docker compose --env-file deploy/container/compose.env \\"
echo "    -f deploy/container/docker-compose.yml --profile backup run --rm \\"
echo "    --entrypoint sh backup -c 'tar -xzf $OUT/photos.tar.gz -C /photos'"
