# -*- coding: utf-8 -*-
"""线路级请求/响应落盘（协议核验专用诊断，默认关闭）。

**是什么**：挂载在 `requests.Session` 上的透传适配器——请求原样放行，仅在侧路把
每个请求/响应的**形状**追加落盘为 JSONL。用途是协议层核验（抓包对照：核对各端点的
方法/路径/头/体形状是否符合观察到的真实行为），不是调试日志，更不是业务功能。

**默认关闭**：仅当环境变量 `YIBAN_WIRE_DUMP` 指向一个可写目录时才挂载。落盘文件含
会话级 Cookie 名与响应体片段，属敏感面——目录按 0700/0600 收权，用完即清。
落盘目录由 `YIBAN_WIRE_DUMP` 指定，**默认不是状态目录**；只有把该变量指向状态目录时，
`state_gc` 的清理（`wire-` 已登记进 `ARTIFACTS`）才会扫到这些件，其余情况由运维自行清理。

**脱敏口径**（供协议核验也绝不外泄凭据）：
- 请求头 `Cookie`/`Authorization` 只记 **名字 + 值长度**，不记值；
- 请求体的**凭据字段**（键名表 = `masking._QKEYED_CRED`，比 `_CRED_KEY` 多含
  `phone_code`；另补签到体字段 `Code`，"表单 k=v" 与 "JSON" 两形态）值替换为固定占位
  `_REDACTED`——真实登录字段 `oauth_upwd` 由 `pwd` 片段覆盖；
- 请求体里的手机号值走 `masking.mask_phones_in_text` 打码——字段名 `oauth_uname`
  承载手机号，键名不含凭据片段，只有按值打码才遮得住；
- URL 面（`url` 与 `final_url`）走 `masking.sanitize_url`，query/fragment 的凭据参数打码；
- **承载 URL 的头**（`Location`/`Content-Location`/`Referer` 值即 URL，整段走
  `sanitize_url`；`Link`/`Refresh` 内嵌 URL，抽出后逐个走 `sanitize_url`）——请求侧与
  响应侧同办。真 OAuth 重定向第 4 步用 `allow_redirects=False`，令牌就在 `Location` 头里；
- `Set-Cookie`（响应侧）只记 cookie 名，值脱敏——cookie 名本身是核验对象；
- 其余头（UA/Content-Type/AppVersion 等静态特征）原样保留——它们正是核验对象。

**未脱敏面（如实登记：哪些面不脱敏、为什么、风险与使用前提）**：
1. `resp_body` 原样落盘。**为什么**：WAF 挑战页/协议返回体是核验对象本体，打码即失去
   核验价值。**风险**：应答若回显 `access_token` 或手机号，会明文入库。**前提**：仅用于
   受控协议核验；目录按 0700/0600 收权、用完即清。
2. **自定义凭据头**未按键名脱敏——请求侧如 `X-Api-Key`/`X-CSRF-Token`/`X-Auth-Token`，
   响应侧如 `X-Session-Token`。**为什么**：键名表只收口 `Cookie`/`Authorization`/`Set-Cookie`
   三类。**风险**：换核引入这类头后，其值明文入库。**前提**：当前 UA 头集合只有静态
   `X-Requested-With` 与 `AppVersion`，不带这类头；换核引入后必须一并收口。
3. 手机号按值打码只覆盖**连续 11 位**本号码（`masking.mask_phones_in_text` 口径）：
   `+86` 前缀、带空格/连字符的分段形态不命中。**这是 `masking` 模块的缺口，不在本模块修**
   （共用面，另立后续项）。**风险**：上游以非 11 位形态回显手机号时原样落盘。**前提**：
   本 App 账号存 11 位本号码，该形态经表单校验不可达。
"""

import json
import logging
import os
import re
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
            "url": masking.sanitize_url(request.url),
            "req_headers": _redact_headers(request.headers),
            "req_body": _redact_body(request.body),
            "status": resp.status_code,
            "resp_headers": _redact_headers(resp.headers, cookie_names_only=True),
            "resp_body": _clip_body(resp),
            "redirects": len(getattr(resp, "history", []) or []),
            "final_url": masking.sanitize_url(resp.url) if resp.url else resp.url,
            "elapsed_ms": round(elapsed * 1000, 1),
        }
        line = json.dumps(record, ensure_ascii=False)
        # 文件名里的日期用 f-string 而非 % 模板：state_gc 的"按日文件必须登记"元测试
        # 只认 `"<前缀>-…{表达式}"` 形态的字面量，%s 模板会逃过扫描（census 已点名）。
        path = os.path.join(self._dir, f"wire-{now().strftime('%Y-%m-%d')}.jsonl")
        with _lock:
            existed = os.path.exists(path)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, (line + "\n").encode("utf-8"))
            finally:
                os.close(fd)
            if not existed:
                os.chmod(path, 0o600)


#: 响应/请求头里**值即 URL** 的头：真 OAuth 重定向第 4 步用 `allow_redirects=False`，
#: 令牌（`verify_request`/`code`/`#access_token=`）就在 `Location` 头里；`Content-Location`
#: 与 `Referer` 同形。值整段走 `sanitize_url`（兼容相对 URL 形态）。
_URL_VALUE_HEADERS = ("location", "content-location", "referer")
#: 值**内嵌** URL 但带包裹语法的头：`Link: <url>; rel=…`、`Refresh: 0; url=…`。
#: 抽出绝对 URL 逐个过 `sanitize_url`，保留其余语法（核验要看得见头形状）。
_URL_EMBEDDED_HEADERS = ("link", "refresh")
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s<>\"']+")


def _redact_headers(headers, cookie_names_only=False):
    """头脱敏：凭据头只记长度/名字，承载 URL 的头走 `sanitize_url`，其余原样。

    `cookie_names_only` 仅控制 `Set-Cookie`（响应侧）是否只留 cookie 名；
    URL 承载头在请求侧与响应侧一律脱敏（同族面，不分侧）。
    """
    out = {}
    for name, value in headers.items():
        low = name.lower()
        if low in ("cookie", "authorization"):
            out[name] = _REDACTED % len(value)
        elif low == "set-cookie":
            if not cookie_names_only:
                out[name] = value
                continue
            # 只留 cookie 名（=左侧），值脱敏——cookie 名本身是核验对象
            values = [value] if isinstance(value, str) else list(value)
            out[name] = []
            for v in values:
                cname, _, rest = v.partition("=")
                out[name].append("%s=%s" % (cname.strip(), _REDACTED % len(rest)))
        elif low in _URL_VALUE_HEADERS:
            out[name] = masking.sanitize_url(value)
        elif low in _URL_EMBEDDED_HEADERS:
            out[name] = _URL_IN_TEXT_RE.sub(
                lambda m: masking.sanitize_url(m.group(0)), str(value))
        else:
            out[name] = value
    return out


#: 凭据键名口径只有一张表：`masking._QKEYED_CRED`（与日志/通知出口面同一张，
#: 比 `_CRED_KEY` 多含 `phone_code`）。键名两侧允许 `_`/`-` 前后缀，故 `oauth_upwd`
#: （真实登录字段）由其中的 `pwd` 片段覆盖。
#: 另补签到体字段 `Code`：其值来自 `phone_code`（设备绑定码，masking 视其为凭据），
#: 但 `_QKEYED_CRED` **不覆盖裸 `Code`**（`code` 不在 `_CRED_KEY`）。故显式加一条
#: 精确字段名 `code`——只认整字段名，不带前后缀，避免误伤含 `code` 子串的普通字段。
_CRED_NAME = r"(?:" + masking._QKEYED_CRED + r"|code)"
#: 表单形态 `k=v&k2=v2`：值只取到下一个 `&`，不跨字段吞并（核验要看得见字段名）。
_FORM_PAIR = re.compile(r"(?i)(^|[&?])(" + _CRED_NAME + r")=([^&]*)")
#: JSON 形态 `"k": "v"`：值取配对的双引号串（含反斜杠转义），单引号 repr 不在 HTTP 体里。
_JSON_PAIR = re.compile(r'(?i)("' + _CRED_NAME + r'")(\s*:\s*)("[^"\\]*(?:\\.[^"\\]*)*")')


#: 占位串形态（`<redacted:N>`）：已脱敏值再次进入脱敏链时原样保留，保证幂等。
_PLACEHOLDER_RE = re.compile(r"^<redacted:\d+>$")


def _mask_form_pair(m):
    """表单字段替换体：保留 `分隔符 + 键名 + =`，值换成带长度的固定占位。"""
    value = m.group(3)
    if _PLACEHOLDER_RE.match(value):  # 幂等：占位串不再二次计量
        return m.group(0)
    return "%s%s=%s" % (m.group(1), m.group(2), _REDACTED % len(value))


def _mask_json_pair(m):
    """JSON 字段替换体：保留键名与冒号，值换成带长度的固定占位（引号成对保住 JSON 形状）。"""
    inner = m.group(3)[1:-1]
    if _PLACEHOLDER_RE.match(inner):  # 幂等：占位串不再二次计量
        return m.group(0)
    return '%s%s"%s"' % (m.group(1), m.group(2), _REDACTED % len(inner))


def _redact_body(body):
    """请求体脱敏：凭据字段值换占位、手机号值打码；不做"有无凭据"的前置判定。

    键名表与手机号口径都取自 `yiban.masking`（唯一事实源）：凭据字段由
    `_QKEYED_CRED` 识别（`oauth_upwd` 命中 `pwd` 片段，`phone_code` 由宽表命中），
    签到体字段 `Code` 另由 wire 侧精确名补齐；`oauth_uname` 这类承载手机号但键名不含
    凭据片段的字段由 `mask_phones_in_text` 按值打码。全量走同一条链，所以**不再**有
    "不含 password 就原样落盘"的分支——那条分支正是明文落盘的成因。

    **幂等**：占位串（`<redacted:N>`）再次进入时由 `_PLACEHOLDER_RE` 原样保留，
    `mask_phones_in_text` 亦幂等，故重复调用不再改变输出。
    """
    if body is None:
        return None
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8")
        except UnicodeDecodeError:
            return "<binary:%d>" % len(body)
    text = _FORM_PAIR.sub(_mask_form_pair, str(body))
    text = _JSON_PAIR.sub(_mask_json_pair, text)
    return masking.mask_phones_in_text(text)[:_BODY_CAP]


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
