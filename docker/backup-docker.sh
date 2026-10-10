#!/usr/bin/env bash
# ============================================================
# Docker 部署加密备份与恢复（2026-08-27 审查补缺 P2-11；加固）
#
# 背景：容器部署的数据备份此前只有 README 的「裸 tar data/」路线，
# 无 backup.sh（宿主 systemd 部署）的默认加密能力，备份明文落盘。
#
# 用法（在 docker-compose.yml 所在目录执行）：
#   备份：YIBAN_BACKUP_PASSPHRASE_FILE=/etc/yiban/backup-passphrase bash docker/backup-docker.sh
#   恢复：YIBAN_BACKUP_PASSPHRASE_FILE=/etc/yiban/backup-passphrase \
#             bash docker/backup-docker.sh \
#             --restore backups/yiban-data-2026-08-29.tar.gz.gpg ./restore-test
# 口令来源：优先 YIBAN_BACKUP_PASSPHRASE_FILE（0600 文件，读入非导出 shell 变量，不进
#   子进程 environ）；YIBAN_BACKUP_PASSPHRASE / BACKUP_GPG_PASSPHRASE 环境变量形态保留
#   兼容，但会打印一行提示——该形态把口令带进 tar/gpg/find/sha256sum 整棵子进程树。
# 可选环境变量：DATA_DIR（默认 ./data）、BACKUP_DIR（默认 ./backups）、
#   RETAIN_DAYS（默认 30，正整数；0 会被拒绝——等价于删光历史备份）
#
# 打包判据（2026-10-09 返工，工单 yiban-auto-sign-4o35 路线 2）：打包**前**遍历 DATA_DIR。
#   白名单之外的条目只要本用户读不到，整轮即响亮失败并点名路径。
#   打包成败**不看** tar 退出码，也不看 stderr 文案（理由见打包处注释）。
#   管理件白名单（进程管理产物，不是用户数据）只在 logs/ 直属一层：
#     logs/supervisord.pid
#     logs/{supervisord,web,sched}.log            —— 当前日志
#     logs/{supervisord,web,sched}.log.<纯数字>   —— 轮转族（supervisord 配了
#       logfile_maxbytes=10MB 与 logfile_backups=3，故 .1/.2/.3 同为 root 属主、
#       本用户读不到，必须放行）。
#   白名单是**显式形态**：`*` 不吞子目录、不吞任意后缀。反例（曾实测被放行）：
#   logs/web.log.d/secret.bin。
#
# 库路径（2026-10-10 返工修，工单复查 M1）：未设 YIBAN_DB_FILE 时默认
#   ${DATA_DIR}/yiban.db（compose 已显式注入，默认只影响手工调用）。解析后若库不在
#   DATA_DIR 之下 ⇒ **硬失败**（与「缺库不许报成功」同口径），不再以 rc=0 收场。
#
# 依赖：宿主机 tar 与 gpg（备份经管道流式处理，不在磁盘留任何明文副本）。
# 口令丢失 = 备份不可解密；请与 ./data 分开存放口令（同 README 密钥分离承诺）。
# 注意：口令与数据同机存放时，加密只能防「备份介质单独失窃」——root 失陷
# （如 SSH 被攻破）即口令与备份同时易手，全部历史备份（含异机副本）离线可解。
# 更强的口径见 scripts/backup.sh 的 BACKUP_GPG_RECIPIENT 公钥模式（服务器无私钥）。
# ============================================================
set -euo pipefail
# M97：产物（密文/校验清单）与中途临时件一律 0600/0700——旧实现无 umask，自检全程
# 密文以 0644 存在（同机其他用户可读走备份）。
umask 077

DATA_DIR="${DATA_DIR:-./data}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
RETAIN_DAYS="${RETAIN_DAYS:-30}"
# M29：RETAIN_DAYS 零校验（对齐 scripts/backup.sh MF-77 口径）——0 会让下面的
# `-mtime +0` 删掉除当天外全部历史备份，一次误配置就清空素材。
case "$RETAIN_DAYS" in
    '' | *[!0-9]*)
        echo "错误：RETAIN_DAYS 必须是正整数（收到：'$RETAIN_DAYS'），拒绝执行以免误删历史备份" >&2
        exit 2
        ;;
esac
if [ "$RETAIN_DAYS" -lt 1 ]; then
    echo "错误：RETAIN_DAYS=0 等于删掉除当天外全部历史备份，拒绝执行（要更短保留请显式设为 >=1 的合理值）" >&2
    exit 2
fi
PASSPHRASE="${YIBAN_BACKUP_PASSPHRASE:-${BACKUP_GPG_PASSPHRASE:-}}"
if [ -z "$PASSPHRASE" ] && [ -n "${YIBAN_BACKUP_PASSPHRASE_FILE:-}" ]; then
    if [ ! -r "${YIBAN_BACKUP_PASSPHRASE_FILE}" ]; then
        echo "错误：YIBAN_BACKUP_PASSPHRASE_FILE 不可读: ${YIBAN_BACKUP_PASSPHRASE_FILE}" >&2
        exit 2
    fi
    PASSPHRASE="$(cat "${YIBAN_BACKUP_PASSPHRASE_FILE}")"
elif [ -n "$PASSPHRASE" ]; then
    echo "警告：口令经环境变量注入，会暴露给 tar/gpg/find/sha256sum 全部子进程；建议改用 YIBAN_BACKUP_PASSPHRASE_FILE（0600 文件）" >&2
fi

if [ -z "$PASSPHRASE" ]; then
    echo "错误：未设置 YIBAN_BACKUP_PASSPHRASE_FILE（或兼容的 YIBAN_BACKUP_PASSPHRASE）。" >&2
    echo "本脚本拒绝生成明文备份——若确需明文请自行手动 tar，后果自负。" >&2
    exit 2
fi
command -v gpg >/dev/null || { echo "错误：宿主机未安装 gpg" >&2; exit 3; }

# 口令经环境变量已在进程环境，但仍坚持不落到 argv：gpg 一律 --passphrase-fd
# 注入（命令行 --passphrase 会暴露于 ps / proc/<pid>/cmdline / shell history，
# 与 scripts/backup.sh 同口径）。
_TMP_PLAIN=""
cleanup_tmp() { [ -n "$_TMP_PLAIN" ] && rm -f "$_TMP_PLAIN" || true; }
trap cleanup_tmp EXIT

# ---- 恢复模式----
# 原 README 恢复指引是一条裸 gpg|tar 管道：口令上命令行、且无 scripts/backup.sh
# --restore 已有的三重安全校验——被篡改的备份包在 root 解包时可借 ../ 路径穿越、
# 符号链接或设备节点逃逸出目标目录。现统一走本校验路径。
if [ "${1:-}" = "--restore" ]; then
    ARCHIVE="${2:-}"
    DEST="${3:-}"
    if [ -z "$ARCHIVE" ] || [ ! -f "$ARCHIVE" ]; then
        echo "错误：用法 backup-docker.sh --restore <备份包.tar.gz.gpg> <目标目录>" >&2
        exit 1
    fi
    if [ -z "$DEST" ]; then
        echo "错误：请指定恢复目标目录（恢复演练用临时目录，避免覆盖生产数据）" >&2
        exit 1
    fi
    _TMP_PLAIN="$(mktemp "${TMPDIR:-/tmp}/yiban-restore.XXXXXX")"
    printf '%s\n' "$PASSPHRASE" | gpg --batch --yes --decrypt --passphrase-fd 0 \
        -o "$_TMP_PLAIN" "$ARCHIVE" 2>/dev/null \
        || { echo "错误：解密失败（口令错误或备份包损坏）" >&2; exit 1; }
    # 三重校验（与 scripts/backup.sh 同口径）。M70：先取列表再 here-string 判——
    # 原 `tar | grep -q` 在 set -o pipefail 下，tar 非 0 或列表超管道缓冲时整条管道
    # 短路，三条护栏被静默跳过（fail-open 放行到解包）。
    _TAR_LIST="$(tar -tzf "$_TMP_PLAIN" 2>/dev/null)" || {
        echo "错误：读包列表失败（包损坏或 tar 不可用），拒绝解包" >&2
        exit 1
    }
    if grep -qE '(^|/)\.\.(/|$)|^/' <<< "$_TAR_LIST"; then
        echo "错误：备份包含不安全条目（路径穿越/绝对路径），已拒绝解包" >&2
        exit 1
    fi
    _TAR_VLIST="$(tar -tvzf "$_TMP_PLAIN" 2>/dev/null)" || {
        echo "错误：读包列表失败（长列表 tar 非零退出），拒绝解包" >&2
        exit 1
    }
    if grep -qE '^l' <<< "$_TAR_VLIST"; then
        echo "错误：备份包含符号链接条目，已拒绝解包（防链接写出目标目录）" >&2
        exit 1
    fi
    if grep -qE '^[bcp]' <<< "$_TAR_VLIST"; then
        echo "错误：备份包含设备/FIFO 特殊条目，已拒绝解包" >&2
        exit 1
    fi
    mkdir -p "$DEST"
    tar -xzf "$_TMP_PLAIN" -C "$DEST" --no-overwrite-dir
    echo "恢复完成: $ARCHIVE → $DEST"
    exit 0
fi

# ---- 备份模式 ----
if [ ! -d "$DATA_DIR" ]; then
    echo "错误：数据目录不存在: $DATA_DIR" >&2
    exit 1
fi

# ---- 打前先查（工单 yiban-auto-sign-4o35 路线 2）----
# 打包前遍历 DATA_DIR，找出**本用户读不到的条目**。
# 判据只认"当前用户能否读"：tar 与本脚本同一用户，故这里能读到什么，tar 就能读到。
# 白名单放行管理件；白名单之外有一条读不到，整轮即失败并点名路径。
# 为什么不用 tar 退出码：本脚本用 tar -czf（边压缩边打包）。下游提前关 stdin 时，
#   收到 SIGPIPE 的是里层压缩程序，tar 报 rc=2（Child returned status 141），
#   与"有条目读不到"同码，退出码无法分辨二者。故退出码不作判据。
# 管理件白名单是**显式形态**，只认 logs/ 直属一层：
#   logs/supervisord.pid
#   logs/{supervisord,web,sched}.log              —— 当前日志
#   logs/{supervisord,web,sched}.log.<纯数字>     —— 轮转族（logfile_backups）
# `*` 不吞子目录、不吞任意后缀。反例（实测被旧 `logs/web.log*` 放行）：
#   logs/web.log.d/secret.bin（读不到却报成功）。
_MGMT_LOG_BASES=(supervisord.log web.log sched.log)
_mgmt_allowed() { # $1 = 相对 DATA_DIR 的路径
    local rel=$1 name base rest
    case "$rel" in
        logs/*) name="${rel#logs/}" ;;
        *) return 1 ;;
    esac
    case "$name" in
        */*) return 1 ;;                 # 只在 logs/ 直属一层，任何子目录一律拦下
        supervisord.pid) return 0 ;;
    esac
    for base in "${_MGMT_LOG_BASES[@]}"; do
        [ "$name" = "$base" ] && return 0
        case "$name" in
            "$base".*)
                rest="${name#"$base".}"
                # 轮转族后缀必须是**纯数字**，不放行任意后缀
                [ -n "$rest" ] || continue
                case "$rest" in *[!0-9]*) continue ;; esac
                return 0
                ;;
        esac
    done
    return 1
}

_unreadable=""
while IFS= read -r -d '' _entry; do
    _rel="${_entry#"${DATA_DIR%/}/"}"
    _mgmt_allowed "$_rel" && continue
    if [ -d "$_entry" ]; then
        # 目录要能被 tar 列内容：既需读位（列名），也需搜位（穿进）
        { [ -r "$_entry" ] && [ -x "$_entry" ]; } || _unreadable="${_unreadable}${_entry}"$'\n'
    elif [ ! -r "$_entry" ]; then
        _unreadable="${_unreadable}${_entry}"$'\n'
    fi
done < <(find "$DATA_DIR" \( -type d -o -type f \) -print0 2>/dev/null)

if [ -n "$_unreadable" ]; then
    echo "错误：备份前检查发现【本用户读不到】的条目——这些条目不会进包，本次不产出备份：" >&2
    printf '%s' "$_unreadable" >&2
    echo "      处置：修好上列条目的属主与权限后重跑。" >&2
    echo "      白名单：logs/ 直属一层——logs/{supervisord,web,sched}.log、其纯数字轮转后缀 .N、logs/supervisord.pid（管理件）。" >&2
    exit 1
fi

mkdir -p "$BACKUP_DIR"
STAMP="$(date +%F)"
OUT="$BACKUP_DIR/yiban-data-$STAMP.tar.gz.gpg"

# 口令必须走独立 fd 3（--passphrase-fd 3 + 3<<<）。
# 原实现 `--passphrase-fd 0 ... <<< "$PASSPHRASE"` 中 herestring 重定向覆盖了
# 管道送入 gpg stdin 的 tar 数据流（同一命令上后出现的重定向胜出），gpg 实际
# 加密的是口令字符串本身——产物约 70 字节的「空备份」，tar 侧 SIGPIPE，
# Docker 部署唯一加密备份入口产出空包。fd 3 让 stdin 保留给 tar 数据流。
# 2026-09-01 CI 修复：容忍 tar 的 SIGPIPE——加密器（gpg/假 gpg）提前关闭 stdin 时
# tar 收 SIGPIPE(141/144)，`set -euo pipefail` 下管道非零会在自检前终止脚本，
# 坏包残留且无「疑似空包」告警。
#
# **2026-10-09 返工（工单 yiban-auto-sign-4o35 路线 2）**：不复用 tar 退出码当判据。
# 机理：本脚本用 tar -czf，边压缩边打包。下游提前关 stdin 时，收到 SIGPIPE 的是里层
# 压缩程序，tar 报 rc=2（Child returned status 141）——与"有条目读不到"的 rc=2 同码，
# 退出码分不出二者。首版修复只容忍 141/144，于是把正常中断判成故障、又对真失败漏判
# （实测把既有守卫 tests/test_scheduler_gate.py 打红）。
# 现保留 `|| true`，判据改由上方「打前先查」承担：它确定性列出读不到的条目，与 tar
# 退出码、stderr 文案都无关。gpg 失败不另判——产物为零字节或不可解开时，下方尺寸下限
# 与自检会拦下并删包。
tar -C "$(dirname "$DATA_DIR")" -czf - "$(basename "$DATA_DIR")" \
    | gpg --batch --yes --symmetric --cipher-algo AES256 --passphrase-fd 3 \
          -o "$OUT" 3<<< "$PASSPHRASE" || true

# 产物自检（B12-1）：先做尺寸下限，再流式解密+解包验证（解密输出直接进
# tar -tzf 的 stdin，不在磁盘留明文副本）。自检失败删除产物并报错退出，
# 绝不留下「看似成功」的坏备份。
MIN_BYTES=200
ACTUAL_BYTES="$(wc -c < "$OUT" 2>/dev/null || echo 0)"
if [ "$ACTUAL_BYTES" -lt "$MIN_BYTES" ]; then
    echo "错误：备份产物仅 ${ACTUAL_BYTES} 字节（低于下限 ${MIN_BYTES}），疑似空包，已删除" >&2
    rm -f "$OUT" "$OUT.sha256"
    exit 1
fi
if ! gpg --batch --yes --decrypt --passphrase-fd 3 -o - 3<<< "$PASSPHRASE" "$OUT" 2>/dev/null \
        | tar -tzf - >/dev/null; then
    echo "错误：备份自检失败（解密/解包验证不通过），产物不可信，已删除" >&2
    rm -f "$OUT" "$OUT.sha256"
    exit 1
fi

# 库本体必须在包里（**硬断言**，工单 yiban-auto-sign-4o35）。与下方锚点检查同一形状，
# 但判据更硬：库是这份备份存在的全部理由，缺它这份包没有任何恢复价值。
# 为什么不能只靠"自检通过"：自检只验"能不能解开"、不验内容；而 DATA_DIR 里日志与
# state 的体积远超尺寸下限（实测 9 KB 对 200 B），故"少了库"能轻松越过体积门。
# 只在磁盘上确有该文件时断言：没有它是"尚未建库"的正常形态（首启前），
# 不构成"备份漏了东西"——两者必须分开，否则全新部署天天报假失败。
#
# 路径口径（返工修，工单复查点名）：库可能在子目录（YIBAN_DB_FILE=/data/sub/x.db）。
# 首版只取 basename 再去 DATA_DIR 根下找，找不到即静默跳过断言——漏判。现按归档内
# **成员路径**精确比对：tar 以 `-C dirname(DATA_DIR) basename(DATA_DIR)` 打包，故
# 成员名 = `<DATA_DIR 基名>/<库相对 DATA_DIR 的路径>`。
# 判据用 grep -cxF（整行、字面）：库名含 `[` 等正则元字符时首版会报错，未转义时会把
# yibanXdb 误配成 yiban.db——两种误判都要挡。
# 库解析后若不在 DATA_DIR 之下（YIBAN_DB_FILE 指到别处）⇒ 硬失败并删产物，与
# 「缺库不许报成功」同口径；旧实现只打一行提醒并 rc=0，等于放行不含库的备份。
# 默认库路径 = ${DATA_DIR}/yiban.db（compose 已用 YIBAN_DB_FILE 显式注入，默认值只
# 影响手工调用）。旧默认 "yiban.db" 是相对 cwd 的路径，手工调用时会被判成"库在
# DATA_DIR 之外"而静默跳过断言。
DB_FILE_RAW="${YIBAN_DB_FILE:-${DATA_DIR}/yiban.db}"
DB_BASENAME="$(basename "$DB_FILE_RAW")"
_DATA_ABS="$(cd "$DATA_DIR" 2>/dev/null && pwd -P)" || _DATA_ABS="$DATA_DIR"
_DB_DIR="$(cd "$(dirname "$DB_FILE_RAW")" 2>/dev/null && pwd -P)" || _DB_DIR="$(dirname "$DB_FILE_RAW")"
DB_ABS="${_DB_DIR}/${DB_BASENAME}"
case "$DB_ABS" in
    "${_DATA_ABS}"/*) DB_MEMBER="$(basename "$DATA_DIR")/${DB_ABS#"${_DATA_ABS}/"}" ;;
    *) DB_MEMBER="" ;;
esac
if [ -z "$DB_MEMBER" ]; then
    echo "错误：库文件不在 ${DATA_DIR} 内（${DB_ABS} 不在 ${_DATA_ABS} 之下）——整体备份不含库本体" >&2
    echo "      库是这份备份的全部理由，缺它这份包没有恢复价值，故拒绝产出。" >&2
    echo "      处置：把 YIBAN_DB_FILE 指回 ${DATA_DIR} 之内，或修正 DATA_DIR（容器默认 /data/yiban.db）。" >&2
    rm -f "$OUT" "$OUT.sha256"
    exit 1
elif [ -e "$DB_ABS" ]; then
    DB_IN_ARCHIVE="$(gpg --batch --yes --decrypt --passphrase-fd 3 -o - 3<<< "$PASSPHRASE" "$OUT" 2>/dev/null \
        | tar -tzf - 2>/dev/null | grep -cxF "$DB_MEMBER" || true)"
    if [ "${DB_IN_ARCHIVE:-0}" -lt 1 ]; then
        echo "错误：备份包内【没有】${DB_MEMBER}——库本体未入包，这份备份恢复不出数据" >&2
        echo "      磁盘上该文件存在（${DB_ABS}），故不是「尚未建库」。" >&2
        echo "      产物不可信，已删除。先查该文件的属主与权限是否让本用户可读。" >&2
        rm -f "$OUT" "$OUT.sha256"
        exit 1
    fi
    echo "库本体 : 已包含 ${DB_MEMBER}（${DB_IN_ARCHIVE} 份）"
else
    echo "提醒   : ${DB_ABS} 不存在（尚未建库？），本次不断言库本体入包" >&2
fi

# 审计链外部锚点必须在包里（断言，不是假设）：容器默认 YIBAN_STATE_DIR=/data/state
# 落在 DATA_DIR 内，理论上是随 data/ 整体入包的——但这一点从未被核验过。一旦有人把
# YIBAN_STATE_DIR 指到 data/ 之外（compose 里改一行），备份包照样"自检通过"却不含
# 锚点，而锚点是审计链唯一的外部参照：缺了它，恢复出来的库再也检不出
# 删尾 / 删前缀 / 整表清空（这三类恰是掩盖篡改最常用的动作）。
ANCHOR_IN_ARCHIVE="$(gpg --batch --yes --decrypt --passphrase-fd 3 -o - 3<<< "$PASSPHRASE" "$OUT" 2>/dev/null \
    | tar -tzf - 2>/dev/null | grep -c 'audit-anchor\.log$' || true)"
if [ "${ANCHOR_IN_ARCHIVE:-0}" -ge 1 ]; then
    echo "锚点   : 已包含审计链外部锚点 audit-anchor.log（${ANCHOR_IN_ARCHIVE} 份）"
else
    echo "⚠⚠⚠ 备份包内【没有】audit-anchor.log —— 审计链外部锚点未入包 ⚠⚠⚠" >&2
    echo "      恢复这份备份后，「删尾/删前缀/整表清空」三类判据全部失效。" >&2
    echo "      常见成因：YIBAN_STATE_DIR 被指到 ${DATA_DIR} 之外（容器默认为 /data/state）。" >&2
    echo "      处置：把状态目录改回 DATA_DIR 内，或另行把 <YIBAN_STATE_DIR>/audit-anchor.log" >&2
    echo "      与备份同批次离机保存（每日日报邮件里也带一条链头锚点）。" >&2
fi

chmod 600 "$OUT"
sha256sum "$OUT" | awk '{print $1}' > "$OUT.sha256"

# 超期清理（按文件名日期粗筛即可）。M29：先列后删同一份列表 + 逐条留痕——
# 原 `find … -delete` 命中即删不留一行日志，删错了无从追溯。
while IFS= read -r _old; do
    [ -n "$_old" ] || continue
    echo "removing: $_old"
    rm -f -- "$_old"
done <<< "$(find "$BACKUP_DIR" -name 'yiban-data-*.tar.gz.gpg' -mtime "+$RETAIN_DAYS" 2>/dev/null || true)"
while IFS= read -r _old; do
    [ -n "$_old" ] || continue
    echo "removing: $_old"
    rm -f -- "$_old"
done <<< "$(find "$BACKUP_DIR" -name '*.sha256' -mtime "+$RETAIN_DAYS" 2>/dev/null || true)"

echo "备份完成: $OUT"
echo "校验值 : $(cat "$OUT.sha256")"
echo "自检   : 解密+解包验证通过"
