# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""状态件路径与清理加固批（census-P2-sched-run/sched-slot-kind, ba-p10-03/04）。

标签：B · 状态件与清理
覆盖：
  ① `sched-run-<day>.json` 只有一个路径定义点：写入、存在性、completed 真值三处
     必须走同一函数（census-P2-sched-run）。
  ② `sched-slot-<kind>-<day>.json` 的 kind 段有枚举；清理侧按完整
     `sched-slot-<kind>-` 前缀取日期，未知 kind 不被当成按日文件
     （census-P2-sched-slot-kind）。
  ③ 容器调度器的清理把 `.env` 里的保留期传给 `state_gc.sweep`（ba-p10-03）。
  ④ 状态目录不可读时 `plan/sweep` 跳过并继续，不把 PermissionError 穿透到 CLI
     （ba-p10-04）。
  ⑤ 兜底心跳写失败留一条 warning，不再零留痕（census-P2-fallback-alive 的实现半）。

依赖：pytest 自带的 monkeypatch/mock；不触真实状态目录。
"""
import datetime
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHED = os.path.join(BASE, "docker", "scheduler.py")

from yiban.adapters.statefiles import state_gc, state_io  # noqa: E402


class SchedRunSinglePathTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sched-run-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_writer_reader_and_exists_share_one_path(self):
        day = datetime.datetime(2026, 1, 2, 7, 0, 0)
        with mock.patch.object(state_io, "_state_dir", return_value=self.tmp), \
                mock.patch.object(state_io.clock, "now", return_value=day):
            state_io._write_sched_done({"completed": True})
            path = state_io.sched_run_path("2026-01-02", state_dir=self.tmp)
            self.assertEqual(os.path.join(self.tmp, "sched-run-2026-01-02.json"), path)
            self.assertTrue(os.path.isfile(path),
                            f"写入点没有落在 sched_run_path 上：{os.listdir(self.tmp)}")
            self.assertTrue(state_io._sched_marker_exists(),
                            "存在性判据没有落在 sched_run_path 上")
            self.assertTrue(state_io.full_run_done_today(self.tmp, "2026-01-02"))


class SchedSlotKindTest(unittest.TestCase):
    def test_allowed_kinds_are_declared(self):
        self.assertEqual({"first", "second", "backup"}, set(state_gc.SCHED_SLOT_KINDS),
                         "sched-slot 的 kind 枚举缺失或漂移")

    def test_unknown_kind_is_not_a_daily_artifact(self):
        got, date = state_gc.match("sched-slot-bogus-2026-01-02.json")
        self.assertIsNone(got, "未知 kind 被当成按日状态件（会被清理/计数）")
        self.assertIsNone(date)

    def test_known_kind_is_matched_with_its_own_prefix(self):
        got, date = state_gc.match("sched-slot-first-2026-01-02.json")
        self.assertIsNotNone(got)
        self.assertEqual("2026-01-02", date)

    def test_scheduler_rejects_unknown_kind(self):
        spec = importlib.util.spec_from_file_location("yiban_sched_slot", SCHED)
        mod = importlib.util.module_from_spec(spec)
        old = os.environ.get("YIBAN_STATE_DIR")
        try:
            os.environ.setdefault("YIBAN_STATE_DIR", tempfile.mkdtemp(prefix="sched-slot-"))
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
            with self.assertRaises(ValueError):
                mod._slot_marker("bogus")
        finally:
            if old is None:
                os.environ.pop("YIBAN_STATE_DIR", None)
            else:
                os.environ["YIBAN_STATE_DIR"] = old


class ContainerCleanupPassesDotenvTest(unittest.TestCase):
    def test_sweep_receives_dotenv_retention(self):
        tmp = tempfile.mkdtemp(prefix="sched-cleanup-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        env_file = os.path.join(tmp, ".env")
        with open(env_file, "w", encoding="utf-8") as f:
            f.write("YIBAN_RETENTION_DAYS=99\n"
                    "YIBAN_SNAPSHOT_RETENTION_DAYS=11\n")
        old = {k: os.environ.get(k) for k in ("YIBAN_ENV_FILE", "YIBAN_STATE_DIR")}
        captured = {}

        def _fake_sweep(state_dir, log_dir=None, env=None, now=None):
            captured["env"] = dict(env or {})
            return 0, []

        try:
            os.environ["YIBAN_ENV_FILE"] = env_file
            os.environ.setdefault("YIBAN_STATE_DIR", tmp)
            spec = importlib.util.spec_from_file_location("yiban_sched_cleanup", SCHED)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
            with mock.patch.object(state_gc, "sweep", side_effect=_fake_sweep):
                mod._cleanup_state()
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        env = captured.get("env", {})
        self.assertEqual("99", str(env.get("YIBAN_RETENTION_DAYS", "")).strip(),
                         f"调度器清理没把 .env 的保留期传下去：{env}")
        self.assertEqual("11", str(env.get("YIBAN_SNAPSHOT_RETENTION_DAYS", "")).strip(),
                         f"调度器清理没把 .env 的快照保留期传下去：{env}")


class UnreadableStateDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="state-unreadable-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_plan_skips_when_listdir_fails(self):
        with mock.patch.object(state_gc.os, "listdir", side_effect=PermissionError("denied")):
            n, detail = state_gc.plan(self.tmp, now=datetime.datetime(2026, 1, 2))
        self.assertEqual((0, []), (n, detail),
                         "目录不可读时 plan 必须跳过并继续，而不是把 OSError 穿透")

    def test_sweep_skips_when_listdir_fails(self):
        with mock.patch.object(state_gc.os, "listdir", side_effect=PermissionError("denied")):
            removed, detail = state_gc.sweep(self.tmp, now=datetime.datetime(2026, 1, 2))
        self.assertEqual((0, []), (removed, detail),
                         "目录不可读时 sweep 必须跳过并继续，而不是把 OSError 穿透")


class FallbackAliveWarningTest(unittest.TestCase):
    def test_write_failure_logs_one_warning(self):
        state_io._FALLBACK_ALIVE_WARNED = False
        with mock.patch.object(state_io, "_write_private_json",
                               side_effect=OSError("disk full")), \
                mock.patch.object(state_io.logger, "warning") as warn:
            state_io._write_fallback_alive()
            state_io._write_fallback_alive()
        self.assertGreaterEqual(warn.call_count, 1,
                                "兜底心跳写失败零留痕（应至少一条 warning）")


class SignStatePathSingleDefinitionTest(unittest.TestCase):
    def test_sign_state_path_is_one_function(self):
        self.assertEqual("sign-state-2026-01-02.json",
                         os.path.basename(state_io.sign_state_path("2026-01-02")))
        # 形状与默认目录的唯一来源是 env_io（census-P2-sign-state）：替换定义点后，
        # state_io 的产物必须跟着变（派生 vs 各写一份，只有这条能分开）。
        from yiban.adapters.config import env_io
        with mock.patch.object(env_io, "sign_state_path",
                               return_value="/tmp/x/SENTINEL.json") as fn:
            self.assertEqual("/tmp/x/SENTINEL.json",
                             state_io._sign_state_path("2026-01-02"))
        self.assertEqual("2026-01-02", fn.call_args.args[0])
        self.assertTrue(str(fn.call_args.kwargs.get("state_dir", "")).strip(),
                        "state_io 没把缺省状态目录传给定义点")


if __name__ == "__main__":
    unittest.main(verbosity=2)
