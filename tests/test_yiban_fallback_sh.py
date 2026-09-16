# -*- coding: utf-8 -*-
"""兜底常驻执行体外壳（`scripts/yiban-fallback.sh`）的行为断言。

用户问"怎么把某个执行体设置成兜底型"——答案是一条 cron 跑这个脚本，而**开关在
`.env` 里**（网页写进去的）。cron 环境里既没有那些变量、又不能安全地 source `.env`，
所以"先读 .env 再判开关"这段样板必须由脚本自己承担，且必须钉住三件事：

1. **开关关 = 静默**：退出码 0、**不调用 CLI**、不写日志、不建状态目录。
   默认就是关，而 cron 每 5 分钟一次——任何输出都会变成周期性邮件噪声；
2. **开关开 = 真的起到 CLI**：以 `--fallback` 调用（不是 `--workers`、不是空跑）；
3. **环境变量优先于 `.env`**：cron 行里显式赋值可临时覆盖网页配置（与 run.sh 同口径）。

写法参照 `tests/test_run_sh_workers.py`：每条用例独立 state dir，用桩 PY 记录被调用
的完整参数——"脚本退出码 0"不足以证明"确实起了兜底进程"（静默退出也是 0）。
"""
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_SH = os.path.join(BASE, "scripts", "yiban-fallback.sh")

#: 桩解释器：记录被调用的全部参数（`$*`），不真的跑签到
STUB_PY = '#!/usr/bin/env bash\necho "$*" >> "$STUB_LOG"\nexit 0\n'


def _to_bash_path(path):
    """Windows 反斜杠路径 → 正斜杠（MSYS/Git Bash 下更省心，与 run.sh 测试同做法）。"""
    return path.replace("\\", "/")


@unittest.skipIf(shutil.which("bash") is None, "需要 bash（Git Bash/WSL）")
class YibanFallbackShTest(unittest.TestCase):
    def setUp(self):
        self.bash = shutil.which("bash")
        self.tmp = tempfile.mkdtemp(prefix="yiban-fallback-")
        # APP_DIR 由脚本反推（`dirname $0/..`）：把脚本复制到临时"应用目录"下，
        # 连 `.venv/bin/python3` 桩一起造好，就不必给脚本加测试专用的路径开关
        self.scripts = os.path.join(self.tmp, "scripts")
        os.makedirs(self.scripts)
        shutil.copy(SRC_SH, os.path.join(self.scripts, "yiban-fallback.sh"))
        self.venv_bin = os.path.join(self.tmp, ".venv", "bin")
        os.makedirs(self.venv_bin)
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.state)
        self.calls = os.path.join(self.tmp, "calls.log")
        self.env_file = os.path.join(self.tmp, ".env")
        self._write_env("")
        for name, body in (("python3", STUB_PY),):
            path = os.path.join(self.venv_bin, name)
            with io.open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(body)
            os.chmod(path, 0o755)
        # 逐键清空 YIBAN_*：**不能**让别的测试留在 os.environ 里的配置渗进来
        # （全量 -n 8 并发时同一 worker 里别的用例会写这些键）
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("YIBAN_")}
        self.env.update({
            "YIBAN_STATE_DIR": _to_bash_path(self.state),
            "STUB_LOG": _to_bash_path(self.calls),
        })

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_env(self, text):
        with io.open(self.env_file, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    def _run(self, **overrides):
        env = dict(self.env)
        env.update(overrides)
        r = subprocess.run(
            [self.bash, "scripts/yiban-fallback.sh"],
            capture_output=True, env=env, cwd=self.tmp, timeout=120)
        return r

    def _calls(self):
        if not os.path.exists(self.calls):
            return []
        with io.open(self.calls, encoding="utf-8", errors="replace") as f:
            return [ln for ln in f.read().splitlines() if ln]

    def _sign_log(self):
        path = os.path.join(self.state, f"sign-{datetime.now().strftime('%Y-%m-%d')}.log")
        if not os.path.exists(path):
            return None
        with io.open(path, encoding="utf-8", errors="replace") as f:
            return f.read()

    def test_disabled_is_silent_and_does_not_start_cli(self):
        """默认态（未设/0/false/写错的词）一律静默：这是**最常见**的形态。"""
        for value in ("", "0", "false", "no", "off", "maybe"):
            with self.subTest(YIBAN_FALLBACK_ENABLE=value):
                self._write_env(f"YIBAN_FALLBACK_ENABLE={value}\n")
                if os.path.exists(self.calls):
                    os.remove(self.calls)
                r = self._run()
                self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
                self.assertEqual(self._calls(), [], "开关关时不得起兜底进程")
                self.assertEqual(r.stdout, b"", "开关关时不得有标准输出（cron 邮件噪声）")
                self.assertEqual(os.listdir(self.state), [], "开关关时不得写任何日志")

    def test_enabled_in_dotenv_starts_cli_with_fallback(self):
        """开关由网页写进 .env（cron 环境里没有这个变量）——这正是本脚本存在的理由。"""
        self._write_env("YIBAN_FALLBACK_ENABLE=1\n")
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        calls = self._calls()
        self.assertEqual(len(calls), 1, f"应恰好调用一次 CLI，实际: {calls}")
        self.assertIn("sign", calls[0])
        self.assertIn("--fallback", calls[0])
        self.assertNotIn("--workers", calls[0], "兜底是独立形态，不该混进并行执行体开关")
        self.assertIsNotNone(self._sign_log(), "应把日志重定向到 sign-<业务日>.log")

    def test_truthy_variants_are_accepted(self):
        for value in ("1", "true", "TRUE", "on", "Yes"):
            with self.subTest(YIBAN_FALLBACK_ENABLE=value):
                self._write_env(f"YIBAN_FALLBACK_ENABLE={value}\n")
                if os.path.exists(self.calls):
                    os.remove(self.calls)
                r = self._run()
                self.assertEqual(r.returncode, 0)
                self.assertEqual(len(self._calls()), 1, f"{value} 应被视为真值")

    def test_environment_overrides_dotenv(self):
        """环境变量优先：cron 行里 `YIBAN_FALLBACK_ENABLE=0 …` 可临时关掉网页配置。"""
        self._write_env("YIBAN_FALLBACK_ENABLE=1\n")
        r = self._run(YIBAN_FALLBACK_ENABLE="0")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self._calls(), [], "环境变量的 0 不能被 .env 的 1 覆盖")
        # 反向：.env 说关、cron 行说开 → 以 cron 行为准
        self._write_env("YIBAN_FALLBACK_ENABLE=0\n")
        if os.path.exists(self.calls):
            os.remove(self.calls)
        r = self._run(YIBAN_FALLBACK_ENABLE="1")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(len(self._calls()), 1, "环境变量的 1 不能被 .env 的 0 覆盖")

    def test_dotenv_is_parsed_safely(self):
        """`.env` 里带 BOM/CRLF/多余空白/杂键时仍能读到开关（与 run.sh 同源解析）。"""
        self._write_env("\ufeff  YIBAN_FALLBACK_ENABLE = true \r\nOTHER_KEY=x\n")
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        self.assertEqual(len(self._calls()), 1)
        self.assertNotIn("OTHER_KEY", r.stdout.decode("utf-8", "replace"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
