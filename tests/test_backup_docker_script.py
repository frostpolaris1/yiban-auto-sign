# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""`docker/backup-docker.sh` 的活体契约：包内必须有**库本体**，且 tar 的真失败不许被吞。

标签：J · 运维：部署/备份/发布
覆盖：以真实 bash 子进程跑 `docker/backup-docker.sh`（tar 按需注入故障桩），钉两条：

    ① **库本体必须在包内**（工单 `yiban-auto-sign-4o35`）。磁盘上有库、包里没有 ⇒
       必须失败并删除产物。为什么单独立门：原有三道护栏全拦不住它——尺寸下限只有
       200 B（容器的日志与 state 远超）、"自检"只验能否解开、唯一的内容断言是审计
       锚点，而锚点住在 `state/` 里故恒成立。
    ② **tar 的非零退出不许被吞**。原实现给整条管道挂 `|| true`，连 tar 的 `rc=2`
       （读不到条目）一起抹平。干净容器实测：把 `/data/yiban.db` 置 `root:0600` 后
       tar 退 2，脚本仍打印「备份完成 / 自检：解密+解包验证通过」且 **exit 0**，包内
       只剩 `yiban.db-wal`/`-shm` 与锚点——容器调度器据此判"备份成功"。

关键断言：全部是**行为断言**——执行真脚本、读真退出码、解真产物列内容；不 grep 源码文本
（源码文本级断言正是"假绿"教训：改了字符串就同步改测试，测不到真实行为）。

**覆盖不到的**：真实权限导致的"读不到"需要非 root 或 `setpriv`；本文件用 tar 桩注入
同一失败形状（`rc=2`），真权限形态已在干净容器上实机验过（见工单 `4o35` 评论）。
依赖：bash + tar + gpg；缺任一整文件 skip（skip 是"测不到"，不是"通过"）。
"""
import glob
import os
import shutil
import subprocess
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(BASE, "docker", "backup-docker.sh")

BASH = shutil.which("bash")
TAR = shutil.which("tar")
GPG = shutil.which("gpg")
FAKE_PASSPHRASE = "e2e-not-a-real-passphrase"  # 假口令，仅对称加密回路使用

#: tar 桩：跑真 tar 把归档写到 stdout，然后**强制以 2 退出**——复刻"有条目读不到"的
#: 真实形状（GNU tar 读不到条目返回 2），但不依赖当前进程有没有 root 权限。
TAR_STUB_RC2 = """#!/usr/bin/env bash
"%s" "$@"
exit 2
"""

#: tar 桩：跑真 tar 但把库排除掉、**以 0 退出**——复刻"产物少了库却不报错"的形状，
#: 专钉①（只做 rc 检查的修法抓不到这一路）。
TAR_STUB_NO_DB = """#!/usr/bin/env bash
"%s" --exclude '*/yiban.db' "$@"
exit 0
"""


@unittest.skipIf(BASH is None or TAR is None or GPG is None,
                 "需要 bash + tar + gpg（缺任一即为「测不到」，不得算通过）")
class ContainerBackupScriptTest(unittest.TestCase):
    """真脚本 + 真 tar/gpg；故障桩只用来注入 tar 的退出码与产物内容。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="backup-docker-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(os.path.join(self.data, "logs"))
        self.backup_dir = os.path.join(self.tmp, "backups")
        os.makedirs(self.backup_dir)
        self.gnupg = os.path.join(self.tmp, "gnupg")
        os.makedirs(self.gnupg, mode=0o700)
        self.passphrase_file = os.path.join(self.tmp, "bp")
        with open(self.passphrase_file, "w", encoding="utf-8") as fh:
            fh.write(FAKE_PASSPHRASE + "\n")
        os.chmod(self.passphrase_file, 0o600)
        # 库本体 + 一个 supervisord 管理件（后者必须被排除，不进包）
        self._write(os.path.join(self.data, "yiban.db"), "SQLite format 3\x00drill")
        self._write(os.path.join(self.data, "logs", "supervisord.pid"), "12345")
        self._write(os.path.join(self.data, "logs", "sign-2026-10-09.log"), "line\n")
        self.stub_dir = os.path.join(self.tmp, "stub")
        os.makedirs(self.stub_dir)

    @staticmethod
    def _write(path, text):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def _run(self, tar_stub=None):
        """跑一次备份脚本。`tar_stub` 给了就把桩放到 PATH 最前（覆盖真 tar）。"""
        env = dict(os.environ)
        env.update({
            "DATA_DIR": self.data,
            "BACKUP_DIR": self.backup_dir,
            "RETAIN_DAYS": "30",
            "GNUPGHOME": self.gnupg,
            "YIBAN_BACKUP_PASSPHRASE_FILE": self.passphrase_file,
            "YIBAN_DB_FILE": os.path.join(self.data, "yiban.db"),
        })
        if tar_stub is not None:
            stub = os.path.join(self.stub_dir, "tar")
            self._write(stub, tar_stub % TAR)
            os.chmod(stub, 0o755)
            env["PATH"] = self.stub_dir + os.pathsep + env.get("PATH", "")
        return subprocess.run([BASH, SCRIPT], capture_output=True, text=True,
                              env=env, timeout=120)

    @staticmethod
    def _out(proc):
        return (proc.stdout or "") + (proc.stderr or "")

    def _product(self):
        hits = glob.glob(os.path.join(self.backup_dir, "yiban-data-*.tar.gz.gpg"))
        self.assertLessEqual(len(hits), 1, "产物不该有多份：%r" % hits)
        return hits[0] if hits else None

    def _archive_members(self, product):
        """解密产物并列出成员名（真 gpg 对称解密，与脚本同回路）。"""
        with open(self.passphrase_file, encoding="utf-8") as fh:
            passphrase = fh.read().strip()
        env = dict(os.environ)
        env["GNUPGHOME"] = self.gnupg
        proc = subprocess.run(
            [GPG, "--batch", "--yes", "--decrypt", "--passphrase-fd", "0", product],
            input=passphrase.encode("utf-8"), capture_output=True, env=env, timeout=120)
        self.assertEqual(proc.returncode, 0, "解密失败（夹具口令应能解开自己产的包）")
        listing = subprocess.run([TAR, "-tzf", "-"], input=proc.stdout,
                                 capture_output=True, timeout=120)
        self.assertEqual(listing.returncode, 0, "列包失败")
        return listing.stdout.decode("utf-8", "replace").split()

    # ---- ① 正常路径：库入包、管理件被排除 ----
    def test_normal_backup_includes_db_and_excludes_supervisord_files(self):
        proc = self._run()
        out = self._out(proc)
        self.assertEqual(proc.returncode, 0, "正常路径必须成功：%s" % out)
        product = self._product()
        self.assertIsNotNone(product, "正常路径必须产出备份包：%s" % out)
        self.assertIn("库本体", out, "成品必须报告库本体已入包：%s" % out)
        members = self._archive_members(product)
        self.assertTrue(any(m.endswith("/yiban.db") for m in members),
                        "包内必须有库本体，实际成员：%r" % members)
        self.assertFalse(any("supervisord.pid" in m for m in members),
                         "supervisord 管理件必须被排除（root 属主读不到，进包只会制造噪声）：%r"
                         % members)

    # ---- ② tar 真失败不许被吞（原实现挂 `|| true` 的回归面）----
    def test_tar_nonzero_exit_is_not_swallowed(self):
        proc = self._run(tar_stub=TAR_STUB_RC2)
        out = self._out(proc)
        self.assertNotEqual(proc.returncode, 0,
                            "tar 退 2（有条目读不到）时脚本必须失败，不得报成功：%s" % out)
        self.assertIn("tar 打包失败", out, "失败原因必须点名 tar：%s" % out)
        self.assertIsNone(self._product(),
                          "失败必须删除产物（坏包残留＝调度器可能把它当成本次备份）")

    # ---- ① 的独立面：tar 退 0 但包里没有库 ⇒ 仍必须拒留 ----
    def test_archive_without_db_is_refused(self):
        proc = self._run(tar_stub=TAR_STUB_NO_DB)
        out = self._out(proc)
        self.assertNotEqual(proc.returncode, 0,
                            "包内缺库本体时必须失败（只做 tar rc 检查的修法抓不到这一路）：%s" % out)
        self.assertIn("没有】", out, "失败原因必须点名缺的是库：%s" % out)
        self.assertIsNone(self._product(), "失败必须删除产物")


if __name__ == "__main__":
    unittest.main(verbosity=2)
