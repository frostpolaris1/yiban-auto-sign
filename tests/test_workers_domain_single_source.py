# -*- coding: utf-8 -*-
"""census P1-3 回归：`YIBAN_WORKERS` 的合法域与越界语义只许有一套。

标签：B · 调度：领取/队列/执行体
覆盖：①同一个越界值（65 / 999 / 0 / -2 / 非整数）经宿主 `run.sh` 与经 Python 侧
   `egress.legacy_worker_count` ⇒ 两侧得到的执行体数必须相同（都是单执行体）；
   ②域内值（2 / 4 / 64）两侧都必须照实生效（数值逐字相同）；
   ③bash 侧的上下限确实来自 Python 的打印：只改 Python 那一侧打印的数，bash 的判定
      跟着变（钉死"两份数"这条形状）；
   ④取不到 Python/常量 ⇒ 响亮告警 + 按单执行体，绝不静默换成另一套缺省；
   ⑤越界不再是静默钳位：Python 侧必须出声（WARNING），点名键与范围；
   ⑥一枚事实一个名字：旧名 `WORKER_COUNT_MAX` 已并入 `WORKERS_MAX`，
      `WORKERS_MAX == SLOT_MAX + 1`；Web 写入校验与单槽位写接口引用同一对常量。
对应实现：`run.sh`（`workers_domain` / `_run_signin_round`）、
   `yiban/egress.py`（`WORKERS_MIN` / `WORKERS_MAX` / `legacy_worker_count`）、
   `web/routes/settings_api.py`（两处读取改走同一对常量/同一个函数）。
关键断言：①② 是"同一份配置两种结果"的直接反证，断的是**绝对值**——只断"两侧相等"
   的话，两份数一起变也会绿。③ 是"bash 向 Python 取值"的直接证据。④ 钉住取不到时的
   方向（少开：每多一个执行体就多一路真实登录）与声音。⑤ 是工单点名的第二处分叉。
   ⑥ 用源码守卫钉住边界：再抄一份数字（bash 或 web）即红。
依赖：bash（假 flock/timeout + 可控的 `$APP_DIR/.venv/bin/python3`，沿用
   `tests/test_run_sh_workers.py` 的做法）；Python 侧用例不依赖 bash。无网络、不连库。
用法（项目根目录）：bash scripts/dev-verify.sh --target "tests/test_workers_domain_single_source.py"
"""
import glob
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_SH = os.path.join(BASE, "run.sh")
SETTINGS_API = os.path.join(BASE, "web", "routes", "settings_api.py")

from yiban import egress  # noqa: E402

FAKE_FLOCK = "#!/usr/bin/env bash\nexit 0\n"
#: 记录被调用的全部参数：要看的就是 `--workers N` 有没有传下去
FAKE_TIMEOUT = '#!/usr/bin/env bash\necho "$*" >> "$FAKE_TIMEOUT_LOG"\nexit 0\n'

#: 假解释器：命中 WORKERS_MIN 那条 `-c` 查询时按 $1 给的话术回答，其余转调真解释器。
#: 生产环境 APP_DIR 就是仓库根，`-c` 里的 sys.path.insert 足以导入 yiban.egress；
#: 本测试把 APP_DIR 指到临时目录，故用 PYTHONPATH 补上仓库根——取的仍是真常量。
PY_STUB = """#!/bin/sh
case "$*" in
*WORKERS_MIN*)
{workers_body}
    ;;
esac
PYTHONPATH="{base}:$PYTHONPATH" exec "{real_py}" "$@"
"""
#: 默认：不拦 WORKERS 查询，交给真解释器（= 用 yiban/egress 里的真常量）
PY_STUB_REAL = ": "
#: 假 Python 侧合法域（证明 bash 跟着打印走）
PY_STUB_NARROW = "printf '1 3\\n'\n    exit 0"
#: 假 Python 侧调用失败（模拟解释器报错）
PY_STUB_ERROR = "exit 7"
#: 假 Python 侧打印不成样子（模拟 traceback/多余输出）
PY_STUB_GARBAGE = "printf 'ab cd ef\\n'\n    exit 0"


def _write_exec(path, text):
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.chmod(path, os.stat(path).st_mode | 0o755)


@unittest.skipIf(shutil.which("bash") is None, "需要 bash（Git Bash/WSL）")
class WorkersDomainRunShTest(unittest.TestCase):
    """①②③④：宿主 `run.sh` 的判定必须跟着 Python 那一侧的数走。"""

    def setUp(self):
        self.bash = shutil.which("bash")
        self.tmp = tempfile.mkdtemp(prefix="workers-domain-")
        self.state_root = os.path.join(self.tmp, "state")
        os.makedirs(self.state_root)
        self.fakebin = os.path.join(self.tmp, "fakebin")
        os.makedirs(self.fakebin)
        _write_exec(os.path.join(self.fakebin, "flock"), FAKE_FLOCK)
        _write_exec(os.path.join(self.fakebin, "timeout"), FAKE_TIMEOUT)
        self.calls_log = os.path.join(self.tmp, "timeout-calls.log")
        # run.sh 在封存当日收尾标记前要问"还需要补跑吗"：给一个恒答 0 的 signin 桩
        scripts = os.path.join(self.tmp, "scripts")
        os.makedirs(scripts, exist_ok=True)
        _write_exec(os.path.join(scripts, "signin.py"), "import sys\nsys.exit(0)\n")
        self.env = dict(os.environ)
        self.env.update({
            "YIBAN_APP_DIR": self.tmp,
            "FAKE_TIMEOUT_LOG": self.calls_log,
        })
        for key in ("YIBAN_WORKERS", "YIBAN_SECOND_RUN", "YIBAN_LOG_FILE",
                    "YIBAN_SIGN_END", "YIBAN_RUN_TIMEOUT_SEC", "YIBAN_GLOBAL_PAUSE",
                    "YIBAN_FALLBACK_ENABLE", "YIBAN_STATE_DIR", "PYTHONPATH"):
            self.env.pop(key, None)
        conv = subprocess.run([self.bash, "-c", 'cygpath -u "$1" 2>/dev/null || echo "$1"',
                               "_", self.fakebin], capture_output=True, text=True)
        self.env["PATH"] = ((conv.stdout.strip() or self.fakebin)
                            + os.pathsep + self.env.get("PATH", ""))
        self._stub_python(PY_STUB_REAL)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _stub_python(self, workers_body):
        """写 `$APP_DIR/.venv/bin/python3`：run.sh 优先用它（见 run.sh 的 PY 选择段）。"""
        venv_bin = os.path.join(self.tmp, ".venv", "bin")
        os.makedirs(venv_bin, exist_ok=True)
        _write_exec(os.path.join(venv_bin, "python3"), PY_STUB.format(
            workers_body=workers_body, base=BASE.replace("\\", "/"),
            real_py=sys.executable.replace("\\", "/")))

    def _run(self, workers, workers_body=PY_STUB_REAL):
        """跑一轮 run.sh，返回（timeout 收到的参数、当日日志、退出码）。"""
        self._stub_python(workers_body)
        state = tempfile.mkdtemp(prefix="state-", dir=self.state_root)
        env = dict(self.env)
        env["YIBAN_STATE_DIR"] = state
        if workers is not None:
            env["YIBAN_WORKERS"] = workers
        if os.path.exists(self.calls_log):
            os.remove(self.calls_log)
        r = subprocess.run([self.bash, RUN_SH], capture_output=True, env=env,
                           cwd=self.tmp, timeout=180)
        # 日志名按业务日（北京钟）落，宿主时区不同名会差一天 ⇒ 按通配读，别猜日期
        chunks = []
        for path in sorted(glob.glob(os.path.join(state, "sign-*.log"))):
            with io.open(path, encoding="utf-8", errors="replace") as fh:
                chunks.append(fh.read())
        calls = []
        if os.path.exists(self.calls_log):
            with io.open(self.calls_log, encoding="utf-8", errors="replace") as fh:
                calls = [ln for ln in fh.read().splitlines() if ln]
        return calls, "\n".join(chunks), r

    @staticmethod
    def _bash_count(calls):
        """run.sh 实际交给 signin 的执行体数：没传 `--workers` = 单执行体。"""
        assert calls, "run.sh 必须真的执行了签到轮（否则计数断言无意义）"
        m = re.search(r"--workers\s+(\d+)", calls[0])
        return int(m.group(1)) if m else 1

    def test_out_of_range_agrees_with_python_side(self):
        """①：越界值两侧都得同一个执行体数（= 1），且两侧都出声。"""
        for value in ("65", "999", "0", "-2", "many", "1_0"):
            with self.subTest(YIBAN_WORKERS=value):
                calls, log, r = self._run(value)
                self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
                bash_count = self._bash_count(calls)
                with self.assertLogs("yiban", level="WARNING") as cap:
                    py_count = egress.legacy_worker_count(
                        {egress.ENV_WORKER_COUNT: value})
                self.assertEqual(bash_count, py_count,
                                 f"run.sh 报 {value} ⇒ {bash_count}，egress ⇒ {py_count}：同一份配置")
                self.assertEqual(py_count, egress.WORKERS_MIN,
                                 "越界必须回退单执行体（少开：多开看不见）")
                self.assertIn(egress.ENV_WORKER_COUNT, "".join(cap.output),
                              "Python 侧越界必须点名键")
                self.assertIn("警告", log, "宿主侧越界必须留在当日日志")

    def test_in_range_agrees_with_python_side(self):
        """②：域内值两侧都照实生效，数值逐字相同（含上限边界）。"""
        for value in ("2", "4", str(egress.WORKERS_MAX)):
            with self.subTest(YIBAN_WORKERS=value):
                calls, log, r = self._run(value)
                self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
                self.assertEqual(self._bash_count(calls), int(value),
                                 f"合法值 {value} 必须真的拉起 {value} 个执行体")
                self.assertEqual(egress.legacy_worker_count(
                    {egress.ENV_WORKER_COUNT: value}), int(value))
                self.assertNotIn("警告: YIBAN_WORKERS", log, "合法值不得打警告")

    def test_bash_follows_the_domain_python_prints(self):
        """③：只改 Python 那一侧打印的合法域 ⇒ bash 的判定跟着变（bash 没有第二份数）。"""
        self._stub_python(PY_STUB_NARROW)
        calls, log, r = self._run("3", workers_body=PY_STUB_NARROW)
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        self.assertEqual(self._bash_count(calls), 3,
                         "Python 说上限是 3 ⇒ 3 必须被接受")
        calls, log, r = self._run("4", workers_body=PY_STUB_NARROW)
        self.assertEqual(self._bash_count(calls), 1,
                         "Python 说上限是 3 ⇒ 4 必须被拒（bash 不许还认 64）")
        self.assertIn("1~3", log, f"告警必须回显取到的域，实际日志: {log}")

    def test_domain_unavailable_is_loud_and_single(self):
        """④：取不到常量 ⇒ 响亮告警 + 单执行体；不许静默换成另一套缺省数。"""
        for name, body in (("解释器报错", PY_STUB_ERROR), ("打印不成样子", PY_STUB_GARBAGE)):
            with self.subTest(形状=name):
                calls, log, r = self._run("4", workers_body=body)
                self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
                self.assertEqual(self._bash_count(calls), 1,
                                 f"{name}时必须按单执行体，而不是照一份缺省数放行")
                self.assertIn("警告", log, f"{name}必须响亮：{log}")
                self.assertIn("YIBAN_WORKERS", log, "告警要点名是哪枚配置取不到")
                self.assertIn("合法域", log, "告警要点名取不到的是合法域")


class WorkersDomainPythonSideTest(unittest.TestCase):
    """⑤⑥：Python 侧的合法域与越界语义。"""

    def test_out_of_range_warns_instead_of_clamping(self):
        """越界 ⇒ 告警 + 回退下限（工单点名的"静默钳 1~64"是第二处分叉）。"""
        for value in ("65", "999", "0", "-2"):
            with self.subTest(YIBAN_WORKERS=value):
                with self.assertLogs("yiban", level="WARNING") as cap:
                    got = egress.legacy_worker_count({egress.ENV_WORKER_COUNT: value})
                self.assertEqual(got, egress.WORKERS_MIN,
                                 f"{value} 不得被钳成 {egress.WORKERS_MAX}")
                text = "".join(cap.output)
                self.assertIn(egress.ENV_WORKER_COUNT, text)
                self.assertIn(str(egress.WORKERS_MAX), text, "告警要把范围说清楚")

    def test_non_integer_warns(self):
        for value in ("many", "1_0", "+5", " 4.5 ", "٣"):
            with self.subTest(YIBAN_WORKERS=value):
                with self.assertLogs("yiban", level="WARNING") as cap:
                    got = egress.legacy_worker_count({egress.ENV_WORKER_COUNT: value})
                self.assertEqual(got, egress.WORKERS_MIN)
                self.assertIn("非法", "".join(cap.output))

    def test_unset_is_quiet_single(self):
        """未设/空白是默认形态 ⇒ 静默单执行体（不该天天红）。"""
        for value in (None, "", "   "):
            with self.subTest(YIBAN_WORKERS=value):
                env = {} if value is None else {egress.ENV_WORKER_COUNT: value}
                with self.assertNoLogs("yiban", level="WARNING"):
                    self.assertEqual(egress.legacy_worker_count(env), egress.WORKERS_MIN)

    def test_one_name_one_fact(self):
        self.assertEqual(egress.WORKERS_MIN, 1)
        self.assertEqual(egress.WORKERS_MAX, egress.SLOT_MAX + 1)
        self.assertEqual(egress.WORKERS_MAX, 64)
        self.assertFalse(hasattr(egress, "WORKER_COUNT_MAX"),
                         "旧名必须一并收掉：一枚事实一个名字")


class WorkersDomainNoSecondCopyTest(unittest.TestCase):
    """⑥：边界守卫——合法域只许有一个定义点（bash/web 不得再抄一份数字）。"""

    def _read(self, path):
        with io.open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_run_sh_has_no_hard_coded_bounds(self):
        text = self._read(RUN_SH)
        self.assertIn("WORKERS_MIN", text, "run.sh 必须向 Python 取上下限")
        self.assertIn("WORKERS_MAX", text)
        self.assertIsNone(re.search(r"-ge\s+2\s*\]", text),
                          "run.sh 里不得再留 `-ge 2` 这类自己抄的下限")
        self.assertIsNone(re.search(r"-le\s+64\s*\]", text),
                          "run.sh 里不得再留 `-le 64` 这类自己抄的上限")

    def test_web_paths_use_the_same_constants(self):
        text = self._read(SETTINGS_API)
        self.assertIsNone(re.search(r"1\s*<=\s*workers\s*<=\s*64", text),
                          "Web 写入校验不得自己抄一份上下限")
        self.assertIn("yb_egress.WORKERS_MIN", text)
        self.assertIn("yb_egress.WORKERS_MAX", text)
        self.assertIsNone(re.search(r"load_env_int\([^)]*YIBAN_WORKERS", text),
                          "单槽位写接口的执行体数必须走 egress.legacy_worker_count")


if __name__ == "__main__":
    unittest.main(verbosity=2)
