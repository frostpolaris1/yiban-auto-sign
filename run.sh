#!/bin/bash
umask 077
# 易班自动签到运行脚本
cd /opt/yiban-auto-sign

# 日志按天分文件（2026-08-16）：sign-YYYY-MM-DD.log，web 端按日期直接读取对应文件；
# 保留 YIBAN_LOG_FILE 配置的目录语义（默认 /var/log/yiban）。
LOG_FILE="${YIBAN_LOG_FILE:-/var/log/yiban/sign.log}"
LOG_FILE="$(dirname "$LOG_FILE")/sign-$(date +%Y-%m-%d).log"

# 单实例锁：自动错峰模式下 06:31 进程可能 sleep 等待时间点，
# 防止 07:10 的 cron 并发启动第二个进程（重复签到/并发竞争）
exec 9>/tmp/yiban-sign.lock
flock -n 9 || {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] === 已有签到进程在运行，本次跳过 ===" >> "$LOG_FILE"
    exit 0
}

# 加载环境变量（set -a 确保变量导出到子进程；source 语义安全，
# 替代易受特殊字符/空格影响的 `export $(cat .env | xargs)`）
set -a
. /opt/yiban-auto-sign/.env
set +a

# 状态文件：记录今天的签到结果，避免重复执行
STATUS_FILE="/var/log/yiban/sign-status-$(date +%Y-%m-%d).txt"

# 检查今天是否已经签到成功
if [ -f "$STATUS_FILE" ]; then
    STATUS=$(cat "$STATUS_FILE")
    if [ "$STATUS" = "SUCCESS" ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 今天已签到成功，跳过执行 ===" >> "$LOG_FILE"
        exit 0
    fi
fi

# 记录脚本开始执行
echo "[$(date '+%Y-%m-%d %H:%M:%S')] === run.sh 开始执行 ===" >> "$LOG_FILE"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 工作目录: $(pwd)" >> "$LOG_FILE"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Python版本: $(python3 --version 2>&1)" >> "$LOG_FILE"

# 执行签到脚本（调度 v2：总超时防失控，与 flock 防并发互补；
# timeout 杀掉后退出码 124，不写 SUCCESS——符合现状语义）
timeout "${YIBAN_RUN_TIMEOUT_SEC:-1800}" /usr/bin/python3 scripts/signin.py >> "$LOG_FILE" 2>&1
EXIT_CODE=$?

# 状态文件只在"确实执行过签到"时写 SUCCESS（退出码 0）：
# 全部 skip（无实际执行，退出码 2）写 SKIPPED，避免把"没签到"记录成成功
# 从而吞掉后续任务；其他失败（退出码 1）不写状态文件
if [ $EXIT_CODE -eq 0 ]; then
    echo "SUCCESS" > "$STATUS_FILE"
elif [ $EXIT_CODE -eq 2 ]; then
    echo "SKIPPED" > "$STATUS_FILE"
fi

# 记录脚本执行结果
echo "[$(date '+%Y-%m-%d %H:%M:%S')] === run.sh 执行完成，退出码: $EXIT_CODE ===" >> "$LOG_FILE"
echo "" >> "$LOG_FILE"

exit $EXIT_CODE
