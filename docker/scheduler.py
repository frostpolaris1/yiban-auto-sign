#!/usr/bin/env python3
"""容器内签到调度：复刻宿主 cron 的语义。

- 06:31 首签 / 07:10 补签（当天已 SUCCESS 则跳过）
- 每日 03:00 清理 /data/logs 下 365 天前的按天日志

数据/配置路径由 compose 注入的 YIBAN_* 环境变量决定，
signin.py 作为子进程启动并继承这些变量（与现有 run.sh 的调用方式对齐）。
"""
import os
import subprocess
import time
from datetime import datetime, timedelta

STATEDIR = os.environ.get("YIBAN_STATE_DIR", "/data/state")
LOGDIR = os.path.dirname(os.environ.get("YIBAN_LOG_FILE", "/data/logs/sign.log"))


def _state_file():
    return os.path.join(STATEDIR, f"sign-status-{datetime.now():%Y-%m-%d}.txt")


def _signed_today():
    """对齐 run.sh 语义：当天状态文件为 SUCCESS 即视为已签到，跳过。"""
    try:
        with open(_state_file(), encoding="utf-8") as fh:
            return fh.read().strip() == "SUCCESS"
    except OSError:
        return False


def _cleanup_logs():
    """删除 365 天前的按天日志（sign-YYYY-MM-DD.log），对齐 scripts/yiban-cleanup.sh。"""
    cutoff = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
    if not os.path.isdir(LOGDIR):
        return
    for name in os.listdir(LOGDIR):
        if name.startswith("sign-") and name.endswith(".log"):
            key = name[len("sign-"):-len(".log")]
            if key < cutoff:
                try:
                    os.remove(os.path.join(LOGDIR, name))
                except OSError:
                    pass


# 首签 / 补签 时间点（分钟级匹配，秒为 0 触发一次）
FIRST, SECOND = (6, 31), (7, 10)
_last_clean = None

while True:
    now = datetime.now()
    hm = (now.hour, now.minute)
    if hm in (FIRST, SECOND) and now.second == 0 and not _signed_today():
        # 与 run.sh 唯一实质差异：容器内无需 flock/宿主绝对路径，状态文件已防重
        subprocess.run(["python3", "scripts/signin.py"], cwd="/app")
    if now.hour == 3 and _last_clean != now.date():
        _cleanup_logs()
        _last_clean = now.date()
    time.sleep(1)