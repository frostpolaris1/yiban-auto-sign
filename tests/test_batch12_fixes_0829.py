# -*- coding: utf-8 -*-
"""批次 12 修复回归测试（对抗性审查 2026-08-29）。

覆盖：
- B12-1  docker/backup-docker.sh 产出真实加密备份（herestring 截断管道的空备份
         修复）+ 尺寸下限/解密自检（假 gpg 端到端，需本机 bash；无则跳过）
- B12-2  调度闸门把 skipped_window/skipped_norange 计入未了结（补签不再被吞）；
         零成功专项告警 _maybe_alert_zero_success
- B12-3  容器子进程超时按窗口动态计算（_child_timeout，与 run.sh 同口径）
- B12-4  锚点路径默认与 web STATE_DIR 对齐；verify_audit_anchor 显式路径同样
         做 app_meta「锚点被删」交叉检查
- B12-5  rekey：全量重加密端到端（--generate）、新钥暂存文件、--env-only 样本
         校验拒绝错误密钥；--force 探活旁路参数存在
- B12-7  最后管理员复核下沉 db 事务：delete_user_with_accounts / set_user_role /
         batch_user_ops 命中抛 LastAdminError
- B12-8  内置主管理员自助改密即时告警
- B12-9  时钟守卫拦截留痕 app_meta（clock_guard_alert）+ clock_guard_alert() 读取
- B12-10 db_export 漏传 migrate=False（捕参数断言）+ 导出审计留痕
- B12-13 默认字面量/弱口令拒绝启动
- B12-14 登录失败阈值留痕审计链；普通用户越权 403 留痕；sign_events 消费端
         （/api/logs 当日事件、/api/admin/sign-events）

用法（项目根目录）：
    py -m pytest tests/test_batch12_fixes_0829.py -v
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))
sys.path.insert(0, os.path.join(BASE, "web"))
sys.path.insert(0, os.path.join(BASE, "docker"))

import db  # noqa: E402
import account_crypto  # noqa: E402
import signin  # noqa: E402
import scheduler  # noqa: E402  （docker/scheduler.py，容器调度器）

TEST_KEY = "c" * 64
AUDIT_KEY = "d" * 64
ADMIN_PASS = "MasterPass#2026"
EMAIL = "user1@test.local"
PHONE = "13800138001"


def _load_webapp():
    spec = importlib.util.spec_from_file_location("webapp_b12", os.path.join(BASE, "web", "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["webapp_b12"] = mod
    with contextlib.suppress(Exception):
        spec.loader.exec_module(mod)
    return mod


class SchedulerGateTest(unittest.TestCase):
    """B12-2/B12-3：容器调度闸门与动态超时。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="b12-sched-")
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        # scheduler.STATEDIR 在模块导入时固化，测试需同步指到临时目录
        cls._old_statedir = scheduler.STATEDIR
        scheduler.STATEDIR = cls.tmp
        cls._old_run_timeout = os.environ.pop("YIBAN_RUN_TIMEOUT_SEC", None)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        os.environ.pop("YIBAN_STATE_DIR", None)
        scheduler.STATEDIR = cls._old_statedir
        if cls._old_run_timeout is not None:
            os.environ["YIBAN_RUN_TIMEOUT_SEC"] = cls._old_run_timeout

    def _write_state(self, payload):
        path = os.path.join(self.tmp, f"sign-state-{scheduler.datetime.now():%Y-%m-%d}.json")
        with io.open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)

    def test_undone_statuses_include_window_skips(self):
        self.assertIn("skipped_window", scheduler._UNDONE_STATUSES)
        self.assertIn("skipped_norange", scheduler._UNDONE_STATUSES)
        self.assertIn("failed", scheduler._UNDONE_STATUSES)
        # 有意状态不算未了结
        self.assertNotIn("user_cancelled", scheduler._UNDONE_STATUSES)
        self.assertNotIn("paused", scheduler._UNDONE_STATUSES)

    def test_gate_reruns_on_all_window_skipped(self):
        """B12-2 核心场景：全员窗口外跳过 → 补签闸门必须放行。"""
        self._write_state({
            "13800000001": {"status": "skipped_window", "message": "签到时段已结束"},
            "13800000002": {"status": "skipped_norange", "message": "窗口缺失"},
        })
        self.assertTrue(scheduler._has_undone_today())

    def test_gate_skips_when_all_final(self):
        self._write_state({
            "13800000001": {"status": "success", "message": "ok"},
            "13800000002": {"status": "user_cancelled", "message": "已取消"},
        })
        self.assertFalse(scheduler._has_undone_today())

    def test_child_timeout_explicit_override(self):
        self.assertEqual(scheduler._child_timeout({"YIBAN_RUN_TIMEOUT_SEC": "1234"}), 1234)
        with mock.patch.dict(os.environ, {"YIBAN_RUN_TIMEOUT_SEC": "999"}):
            self.assertEqual(scheduler._child_timeout({}), 999)

    def test_child_timeout_dynamic_window(self):
        # 窗口在今日深夜 → 超时 = 距窗口结束 + 300（显著大于下限）
        t = scheduler._child_timeout({"YIBAN_SIGN_END": "23:59"})
        self.assertGreaterEqual(t, 600)
        # 窗口整体已过（今日 00:01 已成过去或不足下限余量）→ 回到下限 600
        self.assertEqual(scheduler._child_timeout({"YIBAN_SIGN_END": "00:01"}), 600)
        # 非法窗口回退默认 07:50，且不低于下限
        self.assertGreaterEqual(scheduler._child_timeout({"YIBAN_SIGN_END": "25:99"}), 600)


class ZeroSuccessAlertTest(unittest.TestCase):
    """B12-2：零成功专项告警。"""

    def setUp(self):
        signin._mail_summary.clear()

    def tearDown(self):
        signin._mail_summary.clear()

    def test_alerts_on_all_window_skipped(self):
        accounts = [SimpleNamespace(phone="13800000001"), SimpleNamespace(phone="13800000002")]
        results = {
            "13800000001": (False, "签到时段已结束", True, "skipped_window"),
            "13800000002": (False, "签到时间窗口缺失", True, "skipped_norange"),
        }
        self.assertTrue(signin._maybe_alert_zero_success(accounts, results, ok_n=0))
        self.assertTrue(any(s == "当日零签到告警" for s, _t in signin._mail_summary))

    def test_silent_when_any_success(self):
        accounts = [SimpleNamespace(phone="13800000001")]
        results = {"13800000001": (True, "签到成功", False, "success")}
        self.assertFalse(signin._maybe_alert_zero_success(accounts, results, ok_n=1))
        self.assertFalse(signin._mail_summary)

    def test_silent_when_deliberate_states_only(self):
        accounts = [SimpleNamespace(phone="13800000001")]
        results = {"13800000001": (False, "无需签到", True, "no_task")}
        self.assertFalse(signin._maybe_alert_zero_success(accounts, results, ok_n=0))


class SignEventWriteTest(unittest.TestCase):
    """批次12 裁决：run_queue_retry 经 event_sink 上报签到事件。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="b12-events-")
        self._old_state = os.environ.get("YIBAN_STATE_DIR")
        os.environ["YIBAN_STATE_DIR"] = self.tmp
        signin._mail_summary.clear()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        if self._old_state is not None:
            os.environ["YIBAN_STATE_DIR"] = self._old_state
        else:
            os.environ.pop("YIBAN_STATE_DIR", None)
        signin._mail_summary.clear()

    def test_run_queue_retry_emits_events(self):
        accounts = [signin.Account(phone="13800000001", password="x")]
        rows = []
        with mock.patch.object(signin, "attempt_signin", return_value=(True, "已签到", False, "already")), \
             mock.patch.object(signin, "random_delay"):
            results = signin.run_queue_retry(accounts, "", 0, 0, schedule=None, cred_state={},
                                             event_sink=rows.append)
        self.assertTrue(results["13800000001"][0])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["stage"], "sign")
        self.assertEqual(row["phone"], "13800000001")
        self.assertEqual(row["status"], "already")
        self.assertEqual(row["attempt"], 1)
        self.assertIn("finished_at", row)

    def test_run_queue_retry_without_sink_unchanged(self):
        accounts = [signin.Account(phone="13800000002", password="x")]
        with mock.patch.object(signin, "attempt_signin", return_value=(True, "已签到", False, "already")), \
             mock.patch.object(signin, "random_delay"):
            results = signin.run_queue_retry(accounts, "", 0, 0, schedule=None, cred_state={})
        self.assertTrue(results["13800000002"][0])


class DbLayerB12Test(unittest.TestCase):
    """B12-4/B12-7/B12-9/B12-10：db 层修复。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="b12-db-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with io.open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\nYIBAN_AUDIT_KEY={AUDIT_KEY}\n")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_AUDIT_KEY"] = AUDIT_KEY
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_ENV_FILE"] = cls.env_file

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_AUDIT_KEY", "YIBAN_DB_FILE", "YIBAN_ENV_FILE"):
            os.environ.pop(k, None)

    def setUp(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        db.init_db(self.db_file, env_file=self.env_file)

    # ---- B12-4 锚点路径 ----
    def test_anchor_path_default_matches_web(self):
        if os.name == "nt":
            expected = os.path.join(".", "audit-anchor.log")
        else:
            expected = "/var/log/yiban/audit-anchor.log"
        self.assertEqual(os.path.normpath(db.audit_anchor_path()), os.path.normpath(expected))
        old = os.environ.get("YIBAN_STATE_DIR")
        try:
            os.environ["YIBAN_STATE_DIR"] = "/data/state"
            self.assertEqual(
                os.path.normpath(db.audit_anchor_path()),
                os.path.normpath("/data/state/audit-anchor.log"),
            )
        finally:
            if old is None:
                os.environ.pop("YIBAN_STATE_DIR", None)
            else:
                os.environ["YIBAN_STATE_DIR"] = old

    def test_verify_anchor_meta_check_applies_to_explicit_path(self):
        """B12-4：显式路径校验同样做「锚点被删」元数据交叉检查。"""
        db.audit("tester", "login", "-", "铺底一条审计，锚点才有内容可记")
        anchor = os.path.join(self.tmp, "anchor-test.log")
        db.record_audit_anchor(anchor)
        self.assertIsNotNone(db._last_audit_anchor(anchor))
        os.remove(anchor)
        ok, msg = db.verify_audit_anchor(anchor)
        self.assertFalse(ok, "锚点文件被删但 app_meta 有留痕 → 显式路径校验也必须报异常")
        self.assertIn("疑似锚点文件被删除", msg)

    # ---- B12-7 最后管理员 ----
    def _mk_admin(self, email):
        db.create_user(email, "Secret#123")
        db.update_user(email, {"role": "admin"})

    def test_delete_last_admin_blocked_in_transaction(self):
        self._mk_admin(EMAIL)
        with self.assertRaises(db.LastAdminError):
            db.delete_user_with_accounts(EMAIL, allow_last_admin=False)
        self.assertIsNotNone(db.find_user(EMAIL), "库必须保持原状")

    def test_delete_last_admin_allowed_with_builtin(self):
        self._mk_admin(EMAIL)
        db.delete_user_with_accounts(EMAIL, allow_last_admin=True)
        self.assertIsNone(db.find_user(EMAIL))

    def test_demote_last_admin_blocked(self):
        self._mk_admin(EMAIL)
        with self.assertRaises(db.LastAdminError):
            db.set_user_role(EMAIL, "user", allow_last_admin=False)
        self.assertEqual(db.find_user(EMAIL).get("role"), "admin")

    def test_demote_last_admin_allowed_with_flag(self):
        self._mk_admin(EMAIL)
        self.assertEqual(db.set_user_role(EMAIL, "user", allow_last_admin=True), 1)
        self.assertEqual(db.find_user(EMAIL).get("role"), "user")

    def test_batch_delete_last_admin_blocked(self):
        self._mk_admin(EMAIL)
        with self.assertRaises(db.LastAdminError):
            db.batch_user_ops([("delete_user_with_accounts", EMAIL)])
        self.assertIsNotNone(db.find_user(EMAIL))
        # 内置管理员存在时的放行口径
        db.batch_user_ops([("delete_user_with_accounts", EMAIL, True)])
        self.assertIsNone(db.find_user(EMAIL))

    def test_batch_demote_last_admin_blocked(self):
        self._mk_admin(EMAIL)
        with self.assertRaises(db.LastAdminError):
            db.batch_user_ops([("update_user", EMAIL, {"role": "user"})])
        self.assertEqual(db.find_user(EMAIL).get("role"), "admin")
        db.batch_user_ops([("update_user", EMAIL, {"role": "user"}, True)])
        self.assertEqual(db.find_user(EMAIL).get("role"), "user")

    # ---- B12-9 时钟守卫 ----
    def test_clock_guard_alert_recorded_and_readable(self):
        ok, note = db._clock_jump_guard(db.get_conn(), "purge_accounts_clock")
        self.assertTrue(ok)
        # 模拟参照点为 100 小时前 → 守卫拦截并留痕
        old = (db.datetime.datetime.now() - db.datetime.timedelta(hours=100)).strftime("%Y-%m-%d %H:%M:%S")
        with db._conn_lock:
            conn = db.get_conn()
            conn.execute(
                "INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)",
                ("purge_accounts_clock", old),
            )
            conn.commit()
        ok2, note2 = db._clock_jump_guard(db.get_conn(), "purge_accounts_clock")
        self.assertFalse(ok2)
        alert = db.clock_guard_alert()
        self.assertIsNotNone(alert)
        self.assertIn("系统时间异常跳变", alert["note"])
        # 参照点未被守卫自动更新（防洗白）——仍为旧值
        r = conn.execute(
            "SELECT value FROM app_meta WHERE key='purge_accounts_clock'"
        ).fetchone()
        self.assertEqual(r["value"], old)

    def test_clock_guard_reset_tool(self):
        """B12-9：人工重置工具恢复参照点并清除告警。"""
        import clock_guard_reset
        with db._conn_lock:
            conn = db.get_conn()
            old = (db.datetime.datetime.now() - db.datetime.timedelta(hours=100)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "INSERT OR REPLACE INTO app_meta (key, value) VALUES ('purge_accounts_clock',?)",
                (old,),
            )
            conn.commit()
        db._clock_jump_guard(db.get_conn(), "purge_accounts_clock")
        self.assertIsNotNone(db.clock_guard_alert())
        clock_guard_reset.reset(self.db_file)
        self.assertIsNone(db.clock_guard_alert())
        r = db.get_conn().execute(
            "SELECT value FROM app_meta WHERE key='purge_accounts_clock'"
        ).fetchone()
        self.assertNotEqual(r["value"], old, "重置后参照点应为当前时间")

    # ---- B12-10 db_export ----
    def test_db_export_passes_migrate_false(self):
        import db_export
        out_dir = os.path.join(self.tmp, "export")
        captured = {}

        orig_init = db.init_db

        def spy_init(*a, **kw):
            captured.update(kw)
            return orig_init(*a, **kw)

        with mock.patch.object(db, "init_db", side_effect=spy_init):
            db_export.main(["--out", out_dir])
        self.assertFalse(captured.get("migrate", True), "导出工具必须禁迁移（B12-10）")
        self.assertFalse(captured.get("cleanup", True))
        self.assertTrue(os.path.exists(os.path.join(out_dir, "accounts.json")))
        # 导出留痕审计（B12-14 的一部分）
        with db._conn_lock:
            r = db.get_conn().execute(
                "SELECT COUNT(*) FROM audit_logs WHERE action='db_export'"
            ).fetchone()
        self.assertGreaterEqual(r[0], 1)


class RekeyToolB12Test(unittest.TestCase):
    """B12-5：rekey 工具端到端（--generate 全链路 + --env-only 样本校验）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="b12-rekey-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        with io.open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\nYIBAN_AUDIT_KEY={AUDIT_KEY}\n")
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_AUDIT_KEY"] = AUDIT_KEY
        db.init_db(cls.db_file, env_file=cls.env_file)
        # 用旧钥写入两个加密账号（经 replace_accounts 走正式加密口径）
        db.replace_accounts([
            {"phone": "13800000001", "password": "pw-甲", "phone_code": "code-1", "owner": EMAIL},
            {"phone": "13800000002", "password": "pw-乙", "phone_code": "", "owner": ""},
        ])

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_DB_FILE", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_KEY", "YIBAN_AUDIT_KEY"):
            os.environ.pop(k, None)

    def setUp(self):
        """每例重建规范状态（unittest 按方法名排序执行，用例不可依赖执行顺序）：
        .env = TEST_KEY，库内两行账号均以 TEST_KEY 加密。"""
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\nYIBAN_AUDIT_KEY={AUDIT_KEY}\n")
        db.replace_accounts([
            {"phone": "13800000001", "password": "pw-甲", "phone_code": "code-1", "owner": EMAIL},
            {"phone": "13800000002", "password": "pw-乙", "phone_code": "", "owner": ""},
        ])

    def _run_tool(self, *cli):
        env = dict(os.environ)
        env["YIBAN_DB_FILE"] = self.db_file
        env["YIBAN_ENV_FILE"] = self.env_file
        env["YIBAN_ACCOUNTS_KEY"] = self._current_key()
        env["YIBAN_AUDIT_KEY"] = AUDIT_KEY
        return subprocess.run(
            [sys.executable, os.path.join(BASE, "scripts", "rekey_accounts.py"), *cli],
            capture_output=True, text=True, env=env, cwd=BASE, timeout=120,
            input="n\n",
        )

    def _current_key(self):
        with io.open(self.env_file, encoding="utf-8-sig") as f:
            for ln in f:
                if ln.strip().startswith("YIBAN_ACCOUNTS_KEY="):
                    return ln.split("=", 1)[1].strip()
        return ""

    def _raw_rows(self):
        import sqlite3
        conn = sqlite3.connect(self.db_file)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(
                "SELECT id, phone, password, phone_code FROM accounts ORDER BY id"
            ).fetchall()
        finally:
            conn.close()

    def test_full_rotation_end_to_end(self):
        r = self._run_tool("--generate")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        new_key_hex = self._current_key()
        self.assertNotEqual(new_key_hex, TEST_KEY, ".env 必须已更新为新钥")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, ".env.rekey-staging")),
                         "暂存文件轮换完成后必须删除")
        key = account_crypto._decode_key(new_key_hex)
        for row in self._raw_rows():
            obj = json.loads(row["password"])
            self.assertEqual(account_crypto.decrypt_password(obj, key, row["phone"]), "pw-甲" if row["phone"] == "13800000001" else "pw-乙")
        # 轮换动作留痕审计链（B12-14）
        with db._conn_lock:
            n = db.get_conn().execute(
                "SELECT COUNT(*) FROM audit_logs WHERE action='accounts_key_rekey'"
            ).fetchone()[0]
        self.assertGreaterEqual(n, 1)

    def test_env_only_rejects_wrong_key(self):
        """崩溃补完场景误传新随机钥 → 样本校验必须拒绝写 .env（B12-5）。"""
        before = self._current_key()
        wrong = "e" * 64
        r = self._run_tool("--env-only", "--new-key", wrong)
        self.assertNotEqual(r.returncode, 0, "错误密钥必须被样本校验拒绝")
        self.assertIn("样本校验失败", r.stdout + r.stderr)
        self.assertEqual(self._current_key(), before, ".env 必须保持原状")

    def test_env_only_accepts_correct_key(self):
        """崩溃补完正路：库内已是新钥密文、.env 仍旧钥 → --env-only 用新钥补写成功。"""
        correct = self._current_key()
        # 与 .env/库一致的钥会被"新=旧"防呆拦截（不写入）
        r1 = self._run_tool("--env-only", "--new-key", correct)
        self.assertNotEqual(r1.returncode, 0)
        # 模拟崩溃场景：库内用 new_key 重加密（模拟已提交事务），.env 仍是旧钥
        new_key = "f" * 64
        import sqlite3
        conn = sqlite3.connect(self.db_file)
        try:
            rows = conn.execute("SELECT id, phone, password FROM accounts").fetchall()
            for rid, phone, _raw in rows:
                enc = json.dumps(account_crypto.encrypt_password("pw-甲", account_crypto._decode_key(new_key), phone))
                conn.execute("UPDATE accounts SET password=? WHERE id=?", (enc, rid))
            conn.commit()
        finally:
            conn.close()
        r2 = self._run_tool("--env-only", "--new-key", new_key)
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        self.assertEqual(self._current_key(), new_key, ".env 必须更新为库内实际使用的新钥")

    def test_force_flag_and_staging_helper(self):
        import rekey_accounts
        self.assertTrue(hasattr(rekey_accounts, "_write_staging_key"))
        self.assertTrue(hasattr(rekey_accounts, "sample_verify_key"))
        self.assertTrue(hasattr(rekey_accounts, "_yiban_processes_running"))
        supported, hits = rekey_accounts._yiban_processes_running()
        self.assertIsInstance(supported, bool)
        self.assertIsInstance(hits, list)
        # 暂存文件 0600 + 内容为新钥 hex
        staging = rekey_accounts._write_staging_key(self.env_file, account_crypto._decode_key("a" * 64))
        try:
            self.assertTrue(os.path.exists(staging))
            self.assertEqual(io.open(staging, encoding="utf-8").read().strip(), "a" * 64)
        finally:
            os.remove(staging)


class BackupDockerScriptTest(unittest.TestCase):
    """B12-1/B12-11：Docker 备份脚本端到端（假 gpg；无 bash 环境则跳过）。"""

    BASH = shutil.which("bash")

    FAKE_GPG = r"""#!/usr/bin/env bash
mode=""; out=""; infile=""
while [ $# -gt 0 ]; do
  case "$1" in
    --symmetric) mode=sym ;;
    --decrypt) mode=dec ;;
    -o) shift; out="$1" ;;
    -*) ;;
    *) infile="$1" ;;
  esac
  shift
done
if [ "$FAKE_GPG_FAIL" = "1" ]; then exit 9; fi
if [ "$mode" = "sym" ]; then
  if [ "$FAKE_GPG_EMPTY" = "1" ]; then : > "$out"; else cat > "$out"; fi
elif [ "$mode" = "dec" ]; then
  if [ "$out" = "-" ] || [ -z "$out" ]; then cat "$infile"; else cat "$infile" > "$out"; fi
else
  exit 2
fi
"""

    def _run_script(self, tmp, args, extra_env=None):
        fakebin = os.path.join(tmp, "fakebin")
        os.makedirs(fakebin, exist_ok=True)
        gpg = os.path.join(fakebin, "gpg")
        with io.open(gpg, "w", encoding="utf-8", newline="\n") as f:
            f.write(self.FAKE_GPG)
        env = dict(os.environ)
        env["YIBAN_BACKUP_PASSPHRASE"] = "test-pass-123"
        env["DATA_DIR"] = "data"
        env["BACKUP_DIR"] = "backups"
        env["FAKEBIN"] = fakebin
        if extra_env:
            env.update(extra_env)
        # PATH 注入假 gpg（Git Bash 需 POSIX 路径；无 cygpath 时做朴素转换）
        conv = subprocess.run([self.BASH, "-c", 'cygpath -u "$1" 2>/dev/null || echo "$1"', "_", fakebin],
                              capture_output=True, text=True)
        posix_fakebin = conv.stdout.strip() or fakebin
        env["PATH"] = posix_fakebin + os.pathsep + env.get("PATH", "")
        script = os.path.join(BASE, "docker", "backup-docker.sh")
        return subprocess.run([self.BASH, script] + args, capture_output=True,
                              text=True, env=env, cwd=tmp, timeout=120)

    @unittest.skipIf(shutil.which("bash") is None, "需要 bash（Git Bash/WSL）")
    def test_backup_produces_real_archive(self):
        tmp = tempfile.mkdtemp(prefix="b12-bak-")
        try:
            data = os.path.join(tmp, "data")
            os.makedirs(data)
            # 不可压缩随机内容：排除 gzip 把大文件压小于尺寸下限的假阳性
            with open(os.path.join(data, "yiban.db"), "wb") as f:
                f.write(os.urandom(8192))
            r = self._run_script(tmp, [])
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            out = os.path.join(tmp, "backups")
            products = [n for n in os.listdir(out) if n.endswith(".tar.gz.gpg")]
            self.assertEqual(len(products), 1, "必须且只能产出 1 个备份包")
            size = os.path.getsize(os.path.join(out, products[0]))
            self.assertGreater(size, 200, "备份包必须超过空包尺寸（B12-1 核心）")
            self.assertTrue(os.path.exists(os.path.join(out, products[0] + ".sha256")))
            self.assertIn("自检", r.stdout, "必须完成解密自检")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @unittest.skipIf(shutil.which("bash") is None, "需要 bash（Git Bash/WSL）")
    def test_backup_rejects_empty_product(self):
        tmp = tempfile.mkdtemp(prefix="b12-bak-empty-")
        try:
            os.makedirs(os.path.join(tmp, "data"))
            with io.open(os.path.join(tmp, "data", "f.txt"), "w", encoding="utf-8") as f:
                f.write("x")
            r = self._run_script(tmp, [], extra_env={"FAKE_GPG_EMPTY": "1"})
            self.assertNotEqual(r.returncode, 0, "空包必须被判失败")
            self.assertIn("疑似空包", r.stdout + r.stderr)
            out = os.path.join(tmp, "backups")
            leftovers = [n for n in os.listdir(out) if n.endswith(".gpg")] if os.path.isdir(out) else []
            self.assertEqual(leftovers, [], "失败产物必须删除，不留坏包")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @unittest.skipIf(shutil.which("bash") is None, "需要 bash（Git Bash/WSL）")
    def test_restore_rejects_path_traversal(self):
        tmp = tempfile.mkdtemp(prefix="b12-restore-")
        try:
            evil = tarfile.open(os.path.join(tmp, "evil.tar.gz"), "w:gz")
            info = tarfile.TarInfo("../evil.txt")
            payload = b"pwned"
            import tarfile as _tf
            info.size = len(payload)
            import io as _io
            evil.addfile(info, _io.BytesIO(payload))
            evil.close()
            r = self._run_script(tmp, ["--restore", "evil.tar.gz", "restored"])
            self.assertNotEqual(r.returncode, 0, "含穿越条目的备份包必须被拒绝")
            self.assertIn("路径穿越", r.stdout + r.stderr)
            self.assertFalse(os.path.exists(os.path.join(tmp, "evil.txt")), "不得写出目标目录")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @unittest.skipIf(shutil.which("bash") is None, "需要 bash（Git Bash/WSL）")
    def test_restore_accepts_clean_archive(self):
        tmp = tempfile.mkdtemp(prefix="b12-restore-ok-")
        try:
            data = os.path.join(tmp, "src", "data")
            os.makedirs(data)
            with io.open(os.path.join(data, "f.txt"), "w", encoding="utf-8") as f:
                f.write("hello")
            with tarfile.open(os.path.join(tmp, "good.tar.gz"), "w:gz") as t:
                t.add(os.path.join(tmp, "src"), arcname=".")
            r = self._run_script(tmp, ["--restore", "good.tar.gz", "restored"])
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertTrue(os.path.exists(os.path.join(tmp, "restored", "data", "f.txt")))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class WebB12Test(unittest.TestCase):
    """B12-8/B12-13/B12-14 + sign_events 消费端（web 层）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="b12-web-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls._env_content = (
            f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
            f"YIBAN_AUDIT_KEY={AUDIT_KEY}\n"
            "YIBAN_ADMIN_USER=admin@test.local\n"
            f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
        )
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_AUDIT_KEY"] = AUDIT_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = os.path.join(cls.tmp, "state")
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        cls.webapp = _load_webapp()

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_AUDIT_KEY", "YIBAN_LOG_FILE",
                  "YIBAN_STATE_DIR", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE", "YIBAN_DB_FILE"):
            os.environ.pop(k, None)

    def setUp(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        with io.open(self.accounts_file, "w", encoding="utf-8") as f:
            json.dump([], f)
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write(self._env_content)
        db.init_db(self.db_file, migrate_from=self.accounts_file, env_file=self.env_file)
        os.makedirs(os.path.join(self.tmp, "state"), exist_ok=True)
        self.app = self.webapp.create_app()
        self.c = self.app.test_client()

    # ---- 工具 ----
    def _csrf(self, c):
        return {"X-CSRF-Token": c.get("/api/me").get_json()["csrf_token"]}

    def _login_admin(self):
        r = self.c.post("/api/login", json={"username": "admin@test.local", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def _register_user(self, email, password="UserPass#123"):
        r = self.c.post("/api/register", json={"email": email, "password": password, "agree": True})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r

    def _audit_rows(self, action):
        with db._conn_lock:
            conn = db.get_conn()
            return conn.execute(
                "SELECT username, action, target, detail FROM audit_logs WHERE action=? ORDER BY id",
                (action,),
            ).fetchall()

    # ---- B12-13 默认口令拒绝启动 ----
    def test_default_literal_password_rejected(self):
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write(self._env_content.replace(ADMIN_PASS, "请修改为强密码"))
        with self.assertRaises(SystemExit):
            self.webapp.create_app()

    def test_weak_password_rejected(self):
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write(self._env_content.replace(ADMIN_PASS, "short1"))
        with self.assertRaises(SystemExit):
            self.webapp.create_app()

    def test_strong_password_boots(self):
        self.app  # setUp 已用强口令创建成功

    # ---- B12-8 内置管理员改密告警 ----
    def test_builtin_admin_password_change_alerts(self):
        self._login_admin()
        with mock.patch.object(self.webapp, "send_notification") as notify:
            r = self.c.post(
                "/api/me/password",
                json={"old_password": ADMIN_PASS, "new_password": "Rotated#2026", "confirm_password": "Rotated#2026"},
                headers=self._csrf(self.c),
            )
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            self.assertTrue(notify.called, "主管理员改密必须触发即时告警（B12-8）")
            subjects = [call.args[0] for call in notify.call_args_list]
            self.assertIn("账号安全事件告警", subjects)
        # 还原口令（写回 .env 供后续用例登录）
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write(self._env_content)
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None

    # ---- B12-14 登录失败 / 越权审计 ----
    def test_login_failure_threshold_audited(self):
        for _ in range(3):  # LOGIN_FAIL_NOTIFY = 3
            self.c.post("/api/login", json={"username": EMAIL, "password": "wrong-pass"})
        rows = self._audit_rows("login_failed")
        self.assertGreaterEqual(len(rows), 1, "达到失败阈值必须留痕审计链（B12-14）")
        self.assertNotIn("@", rows[-1]["target"], "IP 必须 hash_ip 匿名化")

    def test_forbidden_path_audited(self):
        self._register_user("f403@test.local")
        r = self.c.post("/api/login", json={"username": "f403@test.local", "password": "UserPass#123"})
        self.assertEqual(r.status_code, 200)
        resp = self.c.get("/api/users")
        self.assertEqual(resp.status_code, 403)
        rows = self._audit_rows("forbidden_path")
        self.assertGreaterEqual(len(rows), 1, "越权访问管理面必须留痕（B12-14）")
        self.assertEqual(rows[-1]["detail"], "/api/users")

    # ---- sign_events 消费端 ----
    def test_logs_api_exposes_sign_events(self):
        ts = db.datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db.add_sign_events_batch([{
            "ts": ts, "phone": PHONE, "status": "failed", "message": "登录失败",
            "stage": "sign", "attempt": 2, "dur_sec": 1.5, "finished_at": ts,
        }])
        self._login_admin()
        data = self.c.get("/api/logs").get_json()
        self.assertEqual(len(data.get("sign_events") or []), 1)
        ev = data["sign_events"][0]
        self.assertNotEqual(ev["phone"], PHONE, "手机号必须脱敏")
        self.assertIn("*", ev["phone"])
        self.assertEqual(ev["attempt"], 2)

    def test_admin_sign_events_endpoint(self):
        ts = db.datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db.add_sign_events_batch([{
            "ts": ts, "phone": PHONE, "status": "success", "message": "签到成功",
            "stage": "sign", "attempt": 1, "finished_at": ts,
        }])
        self._login_admin()
        data = self.c.get("/api/admin/sign-events?days=7").get_json()
        self.assertEqual(data["ok"], True)
        self.assertGreaterEqual(data["count"], 1)
        self.assertGreaterEqual(len(data.get("daily_stats") or []), 1)
        # 普通用户访问 → 403（require_login 白名单外）
        self._register_user("evu@test.local")
        self.c.post("/api/login", json={"username": "evu@test.local", "password": "UserPass#123"})
        resp = self.c.get("/api/admin/sign-events")
        self.assertEqual(resp.status_code, 403)


if __name__ == "__main__":
    unittest.main()
