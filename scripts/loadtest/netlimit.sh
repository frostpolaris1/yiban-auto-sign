#!/bin/bash
# 测试机出口限速开关（用于「带宽是否是签到数量瓶颈」实验，2026-09-16）
# 用法: netlimit.sh on [rate]   | off | status
#   默认 4mbit（= 0.5 MB/s，约等于较差的家庭上行；bandwidth 实验的窄链路档）
#   burst 必须 >= 带宽 x RTT（4Mbit x 100ms = 50KB），否则 TCP 被自己的桶压死：
#   实测 burst 32kbit(4KB) 时国际线路只有 233KB/s，改 64kb(64KB) 后见下测
#   方向: 仅出口（eth0 root qdisc）。实验时签到进程在测试机、假易班在靶机，
#         请求与响应都很小，出口是主要约束；如需同时限入口再用 IFB（实验需要时再加）。
set -e
DEV=eth0
case "$1" in
  on)
    RATE="${2:-4mbit}"
    tc qdisc replace dev $DEV root tbf rate $RATE burst 64kb latency 400ms
    echo "已限速 $DEV → $RATE（tbf, burst 64kb, latency 400ms）"
    ;;
  off)
    tc qdisc del dev $DEV root 2>/dev/null || true
    echo "已恢复 $DEV 默认队列（fq_codel）"
    ;;
  status)
    tc qdisc show dev $DEV
    tc -s qdisc show dev $DEV | sed -n '1,12p'
    ;;
  *)
    echo "用法: $0 on [rate] | off | status" >&2; exit 2 ;;
esac
