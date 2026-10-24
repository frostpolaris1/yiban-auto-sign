# -*- coding: utf-8 -*-
"""门禁不许"扫空当绿"（R3-010）。

标签：J · 运维：部署/备份/发布 · A · 判据：覆盖显性化

缺陷形状：输入为空时门遍历零个对象、零处违规，于是打印"全部可回指"并 exit 0。
"零个对象全部合规"是空真，不是证据（AGENTS §30 跳过 ≠ 绿）。

判据（跑真脚本）：
  ① cron 来源门：cron 目录里没有表 ⇒ 非 0；
  ② cron 来源门：manifest 只有注释（0 条） ⇒ 非 0；
  ③ cron 来源门：表存在但没有任何命令行（0 个 token） ⇒ 非 0；
  ④ deps-lock 门：锁定清单为空 / 只有注释（0 个锁定包） ⇒ 非 0。
同形状扫全的结论见 REPORT（scripts/check-*.sh 逐个评估：其余三道已有下限）。
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRON_GATE = os.path.join(BASE, "scripts", "check-cron-provenance.sh")
DEPS_GATE = os.path.join(BASE, "scripts", "check-deps-lock.sh")
BASH = shutil.which("bash")

MANIFEST_OK = "0700\tscripts/backup.sh\t/usr/local/sbin/yiban-backup.sh\n"
CRON_OK = (
    "SHELL=/bin/sh\n"
    "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n"
    "MAILTO=\"\"\n"
    "0 2 * * * root /usr/local/sbin/yiban-backup.sh >> /var/log/yiban/backup.log 2>&1\n"
)


def _write(path, text):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


@unittest.skipIf(BASH is None, "需要 bash（Git Bash/WSL）")
class CronGateEmptyInputTest(unittest.TestCase):
    """cron 来源门：0 张表 / 0 条 manifest / 0 个 token 都必须非 0。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cron-empty-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cron = os.path.join(self.tmp, "cron.d")
        self.manifest = os.path.join(self.tmp, "manifest.tsv")
        os.makedirs(self.cron)

    def _check(self):
        return subprocess.run(
            [BASH, CRON_GATE, "--cron-dir", self.cron, "--manifest", self.manifest],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120)

    def test_empty_cron_dir_is_red(self):
        _write(self.manifest, MANIFEST_OK)
        r = self._check()
        self.assertNotEqual(r.returncode, 0,
                            "cron 目录里一张表都没有仍报绿（扫空当绿）：\n"
                            + (r.stdout or "") + (r.stderr or ""))

    def test_comment_only_manifest_is_red(self):
        # cron 行只引用系统件：这一条的缺陷只能是"清单 0 条"，不是幽灵路径。
        _write(os.path.join(self.cron, "yiban-sign"),
               "0 2 * * * root /usr/bin/true >> /var/log/yiban/x.log 2>&1\n")
        _write(self.manifest, "# 只有注释的清单\n")
        r = self._check()
        self.assertNotEqual(r.returncode, 0,
                            "manifest 0 条仍报绿：\n" + (r.stdout or "") + (r.stderr or ""))

    def test_table_without_command_line_is_red(self):
        _write(self.manifest, MANIFEST_OK)
        _write(os.path.join(self.cron, "yiban-sign"),
               "SHELL=/bin/sh\nPATH=/usr/bin:/bin\nMAILTO=\"\"\n# 只有注释，没有命令行\n")
        r = self._check()
        self.assertNotEqual(r.returncode, 0,
                            "0 个路径 token 仍报绿（门空转）：\n"
                            + (r.stdout or "") + (r.stderr or ""))

    def test_control_valid_input_is_green(self):
        """对照组：正常输入必须绿，否则上面三条的"红"没有意义。"""
        _write(self.manifest, MANIFEST_OK)
        _write(os.path.join(self.cron, "yiban-sign"), CRON_OK)
        r = self._check()
        self.assertEqual(r.returncode, 0, (r.stdout or "") + (r.stderr or ""))


@unittest.skipIf(BASH is None, "需要 bash（Git Bash/WSL）")
class DepsLockEmptyInputTest(unittest.TestCase):
    """deps-lock 门：0 个锁定包必须非 0（"一致"对空集是空真）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="deps-empty-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.venv = os.path.join(self.tmp, "venv")
        os.makedirs(os.path.join(self.venv, "bin"))
        # 只需可执行的解释器：0 个锁定包时门在读取包元数据之前就该拒绝。
        os.symlink(sys.executable, os.path.join(self.venv, "bin", "python3"))
        self.lock = os.path.join(self.tmp, "requirements.lock")

    def _check(self):
        return subprocess.run(
            [BASH, DEPS_GATE, "--venv", self.venv, "--lock", self.lock],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120)

    def test_empty_lock_is_red(self):
        _write(self.lock, "")
        r = self._check()
        self.assertNotEqual(r.returncode, 0,
                            "0 个锁定包仍报绿（扫空当绿）：\n"
                            + (r.stdout or "") + (r.stderr or ""))

    def test_comment_only_lock_is_red(self):
        _write(self.lock, "# 只有注释\n# 没有 name==version\n")
        r = self._check()
        self.assertNotEqual(r.returncode, 0,
                            "只有注释的清单仍报绿：\n"
                            + (r.stdout or "") + (r.stderr or ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
