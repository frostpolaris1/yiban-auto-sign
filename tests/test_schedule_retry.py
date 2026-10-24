# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""重试重排与补签时刻：重试落点、槽位时刻与宿主脚本契约。

标签：B · 调度：领取/队列/执行体
覆盖：retry_hm
   的取值与容错（调用期读 env）、宿主 run.sh
   与容器的缺省同源、告警阈值随实际补签时刻、容器把实际触发点注入子进程、need_second_run
   判定矩阵、run.sh 进程内补签轮的两轮语义与逃生开关、宿主与容器判定一致。
对应实现：yiban/engine/schedule.py 与 yiban/core/window.py（retry_hm）、run.sh 与
   docker/scheduler.py（SECOND 闸门、_UNDONE_STATUSES）。
关键断言：补签时刻在长驻进程里必须按调用期读取（导入期缓存会让管理员改的值不生效）。宿主与容器两侧的补签判定必须完全一致且未了结集合是同一份引用，两侧各写一套就是漂移的开始；容器注入的实际触发点优先于
   .env 里的旧值。
依赖：run.sh 契约用例真起 bash
   子进程（无 bash 的机器会失败而非 skip，本机可跑），并读 run.sh 与
   docker/scheduler.py 原文做文本断言。不发网络请求。

功能：补签时刻与宿主/容器契约的调度回归。

2026-10-20（工单 `77s2`）：原先驱动冻结载体 `round._next_retry_at` /
`round.run_queue_retry` 的五条重试用例已删。同一批判据改由唯一生产执行体的
`next_retry_at_v3` 承担：`tests/test_executor_v3.py`（有界/可重现、随窗口收缩、
不越过窗口结束）与 `tests/test_retry_window_day_boundary.py`（跨日边界）。
归属：`yiban/engine/schedule.py` 与容器调度入口的交叉测试。
复用：`BASE` 常量。
通信：以假时钟驱动调度纯函数，并读 `run.sh` / `docker/scheduler.py` 文本做契约断言；
由 pytest 收集。
"""
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime as clock_cls
from types import SimpleNamespace
from unittest import mock

import scheduler
import signin

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(BASE, "scripts"))

from yiban.core import (  # noqa: E402
    clock,
    window,
)


class RetryHmAtoMidTest(unittest.TestCase):
    """取值与容错：与窗口起止同用 parse_hhmm（接受 `7:30` 与 `07:30`）。"""

    def test_default_matches_documented_retry_cron(self):
        self.assertEqual(window.retry_hm({}), (7, 12))
        self.assertEqual(window.retry_hm({}), window.DEFAULT_RETRY_HM)

    def test_env_override(self):
        self.assertEqual(window.retry_hm({"YIBAN_SECOND_RUN_TIME": "07:30"}), (7, 30))
        self.assertEqual(window.retry_hm({"YIBAN_SECOND_RUN_TIME": "7:30"}), (7, 30))
        self.assertEqual(window.retry_hm({"YIBAN_SECOND_RUN_TIME": " 06:05 "}), (6, 5))

    def test_invalid_falls_back(self):
        for raw in ("", "abc", "25:00", "07:60", "07", None, "07:12:30"):
            with self.subTest(raw=raw):
                self.assertEqual(window.retry_hm({"YIBAN_SECOND_RUN_TIME": raw}),
                                 window.DEFAULT_RETRY_HM)

    def test_reads_env_at_call_time(self):
        """导入期缓存会让"管理员改了补签时刻"在长驻进程里不生效。"""
        with mock.patch.dict(os.environ, {"YIBAN_SECOND_RUN_TIME": "07:40"}):
            self.assertEqual(window.retry_hm(), (7, 40))
        os.environ.pop("YIBAN_SECOND_RUN_TIME", None)
        self.assertEqual(window.retry_hm(), window.DEFAULT_RETRY_HM)


class HostRunShContractTest(unittest.TestCase):
    """宿主 run.sh 是同键的另一个取用点：缺省必须同源，且要传给子进程。"""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(BASE, "run.sh"), encoding="utf-8") as f:
            cls.src = f.read()

    def test_default_literal_matches_shared_constant(self):
        m = re.search(r'\$\{YIBAN_SECOND_RUN_TIME:-(\d{1,2}:\d{2})\}', self.src)
        self.assertIsNotNone(m, "run.sh 未按 SECOND_HHMM=\"${YIBAN_SECOND_RUN_TIME:-HH:MM}\" 取补签时刻")
        hh, mm = m.group(1).split(":")
        self.assertEqual((int(hh), int(mm)), window.DEFAULT_RETRY_HM,
                         "run.sh 默认补签时刻与 yiban.core.window.DEFAULT_RETRY_HM 漂移")

    def test_exports_key_to_child(self):
        """不导出时子进程读不到 .env 之外的实际取值（signin 会退回默认）。"""
        self.assertRegex(self.src, r'(?m)^export YIBAN_SECOND_RUN_TIME=')


class SigninAlertThresholdTest(unittest.TestCase):
    """告警抑制按**当前**补签时刻判断，且双向都按实际值走。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-retry-slot-")
        p = mock.patch.dict(os.environ, {"YIBAN_STATE_DIR": self.tmp})
        p.start()
        self.addCleanup(p.stop)
        signin._mail_summary.clear()
        self.addCleanup(signin._mail_summary.clear)
        self.accounts = [SimpleNamespace(phone="13800000001"),
                         SimpleNamespace(phone="13800000002")]
        self.results = {
            "13800000001": (True, "已签到", False, "success"),
            "13800000002": (False, "签到时段已结束", True, "skipped_window"),
        }

    def _alert(self, hm):
        """注入固定时刻跑一次告警判定（该路径只取 clock.now() 的时分）。"""
        moment = clock_cls(2026, 9, 15, hm[0], hm[1])
        with mock.patch.object(signin.clock, "now", lambda: moment):
            return signin._maybe_alert_zero_success(
                self.accounts, self.results, ok_n=1, is_second_run=False)

    def test_late_retry_slot_suppresses(self):
        """补签时刻被配到 07:30：07:20 的首签轮仍有下一轮兜底 → 不打扰。"""
        with mock.patch.dict(os.environ, {"YIBAN_SECOND_RUN_TIME": "07:30"}):
            self.assertFalse(self._alert((7, 20)))

    def test_early_retry_slot_alerts(self):
        """补签时刻被配到 07:05：07:20 已是当天最后一轮 → 必须告警（防静默漏报）。"""
        with mock.patch.dict(os.environ, {"YIBAN_SECOND_RUN_TIME": "07:05"}):
            self.assertTrue(self._alert((7, 20)))

    def test_default_slot_boundary(self):
        """缺省 07:12：正好到点即为最后一轮（06:31 关机后 07:12 才起跑的经典场景）。"""
        os.environ.pop("YIBAN_SECOND_RUN_TIME", None)
        self.assertFalse(self._alert((7, 11)))
        self.assertTrue(self._alert((7, 12)))


class ContainerInjectsRetrySlotTest(unittest.TestCase):
    """容器把实际触发点（SECOND）注入子进程环境——容器不读 YIBAN_SECOND_RUN_TIME。"""

    def test_child_env_carries_actual_slot(self):
        captured = {}

        class _Proc:
            def wait(self, timeout=None):
                return 0

        def _fake_popen(cmd, cwd=None, env=None):
            captured["env"] = dict(env or {})
            return _Proc()

        with mock.patch.object(scheduler.subprocess, "Popen", _fake_popen), \
             mock.patch.object(scheduler, "build_child_env", return_value={}):
            scheduler._run_signin_child()
        expect = f"{scheduler.SECOND[0]:02d}:{scheduler.SECOND[1]:02d}"
        self.assertEqual(captured["env"].get("YIBAN_SECOND_RUN_TIME"), expect)

    def test_injection_wins_over_stale_env_key(self):
        """.env 里若写了该键，容器也不据此调度 → 必须以容器实际值覆盖，
        否则 signin 会按一个没人用的时刻判断末轮（静默漏报方向）。"""
        captured = {}

        class _Proc:
            def wait(self, timeout=None):
                return 0

        def _fake_popen(cmd, cwd=None, env=None):
            captured["env"] = dict(env or {})
            return _Proc()

        with mock.patch.object(scheduler.subprocess, "Popen", _fake_popen), \
             mock.patch.object(scheduler, "build_child_env",
                               return_value={"YIBAN_SECOND_RUN_TIME": "23:59"}):
            scheduler._run_signin_child()
        self.assertEqual(captured["env"]["YIBAN_SECOND_RUN_TIME"],
                         f"{scheduler.SECOND[0]:02d}:{scheduler.SECOND[1]:02d}")


#: run.sh 的按日文件（sign-status-<date>.txt / sign-<date>.log）与库内当日事实查询
#: 都取业务钟（`scripts/lib/host_paths.sh::business_day` = `yiban.core.clock`，北京 +8）。
#: 旧实现用宿主 `date +%Y-%m-%d`；UTC 16:00–24:00 窗口内与业务日差一天（P0 2026-10-25）。
TODAY = clock.today()

#: signin/scheduler 的 sched-run-<date>.json / sign-state-<date>.json 同样取业务钟。
#: 两个名字保留（消费方不同），但值是同一事实源，不得再各算一份。
BIZ_TODAY = clock.today()


_MISSING = object()  # 哨兵：区分"不创建该文件"


STUB_SIGNIN = '''# -*- coding: utf-8 -*-
"""测试桩：替代真实 scripts/signin.py。

- 带 --second-run-check：按 $STATE_DIR/check_exit 文件的值退出（缺省 10=需要补跑）
- 否则：把本轮调用追加到 $STATE_DIR/rounds.log（记录 YIBAN_SECOND_RUN），
  并把 $STATE_DIR/sched-run-<today>.json 写成 completed=true，
  再按 $STATE_DIR/round_exit 的值退出（缺省 0）
- 若 $STATE_DIR/flip_after_second 存在且本轮是补签轮（YIBAN_SECOND_RUN=1）：
  把 check_exit 翻成 0——模拟"补签跑完后库内事实干净"，供封存闸门用例消费
"""
import json, os, sys
from datetime import datetime, timedelta

state = os.environ.get("YIBAN_STATE_DIR", ".")
# 模拟生产 signin 的按日留痕（yiban.core.clock 北京钟）：固定 +8，不随宿主 TZ 漂移
today = (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d")


def _read_int(name, default):
    try:
        with open(os.path.join(state, name), encoding="utf-8") as f:
            return int(f.read().strip() or default)
    except (OSError, ValueError):
        return default


if "--second-run-check" in sys.argv:
    sys.exit(_read_int("check_exit", 10))

with open(os.path.join(state, "rounds.log"), "a", encoding="utf-8") as f:
    f.write("round second_run=%s\\n" % os.environ.get("YIBAN_SECOND_RUN", ""))
with open(os.path.join(state, "sched-run-%s.json" % today), "w", encoding="utf-8") as f:
    json.dump({"completed": True}, f)
if (os.environ.get("YIBAN_SECOND_RUN") == "1"
        and os.path.exists(os.path.join(state, "flip_after_second"))):
    with open(os.path.join(state, "check_exit"), "w", encoding="utf-8") as f:
        f.write("0")
sys.exit(_read_int("round_exit", 0))
'''


class NeedSecondRunTest(unittest.TestCase):
    """判定矩阵：与容器 docker/scheduler.py 的 SECOND 闸门同语义。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="b20-2nd-")
        os.environ["YIBAN_STATE_DIR"] = self.tmp

    def tearDown(self):
        os.environ.pop("YIBAN_STATE_DIR", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, payload):
        with io.open(os.path.join(self.tmp, name), "w", encoding="utf-8") as f:
            if isinstance(payload, str):
                f.write(payload)
            else:
                json.dump(payload, f)







    def test_corrupted_state_file_fails_safe(self):
        """状态文件损坏 → 同样按"需要补跑"。"""
        self._write(f"sched-run-{BIZ_TODAY}.json", {"completed": True})
        self._write(f"sign-state-{BIZ_TODAY}.json", "{ 不是合法 JSON")
        self.assertTrue(signin.need_second_run(self.tmp, BIZ_TODAY))

    def test_empty_state_dict_fails_safe(self):
        """状态文件是空对象 → 按"需要补跑"。"""
        self._write(f"sched-run-{BIZ_TODAY}.json", {"completed": True})
        self._write(f"sign-state-{BIZ_TODAY}.json", {})
        self.assertTrue(signin.need_second_run(self.tmp, BIZ_TODAY))

    def test_cli_exit_code_contract(self):
        """CLI 契约：需要补跑 → 10；不需要 → 0（run.sh 依赖该退出码）。"""
        env = dict(os.environ)
        env["YIBAN_STATE_DIR"] = self.tmp
        cmd = [sys.executable, os.path.join(BASE, "scripts", "signin.py"), "--second-run-check"]

        r = subprocess.run(cmd, capture_output=True, env=env, cwd=BASE)
        self.assertEqual(r.returncode, signin.SECOND_RUN_CHECK_NEED,
                         "无标记时应返回 10（需要补跑）")
        self.assertEqual(signin.SECOND_RUN_CHECK_NEED, 10, "退出码契约不得改动")

        self._write(f"sched-run-{BIZ_TODAY}.json", {"completed": True})
        self._write(f"sign-state-{BIZ_TODAY}.json", {"13800000001": {"status": "success"}})
        r2 = subprocess.run(cmd, capture_output=True, env=env, cwd=BASE)
        self.assertEqual(r2.returncode, signin.SECOND_RUN_CHECK_SKIP,
                         "已收尾且无未了结账号时应返回 0")


class RunshSecondRoundTest(unittest.TestCase):
    """真实执行 run.sh：验证进程内补签轮的两轮语义。"""

    @classmethod
    def setUpClass(cls):
        cls.bash = shutil.which("bash")
        if not cls.bash:
            raise unittest.SkipTest("无 bash 环境")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="b20-runsh-")
        self.app = os.path.join(self.tmp, "app")
        self.state = os.path.join(self.tmp, "state")
        self.lock = os.path.join(self.tmp, "lock")
        self.bin = os.path.join(self.tmp, "bin")
        for d in (os.path.join(self.app, "scripts"), os.path.join(self.app, ".venv", "bin"),
                  self.state, self.lock, self.bin):
            os.makedirs(d, exist_ok=True)
        # 应用脚本（桩）+ .env
        with io.open(os.path.join(self.app, "scripts", "signin.py"), "w", encoding="utf-8") as f:
            f.write(STUB_SIGNIN)
        os.makedirs(os.path.join(self.app, "data"), exist_ok=True)
        with io.open(os.path.join(self.app, "data", ".env"), "w", encoding="utf-8") as f:
            f.write("YIBAN_SIGN_END=07:50\n")
        # .venv/bin/python3 包装器（run.sh 优先用它）
        py_wrap = os.path.join(self.app, ".venv", "bin", "python3")
        with io.open(py_wrap, "w", encoding="utf-8", newline="\n") as f:
            f.write('#!/bin/sh\nexec "%s" "$@"\n' % sys.executable.replace("\\", "/"))
        os.chmod(py_wrap, os.stat(py_wrap).st_mode | stat.S_IEXEC)
        # shim：flock 恒成功、timeout 忽略时长参数（本用例不测锁与超时）
        for name, body in (("flock", "#!/bin/sh\nexit 0\n"),
                           ("timeout", '#!/bin/sh\nshift\nexec "$@"\n')):
            p = os.path.join(self.bin, name)
            with io.open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(body)
            os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, extra_env=None):
        env = dict(os.environ)
        env.update({
            "PATH": self.bin + os.pathsep + env.get("PATH", ""),
            "YIBAN_APP_DIR": self.app,
            "YIBAN_STATE_DIR": self.state,
            "YIBAN_LOCK_DIR": self.lock,
            "YIBAN_LOG_FILE": os.path.join(self.state, "sign.log"),
            # 补签时刻设为 00:01（已过）→ 不触发等待，立即补跑
            "YIBAN_SECOND_RUN_TIME": "00:01",
        })
        env.pop("YIBAN_SECOND_RUN", None)
        env.update(extra_env or {})
        return subprocess.run([self.bash, os.path.join(BASE, "run.sh")],
                              capture_output=True, env=env, cwd=self.app)

    def _rounds(self):
        p = os.path.join(self.state, "rounds.log")
        if not os.path.exists(p):
            return []
        with io.open(p, encoding="utf-8") as f:
            return [ln.strip() for ln in f if ln.strip()]

    def _write_ctl(self, name, value):
        with io.open(os.path.join(self.state, name), "w", encoding="utf-8") as f:
            f.write(str(value))

    def test_two_rounds_when_second_needed(self):
        """需要补跑 → 两轮，且第二轮带 YIBAN_SECOND_RUN=1。

        首轮退出码取 2（存在窗口外/未了结账号）——这正是生产里会走到补签的形态：
        状态非 SUCCESS 且状态文件含未了结账号。
        """
        self._write_ctl("check_exit", 10)
        self._write_ctl("round_exit", 2)
        r = self._run()
        self.assertEqual(r.returncode, 2, r.stderr.decode("utf-8", "replace"))
        rounds = self._rounds()
        self.assertEqual(len(rounds), 2, f"应跑两轮，实际 {rounds}")
        self.assertEqual(rounds[0], "round second_run=", "首轮不得带补签标记")
        self.assertEqual(rounds[1], "round second_run=1", "第二轮必须带补签标记")
        with io.open(os.path.join(self.state, "sign-status-%s.txt" % TODAY), encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "SKIPPED", "补签轮仍是未了结 → 状态应为 SKIPPED")

    def test_single_round_when_not_needed(self):
        """不需要补跑 → 只跑一轮，且不留补签痕迹。"""
        self._write_ctl("check_exit", 0)
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        self.assertEqual(self._rounds(), ["round second_run="])

    def test_success_status_short_circuits_second_round(self):
        """首轮 SUCCESS 时不补跑（防御）：状态已是成功，再跑一轮纯属多余登录。

        注：真实路径下 SUCCESS 与"需要补跑"不会同时成立（exit 0 蕴含无未了结账号），
        本用例锁住的是"矛盾输入下取保守且不浪费"的一侧。

        同批更新：SUCCESS 现在须与库内当日事实交叉核对才采信——给一个种了
        当日 done 行的临时库，代表"真成功"（桩 timeout 直落桩 signin，不写库，
        事实由用例预置）。
        """
        self._write_ctl("check_exit", 10)
        self._write_ctl("round_exit", 0)
        db = os.path.join(self.tmp, "facts.db")
        con = sqlite3.connect(db)
        try:
            con.execute("CREATE TABLE sign_tasks (phone TEXT, day TEXT, state TEXT)")
            con.execute("INSERT INTO sign_tasks VALUES (?,?,?)",
                        ("138****0000", TODAY, "done"))
            con.commit()
        finally:
            con.close()
        r = self._run({"YIBAN_DB_FILE": db})
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        self.assertEqual(self._rounds(), ["round second_run="], "SUCCESS 后不应补跑")

    def test_forged_success_does_not_short_circuit_second_round(self):
        """进程内侧翼：桩首轮"成功"写出的 SUCCESS 若与库内当日事实相悖
        （库不存在/无行）⇒ 不采信，补签轮照跑——伪造件不得吃掉当天兜底。"""
        self._write_ctl("check_exit", 10)
        self._write_ctl("round_exit", 0)
        r = self._run({"YIBAN_DB_FILE": os.path.join(self.tmp, "no-such.db")})
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        rounds = self._rounds()
        self.assertEqual(len(rounds), 2,
                         f"无库内事实的 SUCCESS 不得短路补签轮，实际: {rounds}")
        self.assertEqual(rounds[1], "round second_run=1")
        log_path = os.path.join(self.state, "sign-%s.log" % TODAY)
        with io.open(log_path, encoding="utf-8", errors="replace") as f:
            self.assertIn("拒绝采信", f.read())

    def test_second_round_can_be_disabled(self):
        """逃生开关 YIBAN_HOST_SECOND_ROUND=0 → 即使判定需要也只跑一轮。"""
        self._write_ctl("check_exit", 10)
        self._write_ctl("round_exit", 2)
        r = self._run({"YIBAN_HOST_SECOND_ROUND": "0"})
        self.assertEqual(r.returncode, 2, r.stderr.decode("utf-8", "replace"))
        self.assertEqual(self._rounds(), ["round second_run="])

    def test_global_pause_skips_second_round(self):
        """全站暂停时补跑无意义：signin 立即 exit 2，只跑一轮（不为空跑延长持锁）。"""
        self._write_ctl("check_exit", 10)
        self._write_ctl("round_exit", 2)
        r = self._run({"YIBAN_GLOBAL_PAUSE": "1"})
        self.assertEqual(self._rounds(), ["round second_run="])
        with io.open(os.path.join(self.state, "sign-status-%s.txt" % TODAY), encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "GLOBAL_PAUSED")
        self.assertEqual(r.returncode, 2)

    def test_no_third_round_when_already_second_run(self):
        """本轮本身就是补签轮（07:12 兜底 cron 接管）→ 不再评估第三轮。"""
        self._write_ctl("check_exit", 10)
        self._write_ctl("round_exit", 2)
        r = self._run({"YIBAN_SECOND_RUN": "1"})   # 模拟当日已触发过 → 补签轮身份
        self.assertEqual(r.returncode, 2, r.stderr.decode("utf-8", "replace"))
        self.assertEqual(self._rounds(), ["round second_run=1"],
                         "只跑一轮，且该轮已带补签身份")

    def test_settled_marker_short_circuits_later_invocation(self):
        """收尾标记落地后，再次调用 run.sh 直接跳过（07:12 兜底 cron 不再多跑）。

        封存的前置是**库内事实**"确实无未了结"（`--second-run-check` 判 0），不是轮次
        自报收工：本用例用 flip_after_second 模拟"补签轮跑完后事实翻干净"，第一轮才
        封得上存——事实仍有未了结时不得封存，那条反例钉在 test_second_round_rc_contract。
        """
        self._write_ctl("check_exit", 10)
        self._write_ctl("round_exit", 2)
        self._write_ctl("flip_after_second", "")   # 补签轮跑完后 --second-run-check 答 0
        self._run()
        self.assertEqual(len(self._rounds()), 2)
        # 第二次调用（模拟 07:12 的 cron；不带 YIBAN_SECOND_RUN 也应被收尾标记挡住）
        self._run()
        self.assertEqual(len(self._rounds()), 2, "收尾后不得再产生新的签到轮次")
        self.assertTrue(
            any(n.startswith("yiban-settled-") for n in os.listdir(self.state)),
            "应生成当日收尾标记",
        )


class HostContainerAgreementTest(unittest.TestCase):
    """宿主与容器的补签判定必须完全一致（防两侧实现漂移）。

    容器 docker/scheduler.py 现在直接复用 signin 的判定函数；本用例在多种状态文件
    组合下同时调用两侧入口并比对结果——一旦有人只改一边（或重新内联实现），此处即红。
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="b20-agree-")
        cls._old_env = {k: os.environ.get(k) for k in ("YIBAN_STATE_DIR", "YIBAN_ENV_FILE")}
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_ENV_FILE"] = os.path.join(cls.tmp, ".env")
        with io.open(os.environ["YIBAN_ENV_FILE"], "w", encoding="utf-8") as f:
            f.write("")
        # scheduler 读 env 在导入期，必须先设好环境再导入
        sys.path.insert(0, os.path.join(BASE, "scripts"))
        sys.path.insert(0, os.path.join(BASE, "docker"))
        import scheduler
        cls.scheduler = scheduler
        # 全量跑时 scheduler 可能已被别的用例导入过，模块级 STATEDIR 绑定的是当时
        # 那套环境——此处显式改指本类的临时目录再比对，否则是拿两个不同目录作对比
        # （曾造成「已收尾+全成功」「已收尾+暂停」两个子用例在全量下误报不一致）。
        cls._old_statedir = scheduler.STATEDIR
        scheduler.STATEDIR = cls.tmp

    @classmethod
    def tearDownClass(cls):
        cls.scheduler.STATEDIR = cls._old_statedir
        for k, v in cls._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _setup(self, sched_payload, state_payload):
        for name in os.listdir(self.tmp):
            if name.startswith(("sched-run-", "sign-state-")):
                os.remove(os.path.join(self.tmp, name))
        if sched_payload is not _MISSING:
            with io.open(os.path.join(self.tmp, f"sched-run-{BIZ_TODAY}.json"), "w",
                         encoding="utf-8") as f:
                f.write(sched_payload if isinstance(sched_payload, str)
                        else json.dumps(sched_payload))
        if state_payload is not _MISSING:
            with io.open(os.path.join(self.tmp, f"sign-state-{BIZ_TODAY}.json"), "w",
                         encoding="utf-8") as f:
                f.write(state_payload if isinstance(state_payload, str)
                        else json.dumps(state_payload))

    def _assert_agree(self, sched_payload, state_payload, expected):
        self._setup(sched_payload, state_payload)
        host = signin.need_second_run(self.tmp, BIZ_TODAY)
        container = (not self.scheduler._full_run_done_today()) or \
            self.scheduler._has_undone_today()
        self.assertEqual(host, container,
                         "宿主判定与容器判定不一致（semantics 漂移）")
        self.assertEqual(host, expected, "判定结果与预期不符")

    def test_matrix_agrees(self):
        cases = [
            ("无任何状态文件（fail-safe 应补跑）", _MISSING, _MISSING, True),
            ("已收尾 + 全成功", {"completed": True},
             {"a": {"status": "success"}}, False),
            ("已收尾 + 一处失败", {"completed": True},
             {"a": {"status": "failed"}}, True),
            ("已收尾 + 窗口外跳过", {"completed": True},
             {"a": {"status": "skipped_window"}}, True),
            ("已收尾 + 暂停（paused 不算未了结）", {"completed": True},
             {"a": {"status": "paused"}}, False),
            ("completed=false（首轮被击杀）", {"completed": False},
             {"a": {"status": "success"}}, True),
            ("标记在但状态文件缺失", {"completed": True}, _MISSING, True),
        ]
        for label, sp, stp, exp in cases:
            with self.subTest(case=label):
                self._assert_agree(sp, stp, exp)

    def test_undone_status_set_is_single_source(self):
        """容器引用的未了结集合必须就是 signin 的那一份（不是复制品）。"""
        self.assertIs(self.scheduler._UNDONE_STATUSES, signin.UNDONE_STATUSES)
