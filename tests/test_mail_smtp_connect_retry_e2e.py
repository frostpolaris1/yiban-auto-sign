# -*- coding: utf-8 -*-
"""SMTP 连接失败有界重试的 e2e（工单 0eul）。

标签：H · 通知：邮件与推送
覆盖：`yiban/adapters/notifier/mail/transport.py::_send` 的「连接失败」重试门。
    用真 socket 的**最小 SMTP 假服务器**（220 标语 / EHLO + AUTH PLAIN / MAIL /
    RCPT / DATA / QUIT）观测会话数；不引用第三方 SMTP 库。
关键断言（每条都是任何时刻都不该被违反的性质）:
    ① 连接阶段被拒 2 次后接受 ⇒ 同一条目重试 2 次后送达，会话数 3；
    ② 连接一直被拒 ⇒ 最多重试 2 次（会话数 3），按「连接失败」记 warning；
    ③ 认证失败 ⇒ 零重试（会话数 1）；
    ④ DATA 已发出后掉线 ⇒ 零重试（会话数 1），即使失败粗分类仍是「连接失败」。
模拟器等价面（AGENTS.md §32）:
    - 等价：TCP 连接建立/被拒、EHLO、AUTH PLAIN、MAIL/RCPT/DATA 的收发顺序与
      应答码；客户端是标准库 `smtplib.SMTP` 本体。
    - 未测：TLS 握手与证书校验（本用例把 `SMTP_SSL` 换成明文 `smtplib.SMTP` 连本地
      端口，只替换传输层，不改协议层）；正文最终投递到真实 MTA 的全路径。故本文件
      的绿**不构成** TLS 与真实服务商协议证据。
    - 退避等待被打桩（记录调用参数、不真等），否则单次用例要真睡 5s+15s。
依赖：只起本机回环 TCP；不解析外网、不发真实邮件、不落盘。
"""
import smtplib
import socket
import threading
from contextlib import suppress

from yiban.adapters.notifier.mail import config as mail_config
from yiban.adapters.notifier.mail import transport

_ENTRY = {"host": "smtp.example.test", "port": 465,
          "user": "sender@example.test", "pass": "secret"}


class MiniSmtpServer:
    """最小 SMTP 子集：可拒绝前 N 个连接 / 拒认证 / DATA 后掉线。"""

    def __init__(self, *, reject_connections=0, auth_fail=False, drop_after_data=False):
        self.reject_connections = reject_connections
        self.auth_fail = auth_fail
        self.drop_after_data = drop_after_data
        self._lock = threading.Lock()
        self.connections = 0
        self.delivered = 0
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self._stop.set()
        with suppress(OSError):
            self._sock.close()
        self._thread.join(timeout=2)

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        with self._lock:
            self.connections += 1
            reject = self.connections <= self.reject_connections
        if reject:
            # 连接阶段拒绝：TCP 建成后立刻关闭，客户端读不到 220 标语。
            with suppress(OSError):
                conn.close()
            return
        conn.settimeout(5)
        try:
            self._converse(conn)
        except (OSError, ValueError):
            pass
        finally:
            with suppress(OSError):
                conn.close()

    def _converse(self, conn):
        stream = conn.makefile("rwb", buffering=0)

        def say(line):
            stream.write((line + "\r\n").encode("ascii"))

        say("220 mini ESMTP")
        while True:
            raw = stream.readline()
            if not raw:
                return
            line = raw.decode("ascii", "replace").rstrip("\r\n")
            verb, _, _arg = line.partition(" ")
            verb = verb.upper()
            if verb in ("EHLO", "HELO"):
                stream.write(b"250-mini\r\n250-AUTH PLAIN\r\n250 OK\r\n")
            elif verb == "AUTH":
                if self.auth_fail:
                    say("535 5.7.8 Authentication failed")
                else:
                    say("235 2.7.0 Authentication successful")
            elif verb in ("MAIL", "RCPT"):
                say("250 OK")
            elif verb == "DATA":
                say("354 End data with <CR><LF>.<CR><LF>")
                while True:
                    chunk = stream.readline()
                    if not chunk:
                        return
                    if chunk in (b".\r\n", b".\n"):
                        break
                if self.drop_after_data:
                    # DATA 已收下后掉线：不回应答，直接关连接。
                    return
                with self._lock:
                    self.delivered += 1
                say("250 2.0.0 OK")
            elif verb == "QUIT":
                say("221 Bye")
                return
            elif verb == "RSET":
                say("250 OK")
            else:
                say("500 unrecognized")


def _patch_transport(monkeypatch, server):
    """把配置与传输层接到假服务器；退避等待只记录、不真等。"""
    sleeps = []

    def plain_client(host, port=None, timeout=None, context=None):
        # 只替换 TLS 传输层：客户端仍是标准库 smtplib.SMTP 本体。
        return smtplib.SMTP("127.0.0.1", server.port, timeout=timeout)

    monkeypatch.setattr(mail_config, "_read_env_file", lambda: {})
    monkeypatch.setattr(mail_config, "is_enabled", lambda *a, **k: True)
    monkeypatch.setattr(mail_config, "smtp_list", lambda *a, **k: [dict(_ENTRY)])
    monkeypatch.setattr(transport.smtplib, "SMTP_SSL", plain_client)
    monkeypatch.setattr(transport.time, "sleep", sleeps.append)
    return sleeps


def _mailer_logs(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "mailer"]


def test_connect_refused_twice_then_retry_delivers(monkeypatch, caplog):
    """① 前 2 次连接被拒，第 3 次接受 ⇒ 送达；重试次数与退避取值正确。"""
    with MiniSmtpServer(reject_connections=2) as server:
        sleeps = _patch_transport(monkeypatch, server)
        with caplog.at_level("WARNING", logger="mailer"):
            ok = transport._send("主题", "正文", "to@example.test")
    assert ok is True, "连接阶段瞬时失败必须由同条目重试救回"
    assert server.delivered == 1, "同一封只允许送达一次"
    assert server.connections == 3, f"1 次首发 + 2 次重试，实际 {server.connections}"
    assert sleeps == [5, 15], f"退避必须是 5s/15s，实际 {sleeps}"
    assert sum("重试" in m for m in _mailer_logs(caplog)) == 2, "每次重试都要留日志"


def test_persistent_connect_refusal_is_bounded(monkeypatch, caplog):
    """② 连接一直失败 ⇒ 重试不超过 2 次，按「连接失败」收口。"""
    with MiniSmtpServer(reject_connections=10 ** 6) as server:
        sleeps = _patch_transport(monkeypatch, server)
        with caplog.at_level("WARNING", logger="mailer"):
            ok = transport._send("主题", "正文", "to@example.test")
    assert ok is False, "重试用尽必须按未送达收口"
    assert server.connections == 3, f"最多 2 次重试，实际会话 {server.connections}"
    assert sleeps == [5, 15]
    joined = "\n".join(_mailer_logs(caplog))
    assert "连接失败" in joined, "最终失败必须保留失败粗分类"


def test_auth_failure_is_never_retried(monkeypatch, caplog):
    """③ 认证失败是永久错误 ⇒ 零重试。"""
    with MiniSmtpServer(auth_fail=True) as server:
        sleeps = _patch_transport(monkeypatch, server)
        with caplog.at_level("WARNING", logger="mailer"):
            ok = transport._send("主题", "正文", "to@example.test")
    assert ok is False
    assert server.connections == 1, f"认证失败不得重试，实际会话 {server.connections}"
    assert sleeps == []
    assert "认证失败" in "\n".join(_mailer_logs(caplog))


def test_failure_after_data_is_never_retried(monkeypatch, caplog):
    """④ DATA 已发出 ⇒ 不重试（防双发）。失败类型仍是「连接失败」，故只靠类型判会误重试。"""
    with MiniSmtpServer(drop_after_data=True) as server:
        sleeps = _patch_transport(monkeypatch, server)
        with caplog.at_level("WARNING", logger="mailer"):
            ok = transport._send("主题", "正文", "to@example.test")
    assert ok is False
    assert server.connections == 1, f"DATA 后失败不得重试，实际会话 {server.connections}"
    assert sleeps == []
    joined = "\n".join(_mailer_logs(caplog))
    assert "连接失败" in joined, "该失败在类型上仍属连接失败（证明判据是会话阶段，不是异常类型）"
    assert "重试" not in joined, "DATA 阶段失败不得出现任何重试日志"
