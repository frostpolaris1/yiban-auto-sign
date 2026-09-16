# -*- coding: utf-8 -*-
"""出口（代理）分配：**每个执行体都能有自己的出口**，留空则走本机直连。

多执行体并行时，"所有执行体共用一个 IP"会把风控面与带宽面都压在一处；本模块把
"哪个角色用哪个出口"收成一个口径，供以下三类进程共用：

| 角色 | 取值来源 | 说明 |
|------|----------|------|
| `single` | `YIBAN_PROXY` | 单执行体（现状形态），未设=直连 |
| `worker` | `YIBAN_PROXY_LIST[i]` | 第 i 个并行执行体；表里写**空元素**即该执行体直连 |
| `fallback` | `YIBAN_PROXY_FALLBACK`，未设退回 `YIBAN_PROXY` | 兜底常驻执行体；未设=直连 |

分配规则（**可复现、可解释**，部署者按自己的出口数量决定怎么填）：

- `YIBAN_PROXY_LIST="http://a:1,http://b:2,http://c:3"` → 执行体 0/1/2 各用一个；
  执行体数超过表长时**循环取用**（第 4 个执行体用第 1 个出口）；
- 表里留空位表示"这个执行体直连"：`"http://a:1,,http://c:3"` → 执行体 1 直连；
- 三种角色都**允许为空**（= 本机出口），空与非空可以是任意混合。

**脱敏**：代理串可能带 userinfo（`http://user:pass@host:port`），任何进入日志、
接口返回值的地方都必须经 `describe()`——它只回 `scheme://host[:port]`。
"""
import os

from yiban.security import url_desc

DIRECT = ""

#: 三类角色的环境变量名（前端/文档/测试都引用这里，避免各写一份字符串）
ENV_SINGLE = "YIBAN_PROXY"
ENV_WORKER_LIST = "YIBAN_PROXY_LIST"
ENV_FALLBACK = "YIBAN_PROXY_FALLBACK"

ROLE_SINGLE = "single"
ROLE_WORKER = "worker"
ROLE_FALLBACK = "fallback"


def parse_list(raw):
    """解析 `YIBAN_PROXY_LIST`：逗号或空白分隔，**保留空位**（空位=该执行体直连）。

    只去首尾空白、**不过滤空元素**——否则"第 3 个执行体直连"这种配置无法表达。
    注意：末尾多写一个逗号会多出一个空位（`"a,"` = 执行体 0 用 a、执行体 1 直连），
    这是刻意的字面语义，比"悄悄丢掉"更容易向部署者解释。
    """
    if raw is None:
        return []
    return [item.strip() for item in raw.replace(",", "\n").split("\n")]


def resolve(role, index=0, env=None):
    """按角色取出口；未配置返回 `DIRECT`（空串=走本机出口）。

    `index` 只在 `role=worker` 时有意义；表为空（或键未设）时直连。
    """
    env = os.environ if env is None else env
    if role == ROLE_WORKER:
        raw = env.get(ENV_WORKER_LIST, "")
        if not (raw or "").strip():
            # 没配列表 → 退回单执行体那一个出口（"只配 YIBAN_PROXY"的老配置继续可用）
            return env.get(ENV_SINGLE, "").strip()
        items = parse_list(raw)
        return items[index % len(items)].strip()
    if role == ROLE_FALLBACK:
        fallback = env.get(ENV_FALLBACK, "").strip()
        return fallback if fallback else env.get(ENV_SINGLE, "").strip()
    return env.get(ENV_SINGLE, "").strip()


def describe(proxy):
    """出口的可入日志/接口的描述：空=直连，否则只留 `scheme://host[:port]`（去 userinfo）。"""
    proxy = (proxy or "").strip()
    if not proxy:
        return "直连（本机出口）"
    return url_desc(proxy)


def assignments(count, env=None):
    """给 `count` 个并行执行体各算一个出口，返回 `[(index, proxy, 描述)]`。

    供拉起执行体的一方（监督进程 / 容器调度器 / 前端预览）统一使用；
    空出口也照实返回（描述为"直连"），便于在界面与日志里逐个体现在用哪个出口。
    """
    env = os.environ if env is None else env
    out = []
    for i in range(count):
        proxy = resolve(ROLE_WORKER, i, env)
        out.append((i, proxy, describe(proxy)))
    return out
