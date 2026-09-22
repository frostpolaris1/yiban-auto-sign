# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""通知额度账本：磁盘持久化、跨天作废与并发不超支。

账本决定「每天最多发几条告警/通知」，额度记在磁盘上、跨进程共享。本文件并两处断言：
磁盘状态机的读写（含损坏文件归档后重启、首跑缺文件属正常）、以及多线程/多进程并发
消费时不丢更新、不超支，且只读路径不重写磁盘。

功能：通知账本的持久化与并发正确性回归。
归属：`yiban/notify/ledger.py` 的测试。
复用：`BASE` / `KEY` 常量与临时目录隔离助手。
通信：直接调用账本 API，读写临时账本 JSON 并另起进程/线程并发消费。
"""
import contextlib
import importlib.util
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from unittest import mock

import db
import pytest
import signin

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(BASE, "scripts"))

from yiban import notify  # noqa: E402
from yiban.infra import locks  # noqa: E402
from yiban.notify import ledger as notify_ledger  # noqa: E402

KEY = "f" * 64


SCT_KEY = "SCT406257TESTTESTTESTTESTTEST"


class NotifyLedgerDiskTest(unittest.TestCase):
    """P2-3：额度磁盘账本（单进程内验证文件语义）。"""

    def setUp(self):
        # 内部账本名走子模块（旧的 scripts/notify.py 兼容壳已删除）
        from yiban.notify import ledger as notify
        self.notify = notify
        self.tmp = tempfile.mkdtemp(prefix="yiban-ledger-")
        self.env_file = os.path.join(self.tmp, "nope.env")
        os.environ["YIBAN_STATE_DIR"] = self.tmp
        os.environ["YIBAN_ENV_FILE"] = self.env_file
        os.environ["YIBAN_ACCOUNTS_KEY"] = KEY
        notify._throttle_ts.clear()
        notify._general_daily["state"].update({"date": "", "count": 0})
        notify._urgent_daily["state"].update({"date": "", "count": 0})
        for ledger in (notify._general_daily, notify._urgent_daily):
            ledger["notice"].update({"pending": False, "notified": False, "warned": False})
        notify._skip_logged.clear()

    def tearDown(self):
        for k in ("YIBAN_STATE_DIR", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_KEY"):
            os.environ.pop(k, None)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ledger_file(self):
        return os.path.join(self.tmp, "notify-ledger.json")

    def _read_disk(self):
        with open(self._ledger_file(), encoding="utf-8") as f:
            return json.load(f)

    def test_consume_persists_to_disk(self):
        """占用后磁盘账本存在且计数=1。"""
        os.environ["YIBAN_NOTIFY_DAILY_MAX"] = "5"
        ticket = self.notify._consume_daily_budget("general")
        self.assertTrue(ticket.allowed)
        self.assertTrue(os.path.exists(self._ledger_file()))
        disk = self._read_disk()
        self.assertEqual(disk["general"]["count"], 1)
        self.assertEqual(disk["general"]["date"], self.notify._daily_today())

    def test_cross_process_reload_sees_consumed(self):
        """模拟另一进程：重新 import notify（清内存态）后仍读到已占用的计数。"""
        os.environ["YIBAN_NOTIFY_DAILY_MAX"] = "5"
        self.notify._consume_daily_budget("general")
        self.notify._consume_daily_budget("general")
        # 模拟新进程：清空模块级内存账本（新 import 会重建为初始值）
        self.notify._general_daily["state"].update({"date": "", "count": 0})
        # 新进程第一次 _roll_locked 应从磁盘恢复
        remaining = self.notify._daily_remaining("general")
        self.assertEqual(remaining, 3, "磁盘计数 2，上限 5，剩余应为 3（跨进程不重置）")

    def test_daily_rollover_resets_disk(self):
        """跨日：新日期首次调用归零磁盘账本。"""
        os.environ["YIBAN_NOTIFY_DAILY_MAX"] = "5"
        day1 = ("2026-08-31",)
        with mock.patch.object(self.notify, "_daily_today", lambda: day1[0]):
            self.notify._consume_daily_budget("general")
            self.notify._consume_daily_budget("general")
        day2 = ("2026-09-01",)
        with mock.patch.object(self.notify, "_daily_today", lambda: day2[0]):
            remaining = self.notify._daily_remaining("general")
        self.assertEqual(remaining, 5, "跨日后应归零重新计数")

    def test_daily_today_uses_business_clock_not_host_tz(self):
        """Low-3：账本"今日"必须按业务钟（北京），UTC 主机上不得用宿主日期。

        UTC 主机上 `time.strftime` 的本地日期比北京晚（北京时间 09-17 00:00 时
        宿主仍是 09-16 16:00）——额度重置点错位，一个北京日历日内可动用近两份
        额度。修复后 `_daily_today` 委托 `yiban.clock.today`：让两个来源返回
        不同日期，断言账本落盘用的是 clock 那个。
        """
        os.environ["YIBAN_NOTIFY_DAILY_MAX"] = "5"
        with mock.patch("yiban.notify.ledger.clock.today", return_value="2026-09-17"), \
                mock.patch.object(self.notify.time, "strftime",
                                  return_value="2026-09-16"):
            ticket = self.notify._consume_daily_budget("general")
            self.assertTrue(ticket.allowed)
            disk = self._read_disk()
        self.assertEqual(disk["general"]["date"], "2026-09-17",
                         "账本日期必须是业务钟（北京日），不是宿主日期")

    def test_refund_persists_to_disk(self):
        """退还后磁盘计数回退。"""
        os.environ["YIBAN_NOTIFY_DAILY_MAX"] = "5"
        ticket = self.notify._consume_daily_budget("general")
        self.notify._refund_daily_budget(ticket)
        disk = self._read_disk()
        self.assertEqual(disk["general"]["count"], 0, "退还后磁盘计数回退为 0")

    def test_two_module_instances_share_quota(self):
        """双模块实例（模拟 web + signin 两进程）共享同一账本：合计不超过上限。"""
        os.environ["YIBAN_NOTIFY_DAILY_MAX"] = "3"
        # 实例 A（进程 1）
        sent_a = []
        for i in range(2):
            t = self.notify._consume_daily_budget("general")
            self.assertTrue(t.allowed, f"A 第 {i+1} 条应允许")
            sent_a.append(t)
        # 实例 B（进程 2）：把实现包从 sys.modules 里整体摘掉再导入，等价于
        # "新进程从零起"（内存账本为空，只能从磁盘恢复已占用计数）
        #
        # ⚠ 必须完整还原（sys.modules + 父包属性）：摘掉再导入会让本进程里"先前
        # 导入方持有的旧模块对象"与"函数内重新解析到的新对象"同时存在，形成两份
        # 账本实例，后续用例的计数与打桩都会落在错的那一份上。
        saved_modules = {k: v for k, v in sys.modules.items()
                         if k.startswith("yiban.notify")}

        def _restore_notify_modules():
            for k in [k for k in sys.modules if k.startswith("yiban.notify")]:
                del sys.modules[k]
            sys.modules.update(saved_modules)
            # 只还原 sys.modules 不够：`from yiban import notify` 取的是**父包上的属性**，
            # 子模块重新导入时该属性已被换成新对象。属性不一起还原，进程里就留下两份
            # `yiban.notify`——先导入的模块（如 web/services/notify_mail）握着旧对象，
            # 后导入的（测试里 importlib 重载的 webapp）握着新对象，于是
            # `mock.patch.object(webapp.notify, "send")` 打不到真正被调用的那一份，
            # 告警用例表现为"0 次调用"（本文件先跑才复现）。
            pkg = sys.modules.get("yiban")
            if pkg is not None and "yiban.notify" in saved_modules:
                pkg.notify = saved_modules["yiban.notify"]
                for k, v in saved_modules.items():
                    if k.startswith("yiban.notify."):
                        setattr(saved_modules["yiban.notify"], k.rsplit(".", 1)[1], v)

        self.addCleanup(_restore_notify_modules)
        for k in list(saved_modules):
            del sys.modules[k]
        from yiban.notify import ledger as notify_b
        os.environ["YIBAN_STATE_DIR"] = self.tmp
        os.environ["YIBAN_ENV_FILE"] = self.env_file
        os.environ["YIBAN_ACCOUNTS_KEY"] = KEY
        # B 第一次占用应从磁盘读到已用 2/3
        t = notify_b._consume_daily_budget("general")
        self.assertTrue(t.allowed, "B 第 1 条应允许（合计 3/3）")
        t2 = notify_b._consume_daily_budget("general")
        self.assertFalse(t2.allowed, "合计已达 3/3，B 第 2 条应被拒（共享额度不超发）")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """隔离环境：账本目录指向临时目录 + 重置 notify 模块级内存态 + 清空 YIBAN_NOTIFY_*。

    每用例独立 tmp_path（磁盘账本文件互不残留）；notify 是全局单例，内存账本
    必须在用例间复位，否则跨用例的当日计数会让用例从非零起步。
    """
    monkeypatch.setenv("YIBAN_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("YIBAN_ACCOUNTS_KEY", KEY)
    monkeypatch.setenv("YIBAN_ENV_FILE", str(tmp_path / "no-such.env"))
    notify_ledger._throttle_ts.clear()
    notify_ledger._general_daily["state"].update({"date": "", "count": 0})
    notify_ledger._urgent_daily["state"].update({"date": "", "count": 0})
    for ledger in (notify_ledger._general_daily, notify_ledger._urgent_daily):
        ledger["notice"].update({"pending": False, "notified": False, "warned": False})
    notify_ledger._skip_logged.clear()
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


def test_consume_allows_until_limit_then_rejects(tmp_path, monkeypatch):
    """正常放行到上限，超限被拒；磁盘 count 与内存一致。"""
    monkeypatch.setenv("YIBAN_NOTIFY_DAILY_MAX", "3")
    for _ in range(3):
        t = notify_ledger._consume_daily_budget("general")
        assert t.allowed and t.ledger == "general"
    t = notify_ledger._consume_daily_budget("general")
    assert t.allowed is False and t.ledger is None
    assert notify_ledger._general_daily["state"]["count"] == 3
    disk = _read_disk(tmp_path)
    assert disk["general"]["count"] == 3
    assert notify.get_config()["daily_remaining"] == 0


def test_refund_after_failure_restores_count(tmp_path, monkeypatch):
    """发送失败退还：内存与磁盘计数同步回退，虚警耗尽标记被撤回。"""
    monkeypatch.setenv("YIBAN_NOTIFY_DAILY_MAX", "2")
    ticket = notify_ledger._consume_daily_budget("general")
    notify_ledger._consume_daily_budget("general")  # 占满 2/2 → 挂耗尽告知
    assert notify.budget_exhausted_today(False) is True
    notify_ledger._refund_daily_budget(ticket)
    assert notify_ledger._general_daily["state"]["count"] == 1
    disk = _read_disk(tmp_path)
    assert disk["general"]["count"] == 1, "退还必须落盘，另一进程才能读到回退"
    assert notify.budget_exhausted_today(False) is False
    assert notify.pop_exhaustion_notice() == [], "退还后虚警告知应被撤回"


def test_refund_across_day_is_voided(tmp_path, monkeypatch):
    """跨日凭证作废：23:59:59 占用、次日才失败退还，不得扣到次日账上。"""
    monkeypatch.setenv("YIBAN_NOTIFY_DAILY_MAX", "2")
    day = ["2026-09-01"]
    monkeypatch.setattr(notify_ledger, "_daily_today", lambda: day[0])
    stale = notify_ledger._consume_daily_budget("general")
    assert stale.allowed and stale.day == "2026-09-01"
    day[0] = "2026-09-02"
    assert notify_ledger._consume_daily_budget("general").allowed  # 次日已实占 1 条
    notify_ledger._refund_daily_budget(stale)                       # 昨日凭证此刻作废
    assert notify_ledger._general_daily["state"]["count"] == 1, "跨日退还不得少次日一条额度"
    assert _read_disk(tmp_path)["general"]["count"] == 1


def test_live_path_preserves_disk_notified_true(tmp_path, monkeypatch):
    """已交付的耗尽告知标记（notified=True）经活路径写盘后不得回退成 False。

    旧实现靠 `_sync_ledger_to_disk` 的"或"式合并兜底——因为它在临界区**外**写盘，
    内存值可能陈旧。现在读-改-写在同一次文件锁内完成，锁内读到的盘值即最新值，
    因此不再需要"或"语义；本用例改为在活路径（`_consume_daily_budget`）上直接断言
    该不变量，覆盖不因实现换形而丢失。
    """
    monkeypatch.setenv("YIBAN_NOTIFY_DAILY_MAX", "5")
    today = notify_ledger._daily_today()
    _write_disk(tmp_path, {
        "general": {"date": today, "count": 3, "pending": False,
                    "notified": True, "warned": True},
        "urgent": {"date": today, "count": 0, "pending": False,
                   "notified": False, "warned": False},
    })
    assert notify_ledger._consume_daily_budget("general").allowed is True
    disk = _read_disk(tmp_path)
    assert disk["general"]["notified"] is True, "已交付告知标记不得被回退"
    assert disk["general"]["warned"] is True
    assert disk["general"]["count"] == 4, "占用应落盘"


def test_live_path_does_not_touch_other_ledger(tmp_path, monkeypatch):
    """只动本账本：盘上另一本账（urgent）的数据不得被整块覆盖抹掉。"""
    monkeypatch.setenv("YIBAN_NOTIFY_DAILY_MAX", "5")
    today = notify_ledger._daily_today()
    _write_disk(tmp_path, {
        "general": {"date": today, "count": 1, "pending": False,
                    "notified": False, "warned": False},
        "urgent": {"date": today, "count": 2, "pending": True,
                   "notified": False, "warned": True},   # 另一本账有真实待取告知
    })
    assert notify_ledger._consume_daily_budget("general").allowed is True
    disk = _read_disk(tmp_path)
    assert disk["urgent"] == {"date": today, "count": 2, "pending": True,
                              "notified": False, "warned": True}, "不得抹掉另一本账数据"


def test_consume_holds_single_file_lock_critical_section(tmp_path, monkeypatch):
    """每次消费只进入一次文件锁临界区（旧实现 _roll 与 _sync 各持一次，中间留窗）。

    这是跨进程不超发的结构性保证：磁盘是唯一事实源，读盘-判定-写回不可被
    另一进程在两次文件锁之间插入完整操作。
    """
    monkeypatch.setenv("YIBAN_NOTIFY_DAILY_MAX", "5")
    enters = []
    real = notify_ledger._ledger_file_lock

    @contextmanager
    def _counting():
        enters.append(1)
        with real():
            yield

    # 打桩打在账本实现模块上：`_consume_daily_budget` 内部读的就是该模块的全局名
    monkeypatch.setattr(notify_ledger, "_ledger_file_lock", _counting)
    notify_ledger._consume_daily_budget("general")
    assert len(enters) == 1, "读-改-写必须合并为单次文件锁临界区"
    assert _read_disk(tmp_path)["general"]["count"] == 1


def test_readonly_remaining_does_not_rewrite_disk(tmp_path, monkeypatch):
    """纯读路径（_daily_remaining）不触发写盘：无修改就不落盘。"""
    monkeypatch.setenv("YIBAN_NOTIFY_DAILY_MAX", "5")
    notify_ledger._consume_daily_budget("general")
    before = os.stat(os.path.join(str(tmp_path), "notify-ledger.json")).st_mtime_ns
    remaining = notify_ledger._daily_remaining("general")
    assert remaining == 4
    after = os.stat(os.path.join(str(tmp_path), "notify-ledger.json")).st_mtime_ns
    assert before == after, "纯读查询不应重写账本文件"


def test_multi_thread_consume_no_lost_update(tmp_path, monkeypatch):
    """8 线程 barrier 同时消费：全部放行，磁盘 count 与内存 count 一致、无丢失更新。"""
    monkeypatch.setenv("YIBAN_NOTIFY_DAILY_MAX", "50")
    n = 8
    barrier = threading.Barrier(n)
    allowed = []
    guard = threading.Lock()

    def _worker():
        barrier.wait()
        t = notify_ledger._consume_daily_budget("general")
        with guard:
            allowed.append(t.allowed)

    threads = [threading.Thread(target=_worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(allowed) == n, "上限内所有线程都应放行"
    assert notify_ledger._general_daily["state"]["count"] == n
    assert _read_disk(tmp_path)["general"]["count"] == n, \
        "每次消费后磁盘 count 必须与内存 count 一致（无丢失更新）"


@pytest.mark.skipif(locks.lock_kind() is None,
                    reason="本平台无跨进程文件锁（fcntl/msvcrt 均不可用），无法验证文件锁串行化")
def test_no_process_lock_concurrent_not_overspent(tmp_path, monkeypatch):
    """剥离进程内锁（模拟不共享锁的 web/signin 两进程）+ 文件锁生效时：
    读-改-写仍在单次文件锁临界区内串行化，不超发、无丢失更新。"""
    monkeypatch.setenv("YIBAN_NOTIFY_DAILY_MAX", "50")
    real_lock = notify_ledger._general_daily["lock"]
    notify_ledger._general_daily["lock"] = _NoLock()  # 进程内锁退化为无操作
    try:
        n = 8
        barrier = threading.Barrier(n)
        allowed = []
        guard = threading.Lock()

        def _worker():
            barrier.wait()
            t = notify_ledger._consume_daily_budget("general")
            with guard:
                allowed.append(t.allowed)

        threads = [threading.Thread(target=_worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sum(allowed) == n
        assert notify_ledger._general_daily["state"]["count"] == n
        assert _read_disk(tmp_path)["general"]["count"] == n, \
            "文件锁临界区必须串行化读-改-写，防止双进程各读旧值各 +1 造成丢失更新"
    finally:
        notify_ledger._general_daily["lock"] = real_lock


class _NoLock:
    """立即通过的替身锁：模拟"跨进程无共享进程内锁"（只依赖文件锁互斥）。"""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_corrupt_ledger_archived_warned_and_restarts(tmp_path, monkeypatch, caplog):
    """写坏 JSON：消费时记 warning、损坏文件被归档、额度从 0 开始（不静默满额超发）。"""
    monkeypatch.setenv("YIBAN_NOTIFY_DAILY_MAX", "5")
    path = _write_disk(tmp_path, {"general": "not-a-dict", "urgent": []})  # 结构损坏
    with caplog.at_level(logging.WARNING, logger="notify"):
        ticket = notify_ledger._consume_daily_budget("general")
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
        t = notify_ledger._consume_daily_budget("general")
    assert t.allowed
    assert "损坏" in caplog.text
    assert any(n.startswith("notify-ledger.json.corrupt-")
               for n in os.listdir(str(tmp_path)))
    assert _read_disk(tmp_path)["general"]["count"] == 1


def test_missing_ledger_file_is_normal_first_run(tmp_path, monkeypatch, caplog):
    """缺文件是首次运行，不记损坏 warning（与损坏文件区分开）。"""
    monkeypatch.setenv("YIBAN_NOTIFY_DAILY_MAX", "5")
    with caplog.at_level(logging.WARNING, logger="notify"):
        t = notify_ledger._consume_daily_budget("general")
    assert t.allowed
    assert "损坏" not in caplog.text, "首次运行缺文件不应按损坏告警"




TEST_KEY = "a" * 64


ADMIN_PASS = "TestPass1234!"


USER_PASS = "secret1"


EMAIL = "u1@test.local"


PHONE = "13800138000"


class _WebBase(unittest.TestCase):
    """临时 .env/DB + 全新 app（与既有 web 类测试同一骨架）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-d6-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        os.environ.update({
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_ENV_FILE": cls.env_file,
            "YIBAN_DB_FILE": cls.db_file,
            "YIBAN_STATE_DIR": cls.tmp,
            "YIBAN_LOG_FILE": os.path.join(cls.tmp, "sign.log"),
        })
        spec = importlib.util.spec_from_file_location(
            "webapp", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_DB_FILE",
                  "YIBAN_STATE_DIR", "YIBAN_LOG_FILE"):
            os.environ.pop(k, None)

    def setUp(self):
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                    f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n")
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        db.init_db(self.db_file, env_file=self.env_file, cleanup=False)
        db.create_user(EMAIL, self.webapp.generate_password_hash(USER_PASS))
        db.add_account({"name": "我的号", "phone": PHONE, "password": "pw",
                        "owner": EMAIL, "status": "active",
                        "phone_model": "", "phone_code": ""})
        self.app = self.webapp.create_app()
        self.c = self.app.test_client()

    def _login(self):
        r = self.c.post("/api/login", json={"username": EMAIL, "password": USER_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return self.c.get("/api/me").get_json()["csrf_token"]


class UserFailMailQuotaTest(unittest.TestCase):
    """SCH-8：额度语义 = "每天最多成功提醒 N 次"，发送失败必须归还。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-mailquota-")
        self.env = dict(os.environ)
        os.environ["YIBAN_STATE_DIR"] = self.tmp
        os.environ["YIBAN_MAIL_USER_FAIL_DAILY_CAP"] = "1"
        # cap 在模块导入期由 parse_env_int 固定，测试内直接改常量
        self._old_cap = signin.USER_FAIL_MAIL_DAILY_CAP
        signin.USER_FAIL_MAIL_DAILY_CAP = 1

    def tearDown(self):
        signin.USER_FAIL_MAIL_DAILY_CAP = self._old_cap
        os.environ.clear()
        os.environ.update(self.env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _today(self):
        return signin.clock.now().strftime("%Y-%m-%d")

    def test_reserve_then_release_restores_quota(self):
        today = self._today()
        self.assertTrue(signin._user_fail_mail_reserve(PHONE, today))
        self.assertFalse(signin._user_fail_mail_reserve(PHONE, today), "额度已用尽")
        signin._user_fail_mail_release(PHONE, today)
        self.assertTrue(signin._user_fail_mail_reserve(PHONE, today), "归还后应可再用")

    def test_release_floor_is_zero(self):
        today = self._today()
        signin._user_fail_mail_release(PHONE, today)  # 无记录也不得为负
        self.assertTrue(signin._user_fail_mail_reserve(PHONE, today))

    def test_send_failure_releases_quota(self):
        """SMTP 全失败 → 额度归还，当天重试仍有机会（缺陷主场景）。"""
        with mock.patch.object(signin.db, "find_user",
                               return_value={"email": EMAIL, "mail_notify": 1}), \
                mock.patch.object(signin.mailer, "send_user", return_value=False) as send:
            signin.send_user_fail_mail(EMAIL, PHONE, "网络超时")
            signin.send_user_fail_mail(EMAIL, PHONE, "网络超时")  # 若没归还，这次会被额度挡住
        # 两次都尝试过发送 = 额度未被失败吞掉
        self.assertEqual(send.call_count, 2)

    def test_send_success_consumes_quota(self):
        with mock.patch.object(signin.db, "find_user",
                               return_value={"email": EMAIL, "mail_notify": 1}), \
                mock.patch.object(signin.mailer, "send_user", return_value=True) as send:
            signin.send_user_fail_mail(EMAIL, PHONE, "网络超时")
            signin.send_user_fail_mail(EMAIL, PHONE, "网络超时")
        self.assertEqual(send.call_count, 1, "成功后当天不再重复发")
