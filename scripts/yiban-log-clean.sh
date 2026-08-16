#!/bin/bash
# 按天签到日志清理：删除超过保留期（默认 365 天）的 sign-YYYY-MM-DD.log。
# 部署：cron 每天执行一次，如 `0 3 * * * root /usr/local/sbin/yiban-log-clean.sh`
# 环境变量：YIBAN_LOG_DIR（默认 /var/log/yiban）、YIBAN_LOG_RETENTION_DAYS（默认 365）
umask 077

LOG_DIR="${YIBAN_LOG_DIR:-/var/log/yiban}"
RETENTION_DAYS="${YIBAN_LOG_RETENTION_DAYS:-365}"

# 按文件名日期判断（比 mtime 精确：当天文件即使之后被 touch 也不误删）
cutoff="$(date -d "$RETENTION_DAYS days ago" +%Y-%m-%d)"
if [ -z "$cutoff" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 无法计算保留截止日期，跳过" >> "$LOG_DIR/sign-$(date +%Y-%m-%d).log"
    exit 1
fi

removed=0
for f in "$LOG_DIR"/sign-????-??-??.log; do
    [ -e "$f" ] || continue
    d="${f##*/}"          # sign-YYYY-MM-DD.log
    d="${d#sign-}"        # YYYY-MM-DD.log
    d="${d%.log}"         # YYYY-MM-DD
    if [[ "$d" < "$cutoff" ]]; then
        rm -f "$f"
        removed=$((removed + 1))
    fi
done

if [ "$removed" -gt 0 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 已清理 $removed 个超过 $RETENTION_DAYS 天的按天日志（截止 $cutoff）" \
        >> "$LOG_DIR/sign-$(date +%Y-%m-%d).log"
fi
exit 0
