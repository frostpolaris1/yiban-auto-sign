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

mkdir -p "$BACKUP_DIR"
STAMP="$(date +%F)"
OUT="$BACKUP_DIR/yiban-data-$STAMP.tar.gz.gpg"

# 口令必须走独立 fd 3（--passphrase-fd 3 + 3<<<）。
# 原实现 `--passphrase-fd 0 ... <<< "$PASSPHRASE"` 中 herestring 重定向覆盖了
# 管道送入 gpg stdin 的 tar 数据流（同一命令上后出现的重定向胜出），gpg 实际
# 加密的是口令字符串本身——产物约 70 字节的「空备份」，tar 侧 SIGPIPE，
# Docker 部署唯一加密备份入口产出空包。fd 3 让 stdin 保留给 tar 数据流。
# 2026-09-01 CI 修复：`|| true` 容忍 tar 的 SIGPIPE——加密器（gpg/假 gpg）
# 提前关闭 stdin 时 tar 收 SIGPIPE(141)，`set -euo pipefail` 下管道非零会在
# 自检前终止脚本，坏包残留且无「疑似空包」告警。容忍后必然走到下方尺寸
# 下限检查：空包/坏包被检出并删除（B12-1 契约）。
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
