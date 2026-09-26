# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""容器调度器异常面与锁目录 fail-closed 回归。

标签：B · 调度：领取/队列/执行体
覆盖：签到子进程 Popen 抛 OSError 时容器调度器不崩（同文件同判法：接住留痕继续调度）、
   主循环单 tick 兜底（任意 Exception 记日志进下一 tick，KeyboardInterrupt/SystemExit
   直通不被吞）、心跳落盘与探活出口（--check-health 新鲜 0 / 陈旧或非 0）、
   supervisord 对 sched 的存活参数（startsecs/startretries 显式写死，崩溃重启不进
   FATAL 躺平）、compose healthcheck 同时覆盖 web 与 sched、宿主 run.sh 锁目录
   mkdir 失败时拒绝运行（不再静默回退 /tmp，与 08-21 加固注释同一威胁模型）。
对应实现：docker/scheduler.py（_run_signin_child、main_loop、_touch_heartbeat、
   healthcheck_main、__main__ 出口）、docker/supervisord.conf（[program:sched]）、
   docker-compose.yml（healthcheck）、run.sh（LOCK_DIR 块）。
关键断言：一次 Popen 失败或一次 tick 内异常不得让首签/补签/探针/兜底/清理同进程全废；
   兜底的边界是 Exception——监督停机/容器 stop 依赖的信号通路必须原样穿出；
   健康信号必须覆盖 sched 本身而不只 web 端口；锁目录建不成 ⇒ rc=1，
   静默换址等于把锁放到威胁模型点名过的可预测位置。配置面（supervisord/healthcheck）
   本机不可起容器，以配置级断言 + 可执行出口（--check-health 真跑）交付，
   容器内整链生效待生产演练。
依赖：调度器用例按文件路径加载 docker/scheduler.py + subprocess/time 模块桩
   （真循环靠 sleep 抛异常打断）；心跳探活 CLI 用例起 sys.executable 子进程；
   run.sh 锁目录用例真起 bash 子进程（无 bash 时整类跳过，同 test_schedule_retry
   惯例）；supervisord/compose 为纯文本/INI 解析断言。不发网络请求。

用法（在项目根目录）：
    py -m pytest tests/test_scheduler_exception_surface.py -v
"""
import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import subprocess as _sp
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCHED_PATH = os.path.join(BASE, "docker", "scheduler.py")
SUPERVISORD = os.path.join(BASE, "docker", "supervisord.conf")
COMPOSE = os.path.join(BASE, "docker-compose.yml")
RUN_SH = os.path.join(BASE, "run.sh")
BASH = shutil.which("bash")


def _load_sched():
    """按文件路径加载调度器（它不在包内，与 tests/test_container_scheduler.py 同法）。"""
    spec = importlib.util.spec_from_file_location("sched_exc_surface", _SCHED_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Stop(Exception):
    """测试中用于打断 main_loop 无限循环的异常（只在 sleep 桩处抛出）。"""


class _FakeProc:
    """Popen 桩返回的假进程：wait() 立即正常返回。"""

    returncode = 0

    def wait(self, timeout=None):
        return 0


def _stub_module(**members):
    return type("_Stub", (), {k: staticmethod(v) for k, v in members.items()})()


def _boom_popen(cmd, **kw):
    """恒定抛 OSError 的 Popen 桩（模拟 fork 失败/解释器丢失）。"""
    raise OSError("Injected: cannot execute")


class _SchedBase(unittest.TestCase):
    """公共底座：临时状态目录 + 全桩化的 main_loop 触发环境。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sched-exc-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sched = _load_sched()
        self.sched.STATEDIR = self.tmp
        self.sched.ENV_FILE = os.path.join(self.tmp, ".env")
        # 触发点放开到 00:00（首签闸门恒开），补签压到不可达；探针/兜底用桩短路
        self.sched.FIRST = (0, 0)
        self.sched.SECOND = (23, 59)
        self.sched.PROBE_TRY_SECONDS = 0
        self.sched.FALLBACK_TRY_SECONDS = 0
        self.sched.build_child_env = lambda env_file=None, base=None, **kw: {}
        self.sched._tick_fallback = lambda now=None, env=None: False
        self.sched._cleanup_state = lambda: None
        self.sched._full_run_done_today = lambda: True   # 首签闸门默认关（用例逐个打开）
        self.spawns = []

    def _fake_clock(self, dt):
        return mock.patch.object(self.sched.clock, "now", lambda: dt)

    def _stub_subprocess(self, popen):
        return _stub_module(Popen=popen, TimeoutExpired=_sp.TimeoutExpired)

    def _stub_time(self, stop_after):
        """sleep 桩：第 stop_after 次调用抛 _Stop（用于数 tick 数）。"""
        state = {"n": 0}

        def _sleep(_seconds):
            state["n"] += 1
            if state["n"] >= stop_after:
                raise _Stop(state["n"])

        return _stub_module(sleep=_sleep, monotonic=lambda: 0.0, time=lambda: 0.0), state


class ChildPopenOSErrorTest(_SchedBase):
    """_run_signin_child 的 Popen 判法：与兜底拉起调用点同文件同风格——接住并留痕。"""

    def _run(self):
        self.sched.subprocess = self._stub_subprocess(_boom_popen)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.sched._run_signin_child()
        return buf.getvalue()

    def test_popen_oserror_does_not_propagate(self):
        """今天（红）：OSError 直接穿出 _run_signin_child，把 main_loop 带崩。"""
        try:
            self._run()
        except OSError as e:
            self.fail(f"Popen OSError 未被接住（应留痕继续调度）: {e}")

    def test_popen_oserror_logs_trace(self):
        """接住也要出声：与兜底拉起调用点同款 print 留痕（不静默吞）。"""
        out = self._run()
        self.assertIn("签到子进程拉起失败", out)

    def test_oserror_still_marks_slot_once_not_respanwed(self):
        """失败留痕后仍走既有槽位标记：同一时段不逐秒反复 spawn（重启即双跑源），
        当日兜底由补签闸门与宿主 cron 承担。"""
        self.sched._full_run_done_today = lambda: False
        self.sched.subprocess = self._stub_subprocess(_boom_popen)
        time_stub, state = self._stub_time(2)
        self.sched.time = time_stub
        with self._fake_clock(datetime(2026, 9, 26, 6, 31, 0)), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(_Stop):
            self.sched.main_loop(sleep_seconds=1)
        self.assertEqual(state["n"], 2, "异常不得中断循环——应进入下一 tick 再收束")
        slot = os.path.join(self.tmp, "sched-slot-first-2026-09-26.json")
        self.assertTrue(os.path.exists(slot), "失败时段仍落槽位标记（防秒级反复 spawn）")


class MainLoopTickGuardTest(_SchedBase):
    """main_loop 单 tick 兜底：任意 Exception 记日志进下一 tick；信号类直通。"""

    def test_body_exception_logged_and_next_tick_normal(self):
        """活体反例主形态：第一 tick 闸门判定即抛 RuntimeError，
        循环存活；第二 tick 探针照常走到（兜底不吞后续调度）。"""
        calls = {"n": 0}

        def _now():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("模拟单 tick 内部异常")
            return datetime(2026, 9, 26, 8, 0, 0)

        self.sched.subprocess = self._stub_subprocess(
            lambda cmd, **kw: self.spawns.append(cmd) or _FakeProc())
        time_stub, state = self._stub_time(2)
        self.sched.time = time_stub
        buf = io.StringIO()
        with mock.patch.object(self.sched.clock, "now", _now), \
                contextlib.redirect_stdout(buf), self.assertRaises(_Stop):
            self.sched.main_loop(sleep_seconds=1)
        out = buf.getvalue()
        self.assertIn("tick 异常", out)
        self.assertEqual(state["n"], 2, "异常 tick 后必须继续下一 tick")

    def test_keyboard_interrupt_not_swallowed(self):
        """KeyboardInterrupt 从 tick 体内抛出也必须穿出（监督停机路径依赖信号语义）。"""
        self.sched.time = _stub_module(sleep=lambda s: None,
                                       monotonic=lambda: 0.0, time=lambda: 0.0)
        with mock.patch.object(self.sched.clock, "now",
                               _raise_keyboard_interrupt), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(KeyboardInterrupt):
            self.sched.main_loop(sleep_seconds=1)

    def test_system_exit_not_swallowed(self):
        self.sched.time = _stub_module(sleep=lambda s: None,
                                       monotonic=lambda: 0.0, time=lambda: 0.0)
        with mock.patch.object(self.sched.clock, "now",
                               _raise_system_exit), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            self.sched.main_loop(sleep_seconds=1)


def _raise_keyboard_interrupt():
    raise KeyboardInterrupt


def _raise_system_exit():
    raise SystemExit(3)


class HeartbeatTest(_SchedBase):
    """心跳落盘 + 探活出口：sched 死/僵必须让容器健康态变红，而不是只有 web 绿。"""

    def _hb_path(self):
        return os.path.join(self.tmp, "sched-heartbeat.json")

    def test_touch_writes_heartbeat(self):
        self.sched._touch_heartbeat(self.tmp)
        with open(self._hb_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["pid"], os.getpid())

    def test_main_loop_tick_writes_heartbeat(self):
        time_stub, _ = self._stub_time(1)
        self.sched.time = time_stub
        self.sched.subprocess = self._stub_subprocess(lambda cmd, **kw: _FakeProc())
        with self._fake_clock(datetime(2026, 9, 26, 8, 0, 0)), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(_Stop):
            self.sched.main_loop(sleep_seconds=1)
        self.assertTrue(os.path.exists(self._hb_path()),
                        "main_loop 至少一次 tick 必须落下活体心跳")

    def test_heartbeat_write_failure_does_not_break_tick(self):
        """心跳是旁路观测件：写不进去（目录被占成文件）只留痕，不掀翻本 tick。"""
        blocked = os.path.join(self.tmp, "blocked")
        with open(blocked, "w", encoding="utf-8") as fh:
            fh.write("x")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.sched._touch_heartbeat(blocked)   # 父路径是普通文件 ⇒ mkdir 必失败
        self.assertNotIn("Traceback", buf.getvalue())

    def test_healthcheck_main_fresh_ok_stale_and_missing_fail(self):
        """新鲜 0 / mtime 归零（远超容忍窗）1 / 文件缺失 1——探活判据 fail-closed。"""
        hb = self._hb_path()
        self.sched._touch_heartbeat(self.tmp)
        self.assertEqual(self.sched.healthcheck_main(self.tmp), 0)
        os.utime(hb, (0, 0))   # mtime 归零 ⇒ 远超容忍窗
        self.assertEqual(self.sched.healthcheck_main(self.tmp), 1)
        os.remove(hb)
        self.assertEqual(self.sched.healthcheck_main(self.tmp), 1)

    def test_check_health_cli_exit_code(self):
        """--check-health 是可执行出口：compose 的 healthcheck 直接调它（真跑子进程）。"""
        env = dict(os.environ, YIBAN_STATE_DIR=self.tmp)
        self.sched._touch_heartbeat(self.tmp)
        r = subprocess_run_health(env)
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        os.remove(self._hb_path())
        r2 = subprocess_run_health(env)
        self.assertEqual(r2.returncode, 1, "心跳缺失必须判非健康")


def subprocess_run_health(env):
    return _sp.run([sys.executable, _SCHED_PATH, "--check-health"],
                   capture_output=True, env=env, timeout=25)


class SupervisordLivenessTest(unittest.TestCase):
    """配置级断言：sched 崩溃重启不得按默认 startsecs=1/startretries=3 三连进 FATAL。

    证据形态：本机无 supervisord 容器环境，钉住配置值本身；语义（到点重拉、
    连续失败上限）待生产演练。
    """

    def _sched_section(self):
        import configparser
        cp = configparser.ConfigParser(strict=True)
        cp.read(SUPERVISORD, encoding="utf-8")
        return cp["program:sched"]

    def test_sched_has_explicit_startsecs_and_startretries(self):
        sec = self._sched_section()
        self.assertIn("startsecs", sec,
                      "缺省 startsecs=1：启动后第一 tick 崩溃永远赶不上'已启动'判定")
        self.assertIn("startretries", sec,
                      "缺省 startretries=3：秒级三连崩进 FATAL，全天无人再拉起")
        self.assertGreaterEqual(int(sec["startretries"]), 10)

    def test_sched_autorestart_still_on(self):
        self.assertEqual(self._sched_section().get("autorestart"), "true")


class ComposeHealthcheckSchedTest(unittest.TestCase):
    """healthcheck 必须同时探 web 与 sched——只 curl web 时 sched 全死容器仍'健康'。"""

    def _compose_text(self):
        with open(COMPOSE, encoding="utf-8") as fh:
            return fh.read()

    def test_healthcheck_covers_sched_and_web(self):
        text = self._compose_text()
        hb = re.search(r"healthcheck:\s*\n(?:.*\n)*?\s*start_period", text)
        self.assertIsNotNone(hb, "compose 里找不到 healthcheck 块")
        block = hb.group(0)
        self.assertIn("17892", block, "web 探活不得丢（nginx depends_on 依赖它）")
        self.assertIn("--check-health", block,
                      "sched 侧必须探调度器自己的活体出口（心跳），而非只有 web")

    def test_web_only_curl_goes_red(self):
        """活体反例：旧形态（只 curl web）用同一条判据必须红。"""
        legacy = ("healthcheck:\n"
                  "      test: [\"CMD\", \"python3\", \"-c\",\n"
                  "             \"import urllib.request;urllib.request.urlopen"
                  "('http://127.0.0.1:17892/login', timeout=3)\"]\n"
                  "      interval: 30s\n      start_period: 20s\n")
        hb = re.search(r"healthcheck:\s*\n(?:.*\n)*?\s*start_period", legacy)
        self.assertIn("17892", hb.group(0))
        self.assertNotIn("--check-health", hb.group(0))   # 判据点名：旧块缺 sched 探活


# ---------------------------------------------------------------------------
# run.sh LOCK_DIR：fail-closed（真起 bash）
# ---------------------------------------------------------------------------

_STUB_SIGNIN = '''# -*- coding: utf-8 -*-
"""极简桩：记一轮调用，按 round_exit 退出（默认 0=成功收尾）。"""
import os
from datetime import datetime, timedelta

state = os.environ.get("YIBAN_STATE_DIR", ".")
today = (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d")
if "--second-run-check" in __import__("sys").argv:
    raise SystemExit(0)   # 库内事实已了结：单轮收场，用例只关心锁与到点
try:
    with open(os.path.join(state, "round_exit"), encoding="utf-8") as f:
        code = int(f.read().strip() or 0)
except (OSError, ValueError):
    code = 0
with open(os.path.join(state, "rounds.log"), "a", encoding="utf-8") as f:
    f.write("round\\n")
raise SystemExit(code)
'''


class RunShLockDirTest(unittest.TestCase):
    """锁目录建不成 ⇒ 拒绝运行 rc=1；静默回退 /tmp 与 08-21 加固注释自相矛盾。"""

    @classmethod
    def setUpClass(cls):
        if not BASH:
            raise unittest.SkipTest("无 bash 环境")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lockdir-")
        self.app = os.path.join(self.tmp, "app")
        self.state = os.path.join(self.tmp, "state")
        for d in (os.path.join(self.app, "scripts"),
                  os.path.join(self.app, ".venv", "bin"), self.state):
            os.makedirs(d, exist_ok=True)
        with io.open(os.path.join(self.app, "scripts", "signin.py"), "w",
                     encoding="utf-8") as f:
            f.write(_STUB_SIGNIN)
        with io.open(os.path.join(self.app, ".env"), "w", encoding="utf-8") as f:
            f.write("YIBAN_SIGN_END=07:50\n")
        py = os.path.join(self.app, ".venv", "bin", "python3")
        with io.open(py, "w", encoding="utf-8", newline="\n") as f:
            f.write('#!/bin/sh\nexec "%s" "$@"\n' % sys.executable.replace("\\", "/"))
        os.chmod(py, os.stat(py).st_mode | stat.S_IEXEC)
        # flock/timeout shim：本机（Windows）无原生命令，锁与超时非本用例主题
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin, exist_ok=True)
        for name, body in (("flock", "#!/bin/sh\nexit 0\n"),
                           ("timeout", "#!/bin/sh\nshift\nexec \"$@\"\n")):
            p = os.path.join(self.bin, name)
            with io.open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(body)
            os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, lock_dir):
        env = dict(os.environ)
        env.update({
            "PATH": self.bin + os.pathsep + env.get("PATH", ""),
            "YIBAN_APP_DIR": self.app,
            "YIBAN_STATE_DIR": self.state,
            "YIBAN_LOG_FILE": os.path.join(self.state, "sign.log"),
            "YIBAN_SECOND_RUN_TIME": "00:01",
            "YIBAN_LOCK_DIR": lock_dir,
        })
        env.pop("YIBAN_SECOND_RUN", None)
        return _sp.run([BASH, RUN_SH], capture_output=True, env=env, cwd=self.app)

    def _rounds(self):
        p = os.path.join(self.state, "rounds.log")
        return p if os.path.exists(p) else None

    def test_uncreatable_lock_dir_refuses_run_rc1(self):
        """活体反例：锁目录路径的父级是普通文件 ⇒ mkdir 必失败 ⇒ rc=1 且一轮不跑。

        今天（红）：静默回退 /tmp/yiban-sign-<uid> 继续跑，rc=0。
        """
        blocker = os.path.join(self.tmp, "blocker")
        with io.open(blocker, "w", encoding="utf-8") as f:
            f.write("占位：让它成为普通文件，任何子路径 mkdir 都失败")
        r = self._run(os.path.join(blocker, "lock"))
        err = r.stderr.decode("utf-8", "replace")
        self.assertEqual(r.returncode, 1,
                         "锁目录建不成必须拒绝运行 rc=1（实际 rc=%d, stderr=%s）"
                         % (r.returncode, err))
        self.assertIn("拒绝运行", err)
        self.assertIsNone(self._rounds(), "拒绝运行时不得拉起签到轮")

    def test_explicit_lock_dir_created_and_run_proceeds(self):
        """显式 YIBAN_LOCK_DIR 指向不存在的可建路径：自建自负责，正常运行不误伤。"""
        lock = os.path.join(self.tmp, "fresh-lock")
        r = self._run(lock)
        self.assertTrue(os.path.isdir(lock), "自建路径应可用（rc=%d, err=%s）"
                        % (r.returncode, r.stderr.decode("utf-8", "replace")))
        self.assertTrue(os.path.exists(os.path.join(lock, "sign.lock")))
        self.assertIsNotNone(self._rounds(), "锁目录正常时应走到签到轮")

    def test_no_tmp_fallback_wording_left_on_failure(self):
        """判据不许留后门：失败路径的文案里不得再有'回退 /tmp'。"""
        blocker = os.path.join(self.tmp, "blocker2")
        with io.open(blocker, "w", encoding="utf-8") as f:
            f.write("x")
        r = self._run(os.path.join(blocker, "lock"))
        combined = (r.stderr.decode("utf-8", "replace")
                    + r.stdout.decode("utf-8", "replace"))
        self.assertNotIn("回退 /tmp", combined)


if __name__ == "__main__":
    unittest.main(verbosity=2)
