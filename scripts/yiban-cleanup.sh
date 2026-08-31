#!/bin/bash
# 按天数据清理：删除超过保留期的按天日志与状态文件，清理空熔断状态文件。
#
# 覆盖（目录默认 /var/log/yiban，均按文件名日期判断，比 mtime 精确）：
#   - sign-YYYY-MM-DD.log            按天签到日志，保留 YIBAN_RETENTION_DAYS（默认 365）天
#   - sign-state-YYYY-MM-DD.json     按日结构化状态，保留 YIBAN_RETENTION_DAYS 天
#   - sched-snapshot-YYYY-MM-DD.json 调度快照标记，仅近期有意义，保留 YIBAN_SNAPSHOT_RETENTION_DAYS（默认 7）天
#   - cred-state.json                账密熔断状态：内容为空（{}）时删除（无暂停 = 文件不存在语义）
#
# 部署：cron 每天执行一次，如 `0 3 * * * yiban /bin/bash /opt/yiban-auto-sign/scripts/yiban-cleanup.sh`
#       以 yiban 用户运行（与 run.sh/签到日志同属主）；清理结果写入独立 cleanup.log，
#       绝不写 sign-*.log——root 预创建当日签到日志（umask 077）会让 run.sh 全部重定向
#       失败、signin 静默不执行（2026-08-17 事故：当天自动签到整体丢失）。
# 环境变量：YIBAN_DATA_DIR（默认 /var/log/yiban）、YIBAN_RETENTION_DAYS（默认 365）、
#           YIBAN_SNAPSHOT_RETENTION_DAYS（默认 7）
umask 077

DATA_DIR="${YIBAN_DATA_DIR:-/var/log/yiban}"
RETENTION_DAYS="${YIBAN_RETENTION_DAYS:-365}"
SNAPSHOT_RETENTION_DAYS="${YIBAN_SNAPSHOT_RETENTION_DAYS:-7}"

log_file="$DATA_DIR/cleanup.log"

cutoff="$(date -d "$RETENTION_DAYS days ago" +%Y-%m-%d)"
snap_cutoff="$(date -d "$SNAPSHOT_RETENTION_DAYS days ago" +%Y-%m-%d)"
if [ -z "$cutoff" ] || [ -z "$snap_cutoff" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 无法计算保留截止日期，跳过" >> "$log_file"
    exit 1
fi

removed=0
clean_by_date() {
    # 用法：clean_by_date <glob> <前缀> <后缀> <截止日期>；按文件名日期删除更早的文件
    # 用 nullglob + 数组保存匹配结果，避免 glob 因路径含空格被拆词。
    local glob="$1" prefix="$2" suffix="$3" lim="$4"
    local -a files=()
    local IFS=$'\n'
    local f d
    shopt -s nullglob
    files=($glob)
    shopt -u nullglob
    for f in "${files[@]}"; do
        [ -e "$f" ] || continue
        d="${f##*/}"            # 去目录
        d="${d#"$prefix"}"      # 去前缀
        d="${d%"$suffix"}"      # 去后缀 → YYYY-MM-DD
        if [[ "$d" < "$lim" ]]; then
            rm -f "$f"
            removed=$((removed + 1))
        fi
    done
}

clean_by_date "$DATA_DIR/sign-????-??-??.log" "sign-" ".log" "$cutoff"
clean_by_date "$DATA_DIR/sign-state-????-??-??.json" "sign-state-" ".json" "$cutoff"
clean_by_date "$DATA_DIR/sched-snapshot-????-??-??.json" "sched-snapshot-" ".json" "$snap_cutoff"

# cred-state.json：空内容（{} 或空文件）时删除；有暂停记录则保留
if [ -f "$DATA_DIR/cred-state.json" ]; then
    content="$(tr -d '[:space:]' < "$DATA_DIR/cred-state.json" 2>/dev/null)"
    if [ -z "$content" ] || [ "$content" = "{}" ]; then
        rm -f "$DATA_DIR/cred-state.json"
        removed=$((removed + 1))
    fi
fi

if [ "$removed" -gt 0 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 已清理 $removed 个过期文件（日志/状态保留 $RETENTION_DAYS 天，快照保留 $SNAPSHOT_RETENTION_DAYS 天，截止 $cutoff）" \
        >> "$log_file"
fi
exit 0
