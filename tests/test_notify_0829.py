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
- 批次14 P2-1：紧急 / 非紧急两本账互不挤占、发送失败退还额度、首次耗尽一次性告知
  （budget_exhausted_today / pop_exhaustion_notice）、跨日两账同时归零、
  跳过原因日志同窗口去重
- 批次14 修复轮1：额度"打满当次"即挂耗尽告知（不等下一次被拒）、退还按占用凭证执行
  （跨日凭证作废、发送途中改上限不多退不漏退、虚警撤回）、pop_exhaustion_notice
  返回哪些账本耗尽且每本账每日各一次、get_config 一轮只解析一次 .env
- 批次14 修复轮2：虚警判定与告知撤回合并在同一把账本锁内（并发下真实 pending
  不会被陈旧撤回抹掉）；耗尽告知标记与额度计数同属一本账（notify._*_daily["notice"]）
- 批次14 P2-2 同源：get_secret 必须按 YIBAN_ENV_FILE 解析路径取钥，不在 cwd 生成游离密钥
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


def _reset_notices():
    """清空两本账的耗尽告知标记（进程内状态，用例之间必须互不串）。

    批次14 修复轮2：pending/notified/warned 从全局 notify._exhaustion 挪进了
    各本账自己的 notice 子字典（与 count 同一把账本锁），复位口径随之逐本清。
    """
    for ledger in (notify._general_daily, notify._urgent_daily):
        ledger["notice"].update({"pending": False, "notified": False, "warned": False})


def _clear(monkeypatch):
    """隔离环境：固定加密密钥 + 隔离 .env（不读项目根 .env）+ 清空 YIBAN_NOTIFY_*。

    批次15 P2-3：额外隔离账本目录（YIBAN_STATE_DIR 指向临时目录）并清掉旧账本
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
    notify._throttle_ts.clear()
    # 重置进程内状态：两本账、耗尽告知标记、跳过日志去重表（批次14）
    notify._general_daily["state"].update({"date": "", "count": 0})
    notify._urgent_daily["state"].update({"date": "", "count": 0})
    _reset_notices()
    notify._skip_logged.clear()
    for k in list(os.environ):
        if k.startswith("YIBAN_NOTIFY_"):
            monkeypatch.delenv(k)


def _freeze_day(monkeypatch, day):
    """冻结/切换"今天"：patch notify 自己的当日日期来源。

    不去动 stdlib time.strftime——那是全进程共享对象，patch 它会影响其它模块甚至
    其它线程的用例，跨日语义只要 _daily_today 稳定返回目标日期即可。
    """
    monkeypatch.setattr(notify, "_daily_today", lambda: day[0])
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

    monkeypatch.setattr(notify.requests, "post", _post)


def _rejected_post(monkeypatch, calls):
    """服务端拒绝（code!=0）的替身：请求发出去了，但手机没收到。"""
    def _post(url, **kw):
        calls.append(kw["data"]["title"])
        return _Resp(429)

    monkeypatch.setattr(notify.requests, "post", _post)


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


# ---- 仅重要告警 ----

def test_urgent_only_skips_non_urgent(monkeypatch):
    _configure_serverchan(monkeypatch)
    _set(monkeypatch, URGENT_ONLY="1")
    calls = []

    class FakeResp:
        def json(self):
            return {"code": 0}

    monkeypatch.setattr(notify.requests, "post", lambda *a, **k: calls.append(1) or FakeResp())
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

    monkeypatch.setattr(notify.requests, "post", lambda *a, **k: calls.append(1) or FakeResp())
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

    monkeypatch.setattr(notify.requests, "post", lambda *a, **k: calls.append(1) or FakeResp())
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

    monkeypatch.setattr(notify.requests, "post", lambda *a, **k: calls.append(1) or FakeResp())
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

    monkeypatch.setattr(notify.requests, "post", lambda *a, **k: calls.append(1) or FakeResp())
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

    monkeypatch.setattr(notify.requests, "post", lambda *a, **k: calls.append(1) or FakeResp())
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
    real = notify._read_env_file
    reads = []

    def _counting():
        reads.append(1)
        return real()

    monkeypatch.setattr(notify, "_read_env_file", _counting)
    cfg = notify.get_config()
    assert (cfg["daily_max"], cfg["urgent_daily_max"], cfg["cooldown"]) == (4, 2, 15)
    assert len(reads) == 1, f".env 被重复解析了 {len(reads)} 次"


# ---- 批次14 P2-1：额度分两本账 ----

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


# ---- 批次14 P2-1：失败退还 ----

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
    monkeypatch.setattr(notify.requests, "post", mock.Mock(side_effect=RuntimeError("boom")))
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
    monkeypatch.setattr(notify.requests,
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
    monkeypatch.setattr(notify.requests, "post", lambda *a, **k: calls.append(1) or _Resp(0))
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
    monkeypatch.setattr(notify.requests, "post",
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
    stale = notify._consume_daily_budget("general")             # 昨天的占用，请求还没回来
    assert stale.allowed and stale.ledger == "general" and stale.day == "2026-08-29"
    day[0] = "2026-08-30"                                       # 跨日：次日账本归零重来
    calls = []
    _ok_post(monkeypatch, calls)
    assert notify.send("次日1", "内容") is True                   # 今天已实打实发出 1 条
    assert notify.get_config()["daily_remaining"] == 1
    notify._refund_daily_budget(stale)                          # 昨天的失败此刻才退还
    assert notify.get_config()["daily_remaining"] == 1, "跨日退还不得扣到次日账上"
    assert notify.send("次日2", "内容") is True                   # 次日仍是完整的 2 条额度
    assert notify.send("次日3", "内容") is False
    assert len(calls) == 2


def test_refund_kept_when_limit_switched_to_unlimited(monkeypatch):
    """修复轮③ 反向：占用时有限额、退还时管理员已改成不限额，旧凭证仍要照退（不得漏退）。"""
    _configure_serverchan(monkeypatch, cooldown=0)
    _set(monkeypatch, DAILY_MAX="2")
    ticket = notify._consume_daily_budget("general")
    assert ticket.ledger == "general" and ticket.day == notify._daily_today()
    assert notify._general_daily["state"]["count"] == 1
    monkeypatch.setenv("YIBAN_NOTIFY_DAILY_MAX", "0")            # 发送途中改成不限
    notify._refund_daily_budget(ticket)
    assert notify._general_daily["state"]["count"] == 0, \
        "退还只认凭证：不限额是「现在」的状态，不能据此认定当初没占"


def test_refund_ticket_is_one_shot(monkeypatch):
    """凭证一次性：同一笔占用退两次只退一次，否则等于白送一条额度。"""
    _configure_serverchan(monkeypatch, cooldown=0)
    _set(monkeypatch, DAILY_MAX="2")
    ticket = notify._consume_daily_budget("general")                 # 占 1 条
    assert notify._consume_daily_budget("general").allowed is True   # 占满 2/2
    assert notify._general_daily["state"]["count"] == 2
    notify._refund_daily_budget(ticket)
    notify._refund_daily_budget(ticket)                              # 第二次须被忽略
    assert notify._general_daily["state"]["count"] == 1


def test_no_phantom_refund_when_limit_raised_mid_send(monkeypatch):
    """修复轮③ 正向：占用时就是不限额（没占额度），失败退还不得把账退成"凭空多一条"。"""
    _configure_serverchan(monkeypatch, cooldown=0)
    _set(monkeypatch, DAILY_MAX="1")
    calls = []
    _ok_post(monkeypatch, calls)
    assert notify.send("普通1", "内容") is True                  # 唯一的额度用掉
    monkeypatch.setenv("YIBAN_NOTIFY_DAILY_MAX", "0")            # 管理员中途放开
    monkeypatch.setattr(notify.requests, "post", mock.Mock(side_effect=RuntimeError("boom")))
    assert notify.send("普通2", "内容") is False                 # 不限额：本次没占额度
    assert notify._general_daily["state"]["count"] == 1, "没占额度就不该退，退了就是幻影退还"
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
    mine = notify._consume_daily_budget("general")                 # 甲：占 1 条
    rival = notify._consume_daily_budget("general")                # 乙：占满 2/2
    assert mine.allowed and rival.allowed
    assert notify.budget_exhausted_today(False) is True
    assert notify._general_daily["notice"]["pending"] is True      # 告知还没被取走

    def _other_thread():
        """竞争线程：把甲刚退出来的那条重新占掉 → 账本再次真实打满并挂标记。"""
        filled = notify._consume_daily_budget("general")
        assert filled.allowed is True, "前置条件失败：此刻账上应还剩一条可占"
        assert notify._general_daily["state"]["count"] == 2

    real_lock = notify._general_daily["lock"]
    notify._general_daily["lock"] = _InterferingLock(_other_thread)
    try:
        notify._refund_daily_budget(mine)                          # 甲的失败退还在此发生
    finally:
        notify._general_daily["lock"] = real_lock

    assert notify._general_daily["state"]["count"] == 2, "竞争线程占的那条不该被甲退掉"
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
    assert notify._general_daily["state"]["count"] == 0


# ---- 批次14 P2-1：首次耗尽一次性告知 ----

def test_last_message_filling_budget_still_notifies(monkeypatch, caplog):
    """修复轮①：最后一条恰好打满、之后不再有新的 send 调用时，告知仍必须能取到。

    旧实现只在"下一次尝试被拒"时才补标记——当日再无新告警就等于静默，
    而这正是本任务要治的病。
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


# ---- 批次14：跳过原因可见（同窗口去重） ----

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
        notify._skip_logged.clear()
        assert notify.send("高危操作", "内容", urgent=True) is True
        assert notify.send("高危操作", "内容", urgent=True) is False
        assert _info_lines(caplog, "节流") == 1
        assert notify.get_config()["urgent_daily_remaining"] == 2  # 节流不扣额度
        notify._skip_logged.clear()
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
    notify._throttle_ts.clear()
    notify._skip_logged.clear()
    notify._general_daily["state"].update({"date": "", "count": 0})
    notify._urgent_daily["state"].update({"date": "", "count": 0})
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


# ---- 批次14 P2-2 同源：get_secret 的密钥来源与不抛异常契约 ----

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
    monkeypatch.setattr(notify.requests, "post", post)
    monkeypatch.setattr(account_crypto, "load_key", mock.Mock(side_effect=OSError("EACCES")))
    with caplog.at_level(logging.WARNING, logger="notify"):
        assert notify.get_secret() == ""
        assert notify.budget_exhausted_today() is False           # 排除"因耗尽而 False"
        assert notify.send("告警", "内容") is False
    assert "解密失败" in caplog.text
    assert post.call_count == 0, "解不出密钥却仍发起了外发请求"
    assert notify.get_config()["daily_remaining"] == 3, "取钥失败不该消耗额度"
    assert notify.get_config()["enabled"] is False, "设置页应如实显示通道不可用"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
