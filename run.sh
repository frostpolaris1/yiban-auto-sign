#!/bin/bash
# 易班自动签到运行脚本
cd /opt/yiban-auto-sign

# 加载环境变量
export $(cat .env | xargs)

# 记录脚本开始执行
echo "[$(date '+%Y-%m-%d %H:%M:%S')] === run.sh 开始执行 ===" >> /var/log/yiban/sign.log
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 工作目录: $(pwd)" >> /var/log/yiban/sign.log
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Python版本: $(python3 --version 2>&1)" >> /var/log/yiban/sign.log

# 执行签到脚本
/usr/bin/python3 scripts/signin.py >> /var/log/yiban/sign.log 2>&1
EXIT_CODE=$?

# 记录脚本执行结果
echo "[$(date '+%Y-%m-%d %H:%M:%S')] === run.sh 执行完成，退出码: $EXIT_CODE ===" >> /var/log/yiban/sign.log
echo "" >> /var/log/yiban/sign.log

exit $EXIT_CODE
