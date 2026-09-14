#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多进程并发探测：同时拉起 K 个 signin 进程，找三类资源饱和点（测试机用）。

设计要点（避免同账号重复签到）：
  * 账号从库里读出后**互不重叠**地切成 K 份，每份交给一个进程用 ``--only`` 跑；
  * 每个进程独立 ``YIBAN_STATE_DIR`` / ``YIBAN_LOG_FILE``，因此进程级签到锁
    （``<STATE_DIR>/signin-run.lock``）互不干扰、状态文件不互相覆盖。

每档 K 采集：
  * 整机 CPU（/proc/stat，0~100% = 全部核心；2 vCPU 饱和点即接近 100%）
    与引擎自身 CPU（各子进程 utime+stime 之和，按单核百分比另列）；
  * 整机 RSS（各子进程 RSS 之和）与单进程 RSS 中位数、峰值可用内存；
  * SQLite 写并发：日志中的 locked/busy 等错误与重试；以及独立 scratch 库上
    BEGIN IMMEDIATE 单写者排队延迟（p50/p95/max）；
  * 各进程墙钟与「相对单进程（K=1）的单账号墙钟劣化倍数」；
  * 总吞吐（账号/小时）与账号完成数。

输出 JSON + CSV，并给出三类饱和点与首个瓶颈。

用法：
  python3 concurrency_probe.py --repo /opt/repo \\
      --env /opt/yiban-loadtest/test.env --db /opt/yiban-loadtest/data/yiban.db \\
      --ca /opt/yiban-loadtest/ca/ca.pem --mock-log /opt/yiban-loadtest/logs/mock.jsonl \\
      --k-list 1,2,4,8,12,16,20,24 --per-proc 8 --gap 0 \\
      --outdir /opt/yiban-loadtest/results
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import json
import os
import shutil
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime

try:  # resource 仅 POSIX 可用；Windows 本地只能做纯函数单测
    import resource
except ImportError:  # pragma: no cover - 平台差异
    resource = None  # type: ignore[assignment]

NCROP = os.cpu_count() or 1

LOCK_KEYWORDS = ("database is locked", "database table is locked", "database is busy",
                 "operationalerror", "保存会话缓存失败", "写入失败", "disk i/o error")

CSV_FIELDS = [
    "K", "per_proc", "accounts_total", "wall_s",
    "machine_cpu_pct", "engine_cpu_onecore_pct", "engine_cpu_machine_pct", "mock_cpu_onecore_pct",
    "peak_total_rss_mb", "median_proc_rss_mb", "min_available_mb",
    "db_delta_kb", "lock_errors", "oom_killed",
    "proc_wall_avg_s", "proc_wall_max_s", "per_acct_wall_s",
    "degradation_x", "throughput_acct_per_h", "completed", "success", "failed",
    "max_inflight", "db_write_p50_ms", "db_write_p95_ms", "db_write_max_ms",
    "db_write_errors",
]


def log(msg, logfile=None):
    line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    if logfile:
        try:
            with open(logfile, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 纯函数（便于单测）
# ---------------------------------------------------------------------------
def partition_slices(phones, k, per_proc):
    """把账号切成 K 份互不重叠的切片，每份至多 per_proc 个。

    每个进程拿到的账号数一致（末份不足时取到多少算多少），保证各进程工作量可比。
    """
    out = []
    for i in range(k):
        start = i * per_proc
        chunk = phones[start:start + per_proc]
        if not chunk:
            break
        out.append(chunk)
    return out


def scan_lock_errors(text):
    """统计日志中 SQLite 锁/写失败类关键词的行数与命中词。"""
    hits = {}
    low = text.lower()
    for kw in LOCK_KEYWORDS:
        c = low.count(kw)
        if c:
            hits[kw] = c
    return sum(hits.values()), hits


def classify_bottlenecks(rows, total_mem_mb, mem_reserve_mb):
    """按序找三类饱和点：CPU / 内存 / DB 写。返回 dict。

    判定口径：
      CPU：整机 CPU >= 90%（2 vCPU 即约 1.8 核）；或引擎单核 CPU >= 180%。
      内存：峰值总 RSS + 基线占用后可用内存低于 reserve；或子进程被 OOM(-9)。
      DB 写：出现 locked/busy 类错误；或 scratch 写 p95 >= 500ms（相对单进程显著排队）。
    """
    cpu_k = mem_k = db_k = None
    for r in sorted(rows, key=lambda x: x["K"]):
        if cpu_k is None and (r["machine_cpu_pct"] >= 90.0 or r["engine_cpu_onecore_pct"] >= 180.0):
            cpu_k = r["K"]
        if mem_k is None and (r.get("oom_killed") or r.get("mem_abort")
                              or r["min_available_mb"] < mem_reserve_mb):
            mem_k = r["K"]
        if db_k is None and (r.get("lock_errors", 0) > 0 or (r.get("db_write_p95_ms") or 0) >= 500.0):
            db_k = r["K"]
    first = None
    candidates = [(cpu_k, "CPU"), (mem_k, "内存"), (db_k, "DB写")]
    for k, name in sorted([c for c in candidates if c[0] is not None]):
        first = (k, name)
        break
    return {"cpu_sat_k": cpu_k, "mem_sat_k": mem_k, "db_sat_k": db_k,
            "first_bottleneck": first}


def percentile(sv, p):
    if not sv:
        return None
    if len(sv) == 1:
        return sv[0]
    k = (len(sv) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(sv) - 1)
    return sv[lo] + (sv[hi] - sv[lo]) * (k - lo)


# ---------------------------------------------------------------------------
# 系统采样
# ---------------------------------------------------------------------------
def read_meminfo():
    """返回 (MemTotal_MB, MemAvailable_MB)。"""
    total = avail = 0
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for ln in f:
                if ln.startswith("MemTotal:"):
                    total = int(ln.split()[1]) / 1024.0
                elif ln.startswith("MemAvailable:"):
                    avail = int(ln.split()[1]) / 1024.0
    except OSError:
        pass
    return total, avail


def read_stat_cpu():
    """读 /proc/stat 聚合行，返回 (total_jiffies, idle_jiffies)。"""
    try:
        with open("/proc/stat", encoding="utf-8") as f:
            parts = f.readline().split()
        vals = [int(x) for x in parts[1:]]
        total = sum(vals)
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
        return total, idle
    except (OSError, IndexError, ValueError):
        return None, None


def read_proc_status(pid):
    """返回 (rss_kb, hwm_kb, threads) 或 None。"""
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as f:
            rss = hwm = threads = 0
            for ln in f:
                if ln.startswith("VmRSS:"):
                    rss = int(ln.split()[1])
                elif ln.startswith("VmHWM:"):
                    hwm = int(ln.split()[1])
                elif ln.startswith("Threads:"):
                    threads = int(ln.split()[1])
            return rss, hwm, threads
    except (OSError, IndexError, ValueError):
        return None


def read_proc_cpu(pid, hz):
    """返回进程累计 CPU 秒数（POSIX）；进程不存在/非 Linux 返回 None。"""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            fields = f.read().split()
        return (int(fields[13]) + int(fields[14])) / hz
    except (OSError, IndexError, ValueError):
        return None


# ---------------------------------------------------------------------------
# DB 写串行化微基准（独立 scratch 库，避免扰动被测库）
# ---------------------------------------------------------------------------
def db_write_microbench(path, concurrency, per_thread=20, timeout=15.0):
    """K 个线程各持独立连接做 BEGIN IMMEDIATE + INSERT，测提交排队延迟。"""
    if os.path.exists(path):
        with contextlib.suppress(OSError):
            os.remove(path)
    for suf in ("-wal", "-shm"):
        with contextlib.suppress(OSError):
            os.remove(path + suf)
    setup = sqlite3.connect(path)
    setup.execute("PRAGMA journal_mode=WAL")
    setup.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, v TEXT)")
    setup.commit()
    setup.close()

    lat = []
    errors = []
    lock = threading.Lock()

    def worker(wid):
        try:
            conn = sqlite3.connect(path, timeout=timeout)
            conn.execute("PRAGMA busy_timeout=%d" % int(timeout * 1000))
            for i in range(per_thread):
                t = time.perf_counter()
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("INSERT INTO t (v) VALUES (?)", (f"{wid}-{i}",))
                conn.commit()
                ms = (time.perf_counter() - t) * 1000.0
                with lock:
                    lat.append(ms)
            conn.close()
        except Exception as e:
            with lock:
                errors.append(f"{type(e).__name__}: {e}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(concurrency)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - t0
    sv = sorted(lat)
    return {
        "concurrency": concurrency,
        "inserts": len(lat),
        "elapsed_s": round(elapsed, 3),
        "p50_ms": round(percentile(sv, 0.5), 3) if sv else None,
        "p95_ms": round(percentile(sv, 0.95), 3) if sv else None,
        "max_ms": round(sv[-1], 3) if sv else None,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# 环境装配
# ---------------------------------------------------------------------------
def load_env_file(path):
    env = {}
    try:
        with open(path, encoding="utf-8-sig") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                    v = v[1:-1]
                env[k] = v
    except OSError:
        pass
    return env


def clear_session_cache(db_path):
    """清空会话缓存：保证每档每个账号都走完整登录链（口径一致、写入量可比）。"""
    try:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("DELETE FROM session_cache")
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass


def load_phones(db_path, limit):
    """读未删除且非待审/拒绝的账号手机号（按 id 顺序，保证切片稳定）。"""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT phone FROM accounts WHERE deleted=0 "
            "AND (status IS NULL OR status NOT IN ('pending','rejected')) ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows][:limit]


def build_proc_env(env_file, repo, ca, extra):
    env = dict(os.environ)
    env.update(load_env_file(env_file))
    env["REQUESTS_CA_BUNDLE"] = ca
    env["SSL_CERT_FILE"] = ca
    env["CURL_CA_BUNDLE"] = ca
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = os.pathsep.join(
        [os.path.join(repo, "scripts"), repo, env.get("PYTHONPATH", "")]
    ).strip(os.pathsep)
    env.update(extra)
    return env


def read_state_summary(state_dir):
    counts = {}
    try:
        names = [n for n in os.listdir(state_dir) if n.startswith("sign-state-")]
    except OSError:
        return counts
    for n in names:
        try:
            with open(os.path.join(state_dir, n), encoding="utf-8-sig") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, dict):
                    st = str(v.get("status", "")).strip()
                    counts[st] = counts.get(st, 0) + 1
    return counts


def parse_mock_window(log_path, offset=0):
    """从 mock JSONL 的 offset 起统计 (最大并发, 记录数)；空/缺失返回 (0, 0)。"""
    max_infl = n = 0
    try:
        size = os.path.getsize(log_path)
    except OSError:
        return 0, 0
    if offset > size:
        offset = 0
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                n += 1
                max_infl = max(max_infl, int(rec.get("inflight", 1) or 1))
    except OSError:
        return 0, 0
    return max_infl, n


# ---------------------------------------------------------------------------
# 单档 K 运行
# ---------------------------------------------------------------------------
def run_k(args, k, slices, run_dir):
    repo = os.path.abspath(args.repo)
    py = args.python or sys.executable
    if os.path.isdir(run_dir):
        shutil.rmtree(run_dir, ignore_errors=True)  # 清掉同档旧状态，防统计污染
    os.makedirs(run_dir, exist_ok=True)
    clear_session_cache(args.db)  # 每档起点一致：全量登录链 + 等量 session_cache 写
    total_mem_mb, avail0 = read_meminfo()

    # 子进程 CPU/内存由 RUSAGE_CHILDREN 差分获得（探针自身不 fork 其它子进程）
    r0 = resource.getrusage(resource.RUSAGE_CHILDREN) if resource else None
    mock_mark = _file_size(args.mock_log) if args.mock_log else 0
    hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    mock_pid = getattr(args, "mock_pid", 0) or 0
    mock_cpu0 = read_proc_cpu(mock_pid, hz) if mock_pid else None

    procs = []
    for idx, chunk in enumerate(slices):
        state_dir = os.path.join(run_dir, f"state_p{idx}")
        os.makedirs(state_dir, exist_ok=True)
        log_file = os.path.join(run_dir, f"sign_p{idx}.log")
        extra = {
            "YIBAN_STATE_DIR": state_dir,
            "YIBAN_LOG_FILE": log_file,
            "YIBAN_ACCOUNT_GAP_MAX": str(args.gap),
            "YIBAN_START_DELAY_MAX": "0",
            "YIBAN_GLOBAL_PAUSE": "0",
            "YIBAN_NOTIFY_URL": "",
            "YIBAN_PROBE_ENABLE": "0",
            "YIBAN_SECOND_RUN": "0",
            "YIBAN_ENV_FILE": os.path.abspath(args.env),
            "YIBAN_DB_FILE": os.path.abspath(args.db),
        }
        env = build_proc_env(args.env, repo, args.ca, extra)
        cmd = [py, os.path.join(repo, "scripts", "signin.py"), "--only", ",".join(chunk)]
        lf = open(log_file, "w", encoding="utf-8")
        t_start = time.monotonic()
        p = subprocess.Popen(cmd, cwd=repo, env=env,
                             stdout=lf, stderr=subprocess.STDOUT)
        procs.append({"pop": p, "lf": lf, "pid": p.pid, "t0": t_start,
                      "log": log_file, "state_dir": state_dir,
                      "n": len(chunk), "t_end": None, "rc": None})

    # 采样循环：整机 CPU（/proc/stat 差分）、子进程 RSS、可用内存
    t0 = time.monotonic()
    cpu_prev = read_stat_cpu()
    cpu_busy = cpu_total = 0
    peak_rss_kb = 0
    min_avail = avail0
    per_pid_rss = {pr["pid"]: [] for pr in procs}
    oom = False
    mem_abort = False
    hard_floor = max(80.0, args.mem_reserve_mb / 2.0)  # 低于此值立即杀子进程防 OOM
    while True:
        alive = False
        for pr in procs:
            rc = pr["pop"].poll()
            if rc is not None and pr["t_end"] is None:
                pr["t_end"] = time.monotonic()
                pr["rc"] = rc
                if rc == -9:
                    oom = True
            if rc is None:
                alive = True
                st = read_proc_status(pr["pid"])
                if st:
                    per_pid_rss[pr["pid"]].append(st[0])
        cur = read_stat_cpu()
        if cur[0] is not None and cpu_prev[0] is not None:
            d_total = max(0, cur[0] - cpu_prev[0])
            d_idle = max(0, cur[1] - cpu_prev[1])
            cpu_busy += d_total - d_idle
            cpu_total += d_total
        cpu_prev = cur
        rss_now = sum(s[-1] for s in per_pid_rss.values() if s)
        peak_rss_kb = max(peak_rss_kb, rss_now)
        _, avail = read_meminfo()
        min_avail = min(min_avail, avail)
        if not alive:
            break
        if avail < hard_floor:
            mem_abort = True
            for pr in procs:
                if pr["pop"].poll() is None:
                    pr["pop"].kill()
                    pr["pop"].wait()
                if pr["t_end"] is None:
                    pr["t_end"] = time.monotonic()
                    pr["rc"] = pr["pop"].returncode
            break
        if time.monotonic() - t0 > args.timeout:
            for pr in procs:
                if pr["pop"].poll() is None:
                    pr["pop"].kill()
                    pr["pop"].wait()
                if pr["t_end"] is None:
                    pr["t_end"] = time.monotonic()
                    pr["rc"] = pr["pop"].returncode
            break
        time.sleep(args.interval)

    r1 = resource.getrusage(resource.RUSAGE_CHILDREN) if resource else None
    if r0 is not None and r1 is not None:
        engine_cpu_s = (r1.ru_utime - r0.ru_utime) + (r1.ru_stime - r0.ru_stime)
    else:
        engine_cpu_s = 0.0
    mock_cpu1 = read_proc_cpu(mock_pid, hz) if mock_pid else None
    mock_cpu_s = (mock_cpu1 - mock_cpu0) if (mock_cpu0 is not None and mock_cpu1 is not None) else 0.0

    walls = []
    all_rss = []
    for pr in procs:
        with contextlib.suppress(OSError):
            pr["lf"].close()
        if pr["t_end"] is None:
            pr["t_end"] = time.monotonic()
        walls.append(pr["t_end"] - pr["t0"])
        all_rss.extend(per_pid_rss.get(pr["pid"], []))
    wall = max(walls) if walls else (time.monotonic() - t0)

    machine_cpu_pct = (cpu_busy / cpu_total * 100.0) if cpu_total > 0 else 0.0

    # mock 在响应 flush 后才写 JSONL，稍等片刻再读，避免漏掉最后几条
    if args.mock_log:
        time.sleep(0.5)
    max_inflight, mock_records = (parse_mock_window(args.mock_log, mock_mark)
                                  if args.mock_log else (0, 0))

    lock_errors = 0
    lock_hits = {}
    for pr in procs:
        try:
            with open(pr["log"], encoding="utf-8", errors="replace") as f:
                c, hits = scan_lock_errors(f.read())
        except OSError:
            c, hits = 0, {}
        lock_errors += c
        for kk, vv in hits.items():
            lock_hits[kk] = lock_hits.get(kk, 0) + vv

    counts = {}
    for pr in procs:
        for kk, vv in read_state_summary(pr["state_dir"]).items():
            counts[kk] = counts.get(kk, 0) + vv
    success = counts.get("success", 0) + counts.get("already", 0)
    failed = sum(v for kk, v in counts.items()
                 if kk not in ("success", "already", "no_task", "skipped_window",
                               "skipped_norange", "no_position", "paused", "user_cancelled"))
    accounts_total = sum(pr["n"] for pr in procs)
    per_acct = wall / max(1, args.per_proc)
    return {
        "K": k,
        "per_proc": args.per_proc,
        "accounts_total": accounts_total,
        "wall_s": round(wall, 2),
        "machine_cpu_pct": round(machine_cpu_pct, 1),
        "engine_cpu_s": round(engine_cpu_s, 2),
        "engine_cpu_onecore_pct": round(engine_cpu_s / wall * 100, 1) if wall else None,
        "engine_cpu_machine_pct": round(engine_cpu_s / wall * 100 / NCROP, 1) if wall else None,
        "mock_cpu_s": round(mock_cpu_s, 2),
        "mock_cpu_onecore_pct": round(mock_cpu_s / wall * 100, 1) if wall else None,
        "peak_total_rss_mb": round(peak_rss_kb / 1024.0, 1),
        "median_proc_rss_mb": round(statistics.median(all_rss) / 1024.0, 1) if all_rss else None,
        "min_available_mb": round(min_avail, 1),
        "mem_total_mb": round(total_mem_mb, 1),
        "db_delta_kb": round(_db_bytes(args.db) / 1024.0 - args._db_before_kb, 1),
        "lock_errors": lock_errors,
        "lock_hits": lock_hits,
        "oom_killed": oom,
        "mem_abort": mem_abort,
        "proc_wall_avg_s": round(statistics.mean(walls), 2) if walls else None,
        "proc_wall_max_s": round(max(walls), 2) if walls else None,
        "per_acct_wall_s": round(per_acct, 3),
        "throughput_acct_per_h": round(accounts_total / wall * 3600, 1) if wall else None,
        "completed": success + sum(v for kk, v in counts.items()
                                   if kk in ("no_task", "skipped_window", "skipped_norange",
                                             "no_position")),
        "success": success,
        "failed": failed,
        "max_inflight": max_inflight,
        "mock_records": mock_records,
        "status_counts": counts,
        "rcs": [pr["rc"] for pr in procs],
    }


def _file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _db_bytes(db_path):
    total = 0
    for suf in ("", "-wal", "-shm"):
        with contextlib.suppress(OSError):
            total += os.path.getsize(db_path + suf)
    return total


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="concurrency_probe.py",
        description="多进程并发探测：K 阶梯找 CPU/内存/DB 写饱和点（测试机专用）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--repo", required=True, help="被测仓库根目录")
    ap.add_argument("--env", required=True, help="测试 .env 路径")
    ap.add_argument("--db", required=True, help="测试库路径")
    ap.add_argument("--ca", required=True, help="mock CA 证书路径")
    ap.add_argument("--mock-log", default="", help="mock JSONL 路径（用于并发/请求统计）")
    ap.add_argument("--mock-pid", type=int, default=0,
                    help="mock 进程 PID（用于单独统计 mock 自身 CPU，剔除服务端开销）")
    ap.add_argument("--k-list", default="1,2,4,8,12,16,20,24", help="K 阶梯（逗号分隔）")
    ap.add_argument("--per-proc", type=int, default=8, help="每个进程处理的账号数")
    ap.add_argument("--gap", type=int, default=0, help="账号间隔（秒）；0=CPU 压满口径")
    ap.add_argument("--python", default="", help="被测 Python 解释器")
    ap.add_argument("--outdir", default=".", help="结果输出目录")
    ap.add_argument("--interval", type=float, default=0.3, help="采样间隔（秒）")
    ap.add_argument("--timeout", type=float, default=600, help="单档超时（秒）")
    ap.add_argument("--mem-reserve-mb", type=float, default=180.0,
                    help="可用内存低于该值判定内存饱和（同时作为升档余量基准）")
    ap.add_argument("--db-microbench", action="store_true",
                    help="每档前跑一次 scratch 库写并发微基准（默认开）")
    ap.add_argument("--label", default="concurrency", help="结果文件标签")
    args = ap.parse_args(argv)
    os.makedirs(args.outdir, exist_ok=True)
    args.repo = os.path.abspath(args.repo)
    args.env = os.path.abspath(args.env)
    args.db = os.path.abspath(args.db)

    k_list = [int(x) for x in args.k_list.split(",") if x.strip()]
    max_accounts = max(k_list) * args.per_proc
    phones = load_phones(args.db, max_accounts)
    if len(phones) < max(k_list):
        log(f"警告：库中可用账号仅 {len(phones)} 个，少于最大 K={max(k_list)}")
    log(f"=== 并发探测: K={k_list} per_proc={args.per_proc} gap={args.gap} "
        f"可用账号={len(phones)} 机器核数={NCROP} ===")

    args._db_before_kb = _db_bytes(args.db) / 1024.0
    rows = []
    for k in k_list:
        slices = partition_slices(phones, k, args.per_proc)
        if len(slices) < k:
            log(f"K={k}: 账号不足，实际只起 {len(slices)} 个进程，停止升档")
            break
        _, avail = read_meminfo()
        # 只保证下一档有基本余量（真正的饱和由运行中 min_available/OOM 判定）
        if avail < args.mem_reserve_mb + 60:
            log(f"K={k}: 可用内存 {avail:.0f}MB 过少，停止升档（内存饱和）")
            break
        run_dir = os.path.join(args.outdir, f"krun-{args.label}-k{k}")
        log(f"K={k}: 启动 {k} 进程 × {args.per_proc} 账号 ...")
        args._db_before_kb = _db_bytes(args.db) / 1024.0
        # 微基准（在被测负载之前，独立 scratch 库，不扰动被测库）
        micro = None
        if args.db_microbench:
            micro = db_write_microbench(
                os.path.join(args.outdir, f"scratch-{args.label}-k{k}.db"), k)
        row = run_k(args, k, slices, run_dir)
        if micro:
            row["db_write_p50_ms"] = micro["p50_ms"]
            row["db_write_p95_ms"] = micro["p95_ms"]
            row["db_write_max_ms"] = micro["max_ms"]
            row["db_write_errors"] = len(micro["errors"])
        else:
            row["db_write_p50_ms"] = row["db_write_p95_ms"] = row["db_write_max_ms"] = None
            row["db_write_errors"] = None
        rows.append(row)
        log(f"  K={k}: 墙钟 {row['wall_s']}s 整机CPU {row['machine_cpu_pct']}% "
            f"RSS {row['peak_total_rss_mb']}MB 可用内存 {row['min_available_mb']}MB "
            f"锁错误 {row['lock_errors']} 吞吐 {row['throughput_acct_per_h']}账号/h")

    # 劣化倍数：以 K=1 的单账号墙钟为基准
    base = next((r["per_acct_wall_s"] for r in rows if r["K"] == 1), None)
    for r in rows:
        r["degradation_x"] = round(r["per_acct_wall_s"] / base, 2) if base else None

    total_mem_mb, _ = read_meminfo()
    verdict = classify_bottlenecks(rows, total_mem_mb, args.mem_reserve_mb)

    out = {
        "label": args.label,
        "per_proc": args.per_proc,
        "gap": args.gap,
        "ncpu": NCROP,
        "mem_total_mb": round(total_mem_mb, 1),
        "mem_reserve_mb": args.mem_reserve_mb,
        "rows": rows,
        "verdict": verdict,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    out_json = os.path.join(args.outdir, f"concurrency-{args.label}.json")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    write_csv(os.path.join(args.outdir, f"concurrency-{args.label}.csv"), rows)

    # 控制台表格
    print("\n| K | 墙钟s | 整机CPU% | 引擎单核% | mock单核% | RSS MB | 可用MB | 锁错误 | 单账号墙钟s | 劣化x | 吞吐/时 |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['K']} | {r['wall_s']} | {r['machine_cpu_pct']} | "
              f"{r['engine_cpu_onecore_pct']} | {r.get('mock_cpu_onecore_pct')} | "
              f"{r['peak_total_rss_mb']} | "
              f"{r['min_available_mb']} | {r['lock_errors']} | {r['per_acct_wall_s']} | "
              f"{r['degradation_x']} | {r['throughput_acct_per_h']} |")
    print(f"\n饱和点判定: {verdict}")
    log(f"结果: {out_json}")
    return 0


def write_csv(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CSV_FIELDS})


if __name__ == "__main__":
    sys.exit(main())
