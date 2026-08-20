# -*- coding: utf-8 -*-
"""全局暂停（一键暂停签到）测试。

覆盖：
- 自动签到：YIBAN_GLOBAL_PAUSE=1 时 main() 退出码 2（SKIPPED 语义）
- 手动签到：--only 不受暂停影响（放行，不走到 exit(2)）
- 状态常量：STATUS_GLOBAL_PAUSED 定义于 STATUS_SYMBOL 之前（防 NameError）
"""
import json
import os
import sys
import tempfile
import unittest
import unittest.mock as mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))

import signin  # noqa: E402


def _run_main(env_override, argv=None):
    """在隔离环境下执行 signin.main()，返回退出码或捕获的 SystemExit。"""
    accounts_json = json.dumps([{"phone": "13800000000", "password": "test-pass", "name": "测试"}])
    tmp = tempfile.mkdtemp(prefix="yiban-pause-test-")
    old_env = {k: os.environ.get(k) for k in ("YIBAN_GLOBAL_PAUSE", "YIBAN_ACCOUNTS_JSON",
                                              "YIBAN_DB_FILE", "YIBAN_STATE_DIR", "YIBAN_LOG_FILE")}
    old_argv = sys.argv[:]
    try:
        os.environ["YIBAN_ACCOUNTS_JSON"] = accounts_json
        os.environ["YIBAN_DB_FILE"] = os.path.join(tmp, "empty.db")
        os.environ["YIBAN_STATE_DIR"] = tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(tmp, "sign.log")
        for k, v in env_override.items():
            os.environ[k] = v
        sys.argv = ["signin.py"] + (argv or [])
        try:
            signin.main()
            return 0
        except SystemExit as e:
            return e.code
    finally:
        for k in ("YIBAN_GLOBAL_PAUSE", "YIBAN_ACCOUNTS_JSON", "YIBAN_DB_FILE",
                  "YIBAN_STATE_DIR", "YIBAN_LOG_FILE"):
            if old_env.get(k) is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old_env[k]
        sys.argv = old_argv


class TestGlobalPause(unittest.TestCase):
    def test_auto_signin_paused_exits_2(self):
        """自动签到 + 全局暂停 → exit 2（SKIPPED 语义，在周日检查之后、执行签到之前）。"""
        with mock.patch.object(signin, "load_accounts") as m_load:
            m_load.return_value = [mock.Mock(phone="13800000000", user_paused=False)]
            code = _run_main({"YIBAN_GLOBAL_PAUSE": "1"})
        self.assertEqual(code, 2)

    def test_manual_signin_not_blocked(self):
        """--only 手动签到不受全局暂停影响（不应 exit 2）。

        run_queue_retry 是真实网络签到路径，测试环境不触网——mock 掉，仅验证
        "全局暂停检查放行 --only"这一分支语义（防止沙箱无外网时请求挂起）。
        """
        with mock.patch.object(signin, "load_accounts") as m_load, \
             mock.patch.object(signin, "run_queue_retry", return_value={}), \
             mock.patch.object(signin, "_save_cred_state"):
            m_load.return_value = [mock.Mock(phone="13800000000", user_paused=False)]
            code = _run_main({"YIBAN_GLOBAL_PAUSE": "1"}, argv=["--only", "13800000000"])

        # 未暂停拦截 → 不会 exit 2；继续执行（--only 手动放行，进入实际签到）
        self.assertNotEqual(code, 2)

    def test_constant_defined_before_symbol(self):
        """STATUS_GLOBAL_PAUSED 必须定义于 STATUS_SYMBOL 之前（防 NameError）。"""
        self.assertEqual(signin.STATUS_GLOBAL_PAUSED, "global_paused")
        self.assertIn(signin.STATUS_GLOBAL_PAUSED, signin.STATUS_SYMBOL)
        self.assertEqual(signin.STATUS_SYMBOL[signin.STATUS_GLOBAL_PAUSED], "⏸")


if __name__ == "__main__":
    unittest.main()
