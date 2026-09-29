# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""三入口日志装配点的脱敏兜底挂载钉：入口初始化后，logger 链上必须在防线内。

兜底脱敏（`yiban.logging_ext.MaskingFormatter`）的价值在"任何调用点都不可能留下
裸号"——但前提是每个常驻入口真的把它挂上。全仓三个日志装配入口：CLI
（`yiban/engine/cli_support.py::_setup_cli_logging`，经 `runner.main` 调用，覆盖
`scripts/signin.py` 与 `yiban.cli` 的 sign/probe）、web（`web/app.py::create_app`
的 `DailyFlockFileHandler`）、容器调度器（`docker/scheduler.py::_setup_logging`，
supervisord 常驻形态，stdout 进 sched.log）。本文件对每个入口各钉两层：
装配点挂出的 handler 必须带 `MaskingFormatter`（换成裸 `logging.Formatter` 即红），
且在该入口初始化后的真实输出面上构造含裸号的日志记录，落点必为遮罩形态——
形态取生产实测的两款：方括号行（round/client 系列）与中文逗号分隔裸号
（session_cache 作废行，当日 78 行 = 78 个明文号的那条）。

功能：日志脱敏防线的入口级覆盖回归。
归属：`yiban.logging_ext`（输出面兜底）× 三处装配点。
复用：`MaskingFormatter` / `mask_phones_in_text`；装载方式与
`tests/test_container_scheduler.py`、`tests/test_logs_export_masking.py` 同款
（按文件路径 importlib 装载，不依赖包名）。
通信：CLI 用临时目录日志文件读回；web 用临时库 + `create_app` 后按天文件读回；
容器用 `sys.stdout` 换 StringIO 后读缓冲；全部本地，不发网络。
标签：G · 安全：脱敏/审计/配置注入
覆盖：三入口各自的挂载钉（handler 上 formatter 是 `MaskingFormatter`）+ 每入口
端到端不变量（经该入口装配的输出面写裸号 → 落点只剩遮罩形态）+ 装配幂等
（重复调用不叠挂）+ 容器入口 `__main__` 接线、CLI 入口 `runner.main` 接线。
对应实现：`docker/scheduler.py` 的 `_setup_logging` 与 `__main__`、
`yiban/engine/cli_support.py` 的 `_setup_cli_logging`、`web/app.py` 的 `create_app`
日志装配段、`yiban/logging_ext.py` 的 `MaskingFormatter`。
关键断言：钉的是"挂载 + 落点遮罩"两件事——只测 formatter 行为（见
`tests/test_log_masking_formatter.py`）拦不住"装配点把 formatter 摘掉或压根没挂"；
只测挂载不测落点则 formatter 被换成恒等子类也照样绿。容器入口的 stdout 缓冲
在 `_setup_logging()` 调用前换好：`StreamHandler` 在构造时刻绑定流，后换无效。
依赖：无网络、无 skip；`docker/scheduler.py` 与 `web/app.py` 均按文件路径装载
（scheduler 装载会执行 `import signin` 等模块级导入，与既有容器调度器用例同环境）；
用例结束后 root handler 与相关 logger 级别全部现场还原，不污染后续测试。
"""
import contextlib
import importlib.util
import io
import json
import logging
import os
import shutil
import sys
import tempfile
import unittest

from yiban import clock
from yiban.logging_ext import MaskingFormatter

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 假号（138****0000 风格）：生产实测的两款泄露形态各配一条样例文本
PHONE_RAW = "13800001234"
PHONE_MASKED = "138****1234"
BRACKET_LINE = "[%s] ⏹️ 用户已取消签到，跳过执行"
COMMA_LINE = "会话缓存作废（跨业务日）: %s，本次真实登录"

_FMT = "[%(asctime)s] [%(levelname)s] %(name)s: %(message)s"


def _load_by_path(alias, relpath):
    """按文件路径装载非包内模块（与既有 docker/web 用例同款做法）。"""
    spec = importlib.util.spec_from_file_location(alias, os.path.join(BASE, relpath))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)
    return mod


class _MountHarness(unittest.TestCase):
    """保存/还原 root logger 现场（handler 与级别）的公共底座。

    三个装配函数都会动 root 与若干具名 logger 的级别；不还原的话，同进程后续
    用例的日志链路会被这些用例自己挂的 handler 干扰。
    """

    WATCHED = ("yiban", "signin", "scheduler", "web", "notify", "db",
               "requests", "urllib3", "werkzeug", "gunicorn")

    def setUp(self):
        self.root = logging.getLogger()
        self._saved_handlers = list(self.root.handlers)
        self._saved_level = self.root.level
        self._saved_levels = {n: logging.getLogger(n).level for n in self.WATCHED}
        self._saved_handler_lists = {
            n: list(logging.getLogger(n).handlers) for n in self.WATCHED}

    def tearDown(self):
        for h in list(self.root.handlers):
            if h not in self._saved_handlers:
                with contextlib.suppress(Exception):
                    h.close()
                self.root.removeHandler(h)
        self.root.setLevel(self._saved_level)
        for n in self.WATCHED:
            lg = logging.getLogger(n)
            lg.setLevel(self._saved_levels[n])
            for h in list(lg.handlers):
                if h not in self._saved_handler_lists[n]:
                    with contextlib.suppress(Exception):
                        h.close()
                    lg.removeHandler(h)

    def _new_root_handlers(self):
        return [h for h in self.root.handlers if h not in self._saved_handlers]

    def _assert_masked_in(self, text, *, label):
        self.assertIn(PHONE_MASKED, text, f"{label}：输出面未见遮罩形态")
        self.assertNotIn(PHONE_RAW, text, f"{label}：输出面泄露裸号")


class ContainerEntryMountTest(_MountHarness):
    """容器调度器入口：`_setup_logging` 挂 stdout+MaskingFormatter，且入口确实调它。"""

    @classmethod
    def setUpClass(cls):
        cls.sched = _load_by_path("container_scheduler_mountpin", "docker/scheduler.py")

    @classmethod
    def tearDownClass(cls):
        # 不注销的话，按同名 alias 重复装载的其它用例可能撞上这个已执行模块
        sys.modules.pop("container_scheduler_mountpin", None)

    def _mounted(self):
        new = self._new_root_handlers()
        self.assertEqual(len(new), 1, "_setup_logging 应且仅应给 root 挂一个 handler")
        self.assertIsInstance(new[0].formatter, MaskingFormatter,
                              "容器入口 handler 上必须是 MaskingFormatter（换裸 "
                              "Formatter 即脱敏兜底被摘）")
        return new[0]

    def test_setup_logging_mounts_masking_formatter_and_masks_output(self):
        buf = io.StringIO()
        # StreamHandler 构造时刻绑定 sys.stdout：必须先换缓冲再装配
        real_stdout = sys.stdout
        sys.stdout = buf
        try:
            self.sched._setup_logging()
            handler = self._mounted()
            self.sched._setup_logging()  # 幂等：重复装配不得叠挂
            logging.getLogger("yiban.store.session_cache").info(
                COMMA_LINE, PHONE_RAW)
            logging.getLogger("yiban.engine.round").info(BRACKET_LINE, PHONE_RAW)
            # 调度器自身留痕也在防线内（logger 名即"挂了的链条"上的名字）
            logging.getLogger("scheduler").info(BRACKET_LINE, PHONE_RAW)
            handler.flush()
            self._assert_masked_in(buf.getvalue(), label="容器入口 stdout")
        finally:
            sys.stdout = real_stdout

    def test_main_entry_wires_setup_logging(self):
        """`__main__`（supervisord 常驻形态）必须先装配日志再进主循环。

        取**最后一个** `__main__` 段：文件头还有一处 `--check-health` 快路（在业务
        导入图之前短路退出，本就不该装配日志），只有常驻形态这一段是判据。
        """
        with open(os.path.join(BASE, "docker", "scheduler.py"), encoding="utf-8") as f:
            src = f.read()
        entry = src[src.rindex('if __name__ == "__main__"'):]
        self.assertIn("_setup_logging()", entry,
                      "容器入口 __main__ 未调用 _setup_logging——防线在容器形态缺席")
        self.assertLess(entry.index("_setup_logging()"), entry.index("main_loop()"),
                        "应先装配日志再进主循环，否则启动期日志不过兜底")


class CliEntryMountTest(_MountHarness):
    """CLI 入口：`_setup_cli_logging` 挂的按天 handler 带 MaskingFormatter，落盘遮罩。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-mount-cli-")
        cls._saved_env = os.environ.get("YIBAN_LOG_FILE")
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")

    @classmethod
    def tearDownClass(cls):
        if cls._saved_env is None:
            os.environ.pop("YIBAN_LOG_FILE", None)
        else:
            os.environ["YIBAN_LOG_FILE"] = cls._saved_env
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_setup_cli_logging_mounts_masking_formatter_and_masks_file(self):
        from yiban.engine import cli_support
        saved_flag = cli_support._cli_logging_ready
        cli_support._cli_logging_ready = False
        saved_root_handlers = list(self.root.handlers)
        for h in saved_root_handlers:  # 模拟干净进程：basicConfig 只在 root 空时生效
            self.root.removeHandler(h)
        try:
            cli_support._setup_cli_logging()
            self._mounted()
            cli_support._setup_cli_logging()  # 幂等标记：重复调用不叠挂
            self.assertEqual(len(self._new_root_handlers()), 1)
            logging.getLogger("yiban.store.session_cache").info(COMMA_LINE, PHONE_RAW)
            logging.getLogger("yiban.engine.round").info(BRACKET_LINE, PHONE_RAW)
            self.root.handlers[0].flush()
            day = clock.now().strftime("%Y-%m-%d")
            with open(os.path.join(self.tmp, f"sign-{day}.log"), encoding="utf-8") as f:
                self._assert_masked_in(f.read(), label="CLI 入口按天日志")
        finally:
            cli_support._cli_logging_ready = saved_flag
            for h in list(self.root.handlers):
                if h not in saved_root_handlers:
                    with contextlib.suppress(Exception):
                        h.close()
                    self.root.removeHandler(h)
            for h in saved_root_handlers:
                self.root.addHandler(h)
            self.root.setLevel(logging.NOTSET)  # basicConfig 会改 root 级别，复位

    def _mounted(self):
        new = self._new_root_handlers()
        self.assertEqual(len(new), 1, "CLI 入口应且仅应给 root 挂一个 handler")
        self.assertIsInstance(new[0].formatter, MaskingFormatter,
                              "CLI 入口 handler 上必须是 MaskingFormatter")
        return new[0]

    def test_runner_main_wires_setup_cli_logging(self):
        """CLI 真实入口是 `runner.main`（scripts/signin.py 与 yiban.cli 都转交它）。"""
        import inspect

        from yiban.engine import runner
        src = inspect.getsource(runner.main)
        self.assertIn("_setup_cli_logging()", src,
                      "runner.main 未装配 CLI 日志——防线在 CLI 形态缺席")


class WebEntryMountTest(_MountHarness):
    """web 入口：`create_app` 挂的 DailyFlockFileHandler 带 MaskingFormatter。"""

    TEST_KEY = "a" * 64
    ADMIN_PASS = "TestPass1234!"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-mount-web-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={cls.TEST_KEY}\n"
                f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={cls.ADMIN_PASS}\n"
            )
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        with open(cls.accounts_file, "w", encoding="utf-8") as f:
            json.dump([], f)
        cls.log_file = os.path.join(cls.tmp, "sign.log")
        cls._saved_env = {}
        for k, v in (("YIBAN_ACCOUNTS_KEY", cls.TEST_KEY),
                     ("YIBAN_ENV_FILE", cls.env_file),
                     ("YIBAN_ACCOUNTS_FILE", cls.accounts_file),
                     ("YIBAN_USERS_FILE", os.path.join(cls.tmp, "users.json")),
                     ("YIBAN_DB_FILE", cls.db_file),
                     ("YIBAN_STATE_DIR", cls.tmp),
                     ("YIBAN_LOG_FILE", cls.log_file)):
            cls._saved_env[k] = os.environ.get(k)
            os.environ[k] = v
        import db
        cls.db = db
        cls.db.init_db(cls.db_file, migrate_from=cls.accounts_file,
                       env_file=cls.env_file)
        cls.webapp = _load_by_path("webapp_mountpin", "web/app.py")

    @classmethod
    def tearDownClass(cls):
        if cls.db._conn is not None:
            with contextlib.suppress(Exception):
                cls.db._conn.close()
            cls.db._conn = None
        for k, v in cls._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        sys.modules.pop("webapp_mountpin", None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_create_app_mounts_masking_formatter_and_masks_file(self):
        # 装载 web/app.py 期间的导入副作用不在此限；只认 create_app 之后的现场
        self.webapp.create_app()
        mounted = [h for h in self._new_root_handlers()
                   if type(h).__name__ == "DailyFlockFileHandler"]
        self.assertEqual(len(mounted), 1, "create_app 应给 root 挂按天文件 handler")
        self.assertIsInstance(mounted[0].formatter, MaskingFormatter,
                              "web 入口 handler 上必须是 MaskingFormatter")
        logging.getLogger("yiban.store.session_cache").info(COMMA_LINE, PHONE_RAW)
        logging.getLogger("yiban.engine.round").info(BRACKET_LINE, PHONE_RAW)
        mounted[0].flush()
        day = clock.now().strftime("%Y-%m-%d")
        with open(os.path.join(self.tmp, f"sign-{day}.log"), encoding="utf-8") as f:
            self._assert_masked_in(f.read(), label="web 入口按天日志")


if __name__ == "__main__":
    unittest.main()
