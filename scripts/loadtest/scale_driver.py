#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单进程规模驱动：跑一轮真实调度并采样记录（测试机/沙箱用）。

在同一个进程里启动 ``scripts/signin.py``（完整调度模式，非 --only），并行采样：

  * 总墙钟、单账号耗时分布（中位 / p95 / 最大）；
  * 成功 / 失败 / 跳过账号数（读 state 目录的 sign-state-*.json）；
  * 进程 CPU 时间与峰值 RSS、线程数；
  * DB 文件（含 -wal/-shm）大小增量；
  * 实际最大并发（解析 mock JSONL 的 inflight 字段，用于证实严格串行）。

输出 JSON + CSV 两份，字段名稳定，便于跨版本对比。

用法：
  python3 scale_driver.py --n 60 --label net300 --delay-ms 300 \\
      --env /opt/yiban-loadtest/test.env --repo /opt/yiban-test \\
      --mock-log /opt/yiban-loadtest/logs/mock.jsonl --window-sec 420
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import json
import os
import sqlite3
import statistics
import subprocess
import sys
import time
from datetime import datetime, timedelta

# 隔离断言（fail-closed）。见 scripts/loadtest/isolation.py 的加载兼容说明。
try:
    from loadtest import isolation
except ImportError:  # pragma: no cover - 取决于加载方式
    import isolation

DEFAULT_GAP = 10
PROD_WINDOW_SEC = 4680  # 80 分钟窗口 - 前后各 60s 掐头去尾（容量换算基线）

# CSV 列：稳定命名，跨版本对比沿用
CSV_FIELDS = [
    "label", "config", "N", "window_eff_s", "wall_s",
    "cycle_avg_s", "cycle_p50_s", "cycle_p95_s", "cycle_min_s", "cycle_max_s",
    "t_avg_s", "t_p95_s", "requests_run", "requests_per_acct",
    "completed", "success", "failed", "skipped",
    "cpu_s", "cpu_pct_1core", "peak_rss_mb", "threads_max",
    "db_delta_kb", "max_inflight_in_run", "finish_window", "cap80_implied", "rc",
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


def load_env_file(path):
    """读 .env：仅取 KEY=VALUE，去掉包裹引号；返回 dict（用于注入子进程环境）。

    **读不到必须报错**，不得返回空 dict：调用方会 `dict(os.environ).update(...)`，
    静默返回空等于让子进程照单继承宿主环境（压测"配置没生效"却照跑，打的是宿主的
    口径）。文件真存在时正常解析。
    """
    env = {}
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
    return env


def percentile(sorted_vals, p):
    """线性插值分位数（输入须已排序）；空列表返回 None。"""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    frac = k - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


def stats(vals):
    if not vals:
        return {}
    sv = sorted(vals)
    return {
        "n": len(vals),
        "avg": round(statistics.mean(sv), 3),
        "min": round(sv[0], 3),
        "p50": round(percentile(sv, 0.5), 3),
        "p95": round(percentile(sv, 0.95), 3),
        "max": round(sv[-1], 3),
    }


def parse_jsonl_cycles(log_path, offset=0):
    """从 mock JSONL 的 offset 起解析账号周期。

    账号周期以 ``GET /code/html`` 为起点（signin 每个账号登录链第一步必经），
    周期耗时 = 相邻两次起点的时间差（= 单账号耗时 t + 间隔 gap）；
    同时累计每周期内各请求的 dur_ms 得到网络耗时 t。

    返回 (cycles, t_list, max_inflight, inflight_ge2, n_records, new_offset)。
    """
    starts, reqs = [], []
    max_inflight = 0
    inflight_ge2 = 0
    n_records = 0
    try:
        size = os.path.getsize(log_path)
    except OSError:
        return [], [], 0, 0, 0, offset
    if offset > size:
        offset = 0
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
            n_records += 1
            infl = int(rec.get("inflight", 1) or 1)
            max_inflight = max(max_inflight, infl)
            if infl >= 2:
                inflight_ge2 += 1
            if rec.get("path") == "/code/html" and rec.get("method", "GET") == "GET":
                starts.append(rec.get("epoch_ms", 0))
                # 起点请求本身的耗时也计入本周期网络耗时
                reqs.append([float(rec.get("dur_ms", 0) or 0)])
            elif reqs and "epoch_ms" in rec:
                reqs[-1].append(float(rec.get("dur_ms", 0) or 0))
        new_offset = f.tell()
    cycles = [round((starts[i + 1] - starts[i]) / 1000.0, 3) for i in range(len(starts) - 1)]
    t_list = [round(sum(r) / 1000.0, 3) for r in reqs if r]
    return cycles, t_list, max_inflight, inflight_ge2, n_records, new_offset


def read_state_counts(state_dir, day=None):
    """读 sign-state-<day>.json，返回 (counts, total_phones)。"""
    day = day or datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(state_dir, f"sign-state-{day}.json")
    counts = {}
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return counts, 0
    if not isinstance(data, dict):
        return counts, 0
    for v in data.values():
        if isinstance(v, dict):
            st = str(v.get("status", "")).strip()
            counts[st] = counts.get(st, 0) + 1
    return counts, len(data)


def db_bytes(db_path):
    total = 0
    for suf in ("", "-wal", "-shm"):
        with contextlib.suppress(OSError):
            total += os.path.getsize(db_path + suf)
    return total


def clear_session_cache(db_path, fingerprint=None):
    """清空会话缓存，保证每个账号都走完整 6 请求登录链（口径稳定）。

    清空前必须过 `isolation.assert_loadtest_target`（声明指纹 + 目标像压测库）：
    误指生产库的 DELETE 会让全部账号真实重登，故缺指纹/指纹不符/非压测账号都抛错。
    """
    isolation.assert_loadtest_target(db_path, fingerprint)
    try:
        c = sqlite3.connect(db_path)
        try:
            c.execute("DELETE FROM session_cache")
            c.commit()
        finally:
            c.close()
    except sqlite3.Error:
        pass


def _parse_hhmm(s):
    h, m = s.split(":")
    return int(h), int(m)


def compute_window(now, window_sec):
    """把秒级窗口映射为今天的 HH:MM 区间（不跨零点）。返回 (start, end, eff_sec)。"""
    span_min = max(1, int((window_sec + 59) // 60))
    start = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    end = start + timedelta(minutes=span_min)
    if end.date() != start.date():
        end = start.replace(hour=23, minute=59)
    eff = int((end - start).total_seconds())
    return start.strftime("%H:%M"), end.strftime("%H:%M"), max(0, eff)


def reset_state_dir(state_dir):
    """清空测试状态目录：避免上一轮残留的状态/标记污染本轮统计。

    白名单补齐此前漏掉的瞬态件：``sched-slot-*``、``mail-user-fail-*``，以及进程级
    锁文件 ``*.lock``（如 ``signin-run.lock``）与原子写残留 ``*.tmp<pid>``。这些残留
    会让「本轮完成/失败计数」读到上一轮的值，直接污染容量结论。
    """
    try:
        entries = os.listdir(state_dir)
    except OSError:
        return
    prefixes = ("sign-state-", "sign-daily-", "sched-run-", "sched-snapshot-",
                "cred-state", "probe-state", "sched-slot-", "mail-user-fail-")
    for name in entries:
        tmp_pid = ".tmp" in name and name.rsplit(".tmp", 1)[1].isdigit()
        if name.startswith(prefixes) or name.endswith(".lock") or tmp_pid:
            with contextlib.suppress(OSError):
                os.remove(os.path.join(state_dir, name))


def base_env(env_file, repo, ca_pem, extra=None):
    env = dict(os.environ)
    env.update(load_env_file(env_file))
    env["REQUESTS_CA_BUNDLE"] = ca_pem
    env["SSL_CERT_FILE"] = ca_pem
    env["CURL_CA_BUNDLE"] = ca_pem
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = os.pathsep.join(
        [os.path.join(repo, "scripts"), repo, env.get("PYTHONPATH", "")]
    ).strip(os.pathsep)
    if extra:
        env.update(extra)
    # 主动摘除代理键：即便入口已断言过，子进程环境也必须做到「无任何 *PROXY* 键」，
    # 否则被压进程里的 requests 会经代理出口，隔离链当场旁落（见 MF-68）。
    return isolation.strip_proxy(env)


def run_once(args, mock_log, window_sec):
    repo = os.path.abspath(args.repo)
    py = args.python or sys.executable
    db_path = args.db
    env_file = args.env
    st = load_env_file(env_file)
    state_dir = st.get("YIBAN_STATE_DIR", os.path.join(os.path.dirname(db_path), "state"))
    os.makedirs(state_dir, exist_ok=True)

    now = datetime.now()
    if window_sec and window_sec > 0:
        w_start, w_end, eff = compute_window(now, window_sec)
    else:
        w_start = st.get("YIBAN_SIGN_START", "06:30")
        w_end = st.get("YIBAN_SIGN_END", "07:50")
        eff = (_parse_hhmm(w_end)[0] * 60 + _parse_hhmm(w_end)[1]
               - _parse_hhmm(w_start)[0] * 60 - _parse_hhmm(w_start)[1]) * 60

    extra = {
        "YIBAN_SIGN_START": w_start,
        "YIBAN_SIGN_END": w_end,
        "YIBAN_ACCOUNT_GAP_MAX": str(args.gap),
        "YIBAN_START_DELAY_MAX": "0",
        "YIBAN_WINDOW_EDGE_FRONT_SEC": "0",
        "YIBAN_WINDOW_EDGE_BACK_SEC": "0",
        "YIBAN_GLOBAL_PAUSE": "0",
        "YIBAN_NOTIFY_URL": "",
        "YIBAN_PROBE_ENABLE": "0",
        "YIBAN_SECOND_RUN": "0",
    }
    env = base_env(env_file, repo, args.ca, extra)
    env["YIBAN_DB_FILE"] = db_path
    env["YIBAN_STATE_DIR"] = state_dir
    env["YIBAN_ENV_FILE"] = os.path.abspath(env_file)

    clear_session_cache(db_path, args.db_fingerprint)
    reset_state_dir(state_dir)
    mark = _file_size(mock_log)
    db_before = db_bytes(db_path)

    logfile = os.path.join(args.logdir, f"signin-{args.label}-n{args.n}.log")
    os.makedirs(args.logdir, exist_ok=True)
    cmd = [py, os.path.join(repo, "scripts", "signin.py")]
    t0 = time.monotonic()
    hwm_kb = 0
    threads_max = 0
    cpu_s = 0.0
    hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    with open(logfile, "w", encoding="utf-8") as lf:
        p = subprocess.Popen(cmd, cwd=repo, env=env, stdout=lf, stderr=subprocess.STDOUT)
        while p.poll() is None:
            if time.monotonic() - t0 > args.timeout:
                p.kill()
                break
            try:
                with open(f"/proc/{p.pid}/status", encoding="utf-8") as f:
                    for ln in f:
                        if ln.startswith("VmHWM:"):
                            hwm_kb = max(hwm_kb, int(ln.split()[1]))
                        elif ln.startswith("Threads:"):
                            threads_max = max(threads_max, int(ln.split()[1]))
                with open(f"/proc/{p.pid}/stat", encoding="utf-8") as f:
                    fields = f.read().split()
                    cpu_s = (int(fields[13]) + int(fields[14])) / hz
            except (OSError, IndexError, ValueError):
                pass
            time.sleep(0.5)
    wall = time.monotonic() - t0
    rc = p.returncode

    # mock 在响应 flush 后才写 JSONL，稍等片刻再读，避免漏掉最后几条
    time.sleep(0.5)
    cycles, t_list, max_infl, infl_ge2, n_rec, _ = parse_jsonl_cycles(mock_log, mark)
    counts, total = read_state_counts(state_dir)
    db_delta = (db_bytes(db_path) - db_before) / 1024.0
    n = args.n
    ok = counts.get("success", 0) + counts.get("already", 0)
    skip = sum(v for k, v in counts.items()
               if k in ("no_task", "skipped_window", "skipped_norange", "no_position", "paused",
                        "user_cancelled"))
    fail = sum(v for k, v in counts.items()
               if k not in ("success", "already", "no_task", "skipped_window",
                            "skipped_norange", "no_position", "paused", "user_cancelled"))
    completed = ok
    cyc = stats(cycles)
    tt = stats(t_list)
    requests_run = n_rec
    # 实际发生登录链的账号数 = /code/html 起点数 = 周期数 + 1
    attempted = (cyc.get("n", 0) + 1) if cyc else 0
    result = {
        "label": args.label,
        "config": args.config_name,
        "N": n,
        "window": f"{w_start}-{w_end}",
        "window_eff_s": eff,
        "wall_s": round(wall, 2),
        "cycle_stats": cyc,
        "request_time_stats": tt,
        "status_counts": counts,
        "state_entries": total,
        "completed": completed,
        "success": ok,
        "failed": fail,
        "skipped": skip,
        "requests_run": requests_run,
        "requests_per_acct": round(requests_run / attempted, 2) if attempted else None,
        "cpu_s": round(cpu_s, 2),
        "cpu_pct_1core": round(cpu_s / wall * 100, 1) if wall > 0 else None,
        "peak_rss_kb": hwm_kb,
        "peak_rss_mb": round(hwm_kb / 1024.0, 1),
        "threads_max": threads_max,
        "db_delta_kb": round(db_delta, 1),
        "max_inflight_in_run": max_infl,
        "inflight_ge2_records": infl_ge2,
        "finish_window": (counts.get("skipped_window", 0) > 0),
        "rc": rc,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    min_s = (cyc or {}).get("min")
    result["cap80_implied"] = int(PROD_WINDOW_SEC / min_s) if min_s else None
    result["expected_cap_window"] = int(eff / min_s) if min_s else None
    return result


def _file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def write_csv(path, row):
    """按 (label, config) 去重后写 CSV，保持列顺序稳定。"""
    rows = []
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))
        except OSError:
            rows = []
    rows = [r for r in rows if not (r.get("label") == row["label"] and r.get("config") == row["config"])]
    rows.append({k: row.get(k, "") for k in CSV_FIELDS})
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(rows)


def main(argv=None):
    # 启动即断言（fail-closed，早于 argparse）：进程环境含任何 *PROXY* 键即拒绝，
    # 原因见 scripts/loadtest/isolation.py。缺这一条，代理机上会「打真实易班却报绿」。
    try:
        isolation.assert_no_proxy(os.environ, source="scale_driver 进程")
    except isolation.IsolationError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 2

    ap = argparse.ArgumentParser(
        prog="scale_driver.py",
        description="单进程规模驱动：真实调度一轮 + 资源采样（测试机/沙箱专用）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--repo", required=True, help="被测仓库根目录（含 scripts/signin.py）")
    ap.add_argument("--env", required=True, help="测试 .env 路径")
    ap.add_argument("--db", required=True, help="测试库路径")
    ap.add_argument("--db-fingerprint", default="",
                    help="目标库指纹（清空会话缓存前必填；先跑一次看打印值）")
    ap.add_argument("--n", type=int, required=True, help="本轮账号数")
    ap.add_argument("--label", default="run", help="本轮标签（结果文件名/CSV 去重键）")
    ap.add_argument("--config-name", default="custom", help="配置档名（写入结果，便于对比）")
    ap.add_argument("--gap", type=int, default=DEFAULT_GAP, help="账号间隔（秒）")
    ap.add_argument("--window-sec", type=int, default=0,
                    help="0=沿用 .env 窗口；>0=按该秒数生成今天的窗口")
    ap.add_argument("--python", default="", help="被测 Python 解释器（缺省当前解释器）")
    ap.add_argument("--ca", required=True, help="mock CA 证书路径（REQUESTS_CA_BUNDLE）")
    ap.add_argument("--mock-log", required=True, help="mock 逐请求 JSONL 路径")
    ap.add_argument("--mock-config", default="",
                    help="mock 热读配置 JSON 路径；传入后按下方参数写入该档")
    ap.add_argument("--delay-ms", type=float, default=0.0, help="写入 mock 的固定延迟")
    ap.add_argument("--tail-delay-ms", type=float, default=0.0, help="写入 mock 的尾延迟")
    ap.add_argument("--tail-every", type=int, default=0, help="写入 mock 的尾延迟间隔")
    ap.add_argument("--fail-rate", type=float, default=0.0, help="写入 mock 的失败注入概率")
    ap.add_argument("--fail-stage", default="none",
                    choices=["none", "login", "signIn", "signPosition"],
                    help="写入 mock 的失败注入点")
    ap.add_argument("--outdir", default=".", help="JSON/CSV 输出目录")
    ap.add_argument("--logdir", default="", help="signin 进程日志目录（缺省 outdir/logs）")
    ap.add_argument("--timeout", type=float, default=3600, help="单轮超时（秒）")
    args = ap.parse_args(argv)
    args.logdir = args.logdir or os.path.join(args.outdir, "logs")
    os.makedirs(args.outdir, exist_ok=True)
    os.makedirs(args.logdir, exist_ok=True)
    args.repo = os.path.abspath(args.repo)
    args.env = os.path.abspath(args.env)
    args.db = os.path.abspath(args.db)

    # 目标指纹门（fail-closed，早于任何清空）：未声明/不符即退 2，不做任何重活。
    actual_fp = isolation.loadtest_db_fingerprint(args.db)
    if str(args.db_fingerprint).strip() != actual_fp:
        print(f"错误：目标库指纹未声明或不匹配。实际指纹：{actual_fp}\n"
              f"      请确认目标为压测库后加 `--db-fingerprint {actual_fp}` 重跑。",
              file=sys.stderr)
        return 2

    log(f"=== 单进程驱动: N={args.n} label={args.label} config={args.config_name} gap={args.gap} ===")
    if args.mock_config:
        with open(args.mock_config, "w", encoding="utf-8") as f:
            json.dump({"delay_ms": args.delay_ms, "tail_delay_ms": args.tail_delay_ms,
                       "tail_every": args.tail_every, "fail_rate": args.fail_rate,
                       "fail_stage": args.fail_stage}, f)
        log(f"已写入 mock 配置: {args.mock_config} "
            f"(delay={args.delay_ms}ms tail_every={args.tail_every}/"
            f"{args.tail_delay_ms}ms fail={args.fail_stage}@{args.fail_rate})")
    res = run_once(args, args.mock_log, args.window_sec)
    out_json = os.path.join(args.outdir, f"run-{args.label}-n{args.n}.json")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    write_csv(os.path.join(args.outdir, "results.csv"), res)
    c = res.get("cycle_stats", {})
    log(f"墙钟 {res['wall_s']}s | 周期 min/p50/p95/max = "
        f"{c.get('min')}/{c.get('p50')}/{c.get('p95')}/{c.get('max')}s | "
        f"CPU {res['cpu_pct_1core']}% | RSS {res['peak_rss_mb']}MB | "
        f"最大并发 {res['max_inflight_in_run']} | 完成 {res['completed']}/{res['N']} | "
        f"cap80≈{res['cap80_implied']}")
    log(f"结果: {out_json} / {os.path.join(args.outdir, 'results.csv')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
