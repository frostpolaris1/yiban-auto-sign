# -*- coding: utf-8 -*-
"""线路级请求/响应落盘（协议核验专用诊断，默认关闭）。

**是什么**：挂载在 `requests.Session` 上的透传适配器——请求原样放行，仅在侧路把
每个请求/响应的**形状**追加落盘为 JSONL。用途是协议层核验（抓包对照：核对各端点的
方法/路径/头/体形状是否符合观察到的真实行为），不是调试日志，更不是业务功能。

**默认关闭**：仅当环境变量 `YIBAN_WIRE_DUMP` 指向一个可写目录时才挂载。落盘文件含
会话级 Cookie 名与响应体片段，属敏感面——目录按 0700/0600 收权，用完即清。

**脱敏口径**（硬约束，供协议核验也绝不外泄凭据）：
- 请求头 `Cookie`/`Authorization` 只记 **名字 + 值长度**，不记值；
- 请求体的 `password` 字段（表单或 JSON）值替换为固定占位；
- 响应头只记名字 + Set-Cookie 的 cookie 名，不记值；
- 其余头（UA/Referer/CSRF 等静态特征）原样保留——它们正是核验对象。
"""

import json
import logging
import os
import threading
import time

import requests

from yiban import masking
from yiban.clock import now

logger = logging.getLogger(__name__)

#: 单条响应体落盘上限（WAF 挑战页含内联 JS，截短会丢核验对象，故给足量）
_BODY_CAP = 65536
#: 值脱敏后保留的形态说明占位
_REDACTED = "<redacted:%d>"
_ENV_KEY = "YIBAN_WIRE_DUMP"

_lock = threading.Lock()
_warned = False


class WireDumpAdapter(requests.adapters.HTTPAdapter):
    """透传落盘适配器：包住既有 https 适配器，请求行为零改变。"""

    def __init__(self, inner, dump_dir, phone):
        self._inner = inner
        self._dir = dump_dir
        self._phone = masking.mask_phone(phone)
        super().__init__()

    def send(self, request, **kwargs):
        t0 = time.monotonic()
        resp = self._inner.send(request, **kwargs)
        try:
            self._dump(request, resp, time.monotonic() - t0)
        except Exception as e:  # 落盘失败绝不影响签到主流程（仅警告一次）
            global _warned
            if not _warned:
                _warned = True
                logger.debug("线路落盘失败（仅警告一次）: %s", e)
        return resp

    # requests 兼容：证书校验等参数透传给内层；close 同步关闭内层
    def close(self):
        self._inner.close()

    def _dump(self, request, resp, elapsed):
        record = {
            "ts": now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "phone": self._phone,
            "method": request.method,
            "url": request.url,
            "req_headers": _redact_headers(request.headers),
            "req_body": _redact_body(request.body),
            "status": resp.status_code,
            "resp_headers": _redact_headers(resp.headers, cookie_names_only=True),
            "resp_body": _clip_body(resp),
            "redirects": len(getattr(resp, "history", []) or []),
            "final_url": resp.url,
            "elapsed_ms": round(elapsed * 1000, 1),
        }
        line = json.dumps(record, ensure_ascii=False)
        path = os.path.join(self._dir, "wire-%s.jsonl" % now().strftime("%Y-%m-%d"))
        with _lock:
            existed = os.path.exists(path)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, (line + "\n").encode("utf-8"))
            finally:
                os.close(fd)
            if not existed:
                os.chmod(path, 0o600)


def _redact_headers(headers, cookie_names_only=False):
    out = {}
    for name, value in headers.items():
        low = name.lower()
        if low in ("cookie", "authorization"):
            out[name] = _REDACTED % len(value)
        elif low == "set-cookie" and cookie_names_only:
            # 只留 cookie 名（=左侧），值脱敏——cookie 名本身是核验对象
            values = [value] if isinstance(value, str) else list(value)
            out[name] = []
            for v in values:
                cname, _, rest = v.partition("=")
                out[name].append("%s=%s" % (cname.strip(), _REDACTED % len(rest)))
        else:
            out[name] = value
    return out


def _redact_body(body):
    """请求体脱敏：password 字段（表单 k=v 或 JSON）值替换为占位。"""
    if body is None:
        return None
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8")
        except UnicodeDecodeError:
            return "<binary:%d>" % len(body)
    text = str(body)
    if "password" in text.lower():
        # 表单形态逐字段替换；JSON 形态按 key 替换（两种都覆盖，容忍混合）
        parts = []
        for chunk in text.split("&"):
            k, _, v = chunk.partition("=")
            if "password" in k.lower():
                parts.append("%s=%s" % (k, _REDACTED % len(v)))
            else:
                parts.append(chunk)
        text = "&".join(parts) if "&" in str(body) else text
        if '"password"' in text:
            import re
            text = re.sub(r'("password"\s*:\s*)"[^"]*"', r'\1"%s"' % (_REDACTED % 8), text)
    return text[:_BODY_CAP]


def _clip_body(resp):
    ctype = resp.headers.get("Content-Type", "")
    try:
        text = resp.text
    except Exception:  # 解码失败按二进制记长度
        return "<binary:%d>" % len(resp.content or b"")
    if len(text) > _BODY_CAP:
        return {"truncated": True, "content_type": ctype,
                "head": text[:_BODY_CAP]}
    return text


def maybe_mount(session, phone):
    """按环境变量决定是否挂载线路落盘适配器（未设/目录不可建则不挂载）。"""
    target = os.environ.get(_ENV_KEY, "").strip()
    if not target:
        return False
    if getattr(session, "_wire_dump_mounted", False):
        return True
    try:
        os.makedirs(target, mode=0o700, exist_ok=True)
        inner = session.get_adapter("https://")
        session.mount("https://", WireDumpAdapter(inner, target, phone))
        session._wire_dump_mounted = True
        logger.info("线路落盘已启用: %s（协议核验诊断，用完请关闭）", target)
        return True
    except OSError as e:
        logger.warning("线路落盘目录不可用（%s），本次不启用", e)
        return False
