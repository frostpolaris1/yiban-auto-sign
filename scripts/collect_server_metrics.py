# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""服务器性能采集脚本（P0）：把当前服务器指标写入 server_metrics 表。

用法：
    python3 scripts/collect_server_metrics.py [--db yiban.db] [--env .env]

建议由 cron/systemd 每 5~10 秒调用一次。
依赖：psutil（已加入 requirements.txt）。
"""
import argparse
import datetime
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db


def main():
    parser = argparse.ArgumentParser(description="采集服务器性能指标并写入 server_metrics")
    parser.add_argument("--db", default=None, help="yiban.db 路径（默认环境变量/相对路径）")
    parser.add_argument("--env", default=None, help=".env 路径（默认项目根 .env）")
    args = parser.parse_args()

    try:
        import psutil
    except ImportError:
        print("psutil 未安装，请先安装：pip install psutil")
        sys.exit(1)

    if args.db:
        os.environ["YIBAN_DB_FILE"] = args.db

    # CPU：短暂采样一次，避免首次调用返回 0
    cpu = psutil.cpu_percent(interval=0.1)
    mem_pct = psutil.virtual_memory().percent
    disk_pct = psutil.disk_usage("/").percent

    # 网络吞吐：1 秒差值，单位 KB/s
    net1 = psutil.net_io_counters()
    time.sleep(1)
    net2 = psutil.net_io_counters()
    net_in = (net2.bytes_recv - net1.bytes_recv) / 1024.0
    net_out = (net2.bytes_sent - net1.bytes_sent) / 1024.0

    # 系统负载：Unix 可用；Windows 返回 0
    getloadavg = getattr(os, "getloadavg", None)
    if getloadavg is not None:
        load1, load5, load15 = getloadavg()
    else:
        load1 = load5 = load15 = 0.0

    proc_count = len(psutil.pids())
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    db.init_db(args.db, env_file=args.env)
    db.add_server_metric(
        ts,
        cpu=cpu,
        mem_pct=mem_pct,
        disk_pct=disk_pct,
        net_in=net_in,
        net_out=net_out,
        load1=load1,
        load5=load5,
        load15=load15,
        proc_count=proc_count,
    )
    print(
        f"server_metrics 写入成功: ts={ts} cpu={cpu:.1f}% mem={mem_pct:.1f}% "
        f"disk={disk_pct:.1f}% net_in={net_in:.1f}KB/s net_out={net_out:.1f}KB/s "
        f"load={load1:.2f}/{load5:.2f}/{load15:.2f} proc={proc_count}"
    )


if __name__ == "__main__":
    main()
