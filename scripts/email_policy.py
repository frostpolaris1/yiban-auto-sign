#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""邮箱域名可用性审查：注册写入前拦截占位/一次性邮箱域名，保护用户池。

背景：开放注册常收到 example.com、demo.com 等占位域名或 mailinator.com 等
一次性邮箱的注册请求——这类账号收不到任何通知邮件，却持续挤占
YIBAN_MAX_USERS 用户池名额。本模块在注册入口（开放注册 / 管理员自动注册）
对域名做黑白名单审查，命中即在写库前拒绝。

审查顺序（先白后黑）：
1. 白名单（可选）：allowlist 参数非空时仅名单内域名可注册（精确匹配，
   优先级高于黑名单）。web 层来自 .env 的 YIBAN_EMAIL_DOMAIN_ALLOWLIST；
2. 黑名单：
   - 内置保留域名 _RESERVED_DOMAINS（RFC 2606/6761 占位域名 + 常见教程
     占位域名，硬编码兜底：数据文件缺失/损坏时依然拦截）；
   - data 文件 email_blocklist.txt 一次性邮箱域名（与本模块同目录，来源与
     许可见文件头，CC0；放 scripts/data/ 会被根 .gitignore 的 data/ 规则误伤，
     故平置于 scripts/ 下随模块走）；
   - blocklist_extra 参数（web 层来自 .env 的 YIBAN_EMAIL_DOMAIN_BLOCKLIST_EXTRA，
     部署自定义追加，无需改数据文件）。

匹配规则：命中域名本身或其任一祖先域名（子域名关系）即拦截，如名单含
example.com 时 a.b.example.com 一并拦截；伪 TLD（test/invalid/localhost 等）
以裸标签入名单，foo.test、bar.localhost 由祖先匹配覆盖。注意：.local TLD
（mDNS）不在名单——内网部署常用作账号域名，且不构成公网邮箱滥用面。

数据文件带 mtime+size 缓存：替换文件后无需重启进程即可生效（注册为低频
操作，每次 stat 的开销可忽略）。

公开 API：
    review_email(email, allowlist="", blocklist_extra="") -> str | None
通过返回 None；否则返回用户可读的拒绝原因（调用方直接透传给 400 响应）。
"""

import logging
import os
import threading

logger = logging.getLogger(__name__)

# 数据文件：一次性邮箱域名黑名单（与本模块同目录；
# Docker 整目录 COPY scripts/，数据随镜像带走）
_DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "email_blocklist.txt")

# 内置保留/占位域名（硬编码兜底）：
# - RFC 2606/6761 保留：example.com/.net/.org/.edu；裸 TLD test/example/invalid/localhost
# - 常见教程占位域名：demo.com、test.com/.net、foo/bar/baz.com、yourdomain/yoursite.com
# 均不提供真实邮箱服务，注册即挤占用户池，一律拦截（含其子域名）。
_RESERVED_DOMAINS = frozenset({
    "example.com", "example.net", "example.org", "example.edu",
    "demo.com", "test.com", "test.net",
    "foo.com", "bar.com", "baz.com",
    "yourdomain.com", "yoursite.com",
    "test", "example", "invalid", "localhost",
})

# 用户可读拒绝原因（信息分层：不区分命中类别、不回显名单内容，防探测名单边界）
_ERR_BLOCKED = "该邮箱域名不可用于注册，请更换常用邮箱地址"
_ERR_NOT_ALLOWLISTED = "该邮箱域名不在允许注册的范围内，请联系管理员或更换邮箱"

# 数据文件缓存：key（mtime+size）变化才重新解析；锁保证并发注册下只解析一次
_cache_lock = threading.Lock()
_cache = {"key": None, "domains": frozenset()}


def _split_domains(raw):
    """逗号分隔配置串 → 小写域名集合（去空白/空项/@ 前缀）。"""
    if not raw:
        return frozenset()
    return frozenset(p.strip().lower().lstrip("@") for p in raw.split(",") if p.strip())


def _load_blocklist():
    """读取数据文件黑名单；文件缺失/读取失败回退空集合（内置保留名单独立生效）。"""
    try:
        st = os.stat(_DATA_FILE)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return frozenset()
    with _cache_lock:
        if _cache["key"] == key:
            return _cache["domains"]
        domains = set()
        try:
            with open(_DATA_FILE, encoding="utf-8-sig") as f:
                for line in f:
                    d = line.strip().lower()
                    if d.startswith("#"):
                        continue
                    d = d.lstrip(".")  # 兼容 ".tld" 写法：剥前缀按裸 TLD 处理
                    if d:
                        domains.add(d)
        except OSError as e:
            # 留痕（对齐项目"静默失败留痕"惯例）：读失败不阻塞注册，仅当次回退
            logger.warning("邮箱黑名单数据文件读取失败，本进程仅保留内置保留名单: %s", e)
            return frozenset()
        domains = frozenset(domains)
        _cache["key"] = key
        _cache["domains"] = domains
        return domains


def _hit(domains, domain):
    """domain 或其任一祖先域名（子域名关系）在名单中即 True。"""
    labels = domain.split(".")
    return any(".".join(labels[i:]) in domains for i in range(len(labels)))


def review_email(email, allowlist="", blocklist_extra=""):
    """审查邮箱域名可用性。通过返回 None，否则返回用户可读的拒绝原因。

    仅审域名部分；邮箱格式合法性（EMAIL_RE 等）由调用方负责。allowlist
    非空时进入白名单模式（精确匹配，优先于黑名单）；blocklist_extra 追加
    部署自定义黑名单（含子域名匹配）。
    """
    if not email or "@" not in email:
        return None  # 无 @ / 空值属格式问题，交由调用方格式校验处理
    domain = email.rsplit("@", 1)[1].strip().lower()
    if not domain:
        return None
    # 1) 白名单模式：配置非空即生效，名单外一律拒绝（精确匹配，白名单优先）
    allow = _split_domains(allowlist)
    if allow:
        return None if domain in allow else _ERR_NOT_ALLOWLISTED
    # 2) 黑名单：内置保留名单 → 数据文件 → 部署追加（均含子域名匹配）
    if _hit(_RESERVED_DOMAINS, domain):
        return _ERR_BLOCKED
    if _hit(_load_blocklist(), domain):
        return _ERR_BLOCKED
    if _hit(_split_domains(blocklist_extra), domain):
        return _ERR_BLOCKED
    return None
