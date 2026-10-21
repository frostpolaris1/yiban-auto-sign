# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""发射账本：回答「这条事实我们说过了没有」，仲裁在库（单 3zr5）。

**为什么是一个库表而不是模块级变量**：一条「是否已经说过」必须由与它**同作用域、
同寿命**的持久载体回答。引擎侧的成因（未领取/未执行的批量留痕、起跑横幅）作用域是
（业务日, 执行体槽位, 成因）、寿命是该业务日；而模块级变量的作用域是**进程**、
寿命是**进程运行期**——两个维度都错位，于是每个成因都要手写三个边界：

- **身份**：多执行体/同进程换执行体时，第二个执行体被判成"没变"而漏掉它唯一的一条；
- **复位**：跨业务日必须记得把变量清回 `None`；
- **复活**：组成 N→0→N 时第三次留痕不得被吞。

**本模块的机制**：`key = H(day, scope, kind, payload)` 是内容寻址的主键；`day` 与
`scope` 是**必填形参**，键在数据层内部构造 ⇒ 调用方**写不出**"忘了带身份"或
"忘了跨天"的键。仲裁者是库（主键唯一约束 ＋ 单条语句），进程内存不参与。

**两个入口**（都返回"我是不是说的那一个"）：

- `emit_once`（A 型）：这组内容说一次就够。`INSERT OR IGNORE`，插入成功才是第一个。
- `emit_on_change`（B 型）：值变了就要再说（含消失后重现）。条件 `UPDATE … WHERE
  value IS NOT ?`；受影响行数为 1 才是"变了"。

**失败方向**：写不进去时两个入口都返回 `True`（照说）。观测面宁可重复也不静默
丢失——重复由下一次读取方去重，丢失没有任何补写路径。缺 `day`/`scope`/`kind` 时
同样返回 `True` 并记 ERROR：参数是必填的，但观测面不得因为配置错误而静默。

**保留期**：本表按业务日累积，每行很小（每成因每日至多数行）。不设自动清理——
跨天是另一批键，正确性不依赖清理；清理只会让"当天没说过"的判断在窗口外复活。
"""
import contextlib
import hashlib
import logging

from yiban.core import clock

logger = logging.getLogger("yiban.adapters.persistence.emit_ledger")

_INSERT_ONCE_SQL = (
    "INSERT OR IGNORE INTO emit_ledger (key, day, scope, kind, value, seen_at) "
    "VALUES (?,?,?,?,?,?)"
)

# `IS NOT` 兼容 NULL（本表 value 是 NOT NULL，但判据不依赖这一点）。UPSERT 的
# `DO UPDATE … WHERE` 为假时受影响行数为 0，故 rowcount 直接就是"变了没有"。
_UPSERT_ON_CHANGE_SQL = (
    "INSERT INTO emit_ledger (key, day, scope, kind, value, seen_at) "
    "VALUES (?,?,?,?,?,?) "
    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, seen_at=excluded.seen_at "
    "WHERE emit_ledger.value IS NOT excluded.value"
)


def _facade():
    """db 门面（函数内延迟导入，避免与 `yiban.store.db` 形成导入环）。

    必须按**属性**取：`mock.patch.object(db, "get_conn"/"_conn_lock", …)` 一类打桩
    要求打桩点落在 db 门面上，模块级 from-import 会让打桩静默失效。
    """
    from yiban.adapters.persistence import db
    return db


def _key(day, scope, kind, payload):
    """内容寻址键：`H(day, scope, kind, payload)`，含分隔符防止字段拼接歧义。"""
    raw = "\x1f".join(("emit-ledger-v1", str(day), str(scope), str(kind),
                       str(payload)))
    return hashlib.blake2b(raw.encode("utf-8"), digest_size=20).hexdigest()


def _arguments(day, scope, kind, what):
    """必填作用域的取值校验 → `(day, scope, kind, ok)`。

    缺任一项时键会退化成空串拼出的公共键（所有成因/执行体撞在一起），那是比重复
    更坏的"互相压掉"。这里拒绝构造，并让调用方按 fail-open 处理（照说）。
    """
    day = str(day or "").strip()
    scope = str(scope or "").strip()
    kind = str(kind or "").strip()
    ok = bool(day and scope and kind)
    if not ok:
        logger.error("发射账本缺必填作用域（day=%r scope=%r kind=%r），"
                     "本次按「我来说」处理（照说不压掉）: %s", day, scope, kind, what)
    return day, scope, kind, ok


def _write(sql, key, day, scope, kind, value, what):
    """执行一条仲裁语句 → 受影响行数（1 = 这次说了/变了）。失败 fail-open。"""
    conn = None
    try:
        db = _facade()
        with db._conn_lock:
            conn = db.get_conn()
            # BEGIN IMMEDIATE：查重/更新与写入同处一把写锁。跨进程并发时第二个
            # 写者等待（busy_timeout 15s），不会出现"两个都读到没说过"。
            db._begin_immediate(conn)
            cur = conn.execute(
                sql, (key, day, scope, kind, str(value), clock.ts()))
            changed = (cur.rowcount or 0) == 1
            conn.commit()
        return changed
    except Exception as e:
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.rollback()
        logger.warning("发射账本写入失败（本次照说，不静默丢弃）: %s [%s: %s]——%s",
                       what, type(e).__name__, e,
                       "缺失产物归属：表 emit_ledger=v24")
        return True


def emit_once(*, day, scope, kind, payload=""):
    """A 型：这组内容**第一次**说时返回 True，之后返回 False。

    `payload` 进键（内容寻址）：同一成因下内容不同就是另一件事。键不含轮次/时刻
    ——把它们并进键会让每轮都"没说过"，等于没有去重。
    """
    day, scope, kind, ok = _arguments(day, scope, kind, "emit_once")
    if not ok:
        return True
    # `A\x1f` 前缀与 B 型隔开（见 emit_on_change 的理由）。
    return _write(_INSERT_ONCE_SQL, _key(day, scope, kind, "A\x1f" + str(payload)),
                  day, scope, kind, payload, "emit_once")


def emit_on_change(*, day, scope, kind, value=""):
    """B 型：值**变了**返回 True（含空串→值、值→空串、值→同值之外的任何变化）。

    `value` 是该成因的"当前值"（调用方自己决定怎么表达；空串表示归零）。语义与
    "进程内上一个值"逐字相同：N→0→N 是 3 次变化，N→N 只有第一次。
    """
    day, scope, kind, ok = _arguments(day, scope, kind, "emit_on_change")
    if not ok:
        return True
    # `B\x1f` 前缀把两套机制隔开：同一个 (day, scope, kind) 若被两种机制各用一次，
    # 也不得互相覆盖（A 型的 payload 与 B 型的"当前值"是两件事）。
    return _write(_UPSERT_ON_CHANGE_SQL, _key(day, scope, kind, "B\x1fstate"),
                  day, scope, kind, value, "emit_on_change")
