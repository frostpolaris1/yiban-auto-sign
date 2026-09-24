# -*- coding: utf-8 -*-
"""全局暂停（一键暂停签到）的开关语义与状态词汇。

标签：D · 状态词汇与账号生命周期
覆盖：YIBAN_GLOBAL_PAUSE=1 时自动签到 exit 2（SKIPPED 语义）、`--only` 手动签到不受阻、
    STATUS_GLOBAL_PAUSED 的取值与符号映射。
对应实现：状态码与符号的**权威定义**在 `yiban.status`（`STATUS_GLOBAL_PAUSED`、
    `SYMBOL`），`scripts/signin.py` 里只是别名转发；暂停判定发生在签到入口
    `signin.main()`（引擎实现 `yiban/engine/runner.py`）。
关键断言：暂停只对自动路径生效——手动路径若也被 exit 2 挡住，管理员就无法在暂停期间
    单独补签；状态码字面量 "global_paused" 与符号 ⏸ 一并锁死。
依赖：进程内 mock（`load_accounts`/`run_queue_retry`/`_save_cred_state`）+ 临时
    STATE/DB/LOG 与 YIBAN_ACCOUNTS_JSON 注入；不起子进程、不触网、不需 bash/docker。

`test_manual_signin_not_blocked` 把 `run_queue_retry` mock 掉：那是真实网络签到路径，
本用例只验"暂停检查放行 --only"这一分支，不放它去触网（沙箱无外网时会挂住）。
"""
import json
import os
import sys
import tempfile
import unittest
import unittest.mock as mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import signin  # noqa: E402


def _run_main(env_override, argv=None):
    """在隔离环境下执行 signin.main()，返回退出码或捕获的 SystemExit。"""
    accounts_json = json.dumps([{"phone": "13800000000", "password": "test-pass", "name": "测试"}])
    tmp = tempfile.mkdtemp(prefix="yiban-pause-test-")
    old_env = {k: os.environ.get(k) for k in ("YIBAN_GLOBAL_PAUSE", "YIBAN_ACCOUNTS_JSON",
                                              "YIBAN_DB_FILE", "YIBAN_STATE_DIR", "YIBAN_LOG_FILE")}
    old_argv = sys.argv[:]
    try:
        os.environ["YIBAN_ACCOUNTS_JSON"] = accounts_json  #账号走环境变量注入：不落 accounts.json 就不必清理落盘的口令
        os.environ["YIBAN_DB_FILE"] = os.path.join(tmp, "empty.db")
        os.environ["YIBAN_STATE_DIR"] = tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(tmp, "sign.log")
        for k, v in env_override.items():
            os.environ[k] = v
        sys.argv = ["signin.py"] + (argv or [])  #改 sys.argv 而非传参：main() 自己读 argv，这才等价于 cron 命令行
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

    def test_status_constant_and_symbol_mapping(self):
        """全局暂停的状态码取值与符号映射（前端按符号渲染，改动需同步前端映射）。"""
        self.assertEqual(signin.STATUS_GLOBAL_PAUSED, "global_paused")
        self.assertIn(signin.STATUS_GLOBAL_PAUSED, signin.STATUS_SYMBOL)
        self.assertEqual(signin.STATUS_SYMBOL[signin.STATUS_GLOBAL_PAUSED], "⏸")  #锁的是符号字面量：日历按它渲染，换符号得跟前端一起换


if __name__ == "__main__":
    unittest.main()
