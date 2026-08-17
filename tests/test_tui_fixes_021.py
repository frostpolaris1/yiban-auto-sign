# -*- coding: utf-8 -*-
"""Task 6：TUI 与辅助脚本修复测试。

覆盖 brief 中：
- H5：TUI 编辑账号时保留原账号 owner/status/reject_reason/deleted/deleted_at/user_paused；
- H11：_write_env_int_locked 写后内容正确，POSIX 下校验 0600 权限位；
- M20：generate_demo_data --yes 保护（抽纯函数）；
- M21/低项：db_export --env 透传与输出文件 0600（Windows 跳过权限断言）。
"""
import contextlib
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))

import db  # noqa: E402
import db_export  # noqa: E402
import generate_demo_data  # noqa: E402

import tui.app as tui_app  # noqa: E402

TEST_KEY = "a" * 64


class TuiFixes021Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="yiban-tui-fixes-021-")
        self.addCleanup(self.tmp.cleanup)
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None

    # ---- H5：编辑账号合并保留原账号管理字段 ----
    def test_merge_account_edit_preserves_admin_fields(self):
        old = {
            "name": "旧名称",
            "phone": "13800000001",
            "password": "old-pass",
            "phone_model": "OldPhone",
            "phone_code": "old-code",
            "owner": "user@test.local",
            "status": "rejected",
            "reject_reason": "资料不完整",
            "deleted": True,
            "deleted_at": "2026-08-01 10:00:00",
            "user_paused": True,
        }
        form = {
            "name": "新名称",
            "phone": "13800000002",
            "password": " new-pass ",
            "phone_model": "NewPhone",
            "phone_code": "new-code",
        }

        merged = tui_app._merge_account_edit(old, form)

        self.assertEqual(merged["name"], "新名称")
        self.assertEqual(merged["phone"], "13800000002")
        self.assertEqual(merged["password"], " new-pass ", "密码应保存原始值，不做 strip")
        self.assertEqual(merged["phone_model"], "NewPhone")
        self.assertEqual(merged["phone_code"], "new-code")
        self.assertEqual(merged["owner"], "user@test.local")
        self.assertEqual(merged["status"], "rejected")
        self.assertEqual(merged["reject_reason"], "资料不完整")
        self.assertIs(merged["deleted"], True)
        self.assertEqual(merged["deleted_at"], "2026-08-01 10:00:00")
        self.assertIs(merged["user_paused"], True)

    def test_merge_account_edit_returns_form_when_no_old(self):
        form = {"name": "新", "phone": "13800000003", "password": "p"}
        merged = tui_app._merge_account_edit({}, form)
        self.assertEqual(merged, form)

    # ---- H11：_write_env_int_locked 原子写内容正确 + POSIX 0600 ----
    def test_write_env_int_locked_content_and_mode(self):
        env_file = os.path.join(self.tmp.name, ".env")
        with open(env_file, "w", encoding="utf-8") as f:
            f.write("YIBAN_OTHER=1\n")

        tui_app._write_env_int_locked(env_file, "YIBAN_START_DELAY_MAX", 30)

        with open(env_file, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("YIBAN_OTHER=1", content)
        self.assertIn("YIBAN_START_DELAY_MAX=30", content)
        self.assertFalse(os.path.exists(env_file + ".tmp"), "临时文件应已通过 os.replace 替换")
        if os.name != "nt":
            mode = stat.S_IMODE(os.stat(env_file).st_mode)
            self.assertEqual(mode, 0o600, f".env 临时文件应 0600，实际 {oct(mode)}")

    def test_write_env_int_locked_removes_zero(self):
        env_file = os.path.join(self.tmp.name, ".env")
        with open(env_file, "w", encoding="utf-8") as f:
            f.write("YIBAN_START_DELAY_MAX=10\nKEEP=1\n")

        tui_app._write_env_int_locked(env_file, "YIBAN_START_DELAY_MAX", 0)

        with open(env_file, encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn("YIBAN_START_DELAY_MAX=", content)
        self.assertIn("KEEP=1", content)

    # ---- M21/低项：db_export --env 透传 + 输出文件 0600 ----
    def test_db_export_passes_env_and_creates_0600_files(self):
        out_dir = os.path.join(self.tmp.name, "out")
        db_file = os.path.join(self.tmp.name, "yiban.db")
        env_file = os.path.join(self.tmp.name, ".env")
        with open(env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")

        with (
            mock.patch.object(db_export.db, "init_db", return_value=None) as init_db,
            mock.patch.object(db_export.db, "load_accounts_raw", return_value=[]),
            mock.patch.object(db_export.db, "load_users", return_value=[]),
        ):
            db_export.main(["--out", out_dir, "--db", db_file, "--env", env_file])

        init_db.assert_called_once_with(env_file=env_file)
        for name in ("accounts.json", "users.json"):
            path = os.path.join(out_dir, name)
            self.assertTrue(os.path.exists(path), f"{name} 应已生成")
            if os.name != "nt":
                mode = stat.S_IMODE(os.stat(path).st_mode)
                self.assertEqual(mode, 0o600, f"{name} 应 0600，实际 {oct(mode)}")

    # ---- M20：generate_demo_data --yes 保护 ----
    def test_demo_db_allowed_only_default_or_yes(self):
        self.assertTrue(generate_demo_data._is_allowed_demo_db("demo-log/demo.db"))
        self.assertFalse(generate_demo_data._is_allowed_demo_db("other/demo.db"))
        self.assertTrue(generate_demo_data._is_allowed_demo_db("other/demo.db", yes=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
