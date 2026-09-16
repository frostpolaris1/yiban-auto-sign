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
    "scratch-": "loadtest 的临时 SQLite 文件（outdir）",
    "signin-": "loadtest 的每轮日志文件（logdir）",
    "verify-job-": "校验任务的线程名（不是文件）",
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
