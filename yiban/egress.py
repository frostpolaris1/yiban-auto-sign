# -*- coding: utf-8 -*-
"""出口（代理）分配：**每个执行体都能有自己的出口**，留空则走本机直连。

多执行体并行时，"所有执行体共用一个 IP"会把风控面与带宽面都压在一处；本模块把
"哪个角色用哪个出口"收成一个口径，供以下三类进程共用：

| 角色 | 取值来源 | 说明 |
|------|----------|------|
| `single` | `YIBAN_PROXY` | 单执行体，未设=直连（不在清单模型里，始终读这个键） |
| `worker` | 清单里该**槽位**那一行；无清单时 `YIBAN_PROXY_LIST[i]` | 第 i 个并行执行体；旧表里写**空元素**即该执行体直连 |
| `fallback` | 清单里的兜底行；无清单时 `YIBAN_PROXY_FALLBACK`，未设退回 `YIBAN_PROXY` | 兜底常驻执行体；未设=直连 |

分配规则（**可复现、可解释**）：执行体数超过表长时**循环取用**（第 4 个执行体用
第 1 个出口）；表里留空位表示"这个执行体直连"；三种角色都允许为空（= 本机出口）。

`resolve` 只负责取值（空串就是空串）。**把空串落成"真的直连"是 `apply_egress` 的职责**：
执行体的环境从父进程复制而来，出口键（`ENV_SINGLE`）常常已经带着值坐在那里，所以空出口
也必须写进环境压掉它。生产代码里把出口交给执行体的地方只有 `apply_egress` 一处。

**脱敏**：代理串可能带 userinfo（`http://user:pass@host:port`），任何进入日志、
接口返回值的地方都必须经 `describe()`——它只回 `scheme://host[:port]`。

本模块同时是**出口标识的唯一口径**：限速桶按**出口**计数（见
`yiban/engine/token_bucket.py` 模块头），持久键与预算分母都由这里的函数产出：
`egress_identity(proxy)` 给出**出口标识**——空（直连）归一为 `DIRECT_EGRESS`，非空取
`scheme://host[:port]` 的寻址形态（去 userinfo，IPv6 保留方括号）。同一出口的多个执行体
因此共用一个持久键；`outlet_executor_count(identity)` 数出该出口上**真会拉起**的执行体数 n
（worker 行 + **开关打开的**兜底行；停用行不计），供限速器把出口级速率均分成每进程份额
（合计 ≤ λ）。桶键会进日志与 `egress_state` 表，故 `egress_identity` **不回 userinfo**（凭据）。

本模块同时是**执行体身份串的唯一口径**：身份串的构造与解析都在这里，避免"写入一处、
解析另一处"各写一份字符串而漂移。两种形态分别是：

- **稳定槽位名**（`worker-3@{主机名}` / `fallback@{主机名}` / `single@{主机名}`）：
  跨重启不变（不含进程号与启动时刻），前端据此把"执行体"当成跨重启不变的界面对象，
  槽位号/角色也由它解析出来；
- **运行时身份**（`runtime_owner(稳定名)`，形如 `single@{主机名}:{进程号}:{代次}`）：
  写进 `sign_claims.owner` 的那个串。同机的两个进程（cron 全量与网页手动）若共用稳定名，
  在领取池看来就是"自己人"，会互相放行同一账号——运行时身份用进程号+代次把这一点消掉。

两种形态都含主机名，属部署信息——**任何接口/日志都不得回原串**，只回角色与槽位序号
（展示面 `role_label`、日志面 `owner_tag`；后者多带一个可选的轮次序号）。

**稳定槽位名为什么必须稳定、以及它的安全边界**（改这里之前先读完这两条）：

1. **想要的行为**：名字不随进程号/启动时刻变化 ⇒ 界面上"这是第几个执行体、什么角色"
   跨重启稳定，槽位号也不跳。**代价是它不再等于"谁做了多少"能自动归拢**：库里存的是
   运行时身份（每进程一串），按 owner 分组的归属视图会把同一槽位的不同代次分别列出。
   **也要注意它不再等于"重启后立刻认领自己的在飞账号"**：领取池的重入必须出示上一代
   领取时拿到的 epoch（`try_claim` 的 `epoch` 参数），而重启后的新进程没有它，只能等
   租约过期或由心跳/回收机制处置。
2. **代价**：**同一个槽位名不得有两台机器同时跑**。`@{主机名}` 后缀保证跨主机不同名；
   同一台机器上的同名进程并存由既有锁挡住（并行执行体持全局锁 `signin-run.lock`、
   兜底持 `signin-run.lock.fallback`，见 `yiban/engine/workers.py`）。故**不要**去掉
   这两个锁，也**不要**去掉 `@{主机名}` 后缀。

`parse_owner` **同时认识新旧两种格式**：旧格式（含 `:workers:` / `fallback-` /
`exec-`）在库里还有 14 天保留期的存量记录，必须照旧判得出来；带运行时后缀的串按
`@` 右侧整体当主机段解析，角色与序号不受影响。

**执行体清单（`YIBAN_EXECUTORS`）**：本模块同时是清单模型（槽位/类型/每行出口）的
唯一口径。旧口径是"一个数量（`YIBAN_WORKERS`）+ 一整条逗号列表（`YIBAN_PROXY_LIST`）
+ 单独兜底出口（`YIBAN_PROXY_FALLBACK`）"，表达不了"停用某一行""删中间行不重排"，
故改用**单键 JSON 数组**一行一个执行体：

- `worker`：并行执行体（可有 N 行，占"建议值分母"）；
- `fallback`：兜底常驻执行体（最多 1 行）；
- `disabled`：停用行（**保留出口**、不参与分配、不拉起、不计入建议值，但仍占槽位）。

槽位号 **只增不复用**（追加 = 当前最大 + 1；删行不重排；最大 63）。旧三键保留一个
版本周期：清单缺失时回退读旧键（`legacy_rows` 与旧 `resolve`/`assignments` 逐字等价），
首次读到旧键且清单键缺失时由调用方一次性迁移写回（`manifest_state` 给出 `needs_write`；
写 .env 复用既有唯一口径，不在本模块里另造一套写盘）。
"""
import datetime
import json
import logging
import os
import socket
from urllib.parse import urlsplit

from yiban.infra import env_io
from yiban.security import url_desc

#: 告警通道：与 `scripts/child_env.py`、`yiban/engine/*` 同一条 "yiban"（root 之下），
#: 故容器 stdout 与 web 按天日志面都能收到这里越界配置的那一行 WARNING。
logger = logging.getLogger("yiban")

DIRECT = ""

#: 直连（出口为空）的**出口标识**（限速持久键与预算分母的标识）：全部直连执行体归到这一个
#: 出口。为什么归到一个固定串而不是每个执行体各一个空串：限速按出口计数，空出口彼此是
#: 同一个物理出口（本机 IP），必须同归一处，否则该 IP 的预算会随执行体数重复发放（工单 2cwd）。
DIRECT_EGRESS = "direct"

#: `_outlet_addressing` 对不可解析输入回的哨兵。它**不是**出口标识（`is_outlet_identity`
#: 显式排除），否则两个坏代理会被当成同一出口、且展示层把它打出去（工单 2cwd 复审 F4）。
UNPARSEABLE = "<无法解析>"

#: 三类角色的环境变量名（前端/文档/测试都引用这里，避免各写一份字符串）
ENV_SINGLE = "YIBAN_PROXY"
ENV_WORKER_LIST = "YIBAN_PROXY_LIST"
ENV_FALLBACK = "YIBAN_PROXY_FALLBACK"
#: 兜底常驻执行体的开关（1/true/on/yes=开；未设=关）。只设开关不拉进程；但"声明开启"决定
#: `outlet_executor_count` 是否把兜底计入**限速预算分母**（工单 2cwd 复审 F1）。
ENV_FALLBACK_ENABLE = "YIBAN_FALLBACK_ENABLE"

#: 执行体清单键名：**单键 JSON 数组**，每项 `{"slot": 0, "type": "worker", "proxy": "..."}`
#: （`proxy` 空串=直连）。取代旧三键；旧键保留一个版本周期以便回退读取。
ENV_MANIFEST = "YIBAN_EXECUTORS"
#: 旧口径的并行执行体数量键（迁移来源之一，也是"清单缺失时"的回退读取口径）
ENV_WORKER_COUNT = "YIBAN_WORKERS"
#: 本进程的执行体身份键（监督进程在派发点注入）。**不是配置键**：它只作"本进程有没有
#: 被派发过"的判据（见 `effective_worker_count`），网页/`.env` 都不写它。
ENV_EXECUTOR_ID = "YIBAN_EXECUTOR_ID"
#: 旧三键全量：任一存在且清单缺失 ⇒ 需要一次性迁移写回
LEGACY_KEYS = (ENV_WORKER_COUNT, ENV_WORKER_LIST, ENV_FALLBACK)

#: 执行体行的类型
TYPE_WORKER = "worker"
TYPE_FALLBACK = "fallback"
TYPE_DISABLED = "disabled"
TYPES = (TYPE_WORKER, TYPE_FALLBACK, TYPE_DISABLED)

#: 槽位下标上限：`YIBAN_WORKERS` 合法域 1~64 → 下标 0~63。
SLOT_MAX = 63
#: `YIBAN_WORKERS` 的合法域：**这两个常量是全仓唯一的一份数**（census P1-3）。
#: `WORKERS_MAX` 与 `SLOT_MAX` 对应（1 个执行体占槽位 0，64 个占满 0~63）；
#: `WORKERS_MIN` 同时是"未设/非法/越界"时的回退值（= 单执行体）。
#: 宿主 `run.sh` 不再自己抄一份上下限，而是经 `"$PY" -c` 从这里取（取不到 ⇒ 响亮告警 +
#: 按单执行体，绝不静默换成第二份数）；Web 写入校验（`web/routes/settings_api.py`）与
#: 本模块的读取（`legacy_worker_count`）也引用同两个常量。
#: 旧名 `WORKER_COUNT_MAX` 已并入 `WORKERS_MAX`：一枚事实一个名字。
WORKERS_MIN = 1
WORKERS_MAX = SLOT_MAX + 1

ROLE_SINGLE = "single"
ROLE_WORKER = "worker"
ROLE_FALLBACK = "fallback"
#: 角色判不出来时的取值（历史遗留串、空串）。照实回，不猜。
ROLE_UNKNOWN = "unknown"

#: **旧格式**身份串的识别片段（只用于解析库里的存量记录，新写入不再产出）：
#: `{主机名}:workers:{进程号}:w{序号}` 的中缀、`fallback-` 前缀、`exec-` 前缀。
IDENT_WORKER_INFIX = ":workers:"
IDENT_FALLBACK_PREFIX = "fallback-"
IDENT_SINGLE_PREFIX = "exec-"

#: 稳定槽位名里主机后缀的分隔符：`worker-3@host`。**不得去掉**（跨主机唯一性靠它）。
OWNER_HOST_SEP = "@"
#: 稳定槽位名的角色前缀/名字（跨重启不变；不含进程号、不含启动时刻）。
OWNER_WORKER_PREFIX = "worker-"
OWNER_FALLBACK_NAME = "fallback"
OWNER_SINGLE_NAME = "single"

#: `YIBAN_PROXY_LIST` 的段分隔符：**只有逗号**（与 `parse_list` 的切分口径逐字一致——
#: 空白不切段，只在取值时被 strip）。写回时按同一字符连接，故未改动的段逐字不变。
SLOT_SEP = ","


def worker_owner(index, hostname=None):
    """并行执行体身份（**唯一构造处**）：`worker-{序号}@{主机名}`。

    名字**跨重启稳定**（同一台机器上同一槽位永远同名），是界面"执行体"、槽位号与
    各类持久化键的口径；但名字相同**不再等于可立即重入**：领取池的重入必须出示上一代
    领取时拿到的 epoch，而重启后的新进程没有它，只能等租约过期或由心跳/回收机制处置
    后才重新认领自己的在飞账号（见模块 docstring）。**代价与红线见模块 docstring**：
    名字稳定 ⇒ 同一槽位名不得有两台机器同时跑。`hostname` 省略时取本机名
    （`socket.gethostname()`）；显式传入只为测试与"父进程代子进程构造"。
    """
    return f"{OWNER_WORKER_PREFIX}{index}{OWNER_HOST_SEP}{_owner_host(hostname)}"


def fallback_owner(hostname=None):
    """兜底常驻执行体身份：`fallback@{主机名}`（同 `worker_owner` 的稳定名字纪律：
    跨重启稳定的只是名字与持久化键，重入仍须出示上一代 epoch，不等同于重启即接手）。"""
    return f"{OWNER_FALLBACK_NAME}{OWNER_HOST_SEP}{_owner_host(hostname)}"


def single_owner(hostname=None):
    """单执行体身份：`single@{主机名}`（同 `worker_owner` 的稳定名字纪律：名字稳定
    只服务界面与持久化键，重入不复用同名，须出示当前 epoch）。"""
    return f"{OWNER_SINGLE_NAME}{OWNER_HOST_SEP}{_owner_host(hostname)}"


#: 本进程的"代次"标记：进程启动时刻（HMMSS）。进程号会被系统复用（重启后拿到上一轮
#: 的号并非不可能），配上启动时刻才能让每一次进程启动都得到独一无二的身份串。
_RUNTIME_GEN = datetime.datetime.now().strftime("%H%M%S")


def runtime_owner(stable, pid=None, gen=None):
    """把稳定槽位名扩成**这一代进程**的运行时身份：`{稳定名}:{进程号}:{代次}`。

    稳定名（`single@{主机名}` 等）跨重启不变，是给界面上的"执行体"与槽位号用的；但它
    不含进程号，于是**同一台机器上的两个进程会拿到同一个身份串**。领取池的冲突判据按
    owner 串认"自己人"，同名即被互相认成自己人 ⇒ cron 全量与网页手动同时放行同一账号、
    两次真实登录（本项目第一红线）。运行时身份把进程号与代次拼进去消除这种同名：
    它只用于**写库的租约身份**，展示侧仍按 `parse_owner` 折成角色/槽位（接口不回原串）。

    `pid` / `gen` 可显式传入，只为测试与"父进程代子进程构造"。
    """
    return (f"{stable}:{os.getpid() if pid is None else pid}"
            f":{gen or _RUNTIME_GEN}")


def stable_owner(owner):
    """把持有者串折回**稳定槽位名**（`worker-3@host` / `fallback@host` / `single@host`）。

    持有者列存的是运行时身份 `{稳定名}:{进程号}:{代次}`（`runtime_owner`），也可能只是
    计划行写的裸稳定名。本函数是 `runtime_owner` 的**反向**：去掉尾部的运行时段，只留
    `名字@主机`。主机名不含 `:`，故按首个 `:` 切断主机段即可（`worker-3@host:1:2` →
    `worker-3@host`；裸 `worker-3@host` 原样返回）。判不出稳定名（空串、旧格式
    `:workers:` / `fallback-` / `exec-`、缺主机段的历史串）返回 `None`——调用方据此
    **不做豁免**，与库内既有"旧格式不进名册"的口径一致。

    为什么需要它：租约回收的豁免名册要能判断"某个持有者是不是**本执行体自己的稳定槽位**"
    （含其历史代次的运行时串）。只比运行时串会比不出自己的旧代次，于是本进程重启前遗留的
    行永远回收不到（"重启即卡死"）。
    """
    text = (owner or "").strip()
    if _parse_stable_owner(text) is None:
        return None
    name, _sep, host = text.rpartition(OWNER_HOST_SEP)
    return f"{name}{OWNER_HOST_SEP}{host.split(':', 1)[0]}"


def parse_owner(owner):
    """把身份串解析成 `{"role", "index", "label"}`（判不出即 `unknown`）。

    新旧两种格式都认（旧记录仍在库里，保留期 14 天，不能因为改了写入格式就读不懂）：
    新格式见 `worker_owner` / `fallback_owner` / `single_owner`；旧格式含
    `:workers:`（尾部 `w{i}` 为序号）/ `fallback-` / `exec-`；其余（空串、历史遗留的
    无前缀串）→ `unknown`，**照实回而不猜**——老数据里兜底与单执行体同前缀，
    本来就无法追溯。
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
    """角色的中文标签（前端直接显示，不必自己拼文案）。

    `fallback` 的标签是「故障转移」——**唯一一处**，故执行体清单行标签与账号页
    「上次实领」的角色列同时生效（用户 2026-09-17 定：前端不再自己覆盖显示，
    免得两页各有一份口径）。行的自定义名见 `name` 字段（设了就优先显示它）。
    """
    if role == ROLE_WORKER:
        return f"并行执行体 #{index + 1}" if isinstance(index, int) else "并行执行体"
    if role == ROLE_FALLBACK:
        return "故障转移"
    if role == ROLE_SINGLE:
        return "单执行体"
    return "未标注（旧数据）"


def owner_tag(owner, round_no=None):
    """执行体身份串 → **日志归因前缀**：`[worker-3]` / `[fallback r7]` / `[single]` / `[unknown]`。

    只回角色与槽位序号（`parse_owner` 的既有口径）——**不含主机名**：身份原串
    （`worker-3@{主机名}`）带部署信息，任何接口/日志都不得回串（见模块 docstring）。
    本函数是那个"只回角色与槽位"的**唯一渲染处**：写日志的一方都调它，免得各拼一份
    前缀而漂移（多执行体并发写同一个按天日志文件，行与行之间只有这个标记能分辨是谁说的）。

    `round_no` 只给**同一个进程内跑多轮**的兜底常驻：一轮一个序号，排障时能答出"第几轮"。
    定时轮/手动轮一个进程只跑一轮，不传。
    """
    parsed = parse_owner(owner)
    role, index = parsed["role"], parsed["index"]
    if role == ROLE_WORKER and isinstance(index, int):
        name = f"{OWNER_WORKER_PREFIX}{index}"
    elif role == ROLE_FALLBACK:
        name = OWNER_FALLBACK_NAME
    elif role == ROLE_SINGLE:
        name = OWNER_SINGLE_NAME
    else:
        name = ROLE_UNKNOWN
    return f"[{name} r{round_no}]" if round_no is not None else f"[{name}]"


def parse_list(raw):
    """解析 `YIBAN_PROXY_LIST`：逗号或空白分隔，**保留空位**（空位=该执行体直连）。

    只去首尾空白、**不过滤空元素**——否则"第 3 个执行体直连"这种配置无法表达。
    注意：末尾多写一个逗号会多出一个空位（`"a,"` = 执行体 0 用 a、执行体 1 直连），
    这是刻意的字面语义，比"悄悄丢掉"更容易向部署者解释。
    """
    if raw is None:
        return []
    return [item.strip() for item in raw.replace(",", "\n").split("\n")]


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

    `index` 只在 `role=worker` 时有意义（**清单模式下就是槽位号**）。取值顺序：

    1. **清单优先**：`YIBAN_EXECUTORS` 可解析时，`worker` 取该槽位那一行的出口
       （`disabled` 行也算"这个槽位有自己的出口"——停用只表示不参与分配，出口仍保留）；
       `fallback` 取清单里的兜底行；
    2. **回退旧口径**：清单缺失/非法、或该槽位不在清单里（含单执行体形态）→ 沿用旧三键
       （`YIBAN_PROXY_LIST` 下标取值、空位=直连、未配列表退回 `YIBAN_PROXY`）——
       升级期的行为逐字不变。

    单执行体（`single`）不在清单模型里，始终读 `YIBAN_PROXY`。
    """
    env = os.environ if env is None else env
    rows = parse_manifest((env or {}).get(ENV_MANIFEST))
    if rows is not None:
        if role == ROLE_WORKER:
            row = row_by_slot(rows, index)
            if row is not None and row["type"] in (TYPE_WORKER, TYPE_DISABLED):
                return row["proxy"]
        elif role == ROLE_FALLBACK:
            row = fallback_row(rows)
            if row is not None:
                return row["proxy"]
    # 旧口径（清单缺失/非法/槽位不在清单里）
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


def egress_identity(proxy):
    """出口串 → **出口标识**（限速持久键的**唯一口径**，见模块 docstring）。

    空（直连）→ `DIRECT_EGRESS`；非空 → `scheme://host[:port]` 的寻址形态（去 userinfo）。
    **不回 userinfo**：标识进日志与 `egress_state` 表，凭据不得外流。同址不同凭据的代理因此
    归一到同一标识（同一出口）。

    为什么不用原串：原串带 `user:pass@`，进库/进日志即泄凭据（见模块 docstring 的脱敏红线）；
    而且同址两个账号会各得一个键，那个出口的预算会被重复发放。

    为什么不用 `describe` 的口径：它会丢掉 IPv6 的方括号（`http://[::1]:3128` →
    `http://::1:3128`），该形态不可再解析，`is_outlet_identity` 与展示层都会误判。故本函数
    自带一份**保留方括号**的寻址实现（`_outlet_addressing`），与 `describe` 的差异只在 IPv6。
    """
    text = (proxy or "").strip()
    return DIRECT_EGRESS if not text else _outlet_addressing(text)


def is_outlet_identity(identity):
    """该串是否是一个**出口标识**（`egress_identity` 的产物）。

    判据是**幂等**：`direct`，或 `egress_identity` 作用后逐字不变（含 IPv6 的方括号形态）。
    旧执行体身份键（`worker-0@主机名` / `fallback@主机名` 等，改键前的存量 `egress_state`
    行）与 `_outlet_addressing` 的哨兵 `UNPARSEABLE` 判假——展示层据此**不回声**，也避免把
    两个坏代理当成同一出口（工单 2cwd 复审 F4）。
    """
    text = str(identity or "").strip()
    if not text or text == UNPARSEABLE:
        return False
    return text == DIRECT_EGRESS or text == _outlet_addressing(text)


def outlet_label(identity):
    """出口标识 → **展示/日志形态**：直连回 `describe` 的中文描述；其余经 `_outlet_addressing`
    再脱敏一次并**保留 IPv6 方括号**（与标识同形，工单 2cwd 复审 F5）；不是出口标识的旧键与
    哨兵回「已弃用」且**不回声**。日志与 `egress` 子命令的人类可读汇总用它。
    """
    text = str(identity or "").strip()
    if text == DIRECT_EGRESS:
        return describe("")
    if is_outlet_identity(text):
        return _outlet_addressing(text)
    return "已弃用（旧执行体身份键）"


def fallback_enabled(env=None):
    """兜底常驻执行体是否**声明的开启**（`YIBAN_FALLBACK_ENABLE`：1/true/on/yes=开；未设=关）。

    "声明开启"不等于"在跑"，但它是 `outlet_executor_count` 是否把兜底计入预算分母的口径
    （工单 2cwd 复审 F1：只数**真会拉起**的执行体）。真值判定复用全项目唯一开关口径
    `env_io.parse_env_flag`，不另立字面量表。
    """
    env = os.environ if env is None else env
    raw = (env or {}).get(ENV_FALLBACK_ENABLE, "")
    return env_io.parse_env_flag(raw, default=False, key=ENV_FALLBACK_ENABLE, log=logger)


def outlet_executor_count(identity, env=None):
    """该出口上**真会拉起**的执行体数 n（停用行不计）。限速预算按它均分。

    `identity` 是**出口标识**（`egress_identity` 的产物，调用方给 `_Ctx.egress` 或探针的
    出口键）。数出与 `identity` 相同者：

    - **worker**：清单的 `worker` 行（`worker_rows`，即拉起列表）；清单缺失/非法时按旧键的
      `legacy_worker_proxies`（与监督进程的拉起口径同源）。停用行不在内。
    - **兜底**：仅当 `YIBAN_FALLBACK_ENABLE` 声明开启时计入（`fallback_enabled`）；开关未设
      =关（registry 缺省 false）⇒ **默认部署 n=1**，出口速率不被静默减半（F1）。

    返回值至少 1（本进程自己），故份额 = 出口级速率 / n 恒可算。**运行期不得改清单**：
    n 是启动快照，改了要重启执行体（漂移由 `EgressLimiter.persist` 周期告警，见 F3）。
    """
    env = os.environ if env is None else env
    target = str(identity or "").strip()   # `identity` 已是出口标识（调用方给 `egress_identity` 的产物）
    rows = parse_manifest((env or {}).get(ENV_MANIFEST))
    if rows is not None:
        proxies = [r["proxy"] for r in worker_rows(rows)]
    else:
        proxies = list(legacy_worker_proxies(env))
    if fallback_enabled(env):
        proxies.append(resolve(ROLE_FALLBACK, 0, env))
    return max(1, sum(1 for p in proxies if egress_identity(p) == target))


def _outlet_addressing(url):
    """出口标识的寻址形态：`scheme://host[:port]`，**去 userinfo**，host 为 IPv6 时保留
    方括号。不可解析返回哨兵 `UNPARSEABLE`。

    与 `describe` 口径的唯一差异是 IPv6 保留方括号（`urlsplit.hostname` 会剥掉方括号，直接拼回
    就得到不可再解析的串）。两者都只回寻址信息，不回 query/userinfo。
    """
    try:
        parts = urlsplit(str(url))
    except ValueError:
        return UNPARSEABLE
    host = parts.hostname
    if not host:
        return UNPARSEABLE
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    return f"{parts.scheme}://{host}" + (f":{port}" if port else "")


def apply_egress(env, role, index=0):
    """把该角色的出口写进 `env`，返回写进去的出口串（**"空=直连"的唯一落实点**）。

    `resolve` 的契约到"空串就是空串"为止，落地由本函数负责——**生产代码里只许这一处
    把出口写进环境**（守卫：`tests/test_egress_direct_applied.py`）。

    为什么空值也必须写：执行体的环境从父进程复制而来。`run.sh` 无条件逐行 export 整份
    `.env`，`scripts/child_env.build_child_env` 又用 `.env` 压过进程环境，于是出口键
    （`ENV_SINGLE`）往往已经带着一个值坐在子进程环境里。只在出口非空时写入，空出口那一格
    等于"什么都不做"：管理员显式配下的"直连"被静默吞掉，执行体照用父级代理出网，而
    `describe()` 与界面显示的仍是"直连（本机出口）"——两侧都看不见偏差（ba-p08-01）。

    为什么写空串、而不是删键：子进程里 `yiban/engine/probe._egress_env` 先铺 `.env`、
    再让进程环境压过它（那是每槽位出口管控的既有口径）。删掉键就查不到"进程环境"这一项，
    它会退回 `.env` 里的那个值，同一条直连配置在那条路上被吞第二次。留一个空串才同时
    压得住"继承来的进程环境"与"`.env` 补缺"两个来源；空串对每个读者都等于没配代理
    （`yiban/client.py` 与 `resolve` 都 strip 后判假）。
    """
    proxy = resolve(role, index, env)
    env[ENV_SINGLE] = proxy
    return proxy


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


# ---------------------------------------------------------------------------
# 执行体清单：解析 / 序列化 / 迁移 / 行操作（纯函数，不碰文件与网络）
# ---------------------------------------------------------------------------
def parse_manifest(raw):
    """解析 `YIBAN_EXECUTORS` → 行列表（按 slot 升序）；**缺失或非法返回 None**。

    返回 None 让调用方回退读旧三键。**键在但 JSON 坏时也回 None，但调用方不得据此
    覆盖写回**：坏掉的清单多半能手工修，静默按旧键重建会把停用行与槽位结构一起抹掉
    （是否写回由 `manifest_state` 的 `needs_write` 决定，不是这里）。
    单项非法（缺 slot / 类型不认识 / 槽位超范围）只跳过该项，其余行照旧；同 slot
    重复时后写者胜（与 .env 解析"后写覆盖先写"同口径）。
    """
    if raw is None or not str(raw).strip():
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, list):
        return None
    by_slot = {}
    for item in data:
        row = _normalize_row(item)
        if row is not None:
            by_slot[row["slot"]] = row
    return _sorted_rows(by_slot.values())


def dump_manifest(rows):
    """行列表 → 单键 JSON 串（**紧凑、无换行**；只落 slot/type/proxy[/name] 四个字段）。

    紧凑写法是有意的：这个值要整条写进 `.env` 的一行，宿主 `run.sh` 逐行解析并
    `export`；不引入空格与换行最不容易在别处被 strip 或折行。紧凑 JSON 不含任何
    被 `str.splitlines()` 认作行边界的字符，故可直接过 `has_line_break` 校验。

    `name`（行的自定义名）**只在非空时落键**：旧清单与迁移产物里没有它，落键与否
    都不影响解析；这样"迁移后的清单"与历史形态逐字一致，不会因为加了这个字段就让
    既有对比测试全部改断言。
    """
    payload = []
    for r in _sorted_rows(rows):
        item = {"slot": int(r["slot"]), "type": r["type"],
                "proxy": str(r.get("proxy") or "")}
        name = clean_name(r.get("name"))
        if name:
            item["name"] = name
        payload.append(item)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def row_by_slot(rows, slot):
    """按槽位取行；不存在返回 None。"""
    return next((r for r in rows if r["slot"] == slot), None)


def worker_rows(rows):
    """**拉起列表**：清单里 `type=worker` 的行（按 slot 升序）。停用与兜底都不在内。"""
    return [r for r in _sorted_rows(rows) if r["type"] == TYPE_WORKER]


def fallback_row(rows):
    """兜底行（`type=fallback`）；清单里没有则 None。"""
    return next((r for r in _sorted_rows(rows) if r["type"] == TYPE_FALLBACK), None)


def next_slot(rows):
    """追加新行要用的槽位号 = 现有最大 + 1（空清单 = 0）。**只增不复用**：删行不重排。

    纯函数只能算到"最大值 + 1"：删掉**当前最大**那一行之后，下一次追加会拿到刚空出来的号。
    要让"用过的号删掉也不再复用"成立，必须看**领取历史**（谁真的拿这个号跑过）——那是
    数据层的事，故由追加接口算出下限、经 `add_row(..., min_slot=…)` 传进来。
    只想占住槽位、不删行的话，把类型改成 `disabled` 即可。
    """
    return max((r["slot"] for r in rows), default=-1) + 1


def launch_rows(env=None):
    """清单的拉起列表：`type=worker` 的行；**清单缺失/非法返回 None**（调用方回退旧口径）。

    `env` 省略时读进程环境变量（与 `resolve` 同口径：拉起执行体的进程读的是环境变量，
    网页侧则显式传 `.env` 那份配置）。
    """
    env = os.environ if env is None else env
    rows = parse_manifest(env.get(ENV_MANIFEST))
    return None if rows is None else worker_rows(rows)


def launch_slots(env=None):
    """拉起列表的槽位号列表（语义同 `launch_rows`；None = 清单不可用，回退旧口径）。"""
    rows = launch_rows(env)
    return None if rows is None else [r["slot"] for r in rows]


def effective_worker_count(env=None):
    """本轮**生效**的并行执行体数（容量预检按它缩放总阈值）。唯一取法，三级：

    1. 无 `YIBAN_EXECUTOR_ID` ⇒ 本进程没被派发过，生效数恒 1。`runner` 的派发条件保证
       这件事：清单在场、或 `--workers N>1`，二者有一个才会拉子进程；没派发就只有一个
       执行体（本进程自己）。故这一条不是估计，是派发语义的推论。
    2. 清单拉起列表在场 ⇒ 取其行数（与 `runner` 的数法逐字相同：子进程继承监督进程的
       同一份清单，停用/兜底行不在内）。
    3. 清单缺失/非法 ⇒ 旧 worker 口径 `legacy_worker_count`：宿主 `run.sh` 决定
       `--workers N` 读的就是这个键，合法域与回退也同源（越界/非法 ⇒ 1）。

    与派发同源是硬要求：`--workers N` 被监督进程从子进程 argv 剔除，子进程只能从环境
    复原生效数；数错一处，容量预检的告警就在多执行体下少算 N 倍。
    """
    env = os.environ if env is None else env
    if not str(env.get(ENV_EXECUTOR_ID, "") or "").strip():
        return 1
    rows = launch_rows(env)
    if rows is not None:
        return max(1, len(rows))
    return max(1, legacy_worker_count(env))


def executor_label(rtype, slot=None):
    """清单行的中文标签（前端直接显示，不必自己拼文案）。

    `disabled` 的标签就是「已停用」——标签口径已冻结给前端（见接口契约），
    槽位号在 `slot` 字段里，故不再拼进标签。
    """
    if rtype == TYPE_WORKER:
        return role_label(ROLE_WORKER, slot)
    if rtype == TYPE_FALLBACK:
        return role_label(ROLE_FALLBACK)
    if rtype == TYPE_DISABLED:
        return "已停用"
    return role_label(ROLE_UNKNOWN)


def legacy_worker_count(env):
    """`YIBAN_WORKERS` 的执行体数：合法域 `WORKERS_MIN~WORKERS_MAX`，越界回退下限。

    越界与非整数**告警后回退 `WORKERS_MIN`（单执行体），不再静默钳到上限**：钳位会把
    65 读成 64，而宿主 `run.sh` 对同一个值降级单执行体——同一份配置两种结果。选"回退下限"
    不选"钳到上限"：越界的数不是管理员确认过的意图，而每多一个执行体就多一路真实登录
    （风控面）；少开有告警看得见，多开看不见。口径与 `schedule._env_int` 一致
    （超范围 ⇒ 回退默认 + WARNING）。
    整数判据取 ASCII 十进制（`raw.isascii() and raw.isdigit()`）：`int("1_0")`、`int("+5")`
    这类 Python 侧宽容写法会让 bash 的正则与本函数再次分叉，故这里不宽容。
    未设/空白是默认形态（单执行体），按 `WORKERS_MIN` 静默返回。
    """
    raw = str((env or {}).get(ENV_WORKER_COUNT, "")).strip()
    if not raw:
        return WORKERS_MIN
    if not (raw.isascii() and raw.isdigit()):
        logger.warning("配置 %s=%r 非法（须为 %d~%d 的整数），按单执行体执行",
                       ENV_WORKER_COUNT, raw, WORKERS_MIN, WORKERS_MAX)
        return WORKERS_MIN
    n = int(raw)
    if not WORKERS_MIN <= n <= WORKERS_MAX:
        logger.warning("配置 %s=%s 超出范围 [%d, %d]，按单执行体执行",
                       ENV_WORKER_COUNT, n, WORKERS_MIN, WORKERS_MAX)
        return WORKERS_MIN
    return n


def legacy_worker_proxies(env):
    """旧口径下各并行执行体（下标 0..n-1）的出口——与 `resolve(ROLE_WORKER, i)` 同源。

    列表键存在且非空白时按逗号取段、不足**循环取用**、空位=直连；列表键缺失/全空白时
    退回 `YIBAN_PROXY`（"只配了单出口的老配置"继续可用）。
    """
    env = env or {}
    n = legacy_worker_count(env)
    raw = env.get(ENV_WORKER_LIST, "")
    if (raw or "").strip():
        items = parse_list(raw)
        return [items[i % len(items)].strip() for i in range(n)]
    return [(env.get(ENV_SINGLE, "") or "").strip()] * n


def legacy_fallback_proxy(env):
    """旧口径下兜底执行体的出口：`YIBAN_PROXY_FALLBACK`，未设退回 `YIBAN_PROXY`。"""
    env = env or {}
    fb = (env.get(ENV_FALLBACK, "") or "").strip()
    return fb if fb else (env.get(ENV_SINGLE, "") or "").strip()


def legacy_rows(env):
    """旧三键 → 清单行（迁移口径）：worker 行占 0..n-1，兜底行紧随其后。

    **与旧 `resolve`/`assignments` 逐字等价**：worker 行的出口就是旧口径逐个算出的串
    （含"空位=直连""列表不足循环取用""未配列表退回 `YIBAN_PROXY`"），兜底行的出口就是
    `resolve(ROLE_FALLBACK)` 的值。n=64 占满 0..63 时**不产出兜底行**（旧配置里兜底
    本就不占槽位，此时 `fallback.*` 继续按旧键口径解析，行为不变）。
    """
    proxies = legacy_worker_proxies(env)
    rows = [{"slot": i, "type": TYPE_WORKER, "proxy": p} for i, p in enumerate(proxies)]
    if len(rows) <= SLOT_MAX:
        rows.append({"slot": len(rows), "type": TYPE_FALLBACK,
                     "proxy": legacy_fallback_proxy(env)})
    return rows


def manifest_state(env):
    """读清单：返回 `(rows, needs_write)`。

    | 情形 | rows | needs_write |
    |------|------|-------------|
    | 清单可解析（含空数组） | 清单 | False |
    | 清单键在但 JSON 非法 | 旧三键口径 | **False**（不覆盖写回，留给人工修） |
    | 清单缺失/空白且旧三键任一存在 | 旧三键迁移结果 | **True**（调用方写回） |
    | 清单缺失且无任何旧键 | 默认行（1 并行 + 1 兜底，均直连） | False（新部署不长出配置键） |

    写回只由调用方做，且必须复用既有的 .env 唯一写入口径（读-改-写在同一把锁内）。
    """
    env = env or {}
    raw = env.get(ENV_MANIFEST, "")
    rows = parse_manifest(raw)
    if rows is not None:
        return rows, False
    if str(raw or "").strip():
        return legacy_rows(env), False
    return legacy_rows(env), any(k in env for k in LEGACY_KEYS)


def add_row(rows, rtype, proxy, min_slot=None, name=""):
    """追加一行：`slot = max(现有最大 + 1, min_slot)`。

    `min_slot` 是**槽位下限**，供调用方把"领取历史里用过的号"并进来（数据层算，见
    `next_slot` 的说明）：删掉当前最大行后，本次追加就用它跳过那个已用过的号。
    `name` 是行的自定义名（空 = 用后端标签，不落 `name` 键）。
    非法类型 / 兜底重复 / 槽位超上限抛 ValueError。
    """
    _validate_type(rtype)
    if _fallback_taken(rows, None, rtype):
        raise ValueError("兜底执行体最多只能有一行")
    slot = max(next_slot(rows), int(min_slot or 0))
    if slot > SLOT_MAX:
        raise ValueError(f"槽位已达上限 {SLOT_MAX}，无法再追加执行体")
    row = {"slot": slot, "type": rtype, "proxy": _clean_proxy(proxy)}
    clean = clean_name(name)
    if clean:
        row["name"] = clean
    return _sorted_rows([*rows, row])


def update_row(rows, slot, rtype=None, proxy=None, name=None):
    """改一行（`None` = 不改该字段）；槽位不存在抛 ValueError，其余行逐字保留。

    改类型时同样受"兜底最多 1 行"约束；改 `disabled` 只改类型，**出口原样保留**。
    `name`：`None` = 不改，空串 = 清掉自定义名（回到后端标签）。
    """
    row = row_by_slot(rows, slot)
    if row is None:
        raise ValueError(f"槽位 {slot} 不在执行体清单里")
    new_type = row["type"] if rtype is None else rtype
    _validate_type(new_type)
    if _fallback_taken(rows, slot, new_type):
        raise ValueError("兜底执行体最多只能有一行")
    updated = {"slot": slot, "type": new_type,
               "proxy": row["proxy"] if proxy is None else _clean_proxy(proxy)}
    new_name = clean_name(row.get("name")) if name is None else clean_name(name)
    if new_name:
        updated["name"] = new_name
    return _sorted_rows([updated if r["slot"] == slot else r for r in rows])


def delete_row(rows, slot):
    """删一行（**不重排**其余槽位）；槽位不存在抛 ValueError。"""
    if row_by_slot(rows, slot) is None:
        raise ValueError(f"槽位 {slot} 不在执行体清单里")
    return [r for r in rows if r["slot"] != slot]


def apply_legacy_config(rows, env):
    """把旧三键口径应用到现有清单（旧"整条写入"接口用；清单存在时维护它，别让旧键写入变成空写）。

    - **保留已存在的 worker 槽位**（按 slot 升序与新的执行体下标一一对应），槽位不重排；
    - 执行体数变少 → 多出来的 worker 行删除；变多 → 用**当前最大槽位 + 1** 追加
      （只增不复用，且不会占用本次操作里刚删掉的行号）；
    - 兜底行按旧键更新出口；清单里没有兜底行则在尾部追加一行；
    - `disabled` 行原样保留（旧键表达不了它们，但没有理由因为一次旧接口写入就丢掉）。
    槽位已满（>63）抛 ValueError。
    """
    env = env or {}
    proxies = legacy_worker_proxies(env)
    ordered = _sorted_rows(rows)
    worker_slots = [r["slot"] for r in ordered if r["type"] == TYPE_WORKER]
    # 前 len(worker_slots) 个执行体沿用已存在的槽位（顺序即槽位升序），槽位不重排
    # zip 的短侧即"能对应上的执行体数"，故显式 strict=False（多出来的走末尾追加）
    kept = {slot: proxy for slot, proxy in zip(worker_slots, proxies, strict=False)}
    out = []
    for r in ordered:
        if r["type"] == TYPE_WORKER:
            if r["slot"] in kept:
                out.append({"slot": r["slot"], "type": TYPE_WORKER,
                            "proxy": kept[r["slot"]]})
            continue                       # 多余的 worker 行：本次写入要求减少执行体数
        out.append(dict(r))
    # 新增的 worker 行用"本次操作前的最大槽位 + 1"起步：只增不复用，也不会占用刚删掉的号
    cursor = max([r["slot"] for r in ordered], default=-1) + 1
    for proxy in proxies[len(worker_slots):]:
        if cursor > SLOT_MAX:
            raise ValueError(f"槽位已达上限 {SLOT_MAX}，无法增加并行执行体")
        out.append({"slot": cursor, "type": TYPE_WORKER, "proxy": proxy})
        cursor += 1
    # 兜底行：更新出口；清单里没有兜底行则追加一行（旧口径总要能表达兜底出口）
    fallback_proxy = legacy_fallback_proxy(env)
    if any(r["type"] == TYPE_FALLBACK for r in out):
        for r in out:
            if r["type"] == TYPE_FALLBACK:
                r["proxy"] = fallback_proxy
    elif cursor <= SLOT_MAX:
        out.append({"slot": cursor, "type": TYPE_FALLBACK, "proxy": fallback_proxy})
    return _sorted_rows(out)


# ---- 内部实现 ----
def _sorted_rows(rows):
    """按槽位升序（清单的输出顺序**只有这一种**，前端与写回都据此稳定）。"""
    return sorted(rows, key=lambda r: int(r["slot"]))


def _normalize_row(item):
    """单项校验/归一：非法返回 None（调用方跳过）。布尔是 int 的子类，须显式排除。

    `name`（行的自定义名）缺省/空/非法一律**不落键**——等价于"没设名，用后端标签"。
    解析侧刻意宽容（截断超长、剥控制字符），因为手写的清单不该因为一个名字丢整行。
    """
    if not isinstance(item, dict):
        return None
    slot = item.get("slot")
    if isinstance(slot, bool) or not isinstance(slot, int) or not (0 <= slot <= SLOT_MAX):
        return None
    rtype = item.get("type")
    if rtype not in TYPES:
        return None
    row = {"slot": slot, "type": rtype, "proxy": _clean_proxy(item.get("proxy"))}
    name = clean_name(item.get("name"))
    if name:
        row["name"] = name
    return row


#: 行自定义名的长度上限（字符数）。给前端输入的硬边界：超长直接 400，不静默截断。
NAME_MAX_LEN = 32


def clean_name(raw):
    """行自定义名的**解析侧**归一：None/非字符串/空 → `""`；去两侧空白、剥掉控制字符、
    按 `NAME_MAX_LEN` 截断。

    为什么宽容：这个名字要与 `slot`/`type`/`proxy` 一起挤在 `.env` 的一行里，手写或
    旧版本留下的值不该让**整行**被判非法丢掉（丢行 = 少一个执行体）。
    写入侧的严格校验在 Web 层（`_validated_name`）：那里给用户 400，比静默截断好。
    """
    if not isinstance(raw, str):
        return ""
    # 控制字符（含各类行分隔符）会让这个值在 .env 里换行或被 splitlines 切开，直接剥掉
    return "".join(ch for ch in raw.strip() if ch.isprintable())[:NAME_MAX_LEN]


def _clean_proxy(proxy):
    """出口串归一：None/缺省=直连（空串），两侧空白去掉（与 `parse_list` 的取值口径一致）。"""
    return "" if proxy is None else str(proxy).strip()


def _validate_type(rtype):
    if rtype not in TYPES:
        raise ValueError("执行体类型只能是 worker / fallback / disabled")


def _fallback_taken(rows, slot, rtype):
    """把 `slot` 行设为 `rtype` 后是否与别的兜底行冲突（兜底最多 1 行）。"""
    if rtype != TYPE_FALLBACK:
        return False
    return any(r["type"] == TYPE_FALLBACK and r["slot"] != slot for r in rows)


def _owner_host(hostname=None):
    """槽位名里的主机后缀：省略时取本机名（`socket.gethostname()`）。"""
    return socket.gethostname() if hostname is None else hostname


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
