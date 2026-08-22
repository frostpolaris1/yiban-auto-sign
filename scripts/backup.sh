#!/bin/bash
# ============================================================
# backup.sh —— 易班自动签到数据每日备份脚本
# ============================================================
# 功能：
#   1. 本地打包数据文件 → /var/backups/yiban-YYYY-MM-DD.tar.gz（0600）
#      - yiban.db（SQLite：账号+用户表；用 sqlite3 .backup 一致性快照，WAL 安全）
#      - .env（管理员口令 / YIBAN_ACCOUNTS_KEY 等敏感配置）
#      - 密钥文件：/etc/yiban/accounts-key（存在则单独置于 keys/ 子目录）
#        不存在则从 .env 提取 YIBAN_ACCOUNTS_KEY 单独置于 keys/（密钥与数据
#        同包但分目录放置，恢复时可区分）
#      - 签到状态文件（/var/log/yiban 下 sign-daily-*.json / sign-state-*.json / cred-state.json，可选）
#   2. 异机加密副本（可选）：REMOTE_BACKUP 配置后，用 age（优先）或
#      gpg --symmetric 加密备份包，再 rsync（优先）/ scp 到远端。
#      REMOTE_BACKUP 未配置时仅保留本地副本。
#   3. 本地默认加密（M24，2026-08-22）：存在可用加密方式时本地归档即刻转为
#      密文并删除明文 tar.gz——明文落盘需显式 BACKUP_PLAINTEXT=1（大字告警）。
#      无可用加密方式（未配置口令/公钥且无交互终端）保持明文 + 大字告警，
#      不改变既有部署行为。--restore 对 .tar.gz / .gpg / .age 均可直接恢复。
#   4. 保留策略：本地与异机各保留 30 天（find -mtime +30 -delete）。
#   5. --restore 模式：从备份包恢复到指定目录（恢复演练 / 真实恢复）。
#
# 用法：
#   ./backup.sh                      # 执行备份（有加密条件时本地默认密文）
#   ./backup.sh --restore <备份包> <目标目录>   # 恢复演练/恢复（支持 .gpg/.age）
#   REMOTE_BACKUP=user@host:/backup/yiban ./backup.sh
#   REMOTE_BACKUP="user@host:/backup/yiban" BACKUP_GPG_PASSPHRASE=xxx ./backup.sh
#   BACKUP_PLAINTEXT=1 ./backup.sh   # 显式关闭默认加密（明文本地归档，大字告警）
#
# 安装（cron 每日 02:00，见 docs/web-console/DEPLOY-CHECKLIST.md 步骤 7）：
#   sudo install -m 0700 -o root -g root scripts/backup.sh /usr/local/sbin/yiban-backup.sh
#   sudo crontab -e
#   0 2 * * * REMOTE_BACKUP=user@host:/backup/yiban /usr/local/sbin/yiban-backup.sh >> /var/log/yiban/backup.log 2>&1
#
# 依赖：
#   - 本地打包：tar / find / sqlite3（系统自带）
#   - 默认加密/异机副本：age（apt install age）或 gpg（系统自带）；
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
# gpg 对称加密口令（推荐用环境变量/密钥文件注入；旧名 BACKUP_AGE_PASSPHRASE 兼容回退——
# 2026-08-16 审查轮：原 AGE_PASSPHRASE 命名与 age 工具混淆，实际用途是 gpg AES-256 对称加密）
GPG_PASSPHRASE="${BACKUP_GPG_PASSPHRASE:-${BACKUP_AGE_PASSPHRASE:-}}"
GPG_RECIPIENT="${BACKUP_GPG_RECIPIENT:-}"           # gpg 接收者（公钥 ID），配置后走 gpg 公钥加密
# M24：本地归档默认加密的总开关——1 = 显式关闭（明文本地归档，大字告警）
BACKUP_PLAINTEXT="${BACKUP_PLAINTEXT:-0}"

# 待备份数据文件（均为相对 APP_DIR 的路径；文件不存在时静默跳过）
DATA_FILES=(.env)
# SQLite 数据库（账号+用户表；用 sqlite3 .backup 一致性快照，WAL 安全）
DB_FILE="${DB_FILE:-yiban.db}"
# 可选：签到状态文件目录（/var/log/yiban 根下，含 sign-daily-*.json 旧格式、
#      sign-state-*.json 结构化状态 与 cred-state.json 熔断状态；目录不存在则跳过）
SIGN_STATE_DIR="${SIGN_STATE_DIR:-/var/log/yiban}"
# 可选：按天签到日志目录（sign-YYYY-MM-DD.log；过期清理由 yiban-cleanup.sh 负责，此处仅备份现存量）
SIGN_LOG_DIR="${SIGN_LOG_DIR:-/var/log/yiban}"

# 密钥文件：systemd 单元 EnvironmentFile 指向的密钥（0600，root:yiban）
KEY_FILE="${KEY_FILE:-/etc/yiban/accounts-key}"

DATE="$(date +%Y-%m-%d)"
ARCHIVE="${BACKUP_DIR}/yiban-${DATE}.tar.gz"
TMPDIR_BAK="$(mktemp -d "${TMPDIR:-/tmp}/yiban-bak.XXXXXX")"
trap 'rm -rf "${TMPDIR_BAK}"' EXIT

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# 加密/同步失败时的处置。M24 后语义调整：密文由 try_encrypt 产出并自清理
# 半成品，能走到这里的失败只剩 rsync/scp 同步失败——本地已完成的归档
# （明文或密文）必须保留，网络抖动不得摧毁唯一本地副本；
# 仅 --require-encrypt 模式维持原严格策略（失败即清场）。
remote_fail() {
    local msg="$1"
    if [ "${REQUIRE_ENCRYPT:-0}" -eq 1 ]; then
        echo "错误：$msg；--require-encrypt 模式：已删除本轮归档与未完成加密文件" >&2
        rm -f "$ARCHIVE" "$ARCHIVE.gpg" "$ARCHIVE.age"
        exit 1
    fi
    echo "错误：$msg；本地归档（${FINAL_LOCAL:-$ARCHIVE}）已保留，请排查后重试同步" >&2
    exit 1
}

# M24：尝试按优先级对 $ARCHIVE 加密，成功则把密文路径写入全局 ENC_FILE 并返回 0；
# 无可用加密方式或加密失败返回 1（半成品密文自行清理），由调用方决定明文策略。
# 优先级与异机副本一致：
#   1) GPG_RECIPIENT + gpg 公钥（cron 友好，无需口令）
#   2) GPG_PASSPHRASE + gpg AES-256 对称（口令经 stdin，cron 友好）
#   3) 交互终端 + age -p（仅手动执行时可用）
try_encrypt() {
    ENC_FILE=""
    if [ -n "${GPG_RECIPIENT}" ] && command -v gpg > /dev/null 2>&1; then
        if ! gpg --batch --yes --encrypt --recipient "${GPG_RECIPIENT}" \
                -o "${ARCHIVE}.gpg" "${ARCHIVE}"; then
            rm -f "${ARCHIVE}.gpg"
            return 1
        fi
        ENC_FILE="${ARCHIVE}.gpg"
        return 0
    fi
    if [ -n "${GPG_PASSPHRASE}" ] && command -v gpg > /dev/null 2>&1; then
        if ! printf '%s\n' "${GPG_PASSPHRASE}" | \
            gpg --batch --yes --symmetric --cipher-algo AES256 \
                --passphrase-fd 0 -o "${ARCHIVE}.gpg" "${ARCHIVE}"; then
            rm -f "${ARCHIVE}.gpg"
            return 1
        fi
        ENC_FILE="${ARCHIVE}.gpg"
        return 0
    fi
    if command -v age > /dev/null 2>&1 && [ -t 0 ]; then
        if ! age -p -o "${ARCHIVE}.age" "${ARCHIVE}"; then
            rm -f "${ARCHIVE}.age"
            return 1
        fi
        ENC_FILE="${ARCHIVE}.age"
        return 0
    fi
    return 1
}

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
    # M24：密文归档（.gpg/.age）先解密到 TMPDIR_BAK（顶层 trap 统一清理），
    # 再走与明文完全相同的安全校验 + 解包——恢复流程对默认加密透明。
    local plain="$archive"
    case "$archive" in
        *.gpg)
            command -v gpg > /dev/null 2>&1 || { echo "错误：解密 .gpg 归档需要 gpg 命令" >&2; exit 1; }
            plain="${TMPDIR_BAK}/restore-decrypted.tar.gz"
            if [ -n "${GPG_PASSPHRASE}" ]; then
                printf '%s\n' "${GPG_PASSPHRASE}" | gpg --batch --decrypt --passphrase-fd 0 \
                    -o "$plain" "$archive" 2>/dev/null || { echo "错误：.gpg 解密失败（口令错误或包损坏；可经 BACKUP_GPG_PASSPHRASE 注入口令重试）" >&2; exit 1; }
            else
                gpg --batch --decrypt -o "$plain" "$archive" 2>/dev/null || \
                    { echo "错误：.gpg 解密失败（对称加密需 BACKUP_GPG_PASSPHRASE 环境变量注入口令）" >&2; exit 1; }
            fi
            ;;
        *.age)
            command -v age > /dev/null 2>&1 || { echo "错误：解密 .age 归档需要 age 命令" >&2; exit 1; }
            plain="${TMPDIR_BAK}/restore-decrypted.tar.gz"
            age -d -o "$plain" "$archive" 2>/dev/null || { echo "错误：.age 解密失败（需交互输入口令或身份文件）" >&2; exit 1; }
            ;;
    esac
    # 安全校验：拒绝含路径穿越（../ 或绝对路径）或符号链接的条目，防止恶意备份包写出目标目录
    if tar -tzf "$plain" 2>/dev/null | grep -E '(^|/)\.\.(/|$)|^/' | grep -q .; then
        echo "错误：备份包含不安全条目（路径穿越/绝对路径），已拒绝解包" >&2
        exit 1
    fi
    if tar -tvzf "$plain" 2>/dev/null | grep -qE '^l'; then
        echo "错误：备份包含符号链接条目，已拒绝解包（防链接写出目标目录）" >&2
        exit 1
    fi
    # 2026-08-21 对抗性审查加固：同时拒绝设备/字符设备/FIFO 条目——root 恢复时
    # 恶意包可在目标目录创建设备节点（前提苛刻，属纵深防御）
    if tar -tvzf "$plain" 2>/dev/null | grep -qE '^[bcp]'; then
        echo "错误：备份包含设备/FIFO 特殊条目，已拒绝解包" >&2
        exit 1
    fi
    mkdir -p "$dest"
    tar -xzf "$plain" -C "$dest" --anchored --no-overwrite-dir 2>/dev/null || \
        tar -xzf "$plain" -C "$dest" --no-overwrite-dir 2>/dev/null || {
        echo "错误：解包失败（备份包可能损坏）" >&2
        exit 1
    }
    log "已从 $archive 恢复到 $dest"
    log "恢复内容清单："
    find "$dest" -type f -exec ls -l {} \;
    log "恢复演练核对项："
    local n_accounts
    n_accounts=$(sqlite3 "${dest}/data/yiban.db" "SELECT COUNT(*) FROM accounts" 2>/dev/null || echo "?")
    log "  - yiban.db 账号数量：${n_accounts:-0}"
    log "  - keys/ 目录是否含密钥：$(ls "${dest}/keys/" 2>/dev/null | tr '\n' ' ' || echo '无（备份时密钥缺失）')"
    log "提示：恢复演练请核对上述内容后删除临时目录；真实恢复时将 data/ 与 keys/ 覆盖回 $APP_DIR 并 chmod 600（yiban.db 需先停止 yiban-web 服务）。"
}

if [ "${1:-}" = "--restore" ]; then
    restore "${2:-}" "${3:-}"
    exit 0
fi

# --require-encrypt：强制加密，未配置加密时拒绝执行（防未加密备份泄露全部凭证）
REQUIRE_ENCRYPT=0
if [ "${1:-}" = "--require-encrypt" ]; then
    REQUIRE_ENCRYPT=1
fi

# --require-encrypt 前置校验：必须在打包前确认“异机目标已配置”且“加密工具可用”，
# 不满足则直接退出，不得生成任何明文归档。
if [ "$REQUIRE_ENCRYPT" -eq 1 ]; then
    if [ -z "$REMOTE_BACKUP" ]; then
        echo "错误：--require-encrypt 指定但 REMOTE_BACKUP 未配置，无法加密，拒绝创建本地明文归档" >&2
        exit 1
    fi
    if { [ -n "$GPG_RECIPIENT" ] || [ -n "$GPG_PASSPHRASE" ]; } && ! command -v gpg >/dev/null 2>&1; then
        echo "错误：--require-encrypt 指定且配置了 gpg，但未找到 gpg 命令，拒绝创建本地明文归档" >&2
        exit 1
    fi
    if [ -z "$GPG_RECIPIENT" ] && [ -z "$GPG_PASSPHRASE" ] && { ! command -v age >/dev/null 2>&1 || [ ! -t 0 ]; }; then
        echo "错误：--require-encrypt 指定但无可用加密方式（需 REMOTE_BACKUP + gpg/age），拒绝创建本地明文归档" >&2
        exit 1
    fi
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

# 1b) SQLite 数据库：sqlite3 .backup 一致性快照（WAL 模式下 cp 会漏未合并日志，
#     .backup 由 SQLite 内部保证快照一致；--restore 时直接替换回 yiban.db 即可）
if [ -f "${APP_DIR}/${DB_FILE}" ]; then
    if command -v sqlite3 > /dev/null 2>&1; then
        if sqlite3 "${APP_DIR}/${DB_FILE}" ".backup ${TMPDIR_BAK}/data/${DB_FILE}" 2>/dev/null; then
            log "数据库已备份（一致性快照）：${DB_FILE}"
        else
            log "警告：sqlite3 .backup 失败（${DB_FILE} 可能被占用），回退为文件复制"
            cp -p "${APP_DIR}/${DB_FILE}" "${TMPDIR_BAK}/data/" 2>/dev/null || \
                { log "警告：无法复制 ${DB_FILE}，已跳过"; rm -f "${TMPDIR_BAK}/data/${DB_FILE}"; }
        fi
    else
        log "警告：未安装 sqlite3，回退为文件复制（WAL 未合并时快照可能不完整）"
        cp -p "${APP_DIR}/${DB_FILE}" "${TMPDIR_BAK}/data/" 2>/dev/null || \
            { log "警告：无法复制 ${DB_FILE}，已跳过"; rm -f "${TMPDIR_BAK}/data/${DB_FILE}"; }
    fi
else
    log "跳过（不存在）：${APP_DIR}/${DB_FILE}"
fi

# 2) 密钥：优先 /etc/yiban/accounts-key（systemd EnvironmentFile，应含数据加密密钥
#    YIBAN_ACCOUNTS_KEY）——密钥与数据一起备份但分开放置（keys/ 子目录）
if [ -f "${KEY_FILE}" ]; then
    cp -p "${KEY_FILE}" "${TMPDIR_BAK}/keys/accounts-key"
    log "密钥来源：${KEY_FILE}"
    # 2026-08-21 修复（密钥语义冲突）：历史部署可能按旧清单把会话签名密钥
    # YIBAN_SECRET_KEY 写进了该文件——校验内容，缺数据密钥时从 .env 兜底提取
    if ! grep -qE '^[[:space:]]*YIBAN_ACCOUNTS_KEY=' "${KEY_FILE}"; then
        log "警告：${KEY_FILE} 内未找到 YIBAN_ACCOUNTS_KEY（疑似旧清单写入的会话密钥），尝试从 .env 兜底提取"
        if [ -f "${APP_DIR}/.env" ]; then
            grep -E '^[[:space:]]*YIBAN_ACCOUNTS_KEY=' "${APP_DIR}/.env" > "${TMPDIR_BAK}/keys/accounts-key.env" \
                && log "已从 ${APP_DIR}/.env 兜底提取 YIBAN_ACCOUNTS_KEY（keys/accounts-key.env）" \
                || log "警告：.env 中也未找到 YIBAN_ACCOUNTS_KEY，数据密钥未备份！恢复时 yiban.db 密文将不可解！"
        fi
    fi
else
    # 兜底：从 .env 提取 YIBAN_ACCOUNTS_KEY（账号密码 AES-GCM 数据加密密钥，
    # 见 scripts/account_crypto.py；会话签名密钥是另一把 YIBAN_SECRET_KEY）
    if [ -f "${APP_DIR}/.env" ]; then
        grep -E '^[[:space:]]*YIBAN_ACCOUNTS_KEY=' "${APP_DIR}/.env" > "${TMPDIR_BAK}/keys/secret-key.env" \
            && log "密钥来源：${APP_DIR}/.env 内 YIBAN_ACCOUNTS_KEY（已单独提取）" \
            || log "警告：.env 中未找到 YIBAN_ACCOUNTS_KEY，密钥未备份！"
    else
        log "警告：未找到密钥文件（${KEY_FILE}）与 .env，密钥未备份！"
    fi
fi

# 3) 签到状态文件（可选）：/var/log/yiban 根下 sign-daily/sign-state/cred-state
#    ——glob 匹配多个状态文件模式；无匹配时 cp 会失败，静默跳过（状态可重建，非关键）
if [ -d "${SIGN_STATE_DIR}" ]; then
    shopt -s nullglob
    state_files=("${SIGN_STATE_DIR}"/sign-daily-*.json "${SIGN_STATE_DIR}"/sign-state-*.json "${SIGN_STATE_DIR}"/cred-state.json)
    shopt -u nullglob
    if [ ${#state_files[@]} -gt 0 ]; then
        mkdir -p "${TMPDIR_BAK}/state"
        cp -p "${state_files[@]}" "${TMPDIR_BAK}/state/" 2>/dev/null \
            && log "已备份签到状态文件（${#state_files[@]} 个：sign-daily/sign-state/cred-state）"
    fi
fi

# 4) 按天签到日志（可选）：/var/log/yiban 下 sign-YYYY-MM-DD.log
#    ——glob 匹配；无匹配时静默跳过（日志可重建，非关键；过期清理由 yiban-cleanup.sh 负责）
if [ -d "${SIGN_LOG_DIR}" ]; then
    shopt -s nullglob
    log_files=("${SIGN_LOG_DIR}"/sign-*.log)
    shopt -u nullglob
    if [ ${#log_files[@]} -gt 0 ]; then
        mkdir -p "${TMPDIR_BAK}/logs"
        cp -p "${log_files[@]}" "${TMPDIR_BAK}/logs/" 2>/dev/null \
            && log "已备份按天签到日志（${#log_files[@]} 个：sign-*.log）"
    fi
fi

# 5) 打包（--owner/--group 归一化，便于跨机恢复）
#    按实际存在的目录动态拼参数：缺 state/logs 时只跳过缺失目录，
#    绝不因某个目录不存在而丢弃其它已备份目录。
archive_paths=()
for dir_name in data keys state logs; do
    if [ -d "${TMPDIR_BAK}/${dir_name}" ]; then
        archive_paths+=("${dir_name}")
    fi
done
if [ ${#archive_paths[@]} -eq 0 ]; then
    echo "错误：没有可打包的目录" >&2
    exit 1
fi
tar -czf "${ARCHIVE}" -C "${TMPDIR_BAK}" --owner=0 --group=0 "${archive_paths[@]}"
chmod 0600 "${ARCHIVE}"

# ------------------------------------------------------------
# M24 本地默认加密：有可用加密方式时，本地归档即刻转为密文并删除明文 tar.gz；
# 明文落盘需显式 BACKUP_PLAINTEXT=1（大字告警）。无可用方式保持明文 + 大字告警，
# 不改变既有部署行为；--restore 对密文归档透明支持。
# ------------------------------------------------------------
ENC_FILE=""
if [ "${BACKUP_PLAINTEXT}" = "1" ]; then
    log "════════════════════════════════════════════════════════════"
    log "⚠⚠⚠ 已显式设置 BACKUP_PLAINTEXT=1：本轮生成【明文】本地归档 ⚠⚠⚠"
    log "⚠⚠⚠ 归档内含 .env 全部密钥、管理员口令哈希与全量数据库！   ⚠⚠⚠"
    log "════════════════════════════════════════════════════════════"
elif try_encrypt; then
    rm -f "${ARCHIVE}"
    log "已启用本地默认加密：明文归档已移除，本轮密文为 ${ENC_FILE}"
else
    log "════════════════════════════════════════════════════════════" >&2
    log "⚠⚠⚠ 无法加密本地归档（未配置 BACKUP_GPG_RECIPIENT/BACKUP_GPG_PASSPHRASE， ⚠⚠⚠" >&2
    log "⚠⚠⚠ 且非交互终端无法使用 age）：本轮为【明文】归档，请尽快配置加密！     ⚠⚠⚠" >&2
    log "════════════════════════════════════════════════════════════" >&2
fi

# 最终落盘产物 = 密文（默认）或明文（显式关闭/无法加密）；sha256 清单始终对应
# 实际落盘文件。注意：fetch-backup.ps1 目前按 yiban-*.tar.gz 明文名拉取，启用
# 默认加密后需按 docs/web-console/DEPLOY-CHECKLIST.md §7 调整二次副本流程。
FINAL_LOCAL="${ENC_FILE:-${ARCHIVE}}"
if command -v sha256sum > /dev/null 2>&1; then
    sha256sum "${FINAL_LOCAL}" > "${FINAL_LOCAL}.sha256"
    chmod 0600 "${FINAL_LOCAL}.sha256"
    # 加密切换当天避免新旧两份清单并存混淆
    [ -n "${ENC_FILE}" ] && rm -f "${ARCHIVE}.sha256"
    log "已生成校验清单：${FINAL_LOCAL}.sha256"
fi
log "本地备份完成：${FINAL_LOCAL}（$(du -h "${FINAL_LOCAL}" | cut -f1)）"

# ------------------------------------------------------------
# 异机加密副本（REMOTE_BACKUP 未配置时跳过）
# M24：本地默认加密已产出密文（ENC_FILE）时直接复用同一份密文，不再重复加密；
# 本地为明文（显式关闭/无法加密）时仍尝试加密后再出站——异机副本绝不传明文。
# ------------------------------------------------------------
if [ -n "${REMOTE_BACKUP}" ]; then
    log "REMOTE_BACKUP 已配置，准备同步到 ${REMOTE_BACKUP} ..."
    REMOTE_FILE="${ENC_FILE}"
    if [ -z "${REMOTE_FILE}" ] && [ "${BACKUP_PLAINTEXT}" != "1" ]; then
        if try_encrypt; then
            REMOTE_FILE="${ENC_FILE}"
        fi
    fi
    if [ -n "${REMOTE_FILE}" ]; then
        if command -v rsync > /dev/null 2>&1; then
            if ! rsync -az --chmod=600 "${REMOTE_FILE}" "${REMOTE_BACKUP}/"; then
                remote_fail "rsync 同步失败"
            fi
            log "已 rsync 到 ${REMOTE_BACKUP}/$(basename "${REMOTE_FILE}")"
        else
            if ! scp -p "${REMOTE_FILE}" "${REMOTE_BACKUP}/"; then
                remote_fail "scp 同步失败"
            fi
            log "已 scp 到 ${REMOTE_BACKUP}/$(basename "${REMOTE_FILE}")"
        fi
        # 异机侧保留策略：建议在远端另配清理 cron（find ... -mtime +30 -delete），
        # 或定期人工清理；本脚本只保证本地保留天数。
    else
        log "警告：未提供可用加密方式（gpg/age），拒绝把【明文】备份传出本机；仅保留本地备份" >&2
        log "警告：（备份包含密钥+数据；请配置 BACKUP_GPG_RECIPIENT 或 BACKUP_GPG_PASSPHRASE 后重试）" >&2
    fi
else
    # M24 后此分支仅在 BACKUP_PLAINTEXT=1 或无可用加密方式时到达（均已有大字告警）
    log "REMOTE_BACKUP 未配置，仅保留本地${ENC_FILE:+密文}备份"
fi

# ------------------------------------------------------------
# 本地保留策略：删除超过 30 天的本地备份包
# ------------------------------------------------------------
find "${BACKUP_DIR}" -maxdepth 1 -name 'yiban-*.tar.gz' -mtime "+${RETENTION_DAYS}" -delete
find "${BACKUP_DIR}" -maxdepth 1 -name 'yiban-*.tar.gz.age' -mtime "+${RETENTION_DAYS}" -delete
find "${BACKUP_DIR}" -maxdepth 1 -name 'yiban-*.tar.gz.gpg' -mtime "+${RETENTION_DAYS}" -delete
log "本地清理完成（保留 ${RETENTION_DAYS} 天）"

log "=== 备份完成：${FINAL_LOCAL} ==="
log "恢复演练：bash backup.sh --restore ${FINAL_LOCAL} /tmp/yiban-restore-test"
