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

本模块同时是**执行体身份串的唯一口径**：身份串（写进 `sign_claims.owner`）的构造与
解析都在这里，避免"写入一处、解析另一处"各写一份字符串而漂移。当前身份串形如
`worker-3@{主机名}` / `fallback@{主机名}` / `single@{主机名}`——**跨重启稳定**
（不含进程号与启动时刻），前端据此把"执行体"当成跨重启不变的界面对象。
身份串含主机名，属部署信息——**任何接口/日志都不得回原串**，只回角色与槽位序号
（`role_label`）。

**槽位名为什么必须稳定、以及它的安全边界**（改这里之前先读完这两条）：

1. **想要的行为**：名字不再随进程号/启动时刻变化 ⇒ 同一槽位重启后**立刻**认领自己
   上一轮的在飞账号（`try_claim` 对"同一个 owner"是可重入的），不用干等 900s 租约
   过期。别的执行体（包括崩溃后换了进程的那个）owner 不同名，该等租约还得等。
2. **代价**：**同一个槽位名不得有两台机器同时跑**。同名进程会互相认领对方的在飞账号，
   那就是"两个进程同时登录同一账号"——本设计的第一红线。`@{主机名}` 后缀保证跨主机
   不同名；**同一台机器上的同名进程并存由既有锁挡住**（并行执行体持全局锁
   `signin-run.lock`、兜底持 `signin-run.lock.fallback`，见 `yiban/engine/workers.py`）。
   故**不要**去掉这两个锁，也**不要**把 `@{主机名}` 后缀去掉。

`parse_owner` **同时认识新旧两种格式**：旧格式（含 `:workers:` / `fallback-` / `exec-`）
在库里还有 14 天保留期的存量记录，必须照旧判得出来。
"""
import os
import socket

from yiban.security import url_desc

DIRECT = ""

#: 三类角色的环境变量名（前端/文档/测试都引用这里，避免各写一份字符串）
ENV_SINGLE = "YIBAN_PROXY"
ENV_WORKER_LIST = "YIBAN_PROXY_LIST"
ENV_FALLBACK = "YIBAN_PROXY_FALLBACK"

ROLE_SINGLE = "single"
ROLE_WORKER = "worker"
ROLE_FALLBACK = "fallback"
#: 角色判不出来时的取值（历史遗留串、空串）。照实回，不猜。
ROLE_UNKNOWN = "unknown"

#: 并行执行体身份的中缀（**旧格式**）：`{主机名}:workers:{进程号}:w{序号}`。
#: 新格式已改为稳定槽位名（见 `worker_owner`），本常量只为解析存量记录而留。
IDENT_WORKER_INFIX = ":workers:"
#: 兜底常驻执行体的身份前缀（**旧格式**）。**与单执行体区分开**正是本前缀存在的理由——
#: 此前兜底沿用 `exec-`，库里分不出"兜底"与"单执行体"。
IDENT_FALLBACK_PREFIX = "fallback-"
#: 单执行体身份前缀（**旧格式**）：`exec-{主机名}:{进程号}:{启动时刻}`。
IDENT_SINGLE_PREFIX = "exec-"

#: 稳定槽位名里主机后缀的分隔符：`worker-3@host`。**不得去掉**（跨主机唯一性靠它）。
OWNER_HOST_SEP = "@"
#: 稳定槽位名的角色前缀/名字（跨重启不变；不含进程号、不含启动时刻）。
OWNER_WORKER_PREFIX = "worker-"
OWNER_FALLBACK_NAME = "fallback"
OWNER_SINGLE_NAME = "single"


def _owner_host(hostname=None):
    """槽位名里的主机后缀：省略时取本机名（`socket.gethostname()`）。"""
    return socket.gethostname() if hostname is None else hostname


def worker_owner(index, hostname=None):
    """并行执行体身份（**唯一构造处**）：`worker-{序号}@{主机名}`。

    名字**跨重启稳定**（同一台机器上同一槽位永远同名），故重启后立即认领自己上一轮
    的在飞账号，不必等租约过期——这是想要的：进程重启是常态，不该因此换一张脸。
    **代价与红线**（与模块 docstring 的两条安全说明同一件事，此处再钉一遍）：
    名字稳定 ⇒ 同一槽位名**不得有两台机器同时跑**（同名会互相认领在飞账号 →
    两个进程同时登录同一账号）。`@主机名` 后缀保证跨主机不同名，同机同名并存由既有的
    全局锁 `signin-run.lock`（兜底为 `signin-run.lock.fallback`）挡住——
    **不要去掉锁，也不要把后缀去掉**。

    `hostname` 省略时取本机名（`socket.gethostname()`）；显式传入只为测试与
    "父进程代子进程构造"。
    """
    return f"{OWNER_WORKER_PREFIX}{index}{OWNER_HOST_SEP}{_owner_host(hostname)}"


def fallback_owner(hostname=None):
    """兜底常驻执行体身份：`fallback@{主机名}`。

    与并行执行体同名纪律：**跨重启稳定**（同槽位重启后立即接手自己上一轮的在飞账号），
    代价是同一槽位名不得两台机器同时跑（跨主机靠 `@主机名` 区分，同机靠
    `signin-run.lock.fallback` 挡住）。详见 `worker_owner` 与模块 docstring。
    """
    return f"{OWNER_FALLBACK_NAME}{OWNER_HOST_SEP}{_owner_host(hostname)}"


def single_owner(hostname=None):
    """单执行体身份：`single@{主机名}`。

    与 `worker_owner` / `fallback_owner` 同一套稳定名字纪律（跨重启不变；同槽位名不得
    两台机器同时跑，跨主机靠 `@主机名` 区分）。详见 `worker_owner`。
    """
    return f"{OWNER_SINGLE_NAME}{OWNER_HOST_SEP}{_owner_host(hostname)}"


def _parse_stable_owner(text):
    """解析新的稳定槽位名；不是该格式返回 None（交给旧格式分支）。"""
    name, sep, host = text.rpartition(OWNER_HOST_SEP)
    if not (sep and name and host):
        return None                     # 没有 @ / @ 前后为空 → 不是稳定名
    if name.startswith(OWNER_WORKER_PREFIX) or name == OWNER_WORKER_PREFIX.rstrip("-"):
        digits = name[len(OWNER_WORKER_PREFIX):]
        index = int(digits) if digits.isdigit() else None
        return {"role": ROLE_WORKER, "index": index, "label": role_label(ROLE_WORKER, index)}
    if name == OWNER_FALLBACK_NAME:
        return {"role": ROLE_FALLBACK, "index": None, "label": role_label(ROLE_FALLBACK)}
    if name == OWNER_SINGLE_NAME:
        return {"role": ROLE_SINGLE, "index": None, "label": role_label(ROLE_SINGLE)}
    return None


def _worker_index(owner):
    """从**旧格式**并行执行体身份里取序号；取不到（历史串/被改写）返回 None。"""
    tail = owner.rsplit(":", 1)[-1]
    if not tail.startswith("w"):
        return None
    digits = tail[1:]
    return int(digits) if digits.isdigit() else None


def parse_owner(owner):
    """把身份串解析成 `{"role", "index", "label"}`（判不出即 `unknown`）。

    **新旧两种格式都认**（旧记录仍在库里、保留期 14 天，不能因为改了写入格式就读不懂）：

    - 新（稳定槽位名，`worker_owner` / `fallback_owner` / `single_owner` 产出）：
      `worker-3@host` → worker + index 3；`fallback@host` → fallback；`single@host` → single；
    - 旧（`:workers:` / `fallback-` / `exec-`）：含 `:workers:` 且尾部 `w{i}` → worker +
      index；`fallback-` 开头 → fallback；`exec-` 开头 → single；
    - 其余（空串、历史遗留的无前缀串）→ `unknown`，**照实回而不猜**——老数据里兜底与
      单执行体同前缀，本来就无法追溯。
    """
    text = (owner or "").strip()
    stable = _parse_stable_owner(text)
    if stable is not None:
        return stable
    if IDENT_WORKER_INFIX in text:
        index = _worker_index(text)
        return {"role": ROLE_WORKER, "index": index, "label": role_label(ROLE_WORKER, index)}
    if text.startswith(IDENT_FALLBACK_PREFIX):
        return {"role": ROLE_FALLBACK, "index": None, "label": role_label(ROLE_FALLBACK)}
    if text.startswith(IDENT_SINGLE_PREFIX):
        return {"role": ROLE_SINGLE, "index": None, "label": role_label(ROLE_SINGLE)}
    return {"role": ROLE_UNKNOWN, "index": None, "label": role_label(ROLE_UNKNOWN)}


def role_label(role, index=None):
    """角色的中文标签（前端直接显示，不必自己拼文案）。"""
    if role == ROLE_WORKER:
        return f"并行执行体 #{index + 1}" if isinstance(index, int) else "并行执行体"
    if role == ROLE_FALLBACK:
        return "兜底常驻执行体"
    if role == ROLE_SINGLE:
        return "单执行体"
    return "未标注（旧数据）"


def parse_list(raw):
    """解析 `YIBAN_PROXY_LIST`：逗号或空白分隔，**保留空位**（空位=该执行体直连）。

    只去首尾空白、**不过滤空元素**——否则"第 3 个执行体直连"这种配置无法表达。
    注意：末尾多写一个逗号会多出一个空位（`"a,"` = 执行体 0 用 a、执行体 1 直连），
    这是刻意的字面语义，比"悄悄丢掉"更容易向部署者解释。
    """
    if raw is None:
        return []
    return [item.strip() for item in raw.replace(",", "\n").split("\n")]


#: 原始串里的段分隔符：**只有逗号**（与 `parse_list` 的切分口径逐字一致——空白不切段，
#: 只在取值时被 strip）。写回时按同一字符连接，故未改动的段与分隔符逐字不变。
SLOT_SEP = ","


def replace_slot(raw, index, value):
    """把原始串 `raw` 的**第 index 段**换成 `value`，其余段**逐字保留**。

    写单段出口的接口用它：前端只改一个执行体的出口，就绝不能让别段的写法被顺手
    规范化——段的顺序、段内的原样写法、空位（"这个执行体直连"）都是部署者写下的意图，
    重排一次就可能把别人段的代理凭据清成空、静默退回直连。

    切分口径与 `parse_list` **逐字一致**（按逗号切、不按空白切，故"第几段"在两处同号）；
    段数不足 `index + 1` 时**用空段补齐**，故 `replace_slot("a", 2, "c") == "a,,c"`。
    返回新串；`raw` 为 None 按空串处理（= 只有一段空段）。
    """
    fields = (raw or "").split(SLOT_SEP)
    while len(fields) <= index:
        fields.append("")
    fields[index] = value
    return SLOT_SEP.join(fields)


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
