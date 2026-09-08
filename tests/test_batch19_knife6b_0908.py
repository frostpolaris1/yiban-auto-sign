# -*- coding: utf-8 -*-
"""签到/调度/Web 业务逻辑缺陷修复回归（2026-09-08）。

逐项活体复现 + 修复钉版：
1. 补签轮只重跑未了结账号（当日已 success/already 不再二次登录）
2. 一键暂停/周末签到关闭期间探针跳过（门在探针分支内部判定）
3. 仅凭据（密码/手机号）实际变更才清熔断计数，只改备注不再重置 fail_days
4. 死号（自取消/熔断暂停）先判后睡：不再先睡满账号间隔/时段槽位再跳过
5. 手动签到子进程非 0 退出码透传到签到日志（用户可见真实失败原因）
6. SIGTERM 超时终止前冲刷已收集的管理员告警汇总（汇总不随进程死亡）
7. 疑似首签轮但已过补签触发点：部分成功+窗口外仍告警（无第三次兜底）
8. 容器调度触发点落盘标记：同一时段重启后不二次触发
9. _child_timeout 动态下限不截断晚到重跑的自了结（钉版证明）
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import signal
import sys
import tempfile
import time
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import signin  # noqa: E402
import db  # noqa: E402

TEST_KEY = "a" * 64
ADMIN_PASS = "MasterPass#2026"
USER_PASS = "secret1"

_SCHED_PATH = os.path.join(BASE, "docker", "scheduler.py")


def _load_sched(unique_suffix=""):
    """按文件路径加载容器调度器（docker/ 非包内），每次调用返回全新模块实例。"""
    spec = importlib.util.spec_from_file_location(
        f"container_scheduler_knife6b{unique_suffix}", _SCHED_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Stop(Exception):
    """打断 main_loop 的无限循环。"""


def _today():
    return datetime.now().strftime("%Y-%m-%d")


class _FakeDT(datetime):
    """signin.datetime 替身：now() 返回固定时刻。"""

    _date = (2026, 9, 6)
    _hm = (7, 12)

    @classmethod
    def now(cls, tz=None):
        return cls(*cls._date, *cls._hm)


# ---------------------------------------------------------------------------
# 1. 补签轮定向重跑
# ---------------------------------------------------------------------------
class SecondRunFilterTest(unittest.TestCase):
    """补签轮（YIBAN_SECOND_RUN=1）只重跑未了结账号；全部了结时静默结束。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-second-run-")
        self._old_env = {
            k: os.environ.get(k) for k in (
                "YIBAN_SECOND_RUN", "YIBAN_ACCOUNTS_JSON", "YIBAN_DB_FILE",
                "YIBAN_STATE_DIR", "YIBAN_LOG_FILE", "YIBAN_GLOBAL_PAUSE",
            )
        }
        self._old_argv = sys.argv[:]
        os.environ["YIBAN_DB_FILE"] = os.path.join(self.tmp, "yiban.db")
        os.environ["YIBAN_STATE_DIR"] = self.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(self.tmp, "sign.log")
        os.environ.pop("YIBAN_GLOBAL_PAUSE", None)

    def tearDown(self):
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        sys.argv = self._old_argv
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_state(self, statuses):
        path = os.path.join(self.tmp, f"sign-state-{_today()}.json")
        with io.open(path, "w", encoding="utf-8") as f:
            json.dump(
                {p: {"status": s, "message": "", "time": "07:00:00", "task": "default"}
                 for p, s in statuses.items()}, f)

    def _run_main(self, argv=None, accounts=None):
        """执行 signin.main()，返回 (退出码, run_queue_retry 收到的账号列表)。"""
        seen = []
        sys.argv = ["signin.py"] + (argv or [])
        with mock.patch.object(signin, "load_accounts", return_value=accounts), \
             mock.patch.object(signin, "build_schedule", return_value={}), \
             mock.patch.object(signin, "run_queue_retry",
                               side_effect=lambda accs, *a, **kw: seen.append(list(accs)) or {}), \
             mock.patch.object(signin, "_save_cred_state"):
            try:
                signin.main()
                code = 0
            except SystemExit as e:
                code = e.code
        return code, (seen[0] if seen else None)

    def _accounts(self):
        return [
            mock.Mock(phone="13800000001", user_paused=False),
            mock.Mock(phone="13800000002", user_paused=False),
        ]

    def test_second_run_reruns_only_undone_accounts(self):
        """补签轮：当日已 success 的账号被剔除，仅剩 failed 账号进入重跑队列。"""
        self._write_state({"13800000001": "success", "13800000002": "failed"})
        os.environ["YIBAN_SECOND_RUN"] = "1"
        code, rerun = self._run_main(accounts=self._accounts())
        self.assertIsNotNone(rerun)
        self.assertEqual(
            [a.phone for a in rerun], ["13800000002"],
            "补签轮不得重跑当日已 success 的账号（完整登录=风控暴露）",
        )

    def test_second_run_all_done_exits_silently(self):
        """补签轮：全员已了结 → 不进入签到队列、退出码 0（静默结束）。"""
        self._write_state({"13800000001": "success", "13800000002": "already"})
        os.environ["YIBAN_SECOND_RUN"] = "1"
        code, rerun = self._run_main(accounts=self._accounts())
        self.assertEqual(code, 0)
        self.assertIsNone(rerun, "全员已了结时不应再触发任何账号")

    def test_first_run_keeps_all_accounts(self):
        """首签轮（无补签信号）：不做过滤，两个账号都进队列。"""
        self._write_state({"13800000001": "success", "13800000002": "failed"})
        os.environ.pop("YIBAN_SECOND_RUN", None)
        code, rerun = self._run_main(accounts=self._accounts())
        self.assertEqual([a.phone for a in rerun],
                         ["13800000001", "13800000002"])

    def test_second_run_missing_state_keeps_all_accounts(self):
        """状态文件缺失（如目录被清）：宁多勿漏，全量重跑。"""
        os.environ["YIBAN_SECOND_RUN"] = "1"
        code, rerun = self._run_main(accounts=self._accounts())
        self.assertEqual(len(rerun), 2)


# ---------------------------------------------------------------------------
# 2. 探针纳入暂停/周末门
# ---------------------------------------------------------------------------
class ProbeGateTest(unittest.TestCase):
    """一键暂停 / 周末签到关闭期间，探针分支不得发起完整登录。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-probe-gate-")
        self._old_env = {
            k: os.environ.get(k) for k in (
                "YIBAN_GLOBAL_PAUSE", "YIBAN_ACCOUNTS_JSON", "YIBAN_DB_FILE",
                "YIBAN_STATE_DIR", "YIBAN_LOG_FILE",
            )
        }
        self._old_argv = sys.argv[:]
        os.environ["YIBAN_DB_FILE"] = os.path.join(self.tmp, "yiban.db")
        os.environ["YIBAN_STATE_DIR"] = self.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(self.tmp, "sign.log")

    def tearDown(self):
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        sys.argv = self._old_argv
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_probe_main(self):
        sys.argv = ["signin.py", "--probe"]
        with mock.patch.object(signin, "load_accounts", return_value=[
                mock.Mock(phone="13800000000", user_paused=False)]), \
             mock.patch.object(signin, "run_probe") as m_probe:
            try:
                signin.main()
                code = 0
            except SystemExit as e:
                code = e.code
        return code, m_probe

    def test_probe_skipped_when_global_paused(self):
        """一键暂停开启：探针不发起 run_probe（完整登录=风控暴露），静默退出。"""
        os.environ["YIBAN_GLOBAL_PAUSE"] = "1"
        code, m_probe = self._run_probe_main()
        self.assertEqual(code, 0)
        m_probe.assert_not_called()

    def test_probe_skipped_saturday_when_saturday_sign_off(self):
        """周六签到关闭 + 周六：探针同样跳过（与真实签到同一组门）。"""
        os.environ.pop("YIBAN_GLOBAL_PAUSE", None)
        fake = type("_SatDT", (_FakeDT,), {"_date": (2026, 9, 5), "_hm": (10, 0)})
        with mock.patch.object(signin, "datetime", fake), \
             mock.patch.object(signin, "SATURDAY_SIGN", False), \
             mock.patch.object(signin, "SUNDAY_SIGN", False):
            code, m_probe = self._run_probe_main()
        self.assertEqual(code, 0)
        m_probe.assert_not_called()


# ---------------------------------------------------------------------------
# 3. 熔断计数不可被任意编辑重置
# ---------------------------------------------------------------------------
class _WebAppMixin:
    """web/app.py 隔离加载（独立 .env / db / 状态目录）。"""

    @classmethod
    def _isolate_env(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-knife6b-web-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with io.open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
            )
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls.users_file = os.path.join(cls.tmp, "users.json")
        cls.log_file = os.path.join(cls.tmp, "sign.log")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_USERS_FILE"] = cls.users_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = cls.log_file
        spec = importlib.util.spec_from_file_location(
            "webapp_knife6b", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_knife6b"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)

    @classmethod
    def _teardown_env(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE",
                  "YIBAN_USERS_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR",
                  "YIBAN_LOG_FILE"):
            os.environ.pop(k, None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _reset_db(self):
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
        db.init_db(self.db_file, migrate_from=self.accounts_file,
                   env_file=self.env_file)

    def _seed_fuse_pause(self, phone, fail_days=3):
        """写入熔断暂停条目（fail_days=3 表示已连续失败 3 天）。"""
        path = os.path.join(self.tmp, "cred-state.json")
        with io.open(path, "w", encoding="utf-8") as f:
            json.dump({phone: {
                "fail_days": fail_days,
                "last_fail": "2026-09-07",
                "paused_since": "2026-09-07 07:00:00",
            }}, f)

    def _read_fuse_pause(self):
        path = os.path.join(self.tmp, "cred-state.json")
        if not os.path.exists(path):
            return {}
        with io.open(path, encoding="utf-8") as f:
            return json.load(f)

    def _login_admin(self, c):
        r = c.post("/api/login", json={"username": "admin", "password": ADMIN_PASS})
        assert r.status_code == 200, r.get_data(as_text=True)
        return c.get("/api/me").get_json()["csrf_token"]

    def _csrf(self, token):
        return {"X-CSRF-Token": token}


class FuseResetGateTest(_WebAppMixin, unittest.TestCase):
    """管理员/用户编辑账号：仅凭据变更清熔断，只改备注保持 fail_days。"""

    PHONE = "13800138000"

    @classmethod
    def setUpClass(cls):
        cls._isolate_env()

    @classmethod
    def tearDownClass(cls):
        cls._teardown_env()

    def setUp(self):
        self._reset_db()
        self._seed_fuse_pause(self.PHONE)
        # 被编辑账号（不经真实 db 写路径：本用例聚焦 clear_fuse_pause 的触发条件）
        self._account = {
            "id": 1, "phone": self.PHONE, "password": "OldPass123",
            "name": "张三", "phone_code": "", "owner": "admin",
            "status": "active", "deleted": False,
        }
        patcher = mock.patch.object(
            self.webapp, "load_accounts",
            side_effect=lambda: [dict(self._account)])
        patcher.start()
        self.addCleanup(patcher.stop)
        db_patch = mock.patch.object(self.webapp.db, "update_account", return_value=True)
        db_patch.start()
        self.addCleanup(db_patch.stop)

    def _admin_client(self):
        c = self.webapp.create_app().test_client()
        token = self._login_admin(c)
        return c, token

    def test_admin_note_only_edit_keeps_fail_days(self):
        """只改备注：熔断条目原样保留（fail_days 不清零，熔断仍会跳闸）。"""
        c, token = self._admin_client()
        r = c.put("/api/accounts/0",
                  json={"phone": self.PHONE, "name": "只改备注", "password": ""},
                  headers=self._csrf(token))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        entry = self._read_fuse_pause().get(self.PHONE)
        self.assertIsNotNone(entry, "只改备注不得清除熔断暂停条目")
        self.assertEqual(entry.get("fail_days"), 3)

    def test_admin_password_change_clears_fuse_pause(self):
        """改密码（凭据变更）：熔断条目清除，账号立即恢复签到资格。"""
        c, token = self._admin_client()
        r = c.put("/api/accounts/0",
                  json={"phone": self.PHONE, "name": "x", "password": "NewPass456"},
                  headers=self._csrf(token))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertNotIn(self.PHONE, self._read_fuse_pause())

    def test_user_note_only_edit_keeps_fail_days(self):
        """用户侧只改备注：同样不得重置熔断计数。"""
        db.create_user("user1@test.local", self.webapp.generate_password_hash(USER_PASS))
        self._account["owner"] = "user1@test.local"
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "user1@test.local",
                                       "password": USER_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        token = c.get("/api/me").get_json()["csrf_token"]
        r = c.put("/api/my-accounts/0",
                  json={"phone": self.PHONE, "name": "只改备注", "password": ""},
                  headers=self._csrf(token))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        entry = self._read_fuse_pause().get(self.PHONE)
        self.assertIsNotNone(entry, "用户只改备注不得清除熔断暂停条目")
        self.assertEqual(entry.get("fail_days"), 3)


# ---------------------------------------------------------------------------
# 4. 死号先判后睡
# ---------------------------------------------------------------------------
class DeadAccountSkipTest(unittest.TestCase):
    """将跳过的账号（自取消/熔断暂停）不得先睡满间隔再判跳过。"""

    def setUp(self):
        # 窗口拉满全天；请求间隔下限压到 1s，隔离干扰
        env = {
            "YIBAN_SIGN_START": "00:01", "YIBAN_SIGN_END": "23:59",
            "YIBAN_MIN_EXEC_GAP": "1", "YIBAN_EXEC_GAP_MIN": "0",
        }
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)

    def _run(self, accounts, schedule=None, cred_state=None, gap_max=0):
        sleeps = []
        with mock.patch.object(signin.time, "sleep", side_effect=sleeps.append), \
             mock.patch.object(signin, "attempt_signin",
                               return_value=(True, "已签到", False, "already")):
            results = signin.run_queue_retry(
                accounts, "", 0, gap_max, schedule=schedule, cred_state=cred_state)
        return results, sleeps

    def test_schedule_branch_dead_account_skips_without_slot_sleep(self):
        """调度 v2 分支：熔断暂停号的槽位在 +180s，活号立即执行——
        修复前主循环会先睡到死号槽位才发现可跳过（活号被挤出窗口）。"""
        dead = SimpleNamespace(phone="13800000001", user_paused=False, owner="")
        live = SimpleNamespace(phone="13800000002", user_paused=False, owner="")
        now = datetime.now()
        schedule = {
            "13800000001": datetime.fromtimestamp(now.timestamp() + 180),
            "13800000002": datetime.fromtimestamp(now.timestamp() - 5),
        }
        cred_state = {"13800000001": {"paused_since": "2026-09-01 00:00:00",
                                      "probe_date": "2099-01-01"}}
        results, sleeps = self._run([live, dead], schedule=schedule,
                                    cred_state=cred_state)
        self.assertEqual(results["13800000001"][3], signin.STATUS_PAUSED)
        self.assertTrue(
            all(s < 60 for s in sleeps),
            f"死号不得睡满时段槽位（记录到的 sleep: {sleeps}）",
        )
        self.assertEqual(results["13800000002"][3], "already", "活号照常执行")

    def test_queue_branch_dead_account_skips_without_gap_sleep(self):
        """队列分支：[活号, 熔断暂停号]，死号不得先睡满账号间隔（gap_max=10）。"""
        dead = SimpleNamespace(phone="13800000001", user_paused=False, owner="")
        live = SimpleNamespace(phone="13800000002", user_paused=False, owner="")
        cred_state = {"13800000001": {"paused_since": "2026-09-01 00:00:00",
                                      "probe_date": "2099-01-01"}}
        results, sleeps = self._run([live, dead], cred_state=cred_state, gap_max=10)
        self.assertEqual(results["13800000001"][3], signin.STATUS_PAUSED)
        self.assertTrue(
            all(s < 5 for s in sleeps),
            f"死号不得睡满账号间隔（记录到的 sleep: {sleeps}）",
        )

    def test_queue_branch_user_cancelled_skips_without_gap_sleep(self):
        """队列分支：用户自取消账号同样先判后睡（不得消耗 10s 间隔）。"""
        dead = SimpleNamespace(phone="13800000001", user_paused=True, owner="")
        live = SimpleNamespace(phone="13800000002", user_paused=False, owner="")
        results, sleeps = self._run([live, dead], gap_max=10)
        self.assertEqual(results["13800000001"][3], signin.STATUS_USER_CANCELLED)
        self.assertTrue(all(s < 5 for s in sleeps), f"sleeps={sleeps}")


# ---------------------------------------------------------------------------
# 5. 手动签到非 0 退出码透传
# ---------------------------------------------------------------------------
class ManualSignExitTest(_WebAppMixin, unittest.TestCase):
    """手动签到子进程 exit 3（运行锁占用）等不得表现为无声成功。"""

    PHONE = "13800138000"

    @classmethod
    def setUpClass(cls):
        cls._isolate_env()

    @classmethod
    def tearDownClass(cls):
        cls._teardown_env()

    def setUp(self):
        self._reset_db()

    def _today_log(self):
        path = os.path.join(
            self.tmp, f"sign-{datetime.now().strftime('%Y-%m-%d')}.log")
        return path

    def _wait_reap(self):
        for _ in range(50):
            time.sleep(0.05)
            if os.path.exists(self._today_log()):
                with io.open(self._today_log(), encoding="utf-8") as f:
                    if "手动签到未完成" in f.read():
                        return True
        return False

    def test_exit_reason_mapping(self):
        """退出码 → 用户可见原因：3=队列忙，2=跳过，其他=异常退出，0=None。"""
        f = self.webapp._manual_sign_failure_reason
        self.assertIsNone(f(0))
        self.assertIn("队列忙", f(3))
        self.assertIn("跳过", f(2))
        self.assertIn("退出码 1", f(1))

    def test_single_manual_sign_exit3_logged_as_failure(self):
        """单号手动签到子进程 exit 3：签到日志出现失败留痕（前端日志页可见）。"""
        c = self.webapp.create_app().test_client()
        token = self._login_admin(c)
        accounts = [{"phone": self.PHONE, "status": "active", "deleted": False}]
        proc = SimpleNamespace(returncode=3, pid=4321)
        proc.wait = lambda timeout=None: 3
        proc.poll = lambda: 3
        proc.terminate = lambda: None
        proc.kill = lambda: None
        with mock.patch.object(self.webapp, "load_accounts",
                               return_value=accounts), \
             mock.patch.object(self.webapp.subprocess, "Popen",
                               return_value=proc):
            r = c.post("/api/signin", json={"phone": self.PHONE},
                       headers=self._csrf(token))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(
            self._wait_reap(),
            "子进程 exit 3 必须在签到日志留下失败留痕（用户侧唯一结果通道）",
        )

    def test_batch_manual_sign_exit3_logged_as_failure(self):
        """批量手动签到子进程 exit 3：同样在签到日志留痕。"""
        c = self.webapp.create_app().test_client()
        token = self._login_admin(c)
        accounts = [{"phone": self.PHONE, "status": "active", "deleted": False}]
        proc = SimpleNamespace(returncode=3, pid=4322)
        proc.wait = lambda timeout=None: 3
        proc.poll = lambda: 3
        proc.terminate = lambda: None
        proc.kill = lambda: None
        with mock.patch.object(self.webapp, "load_accounts",
                               return_value=accounts), \
             mock.patch.object(self.webapp.subprocess, "Popen",
                               return_value=proc):
            r = c.post("/api/signin/batch", json={"ids": [0], "phones": [self.PHONE]},
                       headers=self._csrf(token))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(self._wait_reap())


# ---------------------------------------------------------------------------
# 6. SIGTERM 超时终止前冲刷告警汇总
# ---------------------------------------------------------------------------
class SigtermFlushTest(unittest.TestCase):
    """签到轮被超时击杀（SIGTERM）时，已收集的管理员告警汇总先发出再退出。"""

    def setUp(self):
        self._summary_backup = list(signin._mail_summary)
        signin._mail_summary.clear()

    def tearDown(self):
        signin._mail_summary.clear()
        signin._mail_summary.extend(self._summary_backup)

    def test_sigterm_flushes_collected_admin_mail(self):
        signin._mail_summary.append(("易班签到失败", "账号: 138****0000\n原因: 登录失败"))
        with mock.patch.object(signin.mailer, "send_admin_alert",
                               return_value=True) as m_send, \
             mock.patch.object(signin.db, "admin_mail_recipients",
                               return_value=["a@b.c"]), \
             self.assertRaises(SystemExit):
            signin._flush_mail_on_sigterm(15, None)
        self.assertEqual(m_send.call_count, 1, "终止前必须把已收集的告警发出")
        args, kwargs = m_send.call_args
        body = kwargs.get("body") or (args[1] if len(args) > 1 else "")
        self.assertIn("超时", body, "汇总需标明本轮被超时终止")
        self.assertEqual(signin._mail_summary, [], "发送后清空，正常收尾不重复")

    def test_sigterm_noop_without_pending_mail(self):
        """无待发告警：不产生任何发送（不重复告警）。"""
        with mock.patch.object(signin.mailer, "send_admin_alert") as m_send, \
             self.assertRaises(SystemExit):
            signin._flush_mail_on_sigterm(15, None)
        m_send.assert_not_called()

    def test_main_registers_sigterm_handler(self):
        """main() 注册 SIGTERM 处理器（run.sh timeout / 手动 terminate 均经此路径）。"""
        previous = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, previous)
        tmp = tempfile.mkdtemp(prefix="yiban-sigterm-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        old_env = {k: os.environ.get(k) for k in (
            "YIBAN_GLOBAL_PAUSE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR",
            "YIBAN_LOG_FILE")}

        def _restore_env():
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(_restore_env)
        old_argv = sys.argv[:]
        self.addCleanup(setattr, sys, "argv", old_argv)
        os.environ["YIBAN_GLOBAL_PAUSE"] = "1"
        os.environ["YIBAN_DB_FILE"] = os.path.join(tmp, "yiban.db")
        os.environ["YIBAN_STATE_DIR"] = tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(tmp, "sign.log")
        sys.argv = ["signin.py"]
        with self.assertRaises(SystemExit):
            signin.main()
        self.assertIs(signal.getsignal(signal.SIGTERM), signin._flush_mail_on_sigterm)


# ---------------------------------------------------------------------------
# 7. 疑似首签轮但已过补签触发点仍告警
# ---------------------------------------------------------------------------
class LateFirstRunAlertTest(unittest.TestCase):
    """06:31 关机、07:10 起的轮次挂首签身份却是当天最后一轮：
    部分成功 + 窗口外不得被首签标签抑制。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-late-first-")
        p = mock.patch.dict(os.environ, {"YIBAN_STATE_DIR": self.tmp})
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("YIBAN_SECOND_RUN", None)
        self.addCleanup(os.environ.pop, "YIBAN_SECOND_RUN", None)
        self.accounts = [SimpleNamespace(phone="13800000001"),
                         SimpleNamespace(phone="13800000002")]
        self.results = {
            "13800000001": (True, "已签到", False, "success"),
            "13800000002": (False, "签到时段已结束", True, "skipped_window"),
        }

    def _alert(self, hm):
        fake = type("_DT", (_FakeDT,), {"_hm": hm})
        with mock.patch.object(signin, "datetime", fake):
            return signin._maybe_alert_zero_success(
                self.accounts, self.results, ok_n=1, is_second_run=False)

    def test_after_second_trigger_time_alerts(self):
        """07:10 之后仍挂着首签身份：不再有第三轮兜底 → 必须告警。"""
        self.assertTrue(self._alert((7, 12)), "部分成功+窗口外在当天最后一轮必须告警")

    def test_before_second_trigger_time_stays_silent(self):
        """07:10 之前的首签轮：补签会重跑，维持既有不打扰语义。"""
        self.assertFalse(self._alert((6, 50)))


# ---------------------------------------------------------------------------
# 8. 容器调度触发点落盘标记（重启不二次触发）
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# 9. _child_timeout 不截断晚到重跑的自了结（钉版证明）
# ---------------------------------------------------------------------------
class ChildTimeoutBoundTest(unittest.TestCase):
    """晚到触发的重跑进程：动态超时恒大于「距窗口关闭的剩余时间」，
    子进程总能在窗口关闭时自了结（剩余账号 skipped_window 后退出），
    600s 下限只在窗口已关闭时生效——彼时剩余合法工作≈0，截断无害。"""

    def test_timeout_always_exceeds_time_to_window_close(self):
        sched = _load_sched("_t")
        env = {"YIBAN_SIGN_END": "07:50"}
        for hm in ((6, 31), (7, 0), (7, 10), (7, 40), (7, 49), (8, 0)):
            fake = type(f"_DT{hm[0]}{hm[1]}", (_FakeDT,), {"_hm": hm})
            with mock.patch.object(sched, "datetime", fake):
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
        with mock.patch.object(sched, "datetime", fake):
            self.assertEqual(sched._child_timeout({"YIBAN_SIGN_END": "07:50"}), 600)


if __name__ == "__main__":
    unittest.main(verbosity=2)
