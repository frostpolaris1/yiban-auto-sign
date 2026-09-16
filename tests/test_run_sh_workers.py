# -*- coding: utf-8 -*-
"""`run.sh` 的多执行体开关（`YIBAN_WORKERS`）外壳行为。

用户要求"CLI 完善要结合实际部署教程"——教程里写"`YIBAN_WORKERS=4` 就会拉起 4 个执行体"，
这句话必须真的成立，所以这里用假 `timeout`（记录被调用的完整参数）钉住三件事：

1. `YIBAN_WORKERS=4` → signin 收到 `--workers 4`；
2. `YIBAN_WORKERS` 未设 / `=1` → **不传** `--workers`（默认形态逐字不变）；
3. 非法值（`0` / `abc` / `999`）→ 不传、并在日志里留一条告警（绝不因为一笔误配置停签）。
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

FAKE_FLOCK = "#!/usr/bin/env bash\nexit 0\n"
#: 记录被调用的全部参数（`$*`）：要看的就是 "--workers 4" 有没有传下去
FAKE_TIMEOUT = '#!/usr/bin/env bash\necho "$*" >> "$FAKE_TIMEOUT_LOG"\nexit 0\n'


def _today():
    return datetime.now().strftime("%Y-%m-%d")


@unittest.skipIf(shutil.which("bash") is None, "需要 bash（Git Bash/WSL）")
class RunShWorkersTest(unittest.TestCase):
    def setUp(self):
        self.bash = shutil.which("bash")
        self.tmp = tempfile.mkdtemp(prefix="runsh-workers-")
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.state)
        self.fakebin = os.path.join(self.tmp, "fakebin")
        os.makedirs(self.fakebin)
        for name, body in (("flock", FAKE_FLOCK), ("timeout", FAKE_TIMEOUT)):
            path = os.path.join(self.fakebin, name)
            with io.open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(body)
            os.chmod(path, 0o755)
        self.calls = os.path.join(self.tmp, "timeout-calls.log")
        self.env = dict(os.environ)
        self.env.update({
            "YIBAN_STATE_DIR": self.state,
            "YIBAN_APP_DIR": self.tmp,          # run.sh 要求应用目录存在
            "FAKE_TIMEOUT_LOG": self.calls,
        })
        for k in ("YIBAN_WORKERS", "YIBAN_SECOND_RUN", "YIBAN_LOG_FILE",
                  "YIBAN_SIGN_END", "YIBAN_RUN_TIMEOUT_SEC"):
            self.env.pop(k, None)
        conv = subprocess.run(
            [self.bash, "-c", 'cygpath -u "$1" 2>/dev/null || echo "$1"', "_", self.fakebin],
            capture_output=True, text=True)
        self.env["PATH"] = (conv.stdout.strip() or self.fakebin) + os.pathsep + \
            self.env.get("PATH", "")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, workers=None):
        env = dict(self.env)
        # 每次调用用**全新的状态目录**：run.sh 有"当日已触发过"的 noclobber 标记，
        # 复用同一个目录时第二次调用会在标记处提前退出、根本走不到 signin
        # （我第一版就是这么写的，四种子用例全被这个标记吃掉）。
        state = tempfile.mkdtemp(prefix="state-", dir=self.tmp)
        env["YIBAN_STATE_DIR"] = state
        self._state = state
        if workers is not None:
            env["YIBAN_WORKERS"] = workers
        r = subprocess.run([self.bash, RUN_SH], capture_output=True, env=env,
                           cwd=self.tmp, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        with io.open(self.calls, encoding="utf-8", errors="replace") as f:
            return [ln for ln in f.read().splitlines() if ln]

    def _log(self):
        path = os.path.join(self._state, f"sign-{_today()}.log")
        if not os.path.exists(path):
            return ""
        with io.open(path, encoding="utf-8", errors="replace") as f:
            return f.read()

    def test_workers_flag_is_passed_through(self):
        calls = self._run("4")
        self.assertTrue(calls, "应调用 signin")
        self.assertIn("--workers 4", calls[0], calls)
        self.assertIn("多执行体: 4 个并行执行体", self._log())

    def test_default_and_one_do_not_pass_workers(self):
        for value in (None, "", "1"):
            with self.subTest(YIBAN_WORKERS=value):
                if os.path.exists(self.calls):
                    os.remove(self.calls)
                calls = self._run(value)
                self.assertTrue(calls)
                self.assertNotIn("--workers", calls[0], calls)

    def test_invalid_value_warns_and_falls_back(self):
        for value in ("0", "abc", "999", "-2"):
            with self.subTest(YIBAN_WORKERS=value):
                if os.path.exists(self.calls):
                    os.remove(self.calls)
                calls = self._run(value)
                self.assertTrue(calls)
                self.assertNotIn("--workers", calls[0], calls)
                self.assertIn("YIBAN_WORKERS", self._log(), "非法值应留告警痕迹")


if __name__ == "__main__":
    unittest.main(verbosity=2)
