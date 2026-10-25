# -*- coding: utf-8 -*-
"""夹具回放机制（ziqz 代码面）：同一夹具原样回放；未实拍状态显式拒答。

标签：K · 登录协议与自研链路

判据：
  ① 同一夹具，回放字节与文件逐字节一致；JSON 条目逐字段一致。
  ② captured 状态可被回放；synthetic 与缺条目状态被拒答（未实拍不编造）。
  ③ 真起假上游：回放端点响应体与夹具一致；请求未实拍状态得 501 与显式标记。

依赖：本机回环 HTTP（127.0.0.1）；不触网。无 skip。
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from yiban.adapters.protocol_fake import MockConfig, create_servers
from yiban.adapters.protocol_fake.replay import (
    PROVENANCE_CAPTURED,
    ReplayNotCovered,
    ReplayUnavailable,
)


# --- ① 夹具与回放逐字节一致 ------------------------------------------------
def test_each_fixture_replays_byte_for_byte(replay_catalog):
    assert replay_catalog.names(), "回放清单为空：判定面失效"
    for name in replay_catalog.names():
        with_fixture = replay_catalog.load(name)
        on_disk = (replay_catalog.fixtures_dir / with_fixture.file).read_bytes()
        assert with_fixture.body == on_disk, f"{name} 的回放字节与夹具不一致"
        assert with_fixture.text == on_disk.decode("utf-8"), f"{name} 的文本不一致"
        if with_fixture.file.endswith(".json"):
            assert with_fixture.json() == json.loads(on_disk.decode("utf-8")), \
                f"{name} 的回放字段与夹具不一致"


def test_resolve_returns_the_registered_entry(replay_catalog):
    for name in replay_catalog.names():
        entry = replay_catalog.load(name)
        got = replay_catalog.find(entry.endpoint, entry.state)
        assert got is not None and got.name == entry.name


# --- ② 来源两档：captured 可回放，synthetic 拒答 ---------------------------

def test_captured_state_is_served(replay_catalog):
    resp = replay_catalog.serve("GET /device/student/index/getState", "ok")
    assert resp.provenance == PROVENANCE_CAPTURED


def test_unrecorded_states_are_listed_and_refused(replay_catalog):
    assert replay_catalog.unrecorded(), "未实拍清单为空：合成夹具没被登记"
    for name in replay_catalog.unrecorded():
        entry = replay_catalog.load(name)
        assert entry.provenance != PROVENANCE_CAPTURED
        with pytest.raises(ReplayUnavailable) as excinfo:
            replay_catalog.serve(entry.endpoint, entry.state)
        assert "未实拍" in str(excinfo.value)


def test_missing_state_on_covered_endpoint_is_refused(replay_catalog):
    """State 3/4/5 未实拍：端点在面内但状态缺条目 ⇒ 拒答，不回落 State 0。"""
    with pytest.raises(ReplayUnavailable):
        replay_catalog.serve("GET /nightAttendance/student/index/signPosition", "state3")


def test_endpoint_outside_replay_is_not_covered(replay_catalog):
    with pytest.raises(ReplayNotCovered):
        replay_catalog.serve("GET /no-such-endpoint", "ok")


def test_file_path_loaded_handler_can_load_replay(replay_catalog):
    """drill 与两个 e2e 按文件路径装载 __init__.py：该形态也必须能装载回放层。

    按文件路径装载没有包上下文，`from . import replay` 会失败；回退装载器必须
    把子模块登记进 sys.modules（dataclasses 在 exec 期间按 __module__ 回查）。
    """
    import importlib.util
    from pathlib import Path

    init = (Path(__file__).resolve().parents[2]
            / "yiban" / "adapters" / "protocol_fake" / "__init__.py")
    spec = importlib.util.spec_from_file_location("_fake_upstream_file_path_probe", init)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    catalog = module._replay_module().ReplayCatalog.from_dir(replay_catalog.fixtures_dir)
    assert catalog.load("device_state_ok").body == \
        replay_catalog.load("device_state_ok").body


# --- ③ 假上游边界：回放与拒答都走真 HTTP ----------------------------------

@pytest.fixture()
def replay_server(replay_catalog):
    config = MockConfig(replay_dir=str(replay_catalog.fixtures_dir))
    servers, _state, config = create_servers(port=0, enable_ipv6=False, config=config)
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for thread in threads:
        thread.start()
    try:
        yield servers[0].server_address[1], config, replay_catalog
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=5)


def _get(port, path):
    return urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10)


def test_server_replays_captured_fixture(replay_server):
    port, _config, replay_catalog = replay_server
    expected = replay_catalog.load("device_state_ok").body
    with _get(port, "/device/student/index/getState") as resp:
        assert resp.status == 200
        assert resp.read() == expected, "假上游的回放体与夹具不一致"


def test_server_refuses_unrecorded_state(replay_server):
    port, config, _catalog = replay_server
    config.replay_state = "session_expired"
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _get(port, "/base/c/auth/yiban")
    err = excinfo.value
    assert err.code == 501
    payload = json.loads(err.read().decode("utf-8"))
    assert payload["replay"] == "unrecorded"
    assert "未实拍" in payload["msg"]


def test_server_refuses_missing_state_on_covered_endpoint(replay_server):
    port, config, _catalog = replay_server
    config.replay_state = "state3"
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _get(port, "/nightAttendance/student/index/signPosition")
    assert excinfo.value.code == 501
