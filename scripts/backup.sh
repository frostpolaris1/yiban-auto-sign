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
#      - 调度闸门与通知账本状态文件（sched-run-* / sched-slot-* / sched-snapshot-* /
#        notify-ledger.json / notify-throttle.json）——缺了它们恢复当日会重签或
#        漏签（"今天是否已全量跑过"的标记丢失）、邮件/推送日额度被重置
#      - 审计链外部锚点 audit-anchor.log——缺了它恢复出来的库无法再自检
#        "删尾 / 删前缀 / 整表清空"（锚点是审计链唯一的外部参照）
#   2. 异机加密副本（可选）：REMOTE_BACKUP 配置后，用 age（优先）或
#      gpg --symmetric 加密备份包，再 rsync（优先）/ scp 到远端。
#      REMOTE_BACKUP 未配置时仅保留本地副本。
#   3. 本地默认加密（M24，2026-08-22）：存在可用加密方式时本地归档即刻转为
#      密文并删除明文 tar.gz——明文落盘需显式 BACKUP_PLAINTEXT=1（大字告警）。
#      无可用加密方式（未配置口令/公钥且无交互终端）保持明文 + 大字告警，
#      不改变既有部署行为。--restore 对 .tar.gz / .gpg / .age 均可直接恢复。
#   4. 保留策略：本地与异机各保留 30 天（find -mtime +30 -delete，含 .sha256 侧车）。
#   5. --restore 模式：从备份包恢复到指定目录（恢复演练 / 真实恢复），并在解包后
#      跑 PRAGMA integrity_check + scripts/audit_verify.py 验证恢复件可用。
#
# 用法：
#   ./backup.sh                      # 执行备份（有加密条件时本地默认密文）
#   ./backup.sh --require-encrypt    # 本轮必须加密成功，否则不生成任何归档
#   ./backup.sh --restore <备份包> <目标目录>   # 恢复演练/恢复（支持 .gpg/.age）
#   REMOTE_BACKUP=user@host:/backup/yiban ./backup.sh
#   REMOTE_BACKUP="user@host:/backup/yiban" BACKUP_GPG_PASSPHRASE=xxx ./backup.sh
#   BACKUP_PLAINTEXT=1 ./backup.sh   # 显式关闭默认加密（明文本地归档，大字告警）
#
# 安装（cron 每日 02:00；部署清单见 README「运维 → 备份与恢复」一节）：
#   M3 批次0 起生产执行件收编在 deploy/prod/（校验和对账 + 一键安装）：
#   sudo DESTDIR= bash deploy/prod/install.sh    # 本脚本→/usr/local/sbin/yiban-backup.sh
#   （或手工：sudo install -m 0700 -o root -g root scripts/backup.sh /usr/local/sbin/yiban-backup.sh）
#   sudo crontab -e
#   # 备份——务必带 --require-encrypt：不带时一旦加密配置失效，cron 会静默产出
#   # 含全部密钥与管理员口令哈希的【明文】归档（备份目录被读 = 全库凭据泄露）。
#   # 口令经 wrapper 的 stdin fd 0 单跳注入（见 deploy/prod/yiban-backup-wrapper.sh），
#   # 别再往 crontab 行/env 里写 BACKUP_GPG_PASSPHRASE——那会把口令带进整棵子进程树。
#   0 2 * * * /usr/local/sbin/yiban-backup-wrapper.sh >> /var/log/yiban/backup.log 2>&1
#   # 异机副本走 REMOTE_BACKUP：放 wrapper 之后的同一行 env 前缀即可（非机密主机名）：
#   # 0 2 * * * REMOTE_BACKUP=user@host:/backup/yiban /usr/local/sbin/yiban-backup-wrapper.sh >> /var/log/yiban/backup.log 2>&1
#   # 取证校验——锚点判据的另一半（离机留痕对照）不能只挂在 web 每日线程上：
#   # web 没起来 / 每日线程没跑到，删链与"锚点文件被截断"就永远没人查。
#   30 2 * * * cd /opt/yiban-auto-sign && python3 scripts/audit_verify.py --db yiban.db --env .env >> /var/log/yiban/audit-verify.log 2>&1
#   # 上一条的退出码 1 = 检出篡改/删除，2 = 无法定论（密钥缺失等）；0 才是健康。
#   # 想让 cron 直接告警，可包一层：|| mail -s 'yiban 审计校验失败' root@localhost
#   # 备份哨兵——本脚本的加密配置一旦失效会 fail-closed 不产出任何归档，cron 拿不到
#   # 任何信号：08:05 的哨兵就是替这里发声的那一步（详见 scripts/yiban-backup-sentinel.sh）
#   5 8 * * * APP_DIR=/opt/yiban-auto-sign /usr/local/sbin/yiban-backup-sentinel.sh >> /var/log/yiban/backup.log 2>&1
#
# 依赖：
#   - 本地打包：tar / find / sqlite3（系统自带）
#   - 默认加密/异机副本：age（apt install age）或 gpg（系统自带）；
#     rsync（apt install rsync）或 scp（系统自带）
#
# 退出码（M3 批次0 起完整口径；4/5 为既有契约，6/7/8 为本轮新增，互不重叠）：
#   0  正常（密文归档且回环自检通过）
#   1  拒绝执行/一般失败（RETENTION_DAYS 非法、--require-encrypt 不满足、解密失败等）
#   4  本轮归档照留，但【源库】integrity_check 未过（垂死库的最后素材）
#   5  本轮归档照留，但快照因缺 sqlite3 未经 integrity 核验
#   6  本轮产物是【明文】归档（显式 BACKUP_PLAINTEXT=1，或无可用加密方式回退）——
#      备份在，但全库凭据裸奔，cron 必须能把它与 0 区分开
#   7  加密"成功"但密文回环自检（解密→解包→integrity）未过：明文保留、清单不出、
#      半成品密文删除（MF-76 契约：可解才删明文）
#   8  轮转后当日归档失踪（保留策略误删当天件）——必须人工立即介入
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
# gpg 对称加密口令（生产推荐经 yiban-backup-wrapper.sh 的 **stdin fd 0 单跳**注入，
# 见下方 YIBAN_READ_PASSPHRASE_STDIN；环境变量 BACKUP_GPG_PASSPHRASE 保留给手工/
# 恢复场景——注意它意味着口令进整棵子进程树 env。旧名 BACKUP_AGE_PASSPHRASE 兼容回退——
# 2026-08-16 审查轮：原 AGE_PASSPHRASE 命名与 age 工具混淆，实际用途是 gpg AES-256 对称加密）
GPG_PASSPHRASE="${BACKUP_GPG_PASSPHRASE:-${BACKUP_AGE_PASSPHRASE:-}}"
GPG_RECIPIENT="${BACKUP_GPG_RECIPIENT:-}"           # gpg 接收者（公钥 ID），配置后走 gpg 公钥加密
# M24：本地归档默认加密的总开关——1 = 显式关闭（明文本地归档，大字告警）
BACKUP_PLAINTEXT="${BACKUP_PLAINTEXT:-0}"

# ------------------------------------------------------------
# M3 批次0 Task6（MF-42）：口令 --passphrase-fd 0 单跳摄取
# yiban-backup-wrapper.sh 不再 export 口令（旧形态把口令带进 tar/sqlite3/rsync/gpg
# 整棵子进程树的 environ，/proc/<pid>/environ 可读即泄露）。改由 wrapper 置
# YIBAN_READ_PASSPHRASE_STDIN=1 并把口令经管道送 stdin：此处读进**非导出**的
# GPG_PASSPHRASE shell 变量（下方加密/解密路径原样复用——它们本就
# printf|gpg --passphrase-fd 0 单跳，从环境变量降为纯 shell 变量后子进程环境清零），
# 并 unset 环境侧口令键。stdin 与 env 同时给了口令 ⇒ stdin 优先；只给标志既无
# stdin 内容也无 env 口令 ⇒ 拒跑（fail-closed，绝不静默回退明文，rc 口径不变）。
# ------------------------------------------------------------
if [ "${YIBAN_READ_PASSPHRASE_STDIN:-0}" = "1" ]; then
    _stdin_pass=""
    IFS= read -r _stdin_pass || true
    if [ -n "${_stdin_pass}" ]; then
        GPG_PASSPHRASE="${_stdin_pass}"
    elif [ -z "${GPG_PASSPHRASE}" ]; then
        echo "错误：YIBAN_READ_PASSPHRASE_STDIN=1 但 stdin 与 BACKUP_GPG_PASSPHRASE 均无口令，拒绝执行" >&2
        exit 1
    fi
    unset _stdin_pass
    # 无论口令最终来自哪一路，环境侧键一律摘除——子进程树不再继承口令
    unset BACKUP_GPG_PASSPHRASE BACKUP_AGE_PASSPHRASE
fi

# 待备份数据文件（均为相对 APP_DIR 的路径；文件不存在时静默跳过）
DATA_FILES=(.env)
# SQLite 数据库（账号+用户表；用 sqlite3 .backup 一致性快照，WAL 安全）
DB_FILE="${DB_FILE:-yiban.db}"
# 可选：签到状态文件目录（/var/log/yiban 根下，含 sign-daily-*.json 旧格式、
#      sign-state-*.json 结构化状态 与 cred-state.json 熔断状态；目录不存在则跳过）
# 应用侧统一键为 YIBAN_STATE_DIR（web/app.py、yiban/store/db.py、
#     .env.example），原 SIGN_STATE_DIR 与其脱钩——自定义状态目录时
#     sign-daily/sign-state/cred-state 静默不入备份包（影响"当天是否已签"的
#     判定恢复）。现以 YIBAN_STATE_DIR 优先，SIGN_STATE_DIR 仅作旧部署回退。
# cron / systemd 直接跑本脚本时进程环境里没有这些键（它们不读 .env），只按环境变量取会
# 让备份**抓错目录**：自定义状态或日志目录的部署会去抓 /var/log/yiban——同一台机器上
# 第二份部署的日志与状态就此混进别人的归档。故 .env 是第二来源，键名与 .env.example 一致。
env_get() {  # $1=键名；进程环境优先，回退 ${APP_DIR}/.env（只认行首 键=值，跳过注释行）
    local v
    v="$(printenv "$1" 2>/dev/null || true)"
    if [ -z "${v}" ] && [ -f "${APP_DIR}/.env" ]; then
        v="$(sed -n "s/^$1=//p" "${APP_DIR}/.env" | head -1 | tr -d '\r')"
    fi
    printf '%s' "${v}"
}
YIBAN_STATE_DIR="${YIBAN_STATE_DIR:-$(env_get YIBAN_STATE_DIR)}"
YIBAN_LOG_FILE="${YIBAN_LOG_FILE:-$(env_get YIBAN_LOG_FILE)}"
SIGN_STATE_DIR="${YIBAN_STATE_DIR:-${SIGN_STATE_DIR:-/var/log/yiban}}"
# 可选：按天签到日志目录（sign-YYYY-MM-DD.log；过期清理由 yiban-cleanup.sh 负责，此处仅备份现存量）
# 跟随 YIBAN_LOG_FILE 所在目录（两者都没配，才回落到与状态目录同级的默认值）
SIGN_LOG_DIR="${SIGN_LOG_DIR:-$(dirname "${YIBAN_LOG_FILE:-${SIGN_STATE_DIR}/sign.log}")}"

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
# MF-76「可解才删明文」：try_encrypt 返回 0 只证明 gpg 没报错，不证明密文可解
# （退出码判据的缺口正是 docker/backup-docker.sh:103-118 早已堵上的——尺寸下限 +
# 解密解包自检 + 失败删件非 0 退出）。这里用与 --restore 同源的逻辑对当日密文做
# 一次真实回环：解密到临时目录 → tar 可列可解 → 包内数据库在 sqlite3 可用时必须
# integrity_check=ok。任何一步失败 ⇒ 调用方【不得删明文、不得出清单】并以 rc=7 结束。
# age 是交互口令加密（仅手动运行时可用），无法无人值守解密——只能做尺寸下限并
# 大字提醒立刻手工跑 --restore；这是刻意保留的最小核查，报告里已声明该边界。
# 解密产物只落在 TMPDIR_BAK（0700，trap 统一清理），不新增第二处明文驻留。
# ------------------------------------------------------------
MIN_ENC_BYTES=200
verify_encrypted_archive() {
    local enc="$1" vdir="${TMPDIR_BAK}/roundtrip" plain="${TMPDIR_BAK}/roundtrip.tar.gz"
    local bytes ic
    rm -rf "${vdir}" "${plain}"
    mkdir -p "${vdir}"
    bytes="$(wc -c < "${enc}" 2>/dev/null || echo 0)"
    if [ "${bytes}" -lt "${MIN_ENC_BYTES}" ]; then
        log "回环自检：密文仅 ${bytes} 字节（下限 ${MIN_ENC_BYTES}），疑似空包/半写入" >&2
        return 1
    fi
    case "${enc}" in
        *.gpg)
            if [ -n "${GPG_PASSPHRASE}" ]; then
                if ! printf '%s\n' "${GPG_PASSPHRASE}" | \
                    gpg --batch --yes --decrypt --passphrase-fd 0 \
                        -o "${plain}" "${enc}"; then
                    log "回环自检：密文解密失败（口令/密钥环异常或包损坏）" >&2
                    return 1
                fi
            elif ! gpg --batch --yes --decrypt -o "${plain}" "${enc}"; then
                log "回环自检：公钥密文解密失败（gpg 密钥环不可用？）" >&2
                return 1
            fi
            ;;
        *.age)
            log "回环自检：age 密文无法无人值守解密——本轮仅做尺寸下限核查，" \
                "请立刻手工跑一次 --restore 演练（${enc}）" >&2
            return 0
            ;;
    esac
    if ! tar -tzf "${plain}" > /dev/null; then
        log "回环自检：解密产物不是可列目录的 tar.gz（加密过程损坏？）" >&2
        return 1
    fi
    if ! tar -xzf "${plain}" -C "${vdir}"; then
        log "回环自检：解密产物解包失败" >&2
        return 1
    fi
    if [ -f "${vdir}/data/${DB_FILE}" ] && command -v sqlite3 > /dev/null 2>&1; then
        ic="$(sqlite3 "${vdir}/data/${DB_FILE}" "PRAGMA integrity_check;")" \
            || { log "回环自检：integrity_check 无法执行" >&2; return 1; }
        if [ "${ic}" != "ok" ]; then
            log "回环自检：integrity_check 未通过：${ic}" >&2
            return 1
        fi
        log "回环自检：解密→解包→integrity_check=ok 全部通过"
    fi
    return 0
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
    # 安全校验（M3 批次0 · MF-80 重写）：三条护栏改为显式判码执行。
    # 旧写法 `if tar -tzf … 2>/dev/null | grep …` 在 set -eo pipefail 下，tar 一旦非 0
    # （包损坏/被替换/读错误）整条管道短路为假 ⇒ 三条护栏【全部跳过】（活体复现
    # GUARD_SKIPPED），失败被拖到解包那一步撞运气。现在"列目录读不了"本身就是
    # 拒绝解包的理由；tar 的诊断也不再被 2>/dev/null 吞掉。
    local list vlist
    if ! list="$(tar -tzf "$plain" 2>&1)"; then
        echo "错误：读包列表失败（tar 非零退出：包损坏或 tar 不可用），拒绝解包" >&2
        printf '%s\n' "${list}" >&2
        exit 1
    fi
    if printf '%s\n' "${list}" | grep -qE '(^|/)\.\.(/|$)|^/'; then
        echo "错误：备份包含不安全条目（路径穿越/绝对路径），已拒绝解包" >&2
        exit 1
    fi
    if ! vlist="$(tar -tvzf "$plain" 2>&1)"; then
        echo "错误：读包列表失败（长列表 tar 非零退出），拒绝解包" >&2
        printf '%s\n' "${vlist}" >&2
        exit 1
    fi
    if printf '%s\n' "${vlist}" | grep -qE '^l'; then
        echo "错误：备份包含符号链接条目，已拒绝解包（防链接写出目标目录）" >&2
        exit 1
    fi
    # 2026-08-21 对抗性审查加固：同时拒绝设备/字符设备/FIFO 条目——root 恢复时
    # 恶意包可在目标目录创建设备节点（前提苛刻，属纵深防御）
    if printf '%s\n' "${vlist}" | grep -qE '^[bcp]'; then
        echo "错误：备份包含设备/FIFO 特殊条目，已拒绝解包" >&2
        exit 1
    fi
    mkdir -p "$dest"
    # --anchored 移除（MF-80："死选项"）：GNU tar 的 --anchored 只在配有 pattern 时
    # 生效，本命令没有 pattern，旧"首选 --anchored 失败 ⇒ 回退不带"的结构里首选
    # 永远白跑一遍，加固从未存在；防逃逸的职责已由上面解包前的三条护栏承担。
    if ! tar -xzf "$plain" -C "$dest" --no-overwrite-dir; then
        echo "错误：解包失败（备份包可能损坏）" >&2
        exit 1
    fi
    log "已从 $archive 恢复到 $dest"

    # 1) 停服提示：进程还活着时覆盖数据目录 = 把主库和它自己的 WAL 写成两份不一致
    #    状态，恢复件当场不可用（容器部署下 web/scheduler 是 supervisord 子进程，
    #    `supervisorctl stop web scheduler` 这类服务名不存在，必须停整个容器）。
    log "⚠ 覆盖回生产前必须先停服：systemctl stop yiban-web / docker compose stop yiban"
    log "⚠ 未停服就替换 yiban.db 会造成主库与 -wal 不一致（丢数据或 file is not a database）"

    # 2) 删残留 -wal/-shm：只替换 yiban.db 而留着上一次的 WAL，SQLite 启动时会把
    #    旧 WAL 重放到刚恢复出来的库上——等于把"恢复"变成"回滚掉刚恢复的内容"。
    for suffix in -wal -shm; do
        if [ -f "${dest}/data/${DB_FILE}${suffix}" ]; then
            rm -f "${dest}/data/${DB_FILE}${suffix}"
            log "已删除恢复目录里的残留 ${DB_FILE}${suffix}（必须与主库同源，不得跨备份混用）"
        fi
    done

    log "恢复内容清单："
    find "$dest" -type f -exec ls -l {} \;
    log "落位说明（生产路径按 APP_DIR / YIBAN_STATE_DIR 调整）："
    log "  - data/${DB_FILE} → ${APP_DIR}/${DB_FILE}"
    log "  - data/.env       → ${APP_DIR}/.env"
    log "  - keys/           → ${KEY_FILE}（或与 .env 合并）"
    log "  - state/          → \${YIBAN_STATE_DIR}（含 sched-run-*/sched-slot-*/sched-snapshot-*/"
    log "                      notify-ledger.json/notify-throttle.json/cred-state.json）"
    if [ -f "${dest}/state/audit-anchor.log" ]; then
        log "  - state/audit-anchor.log → \${YIBAN_STATE_DIR}/audit-anchor.log"
        log "    ↑ 必须与恢复出来的库【同批次】落位：锚点是审计链唯一的外部参照，"
        log "      只回库不回锚点（或回错批次）会让「删尾/删前缀/整表清空」判据整体失效。"
    else
        log "  ⚠ 本备份包内没有 audit-anchor.log —— 恢复后审计防删除判据不可用，"
        log "    需另从离机副本（每日日报邮件 / 异机备份）取回锚点，或重新建立基线"
    fi
    log "  - logs/           → \${YIBAN_LOG_FILE} 所在目录（可选，仅供回看）"

    # 3) 恢复件双验：integrity_check 证明"包没坏、库能开"，audit_verify 证明
    #    "链自洽 + 与锚点对得上 + 无写入欠账"。前者过了后者不过，通常意味着
    #    库与锚点不是同一批次（恢复到半程）——这正是最危险的"看起来成功了"。
    local rc=0
    if command -v sqlite3 > /dev/null 2>&1 && [ -f "${dest}/data/${DB_FILE}" ]; then
        local ic
        ic=$(sqlite3 "${dest}/data/${DB_FILE}" "PRAGMA integrity_check;" 2>/dev/null) || ic="(读取失败)"
        if [ "$ic" = "ok" ]; then
            log "恢复件核验：integrity_check=ok"
        else
            log "错误：恢复件 integrity_check 未通过：${ic}" >&2
            rc=1
        fi
        local n_accounts
        n_accounts=$(sqlite3 "${dest}/data/${DB_FILE}" "SELECT COUNT(*) FROM accounts" 2>/dev/null || echo "?")
        log "  - yiban.db 账号数量：${n_accounts:-0}"
    else
        log "警告：缺 sqlite3 或包内无 ${DB_FILE}，跳过 integrity_check 与账号数核对"
    fi
    log "  - keys/ 目录是否含密钥：$(ls "${dest}/keys/" 2>/dev/null | tr '\n' ' ' || echo '无（备份时密钥缺失）')"

    if [ -f "${dest}/data/${DB_FILE}" ]; then
        # 解释器优先用部署自己的 venv：系统 python3 通常没有 pycryptodome 等依赖，
        # 用它跑会是 ImportError——而 ImportError 的退出码同样是 1，按退出码判就把
        # "工具没跑起来"报成"审计被篡改"，恢复演练因此得出完全错误的结论。
        local py=""
        if [ -x "${APP_DIR}/.venv/bin/python" ]; then py="${APP_DIR}/.venv/bin/python"
        elif command -v python3 > /dev/null 2>&1; then py="python3"; fi
        if [ -n "${py}" ] && [ -f "${APP_DIR}/scripts/audit_verify.py" ]; then
            log "恢复件核验：审计链 + 锚点 + 写入欠账（audit_verify.py，只读；解释器 ${py}）"
            local out arc=0
            out="$(YIBAN_STATE_DIR="${dest}/state" \
                "${py}" "${APP_DIR}/scripts/audit_verify.py" \
                    --db "${dest}/data/${DB_FILE}" \
                    --env "${dest}/data/.env" \
                    --anchor "${dest}/state/audit-anchor.log" 2>&1)" || arc=$?
            printf '%s\n' "${out}"
            # 判据取 CLI **自己的结论文本**，不看退出码：崩溃与"检出篡改"同为非 0。
            if [ "$arc" -eq 0 ] && printf '%s' "${out}" | grep -q "校验通过"; then
                log "恢复件核验：审计校验通过（链自洽 + 与锚点一致 + 无写入欠账）"
            elif printf '%s' "${out}" | grep -q "审计可追溯性校验失败"; then
                log "错误：恢复件审计校验【检出异常】——链被改写/删除，或库与锚点不是同一批次" >&2
                rc=1
            else
                log "错误：恢复件审计校验【无法定论】（退出码 $arc；缺依赖/YIBAN_AUDIT_KEY/包内 .env）" >&2
                log "      别按「备份完好」处理，也别按「被篡改」处理——补齐解释器与密钥来源后重跑" >&2
                rc=2
            fi
        else
            log "错误：包内含数据库，但找不到可用解释器（${APP_DIR}/.venv/bin/python 或 python3）或 ${APP_DIR}/scripts/audit_verify.py——恢复件未经审计核验" >&2
            log "      别按「备份完好」处理：先手工跑一次 audit_verify.py 再决定" >&2
            rc=2
        fi
    fi
    log "提示：恢复演练请核对上述内容后删除临时目录；真实恢复时先停服，再把 data/ keys/ state/ 覆盖回" \
        "${APP_DIR} 与 \${YIBAN_STATE_DIR} 并 chmod 600，然后再跑一次 audit_verify.py。"
    return "$rc"
}

# ------------------------------------------------------------
# 参数解析：解析**全部**参数（顺序无关、未知即拒绝）。
# 只看 `${1}` 会让旗标出现在其它位置时门禁静默失效（加密配置坏了也照出明文归档），
# 未知参数被当"没有参数"直接跑一轮备份。故必须扫全部参数，未知/移位一律在重活前拒绝。
# ------------------------------------------------------------
RESTORE_MODE=0 RESTORE_ARCHIVE="" RESTORE_TARGET="" REQUIRE_ENCRYPT=0
while [ $# -gt 0 ]; do
    case "$1" in
        --require-encrypt) REQUIRE_ENCRYPT=1 ;;
        # 每个位置参数各自判 $# 再 shift：`--restore <包>` 这类只剩 1 个参数时，
        # 无条件的尾部 shift 会让 $# 归 0 而 shift 返回 1——set -euo pipefail 下
        # 整个脚本立刻退出 1 且零输出（连"备份包不存在"的诊断都看不到）。
        # 缺参留给 restore() 报明确原因；`--restore a b c` 里多出的 c 会被下面的
        # `*)` 分支按未知参数拒绝，不再被静默丢弃。
        --restore)
            RESTORE_MODE=1
            shift
            RESTORE_ARCHIVE="${1:-}"; if [ $# -gt 0 ]; then shift; fi
            RESTORE_TARGET="${1:-}";  if [ $# -gt 0 ]; then shift; fi
            continue
            ;;
        -h|--help)
            echo "用法: backup.sh [--require-encrypt] | backup.sh --restore <备份包> <目标目录>"
            exit 0
            ;;
        *)
            echo "错误：未知参数：$1（支持 --require-encrypt 与 --restore <包> <目标目录>）" >&2
            exit 1
            ;;
    esac
    shift
done

if [ "$RESTORE_MODE" -eq 1 ]; then
    # restore() 会带回核验结论（integrity_check / audit_verify 不通过时非 0）——
    # 原实现无条件 exit 0，等于"恢复件是坏的"也报恢复成功。
    restore "$RESTORE_ARCHIVE" "$RESTORE_TARGET"
    exit $?
fi

# RETENTION_DAYS 校验（M3 批次0 · MF-77）：原实现零校验，`RETENTION_DAYS=0` 是合法值
# （find -mtime +0 = 删掉除当天外全部历史），一次误配置就能把 30 天素材清成 1 天，
# 且删完不数不验不写日志——完全静默。非数字（如 "30d"）同理会让四条 find 集体报错或
# 误删。备份轮一律拒绝执行；--restore 在上方已分发，不受此校验牵连（它是只读恢复）。
case "${RETENTION_DAYS}" in
    '' | *[!0-9]*)
        echo "错误：RETENTION_DAYS 必须是正整数（收到：'${RETENTION_DAYS}'），拒绝执行以免误删历史备份" >&2
        exit 1
        ;;
esac
if [ "${RETENTION_DAYS}" -lt 1 ]; then
    echo "错误：RETENTION_DAYS=0 等于删掉除当天外全部历史备份，拒绝执行（要更短保留请显式设为 >=1 的合理值）" >&2
    exit 1
fi

# --require-encrypt 前置校验：打包前确认加密工具可用，不满足则直接退出。
# 只强制"本轮归档必须加密成功"，与异机副本（REMOTE_BACKUP）解耦——未配异机的
# 单机部署同样需要可用的强制加密（2026-09-08）：REMOTE_BACKUP 为空时不再阻止
# 生成并保留本地密文归档；加密失败仍在打包后走 fail-closed 清场（见下方 M24 段）。
if [ "$REQUIRE_ENCRYPT" -eq 1 ]; then
    if { [ -n "$GPG_RECIPIENT" ] || [ -n "$GPG_PASSPHRASE" ]; } && ! command -v gpg >/dev/null 2>&1; then
        echo "错误：--require-encrypt 指定且配置了 gpg，但未找到 gpg 命令，拒绝创建本地明文归档" >&2
        exit 1
    fi
    if [ -z "$GPG_RECIPIENT" ] && [ -z "$GPG_PASSPHRASE" ] && { ! command -v age >/dev/null 2>&1 || [ ! -t 0 ]; }; then
        echo "错误：--require-encrypt 指定但无可用加密方式（需配置 BACKUP_GPG_RECIPIENT / BACKUP_GPG_PASSPHRASE，或交互终端 + age），拒绝创建本地明文归档" >&2
        exit 1
    fi
fi

# BACKUP_PLAINTEXT=1 与 --require-encrypt 互斥检查前移到打包之前。
# 原检查位于 tar 打包之后（下方 342-347 行），拒绝路径无 rm -f——明文归档
# （含 .env 全部密钥 + 数据库 + accounts-key）已落盘 BACKUP_DIR，违反
# --require-encrypt 的"不得生成明文归档"契约。此处提前拦截，不生成任何归档。
if [ "${BACKUP_PLAINTEXT}" = "1" ] && [ "$REQUIRE_ENCRYPT" -eq 1 ]; then
    echo "错误：BACKUP_PLAINTEXT=1 与 --require-encrypt 互斥，请移除其一（已提前拦截，未生成任何归档）" >&2
    exit 1
fi

# ------------------------------------------------------------
# 本地打包
# ------------------------------------------------------------
# MF-79：归档内是 .env（管理员口令哈希/数据密钥）+ 整库 + accounts-key——文件 0600
# 但目录从不设防（umask 077 只护新建文件，已有目录、以及此前建的目录都不在保护范围）。
# 备份目录与主库同机时"目录被列 = 全部备份可枚举"，这里显式收紧 0700；收紧失败
# （目录属主不是运行用户）必须当场失败，不能让裸奔的旧 0755 混过本轮。
mkdir -p "${BACKUP_DIR}"
if ! chmod 0700 "${BACKUP_DIR}"; then
    echo "错误：无法将备份目录 ${BACKUP_DIR} 收紧为 0700（属主不对/只读挂载？），拒绝继续" >&2
    exit 1
fi
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
#
#     快照必须过 integrity_check 才算备份成功（原实现回退 cp 后【静默】归档——
#     WAL 未合并时 cp 出来的可能是撕裂副本，"看着有备份"等于没有备份）：
#       .backup 成功 + 校验不过 → 源库本身已损坏：保留归档（垂死的库这份往往是
#         最后一份素材，也是取证对象），但非 0 退出 + 大字告警；
#       cp 回退 + 校验不过 → 这是一份"看似成功的坏备份"：删掉快照、不落该归档、
#         非 0 退出（坏副本比没有副本更危险——它会让人删掉真正的好副本）；
#       无 sqlite3 可校验 → 保留 + 大字告警（不得静默）。
DB_SNAPSHOT_VERIFIED=0
verify_db_snapshot() {
    local snap="$1" out
    [ -f "$snap" ] || return 1
    command -v sqlite3 > /dev/null 2>&1 || return 2
    out=$(sqlite3 "$snap" "PRAGMA integrity_check;" 2>/dev/null) || return 1
    [ "$out" = "ok" ] || { log "  integrity_check 输出：${out}"; return 1; }
    return 0
}

if [ -f "${APP_DIR}/${DB_FILE}" ]; then
    if command -v sqlite3 > /dev/null 2>&1; then
        if sqlite3 "${APP_DIR}/${DB_FILE}" ".backup ${TMPDIR_BAK}/data/${DB_FILE}" 2>/dev/null; then
            if verify_db_snapshot "${TMPDIR_BAK}/data/${DB_FILE}"; then
                DB_SNAPSHOT_VERIFIED=1
                log "数据库已备份（一致性快照，integrity_check=ok）：${DB_FILE}"
            else
                # 源库损坏：归档照留，但绝不报"备份完成"
                log "════════════════════════════════════════════════════════════"
                log "⚠⚠⚠ 快照 integrity_check 未通过——【源库】已损坏，非本脚本问题 ⚠⚠⚠"
                log "⚠⚠⚠ 本轮归档保留（最后一份素材），但请立即检查/重建数据库 ⚠⚠⚠"
                log "════════════════════════════════════════════════════════════"
                CORRUPT_SOURCE=1
            fi
        else
            log "警告：sqlite3 .backup 失败（${DB_FILE} 可能被占用），回退为文件复制"
            cp -p "${APP_DIR}/${DB_FILE}" "${TMPDIR_BAK}/data/" 2>/dev/null || \
                { log "警告：无法复制 ${DB_FILE}，已跳过"; rm -f "${TMPDIR_BAK}/data/${DB_FILE}"; }
            if [ -f "${TMPDIR_BAK}/data/${DB_FILE}" ]; then
                if verify_db_snapshot "${TMPDIR_BAK}/data/${DB_FILE}"; then
                    DB_SNAPSHOT_VERIFIED=1
                    log "cp 回退快照 integrity_check=ok（WAL 未合并，恢复前建议人工确认）"
                else
                    log "错误：cp 回退的快照 integrity_check 未通过——WAL 未合并的副本不可信"
                    log "错误：不落该归档（坏副本会让人误以为有备份），本轮以非 0 退出"
                    rm -f "${TMPDIR_BAK}/data/${DB_FILE}"
                    exit 1
                fi
            fi
        fi
    else
        log "════════════════════════════════════════════════════════════"
        log "警告：未安装 sqlite3，回退为文件复制且【无法】做 integrity_check" >&2
        log "警告：WAL 未合并时快照可能不完整——请尽快安装 sqlite3 重新备份   " >&2
        log "════════════════════════════════════════════════════════════" >&2
        cp -p "${APP_DIR}/${DB_FILE}" "${TMPDIR_BAK}/data/" 2>/dev/null || \
            { log "警告：无法复制 ${DB_FILE}，已跳过"; rm -f "${TMPDIR_BAK}/data/${DB_FILE}"; }
    fi
else
    log "跳过（不存在）：${APP_DIR}/${DB_FILE}"
fi
# 收尾退出码判据用（set -u：必须先初始化再引用）
CORRUPT_SOURCE="${CORRUPT_SOURCE:-0}"
if [ -f "${TMPDIR_BAK}/data/${DB_FILE}" ]; then
    DB_SNAPSHOT_PRESENT=1
else
    DB_SNAPSHOT_PRESENT=0
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
    # 见 yiban/infra/account_crypto.py；会话签名密钥是另一把 YIBAN_SECRET_KEY）
    if [ -f "${APP_DIR}/.env" ]; then
        grep -E '^[[:space:]]*YIBAN_ACCOUNTS_KEY=' "${APP_DIR}/.env" > "${TMPDIR_BAK}/keys/secret-key.env" \
            && log "密钥来源：${APP_DIR}/.env 内 YIBAN_ACCOUNTS_KEY（已单独提取）" \
            || log "警告：.env 中未找到 YIBAN_ACCOUNTS_KEY，密钥未备份！"
    else
        log "警告：未找到密钥文件（${KEY_FILE}）与 .env，密钥未备份！"
    fi
fi

# 3) 状态文件（可选）：/var/log/yiban 根下的当日闸门标记、通知账本与审计链锚点
#    ——glob 匹配多个模式；无匹配时静默跳过。
#    清单里每一类"可重建"程度不同，缺了会怎样都记在这里：
#      sign-daily-*/sign-state-*  当天是否已签的判定（缺 → 当天可能重签）
#      cred-state.json            熔断状态（缺 → 已熔断的账号被立即再试）
#      sched-run-*/sched-slot-*   当日全量/分片闸门标记（缺 → 恢复当天重签或漏签）
#      sched-snapshot-*           当日调度快照标记（缺 → 窗口判定回落）
#      notify-ledger.json         邮件/推送当日额度账本（缺 → 日额度重置，
#                                 当天所有告警重发一遍，反过来也能被用来刷屏）
#      notify-throttle.json       通知节流状态（缺 → 同上，节流窗口失效）
#      audit-anchor.log           审计链外部锚点（缺 → 恢复出来的库再也无法检出
#                                 删尾/删前缀/整表清空：锚点是链的唯一外部参照，
#                                 只备 yiban.db 等于把"防删除"那半边一起丢掉）
if [ -d "${SIGN_STATE_DIR}" ]; then
    shopt -s nullglob
    state_files=(
        "${SIGN_STATE_DIR}"/sign-daily-*.json
        "${SIGN_STATE_DIR}"/sign-state-*.json
        "${SIGN_STATE_DIR}"/cred-state.json
        "${SIGN_STATE_DIR}"/sched-run-*.json
        "${SIGN_STATE_DIR}"/sched-slot-*.json
        "${SIGN_STATE_DIR}"/sched-snapshot-*.json
        "${SIGN_STATE_DIR}"/notify-ledger.json
        "${SIGN_STATE_DIR}"/notify-throttle.json
        "${SIGN_STATE_DIR}"/audit-anchor.log
    )
    shopt -u nullglob
    if [ ${#state_files[@]} -gt 0 ]; then
        mkdir -p "${TMPDIR_BAK}/state"
        cp -p "${state_files[@]}" "${TMPDIR_BAK}/state/" 2>/dev/null \
            && log "已备份状态/闸门/账本/审计锚点文件（${#state_files[@]} 个）"
        # 锚点在包内却没有库内"曾经写入"痕迹可对照时同样是盲区——这里只保证它在包里
        if [ -f "${SIGN_STATE_DIR}/audit-anchor.log" ]; then
            log "已包含审计链外部锚点：${SIGN_STATE_DIR}/audit-anchor.log"
        else
            log "提示：未发现审计锚点（${SIGN_STATE_DIR}/audit-anchor.log）——" \
                "web 未跑过每日线程或被配到别处时属正常，恢复后审计防删除判据不可用"
        fi
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
if [ "${BACKUP_PLAINTEXT}" = "1" ] && [ "${REQUIRE_ENCRYPT:-0}" -eq 1 ]; then
    # 两个 flag 并存时 --require-encrypt 的"不得生成明文归档"契约
    # 被静默违背——显式互斥。
    # 前置互斥检查已提前拦截本分支，此处仅作兜底清场——拒绝路径
    # 必须删除已落盘明文归档再退出，与 remote_fail 清场同语义。
    echo "错误：BACKUP_PLAINTEXT=1 与 --require-encrypt 互斥，已删除本轮明文归档并退出" >&2
    rm -f "$ARCHIVE"
    exit 1
fi
# 本轮产物是否为明文（BACKUP_PLAINTEXT=1 显式关闭 / 无可用加密方式回退）——
# 收尾以专用退出码 6 让 cron 与"密文正常轮"区分（MF-79）。
PLAINTEXT_LOCAL=0
if [ "${BACKUP_PLAINTEXT}" = "1" ]; then
    PLAINTEXT_LOCAL=1
    # 告警走 stderr（M3 批次0 · MF-79）：stdout 是 backup.log 里的例行流水，
    # 明文轮的大字告警不该淹死在其中；退出码 6 + stderr 双通道才可能被接进监控。
    log "════════════════════════════════════════════════════════════" >&2
    log "⚠⚠⚠ 已显式设置 BACKUP_PLAINTEXT=1：本轮生成【明文】本地归档 ⚠⚠⚠" >&2
    log "⚠⚠⚠ 归档内含 .env 全部密钥、管理员口令哈希与全量数据库！   ⚠⚠⚠" >&2
    log "⚠⚠⚠ 本轮将以退出码 6 结束；明文包保留期从紧（≤2 天），哨兵不认它。 ⚠⚠⚠" >&2
    log "════════════════════════════════════════════════════════════" >&2
    # 明文豁免只作用于【本地】归档；配置了 REMOTE_BACKUP 时
    # 异机副本仍会加密后出站（异机副本绝不传明文），不是一并取消。
    [ -n "${REMOTE_BACKUP}" ] && log "已配置 REMOTE_BACKUP：异机副本仍将加密后出站（非明文）"
elif try_encrypt; then
    # MF-76 契约：可解才删明文。旧实现在 try_encrypt 报 0 后【立刻】rm 明文——
    # 但"加密退出码 0"与"密文可解"之间从未有任何一步真实回环（--restore 才是
    # 唯一能证明可解的路径，而它没有任何自动调用点）。现在先解密→解包→integrity
    # 回环一次，通过才删明文；失败则保留明文、删除坏密文、不出清单、rc=7 结束。
    if verify_encrypted_archive "${ENC_FILE}"; then
        rm -f "${ARCHIVE}"
        log "已启用本地默认加密：回环自检通过，明文归档已移除，本轮密文为 ${ENC_FILE}"
    else
        log "════════════════════════════════════════════════════════════" >&2
        log "⚠⚠⚠ 密文回环自检失败：无法证明 ${ENC_FILE} 可解且完好        ⚠⚠⚠" >&2
        log "⚠⚠⚠ 契约（MF-76）：可解才删明文——本轮【保留】明文归档 ${ARCHIVE}" >&2
        log "⚠⚠⚠ 不出校验清单；半成品密文已删除；退出码 7。请立即人工核查  ⚠⚠⚠" >&2
        log "════════════════════════════════════════════════════════════" >&2
        rm -f "${ENC_FILE}"
        exit 7
    fi
else
    # --require-encrypt 契约必须 fail-closed——管理员显式要求加密时，
    # 加密失败（gpg 密钥环损坏/口令错误/IO 错误）绝不允许静默回退明文归档。
    # 原实现仅打印警告并保留明文（含 .env 全部密钥 + 数据库 + accounts-key 同包），
    # 与脚本头"拒绝创建本地明文归档"的承诺相悖；且异机同步失败的 remote_fail()
    # 在 REQUIRE_ENCRYPT=1 时已会清场，本地加密失败却无同等级处置——处置不对称。
    # 取舍：宁可当天无备份（保留 30 天、错一天可容忍），不可把凭据明文落盘。
    if [ "${REQUIRE_ENCRYPT:-0}" -eq 1 ]; then
        echo "错误：--require-encrypt 指定但加密失败（检查 BACKUP_GPG_RECIPIENT / BACKUP_GPG_PASSPHRASE 与 gpg 密钥环），已删除明文归档并拒绝继续" >&2
        rm -f "${ARCHIVE}" "${ARCHIVE}.sha256"
        exit 1
    fi
    PLAINTEXT_LOCAL=1
    log "════════════════════════════════════════════════════════════" >&2
    log "⚠⚠⚠ 无法加密本地归档（未配置 BACKUP_GPG_RECIPIENT/BACKUP_GPG_PASSPHRASE， ⚠⚠⚠" >&2
    log "⚠⚠⚠ 且非交互终端无法使用 age）：本轮为【明文】归档，请尽快配置加密！     ⚠⚠⚠" >&2
    log "⚠⚠⚠ 本轮将以退出码 6 结束；备份哨兵同样不会把明文包计为健康。           ⚠⚠⚠" >&2
    log "════════════════════════════════════════════════════════════" >&2
fi

# 最终落盘产物 = 密文（默认）或明文（显式关闭/无法加密）；sha256 清单始终对应
# 实际落盘文件。产物名按 .tar.gz / .tar.gz.gpg / .tar.gz.age 区分加密形态，
# 密文包以 .sha256 清单核验完整性，二次副本下载流程无需人工调整。
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
    # 去掉原 `[ "${BACKUP_PLAINTEXT}" != "1" ]` 拦截——BACKUP_PLAINTEXT=1
    # 只豁免【本地】归档的默认加密，异机副本契约不变：本地为明文时仍尝试加密后再
    # 出站（异机副本绝不传明文）。原拦截让"显式明文 + 配置了异机"时异机副本被
    # 整体静默丢弃，且告警文案误导（有 gpg 却报"未提供可用加密方式"）。
    if [ -z "${REMOTE_FILE}" ]; then
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
        if [ "${BACKUP_PLAINTEXT}" = "1" ]; then
            # 明确区分"显式明文"与"无加密方式"——前者更该知道
            # 明文豁免只作用于本地，异机副本因此缺位时需要的是配置加密而非关明文
            log "警告：BACKUP_PLAINTEXT=1 只豁免本地归档的默认加密，异机副本绝不传明文（未配置/不可用加密时本轮无异机副本）" >&2
            log "警告：请配置 BACKUP_GPG_RECIPIENT 或 BACKUP_GPG_PASSPHRASE 后重试（本地明文归档已保留）" >&2
        else
            log "警告：未提供可用加密方式（gpg/age），拒绝把【明文】备份传出本机；仅保留本地备份" >&2
            log "警告：（备份包含密钥+数据；请配置 BACKUP_GPG_RECIPIENT 或 BACKUP_GPG_PASSPHRASE 后重试）" >&2
        fi
    fi
else
    # M24 后此分支仅在 BACKUP_PLAINTEXT=1 或无可用加密方式时到达（均已有大字告警）
    log "REMOTE_BACKUP 未配置，仅保留本地${ENC_FILE:+密文}备份"
fi

# ------------------------------------------------------------
# 本地保留策略（M3 批次0 · MF-77 重写）：mtime 判过期 + 文件名日期"最近 K 组"下界
# + 逐件写日志 + 删后自检当日件
# ------------------------------------------------------------
# .sha256 侧车必须一起轮转：原三条 glob 只覆盖 tar.gz/.age/.gpg，侧车永久堆积——
# 既无限增长，又把"哪天做了备份、产物叫什么名"整份泄露给任何能读备份目录的账号
# （对攻击者这就是一张"哪天该去删哪条审计"的地图）。
# 原实现的四条 find -mtime +N -delete 有三个洞：RETENTION_DAYS 零校验（已在上方
# 拒绝）、命中即删不留一行日志（删错了无从追溯）、删完不数不验（历史被清空而
# 当天件在位时，只查当日包的哨兵完全静默）。这里对齐同仓 pull-prod-backup.sh
# "按文件名日期保留最近 N 份"的口径补上界之外的下界：
# - 下界：按文件名日期（yiban-YYYY-MM-DD*，一天一组，密文/侧车同组共命运）倒序
#   保留最近 ${MIN_KEEP_ARCHIVES} 组——mtime 再老也不许把地板删穿（时钟前跳、
#   cp -p 保时间戳导入、RETENTION 被改小都是真实触发路径）；
# - 明文包从紧：裸 .tar.gz 与其侧车至多留 ${PLAIN_MAX_AGE_DAYS} 天——明文归档含
#   .env 全部密钥，30 天保留期=30 天泄露窗口，2 天已够"昨天出事今天还有素材可查"；
#   与 RETENTION_DAYS 取小者执行。**明文 pass 豁免下界（终审 Important①）**：日备机器
#   第 3 天的明文包仍落在最近 7 组内，下界会把头注释/rc=6 承诺的"≤2 天"静默压成 ~7 天
#   ——下界保护可恢复性（密文是资产），明文是泄露面不是资产；机制取调用点 nofloor 标志。
# K=7 的理由：一周兜底份数——单日误删/坏包时还有可回退的最近一整个星期；
# 可用 BACKUP_MIN_KEEP 覆盖（0=关闭下界，仅剩 mtime 判据——自担风险）。
MIN_KEEP_ARCHIVES="${BACKUP_MIN_KEEP:-7}"
case "${MIN_KEEP_ARCHIVES}" in
    '' | *[!0-9]*)
        echo "错误：BACKUP_MIN_KEEP 必须是非负整数（收到：'${MIN_KEEP_ARCHIVES}'）" >&2
        exit 1
        ;;
esac
PLAIN_MAX_AGE_DAYS=2

# 保留最近 K 组的"组键"（文件名里的 YYYY-MM-DD 段，按字典序=日期序倒排）。
# 不用 head（pipefail 下 head 提前关管道会让 sort 吃 SIGPIPE 而整体失败），用 awk。
KEEP_KEYS="$(find "${BACKUP_DIR}" -maxdepth 1 -name 'yiban-*' -printf '%f\n' 2>/dev/null \
    | sed -nE 's/^yiban-([0-9]{4}-[0-9]{2}-[0-9]{2}).*/\1/p' \
    | sort -u -r | awk -v n="${MIN_KEEP_ARCHIVES}" 'NR<=n')"

rotate_pass() {  # $1=文件名模式 $2=生效天数 $3=nofloor(非空=豁免最近 K 组下界，明文 pass 专用)
    local pat="$1" days="$2" nofloor="${3:-}" f key why
    [ -n "${nofloor}" ] && why="> ${days} 天，明文从紧·豁免下界" || why="> ${days} 天且已过最近 ${MIN_KEEP_ARCHIVES} 组下界"
    while IFS= read -r f; do
        [ -f "${f}" ] || continue  # 前面的 pass 可能已删过同组侧车，不重复报删除
        key="$(basename "${f}" | sed -nE 's/^yiban-([0-9]{4}-[0-9]{2}-[0-9]{2}).*/\1/p')"
        if [ -z "${nofloor}" ] && [ -n "${key}" ] && [ -n "${KEEP_KEYS}" ] \
            && printf '%s\n' "${KEEP_KEYS}" | grep -qxF "${key}"; then
            continue  # 下界保护：最近 K 组之内，mtime 再老也不删（明文 pass 豁免此门）
        fi
        rm -f "${f}"
        log "轮转删除（${why}）：$(basename "${f}")"
    done < <(find "${BACKUP_DIR}" -maxdepth 1 -name "${pat}" -mtime "+${days}")
}

PLAIN_DAYS="${RETENTION_DAYS}"
if [ "${PLAIN_MAX_AGE_DAYS}" -lt "${PLAIN_DAYS}" ]; then
    PLAIN_DAYS="${PLAIN_MAX_AGE_DAYS}"
fi
# 明文两条 pass 传 nofloor：过期即删，即使日期组仍在最近 K 内（终审 Important①）；密文/兜底仍受下界保护。
rotate_pass 'yiban-*.tar.gz' "${PLAIN_DAYS}" nofloor
rotate_pass 'yiban-*.tar.gz.sha256' "${PLAIN_DAYS}" nofloor
rotate_pass 'yiban-*.tar.gz.gpg' "${RETENTION_DAYS}"
rotate_pass 'yiban-*.tar.gz.gpg.sha256' "${RETENTION_DAYS}"
rotate_pass 'yiban-*.tar.gz.age' "${RETENTION_DAYS}"
rotate_pass 'yiban-*.tar.gz.age.sha256' "${RETENTION_DAYS}"
# 兜底：旧命名/孤儿的其它 yiban-*.sha256 侧车按主保留期清（防止回到"侧车永久堆积"）
rotate_pass 'yiban-*.sha256' "${RETENTION_DAYS}"
log "本地清理完成（密文 ${RETENTION_DAYS} 天/明文 ${PLAIN_DAYS} 天，含 .sha256 侧车；密文受最近 ${MIN_KEEP_ARCHIVES} 组下界保护，明文过期即删·豁免下界）"

# 删后自检（MF-77）：轮转"删完不数不验"正是历史被清而无人知的成因之一。
# 当日归档是整条保留策略的锚点——它不在就说明轮转（或别的什么）删错了东西，
# 必须以最大声音失败，绝不允许绿灯收工。
if [ ! -f "${FINAL_LOCAL}" ]; then
    echo "错误：轮转后当日归档失踪（${FINAL_LOCAL} 不在）——保留策略误删当天件，请立即重跑备份并排查" >&2
    exit 8
fi

log "=== 备份完成：${FINAL_LOCAL} ==="
log "恢复演练：bash backup.sh --restore ${FINAL_LOCAL} /tmp/yiban-restore-test"

# 退出码不得谎报"备份成功"：源库损坏 / 快照没做过 integrity 核验时，归档照留
# （垂死的库这份往往是最后一份素材），但 cron 必须看见非 0，否则坏库会安静地
# 把所有后续备份都变成"备份了一份损坏数据"。
if [ "${CORRUPT_SOURCE:-0}" -eq 1 ]; then
    echo "错误：本轮归档内的数据库快照未过 integrity_check（源库损坏），请立即处理" >&2
    exit 4
fi
if [ "${DB_SNAPSHOT_PRESENT:-0}" -eq 1 ] && [ "${DB_SNAPSHOT_VERIFIED}" -ne 1 ]; then
    echo "警告：本轮数据库快照未经 integrity_check 核验（缺 sqlite3 命令）" >&2
    exit 5
fi
if [ "${PLAINTEXT_LOCAL:-0}" -eq 1 ]; then
    # MF-79：明文轮不再与密文轮共用退出码 0——"备份在，但全库凭据裸奔"必须让
    # cron/监控一眼可辨（哨兵侧的 unhealthy 判定见 scripts/backup_sentinel.py）。
    echo "提示：本轮产物为【明文】归档（BACKUP_PLAINTEXT=1 或无可用加密方式），退出码 6" >&2
    exit 6
fi
