# -*- coding: utf-8 -*-
"""mailer 邮箱通知模块单元测试（A 线：管理员告警邮件）。

覆盖：未配置不启用/不发送；SMTP_SSL 成功发送；发送异常静默且日志脱敏
（不泄露授权码、不回显完整发件地址）；多收件人逗号分隔；邮箱打码。
全程 mock smtplib，不发起真实网络请求。
"""
import json
import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import account_crypto  # noqa: E402
import mailer  # noqa: E402

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
    # 保留前 3 字符，其余打码（10 字符 → 7 个星）
    assert mailer._mask_addr("1234567890@qq.com") == "123*******@qq.com"


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
