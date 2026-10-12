# -*- coding: utf-8 -*-
"""M27：`.env` 与进程环境的优先级统一为「`.env` 优先，进程环境只补缺」。

标签：A · 配置：邮件/推送读侧
覆盖：`notify.config._env_str`、`notify.ledger._loginfail_daily_limit`、
   `mail.config._get` 与 `_ALLOW_PRIVATE_KEY` 的取值优先级，以及"`.env` 没有该键时
   仍回退进程环境"这一兜底不得被翻掉。

对应实现：yiban/notify/config.py（`_env_str`）、yiban/notify/ledger.py
   （`_loginfail_daily_limit`）、yiban/mail/config.py（`_get`、私有 CA 开关）。

关键断言：**写侧是 `.env` 优先，读侧也必须是**。本组件的写侧（设置页落盘）与 web 读侧
   都以 `.env` 为事实源；读侧若"环境变量优先"，键一旦进了 web 进程环境，设置页的写入
   就**静默失效**，GET 回显与磁盘状态长期不一致——管理员改完配置看不到任何变化。
   判据是"两处都设了同一个键时取哪个"，不是"能不能读到值"。

依赖：临时 `.env` + 环境变量打桩；不触网、不建库。整文件在本机执行，无 skip。
"""
import os
import shutil
import tempfile
import unittest

from yiban.mail import config as mail_config
from yiban.notify import config as notify_config
from yiban.notify import ledger as notify_ledger

ENV_KEY = "YIBAN_NOTIFY_COOLDOWN"
MAIL_KEY = "YIBAN_MAIL_ENABLE"


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-envprec-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env_file = os.path.join(self.tmp, ".env")
        self._backup = {k: os.environ.get(k) for k in
                        ("YIBAN_ENV_FILE", ENV_KEY, MAIL_KEY,
                         "YIBAN_LOGINFAIL_DAILY_MAX", "YIBAN_NOTIFY_TYPE",
                         "YIBAN_MAIL_ALLOW_PRIVATE_KEY")}

        def _restore():
            for k, v in self._backup.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        self.addCleanup(_restore)
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write("")
        os.environ["YIBAN_ENV_FILE"] = self.env_file
        for k in self._backup:
            if k != "YIBAN_ENV_FILE":
                os.environ.pop(k, None)

    def _set_env_file(self, text):
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write(text)

    def _set_environ(self, key, value):
        os.environ[key] = value


class NotifyEnvStrTest(_Base):
    """`notify.config._env_str` —— 推送配置读侧。"""

    def test_env_file_wins_over_process_env(self):
        self._set_env_file(f"{ENV_KEY}=111\n")
        self._set_environ(ENV_KEY, "222")
        self.assertEqual(notify_config._env_str("COOLDOWN"), "111",
                         "`.env` 优先：否则设置页写入被进程环境静默盖住")

    def test_process_env_fills_gap_when_env_file_has_no_key(self):
        """`.env` 没有该键时回退进程环境——翻优先级不得把这类部署读成"未配置"。"""
        self._set_env_file("YIBAN_OTHER=1\n")
        self._set_environ(ENV_KEY, "222")
        self.assertEqual(notify_config._env_str("COOLDOWN"), "222")

    def test_absent_everywhere_is_empty(self):
        self._set_env_file("YIBAN_OTHER=1\n")
        self.assertEqual(notify_config._env_str("COOLDOWN"), "")

    def test_blank_env_file_value_falls_back_to_process_env(self):
        """`.env` 里写了空值不算"已配置"，仍让进程环境补缺（与旧读法的空值语义一致）。"""
        self._set_env_file(f"{ENV_KEY}=\n")
        self._set_environ(ENV_KEY, "222")
        self.assertEqual(notify_config._env_str("COOLDOWN"), "222")

    def test_caller_supplied_snapshot_is_used(self):
        """调用方传入的 `envs` 快照优先于再读文件（`get_config` 的复用路径不得被破）。"""
        self._set_env_file(f"{ENV_KEY}=111\n")
        self._set_environ(ENV_KEY, "222")
        self.assertEqual(notify_config._env_str("COOLDOWN", {ENV_KEY: "999"}), "999")

    def test_get_secret_and_is_configured_follow_the_same_precedence(self):
        """经 `_env_str` 的上层（`get_secret` / `is_configured`）必须同口径。"""
        self._set_env_file("YIBAN_NOTIFY_TYPE=serverchan\n")
        self._set_environ("YIBAN_NOTIFY_TYPE", "custom")
        self.assertEqual(notify_config._env_str("TYPE"), "serverchan")


class LedgerDailyLimitTest(_Base):
    """`notify.ledger._loginfail_daily_limit` —— 独立命名键，声明"与其他 notify 键一致"。"""

    def test_env_file_wins_over_process_env(self):
        self._set_env_file("YIBAN_LOGINFAIL_DAILY_MAX=7\n")
        self._set_environ("YIBAN_LOGINFAIL_DAILY_MAX", "9")
        self.assertEqual(notify_ledger._loginfail_daily_limit(), 7,
                         "该键自称与其他 notify 键同口径，判据不得与 `_env_str` 分叉")

    def test_process_env_fills_gap(self):
        self._set_env_file("YIBAN_OTHER=1\n")
        self._set_environ("YIBAN_LOGINFAIL_DAILY_MAX", "9")
        self.assertEqual(notify_ledger._loginfail_daily_limit(), 9)


class MailConfigTest(_Base):
    """`mail.config._get` 与私有 CA 开关 —— 邮件配置读侧，与推送同一条口径。"""

    def test_get_env_file_wins_over_process_env(self):
        self._set_env_file(f"{MAIL_KEY}=1\n")
        self._set_environ(MAIL_KEY, "0")
        self.assertEqual(mail_config._get("ENABLE"), "1",
                         "邮件配置与推送配置是同一族口径，不应一个翻了一个没翻")

    def test_get_process_env_fills_gap(self):
        self._set_env_file("YIBAN_OTHER=1\n")
        self._set_environ(MAIL_KEY, "0")
        self.assertEqual(mail_config._get("ENABLE"), "0")

    def test_private_host_switch_follows_the_same_precedence(self):
        """私网放行开关走的是手写的一行读取，同样必须 `.env` 优先。

        `_allow_private_host` 不经 `_get`（它多一个 `env` 注入形参），两处若各判一次
        优先级就会漂移：同一个键在两条读法上取到不同的值。
        """
        key = mail_config._ALLOW_PRIVATE_KEY
        self._set_env_file(f"{key}=1\n")
        self._set_environ(key, "")
        self.assertTrue(mail_config._allow_private_host(),
                        "`.env` 写了 1、进程环境为空 ⇒ 取 1")

    def test_private_host_switch_process_env_fills_gap(self):
        key = mail_config._ALLOW_PRIVATE_KEY
        self._set_env_file("YIBAN_OTHER=1\n")
        self._set_environ(key, "1")
        self.assertTrue(mail_config._allow_private_host(),
                        "`.env` 没有该键时回退进程环境")

    def test_private_host_switch_injected_env_still_wins_unchanged(self):
        """`env` 注入形参的既有契约不变：给了就只读它（不回落文件/环境）。"""
        key = mail_config._ALLOW_PRIVATE_KEY
        self._set_env_file(f"{key}=0\n")
        self._set_environ(key, "0")
        self.assertTrue(mail_config._allow_private_host({key: "1"}),
                        "显式注入的映射必须仍然优先（测试与调用方的既有约定）")


if __name__ == "__main__":
    unittest.main()
