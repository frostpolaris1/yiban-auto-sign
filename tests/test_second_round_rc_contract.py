# -*- coding: utf-8 -*-
"""补签链的 rc 契约与封存前置：监督进程不得把异常退出归 0，标记单点写，封存看库内事实。

标签：B · 调度：领取/队列/执行体
覆盖：`run_worker_supervisor` 的退出码汇总（负数/信号与未知非零码一律算真失败 1，
   4>10>1>3>2>0 优先序不变）、sched-run 全量收尾标记的**单点写**（监督进程写、
   子执行体不写；被信号杀或迁移拒启时不写）、`has_undone_accounts_today` 的并集判据
   （池里有未了结 ⇒ 未了结；池干净但状态文件仍有未了结行 ⇒ 同样未了结）、
   `run.sh` 收尾标记封存闸门（`--second-run-check` 判 0 才封存；判 10 不封存留给
   兜底轮；判定不可得时不封存 + 双声音 + 本轮成功则退出码升 1）、
   `YIBAN_FALLBACK_ENABLE` 置 1 而无进程时 run.sh 启动即告警并拉起、
   **真信号杀演练**（真 run.sh + 真监督进程 + 真子进程 + 真 SIGKILL 的整链收场）。
对应实现：yiban/engine/workers.py（汇总与单点标记）、yiban/engine/runner.py（子执行体
   不写标记）、yiban/engine/state_io.py（`has_undone_accounts_today`）、run.sh
   （封存前置与兜底接线）。
关键断言：补签判定与 SUCCESS 必须消费同一份真实 rc——被 SIGKILL 的子执行体若被归 0，
   run.sh 就写 SUCCESS、封存收尾标记，"进程消失后的当日恢复腿"被同一条坏 rc 弹开。
   封存收尾标记的前置是**库内事实**（--second-run-check 走领取池/状态文件判"确实无
   未了结"），不是子执行体的自报退出码：整批没领、部分没轮到，都不能被当成"做完了"。
   真信号杀演练是这条不变量的主证据：真实进程链上的 -9 必须汇成 1、状态文件必须无
   SUCCESS、收尾标记必须不封存、补签判定必须仍答"需要补跑"。
依赖：监督用例打桩 Popen/sleep/load_accounts（不发请求）；run.sh 用例需 bash
   （skipIf），$PY 用 wrapper 转发当前解释器，scripts/signin.py 用桩件读控制文件；
   演练用例另需 POSIX 信号（无 SIGKILL 时整类跳过），且只把"执行体本体的程序"
   换成挂起桩（真 Popen、真 pid、真信号，零网络）。不发网络请求。
   整文件无 skip（bash/信号缺失时相应类整类跳过）。

用法（项目根目录）：
    py -m pytest tests/test_second_round_rc_contract.py -v
"""
import glob
import io
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_SH = os.path.join(BASE, "run.sh")

if BASE not in sys.path:
    sys.path.insert(0, BASE)

import signin  # noqa: E402  兼容壳：打桩口径与既有监督测试一致（转发到实现模块）

from yiban.engine import runner as runner_mod  # noqa: E402
from yiban.engine import state_io  # noqa: E402


def _acc(phone):
    return SimpleNamespace(phone=phone, user_paused=False, owner="", account_id=0)


def _today():
    return datetime.now().strftime("%Y-%m-%d")


class SupervisorRcAggregationTest(unittest.TestCase):
    """监督进程汇总：任何非零（含负数/信号/未知码）都不得折成 0。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sup-rc-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._env = {k: os.environ.get(k) for k in
                     ("YIBAN_STATE_DIR", "YIBAN_EXECUTOR_ID")}
        os.environ["YIBAN_STATE_DIR"] = self.tmp
        os.environ.pop("YIBAN_EXECUTOR_ID", None)

        def _restore():
            for k, v in self._env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(_restore)

    def _supervise(self, codes):
        """跑监督（假子进程）：`poll()` 按序返回给定退出码；返回 (rc, sched_done 次数)。"""
        pending = list(codes)

        class _FakeProc:
            def __init__(self, cmd, env=None, cwd=None):
                pass

            def poll(self):
                return pending.pop(0) if pending else None

        done_calls = []
        with mock.patch.object(signin.cli_support, "_acquire_run_lock",
                               return_value=io.StringIO()), \
                mock.patch.object(signin.accounts_mod, "load_accounts",
                                  return_value=[_acc("13800000001")]), \
                mock.patch.object(signin.subprocess, "Popen", _FakeProc), \
                mock.patch.object(signin.state_io, "mark_worker_started"), \
                mock.patch.object(signin.state_io, "mark_worker_beat"), \
                mock.patch.object(signin.state_io, "mark_worker_finished"), \
                mock.patch.object(signin.state_io, "_write_sched_done",
                                  side_effect=lambda *a, **k: done_calls.append(a)), \
                mock.patch.object(signin.time, "sleep"), \
                mock.patch.object(signin.workers, "_reap_dead_worker"):
            rc = signin.run_worker_supervisor(len(codes), ["--workers", str(len(codes))])
        return rc, len(done_calls)

    def test_signal_killed_child_aggregates_to_one(self):
        rc, done = self._supervise([-9, 0])
        self.assertEqual(rc, 1, "被 SIGKILL 的子执行体不得归 0——否则 run.sh 写 SUCCESS")
        self.assertEqual(done, 0, "子执行体被信号杀 ⇒ 本轮未收尾，不得写全量完成标记")

    def test_negative_and_unknown_codes_never_surface_as_zero(self):
        for codes in ([-15, 0], [0, -9], [124, 0], [-9, 1], [7, 0], [-9, 2]):
            with self.subTest(codes=codes):
                rc, done = self._supervise(list(codes))
                self.assertNotEqual(rc, 0, "非零退出（含负数/未知码）不得被汇总成 0")
                self.assertEqual(rc, 1, "统一折进「真失败」1（契约码值不新增）")
                self.assertEqual(done, 0, "有子执行体非正常退出 ⇒ 不得写全量完成标记")

    def test_known_priority_unchanged(self):
        for codes, want in (([4, -9], 4), ([10, -9], 10), ([1, 2], 1), ([2, 3], 3),
                            ([2, 0], 2), ([0, 0], 0), ([0, 3], 3)):
            with self.subTest(codes=codes):
                rc, _done = self._supervise(list(codes))
                self.assertEqual(rc, want)

    def test_clean_exit_writes_sched_done_once(self):
        rc, done = self._supervise([0, 1])
        self.assertEqual(rc, 1)
        self.assertEqual(done, 1, "全部子执行体正常退出 ⇒ 监督进程单点写一次全量完成标记")

    def test_migration_refusal_does_not_write_sched_done(self):
        _rc, done = self._supervise([4, 0])
        self.assertEqual(done, 0, "迁移完整性拒启 = 整轮不可信，不得宣称全量已跑完")


class SchedMarkerChildGuardTest(unittest.TestCase):
    """收尾标记单点写：子执行体（监督进程拉起的）不写，由监督进程写。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sched-mark-guard-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._env = {k: os.environ.get(k) for k in
                    ("YIBAN_STATE_DIR", "YIBAN_EXECUTOR_ID", "YIBAN_SCHEDULER_V3",
                     "YIBAN_SECOND_RUN", "YIBAN_GLOBAL_PAUSE")}
        os.environ.update({"YIBAN_STATE_DIR": self.tmp})
        for k in ("YIBAN_EXECUTOR_ID", "YIBAN_SCHEDULER_V3", "YIBAN_SECOND_RUN",
                  "YIBAN_GLOBAL_PAUSE"):
            os.environ.pop(k, None)

        def _restore():
            for k, v in self._env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(_restore)

    def _main_once(self):
        accounts = [_acc("13800000001")]
        outcome = {"13800000001": (True, "签到成功", False, "success")}
        with mock.patch.object(runner_mod.accounts_mod, "load_accounts",
                               return_value=accounts), \
                mock.patch.object(runner_mod.schedule_mod, "build_schedule",
                                  return_value={}), \
                mock.patch.object(runner_mod.schedule_mod, "day_off",
                                  return_value=None), \
                mock.patch.object(runner_mod.round_mod, "run_queue_retry",
                                  return_value=dict(outcome)), \
                mock.patch.object(runner_mod.state_io, "_load_cred_state",
                                  return_value={}), \
                mock.patch.object(runner_mod.state_io, "_save_cred_state"), \
                mock.patch.object(runner_mod.state_io, "_is_second_run",
                                  return_value=False), \
                mock.patch.object(runner_mod.state_io, "_write_sched_done") as wd, \
                mock.patch.object(runner_mod.cli_support, "_acquire_run_lock",
                                  return_value=io.StringIO()), \
                mock.patch.object(runner_mod.db, "add_sign_events_batch"), \
                mock.patch.object(runner_mod.db, "purge_expired_deleted_accounts"), \
                mock.patch.object(runner_mod.alerts, "_maybe_alert_zero_success"), \
                mock.patch.object(runner_mod.alerts, "_flush_admin_mail_summary"):
            code = runner_mod.main([])
        return code, wd.call_count

    def test_direct_full_run_still_writes_marker(self):
        code, calls = self._main_once()
        self.assertEqual(code, 0)
        self.assertEqual(calls, 1, "单执行体直跑仍是自写（它没有监督进程）")

    def test_spawned_child_does_not_write_marker(self):
        # 监督进程拉起的子执行体带 YIBAN_EXECUTOR_ID：收尾标记必须由监督进程单点写
        os.environ["YIBAN_EXECUTOR_ID"] = "worker-0@unit-test"
        code, calls = self._main_once()
        self.assertEqual(code, 0)
        self.assertEqual(calls, 0, "子执行体不得各写一份全量完成标记")


class UndoneFactsUnionTest(unittest.TestCase):
    """「无未了结」必须同时看领取池与状态文件：池里有活 ⇒ 未了结；
    池干净但状态文件还挂着未了结行（整批没领/部分没轮到）⇒ 同样未了结。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="undone-union-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _write(self, name, text):
        with io.open(os.path.join(self.tmp, name), "w", encoding="utf-8") as f:
            f.write(text)

    def test_pool_clean_but_state_file_open_counts_undone(self):
        day = "2026-09-26"
        self._write(f"sched-run-{day}.json", '{"completed": true}')
        self._write(f"sign-state-{day}.json",
                    '{"13800000001": {"status": "pending"}, '
                    '"13800000002": {"status": "success"}}')
        with mock.patch.object(state_io.db, "is_initialized", return_value=True), \
                mock.patch.object(state_io.db, "claim_stats",
                                  return_value={"claimed": 0, "done": 1, "failed": 0,
                                                "settled": 1, "open": 0, "total": 1}):
            self.assertTrue(state_io.has_undone_accounts_today(self.tmp, day),
                            "池里只领过 1 个且已了结，但另一账号从未被领取（状态仍 pending）"
                            "——不得判成「无未了结」")

    def test_pool_open_counts_undone(self):
        day = "2026-09-26"
        self._write(f"sched-run-{day}.json", '{"completed": true}')
        self._write(f"sign-state-{day}.json", '{"13800000001": {"status": "success"}}')
        with mock.patch.object(state_io.db, "is_initialized", return_value=True), \
                mock.patch.object(state_io.db, "claim_stats",
                                  return_value={"claimed": 1, "done": 0, "failed": 0,
                                                "settled": 0, "open": 1, "total": 1}):
            self.assertTrue(state_io.has_undone_accounts_today(self.tmp, day))

    def test_both_sources_clean(self):
        day = "2026-09-26"
        self._write(f"sched-run-{day}.json", '{"completed": true}')
        self._write(f"sign-state-{day}.json", '{"13800000001": {"status": "success"}}')
        with mock.patch.object(state_io.db, "is_initialized", return_value=True), \
                mock.patch.object(state_io.db, "claim_stats",
                                  return_value={"claimed": 0, "done": 1, "failed": 0,
                                                "settled": 1, "open": 0, "total": 1}):
            self.assertFalse(state_io.has_undone_accounts_today(self.tmp, day))


#: run.sh 用例的 signin 桩：--second-run-check 按 $STATE_DIR/check_exit 退出（缺省 10）；
#: 签到轮把 sched-run 写成 completed=true（桩的"轮已收尾"自证），若本轮是补签轮
#: （YIBAN_SECOND_RUN=1）则把 check_exit 翻成 0——模拟"补签跑完后库内事实干净"。
STUB_SIGNIN_RC = r'''# -*- coding: utf-8 -*-
import json, os, sys
from datetime import datetime, timedelta

state = os.environ.get("YIBAN_STATE_DIR", ".")
today = (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d")


def _read_int(name, default):
    try:
        with open(os.path.join(state, name), encoding="utf-8") as f:
            return int((f.read().strip() or default))
    except (OSError, ValueError):
        return default


if "--second-run-check" in sys.argv:
    sys.exit(_read_int("check_exit", 10))

with open(os.path.join(state, "rounds.log"), "a", encoding="utf-8") as f:
    f.write("round second_run=%s\n" % os.environ.get("YIBAN_SECOND_RUN", ""))
with open(os.path.join(state, "sched-run-%s.json" % today), "w", encoding="utf-8") as f:
    json.dump({"completed": True}, f)
if (os.environ.get("YIBAN_SECOND_RUN") == "1"
        and os.path.exists(os.path.join(state, "flip_after_second"))):
    with open(os.path.join(state, "check_exit"), "w", encoding="utf-8") as f:
        f.write("0")
sys.exit(_read_int("round_exit", 0))
'''

FAKE_FLOCK_OK = "#!/usr/bin/env bash\nexit 0\n"
#: 假 timeout：只执行命令本体（本文件不测超时击杀），并透传其退出码
FAKE_TIMEOUT_EXEC = '#!/usr/bin/env bash\nshift\nexec "$@"\n'
#: 假 PY wrapper：`-m`（兜底拉起）记一条 spawned 后 0 退；`-`（存活探测）按
#: E2E_FALLBACK_ALIVE 退出；其余转发真实解释器（跑桩件 signin.py）。
PY_WRAPPER = '''#!/bin/sh
if [ "$1" = "-m" ]; then
    echo "spawned $*" >> "$STATE_DIR/spawns.log" 2>/dev/null
    exit 0
fi
if [ "$1" = "-" ]; then
    exit ${E2E_FALLBACK_ALIVE:-1}
fi
exec "%s" "$@"
'''


#: 真信号杀演练的 `scripts/signin.py`：转交**真引擎入口**（`signin.main`），只把
#: 监督进程拉起的"执行体本体的程序"换成一个真解释器跑的挂起件——真 Popen、真 pid、
#: 真信号，只是那具本体不打校园网（真实执行体会做真实登录，演练不能拿真凭据跑）。
#: 挂起件把监督进程**真实构造的命令行**与自身 pid 落进 STATE_DIR，测试据此击杀。
DRILL_SIGNIN_SHIM = r'''# -*- coding: utf-8 -*-
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, "%s")
sys.path.insert(0, os.path.join("%s", "scripts"))
_REAL_POPEN = subprocess.Popen
from unittest import mock  # noqa: E402
from yiban.engine import workers  # noqa: E402
import signin  # noqa: E402  仓库里的真兼容壳（转发到 yiban.engine.runner）

_BODY = ("import json,os,sys,time;"
         "m=json.loads(sys.argv[1]);"
         "m['pid']=os.getpid();"
         "open(m['marker'],'w').write(json.dumps(m));"
         "time.sleep(m['hold'])")


def _stub_popen(cmd, **kwargs):
    env = kwargs.get("env") or os.environ
    ident = re.sub("[^0-9A-Za-z_.-]", "_", env.get("YIBAN_EXECUTOR_ID", "unknown"))
    hold = float(os.environ.get("YIBAN_DRILL_HOLD_SEC", "8"))
    meta = {"marker": os.path.join(env["YIBAN_STATE_DIR"], "executor-" + ident + ".json"),
            "cmd": [str(c) for c in cmd], "executor": env.get("YIBAN_EXECUTOR_ID", ""),
            "hold": hold}
    return _REAL_POPEN([sys.executable, "-c", _BODY, json.dumps(meta)], **kwargs)


_argv = sys.argv[1:]
if "--workers" in _argv:
    with mock.patch.object(workers.subprocess, "Popen", _stub_popen):
        signin.main(_argv)
else:
    signin.main(_argv)
'''


class _RunShHarness(unittest.TestCase):
    """run.sh 真跑夹具：临时 APP_DIR（含桩 signin.py）+ 假 flock/timeout + PY wrapper。"""

    @classmethod
    def setUpClass(cls):
        if shutil.which("bash") is None:
            raise unittest.SkipTest("需要 bash（Git Bash/WSL）")

    def setUp(self):
        self.bash = shutil.which("bash")
        self.tmp = tempfile.mkdtemp(prefix="runsh-rc-")
        self.app = os.path.join(self.tmp, "app")
        self.state = os.path.join(self.tmp, "state")
        self.lock = os.path.join(self.tmp, "lock")
        self.bin = os.path.join(self.tmp, "bin")
        for d in (os.path.join(self.app, "scripts"),
                  os.path.join(self.app, ".venv", "bin"),
                  self.state, self.lock, self.bin):
            os.makedirs(d, exist_ok=True)
        with io.open(os.path.join(self.app, "scripts", "signin.py"), "w",
                     encoding="utf-8", newline="\n") as f:
            f.write(STUB_SIGNIN_RC)
        with io.open(os.path.join(self.app, ".env"), "w", encoding="utf-8") as f:
            f.write("YIBAN_SIGN_END=07:50\n")
        py_wrap = os.path.join(self.app, ".venv", "bin", "python3")
        with io.open(py_wrap, "w", encoding="utf-8", newline="\n") as f:
            f.write(PY_WRAPPER % sys.executable.replace("\\", "/"))
        os.chmod(py_wrap, os.stat(py_wrap).st_mode | stat.S_IEXEC)
        for name, body in (("flock", FAKE_FLOCK_OK), ("timeout", FAKE_TIMEOUT_EXEC)):
            p = os.path.join(self.bin, name)
            with io.open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(body)
            os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _env(self, **controls):
        """装配 run.sh 的环境与 STATE_DIR 控制件（演练用例要自己起进程，故拆开）。"""
        check_exit = controls.get("check_exit")
        round_exit = controls.get("round_exit", 0)
        env = dict(os.environ)
        env.update({
            "PATH": self.bin + os.pathsep + env.get("PATH", ""),
            "YIBAN_APP_DIR": self.app,
            "YIBAN_STATE_DIR": self.state,
            "YIBAN_LOCK_DIR": self.lock,
            "YIBAN_LOG_FILE": os.path.join(self.state, "sign.log"),
            "YIBAN_SECOND_RUN_TIME": "00:01",
            "STATE_DIR": self.state,
        })
        for k in ("YIBAN_SECOND_RUN", "YIBAN_HOST_SECOND_ROUND", "YIBAN_EXECUTORS",
                  "YIBAN_WORKERS", "YIBAN_FALLBACK_ENABLE", "YIBAN_GLOBAL_PAUSE",
                  "YIBAN_EXECUTOR_ID", "YIBAN_SIGN_START"):
            env.pop(k, None)
        if "second_round" in controls:
            env["YIBAN_HOST_SECOND_ROUND"] = controls["second_round"]
        if controls.get("flip"):
            with io.open(os.path.join(self.state, "flip_after_second"), "w",
                         encoding="utf-8") as f:
                f.write("")
        if check_exit is not None:
            with io.open(os.path.join(self.state, "check_exit"), "w",
                         encoding="utf-8") as f:
                f.write(str(check_exit))
        with io.open(os.path.join(self.state, "round_exit"), "w", encoding="utf-8") as f:
            f.write(str(round_exit))
        env.update(controls.get("extra_env") or {})
        return env

    def _round_log(self):
        path = os.path.join(self.state, "sign-%s.log" % _today())
        if not os.path.exists(path):
            return ""
        with io.open(path, encoding="utf-8", errors="replace") as f:
            return f.read()

    def _run(self, **controls):
        env = self._env(**controls)
        r = subprocess.run([self.bash, RUN_SH], capture_output=True, env=env,
                           cwd=self.app, timeout=120, stdin=subprocess.DEVNULL)
        return r, self._round_log()

    def _settled(self):
        return os.path.join(self.state, "yiban-settled-%s.marker" % _today())

    def _status(self):
        return os.path.join(self.state, "sign-status-%s.txt" % _today())


class RunShSealGateTest(_RunShHarness):
    """run.sh 收尾标记封存的前置 = 库内事实（--second-run-check），不是自报 rc。"""

    def test_facts_clean_seals_marker(self):
        """判定=0（库内确实无未了结）⇒ 封存收尾标记，状态写 SUCCESS（轮 rc 0）。"""
        r, log = self._run(check_exit=0, round_exit=0)
        self.assertEqual(r.returncode, 0, log)
        self.assertTrue(os.path.exists(self._settled()), "无未了结时必须封存")
        with io.open(self._status(), encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "SUCCESS")

    def test_open_facts_block_seal_for_recovery_round(self):
        """判定=10（仍有未了结）⇒ 不得封存——07:12 的恢复腿必须还能被放行。"""
        _r, log = self._run(check_exit=10, round_exit=1)
        self.assertFalse(os.path.exists(self._settled()),
                         "仍有未了结账号时封存收尾标记 = 弹开当日恢复腿：%s" % log)

    def test_signal_failure_no_success_no_seal(self):
        """轮以真失败收场（round rc 1）⇒ 不写状态、不封存、补签判定不被闭眼。"""
        r, _log = self._run(check_exit=10, round_exit=1)
        self.assertFalse(os.path.exists(self._status()), "失败轮不得写 SUCCESS")
        self.assertEqual(r.returncode, 1)

    def test_unjudgeable_seal_blocks_and_escalates(self):
        """判定不可得（check 非 0/10）⇒ 不封存 + 双声音；本轮成功收场则退出码升 1。"""
        r, log = self._run(check_exit=7, round_exit=0, second_round="0")
        self.assertFalse(os.path.exists(self._settled()),
                         "「判不出来」不得等价于「做完了」：%s" % log)
        self.assertIn("收尾标记", log)
        self.assertEqual(r.returncode, 1, "本轮按 0 收场但封存被阻断 ⇒ 升 1 让监控听见")

    def test_second_round_then_seals_when_facts_clear(self):
        """补签轮跑完后复查库内事实：桩在补签轮把判定翻 0 ⇒ 允许封存。"""
        _r, log = self._run(check_exit=10, round_exit=0, flip=True)
        self.assertTrue(os.path.exists(self._settled()),
                        "补签轮把事实清干净后应当封存（两轮语义不得被闸门误伤）：%s" % log)


class RunShFallbackBootTest(_RunShHarness):
    """`YIBAN_FALLBACK_ENABLE` 的文档语义兑现：开关置 1 而无进程 ⇒ 启动即告警并拉起。"""

    def _spawns(self):
        p = os.path.join(self.state, "spawns.log")
        if not os.path.exists(p):
            return []
        with io.open(p, encoding="utf-8") as f:
            return [ln.strip() for ln in f if ln.strip()]

    def test_enabled_without_process_alerts_and_boots(self):
        r, log = self._run(check_exit=0, extra_env={"YIBAN_FALLBACK_ENABLE": "1",
                                                    "E2E_FALLBACK_ALIVE": "1"})
        self.assertIn("兜底", log, "开关置 1 而探测不到进程 ⇒ 启动即告警（日志留痕）")
        self.assertIn("兜底", r.stderr.decode("utf-8", "replace"),
                      "告警必须双声音（stderr 也有一份）")
        spawns = self._spawns()
        self.assertTrue(any("sign --fallback" in s for s in spawns),
                        "开关置 1 必须真的拉起兜底进程：%s" % spawns)

    def test_enabled_with_process_stays_quiet(self):
        _r, log = self._run(check_exit=0, extra_env={"YIBAN_FALLBACK_ENABLE": "1",
                                                     "E2E_FALLBACK_ALIVE": "0"})
        self.assertNotIn("拉起", log)
        self.assertEqual(self._spawns(), [], "兜底在跑（心跳新鲜）就不该重复拉起")

    def test_disabled_boots_nothing(self):
        _r, _log = self._run(check_exit=0)
        self.assertEqual(self._spawns(), [], "开关关：一行都不该拉起（静默是既有语义）")


class RunShRealSigkillDrillTest(_RunShHarness):
    """主证据：真 run.sh → 真监督进程 → 真子进程被真 SIGKILL 的整链收场。

    演练链路上唯一被替换的是**执行体本体的程序**（换成挂起件，避免真实登录外联）；
    bash、run.sh 的封存闸门、`run_worker_supervisor` 的 spawn/`poll()`/汇总/收尸、
    OS 子进程与 SIGKILL 全是真的。断言三件事（任务单 e2e 逐字绑定）：状态文件不写
    SUCCESS、收尾标记不封存、补签判定不闭眼。
    """

    #: 挂起件存活秒数：要长到测试来得及击杀，又短到整轮在十秒级收场
    HOLD_SEC = "8"

    @classmethod
    def setUpClass(cls):
        super(RunShRealSigkillDrillTest, cls).setUpClass()
        if not hasattr(signal, "SIGKILL"):
            raise unittest.SkipTest("真信号杀演练需要 POSIX SIGKILL")

    def setUp(self):
        super(RunShRealSigkillDrillTest, self).setUp()
        shim_path = os.path.join(self.app, "scripts", "signin.py")
        posix_base = BASE.replace("\\", "/")
        with io.open(shim_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(DRILL_SIGNIN_SHIM % (posix_base, posix_base))
        self.shim = shim_path

    def _drill_env(self):
        env = self._env(second_round="0")   # 只跑被杀的那一轮：收场由封存闸门与判定说话
        env.update({
            "YIBAN_WORKERS": "2",           # run.sh → `--workers 2` → 真监督进程
            "YIBAN_ACCOUNTS_KEY": "a" * 64,
            "YIBAN_DB_FILE": os.path.join(self.tmp, "drill.db"),
            "YIBAN_ACCOUNTS_JSON": json.dumps(
                [{"phone": "13800000001", "password": "drill-pw"}]),
            # 周末门与引擎同源，演练要在任意日子真跑起来（周六/周日未开关会被拦在
            # spawn 之前，那就只剩一条空链）；账号是合成号，零真实凭据
            "YIBAN_SATURDAY_SIGN": "1",
            "YIBAN_SUNDAY_SIGN": "1",
            "YIBAN_DRILL_HOLD_SEC": self.HOLD_SEC,
        })
        return env

    def _executors(self, want=2, timeout=90):
        """等监督进程真拉起的子进程把身份牌写进 STATE_DIR，返回 {执行体身份: 元数据}。"""
        deadline = time.monotonic() + timeout
        found = {}
        while time.monotonic() < deadline:
            found = {}
            for path in glob.glob(os.path.join(self.state, "executor-*.json")):
                try:
                    with io.open(path, encoding="utf-8") as f:
                        meta = json.load(f)
                except (OSError, ValueError):
                    continue      # 正在写的一半不算数
                if meta.get("cmd"):
                    found[meta.get("executor", path)] = meta
            if len(found) >= want:
                return found
            time.sleep(0.2)
        self.fail("监督进程没有真拉起 %d 个子执行体（实到 %d）: %s"
                  % (want, len(found), list(found)))

    def test_real_sigkill_no_success_no_seal_judgment_open(self):
        env = self._drill_env()
        proc = subprocess.Popen([self.bash, RUN_SH], env=env, cwd=self.app,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL)
        try:
            exes = self._executors()
            victim = [m for ident, m in exes.items() if ident.startswith("worker-0")]
            self.assertTrue(victim, "两个执行体身份应以 worker-0/worker-1 区分: %s"
                            % sorted(exes))
            # 真实命令行由监督进程构造（`-m yiban.cli sign`），演练只换了本体程序
            self.assertEqual(victim[0]["cmd"][1:4], ["-m", "yiban.cli", "sign"],
                             "子执行体必须是监督进程真实命令行拉起的进程")
            os.kill(victim[0]["pid"], signal.SIGKILL)   # 真信号，不是打桩的假退出码
            rc = proc.wait(timeout=180)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=30)
        log = self._round_log()
        self.assertEqual(rc, 1, "被 SIGKILL 的轮次必须收在真失败 1（%s）" % log)
        # 负数是**真实观测**：监督进程 poll() 到 -9，兄弟执行体正常退出 0
        self.assertIn("退出码 -9", log,
                      "汇总里没有真实的负退出码 ⇒ 子进程没被真信号杀（演练空转）：%s" % log)
        self.assertIn("退出码 0", log,
                      "兄弟执行体正常退出 0 也救不了整轮——单个 -9 就足以否掉 SUCCESS：%s" % log)
        # ① 状态文件不写 SUCCESS（失败轮按既有口径根本不写）
        self.assertFalse(os.path.exists(self._status()),
                         "被信号杀的轮次不得写 SUCCESS：%s" % log)
        if os.path.exists(self._status()):
            with io.open(self._status(), encoding="utf-8") as f:
                self.assertNotEqual(f.read().strip(), "SUCCESS")
        # ② 收尾标记不封存（封存前置=库内事实，且监督进程未写全量完成标记）
        self.assertFalse(os.path.exists(self._settled()),
                         "执行体被杀 ⇒ 收尾标记一次都不该封存，否则 07:12 的恢复腿被弹开：%s"
                         % log)
        self.assertFalse(os.path.exists(os.path.join(self.state,
                                                    "sched-run-%s.json" % _today())),
                         "有执行体非正常退出 ⇒ 全量完成标记不得落盘：%s" % log)
        self.assertIn("不封存", log, "封存闸门要留痕（判定 10 = 仍有未了结）：%s" % log)
        # ③ 补签判定不闭眼：同一份库内事实独立复查，必须仍答"需要补跑"（10）
        check = subprocess.run([sys.executable, self.shim, "--second-run-check"],
                               env=env, cwd=self.app, capture_output=True,
                               timeout=60, stdin=subprocess.DEVNULL)
        self.assertNotEqual(check.returncode, 0,
                            "补签判定被杀掉的轮次闭眼 ⇒ 当日恢复腿彻底失效：%s"
                            % check.stderr.decode("utf-8", "replace"))
        self.assertEqual(check.returncode, 10)


if __name__ == "__main__":
    unittest.main()
