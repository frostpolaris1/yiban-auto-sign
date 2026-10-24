# -*- coding: utf-8 -*-
"""兜底脚本从 cron 的 `$HOME` 起也能进到业务入口（R3-016）。

标签：J · 运维：部署/备份/发布

模拟的真实用户动作：部署者按 README 把 `scripts/yiban-fallback.sh` 写进
`/etc/cron.d`，由 cron 在 `$HOME` 下、以极简环境启动它。用户可见结果 = 兜底执行体
真的起来（窗口内接手没签完的账号）。

cron 的 cwd 是 `$HOME`。脚本最后以 `python -m yiban.entry.cli` 启动执行体，
而 `-m` 从**当前目录**解析包；不先 `cd` 到检出，cron 下必然
`ModuleNotFoundError: No module named 'yiban'`（实拍日志即此）。

判据不是一个退出码，而是"业务入口真的被调用"：桩入口写留痕文件，
再用极简环境（只给 PATH/HOME，不继承 PYTHONPATH）复现 cron。
"""
import io
import os
import shutil
import subprocess
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FALLBACK_SH = os.path.join(BASE, "scripts", "yiban-fallback.sh")
HOST_PATHS = os.path.join(BASE, "scripts", "lib", "host_paths.sh")
BASH = shutil.which("bash")

#: cron 的环境：极简 PATH，不带任何继承的 PYTHONPATH。
MIN_ENV = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"}


def _write(path, text):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _read(path):
    with io.open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


@unittest.skipIf(BASH is None, "需要 bash（Git Bash/WSL）")
class FallbackCronWorkingDirectoryTest(unittest.TestCase):
    """R3-016：cron 在 `$HOME` 起兜底脚本，必须能进到业务入口。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-fallback-cron-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.app = os.path.join(self.tmp, "app")
        os.makedirs(os.path.join(self.app, "scripts", "lib"))
        os.makedirs(os.path.join(self.app, "data"))
        shutil.copyfile(FALLBACK_SH, os.path.join(self.app, "scripts", "yiban-fallback.sh"))
        shutil.copyfile(HOST_PATHS, os.path.join(self.app, "scripts", "lib", "host_paths.sh"))
        # 极简 yiban 包：入口只留痕。它证明 `python -m yiban.entry.cli` 真的找到了包。
        for rel in ("yiban/__init__.py", "yiban/core/__init__.py", "yiban/entry/__init__.py"):
            _write(os.path.join(self.app, rel), "")
        _write(os.path.join(self.app, "yiban", "core", "clock.py"),
               "def today():\n    return '2026-01-02'\n")
        self.marker = os.path.join(self.app, "entry-invoked.txt")
        _write(os.path.join(self.app, "yiban", "entry", "cli.py"),
               "import sys\n"
               "open(%r, 'w', encoding='utf-8').write(' '.join(sys.argv[1:]))\n"
               % self.marker)
        self.state = os.path.join(self.tmp, "state")
        _write(os.path.join(self.app, "data", ".env"),
               "YIBAN_FALLBACK_ENABLE=1\nYIBAN_STATE_DIR=%s\n"
               % self.state.replace("\\", "/"))

    def test_cron_home_cwd_reaches_the_business_entry(self):
        env = dict(MIN_ENV)
        env["HOME"] = self.tmp
        r = subprocess.run(
            [BASH, os.path.join(self.app, "scripts", "yiban-fallback.sh")],
            cwd=self.tmp, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120)
        logs = []
        for root, _dirs, files in os.walk(self.state):
            for name in files:
                logs.append(_read(os.path.join(root, name)))
        out = "\n".join([r.stdout or "", r.stderr or "", *logs])
        self.assertTrue(os.path.exists(self.marker),
                        "cron 的 $HOME cwd 下没进到业务入口（兜底永不运行）：\n" + out)
        self.assertIn("--fallback", _read(self.marker),
                      "业务入口收到的参数不是兜底模式：\n" + out)
        self.assertNotIn("ModuleNotFoundError", out,
                         "从 $HOME 起脚本时包不在 sys.path（缺 cd 到检出）：\n" + out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
