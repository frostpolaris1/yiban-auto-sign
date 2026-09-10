# -*- coding: utf-8 -*-
"""批次20 B3 回归：宿主「进程内补签轮」判定与执行（2026-09-10）。

背景：补签原本由第二个独立 cron（07:12）承担，但 flock 由首签脚本持有至退出，
而首签要 sleep 到最晚自选时间片（生产实测 07:25）才结束 —— 07:12 的 cron 每天撞锁
`exit 0`，补签轮从未真正执行（生产 sign-*.log 12/12 天实证；容器形态因在同进程内
等首轮子进程返回后再判 SECOND，反而正确）。

用户裁决方案一：宿主改为与容器同语义——首轮结束后在**仍持锁的同一进程内**再判定
一次是否需要补跑。判定口径收敛到 `signin.need_second_run()`。

覆盖：
1. `need_second_run()` 判定矩阵（含 fail-safe：无记录/损坏按"需要补跑"）；
2. `signin.py --second-run-check` 的退出码契约（10=需要 / 0=不需要）；
3. **真实执行 run.sh**：需要补跑 → 跑两轮且第二轮带 `YIBAN_SECOND_RUN=1`；
   不需要 → 只跑一轮；`YIBAN_HOST_SECOND_ROUND=0` → 只跑一轮；
   本轮本身是补签轮（`YIBAN_SECOND_RUN=1`）→ 不再评估第三轮；
   收尾标记落地后再次调用 run.sh → 直接跳过（不再产生任何一轮）。

用法（项目根目录）：
    py -m pytest tests/test_second_run_0910.py -q
（run.sh 集成用例需要 bash；无 bash 时自动跳过。）
"""
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import signin  # noqa: E402

TODAY = datetime.now().strftime("%Y-%m-%d")
_MISSING = object()  # 哨兵：区分"不创建该文件"
STUB_SIGNIN = '''# -*- coding: utf-8 -*-
"""测试桩：替代真实 scripts/signin.py。

- 带 --second-run-check：按 $STATE_DIR/check_exit 文件的值退出（缺省 10=需要补跑）
- 否则：把本轮调用追加到 $STATE_DIR/rounds.log（记录 YIBAN_SECOND_RUN），
  并把 $STATE_DIR/sched-run-<today>.json 写成 completed=true，
  再按 $STATE_DIR/round_exit 的值退出（缺省 0）
"""
import json, os, sys
from datetime import datetime

state = os.environ.get("YIBAN_STATE_DIR", ".")
today = datetime.now().strftime("%Y-%m-%d")


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

    def test_no_sched_marker_needs_second(self):
        """当日全量未收尾（标记缺失）→ 需要补跑。"""
        self.assertTrue(signin.need_second_run(self.tmp, TODAY))

    def test_done_and_all_success_no_second(self):
        """已收尾 + 全部 success → 不需要补跑。"""
        self._write(f"sched-run-{TODAY}.json", {"completed": True})
        self._write(f"sign-state-{TODAY}.json", {
            "13800000001": {"status": "success"},
            "13800000002": {"status": "already"},
            "13800000003": {"status": "no_task"},
        })
        self.assertFalse(signin.need_second_run(self.tmp, TODAY))

    def test_done_with_failed_needs_second(self):
        """已收尾但有 failed 账号 → 需要补跑（补签轮的核心价值）。"""
        self._write(f"sched-run-{TODAY}.json", {"completed": True})
        self._write(f"sign-state-{TODAY}.json", {
            "13800000001": {"status": "success"},
            "13800000002": {"status": "failed"},
        })
        self.assertTrue(signin.need_second_run(self.tmp, TODAY))

    def test_done_with_window_skip_needs_second(self):
        """已收尾但有 skipped_window（学校窗口晚于本地配置）→ 需要补跑。"""
        self._write(f"sched-run-{TODAY}.json", {"completed": True})
        self._write(f"sign-state-{TODAY}.json", {"13800000001": {"status": "skipped_window"}})
        self.assertTrue(signin.need_second_run(self.tmp, TODAY))

    def test_completed_false_needs_second(self):
        """标记存在但 completed=false（首轮被 timeout 击杀）→ 需要补跑。"""
        self._write(f"sched-run-{TODAY}.json", {"completed": False})
        self._write(f"sign-state-{TODAY}.json", {"13800000001": {"status": "success"}})
        self.assertTrue(signin.need_second_run(self.tmp, TODAY))

    def test_missing_state_file_fails_safe(self):
        """标记已写但状态文件缺失 → 按"未了结"处理（宁多跑一轮，不漏签）。"""
        self._write(f"sched-run-{TODAY}.json", {"completed": True})
        self.assertTrue(signin.need_second_run(self.tmp, TODAY))

    def test_corrupted_state_file_fails_safe(self):
        """状态文件损坏 → 同样按"需要补跑"。"""
        self._write(f"sched-run-{TODAY}.json", {"completed": True})
        self._write(f"sign-state-{TODAY}.json", "{ 不是合法 JSON")
        self.assertTrue(signin.need_second_run(self.tmp, TODAY))

    def test_empty_state_dict_fails_safe(self):
        """状态文件是空对象 → 按"需要补跑"。"""
        self._write(f"sched-run-{TODAY}.json", {"completed": True})
        self._write(f"sign-state-{TODAY}.json", {})
        self.assertTrue(signin.need_second_run(self.tmp, TODAY))

    def test_cli_exit_code_contract(self):
        """CLI 契约：需要补跑 → 10；不需要 → 0（run.sh 依赖该退出码）。"""
        env = dict(os.environ)
        env["YIBAN_STATE_DIR"] = self.tmp
        cmd = [sys.executable, os.path.join(BASE, "scripts", "signin.py"), "--second-run-check"]

        r = subprocess.run(cmd, capture_output=True, env=env, cwd=BASE)
        self.assertEqual(r.returncode, signin.SECOND_RUN_CHECK_NEED,
                         "无标记时应返回 10（需要补跑）")
        self.assertEqual(signin.SECOND_RUN_CHECK_NEED, 10, "退出码契约不得改动")

        self._write(f"sched-run-{TODAY}.json", {"completed": True})
        self._write(f"sign-state-{TODAY}.json", {"13800000001": {"status": "success"}})
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
        with io.open(os.path.join(self.app, ".env"), "w", encoding="utf-8") as f:
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
        """
        self._write_ctl("check_exit", 10)
        self._write_ctl("round_exit", 0)
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        self.assertEqual(self._rounds(), ["round second_run="], "SUCCESS 后不应补跑")

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
        """收尾标记落地后，再次调用 run.sh 直接跳过（07:12 兜底 cron 不再多跑）。"""
        self._write_ctl("check_exit", 10)
        self._write_ctl("round_exit", 2)
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
            with io.open(os.path.join(self.tmp, f"sched-run-{TODAY}.json"), "w",
                         encoding="utf-8") as f:
                f.write(sched_payload if isinstance(sched_payload, str)
                        else json.dumps(sched_payload))
        if state_payload is not _MISSING:
            with io.open(os.path.join(self.tmp, f"sign-state-{TODAY}.json"), "w",
                         encoding="utf-8") as f:
                f.write(state_payload if isinstance(state_payload, str)
                        else json.dumps(state_payload))

    def _assert_agree(self, sched_payload, state_payload, expected):
        self._setup(sched_payload, state_payload)
        host = signin.need_second_run(self.tmp, TODAY)
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


if __name__ == "__main__":
    unittest.main()
