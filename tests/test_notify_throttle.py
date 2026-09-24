# -*- coding: utf-8 -*-
"""notify 同类型告警节流的跨进程化（磁盘持久化）。

标签：H · 通知：邮件与推送
覆盖：`_throttle_due` 放行即写 `$YIBAN_STATE_DIR/notify-throttle.json`；清空内存节流态
    （模拟另一进程/重启）后窗口内同标题仍被磁盘判定拦下；窗口过期重新放行、
    不同标题互不干扰；cooldown=0 关闭节流且不产生磁盘文件；损坏节流文件归档留证 +
    warning 并按空表处理；写盘顺带清过期条目；冻结时钟下并发同刻恰好一次放行；
    走完整 `send()` 路径时跨进程节流对真实推送生效、force 仍绕过。
对应实现：`yiban/notify/ledger.py`（`_throttle_due`、`_throttle_ts`）与
    `yiban/notify/transport.py`（`send`）。
关键断言：**磁盘是唯一事实源**——修复前只维护进程内字典，web 与 signin 各持一份，
    同一告警在窗口内可各放行一条（第三方配额被双份刷、管理员手机收两条），
    且进程重启即清零。
依赖：pytest + monkeypatch；时间用 `_freeze_time` 替换 notify 模块级 `time` 引用
    （不动全局 time 模块）；推送出口打桩，不连 Server酱、不触网；
    每用例独立 tmp_path，磁盘节流文件互不残留。

`_isolate` 显式关掉 `YIBAN_NOTIFY_URGENT_ONLY`：该档默认开，不关的话非紧急用例会被
档位短路，测不到节流本身。
"""
import json
import logging
import os
import threading
import types

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 直连实现包（旧的 scripts/notify.py 兼容壳已删除）：公共面走包，内部名走子模块。
from yiban import notify  # noqa: E402
from yiban.infra import account_crypto  # noqa: E402
from yiban.notify import ledger as notify_ledger  # noqa: E402
from yiban.notify import transport as notify_transport  # noqa: E402

KEY = "f" * 64
SCT_KEY = "SCT406257TESTTESTTESTTESTTEST"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """隔离环境：状态目录指向临时目录 + 重置 notify 模块级内存态 + 清空 YIBAN_NOTIFY_*。

    每用例独立 tmp_path（磁盘节流文件互不残留）；notify 是全局单例，内存态
    必须在用例间复位。
    """
    monkeypatch.setenv("YIBAN_STATE_DIR", str(tmp_path))  #节流文件的落点就是这个键：指错目录等于用例之间互相看见
    monkeypatch.setenv("YIBAN_ACCOUNTS_KEY", KEY)
    monkeypatch.setenv("YIBAN_ENV_FILE", str(tmp_path / "no-such.env"))
    notify_ledger._throttle_ts.clear()
    notify_ledger._general_daily["state"].update({"date": "", "count": 0})
    notify_ledger._urgent_daily["state"].update({"date": "", "count": 0})
    for ledger in (notify_ledger._general_daily, notify_ledger._urgent_daily):
        ledger["notice"].update({"pending": False, "notified": False, "warned": False})
    notify_ledger._skip_logged.clear()  #跳过原因日志按窗口去重：不清会让后一个用例看不到本该出现的那条 warning
    for k in list(os.environ):
        if k.startswith("YIBAN_NOTIFY_"):
            monkeypatch.delenv(k)
    # 本文件测的是节流（磁盘持久化 + 跨进程），而「仅推送重要告警」默认开——不显式
    # 关掉，非紧急用例会被档位短路，测不到节流本身。
    monkeypatch.setenv("YIBAN_NOTIFY_URGENT_ONLY", "0")
    yield


def _throttle_file(tmp_path):
    return os.path.join(str(tmp_path), "notify-throttle.json")


def _read_disk(tmp_path):
    with open(_throttle_file(tmp_path), encoding="utf-8") as f:
        return json.load(f)


def _set_cooldown(monkeypatch, secs):
    monkeypatch.setenv("YIBAN_NOTIFY_COOLDOWN", str(secs))


def _freeze_time(monkeypatch, clock):
    """冻结 notify 视角的 time.time（只 patch notify 模块的 time 引用）。

    不去动全局 time 模块（那是全进程共享对象），仅替换 notify 模块级
    `time` 名字，同文件其它用例与 pytest 自身计时不受影响。
    """
    real = notify_transport.time
    fake = types.ModuleType("fake_time")
    fake.time = lambda: clock[0]
    fake.strftime = real.strftime
    monkeypatch.setattr(notify_transport, "time", fake)


def _enc(secret):
    return json.dumps(account_crypto.encrypt_text(secret, account_crypto.load_key()),
                      ensure_ascii=False)


# ---------------------------------------------------------------------------
# 单元：放行落盘 / 跨进程判定 / 过期重放 / 不同标题独立
# ---------------------------------------------------------------------------

def test_due_persists_to_disk(tmp_path, monkeypatch):
    """放行时写盘：磁盘节流表存在且记录本次时间戳。"""
    _set_cooldown(monkeypatch, 3600)
    assert notify_transport._throttle_due("磁盘标题") is True
    disk = _read_disk(tmp_path)
    assert list(disk.keys()) == ["磁盘标题"]
    assert isinstance(disk["磁盘标题"], float)


def test_cross_process_reload_sees_disk(tmp_path, monkeypatch):
    """模拟另一进程（清空内存节流态）：窗口内同标题仍被磁盘判定拦下。"""
    _set_cooldown(monkeypatch, 3600)
    assert notify_transport._throttle_due("跨进程标题") is True
    notify_ledger._throttle_ts.clear()  # 新进程不共享内存
    assert notify_transport._throttle_due("跨进程标题") is False, \
        "磁盘节流表必须兜住另一进程/进程重启后的同类告警"


def test_window_expiry_allows_again(tmp_path, monkeypatch):
    """窗口过期后同标题重新放行，且磁盘时间戳随之更新。"""
    clock = [1000000.0]
    _freeze_time(monkeypatch, clock)
    _set_cooldown(monkeypatch, 60)
    assert notify_transport._throttle_due("过期标题") is True
    assert notify_transport._throttle_due("过期标题") is False  # 窗口内
    clock[0] += 61
    assert notify_transport._throttle_due("过期标题") is True   # 窗口外重新放行
    assert _read_disk(tmp_path)["过期标题"] == clock[0], "放行须刷新磁盘时间戳"


def test_distinct_titles_independent(tmp_path, monkeypatch):
    """不同标题互不干扰：各自独立计窗。"""
    _set_cooldown(monkeypatch, 3600)
    assert notify_transport._throttle_due("标题A") is True
    assert notify_transport._throttle_due("标题A") is False
    assert notify_transport._throttle_due("标题B") is True
    assert notify_transport._throttle_due("标题B") is False
    assert sorted(_read_disk(tmp_path).keys()) == ["标题A", "标题B"]


def test_cooldown_zero_disabled_and_no_disk(tmp_path, monkeypatch):
    """cooldown=0 关闭节流：每次都放行，且不产生磁盘文件。"""
    _set_cooldown(monkeypatch, 0)
    assert notify_transport._throttle_due("不限标题") is True
    assert notify_transport._throttle_due("不限标题") is True
    assert not os.path.exists(_throttle_file(tmp_path)), "关闭节流不应碰磁盘"


def test_prune_removes_stale_entries(tmp_path, monkeypatch):
    """写盘时顺带清理过期条目：磁盘文件不会因动态标题无限增长。"""
    clock = [1000000.0]
    _freeze_time(monkeypatch, clock)
    _set_cooldown(monkeypatch, 60)
    assert notify_transport._throttle_due("旧标题") is True
    clock[0] += 61
    assert notify_transport._throttle_due("新标题") is True
    disk = _read_disk(tmp_path)
    assert "旧标题" not in disk, "过窗口的条目应在下次写盘时被清理"
    assert list(disk.keys()) == ["新标题"]


def test_corrupt_file_archived_warned_and_restarts(tmp_path, monkeypatch, caplog):
    """写坏 JSON：放行（按空表）且 warning + 归档留证，不静默。"""
    _set_cooldown(monkeypatch, 3600)
    path = _throttle_file(tmp_path)
    with open(path, "w", encoding="utf-8") as f:
        f.write("{broken-json")
    with caplog.at_level(logging.WARNING, logger="notify"):
        assert notify_transport._throttle_due("损坏标题") is True
    assert "损坏" in caplog.text, "损坏必须记 warning，不得静默重置"
    corrupts = [n for n in os.listdir(str(tmp_path))
                if n.startswith("notify-throttle.json.corrupt-")]
    assert len(corrupts) == 1, "损坏文件应归档留证"
    assert _read_disk(tmp_path)["损坏标题"] > 0, "从空表重新放行并写回"


# ---------------------------------------------------------------------------
# 并发：冻结时钟下同标题窗口内恰好一次放行（跨进程语义的确定性验证）
# ---------------------------------------------------------------------------

def test_concurrent_same_title_only_one_passes(tmp_path, monkeypatch):
    """8 线程冻结时钟同刻请求同标题：恰好一次放行、7 次被磁盘判定拦下。

    时间冻结保证窗口判定确定（无需 sleep）；内存快速路径 + 磁盘文件锁共同
    保证"窗口内每标题只放一条"的跨进程契约在同进程多线程下同样成立。
    """
    clock = [2000000.0]
    _freeze_time(monkeypatch, clock)
    _set_cooldown(monkeypatch, 60)
    n = 8
    barrier = threading.Barrier(n)
    results = []
    guard = threading.Lock()

    def _worker():
        barrier.wait()
        ok = notify_transport._throttle_due("并发标题")
        with guard:
            results.append(ok)

    threads = [threading.Thread(target=_worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1, f"窗口内同标题只能放行一条，实际 {results}"
    assert results.count(False) == n - 1


# ---------------------------------------------------------------------------
# 集成：走完整 send() 路径，节流对真实推送生效且跨进程不双发
# ---------------------------------------------------------------------------

def test_send_throttle_works_across_process(tmp_path, monkeypatch):
    """send() 两次同标题第二次被节流；清空内存模拟另一进程后仍被磁盘拦下；
    force 绕过。"""
    monkeypatch.setenv("YIBAN_NOTIFY_TYPE", "serverchan")
    monkeypatch.setenv("YIBAN_NOTIFY_SECRET_ENC", _enc(SCT_KEY))
    monkeypatch.setenv("YIBAN_NOTIFY_COOLDOWN", "3600")
    calls = []

    class FakeResp:
        def json(self):
            return {"code": 0}

    monkeypatch.setattr(notify_transport.requests, "post", lambda *a, **k: calls.append(1) or FakeResp())
    assert notify.send("集成标题", "第一次") is True
    assert notify.send("集成标题", "第二次") is False  # 窗口内节流
    assert len(calls) == 1
    # 模拟另一进程重启：内存节流态清零（新进程）——磁盘判定仍兜住
    notify_ledger._throttle_ts.clear()
    assert notify.send("集成标题", "第三次") is False, "跨进程不得双发"
    assert len(calls) == 1
    # force 绕过节流与预算（测试推送语义不变）
    assert notify.send("集成标题", "第四次", force=True) is True
    assert len(calls) == 2


def test_send_throttle_zero_still_allows_each(tmp_path, monkeypatch):
    """cooldown=0 时同标题逐条放行（节流关闭语义回归）。"""
    monkeypatch.setenv("YIBAN_NOTIFY_TYPE", "serverchan")
    monkeypatch.setenv("YIBAN_NOTIFY_SECRET_ENC", _enc(SCT_KEY))
    monkeypatch.setenv("YIBAN_NOTIFY_COOLDOWN", "0")
    calls = []

    class FakeResp:
        def json(self):
            return {"code": 0}

    monkeypatch.setattr(notify_transport.requests, "post", lambda *a, **k: calls.append(1) or FakeResp())
    assert notify.send("不节流标题", "1") is True
    assert notify.send("不节流标题", "2") is True
    assert len(calls) == 2
    assert not os.path.exists(_throttle_file(tmp_path)), "关闭节流不产生磁盘文件"
