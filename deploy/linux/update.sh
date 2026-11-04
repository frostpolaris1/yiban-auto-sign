#!/usr/bin/env bash
# ============================================================
# deploy/linux/update.sh — 裸机一键更新：自检 → 备份 → 停服 → 迁移 →
# 切换 → 健康检查 → 失败回滚（二进制/UI 总是回滚；数据库默认恢复）。
#
# 顺序（U2，2026-11-04）：
#   1. 参数与用法检查（签名三件、--server-bin 等，均在任何写入之前）
#   2. flock（<安装位>.lock）与输入快照（root 私有暂存目录，防 TOCTOU）
#   3. 签名发布物校验（信任锚 → 验签 → sha256 对账 → 版本下限）
#   4. 自检门：yiban-cli selfcheck --json（U1 判据）。任何 FAIL → exit 2，
#      不停服、不写安装位、不留暂存件
#   5. 预检：数据库连接角色必须有 CREATEDB（未给 --no-restore-db 时），
#      否则失败后无法回库 → exit 2，不开始
#   6. 统一备份：pg_dump -Fc + pg_restore --list 校验 + 行数/版本记录；
#      可选 yiban-cli backup（GPG + 行数清单）；照片目录 tar 快照；
#      当前二进制归档；sha256 清单。备份失败即中止（仍不停服）
#   7. 停服 → yiban-cli migrate（记录迁移前后 goose 版本）→ 换二进制 → 换 UI
#   8. 起服务 → /healthz 重试探活
#   9. 成功：selfcheck --record-keys（失败只 WARN，不回滚）、写状态、保留备份
#   10. 失败：停服 → 换回 .bak 二进制与 UI → 迁移后若库已改变且备份已验证，
#       默认恢复到新库 yiban_restore_<ts> 并原子切 /etc/yiban/server.env 的
#       YIBAN_DATABASE_URL（原库原样保留取证）→ 起旧件 → 探活。
#       --no-restore-db 显式关闭数据库恢复（只回二进制/UI）。
#
# 用法：
#   sudo bash deploy/linux/update.sh --server-bin NEW [--cli-bin NEW] \
#       [--ui-dir NEW_DIST] \
#       --manifest M --signature S --pubkey K [--expect-version V] \
#       [--no-restore-db] [--backup-dir DIR] [--keep-backups N] \
#       [--backup-passphrase-file FILE]
#   sudo bash deploy/linux/update.sh --selfcheck-only
#
#   只给 --server-bin 时迁移用已安装的 yiban-cli；--ui-dir 给新的前端构建
#   产物，旧目录留 .bak 供人工回退。--expect-version 要求清单版本严格相等
#   （防已签名旧包重放）。
#   旧布局保护：仅存 /etc/yiban/.env（旧名）而无 server.env 时 exit 2 并打印
#   迁移步骤——生成新 server.env 会产生新密钥，旧凭据密文将不可解密。
#
# 退出码（保留既有语义；预检拒绝归 2）：
#   0 成功（含 --selfcheck-only 全 PASS/SKIP）
#   1 升级失败但已回滚到旧件（旧二进制在跑且健康）；签名/对账/版本校验
#     不通过也归 1（与旧版一致，校验失败不碰旧件）
#   2 用法错误、并发锁被占、自检 FAIL、预检（CREATEDB/口令隔离/备份）FAILED、
#     旧布局保护（/etc/yiban/.env 无 server.env）、
#     停服失败（什么都没换）、回滚不完整（见下）、回滚后仍不健康、
#     数据库恢复失败（都打印 manual action）
#   停服成功之后任何一步失败（migrate、换件、systemctl start、健康检查）
#   都走同一条回滚：换回 server/CLI/UI，再按需恢复数据库，最后起旧件探活。
#   退出码：
#     · 二进制/UI/CLI 全部换回且旧件健康 → 1；
#     · 旧件健康但 CLI 没换回（备份里缺 CLI、或备份 CLI 被篡改而拒绝执行）
#       → 2 + manual action（旧件在跑，CLI 版本不配对，必须人工处置）；
#     · 旧件不健康，或数据库恢复失败 → 2 + manual action。
#
# 自检门（U2 新增，fail-closed）：
#   - 用**已安装**的 yiban-cli 跑 `selfcheck --json`：它的嵌入迁移号与库
#     同版本，db.migrations_pending 才能如实判定「升级前系统是健康的」。
#   - 已安装 CLI 不支持 selfcheck（U1 之前的发布件）→ exit 2，提示先升到
#     含 U1 的发布件。自检不可用不等于绿。
#   - --selfcheck-only 不取锁、不写任何文件，只打印摘要与失败项。
#
# 统一备份（U2 新增；落在 /var/lib/yiban-updater/backups/<ts>/，0700/0600）：
#   状态目录 = /var/lib/yiban-updater/state（root 0700；update-state.json、
#   update.log、selfcheck-*.json）。备份根与状态目录同级（不在其下）。
#   备份根与状态目录都是 **root:root 0700**，父目录 /var/lib/yiban-updater
#   也归 root（服务用户改不了名，塞不进软链/恶意件）；备份含凭据密文，回滚
#   还会执行备份内的 CLI。为什么不放在 /var/lib/yiban 下：那里归服务用户
#   （install.sh 把 home 与 photos/ui 交给它），任何子目录它都能换名/替换。
#   U1 的 fs.backup_dir 按**调用者（root）**判权；服务用户写不了是要求，不是故障。
#   旧布局 /var/lib/yiban/{pre-migrate,backups,updates} 不删、不读：新备份只落
#   新根，旧备份仍可手工 pg_restore（root 0600，需 root 读）。
#   每次备份目录：
#     db.dump             pg_dump -Fc（--no-owner --no-privileges），0600
#     db.list             pg_restore --list 的清单，0600（校验转储可读）
#     db-counts.txt       可选：yiban-cli backup 的行数基线（stdout）
#     db.sql.gpg          可选：yiban-cli backup 的 GPG 对称加密归档（含
#                         行数 manifest）——需要口令文件或 YIBAN_BACKUP_PASSPHRASE
#     db-schema.txt       迁移前库名、goose 版本与 public 基表数（恢复后对账基线）
#     env.backup          /etc/yiban/server.env 快照（0600；内含密钥与 DSN 原文）
#     photos.tar          照片目录快照（不跟随内部软链；照片不进数据库备份）
#     bin/                当前已装的 server/cli（含仍在的旧 .bak 归档）
#     SHA256SUMS          以上文件的 sha256（0600，写入后立即 -c 校验）
#   保留最近 --keep-backups 份（缺省 5；0 = 不清理）。只删本目录下形如
#   20261104T101500Z 的子目录；备份失败不删任何东西。
#
# DSN 与口令（U2 新增）：
#   连接口令**不进任何子进程 argv**（/proc/<pid>/cmdline 全局可读）。
#   update.sh 把 DSN 里的口令摘出来写进 0600 的 PGPASSFILE（暂存目录，
#   退出即删），交给 libpq；argv 只保留去掉口令的 DSN。URI 里的百分号
#   编码会解码；key=value 形态按 libpq conninfo 词法解析。解析不了就
#   exit 2（fail-closed），绝不把口令塞回 argv。yiban-cli 走环境变量，
#   不传 --dsn。
#
# 数据库恢复（U2 新增；默认开启）：
#   - 触发条件：失败发生在 migrate 之后（含 migrate 失败），且库已改变
#     （迁移前后 goose 版本不同；读不到版本时按已改变处理保守恢复），
#     且本次 db.dump 已通过 pg_restore --list 校验。
#   - 动作：CREATE DATABASE yiban_restore_<UTC时间戳>（要求连接角色有
#     CREATEDB）→ pg_restore 到新库 → 校验新 DSN 的 current_database() 与
#     新库同名（绝不把备份写回原库）→ 校验 goose 版本与基表数和备份基线
#     一致 → 原子改写 /etc/yiban/server.env 的 YIBAN_DATABASE_URL → 起旧二进制。
#   - 原库原样保留（迁移过的失败库）供取证；不删、不 --clean、不原地覆盖。
#   - 已知代价：健康探测窗口内新二进制写入的行不会保留（恢复到备份时刻）；
#     旧库仍在，供人工对账。
#   - 恢复失败 → 停住（不启动旧件：库状态未知，静默服务更危险），
#     exit 2 并打印 manual action。
#
# 状态与日志：
#   /var/lib/yiban-updater/state/update-state.json（0600）每次更新整体重写：
#   schema_version=1、update_id、started_at、finished_at、result
#   （running/success/failed/aborted/interrupted）、exit_code、phase、
#   version_from、version_to、backup_dir、db_name_before/after、
#   db_schema_before/after、db_restore（disabled/not-needed/restored/failed）、
#   db_restore_target、log_file。
#   /var/lib/yiban-updater/state/update.log 追加一行一事件（时间/阶段/结果）。
#   两者都不含 DSN 口令、密钥与 GPG 口令；DSN 只出现库名与主机。
#
# 签名发布物（默认强制，docs/dev/release-signing.md）：
#   三件必须同时给。给了 → 在任何文件替换之前先钉公钥指纹、再跑
#   scripts/release/verify.sh 验签，并把清单里的 sha256 逐一与本地新件对账
#   （server 必须，cli 给了也算）；任一失败即非 0 退出，不碰旧件。
#   未给三件 → 默认拒绝（exit 2，发生在停服务之前）。
#   逃生口：显式给 --allow-unsigned 才放行未签名升级，放行时打印一行
#   显性 WARN（跳过 ≠ 绿）。--allow-unsigned 与三件互斥，同给即 exit 2。
#
# 信任锚（P0）：签名只证明清单出自某把私钥，不证明那把钥是发布方的。
# update.sh 先钉公钥指纹，再验签：
#   YIBAN_RELEASE_PUBKEY_SHA256（环境），或
#   <安装根>/release-pubkey.sha256（管理员安装时写入，一行 64 位十六进制）
# 两者都缺 → exit 2（fail-closed）；指纹不匹配 → exit 2。
#
# 并发：起手拿 flock（<安装位>.lock）。两次并发共用 .new/.bak，交错写会
# 覆盖回滚件；拿不到锁即 exit 2。
#
# 输入快照：哈希、验签、安装只读 root 私有暂存目录（mktemp -d，0700）里的
# 副本。校验开始后原路径可被改写（TOCTOU），快照之后不再读原路径。
# 失败路径清理快照与 .new 暂存件。
#
# 信任锚文件（P3）：锚必须 root 持有，且组/其他不可写；不符 exit 2。
# 干跑（有安装根前缀）下不校验属主，只校验权限位。
#
# 既有 .bak：本次更新开始前若安装位已有 .bak，先原样归档进本次备份目录
# （bin/yiban-server.rollback-point），再让本轮生成新的回滚点。
#
# 干跑钩子：YIBAN_INSTALL_ROOT=前缀 SYSTEMCTL=命令覆盖（与 install.sh 同义）。
# 破坏性操作（停服、换件、恢复数据库）只在自检与备份都通过之后执行。
# ============================================================
set -euo pipefail

ROOT="${YIBAN_INSTALL_ROOT:-}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
UNIT="yiban.service"

# 仓库根由脚本自身位置推出：运行配置模板取 <repo>/deploy/baremetal/server.env.example。
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# dest 把系统绝对路径映射到干跑前缀下。
dest() { printf '%s%s' "$ROOT" "$1"; }

SERVER_BIN=""
CLI_BIN=""
UI_DIR=""
MANIFEST=""
SIGNATURE=""
PUBKEY=""
EXPECT_VERSION=""
ALLOW_UNSIGNED=""
SELFCHECK_ONLY=""
NO_RESTORE_DB=""
BACKUP_ROOT_FLAG=""
KEEP_BACKUPS="5"
BACKUP_PASS_FILE=""
# need_value <flag> [value]：缺值/空值是用例错误（exit 2）。
# 不用 ${2:?}：它退 1，而用法错误的口径是 2。
need_value() {
    if [ "$#" -lt 2 ] || [ -z "$2" ]; then
        echo "update: $1 requires a non-empty value" >&2
        exit 2
    fi
}
while [ $# -gt 0 ]; do
    case "$1" in
        --server-bin) need_value "$@"; SERVER_BIN="$2"; shift 2 ;;
        --cli-bin) need_value "$@"; CLI_BIN="$2"; shift 2 ;;
        --ui-dir) need_value "$@"; UI_DIR="$2"; shift 2 ;;
        --manifest) need_value "$@"; MANIFEST="$2"; shift 2 ;;
        --signature) need_value "$@"; SIGNATURE="$2"; shift 2 ;;
        --pubkey) need_value "$@"; PUBKEY="$2"; shift 2 ;;
        --expect-version) need_value "$@"; EXPECT_VERSION="$2"; shift 2 ;;
        --allow-unsigned) ALLOW_UNSIGNED=1; shift ;;
        --selfcheck-only) SELFCHECK_ONLY=1; shift ;;
        --no-restore-db) NO_RESTORE_DB=1; shift ;;
        --backup-dir) need_value "$@"; BACKUP_ROOT_FLAG="$2"; shift 2 ;;
        --keep-backups) need_value "$@"; KEEP_BACKUPS="$2"; shift 2 ;;
        --backup-passphrase-file) need_value "$@"; BACKUP_PASS_FILE="$2"; shift 2 ;;
        *) echo "update: unknown argument: $1" >&2; exit 2 ;;
    esac
done

SERVICE_USER="yiban"
SERVICE_HOME="/var/lib/yiban"

# 探活参数（回滚路径也会用，必须在任何失败分支之前就位）。
ADDR="${YIBAN_HTTP_ADDR:-127.0.0.1:47183}"
RETRIES="${YIBAN_HEALTH_RETRIES:-10}"
INTERVAL="${YIBAN_HEALTH_RETRY_INTERVAL:-1}"

SERVER_DEST="$(dest /usr/local/bin/yiban-server)"
CLI_DEST="$(dest /usr/local/bin/yiban-cli)"
SERVER_NEW="$SERVER_DEST.new"
BAK="$SERVER_DEST.bak"
UI_DEST="$(dest "$SERVICE_HOME/ui")"
# UI 暂存在 root 独占的更新器目录里（与 /var/lib/yiban 同文件系统 ⇒ mv -T
# 是原子 rename）。不能就地 cp 到 $UI_DEST.new：服务用户能在那里预置软链，
# root 的 cp/chmod 会顺链写到 /root 或旧备份并改坏其权限（复审 HIGH）。
UI_STAGE=""
UI_BAK="$UI_DEST.bak"
ENV_FILE="$(dest /etc/yiban/server.env)"

# 更新器的状态与备份根：/var/lib/yiban-updater/{state,backups}。
# 为什么不放在 $SERVICE_HOME 里：那里归服务用户（install.sh 把 home 与
# photos/ui 交给它），任何子目录它都能换名/替换；备份里含凭据密文，回滚还会
# 执行备份里的 CLI，root 顺着被替换的路径写/执行就是提权。
# 放在**服务用户不可重命名的 root 独占父目录**下才安全：
#   /var/lib        root:root 0755（yiban 无写权限 ⇒ 不能改名/换软链）
#   /var/lib/yiban-updater  root:root 0700（yiban 连列目录都不行）
# 备份根可用 --backup-dir 覆盖（必须是绝对路径，且拒绝系统/共享目录）。
UPDATER_ROOT="$(dest /var/lib/yiban-updater)"
STATE_DIR="$UPDATER_ROOT/state"
STATE_FILE="$STATE_DIR/update-state.json"
LOG_FILE="$STATE_DIR/update.log"

# check_backup_parent_chain <dir>：备份根必须是「root 独占路径」。
# 只查叶子不够：/tmp/evil/backups 的叶子可以自己建，但 /tmp 谁都能写，
# 服务用户能把 evil 换名/塞成软链，root 随后顺着它写。
# 判据（复审 MEDIUM-1）：
#   · 路径里不许有 `..`（/tmp/../etc 这类绕道直接拒）；
#   · realpath -m 归一化，逐级走到 /（干跑时走到安装根为止）；
#   · 每一级都必须是目录、不是软链、属主不是服务用户、组/其他不可写
#     （/tmp 的 sticky 位也算「可写」）。
# 成功时把归一化后的路径打到 stdout，调用方用它覆盖 BACKUP_ROOT。
check_backup_parent_chain() {
    local dir="$1" norm p owner mode stop_at="$ROOT"
    case "$dir" in
        *..*)
            echo "update: --backup-dir must not contain '..': $dir" >&2
            return 1
            ;;
    esac
    command -v realpath >/dev/null 2>&1 || {
        echo "update: realpath not found; cannot validate the backup path" >&2
        return 1
    }
    norm="$(realpath -m -- "$dir")" || {
        echo "update: cannot normalize the backup path: $dir" >&2
        return 1
    }
    p="$norm"
    while :; do
        p="$(dirname -- "$p")"
        if [ -n "$stop_at" ] && [ "$p" = "$stop_at" ]; then
            break
        fi
        if [ -L "$p" ]; then
            echo "update: --backup-dir parent is a symlink: $p" >&2
            return 1
        fi
        if [ -e "$p" ]; then
            if [ ! -d "$p" ]; then
                echo "update: --backup-dir parent is not a directory: $p" >&2
                return 1
            fi
            owner="$(stat -c '%U' -- "$p" 2>/dev/null || true)"
            mode="$(stat -c '%a' -- "$p" 2>/dev/null || true)"
            case "$mode" in
                ''|*[!0-7]*) mode=777 ;;
            esac
            if [ "$owner" = "$SERVICE_USER" ]; then
                echo "update: --backup-dir parent is owned by the service user: $p" >&2
                return 1
            fi
            if [ $(( 8#$mode & 8#022 )) -ne 0 ]; then
                echo "update: --backup-dir parent is group/other writable (mode $mode): $p" >&2
                return 1
            fi
        fi
        [ "$p" = "/" ] && break
    done
    printf '%s' "$norm"
}

if [ -n "$BACKUP_ROOT_FLAG" ]; then
    case "$BACKUP_ROOT_FLAG" in
        /*) ;;
        *) echo "update: --backup-dir must be an absolute path: $BACKUP_ROOT_FLAG" >&2; exit 2 ;;
    esac
    BACKUP_ROOT="$BACKUP_ROOT_FLAG"
else
    BACKUP_ROOT="$UPDATER_ROOT/backups"
fi
case "$BACKUP_ROOT" in
    /|*/) echo "update: refusing backup root path: $BACKUP_ROOT" >&2; exit 2 ;;
esac
# 归一化 + 父链校验；成功时函数回写归一化路径。
if ! BACKUP_ROOT="$(check_backup_parent_chain "$BACKUP_ROOT")"; then
    exit 2
fi
# 归一化之后再查一遍共享/系统目录名单（拼写变体在这里兜底）。
# 服务用户主目录（$SERVICE_HOME 及其干跑前缀）也拒：install.sh 不再递归 chown 它，
# 但那里的内容仍归服务用户，不能当备份根。
case "$BACKUP_ROOT" in
    /|/var|/var/backups|/tmp|/home|/root|/etc|/usr|/opt|/srv|/boot|/dev|/proc|/sys|/run|/var/lib|/var/log|/var/lib/yiban|"$SERVICE_HOME"|"$SERVICE_HOME"/*|"$(dest "$SERVICE_HOME")"|"$(dest "$SERVICE_HOME")"/*)
        echo "update: --backup-dir $BACKUP_ROOT is a shared/system directory; point it at a dedicated subdirectory (e.g. /var/lib/yiban-updater/backups)" >&2
        exit 2
        ;;
esac
case "$KEEP_BACKUPS" in
    ''|*[!0-9]*) echo "update: --keep-backups must be a non-negative integer: $KEEP_BACKUPS" >&2; exit 2 ;;
esac

# ---- 小工具 ----------------------------------------------------------------

# 状态机与取证变量。PHASE 记录最后进入的阶段，失败时写进状态文件。
PHASE="preflight"
UPDATE_ID=""
STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
FINISHED_AT=""
VERSION_FROM="unknown"
VERSION_TO="unknown"
BACKUP_SET=""
DB_BACKUP_VERIFIED=0
PRE_GOOSE_VERSION=""
PRE_TABLE_COUNT=""
POST_GOOSE_VERSION=""
DB_RESTORED=0
DB_RESTORE_TARGET=""
BINARY_SWAPPED=0
CLI_SWAPPED=0
UI_OLD_MOVED=0
UI_ACTIVATED=0
CLI_ROLLBACK_FAILED=0
DB_RESTORE_FAILED=0
# STATE_READY=1 表示状态/备份目录已通过 prepare_state_dir 的安全校验
# （非软链、属主 root、组/其他不可写）。日志写入以它为闸门。
STATE_READY=0
# DSN_FULL 是含口令的原文：只用于写回 /etc/yiban/server.env 与进程环境。
# DSN 是去掉口令的 argv 安全形态；口令经 0600 PGPASSFILE 交给 libpq。
DSN_FULL=""
DSN=""
DSN_PASSWORD=""
DSN_NAME=""
DSN_NAME_AFTER=""
PGPASS_FILE=""

# json_escape 把值转成 JSON 字符串内容。只做最小必要转义：
# 反斜杠、双引号先处理；控制字符换成空格（路径/版本里不该出现）。
json_escape() {
    local s="$1"
    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    s="${s//$'\n'/ }"
    s="${s//$'\r'/}"
    s="${s//$'\t'/ }"
    printf '%s' "$s"
}

# log_event 追加一行事件到 update.log。日志写失败不中止升级：
# 状态文件与屏幕输出是主证据，日志是附加。
# 只在 prepare_state_dir 校验通过（STATE_READY=1）后才写：校验之前状态目录
# 可能是攻击者放的软链，root 的 >> 会顺着它写到别处（复审 HIGH-3）。
log_event() {
    [ "$STATE_READY" -eq 1 ] || return 0
    [ ! -L "$STATE_DIR" ] || return 0
    [ ! -L "$LOG_FILE" ] || return 0
    printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" 2>/dev/null >>"$LOG_FILE" || true
}

note() {
    echo "update: $*"
    log_event "$*"
}

warn() {
    echo "update: WARN: $*" >&2
    log_event "WARN: $*"
}

# write_state 原子写 update-state.json（0600）。未知字段不保留：本文件
# 由本脚本独占，整体重写；U1 的密钥状态在 /var/lib/yiban/update-state.json，
# 这里**不**碰它（只通过 selfcheck --record-keys 更新）。
write_state() {
    local result="$1" code="$2" tmp
    [ -n "$STATE_FILE" ] || return 0
    # 与 log_event 同闸门：EXIT trap 可能在状态目录被换成软链后才跑到这里，
    # 不校验就等于 root 顺着软链写（复审 MEDIUM-6）。
    [ "$STATE_READY" -eq 1 ] || return 1
    [ ! -L "$STATE_DIR" ] || return 1
    [ ! -L "$STATE_FILE" ] || return 1
    FINISHED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    tmp="$STATE_FILE.tmp.$$"
    (
        umask 077
        printf '{\n'
        printf '  "schema_version": 1,\n'
        printf '  "update_id": "%s",\n' "$(json_escape "$UPDATE_ID")"
        printf '  "started_at": "%s",\n' "$(json_escape "$STARTED_AT")"
        printf '  "finished_at": "%s",\n' "$(json_escape "$FINISHED_AT")"
        printf '  "result": "%s",\n' "$(json_escape "$result")"
        printf '  "exit_code": %s,\n' "$code"
        printf '  "phase": "%s",\n' "$(json_escape "$PHASE")"
        printf '  "version_from": "%s",\n' "$(json_escape "$VERSION_FROM")"
        printf '  "version_to": "%s",\n' "$(json_escape "$VERSION_TO")"
        printf '  "backup_dir": "%s",\n' "$(json_escape "$BACKUP_SET")"
        printf '  "db_name_before": "%s",\n' "$(json_escape "$DSN_NAME")"
        printf '  "db_name_after": "%s",\n' "$(json_escape "$DSN_NAME_AFTER")"
        printf '  "db_schema_before": "%s",\n' "$(json_escape "$PRE_GOOSE_VERSION")"
        printf '  "db_schema_after": "%s",\n' "$(json_escape "$POST_GOOSE_VERSION")"
        printf '  "db_restore": "%s",\n' "$(json_escape "$(db_restore_label)")"
        printf '  "db_restore_target": "%s",\n' "$(json_escape "$DB_RESTORE_TARGET")"
        printf '  "log_file": "%s"\n' "$(json_escape "$LOG_FILE")"
        printf '}\n'
    ) >"$tmp" || { rm -f -- "$tmp"; return 1; }
    chmod 600 "$tmp" || { rm -f -- "$tmp"; return 1; }
    if [ -z "$ROOT" ]; then
        chown root:root "$tmp" || { rm -f -- "$tmp"; return 1; }
    fi
    mv -f "$tmp" "$STATE_FILE"
}

# db_restore_label 把恢复结果压成状态文件里的枚举。
db_restore_label() {
    if [ -n "$NO_RESTORE_DB" ]; then
        printf 'disabled'
        return 0
    fi
    if [ "$DB_RESTORED" -eq 1 ]; then
        printf 'restored'
        return 0
    fi
    if [ "$DB_RESTORE_FAILED" -eq 1 ]; then
        printf 'failed'
        return 0
    fi
    if [ "$DB_BACKUP_VERIFIED" -eq 1 ]; then
        printf 'not-needed'
        return 0
    fi
    printf 'unavailable'
}

# finish <result> <exit_code>：写状态、打一行摘要、退出。
finish() {
    local result="$1" code="$2"
    if ! write_state "$result" "$code"; then
        echo "update: WARN: cannot write update state $STATE_FILE" >&2
    fi
    log_event "result=$result exit_code=$code phase=$PHASE"
    note "state: $STATE_FILE (result=$result exit_code=$code)"
    exit "$code"
}

# manifest_action 打印人工恢复步骤。数据库恢复失败时调用：库状态未知，
# 不自动启动旧件（静默服务比停住更危险）。
print_manual_action() {
    local reason="$1"
    local target="${DB_RESTORE_TARGET:-yiban_restore_<ts>}"
    echo "update: manual action required ($reason)" >&2
    echo "update: 1) 备份在 ${BACKUP_SET:-<无>}（db.dump, db.list, SHA256SUMS）" >&2
    echo "update: 2) 核对失败库：psql \"\$YIBAN_DATABASE_URL\" -c 'select max(version_id) from goose_db_version'" >&2
    echo "update: 3) 手工恢复（选一）：" >&2
    echo "update:    a. sudo -u postgres createdb ${target}" >&2
    echo "update:       # db.dump 是 root 0600（含凭据密文），postgres 读不到原文：复制一份给它，用完删。" >&2
    echo "update:       sudo install -m 0640 -o postgres -g postgres ${BACKUP_SET:-<备份目录>}/db.dump /tmp/yiban-restore.dump" >&2
    echo "update:       sudo -u postgres pg_restore --no-owner --no-privileges -d ${target} /tmp/yiban-restore.dump" >&2
    echo "update:       sudo rm -f /tmp/yiban-restore.dump" >&2
    echo "update:       然后把 /etc/yiban/server.env 的 YIBAN_DATABASE_URL 改到 ${target}，systemctl start $UNIT" >&2
    echo "update:    b. 若已确认旧二进制与新 schema 兼容：sudo systemctl start $UNIT" >&2
    echo "update: 4) 原库未删除，保留供取证；确认无误后再人工处置" >&2
    log_event "manual action required: $reason"
}

# readonly_env 载入 /etc/yiban/server.env 的 KEY=VALUE（systemd 同源）。
# **不** source：DSN 含 `&` 或 `#` 时 shell 会切开赋值，变量落进子 shell
# 不外泄，迁移收到空 DSN。本函数只做四件事：跳过空行与 `#` 注释、
# 取第一个 `=` 分割键值、去掉值两端的成对单/双引号、只导出合法键名。
load_env_file() {
    local file="$1" line key value
    [ -f "$file" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%$'\r'}"
        case "$line" in
            ''|'#'*) continue ;;
        esac
        case "$line" in
            *=*) ;;
            *) continue ;;
        esac
        key="${line%%=*}"
        value="${line#*=}"
        # 键名必须匹配 ^[A-Za-z_][A-Za-z0-9_]*$；其余行不是配置项。
        case "$key" in
            ''|[0-9]*|*[!A-Za-z0-9_]*) continue ;;
        esac
        case "$value" in
            \"*\") value="${value:1:${#value}-2}" ;;
            \'*\') value="${value:1:${#value}-2}" ;;
        esac
        export "$key=$value"
    done < "$file"
}

# psql_q <dsn> <sql>：单值查询（unaligned tuples），失败返回非 0。
# 传进来的 DSN 必须是 prepare_pgpass 之后的**无口令**形态；连接口令走
# PGPASSFILE（argv 里没有秘密）。输出与日志不回显 DSN。
psql_q() {
    psql "$1" -X -q -A -t -v ON_ERROR_STOP=1 -c "$2" 2>/dev/null
}

# schema_version_of <dsn>：goose 最高已应用版本；无版本表返回 0。
schema_version_of() {
    psql_q "$1" "SELECT CASE WHEN to_regclass('goose_db_version') IS NULL THEN 0
                  ELSE (SELECT COALESCE(max(version_id), 0) FROM goose_db_version WHERE is_applied) END"
}

# table_count_of <dsn>：public 下基表数（恢复后对账基线）。
table_count_of() {
    psql_q "$1" "SELECT count(*) FROM information_schema.tables
                  WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
}

# role_can_createdb <dsn>：当前连接角色能否建库（superuser 也算）。
# 注意：不要写 ::text —— boolean::text 是 'true'/'false'，
# psql -tA 输出裸 boolean 才是 't'/'f'。
role_can_createdb() {
    local out
    out="$(psql_q "$1" "SELECT (rolsuper OR rolcreatedb) FROM pg_roles WHERE rolname = current_user")" || return 1
    [ "$out" = "t" ]
}

# version_of <binary>：读 --version 的 version 行；失败返回 unknown。
version_of() {
    local out
    out="$("$1" --version 2>/dev/null)" || { printf 'unknown'; return 0; }
    out="$(printf '%s\n' "$out" | sed -n 's/^version: *//p' | head -n1)"
    [ -n "$out" ] || { printf 'unknown'; return 0; }
    printf '%s' "$out" | tr -cd 'A-Za-z0-9._+-'
}

# ---- DSN 与口令隔离 --------------------------------------------------------
# 任何子进程（psql/pg_dump/pg_restore）的 argv 都不得含口令：
# 口令经 0600 的 PGPASSFILE 交给 libpq（与 cmd/cli/backup.go 同口径），
# argv 只留「去掉口令的 DSN」。解析不了就失败关闭——绝不把口令塞回 argv。

# urldecode <百分号编码串>：按字节还原 %XX（先转义反斜杠，避免 %b 误读）。
urldecode() {
    local s
    s="$(printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/%\([0-9A-Fa-f][0-9A-Fa-f]\)/\\x\1/g')" \
        || return 1
    printf '%b' "$s"
}

# dsn_kw_fields <conninfo>：按 libpq conninfo 词法切进全局数组
# DSN_KW_FIELDS，每项保留原文 `key=value`（值含引号与转义原样保留）。
# 规则：= 两侧空白可选；单引号内的空白与 = 不切；反斜杠转义下一字符；
# 引号内相邻两个单引号表示一个字面单引号。非键值片段原样交出，调用方判失败。
dsn_kw_fields() {
    local s="$1" n=${#1} i=0 key val start c
    DSN_KW_FIELDS=()
    while [ "$i" -lt "$n" ]; do
        while [ "$i" -lt "$n" ]; do
            case "${s:i:1}" in ' '|$'\t'|$'\n'|$'\r') i=$((i + 1)) ;; *) break ;; esac
        done
        [ "$i" -lt "$n" ] || break
        key=""
        while [ "$i" -lt "$n" ]; do
            c="${s:i:1}"
            case "$c" in '='|' '|$'\t'|$'\n'|$'\r') break ;; esac
            key="$key$c"
            i=$((i + 1))
        done
        while [ "$i" -lt "$n" ]; do
            case "${s:i:1}" in ' '|$'\t'|$'\n'|$'\r') i=$((i + 1)) ;; *) break ;; esac
        done
        if [ "$i" -ge "$n" ] || [ "${s:i:1}" != "=" ]; then
            DSN_KW_FIELDS+=("$key")
            continue
        fi
        i=$((i + 1))
        while [ "$i" -lt "$n" ]; do
            case "${s:i:1}" in ' '|$'\t'|$'\n'|$'\r') i=$((i + 1)) ;; *) break ;; esac
        done
        start=$i
        if [ "$i" -lt "$n" ] && [ "${s:i:1}" = "'" ]; then
            i=$((i + 1))
            while [ "$i" -lt "$n" ]; do
                c="${s:i:1}"
                if [ "$c" = "\\" ]; then
                    i=$((i + 2))
                    continue
                fi
                if [ "$c" = "'" ]; then
                    if [ $((i + 1)) -lt "$n" ] && [ "${s:i+1:1}" = "'" ]; then
                        i=$((i + 2))
                        continue
                    fi
                    i=$((i + 1))
                    break
                fi
                i=$((i + 1))
            done
        else
            while [ "$i" -lt "$n" ]; do
                case "${s:i:1}" in ' '|$'\t'|$'\n'|$'\r') break ;; esac
                i=$((i + 1))
            done
        fi
        val="${s:start:i-start}"
        DSN_KW_FIELDS+=("$key=$val")
    done
}

# dsn_kw_unquote <原始值>：去两端单引号并还原 libpq 转义（\x 与 ''）。
dsn_kw_unquote() {
    local v="$1" out="" i=0 n
    case "$v" in
        \'*\') v="${v:1:${#v}-2}" ;;
        *) printf '%s' "$v"; return 0 ;;
    esac
    n=${#v}
    while [ "$i" -lt "$n" ]; do
        if [ "${v:i:1}" = "\\" ] && [ $((i + 1)) -lt "$n" ]; then
            out="$out${v:i+1:1}"
            i=$((i + 2))
            continue
        fi
        if [ "${v:i:1}" = "'" ] && [ $((i + 1)) -lt "$n" ] && [ "${v:i+1:1}" = "'" ]; then
            out="$out'"
            i=$((i + 2))
            continue
        fi
        out="$out${v:i:1}"
        i=$((i + 1))
    done
    printf '%s' "$out"
}

# dsn_split2 <dsn>：把 DSN 拆成「去口令的 DSN」与「口令」两段输出，
# 用 US（\037）分隔——$( ) 只削尾换行，空口令也不会把整条 DSN 当成口令。
# URI 与 key=value 两种形态都支持；URI 查询串里的 password= 同样摘掉
# （不论有没有 userinfo——没有 userinfo 的 `?password=` 一样是秘密）。
# 解析失败返回 1（调用方失败关闭，绝不把口令塞回 argv）。
dsn_split2() {
    local dsn="$1" prefix rest authority hostpart userpart user pass remainder
    local path_part="" query="" param joined="" has_userinfo=0
    case "$dsn" in
        postgres://*|postgresql://*)
            prefix="${dsn%%://*}"
            rest="${dsn#*://}"
            # authority＝userinfo@host[:port]，终止符是第一个 / **或 ?**：
            # postgres://dbhost?password=... 这种「无路径段」形态的查询串
            # 也必须被解析，否则口令会原样留在 argv 里（复审 H4）。
            authority="${rest%%[/?]*}"
            remainder=""
            if [ "$authority" != "$rest" ]; then
                remainder="${rest#"$authority"}"
            fi
            case "$authority" in
                *@*) has_userinfo=1 ;;
            esac
            hostpart="$authority"
            user=""
            pass=""
            if [ "$has_userinfo" -eq 1 ]; then
                hostpart="${authority##*@}"
                userpart="${authority%@*}"
                user="$userpart"
                case "$userpart" in
                    *:*) user="${userpart%%:*}"; pass="${userpart#*:}" ;;
                esac
                if [ -n "$pass" ]; then
                    pass="$(urldecode "$pass")" || return 1
                fi
            fi
            # 查询串里的 ?password= 同样是秘密：不论有没有 userinfo 都要摘掉。
            # （libpq 的 URI 解析让查询参数覆盖 userinfo，这里同口径。）
            path_part="${remainder%%\?*}"
            case "$remainder" in
                *\?*) query="${remainder#*\?}" ;;
            esac
            if [ -n "$query" ]; then
                local -a kept_params=()
                local IFS='&'
                for param in $query; do
                    case "$param" in
                        password=*)
                            pass="$(urldecode "${param#password=}")" || return 1
                            ;;
                        *) kept_params+=("$param") ;;
                    esac
                done
                unset IFS
                for param in "${kept_params[@]}"; do
                    joined="${joined:+$joined&}$param"
                done
                query="${joined:+?$joined}"
            fi
            if [ "$has_userinfo" -eq 1 ]; then
                printf '%s://%s@%s%s%s' "$prefix" "$user" "$hostpart" "$path_part" "$query"
            else
                printf '%s://%s%s%s' "$prefix" "$hostpart" "$path_part" "$query"
            fi
            printf '\037%s' "$pass"
            return 0
            ;;
    esac
    dsn_kw_fields "$dsn"
    local -a keep_fields=()
    for raw in "${DSN_KW_FIELDS[@]}"; do
        case "$raw" in
            *=*) ;;
            *) return 1 ;;
        esac
        key="${raw%%=*}"
        if [ "$key" = "password" ]; then
            pass="$(dsn_kw_unquote "${raw#*=}")" || return 1
            continue
        fi
        keep_fields+=("$raw")
    done
    [ "${#keep_fields[@]}" -gt 0 ] || return 1
    printf '%s' "${keep_fields[*]}"
    printf '\037%s' "${pass:-}"
}

# dsn_dbname <dsn>：取库名（URI 路径段或 dbname= 值）。取不到返回空。
dsn_dbname() {
    local dsn="$1" rest pathq raw
    case "$dsn" in
        postgres://*|postgresql://*)
            rest="${dsn#*://}"
            case "$rest" in
                */*) pathq="${rest#*/}" ;;
                *) return 0 ;;
            esac
            printf '%s' "${pathq%%\?*}"
            return 0
            ;;
    esac
    dsn_kw_fields "$dsn"
    for raw in "${DSN_KW_FIELDS[@]}"; do
        case "$raw" in
            dbname=*) dsn_kw_unquote "${raw#*=}"; return 0 ;;
        esac
    done
    return 0
}

# pgpass_field <值>：.pgpass 字段转义（反斜杠与冒号前加反斜杠）。
pgpass_field() {
    case "$1" in
        "") printf '*' ;;
        *) printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/:/\\:/g' ;;
    esac
}

# prepare_pgpass：把口令从 DSN 里摘出来写进 0600 的 PGPASSFILE，
# 之后所有 psql/pg_dump/pg_restore 的 argv 只带无口令 DSN。
# 口令为空时不动 DSN（没有可泄漏的东西）。失败关闭：解析不了就报错返回 1。
prepare_pgpass() {
    local split
    [ -n "$DSN_FULL" ] || return 0
    split="$(dsn_split2 "$DSN_FULL")" || {
        echo "update: cannot separate the password from YIBAN_DATABASE_URL without putting it in argv; use a postgres:// URI or a key=value DSN with password=..." >&2
        return 1
    }
    # 分隔符是 US（）：$( ) 只削尾换行，空口令时也不会把整条 DSN 当口令。
    case "$split" in
        *$'\037'*) ;;
        *)
            echo "update: internal: DSN split has no separator; refusing to guess the password" >&2
            return 1
            ;;
    esac
    DSN="${split%%$'\037'*}"
    DSN_PASSWORD="${split#*$'\037'}"
    [ -n "$DSN_PASSWORD" ] || return 0
    case "$DSN_PASSWORD" in
        *$'\n'*|*$'\r'*)
            echo "update: the database password contains a line break; refusing to write a pgpass file" >&2
            return 1
            ;;
    esac
    PGPASS_FILE="$STAGE/pgpass"
    if ! ( umask 077; printf '*:*:*:*:%s\n' "$(pgpass_field "$DSN_PASSWORD")" > "$PGPASS_FILE" ); then
        echo "update: cannot write the pgpass file: $PGPASS_FILE" >&2
        return 1
    fi
    chmod 600 "$PGPASS_FILE" || return 1
    export PGPASSFILE="$PGPASS_FILE"
    return 0
}

# dsn_with_dbname <dsn> <newdb>：把 DSN 的库名换成 newdb。
# 支持 postgres:// URI 与 key=value 两种形态；解析不了就返回非 0
# （宁可失败，也不把备份写进原库）。
dsn_with_dbname() {
    local dsn="$1" db="$2" prefix rest authority pathq query
    case "$dsn" in
        postgres://*|postgresql://*)
            prefix="${dsn%%://*}"
            rest="${dsn#*://}"
            case "$rest" in
                */*) ;;
                *)
                    # 无路径段：query（?sslmode=…）要保住，不能被拼进库名。
                    query=""
                    case "$rest" in
                        *\?*) query="?${rest#*\?}"; rest="${rest%%\?*}" ;;
                    esac
                    printf '%s://%s/%s%s' "$prefix" "$rest" "$db" "$query"
                    return 0
                    ;;
            esac
            authority="${rest%%/*}"
            pathq="${rest#*/}"
            query=""
            case "$pathq" in
                *\?*) query="?${pathq#*\?}" ;;
            esac
            printf '%s://%s/%s%s' "$prefix" "$authority" "$db" "$query"
            return 0
            ;;
    esac
    # key=value 形态：替换 dbname=（含单引号值）；没有就追加。
    case "$dsn" in
        *[![:space:]]*) ;;
        *) return 1 ;;
    esac
    if printf '%s' "$dsn" | grep -qE '(^|[[:space:]])dbname='; then
        printf '%s' "$dsn" | sed -E "s/(^|[[:space:]])dbname=('[^']*'|[^[:space:]]+)/\1dbname=${db}/"
    else
        printf '%s dbname=%s' "$dsn" "$db"
    fi
}

# write_env_key <key> <value>：幂等写一行，不重排其余行；原子换名。
# umask 077 让临时文件从创建那一刻就是 0600：密钥没有可读窗口。
write_env_key() {
    local key="$1" value="$2" tmp="$ENV_FILE.tmp.$$"
    [ -r "$ENV_FILE" ] || return 1
    (
        umask 077
        sed "/^${key}=/d" "$ENV_FILE" > "$tmp" &&
            printf '%s=%s\n' "$key" "$value" >> "$tmp"
    ) || { rm -f -- "$tmp"; return 1; }
    chmod 600 "$tmp" || { rm -f -- "$tmp"; return 1; }
    if [ -z "$ROOT" ]; then
        chown root:root "$tmp" || { rm -f -- "$tmp"; return 1; }
    fi
    mv "$tmp" "$ENV_FILE"
}

# write_ui_env 幂等写 YIBAN_UI_DIR；只在 UI 目录就位之后调用。
write_ui_env() { write_env_key YIBAN_UI_DIR "$SERVICE_HOME/ui"; }

# drop_ui_env 删除 YIBAN_UI_DIR 行；用于首次装页面失败后的回退。
drop_ui_env() {
    [ -r "$ENV_FILE" ] || return 0
    local tmp="$ENV_FILE.tmp.$$"
    ( umask 077; sed '/^YIBAN_UI_DIR=/d' "$ENV_FILE" > "$tmp" ) \
        || { rm -f -- "$tmp"; return 1; }
    chmod 600 "$tmp" || { rm -f -- "$tmp"; return 1; }
    if [ -z "$ROOT" ]; then
        chown root:root "$tmp" || { rm -f -- "$tmp"; return 1; }
    fi
    mv "$tmp" "$ENV_FILE"
}

# ---- 自检门 ----------------------------------------------------------------

# selfcheck_cli 选出自检用的 CLI：优先已安装的（与库同版本，pending 判得准）。
selfcheck_cli() {
    if [ -x "$CLI_DEST" ]; then
        printf '%s' "$CLI_DEST"
        return 0
    fi
    command -v yiban-cli 2>/dev/null || return 1
}

# selfcheck_fail_summary <json>：打印 PASS/FAIL/SKIP 计数与失败项。
# JSON 由 MarshalIndent 固定缩进：name/status/detail 各占一行。
selfcheck_fail_summary() {
    awk '
        /"name":/    { n = $0; sub(/.*"name": "/, "", n); sub(/",[[:space:]]*$/, "", n) }
        /"status":/  { f = ($0 ~ /"FAIL"/) }
        /"detail":/  { if (f) { d = $0; sub(/.*"detail": "/, "", d); sub(/",[[:space:]]*$/, "", d);
                        printf "[FAIL] %-22s %s\n", n, d; f = 0 } }
        /"pass":/    { p = $0; sub(/.*"pass": /, "", p); sub(/,.*$/, "", p) }
        /"fail":/    { x = $0; sub(/.*"fail": /, "", x); sub(/,.*$/, "", x) }
        /"skip":/    { s = $0; sub(/.*"skip": /, "", s); sub(/,.*$/, "", s) }
        END { printf "selfcheck: %s 过 / %s 败 / %s 跳\n", p, x, s }
    ' "$1"
}

# run_selfcheck_gate <report-path>：只读自检门。
# 返回 0 = 全 PASS/SKIP；2 = 有 FAIL、报告不可读或 CLI 不支持 selfcheck。
# 判据是报告内容（FAIL 计数），不是 CLI 退出码：两者不一致时按报告判，
# 报告不可读时 fail-closed（跳过 ≠ 绿）。
run_selfcheck_gate() {
    local report="$1" cli rc=0
    cli="$(selfcheck_cli)" || {
        echo "update: selfcheck FAILED — no yiban-cli available (install.sh installs it)" >&2
        return 2
    }
    # --backup-dir 显式给：判权对象必须是本脚本真正写入的目录，
    # 不能吃 U1 的历史缺省（老机上那是 root 0700 的 pre-migrate，会误红）。
    "$cli" selfcheck --json --backup-dir "$BACKUP_ROOT" >"$report" 2>"$report.err" || rc=$?
    if ! grep -q '"schema_version": 1' "$report" 2>/dev/null; then
        if [ -s "$report.err" ]; then
            sed 's/^/update: selfcheck: /' "$report.err" >&2
        fi
        if [ "$rc" -eq 2 ]; then
            echo "update: selfcheck FAILED — the installed yiban-cli does not accept 'selfcheck' (pre-U1 release?); upgrade to a release with U1 selfcheck first" >&2
        else
            echo "update: selfcheck FAILED — yiban-cli selfcheck exit=$rc and the report is not schema_version=1 JSON; nothing was changed" >&2
        fi
        return 2
    fi
    if grep -q '"status": "FAIL"' "$report"; then
        selfcheck_fail_summary "$report" >&2
        echo "update: selfcheck FAILED — fix the FAIL items above and retry (nothing was changed)" >&2
        return 2
    fi
    selfcheck_fail_summary "$report"
    return 0
}

# ---- 备份 ------------------------------------------------------------------

# prepare_state_dir：建/校验更新器的 root 独占树（父目录 + state + backups，
# 都 0700 root）。备份根在这里建：后续每次更新只建自己的子目录。
prepare_state_dir() {
    # 顺序即安全边界（复审 HIGH-3）：父目录 → 状态目录 → 备份根 → 日志文件，
    # 每一步都复核「不是软链、属主 root、组/其他不可写」。
    # 攻击形状：服务用户把 state 换名成软链指向 /etc/passwd，root 的 >> 追随它。
    local d cur_owner cur_mode
    local -a dirs=()

    if [ -L "$UPDATER_ROOT" ]; then
        echo "update: refusing symlinked updater root: $UPDATER_ROOT" >&2
        return 1
    fi
    [ -d "$UPDATER_ROOT" ] || mkdir -p "$UPDATER_ROOT" || return 1
    [ -d "$STATE_DIR" ] || mkdir -p "$STATE_DIR" || return 1
    [ -d "$BACKUP_ROOT" ] || mkdir -p "$BACKUP_ROOT" || return 1
    dirs=("$UPDATER_ROOT" "$STATE_DIR" "$BACKUP_ROOT")

    for d in "${dirs[@]}"; do
        if [ -L "$d" ]; then
            echo "update: refusing symlinked updater directory: $d" >&2
            return 1
        fi
        chmod 700 "$d" || return 1
        if [ -z "$ROOT" ]; then
            # 干跑不校验属主（非 root 造不出 root 属主）。
            cur_owner="$(stat -c '%U' -- "$d" 2>/dev/null || true)"
            cur_mode="$(stat -c '%a' -- "$d" 2>/dev/null || true)"
            case "$cur_mode" in
                ''|*[!0-7]*) cur_mode=777 ;;
            esac
            if [ "$cur_owner" != "root" ]; then
                echo "update: updater directory must be owned by root: $d (owner: ${cur_owner:-unknown})" >&2
                return 1
            fi
            if [ $(( 8#$cur_mode & 8#022 )) -ne 0 ]; then
                echo "update: updater directory is writable by group or others (mode $cur_mode): $d" >&2
                return 1
            fi
            chown root:root "$d" || return 1
        fi
    done

    # 日志是 root 追加写的普通文件；软链一律拒绝（否则会写到别处）。
    if [ -L "$LOG_FILE" ] || [ -L "$STATE_FILE" ]; then
        echo "update: refusing symlinked state file under $STATE_DIR" >&2
        return 1
    fi
    ( umask 077; : >> "$LOG_FILE" ) || return 1
    chmod 600 "$LOG_FILE" || return 1
    # 只有到这里（目录与文件都验过）才允许 log_event 写盘。
    STATE_READY=1
}

# verify_root_artifact <path> <描述>：回滚会执行/导入的文件必须是 root 属主、
# 组与其他不可写、且不是符号链接。备份根虽已 root 0700，这里再钉一层：
# 一旦有人放开权限或塞进软链，root 执行备份里的 CLI 就等于执行别人的代码。
verify_root_artifact() {
    local path="$1" what="$2" owner mode
    if [ -L "$path" ]; then
        echo "update: refusing symlinked $what: $path" >&2
        return 1
    fi
    if [ ! -e "$path" ]; then
        echo "update: $what missing: $path" >&2
        return 1
    fi
    owner="$(stat -c '%U' -- "$path" 2>/dev/null || true)"
    mode="$(stat -c '%a' -- "$path" 2>/dev/null || true)"
    case "$mode" in
        ''|*[!0-7]*) mode=777 ;;
    esac
    # 干跑（YIBAN_INSTALL_ROOT）：整棵树由调用者造，属主只可能是调用者本人。
    local want_owner="root"
    if [ -n "$ROOT" ]; then
        want_owner="$(id -un 2>/dev/null || printf 'root')"
    fi
    if [ "$owner" != "$want_owner" ]; then
        echo "update: $what must be owned by root: $path (owner: ${owner:-unknown})" >&2
        return 1
    fi
    if [ $(( 8#$mode & 8#022 )) -ne 0 ]; then
        echo "update: $what is writable by group or others (mode $mode): $path" >&2
        return 1
    fi
    return 0
}

# backup_sha256 <相对文件名...>：写 SHA256SUMS 并立即校验。
backup_sha256() {
    (
        cd "$BACKUP_SET" || exit 1
        umask 077
        sha256sum -- "$@" > SHA256SUMS || exit 1
        sha256sum -c --quiet SHA256SUMS || exit 1
    )
}

# do_backup：统一备份。失败返回非 0，调用方中止升级（不停服）。
# 所有写入都在 0700 目录、0600 文件；照片不进数据库备份、数据库备份不
# 进照片目录。
do_backup() {
    local photo_dir files=() tar_entries

    UPDATE_ID="$(date -u +%Y%m%dT%H%M%SZ)"
    BACKUP_SET="$BACKUP_ROOT/$UPDATE_ID"
    # 同一秒内的重跑（脚本很快、或上一次刚失败）不得覆盖已有的备份目录。
    # 后缀用 `_N`：字典序排在 `Z` 之后，保留策略仍按时间从新到旧。
    local n=1
    while [ -e "$BACKUP_SET" ]; do
        n=$((n + 1))
        BACKUP_SET="$BACKUP_ROOT/${UPDATE_ID}_$n"
    done
    UPDATE_ID="$(basename -- "$BACKUP_SET")"

    # 备份根由 prepare_state_dir 建好（root:root 0700，服务用户不可写）。
    # 这里只复核形状：符号链接或被换掉就直接失败关闭。
    if [ -L "$BACKUP_ROOT" ] || [ ! -d "$BACKUP_ROOT" ]; then
        echo "update: backup FAILED — backup root is not a directory: $BACKUP_ROOT" >&2
        return 1
    fi
    if ! mkdir "$BACKUP_SET"; then
        echo "update: backup FAILED — backup directory already exists: $BACKUP_SET" >&2
        return 1
    fi
    chmod 700 "$BACKUP_SET" || return 1
    if [ -z "$ROOT" ]; then
        chown root:root "$BACKUP_SET" || return 1
    fi
    note "backup: $BACKUP_SET"

    if [ -z "$DSN" ]; then
        echo "update: backup FAILED — YIBAN_DATABASE_URL is empty; refusing to update without a database backup" >&2
        return 1
    fi
    command -v pg_dump >/dev/null 2>&1 \
        || { echo "update: backup FAILED — pg_dump not found" >&2; return 1; }
    command -v pg_restore >/dev/null 2>&1 \
        || { echo "update: backup FAILED — pg_restore not found (needed to verify the dump)" >&2; return 1; }

    # 迁移前基线：goose 版本与 public 基表数（恢复后对账用）。
    PRE_GOOSE_VERSION="$(schema_version_of "$DSN" || true)"
    PRE_TABLE_COUNT="$(table_count_of "$DSN" || true)"
    (
        umask 077
        printf 'database=%s\ngoose_version=%s\npublic_base_tables=%s\n' \
            "$DSN_NAME" "$PRE_GOOSE_VERSION" "$PRE_TABLE_COUNT" \
            > "$BACKUP_SET/db-schema.txt"
    ) || return 1
    files+=("db-schema.txt")

    # .env 快照：数据库回滚会改写 YIBAN_DATABASE_URL，原文必须留一份
    # （0600，与其余备份文件同权限），供人工对照/回退。里面有密钥与口令，
    # 只进 root 专属目录，不进日志。
    if [ -r "$ENV_FILE" ]; then
        ( umask 077; install -m 0600 "$ENV_FILE" "$BACKUP_SET/env.backup" ) || return 1
        files+=("env.backup")
    else
        warn "cannot read $ENV_FILE; env snapshot SKIPPED"
    fi

    # pg_dump -Fc：可 pg_restore 直接恢复到新库；umask 077 让转储从创建
    # 那一刻就是 0600（含凭据密文与审计行）。
    if ! ( umask 077 && pg_dump --format=custom --no-owner --no-privileges \
            --file="$BACKUP_SET/db.dump" "$DSN" ); then
        echo "update: backup FAILED — pg_dump failed" >&2
        return 1
    fi
    if ! ( umask 077; pg_restore --list "$BACKUP_SET/db.dump" > "$BACKUP_SET/db.list" ) \
            2>"$BACKUP_SET/db.list.err"; then
        echo "update: backup FAILED — pg_restore --list rejected the dump" >&2
        return 1
    fi
    if [ ! -s "$BACKUP_SET/db.list" ]; then
        echo "update: backup FAILED — pg_restore --list produced an empty table of contents" >&2
        return 1
    fi
    rm -f -- "$BACKUP_SET/db.list.err"
    files+=("db.dump" "db.list")
    note "backup: database dump verified ($(wc -l < "$BACKUP_SET/db.list" | tr -d ' ') TOC entries, $(wc -c < "$BACKUP_SET/db.dump" | tr -d ' ') bytes)"

    # 可选：yiban-cli backup 产出 GPG 对称加密归档（含行数 manifest）。
    # 口令来源与 CLI 同源：--backup-passphrase-file > YIBAN_BACKUP_PASSPHRASE。
    if [ -n "$BACKUP_PASS_FILE" ] || [ -n "${YIBAN_BACKUP_PASSPHRASE:-}" ]; then
        local cli_for_backup
        local -a backup_cmd
        cli_for_backup="$CLI_DEST"
        [ -x "$cli_for_backup" ] || cli_for_backup="$(command -v yiban-cli || true)"
        if [ -z "$cli_for_backup" ]; then
            echo "update: backup FAILED — no yiban-cli to produce the GPG archive" >&2
            return 1
        fi
        # DSN 走进程环境（YIBAN_DATABASE_URL），不进 argv：CLI 内部用自己的
        # PGPASSFILE 交给 pg_dump。
        backup_cmd=("$cli_for_backup" backup --out "$BACKUP_SET/db.sql.gpg")
        if [ -n "$BACKUP_PASS_FILE" ]; then
            backup_cmd+=(--passphrase-file "$BACKUP_PASS_FILE")
        fi
        if ! ( umask 077; "${backup_cmd[@]}" > "$BACKUP_SET/db-counts.txt" 2>&1 ); then
            echo "update: backup FAILED — yiban-cli backup (GPG) failed; see $BACKUP_SET/db-counts.txt" >&2
            return 1
        fi
        files+=("db.sql.gpg" "db-counts.txt")
        note "backup: GPG archive db.sql.gpg (yiban-cli backup; row counts in db-counts.txt)"
    else
        warn "no backup passphrase (--backup-passphrase-file or YIBAN_BACKUP_PASSPHRASE); GPG archive SKIPPED (plain -Fc dump only)"
    fi

    # 照片快照：不跟随软链（默认 tar 行为）。
    # 照片目录**本身**若是软链（挂载点常见），先 readlink -f 解析成真实目录再打包；
    # 目录**内部**的软链只存链接本身——否则 tar 会顺着服务用户能造的链接，
    # 把软链指向的 root-only 文件读进备份（复审建议）。
    # 目录不存在 → 显性 WARN 跳过（首次运行会创建）。
    photo_dir="${YIBAN_PHOTO_DIR:-$(dest "$SERVICE_HOME/photos")}"
    case "$photo_dir" in
        ""|"/")
            echo "update: backup FAILED — YIBAN_PHOTO_DIR is $(printf '%s' "${YIBAN_PHOTO_DIR:-<empty>}")" >&2
            return 1
            ;;
    esac
    if [ ! -d "$photo_dir" ]; then
        warn "photo dir does not exist: $photo_dir; photos snapshot SKIPPED"
    else
        # 无条件 readlink -f：既解开目录本身的软链（含 `YIBAN_PHOTO_DIR/`
        # 尾斜杠形态），也归一化 `..` 与重复斜杠——后面的判据都看解析结果。
        photo_src="$(readlink -f -- "$photo_dir" 2>/dev/null || true)"
        if [ -z "$photo_src" ] || [ ! -d "$photo_src" ]; then
            echo "update: backup FAILED — cannot resolve the photo dir: $photo_dir" >&2
            return 1
        fi
        # 解析目标必须是一棵「照片树」，不能是系统目录或更新器自己的目录：
        # 否则 root 会把 /etc、/root、状态/备份根里的 root-only 文件 tar 进备份，
        # 而备份里的 0600 挡不住 tar -xOf（复审 M2）。
        case "$photo_src" in
            /|"$UPDATER_ROOT"|"$UPDATER_ROOT"/*|"$STATE_DIR"|"$STATE_DIR"/*|"$BACKUP_ROOT"|"$BACKUP_ROOT"/*|/etc|/etc/*|/root|/root/*|/usr|/usr/*|/boot|/boot/*|/dev|/dev/*|/proc|/proc/*|/sys|/sys/*|/run|/run/*|/var/log|/var/log/*|/var/lib/yiban-updater|/var/lib/yiban-updater/*)
                echo "update: backup FAILED — photo dir resolves to a system/updater directory: $photo_dir -> $photo_src" >&2
                return 1
                ;;
        esac
        if ! ( umask 077; tar -C "$(dirname -- "$photo_src")" \
                -cf "$BACKUP_SET/photos.tar" -- "$(basename -- "$photo_src")" ); then
            echo "update: backup FAILED — tar of photo dir failed: $photo_dir" >&2
            return 1
        fi
        if ! tar_entries="$(tar -tf "$BACKUP_SET/photos.tar" 2>/dev/null)"; then
            echo "update: backup FAILED — photos.tar is not readable" >&2
            return 1
        fi
        files+=("photos.tar")
        note "backup: photos.tar ($(printf '%s\n' "$tar_entries" | wc -l | tr -d ' ') entries, $(wc -c < "$BACKUP_SET/photos.tar" | tr -d ' ') bytes)"
    fi

    # 当前二进制归档：本次更新前的可回滚件（含仍存在的旧 .bak）。
    if ! mkdir -p "$BACKUP_SET/bin" || ! chmod 700 "$BACKUP_SET/bin"; then
        echo "update: backup FAILED — cannot create $BACKUP_SET/bin" >&2
        return 1
    fi
    if [ -x "$SERVER_DEST" ]; then
        install -m 0700 "$SERVER_DEST" "$BACKUP_SET/bin/yiban-server" || return 1
        files+=("bin/yiban-server")
    fi
    if [ -x "$CLI_DEST" ]; then
        install -m 0700 "$CLI_DEST" "$BACKUP_SET/bin/yiban-cli" || return 1
        files+=("bin/yiban-cli")
    fi
    if [ -e "$BAK" ]; then
        install -m 0700 "$BAK" "$BACKUP_SET/bin/yiban-server.rollback-point" || return 1
        files+=("bin/yiban-server.rollback-point")
        note "backup: previous .bak archived as bin/yiban-server.rollback-point"
    fi

    if ! backup_sha256 "${files[@]}"; then
        echo "update: backup FAILED — sha256 manifest did not verify" >&2
        return 1
    fi
    DB_BACKUP_VERIFIED=1
    note "backup: SHA256SUMS verified (${#files[@]} files)"
    log_event "backup ok dir=$BACKUP_SET schema=$PRE_GOOSE_VERSION tables=$PRE_TABLE_COUNT"

    prune_backups
    return 0
}

# prune_backups：只删本备份根下形如 <UTCts> 的子目录，保留最近 N 份。
# 不删当前这份；不删任何不匹配命名的东西。
prune_backups() {
    local keep="$KEEP_BACKUPS" dir count=0
    [ "$keep" -gt 0 ] || return 0
    [ -d "$BACKUP_ROOT" ] || return 0
    while IFS= read -r dir; do
        [ -n "$dir" ] || continue
        dir="${dir%/}"
        case "$(basename -- "$dir")" in
            [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z) ;;
            [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z_[0-9]*) ;;
            *) continue ;;
        esac
        count=$((count + 1))
        [ "$count" -gt "$keep" ] || continue
        [ "$dir" = "$BACKUP_SET" ] && continue
        case "$dir" in
            "$BACKUP_ROOT"/*) ;;
            *) continue ;;
        esac
        if rm -rf -- "$dir"; then
            note "backup: pruned $dir (keep=$keep)"
        else
            warn "cannot prune old backup $dir"
        fi
    done < <(ls -1d "$BACKUP_ROOT"/*/ 2>/dev/null | sort -r)
    return 0
}

# ---- 数据库恢复 ------------------------------------------------------------

# db_changed：迁移有没有改变库。读不到版本时按「已改变」保守处理。
db_changed() {
    [ -n "$PRE_GOOSE_VERSION" ] || return 0
    [ -n "$POST_GOOSE_VERSION" ] || return 0
    [ "$PRE_GOOSE_VERSION" != "$POST_GOOSE_VERSION" ]
}

# restore_database：把 pre-migrate 备份恢复到一个新库并切换 DSN。
# 只在停服后、备份通过校验时调用。原库不删不改（取证）。
# 成功返回 0 并设置 DSN/DB_RESTORED；失败返回非 0（调用方打印 manual action）。
restore_database() {
    local dump="$BACKUP_SET/db.dump" target new_dsn new_dsn_argv new_split got err_file filtered
    local want_ver="$PRE_GOOSE_VERSION" want_tables="$PRE_TABLE_COUNT"

    [ "$DB_BACKUP_VERIFIED" -eq 1 ] || return 1
    verify_root_artifact "$BACKUP_SET" "backup directory" || return 1
    verify_root_artifact "$dump" "backup dump" || return 1
    [ -n "$DSN" ] || return 1
    command -v pg_restore >/dev/null 2>&1 || return 1
    # 没有基线就没法证明恢复件与备份一致：宁可失败，也不宣布一个没对过账的恢复。
    if [ -z "$want_ver" ] || [ -z "$want_tables" ]; then
        echo "update: db restore: no schema baseline in $BACKUP_SET/db-schema.txt; refusing to restore" >&2
        return 1
    fi

    target="yiban_restore_$(date -u +%Y%m%d%H%M%S)"
    # 建库之前就记下目标名：CREATE DATABASE 成功而后续校验失败时，
    # 状态文件与 manual action 必须给出真名（否则人工会撞名或找不到库）。
    DB_RESTORE_TARGET="$target"
    err_file="$STATE_DIR/db-restore.log"
    filtered="$STATE_DIR/db-restore.filtered"

    note "db restore: creating new database $target (the failed database is kept as is)"
    if ! ( umask 077; psql "$DSN" -X -q -v ON_ERROR_STOP=1 \
        -c "CREATE DATABASE \"$target\"" >/dev/null 2>"$err_file" ); then
        sed 's/^/update: db restore: /' "$err_file" >&2 || true
        return 1
    fi

    # 含口令原文只用来写回 .env；argv 用去口令形态 + PGPASSFILE。
    new_dsn="$(dsn_with_dbname "$DSN_FULL" "$target")" || {
        echo "update: db restore: cannot rewrite the DSN to point at $target" >&2
        return 1
    }
    new_split="$(dsn_split2 "$new_dsn")" || {
        echo "update: db restore: cannot separate the password from the new DSN" >&2
        return 1
    }
    new_dsn_argv="${new_split%%$'\037'*}"
    # 关键守卫：restore 之前先证明新 DSN 真的连到新库。
    # 解析出错时 new_dsn 可能仍指向原库，直接 pg_restore 会覆盖生产库。
    got="$(psql_q "$new_dsn_argv" "SELECT current_database()" || true)"
    if [ "$got" != "$target" ]; then
        echo "update: db restore: new DSN resolves to '$got' instead of '$target'; refusing to restore" >&2
        return 1
    fi

    # pg_restore 到空库。public schema 已存在是已知良性错误（转储自带
    # CREATE SCHEMA public）：过滤它，其余任何错误都算恢复失败。
    ( umask 077; pg_restore --no-owner --no-privileges --dbname="$new_dsn_argv" "$dump" \
        >/dev/null 2>"$err_file" ) || true
    if [ -s "$err_file" ]; then
        grep -v -e 'schema "public" already exists' \
                -e 'Command was: CREATE SCHEMA public' \
                -e '^pg_restore: warning:' \
                -e '^pg_restore: error: errors ignored on restore:' \
                "$err_file" >"$filtered" || true
        if [ -s "$filtered" ]; then
            sed 's/^/update: db restore: /' "$filtered" >&2 || true
            return 1
        fi
        rm -f -- "$filtered"
    fi

    # 对账：goose 版本与 public 基表数必须与备份基线一致。
    got="$(schema_version_of "$new_dsn_argv" || true)"
    if [ -n "$want_ver" ] && [ "$got" != "$want_ver" ]; then
        echo "update: db restore: goose version $got does not match backup baseline $want_ver" >&2
        return 1
    fi
    got="$(table_count_of "$new_dsn_argv" || true)"
    if [ -n "$want_tables" ] && [ "$got" != "$want_tables" ]; then
        echo "update: db restore: public base tables $got do not match backup baseline $want_tables" >&2
        return 1
    fi

    # 只有全部校验通过才切指针：原子改写 .env，systemd 重启后生效。
    if ! write_env_key YIBAN_DATABASE_URL "$new_dsn"; then
        echo "update: db restore: cannot rewrite YIBAN_DATABASE_URL in $ENV_FILE" >&2
        return 1
    fi
    DSN_FULL="$new_dsn"
    DSN="$new_dsn_argv"
    DSN_NAME_AFTER="$target"
    DB_RESTORED=1
    DB_RESTORE_TARGET="$target"
    export YIBAN_DATABASE_URL="$new_dsn"
    note "db restore: serving from $target (schema ${want_ver:-unknown}); previous database kept for forensics"
    return 0
}

# db_restore_wanted：是否应该尝试数据库恢复。
db_restore_wanted() {
    [ -z "$NO_RESTORE_DB" ] || return 1
    [ "$DB_BACKUP_VERIFIED" -eq 1 ] || return 1
    [ -n "$DSN" ] || return 1
    db_changed
}

# ---- 回滚 ------------------------------------------------------------------

# rollback_ui：撤掉本轮装上的页面，只留旧件。
# 动过 UI 的两种形态都要回滚：
#   - 换过旧目录（UI_OLD_MOVED=1）：删新目录、把 ui.bak 换回；
#   - 首次安装（UI_OLD_MOVED=0、UI_ACTIVATED=1）：删新目录、去掉本轮写的
#     YIBAN_UI_DIR（旧二进制恢复「无页面」形态）。
# 两者都没发生过（UI 没动过）直接返回：不能按变量盲删真实目录。
rollback_ui() {
    [ -n "$UI_DIR" ] || return 0
    if [ "$UI_OLD_MOVED" -eq 0 ] && [ "$UI_ACTIVATED" -eq 0 ]; then
        return 0
    fi
    case "$UI_DEST" in
        */var/lib/yiban/ui) rm -rf -- "$UI_DEST" ;;
        *) echo "update: refusing to remove unexpected ui path: $UI_DEST" >&2; return 1 ;;
    esac
    if [ -e "$UI_BAK" ]; then
        mv -T "$UI_BAK" "$UI_DEST" || return 1
    else
        # 首次装页面就回滚：没有旧页面可留，撤掉本轮写的指针。
        if ! drop_ui_env; then
            echo "update: cannot clear YIBAN_UI_DIR; manual fix needed" >&2
        fi
    fi
    UI_OLD_MOVED=0
    UI_ACTIVATED=0
    return 0
}

# fail_after_stop <原因>：失败回滚总入口。停服 → 换回旧件 → 恢复数据库
# （按需）→ 起旧件 → 探活。从不假装成功：恢复失败 exit 2 + manual action。
fail_after_stop() {
    local reason="$1"
    PHASE="rollback"
    log_event "rollback start: $reason"

    # 1) 停服：回滚文件只在服务停住后落位（旧进程持有新二进制）。
    if ! "$SYSTEMCTL" stop "$UNIT" >/dev/null 2>&1; then
        warn "systemctl stop reported an error during rollback; continuing"
    fi

    # 2) 二进制：只有真正换过才回滚，避免把 .bak 覆盖成同一份文件。
    if [ "$BINARY_SWAPPED" -eq 1 ] && [ -e "$BAK" ]; then
        if ! mv -T "$BAK" "$SERVER_DEST"; then
            echo "update: cannot restore the previous binary from $BAK" >&2
            print_manual_action "binary rollback failed"
            write_state "failed" 2
            exit 2
        fi
        BINARY_SWAPPED=0
    fi
    # CLI 回滚失败不在这里停机：DB 恢复、UI 回滚、起旧件都要继续走完，
    # 最后统一按「回滚是否完整」定退出码（复审 MEDIUM-1）。
    if [ "$CLI_SWAPPED" -eq 1 ] && [ ! -f "$BACKUP_SET/bin/yiban-cli" ]; then
        # 备份里没有 CLI：不能把新 CLI 留在安装位（版本不配对）。
        echo "update: the backup has no yiban-cli; the new cli stays in place" >&2
        CLI_ROLLBACK_FAILED=1
    fi
    if [ "$CLI_SWAPPED" -eq 1 ] && [ -f "$BACKUP_SET/bin/yiban-cli" ]; then
        if verify_root_artifact "$BACKUP_SET/bin/yiban-cli" "backup cli"; then
            if install -m 0755 "$BACKUP_SET/bin/yiban-cli" "$CLI_DEST"; then
                CLI_SWAPPED=0
            else
                warn "cannot restore the previous yiban-cli from $BACKUP_SET/bin/yiban-cli"
                CLI_ROLLBACK_FAILED=1
            fi
        else
            echo "update: refusing to install the backup cli from an untrusted path" >&2
            CLI_ROLLBACK_FAILED=1
        fi
    fi
    if ! rollback_ui; then
        echo "update: cannot restore the previous ui from $UI_BAK" >&2
        print_manual_action "ui rollback failed"
        write_state "failed" 2
        exit 2
    fi

    # 3) 数据库：仅在库确实变了且备份已验证时。恢复失败即停住。
    POST_GOOSE_VERSION="$(schema_version_of "$DSN" || true)"
    if db_restore_wanted; then
        if ! restore_database; then
            DB_RESTORE_FAILED=1
            echo "update: db restore FAILED; the old binary was NOT started (database state unknown)" >&2
            print_manual_action "database restore failed"
            write_state "failed" 2
            exit 2
        fi
    elif [ -z "$NO_RESTORE_DB" ]; then
        note "db restore: not needed (schema ${PRE_GOOSE_VERSION:-unknown} -> ${POST_GOOSE_VERSION:-unknown})"
    fi

    # 4) 起旧件并探活。
    "$SYSTEMCTL" start "$UNIT" >/dev/null 2>&1 || true
    if probe; then
        if [ "$CLI_ROLLBACK_FAILED" -eq 1 ]; then
            # 旧二进制在跑，但 CLI 仍是本轮新件：版本不配对，必须人工处置。
            echo "update: the backup cli was NOT restored (untrusted); the old binary is serving" >&2
            print_manual_action "backup cli rollback refused"
            write_state "failed" 2
            exit 2
        fi
        echo "update: rolled back ($reason); old binary is serving again" >&2
        log_event "rollback ok: $reason"
        write_state "failed" 1
        exit 1
    fi
    echo "update: rollback did not restore health; manual action required" >&2
    print_manual_action "rollback did not restore health"
    write_state "failed" 2
    exit 2
}

# probe：对 /healthz 做有界重试探活（与 install.sh 同源）。
probe() {
    local i=1
    while [ "$i" -le "$RETRIES" ]; do
        if "$CLI_DEST" healthcheck --addr "$ADDR" --timeout 2s; then
            return 0
        fi
        sleep "$INTERVAL"
        i=$((i + 1))
    done
    return 1
}

# ---- 旧布局保护 ------------------------------------------------------------
# 只有旧名 /etc/yiban/.env、没有 server.env 时拒绝升级（含 --selfcheck-only）。
# 此时静默生成 server.env 会产生**新的** YIBAN_HASH_PEPPER / YIBAN_CREDENTIAL_KEY：
# 旧钥加密的凭据将永远无法解密（数据丢失）。fail-closed 并给出迁移步骤；
# 密钥只复制、不重生成，老部署迁移后照常启动。
if [ -f "$(dest /etc/yiban/.env)" ] && [ ! -f "$ENV_FILE" ]; then
    echo "update: found legacy /etc/yiban/.env without $ENV_FILE" >&2
    echo "update: migrate the old runtime file first (secrets are preserved, NOT regenerated):" >&2
    echo "update:   sudo install -m 0600 -o root -g root /etc/yiban/.env /etc/yiban/server.env" >&2
    echo "update:   sudo rm -f /etc/yiban/.env" >&2
    echo "update:   then re-run update.sh" >&2
    echo "update: refusing: generating a fresh server.env here would create NEW pepper/credential keys" >&2
    echo "update:          and make existing credentials permanently undecryptable" >&2
    exit 2
fi

# ---- 只读自检模式 ----------------------------------------------------------
# 不取锁、不建状态目录、不写任何文件：只跑 U1 自检并打印摘要。
# 退出码：0 全 PASS/SKIP；2 有 FAIL 或自检不可用（与预检门同一口径）。
if [ -n "$SELFCHECK_ONLY" ]; then
    load_env_file "$ENV_FILE"
    SELFCHECK_REPORT="$(mktemp "${TMPDIR:-/tmp}/yiban-selfcheck.XXXXXX")" || {
        echo "update: cannot create a temporary report file" >&2
        exit 2
    }
    trap 'rm -f -- "$SELFCHECK_REPORT" "$SELFCHECK_REPORT.err"' EXIT
    if run_selfcheck_gate "$SELFCHECK_REPORT"; then
        rm -f -- "$SELFCHECK_REPORT" "$SELFCHECK_REPORT.err"
        exit 0
    fi
    rm -f -- "$SELFCHECK_REPORT" "$SELFCHECK_REPORT.err"
    exit 2
fi

# ---- 升级参数校验（既有语义保留） ------------------------------------------

# 三件同给同不给：半给是操作员笔误，静默跳过校验等于没校验。
if [ -n "$MANIFEST" ] || [ -n "$SIGNATURE" ] || [ -n "$PUBKEY" ]; then
    if [ -z "$MANIFEST" ] || [ -z "$SIGNATURE" ] || [ -z "$PUBKEY" ]; then
        echo "update: --manifest/--signature/--pubkey must be given together (got manifest=${MANIFEST:+yes} signature=${SIGNATURE:+yes} pubkey=${PUBKEY:+yes})" >&2
        exit 2
    fi
fi
# --allow-unsigned 与三件互斥（E-3）：同给是矛盾用法——要么走验签，
# 要么显式跳过，操作员必须二选一。
if [ -n "$ALLOW_UNSIGNED" ] && { [ -n "$MANIFEST" ] || [ -n "$SIGNATURE" ] || [ -n "$PUBKEY" ]; }; then
    echo "update: --allow-unsigned conflicts with --manifest/--signature/--pubkey; give the three signing inputs or --allow-unsigned, not both" >&2
    exit 2
fi
# 默认强制（E-3）：未给全三件且未显式给 --allow-unsigned → 拒绝升级。
# 用法错误（exit 2），发生在任何锁、暂存、文件替换与停服务之前。
# 逃生口必须显式：静默跳过验签等于没验签（跳过 ≠ 绿）。
if [ -z "$ALLOW_UNSIGNED" ] && { [ -z "$MANIFEST" ] || [ -z "$SIGNATURE" ] || [ -z "$PUBKEY" ]; }; then
    echo "update: refusing unsigned update: give --manifest/--signature/--pubkey (all three) to verify the release, or --allow-unsigned to skip verification explicitly" >&2
    exit 2
fi
# 版本期望只有在验签路径里才有意义；单独给是用法错误。
if [ -n "$EXPECT_VERSION" ] && [ -z "$MANIFEST" ]; then
    echo "update: --expect-version requires --manifest/--signature/--pubkey" >&2
    exit 2
fi

[ -n "$SERVER_BIN" ] || { echo "update: --server-bin is required" >&2; exit 2; }
[ -x "$SERVER_BIN" ] || { echo "update: new server binary missing: $SERVER_BIN" >&2; exit 2; }
# 存在性检查前移（P3）：否则签名模式下缺 --cli-bin 会被归类成验签失败（1）。
if [ -n "$CLI_BIN" ] && [ ! -x "$CLI_BIN" ]; then
    echo "update: new cli binary missing: $CLI_BIN" >&2
    exit 2
fi
if [ -n "$UI_DIR" ]; then
    [ -d "$UI_DIR" ] || { echo "update: --ui-dir is not a directory: $UI_DIR" >&2; exit 2; }
    [ -f "$UI_DIR/index.html" ] \
        || { echo "update: --ui-dir has no index.html: $UI_DIR" >&2; exit 2; }
fi
ENV_EXAMPLE="${ENV_EXAMPLE:-$REPO_ROOT/deploy/baremetal/server.env.example}"
[ -f "$ENV_EXAMPLE" ] || { echo "update: server.env template missing: $ENV_EXAMPLE" >&2; exit 2; }

[ -x "$SERVER_DEST" ] || { echo "update: no installed server at $SERVER_DEST; run install.sh first" >&2; exit 2; }

# 并发互斥（P3）：两次并发共用 .new/.bak，交错写会覆盖回滚件。
LOCK_FILE="$SERVER_DEST.lock"
if command -v flock >/dev/null 2>&1; then
    exec 9>"$LOCK_FILE"
    if ! flock -n 9; then
        echo "update: another update is running (lock: $LOCK_FILE); run one update at a time" >&2
        exit 2
    fi
else
    echo "update: WARNING: flock not available; concurrent updates are not prevented" >&2
fi

# 输入快照（P2）：先复制，后校验。任何哈希、验签与安装都只读快照。
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/yiban-update.XXXXXX")" || {
    echo "update: cannot create private staging directory" >&2
    exit 2
}
chmod 0700 "$STAGE" || {
    echo "update: cannot secure staging directory: $STAGE" >&2
    exit 2
}
# 失败路径不留暂存件：快照目录、$SERVER_NEW、$CLI_DEST.new、UI 暂存目录与旧的 .new 一并删除。
# 另外把「已写 running、但没走到终态」的状态文件收口成 interrupted：
# 栈外退出（set -e、信号、意外早退）不得让状态文件长期停在 running。
cleanup() {
    local rc=$?
    rm -f -- "${PGPASS_FILE:-}" 2>/dev/null || true
    rm -rf -- "$STAGE" 2>/dev/null || true
    rm -f -- "$SERVER_NEW" 2>/dev/null || true
    rm -f -- "$CLI_DEST.new" 2>/dev/null || true
    rm -rf -- "${UI_STAGE:-}" 2>/dev/null || true
    rm -rf -- "$UI_DEST.new" 2>/dev/null || true
    # 换名两步之间被打断：把 ui.bak 换回，别留「无页面目录＋指针」。
    if [ -n "${UI_BAK:-}" ] && [ -e "$UI_BAK" ] && [ ! -e "$UI_DEST" ]; then
        mv -T "$UI_BAK" "$UI_DEST" 2>/dev/null || true
    fi
    if [ -n "${STATE_FILE:-}" ] && [ -f "$STATE_FILE" ] \
        && grep -q '"result": "running"' "$STATE_FILE" 2>/dev/null; then
        PHASE="${PHASE}+interrupted"
        write_state "interrupted" "$rc" || true
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

SNAP_SERVER="$STAGE/yiban-server"
install -m 0755 "$SERVER_BIN" "$SNAP_SERVER" || {
    echo "update: cannot stage server binary: $SERVER_BIN" >&2
    exit 2
}
SNAP_CLI=""
if [ -n "$CLI_BIN" ]; then
    SNAP_CLI="$STAGE/yiban-cli"
    install -m 0755 "$CLI_BIN" "$SNAP_CLI" || {
        echo "update: cannot stage cli binary: $CLI_BIN" >&2
        exit 2
    }
fi
SNAP_MANIFEST=""
SNAP_SIG=""
SNAP_PUB=""
if [ -n "$MANIFEST" ]; then
    SNAP_MANIFEST="$STAGE/manifest.json"
    SNAP_SIG="$STAGE/manifest.sig"
    SNAP_PUB="$STAGE/release-pubkey.pem"
    if ! install -m 0600 "$MANIFEST" "$SNAP_MANIFEST" \
        || ! install -m 0600 "$SIGNATURE" "$SNAP_SIG" \
        || ! install -m 0600 "$PUBKEY" "$SNAP_PUB"; then
        echo "update: signing verification FAILED (step: signing) — cannot stage release inputs" >&2
        exit 1
    fi
fi

# 签名发布物校验（可选）。放在任何文件替换之前：失败即退出，旧件原样。
VERIFY_SH="$REPO_ROOT/scripts/release/verify.sh"
if [ -n "$MANIFEST" ]; then
    # 1) 信任锚：公钥指纹必须与钉住值一致。缺锚或不符一律 exit 2，
    #    绝不能把「操作员随手给的一把钥」当成发布方的钥。
    anchor_fail() {
        echo "update: signing verification FAILED (step: trust-anchor) — $1" >&2
        exit 2
    }
    PUBKEY_PIN=""
    PIN_SOURCE=""
    if [ -n "${YIBAN_RELEASE_PUBKEY_SHA256:-}" ]; then
        PUBKEY_PIN="$YIBAN_RELEASE_PUBKEY_SHA256"
        PIN_SOURCE="env YIBAN_RELEASE_PUBKEY_SHA256"
    else
        PIN_FILE="$(dest /release-pubkey.sha256)"
        if [ -f "$PIN_FILE" ]; then
            # 锚文件必须 root 持有且组/其他不可写（P3）：否则改一字节就能换锚。
            # 干跑（有安装根前缀）下不校验属主：非 root 无法构造 root 属主文件。
            PIN_STAT="$(stat -L -c '%U %a' -- "$PIN_FILE" 2>/dev/null)" || anchor_fail \
                "cannot stat anchor file: $PIN_FILE"
            PIN_OWNER="${PIN_STAT%% *}"
            PIN_MODE="${PIN_STAT##* }"
            if [ -z "$ROOT" ] && [ "$PIN_OWNER" != "root" ]; then
                anchor_fail "anchor file must be owned by root: $PIN_FILE (owner: $PIN_OWNER)"
            fi
            PIN_GRP="${PIN_MODE: -2:1}"
            PIN_OTH="${PIN_MODE: -1}"
            if [ $(( PIN_GRP & 2 )) -ne 0 ] || [ $(( PIN_OTH & 2 )) -ne 0 ]; then
                anchor_fail "anchor file is writable by group or others (mode $PIN_MODE): $PIN_FILE"
            fi
            PUBKEY_PIN="$(head -n1 "$PIN_FILE" 2>/dev/null || true)"
            PIN_SOURCE="$PIN_FILE"
        fi
    fi
    [ -n "$PUBKEY_PIN" ] || anchor_fail \
        "no pinned public key fingerprint; set YIBAN_RELEASE_PUBKEY_SHA256 or write $(dest /release-pubkey.sha256)"
    # sha256sum 输出形如 `<hex>  <file>`；只取第一个字段，容忍 CRLF 与大写。
    PUBKEY_PIN="$(printf '%s' "$PUBKEY_PIN" | tr -d '\r' | tr 'A-F' 'a-f' | awk '{print $1}')"
    case "$PUBKEY_PIN" in
        ''|*[!0-9a-f]*) anchor_fail "pinned fingerprint is not lowercase hex (source: $PIN_SOURCE)" ;;
    esac
    [ "${#PUBKEY_PIN}" -eq 64 ] || anchor_fail \
        "pinned fingerprint is not 64 hex chars (source: $PIN_SOURCE)"
    command -v sha256sum >/dev/null 2>&1 || anchor_fail \
        "sha256sum not found; fingerprint DID NOT verify"
    PUBKEY_GOT="$(sha256sum -- "$SNAP_PUB" 2>/dev/null)" || anchor_fail \
        "cannot hash public key: $SNAP_PUB"
    PUBKEY_GOT="${PUBKEY_GOT%% *}"
    if [ "$PUBKEY_GOT" != "$PUBKEY_PIN" ]; then
        anchor_fail "public key fingerprint $PUBKEY_GOT does not match pinned $PUBKEY_PIN (source: $PIN_SOURCE)"
    fi

    echo "update: verifying signed release manifest"
    if [ ! -f "$VERIFY_SH" ]; then
        echo "update: signing verification FAILED (step: signing) — verifier missing: $VERIFY_SH" >&2
        exit 1
    fi
    if ! bash "$VERIFY_SH" --pubkey "$SNAP_PUB" --manifest "$SNAP_MANIFEST" --signature "$SNAP_SIG"; then
        echo "update: signing verification FAILED (step: signing) — refusing to touch installed files" >&2
        exit 1
    fi
    # 2) 防降级：清单版本必须等于操作员期望版本（已签名旧包的重放由此被挡）。
    if [ -n "$EXPECT_VERSION" ]; then
        GOT_VERSION="$(grep -o '"version":"[^"]*"' "$SNAP_MANIFEST" 2>/dev/null \
            | head -n1 | sed 's/.*"version":"//; s/"$//' || true)"
        if [ -z "$GOT_VERSION" ]; then
            echo "update: signing verification FAILED (step: signing) — manifest has no version field" >&2
            exit 1
        fi
        if [ "$GOT_VERSION" != "$EXPECT_VERSION" ]; then
            echo "update: signing verification FAILED (step: signing) — manifest version $GOT_VERSION does not match --expect-version $EXPECT_VERSION" >&2
            exit 1
        fi
    fi
    # 清单 sha256 与本地新件逐一对账。只对账本次要替换的文件：
    # server 必须在清单里；cli 给了 --cli-bin 才对账。清单可以带其他
    # 条目（全量发布清单），不在本机替换范围内的不拦。
    manifest_hash() {
        # 单行 JSON、键序固定（make-manifest.sh 保证）；grep -o 逐匹配提取，
        # 不用跨条目的贪婪 sed。name 只含 [A-Za-z0-9._-]。
        # 末尾 || true 兜住两种非零：无匹配（rc=1）与 head 提前关管的 SIGPIPE。
        # 缺条目的失败由调用方判空后显性点名，不能让 set -e 静默退出。
        grep -o "\"name\":\"$1\",\"sha256\":\"[0-9a-f]\{64\}\"" "$SNAP_MANIFEST" 2>/dev/null \
            | head -n1 | sed "s/.*\"sha256\":\"//; s/\"\$//" || true
    }
    local_check() {
        # local_check <name> <file>
        want="$(manifest_hash "$1")"
        if [ -z "$want" ]; then
            echo "update: signing verification FAILED (step: signing) — manifest has no entry for $1" >&2
            return 1
        fi
        got="$(sha256sum -- "$2")" || {
            echo "update: signing verification FAILED (step: signing) — sha256sum failed on $2" >&2
            return 1
        }
        got="${got%% *}"
        if [ "$got" != "$want" ]; then
            echo "update: signing verification FAILED (step: signing) — $1 sha256 mismatch: manifest=$want local=$got" >&2
            return 1
        fi
    }
    if ! local_check "yiban-server" "$SNAP_SERVER"; then
        exit 1
    fi
    if [ -n "$SNAP_CLI" ] && ! local_check "yiban-cli" "$SNAP_CLI"; then
        exit 1
    fi
    echo "update: signing verification ok"
else
    # 到这里必然带着显式逃生口：无逃生口的未签名升级已在用法检查处拒绝。
    # 放行必须有痕：WARN 行留在 stderr（跳过 ≠ 绿）。
    echo "update: WARNING: no --manifest/--signature/--pubkey given; skipping release verification (unsigned update)" >&2
fi

# ---- 自检门（U2）：任何停服/写安装位之前 -----------------------------------
load_env_file "$ENV_FILE"
DSN_FULL="${YIBAN_DATABASE_URL:-}"
DSN="$DSN_FULL"
DSN_NAME="$(dsn_dbname "$DSN_FULL" || true)"
VERSION_FROM="$(version_of "$SERVER_DEST")"
VERSION_TO="$(version_of "$SNAP_SERVER")"

SELFCHECK_REPORT="$(mktemp "${TMPDIR:-/tmp}/yiban-selfcheck.XXXXXX")" || {
    echo "update: cannot create a temporary selfcheck report" >&2
    exit 2
}
rm -f -- "$SELFCHECK_REPORT.err"
if ! run_selfcheck_gate "$SELFCHECK_REPORT"; then
    log_event "selfcheck gate failed; nothing changed"
    rm -f -- "$SELFCHECK_REPORT" "$SELFCHECK_REPORT.err"
    exit 2
fi
note "selfcheck: ok (version $VERSION_FROM -> $VERSION_TO)"

# 自检通过后才建状态目录并留存证据（此前零写入）。
if ! prepare_state_dir; then
    echo "update: cannot prepare the update state directory: $STATE_DIR" >&2
    exit 2
fi
# 口令从 DSN 里摘出来写进 0600 PGPASSFILE：此后所有 psql/pg_dump 的
# argv 都不含口令。解析不了就失败关闭（exit 2，什么都没动）。
if ! prepare_pgpass; then
    echo "update: preflight FAILED — cannot keep the database password out of argv" >&2
    log_event "preflight failed: DSN password isolation"
    finish "aborted" 2
fi
install -m 0600 "$SELFCHECK_REPORT" "$STATE_DIR/selfcheck-latest.json" || {
    echo "update: cannot persist the selfcheck report" >&2
    exit 2
}
rm -f -- "$SELFCHECK_REPORT"
log_event "start version_from=$VERSION_FROM version_to=$VERSION_TO"
if ! write_state "running" 0; then
    warn "cannot write $STATE_FILE; continuing without a state file"
fi

# ---- 预检：CREATEDB --------------------------------------------------------
if [ -z "$NO_RESTORE_DB" ]; then
    if [ -z "$DSN" ]; then
        echo "update: preflight FAILED — YIBAN_DATABASE_URL is empty; cannot verify CREATEDB for database rollback (use --no-restore-db to disable database rollback explicitly)" >&2
        finish "aborted" 2
    fi
    if ! role_can_createdb "$DSN"; then
        echo "update: preflight FAILED — cannot prove the connection role may create databases (rolcreatedb=false or the psql probe failed), so a failed update could not restore the database; grant CREATEDB or pass --no-restore-db" >&2
        log_event "preflight failed: no CREATEDB"
        finish "aborted" 2
    fi
    note "preflight: connection role can create databases (database rollback enabled)"
else
    warn "--no-restore-db given: a failed update will NOT restore the database (old binary must tolerate the new schema)"
fi

# ---- 统一备份（DB + 照片 + sha256） ----------------------------------------
PHASE="backup"
if ! do_backup; then
    # 半成品备份目录不许留着：看着像备份比没有备份更危险。
    if [ -n "$BACKUP_SET" ] && [ -d "$BACKUP_SET" ]; then
        case "$BACKUP_SET" in
            "$BACKUP_ROOT"/*) rm -rf -- "$BACKUP_SET" ;;
        esac
    fi
    echo "update: backup FAILED; aborting before any service change" >&2
    log_event "backup failed; aborted"
    finish "aborted" 2
fi

# ---- 暂存新件（备份通过后才动安装位） --------------------------------------
PHASE="stage"

# 迁移要用新版的嵌入迁移集：给了 --cli-bin 就暂存新 cli，否则用已装的。
STAGED_CLI=""
if [ -n "$SNAP_CLI" ]; then
    install -m 0755 "$SNAP_CLI" "$CLI_DEST.new"
    STAGED_CLI="$CLI_DEST.new"
fi
install -m 0755 "$SNAP_SERVER" "$SERVER_NEW"

# 运行模板随二进制一起升级：新版本可能新增配置键，
# 只换二进制不换模板会让运维看不到新键（server.env 本身不动：密钥不重写）。
# 落在停服务之前：失败时服务仍在跑旧件。
mkdir -p "$(dest /etc/yiban)"
install -m 0644 "$ENV_EXAMPLE" "$(dest /etc/yiban/server.env.example)"
if [ -z "$ROOT" ]; then
    chown root:root "$(dest /etc/yiban/server.env.example)"
fi

# 前端产物暂存：停服务之前复制。
# 换名与 server.env 都留到二进制就位之后：新 UI 与指针不得先于新二进制生效。
# 暂存失败时服务仍在跑旧件，旧 UI 原样。
if [ -n "$UI_DIR" ]; then
    [ -r "$ENV_FILE" ] || { echo "update: cannot read $ENV_FILE; ui unchanged" >&2; exit 1; }
    # UI 的父目录不得是软链（目标本身可以是：mv -T 会替换链接而不是跟随）。
    if [ -L "$(dest "$SERVICE_HOME")" ]; then
        echo "update: refusing symlinked $SERVICE_HOME; the page stays unchanged" >&2
        exit 1
    fi
    # mv -T 要求父目录存在（首次装页面时 $SERVICE_HOME 可能还没有）。
    if [ ! -d "$(dest "$SERVICE_HOME")" ]; then
        if ! mkdir -p "$(dest "$SERVICE_HOME")"; then
            echo "update: cannot create $SERVICE_HOME; the page stays unchanged" >&2
            exit 1
        fi
        if [ -z "$ROOT" ]; then
            chown "$SERVICE_USER:$SERVICE_USER" "$(dest "$SERVICE_HOME")" || exit 1
        fi
    fi
    [ -d "$UPDATER_ROOT" ] || { echo "update: updater root missing: $UPDATER_ROOT" >&2; exit 1; }
    UI_STAGE="$(mktemp -d "$UPDATER_ROOT/ui.XXXXXX")" || {
        echo "update: cannot create a private ui staging directory" >&2
        exit 1
    }
    chmod 0700 "$UI_STAGE" || { rm -rf -- "$UI_STAGE"; exit 1; }
    if [ -z "$ROOT" ]; then
        chown root:root "$UI_STAGE" || { rm -rf -- "$UI_STAGE"; exit 1; }
    fi
    if ! cp -R "$UI_DIR/." "$UI_STAGE/"; then
        rm -rf -- "$UI_STAGE"
        echo "update: cannot stage new ui; installed files unchanged" >&2
        exit 1
    fi
    # chmod 只作用于刚造出来的真实目录（先于 rename），不碰已就位的目标。
    chmod -R a+rX "$UI_STAGE"
    if [ -z "$ROOT" ]; then
        chown -R "$SERVICE_USER:$SERVICE_USER" "$UI_STAGE" || { rm -rf -- "$UI_STAGE"; exit 1; }
    fi
else
    # 已装过页面却漏了参数：页面停在旧版本，必须显性可见（跳过 ≠ 绿）。
    if [ -r "$ENV_FILE" ] && grep -q '^YIBAN_UI_DIR=' "$ENV_FILE"; then
        echo "update: WARNING: no --ui-dir given; the installed page stays unchanged" >&2
    fi
fi

# ---- 停服 → 迁移 → 换件 → 起服务 ------------------------------------------
PHASE="stop"
echo "update: stopping $UNIT"
if ! "$SYSTEMCTL" stop "$UNIT"; then
    echo "update: cannot stop $UNIT; aborting before any file swap" >&2
    log_event "stop failed; aborted"
    finish "aborted" 2
fi
log_event "service stopped"

PHASE="migrate"
MIGRATE_BIN="${STAGED_CLI:-$CLI_DEST}"
if [ ! -x "$MIGRATE_BIN" ]; then
    echo "update: no cli to run migrate at $MIGRATE_BIN" >&2
    fail_after_stop "migrate: cli missing"
fi

# 迁移前版本在备份阶段已记录（PRE_GOOSE_VERSION）；这里再读一次作为
# migrate 的起点，避免备份与停服之间的漂移。
PRE_GOOSE_VERSION="${PRE_GOOSE_VERSION:-$(schema_version_of "$DSN" || true)}"
if ! "$MIGRATE_BIN" migrate; then
    echo "update: migrate FAILED; rolling back to the previous release" >&2
    fail_after_stop "migrate failed"
fi
POST_GOOSE_VERSION="$(schema_version_of "$DSN" || true)"
note "migrate: schema ${PRE_GOOSE_VERSION:-unknown} -> ${POST_GOOSE_VERSION:-unknown}"

PHASE="swap"
# 换二进制：旧件留 .bak 供回滚（既有 .bak 已在备份阶段归档）。
if ! cp -p "$SERVER_DEST" "$BAK"; then
    echo "update: cannot keep the previous binary at $BAK" >&2
    fail_after_stop "cannot create .bak"
fi
if ! mv "$SERVER_NEW" "$SERVER_DEST"; then
    echo "update: cannot activate the new binary at $SERVER_DEST" >&2
    fail_after_stop "binary swap failed"
fi
BINARY_SWAPPED=1
if [ -n "$STAGED_CLI" ]; then
    if ! mv "$CLI_DEST.new" "$CLI_DEST"; then
        echo "update: cannot activate the new cli at $CLI_DEST" >&2
        fail_after_stop "cli swap failed"
    fi
    CLI_SWAPPED=1
fi

# 前端产物换名：新二进制已就位，起服务之前激活。
# 失败则换回旧二进制并起服务：旧件继续跑，不留「新 UI＋旧 API」。
if [ -n "$UI_DIR" ]; then
    # 删除/换名前钉路径形状：不按变量盲删。
    case "$UI_DEST" in
        */var/lib/yiban/ui) ;;
        *) echo "update: refusing to replace unexpected ui path: $UI_DEST" >&2; fail_after_stop "unexpected ui path" ;;
    esac
    if ! rm -rf -- "$UI_BAK"; then
        echo "update: cannot clear the previous ui: $UI_BAK" >&2
        fail_after_stop "cannot clear the previous ui"
    fi
    # 目标本身是软链：不是合法页面目录（服务用户预置），直接摘链接，
    # 不进 .bak——否则回滚会把这个链接换回来（复审 HIGH）。
    if [ -L "$UI_DEST" ]; then
        if ! rm -f -- "$UI_DEST"; then
            echo "update: cannot remove the planted ui symlink: $UI_DEST" >&2
            fail_after_stop "cannot remove ui symlink"
        fi
    fi
    if [ -e "$UI_DEST" ]; then
        if ! mv -T "$UI_DEST" "$UI_BAK"; then
            echo "update: cannot move the current ui aside: $UI_DEST" >&2
            fail_after_stop "cannot move the current ui aside"
        fi
        UI_OLD_MOVED=1
    fi
    # -T 把目标当普通路径：目标仍在时直接失败，不会把新目录嵌进旧目录。
    if ! mv -T "$UI_STAGE" "$UI_DEST" || [ ! -f "$UI_DEST/index.html" ]; then
        echo "update: cannot activate the new ui" >&2
        fail_after_stop "cannot activate the new ui"
    fi
    UI_ACTIVATED=1
    # 目录就位后才写指针：早退路径不会留下悬空 YIBAN_UI_DIR。
    if ! write_ui_env; then
        echo "update: cannot write $ENV_FILE" >&2
        fail_after_stop "cannot write ui env"
    fi
    note "ui updated at $SERVICE_HOME/ui (previous kept at $UI_BAK)"
fi

PHASE="health"
echo "update: starting $UNIT"
if ! "$SYSTEMCTL" start "$UNIT"; then
    echo "update: systemctl start FAILED after the swap; rolling back" >&2
    fail_after_stop "start failed after swap"
fi

if ! probe; then
    echo "update: healthz failed after swap; rolling back to $BAK" >&2
    fail_after_stop "health check failed after swap"
fi

# ---- 成功 ------------------------------------------------------------------
PHASE="success"
if ( umask 077; "$CLI_DEST" selfcheck --record-keys >"$STATE_DIR/record-keys.log" 2>&1 ); then
    note "keys: fingerprints recorded in /var/lib/yiban/update-state.json"
else
    warn "selfcheck --record-keys failed; see $STATE_DIR/record-keys.log (next selfcheck may report keys.state FAIL until it is re-recorded)"
fi
echo "update complete: healthz ok at $ADDR (old binary kept at $BAK)"
note "version $VERSION_FROM -> $VERSION_TO; backup $BACKUP_SET"
finish "success" 0
