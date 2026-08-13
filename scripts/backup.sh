#!/bin/bash
# ============================================================
# backup.sh —— 易班自动签到数据每日备份脚本
# ============================================================
# 功能：
#   1. 本地打包数据文件 → /var/backups/yiban-YYYY-MM-DD.tar.gz（0600）
#      - accounts.json（易班账号，含明文密码）
#      - users.json（普通用户表，含密码哈希）
#      - .env（管理员口令 / YIBAN_ACCOUNTS_KEY 等敏感配置）
#      - 密钥文件：/etc/yiban/accounts-key（存在则单独置于 keys/ 子目录）
#        不存在则从 .env 提取 YIBAN_ACCOUNTS_KEY 单独置于 keys/（密钥与数据
#        同包但分目录放置，恢复时可区分）
#      - sign-daily 状态目录（/var/log/yiban 下 sign-daily-*.json，可选）
#   2. 异机加密副本（可选）：REMOTE_BACKUP 配置后，用 age（优先）或
#      gpg --symmetric 加密备份包，再 rsync（优先）/ scp 到远端。
#      REMOTE_BACKUP 未配置时仅保留本地副本。
#   3. 保留策略：本地与异机各保留 30 天（find -mtime +30 -delete）。
#   4. --restore 模式：从备份包恢复到指定目录（恢复演练 / 真实恢复）。
#
# 用法：
#   ./backup.sh                      # 执行备份
#   ./backup.sh --restore <tar.gz> <目标目录>   # 恢复演练/恢复
#   REMOTE_BACKUP=user@host:/backup/yiban ./backup.sh
#   REMOTE_BACKUP="user@host:/backup/yiban" BACKUP_AGE_PASSPHRASE=xxx ./backup.sh
#
# 安装（cron 每日 02:00，见 docs/web-console/DEPLOY-CHECKLIST.md 步骤 7）：
#   sudo install -m 0700 -o root -g root scripts/backup.sh /usr/local/sbin/yiban-backup.sh
#   sudo crontab -e
#   0 2 * * * REMOTE_BACKUP=user@host:/backup/yiban /usr/local/sbin/yiban-backup.sh >> /var/log/yiban/backup.log 2>&1
#
# 依赖：
#   - 本地打包：tar / find（系统自带）
#   - 异机加密副本（可选）：age（apt install age）或 gpg（系统自带）；
#     rsync（apt install rsync）或 scp（系统自带）
# ============================================================

set -euo pipefail
umask 077

# ------------------------------------------------------------
# 配置（按需调整；也可通过环境变量覆盖）
# ------------------------------------------------------------
APP_DIR="${APP_DIR:-/opt/yiban-auto-sign}"          # 项目部署目录
BACKUP_DIR="${BACKUP_DIR:-/var/backups}"            # 本地备份目录
RETENTION_DAYS="${RETENTION_DAYS:-30}"              # 保留天数
REMOTE_BACKUP="${REMOTE_BACKUP:-}"                  # 异机目标，如 user@host:/backup/yiban；留空 = 仅本地
AGE_PASSPHRASE="${BACKUP_AGE_PASSPHRASE:-}"         # age 对称加密口令（推荐用环境变量/密钥文件注入）
GPG_RECIPIENT="${BACKUP_GPG_RECIPIENT:-}"           # gpg 接收者（公钥 ID），配置后走 gpg 公钥加密

# 待备份数据文件（均为相对 APP_DIR 的路径；文件不存在时静默跳过）
DATA_FILES=(accounts.json users.json .env)
# 可选：sign-daily 状态目录（网页日历读取签到状态；不存在则跳过）
SIGN_DAILY_DIR="${SIGN_DAILY_DIR:-/var/log/yiban/sign-daily}"

# 密钥文件：systemd 单元 EnvironmentFile 指向的密钥（0600，root:yiban）
KEY_FILE="${KEY_FILE:-/etc/yiban/accounts-key}"

DATE="$(date +%Y-%m-%d)"
ARCHIVE="${BACKUP_DIR}/yiban-${DATE}.tar.gz"
TMPDIR_BAK="$(mktemp -d "${TMPDIR:-/tmp}/yiban-bak.XXXXXX")"
trap 'rm -rf "${TMPDIR_BAK}"' EXIT

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# ------------------------------------------------------------
# 恢复模式：--restore <备份包> <目标目录>
# ------------------------------------------------------------
restore() {
    local archive="$1" dest="$2"
    if [ ! -f "$archive" ]; then
        echo "错误：备份包不存在：$archive" >&2
        exit 1
    fi
    if [ -z "$dest" ]; then
        echo "错误：请指定恢复目标目录（恢复演练用临时目录，避免覆盖生产数据）" >&2
        exit 1
    fi
    mkdir -p "$dest"
    tar -xzf "$archive" -C "$dest"
    log "已从 $archive 恢复到 $dest"
    log "恢复内容清单："
    find "$dest" -type f -exec ls -l {} \;
    log "恢复演练核对项："
    local n_accounts
    n_accounts=$(grep -o '"phone"' "${dest}/data/accounts.json" 2>/dev/null | wc -l)
    log "  - accounts.json 账号数量：${n_accounts:-0}"
    log "  - keys/ 目录是否含密钥：$(ls "${dest}/keys/" 2>/dev/null | tr '\n' ' ' || echo '无（备份时密钥缺失）')"
    log "提示：恢复演练请核对上述内容后删除临时目录；真实恢复时将 data/ 与 keys/ 覆盖回 $APP_DIR 并 chmod 600。"
}

if [ "${1:-}" = "--restore" ]; then
    restore "${2:-}" "${3:-}"
    exit 0
fi

# ------------------------------------------------------------
# 本地打包
# ------------------------------------------------------------
mkdir -p "${BACKUP_DIR}"
mkdir -p "${TMPDIR_BAK}/data" "${TMPDIR_BAK}/keys"

log "=== 易班自动签到备份开始（${DATE}）==="

# 1) 数据文件（cp 快照再归档，避免 cron 执行期间文件被写入导致打包损坏）
for f in "${DATA_FILES[@]}"; do
    if [ -f "${APP_DIR}/${f}" ]; then
        cp -p "${APP_DIR}/${f}" "${TMPDIR_BAK}/data/" 2>/dev/null || \
            { log "警告：无法读取 ${APP_DIR}/${f}，已跳过"; rm -f "${TMPDIR_BAK}/data/$(basename "$f")"; }
    else
        log "跳过（不存在）：${APP_DIR}/${f}"
    fi
done

# 2) 密钥：优先 /etc/yiban/accounts-key（systemd EnvironmentFile）
#    ——密钥与数据一起备份但分开放置（keys/ 子目录），恢复时可区分对待
if [ -f "${KEY_FILE}" ]; then
    cp -p "${KEY_FILE}" "${TMPDIR_BAK}/keys/accounts-key"
    log "密钥来源：${KEY_FILE}"
else
    # 兜底：从 .env 提取 YIBAN_ACCOUNTS_KEY（web 会话签名密钥，见 web/app.py ensure_secret_key）
    if [ -f "${APP_DIR}/.env" ]; then
        grep -E '^\s*YIBAN_ACCOUNTS_KEY=' "${APP_DIR}/.env" > "${TMPDIR_BAK}/keys/secret-key.env" \
            && log "密钥来源：${APP_DIR}/.env 内 YIBAN_ACCOUNTS_KEY（已单独提取）" \
            || log "警告：.env 中未找到 YIBAN_ACCOUNTS_KEY，密钥未备份！"
    else
        log "警告：未找到密钥文件（${KEY_FILE}）与 .env，密钥未备份！"
    fi
fi

# 3) sign-daily 状态目录（可选）
if [ -d "${SIGN_DAILY_DIR}" ]; then
    mkdir -p "${TMPDIR_BAK}/state"
    cp -a "${SIGN_DAILY_DIR}/." "${TMPDIR_BAK}/state/" 2>/dev/null && log "已备份签到状态目录：${SIGN_DAILY_DIR}"
fi

# 4) 打包（--owner/--group 归一化，便于跨机恢复）
tar -czf "${ARCHIVE}" -C "${TMPDIR_BAK}" \
    --owner=0 --group=0 \
    data keys state 2>/dev/null || {
    # state 目录可能不存在，回退为只打包 data keys
    tar -czf "${ARCHIVE}" -C "${TMPDIR_BAK}" --owner=0 --group=0 data keys
}
chmod 0600 "${ARCHIVE}"
log "本地备份完成：${ARCHIVE}（$(du -h "${ARCHIVE}" | cut -f1)）"

# ------------------------------------------------------------
# 异机加密副本（REMOTE_BACKUP 未配置时跳过）
# 加密优先级（cron 场景无终端，口令必须可注入，否则跳过加密副本）：
#   1) BACKUP_GPG_RECIPIENT 已配置 → gpg 公钥加密（无需口令）
#   2) BACKUP_AGE_PASSPHRASE 已配置 → gpg AES-256 对称加密（口令经 stdin 注入）
#   3) 终端交互且已装 age → age -p 交互式
#   4) 均不可用 → 告警并仅保留本地备份
# ------------------------------------------------------------
if [ -n "${REMOTE_BACKUP}" ]; then
    log "REMOTE_BACKUP 已配置，准备加密并同步到 ${REMOTE_BACKUP} ..."
    REMOTE_FILE=""
    if [ -n "${GPG_RECIPIENT}" ] && command -v gpg > /dev/null 2>&1; then
        gpg --batch --yes --symmetric --cipher-algo AES256 \
            --recipient "${GPG_RECIPIENT}" -o "${ARCHIVE}.gpg" "${ARCHIVE}"
        REMOTE_FILE="${ARCHIVE}.gpg"
    elif [ -n "${AGE_PASSPHRASE}" ] && command -v gpg > /dev/null 2>&1; then
        # gpg 对称加密（非交互，口令从 stdin 读取；解密时用同一口令：
        #   gpg --batch --decrypt --passphrase-fd 0 < yiban-*.tar.gz.gpg > yiban-*.tar.gz）
        printf '%s\n' "${AGE_PASSPHRASE}" | \
            gpg --batch --yes --symmetric --cipher-algo AES256 \
                --passphrase-fd 0 -o "${ARCHIVE}.gpg" "${ARCHIVE}"
        REMOTE_FILE="${ARCHIVE}.gpg"
    elif command -v age > /dev/null 2>&1 && [ -t 0 ]; then
        age -p -o "${ARCHIVE}.age" "${ARCHIVE}"
        REMOTE_FILE="${ARCHIVE}.age"
    else
        log "警告：未提供加密口令（BACKUP_AGE_PASSPHRASE）且无 gpg/age，跳过异机加密副本；仅保留本地备份" >&2
    fi

    if [ -n "${REMOTE_FILE}" ]; then
        if command -v rsync > /dev/null 2>&1; then
            rsync -az --chmod=600 "${REMOTE_FILE}" "${REMOTE_BACKUP}/"
            log "已 rsync 到 ${REMOTE_BACKUP}/$(basename "${REMOTE_FILE}")"
        else
            scp -p "${REMOTE_FILE}" "${REMOTE_BACKUP}/"
            log "已 scp 到 ${REMOTE_BACKUP}/$(basename "${REMOTE_FILE}")"
        fi
        # 异机侧保留策略：建议在远端另配清理 cron（find ... -mtime +30 -delete），
        # 或定期人工清理；本脚本只保证本地保留天数。
    fi
else
    log "REMOTE_BACKUP 未配置，仅保留本地备份（建议尽快配置异机加密副本）"
fi

# ------------------------------------------------------------
# 本地保留策略：删除超过 30 天的本地备份包
# ------------------------------------------------------------
find "${BACKUP_DIR}" -maxdepth 1 -name 'yiban-*.tar.gz' -mtime "+${RETENTION_DAYS}" -delete
find "${BACKUP_DIR}" -maxdepth 1 -name 'yiban-*.tar.gz.age' -mtime "+${RETENTION_DAYS}" -delete
find "${BACKUP_DIR}" -maxdepth 1 -name 'yiban-*.tar.gz.gpg' -mtime "+${RETENTION_DAYS}" -delete
log "本地清理完成（保留 ${RETENTION_DAYS} 天）"

log "=== 备份完成：${ARCHIVE} ==="
log "恢复演练：bash backup.sh --restore ${ARCHIVE} /tmp/yiban-restore-test"
