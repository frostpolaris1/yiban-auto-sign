# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""安全域拆分契约：`web/security.py` 是真源，`web/app.py` 只保留名字面与转发。

内置管理员会话凭据与角色判定、口令存储/校验与启动弱口令检测、客户端出口与 IP 计数表、
敏感口令门禁旋钮、账号校验配额与冷却、原子落盘从 `web/app.py` 迁入 `web/security.py`。
本文件钉住五件事，任一件破了都会**静默**改变行为：

1. **名字面完整**：迁移名与随域常量在 `web.app` 与 `web.security` 上都可达（routes 经
   `web.routes.appmod()` 按属性取用）。
2. **转发注入 app 模块级状态**：`.env` 路径与读取器、写路径、整数读取器、键行计数、
   告警出口、时延拉平、会话绝对期都留在 `web.app` 且会被测试改写（直接赋值 /
   `mock.patch.object`），服务层另存一份绑定会让改写静默失效——故转发必须在调用时刻
   现取后传入。
3. **同一对象**：`_atomic_write` / `_replace_with_retry` / `_client_ip` / 计数助手是纯
   再导出（`web.app.<名字> is web.security.<名字>`）；`_rate_lock` 的真源在
   `web/services/locks.py`，`web.app._rate_lock is web.security._rate_lock is
   locks._rate_lock`——限速/失败计数表被登录、改密、注销、恢复与门禁多条路径读写，
   锁不是同一把就等于没有互斥。
4. **行为逐字不变**：口令哈希优先与三道 fail-closed、启动弱口令检测的两道拒绝、
   窗口计数"先判后增"、IP 表回收、账号验证冷却、会话判定（凭据版本 + sid、绝对期）、
   原子写的 0600 与无残留都保持原样。
5. **别名加载安全**：安全域不导入 `web.app`（普通 import 会在别名加载的测试进程里再执行
   一份 app.py 副本），且不持有应由 web.app 调用时刻注入的那些名字。
"""
import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import flask

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64

#: 迁入 `web/security.py` 的名字（实现唯一在那里；web.app 上必须是可达的兼容面）
MOVED_SECURITY = (
    "_new_admin_sid",
    "_issue_admin_sid",
    "_admin_session_facts",
    "_builtin_admin_email",
    "_is_builtin_admin_session",
    "_effective_role",
    "_current_role",
    "migrate_admin_password_to_hash",
    "_constant_time_dummy",
    "reject_default_admin_password",
    "_client_ip",
    "_ip_store_trim",
    "_bump_window_count",
    "_bump_login_failure",
    "_sensitive_gate_params",
    "_verify_attempt_allowed",
    "_verify_fail_cooldown_remaining",
    "_record_verify_failure",
    "check_admin_configured",
    "_builtin_admin_loginable",
    "verify_admin",
    "_atomic_write",
    "_replace_with_retry",
    # 只被迁出族使用、随所属域搬的常量；另加同域的策略/口径常量
    "SCRYPT_METHOD",
    "ADMIN_SID_ENV_KEY",
    "LOGIN_LOCK_SECONDS",
    "TRUSTED_PROXIES",
    "_IP_STORE_LIMIT",
    "_IP_STORE_MAX_AGE",
    "VERIFY_MAX",
    "VERIFY_WINDOW",
    "VERIFY_FAIL_MAX",
    "VERIFY_FAIL_WINDOW",
    "VERIFY_FAIL_COOLDOWN",
    "VERIFY_FAIL_AUTH_KEYWORDS",
    "PW_CONFIRM_TTL_DEFAULT",
    "PW_CONFIRM_TTL_MAX",
    "PW_CONFIRM_COOLDOWN_DEFAULT",
    "_DEFAULT_ADMIN_LITERALS",
    "_REPLACE_RETRY_ATTEMPTS",
    "_REPLACE_RETRY_BASE_SEC",
)

#: 纯再导出（不读 app 模块级状态）：两边必须是同一对象
PURE_REEXPORTS = (
    "_new_admin_sid",
    "_constant_time_dummy",
    "_client_ip",
    "_ip_store_trim",
    "_bump_window_count",
    "_bump_login_failure",
    "_verify_attempt_allowed",
    "_verify_fail_cooldown_remaining",
    "_record_verify_failure",
    "_atomic_write",
    "_replace_with_retry",
)

#: 必须由转发包装注入 app 状态的名字（不能是纯再导出）
FORWARDED = (
    "_issue_admin_sid",
    "_admin_session_facts",
    "_builtin_admin_email",
    "_is_builtin_admin_session",
    "_effective_role",
    "_current_role",
    "migrate_admin_password_to_hash",
    "reject_default_admin_password",
    "_sensitive_gate_params",
    "check_admin_configured",
    "_builtin_admin_loginable",
    "verify_admin",
)

#: 安全域不得持有的 web.app 模块级名字（应调用时刻注入）。
#: `_rate_lock` 不在此列——它的真源是 `web/services/locks.py`（见第 3 条）。
APP_HELD_STATE = (
    "ENV_FILE", "LOG_FILE", "STATE_DIR", "read_env", "write_env_key",
    "write_env_batch", "load_env_int", "_count_env_key_lines", "send_notification",
    "SESSION_ABS_TTL_SECONDS", "verify_jobs", "_verify_sem", "_file_lock",
)


class WebSecuritySplitContractTest(unittest.TestCase):
    """以别名加载的 app 副本为靶子（routes 与测试实际看到的那一份）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-security-split-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls._old_env = {k: os.environ.get(k) for k in
                        ("YIBAN_ENV_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR",
                         "YIBAN_ACCOUNTS_FILE", "YIBAN_ACCOUNTS_KEY", "YIBAN_LOG_FILE")}
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_DB_FILE"] = os.path.join(cls.tmp, "yiban.db")
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_ACCOUNTS_FILE"] = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        cls._write_env([])
        spec = importlib.util.spec_from_file_location(
            "webapp_securitysplit", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_securitysplit"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)
        cls.flask_app = flask.Flask("securitysplit-tests")
        cls.flask_app.secret_key = "securitysplit-test-secret"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k, v in cls._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    @classmethod
    def _write_env(cls, lines):
        with io.open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n" + "\n".join(lines) + "\n")

    def setUp(self):
        self._write_env([])
        self.webapp.ENV_FILE = self.env_file

    # ------------------------------------------------------------------
    # 1. 名字面
    # ------------------------------------------------------------------
    def test_every_moved_name_reachable_on_app_and_home_module(self):
        import web.security as sec
        for name in MOVED_SECURITY:
            self.assertTrue(hasattr(self.webapp, name), f"web.app 兼容面缺失 {name}")
            self.assertTrue(hasattr(sec, name), f"web/security.py 缺 {name}")

    def test_pure_reexports_are_same_object(self):
        import web.security as sec
        for name in PURE_REEXPORTS:
            self.assertIs(getattr(self.webapp, name), getattr(sec, name),
                          f"{name} 应为同一个对象（web.app 只是再导出）")

    def test_forwarders_actually_forward(self):
        """注入型名字不得退化成纯再导出（否则 app 侧打桩面静默失效）。"""
        import web.security as sec
        for name in FORWARDED:
            self.assertIsNot(getattr(self.webapp, name), getattr(sec, name),
                             f"{name} 必须是转发包装（调用时刻现取 app 侧名字）")

    def test_forwarders_keep_original_call_arity(self):
        """routes / 既有测试按原实参个数调用，转发不得改签名。"""
        self.assertIsInstance(self.webapp._builtin_admin_email(), str)
        with self.flask_app.test_request_context():
            self.assertIsInstance(self.webapp._is_builtin_admin_session(), bool)
        self.assertIsInstance(self.webapp.check_admin_configured(), bool)
        self.assertIsInstance(self.webapp._builtin_admin_loginable(), bool)
        self.assertIsNone(self.webapp._effective_role(None))
        self.assertIsInstance(self.webapp._admin_session_facts(self.env_file), tuple)
        self.assertIsInstance(self.webapp._issue_admin_sid(self.env_file), str)
        self.assertIsInstance(self.webapp.verify_admin("x", "y"), bool)
        self.assertIsInstance(self.webapp._sensitive_gate_params(self.env_file), tuple)

    def test_rate_lock_single_definition_point(self):
        """`_rate_lock` 收口在 locks.py：三处必须是同一把锁（禁止另建一把）。"""
        import web.security as sec
        from web.services import locks
        self.assertIs(self.webapp._rate_lock, locks._rate_lock)
        self.assertIs(sec._rate_lock, locks._rate_lock)
        self.assertIsNot(self.webapp._rate_lock, self.webapp._file_lock,
                         "限速锁与文件锁是两把不同用途的锁")

    def test_verify_queue_and_measure_names_untouched(self):
        """安全域不得占用他域名字面（`verify_jobs` 仍指 yiban 真源）。"""
        import web.security as sec
        from yiban.attempt import jobs as yb_jobs
        self.assertIs(self.webapp.verify_jobs, yb_jobs)
        self.assertFalse(hasattr(sec, "verify_jobs"))

    # ------------------------------------------------------------------
    # 2. 打桩往返（注入的是调用时刻的 app 侧名字）
    # ------------------------------------------------------------------
    def test_read_env_stub_reaches_check_admin_configured(self):
        with mock.patch.object(self.webapp, "read_env",
                               return_value={"YIBAN_ADMIN_USER": "a",
                                             "YIBAN_ADMIN_PASSWORD_HASH": "h"}) as spy:
            self.assertTrue(self.webapp.check_admin_configured())
        self.assertEqual(spy.call_count, 1, "转发必须现取 read_env")
        with mock.patch.object(self.webapp, "read_env", return_value={}):
            self.assertFalse(self.webapp.check_admin_configured())

    def test_env_file_assignment_reaches_builtin_admin_email(self):
        self._write_env(["YIBAN_ADMIN_USER=Root@Example.com"])
        self.assertEqual(self.webapp._builtin_admin_email(), "root@example.com",
                         "转发必须现取本模块的 ENV_FILE（小写化口径不变）")

    def test_send_notification_and_read_env_stubs_reach_verify_admin(self):
        """verify_admin 的两条打桩面：.env 读取器与告警出口。"""
        # 只有明文、没有哈希 = M1 明文回退 fail-closed（迁移失败降级态）
        with mock.patch.object(self.webapp, "read_env",
                               return_value={"YIBAN_ADMIN_USER": "a",
                                             "YIBAN_ADMIN_PASSWORD": "Plain#1234"}) as spy:
            self.assertFalse(self.webapp.verify_admin("a", "Plain#1234"))
        self.assertEqual(spy.call_count, 1)
        # 哈希歧义（统计得 2 行）→ 拒绝 + 经 app 侧 send_notification 告警
        with mock.patch.object(self.webapp, "read_env",
                               return_value={"YIBAN_ADMIN_USER": "a",
                                             "YIBAN_ADMIN_PASSWORD_HASH": "h"}), \
                mock.patch.object(self.webapp, "_count_env_key_lines", return_value=2), \
                mock.patch.object(self.webapp, "send_notification") as sn, \
                mock.patch.object(self.webapp, "_constant_time_dummy") as dummy:
            self.assertFalse(self.webapp.verify_admin("a", "x"))
        self.assertTrue(sn.called, "歧义态必须经 app 侧告警出口告警")
        self.assertTrue(sn.call_args.kwargs.get("urgent"))
        self.assertEqual(sn.call_args.kwargs.get("ledger"), "login_fail")
        self.assertEqual(dummy.call_count, 1, "拒绝分支必须走 app 侧的时延拉平")

    def test_constant_time_dummy_stub_reaches_verify_admin(self):
        with mock.patch.object(self.webapp, "read_env", return_value={}), \
                mock.patch.object(self.webapp, "_constant_time_dummy") as dummy:
            self.assertFalse(self.webapp.verify_admin("a", "x"))
        self.assertEqual(dummy.call_count, 1, "凭据未配齐分支须现取 _constant_time_dummy")

    def test_session_abs_ttl_assignment_reaches_current_role(self):
        """`SESSION_ABS_TTL_SECONDS` 在 create_app 里会按 .env 回写模块全局：转发必须现取。"""
        now = int(time.time())
        with mock.patch.object(self.webapp, "read_env", return_value={}), \
                self.flask_app.test_request_context():
            flask.session["auth"] = True
            flask.session["username"] = ""
            flask.session["login_ts"] = now - 100
            with mock.patch.object(self.webapp, "SESSION_ABS_TTL_SECONDS", 1000):
                self.assertIsNone(self.webapp._current_role(), "未登录（无角色）仍是 None")
            with mock.patch.object(self.webapp, "SESSION_ABS_TTL_SECONDS", 10):
                self.assertIsNone(self.webapp._current_role(),
                                  "绝对期被打桩成 10 秒后超龄会话必须失效")

    def test_load_env_int_stub_reaches_sensitive_gate_params(self):
        with mock.patch.object(self.webapp, "load_env_int",
                               return_value=99999) as spy:
            ttl, cooldown = self.webapp._sensitive_gate_params(self.env_file)
        self.assertEqual(ttl, self.webapp.PW_CONFIRM_TTL_MAX,
                         "TTL 上界硬钳在真源里生效（打桩值 99999 只按 900）")
        self.assertEqual(cooldown, 99999, "冷却不钳制（0=关闭）")
        self.assertEqual(spy.call_count, 2, "两个旋钮各读一次，且都经 app 侧读取器")

    def test_write_env_key_and_read_env_stubs_reach_issue_admin_sid(self):
        with mock.patch.object(self.webapp, "write_env_key") as w, \
                mock.patch.object(self.webapp, "read_env",
                                  return_value={self.webapp.ADMIN_SID_ENV_KEY: "old-sid"}):
            sid = self.webapp._issue_admin_sid(self.env_file)
        self.assertEqual(w.call_args.args[0], self.env_file)
        self.assertEqual(w.call_args.args[1], self.webapp.ADMIN_SID_ENV_KEY)
        self.assertNotEqual(sid, "old-sid")
        # 落盘失败（OSError）→ 返回 .env 里的旧值，不把会话锁在门外
        with mock.patch.object(self.webapp, "write_env_key", side_effect=OSError("ro")), \
                mock.patch.object(self.webapp, "read_env",
                                  return_value={self.webapp.ADMIN_SID_ENV_KEY: "old-sid"}):
            self.assertEqual(self.webapp._issue_admin_sid(self.env_file), "old-sid")
        with mock.patch.object(self.webapp, "read_env", return_value={}), \
                mock.patch.object(self.webapp, "write_env_batch") as wb, \
                mock.patch.object(self.webapp, "load_env_int", return_value=1):
            self.webapp.migrate_admin_password_to_hash(self.env_file)
        self.assertEqual(wb.call_count, 0, "无明文时不写 .env（幂等）")

    def test_atomic_write_stub_reaches_env_and_measure_paths(self):
        """`_atomic_write` 必须是同一函数对象，且 env_io / measure 的转发按调用时刻现取。"""
        import web.security as sec
        self.assertIs(self.webapp._atomic_write, sec._atomic_write,
                      "web.app._atomic_write 必须是 web/security.py 的同一函数对象")
        with mock.patch.object(self.webapp, "_atomic_write") as spy:
            self.webapp.write_env_batch(self.env_file, {"YIBAN_X": "1"})
        self.assertEqual(spy.call_count, 1, "env_io 落盘必须经 app 侧 _atomic_write（打桩点）")
        self.assertIs(spy.call_args.kwargs["chmod_priv"], True)
        with mock.patch.object(self.webapp, "_atomic_write") as spy:
            self.webapp._write_measure_state(os.path.join(self.tmp, "m.json"), {"at": ""})
        self.assertEqual(spy.call_count, 1, "实测落盘同样经 app 侧 _atomic_write")

    def test_ip_store_trim_stub_is_reexport(self):
        """计数助手是纯再导出：routes 经 m.* 打桩时命中真实现（同一对象）。"""
        import web.security as sec
        self.assertIs(self.webapp._ip_store_trim, sec._ip_store_trim)
        store = {f"k{i}": (1, 0.0) for i in range(self.webapp._IP_STORE_LIMIT + 1)}
        self.webapp._ip_store_trim(store, self.webapp._IP_STORE_MAX_AGE)
        self.assertLess(len(store), 10, "超限且全过期必须被回收")

    # ------------------------------------------------------------------
    # 3. 行为逐字不变
    # ------------------------------------------------------------------
    def test_migrate_admin_password_to_hash_roundtrip(self):
        real_write = self.webapp.write_env_batch
        with mock.patch.object(self.webapp, "read_env",
                               return_value={"YIBAN_ADMIN_PASSWORD": "Strong#1234"}):
            self.webapp.migrate_admin_password_to_hash(self.env_file)
        text = io.open(self.env_file, encoding="utf-8").read()
        self.assertIn("YIBAN_ADMIN_PASSWORD_HASH=", text)
        self.assertNotIn("Strong#1234", text, "明文必须被清空（值空 → 删行）")
        self.assertNotIn("YIBAN_ADMIN_PASSWORD=", text)
        self.assertIs(real_write, self.webapp.write_env_batch)

    def test_reject_default_admin_password_verbatim_thresholds(self):
        weak = os.path.join(self.tmp, "weak.env")
        strong = os.path.join(self.tmp, "strong.env")
        with io.open(weak, "w", encoding="utf-8") as f:
            f.write("YIBAN_ADMIN_PASSWORD=admin123\n")
        with io.open(strong, "w", encoding="utf-8") as f:
            f.write("YIBAN_ADMIN_PASSWORD=Abcdefghij12\n")
        with self.assertRaises(SystemExit) as cm:
            self.webapp.reject_default_admin_password(weak)
        self.assertEqual(cm.exception.code, 2, "公开模板默认字面量必须 fail-closed")
        self.assertIsNone(self.webapp.reject_default_admin_password(strong),
                          "12 位三类应放行")
        with mock.patch.object(self.webapp, "read_env", side_effect=OSError("boom")):
            self.assertIsNone(self.webapp.reject_default_admin_password(weak),
                              "读取失败不得阻断启动")

    def test_window_and_failure_counters_semantics(self):
        store = {}
        for i in range(10):
            _c, _s, allowed = self.webapp._bump_window_count(store, "ip", 1000.0, 60, limit=10)
            self.assertTrue(allowed, f"第 {i + 1} 次应放行")
        _c, _s, allowed = self.webapp._bump_window_count(store, "ip", 1000.0, 60, limit=10)
        self.assertFalse(allowed, "第 11 次应拒绝")
        self.assertEqual(store["ip"][0], 10, "拒绝时不得递增")
        # 翻窗：窗口起点后移，计数从 1 重新开始
        _c, start, allowed = self.webapp._bump_window_count(store, "ip", 2000.0, 60, limit=10)
        self.assertTrue(allowed)
        self.assertEqual((_c, start), (1, 2000.0))
        fails = {}
        self.assertEqual(self.webapp._bump_login_failure(fails, "k", 5.0), 1)
        self.assertEqual(self.webapp._bump_login_failure(fails, "k", 6.0), 2)
        self.assertEqual(fails["k"], (2, 0, 6.0), "失败表口径 (次数, 0, 时刻) 不变")

    def test_verify_attempt_quota_and_cooldown(self):
        store = {}
        for _ in range(self.webapp.VERIFY_MAX):
            self.assertTrue(self.webapp._verify_attempt_allowed(store, "Admin@x.io"))
        self.assertFalse(self.webapp._verify_attempt_allowed(store, "admin@x.io"),
                         "配额按用户名小写归一且先判后增")
        cooldown = {}
        now = 5000.0
        self.assertEqual(self.webapp._verify_fail_cooldown_remaining(cooldown, "p", now), 0)
        self.assertEqual(self.webapp._record_verify_failure(cooldown, "p", "网络超时", now),
                         "其他失败", "网络类失败不计冷却")
        self.assertEqual(cooldown, {})
        for _ in range(self.webapp.VERIFY_FAIL_MAX):
            kind = self.webapp._record_verify_failure(cooldown, "p", "账号或密码错误", now)
        self.assertEqual(kind, "认证失败")
        self.assertGreater(self.webapp._verify_fail_cooldown_remaining(cooldown, "p", now), 0)
        self.assertEqual(cooldown["p"][0], 0, "触发冷却后窗口计数清零")

    def test_effective_role_builtin_credential_matrix(self):
        env = {"YIBAN_ADMIN_USER": "admin", "YIBAN_ADMIN_PW_VERSION": "3",
               self.webapp.ADMIN_SID_ENV_KEY: "sid-1"}
        cases = [
            ({"auth_source": "builtin", "username": "admin", "sid": "sid-1"}, 3, "admin"),
            ({"auth_source": "builtin", "username": "ADMIN", "sid": "sid-1"}, 3, "admin"),
            ({"auth_source": "builtin", "username": "admin", "sid": "sid-1"}, 2, None),
            ({"auth_source": "builtin", "username": "admin", "sid": "stale"}, 3, None),
            ({"auth_source": "builtin", "username": "admin"}, 3, None),  # 未携带 sid 即不匹配
            ({"auth_source": "user", "username": "admin", "sid": "sid-1"}, 3, None),
            ({"username": "admin", "sid": "sid-1"}, 3, None),  # 缺 auth_source 的旧会话
        ]
        for data, pwv, want in cases:
            with self.subTest(data=data, pwv=pwv):
                with mock.patch.object(self.webapp, "read_env", return_value=env), \
                        mock.patch.object(self.webapp.db, "find_user", return_value=None), \
                        self.flask_app.test_request_context():
                    flask.session.update(data)
                    got = self.webapp._effective_role(data.get("username"), pwv)
                self.assertEqual(got, want)
        # 升级日存量部署兼容：.env 尚未签发 sid（空）时不强制重登
        with mock.patch.object(self.webapp, "read_env",
                               return_value={"YIBAN_ADMIN_USER": "admin"}), \
                self.flask_app.test_request_context():
            flask.session.update({"auth_source": "builtin", "username": "admin"})
            self.assertEqual(self.webapp._effective_role("admin", 1), "admin")
        with mock.patch.object(self.webapp, "read_env", return_value=env), \
                mock.patch.object(self.webapp.db, "find_user",
                                  return_value={"role": "admin", "sid": "s", "pw_version": 2}), \
                self.flask_app.test_request_context():
            flask.session.update({"auth_source": "user", "username": "u@x.io", "sid": "s"})
            self.assertEqual(self.webapp._effective_role("u@x.io", 2), "admin")
            self.assertEqual(self.webapp._effective_role("u@x.io", 1), None,
                             "pw_version 不匹配即失效")

    def test_current_role_absolute_expiry_clears_session(self):
        env = {"YIBAN_ADMIN_USER": "admin"}
        with mock.patch.object(self.webapp, "read_env", return_value=env), \
                self.flask_app.test_request_context():
            self.assertIsNone(self.webapp._current_role(), "无 auth 即未登录")
            flask.session["auth"] = True
            flask.session["username"] = "u@x.io"
            flask.session["login_ts"] = int(time.time()) - self.webapp.SESSION_ABS_TTL_SECONDS - 5
            with mock.patch.object(self.webapp.db, "find_user", return_value=None):
                self.assertIsNone(self.webapp._current_role())
            self.assertEqual(len(flask.session), 0, "超限必须清空会话（视为未登录）")

    def test_client_ip_trusted_proxy_and_fallback(self):
        proxies = self.webapp.TRUSTED_PROXIES
        app = self.flask_app
        with app.test_request_context("/", environ_base={"REMOTE_ADDR": proxies[0]},
                                      headers={"X-Forwarded-For": "203.0.113.9, 10.0.0.1"}):
            self.assertEqual(self.webapp._client_ip(), "203.0.113.9")
        with app.test_request_context("/", environ_base={"REMOTE_ADDR": "203.0.113.9"},
                                      headers={"X-Forwarded-For": "198.51.100.7"}):
            self.assertEqual(self.webapp._client_ip(), "203.0.113.9",
                             "非可信首跳的 XFF 必须被忽略（不可伪造）")
        with app.test_request_context("/", environ_base={"REMOTE_ADDR": proxies[1]}):
            self.assertEqual(self.webapp._client_ip(), proxies[1])

    def test_atomic_write_roundtrip_and_reparse(self):
        target = os.path.join(self.tmp, "aw-security.txt")
        self.webapp._atomic_write(target, "内容content", chmod_priv=True)
        with io.open(target, encoding="utf-8") as f:
            self.assertEqual(f.read(), "内容content")
        leftovers = [p for p in os.listdir(self.tmp)
                     if p.startswith("aw-security") and ".tmp" in p]
        self.assertEqual(leftovers, [], "替换后不得残留 tmp")

    def test_migrate_admin_password_clears_plain_and_hash_priority(self):
        env_path = os.path.join(self.tmp, "migrate.env")
        with io.open(env_path, "w", encoding="utf-8") as f:
            f.write("YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD=Strong#1234\n")
        self.webapp.migrate_admin_password_to_hash(env_path)
        text = io.open(env_path, encoding="utf-8").read()
        self.assertIn("YIBAN_ADMIN_PASSWORD_HASH=", text)
        self.assertNotIn("YIBAN_ADMIN_PASSWORD=Strong#1234", text)
        before = text
        self.webapp.migrate_admin_password_to_hash(env_path)
        self.assertEqual(io.open(env_path, encoding="utf-8").read(), before,
                         "第二次迁移必须幂等（无明文可迁）")
        with mock.patch.object(self.webapp, "read_env") as re_:
            re_.return_value = {"YIBAN_ADMIN_USER": "admin",
                                "YIBAN_ADMIN_PASSWORD_HASH": "not-a-hash"}
            self.assertFalse(self.webapp.verify_admin("admin", "pw"))

    def test_builtin_admin_loginable_three_states(self):
        cases = [
            ({"YIBAN_ADMIN_USER": "", "YIBAN_ADMIN_PASSWORD_HASH": "h"}, 1, False),
            ({"YIBAN_ADMIN_USER": "a", "YIBAN_ADMIN_PASSWORD": "p"}, 0, False),
            ({"YIBAN_ADMIN_USER": "a", "YIBAN_ADMIN_PASSWORD_HASH": "h"}, 2, False),
            ({"YIBAN_ADMIN_USER": "a", "YIBAN_ADMIN_PASSWORD_HASH": "h"}, 1, True),
        ]
        for env, lines, want in cases:
            with self.subTest(env=env, lines=lines), \
                    mock.patch.object(self.webapp, "read_env", return_value=env), \
                    mock.patch.object(self.webapp, "_count_env_key_lines",
                                      return_value=lines):
                self.assertEqual(self.webapp._builtin_admin_loginable(), want)

    # ------------------------------------------------------------------
    # 4/5. 别名加载安全与状态归属
    # ------------------------------------------------------------------
    def test_alias_loaded_app_shares_security_module(self):
        import web.security as sec
        self.assertIs(self.webapp._security, sec,
                      "别名加载的 app 副本必须复用同一个 web.security")

    def test_security_module_holds_no_app_state(self):
        import web.security as sec
        for name in APP_HELD_STATE:
            self.assertFalse(hasattr(sec, name),
                             f"web/security.py 不得持有 {name}（应由 web.app 调用时刻注入）")

    def test_security_source_does_not_import_app(self):
        with io.open(os.path.join(BASE, "web", "security.py"), encoding="utf-8") as f:
            src = f.read()
        import re as _re
        self.assertIsNone(_re.search(r"^\s*(?:import|from)\s+web\.app\b", src, _re.M),
                          "web/security.py 禁止 import web.app")

    def test_importing_security_does_not_execute_web_app(self):
        """全新解释器里只 import web.security：不得把 web.app 拉进 sys.modules。"""
        code = (
            "import sys;"
            f"sys.path[:0]=[{os.path.join(BASE, 'scripts')!r}, {BASE!r}];"
            "import web.security;"
            "print('APP_LOADED' if 'web.app' in sys.modules else 'SECURITY_ONLY_OK')"
        )
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        r = subprocess.run([sys.executable, "-c", code], cwd=BASE, env=env,
                           capture_output=True, text=True, timeout=120)
        self.assertIn("SECURITY_ONLY_OK", r.stdout, r.stderr[-600:])
        self.assertEqual(r.returncode, 0, r.stderr[-600:])


if __name__ == "__main__":
    unittest.main(verbosity=2)
