# -*- coding: utf-8 -*-
"""启动断言"两侧读到同一把钥"（MF-50 验收不变量②）：fail-closed 与接线。

现网拓扑：web 的 YIBAN_ACCOUNTS_KEY 由 systemd `EnvironmentFile=/etc/yiban/accounts-key`
注入（env 档），引擎读 `.env`（文件档），且 **env 优先于 .env**——旧 README 的轮换
流程只让改 `.env`，照做必得"web 一把钥、engine 另一把钥"，且无 kid 时动手前后都
无法自证，换钥后告警通道静默死亡。修法：两档并存且解出不同钥 ⇒ 立即拒绝
（load_key 的 env 档分支与启动自证 `assert_key_sources_agree` 同一判据），
把"另一侧静默解不开"换成"本侧启动即崩 + 日志带两侧 kid"。

功能：两侧同钥断言的判据矩阵与两侧进程启动接线回归。
归属：`yiban/infra/account_crypto.py`（断言本体）、`web/app.py` create_app 与
`yiban/engine/runner.py` main（两侧启动接线）。
复用：`assert_key_sources_agree`、`load_key`、`key_fingerprint`。

标签：G · 安全：脱敏/审计/配置注入
覆盖：判据矩阵四格（两档同钥=放行 INFO；两档分叉=抛 ValueError 含双侧 kid；
仅 env 档=WARNING"第二把钥"；仅文件档=INFO；两档皆无=None 且零写盘）；
文件档非法 hex 按"不一致"抛（非法不是没有）；YIBAN_ENV_FILE 定位；引擎
`runner.main` 分叉形态 rc=1（配置错误，不新增退出码）；web/引擎启动接线的
源码守卫；反向控制（同钥部署照常启动，断言不误杀）。
对应实现：`account_crypto._assert_env_matches_env_file` /
`assert_key_sources_agree` / `load_key` env 档分支、`web/app.py` create_app 的
`account_crypto.assert_key_sources_agree(ENV_FILE)`、`runner.main` 的 rc=1 收口。
关键断言：分叉格必须断"抛的是 ValueError **且消息同时含两把钥的 kid**"——只断
抛出会放过"报错了但说不出是谁和谁"的不可自证形态；rc 测试必须确认引擎在
加载账号/派发**之前**就拒（打桩 load_accounts，断其未被调用）。
依赖：临时目录 .env + 环境变量夹具（teardown 还原并清 `_KEY_CACHE`）；无网络；
`runner.main` 用例把 YIBAN_LOG_FILE/YIBAN_STATE_DIR/YIBAN_DB_FILE 钉进临时目录。
"""
import contextlib
import io
import logging
import os
import shutil
import tempfile
import unittest
from unittest import mock

from yiban.engine import runner
from yiban.infra import account_crypto

KEY_A = "a" * 64
KEY_B = "b" * 64
KEY_A_UPPER = ("a" * 48 + "A" * 16)  # 与 KEY_A 解出同一把钥（大小写写法差异）


class StartupAssertMatrixTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-keyfork-")
        self.env_file = os.path.join(self.tmp, ".env")
        self._old = {k: os.environ.get(k) for k in
                     ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE")}
        os.environ.pop("YIBAN_ACCOUNTS_KEY", None)
        os.environ.pop("YIBAN_ENV_FILE", None)
        account_crypto._KEY_CACHE = None

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        account_crypto._KEY_CACHE = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_env(self, key_hex, extra="OTHER=1\n"):
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write(extra + f"YIBAN_ACCOUNTS_KEY={key_hex}\n")

    def _logs_at(self, level):
        buf = io.StringIO()
        h = logging.StreamHandler(buf)
        h.setLevel(level)
        fmt = logging.Formatter("%(levelname)s %(message)s")
        h.setFormatter(fmt)
        lg = logging.getLogger("yiban-crypto")
        lg.addHandler(h)
        old = lg.level
        # NOTSET(0) 表示"继承父级"，直接 min(old, level) 会把 0 设回去、INFO 仍被
        # 父级 WARNING 挡掉——显式降到 level 之下（DEBUG）由 handler 过滤。
        lg.setLevel(logging.DEBUG)
        return buf, h, lg, old

    def test_both_tiers_same_key_agrees_with_kid_log(self):
        self._write_env(KEY_A)
        os.environ["YIBAN_ACCOUNTS_KEY"] = KEY_A
        buf, h, lg, old = self._logs_at(logging.INFO)
        try:
            kid = account_crypto.assert_key_sources_agree(self.env_file)
        finally:
            lg.removeHandler(h)
            lg.setLevel(old)
        self.assertEqual(kid, account_crypto.key_fingerprint(bytes.fromhex(KEY_A)))
        out = buf.getvalue()
        self.assertIn("同一把钥", out)
        self.assertIn(kid, out, "启动自证日志必须带 kid（可自证）")

    def test_both_tiers_differing_refuses_startup(self):
        self._write_env(KEY_A)
        os.environ["YIBAN_ACCOUNTS_KEY"] = KEY_B
        with self.assertRaises(ValueError) as ctx:
            account_crypto.assert_key_sources_agree(self.env_file)
        msg = str(ctx.exception)
        self.assertIn("两侧不一致", msg)
        self.assertIn(account_crypto.key_fingerprint(bytes.fromhex(KEY_B)), msg)
        self.assertIn(account_crypto.key_fingerprint(bytes.fromhex(KEY_A)), msg)

    def test_case_difference_is_not_a_fork(self):
        """十六进制大小写不同解出同一把钥——放行（判据比较解码字节）。"""
        self._write_env(KEY_A)
        os.environ["YIBAN_ACCOUNTS_KEY"] = KEY_A_UPPER
        kid = account_crypto.assert_key_sources_agree(self.env_file)
        self.assertEqual(kid, account_crypto.key_fingerprint(bytes.fromhex(KEY_A)))

    def test_env_only_tier_warns_about_second_key_generation(self):
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write("OTHER=1\n")  # 文件里没有该键
        os.environ["YIBAN_ACCOUNTS_KEY"] = KEY_A
        buf, h, lg, old = self._logs_at(logging.WARNING)
        try:
            kid = account_crypto.assert_key_sources_agree(self.env_file)
        finally:
            lg.removeHandler(h)
            lg.setLevel(old)
        self.assertEqual(kid, account_crypto.key_fingerprint(bytes.fromhex(KEY_A)))
        self.assertIn("第二把钥", buf.getvalue())
        # 反向控制：WARNING 是提示不是拒绝，且不得回写 .env（该断言零写盘）
        with open(self.env_file, encoding="utf-8") as f:
            self.assertNotIn("YIBAN_ACCOUNTS_KEY", f.read())

    def test_file_only_tier_infos_with_kid(self):
        self._write_env(KEY_A)
        buf, h, lg, old = self._logs_at(logging.INFO)
        try:
            kid = account_crypto.assert_key_sources_agree(self.env_file)
        finally:
            lg.removeHandler(h)
            lg.setLevel(old)
        self.assertEqual(kid, account_crypto.key_fingerprint(bytes.fromhex(KEY_A)))
        self.assertIn("仅", buf.getvalue())

    def test_no_tier_returns_none_without_generating(self):
        """两档皆无 = 首启尚未建钥：断言无对象，返回 None 且**不得**触发自动建钥。"""
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write("OTHER=1\n")
        self.assertIsNone(account_crypto.assert_key_sources_agree(self.env_file))
        with open(self.env_file, encoding="utf-8") as f:
            self.assertNotIn("YIBAN_ACCOUNTS_KEY", f.read())

    def test_file_tier_illegal_hex_counts_as_disagreement(self):
        """文件档存在但非法（短 hex）：对面进程解不出钥 ⇒ 必然不一致，抛。"""
        self._write_env("abcd")
        os.environ["YIBAN_ACCOUNTS_KEY"] = KEY_A
        with self.assertRaises(ValueError):
            account_crypto.assert_key_sources_agree(self.env_file)

    def test_env_file_locator_via_YIBAN_ENV_FILE(self):
        """不显式传路径时按 YIBAN_ENV_FILE 定位文件档（与 load_key 同回落链）。"""
        self._write_env(KEY_A)
        os.environ["YIBAN_ACCOUNTS_KEY"] = KEY_B
        os.environ["YIBAN_ENV_FILE"] = self.env_file
        with self.assertRaises(ValueError):
            account_crypto.assert_key_sources_agree()

    def test_load_key_env_branch_enforces_too(self):
        """运行期纵深：不止启动那一刻——任何 load_key 走 env 档都重比对。"""
        self._write_env(KEY_A)
        os.environ["YIBAN_ACCOUNTS_KEY"] = KEY_B
        with self.assertRaises(ValueError):
            account_crypto.load_key(self.env_file)


class EngineStartupWiringTest(unittest.TestCase):
    """引擎侧接线：分叉部署下 `runner.main` 在加载账号/派发之前拒启（rc=1）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-keyfork-engine-")
        self.env_file = os.path.join(self.tmp, ".env")
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={KEY_A}\n")
        self._old = {k: os.environ.get(k) for k in
                     ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE",
                      "YIBAN_LOG_FILE", "YIBAN_STATE_DIR", "YIBAN_DB_FILE")}
        os.environ.update({
            "YIBAN_ACCOUNTS_KEY": KEY_B,  # env 档 ≠ 文件档 ⇒ 分叉
            "YIBAN_ENV_FILE": self.env_file,
            "YIBAN_LOG_FILE": os.path.join(self.tmp, "log", "yiban.log"),
            "YIBAN_STATE_DIR": os.path.join(self.tmp, "state"),
            "YIBAN_DB_FILE": os.path.join(self.tmp, "nope.db"),
        })
        account_crypto._KEY_CACHE = None

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        account_crypto._KEY_CACHE = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_main(self, argv):
        # 打桩账号加载：断言若失效、流程走到加载账号，这里以 AssertionError 暴露
        with mock.patch("yiban.engine.runner.accounts_mod.load_accounts",
                        side_effect=AssertionError("不得走到加载账号")), \
                contextlib.redirect_stderr(io.StringIO()):
            return runner.main(argv)

    def test_engine_refuses_fork_before_loading_accounts(self):
        # --second-run-check 选它做驱动：无 fork 时它最轻（只读状态），
        # 有 fork 时必须在任何业务动作前拒启；rc=1 走"配置错误"既有档，不新增退出码
        self.assertEqual(self._run_main(["--second-run-check"]), 1)

    def test_engine_refuses_fork_in_sign_mode_too(self):
        self.assertEqual(self._run_main(["--check-config"]), 1)


class StartupWiringSourceGuardTest(unittest.TestCase):
    """两侧启动接线是"存在性"契约：任一入口撤掉调用即红。"""

    def _read(self, rel):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, rel), encoding="utf-8") as f:
            return f.read()

    def test_web_and_engine_both_call_the_assert(self):
        self.assertIn("account_crypto.assert_key_sources_agree(",
                      self._read("web/app.py"))
        self.assertIn("account_crypto.assert_key_sources_agree(",
                      self._read("yiban/engine/runner.py"))


if __name__ == "__main__":
    unittest.main()
