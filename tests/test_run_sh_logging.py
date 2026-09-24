# -*- coding: utf-8 -*-
"""`run.sh` 的日志与退出码契约（B6）：**任何**退出路径都留一行带退出码的日志。

原先只有正常收尾那一行，flock 跳过、当日已签到成功跳过、当日已收尾跳过、timeout 击杀
全都不留痕——日志里看不出"这一轮到底跑过没有、为什么没签"。本文件钉两件事：

1. **留痕**：四条跳过/收场路径都有 `=== run.sh 退出，退出码: N ===`，且行首带触发来源
   前缀（排程 / 手工，按有无控制终端自动判，`YIBAN_TRIGGER` 可覆盖）；
2. **退出码逐字不变**：0/1/2/3/10 照原样透传——加日志不得顺手改了契约。

子进程里跑（无非是"读日志 + 看退出码"这种进程级契约）；`flock` / `timeout` 用假件，
不真的签到、不碰网络。
"""
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_SH = os.path.join(BASE, "run.sh")

#: 假装拿到了锁（run.sh 继续往下走）
FAKE_FLOCK_OK = "#!/usr/bin/env bash\nexit 0\n"
#: 假装锁被别的进程占着（run.sh 走"已有签到进程在运行，本次跳过"）
FAKE_FLOCK_BUSY = "#!/usr/bin/env bash\nexit 1\n"
#: 记录参数并回一个可控退出码：退出码契约与超时击杀路径都由此驱动
FAKE_TIMEOUT = r"""#!/usr/bin/env bash
{
  echo "ARGS=$*"
} >> "$FAKE_TIMEOUT_LOG"
exit ${FAKE_EXIT_CODE:-0}
"""


def _today():
    return datetime.now().strftime("%Y-%m-%d")


@unittest.skipIf(shutil.which("bash") is None, "需要 bash（Git Bash/WSL）")
class RunShExitTrailTest(unittest.TestCase):
    def setUp(self):
        self.bash = shutil.which("bash")
        self.tmp = tempfile.mkdtemp(prefix="runsh-trail-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.app_dir = os.path.join(self.tmp, "app")
        os.makedirs(self.app_dir)
        self.fakebin = os.path.join(self.tmp, "fakebin")
        os.makedirs(self.fakebin)
        self.calls = os.path.join(self.tmp, "timeout-calls.log")
        self._install_fakes(flock_body=FAKE_FLOCK_OK)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("YIBAN_")}
        self.env.update({
            "YIBAN_APP_DIR": self.app_dir,
            "FAKE_TIMEOUT_LOG": self.calls,
            # 补签轮默认关闭：本文件钉的是"退出时留痕"，多跑一轮只会让退出码来源变糊
            "YIBAN_HOST_SECOND_ROUND": "0",
        })
        conv = subprocess.run(
            [self.bash, "-c", 'cygpath -u "$1" 2>/dev/null || echo "$1"', "_", self.fakebin],
            capture_output=True, text=True)
        self.env["PATH"] = (conv.stdout.strip() or self.fakebin) + os.pathsep + \
            self.env.get("PATH", "")

    def _install_fakes(self, *, flock_body, timeout_body=FAKE_TIMEOUT):
        for name, body in (("flock", flock_body), ("timeout", timeout_body)):
            path = os.path.join(self.fakebin, name)
            with io.open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(body)
            os.chmod(path, 0o755)

    def _run(self, state, extra_env=None):
        """跑一次 run.sh；返回 (退出码, 日志文本)。状态目录由调用方准备。"""
        env = dict(self.env)
        env["YIBAN_STATE_DIR"] = state
        env["YIBAN_LOG_FILE"] = os.path.join(state, "sign.log")
        env.update(extra_env or {})
        r = subprocess.run([self.bash, RUN_SH], capture_output=True, env=env,
                           cwd=self.tmp, timeout=120)
        log_path = os.path.join(state, f"sign-{_today()}.log")
        text = ""
        if os.path.exists(log_path):
            with io.open(log_path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        return r.returncode, text

    @staticmethod
    def _exit_line(code, tag="排程"):
        return f"[{tag}] === run.sh 退出，退出码: {code} ==="

    def _assert_exit_line(self, text, code, tag="排程"):
        self.assertIn(self._exit_line(code, tag), text,
                      f"退出路径必须留痕（退出码 {code}）：\n{text}")


class ExitTrailTest(RunShExitTrailTest):
    """四条跳过/收场路径都必须留痕（原先一条都没有）。"""

    def test_flock_busy_path_logs_exit_zero(self):
        self._install_fakes(flock_body=FAKE_FLOCK_BUSY)
        state = tempfile.mkdtemp(prefix="state-", dir=self.tmp)
        code, text = self._run(state)
        self.assertEqual(code, 0, "被 flock 弹开仍是 0（既有语义）")
        self.assertIn("已有签到进程在运行，本次跳过", text)
        self._assert_exit_line(text, 0)

    def test_already_success_path_logs_exit_zero(self):
        state = tempfile.mkdtemp(prefix="state-", dir=self.tmp)
        with io.open(os.path.join(state, f"sign-status-{_today()}.txt"), "w",
                     encoding="utf-8") as f:
            f.write("SUCCESS")
        code, text = self._run(state)
        self.assertEqual(code, 0)
        self.assertIn("今天已签到成功，跳过执行", text)
        self._assert_exit_line(text, 0)

    def test_settled_marker_path_logs_exit_zero(self):
        state = tempfile.mkdtemp(prefix="state-", dir=self.tmp)
        open(os.path.join(state, f"yiban-settled-{_today()}.marker"), "w").close()
        code, text = self._run(state)
        self.assertEqual(code, 0)
        self.assertIn("今天已完成签到收尾", text)
        self._assert_exit_line(text, 0)

    def test_timeout_kill_path_logs_the_killed_code(self):
        """timeout 击杀（124）是原先最静默的一条：状态文件也不写，日志全无。"""
        state = tempfile.mkdtemp(prefix="state-", dir=self.tmp)
        code, text = self._run(state, {"FAKE_EXIT_CODE": "124"})
        self.assertEqual(code, 124, "超时击杀的退出码照原样透传给 cron")
        self._assert_exit_line(text, 124)

    def test_normal_completion_logs_exit_code(self):
        state = tempfile.mkdtemp(prefix="state-", dir=self.tmp)
        code, text = self._run(state, {"FAKE_EXIT_CODE": "0"})
        self.assertEqual(code, 0)
        self.assertIn("run.sh 开始执行", text)
        self._assert_exit_line(text, 0)


class ExitCodeContractTest(RunShExitTrailTest):
    """退出码契约 0/1/2/3/10 逐字不变——加日志不得顺手改码。"""

    def test_signin_codes_pass_through_verbatim(self):
        for code in (0, 1, 2, 3, 10):
            with self.subTest(code=code):
                state = tempfile.mkdtemp(prefix="state-", dir=self.tmp)
                got, text = self._run(state, {"FAKE_EXIT_CODE": str(code)})
                self.assertEqual(got, code, f"退出码 {code} 必须原样透传")
                self._assert_exit_line(text, code)

    def test_success_status_file_only_written_for_zero(self):
        """留痕逻辑不得改变状态文件口径：0 写 SUCCESS，2 写 SKIPPED，其余不写。"""
        for code, want in ((0, "SUCCESS"), (2, "SKIPPED"), (1, None), (3, None)):
            with self.subTest(code=code):
                state = tempfile.mkdtemp(prefix="state-", dir=self.tmp)
                self._run(state, {"FAKE_EXIT_CODE": str(code)})
                path = os.path.join(state, f"sign-status-{_today()}.txt")
                if want is None:
                    self.assertFalse(os.path.exists(path), f"退出码 {code} 不该写状态文件")
                else:
                    with io.open(path, encoding="utf-8") as f:
                        self.assertEqual(f.read().strip(), want)


class TriggerTagTest(RunShExitTrailTest):
    """触发来源前缀：无控制终端 = 排程；有 tty 或 YIBAN_TRIGGER = 手工/显式值。"""

    def test_no_tty_is_scheduled(self):
        state = tempfile.mkdtemp(prefix="state-", dir=self.tmp)
        _code, text = self._run(state)
        self.assertIn("[排程] === run.sh 开始执行 ===", text,
                      f"子进程没有控制终端，应判为排程触发：\n{text}")

    def test_explicit_override_wins(self):
        state = tempfile.mkdtemp(prefix="state-", dir=self.tmp)
        code, text = self._run(state, {"YIBAN_TRIGGER": "手工"})
        self.assertEqual(code, 0)
        self.assertIn("[手工] === run.sh 开始执行 ===", text)
        self._assert_exit_line(text, 0, tag="手工")

    def test_override_value_is_used_verbatim(self):
        """容器/CI 可注入自己的来源标签，日志里原样出现（便于跨系统对齐）。"""
        state = tempfile.mkdtemp(prefix="state-", dir=self.tmp)
        _code, text = self._run(state, {"YIBAN_TRIGGER": "容器"})
        self.assertIn("[容器] === run.sh 开始执行 ===", text)


class SourceContractTest(unittest.TestCase):
    """源码级钉子：trap 与统一日志出口必须在（删掉就静默丢退出码留痕）。"""

    @classmethod
    def setUpClass(cls):
        with io.open(RUN_SH, encoding="utf-8") as f:
            cls.src = f.read()

    def test_traps_installed(self):
        self.assertIn("trap '_on_exit $?' EXIT", self.src, "EXIT trap 是留痕的唯一保证")
        self.assertIn("trap 'exit 143' TERM", self.src, "被 TERM 杀掉也要留一行现场")
        self.assertIn("trap 'exit 130' INT", self.src)

    def test_trap_installed_before_every_early_exit(self):
        """trap 必须装在第一条早退之前：否则 flock/SUCCESS 两条跳过路径照旧无痕。"""
        trap_at = self.src.index("trap '_on_exit $?' EXIT")
        self.assertLess(trap_at, self.src.index("今天已完成签到收尾（含补签轮）"),
                        "trap 装得比早退晚，跳过路径就漏了")
        self.assertLess(trap_at, self.src.index("已有签到进程在运行"))

    def test_all_log_writes_carry_the_tag(self):
        """所有日志走 _log（唯一带前缀的出口），不再散落裸 echo "[时间戳]"。"""
        self.assertEqual(self.src.count('echo "[$(date '), 2,
                         "带时间戳的 echo 只应剩 _log 与 _on_exit 两处（其余走 _log）")
        self.assertEqual(self.src.count("[$TRIGGER_TAG]"), 2,
                         "两处时间戳日志都必须带触发来源前缀")


if __name__ == "__main__":
    unittest.main(verbosity=2)
