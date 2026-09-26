#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""假易班 HTTPS 服务——只用于测试机的回环压测，绝不指向真实易班。

覆盖 ``scripts/signin.py`` 默认登录流程（login_killyiban）与签到流程实际调用的
全部接口形状：

  登录链（默认 KillYiBan 流程，4 步）
    GET  /code/html                                   -> 200 登录页（input#key + var page_use）
    POST /code/usersure                               -> JSON {"code": "s200"}
    GET  /iframe/index                                -> 302 Location 带 verify_request
    GET  /base/c/auth/yiban                           -> JSON {"code": 0}（带 verifyRequest）
  登录链（旧 iOS 流程，YIBAN_LEGACY_LOGIN=1 时启用，多一步跳转）
    GET  /base/c/auth/yiban（不带 verifyRequest）      -> JSON data.Data = OAuth 入口 URL
    POST /code/usersure（scope=1,2,3,4,）             -> JSON {"reUrl": ...}（旧流程的成功标志）
    GET  /iapp7463                                    -> 302 到 iframe/index
    GET  /iframe/index                                -> 302 Location 带 verify_request
    GET  /base/c/auth/yiban（带 verifyRequest）        -> JSON {"code": 0}
  签到（2 步）
    GET  /nightAttendance/student/index/signPosition  -> JSON 点位 + 时间窗口
    POST /nightAttendance/student/index/signIn        -> JSON {"code": 0, "data": {...}}
  探针入口
    GET  /nightAttendance/student/index/signPosition  -> 只读探针（verify/--probe 复用）
  运维入口
    GET  /__stats   -> 运行统计（含并发峰值）
    GET  /__health  -> 存活探测

设计约束：
  * 仅标准库（http.server / ssl / json / threading），不引入第三方依赖。
  * 默认只监听回环地址（127.0.0.1 与 ::1），绝不监听 0.0.0.0。
  * 逐请求结构化落盘 JSONL（毫秒时间戳 / 耗时 / 结果 / 并发数）。
  * 延迟与失败注入可热读配置文件（压测过程中切换档位无需重启）。

RSA 公钥说明：登录页里的 ``input#key`` 需要一段合法 PEM 公钥，signin 会用它做
PKCS1_v1_5 加密（本 mock 从不解密），因此内置一把**测试专用**公钥即可，无私钥、
无敏感信息（可用 ``--pubkey-file`` 覆盖）。

退出码：0 正常；2 参数/运行环境错误。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import random
import socket
import socketserver
import ssl
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

# ---------------------------------------------------------------------------
# 内置测试公钥（RSA-1024，仅公开部分；signin 只做公钥加密，mock 无需私钥）
# ---------------------------------------------------------------------------
DEFAULT_PUBKEY_PEM = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDBC7tcCf+fT9j2EfYfH/CLJezR
rWLs/4OSBrj57z0KDgqI2dvq7/iKq6CZg/Xp2GvS5RufTg2d3L4A2fvW+EMeH5y2
LRkfPxBheozagMaKWfgd+IkdI/CBqvOFS6m/tvzOHnn3fx0TyrhwqId/I3WrHIKX
ZO2F/jOXAwpzw0UKTwIDAQAB
-----END PUBLIC KEY-----"""

# 失败注入可选点（与 signin 实际调用的接口一一对应）。全部**默认关闭**
# （none）——不开时对引擎零变化。故障注入旋钮 = login / signIn / waf /
# nonjson（另有历史点 signPosition，与"假成功"档 login-shallow）：
#   login          POST /code/usersure 回账密错形态（code != s200）；
#   signIn         POST 签到提交回业务失败码（非登录阶段的提交失败）；
#   waf            旧流程真实遇 ydclearance 挑战的落点 GET /iapp7463 回挑战页，
#                  形态对照 `yiban/fyiban/waf.py` 的 looks_like_challenge 真实输入
#                  （window.onload=setTimeout + eval("qo=eval;qo(po);") 双特征 +
#                  Set-Cookie https_ydclearance），保证识别支路按真页走；
#   nonjson        JSON 期望端点（POST usersure/signIn、GET auth/signPosition）回
#                  200 + >2000 字符拦截 HTML——现网"Expecting value:"腿的形状：
#                  长页过 `security.is_waf_blocked` 的 len 短路后在 .json() 处抛。
#   login-shallow  最终认证 GET /base/c/auth/yiban（带 verifyRequest）回
#                  code==0 但**无 data 载荷**的"假成功"——只判 code 的旧登录门会
#                  把它当真成功写会话缓存；带回执判据的客户端必须拒绝。仅打带
#                  verifyRequest 的完成认证步，旧流程入口步（不带 verifyRequest）不受影响。
FAIL_STAGES = ("none", "login", "signIn", "signPosition", "waf", "nonjson",
               "login-shallow")
# 大小写不敏感归一：signIn 这类驼峰名不能被 lower() 直接比较
_STAGE_CANON = {s.lower(): s for s in FAIL_STAGES}

# 签名/验证路由 -> 登录失败注入使用的「登录点」别名
LOGIN_FAIL_PATH = "/code/usersure"

# waf 旋钮的挑战页下发时附带的反爬 cookie（真页在浏览器解出前即随响应下发，
# looks_like_challenge 的第一判据就是它）
WAF_CHALLENGE_SET_COOKIE = "https_ydclearance=mock01clearance02; Path=/; Domain=.yiban.cn"


def waf_challenge_body():
    """`waf` 旋钮的响应体：ydclearance 挑战页形态。

    逐字对照 `yiban/fyiban/waf.py:looks_like_challenge` 的文本特征对；刻意
    **不含**可被 `solve_ydclearance` 提取的挑战函数模板——真实改版/半页场景
    最常触发的就是"识别成挑战但解析失败"，故障注入要喂给分类判据的正是这个
    最难看的形状。
    """
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        "<title>verify</title></head><body>"
        '<script>window.onload=setTimeout("yy(1701368163)", 200);'
        'eval("qo=eval;qo(po);");</script>'
        "</body></html>"
    )


def nonjson_block_body():
    """`nonjson` 旋钮的响应体：>2000 字符的拦截 HTML。

    现网 `Expecting value:` 文案的产生条件是"长拦截页过了按短响应设计的
    is_waf_blocked 长度界、再被 .json() 判死"，故此处刻意超界且非 JSON。
    """
    head = ("<!DOCTYPE html><html><head><title>访问拦截</title></head><body>"
            "<p>您的访问存在风险访问，已被拦截，请联系管理员</p><!--")
    return head + "p" * 2400 + "--></body></html>"


def _now_epoch_ms() -> float:
    return time.time() * 1000.0


def _iso_ms(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]


class MockConfig:
    """延迟/失败注入配置。

    CLI 参数为默认值；若传入 ``config_path``，每次请求热读该 JSON 并覆盖默认值，
    便于压测过程中切换档位（driver 只需重写这个文件）。
    """

    def __init__(self, delay_ms=0, tail_delay_ms=0, tail_every=0,
                 fail_rate=0.0, fail_stage="none", config_path=None):
        self.delay_ms = float(delay_ms or 0)
        self.tail_delay_ms = float(tail_delay_ms or 0)
        self.tail_every = int(tail_every or 0)
        self.fail_rate = float(fail_rate or 0.0)
        self.fail_stage = _STAGE_CANON.get(str(fail_stage or "none").strip().lower(), "none")
        self.config_path = config_path

    def snapshot(self) -> dict:
        """返回本次请求生效的配置（配置文件的字段优先）。"""
        cfg = {
            "delay_ms": self.delay_ms,
            "tail_delay_ms": self.tail_delay_ms,
            "tail_every": self.tail_every,
            "fail_rate": self.fail_rate,
            "fail_stage": self.fail_stage,
        }
        path = self.config_path
        if path:
            try:
                with open(path, encoding="utf-8") as f:
                    disk = json.load(f)
                if isinstance(disk, dict):
                    # 只接受已知键，忽略陌生键，避免写坏配置把 mock 打崩
                    for k in cfg:
                        if k in disk and disk[k] is not None:
                            cfg[k] = disk[k]
            except (OSError, ValueError, TypeError):
                pass  # 配置不可读时退回默认档，不影响服务
        try:
            cfg["delay_ms"] = max(0.0, float(cfg["delay_ms"]))
            cfg["tail_delay_ms"] = max(0.0, float(cfg["tail_delay_ms"]))
            cfg["tail_every"] = int(cfg["tail_every"])
            cfg["fail_rate"] = min(1.0, max(0.0, float(cfg["fail_rate"])))
        except (TypeError, ValueError):
            cfg.update({"delay_ms": 0.0, "tail_delay_ms": 0.0,
                        "tail_every": 0, "fail_rate": 0.0})
        if cfg["fail_stage"] not in FAIL_STAGES:
            cfg["fail_stage"] = "none"
        return cfg

class MockState:
    """跨请求共享计数（线程安全）。"""

    def __init__(self, log_path=None):
        self._lock = threading.Lock()
        self.total = 0
        self.by_path: dict[str, int] = {}
        self.by_host: dict[str, int] = {}
        self.injected = 0
        self.inflight = 0
        self.max_inflight = 0
        self.req_seq = 0
        # 记账对平用：total = 已处理条数；durable = 成功落 JSONL 条数；log_errors =
        # 落盘失败条数。三者满足 total == durable + log_errors（含 log_path 时），
        # 一旦 log_errors>0 说明有请求处理了却没被驱动读到（旁路/丢日志），测不得。
        self.durable = 0
        self.log_errors = 0
        self.t0 = time.time()
        self.log_path = log_path

    # ---- 并发计数 ----
    def enter(self) -> int:
        with self._lock:
            self.inflight += 1
            if self.inflight > self.max_inflight:
                self.max_inflight = self.inflight
            return self.inflight

    def leave(self) -> None:
        with self._lock:
            self.inflight = max(0, self.inflight - 1)

    def next_seq(self) -> int:
        with self._lock:
            self.req_seq += 1
            return self.req_seq

    def record(self, host, path, status, delay_s, inflight, injected, outcome):
        now_ms = _now_epoch_ms()
        row = {
            "ts": _iso_ms(now_ms / 1000.0),
            "epoch_ms": int(now_ms),
            "host": host,
            "path": path,
            "status": status,
            "dur_ms": round(delay_s * 1000.0, 3),
            "inflight": inflight,
            "injected": bool(injected),
            "outcome": outcome,
        }
        # 写行与 durable 计数必须对读者原子：分两段拿锁时，snapshot 会在"行已写、
        # 计数未加"的瞬态看到 total > durable，让对平断言概率性红。文件 I/O 留在
        # 锁内——mock 工具吞吐无碍，对平语义（durable = 确认落盘的行数）不变。
        with self._lock:
            self.total += 1
            self.by_path[path] = self.by_path.get(path, 0) + 1
            self.by_host[host] = self.by_host.get(host, 0) + 1
            if injected:
                self.injected += 1
            if not self.log_path:
                return
            try:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                self.durable += 1
            except OSError:
                # 不再静默吞：计入 log_errors，使 total≠durable 可被驱动侧记账对平检出（MF-68）
                self.log_errors += 1

    def snapshot(self, cfg):
        with self._lock:
            snap = {
                "total": self.total,
                "by_path": dict(self.by_path),
                "by_host": dict(self.by_host),
                "injected": self.injected,
                "inflight": self.inflight,
                "max_inflight": self.max_inflight,
                "req_seq": self.req_seq,
                "durable": self.durable,
                "log_errors": self.log_errors,
                "uptime_s": round(time.time() - self.t0, 1),
            }
        snap["cfg"] = cfg
        return snap


def build_handler(state: MockState, config: MockConfig, pubkey_pem: str,
                  tls_ctx=None, keep_alive=False):
    """构造 HTTP 处理器类（闭包持有 state/config/tls_ctx，便于测试与多端口复用）。

    TLS 在**每个连接的工作线程内**完成握手（见 ``Handler.setup``），而不是在监听
    socket 上包装：后者会让握手串行阻塞在 accept 循环里，高并发下偶发连接被服务端
    提前关闭（客户端表现为 RemoteDisconnected）。

    keep_alive=False（默认）时每个响应带 ``Connection: close``：压测下客户端
    （requests.Session 连接池）不会复用到「服务端恰好已关闭」的连接，避免偶发
    RemoteDisconnected 触发签到重试、污染容量测量；代价是每请求一次 TLS 握手。
    """

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # keep-alive：requests.Session 连接池复用
        server_version = "yiban-mock/1.0"
        timeout = 30  # 半开连接的读超时，防止工作线程永久挂死

        # ---- TLS：在工作线程内对已 accept 的原始连接做服务端握手 ----
        def setup(self):
            if tls_ctx is not None:
                try:
                    self.request = tls_ctx.wrap_socket(self.request, server_side=True)
                except (ssl.SSLError, OSError, ValueError):
                    self._tls_failed = True
                    with contextlib.suppress(OSError):
                        self.request.close()
                    return
            super().setup()

        def handle(self):
            if getattr(self, "_tls_failed", False):
                return
            super().handle()

        def finish(self):
            if getattr(self, "_tls_failed", False):
                return
            super().finish()

        # 关闭默认 stderr 访问日志（结构化 JSONL 已落盘）
        def log_message(self, fmt, *args):
            pass

        # ---- 基础工具 ----
        def _path(self) -> str:
            return self.path.split("?", 1)[0]

        def _host(self) -> str:
            return (self.headers.get("Host", "") or "").split(":")[0]

        def _delay(self, cfg) -> float:
            """固定延迟 + 每 tail_every 个请求追加尾延迟（模拟慢请求）。"""
            d = float(cfg.get("delay_ms", 0) or 0) / 1000.0
            te = int(cfg.get("tail_every", 0) or 0)
            if te > 0:
                n = state.next_seq()
                if n % te == 0:
                    d += float(cfg.get("tail_delay_ms", 0) or 0) / 1000.0
            if d > 0:
                time.sleep(d)
            return d

        def _send(self, body: bytes, code: int, ctype: str, extra_headers=()):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for name, value in extra_headers:
                self.send_header(name, value)
            if not keep_alive:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(body)  # 客户端提前断开（压测 kill）不应抛栈

        def _send_json(self, obj, code=200):
            self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"), code,
                       "application/json; charset=utf-8")

        def _send_html(self, html, code=200):
            self._send(html.encode("utf-8"), code, "text/html; charset=utf-8")

        def _send_redirect(self, location, code=302):
            body = b""
            self.send_response(code)
            self.send_header("Location", location)
            self.send_header("Content-Length", str(len(body)))
            if not keep_alive:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()

        # ---- 失败注入判定 ----
        def _should_fail(self, cfg, stage) -> bool:
            return (cfg.get("fail_stage") == stage
                    and random.random() < float(cfg.get("fail_rate", 0) or 0))

        def _maybe_inject(self, cfg, kind) -> bool:
            """按旋钮形态应答（waf=挑战页 / nonjson=>2000 拦截 HTML）。

            命中即已发响应并返回 True（调用方据此记账 injected）；未命中零副作用
            返回 False——旋钮默认关闭时这里恒 False，现行为零变化。
            """
            if not self._should_fail(cfg, kind):
                return False
            if kind == "waf":
                self._send(waf_challenge_body().encode("utf-8"), 200,
                           "text/html; charset=utf-8",
                           extra_headers=[("Set-Cookie", WAF_CHALLENGE_SET_COOKIE)])
            else:
                self._send(nonjson_block_body().encode("utf-8"), 200,
                           "text/html; charset=utf-8")
            return True

        # ---- HTTP 入口 ----
        def do_GET(self):
            cfg = config.snapshot()
            inflight = state.enter()
            try:
                d = self._delay(cfg)
                p = self._path()
                host = self._host()
                injected = False
                # 运维/探测端点不计入记账，避免驱动侧对 /__stats 的轮询自扰
                is_ops = p in ("/__stats", "/__health")

                if p == "/__stats":
                    self._send_json(state.snapshot(cfg))
                elif p == "/__health":
                    self._send_json({"ok": True})
                elif p == "/code/html":
                    # 登录链第 1 步：返回带 key / page_use 的登录页。
                    # 若 URL 带 ?restored=1 也可模拟会话仍有效的 302（可选）。
                    html = (
                        "<!DOCTYPE html><html><head><title>login</title></head><body>"
                        '<form id="loginform" method="post" action="/code/usersure">'
                        '<input type="text" name="oauth_uname"/>'
                        '<input type="password" name="oauth_upwd"/>'
                        '<input type="hidden" id="key" value="%s"/>'
                        "</form>"
                        "<script>var page_use = 'mockpageuse12345';</script>"
                        "</body></html>"
                    ) % pubkey_pem
                    self._send_html(html)
                elif p == "/iframe/index":
                    # 登录链第 3 步：302 + Location 带 verify_request
                    self._send_redirect(
                        "https://api.uyiban.com/base/c/auth/yiban"
                        "?verify_request=mockvreq123&CSRF=mockcsrf"
                    )
                elif p == "/base/c/auth/yiban":
                    # 带 verifyRequest = 完成认证（两条流程的最后一步）；
                    # 不带 = 旧 iOS 流程的第 1 步：取 OAuth 入口 URL（客户端据此再请求）
                    injected = self._maybe_inject(cfg, "nonjson")
                    if not injected:
                        if "verifyRequest" in self.path:
                            if self._should_fail(cfg, "login-shallow"):
                                # 假成功（shallow success）：code==0 但签发回执 data
                                # 载荷缺失——只判 code 的旧登录门会误认成功
                                injected = True
                                self._send_json({"code": 0,
                                                 "msg": "mock shallow success (no receipt)"})
                            else:
                                self._send_json({"code": 0, "data": {}, "msg": ""})
                        else:
                            self._send_json({"code": 0, "data": {
                                "Data": "https://oauth.yiban.cn/code/html"
                                        "?client_id=95626fa3080300ea"
                                        "&redirect_uri=https://f.yiban.cn/iapp7463"}})
                elif p == "/iapp7463":
                    # 旧 iOS 流程第 4 步的落地页：再跳一次，令牌在下一跳的 Location 里。
                    # waf 旋钮的真实注入点——两条流程里只有这里会被
                    # looks_like_challenge 检查（protocol.py 的 ydclearance 支路）
                    injected = self._maybe_inject(cfg, "waf")
                    if not injected:
                        self._send_redirect(
                            "https://c.uyiban.com/iframe/index?act=iapp7463")
                elif p == "/nightAttendance/student/index/signPosition":
                    injected = self._should_fail(cfg, "signPosition")
                    if injected:
                        self._send_json({"code": 1, "msg": "mock injected signPosition failure"})
                    else:
                        injected = self._maybe_inject(cfg, "nonjson")
                        if not injected:
                            self._send_position()
                else:
                    self._send_json({"code": 404, "msg": "not found"}, code=404)
                if not is_ops:
                    state.record(host, p, self._last_status(), d, inflight, injected, "ok")
            finally:
                state.leave()

        def do_POST(self):
            cfg = config.snapshot()
            inflight = state.enter()
            try:
                d = self._delay(cfg)
                p = self._path()
                host = self._host()
                injected = False
                # 读请求体（保持 keep-alive 连接帧完整；usersure 还要按体区分两条流程）
                body = b""
                try:
                    n = int(self.headers.get("Content-Length", 0) or 0)
                    if n:
                        body = self.rfile.read(n)
                except (ValueError, OSError):
                    pass

                if p == "/code/usersure":
                    # 登录链第 2 步：成功标志默认是 code == "s200"（KillYiBan 流程）；
                    # 旧 iOS 流程（scope 为 "1,2,3,4,"）的成功标志是响应里的 reUrl
                    injected = self._should_fail(cfg, "login")
                    if injected:
                        self._send_json({"code": "e001", "msgCN": "mock injected login failure"})
                    elif self._maybe_inject(cfg, "nonjson"):
                        injected = True
                    elif b"scope=1%2C2%2C3%2C4%2C" in body:
                        self._send_json({"reUrl": "https://f.yiban.cn/iapp7463"})
                    else:
                        self._send_json({"code": "s200", "msgCN": ""})
                elif p == "/nightAttendance/student/index/signIn":
                    injected = self._should_fail(cfg, "signIn")
                    if injected:
                        self._send_json({"code": 1, "msg": "mock injected signIn failure"})
                    elif self._maybe_inject(cfg, "nonjson"):
                        injected = True
                    else:
                        self._send_json({"code": 0, "data": {"Id": "1", "Msg": "ok"}})
                else:
                    self._send_json({"code": 404, "msg": "not found"}, code=404)
                state.record(host, p, self._last_status(), d, inflight, injected, "ok")
            finally:
                state.leave()

        # BaseHTTPRequestHandler 在 send_response 时才确定状态码；记录用
        def _last_status(self) -> int:
            return getattr(self, "_status_code", 200)

        def send_response(self, code, message=None):
            self._status_code = code
            super().send_response(code, message)

        def _send_position(self):
            """签到链第 1 步：返回点位与宽松时间窗口（保证测试期永远在窗口内）。"""
            now = int(time.time())
            self._send_json({
                "code": 0,
                "msg": "",
                "data": {
                    "Msg": "",
                    "Position": [{
                        "Name": "MockTask",
                        "Address": "MockAddr",
                        "Points": ["121.40,31.20", "121.50,31.20",
                                   "121.50,31.30", "121.40,31.30"],
                    }],
                    # 前后各留 1 小时，短窗口压测也不会因跨秒被判窗口外
                    "Range": {"StartTime": now - 3600, "EndTime": now + 3600},
                },
            })

    return Handler


class V4Server(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # 压测下大量短连接：请求队列调大，避免 accept backlog 丢连接
    request_queue_size = 128


class V6Server(V4Server):
    address_family = socket.AF_INET6


def create_servers(host="127.0.0.1", port=443, ipv6_host="::1",
                   cert=None, key=None, state=None, config=None,
                   pubkey_pem=None, enable_ipv6=True, keep_alive=False):
    """创建（但不启动）mock 服务器列表；port=0 时返回实际绑定端口。

    返回 ``(servers, state, config)``；调用方负责在后台线程里 serve_forever。
    无 cert/key 时不启用 TLS（仅测试用，正式压测应始终带证书）。
    """
    state = state or MockState()
    config = config or MockConfig()
    pubkey_pem = pubkey_pem or DEFAULT_PUBKEY_PEM
    tls_ctx = None
    if cert and key:
        tls_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls_ctx.load_cert_chain(cert, key)
    handler = build_handler(state, config, pubkey_pem, tls_ctx, keep_alive=keep_alive)
    servers = []

    s4 = V4Server((host, port), handler)
    actual_port = s4.server_address[1]
    servers.append(s4)

    if enable_ipv6:
        try:
            s6 = V6Server((ipv6_host, actual_port), handler)
            servers.append(s6)
        except OSError:
            # 无 IPv6 栈的环境（部分容器/CI）静默跳过，IPv4 回环仍可用
            pass
    return servers, state, config


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="mock_yiban.py",
        description="假易班 HTTPS 服务（回环压测专用；绝不连接真实易班）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--host", default="127.0.0.1",
                    help="IPv4 回环监听地址（默认仅回环）")
    ap.add_argument("--port", type=int, default=443, help="监听端口")
    ap.add_argument("--ipv6-host", default="::1", help="IPv6 回环监听地址")
    ap.add_argument("--no-ipv6", action="store_true", help="不监听 IPv6 回环")
    ap.add_argument("--keep-alive", action="store_true",
                    help="启用 HTTP keep-alive（默认关闭：每请求新连接，压测更稳）")
    ap.add_argument("--no-tls", action="store_true", help="明文 HTTP（仅本地自测；压测勿用）")
    ap.add_argument("--cert", default="", help="服务器证书 PEM（TLS 必需）")
    ap.add_argument("--key", default="", help="服务器私钥 PEM（TLS 必需）")
    ap.add_argument("--pubkey-file", default="",
                    help="登录页内嵌的 RSA 公钥 PEM 文件（缺省用内置测试公钥）")
    ap.add_argument("--delay-ms", type=float, default=0.0, help="每请求固定人工延迟（毫秒）")
    ap.add_argument("--tail-delay-ms", type=float, default=0.0, help="尾延迟追加毫秒数")
    ap.add_argument("--tail-every", type=int, default=0,
                    help="每 N 个请求追加一次尾延迟（<=0 关闭）")
    ap.add_argument("--fail-rate", type=float, default=0.0, help="失败注入概率 0~1")
    ap.add_argument("--fail-stage", default="none", choices=list(FAIL_STAGES),
                    help="故障注入旋钮（默认 none=全关，不开零变化）：login=登录端点回"
                         "账密错形态；signIn=签到提交回业务失败；signPosition=拉任务失败；"
                         "waf=旧流程挑战落点 GET /iapp7463 回 ydclearance 挑战页"
                         "（形态对照 yiban/fyiban/waf.py 识别输入）；nonjson=JSON 期望"
                         "端点回 200+超长拦截 HTML（现网 Expecting value: 形状）；"
                         "login-shallow=最终认证回 code==0 但无 data 载荷的假成功"
                         "（只判 code 的旧登录门会误写会话缓存，带回执判据必须拒绝）。"
                         "作用域为全局按端点路由；亦可经 --config 热读场景声明运行中切换")
    ap.add_argument("--config", default="",
                    help="热读配置 JSON 路径（字段同上方参数，可运行中切换档位）")
    ap.add_argument("--log", default="", help="逐请求 JSONL 落盘路径（缺省不落盘）")
    ap.add_argument("--ready-file", default="",
                    help="启动完成后写入一行 ready（供驱动等待就绪）")
    args = ap.parse_args(argv)

    if not args.no_tls and not (args.cert and args.key):
        print("错误：未提供 --cert/--key；如确认仅本地自测可加 --no-tls", file=sys.stderr)
        return 2

    pubkey_pem = DEFAULT_PUBKEY_PEM
    if args.pubkey_file:
        try:
            with open(args.pubkey_file, encoding="utf-8") as f:
                pubkey_pem = f.read().strip()
        except OSError as e:
            print(f"错误：读取公钥失败: {e}", file=sys.stderr)
            return 2

    config = MockConfig(
        delay_ms=args.delay_ms, tail_delay_ms=args.tail_delay_ms,
        tail_every=args.tail_every, fail_rate=args.fail_rate,
        fail_stage=args.fail_stage, config_path=args.config or None,
    )
    state = MockState(log_path=args.log or None)

    try:
        servers, state, _ = create_servers(
            host=args.host, port=args.port, ipv6_host=args.ipv6_host,
            cert=None if args.no_tls else args.cert,
            key=None if args.no_tls else args.key,
            state=state, config=config, pubkey_pem=pubkey_pem,
            enable_ipv6=not args.no_ipv6, keep_alive=args.keep_alive,
        )
    except OSError as e:
        print(f"错误：绑定失败（需要 root 才可绑 443）: {e}", file=sys.stderr)
        return 2

    port = servers[0].server_address[1]
    scheme = "http" if args.no_tls else "https"
    addrs = ", ".join("%s://%s:%d" % (scheme, s.server_address[0], s.server_address[1])
                      for s in servers)
    print(f"mock listening {addrs}", flush=True)
    if args.ready_file:
        try:
            with open(args.ready_file, "w", encoding="utf-8") as f:
                f.write(f"{port}\n")
        except OSError:
            pass

    for s in servers[1:]:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for s in servers:
            with contextlib.suppress(Exception):
                s.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
