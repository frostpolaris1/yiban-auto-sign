#!/usr/bin/env bash
# ============================================================
# yiban-backup-wrapper.sh —— root cron 的备份入口（口令注入点，MF-42 收编）
# 仓库来源：deploy/prod/yiban-backup-wrapper.sh
# 安装位：  /usr/local/sbin/yiban-backup-wrapper.sh（0700 root，install.sh 落位）
# root crontab 唯一允许形态（口令不进 crontab 行 / 命令历史 / 任何进程的 env）：
#   0 2 * * * /usr/local/sbin/yiban-backup-wrapper.sh >> /var/log/yiban/backup.log 2>&1
# 口令文件：/etc/yiban/backup-passphrase —— 0600 root，内容只有一行口令。
#
# 为什么存在（现象原文）：旧版 wrapper 用 `export BACKUP_GPG_PASSPHRASE=...` 注入口令，
# 把口令带进 yiban-backup.sh → tar/sqlite3/rsync/gpg 整棵子进程树的 environ，
# /proc/<pid>/environ 可读即泄露。
# 现在（修法）：**--passphrase-fd 0 单跳**——口令只存在于本 shell 的局部变量和一根
# 管道里；yiban-backup.sh 见 YIBAN_READ_PASSPHRASE_STDIN=1 时从 stdin 读入口令进
# 非导出变量并 unset 环境键，再 printf|gpg --passphrase-fd 0 单跳给 gpg。链路上任何
# 一层子进程的环境里都不再有口令（tests/test_deploy_prod_artifacts.py 用 stub 记录
# 子进程真实环境钉死这条不变量，并对旧 export 形态保留活体反例）。
# 防御：即便 crontab 行仍残留 `BACKUP_GPG_PASSPHRASE=...` 前缀（旧部署习惯），
# 本脚本也先 unset 再走 fd 路径——环境里有口令键这件事本身就是缺陷。
# 可测试性：YIBAN_BACKUP_SCRIPT / YIBAN_BACKUP_PASSPHRASE_FILE 可覆盖两个安装路径，
# 供 e2e 以 stub 真跑；不设置时生产默认值不变。
# ============================================================
set -euo pipefail
umask 077

BACKUP_SCRIPT="${YIBAN_BACKUP_SCRIPT:-/usr/local/sbin/yiban-backup.sh}"
PASSPHRASE_FILE="${YIBAN_BACKUP_PASSPHRASE_FILE:-/etc/yiban/backup-passphrase}"

die() { echo "yiban-backup-wrapper: $*" >&2; exit 1; }

[ -f "$BACKUP_SCRIPT" ] && [ -x "$BACKUP_SCRIPT" ] \
    || die "备份脚本缺失或不可执行: $BACKUP_SCRIPT"
[ -r "$PASSPHRASE_FILE" ] \
    || die "口令文件缺失或不可读 (passphrase file unreadable): $PASSPHRASE_FILE"

# 口令文件自身必须 root-only 可读：组/其他位带读 = 文件即泄露，拒跑（fail-closed）
perm=$(stat -c '%a' "$PASSPHRASE_FILE" 2>/dev/null) \
    || die "无法 stat 口令文件 (passphrase file): $PASSPHRASE_FILE"
if (( 8#$perm & 8#077 )); then
    die "口令文件权限过宽 (passphrase file mode $perm, need 0600/0400): $PASSPHRASE_FILE"
fi

passphrase=$(< "$PASSPHRASE_FILE")
[ -n "$passphrase" ] || die "口令文件为空: $PASSPHRASE_FILE"

# 旧注入形态兜底：无论调用方 env 带了什么，进入子进程树前一律摘除口令键
unset BACKUP_GPG_PASSPHRASE BACKUP_AGE_PASSPHRASE GPG_PASSPHRASE 2>/dev/null || true

# 单跳：printf 是 bash 内建——口令不落 ps/argv；管道（fd 0）是它唯一的过桥。
# --require-encrypt 钉死在位（加密失效宁可不产出归档，也不留含全库凭据的明文，
# 口径见 yiban-backup.sh 头注释与 README「备份与恢复」）。退出码原样传播。
printf '%s\n' "$passphrase" \
    | env YIBAN_READ_PASSPHRASE_STDIN=1 "$BACKUP_SCRIPT" --require-encrypt "$@"
