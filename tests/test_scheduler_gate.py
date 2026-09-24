# -*- coding: utf-8 -*-
"""修复回归测试（对抗性审查 2026-08-29）。

覆盖：
- B12-1  docker/backup-docker.sh 产出真实加密备份（herestring 截断管道的空备份
         修复）+ 尺寸下限/解密自检（假 gpg 端到端，需本机 bash；无则跳过）
- B12-2  调度闸门把 skipped_window/skipped_norange 计入未了结（补签不再被吞）；
         零成功专项告警 _maybe_alert_zero_success
- B12-3  容器子进程超时按窗口动态计算（_child_timeout，与 run.sh 同口径）
- B12-4  锚点路径默认与 web STATE_DIR 对齐；verify_audit_anchor 显式路径同样
         做 app_meta「锚点被删」交叉检查
- B12-7  最后管理员复核下沉 db 事务：delete_user_with_accounts / set_user_role /
         batch_user_ops 命中抛 LastAdminError
- B12-8  内置主管理员自助改密即时告警
- B12-9  时钟守卫：跳变时拦截清理并把参照点推进到当前时间（下一轮自动恢复）
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
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

import signin

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import db  # noqa: E402
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
    """窗口外未了结专项告警。"""

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
        self.assertTrue(any(s == "当日签到异常告警" for s, _t in signin._mail_summary))

    def test_silent_when_any_success_first_run(self):
        """首签轮（is_second_run=False）部分成功+窗口外跳过：补签轮会重跑，不打扰。

        抑制仅在补签触发点之前成立——已越过触发点的首签身份轮是当天最后一轮，
        仍告警（见 test_sign_round_guards.LateFirstRunAlertTest）。
        注入补签触发点之前的固定时钟，用例不随运行时刻漂移。
        """
        from datetime import datetime as _dt

        class _EarlyDT(_dt):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 9, 8, 6, 50)

        accounts = [SimpleNamespace(phone="13800000001"), SimpleNamespace(phone="13800000002")]
        results = {
            "13800000001": (True, "签到成功", False, "success"),
            "13800000002": (False, "签到时段已结束", True, "skipped_window"),
        }
        with mock.patch.object(signin.clock, "now", _EarlyDT.now):
            self.assertFalse(signin._maybe_alert_zero_success(
                accounts, results, ok_n=1, is_second_run=False))
        self.assertFalse(signin._mail_summary)

    def test_alerts_on_mixed_second_run(self):
        """补签轮（is_second_run=True）仍有窗口外未了结：当天无下一触发点，必须知情。"""
        accounts = [SimpleNamespace(phone="13800000001"), SimpleNamespace(phone="13800000002")]
        results = {
            "13800000001": (True, "签到成功", False, "success"),
            "13800000002": (False, "签到时段已结束", True, "skipped_window"),
        }
        self.assertTrue(signin._maybe_alert_zero_success(
            accounts, results, ok_n=1, is_second_run=True))
        self.assertTrue(any(s == "签到窗口异常告警" for s, _t in signin._mail_summary))

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
    """run_queue_retry 经 event_sink 上报签到事件。"""

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
        with mock.patch.object(signin, "attempt_signin", return_value=(True, "已签到", False, "already")):
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
        with mock.patch.object(signin, "attempt_signin", return_value=(True, "已签到", False, "already")):
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

    def test_anchor_path_honours_state_dir_from_env_file(self):
        """YIBAN_STATE_DIR 只写进 .env（进程环境没有）时读侧同样生效。

        写侧（web 的 STATE_DIR / run.sh）认 .env，读侧若只认 `os.environ` 就会落在
        默认目录：审计锚点被写到别处而校验读默认目录，锚点防线致盲且 audit_verify
        以退出码 1 误报链破。
        """
        old = os.environ.pop("YIBAN_STATE_DIR", None)
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\nYIBAN_AUDIT_KEY={AUDIT_KEY}\n"
                    f"YIBAN_STATE_DIR={self.tmp}\n")
        try:
            self.assertEqual(
                os.path.normpath(db.audit_anchor_path()),
                os.path.normpath(os.path.join(self.tmp, "audit-anchor.log")),
                "只写在 .env 的 YIBAN_STATE_DIR 必须被锚点路径解析看到")
        finally:
            if old is not None:
                os.environ["YIBAN_STATE_DIR"] = old
            with io.open(self.env_file, "w", encoding="utf-8") as f:
                f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\nYIBAN_AUDIT_KEY={AUDIT_KEY}\n")

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
    def test_clock_guard_trip_skips_once_and_advances_reference(self):
        """跳变判定为假 + 参照点推进到当前时间（下一轮自动恢复清理，无需人工重置）。"""
        ok, _note = db._clock_jump_guard(db.get_conn(), "purge_accounts_clock")
        self.assertTrue(ok)
        # 模拟参照点为 100 小时前 → 守卫判定跳变
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
        self.assertIn("系统时间异常跳变", note2)
        # 参照点已推进：越界路径同样提交，故调用方的 rollback 不会把它带走
        r = conn.execute(
            "SELECT value FROM app_meta WHERE key='purge_accounts_clock'"
        ).fetchone()
        self.assertNotEqual(r["value"], old, "越界路径必须推进参照点，否则冻结永不解除")
        # 推进后同一参照点不再判跳变——这就是"只跳一轮"
        ok3, _note3 = db._clock_jump_guard(db.get_conn(), "purge_accounts_clock")
        self.assertTrue(ok3, "参照点推进后下一轮必须恢复清理")

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
        # 2026-09-01 CI 修复：必须 chmod +x——Linux 上 bash 的 `command -v gpg`
        # 会跳过不可执行文件，落到系统真 gpg（runner 预装），真 gpg 用错误口令
        # 解密 → "解密失败"。Windows Git Bash 权限模拟宽松，此前侥幸通过。
        os.chmod(gpg, 0o755)
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
        return subprocess.run([self.BASH, script, *args], capture_output=True,
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

    def test_admin_policy_12_chars_two_classes_rejected(self):
        # 2026-09-05 主管理员口令提档：12 位但仅两类仍拒绝（普通用户口径不够用）
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write(self._env_content.replace(ADMIN_PASS, "abcdefgh1234"))
        with self.assertRaises(SystemExit):
            self.webapp.create_app()

    def test_strong_password_boots(self):
        self.assertTrue(self.app is not None)  # setUp 已用强口令创建成功

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
        # 阈值取 app 常量：另抄字面量会在阈值调整后"再也到不了阈值"而静默失测
        for _ in range(self.webapp.LOGIN_FAIL_NOTIFY):
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


_SCHED_PATH = os.path.join(BASE, "docker", "scheduler.py")


def _load_sched():
    """按文件路径加载调度器（它位于 docker/ 而非包内，与 test_container_scheduler 同法）。"""
    spec = importlib.util.spec_from_file_location("container_scheduler", _SCHED_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Acc(SimpleNamespace):
    pass


def _mk_acc(phone):
    return _Acc(phone=phone, name="t", owner="admin", user_paused=False)


class OnlyFilterTest(unittest.TestCase):
    """P2-6：--only 部分命中不再静默吞号。"""

    def test_partial_match_logs_missing_warning(self):
        accounts = [_mk_acc("13800000001"), _mk_acc("13800000002")]
        with self.assertLogs(signin.logger, level="WARNING") as cm:
            filtered, missing = signin._apply_only_filter(
                accounts, "13800000001,13899999999"
            )
        self.assertEqual([a.phone for a in filtered], ["13800000001"])
        self.assertEqual(missing, ["13899999999"])
        # 未命中号码落日志即脱敏（裸号不带 [] 定界符，web 展示层脱敏正则盖不住）
        self.assertTrue(
            any("138****9999" in line for line in cm.output),
            f"warning 日志应包含未命中号码的脱敏形态，实际: {cm.output}",
        )
        self.assertFalse(
            any("13899999999" in line for line in cm.output),
            f"warning 日志不得包含未命中号码完整号，实际: {cm.output}",
        )

    def test_all_missing_returns_empty(self):
        """全不命中 → 过滤结果为空（main() 据此报错退出，既有行为保持）。"""
        accounts = [_mk_acc("13800000001")]
        filtered, missing = signin._apply_only_filter(accounts, "13900000000")
        self.assertEqual(filtered, [])
        self.assertEqual(missing, ["13900000000"])

    def test_all_match_no_warning(self):
        """全部命中 → 无 warning。"""
        accounts = [_mk_acc("13800000001"), _mk_acc("13800000002")]
        with mock.patch.object(signin.logger, "warning") as m_warn:
            filtered, missing = signin._apply_only_filter(
                accounts, "13800000001, 13800000002"
            )
        self.assertEqual(len(filtered), 2)
        self.assertEqual(missing, [])
        m_warn.assert_not_called()

    def test_whitespace_only_ignored(self):
        """逗号间空白/空段不产生未命中告警。"""
        accounts = [_mk_acc("13800000001")]
        filtered, missing = signin._apply_only_filter(accounts, "13800000001,, ")
        self.assertEqual([a.phone for a in filtered], ["13800000001"])
        self.assertEqual(missing, [])


class SecondRunEnvTest(unittest.TestCase):
    """P2-4：YIBAN_SECOND_RUN=1 优先于 sched-run 标记判定补签轮。"""

    def setUp(self):
        self._old = os.environ.get("YIBAN_SECOND_RUN")
        os.environ.pop("YIBAN_SECOND_RUN", None)

    def tearDown(self):
        os.environ.pop("YIBAN_SECOND_RUN", None)
        if self._old is not None:
            os.environ["YIBAN_SECOND_RUN"] = self._old

    def test_env_overrides_missing_marker(self):
        """核心场景：标记缺失（exit 124 首签被杀）+ YIBAN_SECOND_RUN=1 → 判为补签轮。"""
        with mock.patch.object(signin, "_sched_marker_exists", return_value=False), \
             mock.patch.dict(os.environ, {"YIBAN_SECOND_RUN": "1"}):
            self.assertTrue(signin._is_second_run())

    def test_marker_fallback_without_env(self):
        """无环境变量时回退 sched-run 标记（容器首签正常收尾场景）。"""
        with mock.patch.object(signin, "_sched_marker_exists", return_value=True):
            self.assertTrue(signin._is_second_run())

    def test_first_run_when_no_signal(self):
        """既无环境变量也无标记 → 首签轮。"""
        with mock.patch.object(signin, "_sched_marker_exists", return_value=False):
            self.assertFalse(signin._is_second_run())

    def test_alert_on_partial_success_window_skip_second_run(self):
        """部分成功 + 窗口外：补签轮（YIBAN_SECOND_RUN=1，标记缺失）必须告警。"""
        accounts = [_mk_acc("13800000001"), _mk_acc("13800000002")]
        results = {
            "13800000001": (True, "签到成功", False, signin.STATUS_SUCCESS),
            "13800000002": (False, "签到时段已结束", True, signin.STATUS_SKIPPED_WINDOW),
        }
        with mock.patch.object(signin, "_collect_admin_mail") as m_mail, \
             mock.patch.object(signin, "_sched_marker_exists", return_value=False), \
             mock.patch.dict(os.environ, {"YIBAN_SECOND_RUN": "1"}):
            is_second = signin._is_second_run()
            alerted = signin._maybe_alert_zero_success(
                accounts, results, 1, is_second_run=is_second
            )
        self.assertTrue(is_second, "YIBAN_SECOND_RUN=1 时应判为补签轮")
        self.assertTrue(alerted, "补签轮部分成功+窗口外必须告警")
        m_mail.assert_called_once()

    def test_no_alert_partial_success_first_run(self):
        """部分成功 + 窗口外：补签触发点之前的首签轮不告警（避免误报噪音）。

        抑制语义依赖「07:10 补签会重跑」：已越过补签触发点（07:10）的首签身份轮
        是当天最后一轮（06:31 关机 07:10 起的场景），仍会告警——
        见 test_sign_round_guards.LateFirstRunAlertTest。此处注入补签触发点
        之前的固定时钟，用例不再随运行时刻漂移。
        """
        from datetime import datetime as _dt

        class _EarlyDT(_dt):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 9, 8, 6, 50)

        accounts = [_mk_acc("13800000001"), _mk_acc("13800000002")]
        results = {
            "13800000001": (True, "签到成功", False, signin.STATUS_SUCCESS),
            "13800000002": (False, "签到时段已结束", True, signin.STATUS_SKIPPED_WINDOW),
        }
        with mock.patch.object(signin, "_collect_admin_mail") as m_mail, \
             mock.patch.object(signin, "_sched_marker_exists", return_value=False), \
             mock.patch.object(signin.clock, "now", _EarlyDT.now):
            is_second = signin._is_second_run()
            alerted = signin._maybe_alert_zero_success(
                accounts, results, 1, is_second_run=is_second
            )
        self.assertFalse(is_second)
        self.assertFalse(alerted)
        m_mail.assert_not_called()


class ChildTimeoutPrecedenceTest(unittest.TestCase):
    """P2-5：docker/scheduler.py `_child_timeout` 键优先级 .env 优先于进程环境。"""

    def setUp(self):
        self.sched = _load_sched()
        self._old = os.environ.get("YIBAN_RUN_TIMEOUT_SEC")
        os.environ.pop("YIBAN_RUN_TIMEOUT_SEC", None)

    def tearDown(self):
        os.environ.pop("YIBAN_RUN_TIMEOUT_SEC", None)
        if self._old is not None:
            os.environ["YIBAN_RUN_TIMEOUT_SEC"] = self._old

    def test_env_file_wins_over_process_env(self):
        """.env（env dict）900s 优先于进程环境 500s → 900。"""
        os.environ["YIBAN_RUN_TIMEOUT_SEC"] = "500"
        self.assertEqual(self.sched._child_timeout({"YIBAN_RUN_TIMEOUT_SEC": "900"}), 900)

    def test_process_env_used_when_env_file_empty(self):
        """.env 未设置时回退进程环境（500 低于下限 600 → 钳到 600）。"""
        os.environ["YIBAN_RUN_TIMEOUT_SEC"] = "500"
        self.assertEqual(self.sched._child_timeout({}), 600)

    def test_process_env_value_above_floor(self):
        """进程环境 800s 正常生效（>= 下限）。"""
        os.environ["YIBAN_RUN_TIMEOUT_SEC"] = "800"
        self.assertEqual(self.sched._child_timeout({}), 800)

    def test_both_absent_dynamic(self):
        """均未设置 → 按窗口动态计算（>= 下限 600）。"""
        self.assertGreaterEqual(self.sched._child_timeout({}), 600)


_SCHED_PATH = os.path.join(BASE, "docker", "scheduler.py")


def _load_sched(unique_suffix=""):
    """按文件路径加载容器调度器（docker/ 非包内），每次调用返回全新模块实例。"""
    spec = importlib.util.spec_from_file_location(
        f"container_scheduler_marks{unique_suffix}", _SCHED_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Stop(Exception):
    """打断 main_loop 的无限循环。"""


def _today():
    return datetime.now().strftime("%Y-%m-%d")


class _FakeDT(datetime):
    """时钟替身：now() 返回固定时刻（被 patch 到 `scheduler.clock.now`）。"""

    _date = (2026, 9, 6)
    _hm = (7, 12)

    @classmethod
    def now(cls, tz=None):
        return cls(*cls._date, *cls._hm)


class _FakeProc:
    def __init__(self):
        self.returncode = 0

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass


def _stub_module(**members):
    return type("_Stub", (), {k: staticmethod(v) for k, v in members.items()})()


class SlotMarkerRestartTest(unittest.TestCase):
    """hm >= FIRST/SECOND 无上界 + 闩锁仅存内存：容器重启会追加必然
    skipped_window 的全站负载。触发点落盘后，同一时段重启不再二次触发。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-slot-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _prepare(self, sched):
        sched.STATEDIR = self.tmp
        sched.ENV_FILE = os.path.join(self.tmp, ".env")
        sched.FIRST = (0, 0)
        sched.SECOND = (0, 0)
        sched.LOGDIR = os.path.join(self.tmp, "logs")
        sched.build_child_env = lambda env_file=None, base=None, **kw: {}
        sched.subprocess = _stub_module(Popen=lambda cmd, **kw: _FakeProc())
        sched.time = _stub_module(sleep=lambda s: (_ for _ in ()).throw(_Stop()))

    def _tick(self, suffix):
        sched = _load_sched(suffix)
        self._prepare(sched)
        spawns = []
        sched._run_signin_child = lambda extra=None, env=None: spawns.append(extra)
        with self.assertRaises(_Stop):
            sched.main_loop(sleep_seconds=1)
        return sched, spawns

    def test_restart_does_not_retrigger_same_slot(self):
        """首轮正常跑完后容器重启（新进程、当日已触发过）：不得再追加全站轮。"""
        _, first = self._tick("_p1")
        self.assertEqual(len(first), 2, "首签+补签各触发一次")
        # 模拟运行结束后的当日状态：全量标记 + 一个 failed 账号（补签闸门恒真）
        with io.open(os.path.join(self.tmp, f"sched-run-{_today()}.json"), "w") as f:
            json.dump({"completed": True}, f)
        with io.open(os.path.join(self.tmp, f"sign-state-{_today()}.json"), "w") as f:
            json.dump({"13800000000": {"status": "failed"}}, f)
        _, second = self._tick("_p2")
        self.assertEqual(
            second, [],
            "同一时段重启后不得二次触发（追加轮=必然 skipped_window 的全站负载）",
        )

    def test_unfinished_first_run_not_respawned_after_restart(self):
        """首签子进程被杀后重启：首签槽位当日已触发不重跑；
        未了结账号仍由 07:10 补签闸门兜底（补签槽位未触发）。"""
        _, first = self._tick("_q1")
        self.assertEqual(len(first), 2)
        # 不写 sched-run 标记（模拟首签被 timeout 击杀）
        with io.open(os.path.join(self.tmp, f"sign-state-{_today()}.json"), "w") as f:
            json.dump({"13800000000": {"status": "failed"}}, f)
        sched2, second = self._tick("_q2")
        self.assertEqual(second, [], "首签槽位当日已触发过，不得重跑")
        # 补签闸门仍可判定为需要补签（谓词本身不受影响）
        self.assertTrue(sched2._has_undone_today())

    def test_next_day_slot_marker_expires(self):
        """标记按日命名：跨日自动失效（次日仍可正常触发）。"""
        self._tick("_r1")
        files = [n for n in os.listdir(self.tmp) if n.startswith("sched-slot-")]
        self.assertEqual(len(files), 2, "首签/补签各写一个当日槽位标记")
        for n in files:
            self.assertIn(_today(), n, "标记须按日命名，跨日失效")


class ChildTimeoutBoundTest(unittest.TestCase):
    """晚到触发的重跑进程：动态超时恒大于「距窗口关闭的剩余时间」，
    子进程总能在窗口关闭时自了结（剩余账号 skipped_window 后退出），
    600s 下限只在窗口已关闭时生效——彼时剩余合法工作≈0，截断无害。"""

    def test_timeout_always_exceeds_time_to_window_close(self):
        sched = _load_sched("_t")
        env = {"YIBAN_SIGN_END": "07:50"}
        for hm in ((6, 31), (7, 0), (7, 10), (7, 40), (7, 49), (8, 0)):
            fake = type(f"_DT{hm[0]}{hm[1]}", (_FakeDT,), {"_hm": hm})
            with mock.patch.object(sched.clock, "now", fake.now):
                timeout = sched._child_timeout(env)
            now = datetime.now().replace(year=2026, month=9, day=6,
                                         hour=hm[0], minute=hm[1], second=0)
            end = now.replace(hour=7, minute=50)
            remaining = max(0, (end - now).total_seconds())
            self.assertGreater(
                timeout, remaining,
                f"{hm[0]:02d}:{hm[1]:02d} 触发的重跑必须能活到窗口关闭自行了结",
            )

    def test_late_trigger_floor_is_600(self):
        """窗口已关闭的晚到触发：下限 600s，此时子进程即刻全员窗口外跳过退出。"""
        sched = _load_sched("_u")
        fake = type("_DTLate", (_FakeDT,), {"_hm": (9, 0)})
        with mock.patch.object(sched.clock, "now", fake.now):
            self.assertEqual(sched._child_timeout({"YIBAN_SIGN_END": "07:50"}), 600)


RUN_SH = os.path.join(BASE, "run.sh")


def _today():
    return datetime.now().strftime("%Y-%m-%d")


FAKE_FLOCK = "#!/usr/bin/env bash\nexit ${FAKE_FLOCK_EXIT:-0}\n"


FAKE_TIMEOUT = r"""#!/usr/bin/env bash
{
  echo "SECOND_RUN=${YIBAN_SECOND_RUN-UNSET}"
  echo "TIMEOUT_ARG=$1"
} >> "$FAKE_TIMEOUT_LOG"
exit ${FAKE_TIMEOUT_EXIT:-0}
"""


class WindowDegradeMailTest(unittest.TestCase):
    """缓冲过大（前后裁剪合计 >= 窗口宽度）→ 收缩缓冲 + 一次性管理员汇总邮件。"""

    def setUp(self):
        signin._window_clamped_notified = False
        signin._window_fallback_notified = False
        signin._mail_summary.clear()

    def tearDown(self):
        signin._window_clamped_notified = False
        signin._window_fallback_notified = False
        signin._mail_summary.clear()

    @staticmethod
    def _overflow_cfg():
        # 06:30~06:40 共 10 分钟窗口，前后各裁 5 分钟 → 缓冲合计吃满窗口
        return {
            "sign_start": (6, 30),
            "sign_end": (6, 40),
            "edge_front_sec": 300,
            "edge_back_sec": 300,
        }

    def test_overflow_edges_collects_admin_mail_once(self):
        cfg = self._overflow_cfg()
        with mock.patch.object(signin, "_collect_admin_mail") as m_mail:
            blocks1, lo1, hi1 = signin._schedule_blocks(dict(cfg))
            blocks2, lo2, hi2 = signin._schedule_blocks(dict(cfg))
        m_mail.assert_called_once()  # 去重标记必须保证多轮调用只收集一次
        title, text = m_mail.call_args[0]
        self.assertEqual(title, "签到窗口缓冲已收缩")
        self.assertIn("06:30~06:40", text)
        self.assertIn("300", text)
        self.assertIn("60", text)
        # 窗口保留、缓冲收缩为各 60s：06:30+60s ~ 06:40-60s
        self.assertEqual((lo1, hi1), (391.0, 399.0))
        self.assertEqual((lo2, hi2), (391.0, 399.0))
        self.assertTrue(blocks1, "收缩后必须产出非空块列表")
        self.assertEqual(len(blocks1), len(blocks2))

    def test_normal_window_no_mail(self):
        cfg = {
            "sign_start": (6, 30),
            "sign_end": (7, 50),
            "edge_front_sec": 60,
            "edge_back_sec": 60,
        }
        with mock.patch.object(signin, "_collect_admin_mail") as m_mail:
            blocks, lo, hi = signin._schedule_blocks(cfg)
        m_mail.assert_not_called()
        self.assertTrue(blocks)
        self.assertEqual((lo, hi), (391.0, 469.0))


if __name__ == "__main__":
    unittest.main()
