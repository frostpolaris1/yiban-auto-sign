# -*- coding: utf-8 -*-
"""backup.sh 契约（2026-09-08，静态源码核验）。

--require-encrypt 与异机副本（REMOTE_BACKUP）解耦：只强制"本轮归档必须
加密成功"（gpg/age 可用即满足）；未配 REMOTE_BACKUP 的单机部署不再被拒绝
执行（原实现一份备份都不生成，该标志在单机部署下不可用）；加密失败的
fail-closed 清场语义保持原样。

文本级断言（显式 utf-8 读脚本）：脚本含中文输出，Windows 子进程 GBK 解码会
误报失败（见 test_scheduler_gate.py 的 BackupDockerScriptTest），故不跑
子进程；bash 可用时另以 `bash -n` 做语法核验（只解析不执行）。
"""
import os
import shutil
import subprocess
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class BackupRequireEncryptTest(unittest.TestCase):
    """--require-encrypt 前置校验：只看加密可用性，不再绑定 REMOTE_BACKUP。"""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(BASE, "scripts", "backup.sh"), encoding="utf-8") as f:
            cls.src = f.read()

    def _precheck_block(self):
        """提取 --require-encrypt 前置校验分支（从其注释到 BACKUP_PLAINTEXT 互斥检查）。"""
        start = self.src.index("--require-encrypt 前置校验")
        end = self.src.index("# BACKUP_PLAINTEXT=1 与 --require-encrypt 互斥检查前移到打包之前")
        return self.src[start:end]

    def test_precheck_no_longer_requires_remote_backup(self):
        # 解耦点：REMOTE_BACKUP 为空不再阻止执行，单机部署也能强制本地密文归档
        self.assertNotIn("REMOTE_BACKUP 未配置，无法加密", self.src,
                         "原强耦合分支仍存在：未配 REMOTE_BACKUP 时拒绝生成任何备份")
        self.assertNotIn('-z "$REMOTE_BACKUP"', self._precheck_block(),
                         "前置校验不得再检查 REMOTE_BACKUP 是否为空")

    def test_precheck_still_rejects_unavailable_encryption(self):
        block = self._precheck_block()
        self.assertIn("无可用加密方式", block, "无 gpg/age 可用时仍须拒绝执行")
        self.assertIn("未找到 gpg 命令", block, "配置了 gpg 但命令缺失仍须拒绝执行")

    def test_fail_closed_on_encrypt_failure_kept(self):
        # 加密失败即清场：删除本轮归档并不再继续（不得静默回退明文）
        idx = self.src.index("--require-encrypt 指定但加密失败")
        block = self.src[idx:idx + 400]
        self.assertIn("rm -f", block, "加密失败必须删除明文归档")
        self.assertIn("exit 1", block, "加密失败必须以非零码退出")

    def test_bash_syntax_ok(self):
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("无 bash 环境")
        r = subprocess.run([bash, "-n", os.path.join(BASE, "scripts", "backup.sh")],
                           capture_output=True)
        self.assertEqual(r.returncode, 0,
                         f"backup.sh 语法错误：{r.stderr.decode('utf-8', errors='replace')}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
