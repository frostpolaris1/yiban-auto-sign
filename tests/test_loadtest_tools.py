#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""压测工具链轻量冒烟（秒级，不进常规重负载）。

标签：J · 运维：部署/备份/发布
覆盖：mock_yiban 全部接口形状 + 失败注入 + JSONL 落盘 + 配置热读 + 默认只绑 loopback；
    scale_driver 的解析/统计纯函数（N=2 模拟日志）；concurrency_probe 的切片、锁错误、
    饱和点判定；capacity_probe 的容量换算与建议值（实测 × 2/3、执行体数、硬件上限）；
    mock_env 的 hosts 标记块读写与 --dry-run 幂等；五个脚本的 --help 可执行性。
对应实现：`scripts/loadtest/` 下的 mock_yiban、scale_driver、concurrency_probe、
    capacity_probe、mock_env、seed_accounts。
关键断言：容量结论的可信方向——未饱和只能当**下界**、硬件上限要在校准前刹车、
    环境准备失败必须中断阶梯而不是给出建议值。
依赖：pytest（本文件是模块级函数用例）；起本地 loopback HTTP mock 服务与
    `sys.executable --help` 子进程；端到端用例带 skipif，需 `YIBAN_LOADTEST_E2E=1`
    且真实 signin 进程 + TLS + /etc/hosts（要 root/测试机），默认跳过；不连外网。

`mock_env` 的 hosts 读写只在 tmp_path 的副本上做，不改本机 /etc/hosts。
"""

from __future__ import annotations

import http.client
import importlib
import json
import os
import subprocess
import sys
import threading
import time

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_LOADTEST = os.path.join(_ROOT, "scripts", "loadtest")
if _LOADTEST and os.path.dirname(_LOADTEST) not in sys.path:
    sys.path.insert(0, os.path.dirname(_LOADTEST))

mock_yiban = importlib.import_module("loadtest.mock_yiban")
mock_env = importlib.import_module("loadtest.mock_env")
scale_driver = importlib.import_module("loadtest.scale_driver")
concurrency_probe = importlib.import_module("loadtest.concurrency_probe")
capacity_probe = importlib.import_module("loadtest.capacity_probe")
isolation = importlib.import_module("loadtest.isolation")


# ---------------------------------------------------------------------------
# 工具：进程内起 mock（明文 HTTP，端口 0）
# ---------------------------------------------------------------------------
class MockServer:
    def __init__(self, tmp_path, **cfg):
        state = mock_yiban.MockState(log_path=str(tmp_path / "mock.jsonl"))
        config = mock_yiban.MockConfig(**cfg)
        servers, state, config = mock_yiban.create_servers(
            host="127.0.0.1", port=0, cert=None, key=None,
            state=state, config=config, enable_ipv6=False,
        )
        self.server = servers[0]
        self.port = self.server.server_address[1]
        self.state = state
        self.config = config
        self.log_path = str(tmp_path / "mock.jsonl")
        self._t = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._t.start()

    def request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        loc = resp.getheader("Location")
        status = resp.status
        conn.close()
        return status, data, loc

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def mock(tmp_path):
    srv = MockServer(tmp_path)
    yield srv
    srv.close()


# ---------------------------------------------------------------------------
# mock_yiban
# ---------------------------------------------------------------------------
def test_mock_all_endpoint_shapes(mock):
    """登录链 4 步 + 签到 2 步 + 探针/运维入口的形状都必须与 signin 调用一致。"""
    st, body, _ = mock.request("GET", "/__health")
    assert st == 200 and json.loads(body)["ok"] is True

    st, body, _ = mock.request("GET", "/code/html")
    html = body.decode("utf-8")
    assert st == 200
    assert 'id="key"' in html and "BEGIN PUBLIC KEY" in html
    assert "var page_use = 'mockpageuse12345'" in html

    st, body, _ = mock.request("POST", "/code/usersure", body="oauth_uname=1")
    assert st == 200 and json.loads(body)["code"] == "s200"

    st, body, loc = mock.request("GET", "/iframe/index?act=iapp7463")
    assert st == 302 and "verify_request=" in (loc or "")
    assert loc.startswith("https://api.uyiban.com/base/c/auth/yiban")

    st, body, _ = mock.request("GET", "/base/c/auth/yiban?verifyRequest=x&CSRF=y")
    assert st == 200 and json.loads(body)["code"] == 0

    st, body, _ = mock.request("GET", "/nightAttendance/student/index/signPosition?CSRF=y")
    data = json.loads(body)
    assert st == 200 and data["code"] == 0
    assert data["data"]["Position"][0]["Name"] == "MockTask"
    assert data["data"]["Range"]["StartTime"] < data["data"]["Range"]["EndTime"]

    st, body, _ = mock.request("POST", "/nightAttendance/student/index/signIn?CSRF=y", body="x=1")
    assert st == 200 and json.loads(body)["code"] == 0


def test_mock_unknown_path_404(mock):
    st, _body, _ = mock.request("GET", "/nope")
    assert st == 404


def test_mock_failure_injection(tmp_path):
    """失败注入点 signIn 与 login 各验证一次（signin 会据此走失败分支）。"""
    srv = MockServer(tmp_path, fail_rate=1.0, fail_stage="login")
    try:
        st, body, _ = srv.request("POST", "/code/usersure", body="x=1")
        assert json.loads(body)["code"] != "s200"
    finally:
        srv.close()

    srv2 = MockServer(tmp_path, fail_rate=1.0, fail_stage="signIn")
    try:
        st, body, _ = srv2.request("POST", "/nightAttendance/student/index/signIn", body="x=1")
        assert st == 200 and json.loads(body)["code"] == 1
    finally:
        srv2.close()


def test_mock_config_hot_read(tmp_path):
    """配置文件热读：运行中把 fail_rate 打开即生效，无需重启。"""
    cfg_path = tmp_path / "mock_config.json"
    cfg_path.write_text(json.dumps({"delay_ms": 0, "fail_rate": 0.0, "fail_stage": "login"}),
                        encoding="utf-8")
    srv = MockServer(tmp_path, config_path=str(cfg_path))
    try:
        _st, body, _ = srv.request("POST", "/code/usersure", body="x=1")
        assert json.loads(body)["code"] == "s200"
        cfg_path.write_text(json.dumps({"fail_rate": 1.0, "fail_stage": "login"}), encoding="utf-8")
        _st, body, _ = srv.request("POST", "/code/usersure", body="x=1")
        assert json.loads(body)["code"] == "e001"
    finally:
        srv.close()


def _read_jsonl_rows(path):
    """读 JSONL，跳过正在写入的半行（并行满载时会被读到）。"""
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def test_mock_jsonl_logging(mock):
    """逐请求 JSONL 必须含毫秒时间戳/耗时/结果/并发字段。"""
    mock.request("GET", "/code/html")
    # 服务端在响应 flush 之后才由请求线程落盘：客户端返回不代表行已写入。
    # 只等"文件存在"会在满载时读到空/半行文件（xdist -n 8 下必现间歇失败），
    # 因此轮询到目标行真正出现为止（最多 5s）。
    deadline = time.monotonic() + 5.0
    rows = []
    while time.monotonic() < deadline:
        if os.path.exists(mock.log_path):
            rows = _read_jsonl_rows(mock.log_path)
            if any(x.get("path") == "/code/html" for x in rows):
                break
        time.sleep(0.02)
    assert any(x.get("path") == "/code/html" for x in rows), "应至少落一条 /code/html 的 JSONL"
    r = next(x for x in rows if x["path"] == "/code/html")
    assert r["host"] == "127.0.0.1"
    assert isinstance(r["epoch_ms"], int) and r["epoch_ms"] > 0
    assert "dur_ms" in r and r["outcome"] == "ok"
    assert r["inflight"] >= 1


def test_mock_default_binds_loopback_only():
    """默认参数必须是回环地址，避免误暴露到公网。"""
    # 直接检查 main 的默认值（不实际启动 443）
    src = open(os.path.join(_LOADTEST, "mock_yiban.py"), encoding="utf-8").read()
    assert 'default="127.0.0.1"' in src
    assert 'default="::1"' in src
    assert 'default="0.0.0.0"' not in src


# ---------------------------------------------------------------------------
# scale_driver
# ---------------------------------------------------------------------------
def test_driver_percentile_and_stats():
    vals = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert scale_driver.percentile(vals, 0.5) == 3.0
    s = scale_driver.stats(vals)
    assert s["n"] == 5 and s["min"] == 1.0 and s["max"] == 5.0
    assert scale_driver.percentile([], 0.5) is None


def test_driver_parse_jsonl_cycles_n2(tmp_path):
    """模拟 N=2：两条 /code/html 起点 → 1 个周期；并解析请求耗时与最大并发。"""
    log = tmp_path / "mock.jsonl"
    rows = [
        # 账号 1：6 次请求，起点 t=0ms
        {"epoch_ms": 1000, "path": "/code/html", "method": "GET", "dur_ms": 300, "inflight": 1},
        {"epoch_ms": 1100, "path": "/code/usersure", "method": "POST", "dur_ms": 300, "inflight": 1},
        {"epoch_ms": 1200, "path": "/iframe/index", "method": "GET", "dur_ms": 300, "inflight": 1},
        {"epoch_ms": 1300, "path": "/base/c/auth/yiban", "method": "GET", "dur_ms": 300, "inflight": 1},
        {"epoch_ms": 1400, "path": "/nightAttendance/student/index/signPosition", "method": "GET", "dur_ms": 300, "inflight": 1},
        {"epoch_ms": 1500, "path": "/nightAttendance/student/index/signIn", "method": "POST", "dur_ms": 300, "inflight": 1},
        # 账号 2：起点 t=10875ms（周期 ≈ 9.875s）
        {"epoch_ms": 11875, "path": "/code/html", "method": "GET", "dur_ms": 300, "inflight": 1},
        {"epoch_ms": 11900, "path": "/code/usersure", "method": "POST", "dur_ms": 300, "inflight": 2},
    ]
    with open(log, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    cycles, t_list, max_infl, ge2, n_rec, off = scale_driver.parse_jsonl_cycles(str(log))
    assert cycles == [10.875]
    assert t_list and abs(t_list[0] - 1.8) < 1e-6
    assert max_infl == 2 and ge2 == 1 and n_rec == 8
    assert off > 0


def test_driver_env_and_csv(tmp_path):
    envp = tmp_path / ".env"
    envp.write_text('YIBAN_DB_FILE="/tmp/x.db"\n# c\nYIBAN_GAP=10\n', encoding="utf-8")
    env = scale_driver.load_env_file(str(envp))
    assert env["YIBAN_DB_FILE"] == "/tmp/x.db" and env["YIBAN_GAP"] == "10"
    csv_path = tmp_path / "results.csv"
    row = {k: "" for k in scale_driver.CSV_FIELDS}
    row.update({"label": "smoke", "config": "net300", "N": 2})
    scale_driver.write_csv(str(csv_path), row)
    scale_driver.write_csv(str(csv_path), row)  # 去重：仍只有 1 行数据
    lines = csv_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert lines[1].startswith("smoke,net300,2,")


def test_driver_compute_window():
    from datetime import datetime
    start, end, eff = scale_driver.compute_window(datetime(2026, 9, 14, 6, 0, 0), 420)
    assert eff == 420 and start < end


# ---------------------------------------------------------------------------
# concurrency_probe
# ---------------------------------------------------------------------------
def test_probe_partition_slices_disjoint():
    phones = [f"13{i:09d}" for i in range(20)]
    slices = concurrency_probe.partition_slices(phones, k=4, per_proc=5)
    assert len(slices) == 4
    flat = [p for s in slices for p in s]
    assert len(flat) == len(set(flat)) == 20  # 互不重叠
    assert all(len(s) == 5 for s in slices)
    # 账号不足时提前截断
    assert len(concurrency_probe.partition_slices(phones, k=8, per_proc=5)) == 4


def test_probe_scan_lock_errors():
    text = "WARN 保存会话缓存失败（不影响签到）: database is locked\nall good\n"
    n, hits = concurrency_probe.scan_lock_errors(text)
    assert n >= 2 and "database is locked" in hits


def test_probe_classify_bottlenecks():
    rows = [
        {"K": 1, "machine_cpu_pct": 10, "engine_cpu_onecore_pct": 5,
         "min_available_mb": 900, "lock_errors": 0, "db_write_p95_ms": 1, "oom_killed": False},
        {"K": 2, "machine_cpu_pct": 95, "engine_cpu_onecore_pct": 60,
         "min_available_mb": 800, "lock_errors": 0, "db_write_p95_ms": 2, "oom_killed": False},
        {"K": 4, "machine_cpu_pct": 99, "engine_cpu_onecore_pct": 120,
         "min_available_mb": 100, "lock_errors": 1, "db_write_p95_ms": 600, "oom_killed": False},
    ]
    v = concurrency_probe.classify_bottlenecks(rows, 1700, 150)
    assert v["cpu_sat_k"] == 2
    assert v["mem_sat_k"] == 4
    assert v["db_sat_k"] == 4
    assert v["first_bottleneck"] == (2, "CPU")


# ---------------------------------------------------------------------------
# capacity_probe
# ---------------------------------------------------------------------------
def test_capacity_measured_cycle_adds_back_first_account_gap():
    """周期还原：压测的"平均墙钟"少了"第一个账号不等待间隔"的那部分（gap / n）。

    实测教训（2026-09-16 测试机）：生产间隔档 gap=10 / 每进程 6 个账号，
    平均墙钟 10.321s；补回 10/6 得周期 11.99s，与 2026-09-14 独立实测的
    11.88s 吻合（不补则会高估容量约 16%）。
    """
    assert round(capacity_probe.measured_cycle(10.321, 10, 6), 2) == 11.99
    assert capacity_probe.measured_cycle(1.954, 0, 8) == 1.954   # 无间隔档原样
    assert capacity_probe.measured_cycle(1.954, 10, 0) == 1.954  # 缺 per_proc 时不猜
    assert capacity_probe.executor_capacity(4680, 11.99) == 390
    assert capacity_probe.executor_capacity(4680, 8.0) == 585
    # 退化输入不得抛异常（除零/负数）
    assert capacity_probe.executor_capacity(4680, 0) == 0
    assert capacity_probe.executor_capacity(0, 8.0) == 0


def test_capacity_recommend_applies_two_thirds():
    """用户裁决口径：建议每执行体账号数 = 实测 × 2/3（向下取整，至少 1）。"""
    assert capacity_probe.recommend_per_executor(300) == 200
    assert capacity_probe.recommend_per_executor(369) == 246
    assert capacity_probe.recommend_per_executor(1) == 1     # 不为 0
    assert capacity_probe.executors_needed(5000, 246) == 21
    assert capacity_probe.executors_needed(5000, 0) is None


def test_capacity_not_saturated_is_a_lower_bound_not_a_ceiling():
    """「未触及饱和」是**下界**：需求超出已测范围时不得据此断言"机器不够"。"""
    rows = [{"K": 1, "per_acct_wall_s": 10.3, "degradation_x": 1.0, "machine_cpu_pct": 25},
            {"K": 4, "per_acct_wall_s": 10.4, "degradation_x": 1.01, "machine_cpu_pct": 60}]
    v = capacity_probe.build_verdict(rows, users=5000, window_sec=4680, gap=10)
    assert v["hardware_ceiling_k"] == 4
    assert v["hardware_ceiling_why"] == capacity_probe.NOT_SATURATED
    assert v["verdict_code"] == "needs_wider_ladder"
    text = capacity_probe.format_verdict(v, "production", rows)
    assert "不能据此说机器不够" in text and "K≤4" in text


def test_capacity_hardware_ceiling_stops_before_degradation():
    """硬件上限取「首个劣化>1.5× 或资源饱和」档**之前**的档。"""
    rows = [
        {"K": 1, "per_acct_wall_s": 2.0, "degradation_x": 1.0, "machine_cpu_pct": 20},
        {"K": 2, "per_acct_wall_s": 2.4, "degradation_x": 1.2, "machine_cpu_pct": 40},
        {"K": 4, "per_acct_wall_s": 4.4, "degradation_x": 2.2, "machine_cpu_pct": 60},
    ]
    k, why = capacity_probe.hardware_ceiling(rows)
    assert k == 2 and "劣化" in why
    # 资源饱和同样终止升档（锁错误也算）
    rows2 = [{"K": 1, "per_acct_wall_s": 2.0, "degradation_x": 1.0, "machine_cpu_pct": 95}]
    assert capacity_probe.hardware_ceiling(rows2)[0] is None
    # 全程无饱和时给出最后测到的档，并诚实说明
    rows3 = [{"K": 1, "per_acct_wall_s": 2.0, "degradation_x": 1.0, "machine_cpu_pct": 10},
             {"K": 2, "per_acct_wall_s": 2.1, "degradation_x": 1.05, "machine_cpu_pct": 30}]
    assert capacity_probe.hardware_ceiling(rows3) == (2, "未触及饱和")


def test_capacity_verdict_is_feasible_only_when_machine_holds():
    """结论必须做「需要几个执行体 ≤ 本机实测能跑几个」的对照，不够就明说不够。"""
    rows = [
        {"K": 1, "per_acct_wall_s": 8.0, "degradation_x": 1.0, "machine_cpu_pct": 25,
         "throughput_acct_per_h": 400},
        {"K": 2, "per_acct_wall_s": 8.3, "degradation_x": 1.04, "machine_cpu_pct": 45},
        {"K": 4, "per_acct_wall_s": 13.0, "degradation_x": 1.63, "machine_cpu_pct": 80},
    ]
    v = capacity_probe.build_verdict(rows, users=5000, window_sec=4680, gap=10)
    # rows 里没有 per_proc → 不做"补回首账号间隔"的还原，周期取实测墙钟 8.0s
    assert v["ok"] and v["single_executor_capacity"] == 585
    assert v["recommended_per_executor"] == 390
    assert v["executors_needed"] == 13
    assert v["hardware_ceiling_k"] == 2          # K=4 劣化 1.63× > 1.5×
    assert v["feasible_on_this_machine"] is False
    text = capacity_probe.format_verdict(v, "production", rows)
    assert "不够" in text and "13 个执行体" in text and "2（单账号耗时劣化" in text

    # 小规模则可行：同一台机器带 300 个账号
    v2 = capacity_probe.build_verdict(rows, users=300, window_sec=4680, gap=10)
    assert v2["executors_needed"] == 1 and v2["feasible_on_this_machine"] is True
    assert "本机够用" in capacity_probe.format_verdict(v2, "production", rows)


def test_capacity_profiles_are_consistent():
    """档位定义自检：K 阶梯递增、每进程账号数>0、间隔与延迟非负。"""
    assert capacity_probe.PROFILES, "档位表不能为空"
    for name, cfg in capacity_probe.PROFILES.items():
        assert cfg["k_list"] == sorted(cfg["k_list"]), name
        assert all(k >= 1 for k in cfg["k_list"]), name
        assert cfg["per_proc"] > 0, name
        assert cfg["gap"] >= 0 and cfg["delay_ms"] >= 0, name


# ---------------------------------------------------------------------------
# mock_env
# ---------------------------------------------------------------------------
def test_mock_env_hosts_block_idempotent(tmp_path):
    hosts = tmp_path / "hosts"
    hosts.write_text("127.0.0.1 localhost\n", encoding="utf-8")
    backup = str(tmp_path / "hosts.orig")
    domains = ["oauth.yiban.cn", "f.yiban.cn"]
    changed = mock_env.apply_hosts(str(hosts), domains, backup)
    assert changed
    text1 = hosts.read_text(encoding="utf-8")
    assert mock_env.HOSTS_BEGIN in text1 and "127.0.0.1 oauth.yiban.cn" in text1
    assert "::1 f.yiban.cn" in text1
    # 幂等：第二次不改动
    assert mock_env.apply_hosts(str(hosts), domains, backup) is False
    assert hosts.read_text(encoding="utf-8") == text1
    # 还原：标记块删除、原有内容保留，再还原也无变化
    assert mock_env.restore_hosts(str(hosts), backup) is True
    assert mock_env.HOSTS_BEGIN not in hosts.read_text(encoding="utf-8")
    assert "127.0.0.1 localhost" in hosts.read_text(encoding="utf-8")
    assert mock_env.restore_hosts(str(hosts), backup) is False


def test_mock_env_dry_run_ok(tmp_path):
    """--dry-run 不落任何改动：无副作用，无需 --egress-probe-ip / opt-in，退码 0。"""
    hosts = tmp_path / "hosts"
    hosts.write_text("", encoding="utf-8")
    r = subprocess.run(
        [sys.executable, os.path.join(_LOADTEST, "mock_env.py"),
         "--dry-run", "--no-iptables", "--hosts-file", str(hosts),
         "--base-dir", str(tmp_path / "base")],
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stderr
    assert "[dry-run]" in r.stdout or "证书" in r.stdout


def test_mock_env_selfcheck_without_setup(tmp_path):
    """未做 hosts 改写时自检必须判失败（防止"假通过"）。"""
    hosts = tmp_path / "hosts"
    hosts.write_text("", encoding="utf-8")
    ok, _rows = mock_env.selfcheck(["example.invalid"], str(hosts), ipv6=False)
    assert ok is False


def _patch_setup_peripherals(monkeypatch, tmp_path, *, egress_ok, ipt_ok):
    """把搭建路径上的外部副作用换掉，只留"退出码是否反映实情"这一条被测行为。

    含还原桩：搭建失败时 main 的 finally 会自动还原已生效改动（fail-closed 清理），
    若不打桩就会在测试里真调 iptables。--egress-probe-ip 现为搭建路径必填项。
    """
    monkeypatch.setattr(mock_env.sys, "platform", "linux")  # 绕过"仅 Linux"前置
    # 绕过"需要 root"前置：CI runner 是普通用户（Linux 有 geteuid 且非 0 → main 直接退 2）
    # raising=False：Windows 的 os 没有 geteuid，本地照样绿
    monkeypatch.setattr(mock_env.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(mock_env, "ensure_certs", lambda *a, **k: {})
    monkeypatch.setattr(mock_env, "apply_hosts", lambda *a, **k: True)
    monkeypatch.setattr(mock_env, "verify_zero_egress", lambda *a, **k: egress_ok)
    monkeypatch.setattr(mock_env, "apply_iptables", lambda ipv6, dry_run=False: ipt_ok)
    monkeypatch.setattr(mock_env, "restore_hosts", lambda *a, **k: False)
    monkeypatch.setattr(mock_env, "restore_iptables", lambda *a, **k: False)
    return ["--hosts-file", str(tmp_path / "hosts"), "--base-dir", str(tmp_path / "base"),
            "--egress-probe-ip", "203.0.113.7"]


def test_mock_env_selfcheck_failure_returns_nonzero(tmp_path, monkeypatch):
    """零真实外联自检 FAIL 时 main 必须非零退出（此前返回值被丢弃 → 假"环境就绪"）。"""
    cli = _patch_setup_peripherals(monkeypatch, tmp_path, egress_ok=False, ipt_ok=True)
    assert mock_env.main(cli) == 1


def test_mock_env_iptables_failure_returns_nonzero(tmp_path, monkeypatch):
    """出站兜底规则没装上也必须非零退出：缺 REJECT 时压测会直连真实易班。"""
    cli = _patch_setup_peripherals(monkeypatch, tmp_path, egress_ok=True, ipt_ok=False)
    assert mock_env.main(cli) == 1


def test_mock_env_all_green_returns_zero(tmp_path, monkeypatch):
    """自检通过且规则就位：退出码保持 0（不要把成功路径一并判失败）。"""
    cli = _patch_setup_peripherals(monkeypatch, tmp_path, egress_ok=True, ipt_ok=True)
    assert mock_env.main(cli) == 0


def test_capacity_run_failure_message_includes_child_output():
    """子进程非零退出时，失败信息要带上它的输出尾部（只报退出码无从定位）。

    子进程吐出的标记由运行期拼接：它不会出现在命令行回显里，故断言只可能来自
    真正被带出来的输出（否则"命令里恰好有这串字"会让本用例假绿）。
    """
    with pytest.raises(RuntimeError) as cm:
        capacity_probe._run([sys.executable, "-c",
                             "import sys; print('-'.join(['probe', 'needle', '42']));"
                             " sys.exit(3)"])
    msg = str(cm.value)
    assert "退出码 3" in msg
    assert "probe-needle-42" in msg


def test_capacity_aborts_ladder_when_env_prep_fails(tmp_path, monkeypatch):
    """mock_env 报告环境未就绪：不跑 K 阶梯、非零退出，并把子进程输出带出来。"""
    monkeypatch.setattr(capacity_probe, "ensure_platform", lambda: None)
    monkeypatch.setattr(capacity_probe, "restore_env", lambda *a, **k: None)
    ran = []

    def _boom(*a, **k):
        raise RuntimeError("命令失败（退出码 1）：mock_env.py\n"
                           "  [FAIL] oauth.yiban.cn -> 203.0.113.9")

    monkeypatch.setattr(capacity_probe, "prepare_env", _boom)
    monkeypatch.setattr(capacity_probe, "run_ladder", lambda *a, **k: ran.append(1))
    with pytest.raises(SystemExit) as cm:
        capacity_probe.main(["--repo", str(tmp_path), "--base-dir", str(tmp_path),
                             "--profile", "simulated"])
    msg = str(cm.value)
    assert "不跑 K 阶梯" in msg
    assert "[FAIL] oauth.yiban.cn -> 203.0.113.9" in msg, "失败信息须带上子进程输出"
    assert ran == [], "环境未就绪时不得跑 K 阶梯"


# ---------------------------------------------------------------------------
# --help 冒烟（所有脚本可被解释器加载）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["mock_yiban", "mock_env", "seed_accounts",
                                  "scale_driver", "concurrency_probe"])
def test_scripts_help(name):
    r = subprocess.run([sys.executable, os.path.join(_LOADTEST, f"{name}.py"), "--help"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "usage:" in r.stdout.lower()


# ---------------------------------------------------------------------------
# 可选端到端（仅测试机/root，默认跳过）
# ---------------------------------------------------------------------------
@pytest.mark.skipif(os.environ.get("YIBAN_LOADTEST_E2E") != "1",  #端到端要 root 与测试机：不进常规套件，显式开环境变量才收集
                    reason="端到端需测试机 root + hosts/TLS，设置 YIBAN_LOADTEST_E2E=1 开启")
def test_scale_driver_e2e_optional(tmp_path):
    repo = os.environ["YIBAN_LOADTEST_REPO"]
    env = os.environ["YIBAN_LOADTEST_ENV"]
    db = os.environ["YIBAN_LOADTEST_DB"]
    ca = os.environ["YIBAN_LOADTEST_CA"]
    mock_log = os.environ["YIBAN_LOADTEST_MOCK_LOG"]
    r = subprocess.run(
        [sys.executable, os.path.join(_LOADTEST, "scale_driver.py"),
         "--repo", repo, "--env", env, "--db", db, "--n", "2", "--label", "e2e",
         "--window-sec", "20", "--ca", ca, "--mock-log", mock_log,
         "--db-fingerprint", isolation.loadtest_db_fingerprint(db),
         "--outdir", str(tmp_path)],
        capture_output=True, text=True, timeout=300,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads((tmp_path / "run-e2e-n2.json").read_text(encoding="utf-8"))
    # 严格串行的直接证据：单进程运行期间 mock 侧观测到的最大并发为 1
    assert out["max_inflight_in_run"] == 1
