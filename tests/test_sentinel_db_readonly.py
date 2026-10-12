# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""备份哨兵的库路径与只读初始化：`.env` 的声明不许被顶掉，缺库不许被建成空伪库。

标签：J · 运维：部署/备份/发布
覆盖：工单 ba-p02-02（`scripts/yiban-backup-sentinel.sh` 的兜底 export 主动压掉
    `.env` 声明的库路径）与 ba-p11-02（`scripts/backup_sentinel.py::_anchor_snapshot()`
    漏 `create=False`，缺失的库被当场建出来，"空链"于是冒充当日离机基线）。
    两枚是同一条链的两个环：路径解析口径 + 只读取证初始化。
对应实现：`scripts/yiban-backup-sentinel.sh`（薄包装只该导出 `.env` 指针与 cwd）、
    `scripts/backup_sentinel.py::_anchor_snapshot`（经 `db.init_db(create=False)`）、
    `yiban/infra/env_io.py:resolve_path`（进程环境 → .env → 默认值的唯一解析口径）。
关键断言：全部经**子进程**跑真脚本 / 真解析器，按行为判（归档里选了哪条路径、磁盘上
    多出哪些文件、外发的正文写的是"读不到"还是"空链"）。不在测试进程里调
    `db.init_db`——那是**进程级单例连接**的初始化，会把同 worker 后续用例的库指走
    （先例见 tests/test_backup_e2e.py 对 `_anchor_snapshot` 的打桩说明）。

依赖：bash 与 python3（缺则按 skip 处理）；sqlite3 命令不需要。
测试树全部在临时目录里合成：不碰仓内任何 `*.db`、不碰真实备份目录、不联网、不发信
    （收件人算法在无库无 .env 的隔离环境里解析为空，发送出口另按用例打桩）。
"""
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WRAPPER = os.path.join(BASE, "scripts", "yiban-backup-sentinel.sh")
SENTINEL = os.path.join(BASE, "scripts", "backup_sentinel.py")

FAKE_KEY = "f" * 64  # 假数据密钥（hex 形态），不含任何真实凭据


def _clean_env(**overrides):
    """剥掉继承的 YIBAN_*/APP_DIR/BACKUP_DIR，再按本用例的合成树钉死路径。"""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("YIBAN_", "BACKUP_")) and k not in ("APP_DIR",)}
    env.update({
        "YIBAN_ACCOUNTS_KEY": FAKE_KEY,
        "YIBAN_MAIL_ENABLE": "0",
        "PYTHONPATH": BASE,
        "PYTHONIOENCODING": "utf-8",
    })
    env.update({k: v for k, v in overrides.items() if v is not None})
    return env


class _TreeMixin:
    """合成树：APP_DIR（含 .env）+ 库在 APP_DIR 之外的自定义位置 + 空的备份目录。"""

    def _make_tree(self):
        self.tmp = tempfile.mkdtemp(prefix="sentinel-chain-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.app = os.path.join(self.tmp, "app")
        os.makedirs(os.path.join(self.app, "scripts"))
        # 合成 APP_DIR 里放一个 .venv/bin/python3 指向跑测解释器：包装脚本按
        # `$APP_DIR/.venv/bin/python3` → PATH python3 三级解析解释器，生产用的就是
        # 第一级；不设这级会退到系统 python3，那个解释器没装本项目的依赖。
        venv_bin = os.path.join(self.app, ".venv", "bin")
        os.makedirs(venv_bin, exist_ok=True)
        # 用 exec 包装而不是 symlink：symlink 会让解释器按链接目标解析 venv 上下文，
        # 指到 base python 上就没有项目依赖了。
        shim = os.path.join(venv_bin, "python3")
        with io.open(shim, "w", encoding="utf-8", newline="\n") as f:
            f.write("#!/bin/sh\nexec " + sys.executable + ' "$@"\n')
        os.chmod(shim, 0o755)
        # 把真哨兵摆进合成 APP_DIR：包装脚本按 $APP_DIR/scripts/backup_sentinel.py 找它
        shutil.copy(SENTINEL, os.path.join(self.app, "scripts", "backup_sentinel.py"))
        self.backups = os.path.join(self.tmp, "backups")
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.backups)
        os.makedirs(self.state)
        self.env_file = os.path.join(self.app, ".env")
        self.custom_db = os.path.join(self.tmp, "elsewhere", "audit.db")
        self.proc_db = os.path.join(self.tmp, "procenv", "proc.db")
        self._write_env("")
        return self.app

    def _write_env(self, body):
        with io.open(self.env_file, "w", encoding="utf-8", newline="\n") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={FAKE_KEY}\n{body}")

    def _base_env(self, **overrides):
        return _clean_env(APP_DIR=self.app, BACKUP_DIR=self.backups,
                          YIBAN_ENV_FILE=self.env_file,
                          YIBAN_STATE_DIR=self.state,
                          YIBAN_LOG_FILE=os.path.join(self.tmp, "logs", "sign.log"),
                          YIBAN_BACKUP_INSTALLED=os.path.join(self.tmp, "absent.sh"),
                          **overrides)


class WrapperDbPathTest(_TreeMixin, unittest.TestCase):
    """包装脚本不得自己给库路径编一个值——导出即压过 `.env`（store 侧进程环境优先）。"""

    #: 假哨兵：只回报"按 store 侧同一解析器算出来的库路径"，判据本身由本类钉
    STUB = ("import os, sys\n"
            "sys.path.insert(0, os.environ['SENTINEL_REPO'])\n"
            "from yiban.infra import env_io\n"
            "print(env_io.resolve_path('YIBAN_DB_FILE', 'yiban.db'))\n")

    @classmethod
    def setUpClass(cls):
        if shutil.which("bash") is None or shutil.which("python3") is None:
            raise unittest.SkipTest("需要 bash 与 python3（Git Bash/WSL）")

    def setUp(self):
        self._make_tree()

    def _run_wrapper(self, extra_env=None):
        with io.open(os.path.join(self.app, "scripts", "backup_sentinel.py"), "w",
                     encoding="utf-8", newline="\n") as f:
            f.write(self.STUB)
        env = self._base_env()
        env["SENTINEL_REPO"] = BASE
        env.update(extra_env or {})
        home = os.path.join(self.tmp, "home")
        os.makedirs(home, exist_ok=True)
        r = subprocess.run([shutil.which("bash"), WRAPPER], capture_output=True,
                           text=True, cwd=home, env=env, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        return os.path.abspath(os.path.join(self.app, r.stdout.strip()))

    def test_dotenv_db_path_is_not_overridden_by_the_wrapper(self):
        """.env 声明自定义库、进程环境不设 ⇒ 哨兵进程解析到的就是那条，不是兜底值。"""
        self._write_env(f"YIBAN_DB_FILE={self.custom_db}\n")
        self.assertEqual(self._run_wrapper(), os.path.abspath(self.custom_db),
                         "包装脚本的兜底 export 顶掉了 .env 声明的库路径")

    def test_process_env_db_path_still_wins(self):
        """进程环境已设该键 ⇒ 进程环境赢（与 env_io.resolve_path 同档，不许反过来）。"""
        self._write_env(f"YIBAN_DB_FILE={self.custom_db}\n")
        got = self._run_wrapper({"YIBAN_DB_FILE": self.proc_db})
        self.assertEqual(got, os.path.abspath(self.proc_db))

    def test_unset_anywhere_falls_back_to_app_dir(self):
        """两侧都没写 ⇒ 回落 <APP_DIR>/yiban.db：M28 加这条导出要保的东西一件不少。"""
        self._write_env("")
        got = self._run_wrapper()
        self.assertEqual(got, os.path.abspath(os.path.join(self.app, "yiban.db")))

    def test_wrapper_does_not_resolve_the_db_path_itself(self):
        """边界守卫（AGENTS §15）：包装脚本不再自己给 YIBAN_DB_FILE 赋值。

        库路径的唯一解析器在 Python 侧（`env_io.resolve_path`）。bash 里每多一处
        赋值就多一套口径，而这套口径与 Python 侧的三档顺序天然相反。
        """
        with io.open(WRAPPER, encoding="utf-8") as f:
            src = f.read()
        assigns = [line for line in src.splitlines()
                   if line.strip().startswith(("export YIBAN_DB_FILE", "YIBAN_DB_FILE="))]
        self.assertEqual([], assigns,
                         f"包装脚本里给库路径赋值的行必须为零（实测 {assigns}）")


class AnchorSnapshotReadOnlyTest(_TreeMixin, unittest.TestCase):
    """`_anchor_snapshot()` 真只读：缺库不建库、不切 WAL，结论是"读不到"不是"空链"。"""

    #: 子进程探针：调真的 `_anchor_snapshot()` 与真的外发决策，把结论打出来。
    #: 打桩只打在**发送出口**（不发真信），读库路径与建库判定全程是真的。
    PROBE = (
        "import json, os, sys\n"
        "sys.path.insert(0, os.environ['SENTINEL_REPO'])\n"
        "sys.path.insert(0, os.path.join(os.environ['SENTINEL_REPO'], 'scripts'))\n"
        "import backup_sentinel as bs\n"
        "sent = []\n"
        "bs._send_admin_alert = lambda title, mail: (sent.append([title, mail.to_plain()]), True)[1]\n"
        "bs._alert_due = lambda *a, **k: True\n"
        "verdict = {'anchor_title': bs.ANCHOR_TITLE}\n"
        "try:\n"
        "    verdict['snapshot'] = bs._anchor_snapshot()\n"
        "except Exception as e:\n"
        "    verdict['snapshot_raised'] = type(e).__name__\n"
        "verdict['broadcast_sent'] = bool(bs._broadcast_anchor('2026-01-01', None, True))\n"
        "verdict['mails'] = sent\n"
        "print('PROBE_JSON=' + json.dumps(verdict, ensure_ascii=False))\n")

    def setUp(self):
        self._make_tree()
        self.probe_path = os.path.join(self.tmp, "probe.py")
        with io.open(self.probe_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(self.PROBE)

    def _probe(self, db_file):
        env = self._base_env(YIBAN_DB_FILE=db_file, SENTINEL_REPO=BASE)
        r = subprocess.run([sys.executable, self.probe_path], capture_output=True,
                           text=True, env=env, cwd=self.app, timeout=180)
        line = [ln for ln in (r.stdout or "").splitlines()
                if ln.startswith("PROBE_JSON=")]
        self.assertTrue(line, f"探针没有给出结论：stdout={r.stdout!r} stderr={r.stderr!r}")
        return json.loads(line[0][len("PROBE_JSON="):]), r

    def _anchor_mails(self, verdict):
        return [m for m in verdict["mails"] if m[0] == verdict["anchor_title"]]

    def test_missing_db_is_not_created_and_reads_as_unreadable(self):
        """库不存在：不得新建文件、不得建 -wal/-shm；外发结论必须是"读不到"。"""
        missing = os.path.join(self.tmp, "elsewhere", "audit.db")
        # 父目录先备好：目录缺失时 sqlite3.connect 自己就写不进去，那条用例测的就不是
        # "只读取证不建库"，而是"写不进去"——假绿。
        os.makedirs(os.path.dirname(missing), exist_ok=True)
        self.assertFalse(os.path.exists(missing))
        verdict, _ = self._probe(missing)
        self.assertIsNone(verdict.get("snapshot"),
                          f"库不存在却拿到了快照（读不到被当成了空链）：{verdict}")
        self.assertEqual([], self._anchor_mails(verdict),
                          "读不到链头时仍外发了锚点邮件")
        self.assertFalse(verdict["broadcast_sent"],
                         f"读不到链头却报外发成功：{verdict}")
        leftovers = [p for p in (missing, missing + "-wal", missing + "-shm")
                     if os.path.exists(p)]
        self.assertEqual([], leftovers, f"只读取证建出了库文件：{leftovers}")

    def test_existing_db_is_not_switch_to_wal_and_empty_is_told_apart(self):
        """库存在且审计链为空：不得切 WAL、不得改 mtime；结论必须是可区分的"空链"。"""
        built = self._build_db()
        before = (os.path.getmtime(built), os.path.getsize(built))
        verdict, _ = self._probe(built)
        snap = verdict.get("snapshot")
        self.assertIsInstance(snap, dict,
                              f"库存在时链头应读出结论而不是读不到：{verdict}")
        self.assertEqual("empty", snap["state"],
                         "空链应报 state=empty（与'读不到'分档）")
        self.assertEqual([], [p for p in (built + "-wal", built + "-shm")
                              if os.path.exists(p)],
                         "只读取证给库添了 WAL sidecar")
        self.assertEqual("delete", self._journal_mode(built),
                         "只读取证把库切成了 WAL（init_db 缺省 create=True 会做这件事）")
        self.assertEqual(before, (os.path.getmtime(built), os.path.getsize(built)),
                         "只读取证改动了库文件（建表/切 WAL/迁移都会留痕）")
        mails = self._anchor_mails(verdict)
        self.assertEqual(1, len(mails), f"空链当天仍应外发基线并写明原因：{verdict}")
        self.assertIn("空链", mails[0][1])
        self.assertNotIn("读不到", mails[0][1])

    def _build_db(self):
        """在子进程里用引擎建一份真 schema 的空链库，再退回 rollback-journal 模式。

        退回 `journal_mode=DELETE` 是给"不许切 WAL"这条判据用的：库本来就是 WAL 时，
        只读打开也可能留 `-shm`/`-wal`（SQLite 的读簿记），"有没有被改成 WAL"就读不出来了。
        测试进程本身不碰 `db` 单例连接——建表全部经子进程。
        """
        built = os.path.join(self.tmp, "seeded", "audit.db")
        os.makedirs(os.path.dirname(built), exist_ok=True)
        code = ("import os, sys; sys.path.insert(0, os.environ['SENTINEL_REPO']);"
                "from yiban.store import db;"
                "c = db.init_db(db_file=os.environ['TARGET_DB'],"
                " env_file=os.environ['YIBAN_ENV_FILE'], cleanup=False);"
                "c.close()")
        env = self._base_env(SENTINEL_REPO=BASE, TARGET_DB=built)
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           env=env, cwd=self.app, timeout=180)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        conn = sqlite3.connect(built)
        try:
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.commit()
        finally:
            conn.close()
        self.assertEqual("delete", self._journal_mode(built), "测试件没退回 rollback journal")
        return built

    @staticmethod
    def _journal_mode(db_file):
        conn = sqlite3.connect(f"file:{db_file}?mode=ro&immutable=1", uri=True)
        try:
            return str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
