# -*- coding: utf-8 -*-
"""批次20 B1 回归：生产备份异机副本拉取脚本的契约（2026-09-10）。

脚本本身是运维工具（在运维工作站上跑，对生产只读）。此处只做**不联网**的契约
核验，避免测试依赖 ssh 可达性：
- bash 语法可用；
- 参数契约：未知参数 → 2，-h → 0 且打印用法；
- 静态不变量：只读契约、禁止在生产机自拉、密文-only、三处哈希校验、新鲜度自检、
  "补齐式增量"设计（这是"每天开机但时间不定"能成立的前提）。

用法（项目根目录）：
    py -m pytest tests/test_pull_prod_backup_0910.py -q
"""
import io
import os
import shutil
import subprocess
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(BASE, "scripts", "pull-prod-backup.sh")


class PullProdBackupContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bash = shutil.which("bash")
        with io.open(SCRIPT, encoding="utf-8") as f:
            cls.src = f.read()

    def _bash(self, *args):
        return subprocess.run([self.bash, SCRIPT, *args], capture_output=True)

    def test_bash_syntax_ok(self):
        if not self.bash:
            self.skipTest("无 bash 环境")
        r = subprocess.run([self.bash, "-n", SCRIPT], capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))

    def test_unknown_arg_exits_2(self):
        if not self.bash:
            self.skipTest("无 bash 环境")
        r = self._bash("--nope")
        self.assertEqual(r.returncode, 2, "未知参数应返回 2（用法错误）")

    def test_help_prints_usage(self):
        if not self.bash:
            self.skipTest("无 bash 环境")
        r = self._bash("-h")
        self.assertEqual(r.returncode, 0)
        self.assertIn("用法", r.stdout.decode("utf-8", "replace"))

    def test_readonly_contract_documented(self):
        """对生产只读是硬契约，必须写在脚本里（远端只允许 ls / sha256sum + scp 下载）。"""
        self.assertIn("对生产完全只读", self.src)
        self.assertIn("ls", self.src)
        self.assertIn("sha256sum", self.src)

    def test_refuses_to_run_on_production_host(self):
        """防呆：别在生产机自己身上"做异机副本"。"""
        self.assertIn("/opt/yiban-auto-sign", self.src)
        self.assertIn("异机副本必须在另一台机器上拉取", self.src)

    def test_only_pulls_ciphertext(self):
        """只搬密文（.tar.gz.gpg），绝不把明文 tar.gz 拉回工作站。"""
        self.assertIn("yiban-*.tar.gz.gpg", self.src)
        self.assertNotIn("yiban-*.tar.gz\"", self.src.replace("yiban-*.tar.gz.gpg", ""))

    def test_three_way_hash_check(self):
        """远端哈希 / 下载后复算 / 随行清单三处一致才入库，失败改名 .bad 保留证据。"""
        self.assertIn("_remote_sha256", self.src)
        self.assertIn(".bad", self.src)

    def test_freshness_selfcheck_present(self):
        """新鲜度自检：本地落后于远端、或远端本身停更 → 非 0 退出（供定时任务告警）。"""
        self.assertIn("freshness_check", self.src)
        self.assertIn("STALE_DAYS", self.src)
        self.assertIn("生产的每日备份链路疑似中断", self.src)
        self.assertIn("--status", self.src)

    def test_incremental_backfill_documented(self):
        """补齐式增量是"开机时间不定"能成立的前提，必须写进脚本说明。"""
        self.assertIn("增量", self.src)
        self.assertIn("补齐", self.src)

    def test_default_mirror_dir_is_neutral(self):
        """默认镜像目录不得写入某个运维者的个人目录布局（本仓库公开）。

        2026-09-10 复检：脚本首版把 Windows 默认写成 D:/code/backups/...（本机路径），
        属个人环境信息泄漏到公开仓库；现改为跨平台中性的 $HOME/yiban-prod-mirror，
        需要换盘由 LOCAL_MIRROR_DIR 覆盖。此断言防止回退。
        """
        self.assertIn('LOCAL_MIRROR_DIR="${LOCAL_MIRROR_DIR:-$HOME/yiban-prod-mirror}"',
                      self.src)
        self.assertNotIn("D:/code/", self.src)
        self.assertNotIn("C:/Users/", self.src)


if __name__ == "__main__":
    unittest.main()
