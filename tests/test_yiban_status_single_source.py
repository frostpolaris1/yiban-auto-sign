# -*- coding: utf-8 -*-
"""状态词汇表单一事实源：`yiban.status` 定义，signin / web 只做别名引用。

标签：D · 状态词汇与账号生命周期
覆盖：12 个状态码常量在 `signin` 侧与 `yiban.status` 等值、`STATUS_SYMBOL`/
    `UNDONE_STATUSES` 是**同一对象**；web 侧常量与 `STATUS_ICON`/`STATUS_TEXT` 同为
    别名；未了结集合的成员口径；两张展示映射刻意不合并。
对应实现：权威定义 = `yiban/status.py`（`STATUS_*`、`SYMBOL`、`ICON`、`TEXT`、
    `UNDONE_STATUSES`）；`scripts/signin.py` 与 `web/app.py` 里的同名符号是引用别名。
关键断言：用 `assertIs` 而不是 `assertEqual`——值相等也可能是各复制一份，那正是曾经
    的病灶（web 侧缺 `no_position`/`global_paused`，signin 符号表缺 `pending`）。
    未了结集合里不得出现成功类状态，否则会无限触发补签轮。
依赖：importlib 隔离加载 `web/app.py` + 直接 import `signin`；临时 .env/DB/STATE，
    `YIBAN_DISABLE_PURGE_LOOP=1` 关掉导入期清理循环；不触网、不起子进程。

**两张映射刻意不合并**（服务不同消费方，合并会改变前端可见表现，属前后端协同改动），
所以这里断言的是"别名指向同一对象"，而不是"内容相同"。
"""
import contextlib
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import db  # noqa: E402

from yiban import status as yiban_status  # noqa: E402

TEST_KEY = "a" * 64


class StatusSingleSourceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-status-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        os.environ.update({
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_ENV_FILE": cls.env_file,
            "YIBAN_DB_FILE": os.path.join(cls.tmp, "yiban.db"),
            "YIBAN_STATE_DIR": cls.tmp,
            "YIBAN_DISABLE_PURGE_LOOP": "1",  #导入 web 模块会带起清理循环：测试进程里必须关掉，否则动到真实状态目录
        })
        import signin
        cls.signin = signin
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

    def test_signin_is_alias_not_duplicate(self):
        for name in ("STATUS_SUCCESS", "STATUS_ALREADY", "STATUS_NO_TASK", "STATUS_FAILED",
                     "STATUS_RETRYING", "STATUS_SKIPPED_WINDOW", "STATUS_SKIPPED_NORANGE",
                     "STATUS_NO_POSITION", "STATUS_PAUSED", "STATUS_USER_CANCELLED",
                     "STATUS_PENDING", "STATUS_GLOBAL_PAUSED"):
            with self.subTest(name=name):
                self.assertEqual(getattr(self.signin, name), getattr(yiban_status, name))
        self.assertIs(self.signin.STATUS_SYMBOL, yiban_status.SYMBOL,  #is 而非 ==：复制一份内容相同的字典同样能骗过等值断言
                      "符号表必须是同一对象（别名），不是重新构造的副本")
        self.assertIs(self.signin.UNDONE_STATUSES, yiban_status.UNDONE_STATUSES)

    def test_web_is_alias_not_duplicate(self):
        for name in ("STATUS_SUCCESS", "STATUS_ALREADY", "STATUS_NO_TASK", "STATUS_FAILED",
                     "STATUS_RETRYING", "STATUS_SKIPPED_WINDOW", "STATUS_SKIPPED_NORANGE",
                     "STATUS_PAUSED", "STATUS_USER_CANCELLED", "STATUS_PENDING"):
            with self.subTest(name=name):
                self.assertEqual(getattr(self.webapp, name), getattr(yiban_status, name))
        self.assertIs(self.webapp.STATUS_ICON, yiban_status.ICON)
        self.assertIs(self.webapp.STATUS_TEXT, yiban_status.TEXT)

    def test_undone_statuses_contents(self):
        """未了结集合的口径（补签闸门依赖它：落此集合即判定本轮未跑完）。"""
        self.assertEqual(
            set(yiban_status.UNDONE_STATUSES),
            {"failed", "retrying", "pending", "skipped_window", "skipped_norange",
             "no_position"},
        )
        # 成功类状态不得进集合（否则会无限触发补签轮）
        for done in (yiban_status.STATUS_SUCCESS, yiban_status.STATUS_ALREADY,
                     yiban_status.STATUS_NO_TASK, yiban_status.STATUS_PAUSED,
                     yiban_status.STATUS_USER_CANCELLED):
            self.assertNotIn(done, yiban_status.UNDONE_STATUSES)

    def test_word_and_display_maps_are_separate_by_design(self):
        """两张映射服务不同消费方，**当前确实不同**——记录差异，合并须前后端协同。"""
        self.assertIn(yiban_status.STATUS_NO_POSITION, yiban_status.SYMBOL)
        self.assertNotIn(yiban_status.STATUS_NO_POSITION, yiban_status.ICON)
        self.assertIn(yiban_status.STATUS_PENDING, yiban_status.ICON)
        self.assertNotIn(yiban_status.STATUS_PENDING, yiban_status.SYMBOL)


if __name__ == "__main__":
    unittest.main(verbosity=2)
