# -*- coding: utf-8 -*-
"""mailer 邮箱通知模块单元测试（A 线：管理员告警邮件）。

标签：H · 通知：邮件与推送
覆盖：未配置不启用/不发送；`SMTP_SSL` 成功发送；发送异常静默且日志脱敏（不泄露授权码、
    不回显完整发件地址）；多收件人逗号分隔；邮箱打码。
对应实现：`yiban/mail/config.py`（配置与启用判定）、`yiban/mail/transport.py`
    （实际发送与日志）。
关键断言：失败必须**静默**（发信失败不能把签到主流程带崩），但日志要留下可判定的
    分类——"静默"只针对异常传播，不针对记录。
依赖：monkeypatch `yiban.mail.transport.smtplib`；临时 .env 与 `YIBAN_ACCOUNTS_KEY`；
    不真发信、不触网。
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sqlite3
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

    def test_put_admin_to_without_password_ok(self):
        """收件人变更是可逆路由改动（缩减批 6a 免门）：无口令直达落盘，留痕在审计行。"""
        self._reset_env_file("YIBAN_MAIL_ADMIN_TO=old@test.local\n")
        c, h = self._master()
        r = c.put("/api/mail-config", json={"admin_to": "new@test.local"}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self._env_admin_to(), "new@test.local")

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

    def test_old_recipient_not_notified_on_change(self):
        """改收件人不再单独通知旧地址，也不发变更告警：只在审计里留痕。

        旧实现用"发给旧收件人"防致盲，但那一封走的正是被改动的通道本身（拆报警器的
        动作由报警器来通报）——只有通道还活着时才有意义；留痕改由 mail_config 审计承担。
        """
        self._reset_env_file("YIBAN_MAIL_ADMIN_TO=old@test.local\n")
        c, h = self._master()
        with mock.patch.object(self.webapp.mailer, "send_admin_alert", return_value=True) as m, \
                mock.patch.object(self.webapp, "send_notification", return_value=None) as sn:
            r = c.put("/api/mail-config",
                      json={"admin_to": "new@test.local", "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(m.call_args_list, [], "旧收件人通知已下线")
        sn.assert_not_called()
        self.assertEqual(self._env_admin_to(), "new@test.local", "改收件人本身必须照旧生效")

    def test_recipient_unchanged_writes_no_alert(self):
        """地址没变（同值提交）不产生任何外发。"""
        self._reset_env_file("YIBAN_MAIL_ADMIN_TO=same@test.local\n")
        c, h = self._master()
        with mock.patch.object(self.webapp.mailer, "send_admin_alert", return_value=True) as m, \
                mock.patch.object(self.webapp, "send_notification", return_value=None) as sn:
            r = c.put("/api/mail-config",
                      json={"admin_to": "same@test.local", "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(m.call_args_list, [])
        sn.assert_not_called()


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
        """PUT 传入含 admin_to 的条目：该死键不再落盘，保存结果只含有效字段。

        id 是条目稳定身份、保存时补发（本用例不带 id → 后端生成一个形状合法的），
        所以精确断言前先摘掉 id，形状本身由 SmtpEntryIdentityTest 钉。
        """
        self._reset_env_file()
        c, h = self._master()
        r = c.put("/api/mail-config",
                  json={"smtps": self.smtps(), "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        entries = self._read_enc_entries()
        self.assertRegex(entries[0].get("id"), r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$",
                         "无 id 提交应被补发稳定 id（禁止以位置为身份）")
        entries[0].pop("id")
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
        """JSON null 入参兜底：host=null → 400；user/pass=null 视为留空（按稳定身份/
        目标认领保留旧值，非按数组位置），均不得经 str(None) 落盘为 "None"。"""
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
        # user/pass=null：视为留空 → 该条无 id、按 (host,port) 迁移口径认领旧条目并沿用其 user/pass
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


class SmtpEntryIdentityTest(_Base_FAILOVER):
    """SMTP 条目稳定 id：身份= id（不是数组位置），凭据只随"同 id 且目标未变"沿用。

    反例矩阵（修复前全部翻车）：中间删一行凭据错配 / 只改 host 旧授权码随新域名
    发出 / 旧格式配置（无 id）必须照旧可读可改（迁移口径）/ 改中继收件人必须
    入审计且留可还原目标。id 形状与 web/routes/notify.py 的 _SMTP_ID_RE 同口径。
    """

    ID_RE = r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$"

    def _put(self, c, h, smtps, **extra):
        body = dict(extra)
        body.update({"smtps": smtps, "confirm_password": ADMIN_PASS})
        return c.put("/api/mail-config", json=body, headers=h)

    def _three(self):
        """三条典型清单。host 刻意取短：审计视图预算 165 字符（含作用域标记），
        超长 host 的裁剪行为另有专门用例（test_audit_view_over_budget…）钉。"""
        return [
            {"id": "smtp-aaaa00000001", "host": "a.io", "port": 465,
             "user": "a@x.com", "pass": "passA"},
            {"id": "smtp-bbbb00000002", "host": "b.io", "port": 587,
             "user": "b@x.com", "pass": "passB"},
            {"id": "smtp-cccc00000003", "host": "c.io", "port": 465,
             "user": "c@x.com", "pass": "passC"},
        ]

    def _write_legacy_blob(self, entries):
        """不经端点、直接落一份旧形状（无 id）密文——现网存量配置的样貌。"""
        enc = account_crypto.encrypt_text(
            json.dumps(entries, ensure_ascii=False), account_crypto.load_key(self.env_file))
        self._reset_env_file(f"YIBAN_MAIL_SMTPS_ENC={json.dumps(enc, ensure_ascii=False)}\n")

    def _last_audit_detail(self):
        rows = self.db._conn.execute(
            "SELECT detail FROM audit_logs WHERE action='mail_config' ORDER BY id DESC LIMIT 1"
        ).fetchall()
        self.assertTrue(rows, "应写入 mail_config 审计")
        return rows[0][0]

    # ---- ① id 契约 ----
    def test_generated_id_shape_and_roundtrip(self):
        """无 id 提交→后端补发稳定 id；GET 原样带回；显式 id 保存后不变。"""
        self._reset_env_file()
        c, h = self._master()
        r = self._put(c, h, [{"host": "smtp.x.com", "user": "a@x.com", "pass": "p1"}])
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        saved = self._read_enc_entries()[0]
        self.assertRegex(saved["id"], self.ID_RE)
        got = c.get("/api/mail-config", headers=h).get_json()["smtps"][0]
        self.assertEqual(got["id"], saved["id"], "GET 必须带回落盘 id（前后端同口径）")
        r = self._put(c, h, [{"id": "smtp-manual00001", "host": "smtp.x.com",
                              "user": "a@x.com", "pass": ""}])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._read_enc_entries()[0]["id"], "smtp-manual00001")

    def test_bad_id_shapes_rejected(self):
        """id 是不透明标识不是自由文本：非法形状/请求内重复 → 400，且零落盘。"""
        self._reset_env_file()
        c, h = self._master()
        before = self._read_env_all()
        for bad in ("bad id!", "x" * 65, "smtp/../etc"):
            r = self._put(c, h, [{"id": bad, "host": "smtp.x.com", "user": "a@x.com"}])
            self.assertEqual(r.status_code, 400, bad)
        r = self._put(c, h, [{"id": "smtp-dup0000001", "host": "smtp.x.com"},
                             {"id": "smtp-dup0000001", "host": "smtp.y.com"}])
        self.assertEqual(r.status_code, 400, "同一请求内 id 重复=两条目认领同一身份")
        self.assertEqual(self._read_env_all(), before, "被拒请求不得动 .env")

    # ---- ② 反例矩阵：删中间行 / 重排 / 只改 host ----
    def test_delete_middle_row_credentials_follow_id(self):
        """删掉中间那行后，剩余两条必须各自保住**自己**的授权码/账号。

        修复前：后端按提交索引取旧条目，第二条（c.io）会沿用到被删那条
        （passB）的授权码——凭据错配。
        """
        self._reset_env_file()
        c, h = self._master()
        self.assertEqual(self._put(c, h, self._three()).status_code, 200)
        kept = [{"id": "smtp-aaaa00000001", "host": "a.io", "port": 465,
                 "user": "", "pass": ""},
                {"id": "smtp-cccc00000003", "host": "c.io", "port": 465,
                 "user": "", "pass": ""}]
        self.assertEqual(self._put(c, h, kept).status_code, 200)
        saved = self._read_enc_entries()
        self.assertEqual([e["id"] for e in saved],
                         ["smtp-aaaa00000001", "smtp-cccc00000003"])
        self.assertEqual([e["pass"] for e in saved], ["passA", "passC"],
                         "留空沿用必须按 id 找回各自的旧授权码（旧实现第二条会拿到 passB）")
        self.assertEqual([e["user"] for e in saved], ["a@x.com", "c@x.com"])

    def test_reorder_credentials_follow_id(self):
        """整表重排：凭据跟着 id 走，不跟着位置走。"""
        self._reset_env_file()
        c, h = self._master()
        self.assertEqual(self._put(c, h, self._three()).status_code, 200)
        shuffled = [
            {"id": "smtp-cccc00000003", "host": "c.io", "port": 465, "user": "", "pass": ""},
            {"id": "smtp-aaaa00000001", "host": "a.io", "port": 465, "user": "", "pass": ""},
            {"id": "smtp-bbbb00000002", "host": "b.io", "port": 587, "user": "", "pass": ""},
        ]
        self.assertEqual(self._put(c, h, shuffled).status_code, 200)
        saved = self._read_enc_entries()
        self.assertEqual([e["pass"] for e in saved], ["passC", "passA", "passB"])

    def test_host_change_never_carries_old_credentials(self):
        """只改 host = 换中继：同 id 也**不得**把旧授权码/旧发件账号带过去。

        修复前这正是"risk 档零口令零确认"下最静默的泄漏：留空被当成"沿用"，
        旧码随新域名发出。现在留空按空值落盘，has_pass 变 false。
        """
        self._reset_env_file()
        c, h = self._master()
        self.assertEqual(self._put(c, h, self._three()).status_code, 200)
        moved = [{"id": "smtp-aaaa00000001", "host": "smtp.evil-new.com",
                  "port": 465, "user": "", "pass": ""}]
        self.assertEqual(self._put(c, h, moved).status_code, 200)
        saved = self._read_enc_entries()
        self.assertEqual(saved[0]["id"], "smtp-aaaa00000001", "目标变了身份可以保留（同一行的语义）")
        self.assertEqual(saved[0]["pass"], "", "旧授权码绝不随新域名发出")
        self.assertEqual(saved[0]["user"], "", "旧发件账号同样不跟去新目标")
        got = c.get("/api/mail-config", headers=h).get_json()["smtps"][0]
        self.assertFalse(got["has_pass"], "GET 必须如实报告这条已经没有授权码了")

    def test_port_change_same_target_rule(self):
        """同 host 换端口也算换目标（凭据按中继+端口整体走），同样不沿用。"""
        self._reset_env_file()
        c, h = self._master()
        self.assertEqual(self._put(c, h, [{"id": "smtp-port0000001", "host": "smtp.x.com",
                                           "port": 465, "user": "a@x.com", "pass": "pS"}]).status_code, 200)
        self.assertEqual(self._put(c, h, [{"id": "smtp-port0000001", "host": "smtp.x.com",
                                           "port": 587, "user": "", "pass": ""}]).status_code, 200)
        self.assertEqual(self._read_enc_entries()[0]["pass"], "")

    # ---- ③ 迁移口径：旧配置（无 id）兼容读取 + 反例 ----
    def test_legacy_blob_readable_and_id_assigned_on_save(self):
        """旧形状密文（无 id）：GET 可读（id=null）、可发（smtp_list 不受影响），
        下一次保存按 (host,port) 唯一匹配认领旧凭据并落定 id。"""
        legacy = [
            {"host": "smtp.a.com", "port": 465, "user": "a@x.com", "pass": "passA"},
            {"host": "smtp.b.com", "port": 465, "user": "b@x.com", "pass": "passB"},
        ]
        self._write_legacy_blob(legacy)
        import yiban.mail as mailer_mod
        self.assertEqual(mailer_mod.smtp_list(), legacy, "旧配置发送路径读取不得受影响")
        c, h = self._master()
        got = c.get("/api/mail-config", headers=h).get_json()["smtps"]
        self.assertEqual([e["id"] for e in got], [None, None], "旧格式条目 id 回 null（前端现造）")
        # 旧客户端（完全不带 id）留空保存：仍按 host:port 认领，旧凭据不丢
        self.assertEqual(self._put(c, h, [
            {"host": "smtp.b.com", "port": 465, "user": "", "pass": ""},
            {"host": "smtp.a.com", "port": 465, "user": "", "pass": ""},
        ]).status_code, 200)
        saved = self._read_enc_entries()
        self.assertEqual([e["pass"] for e in saved], ["passB", "passA"],
                         "重排 + 无 id：host:port 唯一匹配，位置不参与身份")
        for e in saved:
            self.assertRegex(e["id"], self.ID_RE, "保存后 id 必须落定（迁移只此一次）")

    def test_legacy_middle_delete_credentials_follow_host(self):
        """旧配置 + 旧客户端删中间行：剩余两条的凭据按 host 找回自己那条。

        修复前这是最典型事故：删掉中间一行后，第三条滑到索引 1，沿用被删那行的
        passB——凭据错配。
        """
        self._write_legacy_blob([
            {"host": "smtp.a.com", "port": 465, "user": "a@x.com", "pass": "passA"},
            {"host": "smtp.b.com", "port": 465, "user": "b@x.com", "pass": "passB"},
            {"host": "smtp.c.com", "port": 465, "user": "c@x.com", "pass": "passC"},
        ])
        c, h = self._master()
        self.assertEqual(self._put(c, h, [
            {"host": "smtp.a.com", "port": 465, "user": "", "pass": ""},
            {"host": "smtp.c.com", "port": 465, "user": "", "pass": ""},
        ]).status_code, 200)
        saved = self._read_enc_entries()
        self.assertEqual([e["pass"] for e in saved], ["passA", "passC"],
                         "旧实现第二条会拿到 passB（按索引）")

    def test_ambiguous_legacy_target_fails_closed(self):
        """旧配置里同 host:port 两条（歧义）+ 无 id 提交：不猜归属，留空按空落盘。"""
        self._write_legacy_blob([
            {"host": "smtp.d.com", "port": 465, "user": "d1@x.com", "pass": "passD1"},
            {"host": "smtp.d.com", "port": 465, "user": "d2@x.com", "pass": "passD2"},
        ])
        c, h = self._master()
        self.assertEqual(self._put(c, h, [
            {"host": "smtp.d.com", "port": 465, "user": "", "pass": ""},
        ]).status_code, 200)
        saved = self._read_enc_entries()
        self.assertEqual(saved[0]["pass"], "", "歧义时宁可清空也不猜——猜错就是凭据错配")

    # ---- ④ 改告警去向 = 入审计且留可还原目标 ----
    def test_audit_records_from_to_without_secrets(self):
        """中继与收件人的变更审计必须能读出"从哪改到哪"（打码口径），零凭据。

        host 刻意取短：审计预算 200 字符（含作用域标记），超长清单的裁剪行为由
        下一条用例单独钉，本用例验的是典型规模下 from/to 双方完整可读。
        """
        self._reset_env_file("YIBAN_MAIL_ADMIN_TO=boss@o.io\n")
        c, h = self._master()
        self.assertEqual(self._put(c, h, self._three()).status_code, 200)
        moved = [{"id": "smtp-aaaa00000001", "host": "a.io", "port": 465,
                  "user": "a@x.com", "pass": "passA"},
                 {"id": "smtp-new00000009", "host": "s9.io", "port": 465,
                  "user": "z@x.com", "pass": "passZ"}]
        r = self._put(c, h, moved, admin_to="alert@new.io")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        raw = self._last_audit_detail()
        detail = json.loads(raw)
        self.assertEqual(detail["smtps_from"],
                         ["a.io:465", "b.io:587", "c.io:465"],
                         "旧去向必须逐条留下（可还原目标）")
        self.assertEqual(detail["smtps_to"], ["a.io:465", "s9.io:465"])
        self.assertEqual(detail["admin_to"], "a****@new.io")
        self.assertEqual(detail["admin_to_from"], "b***@o.io",
                         "from 必须是落盘前的旧值，不能是刚写进去的新值")
        for secret in ("passA", "passZ", "passB", "passC", "boss@o.io",
                       "alert@new.io", "a@x.com", "z@x.com", "b@x.com", "c@x.com",
                       '"pass"'):
            self.assertNotIn(secret, raw, f"审计不得含敏感值：{secret}")

    def test_audit_view_over_budget_degrades_loudly_not_broken(self):
        """超出审计 200 字符预算时：裁剪 + `_cut` 标注，detail 仍是合法 JSON。

        默认实现（audit 层 [:200] 直切）会把 JSON 截成非法串——下游还原失败，
        "可还原目标"名存实亡。本用例钉"放不下要出声，不能截烂"。
        """
        self._reset_env_file()
        c, h = self._master()
        long_host = "smtp-" + "a" * 48 + ".example-long.test"
        many = [{"id": f"smtp-long{i:011d}", "host": f"{chr(97 + i)}{long_host}",
                 "port": 465, "user": "u@x.com", "pass": "p"}
                for i in range(6)]
        self.assertEqual(self._put(c, h, many).status_code, 200)
        # 第二次保存同规模清单（换 host 前缀即可）：from 与 to 都装不进预算，
        # 两侧同受裁剪——钉"截烂"回归与"先新后旧"的裁剪方向
        many2 = [dict(e, id=f"smtp-new{i:011d}",
                      host=f"{chr(106 + i)}{long_host}") for i, e in enumerate(many)]
        self.assertEqual(self._put(c, h, many2).status_code, 200)
        detail = self._last_audit_detail()
        self.assertLessEqual(len(detail), 200, "detail 必须在审计预算内")
        parsed = json.loads(detail)   # 非法 JSON 在这里直接炸——即回归点
        self.assertTrue(any(k.endswith("_cut") for k in parsed) or parsed.get("smtps_view"),
                        "裁剪必须显式标注而不是静默截烂")
        self.assertEqual(parsed["smtps_count"], 6, "计数等骨架键不得随视图丢失")
        # 新清单先被牺牲、旧存证尽量保完整（to 可再生，from 是全库唯一的旧去向）
        self.assertLessEqual(len(parsed.get("smtps_to", [])),
                             len(parsed.get("smtps_from", [])))

    def test_audit_clearing_all_smtps_keeps_old_targets(self):
        """清空发信清单也是改告警去向：smtps_from 保留全部旧目标，smtps_to 为空。"""
        self._reset_env_file()
        c, h = self._master()
        self.assertEqual(self._put(c, h, self._three()).status_code, 200)
        self.assertEqual(self._put(c, h, []).status_code, 200)
        detail = json.loads(self._last_audit_detail())
        self.assertEqual(detail["smtps_count"], 0)
        self.assertEqual(len(detail["smtps_from"]), 3, "清掉的去向才是最需要能还原的现场")
        self.assertEqual(detail["smtps_to"], [])

    def _read_env_all(self):
        from yiban.infra import env_io
        return env_io.parse_env_file(self.env_file)


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

    def test_write_failure_leaves_no_trace_and_no_alert(self):
        """落盘失败 → 500、零写入、零外发；落盘成功 → 配置生效、仍零外发。

        告警下线后留下的是更硬的事实：失败请求不得在 .env 上留半个键。
        """
        self._reset_env_file()
        c, h = self._master()
        alerts = []
        with mock.patch.object(
                self.webapp, "send_notification",
                side_effect=lambda t, c_, urgent=False, force=False, ledger=None:
                alerts.append(t)):
            # 写入失败（模拟磁盘错）：不得外发，也不得在 .env 上留半个键
            with mock.patch.object(self.webapp, "write_env_batch",
                                   side_effect=RuntimeError("disk full")):
                r = c.put("/api/mail-config",
                          json={"enabled": True, "smtps": self.smtps(),
                                "confirm_password": ADMIN_PASS}, headers=h)
            self.assertEqual(r.status_code, 500, r.get_data(as_text=True))
            self.assertEqual(alerts, [], "写入失败不得外发")
            self.assertNotIn("YIBAN_MAIL_SMTPS_ENC",
                             env_io.parse_env_file(self.env_file))
            # 写入成功：配置真的落盘（通道变更只留审计，不再外发告警）
            r = c.put("/api/mail-config",
                      json={"enabled": True, "smtps": self.smtps(),
                            "confirm_password": ADMIN_PASS}, headers=h)
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(alerts, [], "通道变更告警已下线")
        self.assertEqual(self._read_enc_entries()[0]["user"], "a@x.com")


# ---- MF-44 / MF-90：投递账、幂等键、发信账号单行化 ----

def test_recipients_refused_writes_delivery_ledger(monkeypatch, tmp_path):
    """B5（MF-44 补句）：`SMTPRecipientsRefused.recipients` 不再是零消费者。

    旧实现把逐地址拒收结果聚合成一行粗分类 warning——管理员无法回答"哪个地址死了、
    是退信还是中继拒绝"。现在每收件人一条可机读投递账，落**既有审计面**（不新造
    第二套状态存储）；对端回显的完整收件人地址先换成打码形态（隐私与日志注入同收口）。
    """
    import smtplib

    from yiban.store import db as store_db

    _isolate_env(monkeypatch, tmp_path)
    _set_mail(monkeypatch, ENABLE="1", USER="sender@qq.com", PASS="secret")
    # 投递账要落库：为本用例起一颗独立临时库（结束后复位连接，不牵连同文件其它用例）
    db_file = str(tmp_path / "delivery.db")
    monkeypatch.setenv("YIBAN_ACCOUNTS_KEY", _KEY)
    monkeypatch.setenv("YIBAN_STATE_DIR", str(tmp_path))
    with contextlib.suppress(Exception):
        if store_db._conn is not None:
            store_db._conn.close()
    store_db._conn = None
    store_db.init_db(db_file, env_file=str(tmp_path / "nope.env"))

    victim = "victim@qq.com"
    refused = smtplib.SMTPRecipientsRefused(
        {victim: (550, b"<victim@qq.com>: Recipient address rejected: User unknown")})

    class RefusedServer:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, u, p):
            pass

        def sendmail(self, frm, to, msg):
            raise refused

    monkeypatch.setattr(mailer_transport.smtplib, "SMTP_SSL", RefusedServer)
    assert mailer_transport.send_admin_alert("爆破告警", "正文", to=victim) is False

    conn = sqlite3.connect(db_file)
    try:
        rows = conn.execute(
            "SELECT target, detail FROM audit_logs WHERE action='mail_refused'").fetchall()
    finally:
        conn.close()
    assert len(rows) == 1, f"每收件人一条投递账：{rows}"
    target, detail = rows[0]
    assert target == "v*****@qq.com", f"target 必须是打码形态：{target}"
    assert "code=550" in detail and "entry=1/1" in detail, detail
    assert victim not in target + detail, "投递账不得含明文收件地址（含对端回显段）"
    with contextlib.suppress(Exception):
        store_db._conn.close()
    store_db._conn = None


def test_failover_resends_identical_message_id_and_logs_retry(monkeypatch, tmp_path, caplog):
    """C7+C8（MF-90）：QUIT 抛异常换条目重投的是同一封——同确定性 Message-ID，重投可见。

    机制：`smtplib.SMTP.__exit__` 的 QUIT 抛 SMTPResponseException ⇒ 捕获后换下一条
    重投；旧构造无 `Message-ID`，接收端无从去重 ⇒ 同一封最多可送达 10 次（条目上限）。
    现在 10 次尝试携带同一个由内容哈希出的键（非随机 nonce），且每次换条目留一行
    "同封重投"记录——上界从 10 降到 1 的机制基础 + 有界可观测。
    """
    import logging
    import re
    import smtplib

    entries = [{"host": f"smtp{i}.example.test", "port": 465,
                "user": f"sender{i}@qq.com", "pass": "secret"} for i in range(10)]
    _env_with_blob(monkeypatch, tmp_path, _enc_blob(entries))
    monkeypatch.setenv("YIBAN_MAIL_ENABLE", "1")
    captured = []

    class QuitBadServer:
        """login/sendmail 都"成功"，__exit__ 的 QUIT 抛——前 9 条炸，第 10 条放行。"""

        def __init__(self, host, port, **k):
            self.host = host

        def __enter__(self):
            return self

        def __exit__(self, *a):
            if self.host != "smtp9.example.test":
                raise smtplib.SMTPResponseException(421, b"server busy, quitting later")
            return False

        def login(self, u, p):
            pass

        def sendmail(self, frm, to, msg):
            captured.append(msg)

    monkeypatch.setattr(mailer_transport.smtplib, "SMTP_SSL", QuitBadServer)
    with caplog.at_level(logging.INFO, logger="mailer"):
        assert mailer_transport.send_admin_alert("同一封告警", "正文", to="admin@qq.com") is True
    assert len(captured) == 10, "QUIT 炸 9 次 + 第 10 条成功 = 恰好投了 10 次（有界）"
    ids = re.findall(r"(?m)^Message-ID: (.*)$", "\n".join(captured))
    assert len(ids) == 10 and set(ids) == {ids[0]}, \
        f"跨条目重投必须同幂等键（同封可辨识）：{set(ids)}"
    assert re.fullmatch(r"<[0-9a-f]{32}@yiban>", ids[0]), f"幂等键须确定性可辨：{ids[0]}"
    assert caplog.text.count("同封重投") == 9, "换条目重投每条都要有结构化记录"
    # 确定性：同告警同键；换了内容/收件人即换键（不许撞车）
    mid = mailer_transport._message_id
    assert mid("a", "b", "c") == mid("a", "b", "c")
    assert mid("a", "b", "c") != mid("a", "b", "c2")
    assert mid("a", "b", "c") != mid("a", "d", "c")


def test_sender_user_newlines_never_reach_smtp(monkeypatch, tmp_path):
    """D11（MF-44）：`user` 字段一处最小收口——发送前同源单行化。

    管理员把 SMTP 用户名写成含 CRLF 属"只可能由管理员本人制造"的情形（章程校准：
    一处收口、不建校验框架）：单行化后注入形态变成必然被对端拒绝的坏地址，
    MAIL FROM / From 头的物理注入面不存在。
    """
    _isolate_env(monkeypatch, tmp_path)
    sneaky = "evil@qq.com\r\nMAIL FROM:<pwn@evil.test>"
    _set_mail(monkeypatch, ENABLE="1", USER=sneaky, PASS="secret")
    seen = {}

    class CaptureServer:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, u, p):
            seen["login_user"] = u

        def sendmail(self, frm, to, msg):
            seen["frm"] = frm
            seen["msg"] = msg

    monkeypatch.setattr(mailer_transport.smtplib, "SMTP_SSL", CaptureServer)
    assert mailer_transport.send_admin_alert("告警", "正文", to="admin@qq.com") is True
    assert "\r" not in seen["login_user"] and "\n" not in seen["login_user"]
    assert "\r" not in seen["frm"] and "\n" not in seen["frm"]
    assert "\r\nMAIL FROM:<pwn@evil.test>" not in seen["msg"], \
        "换行注入体不得以物理行形态进入邮件数据"
    assert "MAIL FROM:<pwn@evil.test>" in seen["msg"], "收口是单行化（可见字面量），不是静默删除"
