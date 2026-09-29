# -*- coding: utf-8 -*-
"""backup.sh 契约：--require-encrypt 与异机副本解耦。

标签：J · 运维：部署/备份/发布
覆盖：backup.sh 的 --require-encrypt 前置校验、加密失败的 fail-closed 清场、脚本语法。
对应实现：`scripts/backup.sh`（纯 shell，Python 侧无对应实现）。
关键断言：① 前置校验不再检查 REMOTE_BACKUP 是否为空（单机部署也能强制本地密文归档）；
    ② 无 gpg/age、或配了 gpg 而命令缺失时仍须拒绝执行；③ 加密失败必须 `rm -f` 本轮归档
    并 `exit 1`——不得静默回退明文。
依赖：读 .sh 文本（显式 utf-8）；仅 `test_bash_syntax_ok` 需要 bash，无 bash 时该条
    skipTest，其余用例不跑子进程；不需 docker、不连网络。

文本级断言而不跑子进程：脚本含中文输出，Windows 子进程按 GBK 解码会把成功误报成失败
（见 tests/test_scheduler_gate.py 的 BackupDockerScriptTest）；`bash -n` 只解析不执行。
未配 REMOTE_BACKUP 的原实现一份备份都不生成，该标志在单机部署下不可用。
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
        end = self.src.index("# BACKUP_PLAINTEXT=1 与 --require-encrypt 互斥检查前移到打包之前")  #切到此行为止：该行之后是另一条契约（明文互斥检查），本文件不钉它
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
        idx = self.src.index("--require-encrypt 指定但加密失败")  #锚在失败提示语上：改文案就报红是刻意的——这条错误必须让运维看得见
        block = self.src[idx:idx + 400]  #只看其后 400 字：再往后是无关分支，那里的 rm -f 不算本轮清场
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
