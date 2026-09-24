# -*- coding: utf-8 -*-
"""按天状态文件的清理策略（`yiban/state_gc.py`）与两处调用点。

缺陷背景（DAT-6）：状态目录里凡按日生成的文件，除日志/结构化状态/调度快照外
**都没有清理规则**（`sched-run-*`、`sched-slot-*`、`sign-daily-*`、`mail-user-fail-*`
及其 `.lock` 伴生文件）。生产实测状态目录 257 个条目、其中 `sign-daily-` 53 个、
`sched-run-` 14 个、`mail-user-fail-` 4 个——只增不减；web 日历又要按前缀
`os.scandir` 整目录扫描，条目数随天数线性膨胀。根因不是"漏了某几个模式"，而是
**规则写在 bash 里、容器侧另写一份**，新增一类按日文件没有机制提醒补规则。

覆盖：
1. 策略表生效：各类按日文件按各自保留期删除，未过期的保留；
2. `.lock` 伴生文件与孤儿半成品（`.tmp*`）也被清；
3. 保留期可用环境变量覆盖，非法值响亮失败（不静默退化）；
4. 元测试：仓库里出现的每个按日状态文件名都必须在策略表里（防"新增文件忘了登记"）；
5. 宿主 CLI 与容器调度都调用同一策略（且 CLI 的目录键与 run.sh 同口径）。
"""
import contextlib
import io
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import state_cleanup  # noqa: E402  （scripts/state_cleanup.py）

from yiban import state_gc  # noqa: E402


def _day(delta):
    return (datetime.now() + timedelta(days=delta)).strftime("%Y-%m-%d")


class SweepPolicyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-gc-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _touch(self, name, when=None):
        path = os.path.join(self.tmp, name)
        with io.open(path, "w", encoding="utf-8") as f:
            f.write("{}")
        if when is not None:
            os.utime(path, (when, when))
        return path

    def test_expired_daily_artifacts_are_removed(self):
        expired = [
            "sign-%s.log" % _day(-400),
            "sign-state-%s.json" % _day(-400),
            "sign-daily-%s.json" % _day(-400),
            "sched-run-%s.json" % _day(-30),
            "sched-slot-first-%s.json" % _day(-30),
            "sched-snapshot-%s.json" % _day(-30),
            "mail-user-fail-%s.json" % _day(-30),
        ]
        for name in expired:
            self._touch(name)
        removed, detail = state_gc.sweep(self.tmp)
        self.assertEqual(removed, len(expired), detail)
        self.assertEqual(sorted(os.listdir(self.tmp)), [])

    def test_recent_artifacts_are_kept(self):
        keep = [
            "sign-%s.log" % _day(0),
            "sign-state-%s.json" % _day(-3),
            "sign-daily-%s.json" % _day(-3),
            "sched-run-%s.json" % _day(0),
            "sched-slot-second-%s.json" % _day(-1),
            "sched-snapshot-%s.json" % _day(-1),
            "mail-user-fail-%s.json" % _day(0),
            # 非按日文件一律不动：账本/节流状态/审计锚点/持久锁/归档目录
            "cred-state.json", "cred-state.json.lock", "probe-state.json",
            "probe-state.json.lock", "notify-ledger.json", "notify-throttle.json",
            "audit-anchor.log", "cleanup.log", "backup.log", "run-cron.log",
        ]
        for name in keep:
            self._touch(name)
        removed, detail = state_gc.sweep(self.tmp)
        self.assertEqual(removed, 0, detail)
        self.assertEqual(sorted(os.listdir(self.tmp)), sorted(keep))

    def test_retention_boundary_is_by_filename_date(self):
        """保留期边界与旧 bash 同口径：删除**严格早于**截止日的文件，截止日当天保留。"""
        self._touch(f"sched-run-{_day(-7)}.json")
        removed, _ = state_gc.sweep(self.tmp)
        self.assertEqual(removed, 0, "截止日当天仍在保留期内")
        self._touch(f"sched-run-{_day(-8)}.json")
        removed, _ = state_gc.sweep(self.tmp)
        self.assertEqual(removed, 1, "早于截止日的应删除")

    def test_lock_companions_are_removed(self):
        self._touch("mail-user-fail-%s.json.lock" % _day(-30))
        self._touch("cred-state.json.lock")
        removed, detail = state_gc.sweep(self.tmp)
        self.assertEqual(removed, 1, detail)
        self.assertEqual(os.listdir(self.tmp), ["cred-state.json.lock"],
                         "持久锁（非按日）不得被清")

    def test_orphan_tmp_is_removed_but_fresh_one_kept(self):
        stale = self._touch("cred-state.json.tmp1234", when=time.time() - 3 * 86400)
        fresh = self._touch("sched-run-%s.json.tmp99" % _day(0))
        removed, detail = state_gc.sweep(self.tmp)
        self.assertEqual(removed, 1, detail)
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(fresh), "刚写入的半成品必须保留（可能正在写）")

    def test_retention_cutoff_uses_business_clock_not_host_tz(self):
        """H2：UTC 主机（宿主比北京慢 8 小时）上，保留期截止日必须按业务钟。

        宿主 09-16 22:00 时北京已是 09-17 06:00：文件按业务日命名，按宿主时间算
        截止日会少算一天——把"昨天"的文件当成"今天"保留（多滞留约一天）。
        按业务钟 cutoff=09-10（保留 7 天、删严格早于截止日）：只删 09-09；
        按宿主钟 cutoff=09-09：09-09 当天也被保留，removed=0。
        """
        self._touch("sched-run-2026-09-10.json")
        self._touch("sched-run-2026-09-09.json")
        biz_now = datetime(2026, 9, 17, 6, 0, 0)        # 北京 09-17 06:00
        host_now = datetime(2026, 9, 16, 22, 0, 0)      # 宿主 UTC 09-16 22:00
        with mock.patch("yiban.state_gc.datetime.datetime") as dt, \
             mock.patch("yiban.clock.now", return_value=biz_now):
            dt.now.return_value = host_now
            dt.timedelta = timedelta
            removed, _ = state_gc.sweep(self.tmp)
        self.assertEqual(removed, 1,
                         "按业务钟 09-17 起算 7 天：删严格早于 09-10 的文件")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "sched-run-2026-09-09.json")),
                         "09-09 已过期应删除")
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "sched-run-2026-09-10.json")),
                        "09-10 是截止日当天，仍在保留期内")

    def test_nonexistent_dir_is_not_an_error(self):
        removed, detail = state_gc.sweep(os.path.join(self.tmp, "nope"))
        self.assertEqual((removed, detail), (0, []))

    def test_log_dir_is_separate_for_logs(self):
        """容器形态：日志与状态分属两个目录（宿主形态同目录）。"""
        state = os.path.join(self.tmp, "state")
        logs = os.path.join(self.tmp, "logs")
        os.makedirs(state)
        os.makedirs(logs)
        with io.open(os.path.join(logs, "sign-%s.log" % _day(-400)), "w") as f:
            f.write("x")
        with io.open(os.path.join(state, "sign-state-%s.json" % _day(-400)), "w") as f:
            f.write("{}")
        removed, detail = state_gc.sweep(state, logs)
        self.assertEqual(removed, 2, detail)
        self.assertEqual(os.listdir(state), [])
        self.assertEqual(os.listdir(logs), [])

    def test_env_overrides_retention(self):
        self._touch("sched-run-%s.json" % _day(-10))
        with mock.patch.dict(os.environ, {"YIBAN_SNAPSHOT_RETENTION_DAYS": "3"}):
            removed, _ = state_gc.sweep(self.tmp)
        self.assertEqual(removed, 1)
        self._touch("sign-%s.log" % _day(-100))
        with mock.patch.dict(os.environ, {"YIBAN_RETENTION_DAYS": "30"}):
            removed, _ = state_gc.sweep(self.tmp)
        self.assertEqual(removed, 1)

    def test_invalid_retention_raises(self):
        with mock.patch.dict(os.environ, {"YIBAN_RETENTION_DAYS": "abc"}),              self.assertRaises(ValueError):
            state_gc.sweep(self.tmp)
        with mock.patch.dict(os.environ, {"YIBAN_SNAPSHOT_RETENTION_DAYS": "-1"}),              self.assertRaises(ValueError):
            state_gc.sweep(self.tmp)

    def test_empty_cred_state_removed_only_when_empty(self):
        self._touch("cred-state.json")
        self.assertTrue(state_gc.sweep_empty_cred_state(self.tmp))
        with io.open(os.path.join(self.tmp, "cred-state.json"), "w", encoding="utf-8") as f:
            f.write('{"13800000000": {"fail_days": 3}}')
        self.assertFalse(state_gc.sweep_empty_cred_state(self.tmp))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "cred-state.json")))
        self.assertFalse(state_gc.sweep_empty_cred_state(os.path.join(self.tmp, "nope")))


# 扫描会命中、但不属于"状态目录里的按日文件"的前缀（逐条理由）
ALLOWED_NON_STATE = {
    "concurrency-": "loadtest 工具的输出 JSON/CSV（outdir，由压测者自行管理）",
    "i-": "build_lucide_sprite 生成的图标 id（构建期产物，不是文件）",
    "krun-": "loadtest 的每轮临时目录",
    "mock-pass-": "loadtest 假账号口令字面量（不是文件名）",
    "pass-": "generate_demo_data 的演示口令字面量",
    "run-": "loadtest 的输出 JSON（outdir）",
    "cap-": "容量基准的每档标签（用于 concurrency-<label> 输出名，不是文件）",
    "probe-": "容量基准每档的探针日志（outdir，由测量者自行管理）",
    "scratch-": "loadtest 的临时 SQLite 文件（outdir）",
    "signin-": "loadtest 的每轮日志文件（logdir）",
    "verify-job-": "校验任务的线程名（不是文件）",
    # 邮件排版层的 HTML 内联样式：扫描正则只看"引号 + 小写 token + '-' + 后接 {表达式}"，
    # 而 style="border-top:1px solid {_RULE}" 正好是这个形状——CSS 属性名，不是文件名。
    "border-": "layout.py 的 HTML 内联样式属性名（style=\"border-…: {常量}\"）",
    "font-": "layout.py 的 HTML 内联样式属性名（style=\"font-family:{_FONT}\"）",
    "line-": "layout.py 的 HTML 内联样式属性名（style=\"line-height:{_LEAD}\"）",
    "vertical-": "layout.py 的 HTML 内联样式属性名（style=\"vertical-align:top;…{…}\"）",
    "word-": "layout.py 的 HTML 内联样式属性名（style=\"word-break:break-word;…{…}\"）",
}


class EveryDailyStateFileIsRegisteredTest(unittest.TestCase):
    """元测试：代码里出现的每个"带日期后缀"的文件名都要有交代（防新增文件被遗忘）。

    做法是**扫描**而不是列举：正则找出所有形如 `"<前缀>-{日期表达式}…"` 的字面量，
    每个前缀必须落在下列三者之一：
    1. 策略表 `state_gc.ARTIFACTS`（会被清理）；
    2. 非状态目录的允许清单（压测工具输出、构建产物、线程名等，逐条给理由）；
    3. 都不在 → 失败，提示"新按日文件未登记，会无界增长"。
    """


    _NAME_RE = re.compile(r'["\']([a-z][a-z0-9-]*)-[^"\']*(?:\{[^}]*\}|%Y|YYYY)')

    def _scan(self):
        found = {}
        for root in ("scripts", "docker", "web", "yiban"):
            for dirpath, _dirs, files in os.walk(os.path.join(BASE, root)):
                if "__pycache__" in dirpath:
                    continue
                for name in files:
                    if not name.endswith((".py", ".sh")):
                        continue
                    path = os.path.join(dirpath, name)
                    with io.open(path, encoding="utf-8", errors="ignore") as f:
                        src = f.read()
                    for m in self._NAME_RE.finditer(src):
                        found.setdefault(m.group(1) + "-", set()).add(
                            os.path.relpath(path, BASE).replace("\\", "/"))
        return found

    def test_new_daily_file_must_be_registered(self):
        found = self._scan()
        # 防扫描器失效（正则改坏会让下面的断言恒真）
        self.assertIn("sign-state-", found)
        self.assertIn("sched-slot-", found)
        known = {art.prefix for art in state_gc.ARTIFACTS} | set(ALLOWED_NON_STATE)
        unregistered = {k: sorted(v) for k, v in found.items() if k not in known}
        self.assertEqual(unregistered, {},
                         "下列按日前缀未登记清理策略或允许清单（会无界增长）")

    def test_policy_entries_all_have_writer(self):
        """反向核对：策略表里的每项都必须真有写入点（避免清理一个已不存在的文件）。"""
        found = self._scan()
        for art in state_gc.ARTIFACTS:
            with self.subTest(prefix=art.prefix):
                self.assertIn(art.prefix, found, f"{art.prefix} 无写入点，策略表项已过期")


class CleanupEntryPointsTest(unittest.TestCase):
    """宿主 CLI 与容器调度必须共用同一策略，且目录解析与 run.sh 同口径。"""

    def test_cli_uses_shared_policy(self):
        with open(os.path.join(BASE, "scripts", "state_cleanup.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("state_gc.sweep(", src)
        self.assertIn("state_gc.sweep_empty_cred_state(", src)

    def test_bash_wrapper_delegates_and_keeps_login_safe_log(self):
        with open(os.path.join(BASE, "scripts", "yiban-cleanup.sh"), encoding="utf-8") as f:
            sh = f.read()
        self.assertIn("state_cleanup.py", sh, "bash 侧不得再自持一份清理规则")
        self.assertNotIn("clean_by_date", sh)
        # 只看可执行部分（注释里会引用文件名）：委托实现后 bash 不该再有删除/写入动作，
        # 更不能碰 sign-*.log——root 预创建当日签到日志会让 run.sh 的重定向失败，
        # 当天签到整体丢失（2026-08-17 事故）。
        body = "\n".join(ln for ln in sh.splitlines() if not ln.strip().startswith("#"))
        self.assertNotIn("rm ", body)
        self.assertNotIn("sign-", body)

    def test_container_scheduler_uses_shared_policy(self):
        with open(os.path.join(BASE, "docker", "scheduler.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("state_gc.sweep(STATEDIR, LOGDIR)", src)
        self.assertNotIn("_cleanup_logs", src, "旧的只清日志实现应已被替换")

    def test_state_dir_resolution_matches_run_sh(self):
        """同一套键：.env 只被 run.sh 导出，故 CLI 也按 YIBAN_STATE_DIR/LOG_FILE 解析。"""
        with mock.patch.dict(os.environ, {"YIBAN_STATE_DIR": "/tmp/x-state",
                                          "YIBAN_LOG_FILE": "/tmp/x-logs/sign.log"}):
            self.assertEqual(state_cleanup.state_dir_from_env(), "/tmp/x-state")
            self.assertEqual(state_cleanup.log_dir_from_env("/tmp/x-state"), "/tmp/x-logs")
        with mock.patch.dict(os.environ, {"YIBAN_STATE_DIR": "", "YIBAN_LOG_FILE": ""}):
            self.assertEqual(state_cleanup.state_dir_from_env(), "/var/log/yiban")
            self.assertEqual(state_cleanup.log_dir_from_env("/var/log/yiban"), "/var/log/yiban")

    def test_cli_end_to_end_writes_cleanup_log(self):
        tmp = tempfile.mkdtemp(prefix="yiban-gc-cli-")
        try:
            with io.open(os.path.join(tmp, "sched-run-%s.json" % _day(-30)), "w") as f:
                f.write("{}")
            with mock.patch.dict(os.environ, {"YIBAN_STATE_DIR": tmp, "YIBAN_LOG_FILE": ""}):
                self.assertEqual(state_cleanup.main([]), 0)
            self.assertFalse(os.path.exists(os.path.join(tmp, "sched-run-%s.json" % _day(-30))))
            with io.open(os.path.join(tmp, "cleanup.log"), encoding="utf-8") as f:
                logged = f.read()
            self.assertIn("已清理 1 个过期文件", logged)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_cli_missing_state_dir_is_loud_and_creates_nothing(self):
        """目录不存在 → 响亮失败（cron 会报给运维），但**不得**顺手把目录造出来。"""
        missing = os.path.join(tempfile.mkdtemp(prefix="yiban-gc-miss-"), "state")
        with mock.patch.dict(os.environ, {"YIBAN_STATE_DIR": missing, "YIBAN_LOG_FILE": ""}), \
             mock.patch.object(sys, "stderr", io.StringIO()) as err:
            self.assertEqual(state_cleanup.main([]), 1)
        self.assertIn("状态目录不存在", err.getvalue())
        self.assertFalse(os.path.exists(missing), "清理脚本不该创建状态目录")

    def test_cli_fails_loudly_on_bad_retention(self):
        tmp = tempfile.mkdtemp(prefix="yiban-gc-cli-bad-")
        try:
            with mock.patch.dict(os.environ, {"YIBAN_STATE_DIR": tmp,
                                              "YIBAN_RETENTION_DAYS": "many"}),                  mock.patch.object(sys, "stderr", io.StringIO()):
                self.assertEqual(state_cleanup.main([]), 1)
            with io.open(os.path.join(tmp, "cleanup.log"), encoding="utf-8") as f:
                self.assertIn("保留期配置非法", f.read())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


TEST_KEY = "a" * 64


AUDIT_KEY = "b" * 64


class CleanupResidueTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-residue-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\nYIBAN_AUDIT_KEY={AUDIT_KEY}\n")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_AUDIT_KEY"] = AUDIT_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        global db
        import db

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for key in ("YIBAN_ACCOUNTS_KEY", "YIBAN_AUDIT_KEY", "YIBAN_ENV_FILE",
                    "YIBAN_DB_FILE", "YIBAN_STATE_DIR"):
            os.environ.pop(key, None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                os.remove(self.db_file + suffix)
        db.init_db(cleanup=False)

    def _add(self, phone, owner="t@test.local"):
        return db.add_account({
            "name": phone, "phone": phone, "password": "pw-1",
            "owner": owner, "status": "active",
        })

    def _event_count(self, phone):
        with db._conn_lock:
            conn = db.get_conn()
            return conn.execute(
                "SELECT COUNT(*) FROM sign_events WHERE phone=?", (phone,)
            ).fetchone()[0]

    def _req_count(self, email):
        with db._conn_lock:
            conn = db.get_conn()
            return conn.execute(
                "SELECT COUNT(*) FROM user_delete_requests WHERE username=?", (email,)
            ).fetchone()[0]

    # ---------------- M2：删号连带清 sign_events ----------------
    def test_purge_account_clears_sign_events(self):
        aid = self._add("13900000001")
        db.add_sign_event("2026-08-28 06:31:00", "13900000001", "success")
        self.assertEqual(self._event_count("13900000001"), 1)
        db.purge_account(aid)
        self.assertEqual(self._event_count("13900000001"), 0,
                         "purge_account 后 sign_events 不应残留明文手机号（M2）")

    def test_delete_user_with_accounts_clears_sign_events(self):
        self._add("13900000002", owner="victim@test.local")
        db.add_sign_event("2026-08-28 06:32:00", "13900000002", "failed")
        db.delete_user_with_accounts("victim@test.local")
        self.assertEqual(self._event_count("13900000002"), 0)

    def test_batch_purge_clears_sign_events(self):
        aid = self._add("13900000003")
        db.add_sign_event("2026-08-28 06:33:00", "13900000003", "success")
        db.batch_account_ops([("purge", aid)])
        self.assertEqual(self._event_count("13900000003"), 0)

    def test_soft_delete_keeps_events(self):
        """软删除（宽限期内可恢复）不清理事件——恢复后历史仍需保留。"""
        aid = self._add("13900000004")
        db.add_sign_event("2026-08-28 06:34:00", "13900000004", "success")
        db.set_account_deleted(aid, True, "2026-08-28 06:35:00")
        self.assertEqual(self._event_count("13900000004"), 1, "软删除不应清理事件")

    def test_replace_accounts_clears_sign_events(self):
        """整表替换（replace_accounts）移除的账号必须连带清 sign_events。"""
        self._add("13900000005", owner="keep@test.local")
        self._add("13900000006", owner="drop@test.local")
        db.add_sign_event("2026-08-28 06:36:00", "13900000005", "success")
        db.add_sign_event("2026-08-28 06:36:01", "13900000006", "success")
        # 整表替换：只保留 13900000005（模拟删除 13900000006 后整表保存）
        from scripts import db as _db  # noqa: F401  (db 已在 setUpClass 导入)
        rows = [r for r in db.load_accounts_raw() if r["phone"] == "13900000005"]
        kept = {
            "name": rows[0]["name"], "phone": rows[0]["phone"],
            "password": "pw-1", "phone_model": "", "phone_code": "",
            "owner": "keep@test.local", "status": "active",
        }
        db.replace_accounts([kept])
        self.assertEqual(self._event_count("13900000005"), 1, "保留账号的事件不清理")
        self.assertEqual(self._event_count("13900000006"), 0,
                         "replace_accounts 移除账号后 sign_events 不应残留明文手机号")

    # ---------------- M3：时钟跳变保护 ----------------
    def test_clock_jump_forward_blocked(self):
        """系统时间比上次记录前进超过 72h → 跳过清理并告警，且参照点推进到当前时间。"""
        conn = db.get_conn()
        with db._conn_lock:
            four_days_ago = (datetime.now() - timedelta(days=4)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)",
                         ("test_clock_fwd", four_days_ago))
            conn.commit()
            ok, note = db._clock_jump_guard(conn, "test_clock_fwd")
            row = conn.execute("SELECT value FROM app_meta WHERE key='test_clock_fwd'").fetchone()
        self.assertFalse(ok, "前进 4 天（>72h）必须被判定为跳变")
        self.assertIn("跳变", note)
        self.assertNotEqual(row["value"], four_days_ago,
                            "越界路径必须推进参照点，否则清理永久冻结")

    def test_clock_jump_backward_blocked(self):
        """系统时间比上次记录回拨超过 1h → 跳过清理，参照点同样推进。"""
        conn = db.get_conn()
        with db._conn_lock:
            later = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)",
                         ("test_clock_back", later))
            conn.commit()
            ok, _note = db._clock_jump_guard(conn, "test_clock_back")
        self.assertFalse(ok, "回拨 2h（>1h）必须被判定为跳变")

    def test_clock_normal_passes(self):
        """正常间隔（1h）→ 放行并更新参照。"""
        conn = db.get_conn()
        with db._conn_lock:
            hour_ago = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)",
                         ("test_clock_ok", hour_ago))
            ok, note = db._clock_jump_guard(conn, "test_clock_ok")
        self.assertTrue(ok, note)
        self.assertEqual(note, "")

    def test_purge_accounts_skips_one_round_then_resumes(self):
        """拨快后 purge_expired_deleted_accounts 跳过本轮；参照点随之推进，下一轮恢复清除。"""
        # 记录一次"上次运行时刻" = 现在 - 10 天 → 本次调用视为跳变
        aid = self._add("13900000005")
        old = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
        db.set_account_deleted(aid, True, old)  # 已软删且超 7 天保留期
        conn = db.get_conn()
        with db._conn_lock:
            conn.execute("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)",
                         ("purge_accounts_clock", old))
            conn.commit()
        db.purge_expired_deleted_accounts()  # 应因跳变跳过，账号保留
        rows = db.load_accounts_raw()
        self.assertTrue(
            any(r["phone"] == "13900000005" for r in rows),
            "时钟跳变时 purge 必须跳过——否则刚软删的数据被拨快后立即物理清除",
        )
        # 只跳一轮：越界已把参照点推进到当前时间，下一次调用按正常间隔放行
        db.purge_expired_deleted_accounts()
        rows = db.load_accounts_raw()
        self.assertFalse(
            any(r["phone"] == "13900000005" for r in rows),
            "跳变只应跳过一轮——参照点推进后下一轮必须恢复物理清除",
        )

    # ---------------- M4a：删用户连带清冷却计数 ----------------
    def test_purge_deleted_users_clears_delete_requests(self):
        db.create_user("victim2@test.local", "hash", role="user",
                       created_at="2026-08-28 00:00:00", pw_version=1)
        db.record_user_delete_request("victim2@test.local", ip_hash="x", kind="delete")
        self.assertEqual(self._req_count("victim2@test.local"), 1)
        # 直接模拟"宽限期已过"：把 deleted_at 拨到 10 天前
        conn = db.get_conn()
        with db._conn_lock:
            old = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("UPDATE users SET deleted=1, deleted_at=? WHERE email=?",
                         (old, "victim2@test.local"))
        # 时钟参照设为正常（1 小时前），避免 M3 误拦
        hour_ago = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        with db._conn_lock:
            conn.execute("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)",
                         ("purge_users_clock", hour_ago))
        db.purge_deleted_users()
        self.assertEqual(self._req_count("victim2@test.local"), 0,
                         "purge_deleted_users 后 user_delete_requests 不应残留明文邮箱（M4a）")

    def test_purge_deleted_users_hard_clears_delete_requests(self):
        db.create_user("victim3@test.local", "hash", role="user",
                       created_at="2026-08-28 00:00:00", pw_version=1)
        db.record_user_delete_request("victim3@test.local", ip_hash="x", kind="restore")
        conn = db.get_conn()
        with db._conn_lock:
            conn.execute("UPDATE users SET deleted=1, deleted_at=? WHERE email=?",
                         ("2026-08-28 00:00:00", "victim3@test.local"))
            # 遗留未提交事务现在会被安全回滚（不再被盲提交）——
            # 夹具自提交，不依赖任何后续函数的隐式提交
            conn.commit()
        purged = db.purge_deleted_users_hard(["victim3@test.local"])
        self.assertEqual(purged, ["victim3@test.local"])
        self.assertEqual(self._req_count("victim3@test.local"), 0,
                         "管理员手动清除后冷却计数也应连带清除（M4a）")


class LastAdminAndUpdateUserTest(unittest.TestCase):
    """C-M1 update_user 行数 + C-M3 最后管理员事务内复核。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-lastadmin-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\nYIBAN_AUDIT_KEY={AUDIT_KEY}\n")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_AUDIT_KEY"] = AUDIT_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        global db
        import db

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for key in ("YIBAN_ACCOUNTS_KEY", "YIBAN_AUDIT_KEY", "YIBAN_ENV_FILE",
                    "YIBAN_DB_FILE", "YIBAN_STATE_DIR"):
            os.environ.pop(key, None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                os.remove(self.db_file + suffix)
        db.init_db(cleanup=False)

    # ---------------- C-M1 ----------------
    def test_update_user_nonexistent_returns_zero(self):
        affected = db.update_user("nobody@test.local", {"mail_notify": 0})
        self.assertEqual(affected, 0, "不存在的邮箱 update_user 必须返回 0（原返回 None 静默 no-op）")

    def test_update_user_existing_returns_one(self):
        db.create_user("someone@test.local", "hash", role="user",
                       created_at="2026-08-28 00:00:00", pw_version=1)
        affected = db.update_user("someone@test.local", {"mail_notify": 0})
        self.assertEqual(affected, 1)

    def test_update_user_soft_deleted_returns_zero(self):
        """已注销用户行也不可更新（deleted=0 过滤），返回 0。"""
        db.create_user("gone@test.local", "hash", role="user",
                       created_at="2026-08-28 00:00:00", pw_version=1)
        db.soft_delete_user_with_accounts("gone@test.local")
        affected = db.update_user("gone@test.local", {"mail_notify": 0})
        self.assertEqual(affected, 0)

    # ---------------- C-M3 ----------------
    def test_last_registered_admin_cannot_soft_delete(self):
        db.create_user("admin1@test.local", "hash", role="admin",
                       created_at="2026-08-28 00:00:00", pw_version=1)
        with self.assertRaises(db.LastAdminError):
            db.soft_delete_user_with_accounts("admin1@test.local")
        # 注销未发生
        rows = [u for u in db.load_users(include_deleted=True)
                if u["email"] == "admin1@test.local"]
        self.assertTrue(rows and not rows[0].get("deleted"), "最后管理员不得被软注销")

    def test_second_admin_can_soft_delete(self):
        db.create_user("admin1@test.local", "hash", role="admin",
                       created_at="2026-08-28 00:00:00", pw_version=1)
        db.create_user("admin2@test.local", "hash", role="admin",
                       created_at="2026-08-28 00:00:00", pw_version=1)
        self.assertTrue(db.soft_delete_user_with_accounts("admin1@test.local"),
                        "存在第二个管理员时允许注销")
        rows = [u for u in db.load_users(include_deleted=True)
                if u["email"] == "admin1@test.local"]
        self.assertTrue(rows[0].get("deleted"))

    def test_regular_user_soft_delete_unaffected(self):
        db.create_user("admin1@test.local", "hash", role="admin",
                       created_at="2026-08-28 00:00:00", pw_version=1)
        db.create_user("user1@test.local", "hash", role="user",
                       created_at="2026-08-28 00:00:00", pw_version=1)
        self.assertTrue(db.soft_delete_user_with_accounts("user1@test.local"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
