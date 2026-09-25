#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""**功能**
loadtest 隔离链的「启动即断言」与代理摘除原语（**仅限测试机压测工具链**）。

压测声称全程只打回环 mock，但这条前提此前是软的：子进程用 ``dict(os.environ)``
继承全部 ``*PROXY*`` 键（现网确有 Squid），引擎侧 ``requests.Session()`` 默认
``trust_env=True`` 会经代理解析/出口，唯一的主动探测又在缺省 ``--egress-probe-ip``
下恒走 ``[SKIP]``——三者叠加，代理机上会「打真实易班却报绿」。本模块把隔离改成
fail-closed：任一不变量不满足即拒绝启动（非 0 + 原因）。

## 归属
`scripts/loadtest/` 工具链的唯一隔离断言实现。四个入口（``mock_env`` /
``scale_driver`` / ``concurrency_probe`` / ``capacity_probe``）在 ``main`` 顶部统一
调用本模块，是「单一逻辑收口、多点接线」：入口彼此独立（运维可单独手跑），无法用
一个进程内 chokepoint 覆盖，故每个门都接一次同一份实现。

## 复用
- :func:`assert_no_proxy` / :func:`strip_proxy` —— 进程自身与子进程环境的代理治理；
- :func:`require_egress_probe` —— 出站探测目标必填，取消静默 SKIP；
- :func:`assert_accounting` —— mock 侧记账条数 == 发出条数；
- :func:`loadtest_session` —— loadtest 自建 HTTP 会话，显式 ``trust_env=False``
  （满足 registry 不变量：``grep -rn trust_env scripts/`` 有正向命中）。

## 通信
纯断言/纯函数，除 :func:`loadtest_session`/:func:`mock_recorded_total` 外不触网。
所有拒绝都以 :class:`IsolationError` 抛出，由调用方打印到 stderr 并非零退出。
"""

from __future__ import annotations

import hashlib
import os
import sqlite3

#: 命中该子串（大小写不敏感）的环境变量名一律视为代理相关键
PROXY_MARK = "PROXY"

#: 压测账号归属域：判"这个库像不像压测库"的内容判据（防误指生产库）
LOADTEST_OWNER_SUFFIX = "@mock.invalid"


class IsolationError(RuntimeError):
    """隔离不变量被破坏：调用方据此拒绝启动/判定不通过。"""


def proxy_keys(env=None):
    """返回 env 中所有 ``*PROXY*`` 键（大写排序）；env 缺省取 os.environ。"""
    env = os.environ if env is None else env
    return sorted(k for k in env if PROXY_MARK in k.upper())


def strip_proxy(env):
    """返回去掉全部 ``*PROXY*`` 键的新 dict（构造子进程环境时的主动摘除）。

    ``no_proxy`` 也含 PROXY，一并摘除：requests 在没有其它代理键时本就不走代理，
    摘除 ``no_proxy`` 无副作用，且保证「子进程环境无任何 *PROXY* 键」这一硬口径。
    """
    return {k: v for k, v in env.items() if PROXY_MARK not in k.upper()}


def assert_no_proxy(env=None, source="process"):
    """断言给定环境不含任何 ``*PROXY*`` 键；否则抛错（fail-closed）。"""
    keys = proxy_keys(env)
    if keys:
        raise IsolationError(
            f"隔离失败：{source} 环境存在代理键 {keys}；压测必须直连回环 mock，"
            f"禁止经代理解析/出口。请先 unset 这些变量再运行 loadtest。")
    return True


def require_egress_probe(ip):
    """``--egress-probe-ip`` 必填：空值即拒绝（此前缺省恒走 [SKIP] ⇒ 代理路径必报绿）。"""
    if ip is None or not str(ip).strip():
        raise IsolationError(
            "隔离失败：--egress-probe-ip 必填。缺省空值会跳过主动出站探测，"
            "代理机上的隔离无法自证——请显式给出一个应被拒绝的目标 IP:443。")
    return str(ip).strip()


def _account_owners(db_path):
    """只读取 accounts.owner 列表；库/表缺失返回 None（读不到不等于"像压测库"）。"""
    if not os.path.exists(db_path):
        return []
    try:
        conn = sqlite3.connect(f"file:{os.path.abspath(db_path)}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    try:
        try:
            return [str(r[0] or "") for r in conn.execute("SELECT owner FROM accounts")]
        except sqlite3.Error:
            return None
    finally:
        conn.close()


def loadtest_db_fingerprint(db_path):
    """→ 压测库目标指纹（由库**内容**派生：路径 + 账号数 + 压测账号数 + 会话数）。

    只读，不建库。行数/归属差异让它能区分"压测库"与"误指的生产库"——生产库账号数
    成千且 owner 是真实邮箱，指纹与归属判据都会露出来。
    """
    counts = {"accounts": 0, "session_cache": 0}
    if os.path.exists(db_path):
        for table in counts:
            try:
                conn = sqlite3.connect(f"file:{os.path.abspath(db_path)}?mode=ro",
                                       uri=True, timeout=5)
            except sqlite3.Error:
                continue
            try:
                try:
                    counts[table] = int(
                        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                except sqlite3.Error:
                    counts[table] = 0
            finally:
                conn.close()
    owners = _account_owners(db_path) or []
    mock = sum(1 for o in owners if o.endswith(LOADTEST_OWNER_SUFFIX))
    payload = "loadtest|%s|accounts=%d|session=%d|mock=%d" % (
        os.path.abspath(db_path), counts["accounts"], counts["session_cache"], mock)
    return "LT-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def assert_loadtest_target(db_path, fingerprint):
    """清空会话缓存前的目标门：声明指纹 + 目标像压测库，二者缺一即拒（fail-closed）。

    为什么这道门存在：`clear_session_cache` 对目标库做 `DELETE FROM session_cache`，
    误指生产库会让全部账号下次都走完整登录链——真实重登风暴。故（1）必须显式声明
    指纹（`--db-fingerprint`，先跑一次看打印值）；（2）库内非空账号的 owner 必须全部
    属压测域——两者都按**内容**判，不看路径字符串。返回实际指纹供调用方复用。
    """
    actual = loadtest_db_fingerprint(db_path)
    declared = str(fingerprint or "").strip()
    if not declared:
        raise IsolationError(
            f"清空会话缓存前必须显式声明目标指纹。实际指纹：{actual}；"
            f"确认目标是压测库后，用 --db-fingerprint {actual} 重跑。")
    if declared != actual:
        raise IsolationError(
            f"目标指纹不匹配（声明 {declared} ≠ 实际 {actual}）：目标库可能被换或误指，"
            f"拒绝清空会话缓存。")
    owners = _account_owners(db_path)
    if owners is None or any(not o.endswith(LOADTEST_OWNER_SUFFIX) for o in owners):
        raise IsolationError(
            f"目标库含非压测账号（owner 不以 {LOADTEST_OWNER_SUFFIX} 结尾），"
            f"疑似误指生产库：拒绝清空会话缓存（防真实重登风暴）。")
    return actual


def assert_accounting(issued, recorded):
    """mock 侧记账条数必须等于发出条数；不等 ⇒ 抛错（静默丢日志/旁路即破坏测量可信度）。"""
    issued, recorded = int(issued), int(recorded)
    if issued != recorded:
        raise IsolationError(
            f"记账不平衡：发出 {issued} 条 ≠ mock 记账 {recorded} 条；"
            f"差值意味着有请求未经 mock 落盘（旁路真实出口或静默丢日志），测量不可信。")
    return issued


def loadtest_session(ca=None):
    """loadtest 自建 HTTP 会话：显式 ``trust_env=False``，绝不使用环境/系统代理。

    这是 registry 不变量「``grep -rn trust_env scripts/`` 有正向命中」的落点；
    只作用于 loadtest 侧，不触碰 ``yiban/`` 引擎（引擎代理出口受全局约束保护）。
    """
    import requests  # 延迟导入：纯断言用例无需 requests

    session = requests.Session()
    session.trust_env = False  # 隔离压测：无视 *PROXY*，直连回环 mock
    if ca:
        session.verify = ca
    return session


def mock_recorded_total(base_url, session=None):
    """向 mock 的 ``/__stats`` 取服务端权威记账条数（``total``）。"""
    s = session or loadtest_session()
    resp = s.get(base_url.rstrip("/") + "/__stats", timeout=8)
    resp.raise_for_status()
    return int(resp.json()["total"])
