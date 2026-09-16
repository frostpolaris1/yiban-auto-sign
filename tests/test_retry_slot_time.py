# -*- coding: utf-8 -*-
"""补签轮时刻（"当天最后一轮触发点"）的单一事实源与三处取用点。

缺陷背景：signin 的告警抑制原先用模块常量 `_LAST_RETRY_HM = (7, 10)`，而宿主补签
cron 的实际时刻是 `YIBAN_SECOND_RUN_TIME`（默认 07:12，可配）。两侧不一致的后果
**有方向性**：阈值早于真实末轮 → 提前告警（噪音）；晚于真实末轮 → 真异常当天不再有
任何提示（静默漏报，危害更大）。现收敛到 `yiban.window.retry_hm()`。

覆盖：
1. `retry_hm()` 的取值与容错（默认 / 覆盖 / 非法回退），且**按调用时刻读环境**
   （导入期缓存会让"改了键不生效"）；
2. 宿主 `run.sh` 的缺省字面量与 `DEFAULT_RETRY_HM` 一致，且把该键导出给子进程；
3. `signin._maybe_alert_zero_success` 按该键的双向行为（越过即告警 / 未越即静默）；
4. 容器 `scheduler` 把**自己实际用的**触发点注入子进程环境（容器不读该键，
   不注入则 signin 会按宿主默认值判，与容器的 07:10 错位）。
"""
import os
import re
import tempfile
import unittest
from datetime import datetime as clock_cls
from types import SimpleNamespace
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import scheduler  # noqa: E402
import signin  # noqa: E402

from yiban import window  # noqa: E402


class RetryHmAtoMidTest(unittest.TestCase):
    """取值与容错：与窗口起止同用 parse_hhmm（接受 `7:30` 与 `07:30`）。"""

    def test_default_matches_documented_retry_cron(self):
        self.assertEqual(window.retry_hm({}), (7, 12))
        self.assertEqual(window.retry_hm({}), window.DEFAULT_RETRY_HM)

    def test_env_override(self):
        self.assertEqual(window.retry_hm({"YIBAN_SECOND_RUN_TIME": "07:30"}), (7, 30))
        self.assertEqual(window.retry_hm({"YIBAN_SECOND_RUN_TIME": "7:30"}), (7, 30))
        self.assertEqual(window.retry_hm({"YIBAN_SECOND_RUN_TIME": " 06:05 "}), (6, 5))

    def test_invalid_falls_back(self):
        for raw in ("", "abc", "25:00", "07:60", "07", None, "07:12:30"):
            with self.subTest(raw=raw):
                self.assertEqual(window.retry_hm({"YIBAN_SECOND_RUN_TIME": raw}),
                                 window.DEFAULT_RETRY_HM)

    def test_reads_env_at_call_time(self):
        """导入期缓存会让"管理员改了补签时刻"在长驻进程里不生效。"""
        with mock.patch.dict(os.environ, {"YIBAN_SECOND_RUN_TIME": "07:40"}):
            self.assertEqual(window.retry_hm(), (7, 40))
        os.environ.pop("YIBAN_SECOND_RUN_TIME", None)
        self.assertEqual(window.retry_hm(), window.DEFAULT_RETRY_HM)


class HostRunShContractTest(unittest.TestCase):
    """宿主 run.sh 是同键的另一个取用点：缺省必须同源，且要传给子进程。"""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(BASE, "run.sh"), encoding="utf-8") as f:
            cls.src = f.read()

    def test_default_literal_matches_shared_constant(self):
        m = re.search(r'\$\{YIBAN_SECOND_RUN_TIME:-(\d{1,2}:\d{2})\}', self.src)
        self.assertIsNotNone(m, "run.sh 未按 SECOND_HHMM=\"${YIBAN_SECOND_RUN_TIME:-HH:MM}\" 取补签时刻")
        hh, mm = m.group(1).split(":")
        self.assertEqual((int(hh), int(mm)), window.DEFAULT_RETRY_HM,
                         "run.sh 默认补签时刻与 yiban.window.DEFAULT_RETRY_HM 漂移")

    def test_exports_key_to_child(self):
        """不导出时子进程读不到 .env 之外的实际取值（signin 会退回默认）。"""
        self.assertRegex(self.src, r'(?m)^export YIBAN_SECOND_RUN_TIME=')


class SigninAlertThresholdTest(unittest.TestCase):
    """告警抑制按**当前**补签时刻判断，且双向都按实际值走。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-retry-slot-")
        p = mock.patch.dict(os.environ, {"YIBAN_STATE_DIR": self.tmp})
        p.start()
        self.addCleanup(p.stop)
        signin._mail_summary.clear()
        self.addCleanup(signin._mail_summary.clear)
        self.accounts = [SimpleNamespace(phone="13800000001"),
                         SimpleNamespace(phone="13800000002")]
        self.results = {
            "13800000001": (True, "已签到", False, "success"),
            "13800000002": (False, "签到时段已结束", True, "skipped_window"),
        }

    def _alert(self, hm):
        """注入固定时刻跑一次告警判定（该路径只取 clock.now() 的时分）。"""
        moment = clock_cls(2026, 9, 15, hm[0], hm[1])
        with mock.patch.object(signin.clock, "now", lambda: moment):
            return signin._maybe_alert_zero_success(
                self.accounts, self.results, ok_n=1, is_second_run=False)

    def test_late_retry_slot_suppresses(self):
        """补签时刻被配到 07:30：07:20 的首签轮仍有下一轮兜底 → 不打扰。"""
        with mock.patch.dict(os.environ, {"YIBAN_SECOND_RUN_TIME": "07:30"}):
            self.assertFalse(self._alert((7, 20)))

    def test_early_retry_slot_alerts(self):
        """补签时刻被配到 07:05：07:20 已是当天最后一轮 → 必须告警（防静默漏报）。"""
        with mock.patch.dict(os.environ, {"YIBAN_SECOND_RUN_TIME": "07:05"}):
            self.assertTrue(self._alert((7, 20)))

    def test_default_slot_boundary(self):
        """缺省 07:12：正好到点即为最后一轮（06:31 关机后 07:12 才起跑的经典场景）。"""
        os.environ.pop("YIBAN_SECOND_RUN_TIME", None)
        self.assertFalse(self._alert((7, 11)))
        self.assertTrue(self._alert((7, 12)))


class ContainerInjectsRetrySlotTest(unittest.TestCase):
    """容器把实际触发点（SECOND）注入子进程环境——容器不读 YIBAN_SECOND_RUN_TIME。"""

    def test_child_env_carries_actual_slot(self):
        captured = {}

        class _Proc:
            def wait(self, timeout=None):
                return 0

        def _fake_popen(cmd, cwd=None, env=None):
            captured["env"] = dict(env or {})
            return _Proc()

        with mock.patch.object(scheduler.subprocess, "Popen", _fake_popen), \
             mock.patch.object(scheduler, "build_child_env", return_value={}):
            scheduler._run_signin_child()
        expect = f"{scheduler.SECOND[0]:02d}:{scheduler.SECOND[1]:02d}"
        self.assertEqual(captured["env"].get("YIBAN_SECOND_RUN_TIME"), expect)

    def test_injection_wins_over_stale_env_key(self):
        """.env 里若写了该键，容器也不据此调度 → 必须以容器实际值覆盖，
        否则 signin 会按一个没人用的时刻判断末轮（静默漏报方向）。"""
        captured = {}

        class _Proc:
            def wait(self, timeout=None):
                return 0

        def _fake_popen(cmd, cwd=None, env=None):
            captured["env"] = dict(env or {})
            return _Proc()

        with mock.patch.object(scheduler.subprocess, "Popen", _fake_popen), \
             mock.patch.object(scheduler, "build_child_env",
                               return_value={"YIBAN_SECOND_RUN_TIME": "23:59"}):
            scheduler._run_signin_child()
        self.assertEqual(captured["env"]["YIBAN_SECOND_RUN_TIME"],
                         f"{scheduler.SECOND[0]:02d}:{scheduler.SECOND[1]:02d}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
