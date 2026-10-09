# -*- coding: utf-8 -*-
"""ba-p11-04 止血回归：`scripts/child_env.py` 读不到 `.env` 必须响亮告警。

标签：B · 调度：领取/队列/执行体
覆盖：`scripts/child_env.py` 的两类读失败出口（路径不存在 / 存在但不可读）必须各留
   一行 WARNING（点名路径 + 说明后果），且取值仍是"告警后继续"（返回继承环境、不抛）；
   可读路径不得告警；容器调度器与 web 手动签到两条消费方路径下告警必须到达 root
   日志面（即调度器 stdout handler / web 按天文件 handler 所挂的祖先）。
对应实现：`scripts/child_env.py`（parse_env_file / build_child_env）、
   `docker/scheduler.py::_run_signin_child`、`web/routes/signin_api.py::_launch_signin_proc`。
关键断言：三类形态必须**分别**成立——不存在与不可读的消息不同、都不抛、可读时零告警。
   把三者混成"任何 OSError 一律静默"（本次修复前的形态）正是工单点名的病：管理员事后
   在设置页改的 YIBAN_PROXY / 全局暂停对子进程静默失效，且无一行日志指向"`.env` 没读到"。
   消费方用**真入口**（调度器 spawn 函数 / web 手动签到 spawn 函数）验证告警不被吞。
依赖：临时目录、按路径装载 `docker/scheduler.py`；子进程一律打桩，无网络、无 skip。
用法（项目根目录）：py -m pytest tests/test_child_env_degrade_loud.py -v
"""
import contextlib
import importlib.util
import logging
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 告警通道：与 `yiban/infra/env_io.py` 的 .env 解析告警同通道（调度器与 web 两侧
# 都把 "yiban" 放开到 INFO 且挂在 root 之下，故 WARNING 必然到达各自处理器）
CHANNEL = "yiban"


def _load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_child_env():
    """按文件路径装载 `scripts/child_env.py`（非包内模块，沿用既有装载方式）。"""
    return _load_module(
        os.path.join(BASE, "scripts", "child_env.py"), "yiban_child_env_degrade")


class _Capture(logging.Handler):
    """挂在 root 上收记录：证明告警**传播到了**进程日志面，而非被吞在某层。"""

    def __init__(self):
        super().__init__(level=logging.NOTSET)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def warnings(self):
        return [r for r in self.records if r.levelno >= logging.WARNING]


@contextlib.contextmanager
def _capture_from_root():
    handler = _Capture()
    chan = logging.getLogger(CHANNEL)
    prev = chan.level
    chan.setLevel(logging.DEBUG)   # 只放开本通道，不动 root 全局级别
    logging.getLogger().addHandler(handler)
    try:
        yield handler
    finally:
        logging.getLogger().removeHandler(handler)
        chan.setLevel(prev)


class ChildEnvDegradeLoudTest(unittest.TestCase):
    """红线：`.env` 读不到时不许静默退化。"""

    @classmethod
    def setUpClass(cls):
        cls.m = _load_child_env()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-childenv-loud-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def _missing(self):
        return os.path.join(self.tmp, "no-such.env")

    def _unreadable(self):
        """目录冒充"存在但不可读"（IsADirectoryError：属 OSError 但非 FileNotFoundError）。

        `chmod 000` 在 root 身份下读得到，故不做；目录是稳的构造，且与 run.sh 的
        `[ -e ]` 分类同判（存在 ⇒ "存在但不可读"）。
        """
        d = os.path.join(self.tmp, "adir")
        os.mkdir(d)
        return d

    def test_missing_env_warns_and_still_returns_inherited_env(self):
        path = self._missing()
        base = {"PATH": "/x", "YIBAN_PROXY": "http://old:1"}
        with _capture_from_root() as cap:
            env = self.m.build_child_env(path, base=base)
        warns = cap.warnings()
        self.assertEqual(len(warns), 1, f"应恰好一条 WARNING，实得 {cap.records!r}")
        msg = warns[0].getMessage()
        self.assertEqual(warns[0].name, CHANNEL)
        self.assertIn("不存在", msg)
        self.assertIn(path, msg, "告警必须点名路径")
        self.assertIn("YIBAN_", msg, "告警必须说明后果：全部 YIBAN_* 配置回落")
        self.assertIn("进程环境", msg)
        # 不中断：返回值与"纯继承"完全一致（键集合 = base，无任何注入）
        self.assertEqual(env, base)

    def test_unreadable_env_message_differs_from_missing(self):
        missing_path, unreadable_path = self._missing(), self._unreadable()
        base = {"PATH": "/x"}
        with _capture_from_root() as cap:
            env = self.m.build_child_env(unreadable_path, base=base)
        warns = cap.warnings()
        self.assertEqual(len(warns), 1)
        msg = warns[0].getMessage()
        self.assertIn("存在但当前用户不可读", msg)
        self.assertIn(unreadable_path, msg)
        self.assertEqual(env, base, "读失败仍不得抛、不得改动继承环境")
        with _capture_from_root() as cap2:
            self.m.parse_env_file(missing_path)
        self.assertNotEqual(msg, cap2.warnings()[0].getMessage(),
                            "两类读失败必须给出可区分的消息")

    def test_readable_env_no_warning_and_keys_injected(self):
        path = os.path.join(self.tmp, ".env")
        with open(path, "w", encoding="utf-8") as f:
            f.write("YIBAN_PROXY=http://new:2\nNOT_YIBAN=x\n")
        base = {"PATH": "/x", "YIBAN_PROXY": "http://old:1"}
        with self.assertNoLogs(CHANNEL, level="WARNING"):
            env = self.m.build_child_env(path, base=base)
        self.assertEqual(env.get("YIBAN_PROXY"), "http://new:2", ".env 值覆盖进程环境")
        self.assertEqual(env.get("PATH"), "/x")
        self.assertNotIn("NOT_YIBAN", env)


class ChildEnvConsumerVisibilityTest(unittest.TestCase):
    """消费方可见性：调度器 / web 两条 spawn 路径下告警真到达日志面。"""

    @classmethod
    def setUpClass(cls):
        cls.sched = _load_module(
            os.path.join(BASE, "docker", "scheduler.py"), "scheduler_childenv_loud")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-childenv-consumer-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.missing = os.path.join(self.tmp, "no-such.env")

    def test_scheduler_child_spawn_warns_to_root(self):
        """真入口：容器调度器的签到子进程 spawn（`_run_signin_child` 内调
        `build_child_env(ENV_FILE)`）。"""
        spawned = {}

        def fake_popen(cmd, **kw):
            spawned["cmd"] = cmd
            spawned["env"] = kw.get("env", {})
            return types.SimpleNamespace(wait=lambda timeout=None: 0)

        saved = self.sched.ENV_FILE
        self.sched.ENV_FILE = self.missing
        try:
            with mock.patch.object(self.sched.subprocess, "Popen", fake_popen), \
                    _capture_from_root() as cap:
                self.sched._run_signin_child(env=None)
        finally:
            self.sched.ENV_FILE = saved
        warns = cap.warnings()
        self.assertTrue(warns, "调度器路径下告警被吞：必须有一行 WARNING")
        self.assertIn("不存在", warns[0].getMessage())
        self.assertIn(self.missing, warns[0].getMessage())
        self.assertIn("PATH", spawned["env"],
                      "读不到 .env 时仍是纯继承（进程环境为底座，无注入）")

    def test_web_manual_signin_spawn_warns_to_root(self):
        """真入口：web 手动签到的子进程 spawn（`_launch_signin_proc`）。"""
        sys.path.insert(0, os.path.join(BASE, "scripts"))
        import child_env

        from web.routes import signin_api

        m = types.SimpleNamespace(
            __file__=os.path.join(BASE, "web", "app.py"),
            ENV_FILE=self.missing,
            DB_FILE=os.path.join(self.tmp, "yiban.db"),
            child_env=child_env,
            log_path_for=lambda: os.path.join(self.tmp, "sign.log"),
        )
        spawned = {}

        def fake_popen(cmd, **kw):
            spawned["env"] = kw.get("env", {})
            return types.SimpleNamespace(pid=os.getpid())

        with mock.patch.object(signin_api.subprocess, "Popen", fake_popen), \
                _capture_from_root() as cap:
            signin_api._launch_signin_proc(m, "13800000000")
        warns = cap.warnings()
        self.assertTrue(warns, "web 手动签到路径下告警被吞：必须有一行 WARNING")
        self.assertIn("不存在", warns[0].getMessage())
        self.assertIn(self.missing, warns[0].getMessage())
        # 不中断：显式注入的两枚路径键仍在（spawn 未被告警改变）
        self.assertEqual(spawned["env"].get("YIBAN_ENV_FILE"), self.missing)


if __name__ == "__main__":
    unittest.main()
