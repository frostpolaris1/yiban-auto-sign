# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""内核进度事件域：`run_events` 表的写入、读取与保留期清理（N2a 打点层）。

**功能**
- 写入：`report`（单条）与 `report_many`（单事务批量）——执行体与编排层共用的
  **唯一 reporter 接口**，调用方不拼 SQL；
- 读取：`summarize`（轮级摘要）、`timeline`（单轮时间线）、`day_bounds`（窗口边界）
  ——巡检面的**唯一读取接口**，调用方不拼 SQL；
- 保留期：`purge`（删除超期事件行，同事务写清理留痕）。

**读取面（巡检）**
本模块是写入面与读取面的唯一收口点。写入面净化并截断 `message`。读取面遮罩
`phone`、把 `executor` 收敛成公开标签、并**再复遮一次 `message`**。
- `phone`：复用 `yiban.masking.mask_phone` 的单源口径。响应不出现原始号码。
- `executor`：复用 `yiban.egress.owner_tag`，只回角色与槽位序号。身份原串带
  主机名；主机名属部署信息，任何接口都不得回原串。
- `message`：复用 `yiban.masking.sanitize_text` 再复遮一次。写入面
  已净化，读取面复遮是**纵深防御**：历史行与直插行未过写入面净化时仍不漏。
  复遮**不是无条件幂等**：已遮值与下一个凭据键之间无分隔符时会吞掉后一段。
  对写入口径的产物在实测形态下幂等。实测口径：定向 fuzz 出过非不动点；
  务实形态 fuzz 与真实报文均为不动点。
- 轮级摘要的分组键是 (业务日, 执行体)。本表没有轮次列，这是唯一可复算的口径。
  日筛选**下推到 SQL**（`summarize(day=…)`），故 `_MAX_ROUNDS` 截断只作用于已
  筛出的集合；被截断时调用方必须回显 `rounds_truncated`。
- `unexecuted` 的含义是"领取后未发起请求的账号数"。判据是有 claim 无 start。
  本表不记未被领取的账号，故这里看不到"无人领取"的那些账号。
- 读取面只读。它不写表，也不写审计链。

**可见窗口**
本表只保留 `RETENTION_DAYS` 天（默认 14）。窗口外的日期没有数据。
窗口区间 = `[今天-(RETENTION_DAYS-1), 今天]`，**唯一定义点是本模块的 `window_range()`**：
读取面与端点都从它取值，谁都不另算公式。窗口外的日期（早于下界或晚于上界）都要回空。
读取面与页面必须显式写明窗口。窗口外要给"跨月回溯请走审计日志页"的指引。
不许静默出空表——那会让人以为那几天没有数据。

**节点值域（本模块是唯一定义点）**
`NODE_CLAIM` 领取 / `NODE_START` 开始 / `NODE_SUCCESS` 成功 / `NODE_FAIL` 失败 /
`NODE_PAUSE` 暂停 / `NODE_FINALIZE` 收尾。写未知节点被拒并告警：节点值是下游
（SSE 端点、日志页）的契约面，落一个没人认得的取值比丢一条事件更坏。

**归属**
`yiban/engine/executor_v3.py`（执行体节点）与 `yiban/engine/runner.py`（编排节点）
共用的一张**观测表**。本表不参与签到正确性：任何一行写不进去都不改变签到结论。
建表与索引在迁移域（`yiban/store/migrations.py` 的 v21），本模块只读写；
保留期清理由 `yiban/store/cleanup.py` 的每日编排调用（`purge_run_events`）。

**收尾有两层（`finalize` 的 message 前缀是它们的判别面）**
- `执行体会话收尾：`：一次执行体会话正常结束（`run_executor_v3` 正常返回路径）；
- `轮次收尾：`：一次轮次结束、退出码与汇总已成定局（`runner.main` 汇总处）。

单执行体路径下这两层在同一个进程里前后各落一行，`(业务日, 执行体, 节点)` 因此
相同。这不是重复计数：两层是不同的事实（会话可以在一轮里出现多次，例如兜底常驻
的反复扫描）。消费方按 message 前缀取自己要的那一层，**不得对 `finalize` 计数求和**。

**为什么是独立表而不是并入 sign_events**
`sign_events` 按**尝试**落行（一次登录一条，含 dur_sec / 结果文本），是签到事实；
本表按**进度节点**落行（领取/开始等不发起请求的节点也在内），是运行进度。两者
基数与消费者不同：签到报表按账号去重统计 `sign_events`，进度流按 id 增量尾读
本表。并表会让"成功率"口径混入进度行。

**复用**
`report` / `report_many` 是仅有的两个写入入口（后者只做"同一时刻的一批"，例如
一次领取返回的整批行）；节点常量供调用方与下游消费者对齐取值。写入失败**只告警
不抛**——打点是观测面，不是业务面。行随事件即时落库（不做攒批）：本表的下游是
"实时进度"，攒批会让进度流滞后到一轮结束。批量入口存在的理由是**取一次写锁**：
领取发生在 asyncio 事件循环线程里，逐行提交会把"一行一次锁等待"叠加成整条通道的
停顿（连接层的写锁等待上限是秒级）。

**通信**
连接、锁、时钟跳变守卫与留痕写入（`get_conn` / `_conn_lock` / `_begin_immediate` /
`_clock_jump_guard` / `_table_min_max` / `_record_purge_event`）一律经 `_facade()`
按属性取：`mock.patch.object(db, "get_conn", …)` 一类打桩要求打桩点落在 db 门面上，
直接调本模块同名函数会让打桩静默失效（测试仍绿，但打桩点不再是它以为的那一个）。
调用谁：`yiban.clock`。
谁调用：`yiban/engine/executor_v3.py`、`yiban/engine/runner.py`、
`yiban/store/cleanup.py`（每日清理）。
"""
import contextlib
import datetime
import logging

from yiban import clock
from yiban.masking import mask_phone as _mask_phone
from yiban.masking import sanitize_text as _sanitize_text

logger = logging.getLogger("yiban.store.run_events")

# 节点值域：唯一事实源。改名即改契约，下游读取点必须同批对齐。
NODE_CLAIM = "claim"
NODE_START = "start"
NODE_SUCCESS = "success"
NODE_FAIL = "fail"
NODE_PAUSE = "pause"
NODE_FINALIZE = "finalize"
NODES = (NODE_CLAIM, NODE_START, NODE_SUCCESS, NODE_FAIL, NODE_PAUSE, NODE_FINALIZE)

#: 节点 → 中文标签。读取面与前端共用这一份，前端不另抄一张表。
NODE_LABELS = {
    NODE_CLAIM: "领取",
    NODE_START: "开始",
    NODE_SUCCESS: "成功",
    NODE_FAIL: "失败",
    NODE_PAUSE: "暂停",
    NODE_FINALIZE: "收尾",
}

#: 事件行保留期（天）。进度事件只服务"实时进度 + 近期排障"，与签到事实
#: （sign_events 180 天）不同档；取 14 与领取台账（claims.RETENTION_DAYS）同量级。
RETENTION_DAYS = 14

# 单条与批量共用同一列序：分列两处时，只改一侧的列序会让另一条写入路径静默错位。
_INSERT_SQL = (
    "INSERT INTO run_events (ts, day, node, executor, phone, message) "
    "VALUES (?,?,?,?,?,?)"
)

# 写入失败时的定位线索：INSERT 报 "no such table/column" 时，后果是本表在保留期内
# **永久零写入**（每次写都失败、失败只发告警），而告警原文里没有升级链的线索。
_ARTIFACT_HINT = (
    "缺失产物归属：表 run_events=v21（核对 yiban/store/migrations.py 的"
    "产物登记表与迁移完成记录）"
)


def _facade():
    """db 门面（函数内延迟导入，避免与 `yiban.store.db` 形成导入环）。

    打桩可见性见模块说明：必须按**属性**取而不是模块级 from-import。
    """
    from yiban.store import db
    return db


def _row(node, day, executor, phone="", message=""):
    """→ 一行事件元组；节点值不在值域内时返回 None（调用方告警并丢弃）。

    `message` 在这里过脱敏与转义再截断——**净化点收在本模块**，与 `sign_events` 的
    写入口径用同一份原语。为什么不放在各调用点：`message` 常是易班服务端返回的原文
    （`attempts.attempt_signin` 的结果），可能带手机号、token、换行与控制字符；表里
    落了原文就有一条通往"日志/SSE 页面回显"的泄漏路径。逐调用点各净化一遍是
    "漏一个就泄漏"的形状，收在唯一写入点才没有下一个漏点。
    """
    if node not in NODES:
        logger.warning("进度事件节点值非法，已丢弃本条（不在值域 %s 内）: %r",
                       "|".join(NODES), node)
        return None
    ts = clock.now().strftime("%Y-%m-%d %H:%M:%S")
    return (ts, str(day or ""), node, str(executor or ""), str(phone or ""),
            _sanitize_text(str(message or ""))[:200])


def report(node, *, day, executor, phone="", message=""):
    """写一条进度事件；→ 是否落库（False = 节点值非法或写失败）。

    失败仅告警，不影响调用方：事件写入不得拖累签到主流程（既有契约，
    与 `sign_events` 的写入口径一致）。级别保持 warning，文本必须能定位。
    """
    return report_many([{"node": node, "day": day, "executor": executor,
                         "phone": phone, "message": message}]) == 1


def report_many(events):
    """单事务写入一批进度事件；→ 实际落库行数（0 = 全被丢弃或整批失败）。

    `events` 为 dict 列表，键与 `report` 的形参同名。逐行过 `_row` 做节点值域判定，
    再 `executemany` + 一次 commit——**一次写锁**，不在事件循环线程里逐行抢锁。
    失败仅告警，不影响调用方。

    **构造行也在保护区内**（逐条 try）：`_row` 会取业务钟、过脱敏原语、读条目的键
    ——三件事都可能抛（`clock.now()` 异常、净化原语异常、条目不是映射时 `e.get`
    抛 `AttributeError`）。调用方（`executor_v3._report` / runner）都不包 try 也不看
    返回值，它们依赖的是本函数"绝不抛"的承诺；把构造放在 try 之外，一次构造异常就会
    从 asyncio 任务里逃到签到主流程，与"观测面不得改变签到结论"的契约相反。
    """
    rows = []
    for e in events:
        try:
            row = _row(e.get("node"), e.get("day"), e.get("executor"),
                       e.get("phone", ""), e.get("message", ""))
        except Exception as exc:      # 条目不是映射 / 取时钟失败 / 净化失败
            logger.warning("进度事件条目不可用，已丢弃本条: [%s: %s]",
                           type(exc).__name__, exc)
            continue
        if row is not None:
            rows.append(row)
    if not rows:
        return 0
    conn = None
    try:
        db = _facade()
        with db._conn_lock:
            conn = db.get_conn()
            conn.executemany(_INSERT_SQL, rows)
            conn.commit()
        return len(rows)
    except Exception as e:
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.rollback()
        # 出声但不抛：进度事件不得拖累签到主流程；本批事件在保留期内没有补写路径，
        # 故文本要能直接定位到"哪张表、哪一档迁移"。
        logger.warning("写入 run_events 失败（本批 %d 条未落库，%d 天保留期内不补写）: "
                       "[%s: %s]——%s", len(rows), RETENTION_DAYS,
                       type(e).__name__, e, _ARTIFACT_HINT)
        return 0


def purge(days=RETENTION_DAYS):
    """清理保留期外的事件行（按业务日字符串比较）。失败仅告警，返回删除行数。

    本表逐日累积，不清理会无限增长。**复用与 `claims.purge` / `queue_store.purge`
    同一套时钟跳变守卫**（`db._clock_jump_guard`，各表各一份参照点）：系统时间被
    拨快时按日比较的 cutoff 会跳到未来，"保留期外"判据会把最近几天全算超期——
    整删当日行等于把正在看的进度流清空。跳变只跳本轮：守卫在越界路径上也推进
    参照点，下一轮即恢复。
    """
    db = _facade()
    conn = None
    cutoff = (clock.now() - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        with db._conn_lock:
            conn = db.get_conn()
            ok, note = db._clock_jump_guard(conn, "purge_run_events_clock")
            if not ok:
                logger.error("%s", note)
                conn.rollback()   # 越界路径已在守卫内提交参照点；此处只是解除写锁
                return 0
            before = db._table_min_max(conn, "run_events")
            cur = conn.execute("DELETE FROM run_events WHERE day < ?", (cutoff,))
            deleted = cur.rowcount or 0
            db._record_purge_event(conn, "run_events", "run_events_cleanup", cutoff,
                                   deleted, before, db._table_min_max(conn, "run_events"))
            conn.commit()
            return deleted
    except Exception as e:
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.rollback()
        logger.warning("清理进度事件失败（不影响其他清理）: %s", e)
        return 0


# ---------------------------------------------------------------------------
# 读取面（巡检）
# ---------------------------------------------------------------------------
# 本模块是写入面与读取面的**唯一收口点**。写入面把 message 净化后截断（`_row`）。
# 读取面把 phone 遮罩、把 executor 收敛成公开标签。两处都只做一次，调用方不重复处理。
# 为什么收在这里：`phone` 列存的是原始号码（`_row` 存原值），逐调用点遮罩是
# "漏一个就泄漏"的形状；收在唯一读取点才没有下一个漏点。

#: 轮级摘要一次最多回几行（防异常写入把响应撑爆）。
_MAX_ROUNDS = 200

#: `_MAX_ROUNDS` 的公开名。端点的 `rounds_limit` 取值源；该值已被前端渲染成用户
#: 可见文案（"本日最多 N 轮"），属契约字段。内部读取层仍用私有名。
MAX_ROUNDS = _MAX_ROUNDS


def window_range(days=RETENTION_DAYS):
    """→ 保留窗口的 (下界, 上界) 业务日串，两端**含**。只读。

    窗口区间的**唯一定义点**。下界 = `今天-(days-1)`，上界 = `今天`。
    读取面（`_rows_on` / `_rows_since` / `timeline`）与端点（`web/routes/run_events_api.py`
    的 `window.start_day` / `window.end_day`）都从这里取值；端点不得另算公式，否则
    "唯一定义点"失真、两处会静默漂移。

    为什么必须有下界：`purge` 的删界是 `day < 今天-days`，窗口起点却是 `今天-(days-1)`，
    故窗口起点之外那一天在清理跑过之前仍有行。少了下界，端点会对窗口外日期回
    `has_data=true`，与"窗口外回 has_data=false"的契约矛盾。

    为什么必须有上界：业务日晚于今天时（业务钟快一天即可产生未来日），少了上界，
    端点对未来日回 `in_window=false` 却 `has_data=true`，那是半个窗口。
    """
    d = max(1, int(days or RETENTION_DAYS))
    now = clock.now()
    upper = now.strftime("%Y-%m-%d")
    lower = (now - datetime.timedelta(days=d - 1)).strftime("%Y-%m-%d")
    return (lower, upper)


def _public_executor(executor):
    """原始身份串 → 公开执行体标签（角色 + 槽位序号，不含主机名）。

    复用 `yiban.egress.owner_tag` 的唯一渲染点（日志归因前缀），只去掉方括号。
    身份原串带主机名，属部署信息；任何接口都不得回原串（见 egress 模块说明）。
    判不出的串回 `unknown`，照实回而不猜。
    """
    from yiban import egress
    tag = egress.owner_tag(executor or "")
    return tag[1:-1] if tag.startswith("[") and tag.endswith("]") else tag


def _executor_label(executor):
    """原始身份串 → 中文角色标签（`yiban.egress.parse_owner` 的既有口径）。"""
    from yiban import egress
    return egress.parse_owner(executor or "")["label"]


def _parse_ts(text):
    """事件时刻串 → `datetime`；解析不出回 None（耗时按 None 处理，不抛）。"""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.datetime.strptime(str(text), fmt)
        except (TypeError, ValueError):
            continue
    return None


def day_bounds():
    """→ (最早已落库的业务日, 最晚已落库的业务日)。表空时回 (None, None)。

    只读。调用方据此回显"可见窗口的真实边界"。窗口外的日期必须给指引，
    不许静默出空表——那会让人以为那几天没有数据。
    """
    db = _facade()
    with db._conn_lock:
        row = db.get_conn().execute(
            "SELECT MIN(day) AS lo, MAX(day) AS hi FROM run_events").fetchone()
    if row is None or row["lo"] is None:
        return (None, None)
    return (str(row["lo"]), str(row["hi"]))


def _rows_since(lower, upper):
    """→ 落在窗口 `[lower, upper]`（含两端）内的事件行（按业务日降序、id 升序）。只读。

    区间来自 `window_range()`：两端都过滤，未来业务日不得混入缺省日的聚合。
    """
    db = _facade()
    with db._conn_lock:
        cur = db.get_conn().execute(
            "SELECT id, ts, day, node, executor, phone, message FROM run_events "
            "WHERE day >= ? AND day <= ? ORDER BY day DESC, id ASC", (lower, upper))
        return [dict(r) for r in cur.fetchall()]


def _rows_on(day):
    """→ 该业务日的全部事件行（按 id 升序）。只读。日筛选在 SQL 侧完成。

    **带完整保留窗口**：日期早于下界或晚于上界都回空行。窗口下界与清理删界之间有一天的
    缝（清理删 `day < 今天-RETENTION_DAYS`，窗口起点是 `今天-(RETENTION_DAYS-1)`），
    `今天-RETENTION_DAYS` 在清理跑过前仍有行；上界挡掉未来业务日（业务钟快一天可产生）。
    窗口两端都取自 `window_range()`。
    """
    db = _facade()
    lo, hi = window_range()
    with db._conn_lock:
        cur = db.get_conn().execute(
            "SELECT id, ts, day, node, executor, phone, message FROM run_events "
            "WHERE day = ? AND day >= ? AND day <= ? ORDER BY id ASC",
            (str(day), lo, hi))
        return [dict(r) for r in cur.fetchall()]


def _group_rounds(rows):
    """事件行 → 轮级摘要行列表（按"业务日降序、该轮最晚时刻降序"排列）。只读。"""
    groups = {}
    order = []
    for row in rows:
        key = (row["day"], _public_executor(row["executor"]))
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                "day": row["day"], "executor": key[1],
                "executor_label": _executor_label(row["executor"]),
                "claim": 0, "start": 0, "success": 0, "fail": 0, "pause": 0,
                "claimed": set(), "started": set(),
                "_ts": [],
            }
            order.append(key)
        node = row["node"]
        if node in ("claim", "start", "success", "fail", "pause"):
            g[node] += 1
        if node == "claim" and row["phone"]:
            g["claimed"].add(row["phone"])
        if node == "start" and row["phone"]:
            g["started"].add(row["phone"])
        stamp = _parse_ts(row["ts"])
        if stamp is not None:
            g["_ts"].append(stamp)

    out = []
    for key in order:
        g = groups[key]
        stamps = g.pop("_ts")
        g["unexecuted"] = len(g.pop("claimed") - g.pop("started"))
        if stamps:
            lo, hi = min(stamps), max(stamps)
            g["first_ts"] = lo.strftime("%Y-%m-%d %H:%M:%S")
            g["last_ts"] = hi.strftime("%Y-%m-%d %H:%M:%S")
            g["duration_sec"] = int((hi - lo).total_seconds())
        else:
            g["first_ts"] = ""
            g["last_ts"] = ""
            g["duration_sec"] = None
        out.append(g)

    out.sort(key=lambda r: (r["day"], r["last_ts"]), reverse=True)
    return out


def summarize(*, day=None, days=RETENTION_DAYS):
    """轮级摘要：按 (业务日, 执行体) 聚合。→ (rounds, truncated)。只读。

    **两个参数都是 keyword-only**：旧签名是 `summarize(days=…)`，`days` 曾在首位。
    加上 `day` 后若允许位置调用，`summarize(7)` 会静默变成 `day="7"`，回空表而不报错。
    keyword-only 让旧式位置调用直接抛 `TypeError`，不静默回空。

    **一轮 = 一个 (业务日, 执行体) 分组**。表里没有轮次列，本口径是唯一可复算的定义。

    `day` 给定则只聚合该业务日：日筛选**下推到 SQL**（`WHERE day = ?`），
    故 `_MAX_ROUNDS` 截断只作用于已筛出的集合。为什么必须下推：先在全部窗口内
    截断、再按日筛选，会把"最新若干轮之外的整日"静默切掉——有数据的一日回空表，
    页面写"该日没有运行进度记录"。受影响的正是"查昨天那轮"这一主用途。
    `day` 缺省则聚合最近 `days` 个业务日。
    `truncated` = 分组数超过 `_MAX_ROUNDS`、`rounds` 已被截断（消费方必须显式回显）。

    每行字段与口径：
    - `claim` / `start` / `success` / `fail` / `pause`：该节点的行数；
    - `unexecuted`：**领取后未发起请求**的账号数（有 claim 无 start，按账号去重）。
      本表只记已被领取的账号，故这里看不到"无人领取"的账号；
    - `duration_sec`：该轮最晚与最早时刻之差（秒）；时刻解析不出时回 None；
    - `first_ts` / `last_ts`：该轮最早与最晚时刻；
    - `executor` / `executor_label`：公开标签（角色 + 槽位序号），**不含主机名**。
    """
    if day:
        out = _group_rounds(_rows_on(str(day)))
    else:
        out = _group_rounds(_rows_since(*window_range(days)))
    return out[:_MAX_ROUNDS], len(out) > _MAX_ROUNDS


def timeline(day, executor=None, limit=200):
    """某一轮的事件时间线（按影响行序 = 写入序）。→ (rows, truncated)。只读。

    `executor` 是**公开标签**（`summarize` 的 `executor` 字段）；None 表示该日全部。
    每行字段：`ts` / `node` / `node_label` / `phone`（已遮罩）/ `message`。
    只回保留窗口内的日期（带完整窗口两端，见 `window_range`）；窗口外回空表。
    `message` 再过一次 `sanitize_text`：写入面已净化，读取面再复遮一次，
    构成纵深防御。复遮**不是无条件幂等**（见模块说明），但对写入口径的产物在实测
    形态下幂等。为什么读取面也要遮：历史行与直插行可能没过写入面净化；本模块是
    读取面的唯一收口点，收在这里才没有下一个漏点。
    行数超过 `limit` 时截断，第二个返回值给 True。
    """
    limit = max(1, int(limit or 1))
    db = _facade()
    lo, hi = window_range()
    with db._conn_lock:
        cur = db.get_conn().execute(
            "SELECT ts, node, executor, phone, message FROM run_events "
            "WHERE day = ? AND day >= ? AND day <= ? ORDER BY id ASC",
            (str(day), lo, hi))
        raw = [dict(r) for r in cur.fetchall()]
    rows = []
    for r in raw:
        if executor is not None and _public_executor(r["executor"]) != executor:
            continue
        rows.append({
            "ts": str(r["ts"] or ""),
            "node": r["node"],
            "node_label": NODE_LABELS.get(r["node"], r["node"]),
            "phone": _mask_phone(str(r["phone"] or "")),
            "message": _sanitize_text(str(r["message"] or "")),
        })
    return rows[:limit], len(rows) > limit
