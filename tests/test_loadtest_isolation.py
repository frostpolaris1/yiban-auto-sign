#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""loadtest 隔离链 fail-closed 断言。

标签：J · 运维：部署/备份/发布
覆盖：任务 4 的四条「启动即断言 / 记账对平」验收不变量（全部真执行，不做源码断言）：

  1. 代理注入 ⇒ 拒绝启动（四个入口都以子进程实跑，反例可失败）；
  2. ``--egress-probe-ip`` 缺省 ⇒ 拒绝启动（不再静默 [SKIP]）；
  3. mock 侧记账条数 == 发出条数（不等 ⇒ 非零 / 抛错）；
  4. loadtest 自建会话显式 ``trust_env = False``（满足 registry 正向命中）。

另含：子进程环境构造处主动摘除 ``*PROXY*`` 键、iptables REJECT 改为 ``-I`` 前插、
``reset_state_dir`` 前缀白名单补齐、``read_meminfo`` 读取失败显式报错。
对应实现：`scripts/loadtest/isolation.py`（代理摘除、egress-probe 门、`reset_state_dir`
白名单、`read_meminfo`）与四个压测入口 `scale_driver.py` / `concurrency_probe.py` /
`capacity_probe.py` / `mock_env.py` 的启动自检与 `trust_env=False` 会话装配。
关键断言：以子进程实跑的退出码 / 抛错 / 记账条数为准（活体反例可失败），不 grep 被测源码。
依赖：纯函数与真起 mock 子进程（回环、明文、端口 0）的用例均可在普通套件跑；
iptables 真装规则需 root+Linux，改用「捕获下发的命令串」做等价断言，不碰系统。
"""

from __future__ import annotations

import http.client
import importlib
import os
import sqlite3
import subprocess
import sys
import threading
import time
import types

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_LOADTEST = os.path.join(_ROOT, "scripts", "loadtest")
if os.path.dirname(_LOADTEST) not in sys.path:
    sys.path.insert(0, os.path.dirname(_LOADTEST))

isolation = importlib.import_module("loadtest.isolation")
mock_yiban = importlib.import_module("loadtest.mock_yiban")
mock_env = importlib.import_module("loadtest.mock_env")
scale_driver = importlib.import_module("loadtest.scale_driver")
concurrency_probe = importlib.import_module("loadtest.concurrency_probe")
capacity_probe = importlib.import_module("loadtest.capacity_probe")

# 合成主机（TEST-NET，见 RFC 5737），绝不写入真实值
_FAKE_PROXY = "http://127.0.0.1:3128"
_TESTNET_IP = "203.0.113.7"


# ---------------------------------------------------------------------------
# 1. 代理注入 ⇒ 拒绝启动（真子进程 e2e，四个 loadtest 入口）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("script", ["mock_env", "scale_driver",
                                     "concurrency_probe", "capacity_probe"])
def test_proxy_in_env_refuses_to_start(script):
    """带 HTTPS_PROXY=x 直接拉起入口脚本：必须非零退出并说明代理原因。"""
    path = os.path.join(_LOADTEST, f"{script}.py")
    assert os.path.exists(path), path
    env = dict(os.environ)
    env["HTTPS_PROXY"] = _FAKE_PROXY
    r = subprocess.run([sys.executable, path],  # 无参数：断言必须早于 argparse
                       capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode != 0, f"{script} 带代理却启动成功（隔离未 fail-closed）"
    blob = (r.stderr + r.stdout).upper()
    assert "PROXY" in blob, f"{script} 未把代理拒绝原因打到 stderr：{r.stderr!r}"


def test_startup_guard_reports_offending_keys(monkeypatch, capsys):
    """进程环境断言：命中 *PROXY* 键时抛错并点名具体键名。"""
    monkeypatch.setattr(os, "environ", {"PATH": "/x", "https_proxy": _FAKE_PROXY})
    with pytest.raises(isolation.IsolationError) as cm:
        isolation.assert_no_proxy(os.environ, source="scale_driver 进程")
    assert "https_proxy" in str(cm.value).lower() or "PROXY" in str(cm.value)


# ---------------------------------------------------------------------------
# 2. 子进程环境构造处主动摘除代理键
# ---------------------------------------------------------------------------
def _proxy_free(env):
    return [k for k in env if isolation.PROXY_MARK in k.upper()]


def test_scale_driver_base_env_strips_proxy(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "environ", {
        "PATH": "/usr/bin", "HTTP_PROXY": _FAKE_PROXY,
        "https_proxy": _FAKE_PROXY, "no_proxy": "localhost"})
    env_file = tmp_path / "test.env"
    env_file.write_text('YIBAN_DB_FILE="/tmp/x.db"\n', encoding="utf-8")
    env = scale_driver.base_env(str(env_file), "/repo", "/repo/ca.pem")
    assert _proxy_free(env) == [], f"子进程环境仍含代理键：{_proxy_free(env)}"
    assert env["REQUESTS_CA_BUNDLE"] == "/repo/ca.pem"  # 其余装配保持不变


def test_concurrency_probe_build_proc_env_strips_proxy(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "environ", {
        "PATH": "/usr/bin", "ALL_PROXY": _FAKE_PROXY})
    env_file = tmp_path / "test.env"
    env_file.write_text("", encoding="utf-8")
    env = concurrency_probe.build_proc_env(str(env_file), "/repo", "/repo/ca.pem", {})
    assert _proxy_free(env) == []


# ---------------------------------------------------------------------------
# 3. --egress-probe-ip 必填：缺省 ⇒ 拒绝启动（反例可失败）
# ---------------------------------------------------------------------------
def test_mock_env_requires_egress_probe_ip(tmp_path):
    rc = mock_env.main(["--base-dir", str(tmp_path / "base"),
                        "--hosts-file", str(tmp_path / "hosts")])
    assert rc != 0, "缺省 --egress-probe-ip 竟允许搭建（会恒走 SKIP 假报绿）"


def test_verify_zero_egress_empty_probe_fails_not_skips(tmp_path, capsys):
    """空探测目标必须判 FAIL（此前是 [SKIP] 并可能返回 True）。"""
    hosts = tmp_path / "hosts"
    hosts.write_text("127.0.0.1 localhost\n", encoding="utf-8")
    ok = mock_env.verify_zero_egress(["127.0.0.1"], str(hosts), probe_ip="")
    out = capsys.readouterr().out
    assert ok is False
    assert "[SKIP]" not in out
    assert "[FAIL]" in out


# ---------------------------------------------------------------------------
# 4. trust_env = False
# ---------------------------------------------------------------------------
def test_loadtest_session_trusts_env_false():
    s = isolation.loadtest_session()
    assert s.trust_env is False
    s2 = isolation.loadtest_session(ca="/x/ca.pem")
    assert s2.trust_env is False and s2.verify == "/x/ca.pem"


# ---------------------------------------------------------------------------
# 5. 记账对平：发出 n == mock 记账 n，不等 ⇒ 抛错/非零
# ---------------------------------------------------------------------------
def test_assert_accounting_balanced_and_imbalanced():
    assert isolation.assert_accounting(5, 5) == 5
    with pytest.raises(isolation.IsolationError):
        isolation.assert_accounting(5, 4)


def test_mock_state_counts_silently_dropped_log_writes(tmp_path):
    """JSONL 落盘失败不再被静默吞掉：total 记账、durable 落盘、log_errors 丢弃分别计数。"""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    bad_log = os.path.join(str(blocker), "sub", "mock.jsonl")  # 父目录是文件 → open 失败
    st = mock_yiban.MockState(log_path=bad_log)
    st.record("127.0.0.1", "/code/html", 200, 0.0, 1, False, "ok")
    assert st.total == 1
    assert st.durable == 0 and st.log_errors == 1
    # 记账不平衡因此可被 assert_accounting 捕获（发出 1 条，落盘 0 条）
    with pytest.raises(isolation.IsolationError):
        isolation.assert_accounting(st.total, st.durable)


def test_mock_stats_excludes_ops_and_balance_holds(tmp_path):
    """端到端对平：真起 mock（回环/明文/端口0），发出 N 条请求 ⇒ /__stats.total==N==JSONL 行数；
    /__stats 与 /__health 属运维探测，不计入「发出条数」。"""
    state = mock_yiban.MockState(log_path=str(tmp_path / "mock.jsonl"))
    servers, state, _cfg = mock_yiban.create_servers(
        host="127.0.0.1", port=0, cert=None, key=None,
        state=state, config=mock_yiban.MockConfig(), enable_ipv6=False)
    srv = servers[0]
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        issued = 0
        for path, method, body in [
            ("/code/html", "GET", None),
            ("/code/usersure", "POST", "oauth_uname=1"),
            ("/iframe/index", "GET", None),
            ("/nightAttendance/student/index/signPosition", "GET", None),
            ("/nightAttendance/student/index/signIn", "POST", "x=1"),
        ]:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request(method, path, body=body)
            conn.getresponse().read()
            conn.close()
            issued += 1
        # 探测 /__stats 多次也不改变 total（运维端点不记账）
        for _ in range(3):
            assert isolation.mock_recorded_total(f"http://127.0.0.1:{port}",
                                                 isolation.loadtest_session()) == issued
        # 轮询等待最后一条 JSONL 落盘（服务端 flush 后才写）
        deadline = time.monotonic() + 5.0
        lines = 0
        while time.monotonic() < deadline:
            with open(tmp_path / "mock.jsonl", encoding="utf-8") as f:
                lines = sum(1 for ln in f if ln.strip())
            if lines >= issued:
                break
            time.sleep(0.02)
        assert lines == issued
        isolation.assert_accounting(issued, lines)  # 对平
    finally:
        srv.shutdown()
        srv.server_close()


def test_verify_accounting_raises_on_imbalance(monkeypatch):
    """收尾对平原语：服务端权威 total != 驱动读到的 JSONL 条数 ⇒ IsolationError。"""
    monkeypatch.setattr(capacity_probe.isolation, "mock_recorded_total",
                        lambda *a, **k: 9)  # 假装 mock 记账 9 条
    with pytest.raises(isolation.IsolationError) as cm:
        capacity_probe.verify_accounting([{"mock_records": 4}], ca_path="",
                                         stats_url="http://127.0.0.1:1")
    assert "记账不平衡" in str(cm.value)
    # 对平则不抛，并返回权威条数
    monkeypatch.setattr(capacity_probe.isolation, "mock_recorded_total",
                        lambda *a, **k: 6)
    assert capacity_probe.verify_accounting([{"mock_records": 4}, {"mock_records": 2}],
                                            ca_path="", stats_url="http://127.0.0.1:1") == 6


def test_capacity_probe_main_aborts_on_accounting_imbalance(tmp_path, monkeypatch):
    """常规层闭合「记账不等 ⇒ 非零」：把编排里除 verify_accounting 外的副作用全打桩，
    注入不等 ⇒ capacity_probe.main 必须非零退出（SystemExit），且不产出结论文件。

    这是 root e2e 之外、真正跑到「IsolationError → SystemExit」接线的用例。
    """
    monkeypatch.setattr(capacity_probe, "ensure_platform", lambda: None)
    monkeypatch.setattr(capacity_probe, "prepare_env", lambda *a, **k: None)
    monkeypatch.setattr(capacity_probe, "seed", lambda *a, **k: None)
    monkeypatch.setattr(capacity_probe, "restore_env", lambda *a, **k: None)
    monkeypatch.setattr(capacity_probe, "run_ladder", lambda *a, **k: None)
    monkeypatch.setattr(capacity_probe, "stop_mock", lambda *a, **k: None)
    monkeypatch.setattr(capacity_probe, "start_mock",
                        lambda *a, **k: types.SimpleNamespace(pid=4242))
    # 驱动读到 4 条，mock 侧却记账 9 条 ⇒ 不等
    monkeypatch.setattr(capacity_probe, "read_probe_result",
                        lambda *a, **k: {"rows": [{"mock_records": 4}]})
    monkeypatch.setattr(capacity_probe.isolation, "mock_recorded_total",
                        lambda *a, **k: 9)

    base = tmp_path / "b"
    with pytest.raises(SystemExit) as cm:
        capacity_probe.main(["--repo", str(tmp_path), "--base-dir", str(base),
                             "--profile", "simulated"])
    assert "记账不平衡" in str(cm.value.code)
    # 结论文件不应产出（测量被判不可信）
    assert not (base / "results" / "capacity-verdict.json").exists()


# ---------------------------------------------------------------------------
# 6. iptables REJECT 改为 -I 前插（与 ACCEPT 对齐，不再落链尾）
# ---------------------------------------------------------------------------
def test_apply_iptables_reject_is_front_inserted(monkeypatch):
    cmds = []

    def fake_run(cmd, dry_run=False, check=False):
        cmds.append(list(cmd))
        return 0, ""

    monkeypatch.setattr(mock_env, "run", fake_run)
    monkeypatch.setattr(mock_env, "_ipt_has", lambda *a, **k: False)  # 规则均不存在
    ok = mock_env.apply_iptables(False)
    assert ok is True
    inserts = [c for c in cmds if "-I" in c]
    rejects = [c for c in inserts if "REJECT" in c]
    accepts = [c for c in inserts if "ACCEPT" in c]
    assert rejects, f"REJECT 未用 -I 前插：{cmds}"
    assert accepts, "ACCEPT 应用 -I"
    assert not any("-A" in c for c in cmds), f"仍有追加到链尾的 -A：{cmds}"
    # ACCEPT 位序 <= REJECT 位序，保证回环放行先于兜底拒绝
    def pos(c):
        i = c.index("-I")
        return int(c[i + 2]) if len(c) > i + 2 and c[i + 2].isdigit() else 1
    assert pos(accepts[0]) <= pos(rejects[0])


# ---------------------------------------------------------------------------
# 7. --no-iptables 不再静默退 0（要么显式知情，要么拒绝）
# ---------------------------------------------------------------------------
def test_no_iptables_without_optin_refuses_before_touching_system(tmp_path):
    """缺知情标记的 --no-iptables 必须拒绝，且拒绝发生在任何改系统动作之前。

    探针可失败：若门禁被挪到 ensure_certs/apply_hosts 之后，(base/ca) 会被生成、
    hosts 会被改写，下面两条断言随即转红。
    """
    hosts = tmp_path / "hosts"
    sentinel = "127.0.0.1 localhost\nSENTINEL-UNTOUCHED\n"
    hosts.write_text(sentinel, encoding="utf-8")
    base = tmp_path / "base"
    rc = mock_env.main(["--base-dir", str(base),
                        "--hosts-file", str(hosts),
                        "--egress-probe-ip", _TESTNET_IP,
                        "--no-iptables"])   # 故意不给 --i-understand-no-isolation
    assert rc != 0
    assert not (base / "ca").exists(), "拒绝前不应生成证书目录（门禁须早于 ensure_certs）"
    assert hosts.read_text(encoding="utf-8") == sentinel, \
        "拒绝前不应改写 hosts（门禁须早于 apply_hosts）"


def test_dry_run_no_iptables_still_ok(tmp_path):
    """dry-run 不落任何改动：--no-iptables 无需知情标记也应放行（保持既有冒烟用例契约）。"""
    hosts = tmp_path / "hosts"
    hosts.write_text("127.0.0.1 localhost\n", encoding="utf-8")
    r = subprocess.run(
        [sys.executable, os.path.join(_LOADTEST, "mock_env.py"),
         "--dry-run", "--no-iptables", "--hosts-file", str(hosts),
         "--base-dir", str(tmp_path / "base")],
        capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


# ---------------------------------------------------------------------------
# 8. reset_state_dir 前缀白名单补齐
# ---------------------------------------------------------------------------
def test_reset_state_dir_covers_all_transient_prefixes(tmp_path):
    keep = ["accounts.db", "keep-me.json", "unrelated.log", "yiban.db-wal"]
    transient = ["sign-state-2026-09-25.json", "sign-daily-x", "sched-run-a",
                 "sched-snapshot-a", "cred-state.json", "probe-state-x",
                 "sched-slot-1", "mail-user-fail-2", "signin-run.lock",
                 "notes.tmp4321"]
    for n in keep + transient:
        (tmp_path / n).write_text("x", encoding="utf-8")
    scale_driver.reset_state_dir(str(tmp_path))
    left = set(os.listdir(tmp_path))
    for n in transient:
        assert n not in left, f"{n} 应被清理（漏白名单）"
    for n in keep:
        assert n in left, f"{n} 属无关文件，不得被误删"


# ---------------------------------------------------------------------------
# 9. read_meminfo 读取失败 ⇒ 显式报错（不得假报内存饱和）
# ---------------------------------------------------------------------------
def test_read_meminfo_raises_on_unreadable():
    with pytest.raises(Exception) as cm:
        concurrency_probe.read_meminfo("/nonexistent/proc/meminfo-xyz")
    assert "meminfo" in str(cm.value).lower() or "内存" in str(cm.value)


def test_concurrency_probe_mem_abort_not_reported_when_meminfo_unreadable(
        tmp_path, monkeypatch, capsys):
    """内存不可读时不得给出「内存饱和」的假结论：显式报错退出、非零。"""
    calls = {"n": 0}

    def _raise(_path=None):
        calls["n"] += 1
        raise concurrency_probe.MeminfoUnavailable("无法读取 /proc/meminfo")

    monkeypatch.setattr(concurrency_probe, "read_meminfo", _raise)
    monkeypatch.setattr(concurrency_probe, "load_phones",
                        lambda *a, **k: [f"13{i:09d}" for i in range(8)])
    db = tmp_path / "yiban.db"
    db.write_bytes(b"")
    envf = tmp_path / "test.env"
    envf.write_text("", encoding="utf-8")
    rc = concurrency_probe.main([
        "--repo", str(tmp_path), "--env", str(envf), "--db", str(db),
        "--ca", str(tmp_path / "ca.pem"), "--mock-log", "",
        "--k-list", "1", "--per-proc", "4", "--outdir", str(tmp_path / "out")])
    assert rc != 0
    out = (capsys.readouterr().out + "").lower()
    assert "内存饱和" not in out


# ---------------------------------------------------------------------------
# 10. 归属读取：库缺失 ⇒ None（owner 检查不得空过放行）
# ---------------------------------------------------------------------------
def test_account_owners_missing_db_returns_none(tmp_path):
    assert isolation._account_owners(str(tmp_path / "no-such.db")) is None


def test_loadtest_target_refuses_missing_db(tmp_path):
    """库缺失 ⇒ 归属读不到 ⇒ 目标门拒绝（fail-closed），不再被空列表空过放行。"""
    missing = str(tmp_path / "no-such.db")
    with pytest.raises(isolation.IsolationError):
        isolation.assert_loadtest_target(
            missing, isolation.loadtest_db_fingerprint(missing))


def test_account_owners_reads_special_char_path(tmp_path):
    """含空格的库路径也要能只读读到 owner（只读 URI 经 pathlib 转义，不拼裸路径）。"""
    d = tmp_path / "dir with space"
    d.mkdir()
    p = d / "lt.db"
    conn = sqlite3.connect(str(p))
    try:
        conn.execute("CREATE TABLE accounts (owner TEXT)")
        conn.execute("INSERT INTO accounts (owner) VALUES ('a@mock.invalid')")
        conn.commit()
    finally:
        conn.close()
    assert isolation._account_owners(str(p)) == ["a@mock.invalid"]
