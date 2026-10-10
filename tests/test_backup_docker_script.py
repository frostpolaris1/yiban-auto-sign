# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""`docker/backup-docker.sh` 的活体契约：打前先查 + 库本体入包。

标签：J · 运维：部署/备份/发布
覆盖：以真实 bash 子进程跑 `docker/backup-docker.sh`。钉三条：

    ① **打前先查是唯一判据**（工单 `yiban-auto-sign-4o35` 路线 2）。打包前遍历
       DATA_DIR，白名单之外只要有一条本用户读不到，整轮必须响亮失败并点名路径。
       打包成败不看 tar 退出码：本脚本用 `tar -czf`，下游提前关 stdin 时收到
       SIGPIPE 的是里层压缩程序，tar 报 rc=2，与"有条目读不到"同码，故退出码不可用
       当判据。管理件（logs/ 直属一层的 `supervisord.log`、`web.log`、`sched.log`
       及其纯数字轮转后缀 `.N`、`supervisord.pid`）按模式白名单放行：supervisord 配了
       `logfile_maxbytes`/`logfile_backups`，轮转族 `.1/.2/.3` 同为 root 属主，必须整体放行，否则天天失败。
    ② **库本体必须在包内**。磁盘上有库、包里没有 ⇒ 必须失败并删除产物。
    ③ **库在子目录时判据查对路径**。原实现只取 `basename` 再去 DATA_DIR 根下找，
       `YIBAN_DB_FILE=sub/x.db` 时找不到磁盘文件 ⇒ 静默跳过断言（漏判）。

关键断言：全部是**行为断言**——执行真脚本、读真退出码、解真产物列内容；不 grep
源码文本（源码文本级断言正是"假绿"教训：改了字符串就同步改测试）。

**读不到的条目**：root 跑测时 root 能读一切，故用 `setpriv` 降到 nobody 造该位格；
非 root 跑测时用 mode 0000 造该位格。缺 `setpriv`（且是 root）即失败，不 skip——
skip 会让"打前先查"这条核心判据在门禁里假绿。
依赖：bash + tar + gpg。缺任一即失败，不 skip。
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
SETPRIV = shutil.which("setpriv")
# WSL Ubuntu 的 nobody/nogroup 位格；只借它的 uid/gid，不碰任何账号数据。
UNPRIVILEGED_ID = 65534
FAKE_PASSPHRASE = "e2e-not-a-real-passphrase"  # 假口令，仅对称加密回路使用

#: tar 桩：跑真 tar 但把库排除掉、**以 0 退出**——复刻"产物少了库却不报错"的形状，
#: 专钉②（只做"打前先查"的修法抓不到这一路）。
TAR_STUB_NO_DB = """#!/usr/bin/env bash
"%s" --exclude '*/yiban.db' "$@"
exit 0
"""


def _is_root():
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _unprivileged_prefix():
    """造「非 root 位格」的 argv 前缀。

    ()   ⇒ 当前进程已不是 root，直接跑就是无特权跑法。
    None ⇒ 当前是 root 但宿主没有 setpriv ⇒ 造不出该位格，用例须失败（不 skip）。
    其余 ⇒ 用 setpriv 降到 nobody 的 uid/gid 并清掉附加组。
    """
    if not _is_root():
        return []
    if SETPRIV is None:
        return None
    return [SETPRIV, "--reuid=%d" % UNPRIVILEGED_ID,
            "--regid=%d" % UNPRIVILEGED_ID, "--clear-groups"]


def _chown_tree(root, uid, gid):
    os.chown(root, uid, gid)
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            os.chown(os.path.join(dirpath, name), uid, gid)


class ContainerBackupScriptTest(unittest.TestCase):
    """真脚本 + 真 tar/gpg；故障桩只用来注入产物内容。"""

    @classmethod
    def setUpClass(cls):
        # 缺依赖即失败，不 skip：本文件是"打前先查"与"库本体入包"的唯一门禁，
        # skip 会让门禁静默变绿（工单 4o35 复查点名）。
        missing = [name for name, path in
                   (("bash", BASH), ("tar", TAR), ("gpg", GPG)) if path is None]
        if missing:
            raise RuntimeError(
                "缺依赖 %s：本文件必须真跑脚本，缺依赖即失败（不得 skip）" % ",".join(missing))

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
        self.db = os.path.join(self.data, "yiban.db")
        self._write(self.db, "SQLite format 3\x00drill")
        self._write(os.path.join(self.data, "logs", "sign-2026-10-09.log"), "line\n")
        # 供非特权位格跑用的脚本副本：pytest 的 cwd 常在 /root 下，nobody 穿不进去，
        # 故把脚本拷到本用例的临时区（该区已放开给 nobody）。
        self.unpriv_script = os.path.join(self.tmp, "backup-docker.sh")
        shutil.copyfile(SCRIPT, self.unpriv_script)
        os.chmod(self.unpriv_script, 0o755)
        self.stub_dir = os.path.join(self.tmp, "stub")
        os.makedirs(self.stub_dir)

    @staticmethod
    def _write(path, text):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def _base_env(self, db_path):
        env = dict(os.environ)
        env.update({
            "DATA_DIR": self.data,
            "BACKUP_DIR": self.backup_dir,
            "RETAIN_DAYS": "30",
            "GNUPGHOME": self.gnupg,
            "YIBAN_BACKUP_PASSPHRASE_FILE": self.passphrase_file,
            "YIBAN_DB_FILE": db_path,
        })
        return env

    def _run(self, tar_stub=None, db_path=None):
        """以当前用户（root 位格）跑一次备份脚本。"""
        env = self._base_env(db_path or self.db)
        if tar_stub is not None:
            stub = os.path.join(self.stub_dir, "tar")
            self._write(stub, tar_stub % TAR)
            os.chmod(stub, 0o755)
            env["PATH"] = self.stub_dir + os.pathsep + env.get("PATH", "")
        return subprocess.run([BASH, SCRIPT], capture_output=True, text=True,
                              env=env, timeout=120)

    def _stage_unprivileged(self, unreadable_rels):
        """把临时区交给 nobody，再把指定条目收成"读不到"。

        root 位格：条目 chown 回 root + 0600，nobody 读不到。
        非 root 位格：条目 chmod 0000，属主自己也读不到。
        """
        if _is_root():
            os.chmod(self.tmp, 0o755)
            _chown_tree(self.tmp, UNPRIVILEGED_ID, UNPRIVILEGED_ID)
        for rel in unreadable_rels:
            path = os.path.join(self.data, rel)
            os.chmod(path, 0o000)
            if _is_root():
                os.chown(path, 0, 0)

    def _run_unprivileged(self, prefix, db_path=None):
        env = self._base_env(db_path or self.db)
        return subprocess.run([*prefix, BASH, self.unpriv_script],
                              capture_output=True, text=True, env=env,
                              cwd=self.tmp, timeout=120)

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

    # ---- 正常路径：库入包 ----
    def test_normal_backup_includes_db(self):
        proc = self._run()
        out = self._out(proc)
        self.assertEqual(proc.returncode, 0, "正常路径必须成功：%s" % out)
        product = self._product()
        self.assertIsNotNone(product, "正常路径必须产出备份包：%s" % out)
        self.assertIn("库本体", out, "成品必须报告库本体已入包：%s" % out)
        members = self._archive_members(product)
        self.assertTrue(any(m.endswith("/yiban.db") for m in members),
                        "包内必须有库本体，实际成员：%r" % members)

    # ---- ② 的独立面：tar 退 0 但包里没有库 ⇒ 仍必须拒留 ----
    def test_archive_without_db_is_refused(self):
        proc = self._run(tar_stub=TAR_STUB_NO_DB)
        out = self._out(proc)
        self.assertNotEqual(proc.returncode, 0,
                            "包内缺库本体时必须失败（只做「打前先查」的修法抓不到这一路）：%s" % out)
        self.assertIn("没有】", out, "失败原因必须点名缺的是库：%s" % out)
        self.assertIsNone(self._product(), "失败必须删除产物")

    # ---- ③ 库在子目录：断言必须查对路径（原实现静默跳过 ⇒ 漏判）----
    def test_db_in_subdir_missing_from_archive_is_refused(self):
        os.remove(self.db)  # 库里没有根下的 yiban.db，只在子目录里
        sub = os.path.join(self.data, "sub")
        os.makedirs(sub)
        db = os.path.join(sub, "yiban.db")
        self._write(db, "SQLite format 3\x00drill")
        proc = self._run(tar_stub=TAR_STUB_NO_DB, db_path=db)
        out = self._out(proc)
        self.assertNotEqual(proc.returncode, 0,
                            "库在子目录、包里缺库时必须失败（原实现查错路径而静默跳过断言）：%s" % out)
        self.assertIn("没有】", out, "失败原因必须点名缺的是库：%s" % out)
        self.assertIsNone(self._product(), "失败必须删除产物")

    # ---- ① 白名单外有读不到的条目 ⇒ 响亮失败且点名路径 ----
    def test_unreadable_non_whitelisted_entry_fails_loudly(self):
        prefix = _unprivileged_prefix()
        if prefix is None:
            self.fail("当前是 root 而宿主没有 setpriv：造不出非 root 位格（本条不得 skip）")
        self._write(os.path.join(self.data, "secrets.bin"), "root-only\n")
        self._stage_unprivileged(["secrets.bin"])
        proc = self._run_unprivileged(prefix)
        out = self._out(proc)
        self.assertNotEqual(proc.returncode, 0,
                            "白名单外有读不到的条目时必须失败，不得报成功：%s" % out)
        self.assertIn("备份前检查", out, "失败必须出自「打前先查」，不是靠 tar 退出码：%s" % out)
        self.assertIn("secrets.bin", out, "失败必须点名读不到的路径：%s" % out)
        self.assertIsNone(self._product(), "失败必须不产出备份")

    # ---- M1 ① 未设 YIBAN_DB_FILE：默认取 ${DATA_DIR}/yiban.db，断言须命中而非跳过 ----
    def test_default_db_path_without_env_is_asserted(self):
        """手工调用不注入 YIBAN_DB_FILE 时，库默认在 DATA_DIR 内，必须命中库断言。"""
        env = self._base_env(self.db)
        env.pop("YIBAN_DB_FILE", None)
        proc = subprocess.run([BASH, SCRIPT], capture_output=True, text=True,
                              env=env, timeout=120)
        out = self._out(proc)
        self.assertEqual(proc.returncode, 0,
                         "未设 YIBAN_DB_FILE 的手工形态必须成功：%s" % out)
        self.assertIn("库本体", out,
                      "默认库路径须命中库断言并报告入包，不得跳过：%s" % out)
        self.assertNotIn("本次不断言", out,
                         "默认库路径就落在 DATA_DIR 内，不得走「不断言」旁路：%s" % out)
        self.assertIsNotNone(self._product(), "正常形态必须产出备份包：%s" % out)

    # ---- M1 ② 库被指到 DATA_DIR 之外 ⇒ 硬失败非零退出（不得 rc=0 收场）----
    def test_db_outside_data_dir_is_refused(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        db = os.path.join(outside, "yiban.db")
        self._write(db, "SQLite format 3\x00drill")
        proc = self._run(db_path=db)
        out = self._out(proc)
        self.assertNotEqual(proc.returncode, 0,
                            "库在 DATA_DIR 之外时必须硬失败，不得 rc=0 收场：%s" % out)
        self.assertIn("不在", out, "失败必须说明库不在 DATA_DIR 内：%s" % out)
        self.assertIsNone(self._product(), "失败必须删除产物")

    # ---- L1 白名单 `*` 不得吞子目录：logs/web.log.d/ 下的条目不放行 ----
    def test_log_subdir_entry_is_not_whitelisted(self):
        prefix = _unprivileged_prefix()
        if prefix is None:
            self.fail("当前是 root 而宿主没有 setpriv：造不出非 root 位格（本条不得 skip）")
        os.makedirs(os.path.join(self.data, "logs", "web.log.d"))
        self._write(os.path.join(self.data, "logs", "web.log.d", "secret.bin"), "root-only\n")
        self._stage_unprivileged(["logs/web.log.d/secret.bin"])
        proc = self._run_unprivileged(prefix)
        out = self._out(proc)
        self.assertNotEqual(proc.returncode, 0,
                            "logs/web.log.d/ 下的条目不在白名单内，读不到时必须失败：%s" % out)
        self.assertIn("备份前检查", out, "失败必须出自「打前先查」：%s" % out)
        self.assertIn("secret.bin", out, "失败必须点名读不到的路径：%s" % out)
        self.assertIsNone(self._product(), "失败必须不产出备份")

    # ---- ① 的对照面：管理件轮转族在白名单内 ⇒ 不再导致整轮失败 ----
    def test_manager_rotation_family_is_whitelisted(self):
        prefix = _unprivileged_prefix()
        if prefix is None:
            self.fail("当前是 root 而宿主没有 setpriv：造不出非 root 位格（本条不得 skip）")
        # supervisord 轮转族：.1/.2/.3 同为 root 属主，本用户读不到
        for name in ("supervisord.log.1", "supervisord.log.2", "supervisord.log.3"):
            self._write(os.path.join(self.data, "logs", name), "rotated\n")
        self._write(os.path.join(self.data, "logs", "supervisord.pid"), "12345")
        self._stage_unprivileged([
            "logs/supervisord.log.1",
            "logs/supervisord.log.2",
            "logs/supervisord.log.3",
            "logs/supervisord.pid",
        ])
        proc = self._run_unprivileged(prefix)
        out = self._out(proc)
        self.assertNotIn("备份前检查", out,
                         "轮转族已在白名单内，不得触发「打前先查」失败：%s" % out)
        self.assertEqual(proc.returncode, 0,
                         "轮转族属管理件，不应让整轮失败（否则容器备份天天失败）：%s" % out)
        self.assertIn("库本体", out, "正常完成必须报告库本体入包：%s" % out)
        product = self._product()
        self.assertIsNotNone(product, "正常完成必须产出备份包：%s" % out)
        members = self._archive_members(product)
        self.assertTrue(any(m.endswith("/yiban.db") for m in members),
                        "包内必须有库本体，实际成员：%r" % members)


if __name__ == "__main__":
    unittest.main(verbosity=2)
