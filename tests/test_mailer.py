# -*- coding: utf-8 -*-
"""mailer 邮箱通知模块单元测试（A 线：管理员告警邮件）。

覆盖：未配置不启用/不发送；SMTP_SSL 成功发送；发送异常静默且日志脱敏
（不泄露授权码、不回显完整发件地址）；多收件人逗号分隔；邮箱打码。
全程 mock smtplib，不发起真实网络请求。
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))

import mailer  # noqa: E402


def _isolate_env(monkeypatch, tmp_path):
    """隔离环境：YIBAN_ENV_FILE 指向不存在文件 + 清理已有 YIBAN_MAIL_* 环境变量。"""
    monkeypatch.setenv("YIBAN_ENV_FILE", str(tmp_path / "no-such.env"))
    for k in list(os.environ):
        if k.startswith("YIBAN_MAIL_"):
            monkeypatch.delenv(k)


def _set_mail(monkeypatch, **kwargs):
    """批量设置 YIBAN_MAIL_* 环境变量。"""
    for k, v in kwargs.items():
        monkeypatch.setenv("YIBAN_MAIL_" + k, v)


def test_mask_addr_masks_local_part():
    # 保留前 3 字符，其余打码（9 字符 → 6 个星）
    assert mailer._mask_addr("477929858@qq.com") == "477******@qq.com"


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
    assert mailer.send_admin_alert("标题", "内容") is False


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

    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", FakeServer)
    # 2026-08-27 契约变更：to 必须由调用方显式传入（缺省不再回退 ADMIN_TO）
    assert mailer.send_admin_alert("告警", "内容", to="admin@qq.com") is True
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

    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", Boom)
    with caplog.at_level("WARNING", logger="mailer"):
        assert mailer.send_admin_alert("告警", "内容", to="admin@qq.com") is False
    assert "OSError" in caplog.text
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

    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", BoomShouldNotConstruct)
    with caplog.at_level("WARNING", logger="mailer"):
        assert mailer.send_admin_alert("告警", "内容") is False
        assert mailer.send_admin_alert("告警", "内容", to="   ") is False
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

    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", FakeServer)
    mailer.send_admin_alert("告警", "内容", to="a@qq.com,b@qq.com")
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

    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", FakeServer)
    mailer.send_admin_alert("告警", "内容", to="only@qq.com")
    assert targets == ["only@qq.com"], "显式 to 应只发给传入地址"


def test_get_config_never_exposes_password(monkeypatch, tmp_path):
    _isolate_env(monkeypatch, tmp_path)
    _set_mail(monkeypatch, ENABLE="1", USER="sender@qq.com", PASS="topsecret", ADMIN_TO="admin@qq.com")
    cfg = mailer.get_config()
    assert "topsecret" not in str(cfg), "get_config 不得泄露授权码"
    assert cfg["user"] == "sen***@qq.com"
