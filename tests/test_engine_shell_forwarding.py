# -*- coding: utf-8 -*-
"""兼容壳的**打桩转发**有效性（引擎按"执行一轮"切分后的收口自证）。

壳存在的意义不止"旧名还能 import"：既有用例大量以 `signin.<名字> = 替身` /
`mock.patch.object(signin, ...)` 打桩，而调用点已经搬进 `yiban/engine/*`。若壳只转发
**读取**、不把写入同步到真正持有该名字的实现模块，打桩就会静默失效——测试表面通过、
实则跑的是真实现（真登录、真写状态文件、真发告警）。这类失效最难发现：断言往往
"照样过"，直到生产上出现重复登录才暴露。

本文件逐个钉住"实现模块里的跨模块调用点看到的是替身"：先用替身记录调用，再从壳发起
一轮，断言替身确实被调用过。新增引擎模块时，把新出现的高频打桩名照此补一行即可。

用法（项目根目录）：python -m pytest tests/test_engine_shell_forwarding.py -v
"""
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import signin  # noqa: E402

PHONE = "13800000001"


class ShellForwardingStubTest(unittest.TestCase):
    """`signin.<名字> = 替身` 必须落到实现模块，跨模块调用点才看得到。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-shell-fwd-")
        os.environ.update({
            "YIBAN_STATE_DIR": cls.tmp,
            "YIBAN_LOG_FILE": os.path.join(cls.tmp, "sign.log"),
            "YIBAN_DB_FILE": os.path.join(cls.tmp, "yiban.db"),
            "YIBAN_ENV_FILE": os.path.join(cls.tmp, ".env"),
        })

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_STATE_DIR", "YIBAN_LOG_FILE", "YIBAN_DB_FILE", "YIBAN_ENV_FILE"):
            os.environ.pop(k, None)

    def _acc(self):
        return SimpleNamespace(phone=PHONE, user_paused=False, owner="")

    def _run_round(self, calls):
        """跑一轮单账号队列；三个高频打桩名都换成记录调用的替身。"""

        def fake_attempt(_acc):
            calls["attempt_signin"].append(_acc.phone)
            return True, "签到成功", False, signin.STATUS_SUCCESS

        def fake_cred(_state, phone, *_a, **_kw):
            calls["_update_cred_state"].append(phone)

        def fake_state(phone, *_a, **_kw):
            calls["_write_sign_state"].append(phone)

        with mock.patch.object(signin, "attempt_signin", side_effect=fake_attempt), \
                mock.patch.object(signin, "_update_cred_state", side_effect=fake_cred), \
                mock.patch.object(signin, "_write_sign_state", side_effect=fake_state), \
                mock.patch.object(signin.time, "sleep"):
            signin.run_queue_retry([self._acc()], "", 0, 0, schedule=None, cred_state={})

    def test_attempt_signin_stub_reaches_the_engine(self):
        """`attempt_signin` 的调用点在 `yiban/engine/round.py`（单账号尝试已迁出壳）。"""
        calls = {"attempt_signin": [], "_update_cred_state": [], "_write_sign_state": []}
        self._run_round(calls)
        self.assertEqual(calls["attempt_signin"], [PHONE],
                         "替身没被调用：打桩被固定在旧引用上（转发失效）")

    def test_update_cred_state_stub_reaches_the_engine(self):
        """`_update_cred_state` 的调用点在引擎轮次里（熔断计数随一轮队列走）。"""
        calls = {"attempt_signin": [], "_update_cred_state": [], "_write_sign_state": []}
        self._run_round(calls)
        self.assertEqual(calls["_update_cred_state"], [PHONE],
                         "替身没被调用：熔断计数打桩静默失效")

    def test_write_sign_state_stub_reaches_the_engine(self):
        """`_write_sign_state` 的调用点在引擎轮次里（状态文件写入随一轮队列走）。"""
        calls = {"attempt_signin": [], "_update_cred_state": [], "_write_sign_state": []}
        self._run_round(calls)
        self.assertEqual(calls["_write_sign_state"], [PHONE],
                         "替身没被调用：状态写入打桩静默失效")

    def test_run_queue_retry_stub_reaches_the_entry(self):
        """`run_queue_retry` 的调用点在 `yiban/engine/runner.py::main`。

        同时钉住壳入口的退出码传递：`signin.main()` 抛 `SystemExit(码)`（旧调用方与
        run.sh 依赖它），而 `yiban.engine.runner.main` 只**返回**码。
        """
        acc = self._acc()
        # 归"窗口外未了结"→ 退出码 2（run.sh 据此写 SKIPPED 并触发补签）
        window_skip = {PHONE: (False, "签到时段已结束", True, signin.STATUS_SKIPPED_WINDOW)}
        with mock.patch.object(signin, "load_accounts", return_value=[acc]), \
                mock.patch.object(signin, "run_queue_retry",
                                  return_value=window_skip) as m_round, \
                mock.patch.object(signin, "_maybe_alert_zero_success", return_value=False), \
                self.assertRaises(SystemExit) as ctx:
            signin.main(["--only", PHONE])
        self.assertEqual(ctx.exception.code, 2)
        m_round.assert_called_once()
        self.assertEqual(m_round.call_args[0][0], [acc], "实参应是本轮账号列表")

    def test_runner_main_returns_code_instead_of_exiting(self):
        """`runner.main` 返回 int（壳负责 sys.exit）——两端契约不同，勿合并。"""
        from yiban.engine import runner
        window_skip = {PHONE: (False, "签到时段已结束", True, signin.STATUS_SKIPPED_WINDOW)}
        with mock.patch.object(signin, "load_accounts", return_value=[self._acc()]), \
                mock.patch.object(signin, "run_queue_retry", return_value=window_skip), \
                mock.patch.object(signin, "_maybe_alert_zero_success", return_value=False):
            code = runner.main(["--only", PHONE])
        self.assertEqual(code, 2)
        self.assertIsInstance(code, int)


if __name__ == "__main__":
    unittest.main(verbosity=2)
