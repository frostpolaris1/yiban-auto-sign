# -*- coding: utf-8 -*-
"""P1-1 跨进程 RMW 竞态修复回归测试：notify 每日预算账本 = 单次文件锁临界区。

修复前的缺陷（已实证）：
- `_consume_daily_budget` 的流程是 进程内锁 → `_roll_locked`（内部拿文件锁读盘
  刷新内存、随后释放）→ 内存 count+1 → `_sync_ledger_to_disk`（重新拿文件锁整块
  覆盖写盘）。web（常驻）与 signin（cron 新进程）不共享进程内锁，进程 B 可在
  A 的两次文件锁之间插入完整操作：双方各读旧 count=0、各 +1、各写盘 → 磁盘记 1
  实际放行 2 条，Server酱 5 条/天全局限额可被绕过。
- `_sync_ledger_to_disk` 用本进程内存整块覆盖盘上 pending/notified → 跨进程可将
  已交付的耗尽告知标记回退（告知邮件重复或漏发）。
- `_load_ledger_file` 损坏/缺文件静默返回 {} → 额度重置为满额（超发）+ 另一本账
  被整体抹除，零日志。

修复后的契约（本文件逐一断言）：
1. 读盘 → 判定/修改 → 原子写回在**同一次文件锁持有**内完成（磁盘是唯一事实源）；
2. `_sync_ledger_to_disk` 合并式写回，不覆盖盘上已交付标记；
3. `_load_ledger_file` 损坏文件归档留证 + warning，不再静默满额；
4. 多线程并发（保留进程内锁）同进程语义不破坏，磁盘与内存计数始终一致；
5. Linux/CI 上剥离进程内锁时，文件锁临界区仍能串行化读-改-写、不超发。

用法（项目根目录）：
    py -m pytest tests/test_notify_ledger_race_091.py -v
"""
import json
import logging
import os
import sys
import threading
from contextlib import contextmanager
from unittest import mock

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))

import notify  # noqa: E402

KEY = "f" * 64


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """隔离环境：账本目录指向临时目录 + 重置 notify 模块级内存态 + 清空 YIBAN_NOTIFY_*。

    每用例独立 tmp_path（磁盘账本文件互不残留）；notify 是全局单例，内存账本
    必须在用例间复位，否则跨用例的当日计数会让用例从非零起步。
    """
    monkeypatch.setenv("YIBAN_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("YIBAN_ACCOUNTS_KEY", KEY)
    monkeypatch.setenv("YIBAN_ENV_FILE", str(tmp_path / "no-such.env"))
    notify._throttle_ts.clear()
    notify._general_daily["state"].update({"date": "", "count": 0})
    notify._urgent_daily["state"].update({"date": "", "count": 0})
    for ledger in (notify._general_daily, notify._urgent_daily):
        ledger["notice"].update({"pending": False, "notified": False, "warned": False})
    notify._skip_logged.clear()
    for k in list(os.environ):
        if k.startswith("YIBAN_NOTIFY_"):
            monkeypatch.delenv(k)
    yield


def _read_disk(tmp_path):
    with open(os.path.join(str(tmp_path), "notify-ledger.json"), encoding="utf-8") as f:
        return json.load(f)


def _write_disk(tmp_path, data):
    path = os.path.join(str(tmp_path), "notify-ledger.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return path


# ---------------------------------------------------------------------------
# 单元：正常放行 / 拒满 / 失败退还 / 跨日作废
# ---------------------------------------------------------------------------

def test_consume_allows_until_limit_then_rejects(tmp_path, monkeypatch):
    """正常放行到上限，超限被拒；磁盘 count 与内存一致。"""
    monkeypatch.setenv("YIBAN_NOTIFY_DAILY_MAX", "3")
    for i in range(3):
        t = notify._consume_daily_budget("general")
        assert t.allowed and t.ledger == "general"
    t = notify._consume_daily_budget("general")
    assert t.allowed is False and t.ledger is None
    assert notify._general_daily["state"]["count"] == 3
    disk = _read_disk(tmp_path)
    assert disk["general"]["count"] == 3
    assert notify.get_config()["daily_remaining"] == 0


def test_refund_after_failure_restores_count(tmp_path, monkeypatch):
    """发送失败退还：内存与磁盘计数同步回退，虚警耗尽标记被撤回。"""
    monkeypatch.setenv("YIBAN_NOTIFY_DAILY_MAX", "2")
    ticket = notify._consume_daily_budget("general")
    notify._consume_daily_budget("general")  # 占满 2/2 → 挂耗尽告知
    assert notify.budget_exhausted_today(False) is True
    notify._refund_daily_budget(ticket)
    assert notify._general_daily["state"]["count"] == 1
    disk = _read_disk(tmp_path)
    assert disk["general"]["count"] == 1, "退还必须落盘，另一进程才能读到回退"
    assert notify.budget_exhausted_today(False) is False
    assert notify.pop_exhaustion_notice() == [], "退还后虚警告知应被撤回"


def test_refund_across_day_is_voided(tmp_path, monkeypatch):
    """跨日凭证作废：23:59:59 占用、次日才失败退还，不得扣到次日账上。"""
    monkeypatch.setenv("YIBAN_NOTIFY_DAILY_MAX", "2")
    day = ["2026-09-01"]
    monkeypatch.setattr(notify, "_daily_today", lambda: day[0])
    stale = notify._consume_daily_budget("general")
    assert stale.allowed and stale.day == "2026-09-01"
    day[0] = "2026-09-02"
    assert notify._consume_daily_budget("general").allowed  # 次日已实占 1 条
    notify._refund_daily_budget(stale)                       # 昨日凭证此刻作废
    assert notify._general_daily["state"]["count"] == 1, "跨日退还不得少次日一条额度"
    assert _read_disk(tmp_path)["general"]["count"] == 1


# ---------------------------------------------------------------------------
# 单元：合并式写回，不覆盖盘上已交付标记
# ---------------------------------------------------------------------------

def test_sync_ledger_to_disk_preserves_disk_notified_true(tmp_path, monkeypatch):
    """`_sync_ledger_to_disk` 合并式写回：本进程内存 notified=False（陈旧）不得覆盖
    盘上另一进程已写下的 notified=True（否则已交付的耗尽告知会回退成待取，重复发信）。"""
    monkeypatch.setenv("YIBAN_NOTIFY_DAILY_MAX", "5")
    today = notify._daily_today()
    # 构造盘上状态：另一进程已把"耗尽告知"交付（notified=True）并落盘
    _write_disk(tmp_path, {
        "general": {"date": today, "count": 3, "pending": False,
                    "notified": True, "warned": True},
        "urgent": {"date": today, "count": 0, "pending": False,
                   "notified": False, "warned": False},
    })
    # 本进程内存态陈旧：还没对齐到盘上最新值
    notify._general_daily["state"].update({"date": today, "count": 3})
    notify._general_daily["notice"].update({"pending": False, "notified": False, "warned": True})
    notify._sync_ledger_to_disk("general")
    disk = _read_disk(tmp_path)
    assert disk["general"]["notified"] is True, "合并式写回不得把盘上已交付标记回退成 False"
    assert disk["general"]["pending"] is False
    assert disk["general"]["count"] == 3


def test_sync_ledger_to_disk_preserves_other_ledger(tmp_path, monkeypatch):
    """合并式写回只动本账本：盘上另一本账（urgent）的数据不得被整块覆盖抹掉。"""
    monkeypatch.setenv("YIBAN_NOTIFY_DAILY_MAX", "5")
    today = notify._daily_today()
    _write_disk(tmp_path, {
        "general": {"date": today, "count": 1, "pending": False,
                    "notified": False, "warned": False},
        "urgent": {"date": today, "count": 2, "pending": True,
                   "notified": False, "warned": True},   # 另一本账有真实待取告知
    })
    notify._general_daily["state"].update({"date": today, "count": 1})
    notify._general_daily["notice"].update({"pending": False, "notified": False, "warned": False})
    notify._sync_ledger_to_disk("general")
    disk = _read_disk(tmp_path)
    assert disk["urgent"] == {"date": today, "count": 2, "pending": True,
                              "notified": False, "warned": True}, "不得抹掉另一本账数据"


# ---------------------------------------------------------------------------
# 结构契约：读-改-写合并为单次文件锁临界区
# ---------------------------------------------------------------------------

def test_consume_holds_single_file_lock_critical_section(tmp_path, monkeypatch):
    """每次消费只进入一次文件锁临界区（旧实现 _roll 与 _sync 各持一次，中间留窗）。

    这是跨进程不超发的结构性保证：磁盘是唯一事实源，读盘-判定-写回不可被
    另一进程在两次文件锁之间插入完整操作。
    """
    monkeypatch.setenv("YIBAN_NOTIFY_DAILY_MAX", "5")
    enters = []
    real = notify._ledger_file_lock

    @contextmanager
    def _counting():
        enters.append(1)
        with real():
            yield

    monkeypatch.setattr(notify, "_ledger_file_lock", _counting)
    notify._consume_daily_budget("general")
    assert len(enters) == 1, "读-改-写必须合并为单次文件锁临界区"
    assert _read_disk(tmp_path)["general"]["count"] == 1


def test_readonly_remaining_does_not_rewrite_disk(tmp_path, monkeypatch):
    """纯读路径（_daily_remaining）不触发写盘：无修改就不落盘。"""
    monkeypatch.setenv("YIBAN_NOTIFY_DAILY_MAX", "5")
    notify._consume_daily_budget("general")
    before = os.stat(os.path.join(str(tmp_path), "notify-ledger.json")).st_mtime_ns
    remaining = notify._daily_remaining("general")
    assert remaining == 4
    after = os.stat(os.path.join(str(tmp_path), "notify-ledger.json")).st_mtime_ns
    assert before == after, "纯读查询不应重写账本文件"


# ---------------------------------------------------------------------------
# 并发：barrier 多线程验证同进程语义不破坏 + 磁盘/内存一致无丢失更新
# ---------------------------------------------------------------------------

def test_multi_thread_consume_no_lost_update(tmp_path, monkeypatch):
    """8 线程 barrier 同时消费：全部放行，磁盘 count 与内存 count 一致、无丢失更新。"""
    monkeypatch.setenv("YIBAN_NOTIFY_DAILY_MAX", "50")
    n = 8
    barrier = threading.Barrier(n)
    allowed = []
    guard = threading.Lock()

    def _worker():
        barrier.wait()
        t = notify._consume_daily_budget("general")
        with guard:
            allowed.append(t.allowed)

    threads = [threading.Thread(target=_worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(allowed) == n, "上限内所有线程都应放行"
    assert notify._general_daily["state"]["count"] == n
    assert _read_disk(tmp_path)["general"]["count"] == n, \
        "每次消费后磁盘 count 必须与内存 count 一致（无丢失更新）"


@pytest.mark.skipif(notify.fcntl is None,
                    reason="Windows 无 fcntl，文件锁退化为进程内，无法验证文件锁串行化")
def test_no_process_lock_concurrent_not_overspent(tmp_path, monkeypatch):
    """剥离进程内锁（模拟不共享锁的 web/signin 两进程）+ 文件锁生效时：
    读-改-写仍在单次文件锁临界区内串行化，不超发、无丢失更新。"""
    monkeypatch.setenv("YIBAN_NOTIFY_DAILY_MAX", "50")
    real_lock = notify._general_daily["lock"]
    notify._general_daily["lock"] = _NoLock()  # 进程内锁退化为无操作
    try:
        n = 8
        barrier = threading.Barrier(n)
        allowed = []
        guard = threading.Lock()

        def _worker():
            barrier.wait()
            t = notify._consume_daily_budget("general")
            with guard:
                allowed.append(t.allowed)

        threads = [threading.Thread(target=_worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sum(allowed) == n
        assert notify._general_daily["state"]["count"] == n
        assert _read_disk(tmp_path)["general"]["count"] == n, \
            "文件锁临界区必须串行化读-改-写，防止双进程各读旧值各 +1 造成丢失更新"
    finally:
        notify._general_daily["lock"] = real_lock


class _NoLock:
    """立即通过的替身锁：模拟"跨进程无共享进程内锁"（只依赖文件锁互斥）。"""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# 损坏文件：warning + 归档留证 + 从空账开始（不静默满额）
# ---------------------------------------------------------------------------

def test_corrupt_ledger_archived_warned_and_restarts(tmp_path, monkeypatch, caplog):
    """写坏 JSON：消费时记 warning、损坏文件被归档、额度从 0 开始（不静默满额超发）。"""
    monkeypatch.setenv("YIBAN_NOTIFY_DAILY_MAX", "5")
    path = _write_disk(tmp_path, {"general": "not-a-dict", "urgent": []})  # 结构损坏
    with caplog.at_level(logging.WARNING, logger="notify"):
        ticket = notify._consume_daily_budget("general")
    assert ticket.allowed, "损坏按空账处理：应从 0 开始放行"
    assert "损坏" in caplog.text, "损坏必须记 warning，不得静默重置"
    corrupts = [n for n in os.listdir(str(tmp_path))
                if n.startswith("notify-ledger.json.corrupt-")]
    assert len(corrupts) == 1, "损坏文件应归档留证"
    assert os.path.exists(path), "原路径应由写回重建为正常账本"
    assert _read_disk(tmp_path)["general"]["count"] == 1, "从空账开始消费 1 条"
    assert notify.get_config()["daily_remaining"] == 4, "额度从 0 开始而非满额重置"


def test_corrupt_ledger_archives_broken_json(tmp_path, monkeypatch, caplog):
    """纯坏 JSON（非 dict 结构）同样归档 + warning。"""
    monkeypatch.setenv("YIBAN_NOTIFY_DAILY_MAX", "5")
    path = os.path.join(str(tmp_path), "notify-ledger.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write("{ not valid json !!!")
    with caplog.at_level(logging.WARNING, logger="notify"):
        t = notify._consume_daily_budget("general")
    assert t.allowed
    assert "损坏" in caplog.text
    assert any(n.startswith("notify-ledger.json.corrupt-")
               for n in os.listdir(str(tmp_path)))
    assert _read_disk(tmp_path)["general"]["count"] == 1


def test_missing_ledger_file_is_normal_first_run(tmp_path, monkeypatch, caplog):
    """缺文件是首次运行，不记损坏 warning（与损坏文件区分开）。"""
    monkeypatch.setenv("YIBAN_NOTIFY_DAILY_MAX", "5")
    with caplog.at_level(logging.WARNING, logger="notify"):
        t = notify._consume_daily_budget("general")
    assert t.allowed
    assert "损坏" not in caplog.text, "首次运行缺文件不应按损坏告警"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
