#!/bin/bash
# ============================================================
# pull-prod-backup.sh —— 生产备份「异机副本」拉取（在运维工作站上运行）
# ============================================================
# 为什么需要它（批次20 审查 B1）：
#   生产每日 02:00 的备份与系统**同机同盘**（REMOTE_BACKUP 未配置），加密口令
#   /etc/yiban/backup-passphrase 也在同一台机器上——服务器整机失陷/磁盘损毁时
#   密文与口令一起丢失，备份形同虚设。本脚本把生产已加密的备份包**拉取到另一台
#   机器（运维工作站）**，构成真正的异机副本。
#
# 设计约束（务必保持）：
#   1. **对生产完全只读**：远端只执行 `ls` 与 `sha256sum`，只用 `scp` 下载；
#      本脚本绝不向生产写入任何文件、不重启服务、不改配置。
#   2. **只搬密文**：拉取 `yiban-*.tar.gz.gpg` 与其 `.sha256` 清单。备份口令不在
#      本机时副本无法解密——请把口令单独保存在密码管理器（本项目生产口令已由用户在
#      桌面《服务器访问信息.md》记录，建议同时转存密码管理器）。
#   3. **完整性校验后才入库**：`.sha256` 与服务端比对，下载后本地再算一次；
#      校验失败的文件改名 `.bad` 并保留原文件（不静默丢弃证据）。
#   4. **本地保留期更长**：生产保留 30 天，本地默认保留 60 天——用于兜住
#      "生产备份悄悄坏了近一个月才被发现"的情形。
#
# 用法：
#   bash scripts/pull-prod-backup.sh              # 增量拉取 + 校验 + 清理
#   bash scripts/pull-prod-backup.sh --check      # 只报告（列出远端/本地差异，不下载）
#   bash scripts/pull-prod-backup.sh --verify     # 只校验本地已有副本完整性
#
# 环境变量（均可覆盖）：
#   YIBAN_SSH_HOST       ssh 别名，默认 yiban（~/.ssh/config 中已配置，禁止直连 IP）
#   REMOTE_BACKUP_DIR    生产备份目录，默认 /var/backups
#   LOCAL_MIRROR_DIR     本地镜像目录，Windows 默认 D:/code/backups/yiban-prod-mirror，
#                        其他平台默认 $HOME/yiban-prod-mirror
#   LOCAL_KEEP           本地保留份数，默认 60（0=不清理）
#   PULL_MAX_FETCH       单次最多拉取份数，默认 30（防止误配导致一次拉爆）
#
# 退出码：0=成功（或 --check 无差异）；1=失败/校验不过；2=用法错误
umask 077
set -u

SSH_HOST="${YIBAN_SSH_HOST:-yiban}"
REMOTE_DIR="${REMOTE_BACKUP_DIR:-/var/backups}"
LOCAL_KEEP="${LOCAL_KEEP:-60}"
MAX_FETCH="${PULL_MAX_FETCH:-30}"

if [ -z "${LOCAL_MIRROR_DIR:-}" ]; then
    case "$(uname -s)" in
        MINGW*|MSYS*|CYGWIN*) LOCAL_MIRROR_DIR="D:/code/backups/yiban-prod-mirror" ;;
        *)                    LOCAL_MIRROR_DIR="$HOME/yiban-prod-mirror" ;;
    esac
fi

MODE="pull"
case "${1:-}" in
    "")        MODE="pull" ;;
    --check)   MODE="check" ;;
    --verify)  MODE="verify" ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    *)         echo "未知参数: $1（支持 --check / --verify）" >&2; exit 2 ;;
esac

mkdir -p "$LOCAL_MIRROR_DIR" || { echo "无法创建本地镜像目录 $LOCAL_MIRROR_DIR" >&2; exit 1; }
LOG="$LOCAL_MIRROR_DIR/pull.log"
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG" >&2; }

# 防呆：别在生产机自己身上"做异机副本"
if [ -d /opt/yiban-auto-sign ] && [ "$(uname -s)" = "Linux" ]; then
    log "致命：本机看起来就是生产服务器（存在 /opt/yiban-auto-sign）。异机副本必须在另一台机器上拉取。"
    exit 1
fi

_remote_ls() {
    # 只读：按 mtime 倒序列出远端密文包（不含明文 tar.gz，避免把明文搬到工作站）
    ssh -o BatchMode=yes -o ConnectTimeout=15 "$SSH_HOST" \
        "ls -1t ${REMOTE_DIR}/yiban-*.tar.gz.gpg 2>/dev/null | head -n $MAX_FETCH"
}

_sha256_of() {
    # GNU coreutils 与 Git Bash 的 sha256sum 输出格式一致：<hash>  <file>
    sha256sum "$1" 2>/dev/null | awk '{print $1}'
}

_remote_sha256() {
    ssh -o BatchMode=yes -o ConnectTimeout=15 "$SSH_HOST" \
        "sha256sum '$1' 2>/dev/null | awk '{print \$1}'"
}

verify_local() {
    # 校验本地全部副本：与随行的 .sha256 清单比对
    local bad=0 n=0 newest=""
    for f in "$LOCAL_MIRROR_DIR"/yiban-*.tar.gz.gpg; do
        [ -e "$f" ] || continue
        n=$((n + 1)); newest="$f"
        if [ ! -f "${f}.sha256" ]; then
            log "校验失败（缺 .sha256 清单）: $(basename "$f")"; bad=$((bad + 1)); continue
        fi
        local want got
        want="$(awk '{print $1}' "${f}.sha256")"
        got="$(_sha256_of "$f")"
        if [ "$want" != "$got" ]; then
            log "校验失败（哈希不符）: $(basename "$f") want=$want got=$got"
            bad=$((bad + 1))
        fi
    done
    log "本地副本校验完成：共 $n 份，异常 $bad 份；目录 $LOCAL_MIRROR_DIR"
    [ "$n" -gt 0 ] && log "最新本地副本：$(basename "$newest")（$(date -r "$newest" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo '?')）"
    [ "$bad" -eq 0 ] || return 1
    return 0
}

if [ "$MODE" = "verify" ]; then
    verify_local || exit 1
    exit 0
fi

log "=== 开始拉取生产备份副本（host=$SSH_HOST dir=$REMOTE_DIR → $LOCAL_MIRROR_DIR）==="
REMOTE_FILES="$(_remote_ls)"
if [ -z "$REMOTE_FILES" ]; then
    log "远端未列出任何 yiban-*.tar.gz.gpg（路径错误？备份任务停了？）"
    exit 1
fi
# 必须先落成数组再遍历：循环体内有 ssh/scp，它们会读取 stdin 把 while read 的
# 输入流吃掉，导致只处理第一项（首版实测只拉到 1/6 份）。
# 同时循环体内所有 ssh/scp 都显式 </dev/null，杜绝再次偷吃输入流。
REMOTE_ARR=()
while IFS= read -r _line; do
    [ -n "$_line" ] && REMOTE_ARR+=("$_line")
done <<< "$REMOTE_FILES"

fetched=0 skipped=0 failed=0
for rpath in "${REMOTE_ARR[@]}"; do
    name="$(basename "$rpath")"
    local_file="$LOCAL_MIRROR_DIR/$name"

    # 已存在且自校验通过 → 跳过（增量）
    if [ -f "$local_file" ] && [ -f "${local_file}.sha256" ]; then
        want="$(awk '{print $1}' "${local_file}.sha256")"
        [ "$want" = "$(_sha256_of "$local_file")" ] && { skipped=$((skipped + 1)); continue; }
    fi

    if [ "$MODE" = "check" ]; then
        log "待拉取: $name"
        fetched=$((fetched + 1))
        continue
    fi

    # 先取远端哈希，再下载，最后本地复算——三处一致才入库
    rsum="$(_remote_sha256 "$rpath" < /dev/null)"
    if [ -z "$rsum" ]; then
        log "跳过 $name：远端无法计算 sha256"; failed=$((failed + 1)); continue
    fi
    if ! scp -q -o BatchMode=yes -o ConnectTimeout=20 "$SSH_HOST:$rpath" "$local_file.tmp" < /dev/null; then
        log "下载失败: $name"; rm -f "$local_file.tmp"; failed=$((failed + 1)); continue
    fi
    lsum="$(_sha256_of "$local_file.tmp")"
    if [ "$rsum" != "$lsum" ]; then
        log "校验失败（远端 $rsum ≠ 本地 $lsum），已改名为 .bad 保留证据: $name"
        mv -f "$local_file.tmp" "$local_file.bad" 2>/dev/null || rm -f "$local_file.tmp"
        failed=$((failed + 1)); continue
    fi
    mv -f "$local_file.tmp" "$local_file"
    printf '%s  %s\n' "$lsum" "$name" > "${local_file}.sha256"
    log "已拉取并校验: $name（$lsum）"
    fetched=$((fetched + 1))
done

if [ "$MODE" = "check" ]; then
    log "=== 检查完成：待拉取 $fetched 份，已同步 $skipped 份 ==="
    [ "$fetched" -eq 0 ] && exit 0
    exit 0
fi

# 本地保留策略：按文件名日期倒序保留最近 $LOCAL_KEEP 份（0=不清理）
if [ "$LOCAL_KEEP" -gt 0 ]; then
    # 末 N 天之外的旧包删除；只匹配本项目命名，绝不用通配删整个目录
    ls -1 "$LOCAL_MIRROR_DIR"/yiban-*.tar.gz.gpg 2>/dev/null | sort -r | \
        tail -n +"$((LOCAL_KEEP + 1))" | while IFS= read -r old; do
            rm -f "$old" "${old}.sha256"
            log "本地清理（保留 $LOCAL_KEEP 份）: $(basename "$old")"
        done
fi

verify_local || { log "=== 拉取结束但本地校验存在异常 ==="; exit 1; }
log "=== 拉取完成：新增 $fetched 份 / 已同步 $skipped 份 / 失败 $failed 份 ==="
[ "$failed" -eq 0 ] || exit 1
exit 0
