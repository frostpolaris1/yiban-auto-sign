#!/bin/bash
# 易班自动签到运行脚本
cd /opt/yiban-auto-sign
export $(cat .env | xargs)
/usr/bin/python3 scripts/signin.py >> /var/log/yiban/sign.log 2>&1
