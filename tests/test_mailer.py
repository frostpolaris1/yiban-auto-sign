# -*- coding: utf-8 -*-
"""mailer 邮箱通知模块单元测试（A 线：管理员告警邮件）。

覆盖：未配置不启用/不发送；SMTP_SSL 成功发送；发送异常静默且日志脱敏
（不泄露授权码、不回显完整发件地址）；多收件人逗号分隔；邮箱打码。
全程 mock smtplib，不发起真实网络请求。
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from yiban.infra import account_crypto, env_io
from yiban.mail import config as mailer
from yiban.mail import transport as mailer_transport

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 直连实现包（旧的 scripts/mailer.py 兼容壳已删除）：配置名走 config 子模块，
# 发送名（smtplib / _send 链路）走 transport 子模块——打桩必须打在真正调用它的模块上。

_KEY = "a" * 64


def _isolate_env(monkeypatch, tmp_path):
    """隔离环境：YIBAN_ENV_FILE 指向不存在文件 + 清理已有 YIBAN_MAIL_* 环境变量。"""
    monkeypatch.setenv("YIBAN_ENV_FILE", str(tmp_path / "no-such.env"))
    for k in list(os.environ):
        if k.startswith("YIBAN_MAIL_"):
            monkeypatch.delenv(k)


def _env_with_blob(monkeypatch, tmp_path, smtps_enc_line):
    """隔离 + 写真实 .env（账号钥 + 可选 SMTPS_ENC 行），YIBAN_ENV_FILE 指向它。"""
    path = tmp_path / "deploy.env"
    lines = [f"YIBAN_ACCOUNTS_KEY={_KEY}"]
    if smtps_enc_line:
        lines.append(f"YIBAN_MAIL_SMTPS_ENC={smtps_enc_line}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("YIBAN_ENV_FILE", str(path))
    # 密钥走环境变量（load_key 的最高优先档），不受进程内 _KEY_CACHE 残留影响
    monkeypatch.setenv("YIBAN_ACCOUNTS_KEY", _KEY)


def _enc_blob(entries):
    """按 web 设置页同口径生成 YIBAN_MAIL_SMTPS_ENC 的值。"""
    return json.dumps(account_crypto.encrypt_text(
        json.dumps(entries, ensure_ascii=False), account_crypto._decode_key(_KEY)),
        ensure_ascii=False)


def _set_mail(monkeypatch, **kwargs):
    """批量设置 YIBAN_MAIL_* 环境变量。"""
    for k, v in kwargs.items():
        monkeypatch.setenv("YIBAN_MAIL_" + k, v)


def test_mask_addr_masks_local_part():
    # 长用户名保留前 3 字符，其余打码（10 字符 → 7 个星）
    assert mailer._mask_addr("1234567890@qq.com") == "123*******@qq.com"


def test_mask_addr_short_name_keeps_one_char():
    # 短用户名（<=6 位）只保留首位：固定保留前 3 位时短名几乎全暴露
    assert mailer._mask_addr("ab@x.com") == "a***@x.com"
    assert mailer._mask_addr("sender@qq.com") == "s*****@qq.com"  # 恰 6 位按短名处理；星号补足原宽


def test_mask_addr_non_email_passthrough():
    assert mailer._mask_addr("not-an-email") == "not-an-email"


def test_mask_addr_empty():
    assert mailer._mask_addr("") == "<未配置>"


def test_is_enabled_false_without_config(monkeypatch, tmp_path):
    _isolate_env(monkeypatch, tmp_path)
    assert mailer.is_enabled() is False


def test_is_enabled_true_when_complete(monkeypatch, tmp_path):
    _isolate_env(monkeypatch, tmp_path)
    _set_mail(monkeypatch, ENABLE="1", USER="sender@qq.com", PASS="secret")
    assert mailer.is_enabled() is True


def test_admin_notify_enabled_default_true(monkeypatch, tmp_path):
    _isolate_env(monkeypatch, tmp_path)
    _set_mail(monkeypatch, ENABLE="1", USER="sender@qq.com", PASS="secret")
    assert mailer.admin_notify_enabled() is True


def test_admin_notify_enabled_off(monkeypatch, tmp_path):
    _isolate_env(monkeypatch, tmp_path)
    _set_mail(monkeypatch, ADMIN_NOTIFY="0")
    assert mailer.admin_notify_enabled() is False


def test_send_admin_alert_skipped_when_disabled(monkeypatch, tmp_path):
    _isolate_env(monkeypatch, tmp_path)
    _set_mail(monkeypatch, ENABLE="0", USER="sender@qq.com", PASS="secret", ADMIN_TO="admin@qq.com")
    assert mailer_transport.send_admin_alert("标题", "内容") is False


def test_send_admin_alert_success(monkeypatch, tmp_path):
    _isolate_env(monkeypatch, tmp_path)
    _set_mail(monkeypatch, ENABLE="1", USER="sender@qq.com", PASS="secret", ADMIN_TO="admin@qq.com")
    calls = []

    class FakeServer:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, u, p):
            calls.append(("login", u, p))

        def sendmail(self, frm, to, msg):
            calls.append(("sendmail", frm, to, msg))

    monkeypatch.setattr(mailer_transport.smtplib, "SMTP_SSL", FakeServer)
    # 2026-08-27 契约变更：to 必须由调用方显式传入（缺省不再回退 ADMIN_TO）
    assert mailer_transport.send_admin_alert("告警", "内容", to="admin@qq.com") is True
    assert calls[0][0] == "login"
    assert calls[0][1] == "sender@qq.com"
    assert calls[0][2] == "secret"
    assert calls[1][1] == "sender@qq.com"
    assert calls[1][2] == ["admin@qq.com"]


def test_send_failure_logs_safe(monkeypatch, tmp_path, caplog):
    _isolate_env(monkeypatch, tmp_path)
    _set_mail(monkeypatch, ENABLE="1", USER="sender@qq.com", PASS="secret", ADMIN_TO="admin@qq.com")

    class Boom:
        def __init__(self, *a, **k):
            raise OSError("connect refused")

    monkeypatch.setattr(mailer_transport.smtplib, "SMTP_SSL", Boom)
    with caplog.at_level("WARNING", logger="mailer"):
        assert mailer_transport.send_admin_alert("告警", "内容", to="admin@qq.com") is False
    # 契约变更：失败日志不再回显原始异常类型名。`OSError` / `ConnectionRefusedError` /
    # 超时等类型名互不相同，等于把"目标端口是否开放、域名能否解析"的探测指纹写进管理员
    # 可读日志；改记粗分类，保留"哪一类问题"的排障价值而不暴露端口状态。
    assert "OSError" not in caplog.text
    assert "连接失败" in caplog.text
    assert "secret" not in caplog.text, "授权码不得出现在日志"
    assert "sender@qq.com" not in caplog.text, "完整发件地址不得回显"
    assert "告警" in caplog.text


def test_send_admin_alert_requires_filtered_to(monkeypatch, tmp_path, caplog):
    """P2-3 回归：to 缺省时 fail-closed 拒发（不回退 ADMIN_TO 绕过个人开关）。"""
    _isolate_env(monkeypatch, tmp_path)
    _set_mail(monkeypatch, ENABLE="1", USER="sender@qq.com", PASS="secret",
              ADMIN_TO="admin@qq.com")
    attempted = []

    class BoomShouldNotConstruct:
        def __init__(self, *a, **k):
            attempted.append(1)
            raise AssertionError("拒发路径不得尝试建立 SMTP 连接")

    monkeypatch.setattr(mailer_transport.smtplib, "SMTP_SSL", BoomShouldNotConstruct)
    with caplog.at_level("WARNING", logger="mailer"):
        assert mailer_transport.send_admin_alert("告警", "内容") is False
        assert mailer_transport.send_admin_alert("告警", "内容", to="   ") is False
    assert not attempted, "缺省/空白收件人必须直接拒绝"
    assert "拒绝发送" in caplog.text


def test_send_admin_alert_multi_recipients(monkeypatch, tmp_path):
    _isolate_env(monkeypatch, tmp_path)
    _set_mail(monkeypatch, ENABLE="1", USER="sender@qq.com", PASS="secret",
              ADMIN_TO="a@qq.com, b@qq.com")
    targets = []

    class FakeServer:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, u, p):
            pass

        def sendmail(self, frm, to, msg):
            targets.extend(to)

    monkeypatch.setattr(mailer_transport.smtplib, "SMTP_SSL", FakeServer)
    mailer_transport.send_admin_alert("告警", "内容", to="a@qq.com,b@qq.com")
    assert targets == ["a@qq.com", "b@qq.com"]


def test_send_admin_alert_explicit_to(monkeypatch, tmp_path):
    """显式 to 覆盖 ADMIN_TO：仅发给传入列表（调用方已按个人开关过滤）。"""
    _isolate_env(monkeypatch, tmp_path)
    _set_mail(monkeypatch, ENABLE="1", USER="sender@qq.com", PASS="secret", ADMIN_TO="admin@qq.com")
    targets = []

    class FakeServer:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, u, p):
            pass

        def sendmail(self, frm, to, msg):
            targets.extend(to)

    monkeypatch.setattr(mailer_transport.smtplib, "SMTP_SSL", FakeServer)
    mailer_transport.send_admin_alert("告警", "内容", to="only@qq.com")
    assert targets == ["only@qq.com"], "显式 to 应只发给传入地址"


def test_get_config_never_exposes_password(monkeypatch, tmp_path):
    _isolate_env(monkeypatch, tmp_path)
    _set_mail(monkeypatch, ENABLE="1", USER="sender@qq.com", PASS="topsecret", ADMIN_TO="admin@qq.com")
    cfg = mailer.get_config()
    assert "topsecret" not in str(cfg), "get_config 不得泄露授权码"
    assert cfg["user"] == "s*****@qq.com"


def test_channel_state_off_when_disabled(monkeypatch, tmp_path):
    _env_with_blob(monkeypatch, tmp_path, None)
    _set_mail(monkeypatch, ENABLE="0", USER="sender@qq.com", PASS="secret")
    state, detail = mailer.smtp_channel_state()
    assert state == "off"
    assert "ENABLE" in detail


def test_channel_state_ok_with_legacy_keys(monkeypatch, tmp_path):
    _env_with_blob(monkeypatch, tmp_path, None)
    _set_mail(monkeypatch, ENABLE="1", USER="sender@qq.com", PASS="secret")
    state, _ = mailer.smtp_channel_state()
    assert state == "ok"


def test_channel_state_ok_with_enc_blob(monkeypatch, tmp_path):
    entries = [{"host": "smtp.x.com", "port": 465, "user": "a@x.com",
                "pass": "p1", "admin_to": ""}]
    _env_with_blob(monkeypatch, tmp_path, _enc_blob(entries))
    _set_mail(monkeypatch, ENABLE="1")
    state, detail = mailer.smtp_channel_state()
    assert state == "ok"
    assert "1" in detail


def test_channel_state_broken_when_entries_missing_credentials(monkeypatch, tmp_path):
    """条目结构性残缺（有 host 缺 user/pass）：配了但每封必败，不得报 ok。"""
    _env_with_blob(monkeypatch, tmp_path, _enc_blob([{"host": "x"}]))
    _set_mail(monkeypatch, ENABLE="1")
    state, detail = mailer.smtp_channel_state()
    assert state == "broken"
    assert "缺发件账号/授权码" in detail


def test_channel_state_broken_when_blob_undecryptable(monkeypatch, tmp_path):
    """密文解不开且旧键也为空：不得 fail-open 报成可用。"""
    _env_with_blob(monkeypatch, tmp_path, "not-a-json-ciphertext")
    _set_mail(monkeypatch, ENABLE="1")
    state, detail = mailer.smtp_channel_state()
    assert state == "broken"
    assert "无法解密" in detail


def test_channel_state_broken_when_nothing_configured(monkeypatch, tmp_path):
    _env_with_blob(monkeypatch, tmp_path, None)
    _set_mail(monkeypatch, ENABLE="1")
    state, detail = mailer.smtp_channel_state()
    assert state == "broken"
    assert "未配置" in detail


def test_is_enabled_true_only_for_ok_state(monkeypatch, tmp_path):
    """is_enabled 与三态单源：坏 blob / 关开关都不是启用；旧键齐全才是启用。"""
    _env_with_blob(monkeypatch, tmp_path, _enc_blob([{"host": "x"}]))
    _set_mail(monkeypatch, ENABLE="1")
    assert mailer.is_enabled() is False, "条目缺账号/授权码不得报启用"
    _set_mail(monkeypatch, ENABLE="0")
    assert mailer.is_enabled() is False
    # 密文优先于旧键：上一段的坏 blob 在场时旧键齐全也不算启用；换干净 .env 再验 ok
    _env_with_blob(monkeypatch, tmp_path, None)
    _set_mail(monkeypatch, ENABLE="1", USER="sender@qq.com", PASS="secret")
    assert mailer.is_enabled() is True


TEST_KEY = "a" * 64


ADMIN_PASS = "MasterPass#2026"


_ENV_KEYS = (
    "YIBAN_MAIL_ENABLE", "YIBAN_MAIL_ADMIN_TO", "YIBAN_MAIL_ADMIN_NOTIFY",
    "YIBAN_MAIL_USER", "YIBAN_MAIL_PASS", "YIBAN_MAIL_SMTPS_ENC",
    "YIBAN_SITE_DESCRIPTION", "YIBAN_SITE_IMAGE",
)


def _load_webapp(tag):
    import db as _db
    spec = importlib.util.spec_from_file_location(f"webapp_{tag}", os.path.join(BASE, "web", "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"webapp_{tag}"] = mod
    with contextlib.suppress(Exception):
        spec.loader.exec_module(mod)
    return _db, mod


class _Base(unittest.TestCase):
    """共享脚手架：临时 .env/DB + webapp 加载 + 主管理员登录（与本文件的 MailFailoverTest 共用同一套脚手架）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-admin-to-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with io.open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                "YIBAN_ADMIN_USER=admin@test.local\n"
                f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
                # 改 SMTP/收件人/通道开关的门禁用例钉的是"当次要口令、失败零落盘"，
                # 固定在 full（默认档 risk 下这些动作不再当次要口令）
                "YIBAN_PW_GATE=full\n"
            )
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls._old_env = {k: os.environ.get(k) for k in (*_ENV_KEYS, "YIBAN_ENV_FILE")}
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        cls.db, cls.webapp = _load_webapp(cls.__name__)

    @classmethod
    def tearDownClass(cls):
        if cls.db._conn is not None:
            with contextlib.suppress(Exception):
                cls.db._conn.close()
            cls.db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k, v in cls._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def setUp(self):
        if self.db._conn is not None:
            with contextlib.suppress(Exception):
                self.db._conn.close()
            self.db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        self.db.init_db(self.db_file, migrate_from=self.accounts_file, env_file=self.env_file)

    def _reset_env_file(self, extra=""):
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                "YIBAN_ADMIN_USER=admin@test.local\n"
                f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
                "YIBAN_PW_GATE=full\n"  # 重置也保留门禁档位，见 setUpClass 的说明
                + extra
            )

    def _master(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "admin@test.local", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        t = c.get("/api/me").get_json()["csrf_token"]
        return c, {"X-CSRF-Token": t}

    def _env_admin_to(self):
        return env_io.parse_env_file(self.env_file).get("YIBAN_MAIL_ADMIN_TO")


class AdminToWriteTest(_Base):
    """PUT /api/mail-config admin_to：写入路径与门禁。"""

    def test_put_writes_single_address(self):
        self._reset_env_file("YIBAN_MAIL_ADMIN_TO=old@test.local\n")
        c, h = self._master()
        r = c.put("/api/mail-config",
                  json={"admin_to": "new@test.local", "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self._env_admin_to(), "new@test.local")

    def test_put_writes_multiple_addresses_normalized(self):
        """逗号分隔多地址：去空白后按顺序落盘。"""
        self._reset_env_file()
        c, h = self._master()
        r = c.put("/api/mail-config",
                  json={"admin_to": " a@test.local , b@test.local ", "confirm_password": ADMIN_PASS},
                  headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self._env_admin_to(), "a@test.local,b@test.local")

    def test_put_requires_confirm_password(self):
        self._reset_env_file("YIBAN_MAIL_ADMIN_TO=old@test.local\n")
        c, h = self._master()
        r = c.put("/api/mail-config", json={"admin_to": "new@test.local"}, headers=h)
        self.assertEqual(r.status_code, 400, "改收件人属高危动作，必须二次鉴权")
        self.assertEqual(self._env_admin_to(), "old@test.local", "未通过鉴权不得落盘")

    def test_put_requires_master_admin(self):
        """普通注册管理员不得改收件人（403，不落盘）。"""
        self._reset_env_file("YIBAN_MAIL_ADMIN_TO=old@test.local\n")
        self.db.create_user("regadmin@test.local",
                            self.webapp.generate_password_hash("RegPass#2026"),
                            role="admin", pw_version=1)
        c = self.webapp.create_app().test_client()
        c.post("/api/login", json={"username": "regadmin@test.local", "password": "RegPass#2026"})
        t = c.get("/api/me").get_json()["csrf_token"]
        r = c.put("/api/mail-config",
                  json={"admin_to": "new@test.local", "confirm_password": "RegPass#2026"},
                  headers={"X-CSRF-Token": t})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self._env_admin_to(), "old@test.local")

    def test_invalid_address_rejected_and_no_write(self):
        """非法地址整请求拒绝（多地址中任一非法即拒绝），零写盘痕迹。"""
        self._reset_env_file("YIBAN_MAIL_ADMIN_TO=old@test.local\n")
        c, h = self._master()
        for bad in ("not-an-email", "a@test.local,broken", "@test.local", "a b@test.local"):
            r = c.put("/api/mail-config",
                      json={"admin_to": bad, "confirm_password": ADMIN_PASS}, headers=h)
            self.assertEqual(r.status_code, 400, f"{bad!r} 应被拒绝")
        self.assertEqual(self._env_admin_to(), "old@test.local", "非法输入不得改动配置")

    def test_put_empty_string_clears_recipient(self):
        """显式提交空串 = 清空收件人（write_env_batch 空值 = 删键）。"""
        self._reset_env_file("YIBAN_MAIL_ADMIN_TO=old@test.local\n")
        c, h = self._master()
        r = c.put("/api/mail-config",
                  json={"admin_to": "", "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIsNone(self._env_admin_to(), "清空后 ADMIN_TO 键应被删除")

    def test_put_absent_key_leaves_value_unchanged(self):
        """键缺失 = 不改动（部分更新不得把收件人静默清空）。"""
        self._reset_env_file("YIBAN_MAIL_ADMIN_TO=keep@test.local\n")
        c, h = self._master()
        r = c.put("/api/mail-config", json={"enabled": True}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self._env_admin_to(), "keep@test.local")

    def test_get_masks_recipient(self):
        self._reset_env_file("YIBAN_MAIL_ADMIN_TO=boss@test.local\n")
        c, h = self._master()
        data = c.get("/api/mail-config", headers=h).get_json()
        self.assertIn("admin_to", data)
        self.assertNotIn("boss@test.local", json.dumps(data), "状态响应不得回显完整收件人地址")

    def test_audit_records_masked_recipient(self):
        self._reset_env_file()
        c, h = self._master()
        c.put("/api/mail-config",
              json={"admin_to": "audited@test.local", "confirm_password": ADMIN_PASS}, headers=h)
        conn = self.db._conn
        rows = conn.execute(
            "SELECT detail FROM audit_logs WHERE action='mail_config' ORDER BY id DESC LIMIT 1"
        ).fetchall()
        self.assertTrue(rows, "应写入 mail_config 审计")
        self.assertNotIn("audited@test.local", rows[0][0], "审计只记打码值")

    def test_old_recipient_notified_on_change(self):
        """被摘掉的旧收件人必须收到通知——否则被盗会话改收件人即致盲。"""
        self._reset_env_file("YIBAN_MAIL_ADMIN_TO=old@test.local\n")
        c, h = self._master()
        with mock.patch.object(self.webapp.mailer, "send_admin_alert", return_value=True) as m, \
                mock.patch.object(self.webapp, "send_notification", return_value=None):
            r = c.put("/api/mail-config",
                      json={"admin_to": "new@test.local", "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        targets = [call.kwargs.get("to") for call in m.call_args_list]
        self.assertIn("old@test.local", targets,
                      "旧收件人应收到『你已不再是收件人』通知")

    def test_no_stale_notice_when_address_unchanged(self):
        """地址没变（仅大小写/空白差异之外的同值）不应重复打扰。"""
        self._reset_env_file("YIBAN_MAIL_ADMIN_TO=same@test.local\n")
        c, h = self._master()
        with mock.patch.object(self.webapp.mailer, "send_admin_alert", return_value=True) as m, \
                mock.patch.object(self.webapp, "send_notification", return_value=None):
            r = c.put("/api/mail-config",
                      json={"admin_to": "same@test.local", "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(m.call_args_list, [], "同值提交不应发出旧收件人通知")


_MAIL_ENV_KEYS = (
    "YIBAN_MAIL_ENABLE", "YIBAN_MAIL_SMTPS_ENC", "YIBAN_MAIL_USER", "YIBAN_MAIL_PASS",
    "YIBAN_MAIL_SMTP_HOST", "YIBAN_MAIL_SMTP_PORT", "YIBAN_MAIL_ADMIN_TO",
)


def _load_webapp(tag):
    import db as _db
    spec = importlib.util.spec_from_file_location(f"webapp_{tag}", os.path.join(BASE, "web", "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"webapp_{tag}"] = mod
    with contextlib.suppress(Exception):
        spec.loader.exec_module(mod)
    return _db, mod


class _Base_FAILOVER(unittest.TestCase):
    """共享脚手架：临时 .env/DB + webapp 加载 + 主管理员登录（照抄 batch19）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-failover-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with io.open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                "YIBAN_ADMIN_USER=admin@test.local\n"
                f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
                # 改 SMTP/收件人/通道开关的门禁用例钉的是"当次要口令、失败零落盘"，
                # 固定在 full（默认档 risk 下这些动作不再当次要口令）
                "YIBAN_PW_GATE=full\n"
            )
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls._old_env = {k: os.environ.get(k) for k in (*_MAIL_ENV_KEYS, "YIBAN_ENV_FILE")}
        for k in _MAIL_ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        cls.db, cls.webapp = _load_webapp(cls.__name__)

    @classmethod
    def tearDownClass(cls):
        if cls.db._conn is not None:
            with contextlib.suppress(Exception):
                cls.db._conn.close()
            cls.db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k, v in cls._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def setUp(self):
        if self.db._conn is not None:
            with contextlib.suppress(Exception):
                self.db._conn.close()
            self.db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        self.db.init_db(self.db_file, migrate_from=self.accounts_file, env_file=self.env_file)

    def _reset_env_file(self, extra=""):
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                "YIBAN_ADMIN_USER=admin@test.local\n"
                f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
                "YIBAN_PW_GATE=full\n"  # 重置也保留门禁档位，见 setUpClass 的说明
                + extra
            )

    def _master(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "admin@test.local", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        t = c.get("/api/me").get_json()["csrf_token"]
        return c, {"X-CSRF-Token": t}

    def _read_enc_entries(self):
        """从 .env 读出 YIBAN_MAIL_SMTPS_ENC 并解密回列表（独立于 mailer 校验落盘格式）。"""
        raw = env_io.parse_env_file(self.env_file).get("YIBAN_MAIL_SMTPS_ENC", "")
        self.assertTrue(raw, "应写入 YIBAN_MAIL_SMTPS_ENC")
        entry = json.loads(raw)
        plain = account_crypto.decrypt_text(entry, account_crypto.load_key(self.env_file))
        return json.loads(plain)


class SmtpListTest(_Base_FAILOVER):
    """smtp_list：旧键回落与 ENC 解析。"""

    def test_fallback_single_entry_from_legacy_keys(self):
        self._reset_env_file("YIBAN_MAIL_USER=sender@qq.com\nYIBAN_MAIL_PASS=secret\n")
        entries = mailer.smtp_list()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["host"], "smtp.qq.com")  # HOST 未配置 → 默认
        self.assertEqual(entries[0]["port"], 465)
        self.assertEqual(entries[0]["user"], "sender@qq.com")
        self.assertEqual(entries[0]["pass"], "secret")

    def test_fallback_empty_when_no_config(self):
        self._reset_env_file()
        self.assertEqual(mailer.smtp_list(), [])

    def test_enc_roundtrip(self):
        self._reset_env_file()
        smtps = [
            {"host": "smtp1.example.com", "port": 465, "user": "a@x.com", "pass": "p1", "admin_to": ""},
            {"host": "smtp2.example.com", "port": 587, "user": "b@x.com", "pass": "p2",
             "admin_to": "boss@x.com"},
        ]
        enc = account_crypto.encrypt_text(
            json.dumps(smtps, ensure_ascii=False), account_crypto.load_key(self.env_file))
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_MAIL_SMTPS_ENC={json.dumps(enc, ensure_ascii=False)}\n"
            )
        self.assertEqual(mailer.smtp_list(), smtps)

    def test_enc_non_dict_entries_filtered(self):
        """解密列表中的非 dict 元素被过滤（脏数据不让调用方 e.get 崩溃）。"""
        self._reset_env_file()
        good = {"host": "smtp.x.com", "port": 465, "user": "a@x.com", "pass": "p1", "admin_to": ""}
        enc = account_crypto.encrypt_text(
            json.dumps(["garbage", good, 42], ensure_ascii=False),
            account_crypto.load_key(self.env_file))
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_MAIL_SMTPS_ENC={json.dumps(enc, ensure_ascii=False)}\n"
            )
        self.assertEqual(mailer.smtp_list(), [good])


class MailConfigSmtpsApiTest(_Base_FAILOVER):
    """PUT /api/mail-config smtps 字段：落盘加密、旧 pass/user 保留、口令门禁、GET 脱敏。"""

    def smtps(self):
        return [{"host": "smtp.x.com", "user": "a@x.com", "pass": "topsecret",
                 "admin_to": "boss@x.com"}]

    def test_put_smtps_writes_enc_and_roundtrips(self):
        """PUT 传入含 admin_to 的条目：该死键不再落盘，保存结果只含有效字段。"""
        self._reset_env_file()
        c, h = self._master()
        r = c.put("/api/mail-config",
                  json={"smtps": self.smtps(), "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        entries = self._read_enc_entries()
        self.assertEqual(entries, [{
            "host": "smtp.x.com", "port": 465, "user": "a@x.com",
            "pass": "topsecret",
        }])
        self.assertNotIn("admin_to", entries[0], "条目级 admin_to 死字段不得落盘")

    def test_put_smtps_empty_pass_keeps_old(self):
        self._reset_env_file()
        c, h = self._master()
        r = c.put("/api/mail-config",
                  json={"smtps": self.smtps(), "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        # 再次保存：host/user 不变、pass 留空 → 旧 pass 保留
        r = c.put("/api/mail-config",
                  json={"smtps": [{"host": "smtp.x.com", "user": "a@x.com", "pass": ""}],
                        "confirm_password": ADMIN_PASS},
                  headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        entries = self._read_enc_entries()
        self.assertEqual(entries[0]["pass"], "topsecret", "pass 留空应保留旧授权码")

    def test_put_smtps_empty_user_keeps_old(self):
        self._reset_env_file()
        c, h = self._master()
        r = c.put("/api/mail-config",
                  json={"smtps": self.smtps(), "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        # 再次保存：GET 已打码（不回显完整地址），user 留空 → 旧 user 保留
        r = c.put("/api/mail-config",
                  json={"smtps": [{"host": "smtp.x.com", "user": "", "pass": ""}],
                        "confirm_password": ADMIN_PASS},
                  headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        entries = self._read_enc_entries()
        self.assertEqual(entries[0]["user"], "a@x.com", "user 留空应保留旧发件账号")

    def test_put_smtps_requires_confirm_password(self):
        self._reset_env_file()
        c, h = self._master()
        r = c.put("/api/mail-config", json={"smtps": self.smtps()}, headers=h)
        self.assertEqual(r.status_code, 400)

    def test_put_smtps_invalid_entries_400(self):
        self._reset_env_file()
        c, h = self._master()
        for bad in (
            {"host": "", "user": "a@x.com"},
            {"host": "smtp.x.com", "user": "a@x.com", "port": 70000},
            {"host": "smtp.x.com", "user": "a@x.com", "port": "abc"},
        ):
            r = c.put("/api/mail-config",
                      json={"smtps": [bad], "confirm_password": ADMIN_PASS}, headers=h)
            self.assertEqual(r.status_code, 400, bad)
        r = c.put("/api/mail-config", json={"smtps": "not-a-list",
                                            "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 400)

    def test_put_smtps_null_fields_not_persisted_as_none(self):
        """JSON null 入参兜底：host=null → 400；user/pass=null 视为留空（按索引保留旧值），
        均不得经 str(None) 落盘为 "None"。"""
        self._reset_env_file()
        c, h = self._master()
        r = c.put("/api/mail-config",
                  json={"smtps": self.smtps(), "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        # host=null：修复前 str(None)="None" 非空会通过校验并落盘，必须 400
        r = c.put("/api/mail-config",
                  json={"smtps": [{"host": None, "user": "a@x.com"}],
                        "confirm_password": ADMIN_PASS},
                  headers=h)
        self.assertEqual(r.status_code, 400, "host=null 应拒绝（不得落盘为 \"None\"）")
        # user/pass=null：视为留空 → 按索引保留旧条目的 user/pass
        r = c.put("/api/mail-config",
                  json={"smtps": [{"host": "smtp.x.com", "user": None, "pass": None}],
                        "confirm_password": ADMIN_PASS},
                  headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        entries = self._read_enc_entries()
        self.assertEqual(entries[0]["user"], "a@x.com", "user=null 应视为留空并保留旧值")
        self.assertEqual(entries[0]["pass"], "topsecret", "pass=null 应视为留空并保留旧值")
        self.assertNotIn("None", (entries[0]["host"], entries[0]["user"]),
                         "任何字段都不得落盘为字符串 \"None\"")

    def test_get_smtps_masking_and_never_echoes_pass(self):
        self._reset_env_file()
        c, h = self._master()
        r = c.put("/api/mail-config",
                  json={"smtps": self.smtps(), "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        r = c.get("/api/mail-config", headers=h)
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(len(data["smtps"]), 1)
        self.assertEqual(data["smtps"][0]["host"], "smtp.x.com")
        # user 打码（与顶层字段同口径）：不含完整邮箱
        self.assertEqual(data["smtps"][0]["user"], "a***@x.com")
        self.assertNotIn("admin_to", data["smtps"][0], "条目级 admin_to 死字段不得序列化")
        self.assertTrue(data["smtps"][0]["has_pass"])
        self.assertNotIn("pass", data["smtps"][0], "pass 键不得出现在响应条目中")
        body = r.get_data(as_text=True)
        self.assertNotIn("topsecret", body, "响应全文不得含 pass 明文")
        self.assertNotIn("a@x.com", body, "响应全文不得含完整发件账号")
        self.assertNotIn("boss@x.com", body, "响应全文不得含完整 admin_to 地址")


class MailConfigSaveAtomicTest(_Base_FAILOVER):
    """PUT /api/mail-config：密文与开关一次原子写入；变更告警只在写入成功后发。

    原实现两次独立写（write_env_key 写密文 + write_env_batch 写开关），中间崩溃
    留下"密文新/开关旧"中间态；且两条"配置变更"告警在加密/落盘**之前**外发，
    加密失败（500）时运营者已收到一条描述从未生效变更的通知。
    """

    def smtps(self):
        return [{"host": "smtp.x.com", "user": "a@x.com", "pass": "topsecret",
                 "admin_to": ""}]

    def _spy_write(self):
        """记录含 YIBAN_MAIL_* 键的 write_env_batch 调用，其余照常透传。"""
        real = self.webapp.write_env_batch
        calls = []

        def spy(env_path, updates):
            if any(k.startswith("YIBAN_MAIL") for k in updates):
                calls.append(dict(updates))
            return real(env_path, updates)

        return mock.patch.object(self.webapp, "write_env_batch", side_effect=spy), calls

    def test_smtps_and_flags_in_one_atomic_write(self):
        self._reset_env_file()
        c, h = self._master()
        p, calls = self._spy_write()
        with p:
            r = c.put("/api/mail-config",
                      json={"enabled": False, "admin_notify": True,
                            "smtps": self.smtps(), "confirm_password": ADMIN_PASS},
                      headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(len(calls), 1, "密文与开关必须同一次 write_env_batch 落盘")
        self.assertIn("YIBAN_MAIL_SMTPS_ENC", calls[0])
        self.assertEqual(calls[0]["YIBAN_MAIL_ENABLE"], "0")
        self.assertEqual(calls[0]["YIBAN_MAIL_ADMIN_NOTIFY"], "1")
        self.assertEqual(self._read_enc_entries()[0]["user"], "a@x.com")
        self.assertEqual(env_io.parse_env_file(self.env_file).get("YIBAN_MAIL_ENABLE"), "0")

    def test_invalid_smtps_400_leaves_env_unchanged(self):
        self._reset_env_file("YIBAN_MAIL_ENABLE=1\n")
        c, h = self._master()  # 登录会把口令迁移成哈希并重写 .env，快照取在登录之后
        with io.open(self.env_file, encoding="utf-8-sig") as f:
            before = f.read()
        r = c.put("/api/mail-config",
                  json={"smtps": [{"user": "a@x.com"}], "confirm_password": ADMIN_PASS},
                  headers=h)
        self.assertEqual(r.status_code, 400)
        with io.open(self.env_file, encoding="utf-8-sig") as f:
            self.assertEqual(f.read(), before, "校验失败的请求不得动 .env")

    def test_change_alert_fires_only_after_successful_write(self):
        self._reset_env_file()
        c, h = self._master()
        alerts = []
        with mock.patch.object(
                self.webapp, "send_notification",
                side_effect=lambda t, c_, urgent=False, force=False, ledger=None:
                alerts.append(t)):
            # 写入失败（模拟磁盘错）："配置已变更"告警不得外发——它只能描述已落盘的事实
            with mock.patch.object(self.webapp, "write_env_batch",
                                   side_effect=RuntimeError("disk full")):
                r = c.put("/api/mail-config",
                          json={"enabled": True, "smtps": self.smtps(),
                                "confirm_password": ADMIN_PASS}, headers=h)
            self.assertEqual(r.status_code, 500, r.get_data(as_text=True))
            self.assertEqual(alerts, [], "写入失败不得外发「配置已变更」告警")
            self.assertNotIn("YIBAN_MAIL_SMTPS_ENC",
                             env_io.parse_env_file(self.env_file))
            # 写入成功：两条变更告警在落盘之后发出（force=True 绕过节流，不因
            # 新写入的参数被吞）
            r = c.put("/api/mail-config",
                      json={"enabled": True, "smtps": self.smtps(),
                            "confirm_password": ADMIN_PASS}, headers=h)
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIn("邮件配置变更告警", alerts)
        self.assertIn("邮件 SMTP 配置变更告警", alerts)
        self.assertEqual(self._read_enc_entries()[0]["user"], "a@x.com")
