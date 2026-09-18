# -*- coding: utf-8 -*-
"""设置项权限三档（`MASTER_ONLY_KEYS` / `GATED_KEYS`）的行为契约。

被修的现实缺陷：档位判定只写在 `api_settings_save` 的函数体里（一份手写 403 清单），
前端另有自己的收控件清单，两处各写一遍必然漂移；且"哪些改动足以让全站静默漏签"没有
单一口径——周末开关、签到窗口、边缘裁切与普通字段同权，普通管理员一次误操作即可让
周六日全体漏签。

本文件钉住重排后的口径（分档判据 = 影响半径 × 能否造成静默漏签）：

1. **档位单源**：路由与告警/审计都读同一对常量；元测试比对常量表、两表互斥，并要求
   每个档位键的现值都能读回（读不回就没法判"值变没变"）。
2. **A 档**：仅主管理员；值真变化时还要当次口令（`always_required=True`，豁免不适用）。
3. **B 档**：任意管理员可写；值真变化时过统一门禁（可用豁免）；无口令即拒。
4. **急停例外**：`global_pause` 0→1 任意管理员可做（当次口令 + 占用高危额度 +
   紧急告警）；1→0 恢复仍仅主管理员。
5. **变更告警合并**：一次请求把全部真变化的档位键合成**一条**通知（一键一封会被
   拿来刷告警额度），A 档 urgent 不 force，只有"全停急停"才 force。

全程 mock / 纯本地（Flask test client），无任何网络请求。
用法（项目根目录）：
    python -m pytest tests/test_settings_tiers_94.py -v
"""
import contextlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64
ADMIN_PASS = "TestPass1234!"
REG_ADMIN = "reg-admin@test.local"
REG_PASS = "RegPass5678!"

# 规格表（权限三档的判据原文）——与 webapp 常量比对，任何一方单方面改动都会被这条用例咬住
SPEC_MASTER_ONLY = {
    "sign_window", "window_edge_sec", "edge_front_sec", "edge_back_sec",
    "sunday_sign", "saturday_sign", "registration_pause",
    "start_delay_max", "gap_max", "max_users", "max_accounts",
    "account_verify", "probe_enable", "probe_time", "probe_interval",
}
SPEC_GATED = {"sign_order", "sign_dist", "sign_mode", "allow_time_pref"}
# 每个 A 档键的一份「会造成真变更」的取值（用于越界尝试）
A_SAMPLES = {
    "sign_window": "03:00 ~ 04:00",
    "window_edge_sec": 90,
    "edge_front_sec": 90,
    "edge_back_sec": 120,
    "sunday_sign": 1,
    "saturday_sign": 1,
    "registration_pause": 1,
    "start_delay_max": 120,
    "gap_max": 30,
    "max_users": 123,
    "max_accounts": 45,
    "account_verify": 1,
    "probe_enable": 1,
    "probe_time": "06:00",
    "probe_interval": "3",
}
# 每个 B 档键的一份「会造成真变更」的取值（默认生效值 sequence / uniform / "" / 0）
B_SAMPLES = {
    "sign_order": "random",
    "sign_dist": "normal",
    "sign_mode": "random",
    "allow_time_pref": 1,
}


class _TierBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-tiers-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
            )
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_USERS_FILE"] = os.path.join(cls.tmp, "users.json")
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        os.environ["YIBAN_DISABLE_PURGE_LOOP"] = "1"
        global db
        import db
        # 独立名字加载 web/app.py：它在导入期把 ENV_FILE 等读成模块级常量，与别的测试
        # 文件共用同一模块对象会读到另一个 .env（单跑绿、全量红的老坑）
        spec = importlib.util.spec_from_file_location(
            "webapp_settings_tiers", os.path.join(BASE, "web", "app.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["webapp_settings_tiers"] = mod
        with contextlib.suppress(Exception):
            spec.loader.exec_module(mod)
        cls.webapp = mod

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE",
                  "YIBAN_USERS_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR",
                  "YIBAN_LOG_FILE", "YIBAN_DISABLE_PURGE_LOOP"):
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
        with open(self.accounts_file, "w", encoding="utf-8") as f:
            json.dump([], f)
        db.init_db(self.db_file, migrate_from=self.accounts_file, env_file=self.env_file)
        # 每例回到「全部档位键未配置 = 生效值即默认」的基线
        self.webapp.write_env_batch(self.env_file, {
            "YIBAN_GLOBAL_PAUSE": "", "YIBAN_REGISTRATION_PAUSE": "",
            "YIBAN_SUNDAY_SIGN": "", "YIBAN_SATURDAY_SIGN": "",
            "YIBAN_SIGN_ORDER": "", "YIBAN_SIGN_DIST": "", "YIBAN_SIGN_MODE": "",
            "YIBAN_ALLOW_TIME_PREF": "", "YIBAN_START_DELAY_MAX": "",
            "YIBAN_ACCOUNT_GAP_MAX": "", "YIBAN_WINDOW_EDGE_FRONT_SEC": "",
            "YIBAN_WINDOW_EDGE_BACK_SEC": "", "YIBAN_WINDOW_EDGE_SEC": "",
            "YIBAN_MAX_USERS": "", "YIBAN_MAX_ACCOUNTS": "",
            "YIBAN_ACCOUNT_VERIFY": "", "YIBAN_PROBE_ENABLE": "",
            "YIBAN_PROBE_TIME": "", "YIBAN_PROBE_INTERVAL_DAYS": "",
            "YIBAN_SIGN_START": "", "YIBAN_SIGN_END": "",
            "YIBAN_PW_CONFIRM_TTL": "", "YIBAN_PW_CONFIRM_COOLDOWN_SEC": "",
            "YIBAN_ADMIN_DELETE_COOLDOWN_SEC": "", "YIBAN_ADMIN_DELETE_MAX": "",
        })
        self.alerts = []
        patcher = mock.patch.object(
            self.webapp, "send_notification",
            side_effect=lambda t, c, urgent=False, force=False, ledger=None:
            self.alerts.append((t, c, urgent, force)))
        patcher.start()
        self.addCleanup(patcher.stop)

    # ---- 工具 ----
    def _client(self, username, password):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        c.csrf = c.get("/api/me").get_json()["csrf_token"]
        return c

    def _master(self):
        return self._client("admin", ADMIN_PASS)

    def _reg_admin(self):
        db.create_user(REG_ADMIN, self.webapp.generate_password_hash(REG_PASS),
                       role="admin")
        return self._client(REG_ADMIN, REG_PASS)

    def _hdr(self, c):
        return {"X-CSRF-Token": c.csrf}

    def _save(self, c, payload):
        return c.post("/api/settings", json=payload, headers=self._hdr(c))

    def _env_has(self, needle):
        with open(self.env_file, encoding="utf-8-sig") as f:
            return needle in f.read()

    def _audit_rows(self, action):
        return [dict(r) for r in db.get_conn().execute(
            "SELECT detail FROM audit_logs WHERE action=?", (action,)).fetchall()]


class TierTableMetaTest(_TierBase):
    """档位表单源：常量对 == 规格表，且每个档位键的现值都读得回来。"""

    def test_tier_sets_match_spec(self):
        self.assertEqual(set(self.webapp.MASTER_ONLY_KEYS), SPEC_MASTER_ONLY,
                         "A 档表被单方面改动：规格与常量必须同时改")
        self.assertEqual(set(self.webapp.GATED_KEYS), SPEC_GATED,
                         "B 档表被单方面改动：规格与常量必须同时改")
        self.assertFalse(set(self.webapp.MASTER_ONLY_KEYS) & set(self.webapp.GATED_KEYS),
                         "一个键不可能既是 A 档又是 B 档")
        # 急停靠「按变更方向分流」实现，混进任一张表都会让方向语义失效
        self.assertNotIn("global_pause", self.webapp.MASTER_ONLY_KEYS)
        self.assertNotIn("global_pause", self.webapp.GATED_KEYS)
        self.assertEqual(self.webapp.GLOBAL_PAUSE_KEY, "global_pause")

    def test_every_tier_key_is_readable_back(self):
        """档位键必须能读回现值：读不回就没法判「这次到底变没变」，门禁会被静默跳过。

        两个容量上限挂在 capacity.users_max / accounts_max 下（非顶层键），单独放行。
        """
        c = self._master()
        payload = c.get("/api/settings", headers=self._hdr(c)).get_json()
        nested = {"max_users": "users_max", "max_accounts": "accounts_max"}
        for key in set(self.webapp.MASTER_ONLY_KEYS) | set(self.webapp.GATED_KEYS):
            if key in nested:
                self.assertIn(nested[key], payload["capacity"], f"{key} 现值读不回")
            else:
                self.assertIn(key, payload, f"{key} 现值读不回")

    def test_effective_values_cover_both_tiers(self):
        cur = self.webapp._settings_effective_values(self.env_file)
        keys = (set(self.webapp.MASTER_ONLY_KEYS) | set(self.webapp.GATED_KEYS)
                | {self.webapp.GLOBAL_PAUSE_KEY})
        self.assertEqual(keys - set(cur), set(), "有档位键没有现值口径")
        # 现值必须是「生效值」而不是「键在不在」：未配置的延迟要报默认值
        self.assertEqual(cur["gap_max"], str(self.webapp.DEFAULT_ACCOUNT_GAP_MAX))
        self.assertEqual(cur["sign_order"], "sequence")


class MasterOnlyTierTest(_TierBase):
    """A 档：普通管理员连口令都不给过；主管理员无口令也不给过。"""

    def test_regular_admin_denied_on_every_master_key(self):
        c = self._reg_admin()
        for key, value in sorted(A_SAMPLES.items()):
            with self.subTest(key=key):
                r = self._save(c, {key: value, "confirm_password": REG_PASS})
                self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        self.assertFalse(self._env_has("YIBAN_SUNDAY_SIGN"), "越界请求不得落盘")
        self.assertEqual(self.alerts, [], "被拒的越界请求不得发出变更告警")

    def test_promoted_keys_are_named_individually(self):
        """本次上收的键逐一点名（周末×2 / 窗口 / 边缘）——防「改了表漏了路由」。"""
        c = self._reg_admin()
        for key in ("sunday_sign", "saturday_sign", "sign_window",
                    "edge_front_sec", "edge_back_sec"):
            with self.subTest(key=key):
                r = self._save(c, {key: A_SAMPLES[key], "confirm_password": REG_PASS})
                self.assertEqual(r.status_code, 403, r.get_data(as_text=True))

    def test_demoted_keys_are_no_longer_denied(self):
        """本次下放的四个键（排序/分布/模式/自选）不得再吃「仅主管理员」403。"""
        c = self._reg_admin()
        for key in sorted(B_SAMPLES):
            with self.subTest(key=key):
                r = self._save(c, {key: B_SAMPLES[key], "confirm_password": REG_PASS})
                self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_master_needs_password_of_this_request(self):
        """主管理员改 A 档值：无口令 403、当次正确口令 200。"""
        c = self._master()
        self.assertEqual(self._save(c, {"sunday_sign": 1}).status_code, 403)
        self.assertFalse(self._env_has("YIBAN_SUNDAY_SIGN=1"), "被拒不得落盘")
        r = self._save(c, {"sunday_sign": 1, "confirm_password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(self._env_has("YIBAN_SUNDAY_SIGN=1"))

    def test_master_same_value_needs_no_password(self):
        """值未变不要求口令（沿用系统开关门既有语义），且现值是服务端现读的。"""
        c = self._master()
        self.assertEqual(self._save(c, {"sunday_sign": 0}).status_code, 200)
        # 请求自称「从 1 改成 1」也没用：现读值是 0，这就是真变更 → 仍要口令
        self.assertEqual(self._save(c, {"sunday_sign": 1, "old_sunday_sign": 1}).status_code,
                         403)

    def test_exemption_does_not_cover_master_only_keys(self):
        """A 档不吃短时豁免：刚复核过的同一出口，改另一档 A 档仍要当次口令。"""
        c = self._master()
        self.assertEqual(self._save(c, {"sunday_sign": 1,
                                        "confirm_password": ADMIN_PASS}).status_code, 200)
        self.assertEqual(self._save(c, {"saturday_sign": 1}).status_code, 403,
                         "豁免只给配置类（B 档 / 执行体写）动作")


class GatedTierTest(_TierBase):
    """B 档：下放到任意管理员，真变更过统一门禁（可豁免）。"""

    def test_regular_admin_can_write_every_gated_key_with_password(self):
        c = self._reg_admin()
        for key, value in sorted(B_SAMPLES.items()):
            with self.subTest(key=key):
                r = self._save(c, {key: value, "confirm_password": REG_PASS})
                self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(self._env_has("YIBAN_SIGN_ORDER=random"))
        self.assertTrue(self._env_has("YIBAN_ALLOW_TIME_PREF=1"))

    def test_regular_admin_denied_without_password(self):
        c = self._reg_admin()
        for key, value in sorted(B_SAMPLES.items()):
            with self.subTest(key=key):
                self.assertEqual(self._save(c, {key: value}).status_code, 403)
        self.assertFalse(self._env_has("YIBAN_SIGN_DIST=normal"), "被拒不得落盘")

    def test_gated_key_honours_recent_reconfirm(self):
        """B 档真变更可被「刚复核过 + 同出口 IP」的豁免覆盖。"""
        c = self._reg_admin()
        self.assertEqual(self._save(c, {"sign_order": "random",
                                        "confirm_password": REG_PASS}).status_code, 200)
        r = self._save(c, {"sign_dist": "normal"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(self._env_has("YIBAN_SIGN_DIST=normal"))

    def test_legacy_derived_value_counts_as_unchanged(self):
        """只存旧 sign_mode 的存量配置：提交其派生排序值属「没改」，不该多一道口令。"""
        self.webapp.write_env_key(self.env_file, "YIBAN_SIGN_MODE", "random")
        c = self._reg_admin()
        r = self._save(c, {"sign_order": "random"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))


class EmergencyStopTest(_TierBase):
    """急停例外：0→1 人人可做（当次口令 + 高危额度 + 紧急告警），1→0 仅主管理员。"""

    def test_any_admin_can_stop_but_not_resume(self):
        c = self._reg_admin()
        self.assertEqual(self._save(c, {"global_pause": 1}).status_code, 403,
                         "急停同样要当次口令，不是带个会话就能按")
        r = self._save(c, {"global_pause": 1, "confirm_password": REG_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(self._env_has("YIBAN_GLOBAL_PAUSE=1"))
        # 恢复方向仍收归主管理员（普通管理员即使带口令也是 403）
        self.assertEqual(self._save(c, {"global_pause": 0,
                                        "confirm_password": REG_PASS}).status_code, 403)
        self.assertTrue(self._env_has("YIBAN_GLOBAL_PAUSE=1"), "恢复不得由普通管理员完成")

    def test_any_admin_can_stop_a_different_admins_switch(self):
        """「急停」不是「任何管理员可改任何暂停」：注册暂停仍不在急停例外里。"""
        c = self._reg_admin()
        r = self._save(c, {"registration_pause": 1, "confirm_password": REG_PASS})
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        self.assertFalse(self._env_has("YIBAN_REGISTRATION_PAUSE=1"))

    def test_same_value_pause_needs_no_password(self):
        """已处于急停态时再交一次 1：值未变 → 不进门禁、不占额度。"""
        self.webapp.write_env_batch(self.env_file, {"YIBAN_GLOBAL_PAUSE": "1"})
        c = self._reg_admin()
        self.assertEqual(self._save(c, {"global_pause": 1}).status_code, 200)

    def test_stop_shares_high_risk_quota(self):
        """急停与「删数据 / 拆报警器」共用同一套高危额度（默认 5 次/60 秒）。"""
        c = self._master()
        codes = []
        for _ in range(self.webapp.ADMIN_DELETE_MAX + 1):
            # 1→0 是恢复（不占额度），0→1 才是急停
            self._save(c, {"global_pause": 0, "confirm_password": ADMIN_PASS})
            codes.append(self._save(
                c, {"global_pause": 1, "confirm_password": ADMIN_PASS}).status_code)
        self.assertEqual(codes[:-1], [200] * self.webapp.ADMIN_DELETE_MAX,
                         f"额度内应放行，实际 {codes}")
        self.assertEqual(codes[-1], 429, "超出高危额度后应 429")
        denied = self._save(c, {"global_pause": 1, "confirm_password": ADMIN_PASS})
        self.assertEqual(denied.status_code, 429)
        self.assertNotIn(str(self.webapp.ADMIN_DELETE_MAX), denied.get_json()["error"],
                         "429 文案不得泄露内部阈值参数")

    def test_wrong_password_does_not_consume_quota(self):
        """先鉴权后占额度：不知口令的被盗会话刷不光合法管理员的急停预算。"""
        # 冷却窗口关成 0（运维降级旋钮）：本例要钉的是「额度不被错口令吃掉」，
        # 留着冷却的话第 4 次起就是 429 而不是 403，测不到额度这一层
        self.webapp.write_env_batch(self.env_file, {"YIBAN_PW_CONFIRM_COOLDOWN_SEC": "0"})
        c = self._master()
        for _ in range(self.webapp.ADMIN_DELETE_MAX + 3):
            self._save(c, {"global_pause": 1, "confirm_password": "WrongPass999!"})
        r = self._save(c, {"global_pause": 1, "confirm_password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, f"错口令不得吃掉急停额度：{r.get_data(as_text=True)}")


class ChangeAlertTest(_TierBase):
    """变更告警：一次请求合并成一条，档位决定紧急度。"""

    def test_multi_key_request_emits_single_alert(self):
        c = self._master()
        r = self._save(c, {"sunday_sign": 1, "saturday_sign": 1, "gap_max": 30,
                           "sign_order": "random", "confirm_password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(len(self.alerts), 1, f"一次请求只应有一条告警：{self.alerts}")
        title, body, urgent, force = self.alerts[0]
        self.assertIn("系统设置变更", title)
        self.assertTrue(urgent, "含 A 档变更必须 urgent")
        self.assertFalse(force, "非「拆报警器 / 不可逆清除 / 全停急停」不得 force")
        for frag in ("周日签到=关→开", "周六签到=关→开", "签到排序=sequence→random",
                     "操作者: admin"):
            self.assertIn(frag, body, f"告警正文缺少 {frag}")
        rows = self._audit_rows("settings_save")
        self.assertEqual(len(rows), 1, "审计仍是一行 settings_save")
        self.assertIn("周日签到=关→开", rows[0]["detail"], "审计须带真变化键的旧→新")

    def test_gated_only_change_is_non_urgent(self):
        c = self._master()
        r = self._save(c, {"sign_dist": "normal", "confirm_password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(len(self.alerts), 1)
        self.assertFalse(self.alerts[0][2], "纯 B 档变更属非紧急")
        self.assertFalse(self.alerts[0][3])

    def test_emergency_stop_alert_is_forced(self):
        c = self._reg_admin()
        r = self._save(c, {"global_pause": 1, "confirm_password": REG_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(len(self.alerts), 1)
        _title, body, urgent, force = self.alerts[0]
        self.assertTrue(urgent and force, "全停急停要立刻叫醒：urgent + force")
        self.assertIn("全局暂停签到=关→开", body)

    def test_noop_save_sends_no_alert(self):
        """误点保存（值一个没变）不该发出任何告警，也不该被口令挡住。"""
        c = self._master()
        r = self._save(c, {"sunday_sign": 0,
                           "gap_max": self.webapp.DEFAULT_ACCOUNT_GAP_MAX})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.alerts, [], "无实质变更不得发告警（否则告警成了免费打字机）")

    def test_alert_body_has_no_raw_newline_from_values(self):
        """值里的换行/控制字符不得把告警正文撑成多行（伪造第二行文案）。"""
        c = self._master()
        r = self._save(c, {"probe_time": "06:00", "confirm_password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(len(self.alerts), 1)
        body = self.alerts[0][1]
        self.assertEqual(len(body.splitlines()), 3, f"正文应为操作者/明细/时间三行：{body!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
