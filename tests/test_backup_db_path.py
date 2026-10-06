# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""backup.sh 选哪份库进归档：键名与优先级必须与引擎同口径（工单 ba-p02-01）。

标签：J · 运维：部署/备份/发布
覆盖：`scripts/backup.sh` 的库路径取值——① `.env` 里声明的 `YIBAN_DB_FILE` 必须真的
    被备份进归档（改前红：只按 `${APP_DIR}/${DB_FILE}` 拼，从不读 `.env`，归档里没有
    现役数据库而脚本仍按 rc=0/6 收工）；② 进程环境已设该键时进程环境赢；
    ③ 同一条输入，bash 选中的库与 Python 侧 `env_io.resolve_path` 解析出的库必须
    是同一份文件（两侧各测一次）。
对应实现：`scripts/backup.sh` 的 `env_get`（进程环境 → `${APP_DIR}/.env`）与
    `yiban/infra/env_io.py:resolve_path`（进程环境 → .env → 默认值）。
关键断言：判据全部是**行为断言**——跑真脚本、解真归档、读归档里那份 sqlite 的表名、
    真跑一次 `--restore` 演练。唯一的源码断言是下面那条边界守卫，它钉的是"实现形态"
    而非行为，没有可观察的行为形状。
边界守卫（AGENTS §15）：`test_dotenv_reader_stays_single` 钉住"本文件只许一套
    `.env` 解析"——库路径必须复用已有的 `env_get`，谁再往里添第五套 `.env` 读法即红。

依赖：bash 与 sqlite3 命令（缺一按整文件 skip）——判据读的是 sqlite3 的一致性快照
    与 integrity_check，没有 sqlite3 时脚本自身会退成 cp 路径，那不是本单要钉的形态。
测试树全部在临时目录里合成：不碰仓内任何 `*.db`、不碰真实备份目录、不联网。
"""
import contextlib
import glob
import io
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(BASE, "scripts", "backup.sh")
BASH = shutil.which("bash")
SQLITE3 = shutil.which("sqlite3")

FAKE_KEY = "f" * 64  # 假数据密钥（hex 形态），不含任何真实凭据

#: 三份候选库各带一张"身份证"表——归档里出现哪张表，就说明本轮备份选中了哪份库
TBL_DEFAULT = "id_default_yiban_db"
TBL_FROM_ENV = "id_from_env_file_db"
TBL_FROM_PROC = "id_from_process_env_db"


def _make_db(path, table):
    """造一份只含单表的最小 sqlite 库（表名即身份）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(f"CREATE TABLE {table} (x)")
        conn.commit()
    finally:
        conn.close()


def _tables_of(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    try:
        return sorted(r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"))
    finally:
        conn.close()


@unittest.skipIf(BASH is None, "需要 bash（Git Bash/WSL）")
@unittest.skipIf(SQLITE3 is None, "需要 sqlite3 命令（一致性快照与 integrity_check 判据）")
class BackupDbPathTest(unittest.TestCase):
    """合成树里跑真 backup.sh：`.env` 声明的库必须进归档。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="backup-dbpath-")
        self.app = os.path.join(self.tmp, "app")
        self.backups = os.path.join(self.tmp, "backups")
        self.state = os.path.join(self.tmp, "state")
        self.logs = os.path.join(self.tmp, "logs")
        for d in (self.app, self.backups, self.state, self.logs):
            os.makedirs(d)
        self.env_file = os.path.join(self.app, ".env")
        self._write_env("")
        # 继承环境里与本轮无关的键全部摘除，保证"进程环境不设"是真的不设
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("YIBAN_", "BACKUP_", "REMOTE_"))
                    and k not in ("RETENTION_DAYS", "KEY_FILE", "DB_FILE",
                                  "SIGN_STATE_DIR", "SIGN_LOG_DIR")}
        self.env.update({
            "APP_DIR": self.app,
            "BACKUP_DIR": self.backups,
            "YIBAN_STATE_DIR": self.state,
            "YIBAN_LOG_FILE": os.path.join(self.logs, "sign.log"),
            "KEY_FILE": os.path.join(self.tmp, "no-accounts-key"),
            "BACKUP_PLAINTEXT": "1",
        })
        self.db_default = os.path.join(self.app, "yiban.db")
        self.db_from_env = os.path.join(self.tmp, "data", "custom.db")
        self.db_from_proc = os.path.join(self.tmp, "procenv", "proc.db")
        _make_db(self.db_default, TBL_DEFAULT)
        _make_db(self.db_from_env, TBL_FROM_ENV)
        _make_db(self.db_from_proc, TBL_FROM_PROC)
        self.addCleanup(self._sweep)

    def _sweep(self):
        """明文归档 0600 / 备份目录 0700：整棵临时树要能删，先放开权限。"""
        for root, dirs, files in os.walk(self.tmp):
            for name in dirs + files:
                with contextlib.suppress(OSError):
                    os.chmod(os.path.join(root, name), 0o700)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_env(self, body):
        with io.open(self.env_file, "w", encoding="utf-8", newline="\n") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={FAKE_KEY}\n{body}")

    def _run_backup(self):
        r = subprocess.run([BASH, SCRIPT], capture_output=True, env=self.env,
                           cwd=self.tmp, timeout=300)
        out = ((r.stdout or b"") + (r.stderr or b"")).decode("utf-8", errors="replace")
        # 退出码 6 = 明文轮（本轮显式 BACKUP_PLAINTEXT=1）；归档必须真实产出
        self.assertEqual(r.returncode, 6, f"明文轮应以 rc=6 收工：{out}")
        found = glob.glob(os.path.join(self.backups, "yiban-*.tar.gz"))
        self.assertEqual(1, len(found), f"本轮应恰好产出一个明文归档：{out}")
        return found[0], out

    def _archived_db(self):
        """跑一轮备份，返回 (归档 data/ 的 {文件名: 表清单}, 全部输出, 归档路径)。"""
        archive, out = self._run_backup()
        return self._archived_tables(archive), out, archive

    def _restore_drill_out(self, archive):
        """对真归档跑一次 `--restore` 演练，返回其全部输出。"""
        dest = os.path.join(self.tmp, "restored")
        r = subprocess.run([BASH, SCRIPT, "--restore", archive, dest],
                           capture_output=True, env=self.env, cwd=self.tmp, timeout=300)
        return ((r.stdout or b"") + (r.stderr or b"")).decode("utf-8", errors="replace")

    def _archived_tables(self, archive):
        """解包归档，返回 data/ 下每份 sqlite 库的 {文件名: 表清单}。

        解包落点在临时根下，不落进 BACKUP_DIR——否则备份目录里多出一份
        "看着像恢复件"的东西，会被后续轮转与恢复演练读到。
        """
        extract = os.path.join(self.tmp, "unpacked")
        os.makedirs(extract, exist_ok=True)
        with tarfile.open(archive) as t:
            t.extractall(extract)
        data = os.path.join(extract, "data")
        if not os.path.isdir(data):
            return {}
        got = {}
        for name in sorted(os.listdir(data)):
            full = os.path.join(data, name)
            if os.path.isfile(full) and name != ".env":
                got[name] = _tables_of(full)
        return got

    def _python_side_choice(self):
        """Python 侧唯一解析器对同一输入的答案（子进程，与 web/引擎同形态）。"""
        code = ("import sys; sys.path.insert(0, sys.argv[1]);"
                "from yiban.infra import env_io;"
                "print(env_io.resolve_path('YIBAN_DB_FILE', 'yiban.db'))")
        r = subprocess.run([sys.executable, "-c", code, BASE], capture_output=True,
                           env=self.env, cwd=self.app, timeout=120, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return os.path.abspath(r.stdout.strip())

    # ---------------- 判据 ①：.env 声明的库必须进归档 ----------------
    def test_db_file_declared_only_in_dotenv_is_archived(self):
        """.env 写 YIBAN_DB_FILE（绝对路径）、进程环境不设 ⇒ 归档里是那份库。

        同时钉恢复核对段：它必须以【包内那份库】为对象真跑起来，不再静默空转。
        """
        self._write_env(f"YIBAN_DB_FILE={self.db_from_env}\n")
        got, out, archive = self._archived_db()
        self.assertEqual([TBL_FROM_ENV], got.get(os.path.basename(self.db_from_env)),
                         f"`.env` 声明的库没进归档（归档里是 {got}）——"
                         f"备份侧仍在裸读无前缀键 DB_FILE：{out}")
        self.assertNotIn(TBL_DEFAULT, [t for tbls in got.values() for t in tbls],
                         f"声明了自定义库路径却把默认库当现役库备进来了：{got}")
        self.assertNotIn("跳过（不存在）", out,
                         "库明明存在（.env 已声明），备份段仍报「跳过（不存在）」：" + out)
        # 绝对路径必须以 **sqlite3 .backup** 的一致性快照落进归档：归档内落点是绝对
        # 路径时 .backup 直接失败、退成 cp——WAL 未合并的副本比没有副本更危险。
        self.assertIn("数据库已备份（一致性快照，integrity_check=ok）", out,
                      f"声明的库没走 .backup 一致性快照路径：{out}")
        self.assertNotIn("回退为文件复制", out,
                         f".backup 失败被 cp 静默掩盖（归档内落点拼成了绝对路径）：{out}")
        drill = self._restore_drill_out(archive)
        self.assertIn("integrity_check=ok", drill,
                      "恢复核对段没有对包内那份库真跑起来（空转）：" + drill)
        self.assertNotIn("包内无", drill,
                         "恢复核对段找不到包内的库——归档内落点与核对落点不同名：" + drill)

    # ---------------- 判据 ②③：两侧同输入同答案 ----------------
    def test_process_env_beats_dotenv_on_both_sides(self):
        """进程环境已设该键 ⇒ 进程环境赢；bash 与 Python 对同一输入同答案。"""
        self._write_env(f"YIBAN_DB_FILE={self.db_from_env}\n")
        self.env["YIBAN_DB_FILE"] = self.db_from_proc
        py_choice = self._python_side_choice()
        got, out, _ = self._archived_db()
        self.assertEqual([TBL_FROM_PROC], got.get(os.path.basename(self.db_from_proc)),
                         f"进程环境应压过 .env（归档里是 {got}）：{out}")
        self.assertEqual(py_choice, os.path.abspath(self.db_from_proc),
                         "bash 侧与 Python 侧选中的库不是同一份——优先级口径分叉")

    def test_dotenv_value_is_the_choice_on_both_sides(self):
        """只有 .env 有值时，两侧选中的也必须是同一份。"""
        self._write_env(f"YIBAN_DB_FILE={self.db_from_env}\n")
        py_choice = self._python_side_choice()
        got, out, _ = self._archived_db()
        self.assertEqual([TBL_FROM_ENV], got.get(os.path.basename(self.db_from_env)),
                         f"`.env` 声明的库没进归档（归档里是 {got}）：{out}")
        self.assertEqual(py_choice, os.path.abspath(self.db_from_env),
                         "Python 侧解析到的路径与 .env 声明不一致（解析器本体漂了）")

    def test_no_declaration_anywhere_still_backs_up_default_db(self):
        """两侧都没声明 ⇒ 回落默认库，反向控制：收口不许把默认档改丢。"""
        self._write_env("")
        got, out, _ = self._archived_db()
        self.assertEqual([TBL_DEFAULT], got.get("yiban.db"),
                         f"未声明时归档里应是且仅是默认库（归档里是 {got}）：{out}")

    # ---------------- 边界守卫（AGENTS §15） ----------------
    def test_dotenv_reader_stays_single(self):
        """库路径必须复用本文件已有的 `env_get`：全文件只许一套 `.env` 解析。"""
        with io.open(SCRIPT, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("env_get YIBAN_DB_FILE", src,
                      "库路径没走本文件已有的 env_get 双源口径")
        readers = [line for line in src.splitlines()
                   if "APP_DIR}/.env" in line and "sed -n" in line]
        self.assertEqual(1, len(readers),
                         f"backup.sh 里按行解析 .env 的实现应只有 env_get 一处"
                         f"（实测 {len(readers)} 处）——再添一套就是根没找到")


if __name__ == "__main__":
    unittest.main(verbosity=2)
