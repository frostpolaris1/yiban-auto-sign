# -*- coding: utf-8 -*-
"""scripts/notify.py Webhook 推送组件单元测试（2026-08-29）。

覆盖：
- 配置读取：未配置禁用 / 兼容旧明文 YIBAN_NOTIFY_URL / 加密密文解密回读 /
  密文损坏返回空
- Server酱：URL 与参数格式（title+desp）、标题截断去换行、code==0 成功、
  非零 code 失败并告警、异常静默失败
- 自定义 URL：JSON {title,content}、SSRF 白名单拒绝不安全地址、非 2xx 失败
- 同类型节流：窗口内同标题跳过、force 绕过、cooldown=0 关闭
- 日志脱敏：SendKey / URL token / userinfo 一律不进日志
全程 mock requests，不发起真实网络请求。
用法（项目根目录）：
    py -m pytest tests/test_notify_0829.py -v
"""
import json
import logging
import os
import sys
import tempfile
from unittest import mock

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))

import account_crypto  # noqa: E402
import notify  # noqa: E402

KEY = "f" * 64
SCT_KEY = "SCT406257TESTTESTTESTTESTTEST"


def _clear(monkeypatch):
    """隔离环境：固定加密密钥 + 隔离 .env（不读项目根 .env）+ 清空 YIBAN_NOTIFY_*。"""
    monkeypatch.setenv("YIBAN_ACCOUNTS_KEY", KEY)
    # 指向不存在的 .env，避免 notify 回退读取项目根 .env（含真实 Server酱 配置）
    monkeypatch.setenv("YIBAN_ENV_FILE",
                       os.path.join(tempfile.gettempdir(), "yiban-notify-no-such.env"))
    notify._throttle_ts.clear()
    for k in list(os.environ):
        if k.startswith("YIBAN_NOTIFY_"):
            monkeypatch.delenv(k)


def _set(monkeypatch, **kwargs):
    for k, v in kwargs.items():
        monkeypatch.setenv("YIBAN_NOTIFY_" + k, v)


def _enc(secret):
    return json.dumps(account_crypto.encrypt_text(secret, account_crypto.load_key()),
                      ensure_ascii=False)


def _configure_serverchan(monkeypatch, cooldown=None):
    _clear(monkeypatch)
    kw = {"TYPE": "serverchan", "SECRET_ENC": _enc(SCT_KEY)}
    if cooldown is not None:
        kw["COOLDOWN"] = str(cooldown)
    _set(monkeypatch, **kw)


# ---- 配置读取 ----

def test_no_config_disabled(monkeypatch):
    _clear(monkeypatch)
    assert notify.get_secret() == ""
    cfg = notify.get_config()
    assert cfg["enabled"] is False
    assert cfg["configured"] is False


def test_legacy_plain_url_fallback(monkeypatch):
    _clear(monkeypatch)
    _set(monkeypatch, URL="https://example.com/hook")
    assert notify.get_secret() == "https://example.com/hook"
    cfg = notify.get_config()
    assert cfg["enabled"] is True
    assert cfg["type"] == "custom"


def test_encrypted_secret_roundtrip_and_masked(monkeypatch):
    _clear(monkeypatch)
    _set(monkeypatch, TYPE="serverchan", SECRET_ENC=_enc(SCT_KEY))
    assert notify.get_secret() == SCT_KEY
    cfg = notify.get_config()
    assert cfg["enabled"] is True
    assert cfg["type"] == "serverchan"
    assert SCT_KEY not in cfg["secret_masked"], "打码值不得含明文密钥"


def test_bad_encrypted_secret_returns_empty(monkeypatch):
    _clear(monkeypatch)
    _set(monkeypatch, TYPE="serverchan", SECRET_ENC="not-json")
    assert notify.get_secret() == ""
    assert notify.get_config()["enabled"] is False


# ---- Server酱 ----

def test_serverchan_success_format(monkeypatch):
    _configure_serverchan(monkeypatch)
    calls = {}

    class FakeResp:
        def json(self):
            return {"code": 0, "message": "", "data": {}}

    def _post(url, **kw):
        calls["url"] = url
        calls["data"] = kw.get("data")
        return FakeResp()

    monkeypatch.setattr(notify.requests, "post", _post)
    assert notify.send("签到失败", "账号: 138****0001") is True
    assert calls["url"] == f"https://sctapi.ftqq.com/{SCT_KEY}.send"
    assert calls["data"]["title"] == "签到失败"
    assert calls["data"]["desp"] == "账号: 138****0001"


def test_serverchan_title_truncated_and_flattened(monkeypatch):
    _configure_serverchan(monkeypatch)
    data = {}

    class FakeResp:
        def json(self):
            return {"code": 0}

    def _post(url, **kw):
        data.update(kw.get("data"))
        return FakeResp()

    monkeypatch.setattr(notify.requests, "post", _post)
    notify.send("很长的标题" * 20 + "\n换行", "内容")
    assert "\n" not in data["title"]
    assert len(data["title"]) <= 32


def test_serverchan_nonzero_code_fails_and_masks_key(monkeypatch, caplog):
    _configure_serverchan(monkeypatch)

    class FakeResp:
        def json(self):
            return {"code": 429, "message": "超过今日免费额度"}

    monkeypatch.setattr(notify.requests, "post", lambda *a, **k: FakeResp())
    with caplog.at_level(logging.WARNING, logger="notify"):
        assert notify.send("告警", "内容") is False
    assert "429" in caplog.text
    assert SCT_KEY not in caplog.text, "SendKey 不得进日志"


def test_serverchan_raise_fails_silently(monkeypatch, caplog):
    _configure_serverchan(monkeypatch)
    monkeypatch.setattr(notify.requests, "post", mock.Mock(side_effect=RuntimeError("boom")))
    with caplog.at_level(logging.WARNING, logger="notify"):
        assert notify.send("告警", "内容") is False
    assert SCT_KEY not in caplog.text
    assert "boom" not in caplog.text


# ---- 自定义 URL ----

def test_custom_sends_json_and_masks_host(monkeypatch, caplog):
    _clear(monkeypatch)
    _set(monkeypatch, TYPE="custom",
         SECRET_ENC=_enc("https://user:pass@example.com:8443/hook?token=1"))
    calls = {}

    class FakeResp:
        status_code = 500

    def _post(url, **kw):
        calls["url"] = url
        calls["json"] = kw.get("json")
        return FakeResp()

    monkeypatch.setattr(notify.requests, "post", _post)
    with caplog.at_level(logging.WARNING, logger="notify"):
        assert notify.send("告警", "内容") is False
    assert calls["url"] == "https://user:pass@example.com:8443/hook?token=1"
    assert calls["json"] == {"title": "告警", "content": "内容"}
    assert "https://example.com:8443" in caplog.text, "host:port 应可见"
    assert "token=1" not in caplog.text
    assert "user:pass" not in caplog.text


def test_custom_rejects_unsafe_url(monkeypatch):
    _clear(monkeypatch)
    for bad in ("http://example.com/hook",
                "https://127.0.0.1/hook",
                "https://192.168.1.1/hook",
                "https://localhost/hook"):
        _set(monkeypatch, TYPE="custom", SECRET_ENC=_enc(bad))
        assert notify.send("告警", "内容") is False, bad
        _clear(monkeypatch)


def test_is_safe_url():
    assert notify.is_safe_url("https://example.com/hook") is True
    assert notify.is_safe_url("http://example.com/hook") is False
    assert notify.is_safe_url("https://127.0.0.1/hook") is False
    assert notify.is_safe_url("https://192.168.0.1/hook") is False
    assert notify.is_safe_url("https://localhost/hook") is False


# ---- 节流 ----

def test_throttle_same_title_skipped_force_bypasses(monkeypatch):
    _configure_serverchan(monkeypatch, cooldown=60)
    calls = []

    class FakeResp:
        def json(self):
            return {"code": 0}

    monkeypatch.setattr(notify.requests, "post", lambda *a, **k: calls.append(1) or FakeResp())
    assert notify.send("同标题", "1") is True
    assert notify.send("同标题", "2") is False  # 窗口内同标题被节流
    assert notify.send("同标题", "3", force=True) is True  # force 绕过
    assert len(calls) == 2


def test_throttle_zero_disables(monkeypatch):
    _configure_serverchan(monkeypatch, cooldown=0)
    calls = []

    class FakeResp:
        def json(self):
            return {"code": 0}

    monkeypatch.setattr(notify.requests, "post", lambda *a, **k: calls.append(1) or FakeResp())
    assert notify.send("同标题", "1") is True
    assert notify.send("同标题", "2") is True
    assert len(calls) == 2


# ---- 兼容旧明文 URL 走 custom ----

def test_legacy_url_uses_custom_channel(monkeypatch):
    _clear(monkeypatch)
    _set(monkeypatch, URL="https://example.com/hook", COOLDOWN="0")
    calls = []

    class FakeResp:
        status_code = 200

    monkeypatch.setattr(notify.requests, "post", lambda *a, **k: calls.append(k.get("json")) or FakeResp())
    assert notify.send("告警", "内容") is True
    assert calls == [{"title": "告警", "content": "内容"}]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
