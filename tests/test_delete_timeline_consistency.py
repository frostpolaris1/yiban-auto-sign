# -*- coding: utf-8 -*-
"""注销（用户软删）与账号软删的**保留期与恢复口径**一致性。

标签：L · 注销与软删
覆盖：用户先自删账号再注销这一路径下，账号行的到期时刻与用户反悔窗口是否一致；
    `restore_user` 恢复哪一行；`_purge_expired_deleted` 对"owner 已注销但仍 in
    窗口"的账号的豁免。
对应实现：`yiban/store/users.py`（`restore_user`、`purge_deleted_users`）与
    `yiban/store/cleanup.py`（`_purge_expired_deleted`、`purge_expired_deleted_accounts`）；
    触发入口在 `web/app.py` 的注销与恢复端点。
关键断言：两条路径各自的触发条件必须分清——**软删可恢复**（本人撤销/管理员恢复，
    按时刻等值匹配会漏行）与**到期物理清除**（按 deleted_at 独立到期）；
    原实现让账号先于用户被物理清除，用户回来时"一个账号都没有"。
依赖：临时 sqlite + 手工构造时间戳（宽限期边界用文件名/字段日期判定），
    Flask test client 或直接调数据层；不触网、不发信。
"""
import contextlib
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64
EMAIL = "timeline@test.local"
PHONE_A = "13800138011"
PHONE_B = "13800138012"
STAMP = "%Y-%m-%d %H:%M:%S"


def _stamp(delta_days=0):
    return (datetime.now() + timedelta(days=delta_days)).strftime(STAMP)


class DeleteTimelineConsistencyTest(unittest.TestCase):
    """db 层直测：保留期豁免 + 单行恢复的两侧边界。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-timeline-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\nYIBAN_ADMIN_USER=admin\n")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        global db  #先 global 再 import：setUpClass 里绑的模块名要让各用例方法看得见
        import db

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        if db._conn is not None:
            db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):  #WAL/SHM 一起删：留着 sidecar 会让下一个用例读到上一个用例的库内容
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        with open(self.accounts_file, "w", encoding="utf-8") as f:
            json.dump([], f)
        db.init_db(self.db_file, migrate_from=self.accounts_file, env_file=self.env_file)  #走真迁移而不是手写建表：owner 的部分唯一索引只存在于真 schema 里
        db.create_user(EMAIL, "x" * 20)
        self.a = self._add(PHONE_A)

    # ---- 工具 ----
    def _add(self, phone):
        """加一条生效账号行；`add_account` 返回新行 id。"""
        return db.add_account({
            "name": "账号" + phone[-2:], "phone": phone, "password": "p",
            "phone_model": "", "phone_code": "", "owner": EMAIL, "status": "active",
        })

    def _add_deleted_row(self, phone, deleted_at, deleted_by):
        """加一条**软删**账号行（owner 唯一索引只约束未删除行，故先把 A 暂挂再放回）。"""
        a_live = not self._row(self.a)["deleted"]  #先把生效行暂挂再插软删行：唯一索引只管未删除行，不暂挂就插不进去
        if a_live:
            self._set_deleted(self.a, True, deleted_at=_stamp(), deleted_by="")
        row_id = self._add(phone)
        self._set_deleted(row_id, True, deleted_at=deleted_at, deleted_by=deleted_by)
        if a_live:
            self._set_deleted(self.a, False)
        return row_id

    def _set_deleted(self, account_id, deleted, deleted_at="", deleted_by=""):
        db.set_account_deleted(account_id, deleted, deleted_at=deleted_at, deleted_by=deleted_by)

    def _row(self, account_id):
        with db._conn_lock:
            return dict(db.get_conn().execute(
                "SELECT id, deleted, deleted_at, deleted_by FROM accounts WHERE id=?",
                (account_id,),
            ).fetchone())

    def _user_row(self):
        with db._conn_lock:
            return dict(db.get_conn().execute(
                "SELECT id, deleted, deleted_at FROM users WHERE email=?", (EMAIL,)
            ).fetchone())

    def _cancel(self):
        self.assertTrue(db.soft_delete_user_with_accounts(EMAIL))  #注销走这个函数：它决定"注销当时哪一行在生效"的时刻，恢复语义全看它

    def _count(self, phone):
        with db._conn_lock:
            return db.get_conn().execute(
                "SELECT COUNT(*) FROM accounts WHERE phone=?", (phone,)
            ).fetchone()[0]

    # ---- 1. 原始症状：唯一账号先自删、再注销，恢复后必须有账号 ----
    def test_only_account_deleted_before_cancel_comes_back(self):
        self._set_deleted(self.a, True, deleted_at=_stamp(-3), deleted_by=EMAIL)
        self._cancel()
        self.assertTrue(db.restore_user(EMAIL))
        row = self._row(self.a)
        self.assertEqual(row["deleted"], 0, "恢复用户必须把账号一起带回来")
        self.assertEqual(row["deleted_by"], "")

    # ---- 2. 自删行活到用户的反悔窗口结束（不被提前物理清除） ----
    def test_prior_self_deleted_row_survives_user_grace_window(self):
        # 账号 30 天前自删（早已越过自身 7 天保留期），此后用户注销
        self._set_deleted(self.a, True, deleted_at=_stamp(-30), deleted_by=EMAIL)
        self._cancel()
        db.purge_expired_deleted_accounts()
        self.assertEqual(self._row(self.a)["deleted"], 1,
                         "owner 已注销的行不该在用户仍可反悔时被物理清除")
        self.assertTrue(db.restore_user(EMAIL))
        self.assertEqual(self._row(self.a)["deleted"], 0)

    # ---- 3. 用户行被清除后账号不再豁免（不无界驻留） ----
    def test_row_is_purged_after_owner_is_hard_deleted(self):
        self._set_deleted(self.a, True, deleted_at=_stamp(-30), deleted_by=EMAIL)
        self._cancel()
        db.purge_expired_deleted_accounts()          # 用户仍在窗口内 → 豁免
        self.assertEqual(self._row(self.a)["deleted"], 1)
        db.purge_deleted_users_hard([EMAIL])         # 用户宽限期满被物理清除
        db.purge_expired_deleted_accounts()
        self.assertEqual(self._count(PHONE_A), 0, "用户行清除后账号必须随之清除，不能无界驻留")

    # ---- 4. 旧版本写下的错位存量行同样能恢复（无需数据迁移） ----
    def test_restore_covers_legacy_mismatched_rows(self):
        self._set_deleted(self.a, True, deleted_at=_stamp(-3), deleted_by=EMAIL)
        with db._conn_lock, db.get_conn() as conn:
            conn.execute("UPDATE users SET deleted=1, deleted_at=? WHERE email=?",
                         (_stamp(-1), EMAIL))
        self.assertTrue(db.restore_user(EMAIL))
        self.assertEqual(self._row(self.a)["deleted"], 0)

    # ---- 5. 多行候选只恢复"注销当时生效"的那一行（且不撞 owner 唯一索引） ----
    def test_restore_picks_single_latest_row(self):
        # 真实次序：A 自删（-5）→ 建 B → B 自删（-3）→ 注销（无生效行）
        self._set_deleted(self.a, True, deleted_at=_stamp(-5), deleted_by=EMAIL)
        b = self._add_deleted_row(PHONE_B, _stamp(-3), EMAIL)
        self._cancel()
        self.assertTrue(db.restore_user(EMAIL))
        self.assertEqual(self._row(b)["deleted"], 0, "应恢复最近一次使用后自删的那一行")
        self.assertEqual(self._row(self.a)["deleted"], 1,
                         "更早自删的行保持软删（用户在「我的账号」逐个撤销）")

    # ---- 6. 管理员删除的行不复活、时刻不动 ----
    def test_admin_deleted_account_is_not_revived(self):
        admin_at = _stamp(-2)
        b = self._add_deleted_row(PHONE_B, admin_at, "admin")
        self._set_deleted(self.a, True, deleted_at=_stamp(-3), deleted_by=EMAIL)
        self._cancel()
        self.assertEqual(self._row(b)["deleted_at"], admin_at,
                         "管理员删除的行不该被注销路径改动")
        self.assertTrue(db.restore_user(EMAIL))
        row = self._row(b)
        self.assertEqual(row["deleted"], 1, "管理员清退的账号不该随用户恢复复活")
        self.assertEqual(row["deleted_by"], "admin")

    # ---- 7. 僵尸行（deleted_at 为空）不纳入恢复 ----
    def test_empty_deleted_at_row_is_not_revived(self):
        b = self._add_deleted_row(PHONE_B, "", "")
        self._cancel()
        self.assertTrue(db.restore_user(EMAIL))
        self.assertEqual(self._row(b)["deleted"], 1,
                         "无法判定归属的 deleted_at='' 行不复活")

    # ---- 8. 注销只给"当时生效"的行打时刻，不覆盖更早的自删时刻 ----
    def test_cancel_does_not_restamp_prior_self_deleted_rows(self):
        deleted_at = _stamp(-3)
        self._set_deleted(self.a, True, deleted_at=deleted_at, deleted_by=EMAIL)
        self._cancel()
        user_at = self._user_row()["deleted_at"]
        self.assertEqual(self._row(self.a)["deleted_at"], deleted_at)
        self.assertNotEqual(self._row(self.a)["deleted_at"], user_at,
                            "自删时刻是「注销当时哪一行在生效」的唯一线索，不能覆盖")

    # ---- 9. 豁免随用户宽限期失效（cron-only 部署也不会无界驻留） ----
    def test_exemption_expires_with_user_grace_window(self):
        """只跑账号清理（signin 的 cron 路径不调 purge_deleted_users）时，
        用户宽限期一过账号必须能被清除——否则豁免会变成新的无界驻留。"""
        self._cancel()
        old = _stamp(-db.SOFT_DELETE_RETENTION_DAYS - 1)
        with db._conn_lock, db.get_conn() as conn:
            conn.execute("UPDATE accounts SET deleted_at=? WHERE owner=?", (old, EMAIL))
            conn.execute("UPDATE users SET deleted_at=? WHERE email=?", (old, EMAIL))
        db.purge_expired_deleted_accounts()      # 只清账号，不清用户行
        self.assertEqual(self._count(PHONE_A), 0,
                         "用户宽限期已过的账号不得被无限期豁免")


if __name__ == "__main__":
    unittest.main(verbosity=2)
