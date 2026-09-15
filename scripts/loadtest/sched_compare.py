#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""调度算法对比基准（离散事件仿真，测试机/沙箱专用）。

对比两种调度策略在处理「用户自选时间片 + 随机排序签到」时的表现：
  简易并发（simple）：账号清单随机打乱后进共享池，K 个执行器按 FIFO 顺序轮取，
                     完全不参考用户自选时间片。
  改良版调度（improved）：按用户自选片“节点截止期（slot_hi）升序”轮取（EDF），
                     并在片窗起点前留短暂前导（lead）以便贴近用户所选时刻。

建模沿用仓库压测口径：
  * 有效窗口 = PROD_WINDOW_SEC = 4680s（80 分钟窗口 - 首尾各 60s）；
  * 单账号耗时 s ≈ 对数正态，中位 = avg_attempt（压测实测定档 3s）；
  * K 个执行器（= Celery prefork / 并发进程数），服务时间可变；
  * 每个用户有一个自选 5 分钟片 [lo, hi)；
  * 可选 fail-rate：部分账号瞬时失败需重跑（服务时间翻倍），用于考察截止鲁棒性。

输出：完成率、makespan（效率）、自选片命中率、截止安全率，并跨多随机种取
均值±标准差（稳定性）。不改动生产代码，仅作调度启发式对比。

用法：
  python3 sched_compare.py [--n 8000] [--window 4680] [--k-list 2,4,6,8,10,12]
      [--avg 3.0] [--lead 45] [--gap 0] [--nb 16] [--seeds 5] [--fail-rate 0.0]
"""

from __future__ import annotations

import argparse
import heapq
import math
import random
import statistics


def gen_jobs(n, window, nb, avg, rng):
    """生成本轮用户：每人一个自选 5 分钟片 + 随机排序占位 + 单账号耗时。"""
    slot_w = window / nb
    jobs = []
    for i in range(n):
        slot = rng.randrange(nb)
        lo = slot * slot_w
        hi = min((slot + 1) * slot_w, window)
        # 对数正态：中位≈avg，取值大致覆盖 0.5~2×avg
        base = avg * math.exp(rng.gauss(0, 0.30))
        s = max(0.5, base)
        jobs.append({"id": i, "lo": lo, "hi": hi, "s": s})
    return jobs


def run_policy(jobs, k, window, policy, lead, fail_rate, rng, gap):
    """对给定策略跑一轮离散事件仿真，返回统计。

    共享池 = 一个有序的账号拉取顺序；K 个执行器轮流从池中按顺序取号，
    用「下次空闲最早」的堆来推进时间。start 受全局最小间隔 gap 约束。
    """
    order = list(range(len(jobs)))
    if policy == "improved":
        # EDF：按自选片截止期升序（其次按片起点），尽量贴近用户所选时刻
        order.sort(key=lambda i: (jobs[i]["hi"], jobs[i]["lo"]))
    else:
        # 简易：清单随机打乱（代表随机排序签到），完全不看自选片
        rng.shuffle(order)

    lane = [(0.0, l) for l in range(k)]
    heapq.heapify(lane)
    global_last = 0.0  # 全局最小间隔：相邻账号执行起点至少隔 gap
    nxt = 0
    completed = pref_hit = deadline_ok = 0
    last_end = 0.0

    while nxt < len(order):
        free_at, lane_id = heapq.heappop(lane)
        ji = order[nxt]
        nxt += 1
        j = jobs[ji]
        s = j["s"] * (2.0 if (fail_rate > 0 and rng.random() < fail_rate) else 1.0)
        if policy == "improved":
            start = max(free_at, global_last + gap, j["lo"] - lead)
        else:
            start = max(free_at, global_last + gap)
        if start + s > window:  # 窗口内无法完成 → 该执行器停止
            heapq.heappush(lane, (free_at, lane_id))
            continue
        end = start + s
        completed += 1
        if start < j["hi"] and end > j["lo"]:
            pref_hit += 1
        if start <= j["hi"] + 60:
            deadline_ok += 1
        global_last = start
        last_end = max(last_end, end)
        heapq.heappush(lane, (end, lane_id))

    return {
        "completed": completed,
        "makespan": last_end,
        "pref_hit": pref_hit,
        "deadline_ok": deadline_ok,
    }


def agg(stats_list):
    """跨随机种子聚合：均值±标准差；样本为 1 时仅给均值。"""
    out = {}
    for key in ("completed", "makespan", "pref_hit", "deadline_ok"):
        vals = [s[key] for s in stats_list]
        mu = statistics.mean(vals)
        sd = statistics.pstdev(vals) if len(vals) > 1 else 0.0
        out[key] = (mu, sd)
    return out


def pct(x, n):
    return f"{100.0 * x / n:.1f}%"


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="sched_compare.py",
        description="调度算法 DES 对比：简易并发 vs 改良版调度（自选片 EDF）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--n", type=int, default=8000, help="用户（账号）总数")
    ap.add_argument("--window", type=int, default=4680,
                    help="有效签到窗口（秒），PROD_WINDOW_SEC 基线")
    ap.add_argument("--k-list", default="2,4,6,8,10,12", help="执行器数量（逗号分隔）")
    ap.add_argument("--avg", type=float, default=3.0, help="单账号耗时中位（秒）")
    ap.add_argument("--lead", type=float, default=45.0,
                    help="自选片起点前允许的前导秒数（改良版靠近用户所选时刻用）")
    ap.add_argument("--gap", type=float, default=0.0, help="全局最小账号间隔（秒）")
    ap.add_argument("--nb", type=int, default=16, help="自选片分块数（5 分钟片 ≈ 窗口/300）")
    ap.add_argument("--seeds", type=int, default=5, help="随机种子数（求稳定性）")
    ap.add_argument("--fail-rate", type=float, default=0.0,
                    help="瞬时失败概率（>0 时服务时间翻倍重跑，考察截止鲁棒性）")
    args = ap.parse_args(argv)

    K_LIST = [int(x) for x in args.k_list.split(",") if x.strip()]
    print(f"=== 调度对比 DES: N={args.n} 窗口={args.window}s K={K_LIST} "
          f"avg={args.avg}s lead={args.lead}s gap={args.gap}s nb={args.nb} "
          f"seeds={args.seeds} fail={args.fail_rate} ===")
    print("\n| K | 策略 | 完成数 | 完成率  | makespan(效率)s | 自选片命中  | 截止安全 |")
    print("|---|------|--------|---------|----------------|-------------|----------|")
    for k in K_LIST:
        for policy in ("simple", "improved"):
            acc = []
            for seed in range(args.seeds):
                rng = random.Random(seed)
                jobs = gen_jobs(args.n, args.window, args.nb, args.avg, rng)
                acc.append(run_policy(jobs, k, args.window, policy,
                                      args.lead, args.fail_rate, rng, args.gap))
            a = agg(acc)
            comp_mu, comp_sd = a["completed"]
            mp_mu, _ = a["makespan"]
            hit_mu, hit_sd = a["pref_hit"]
            ddl_mu, _ = a["deadline_ok"]
            comp_s = f"{pct(comp_mu, args.n)}"
            if len(acc) > 1:
                comp_s += f" ±{pct(comp_sd, args.n)}"
                hit = f"{pct(hit_mu, args.n)} ±{pct(hit_sd, args.n)}"
            else:
                hit = f"{pct(hit_mu, args.n)}"
            print(f"| {k} | {policy:<11} | {comp_mu:>6.0f} | {comp_s:<9} | "
                  f"{mp_mu:>10.0f}s | {hit:<12} | {pct(ddl_mu, args.n):<8} |")
    print("\n说明:")
    print("  完成率=窗口内完成占比；自选片命中=执行区间与用户所选片有重叠；"
          "截止安全=启动不晚于自选片终点+60s。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())