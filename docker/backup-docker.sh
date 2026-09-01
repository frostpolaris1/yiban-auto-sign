#!/usr/bin/env bash
# ============================================================
# Docker 部署加密备份与恢复（2026-08-27 审查补缺 P2-11；批次12 B12-1/B12-11 加固）
#
# 背景：容器部署的数据备份此前只有 README 的「裸 tar data/」路线，
# 无 backup.sh（宿主 systemd 部署）的默认加密能力，备份明文落盘。
#
# 用法（在 docker-compose.yml 所在目录执行）：
#   备份：YIBAN_BACKUP_PASSPHRASE='你的口令' bash docker/backup-docker.sh
#   恢复：YIBAN_BACKUP_PASSPHRASE='你的口令' bash docker/backup-docker.sh \
#             --restore backups/yiban-data-2026-08-29.tar.gz.gpg ./restore-test
# 可选环境变量：DATA_DIR（默认 ./data）、BACKUP_DIR（默认 ./backups）、
#   RETAIN_DAYS（默认 30，超期自动删除）
#
# 依赖：宿主机 tar 与 gpg（备份经管道流式处理，不在磁盘留任何明文副本）。
# 口令丢失 = 备份不可解密；请与 ./data 分开存放口令（同 README 密钥分离承诺）。
# 注意：口令与数据同机存放时，加密只能防「备份介质单独失窃」——root 失陷
# （如 SSH 被攻破）即口令与备份同时易手，全部历史备份（含异机副本）离线可解。
# 更强的口径见 scripts/backup.sh 的 BACKUP_GPG_RECIPIENT 公钥模式（服务器无私钥）。
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
command -v gpg >/dev/null || { echo "错误：宿主机未安装 gpg" >&2; exit 3; }

# 口令经环境变量已在进程环境，但仍坚持不落到 argv：gpg 一律 --passphrase-fd
# 注入（命令行 --passphrase 会暴露于 ps / proc/<pid>/cmdline / shell history，
# 与 scripts/backup.sh 同口径）。
_TMP_PLAIN=""
cleanup_tmp() { [ -n "$_TMP_PLAIN" ] && rm -f "$_TMP_PLAIN" || true; }
trap cleanup_tmp EXIT

# ---- 恢复模式（批次12 B12-11）----
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
    # 三重校验（与 scripts/backup.sh:165-179 同口径）
    if tar -tzf "$_TMP_PLAIN" 2>/dev/null | grep -E '(^|/)\.\.(/|$)|^/' | grep -q .; then
        echo "错误：备份包含不安全条目（路径穿越/绝对路径），已拒绝解包" >&2
        exit 1
    fi
    if tar -tvzf "$_TMP_PLAIN" 2>/dev/null | grep -qE '^l'; then
        echo "错误：备份包含符号链接条目，已拒绝解包（防链接写出目标目录）" >&2
        exit 1
    fi
    if tar -tvzf "$_TMP_PLAIN" 2>/dev/null | grep -qE '^[bcp]'; then
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

# 批次12 B12-1：口令必须走独立 fd 3（--passphrase-fd 3 + 3<<<）。
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

chmod 600 "$OUT"
sha256sum "$OUT" | awk '{print $1}' > "$OUT.sha256"

# 超期清理（按文件名日期粗筛即可）
find "$BACKUP_DIR" -name 'yiban-data-*.tar.gz.gpg' -mtime "+$RETAIN_DAYS" -delete 2>/dev/null || true
find "$BACKUP_DIR" -name '*.sha256' -mtime "+$RETAIN_DAYS" -delete 2>/dev/null || true

echo "备份完成: $OUT"
echo "校验值 : $(cat "$OUT.sha256")"
echo "自检   : 解密+解包验证通过"
