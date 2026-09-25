#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""**功能**
容量基准 CLI：**一条命令**测出「这台机器能带多少账号」并给出建议值（**仅限隔离测试机**）。

它把已有四件工具串起来跑（不重复实现任何测量逻辑）：

    mock_env（自签证书 + hosts 回环 + 443 出站兜底）
      → seed_accounts（造号）
      → mock_yiban（假易班，真 TLS 监听 443）
      → concurrency_probe（K 阶梯：1/2/4/8/… 个执行体同时跑）
      → 本文件：把实测值换算成「每执行体建议账号数」与「需要几个执行体」
      → mock_env --restore（**默认执行**，除非显式 --keep-env）

## 结论口径（三个数，全部来自实测）

1. **单执行体容量** = `窗口秒数 ÷ (单账号实测耗时 + 账号间隔)`；
2. **建议每执行体账号数** = 单执行体容量 **× 2/3**（用户裁决的余量口径）；
3. **需要几个执行体** = `用户数 ÷ 建议每执行体账号数` 向上取整，再与
   **本机实测能同时跑几个执行体**（首个耗时劣化/资源饱和档之前）对照：跑不下就明说
   "这台机器不够"，而不是给一个乐观数字。

**建议值只是提醒**：不同部署者的机器差别很大（带宽/CPU/内存/网络延迟都不同），
所以这个数字由**每台机器自己跑出来**，不得写进程序当固定上限。

**归属**
`scripts/loadtest/` 隔离压测工具链的**顶层入口**；`yiban/cli.py capacity` 子命令是同
口径的命令行包装（生产侧只做建议，不做实测）。只在测试机运行。

**复用**
建议值换算函数与三档 `--profile` 定义被 `yiban/cli.py capacity` 复用；测量本身不
重复实现，一律转调 `concurrency_probe` 等既有工具。

**通信**
用法：

    sudo python3 scripts/loadtest/capacity_probe.py --repo /opt/repo \\
        --base-dir /opt/yiban-capacity --users 5000

    # 只跑某一档、复用已就绪环境（不碰 hosts/iptables）
    sudo python3 scripts/loadtest/capacity_probe.py --repo /opt/repo \\
        --base-dir /opt/yiban-capacity --profile simulated --skip-env

输入：`--repo` / `--base-dir` / `--users` / `--profile` / `--skip-env` / `--keep-env`。
输出：实测容量与三条建议值到 stdout；退出码 0 正常；2 参数/平台/前置不满足；
3 某档测量失败。
调用谁：`mock_env` / `seed_accounts` / `mock_yiban` / `concurrency_probe`（子进程）。
谁调用：运维在隔离测试机手工执行。

## 安全红线（与 loadtest 工具链一致）

1. 绝不指向真实易班：`mock_env` 会把易班域名改写为回环并用 iptables 拒绝其余 443 出站；
2. 只允许在隔离测试机上以 root 运行（改 `/etc/hosts`/iptables/自签证书需要 root）；
3. 结束必须还原：脚本默认自动 `--restore`，中断（Ctrl-C）时也会尝试还原；
   `--keep-env` 用于连续跑多档（此时由调用方最后手动 restore）。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import platform
import subprocess
import sys
import time

# 隔离断言（fail-closed）。见 scripts/loadtest/isolation.py 的加载兼容说明。
try:
    from loadtest import isolation
except ImportError:  # pragma: no cover - 取决于加载方式
    import isolation

HERE = os.path.dirname(os.path.abspath(__file__))

#: 假易班记账端点（回环，TLS）；证书 SAN 含 127.0.0.1，故直连回环即可，无需 hosts
DEFAULT_MOCK_STATS_URL = "https://127.0.0.1"

#: mock_env 搭建路径必填的出站探测目标：TEST-NET-3（RFC 5737）合成地址，不可路由，
#: 绝不指向真实易班。REJECT 生效时该地址 443 被拒；用于满足「探测必填、不静默 SKIP」。
DEFAULT_EGRESS_PROBE_IP = "203.0.113.7"

#: 生效窗口：生产 06:30–07:50（80 分钟）去掉前后留白后的有效秒数
DEFAULT_WINDOW_SEC = 4680

#: 档位定义：名字 → (mock 每请求延迟 ms, 账号间隔 s, K 阶梯, 每进程账号数)
#: - low-latency：本机/局域网延迟，测「机器本身的天花板」；
#: - simulated：300ms 拟真校园网络延迟，测「现实网络下的容量」；
#: - production：拟真延迟 + 生产账号间隔（10s），用于换算真实窗口容量。
PROFILES = {
    "low-latency": {"delay_ms": 50, "gap": 0,
                    "k_list": [1, 2, 4, 8, 12, 16], "per_proc": 20},
    "simulated": {"delay_ms": 300, "gap": 0,
                  "k_list": [1, 2, 4, 8, 12, 16], "per_proc": 8},
    "production": {"delay_ms": 300, "gap": 10,
                   "k_list": [1, 2, 4, 8, 12], "per_proc": 6},
}

#: 建议余量（用户裁决：实测 × 2/3）
DEFAULT_RATIO = 2.0 / 3.0


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# 纯函数（换算与判定口径，全部有单测；不依赖任何测量设备）
# ---------------------------------------------------------------------------
def measured_cycle(per_acct_wall_s, gap, per_proc):
    """把压测给出的"单账号平均墙钟"还原成**周期**（含间隔对齐）。

    压测口径是 `墙钟 ÷ 进程内账号数`，而**每个进程的第一个账号不等待间隔**
    （前面没有账号），故平均墙钟比真实周期少 `gap / n`：

        周期 = 平均墙钟 + gap / n

    实测对照（生产间隔档 gap=10、每进程 6 个账号）：平均墙钟 10.321s → 周期
    11.99s，与 2026-09-14 独立实测的 11.88s（拟真 300ms + gap 10）吻合；
    若不补这一项，容量会被高估 `(t+gap)/(t+gap-gap/n)` ≈ 16%。

    gap=0 的档（纯机器能力）不含此项。
    """
    per_acct = float(per_acct_wall_s or 0)
    if not gap or not per_proc:
        return per_acct
    return per_acct + float(gap) / float(per_proc)


def executor_capacity(window_sec, cycle_sec):
    """**单执行体**在一个窗口内能跑完的账号数 = 窗口 ÷ 单账号周期。

    周期由 `measured_cycle` 从压测结果还原（已含间隔对齐），此处不再做任何修正。
    """
    cycle = float(cycle_sec or 0)
    if cycle <= 0:
        return 0
    return int(float(window_sec) // cycle)


def recommend_per_executor(capacity, ratio=DEFAULT_RATIO):
    """建议每执行体账号数 = 实测容量 × 余量（向下取整，至少 1）。"""
    return max(1, int(capacity * ratio))


def executors_needed(users, per_executor):
    """覆盖 `users` 个账号需要几个执行体（向上取整）。"""
    if per_executor <= 0:
        return None
    return math.ceil(users / float(per_executor))


#: "未触及饱和"的标记：此时可用的 K 只是**已测范围的下界**，不是上限
NOT_SATURATED = "未触及饱和"


def hardware_ceiling(rows, degrade_limit=1.5):
    """本机实测能**同时**跑几个执行体：首个「单账号耗时劣化超过 limit 或资源饱和」档之前。

    返回 `(K, 原因)`——`K` 为可用的最大档位，`原因` 说明为何在该档之外不可用；
    全程没有劣化/饱和时返回最后一个测到的档位与 `NOT_SATURATED`（**这是下界**：
    意思是"至少这么多还能跑"，不能读成"最多只能这么多"，否则会得出错的"机器不够"）。
    """
    safe_k, reason = None, "未测量"
    for r in sorted(rows, key=lambda x: x["K"]):
        degraded = (r.get("degradation_x") or 1.0) > degrade_limit
        saturated = bool(r.get("oom_killed") or r.get("mem_abort")) or \
            (r.get("machine_cpu_pct") or 0) >= 90.0 or (r.get("lock_errors") or 0) > 0
        if degraded or saturated:
            reason = ("单账号耗时劣化 %.2f×" % (r.get("degradation_x") or 0)) if degraded \
                else "资源/锁饱和"
            return (safe_k, reason)
        safe_k = r["K"]
    return (safe_k, NOT_SATURATED)


def build_verdict(rows, *, users, window_sec, gap, ratio=DEFAULT_RATIO):
    """把某一档的 K 阶梯结果换算成部署建议（全部为实测值）。"""
    if not rows:
        return {"ok": False, "why": "没有测量结果"}
    base = next((r for r in rows if r["K"] == 1), rows[0])
    cycle = measured_cycle(base.get("per_acct_wall_s"),
                           gap, base.get("per_proc"))
    capacity = executor_capacity(window_sec, cycle)
    per_exec = recommend_per_executor(capacity, ratio)
    need = executors_needed(users, per_exec)
    ceiling_k, ceiling_why = hardware_ceiling(rows)
    measured_upto = max(r["K"] for r in rows)
    if ceiling_k is None:
        verdict_code = "insufficient"          # 连最低档都饱和
    elif need is None:
        verdict_code = "unknown"
    elif need <= ceiling_k:
        verdict_code = "ok"
    elif ceiling_why == NOT_SATURATED:
        verdict_code = "needs_wider_ladder"    # 需求超过已测范围，无实测依据下结论
    else:
        verdict_code = "insufficient"
    return {
        "verdict_code": verdict_code,
        "measured_upto_k": measured_upto,
        "ok": True,
        "users": users,
        "window_sec": window_sec,
        "gap": gap,
        "ratio": round(ratio, 4),
        "cycle_s": round(cycle, 3),
        "cycle_includes_gap_s": gap,
        "single_executor_capacity": capacity,
        "recommended_per_executor": per_exec,
        "executors_needed": need,
        "hardware_ceiling_k": ceiling_k,
        "hardware_ceiling_why": ceiling_why,
        "feasible_on_this_machine": verdict_code == "ok",
        "throughput_acct_per_h": base.get("throughput_acct_per_h"),
        "first_bottleneck": (base.get("first_bottleneck") or None),
    }


def format_verdict(v, profile, rows):
    """把结论排成人类可读的几行（给部署者看）。"""
    if not v.get("ok"):
        return f"[{profile}] 无法给出建议：{v.get('why')}"
    ceiling = ("至少 %s（%s）" % (v["hardware_ceiling_k"], v["hardware_ceiling_why"])
               if v.get("hardware_ceiling_why") == NOT_SATURATED
               else "%s（%s）" % (v["hardware_ceiling_k"], v["hardware_ceiling_why"]))
    lines = [
        "",
        f"=== [{profile}] 结论（全部为实测值，非估算）===",
        f"  单账号周期（实测，含间隔对齐）：{v['cycle_s']}s"
        f"（本档账号间隔 {v['cycle_includes_gap_s']}s）",
        f"  单执行体容量：{v['single_executor_capacity']} 账号 / {v['window_sec']}s 窗口",
        f"  建议每执行体带：{v['recommended_per_executor']} 账号"
        f"（= 实测容量 × {v['ratio']:.2f}，留余量）",
        f"  {v['users']} 个账号需要：{v['executors_needed']} 个执行体",
        f"  本机实测可同时跑：{ceiling} 个执行体",
    ]
    if v.get("verdict_code") == "ok":
        lines.append("  → 本机够用；但**建议值只是提醒**，实际上线后仍要看失败率与单账号耗时漂移。")
    elif v.get("verdict_code") == "needs_wider_ladder":
        lines.append(
            f"  → ⚠ 需求（{v['executors_needed']} 个）超过已测范围（K≤{v['measured_upto_k']}，"
            "且未见饱和）：**不能据此说机器不够**。要下结论请把 K 阶梯加大重测。"
        )
    else:
        lines.append(
            "  → ⚠ 按 2/3 余量口径，本机在执行体数上不够：要么接受更长窗口/更高失败率，"
            "要么提高单机能力（更多核/更高带宽），要么多机分担。数字在此，不要靠估算拍板。"
        )
    sat = [f"K={r['K']}" for r in rows if (r.get("machine_cpu_pct") or 0) >= 90]
    if sat:
        lines.append(f"  注：整机 CPU ≥90% 出现在 {', '.join(sat)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 编排（调用既有工具；每步失败都明确报错并尽量还原环境）
# ---------------------------------------------------------------------------
def _tail(text, limit=2000):
    """取子进程输出的尾部若干字符（失败信息里带上，便于定位是哪一步/哪条 FAIL）。"""
    if not text:
        return ""
    text = text.strip()
    return text[-limit:]


def _run(cmd, *, cwd=None, env=None, timeout=None, check=True, logfile=None):
    """跑子命令并回显；`logfile` 给定时输出落盘（长任务便于事后查）。

    失败（check=True 且非零退出）时把子进程输出尾部带进异常——只有"命令失败"
    一行时，调用方只能看到笼统的退出码，定位不到是自检哪一条 FAIL（或 iptables
    哪条规则没装上）；输出落在 logfile 时从落盘文件取尾部。
    """
    log("$ " + " ".join(str(c) for c in cmd))
    printable = " ".join(str(c) for c in cmd)
    if logfile:
        with open(logfile, "a", encoding="utf-8") as f:
            f.write(printable + "\n")
        fh = open(logfile, "a", encoding="utf-8")
    stdout = fh if logfile else subprocess.PIPE
    try:
        p = subprocess.run(cmd, cwd=cwd, env=env, timeout=timeout,
                           stdout=stdout, stderr=subprocess.STDOUT, text=True)
    finally:
        if logfile:
            with contextlib.suppress(Exception):
                fh.close()
    if check and p.returncode != 0:
        output = p.stdout
        if output is None and logfile:
            with contextlib.suppress(OSError), \
                    open(logfile, encoding="utf-8", errors="replace") as f:
                output = f.read()
        msg = f"命令失败（退出码 {p.returncode}）：{printable}"
        tail = _tail(output)
        if tail:
            msg += f"\n--- 子进程输出尾部 ---\n{tail}"
        raise RuntimeError(msg)
    return p


def _python():
    """跑工具链用的解释器：优先当前解释器（测试机上一般是 venv 里那个）。"""
    return sys.executable or "python3"


def ensure_platform():
    """平台前置：只允许在 Linux 测试机以 root 运行（hosts/iptables/自签证书需要）。"""
    if platform.system() != "Linux":
        raise SystemExit("错误：本工具只在 Linux 测试机上运行（需要改 /etc/hosts 与 iptables）")
    if os.geteuid() != 0:
        raise SystemExit("错误：需要 root（自签证书 + hosts 改写 + iptables 兜底）")


def prepare_env(base_dir, repo, egress_probe_ip=DEFAULT_EGRESS_PROBE_IP):
    # mock_env 搭建路径现要求 --egress-probe-ip 非空（缺省即拒绝）；编排器代传安全默认
    # （TEST-NET，绝不写入真实值），使一条命令仍能跑通全链。
    _run([_python(), os.path.join(repo, "scripts", "loadtest", "mock_env.py"),
          "--base-dir", base_dir, "--egress-probe-ip", egress_probe_ip])


def restore_env(base_dir, repo):
    _run([_python(), os.path.join(repo, "scripts", "loadtest", "mock_env.py"),
          "--base-dir", base_dir, "--restore"])


def start_mock(base_dir, repo, delay_ms, log_path, ready_path, pubkey_path):
    """起假易班（真 TLS 443）；返回进程对象。"""
    cmd = [_python(), os.path.join(repo, "scripts", "loadtest", "mock_yiban.py"),
           "--cert", os.path.join(base_dir, "ca", "server.pem"),
           "--key", os.path.join(base_dir, "ca", "server.key"),
           "--log", log_path, "--ready-file", ready_path,
           "--delay-ms", str(delay_ms)]
    if pubkey_path and os.path.exists(pubkey_path):
        cmd += ["--pubkey-file", pubkey_path]
    log("$ " + " ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
                            env=isolation.strip_proxy(os.environ))
    deadline = time.time() + 20
    while time.time() < deadline:
        if os.path.exists(ready_path):
            return proc
        if proc.poll() is not None:
            raise RuntimeError("假易班启动失败（见上方输出）")
        time.sleep(0.2)
    proc.terminate()
    raise RuntimeError("假易班 20s 内未就绪")


def stop_mock(proc):
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    with contextlib.suppress(Exception):
        proc.wait(timeout=10)


def seed(base_dir, db_path, env_path, n, gap, repo):
    _run([_python(), os.path.join(repo, "scripts", "loadtest", "seed_accounts.py"),
          "--n", str(n), "--db", db_path, "--env", env_path, "--gap", str(gap)])


def run_ladder(base_dir, repo, db_path, env_path, ca_path, mock_log,
               k_list, per_proc, gap, label, outdir, mock_pid, timeout_s):
    _run([_python(), os.path.join(repo, "scripts", "loadtest", "concurrency_probe.py"),
          "--repo", repo, "--env", env_path, "--db", db_path, "--ca", ca_path,
          "--mock-log", mock_log, "--mock-pid", str(mock_pid),
          "--k-list", ",".join(str(k) for k in k_list),
          "--per-proc", str(per_proc), "--gap", str(gap),
          "--label", label, "--outdir", outdir],
         timeout=timeout_s, logfile=os.path.join(outdir, f"probe-{label}.log"))


def read_probe_result(outdir, label):
    path = os.path.join(outdir, f"concurrency-{label}.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def verify_accounting(rows, ca_path, *, stats_url=DEFAULT_MOCK_STATS_URL):
    """启动即/收尾记账对平断言：mock 服务端权威记账 == 驱动从 JSONL 读到的条数。

    二者不等说明有请求处理了却没进驱动读到的账（旁路真实出口 / mock 静默丢日志），
    本轮容量结论不可信 —— 抛 IsolationError，由 main 转成非零退出。用 trust_env=False
    的会话取 /__stats，代理机也不会被假账骗过。
    """
    session = isolation.loadtest_session(ca=ca_path)
    issued = isolation.mock_recorded_total(stats_url, session=session)
    accounted = sum(int(r.get("mock_records") or 0) for r in rows)
    isolation.assert_accounting(issued, accounted)
    log(f"记账对平：mock 发出 {issued} 条 == 驱动读到 {accounted} 条")
    return issued


def main(argv=None):
    # 启动即断言（fail-closed，早于 argparse）：见 isolation.assert_no_proxy 与 MF-68。
    try:
        isolation.assert_no_proxy(os.environ, source="capacity_probe 进程")
    except isolation.IsolationError as e:
        raise SystemExit(f"错误：{e}") from e

    ap = argparse.ArgumentParser(
        prog="capacity_probe.py",
        description="容量基准：一条命令测出本机可承载账号数并给出建议（测试机专用）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--repo", required=True, help="被测仓库根目录")
    ap.add_argument("--base-dir", default="/opt/yiban-capacity",
                    help="证书/数据/结果落盘目录")
    ap.add_argument("--profile", default="simulated,production",
                    help="档位（逗号分隔：low-latency/simulated/production）")
    ap.add_argument("--users", type=int, default=5000,
                    help="要覆盖的账号数（用于算需要几个执行体）")
    ap.add_argument("--window-sec", type=int, default=DEFAULT_WINDOW_SEC,
                    help="有效签到窗口秒数（生产 80 分钟去掉前后留白 = 4680）")
    ap.add_argument("--ratio", type=float, default=DEFAULT_RATIO,
                    help="建议余量（实测 × ratio）")
    ap.add_argument("--skip-env", action="store_true",
                    help="复用已就绪环境（不跑 mock_env，也不还原）")
    ap.add_argument("--keep-env", action="store_true",
                    help="跑完不还原 hosts/iptables（连续多档时用，最后一次记得手动还原）")
    ap.add_argument("--timeout-per-k", type=float, default=600.0,
                    help="单档阶梯的整段超时（秒）")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要执行的步骤")
    ap.add_argument("--reuse-results", action="store_true",
                    help="不测量，只按已落盘的 concurrency-*.json 重新出结论（改换算口径时用）")
    ap.add_argument("--k-list", default="",
                    help="覆盖档位内置的 K 阶梯（逗号分隔；用于把已测范围加大）")
    ap.add_argument("--per-proc", type=int, default=0,
                    help="覆盖每进程账号数（默认用档位内置值）")
    ap.add_argument("--mock-stats-url", default=DEFAULT_MOCK_STATS_URL,
                    help="假易班记账端点（/stats 所在基址）")
    ap.add_argument("--egress-probe-ip", default=DEFAULT_EGRESS_PROBE_IP,
                    help="搭建时传给 mock_env 的出站探测目标（默认 TEST-NET，勿指向真实易班）")
    args = ap.parse_args(argv)

    repo = os.path.abspath(args.repo)
    base = os.path.abspath(args.base_dir)
    outdir = os.path.join(base, "results")
    os.makedirs(outdir, exist_ok=True)
    profiles = [p.strip() for p in args.profile.split(",") if p.strip()]
    for p in profiles:
        if p not in PROFILES:
            raise SystemExit(f"错误：未知档位 {p}（可选：{', '.join(PROFILES)}）")
    # 覆盖项：用于"需求超出已测范围"时把 K 阶梯加大（结论必须来自实测范围）
    override_k = [int(x) for x in args.k_list.split(",") if x.strip()]
    if override_k:
        for p in profiles:
            PROFILES[p]["k_list"] = override_k
    if args.per_proc:
        for p in profiles:
            PROFILES[p]["per_proc"] = args.per_proc

    if args.dry_run:
        print("档位：")
        for p in profiles:
            cfg = PROFILES[p]
            print(f"  {p}: mock 延迟 {cfg['delay_ms']}ms / 间隔 {cfg['gap']}s / "
                  f"K={cfg['k_list']} / 每进程 {cfg['per_proc']} 账号")
        print(f"环境：{'复用（--skip-env）' if args.skip_env else f'搭建并还原（{base}）'}")
        print(f"用户数 {args.users} / 窗口 {args.window_sec}s / 余量 ×{args.ratio:.3f}")
        return 0

    ensure_platform()
    env_path = os.path.join(base, "test.env")
    db_path = os.path.join(base, "data", "yiban.db")
    ca_path = os.path.join(base, "ca", "ca.pem")
    pubkey_path = os.path.join(base, "ca", "pub.pem")
    mock_log = os.path.join(base, "logs", "mock.jsonl")
    ready_path = os.path.join(base, "mock.ready")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    os.makedirs(os.path.dirname(mock_log), exist_ok=True)

    env_ready = args.skip_env
    verdicts = {}
    try:
        if not env_ready:
            # 先置位再搭环境：mock_env 可能在自检失败前已改过 hosts/iptables，
            # 失败路径同样要走到 finally 的还原。
            env_ready = True
            try:
                prepare_env(base, repo, args.egress_probe_ip)
            except RuntimeError as e:
                # 环境未就绪（hosts/iptables 兜底没装好或零外联自检 FAIL）：
                # **不跑 K 阶梯**——出站没被拦住时压测会直连真实易班。
                # 子进程输出尾部已在异常信息里带上。
                raise SystemExit(
                    f"错误：压测环境未就绪，已中止（不跑 K 阶梯）：{e}") from e

        if not (args.reuse_results and args.skip_env):
            max_accounts = max(max(PROFILES[p]["k_list"]) * PROFILES[p]["per_proc"]
                               for p in profiles)
            # 造号只做一次：各档共用同一批账号（口径一致，便于横向比较）
            seed(base, db_path, env_path, max_accounts, PROFILES[profiles[0]]["gap"], repo)

        for p in profiles:
            cfg = PROFILES[p]
            label = f"cap-{p}"
            if args.reuse_results:
                v = build_verdict(read_probe_result(outdir, label).get("rows") or [],
                                  users=args.users, window_sec=args.window_sec,
                                  gap=cfg["gap"], ratio=args.ratio)
                v["profile"] = p
                v["profile_delay_ms"] = cfg["delay_ms"]
                verdicts[p] = v
                print(format_verdict(v, p, read_probe_result(outdir, label).get("rows") or []))
                continue
            if os.path.exists(ready_path):
                os.remove(ready_path)
            mock = start_mock(base, repo, cfg["delay_ms"], mock_log, ready_path, pubkey_path)
            try:
                run_ladder(base, repo, db_path, env_path, ca_path, mock_log,
                           cfg["k_list"], cfg["per_proc"], cfg["gap"], label,
                           outdir, mock.pid, args.timeout_per_k)
                # 记账对平必须在 mock 还活着时做：服务端权威 total == 驱动读到的条数，
                # 不等则本轮测量不可信（fail-closed，不产出建议值）。
                result = read_probe_result(outdir, label)
                rows = result.get("rows") or []
                try:
                    verify_accounting(rows, ca_path, stats_url=args.mock_stats_url)
                except isolation.IsolationError as e:
                    raise SystemExit(f"错误：{e}") from e
            finally:
                stop_mock(mock)
            v = build_verdict(rows, users=args.users, window_sec=args.window_sec,
                              gap=cfg["gap"], ratio=args.ratio)
            v["profile"] = p
            v["profile_delay_ms"] = cfg["delay_ms"]
            verdicts[p] = v
            print(format_verdict(v, p, rows))
    finally:
        if env_ready and not args.skip_env and not args.keep_env:
            with contextlib.suppress(Exception):
                restore_env(base, repo)
        elif env_ready and not args.skip_env:
            log("已保留环境（--keep-env）：用完请手动执行 "
                f"mock_env.py --base-dir {base} --restore")

    out_path = os.path.join(outdir, "capacity-verdict.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"profiles": verdicts, "users": args.users,
                   "window_sec": args.window_sec, "ratio": args.ratio,
                   "created_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                  f, ensure_ascii=False, indent=2)
    log(f"结论已落盘：{out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
