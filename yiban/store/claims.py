# -*- coding: utf-8 -*-
"""数据层：签到领取池与账号级租约（v17，多执行体协调）。

多执行体的分工**不做静态分片**：按账号数平均切分时，"最慢的那一份"决定全天成败，
而签到窗口是硬的、学校放号时间不由我们定。改成**动态领取**——每个执行体在
`BEGIN IMMEDIATE` 语义下原子领取一个账号，领到才算它的活；执行体崩了，它持有的
账号靠**租约超时**被任何执行体接管（不需要人工介入，也不需要"分片数"这种记账）。

一行 = 一个账号在一个业务日的一条记录，其 `state` 就是"当日了结与否"的判据：

| state | 含义 | 是否了结 |
|-------|------|----------|
| `claimed` | 已被某执行体领取、尚未收尾（含执行中） | 否 |
| `done` | 收尾且**当日无需再签**（即 `yiban.status.CLAIM_DONE_STATUSES`：成功 / 已签到 / 今日无任务） | 是 |
| `failed` | 收尾但结果未了结（本轮重试预算耗尽、窗口外跳过、无点位） | 否（当日仍可再领，见 `STATE_FAILED`） |

**四条纪律**：

1. **只做协调，不改展示契约**：按日状态文件（`sign-state-*.json`）的 JSON 结构与
   写入时机不动——它是日历/状态展示的事实源，本表只回答"谁领了、了结没有"。
2. **写入带 owner 条件（CAS）+ fencing token**：租约被接管后，被接管的旧执行体写不进去，
   避免"两个执行体都以为自己签成功了"；owner 相同但 `epoch` 落后时同样写不进去
   （同 owner 重入会让 epoch 递增，见纪律 4）。**owner 串本身不是身份证明**：同一个
   稳定槽位名（`single@{主机名}` 之类）可能同机两个进程共用（cron 全量与网页手动），
   故缺省身份由 `yiban.egress.runtime_owner` 拼上进程号与代次，且冲突分支不认
   "owner 相同即重入"——重入必须出示上一代的 `epoch`。
3. **协调不可用即拒跑（fail-closed）**：表未落地（迁移被延后）或库抖动时，`try_claim`
   返回 `(False, 0)` 并告警，**绝不**答"可执行"。答"可执行"在多执行体下会让两个执行体
   同时放行同一账号 ⇒ 两次真实登录，踩上游风控红线（"同一账号一天只真实登录一次"是本
   项目的第一红线）。单执行体形态由调用方按"本轮空转"处理。
4. **每次写都带 fencing token**：领取自增 `epoch`，`settle` / `give_up` / `touch` 的 WHERE
   都带 `epoch=?`。只给领取侧发号而不校验收尾写等于没做——执行体被 STW 停顿/容器挂起卡住
   数分钟后醒来，仍以为自己持有该账号，迟到的写会覆盖接管者的结论。

连接与进程内锁取自同包的 `yiban.store.db`；门面对本模块是**重命名**再导出（`claim_*`
前缀），逐条别名见 `yiban.store.db` 领取池绑定处的行尾注释。

**过渡说明（v18 起）**：v18 新增的 `sign_tasks`（访问层 `yiban/store/queue_store.py`）
把本表的 state / result / attempts 语义整体并入，并把本表存量行一次性平移进新表
（`vshard=-1`、`run_at=claimed_at`，历史行不会被新队列重复领取）。旧表**不删**：
14 天过渡期内本模块行为不变（仍读写本表），新队列只读写 `sign_tasks`，两表暂不对写；
双写对齐到一定版本后再由后续迁移冻结旧表。故展示与了结判据此刻仍以本表为准。
"""
import datetime
import logging
import os
import socket

from yiban import clock, masking
from yiban import status as yiban_status

logger = logging.getLogger("yiban.store.claims")

#: 租约时长（秒）：超过它没续租的记录可被任何执行体接管。
#: 合法单账号耗时上限 = 登录 + 签到 + 重试间隔（数十秒级），900s 是其数倍；
#: 取太短会把"正在重试的慢账号"误判为死执行体而重复登录（同一账号两次登录
#: 会加速触发易班侧风控），取太长会让崩溃后的账号等到窗口结束都没人接。
LEASE_SECONDS = 900

#: 保留期（天）：只用于运维追溯与"昨日的了结情况"，展示口径不读它。
RETENTION_DAYS = 14

#: 在飞：已被某执行体领取、尚未收尾。
STATE_CLAIMED = "claimed"
#: **当日了结**：无需再签（成员见 `yiban.status.CLAIM_DONE_STATUSES`）。
STATE_DONE = "done"
#: 尝试过但**未了结**（本轮重试预算耗尽、窗口外跳过等）：当日仍可被别的执行体或
#: 下一轮（补签轮 / 兜底常驻）接手——给弃时会把租约立刻置为过期，见 `give_up`。
#: 两点必须知道：①「预算」住在**单轮进程内**（`yiban.engine.attempts._retry_budget`），
#: 换一轮即重新计数；本表的 `attempts` 列只被领取侧自增，没有任何判据读它。②所以预算
#: 耗尽而弃权的账号，当日仍可被后面的轮次再接手、重走一遍登录+签到（会话缓存还有效时
#: 是探活复用，缓存被清过的那一轮就是一次真实登录）——这正是补签链接得上失败账号的前提，
#: 代价是领取层不按原因分档、也没有跨轮上限，跨轮的止损都在旁路（凭据熔断、站点限速）。
STATE_FAILED = "failed"
#: 终态集合（「了结」的账号）。
SETTLED_STATES = (frozenset((STATE_CLAIMED, STATE_DONE, STATE_FAILED))
                  # 「了结」的词义定义在 `yiban.status`（同一件事在 sign_claims / sign_tasks /
                  # 状态文件里各有一套 state 名），此处只做本表词表下的投影，不自写第二份判据
                  & yiban_status.TASKS_SETTLED_STATES)  # 交集后只剩 done：failed 是"可再领"、不是终态
#: 参与"未了结账号"统计的状态（与 done 互斥）
OPEN_STATES = (STATE_CLAIMED, STATE_FAILED)


#: 领取池不可用的一次性告警标记：`try_claim` 每个账号每轮都会被调到，而"表未落地/库锁"
#: 是持续状态，不去重会把同一条故障刷成几十条（与 `yiban/engine/schedule.py` 的配置告警
#: 同一手法：进程内一次）。
_pool_down_notified = False


def _integrity_errors():
    """唯一键冲突类异常（延迟取 sqlite3，便于本模块零依赖导入）。"""
    import sqlite3
    return sqlite3.IntegrityError


def _notify_pool_down(e):
    """领取池不可用（fail-closed 拒跑）的告警：进程内只报一次 + 并入当日汇总邮件。

    只写日志不够：管理员在设置页看到的"多执行体"配置看起来生效，实际签到被静默拒跑，
    无人知情。故并入当日汇总（A 线），并用模块级标记去重。
    """
    global _pool_down_notified
    if _pool_down_notified:
        return
    _pool_down_notified = True
    logger.error("领取签到账号失败（fail-closed 拒跑）: %s", e)
    # 局部导入：alerts 经引擎入口反向依赖本模块所在的数据层，模块级互引会成环
    # （与 yiban/engine/schedule.py 取 alerts 同一手法）。
    from yiban.engine import alerts
    alerts._collect_admin_mail(
        "签到领取池不可用",
        f"领取池读取失败，本执行体已拒绝执行签到（防同一账号被重复真实登录）：{e}",
    )


def new_owner(prefix=""):
    """执行体标识：`主机:进程:启动时刻`——重启后必然变化，便于识别"上一轮的持有者"。"""
    return f"{prefix}{socket.gethostname()}:{os.getpid()}:{datetime.datetime.now():%H%M%S}"


def _now_str(now=None):
    return now or clock.ts()


def _utc_offset_str(seconds):
    """把"早于此刻即过期"换算成可比较的时间串（与库内时间串同口径：业务时间）。"""
    t = clock.now() - datetime.timedelta(seconds=seconds)
    return t.strftime("%Y-%m-%d %H:%M:%S")


def try_claim(phone, day, owner, lease_sec=LEASE_SECONDS, now=None, allow_settled=False,
              epoch=None):
    """原子领取一个账号。返回 `(ok, epoch)`。

    `ok=False` 表示"没领到，必须放弃该账号本轮"；`epoch` 是本次领取的 fencing token
    （单调递增，插入分支为 1），收尾时必须原样传给 `settle` / `give_up` / `touch`。

    领不到的情形：已被别的执行体领取且租约未过期，当日已了结（除非
    `allow_settled=True`——手动指定账号、补签轮重跑等显式路径用），或领取池不可用。

    实现是**单条 upsert**：并发下 SQLite 串行化写者，后到者的 WHERE 会看到
    先到者已提交的行，故"只可能有一个赢家"，不需要额外的锁表。判据与写入在同一条
    UPDATE 的 WHERE 里，没有"先查后写"可被撞上的跨语句窗口。

    **冲突分支的准入判据（按行状态分两条）**：

    - 已了结（`done`）：只由 `allow_settled` 决定——done 行无人持有，没有租约可校。
      `epoch` 另给了就要求行仍在这一代：`allow_settled` 的调用方若先读过行再重开，
      落后的代说明中间已被人改过，此时必须拒绝而不是盲目覆盖。
    - 未了结（`claimed` / `failed`）：**租约已过期**（`heartbeat_at <= now - lease_sec`，
      崩溃自愈的唯一入口），或**出示领取时拿到的 `epoch`**（真持有者本人的轮内重试）。
      **仅 owner 串相同不放行**：同机上两个进程可能拿到同一个身份串
      （cron 全量与网页手动曾都是 `single@{主机名}`），把同名认成"自己人"就是两边
      同时登录同一账号——本项目第一红线。故缺省身份须含进程号与代次
      （`yiban.egress.runtime_owner`），重入须出示 token；重启后的新进程没有上一代的
      token，只能等租约过期或由心跳/回收机制处置。

    `epoch=None` 保持迁移期调用方的旧语义（不校验代），但**同样不允许**同名重入。

    库异常（表未落地/锁超时/IO）时 **fail-closed**：告警 + 返回 `(False, 0)`。
    语义：多执行体下"按可执行处理"会让两个执行体同时放行同一账号 ⇒ 两次真实登录，
    踩上游风控红线；故改拒跑，由调用方按"本轮空转"处理。
    """
    from yiban.store import db
    ts = _now_str(now)
    expired_before = _utc_offset_str(lease_sec)
    # 两条分支都自增 epoch：**任何**一次成功领取都换一代 token，旧 token 随即作废。
    # <= 的比较口径：租约 0 秒即"立刻可接管"；弃权时租约被主动置为过期（见 give_up）。
    sql = (
        "INSERT INTO sign_claims (phone, day, owner, claimed_at, heartbeat_at, "
        "state, result, attempts, epoch) VALUES (?, ?, ?, ?, ?, ?, '', 0, 1) "
        "ON CONFLICT(phone, day) DO UPDATE SET "
        "owner=excluded.owner, claimed_at=excluded.claimed_at, "
        "heartbeat_at=excluded.heartbeat_at, state=excluded.state, "
        "epoch=sign_claims.epoch + 1, "
        "attempts=sign_claims.attempts + 1 "
        "WHERE (sign_claims.state = ? AND ? AND (? IS NULL OR sign_claims.epoch = ?))"
        "   OR (sign_claims.state IN (?, ?) "
        "       AND (sign_claims.heartbeat_at <= ? "
        "            OR (? IS NOT NULL AND sign_claims.owner = excluded.owner "
        "                AND sign_claims.epoch = ?))) "
        "RETURNING epoch"
    )
    try:
        conn = db.get_conn()
        with db._conn_lock:
            cur = conn.execute(
                sql, (phone, day, owner, ts, ts, STATE_CLAIMED,
                      STATE_DONE, 1 if allow_settled else 0, epoch, epoch,
                      STATE_CLAIMED, STATE_FAILED, expired_before,
                      epoch, epoch),
            )
            # RETURNING 只对**真的写成**的行出行：条件不满足时零行，正是"没领到"。
            row = cur.fetchone()
            conn.commit()
            return (True, row[0]) if row else (False, 0)
    except _integrity_errors() as e:
        # 唯一键冲突 = 有别人刚领到（极端时序下 upsert 之外的可能路径）。
        # 此时**必须**答"没领到"：答"领到了"会让两个执行体同时登录同一账号，
        # 那是本设计的第一红线（重复登录会加速触发易班侧风控）。故它**不得**折进
        # 下面的 fail-closed 分支——那会把"别人在做"这个事实掩盖成"池子坏了"。
        logger.debug("领取竞争失败（他人已领）: %s", e)
        return (False, 0)
    except Exception as e:
        _notify_pool_down(e)
        return (False, 0)


def touch(phone, day, owner, lease_sec=LEASE_SECONDS, now=None, epoch=None):
    """续租（只续自己的）。返回是否续上（被接管/已了结/token 落后时为 False）。

    `lease_sec` 只用于保持既有调用签名：租约判据在领取侧按心跳时间串比较（见 try_claim），
    续租只是把心跳写成"现在"。`epoch=None` 时不校验 token（迁移期调用方与既有测试）。
    """
    from yiban.store import db
    ts = _now_str(now)
    sql = ("UPDATE sign_claims SET heartbeat_at=? "
           "WHERE phone=? AND day=? AND owner=? AND state=?")
    params = [ts, phone, day, owner, STATE_CLAIMED]
    if epoch is not None:
        sql += " AND epoch=?"
        params.append(epoch)
    try:
        conn = db.get_conn()
        with db._conn_lock:
            cur = conn.execute(sql, tuple(params))
            conn.commit()
            return cur.rowcount == 1
    except Exception as e:
        logger.debug("续租失败（不影响签到）: %s", e)
        return False


def _explain_fenced_write(phone, day, what):
    """被 fencing 拒时的回读：区分"对方已了结"与"本执行体确实被接管"。

    写 0 行有两种成因，只 `return False` 会把它们混成一个：
    ① 行已被收尾（`done`）——本次写是幂等重入（Stripe 幂等键语义），按 info 记；
    ② 行仍无终态——本执行体拿着作废的 token 在写，按 warning 记。
    """
    from yiban.store import db
    try:
        with db._conn_lock:
            row = db.get_conn().execute(
                "SELECT state FROM sign_claims WHERE phone=? AND day=?",
                (phone, day)).fetchone()
    except Exception as e:
        logger.debug("回读签到记录失败（不影响签到结果）: %s", e)
        return
    state = row[0] if row else ""
    who = masking.mask_phone(phone)
    if state in SETTLED_STATES:
        logger.info("%s被 fencing 拒，但该行已有终态 %s（幂等重入）: %s", what, state, who)
    else:
        logger.warning("%s被 fencing 拒且无终态: %s", what, who)


def settle(phone, day, owner, state=STATE_DONE, result="", epoch=None):
    """收尾：把领取记录置为终态。返回是否写成功（被接管 / token 落后时为 False）。

    `result` 只存**摘要**（截断），且调用方须先脱敏——本表可能被运维查询导出。

    `epoch` 传领取时拿到的 fencing token，被接管者的迟到写由存储端拒绝；写 0 行且传了
    epoch 时回读一次以区分"幂等重入"与"真被接管"（见 `_explain_fenced_write`）。
    """
    from yiban.store import db
    if state not in SETTLED_STATES:
        raise ValueError(f"非法终态: {state!r}")
    sql = ("UPDATE sign_claims SET state=?, result=?, heartbeat_at=? "
           "WHERE phone=? AND day=? AND owner=?")
    params = [state, (result or "")[:200], clock.ts(), phone, day, owner]  # 只留摘要；200 与 queue_store.RESULT_MAX 同口径，改一处要同步另一处
    if epoch is not None:
        sql += " AND epoch=?"
        params.append(epoch)
    try:
        conn = db.get_conn()
        with db._conn_lock:
            cur = conn.execute(sql, tuple(params))
            conn.commit()
            if cur.rowcount == 1:
                return True
    except Exception as e:
        logger.warning("收尾签到记录失败（不影响签到结果）: %s", e)
        return False
    if epoch is not None:
        _explain_fenced_write(phone, day, "收尾")
    return False


def give_up(phone, day, owner, result="", epoch=None):
    """本次执行放弃该账号，但**当日仍未了结**：置 `failed` 并**立刻放开租约**。

    为什么必须放开：补签轮（窗口内第二轮）与兜底执行体的存在意义就是接手失败账号。
    若把租约留满 900s，07:10 弃权的账号在 07:12 的补签轮里仍"被持有"→ 补签轮领不到、
    当日再也签不上。放开后任何执行体/任何一轮都能立刻接手。

    `epoch` 的语义同 `settle`（弃权同样是终态写：被接管者不得把接管者的在飞记录改成 failed）。
    返回是否写成功（被接管 / token 落后时为 False）。
    """
    from yiban.store import db
    sql = ("UPDATE sign_claims SET state=?, result=?, heartbeat_at=? "
           "WHERE phone=? AND day=? AND owner=?")
    expired = _utc_offset_str(LEASE_SECONDS)   # 主动置为"已过期"
    params = [STATE_FAILED, (result or "")[:200], expired, phone, day, owner]
    if epoch is not None:
        sql += " AND epoch=?"
        params.append(epoch)
    try:
        conn = db.get_conn()
        with db._conn_lock:
            cur = conn.execute(sql, tuple(params))
            conn.commit()
            if cur.rowcount == 1:
                return True
    except Exception as e:
        logger.warning("放弃签到记录失败（不影响签到结果）: %s", e)
        return False
    if epoch is not None:
        _explain_fenced_write(phone, day, "弃权")
    return False


def _day_column(day, column):
    """按业务日取 `(phone, column)` 全量行；库不可用时返回 None（调用方折成空）。

    `column` **只接受本模块的字面量**（"state"/"owner"），不来自外部输入——
    它是拼进 SQL 的，这是本函数不对外暴露的原因。
    按 `day` 过滤走主键前缀 `(phone, day)`/`(day, state)`，一次取全不分页。
    """
    from yiban.store import db
    try:
        with db._conn_lock:
            return db.get_conn().execute(
                f"SELECT phone, {column} FROM sign_claims WHERE day=?", (day,)
            ).fetchall()
    except Exception as e:
        logger.debug("读取当日签到记录失败（按空处理）: %s", e)
        return None


def states_for_day(day):
    """当日已建记录的 `phone -> state` 映射（未建的账号不在映射里 = 未领取）。"""
    rows = _day_column(day, "state")
    return {} if rows is None else {r["phone"]: r["state"] for r in rows}


def in_flight_phones(day, lease_sec=LEASE_SECONDS):
    """当日仍在飞（`claimed` 且租约未过期）的账号——诊断"谁卡住了"用。"""
    from yiban.store import db
    expired_before = _utc_offset_str(lease_sec)
    try:
        with db._conn_lock:
            rows = db.get_conn().execute(
                # 与 try_claim 的接管判据同界：心跳 <= now-lease 即"租约已过期"，
                # 故"在飞"是严格大于（lease_sec=0 时不应有任何在飞记录）
                "SELECT phone, owner, claimed_at, heartbeat_at FROM sign_claims "
                "WHERE day=? AND state=? AND heartbeat_at > ?",
                (day, STATE_CLAIMED, expired_before),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.debug("读取在飞账号失败（按空处理）: %s", e)
        return []


def stats(day):
    """当日各状态计数——供设置页/CLI 展示"了结进度"。"""
    from yiban.store import db
    try:
        with db._conn_lock:
            rows = db.get_conn().execute(
                "SELECT state, COUNT(*) AS n FROM sign_claims WHERE day=? GROUP BY state",
                (day,),
            ).fetchall()
        out = {STATE_CLAIMED: 0, STATE_DONE: 0, STATE_FAILED: 0}
        for r in rows:
            out[r["state"]] = r["n"]
        out["settled"] = out[STATE_DONE]  # 了结数只等于 done：failed 当日仍可再领，落在 open 里（见 OPEN_STATES）
        out["open"] = out[STATE_CLAIMED] + out[STATE_FAILED]
        out["total"] = out["settled"] + out["open"]
        return out
    except Exception as e:
        logger.debug("读取签到进度失败（按空处理）: %s", e)
        return {"claimed": 0, "done": 0, "failed": 0, "settled": 0, "open": 0,
                "total": 0}


def activity(day):
    """当日**按执行体归属**的分组计数（前端"谁做了多少"的数据来源）。

    与 `stats(day)` 的区别只在分组维度：`stats` 回答"了结了多少"，本函数回答
    "这些活分别是谁做的"。故它只做 `GROUP BY owner, state` 的计数，**不解析角色、
    不脱敏**——角色口径与脱敏是展示层的事（Web 层用 `yiban.egress.parse_owner`
    把 owner 折成角色与槽位序号，绝不把 owner 原串回给前端）。本模块因此不依赖
    `yiban.egress`，数据层保持对展示口径无感。

    返回 `[{"owner":…, "claimed":n, "failed":n, "done":n, "total":n}, …]`
    （按 owner 升序，顺序稳定；空库/库未初始化 → `[]`，与 `stats` 同口径不抛）。
    """
    from yiban.store import db
    out = {}
    try:
        with db._conn_lock:
            rows = db.get_conn().execute(
                "SELECT owner, state, COUNT(*) AS n FROM sign_claims "
                "WHERE day=? GROUP BY owner, state ORDER BY owner",
                (day,),
            ).fetchall()
    except Exception as e:
        logger.debug("读取执行体归属计数失败（按空处理）: %s", e)
        return []
    for r in rows:
        row = out.setdefault(
            r["owner"], {STATE_CLAIMED: 0, STATE_DONE: 0, STATE_FAILED: 0})
        # 未知状态照实加到它自己的键上（不丢数），总量仍等于当日全部记录数
        row[r["state"]] = row.get(r["state"], 0) + r["n"]
    result = []
    for owner, counts in out.items():
        item = {"owner": owner}
        item.update(counts)
        item["total"] = sum(counts.values())
        result.append(item)
    return result


def latest_claims_day():
    """`sign_claims` 里最近一次有记录的业务日（`MAX(day)`）；表空 / 库不可用返回 None。"""
    from yiban.store import db
    try:
        with db._conn_lock:
            # 「上次实领」取最近一次有记录的日、不是"昨天"：周末停签后按"昨天"取会让整列
            # 空白到下一个工作日，按最近一次取则跨周末也能看到上一轮是谁签的
            row = db.get_conn().execute("SELECT MAX(day) FROM sign_claims").fetchone()
    except Exception as e:
        logger.debug("读取最近一次签到记录日失败（按空处理）: %s", e)
        return None
    return row[0] if row else None


def owners_for_day(day):
    """某个业务日 `phone -> owner` 映射，供账号列表批量标注归属。

    **必须一次取全**：账号列表可能有几百行，逐账号查会让一次列表请求变成几百次查询。
    调用方在内存里按手机号匹配即可。

    与 `activity` 同一纪律：**只回 owner 原串、不解析角色、不脱敏**——角色口径与
    脱敏是展示层的事（Web 层用 `yiban.egress.parse_owner` 折成角色与槽位，绝不把
    owner 原串回给前端）。库未初始化/表未落地 → `{}`，与 `stats` 同口径不抛。
    """
    rows = _day_column(day, "owner")
    return {} if rows is None else {r["phone"]: r["owner"] for r in rows}


def owners_since(days=RETENTION_DAYS):
    """保留期内出现过的执行体身份串（去重，升序）——供"槽位号只增不复用"用。

    用途：删除清单里**当前最大**那一行之后，纯函数只能给出"最大值 + 1"（它会拿到刚空出
    的号）；追加接口据此再跳过"保留期内真用过的号"，这样"下标只增不复用"在删除后也成立。
    只回答"出现过哪些身份串"，**不解析角色**（解析是展示层的事，见 `activity`）。

    库不可用时返回 `[]`（调用方退回"只按清单最大值 +1"，不影响追加本身）。
    """
    from yiban.store import db
    cutoff = (clock.now() - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        with db._conn_lock:
            rows = db.get_conn().execute(
                "SELECT DISTINCT owner FROM sign_claims WHERE day >= ? ORDER BY owner",
                (cutoff,),
            ).fetchall()
        return [r["owner"] for r in rows if r["owner"]]
    except Exception as e:
        logger.debug("读取执行体历史身份失败（按空处理）: %s", e)
        return []


def purge(days=RETENTION_DAYS):
    """清理保留期外的记录（按业务日字符串比较）。失败仅告警，返回删除行数。

    接入时钟跳变守卫（同库其余清理同形，见 `yiban.store.db._clock_jump_guard`）：
    系统时间被拨快 >72h 时按日比较的 cutoff 会一下子跳到未来，"保留期外"的判据于是
    把最近几天的行全部算超期——本表是**当日互斥面**（(phone, day) 的持有记录），
    整删当日行等于把当天所有账号放行给下一个执行体重复真实登录。跳变只跳本轮：
    守卫在越界路径上也推进参照点，下一轮（≤24h 后）即恢复正常清理。
    守卫的 INSERT upsert 在 WAL 下即持 RESERVED 写锁，兼作 DELETE 的事务边界。
    """
    from yiban.store import db
    cutoff = (clock.now() - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        conn = db.get_conn()
        with db._conn_lock:
            ok, note = db._clock_jump_guard(conn, "purge_claims_clock")
            if not ok:
                logger.error("%s", note)
                conn.rollback()   # 越界路径已在守卫内提交参照点；此处只是解除写锁
                return 0
            cur = conn.execute("DELETE FROM sign_claims WHERE day < ?", (cutoff,))
            conn.commit()
            return cur.rowcount
    except Exception as e:
        logger.warning("清理签到领取记录失败: %s", e)
        return 0
