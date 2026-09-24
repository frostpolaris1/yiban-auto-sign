# -*- coding: utf-8 -*-
"""`yiban/notify` Webhook 推送组件单元测试（2026-08-29）。

覆盖：
- 配置读取：未配置禁用 / 兼容旧明文 YIBAN_NOTIFY_URL / 加密密文解密回读 /
  密文损坏返回空
- Server酱：URL 与参数格式（title+desp）、标题截断去换行、code==0 成功、
  非零 code 失败并告警、异常静默失败
- 自定义 URL：JSON {title,content}、SSRF 白名单拒绝不安全地址、非 2xx 失败
- 同类型节流：窗口内同标题跳过、force 绕过、cooldown=0 关闭
- 日志脱敏：SendKey / URL token / userinfo 一律不进日志
- 紧急 / 非紧急两本账互不挤占、发送失败退还额度、首次耗尽一次性告知
  （budget_exhausted_today / pop_exhaustion_notice）、跨日两账同时归零、
  跳过原因日志同窗口去重
- 额度"打满当次"即挂耗尽告知（不等下一次被拒）、退还按占用凭证执行
  （跨日凭证作废、发送途中改上限不多退不漏退、虚警撤回）、pop_exhaustion_notice
  返回哪些账本耗尽且每本账每日各一次、get_config 一轮只解析一次 .env
- 虚警判定与告知撤回合并在同一把账本锁内（并发下真实 pending
  不会被陈旧撤回抹掉）；耗尽告知标记与额度计数同属一本账（notify_ledger._*_daily["notice"]）
- get_secret 必须按 YIBAN_ENV_FILE 解析路径取钥，不在 cwd 生成游离密钥
全程 mock requests，不发起真实网络请求。
用法（项目根目录）：
    py -m pytest tests/test_notify_0829.py -v
"""
import contextlib
import importlib.util
import io
import json
import logging
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

import pytest

import web.security as web_security
from yiban.notify import ledger as notify_ledger
from yiban.notify import transport as _notify_K2

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 直连实现包（旧的 scripts/notify.py 兼容壳已删除）：公共面走包，内部名走子模块——
# 打桩必须打在**真正持有并调用该名字**的模块上，否则会静默失效。
from yiban import notify  # noqa: E402
from yiban.infra import account_crypto  # noqa: E402
from yiban.notify import config as notify_config  # noqa: E402
from yiban.notify import transport as notify_transport  # noqa: E402

KEY = "f" * 64
SCT_KEY = "SCT406257TESTTESTTESTTESTTEST"


def _reset_notices():
    """清空两本账的耗尽告知标记（进程内状态，用例之间必须互不串）。

    pending/notified/warned 从全局 notify._exhaustion 挪进了
    各本账自己的 notice 子字典（与 count 同一把账本锁），复位口径随之逐本清。
    """
    for ledger in (notify_ledger._general_daily, notify_ledger._urgent_daily):
        ledger["notice"].update({"pending": False, "notified": False, "warned": False})


def _clear(monkeypatch):
    """隔离环境：固定加密密钥 + 隔离 .env（不读项目根 .env）+ 清空 YIBAN_NOTIFY_*。

    额外隔离账本目录（YIBAN_STATE_DIR 指向临时目录）并清掉旧账本
    文件——每日预算改为磁盘持久化后，跨用例残留的 notify-ledger.json（旧测试
    写入的当日计数）会让计数从非零起步，send 误判额度耗尽。每用例独立 tmpdir。
    """
    import tempfile as _tf
    _tmp_ledger = _tf.mkdtemp(prefix="yiban-notify-ledger-")
    monkeypatch.setenv("YIBAN_STATE_DIR", _tmp_ledger)
    monkeypatch.setenv("YIBAN_ACCOUNTS_KEY", KEY)
    # 指向不存在的 .env，避免 notify 回退读取项目根 .env（含真实 Server酱 配置）
    monkeypatch.setenv("YIBAN_ENV_FILE",
                       os.path.join(tempfile.gettempdir(), "yiban-notify-no-such.env"))
    notify_ledger._throttle_ts.clear()
    # 重置进程内状态：两本账、耗尽告知标记、跳过日志去重表
    notify_ledger._general_daily["state"].update({"date": "", "count": 0})
    notify_ledger._urgent_daily["state"].update({"date": "", "count": 0})
    _reset_notices()
    notify_ledger._skip_logged.clear()
    for k in list(os.environ):
        if k.startswith("YIBAN_NOTIFY_"):
            monkeypatch.delenv(k)


def _freeze_day(monkeypatch, day):
    """冻结/切换"今天"：patch notify 自己的当日日期来源。

    不去动 stdlib time.strftime——那是全进程共享对象，patch 它会影响其它模块甚至
    其它线程的用例，跨日语义只要 _daily_today 稳定返回目标日期即可。
    """
    monkeypatch.setattr(notify_ledger, "_daily_today", lambda: day[0])
    return day


def _set(monkeypatch, **kwargs):
    for k, v in kwargs.items():
        monkeypatch.setenv("YIBAN_NOTIFY_" + k, v)


def _enc(secret):
    return json.dumps(account_crypto.encrypt_text(secret, account_crypto.load_key()),
                      ensure_ascii=False)


def _enc_with(key_hex, secret):
    """用指定密钥加密（走 .env 取钥路径的用例不能用进程内 load_key）。"""
    return json.dumps(account_crypto.encrypt_text(secret, account_crypto._decode_key(key_hex)),
                      ensure_ascii=False)


class _Resp:
    def __init__(self, code=0, status=200):
        self._code = code
        self.status_code = status

    def json(self):
        return {"code": self._code, "message": "quota" if self._code else ""}


def _ok_post(monkeypatch, calls):
    """成功送达的 requests.post 替身：记录标题，返回 Server酱 code=0。"""
    def _post(url, **kw):
        calls.append(kw["data"]["title"])
        return _Resp(0)

    monkeypatch.setattr(notify_transport.requests, "post", _post)


def _rejected_post(monkeypatch, calls):
    """服务端拒绝（code!=0）的替身：请求发出去了，但手机没收到。"""
    def _post(url, **kw):
        calls.append(kw["data"]["title"])
        return _Resp(429)

    monkeypatch.setattr(notify_transport.requests, "post", _post)


def _info_lines(caplog, needle):
    """统计 notify 的 INFO 级日志行数（用于验证跳过日志的同窗口去重）。"""
    return sum(1 for r in caplog.records
               if r.levelno == logging.INFO and needle in r.getMessage())


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

    monkeypatch.setattr(notify_transport.requests, "post", _post)
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

    monkeypatch.setattr(notify_transport.requests, "post", _post)
    notify.send("很长的标题" * 20 + "\n换行", "内容")
    assert "\n" not in data["title"]
    assert len(data["title"]) <= 32


def test_serverchan_nonzero_code_fails_and_masks_key(monkeypatch, caplog):
    _configure_serverchan(monkeypatch)

    class FakeResp:
        def json(self):
            return {"code": 429, "message": "超过今日免费额度"}

    monkeypatch.setattr(notify_transport.requests, "post", lambda *a, **k: FakeResp())
    with caplog.at_level(logging.WARNING, logger="notify"):
        assert notify.send("告警", "内容") is False
    assert "429" in caplog.text
    assert SCT_KEY not in caplog.text, "SendKey 不得进日志"


def test_serverchan_raise_fails_silently(monkeypatch, caplog):
    _configure_serverchan(monkeypatch)
    monkeypatch.setattr(notify_transport.requests, "post", mock.Mock(side_effect=RuntimeError("boom")))
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

    monkeypatch.setattr(notify_transport.requests, "post", _post)
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


def test_is_safe_url_rejects_loopback_variants():
    """Low-1：非 IP 字面量的回环域名（纯数字/0x/前导零/短式/尾点）。

    实测 `https://2130706433/hook`、`https://0x7f000001/hook`、`https://0177.0.0.1/hook`、
    `https://127.1/hook`、`https://localhost./hook` 全部直通（原实现非 IP 域名一律
    放行）。`[::ffff:127.0.0.1]`/`[::1]`/`169.254.169.254` 已拦住（ipaddress 可解析）。
    改法 (a)：静态层拦 IP 字面量变体；`*.nip.io` 类域名型重绑定需连接期 DNS 复检
    （选项 b，引入 DNS 依赖与 TOCTOU 残余，本批按工单建议不做）。
    """
    bad = [
        "https://2130706433/hook",              # 十进制 IP 字面量（127.0.0.1）
        "https://0x7f000001/hook",              # 十六进制
        "https://0177.0.0.1/hook",              # 前导零八进制
        "https://127.1/hook",                   # 短式回环
        "https://localhost./hook",              # 尾点后缀
        "https://0.0.0.0/hook",                 # 未指定
    ]
    for url in bad:
        assert notify.is_safe_url(url) is False, url
    # 合法域名不受影响
    assert notify.is_safe_url("https://example.com/hook") is True


# ---- 节流 ----

def test_throttle_same_title_skipped_force_bypasses(monkeypatch):
    _configure_serverchan(monkeypatch, cooldown=60)
    calls = []

    class FakeResp:
        def json(self):
            return {"code": 0}

    monkeypatch.setattr(notify_transport.requests, "post", lambda *a, **k: calls.append(1) or FakeResp())
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

    monkeypatch.setattr(notify_transport.requests, "post", lambda *a, **k: calls.append(1) or FakeResp())
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

    monkeypatch.setattr(notify_transport.requests, "post", lambda *a, **k: calls.append(k.get("json")) or FakeResp())
    assert notify.send("告警", "内容") is True
    assert calls == [{"title": "告警", "content": "内容"}]


# ---- 仅重要告警 ----

def test_urgent_only_skips_non_urgent(monkeypatch):
    _configure_serverchan(monkeypatch)
    _set(monkeypatch, URGENT_ONLY="1")
    calls = []

    class FakeResp:
        def json(self):
            return {"code": 0}

    monkeypatch.setattr(notify_transport.requests, "post", lambda *a, **k: calls.append(1) or FakeResp())
    assert notify.send("用户日常改密", "内容") is False          # 非紧急跳过
    assert notify.send("用户日常改密", "内容", urgent=False) is False
    assert notify.send("高危操作告警", "内容", urgent=True) is True  # 紧急放行
    assert len(calls) == 1


def test_urgent_only_off_pushes_all(monkeypatch):
    _configure_serverchan(monkeypatch, cooldown=0)
    calls = []

    class FakeResp:
        def json(self):
            return {"code": 0}

    monkeypatch.setattr(notify_transport.requests, "post", lambda *a, **k: calls.append(1) or FakeResp())
    assert notify.send("用户日常改密", "内容") is True   # 未开启时不区分紧急
    assert notify.send("用户日常改密", "内容", urgent=True) is True
    assert len(calls) == 2


def test_urgent_only_force_bypasses(monkeypatch):
    _configure_serverchan(monkeypatch)
    _set(monkeypatch, URGENT_ONLY="1")
    calls = []

    class FakeResp:
        def json(self):
            return {"code": 0}

    monkeypatch.setattr(notify_transport.requests, "post", lambda *a, **k: calls.append(1) or FakeResp())
    assert notify.send("测试", "内容", force=True) is True  # 测试推送不受紧急过滤
    assert len(calls) == 1


# ---- 每日预算 ----

def test_daily_budget_stops_after_limit(monkeypatch):
    _configure_serverchan(monkeypatch, cooldown=0)
    _set(monkeypatch, DAILY_MAX="3")
    calls = []

    class FakeResp:
        def json(self):
            return {"code": 0}

    monkeypatch.setattr(notify_transport.requests, "post", lambda *a, **k: calls.append(1) or FakeResp())
    for i in range(3):
        assert notify.send(f"告警{i}", "内容") is True
    assert notify.send("告警3", "内容") is False  # 预算耗尽
    assert len(calls) == 3
    assert notify.get_config()["daily_remaining"] == 0


def test_daily_budget_force_bypasses(monkeypatch):
    _configure_serverchan(monkeypatch, cooldown=0)
    _set(monkeypatch, DAILY_MAX="1")
    calls = []

    class FakeResp:
        def json(self):
            return {"code": 0}

    monkeypatch.setattr(notify_transport.requests, "post", lambda *a, **k: calls.append(1) or FakeResp())
    assert notify.send("告警1", "内容") is True
    assert notify.send("告警2", "内容") is False
    assert notify.send("测试", "内容", force=True) is True  # force 绕过预算
    assert len(calls) == 2


def test_daily_budget_zero_unlimited(monkeypatch):
    _configure_serverchan(monkeypatch, cooldown=0)
    _set(monkeypatch, DAILY_MAX="0")
    calls = []

    class FakeResp:
        def json(self):
            return {"code": 0}

    monkeypatch.setattr(notify_transport.requests, "post", lambda *a, **k: calls.append(1) or FakeResp())
    for i in range(7):
        assert notify.send(f"告警{i}", "内容") is True
    assert len(calls) == 7
    assert notify.get_config()["daily_remaining"] is None


def test_get_config_exposes_urgent_and_daily(monkeypatch):
    _configure_serverchan(monkeypatch)
    _set(monkeypatch, URGENT_ONLY="1", DAILY_MAX="8")
    cfg = notify.get_config()
    assert cfg["urgent_only"] is True
    assert cfg["daily_max"] == 8
    assert cfg["daily_remaining"] == 8


def test_get_config_parses_env_file_once(monkeypatch):
    """修复轮⑤：一轮 get_config 只解析一次 .env（原先每个键各自读一遍全文件 + 一遍解析）。"""
    _configure_serverchan(monkeypatch)
    _set(monkeypatch, DAILY_MAX="4", URGENT_DAILY_MAX="2", COOLDOWN="15")
    real = notify_config._read_env_file
    reads = []

    def _counting():
        reads.append(1)
        return real()

    monkeypatch.setattr(notify_config, "_read_env_file", _counting)
    cfg = notify.get_config()
    assert (cfg["daily_max"], cfg["urgent_daily_max"], cfg["cooldown"]) == (4, 2, 15)
    assert len(reads) == 1, f".env 被重复解析了 {len(reads)} 次"


# ---- 额度分两本账 ----

def test_urgent_exhausted_does_not_block_general(monkeypatch):
    """紧急账打满不影响非紧急可达：两本账各自独立计数。"""
    _configure_serverchan(monkeypatch, cooldown=0)
    _set(monkeypatch, DAILY_MAX="5", URGENT_DAILY_MAX="2")
    calls = []
    _ok_post(monkeypatch, calls)
    assert notify.send("紧急1", "内容", urgent=True) is True
    assert notify.send("紧急2", "内容", urgent=True) is True
    assert notify.send("紧急3", "内容", urgent=True) is False  # 紧急账耗尽
    assert notify.budget_exhausted_today(True) is True
    assert notify.budget_exhausted_today(False) is False
    for i in range(5):
        assert notify.send(f"普通{i}", "内容") is True            # 非紧急一路照发
    assert len(calls) == 7
    assert notify.get_config()["urgent_daily_remaining"] == 0
    assert notify.get_config()["daily_remaining"] == 0


def test_general_exhausted_does_not_block_urgent(monkeypatch):
    """非紧急账打满不影响紧急告警——正是 P2-1 的攻击场景（噪声烧额度后真告警仍可达）。"""
    _configure_serverchan(monkeypatch, cooldown=0)
    _set(monkeypatch, DAILY_MAX="2", URGENT_DAILY_MAX="3")
    calls = []
    _ok_post(monkeypatch, calls)
    assert notify.send("普通1", "内容") is True
    assert notify.send("普通2", "内容") is True
    assert notify.send("普通3", "内容") is False                 # 非紧急账耗尽
    assert notify.budget_exhausted_today(False) is True
    assert notify.send("审计链异常", "内容", urgent=True) is True   # 紧急仍可达手机
    assert notify.get_config()["urgent_daily_remaining"] == 2
    assert len(calls) == 3


def test_urgent_budget_zero_unlimited(monkeypatch):
    """紧急账 0=不限；设 0 时该账永不判耗尽。"""
    _configure_serverchan(monkeypatch, cooldown=0)
    _set(monkeypatch, DAILY_MAX="1", URGENT_DAILY_MAX="0")
    calls = []
    _ok_post(monkeypatch, calls)
    for i in range(6):
        assert notify.send(f"紧急{i}", "内容", urgent=True) is True
    assert notify.send("普通", "内容") is True
    assert notify.send("普通2", "内容") is False                 # 非紧急账仍受限
    assert notify.get_config()["urgent_daily_remaining"] is None
    assert notify.budget_exhausted_today(True) is False
    assert notify.budget_exhausted_today(False) is True
    assert notify.budget_exhausted_today() is True               # None = 任一账耗尽
    assert len(calls) == 7


def test_cross_day_resets_both_ledgers(monkeypatch):
    """跨日两本账同时归零、耗尽告知标记同步重置。"""
    _configure_serverchan(monkeypatch, cooldown=0)
    _set(monkeypatch, DAILY_MAX="1", URGENT_DAILY_MAX="1")
    day = _freeze_day(monkeypatch, ["2026-08-29"])
    calls = []
    _ok_post(monkeypatch, calls)
    assert notify.send("普通1", "内容") is True
    assert notify.send("紧急1", "内容", urgent=True) is True
    assert notify.send("普通2", "内容") is False
    assert notify.send("紧急2", "内容", urgent=True) is False
    assert notify.budget_exhausted_today() is True
    # 一次调用就把当日所有耗尽的账本取走（供一封邮件写清两行）
    assert notify.pop_exhaustion_notice() == ["general", "urgent"]
    day[0] = "2026-08-30"
    assert notify.budget_exhausted_today() is False
    assert notify.pop_exhaustion_notice() == []  # 昨日未取走的标记不补发
    assert notify.send("普通3", "内容") is True
    assert notify.send("紧急3", "内容", urgent=True) is True
    assert len(calls) == 4


# ---- 失败退还 ----

def test_send_rejected_refunds_budget(monkeypatch):
    """服务端拒绝（code!=0）不扣额度，退还后仍可再试（旧口径失败照扣=可被烧额度）。"""
    _configure_serverchan(monkeypatch, cooldown=0)
    _set(monkeypatch, DAILY_MAX="1")
    calls = []
    _rejected_post(monkeypatch, calls)
    assert notify.send("告警", "内容") is False
    assert notify.get_config()["daily_remaining"] == 1
    assert notify.budget_exhausted_today() is False
    assert notify.pop_exhaustion_notice() == []                  # 失败不算耗尽：虚警要撤回
    _ok_post(monkeypatch, calls)
    assert notify.send("告警", "内容") is True
    assert notify.get_config()["daily_remaining"] == 0
    assert len(calls) == 2


def test_send_exception_refunds_urgent_budget(monkeypatch):
    """HTTP 异常同样退还，且只退所属那本账。"""
    _configure_serverchan(monkeypatch, cooldown=0)
    _set(monkeypatch, DAILY_MAX="1", URGENT_DAILY_MAX="1")
    calls = []
    _ok_post(monkeypatch, calls)
    assert notify.send("普通1", "内容") is True                  # 非紧急账占满
    monkeypatch.setattr(notify_transport.requests, "post", mock.Mock(side_effect=RuntimeError("boom")))
    assert notify.send("紧急1", "内容", urgent=True) is False
    assert notify.get_config()["urgent_daily_remaining"] == 1
    assert notify.get_config()["daily_remaining"] == 0           # 不得误退非紧急账
    _ok_post(monkeypatch, calls)
    assert notify.send("紧急1", "内容", urgent=True) is True
    assert notify.get_config()["urgent_daily_remaining"] == 0


def test_non_json_response_refunds_budget(monkeypatch):
    """修复轮⑥补漏：Server酱 返回非 JSON（网关页/限流页常见）判失败并退还额度。"""
    _configure_serverchan(monkeypatch, cooldown=0)
    _set(monkeypatch, DAILY_MAX="1")

    class _BrokenResp:
        status_code = 200

        def json(self):
            raise ValueError("Expecting value")

    calls = []
    monkeypatch.setattr(notify_transport.requests,
                        "post",
                        lambda *a, **k: calls.append(k["data"]["title"]) or _BrokenResp())
    assert notify.send("告警", "内容") is False
    assert calls == ["告警"]                                     # 请求确实发出去了但没送达
    assert notify.get_config()["daily_remaining"] == 1
    assert notify.pop_exhaustion_notice() == []                  # 占满又退回，不算耗尽


def test_unknown_type_refunds_budget(monkeypatch):
    """修复轮⑥补漏：未知 type（配置漂移）判失败并退还，且零外发。"""
    _configure_serverchan(monkeypatch, cooldown=0)
    _set(monkeypatch, DAILY_MAX="1", TYPE="telegram")
    calls = []
    monkeypatch.setattr(notify_transport.requests, "post", lambda *a, **k: calls.append(1) or _Resp(0))
    assert notify.send("告警", "内容") is False
    assert calls == [], "未知类型不该有外发"
    assert notify.get_config()["daily_remaining"] == 1
    assert notify.pop_exhaustion_notice() == []


def test_unsafe_url_rejection_refunds_budget(monkeypatch):
    """SSRF 白名单拒发（请求根本没发出）也退还额度。"""
    _clear(monkeypatch)
    _set(monkeypatch, TYPE="custom", DAILY_MAX="2",
         SECRET_ENC=_enc("http://example.com/hook"))
    calls = []
    monkeypatch.setattr(notify_transport.requests, "post",
                        lambda *a, **k: calls.append(1) or _Resp(0, 200))
    assert notify.send("告警", "内容") is False
    assert calls == []
    assert notify.get_config()["daily_remaining"] == 2


def test_refund_across_day_does_not_charge_next_day(monkeypatch):
    """修复轮②：23:59:59 占用、次日才失败退还时，凭证作废——不得凭空少次日一条额度。

    必须先让次日账上已经消耗过一条：跨日归零会把 count 抹平，此时"多退一次"只在
    次日已有占用时才显形（否则退到 0 就被 count<=0 兜住，测不出差异）。
    """
    _configure_serverchan(monkeypatch, cooldown=0)
    _set(monkeypatch, DAILY_MAX="2")
    day = _freeze_day(monkeypatch, ["2026-08-29"])
    stale = notify_ledger._consume_daily_budget("general")             # 昨天的占用，请求还没回来
    assert stale.allowed and stale.ledger == "general" and stale.day == "2026-08-29"
    day[0] = "2026-08-30"                                       # 跨日：次日账本归零重来
    calls = []
    _ok_post(monkeypatch, calls)
    assert notify.send("次日1", "内容") is True                   # 今天已实打实发出 1 条
    assert notify.get_config()["daily_remaining"] == 1
    notify_ledger._refund_daily_budget(stale)                          # 昨天的失败此刻才退还
    assert notify.get_config()["daily_remaining"] == 1, "跨日退还不得扣到次日账上"
    assert notify.send("次日2", "内容") is True                   # 次日仍是完整的 2 条额度
    assert notify.send("次日3", "内容") is False
    assert len(calls) == 2


def test_refund_kept_when_limit_switched_to_unlimited(monkeypatch):
    """修复轮③ 反向：占用时有限额、退还时管理员已改成不限额，旧凭证仍要照退（不得漏退）。"""
    _configure_serverchan(monkeypatch, cooldown=0)
    _set(monkeypatch, DAILY_MAX="2")
    ticket = notify_ledger._consume_daily_budget("general")
    assert ticket.ledger == "general" and ticket.day == notify_ledger._daily_today()
    assert notify_ledger._general_daily["state"]["count"] == 1
    monkeypatch.setenv("YIBAN_NOTIFY_DAILY_MAX", "0")            # 发送途中改成不限
    notify_ledger._refund_daily_budget(ticket)
    assert notify_ledger._general_daily["state"]["count"] == 0, \
        "退还只认凭证：不限额是「现在」的状态，不能据此认定当初没占"


def test_refund_ticket_is_one_shot(monkeypatch):
    """凭证一次性：同一笔占用退两次只退一次，否则等于白送一条额度。"""
    _configure_serverchan(monkeypatch, cooldown=0)
    _set(monkeypatch, DAILY_MAX="2")
    ticket = notify_ledger._consume_daily_budget("general")                 # 占 1 条
    assert notify_ledger._consume_daily_budget("general").allowed is True   # 占满 2/2
    assert notify_ledger._general_daily["state"]["count"] == 2
    notify_ledger._refund_daily_budget(ticket)
    notify_ledger._refund_daily_budget(ticket)                              # 第二次须被忽略
    assert notify_ledger._general_daily["state"]["count"] == 1


def test_no_phantom_refund_when_limit_raised_mid_send(monkeypatch):
    """修复轮③ 正向：占用时就是不限额（没占额度），失败退还不得把账退成"凭空多一条"。"""
    _configure_serverchan(monkeypatch, cooldown=0)
    _set(monkeypatch, DAILY_MAX="1")
    calls = []
    _ok_post(monkeypatch, calls)
    assert notify.send("普通1", "内容") is True                  # 唯一的额度用掉
    monkeypatch.setenv("YIBAN_NOTIFY_DAILY_MAX", "0")            # 管理员中途放开
    monkeypatch.setattr(notify_transport.requests, "post", mock.Mock(side_effect=RuntimeError("boom")))
    assert notify.send("普通2", "内容") is False                 # 不限额：本次没占额度
    assert notify_ledger._general_daily["state"]["count"] == 1, "没占额度就不该退，退了就是幻影退还"
    monkeypatch.setenv("YIBAN_NOTIFY_DAILY_MAX", "1")            # 再改回来
    assert notify.get_config()["daily_remaining"] == 0, "幻影退还等于白送一条额度"


class _InterferingLock:
    """替身账本锁：在"最外层临界区刚结束"的那一刻插入一次竞争线程的动作。

    为什么不用真线程：真线程只能靠 sleep 赌时序，红绿不稳定；而这里要防的恰好是一个
    **确定的交错**——"甲判定虚警之后、撤回告知之前，乙已经跑完 _consume + _mark_exhausted"。
    把乙的动作挂在账本锁的释放点上，它在旧实现里正落在那道窄窗中间；在修复后的实现里
    释放点已在撤回之后，交错依旧合法、必须不出问题。两种形态都无需运气。
    """

    def __init__(self, hook):
        self._hook = hook
        self._depth = 0
        self._fired = False

    def __enter__(self):
        self._depth += 1
        return self

    def __exit__(self, exc_type, exc, tb):
        self._depth -= 1
        if self._depth == 0 and not self._fired:
            self._fired = True  # 只插入一次：钩子内部再取本锁不会递归触发
            self._hook()
        return False


def test_refund_does_not_clobber_concurrent_exhaustion_notice(monkeypatch):
    """修复轮2 竞态：虚警判定与撤回之间不得留窗口，让陈旧撤回抹掉真实的耗尽告知。

    场景（DAILY_MAX=2）：
      甲占 1 条、乙占满第 2 条 → 账本打满并挂上 pending；
      甲发送失败退还 → 退完 count=1 < 2，甲据此判定"那次耗尽是虚警"；
      就在甲判定之后、撤回之前，另一个线程把退还出来的那条又占掉 → 账本**确实**再次打满，
      并挂上真实 pending；
      若甲仍按陈旧判定去 discard，这条真实 pending 就没了：warned 不撤销、已 notified
      不再重挂，当日再无被拒尝试 → "额度已用尽"永远发不出去，正是本档要治的告知静默。
    """
    _configure_serverchan(monkeypatch, cooldown=0)
    _set(monkeypatch, DAILY_MAX="2")
    mine = notify_ledger._consume_daily_budget("general")                 # 甲：占 1 条
    rival = notify_ledger._consume_daily_budget("general")                # 乙：占满 2/2
    assert mine.allowed and rival.allowed
    assert notify.budget_exhausted_today(False) is True
    assert notify_ledger._general_daily["notice"]["pending"] is True      # 告知还没被取走

    def _other_thread():
        """竞争线程：把甲刚退出来的那条重新占掉 → 账本再次真实打满并挂标记。"""
        filled = notify_ledger._consume_daily_budget("general")
        assert filled.allowed is True, "前置条件失败：此刻账上应还剩一条可占"
        assert notify_ledger._general_daily["state"]["count"] == 2

    real_lock = notify_ledger._general_daily["lock"]
    notify_ledger._general_daily["lock"] = _InterferingLock(_other_thread)
    try:
        notify_ledger._refund_daily_budget(mine)                          # 甲的失败退还在此发生
    finally:
        notify_ledger._general_daily["lock"] = real_lock

    assert notify_ledger._general_daily["state"]["count"] == 2, "竞争线程占的那条不该被甲退掉"
    assert notify.budget_exhausted_today(False) is True            # 账本确实仍打满
    assert notify.pop_exhaustion_notice() == ["general"], \
        "真实耗尽的告知被一次陈旧的虚警撤回抹掉了（判定与撤回没在同一把锁内完成）"
    assert notify.pop_exhaustion_notice() == []                    # 仍然每本账每日各一次


def test_force_send_does_not_consume_or_refund(monkeypatch):
    """force（测试推送）不占额度，失败也不会把别人的额度退成负数。"""
    _configure_serverchan(monkeypatch, cooldown=0)
    _set(monkeypatch, DAILY_MAX="2")
    calls = []
    _rejected_post(monkeypatch, calls)
    assert notify.send("测试", "内容", force=True) is False
    assert notify.send("测试2", "内容", force=True, urgent=True) is False
    assert notify.get_config()["daily_remaining"] == 2
    assert notify.get_config()["urgent_daily_remaining"] == 3
    assert notify_ledger._general_daily["state"]["count"] == 0


# ---- 首次耗尽一次性告知 ----

def test_last_message_filling_budget_still_notifies(monkeypatch, caplog):
    """修复轮①：最后一条恰好打满、之后不再有新的 send 调用时，告知仍必须能取到。

    旧实现只在"下一次尝试被拒"时才补标记——当日再无新告警就等于静默，
    而这正是要治的病。
    """
    _configure_serverchan(monkeypatch, cooldown=0)
    _set(monkeypatch, DAILY_MAX="2")
    calls = []
    _ok_post(monkeypatch, calls)
    assert notify.send("告警1", "内容") is True
    with caplog.at_level(logging.WARNING, logger="notify"):
        assert notify.send("告警2", "内容") is True             # 正好用光，之后再不发
    assert notify.budget_exhausted_today(False) is True
    assert "额度已用尽" in caplog.text and "非紧急" in caplog.text
    assert notify.pop_exhaustion_notice() == ["general"]
    assert notify.pop_exhaustion_notice() == []                 # 每本账每日各一次
    assert len(calls) == 2


def test_urgent_last_message_filling_budget_still_notifies(monkeypatch):
    """修复轮①：紧急账同理——打满即挂标记，不等下一次被拒。"""
    _configure_serverchan(monkeypatch, cooldown=0)
    _set(monkeypatch, URGENT_DAILY_MAX="1")
    calls = []
    _ok_post(monkeypatch, calls)
    assert notify.send("审计链异常", "内容", urgent=True) is True
    assert notify.budget_exhausted_today(True) is True
    assert notify.pop_exhaustion_notice() == ["urgent"]
    assert notify.pop_exhaustion_notice() == []


def test_first_exhaustion_notices_only_once(monkeypatch, caplog):
    """某本账当日首次耗尽记 warning 并置标记；pop 读后置假，二次耗尽不重复置。"""
    _configure_serverchan(monkeypatch, cooldown=0)
    _set(monkeypatch, DAILY_MAX="1")
    calls = []
    _ok_post(monkeypatch, calls)
    assert notify.send("普通1", "内容") is True
    with caplog.at_level(logging.WARNING, logger="notify"):
        assert notify.send("普通2", "内容") is False
    assert "额度已用尽" in caplog.text and "非紧急" in caplog.text
    assert notify.pop_exhaustion_notice() == ["general"]
    assert notify.pop_exhaustion_notice() == []
    assert notify.send("普通3", "内容") is False
    assert notify.pop_exhaustion_notice() == []                 # 当日只告知一次
    assert len(calls) == 1


def test_each_ledger_notices_once_per_day(monkeypatch):
    """修复轮④：告知是"每本账每日各一次"（一天两封的风险由调用方一次取全列表规避）。"""
    _configure_serverchan(monkeypatch, cooldown=0)
    _set(monkeypatch, DAILY_MAX="1", URGENT_DAILY_MAX="1")
    calls = []
    _ok_post(monkeypatch, calls)
    assert notify.pop_exhaustion_notice() == []                 # 未耗尽：falsy（向后兼容）
    assert notify.send("普通1", "内容") is True
    assert notify.send("普通2", "内容") is False
    assert notify.pop_exhaustion_notice() == ["general"]
    assert notify.send("紧急1", "内容", urgent=True) is True
    assert notify.send("紧急2", "内容", urgent=True) is False
    assert notify.pop_exhaustion_notice() == ["urgent"]
    assert not notify.pop_exhaustion_notice()                   # 两本账各自已告知过
    assert notify.pop_exhaustion_notice() == []


# ---- 跳过原因可见（同窗口去重） ----

def test_skip_reason_logs_deduped_per_window(monkeypatch, caplog):
    """非紧急过滤 / 节流命中 / 额度耗尽各一行 info，且同原因窗口内只记一行。"""
    _configure_serverchan(monkeypatch, cooldown=60)
    _set(monkeypatch, URGENT_ONLY="1", DAILY_MAX="1", URGENT_DAILY_MAX="3")
    calls = []
    _ok_post(monkeypatch, calls)
    with caplog.at_level(logging.INFO, logger="notify"):
        for i in range(3):
            assert notify.send(f"用户改密{i}", "内容") is False
        assert _info_lines(caplog, "URGENT_ONLY") == 1            # 只记一行原因
        notify_ledger._skip_logged.clear()
        assert notify.send("高危操作", "内容", urgent=True) is True
        assert notify.send("高危操作", "内容", urgent=True) is False
        assert _info_lines(caplog, "节流") == 1
        assert notify.get_config()["urgent_daily_remaining"] == 2  # 节流不扣额度
        notify_ledger._skip_logged.clear()
        monkeypatch.delenv("YIBAN_NOTIFY_URGENT_ONLY")
        assert notify.send("普通1", "内容") is True
        assert notify.send("普通2", "内容") is False
        assert notify.send("普通3", "内容") is False
        assert _info_lines(caplog, "已用尽") == 1


def test_cooldown_explicit_zero_from_env_file_disables_throttle(monkeypatch, tmp_path):
    """生产口径回归：.env 里显式 YIBAN_NOTIFY_COOLDOWN=0 必须真的关闭节流（而非回落 60）。

    同时覆盖 get_secret 走 YIBAN_ENV_FILE 路径取钥（不依赖 cwd）。
    """
    key = "b" * 64
    env = tmp_path / "prod-like.env"
    env.write_text(
        "YIBAN_ACCOUNTS_KEY={k}\nYIBAN_NOTIFY_TYPE=serverchan\n"
        "YIBAN_NOTIFY_SECRET_ENC={enc}\nYIBAN_NOTIFY_COOLDOWN=0\nYIBAN_NOTIFY_DAILY_MAX=0\n".format(
            k=key, enc=_enc_with(key, SCT_KEY)),
        encoding="utf-8")
    notify_ledger._throttle_ts.clear()
    notify_ledger._skip_logged.clear()
    notify_ledger._general_daily["state"].update({"date": "", "count": 0})
    notify_ledger._urgent_daily["state"].update({"date": "", "count": 0})
    _reset_notices()
    for k in list(os.environ):
        if k.startswith("YIBAN_NOTIFY_") or k == "YIBAN_ACCOUNTS_KEY":
            monkeypatch.delenv(k)
    monkeypatch.setenv("YIBAN_ENV_FILE", str(env))
    monkeypatch.setattr(account_crypto, "_KEY_CACHE", None)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(exist_ok=True)
    monkeypatch.chdir(str(elsewhere))  # 故意让 cwd 与 .env 不同目录
    calls = []
    _ok_post(monkeypatch, calls)
    assert notify.get_secret() == SCT_KEY, "密钥必须按 YIBAN_ENV_FILE 指向的文件解出"
    assert notify.get_config()["cooldown"] == 0
    assert notify.send("同标题", "1") is True
    assert notify.send("同标题", "2") is True
    assert len(calls) == 2
    assert not (elsewhere / ".env").exists(), "不得在 cwd 生成游离密钥文件"


# ---- get_secret 的密钥来源与不抛异常契约 ----

def test_get_secret_reads_env_file_not_cwd(monkeypatch, tmp_path):
    """密文与密钥都在 YIBAN_ENV_FILE 指的文件里：解钥必须读同一个文件，不碰 cwd。"""
    key = "c" * 64
    env = tmp_path / "remote.env"
    env.write_text("YIBAN_ACCOUNTS_KEY={k}\nYIBAN_NOTIFY_TYPE=serverchan\n"
                   "YIBAN_NOTIFY_SECRET_ENC={enc}\n".format(k=key, enc=_enc_with(key, SCT_KEY)),
                   encoding="utf-8")
    work = tmp_path / "cwd"
    work.mkdir()
    monkeypatch.delenv("YIBAN_ACCOUNTS_KEY", raising=False)
    monkeypatch.setenv("YIBAN_ENV_FILE", str(env))
    monkeypatch.setattr(account_crypto, "_KEY_CACHE", None)
    monkeypatch.chdir(str(work))
    assert notify.get_secret() == SCT_KEY
    assert not (work / ".env").exists(), "load_key 不得回落到 cwd/.env 并就地生成游离密钥"


def test_get_secret_swallows_key_file_read_error(monkeypatch, caplog):
    """解钥遇 OSError（密钥文件存在但读不到）仍返回空并 warning，绝不外抛。

    旧断言只有 send() is False：密钥为空时它本就成立，换成"额度耗尽""节流命中"
    甚至"类型写错"都照样绿，等于没测。这里把因由钉死——因"解不出密钥"而不外发、
    不占额度、配置页显示未启用。
    """
    _clear(monkeypatch)
    _set(monkeypatch, TYPE="serverchan", DAILY_MAX="3", SECRET_ENC=_enc(SCT_KEY))
    post = mock.Mock(return_value=_Resp(0))
    monkeypatch.setattr(notify_transport.requests, "post", post)
    monkeypatch.setattr(account_crypto, "load_key", mock.Mock(side_effect=OSError("EACCES")))
    with caplog.at_level(logging.WARNING, logger="notify"):
        assert notify.get_secret() == ""
        assert notify.budget_exhausted_today() is False           # 排除"因耗尽而 False"
        assert notify.send("告警", "内容") is False
    assert "解密失败" in caplog.text
    assert post.call_count == 0, "解不出密钥却仍发起了外发请求"
    assert notify.get_config()["daily_remaining"] == 3, "取钥失败不该消耗额度"
    assert notify.get_config()["enabled"] is False, "设置页应如实显示通道不可用"


TEST_KEY = "a" * 64


ADMIN_PASS = "MasterPass#2026"


SCT_KEY_K2 = "SCT406257TESTTESTTESTTEST"


class _RecordingProc:
    """fake Popen 返回值：记录 wait(timeout=...) 收到的超时值（M5 断言用）。"""

    def __init__(self, recorded):
        self._recorded = recorded
        self.returncode = 0

    def wait(self, timeout=None):
        self._recorded.append(timeout)
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass


class Batch18Knife2WebTest(unittest.TestCase):
    """web/app.py 侧：M4 冷却单源 / M5 上限与超时 / M8 接线 / P3-1 / P3-6。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-knife2-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls._env_content = (
            f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
            f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
        )
        with io.open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(cls._env_content)
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls.users_file = os.path.join(cls.tmp, "users.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_USERS_FILE"] = cls.users_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        global db
        import db
        spec = importlib.util.spec_from_file_location(
            "webapp", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)
        # Popen 类级 patch（口径沿用 tests/test_sign_round_guards.py 的 BatchSignCooldownTest）：
        # 覆盖后台队列线程的任意调度时刻，防真实 spawn signin 子进程。
        cls.wait_timeouts = []  # _RecordingProc 记录的 wait(timeout=...) 值
        cls._popen_patch = mock.patch.object(
            cls.webapp.subprocess, "Popen",
            side_effect=lambda *a, **k: _RecordingProc(cls.wait_timeouts))
        cls._popen_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls._popen_patch.stop()
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE",
                  "YIBAN_USERS_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR"):
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
        self._set_cooldown(None)
        self.wait_timeouts.clear()

    def tearDown(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None

    # ---- 辅助 ----
    def _set_cooldown(self, value):
        """写/删 .env 的冷却键（None=删键回默认 1800s）。"""
        with open(self.env_file, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
        lines = [ln for ln in lines
                 if not ln.strip().startswith("YIBAN_BATCH_SIGN_COOLDOWN_SEC=")]
        if value is not None:
            lines.append(f"YIBAN_BATCH_SIGN_COOLDOWN_SEC={value}")
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def _add_accounts(self, n, start=0):
        for i, phone in enumerate(self._phones(n, start)):
            db.add_account({
                "phone": phone, "password": f"pw{i}",
                "owner": "admin", "name": f"t{i}", "status": "active",
                "phone_code": "",
            })

    @staticmethod
    def _phones(n, start=0):
        """生成 n 个互异的 11 位测试手机号（与账号添加顺序一致，供 ids 对位）。"""
        return [f"138{start + i:08d}" for i in range(n)]

    def _login(self, c):
        r = c.post("/api/login", json={"username": "admin", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c.get("/api/me").get_json()["csrf_token"]

    def _trigger_batch(self, c, csrf, phones):
        # ids 按 _phones 编码的账号下标还原（138{start+i:08d} → i），任意子集与
        # load_accounts() 顺序对位，避免 409「账号列表已变化」（防错位校验拦截）
        ids = [int(p[3:]) for p in phones]
        return c.post("/api/signin/batch",
                      json={"ids": ids, "phones": list(phones)},
                      headers={"X-CSRF-Token": csrf})

    def _trigger_until_batch_done(self, c, csrf, phones, deadline_s=15):
        """触发批量签到；若后台队列未完成（"正在执行"）则轮询重试至完成。"""
        deadline = time.time() + deadline_s
        while True:
            r = self._trigger_batch(c, csrf, phones)
            if r.status_code != 429:
                return r
            err = r.get_json().get("error", "")
            if "正在执行" in err and time.time() < deadline:
                time.sleep(0.2)
                continue
            return r

    # =====================================================================
    # 1. M4 手动签到冷却单源化
    # =====================================================================
    def test_m4_single_trigger_blocks_batch_within_cooldown(self):
        """验收：单条手动签到成功后立刻触发批量 → 429 冷却提示（共用同一计数）。"""
        self._add_accounts(3)
        phones = self._phones(3)
        c = self.webapp.create_app().test_client()
        csrf = self._login(c)
        r1 = c.post("/api/signin", json={"phone": phones[0]},
                    headers={"X-CSRF-Token": csrf})
        self.assertEqual(r1.status_code, 200, r1.get_data(as_text=True))
        r2 = self._trigger_batch(c, csrf, phones[1:])
        self.assertEqual(r2.status_code, 429, r2.get_data(as_text=True))
        self.assertIn("冷却中", r2.get_json()["error"])

    def test_m4_single_trigger_blocks_another_single(self):
        """验收：单条触发后立刻再触发任意号 → 429 冷却提示（非 per-phone 语义）。"""
        self._add_accounts(3)
        phones = self._phones(3)
        c = self.webapp.create_app().test_client()
        csrf = self._login(c)
        r1 = c.post("/api/signin", json={"phone": phones[0]},
                    headers={"X-CSRF-Token": csrf})
        self.assertEqual(r1.status_code, 200, r1.get_data(as_text=True))
        r2 = c.post("/api/signin", json={"phone": phones[1]},
                    headers={"X-CSRF-Token": csrf})
        self.assertEqual(r2.status_code, 429, r2.get_data(as_text=True))
        self.assertIn("冷却中", r2.get_json()["error"])

    def test_m4_cooldown_zero_disables_for_single_and_batch(self):
        """反面：YIBAN_BATCH_SIGN_COOLDOWN_SEC=0 时单条连发不受拦（共用开关）。"""
        self._add_accounts(3)
        phones = self._phones(3)
        self._set_cooldown(0)
        c = self.webapp.create_app().test_client()
        csrf = self._login(c)
        for _ in range(2):
            r = c.post("/api/signin", json={"phone": phones[0]},
                       headers={"X-CSRF-Token": csrf})
            self.assertIn(r.status_code, (200, 429))
            if r.status_code == 429:  # 60s per-phone 防抖仍在（语义保持）
                self.assertIn("正在签到", r.get_json()["error"])
        r = self._trigger_batch(c, csrf, phones[1:])
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_m4_failed_batch_spawn_does_not_refresh_baseline(self):
        """失败不刷基准：spawn 失败（Popen 抛 FileNotFoundError）后，单条签到不得
        撞上冷却（新行为：launch 失败 → 500 启动失败；旧行为：finally 无条件刷基准
        → 单条会被 429 冷却拦下）。"""
        self._add_accounts(3)
        phones = self._phones(3)
        c = self.webapp.create_app().test_client()
        csrf = self._login(c)
        with mock.patch.object(self.webapp.subprocess, "Popen",
                               side_effect=FileNotFoundError("no script")):
            r = self._trigger_until_batch_done(c, csrf, phones[:2])
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            r2 = c.post("/api/signin", json={"phone": phones[2]},
                        headers={"X-CSRF-Token": csrf})
        self.assertEqual(r2.status_code, 500, r2.get_data(as_text=True))
        self.assertIn("启动失败", r2.get_json()["error"],
                      "单条应越过冷却检查后在启动环节失败（基准未被失败批量刷新）")

    # =====================================================================
    # 2. M5 批量超时缩放 + 上限
    # =====================================================================
    def test_m5_batch_over_limit_400(self):
        """验收：选中 11 个号 → 400（与 /api/accounts/batch 的 BATCH_OP_LIMIT 口径一致）。"""
        self._add_accounts(11)
        c = self.webapp.create_app().test_client()
        csrf = self._login(c)
        r = self._trigger_batch(c, csrf, self._phones(11))
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn("最多 10 个账号", r.get_json()["error"])

    def test_m5_batch_at_limit_accepted(self):
        """反面：10 个号（恰在上限内）→ 200 正常入队。"""
        self._add_accounts(10)
        c = self.webapp.create_app().test_client()
        csrf = self._login(c)
        r = self._trigger_batch(c, csrf, self._phones(10))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_m5_batch_wait_timeout_formula(self):
        """验收：超时计算公式 max(300, 120*n+300) 单测（默认参数 300 不动）。"""
        f = self.webapp._batch_wait_timeout
        self.assertEqual(f(0), 300)
        self.assertEqual(f(1), 420)
        self.assertEqual(f(5), 900)
        self.assertEqual(f(10), 1500)  # 120*10+300
        self.assertEqual(self.webapp._wait_signin_proc.__defaults__[0], 300,
                         "_wait_signin_proc 默认参数必须保持 300")

    def test_m5_batch_wait_timeout_scaled_at_call_site(self):
        """调用点传参缩放：2 个号的后台队列 wait(timeout=540)。完成后基准已刷新
        （下次批量 429 冷却中），以此确认 wait 已真实发生再断言。"""
        self._add_accounts(2)
        phones = self._phones(2)
        c = self.webapp.create_app().test_client()
        csrf = self._login(c)
        r = self._trigger_batch(c, csrf, phones)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        deadline = time.time() + 15
        while time.time() < deadline:
            probe = self._trigger_batch(c, csrf, phones)
            if probe.status_code == 429 and "冷却中" in probe.get_json().get("error", ""):
                break  # 冷却基准已挂 = spawn 成功 = wait(timeout=...) 已执行
            time.sleep(0.1)
        self.assertEqual(self.wait_timeouts, [540],
                         f"2 号队列超时应为 120*2+300=540，实际 {self.wait_timeouts}")

    # =====================================================================
    # 4. M8 web 侧接线：登录失败告警走独立账本
    # =====================================================================
    def test_m8_login_alert_routed_to_loginfail_ledger(self):
        """验收：3 次登录失败 → notify.send 收到 ledger="login_fail"（走真实
        send_notification 验证透传链路）；urgent 仍按喷洒判据为 False。"""
        c = self.webapp.create_app().test_client()
        # 口令校验的**两份绑定都要打**：`webapp.check_password_hash` 覆盖注册用户路径
        # （路由经 `m.*` 取 app 侧绑定），`web_security.check_password_hash` 覆盖内置
        # 管理员路径（`web/security.py` 的 verify_admin 用该模块自己的绑定，app 侧打桩
        # 到不了它）。本 block 内 verify_admin 已被打成 False 短路内置路径，security 侧
        # 这一桩是防"日后去掉 verify_admin 桩"时真实 scrypt 悄悄回流的兜底。
        with mock.patch.object(self.webapp, "verify_admin", return_value=False), \
             mock.patch.object(self.webapp, "check_password_hash", return_value=False), \
             mock.patch.object(web_security, "check_password_hash", return_value=False), \
             mock.patch.object(self.webapp, "_constant_time_dummy", lambda pwd: None), \
             mock.patch.object(self.webapp.notify, "send") as nsend:
            for _ in range(self.webapp.LOGIN_FAIL_NOTIFY):
                r = c.post("/api/login", json={"username": "admin", "password": "WrongPass#111"})
                self.assertEqual(r.status_code, 401)
        self.assertEqual(nsend.call_count, 1)
        kwargs = nsend.call_args.kwargs
        self.assertEqual(kwargs.get("ledger"), "login_fail",
                         "登录失败告警必须走 login_fail 独立账本")
        self.assertIs(kwargs.get("urgent"), False)

    # =====================================================================
    # 5. P3-1 限速内存表卫生
    # =====================================================================
    def test_p31_login_fail_key_truncated_to_128(self):
        """验收：200 长用户名截断——同 128 前缀的不同超长用户名共享失败计数
        （3+2=5 次即锁定）；若未截断则第二键独立计数、第 5 次仍是 401。"""
        c = self.webapp.create_app().test_client()
        long_a, long_b = "a" * 200, "a" * 128 + "b" * 72
        # 两份 check_password_hash 绑定同 test_m8_login_alert_routed_to_loginfail_ledger。
        with mock.patch.object(self.webapp, "verify_admin", return_value=False), \
             mock.patch.object(self.webapp, "check_password_hash", return_value=False), \
             mock.patch.object(web_security, "check_password_hash", return_value=False), \
             mock.patch.object(self.webapp, "_constant_time_dummy", lambda pwd: None), \
             mock.patch.object(self.webapp, "send_notification"):
            for _ in range(3):
                c.post("/api/login", json={"username": long_a, "password": "x"})
            r = c.post("/api/login", json={"username": long_b, "password": "x"})
            self.assertEqual(r.status_code, 401)
            r = c.post("/api/login", json={"username": long_b, "password": "x"})
        self.assertEqual(r.status_code, 429, r.get_data(as_text=True))
        self.assertIn("已锁定", r.get_json()["error"], "同 128 前缀应合并计数并锁定")

    def test_p31_restore_fail_key_truncated_to_128(self):
        """验收：restore 的 200 长邮箱同样截断 [:128]（3+2=5 次锁定）。"""
        c = self.webapp.create_app().test_client()
        long_a, long_b = "r" * 200, "r" * 128 + "s" * 72
        with mock.patch.object(self.webapp, "_constant_time_dummy", lambda pwd: None), \
             mock.patch.object(self.webapp, "send_notification"):
            for _ in range(3):
                r = c.post("/api/me/restore", json={"email": long_a, "password": "x"})
                self.assertEqual(r.status_code, 400)
            r = c.post("/api/me/restore", json={"email": long_b, "password": "x"})
            self.assertEqual(r.status_code, 400)
            r = c.post("/api/me/restore", json={"email": long_b, "password": "x"})
        self.assertEqual(r.status_code, 429, r.get_data(as_text=True))
        self.assertIn("密码错误次数过多", r.get_json()["error"])

    def test_p31_verify_limits_store_trim_evicts_stale(self):
        """_verify_limits 写入路径 trim：>10000 条且全过期的 store 在配额判定后
        应被清到只剩新鲜条目。"""
        store = {}
        stale = time.time() - (self.webapp.VERIFY_WINDOW + self.webapp._IP_STORE_MAX_AGE + 10)
        for i in range(self.webapp._IP_STORE_LIMIT + 1):
            store[f"u{i}"] = (1, stale)
        allowed = self.webapp._verify_attempt_allowed(store, "fresh-user")
        self.assertTrue(allowed)
        self.assertLess(len(store), 5, "过期条目应被 trim 回收")
        self.assertIn("fresh-user", store)

    def test_p31_restore_fail_rate_trim_called_on_write_path(self):
        """_restore_fail_rate（唯一无 trim 的表）写入路径补 trim：restore 失败路径
        必须以 RESTORE_FAIL_WINDOW + _IP_STORE_MAX_AGE 调用 _ip_store_trim。"""
        c = self.webapp.create_app().test_client()
        real_trim = self.webapp._ip_store_trim
        seen = []

        def spy(store, max_age):
            seen.append(max_age)
            return real_trim(store, max_age)

        with mock.patch.object(self.webapp, "_ip_store_trim", side_effect=spy), \
             mock.patch.object(self.webapp, "_constant_time_dummy", lambda pwd: None), \
             mock.patch.object(self.webapp, "send_notification"):
            c.post("/api/me/restore", json={"email": "gone@qq.com", "password": "x"})
        want = self.webapp.RESTORE_FAIL_WINDOW + self.webapp._IP_STORE_MAX_AGE
        self.assertIn(want, seen,
                      f"restore 失败路径应以 max_age={want} trim _restore_fail_rate，实际 {seen}")

    # =====================================================================
    # 6. P3-6 _atomic_write 创建即 0600
    # =====================================================================
    def test_p36_atomic_write_creates_with_0600(self):
        """验收：_atomic_write 的 tmp 文件经 os.open(..., 0o600) 创建（跨平台断言
        mode 实参；与 account_crypto._write_key_to_env_file 口径一致）。"""
        real_open = os.open
        recorded = []

        def spy(path, flags, mode=0o666):
            recorded.append(mode)
            return real_open(path, flags, mode)

        target = os.path.join(self.tmp, "aw-mode.txt")
        with mock.patch.object(self.webapp.os, "open", side_effect=spy):
            self.webapp._atomic_write(target, "hello")
        self.assertEqual(recorded, [0o600], "创建即 0600，不得先 0644 再补 chmod")
        self.assertTrue(os.path.exists(target))

    def test_p36_atomic_write_roundtrip_and_no_tmp_left(self):
        """功能不回归：内容正确写盘、无 .tmp 残留、chmod_priv 路径不炸；
        POSIX 上落盘文件权限 0600。"""
        target = os.path.join(self.tmp, "aw-roundtrip.txt")
        self.webapp._atomic_write(target, "内容content", chmod_priv=True)
        with open(target, encoding="utf-8") as f:
            self.assertEqual(f.read(), "内容content")
        leftovers = [p for p in os.listdir(self.tmp)
                     if p.startswith("aw-roundtrip") and ".tmp" in p]
        self.assertEqual(leftovers, [], "os.replace 后不得残留 tmp 文件")
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)


class Batch18Knife2SummaryMailTest(unittest.TestCase):
    """scripts/signin.py 侧 M7：_flush_admin_mail_summary 兜底。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-knife2-signin-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_DB_FILE"] = os.path.join(cls.tmp, "yiban.db")
        global db, signin
        import db
        import signin

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for k in ("YIBAN_ENV_FILE", "YIBAN_STATE_DIR", "YIBAN_DB_FILE"):
            os.environ.pop(k, None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        signin._mail_summary.clear()

    def tearDown(self):
        signin._mail_summary.clear()

    def test_m7_mail_failure_falls_back_to_webhook_urgent_force(self):
        """验收：mock SMTP 不可用（send_admin_alert 返回 False）+ webhook 启用 →
        汇总告警降级 notify.send(urgent=True, force=True)，零成功类告警仍达 webhook。"""
        signin._collect_admin_mail("当日签到异常告警", "本次全量签到 0 个账号成功")
        with mock.patch.object(signin.db, "admin_mail_recipients",
                               return_value=["a@x.com"]), \
             mock.patch.object(signin.mailer, "send_admin_alert", return_value=False) as m_mail, \
             mock.patch.object(signin.notify, "send", return_value=True) as m_notify:
            signin._flush_admin_mail_summary()
        m_mail.assert_called_once()
        m_notify.assert_called_once()
        self.assertEqual(m_notify.call_args.args[0], "易班签到汇总")
        self.assertTrue(m_notify.call_args.kwargs.get("urgent"),
                        "webhook 兜底必须 urgent=True")
        self.assertTrue(m_notify.call_args.kwargs.get("force"),
                        "webhook 兜底必须 force=True（绕过节流与当日额度）")
        self.assertEqual(signin._mail_summary, [])

    def test_m7_mail_exception_falls_back_to_webhook(self):
        """mailer 抛异常同样降级（不外泄、不中断收尾）。"""
        signin._collect_admin_mail("易班签到耗时告警", "账号: 138****0001")
        with mock.patch.object(signin.db, "admin_mail_recipients",
                               return_value=["a@x.com"]), \
             mock.patch.object(signin.mailer, "send_admin_alert",
                               side_effect=RuntimeError("smtp boom")), \
             mock.patch.object(signin.notify, "send", return_value=True) as m_notify:
            signin._flush_admin_mail_summary()
        m_notify.assert_called_once()
        self.assertTrue(m_notify.call_args.kwargs.get("force"))
        self.assertEqual(signin._mail_summary, [])

    def test_m7_mail_success_skips_webhook_fallback(self):
        """反面：邮件发送成功 → 不走 webhook 兜底（不双发）。"""
        signin._collect_admin_mail("易班签到失败", "账号: 138****0001\n原因: A")
        with mock.patch.object(signin.db, "admin_mail_recipients",
                               return_value=["a@x.com"]), \
             mock.patch.object(signin.mailer, "send_admin_alert", return_value=True), \
             mock.patch.object(signin.notify, "send") as m_notify:
            signin._flush_admin_mail_summary()
        m_notify.assert_not_called()
        self.assertEqual(signin._mail_summary, [])

    def test_m7_empty_recipients_logs_warning_and_skips_mail(self):
        """收件人集为空且推送未配置：显式 warning 留痕、不调 mailer、不走 webhook、清空收集器。"""
        signin._collect_admin_mail("当日签到异常告警", "零成功且窗口外")
        with mock.patch.object(signin.db, "admin_mail_recipients", return_value=[]), \
             mock.patch.object(signin.mailer, "send_admin_alert") as m_mail, \
             mock.patch.object(signin.notify, "is_configured", return_value=False), \
             mock.patch.object(signin.notify, "send") as m_notify, \
             self.assertLogs("yiban", level="WARNING") as logs:
            signin._flush_admin_mail_summary()
        m_mail.assert_not_called()
        m_notify.assert_not_called()
        self.assertEqual(signin._mail_summary, [])
        self.assertTrue(any("无可用收件人" in msg for msg in logs.output),
                        f"收件人为空必须 warning 留痕，实际 {logs.output}")


class Batch18Knife2NotifyLedgerTest(unittest.TestCase):
    """yiban/notify 侧 M8：login_fail 独立账本。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-knife2-notify-")
        self.env_file = os.path.join(self.tmp, "nope.env")
        os.environ["YIBAN_STATE_DIR"] = self.tmp
        os.environ["YIBAN_ENV_FILE"] = self.env_file
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_NOTIFY_TYPE"] = "serverchan"
        os.environ["YIBAN_NOTIFY_URL"] = SCT_KEY_K2
        os.environ["YIBAN_NOTIFY_COOLDOWN"] = "0"  # 关节流，单测只看账本
        for k in ("YIBAN_LOGINFAIL_DAILY_MAX", "YIBAN_NOTIFY_DAILY_MAX",
                  "YIBAN_NOTIFY_URGENT_DAILY_MAX"):
            os.environ.pop(k, None)
        notify_ledger._throttle_ts.clear()
        notify_ledger._skip_logged.clear()
        self._reset_ledgers()

    def tearDown(self):
        for k in ("YIBAN_STATE_DIR", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_KEY",
                  "YIBAN_NOTIFY_TYPE", "YIBAN_NOTIFY_URL", "YIBAN_NOTIFY_COOLDOWN",
                  "YIBAN_LOGINFAIL_DAILY_MAX", "YIBAN_NOTIFY_DAILY_MAX",
                  "YIBAN_NOTIFY_URGENT_DAILY_MAX"):
            os.environ.pop(k, None)
        self._reset_ledgers()
        notify_ledger._throttle_ts.clear()
        notify_ledger._skip_logged.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _reset_ledgers(self):
        for led in (notify_ledger._general_daily, notify_ledger._urgent_daily,
                    notify_ledger._loginfail_daily):
            led["state"].update({"date": "", "count": 0})
            led["notice"].update({"pending": False, "notified": False, "warned": False})
        # 磁盘是唯一事实源（_with_ledger_locked 每次读盘合并）：只重置内存不删盘，
        # 上一子步骤的计数会跨步骤串账——重置必须连磁盘账本一起清
        with contextlib.suppress(OSError):
            os.remove(self._ledger_file())

    def _ledger_file(self):
        return os.path.join(self.tmp, "notify-ledger.json")

    def _read_disk(self):
        with open(self._ledger_file(), encoding="utf-8") as f:
            return json.load(f)

    def test_m8_loginfail_exhausts_independently(self):
        """验收：YIBAN_LOGINFAIL_DAILY_MAX=1 → login_fail 账第 2 条被拒后，
        主 urgent 账与 general 账仍可发（互不挤占）。"""
        os.environ["YIBAN_LOGINFAIL_DAILY_MAX"] = "1"
        with mock.patch.object(_notify_K2, "_send_serverchan", return_value=True) as send:
            self.assertTrue(_notify_K2.send("登录失败告警", "1", ledger="login_fail"))
            self.assertFalse(_notify_K2.send("登录失败告警B", "2", ledger="login_fail"),
                             "login_fail 账打满后应停手")
            self.assertTrue(_notify_K2.send("审计链异常", "3", urgent=True),
                            "login_fail 打满后主 urgent 账仍可发")
            self.assertTrue(_notify_K2.send("用户日常改密", "4"),
                            "login_fail 打满后 general 账仍可发")
        self.assertEqual(send.call_count, 3)

    def test_m8_loginfail_limit_reading_env_var_and_dotenv(self):
        """读取口径：环境变量优先于 .env；0=不限；非法值回退默认 3。"""
        with mock.patch.object(_notify_K2, "_send_serverchan", return_value=True):
            # 1) .env 键（无环境变量）：上限 2
            with open(self.env_file, "w", encoding="utf-8") as f:
                f.write("YIBAN_LOGINFAIL_DAILY_MAX=2\n")
            for i in range(2):
                self.assertTrue(_notify_K2.send(f"lf-a{i}", "x", ledger="login_fail"))
            self.assertFalse(_notify_K2.send("lf-a2", "x", ledger="login_fail"))
            self._reset_ledgers()
            # 2) 环境变量优先：env=3 覆盖 .env=1
            os.environ["YIBAN_LOGINFAIL_DAILY_MAX"] = "3"
            with open(self.env_file, "w", encoding="utf-8") as f:
                f.write("YIBAN_LOGINFAIL_DAILY_MAX=1\n")
            for i in range(3):
                self.assertTrue(_notify_K2.send(f"lf-b{i}", "x", ledger="login_fail"))
            self.assertFalse(_notify_K2.send("lf-b3", "x", ledger="login_fail"))
            self._reset_ledgers()
            # 3) 0 = 不限
            os.environ["YIBAN_LOGINFAIL_DAILY_MAX"] = "0"
            for i in range(5):
                self.assertTrue(_notify_K2.send(f"lf-c{i}", "x", ledger="login_fail"))
            self._reset_ledgers()
            # 4) 非法值回退默认 3
            os.environ["YIBAN_LOGINFAIL_DAILY_MAX"] = "abc"
            for i in range(3):
                self.assertTrue(_notify_K2.send(f"lf-d{i}", "x", ledger="login_fail"))
            self.assertFalse(_notify_K2.send("lf-d3", "x", ledger="login_fail"))

    def test_m8_loginfail_ledger_persists_to_disk(self):
        """login_fail 账随 P2-3 机制落盘（跨进程共享额度），结构完整。"""
        with mock.patch.object(_notify_K2, "_send_serverchan", return_value=True):
            self.assertTrue(_notify_K2.send("登录失败告警", "x", ledger="login_fail"))
        self.assertTrue(os.path.exists(self._ledger_file()))
        disk = self._read_disk()
        self.assertEqual(disk["login_fail"]["count"], 1)
        self.assertEqual(disk["login_fail"]["date"], notify_ledger._daily_today())
        # 既有两本账不受影响
        self.assertIn("general", disk)
        self.assertIn("urgent", disk)

    def test_m8_default_behavior_unchanged_without_ledger_arg(self):
        """反面（向后兼容）：不传 ledger → 现行行为（urgent→urgent 账 / 其余→general 账）。"""
        os.environ["YIBAN_NOTIFY_DAILY_MAX"] = "1"
        os.environ["YIBAN_NOTIFY_URGENT_DAILY_MAX"] = "1"
        with mock.patch.object(_notify_K2, "_send_serverchan", return_value=True):
            self.assertTrue(_notify_K2.send("g1", "x"))
            self.assertFalse(_notify_K2.send("g2", "x"), "general 账上限 1 → 第二条拒")
            self.assertTrue(_notify_K2.send("u1", "x", urgent=True))
            self.assertFalse(_notify_K2.send("u2", "x", urgent=True), "urgent 账上限 1 → 第二条拒")
        disk = self._read_disk()
        self.assertEqual(disk["general"]["count"], 1)
        self.assertEqual(disk["urgent"]["count"], 1)
        self.assertEqual(disk["login_fail"]["count"], 0,
                         "不传 ledger 不得在 login_fail 账上记账")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
