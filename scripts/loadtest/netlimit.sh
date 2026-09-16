#!/bin/bash
# 测试机出口限速开关（用于「带宽是否是签到数量瓶颈」实验）
# 用法: netlimit.sh on [rate] | off | status
#
# 档位依据（**实测，不是估值**，2026-09-16）：
#   生产机出向上限 = 3.8 Mbps —— 从靶机下载生产站 434KB 静态文件测三点：
#     单连接 2.05 Mbps / 8 并发 3.76 Mbps / 16 并发 3.72 Mbps
#     → 8 与 16 并发收敛到同一水平，说明这是**带宽上限**（不是并发受限）。
#   本脚本默认 4mbit（= 500 KB/s）：iperf3 实测接收端 3.86 Mbps，
#     与生产机的 3.76 Mbps 相差 3% —— 即默认档就是生产机出口的忠实模拟。
#   要改档位：netlimit.sh on 8mbit（带宽翻倍档，用于验证"是不是带宽卡住"）
#
#   方向：仅出口（eth0 root qdisc）。实验里签到进程在测试机、假易班在靶机，
#   请求走出口；**响应走入口不受限**（实测入口 107 Mbps 未被限速影响）
#   —— 若实验需要同时限入口，再加 IFB 设备（届时单独做）。
#   burst 必须 >= 带宽 × RTT（4Mbit × 100ms ≈ 50KB），否则 TCP 被自己的桶压死、把
#   数据污染成"假带宽瓶颈"（初版误设 32kbit=4KB 已被实测暴露）。
set -e
DEV=eth0
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
