# -*- coding: utf-8 -*-
"""状态文件写盘的原子性与"标记损坏"的失效方向。

标签：D · 状态词汇与账号生命周期
覆盖：容器调度时段标记 `_mark_slot` 经 `os.replace` 原子落盘、替换失败时标记路径
    不得存在半截文件、被杀残留的半成品由 `state_gc.sweep` 按 mtime 清走、坏文件退化为
    "未跑过"而不抛异常；signin 侧四个状态写入函数的原子性与"实现只有一份"；
    **私有写单通道**——全部 14 个状态写入站点在 `umask(0o000)` 下拉宽测试环境后终文件
    仍 0600 且无 `.tmp` 残留，生产写盘模块不得再用内置 `open()` 建 tmp。
对应实现：容器侧在 `docker/scheduler.py`（`_mark_slot`/`_slot_done`/`_slot_marker`/
    `_touch_heartbeat`），引擎侧写入按 `yiban/engine/state_io.py`、`yiban/engine/probe.py`、
    `yiban/engine/alerts.py`、`yiban/engine/runner.py`、`yiban/cred_state.py`、
    `yiban/notify/ledger.py` 落点，单通道实现唯一在 `yiban/infra/private_json.py`
    （`state_io._write_private_json` 为其既有锚点），兼容壳 `scripts/signin.py` 只剩转发。
关键断言：**失效方向必须是"标记缺失"而不是"文件损坏"**——半截 JSON 会让 `_slot_done`
    的 `json.load` 恒失败而判定成"本时段没跑过"，配合无-upper-bound 的 `hm >= FIRST`
    判定，会再触发一轮全站真实登录并覆盖当日已 success 的状态。
    **模式判据必须先把外层 umask 拉到 0o000 再断 0600**——测试进程默认继承 077 时，
    内置 `open()` 也能写出 0600，断言恒绿（这正是当年 14 处漏网的原因）。
依赖：进程内打桩 `os.replace`（包住真函数再计数）+ 临时 STATEDIR；`umask`/`environ`
    成对打桩（POSIX 判定，Windows 上模式位非 POSIX 语义，跳过）；importlib 加载
    `docker/scheduler.py`；不跑子进程、不需 bash/docker CLI、不触网。

原先只有容器调度这一处漏了 tmp + `os.replace`，signin 侧早已是全项目约定。
"""
import json
import os
import re
import shutil
import stat
import tempfile
import unittest
import unittest.mock as mock
from types import SimpleNamespace

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import scheduler  # noqa: E402  （docker/scheduler.py）

from yiban import cred_state, state_gc  # noqa: E402
from yiban.engine import alerts, probe, runner, state_io  # noqa: E402
from yiban.notify import ledger  # noqa: E402


class SlotMarkerAtomicWriteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-slot-")
        p = mock.patch.object(scheduler, "STATEDIR", self.tmp)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _marker(self, kind="first"):
        return scheduler._slot_marker(kind)

    def test_mark_slot_replaces_atomically(self):
        real_replace = os.replace  #包住真 replace 再计数：既要证明走过 os.replace，又不能真把它替掉
        calls = []

        def _spy(src, dst):
            calls.append((src, dst))
            real_replace(src, dst)

        with mock.patch.object(scheduler.os, "replace", _spy):
            scheduler._mark_slot("first")
        self.assertEqual(len(calls), 1, "标记必须经 os.replace 原子落盘")
        src, dst = calls[0]
        self.assertEqual(dst, self._marker("first"))
        self.assertTrue(src.startswith(dst), f"临时名应基于标记名: {src}")
        with open(dst, encoding="utf-8") as f:
            self.assertIn("triggered_at", json.load(f))
        self.assertTrue(scheduler._slot_done("first"))

    def test_failed_replace_leaves_no_partial_marker(self):
        """替换失败时标记路径必须**不存在**（而不是存在但内容损坏）。"""
        with mock.patch.object(scheduler.os, "replace",
                               side_effect=OSError("killed mid-write")):
            scheduler._mark_slot("second")
        self.assertFalse(os.path.exists(self._marker("second")),
                         "半截文件会让 _slot_done 每次解析失败，失效方向错")
        self.assertFalse(scheduler._slot_done("second"), "读不到即视为未跑过（既有闩锁语义）")
        leftovers = [n for n in os.listdir(self.tmp) if ".tmp" in n]
        self.assertEqual(leftovers, [], "失败路径必须清掉自己的半成品")

    def test_interrupted_write_is_swept_later(self):
        """被杀（SIGKILL）留下的半成品没有机会自清 → 由状态清理按 mtime 兜底。"""
        tmp_file = self._marker("first") + ".tmp424242"
        with open(tmp_file, "w", encoding="utf-8") as f:
            f.write('{"triggered')          # 半截 JSON
        os.utime(tmp_file, (1, 1))         # 1 = 1970，远早于 1 天阈值
        removed, detail = state_gc.sweep(self.tmp)  #半成品清不掉就等清理兜底：这条把 _mark_slot 与 state_gc.sweep 绑成一对
        self.assertEqual(removed, 1, detail)
        self.assertFalse(os.path.exists(tmp_file))

    def test_corrupt_marker_is_treated_as_not_done(self):
        """坏文件（历史遗留/手工改动）必须退化为"未跑过"，不得抛异常打断调度循环。"""
        with open(self._marker("first"), "w", encoding="utf-8") as f:
            f.write('{"triggered_at": ')
        self.assertFalse(scheduler._slot_done("first"))
        self.assertFalse(scheduler._slot_done("never-written"))


class SigninWritesAreAtomicTest(unittest.TestCase):
    """signin 侧的状态文件写入同口径（本项目防止该类回归的既有约定）。

    引擎按"执行一轮"的边界切分后，这些写入各自落在实现模块里：按日状态与全量收尾标记
    在 `yiban/engine/state_io.py`，探针状态在 `probe.py`，用户失败邮件额度账本在
    `alerts.py`，兼容壳 `scripts/signin.py` 只剩转发。故断言直接读实现模块，并额外锁住
    "实现只有一份"——壳里再出现同名定义就是两份实现，改一份另一份照旧跑。
    """

    def _read(self, rel):
        with open(os.path.join(BASE, *rel.split("/")), encoding="utf-8") as f:
            return f.read()

    def test_cred_state_write_is_delegated(self):
        """熔断状态文件的原子写归 `yiban/cred_state.py`（唯一读写入库）；
        引擎不得自己再写一份（原子性与并发由 tests/test_breaker.py 钉住）。"""
        src = self._read("yiban/engine/state_io.py")
        # 只取本函数体（到下一个顶层 def 为止）——按字符数截取会把相邻函数一起断言
        body = src.split("def _save_cred_state(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("cred_state.update(", body)
        self.assertIn("cred_state.merge(", body)
        self.assertNotIn("open(", body)


class SignStateBomReadTest(unittest.TestCase):
    """按日状态文件带 BOM 时，读侧不得判"损坏"而清空整日数据。

    写入方 `_write_sign_state` 的读-改-写若用 utf-8 读带 BOM 的存档，json 解析
    必失败 → 按空数据重建 → 当日已写结论全部丢失。读侧其余入口
    （`_daily_statuses` / sched-run）早已统一 utf-8-sig，本用例钉住写侧同口径。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-signstate-")
        p = mock.patch.object(state_io, "_state_dir", return_value=self.tmp)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_bom_file_keeps_existing_entries(self):
        path = os.path.join(
            self.tmp, f"sign-state-{state_io.clock.now():%Y-%m-%d}.json")
        # utf-8-sig 写：文件头带 BOM（Windows 记事本等工具另存的形态）
        with open(path, "w", encoding="utf-8-sig") as f:
            json.dump({"13800000001": {"status": "success", "message": "先写入"}}, f)

        state_io._write_sign_state("13800000002", "failed", "后写入")

        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
        self.assertEqual(data["13800000001"]["status"], "success",
                         "带 BOM 的既有条目被当损坏清空重建 = 整日数据丢失")
        self.assertEqual(data["13800000002"]["status"], "failed")


_PHONE = "13800000001"


@unittest.skipUnless(os.name == "posix", "umask 与 POSIX 模式位在 Windows 上不生效")
class StateWritesArePrivateUnderWideUmaskTest(unittest.TestCase):
    """私有写单通道的验收不变量：外层 `umask(0o000)` 下每个写站点终文件仍 0600、无 .tmp 残留。

    为什么判据必须拉宽 umask：测试进程默认继承 077 时，内置 `open()` 建的 tmp 经
    `os.replace` 后也是 0600——不拉宽就恒绿，这正是当年 14 处状态写入站点（含 6 处
    明手机号）漏网的机制。改造后模式只由 `os.open(..., 0o600)` 钉死，与外层 umask、
    systemd `UMask=`、入口 `os.umask(0o077)` 都无关（后两者降为纵深，不再是任何
    站点模式的唯一来源）。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-umask-")
        old = os.umask(0o000)
        self.addCleanup(os.umask, old)
        env = mock.patch.dict(os.environ, {"YIBAN_STATE_DIR": self.tmp})
        env.start()
        self.addCleanup(env.stop)
        statedir = mock.patch.object(scheduler, "STATEDIR", self.tmp)
        statedir.start()
        self.addCleanup(statedir.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.day = state_io.clock.now().strftime("%Y-%m-%d")

    def _assert_private(self, path, site):
        self.assertTrue(os.path.exists(path), f"{site}：终文件必须落盘")
        mode = stat.S_IMODE(os.stat(path).st_mode)
        self.assertEqual(mode, 0o600,
                         f"{site}：umask(0o000) 下终文件仍须 0600，实际 {oct(mode)}"
                         f"——宽模式说明写盘又押回了 umask")
        residues = [n for n in os.listdir(self.tmp) if ".tmp" in n]
        self.assertEqual(residues, [], f"{site}：成功写盘后不得留半成品 tmp")

    # ---- 各站点的驱动（返回终文件路径）----
    def _drive_sign_state(self):
        state_io._write_sign_state(_PHONE, state_io.STATUS_SUCCESS, "签到成功")
        return os.path.join(self.tmp, f"sign-state-{self.day}.json")

    def _drive_sign_state_hold(self):
        # 先落一条终态，再用 pending 计划触达"计划态不覆盖已有结果"的保留分支
        state_io._write_sign_state(_PHONE, state_io.STATUS_SUCCESS, "签到成功")
        state_io._write_sign_state(_PHONE, state_io.STATUS_PENDING,
                                   "计划 07:00", scheduled="07:00:00")
        return os.path.join(self.tmp, f"sign-state-{self.day}.json")

    def _drive_sched_done(self):
        state_io._write_sched_done({"ok_n": 1})
        return os.path.join(self.tmp, f"sched-run-{self.day}.json")

    def _drive_fallback_alive(self):
        state_io._write_fallback_alive()
        return os.path.join(self.tmp, state_io.FALLBACK_ALIVE_FILE)

    def _drive_fail_mail_reserve(self):
        self.assertTrue(alerts._user_fail_mail_reserve(_PHONE, self.day))
        return os.path.join(self.tmp, f"mail-user-fail-{self.day}.json")

    def _drive_fail_mail_release(self):
        alerts._user_fail_mail_reserve(_PHONE, self.day)
        alerts._user_fail_mail_release(_PHONE, self.day)
        return os.path.join(self.tmp, f"mail-user-fail-{self.day}.json")

    def _drive_sched_snapshot(self):
        runner._write_sched_snapshot(self.tmp, self.day)
        return os.path.join(self.tmp, f"sched-snapshot-{self.day}.json")

    def _drive_sign_daily(self):
        accounts = [SimpleNamespace(phone=_PHONE)]
        results = {_PHONE: (True, "ok", False, runner.STATUS_SUCCESS)}
        runner._write_sign_daily(self.tmp, accounts, results)
        return os.path.join(self.tmp, f"sign-daily-{self.day}.json")

    def _drive_cred_state(self):
        cred_state.merge([_PHONE], {_PHONE: {"fail_days": 1, "last_fail": self.day}})
        return os.path.join(self.tmp, "cred-state.json")

    def _drive_notify_ledger(self):
        ledger._save_ledger_file({"urgent": {"date": self.day, "count": 1,
                                             "pending": False, "notified": False,
                                             "warned": False}})
        return os.path.join(self.tmp, "notify-ledger.json")

    def _drive_notify_throttle(self):
        ledger._save_throttle_file({"某标题": 1.0})
        return os.path.join(self.tmp, "notify-throttle.json")

    def _drive_probe_state(self):
        probe._write_probe_state({"last_run": self.day})
        return os.path.join(self.tmp, "probe-state.json")

    def _drive_sched_slot(self):
        scheduler._mark_slot("first")
        return scheduler._slot_marker("first")

    def _drive_sched_heartbeat(self):
        scheduler._touch_heartbeat(self.tmp)
        return scheduler._heartbeat_path(self.tmp)

    def _drive_channel_direct(self):
        path = os.path.join(self.tmp, "direct.json")
        state_io._write_private_json(path, {"a": 1})
        return path

    def test_every_state_site_is_0600_under_umask_0000(self):
        """14 站点逐条：umask(0o000) 反例矩阵（含 6 处明手机号站点优先覆盖）。"""
        sites = [
            ("按日状态主写", self._drive_sign_state),
            ("按日状态计划保留分支", self._drive_sign_state_hold),
            ("用户失败提醒额度预占", self._drive_fail_mail_reserve),
            ("用户失败提醒额度归还", self._drive_fail_mail_release),
            ("按日汇总 sign-daily", self._drive_sign_daily),
            ("账密熔断 cred-state", self._drive_cred_state),
            ("全量收尾 sched-run", self._drive_sched_done),
            ("兜底心跳 fallback-alive", self._drive_fallback_alive),
            ("调度快照 sched-snapshot", self._drive_sched_snapshot),
            ("推送额度账本", self._drive_notify_ledger),
            ("推送节流表", self._drive_notify_throttle),
            ("探针状态 probe-state", self._drive_probe_state),
            ("容器时段标记 sched-slot", self._drive_sched_slot),
            ("容器调度心跳", self._drive_sched_heartbeat),
            ("单通道直写", self._drive_channel_direct),
        ]
        self.assertGreaterEqual(len(sites), 14, "站点矩阵本身不得缩于登记数")
        for site, drive in sites:
            with self.subTest(site=site):
                shutil.rmtree(self.tmp, ignore_errors=True)
                os.makedirs(self.tmp, exist_ok=True)
                self._assert_private(drive(), site)

    def test_worker_alive_heartbeat_also_private(self):
        """既有用法（并行执行体心跳）同口径回归：它是最早走单通道的一处。"""
        state_io.mark_worker_started(1)
        self._assert_private(state_io.worker_alive_path(1), "并行执行体心跳")


class StateWriteChannelSourceGuardTest(unittest.TestCase):
    """单通道源码守卫：生产状态写盘模块不得再用内置 `open()` 建 tmp。

    判据是"只走一个通道"而非"补 chmod"——出现 `open(tmp, "w")` 形态即模式重新
    押回 umask；允许只读 `open(path, encoding=...)` 与合规的 `os.open(...0o600)`。
    """

    _FILES = (
        "yiban/engine/state_io.py",
        "yiban/engine/alerts.py",
        "yiban/engine/runner.py",
        "yiban/engine/probe.py",
        "yiban/cred_state.py",
        "yiban/notify/ledger.py",
        "yiban/infra/private_json.py",
        "docker/scheduler.py",
    )
    _BAD = re.compile(r'\bopen\([^)]*[tT]mp[^)]*,\s*["\']w')

    def _read(self, rel):
        with open(os.path.join(BASE, *rel.split("/")), encoding="utf-8") as f:
            return f.read()

    def test_no_builtin_open_tmp_write(self):
        for rel in self._FILES:
            src = self._read(rel)
            for lineno, line in enumerate(src.splitlines(), 1):
                if "os.open" in line:      # 合规通道自己的创建行
                    continue
                if self._BAD.search(line):
                    self.fail(f"{rel}:{lineno} 用内置 open() 写 tmp——模式会继承 umask，"
                              f"必须走状态文件私有写单通道：{line.strip()}")

    def test_guard_fires_on_the_shape_it_must_reject(self):
        """活体反例（N 簇元判据）：把守卫喂给它该拒的旧形态，必须命中。"""
        self.assertTrue(self._BAD.search('with open(tmp, "w", encoding="utf-8") as f:'),
                        "守卫正则失效 = 这条门禁形同虚设")
        self.assertTrue(self._BAD.search('with open(daily_tmp, "w") as f:'))


if __name__ == "__main__":
    unittest.main(verbosity=2)
