# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""审计链域：HMAC 审计哈希链的写入/校验、库外锚点、审计密钥来源与清理留痕口径。

**功能**
- 密钥来源与缓存：`_parse_env_file` / `_decode_audit_key` / `_resolve_key_env_file` /
  `_write_audit_key_to_env_file` / `_assert_key_source_certain` / `_audit_key`（含
  `_AUDIT_KEY_CACHE`、`_AUDIT_KEY_LOCK`）与签名计算 `_audit_hash`；
- 写入链路：`audit()`、请求作用域（`set_request_scope` / `_scope_detail`，web 每请求一个
  id、CLI 退化为进程级 id）、"业务+审计同事务"原语（`audit_unit` / `record_in_txn` /
  失败即拒绝的 `audit_or_refuse`）、写入失败欠账计数（`_bump_audit_write_failure` 等）与
  只读口径（`audit_head_hash` / `audit_head_hash_ex` / `audit_row_count` / `verify_audit_chain`）；
- 欠账告警"按账目变化"触发：总账单调（取证事实）而通知基线随发信推进
  （`audit_write_failures_unnotified` / `audit_alert_needs_attention` / `mark_audit_alert_sent`）；
- 全表重链留痕：`_rechain_audit_logs` / `_record_rechain_event` / `audit_rechain_events`；
- 库外锚点族与最近清理口径：`record_audit_anchor` / `verify_audit_anchor` / `audit_health` /
  `_rechain_hint` / `audit_purge_total` / `audit_purge_events`。

**归属**
审计可追溯性是 web 与 signin 两个进程共用的一条链：写入方是 `db.audit()` 的全体调用点，
读出方是 web 每日线程与 `scripts/audit_verify.py`（都经 `audit_health()` 汇总）。建表与全部
迁移函数在 `yiban/store/migrations.py`；写事务入口 `_begin_immediate`、留痕的**写入**侧
（`_table_min_max` / `_record_purge_event`）在 `yiban/store/db.py`，每日清理 `_audit_cleanup`
的定义点已迁到 `yiban/store/cleanup.py`（门面按原名再导出），
追踪盐与加盐匿名哈希 `hash_ip` / `hash_phone` / `_track_salt` 在 `yiban/store/tracking.py`
（与审计密钥同住一个 .env、共用本模块的路径回落链）。

**复用**
`yiban.store.db` 把本模块的 40 个函数与 14 个常量按原样再导出，`db.audit()` /
`db.audit_health()` / `db._audit_hash(...)` 一类调用与身份断言不变；三个进程内可变状态
（`_AUDIT_KEY_CACHE` / `_AUDIT_FAIL_UNFLUSHED` / `_AUDIT_FAIL_UNFLUSHED_DB`）由 db 侧模块类
读写转发——`db._AUDIT_KEY_CACHE = None`（tests/test_rekey_key_source.py 清缓存）必须真的清到
本模块缓存，快照式再导出会静默失效。

**通信**
连接、进程内锁、写事务入口与 `get_meta` 一律经 `_facade()` 按属性取（`_conn_lock` /
`get_conn` / `_begin_immediate` / `get_meta`，以及 `_audit_hash` / `_audit_purge_total`）。
必须按属性取而非模块级 from-import：`mock.patch.object(db, "get_conn"/"_conn_lock", …)`、
`db._audit_hash = 替身`（tests/test_audit_anchor.py、tests/test_audit_tamper_94.py）与
`db._audit_purge_total = 注入`（tests/test_audit_anchor_field_source.py）都要求打桩点落在 db
门面上；直接调本模块同名函数会让打桩静默失效（测试仍绿，但打桩点不再是它以为的那一个）。
"""
import contextlib
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from yiban import clock
from yiban.infra import env_io, env_lock
from yiban.store import connection as _connection

logger = logging.getLogger("yiban.store.audit_chain")

# 审计 HMAC 密钥缓存与互斥
_AUDIT_KEY_CACHE = None
_AUDIT_KEY_LOCK = threading.Lock()


def _facade():
    """db 门面（函数内延迟导入，避免与 `yiban.store.db` 形成导入环）。

    打桩可见性见模块说明：必须按**属性**取而不是模块级 from-import，`db.<名字> = 替身`
    才会在函数体里生效。
    """
    from yiban.store import db
    return db


# ---------------------------------------------------------------------------
# 审计密钥来源与哈希链签名
# ---------------------------------------------------------------------------
def _parse_env_file(env_file):
    """读取 .env 全部键值，返回 dict（文件缺失返回空；非法行跳过）。

    严格策略：文件存在但读取失败 → 记 ERROR 并重抛（解析实现在 env_io）。
    """
    try:
        return env_io.parse_env_file(env_file, strict=True)
    except OSError as e:
        # 文件存在但读取失败：绝不静默当作"未配置"，否则 audit key / track salt
        # 自动生成路径会误判无密钥而重新生成，致既有审计链/追踪盐失效。
        # 宁可启动失败也不生成替代密钥。
        logger.error("环境变量文件存在但读取失败，按错误处理而非未配置（请检查权限）: %s [%s]", env_file, e)
        raise


def _decode_audit_key(raw):
    """把 hex 字符串审计密钥解码为 bytes；格式/长度非法抛 ValueError。"""
    try:
        key = bytes.fromhex(raw)
    except (TypeError, ValueError) as e:
        raise ValueError("YIBAN_AUDIT_KEY 格式非法：应为 64 位十六进制字符串") from e
    if len(key) != 32:
        raise ValueError("YIBAN_AUDIT_KEY 长度非法：应为 32 字节（64 位十六进制）")
    return key


def _resolve_key_env_file():
    """解析密钥来源 .env 路径：connection._env_file → 环境变量 YIBAN_ENV_FILE（去空白）→ ".env"。

    密钥来源不能绑在 cwd 上（不能写成 `env_file = _env_file or ".env"`）：
    取证/清点类 CLI（audit_verify / list_duplicate_owners）未传 env_file 时，在应用根
    之外运行会读不到旧钥，进而就地生成新钥落盘，同时产出"游离在错误目录的 .env"和
    "用错密钥签的审计行"（真实审计链随即判破，而这正是取证要用的工具）。

    返回 (path, from_cwd_default)：from_cwd_default=True 表示该路径纯粹靠 cwd 默认
    ".env" 兜底（既无 init_db(env_file=...) 也无 YIBAN_ENV_FILE）——此时文件不存在
    意味着密钥来源不确定，调用方须拒绝生成新密钥。
    """
    explicit = (_connection._env_file or "").strip()
    if explicit:
        return explicit, False
    env_var = (os.environ.get("YIBAN_ENV_FILE") or "").strip()
    if env_var:
        return env_var, False
    return ".env", True


def _write_audit_key_to_env_file(env_file, key):
    """把新生成的审计密钥写入 .env（保留其他行，原子替换，Unix 权限 0600）。

    读-写-替换整体包进共享 env_lock：与 web 写 .env 互斥；锁内仍保留
    “写入前重读”的既有兜底，避免多进程首启竞态覆盖。
    行模型（窄行读 + 逐行行分隔符校验 + 键行折叠 + 原子 0600 替换）下沉在
    `env_io.write_env_key`：与账号密钥、追踪盐两处写入方共用同一份实现，
    值里潜伏的 U+2028 之类不会被实体化成真配置行。
    """
    with env_lock.env_write_lock(env_file):
        existing = _parse_env_file(env_file).get("YIBAN_AUDIT_KEY", "").strip()
        if existing:
            return _decode_audit_key(existing)
        env_io.write_env_key(env_file, "YIBAN_AUDIT_KEY", key.hex())
        return key


def _assert_key_source_certain(what, env_file, from_cwd):
    """密钥来源只能靠 cwd 默认 ".env" 兜底且该文件不存在时拒绝生成。

    文件存在时行为完全不变（正常首启在应用根生成）；只有"来源不确定"（既无
    显式 env_file 也无 YIBAN_ENV_FILE，且当前目录没有 .env）才抛错——宁可不写
    也不要在错误目录留下第二个密钥源。
    """
    if from_cwd and not os.path.exists(env_file):
        logger.error(
            "%s生成中止：当前工作目录无 .env，且调用方未指定密钥来源"
            "（既未传 init_db(env_file=…) 也未设 YIBAN_ENV_FILE）——"
            "此时就地生成会在错误目录留下游离密钥，并使审计链/追踪哈希从此不可复现",
            what,
        )
        raise ValueError(f"{what}来源不确定，拒绝生成新密钥；请用 --env 或 YIBAN_ENV_FILE 指定 .env 路径")


def _audit_key(create=True):
    """获取审计 HMAC 密钥：环境变量 YIBAN_AUDIT_KEY 优先，回退 .env。

    .env 路径按 init_db(env_file=…) → YIBAN_ENV_FILE → 当前目录 ".env" 有序回落
    （不再无条件依赖 cwd）。
    create=True（默认）时缺失会生成并写入 .env；create=False 供只读校验，
    密钥缺失返回 None，由调用方按 fail-closed 处理。生成前
    若判定密钥来源只能靠 cwd 兜底且文件不存在，则拒绝生成并抛 ValueError。
    """
    global _AUDIT_KEY_CACHE
    env_file, from_cwd = _resolve_key_env_file()
    env_key = os.environ.get("YIBAN_AUDIT_KEY", "").strip()
    if env_key:
        _AUDIT_KEY_CACHE = _decode_audit_key(env_key)
        return _AUDIT_KEY_CACHE
    if _AUDIT_KEY_CACHE is not None:
        return _AUDIT_KEY_CACHE
    with _AUDIT_KEY_LOCK:
        if _AUDIT_KEY_CACHE is not None:
            return _AUDIT_KEY_CACHE
        file_key = _parse_env_file(env_file).get("YIBAN_AUDIT_KEY", "").strip()
        if file_key:
            _AUDIT_KEY_CACHE = _decode_audit_key(file_key)
            return _AUDIT_KEY_CACHE
        if not create:
            return None
        _assert_key_source_certain("审计密钥", env_file, from_cwd)
        logger.info("未找到 YIBAN_AUDIT_KEY，已生成新密钥并写入 %s（chmod 600）", env_file)
        _AUDIT_KEY_CACHE = _write_audit_key_to_env_file(env_file, secrets.token_bytes(32))
        return _AUDIT_KEY_CACHE


def _audit_hash(prev_hash, ts, username, action, target, detail):
    """计算单条审计日志的 HMAC-SHA256 哈希。"""
    payload = json.dumps(
        [prev_hash, ts, username, action, target, detail],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hmac.new(_audit_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _rechain_audit_logs(conn, record_event=None):
    """按 id 升序重建审计哈希链（从库内首行原 prev_hash 接续）。

    单事务原子承诺：旧实现按 10000 行游标**分批 commit**，中途失败/被杀会留下
    "前半段用新密钥、后半段还是旧 hash"的半重链——它是自洽链里最难发现的一种，且
    再重跑一次还会因为 user_version 已推进而不再触发。改为全部 UPDATE 在一个事务内
    完成后一次 commit；任何异常回滚到重链前状态（原链完好），不留半重链。代价是把
    全表读进内存（不再分批），换取"要么全链重签、要么原样不动"。

    不要在这里 BEGIN IMMEDIATE：调用方（migrate_v3）此前可能已有未提交的
    `ALTER TABLE ADD COLUMN`（SQLite DDL 也在事务内），显式 BEGIN 会撞
    "within a transaction"、回滚式解除又会把刚补的列一起丢掉。首个 UPDATE 自带的
    隐式事务已提供原子性。

    record_event：可选无参回调，在同一事务内、commit 之前执行。生产由 migrate_v3
    传入"写重链留痕"——让"重写整条链"与"记下这次重写"同事务，中间被杀不会留下
    "链被重签却无留痕"的静默状态。
    """
    rows = conn.execute(
        "SELECT id, ts, username, action, target, detail, prev_hash FROM audit_logs ORDER BY id"
    ).fetchall()
    prev = (rows[0]["prev_hash"] or "") if rows else ""
    try:
        for r in rows:
            h = _facade()._audit_hash(
                prev, r["ts"], r["username"], r["action"], r["target"], r["detail"]
            )
            conn.execute(
                "UPDATE audit_logs SET prev_hash=?, hash=? WHERE id=?",
                (prev, h, r["id"]),
            )
            prev = h
        if record_event is not None:
            record_event()
        conn.commit()
    except BaseException:
        with contextlib.suppress(Exception):
            conn.rollback()
        raise


def _record_rechain_event(conn, from_version, rows, empty_hash_rows, head_before, head_after):
    """全表重链留痕（app_meta.audit_rechain_events，最新在末尾）。

    重签整条链是"改完内容 → 清空 hash → 重启走正常启动路径 → 链重新自洽"这条路
    的唯一落点，所以每一次重链都必须记账，供 audit_health 与锚点交叉判定。写失败
    刻意上抛：宁可让迁移失败暴露出来，也不能悄悄重写链而不留痕。
    """
    # app_meta 原挂在 v8（后由 v12 幂等补建），v3 阶段可能还不存在——先补建，
    # 否则"留痕"这件事本身会把启动带崩。DDL 与 v8/v12 逐字一致。
    conn.execute(
        "CREATE TABLE IF NOT EXISTS app_meta ("
        "key   TEXT PRIMARY KEY, "
        "value TEXT NOT NULL"
        ")"
    )
    events = _rechain_events(conn)
    events.append({
        "ts": clock.now().strftime("%Y-%m-%d %H:%M:%S"),
        "from_version": int(from_version),
        "rows": int(rows),
        "empty_hash_rows": int(empty_hash_rows),
        "head_before": head_before or "",
        "head_after": head_after or "",
    })
    if len(events) > _RECHAIN_EVENTS_KEEP:
        events = events[-_RECHAIN_EVENTS_KEEP:]
    conn.execute(
        "INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)",
        (_RECHAIN_EVENTS_KEY, json.dumps(events, ensure_ascii=False)),
    )


def audit_rechain_events():
    """全表重链留痕（公开只读，最新在末尾；缺表/损坏 → []）。"""
    try:
        with _facade()._conn_lock:
            conn = _facade().get_conn()
            return _rechain_events(conn)
    except Exception as e:
        logger.warning("读取全表重链留痕失败: %s", e)
        return []


# ---------------------------------------------------------------------------
# 操作审计
# ---------------------------------------------------------------------------
# 审计写入失败欠账。审计是唯一追溯凭据，写入失败若无人察觉，就会出现
# "业务操作已生效、审计表里却没有这条记录"且哈希链依然自洽的静默丢失。
# 全仓 db.audit() 调用点众多且不检查返回值，故由此计数器兜底：由每日校验
# （web 每日线程 / audit_verify.py）读取并告警，无需逐调用点改造。
#
# 欠账必须**落库**：只留进程内 int 时 systemctl restart 即归零——而"锁竞争
# 导致审计写不进去"往往正是数据库已经出问题的时段，重启一次就把"有操作未留痕"
# 这件事连同证据一起忘掉。故以 app_meta.audit_write_fail_total 为权威（单调累加），
# 进程内只保留"落库也失败"的余额（那种时刻库本来就写不进，不能再放大故障）。
_AUDIT_FAIL_KEY = "audit_write_fail_total"
_AUDIT_FAIL_UNFLUSHED = 0
_AUDIT_FAIL_UNFLUSHED_DB = None
_AUDIT_FAIL_LOCK = threading.Lock()
# 审计写入重试：库级锁竞争导致的短暂失败值得重试，重试耗尽才计入欠账
_AUDIT_RETRIES = 3
_AUDIT_RETRY_BASE_DELAY = 0.2


def _bump_audit_write_failure():
    """欠账 +1（app_meta 单调累加）。落库失败时记在进程内余额上。"""
    global _AUDIT_FAIL_UNFLUSHED, _AUDIT_FAIL_UNFLUSHED_DB
    try:
        with _facade()._conn_lock:
            conn = _facade().get_conn()
            _facade()._begin_immediate(conn)
            try:
                row = conn.execute(
                    "SELECT value FROM app_meta WHERE key=?", (_AUDIT_FAIL_KEY,)
                ).fetchone()
                try:
                    total = int(str(row["value"]).strip() or 0) if row and row["value"] else 0
                except ValueError:
                    total = 0
                conn.execute(
                    "INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)",
                    (_AUDIT_FAIL_KEY, str(total + 1)),
                )
                conn.commit()
            except Exception:
                with contextlib.suppress(Exception):
                    conn.rollback()
                raise
        return True
    except Exception as e:
        # 欠账本身就是"库写不进去"时产生的，落这条计数也可能失败——余额留在进程内，
        # 至少本轮校验还能看见，绝不静默丢弃。
        with _AUDIT_FAIL_LOCK:
            _AUDIT_FAIL_UNFLUSHED += 1
            _AUDIT_FAIL_UNFLUSHED_DB = _connection._db_file
        logger.error("审计写入欠账落库失败（记入进程内余额）: %s", e)
        return False


def _unflushed_audit_failures():
    """进程内未落库余额；所属库已切换则视为 0（欠账是每个库各自的事实）。"""
    if _AUDIT_FAIL_UNFLUSHED_DB is not None and _connection._db_file != _AUDIT_FAIL_UNFLUSHED_DB:
        return 0
    return _AUDIT_FAIL_UNFLUSHED


def _reset_audit_fail_memory():
    """清空进程内余额（模拟进程重启/换库；生产路径不调用）。"""
    global _AUDIT_FAIL_UNFLUSHED, _AUDIT_FAIL_UNFLUSHED_DB
    with _AUDIT_FAIL_LOCK:
        _AUDIT_FAIL_UNFLUSHED = 0
        _AUDIT_FAIL_UNFLUSHED_DB = None


def audit_persisted_write_failures():
    """已落库的审计写入欠账（app_meta；读不到按 0，不抛）。"""
    return _meta_int(_AUDIT_FAIL_KEY)


def audit_write_failures():
    """累计的审计写入失败次数（供每日校验告警；0 = 无欠账）。

    = 已落库的累计值 + 本轮进程内未落库的余额。重启不再归零。
    """
    return audit_persisted_write_failures() + _unflushed_audit_failures()


# 欠账告警基线：总账单调累加（取证事实，不归零），但**告警按"账目变化"触发**。同一笔
# 欠账若每天都发一封 urgent，紧急额度会被它吃光、真告警反而发不出去（永久刷屏）；故另
# 存一条"已确认到的总账值"，只有总账高于它（有新欠账）才算新事件。归零口径：基线随发信
# 推进，总账不动——"续计"以总账为准，"不再重发"以基线为准。
_AUDIT_FAIL_NOTIFIED_KEY = "audit_write_fail_notified"
# 体检级告警签名基线：链/锚点/见证/欠账/重链任一变化才重发（同一故障态不刷屏）。
_AUDIT_ALERT_STATE_KEY = "audit_alert_state"


def audit_write_failures_unnotified():
    """自上次确认以来**新增**的审计欠账条数（无新增 → 0，不重复告警）。"""
    return max(0, audit_write_failures() - _meta_int(_AUDIT_FAIL_NOTIFIED_KEY))


def mark_audit_write_failures_notified():
    """把欠账告警基线推进到当前总账（告警发出后调用）；失败只告警不抛。"""
    return _facade().set_meta(_AUDIT_FAIL_NOTIFIED_KEY, str(audit_write_failures()))


def audit_alert_signature(health):
    """体检结果的告警签名：只有**内容变化**才值得再发一封 urgent。

    覆盖会独立改变结论的字段（链自洽/断点数、锚点三态、见证形态、欠账总账、空 hash
    行、是否有重链留痕）。不含消息文本（文本随同一事实抖动会造成假"变化"）。
    """
    return "|".join(str(x) for x in (
        health.get("chain_ok"), health.get("broken"), health.get("anchor_status"),
        health.get("anchor_witness"), health.get("write_failures"),
        health.get("empty_hash_rows"), bool(health.get("rechain_events")),
    ))


def audit_alert_needs_attention(health):
    """本次不健康结论是否与上次已告警的**不同**（纯读，无副作用）。"""
    return _facade().get_meta(_AUDIT_ALERT_STATE_KEY, "") != audit_alert_signature(health)


def mark_audit_alert_sent(health):
    """告警发出后推进签名基线，使同一故障态不再逐日重发。"""
    return _facade().set_meta(_AUDIT_ALERT_STATE_KEY, audit_alert_signature(health))


# ---------------------------------------------------------------------------
# 请求/会话作用域
# ---------------------------------------------------------------------------
# 审计的"来源"列只有加盐 IP 哈希，而其输入（X-Forwarded-For / remote_addr）是客户端
# 可控的 —— 它回答不了"同上出口的哪一次操作是谁做的"。故审计行额外携带**请求作用域
# id**：web 侧由 before_request 钩子为每个请求生成随机 id 写进线程局部，CLI/离线脚本
# 退化为进程级 id（pid + 进程认领时刻）。id 编码进 detail（不新增列、不改链构造；
# 链 HMAC 覆盖 detail，改不动），并以 `[req=...]` 标记自证。
# 明确不承诺：IP 哈希仍可伪造，作用域 id 只解决"同一出口内区分请求"，不解决身份。
_REQUEST_SCOPE = threading.local()
_PROCESS_SCOPE_SEEN = {}
_SCOPE_MARKER = " [req="
# 入参 detail 里出现的标记字面量消毒成该形态：与真标记不再相撞，也不像另一枚真标记
# （供下游按 `_SCOPE_MARKER in detail` 判定"是否已带作用域"时不会误判为已带）。
_SCOPE_MARKER_SANITIZED = " [req_"


def set_request_scope(rid):
    """设置当前线程的请求作用域 id（web before_request 钩子调用；None 清除）。"""
    _REQUEST_SCOPE.rid = rid or None


def current_request_scope():
    """当前线程的请求作用域 id；未设置返回 None（由 `_process_scope` 兜底）。"""
    return getattr(_REQUEST_SCOPE, "rid", None)


def _process_scope():
    """进程级作用域 id（CLI/离线脚本）：pid + 该进程首次认领的时刻，重启可区分。"""
    pid = os.getpid()
    seen = _PROCESS_SCOPE_SEEN.get(pid)
    if seen is None:
        seen = clock.now().strftime("%Y%m%d%H%M%S")
        _PROCESS_SCOPE_SEEN[pid] = seen
    return f"proc{pid}-{seen}"


def _scope_detail(detail, request_id=None):
    """把作用域 id 编码进 detail（重复调用只留一个真实标记）。

    两形态：detail 是 JSON 对象时注入 `"_req"` 键（保住 JSON 可解析——配置变更类审计
    的 detail 就是被下游 `json.loads` 还原"改了哪些键"的结构化记录，追加裸后缀会把它
    弄成非法 JSON 而丢掉还原能力）；其余情况追加 `[req=...]` 后缀。标记被截掉就答不了
    "哪个请求做的"，故先留足标记额度再截正文。

    入参 detail 中出现的 `[req=` 字面量一律先**消毒**（用户可控字段带进来的标记会被
    下方幂等判据误当成"已带作用域"，从而抑止真实 id 的附加——即让请求体伪造归属），
    再统一附加真实标记。

    **已知缺口**：JSON 对象序列化后超过 200 字符时，`"_req"` 键这条路径放不下，退化为
    追加后缀并截断，产出的 detail 不再是合法 JSON（下游按 JSON 还原会失败）。这是
    200 字符上限与"保住 JSON"两个目标在超长结构化 detail 下不可兼得时的取舍。
    """
    detail = detail or ""
    # 先消毒再附加：不让任何入参内容冒充真实作用域标记（见 docstring）
    detail = detail.replace(_SCOPE_MARKER, _SCOPE_MARKER_SANITIZED)
    rid = request_id or current_request_scope() or _process_scope()
    stripped = detail.strip()
    if stripped.startswith("{"):
        try:
            obj = json.loads(stripped)
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            # 真实作用域 id 覆盖入参里可能已被用户塞入的 `_req`（伪造归属），与
            # 非 JSON 分支的标记消毒同口径：只认本函数追加的值。
            obj["_req"] = rid
            out = json.dumps(obj, ensure_ascii=False)
            if len(out) <= 200:
                return out
    tag = f"{_SCOPE_MARKER}{rid}]"
    return (detail[: max(0, 200 - len(tag))] + tag)[:200]


def audit(username, action, target="", detail="", request_id=None):
    """记录关键管理操作（多管理员追溯；detail 需已脱敏）。

    `request_id` 为请求作用域 id；缺省取当前线程的请求作用域，再退化为进程级 id，
    最终以 `[req=...]` 标记附加在 detail 末尾（见上方作用域注释）。

    写入 HMAC 哈希链：prev_hash 取上一条 hash，hash 由 `_audit_hash` 对
    [prev_hash, ts, username, action, target, detail] 计算。**prev_hash 的读取必须与
    INSERT 同处一个 BEGIN IMMEDIATE 写事务**：_conn_lock 只管进程内，读上一条与 INSERT
    之间若没有跨进程互斥，web 多进程并发写会读到同一 prev_hash 造成链分叉（verify 断链）。

    失败口径（fail-loud）：失败先重试；重试耗尽仍失败则计 ERROR + 欠账落 app_meta
    （重启不归零，供每日校验告警）并返回 False。**不允许只记 WARNING 后返回 None 而吞掉
    失败**：锁等待超过 busy_timeout 时（长事务如 replace_accounts 整表重插、夜间批量签到
    与 web 争锁）业务接口照常成功，审计表里却没有这条记录，且因为是"没写进去"而非"写完被
    删"，哈希链依然自洽，verify 永远验不出问题。

    返回 True 表示已落库；False 表示重试耗尽仍未写入（调用方据此决定是否
    阻断业务）。既有调用点不检查返回值也不会出错，失败会由每日校验兜住。
    """
    conn = None
    ts = clock.now().strftime("%Y-%m-%d %H:%M:%S")
    detail = _scope_detail(detail[:200], request_id)
    last_err = None
    for attempt in range(_AUDIT_RETRIES):
        try:
            with _facade()._conn_lock:
                conn = _facade().get_conn()
                # 防御：正常路径所有写操作均已提交（with conn 模式），若前序调用遗留
                # 未提交事务，先解除——否则 BEGIN IMMEDIATE 会报 "within a transaction"。
                # 必须用【回滚】而不是提交来解除：写锁本质由未完成事务持有，回滚同样有效；
                # 而提交会把未知的半事务发布成持久数据（如 replace_accounts 中途可见的残表）。
                # 遗留事务本身是某条写路径未正确 commit/rollback 的 bug，应据堆栈定位修复。
                if conn.in_transaction:
                    logger.error(
                        "检测到遗留未提交事务，已回滚解除写锁（未知半事务按安全默认"
                        "丢弃）——请检查此前调用路径是否有写操作未 commit/rollback"
                        "（in_transaction 状态残留）"
                    )
                    conn.rollback()
                # IMMEDIATE：取 prev_hash 前先拿库级写锁，跨进程串行化"读尾→追加"
                _facade()._begin_immediate(conn)
                row = conn.execute(
                    "SELECT hash FROM audit_logs ORDER BY id DESC LIMIT 1"
                ).fetchone()
                prev_hash = row["hash"] if row else ""
                h = _facade()._audit_hash(prev_hash, ts, username, action, target, detail)
                conn.execute(
                    "INSERT INTO audit_logs (ts, username, action, target, detail, prev_hash, hash) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (ts, username, action, target, detail, prev_hash, h),
                )
                conn.commit()
            return True
        except Exception as e:
            last_err = e
            if conn is not None:
                with contextlib.suppress(Exception):
                    conn.rollback()
            if attempt < _AUDIT_RETRIES - 1:
                logger.warning(
                    "审计写入失败，%.1fs 后重试（第 %d/%d 次）: %s",
                    _AUDIT_RETRY_BASE_DELAY * (attempt + 1), attempt + 1, _AUDIT_RETRIES, e,
                )
                time.sleep(_AUDIT_RETRY_BASE_DELAY * (attempt + 1))
    _bump_audit_write_failure()
    logger.error(
        "审计写入最终失败（业务操作已生效但无留痕，重试 %d 次）: %s | action=%s target=%s",
        _AUDIT_RETRIES, last_err, action, target,
    )
    return False


class AuditWriteRefused(RuntimeError):
    """审计写入失败且要求 fail-closed：调用方必须回滚/拒绝对应的业务操作。

    只在 `audit_or_refuse` 抛出；`audit()` 的返回 False 口径不变（既有调用点不检查
    返回值，由每日欠账判据兜住）。
    """


def record_in_txn(conn, username, action, target="", detail="", request_id=None):
    """在**调用方已开启的写事务**内插入审计行（不 BEGIN、不 commit）。返回本条 hash。

    给"业务写与审计写同事务"的调用方用：业务写与这一步同一 BEGIN IMMEDIATE，调用方
    一次 commit 让两者同在、rollback 让两者同不在——中间被杀不会留下"做了无留痕、
    欠账仍为 0"（欠账计数结构性看不见这种丢法）。要求调用方已持 `_conn_lock` 且事务
    已开启：读链尾与 INSERT 之间若无跨进程互斥，会读到同一 prev_hash 造成链分叉。
    """
    ts = clock.now().strftime("%Y-%m-%d %H:%M:%S")
    detail = _scope_detail((detail or "")[:200], request_id)
    row = conn.execute(
        "SELECT hash FROM audit_logs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    prev_hash = row["hash"] if row else ""
    h = _facade()._audit_hash(prev_hash, ts, username, action, target, detail)
    conn.execute(
        "INSERT INTO audit_logs (ts, username, action, target, detail, prev_hash, hash) "
        "VALUES (?,?,?,?,?,?,?)",
        (ts, username, action, target, detail, prev_hash, h),
    )
    return h


@contextlib.contextmanager
def audit_unit(username, action, target="", detail="", request_id=None):
    """把一段业务写与它的审计行放进**同一个 BEGIN IMMEDIATE 事务**。

    用法：`with db.audit_unit(actor, "action", target, detail) as conn:` 体内用传入的
    conn 做业务写；退出时业务写与审计行一次 commit，抛异常则整体 rollback（业务与审计
    都不留）。业务写**必须用传入的 conn**——另开事务会撞 "within a transaction"，业务
    写自成事务则又把窗口还回来。

    **生产调用点不经本上下文**：现有同事务审计由 store 层各写函数内嵌 `record_in_txn`
    完成（`accounts.py` / `users.py` 的 `audit_spec` 参数、`purge_guard.py` 的
    `audit_or_refuse`），不新增一个"每个调用点各写一遍上下文"的面。本上下文是测试与
    自定义组合写路径的入口——`tests/test_audit_transaction.py` 用它复现 kill 注入，
    验证"未提交事务随进程退出被回滚"。

    与"先业务后 audit()"的差别：那两个事务之间被杀会留下"业务已生效、审计表无此条、
    欠账计数仍为 0"，本上下文消除该窗口（kill 注入下要么两者都在，要么都不在）。
    """
    with _facade()._conn_lock:
        conn = _facade().get_conn()
        _facade()._begin_immediate(conn)
        try:
            yield conn
            record_in_txn(conn, username, action, target, detail, request_id)
            conn.commit()
        except BaseException:
            with contextlib.suppress(Exception):
                conn.rollback()
            raise


def audit_or_refuse(username, action, target="", detail="", request_id=None):
    """fail-closed 审计：成功返回 True，失败抛 `AuditWriteRefused`。

    给**无法与业务并事务**的调用点用（业务写跨另一存储、另开连接或异步，SQLite 事务
    覆盖不到：.env 口令/清单写、跨库清库、后台任务）。这些路径必须"审计失败即拒绝
    业务"，而不是"先做业务再补审计"。范例见 `yiban/store/purge_guard.py` 的
    `write_purge_audit`（返回 False 时调用方放弃删除，本函数是同一口径的异常版）。
    """
    if _facade().audit(username, action, target, detail, request_id=request_id):
        return True
    raise AuditWriteRefused(
        f"审计写入失败，拒绝业务操作（fail-closed）: action={action} target={target}"
    )


def audit_head_hash():
    """审计链当前头哈希；空链返回 ""，**读取失败返回 None**。

    锚点存在的理由：HMAC 链密钥与数据同盘时"整体重算"零成本，
    把链头哈希定期追加到独立文件（web 每日线程写 STATE_DIR/audit-anchor.log），
    使重写库内审计链还需同步篡改锚点文件，外部锚定抬高伪造成本。

    读失败与空链**必须分开**：旧实现两者都返回 ""，"读不动"于是被当成"空链"，
    锚点/日报据此省略判定——把"没查"印成"没有"。调用方需要三态时用
    `audit_head_hash_ex`；本函数对读失败返回 None 作为兼容的显式降级信号。
    """
    state, head = audit_head_hash_ex()
    return head if state == "ok" else ("" if state == "empty" else None)


def audit_head_hash_ex():
    """审计链头读取的三态：`(state, head)`，state ∈ ok（有记录）/ empty（空链）/ error。

    空链是**查清了的结论**（head=""），读失败是**没查成**（head=None）；两者下游
    处置相反（空链可正常写锚点/报"空链"，读失败必须省略判定并告警）。
    """
    try:
        with _facade()._conn_lock:
            conn = _facade().get_conn()
            row = conn.execute(
                "SELECT hash FROM audit_logs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row is None or not (row["hash"] or ""):
                return "empty", ""
            return "ok", row["hash"]
    except Exception as e:
        logger.warning("读取审计链头失败: %s", e)
        return "error", None


def audit_row_count():
    """审计链当前记录总数（只读 COUNT，供日报锚点行等链状态对照）。

    刻意不吞异常：与 audit_head_hash 的"记日志返回空"不同，本函数让读取失败
    原样上抛，由调用方（日报的 try/except 纪律）决定省略——锚点行是取证对照
    数据，宁缺毋滥。
    """
    with _facade()._conn_lock:
        conn = _facade().get_conn()
        row = conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()
        return row[0] if row else 0


def verify_audit_chain():
    """校验审计哈希链。

    把当前表中 id 最小的一行视为链根：首行以自身 prev_hash 为锚校验 hash，
    不判首行 prev 断链（清理旧行后 prev_hash 指向已删除的前序行是合法状态）。
    从第二行开始要求 prev_hash 等于上一行 hash，且每行 hash 与内容匹配。

    返回 (ok, broken_count, first_broken_id)；broken_count=-1 表示校验过程异常
    或审计密钥缺失（fail-closed）。
    """
    try:
        key = _audit_key(create=False)
        if key is None:
            logger.warning("审计链校验失败: 未配置 YIBAN_AUDIT_KEY")
            return False, -1, None
        with _facade()._conn_lock:
            conn = _facade().get_conn()
            rows = conn.execute(
                "SELECT id, prev_hash, hash, ts, username, action, target, detail "
                "FROM audit_logs ORDER BY id"
            ).fetchall()
            if not rows:
                return True, 0, None
            prev = None
            broken = 0
            first_broken = None
            for r in rows:
                # 首行锚点取自身 prev_hash；后续行要求 prev_hash 与上一行 hash 一致
                anchor = r["prev_hash"] if prev is None else prev
                if prev is not None and r["prev_hash"] != prev:
                    broken += 1
                    if first_broken is None:
                        first_broken = r["id"]
                expected_hash = _facade()._audit_hash(
                    anchor, r["ts"], r["username"], r["action"], r["target"], r["detail"]
                )
                if r["hash"] != expected_hash:
                    broken += 1
                    if first_broken is None:
                        first_broken = r["id"]
                prev = r["hash"]
            return broken == 0, broken, first_broken
    except Exception as e:
        logger.warning("审计链校验失败: %s", e)
        return False, -1, None


# ---------------------------------------------------------------------------
# 审计链外部锚点
# ---------------------------------------------------------------------------
# verify_audit_chain() 只能检出"删中间"：首行以自身 prev_hash 自锚、空表直接判
# 通过（见其实现）。于是「删前缀 / 删尾 / 清空整表」三类篡改全部验不出来。
#
# 锚点记录 (min_id, max_id, head_hash) 三元组即可覆盖：
#   - 当前 max_id < 锚点 max_id            → 尾部被删（最危险：抹掉最近的记录）
#   - 表空但锚点 max_id > 0                → 整表被清空
#   - max_id 相同但 head_hash 不符          → 链尾内容被篡改
#   - 当前 min_id > 锚点 min_id            → 前缀被删（**不判失败**）
#
# 前缀删除刻意降级为「信息」而非「失败」：_audit_cleanup（保留 180 天）本身就是
# 删前缀的合法路径，判失败会让每日校验天天误报，反而淹没真告警。
#
# 锚点存放在**库外**：若存进 yiban.db，有写库权限者可连同审计表一起删掉，锚点
# 形同虚设。库外文件使"整体重写审计链"还需同步篡改该文件。
#
# 锚点行格式（空格分隔；时间戳本身含 1 个空格，故按 token 数区分版本，
# 解析一律从行尾取字段）：
#   v1（存量生产文件）：              <ts> <min_id> <max_id> <head>              = 5 token
#   v2（当前）：                        <ts> <min_id> <max_id> <count>
#                                        <purge_total> <head> <prev_line_hash>     = 8 token
# v2 新增三字段的用途——
#   count         锚定时刻链内行数 → "min..max 区间该有多少行"的稠密性判据数据源；
#   purge_total   锚定时刻已留痕的物理删除累计条数 → 只有锚点**之后**的留痕清理
#                 才能解释缺口，事后补写事件无法自证；
#   prev_line_hash 前一行原文 sha256 → 锚点文件自身成链，改写任一历史行/删中间行可检出。
# 读 v1 行时这三字段为 None（不是 0/""），依赖它们的判据自动降级，不误报。
_ANCHOR_V1_TOKENS = 5
_ANCHOR_V2_TOKENS = 8
#: 库内锚点指纹：{"lines": int, "last_hash": str, "ts": str}
_ANCHOR_META_KEY = "audit_anchor_meta"
#: audit_logs 物理删除留痕：累计条数（int）+ 最近若干条事件（JSON 列表）
_AUDIT_PURGE_TOTAL_KEY = "audit_purge_total"
_AUDIT_PURGE_EVENTS_KEY = "audit_purge_events"
#: 留痕事件列表上限（app_meta 单值不宜无界增长；只保留最近 N 条足够追溯）
_PURGE_EVENTS_KEEP = 200
#: 全表重链留痕（migrate_v3 每次重签整条链都记一条：ts/来源版本/行数/重链前后 head）
_RECHAIN_EVENTS_KEY = "audit_rechain_events"
_RECHAIN_EVENTS_KEEP = 50
#: 文件首行的"前驱哈希"哨兵。必须是非空定长串——写成空串会让行尾空格在 split()
#: 后少一个 token，整行变得不可解析（曾导致每日误报"锚点文件被删除"）。
_ANCHOR_GENESIS = "0" * 64

#: 独立见证指纹文件的部署目录（与锚点文件**不同目录、不同属主**）。
#: 生产由 deploy/prod/install.sh 预建、root 侧定时进程写入，应用身份只读——这是
#: "锚点 + 库内指纹同属应用，拿到应用写权限者可双写掩盖删审计"的唯一补丁。
#: 开发/测试环境（os.name == "nt"）落当前目录，属主与锚点相同，即降级形态。
_ANCHOR_FP_DEFAULT_DIR_POSIX = "/var/lib/yiban-audit"
_ANCHOR_FP_FILENAME = "anchor-fingerprint.json"


def audit_anchor_fingerprint_path():
    """独立见证指纹文件路径。

    刻意**不**跟随 YIBAN_STATE_DIR：锚点文件在状态目录（/var/log/yiban），若见证
    也落在那里，两者同属主同目录，应用身份可同时改写 ⇒ 跨权限保护消失。生产路径
    由 install.sh 创建（0755 root）、root 侧进程写 0644，应用只读。

    开发/测试（Windows）无特权目录可用，落当前目录下的 yiban-audit-witness/，
    属主与锚点相同；此形态下独立文件对"同 uid 双写者"不构成额外阻力，残余风险
    在 `_anchor_witness_state` 与 `_anchor_file_state_ex` 的注释里写明。
    """
    if os.name == "nt":
        return os.path.join(".", "yiban-audit-witness", _ANCHOR_FP_FILENAME)
    return os.path.join(_ANCHOR_FP_DEFAULT_DIR_POSIX, _ANCHOR_FP_FILENAME)


def audit_anchor_path():
    """外部锚点文件路径（库外 append-only）。

    默认与 web/app.py 的 STATE_DIR 对齐（/var/log/yiban），不能用 "."（进程 cwd）：
    否则裸机部署下 web 把锚点写到 /var/log/yiban/audit-anchor.log，
    而 audit_health 读 <cwd>/audit-anchor.log：每日误报「锚点文件被删除」淹没真告警，
    且删尾/清空/链尾篡改检测从未比对过真实锚点（锚点防线整体致盲）。
    Windows 开发/测试环境保留 "."（/var/log 不可写）。

    YIBAN_STATE_DIR 按 `env_io.resolve_path` 解析（进程环境 → .env → 默认值），
    与写入方（web 的 STATE_DIR、run.sh）同一口径：只写进 .env 的部署读侧同样生效。
    此前只读 `os.environ`，写侧却认得 .env——审计锚点被写到别处而校验读默认目录，
    锚点防线致盲且 audit_verify 以退出码 1 误报链破。
    """
    default_dir = "." if os.name == "nt" else "/var/log/yiban"
    state_dir = env_io.resolve_path("YIBAN_STATE_DIR", default_dir)
    return os.path.join(state_dir, "audit-anchor.log")


def _anchor_line_sha(text):
    """锚点行原文哈希（行间链用）。刻意用无密钥 sha256：锚点文件的定位是"抬高
    伪造成本 + 留下可追改动"，不是消息认证——密钥与数据同盘时任何 MAC 都可被
    同一权限重算，真正兜住"整体重写"的是离机副本（日报邮件 / 异机备份）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_anchor_line(ln):
    """解析单条锚点行；不可解析返回 None。

    字段一律**从行尾**取——时间戳本身含空格（"YYYY-MM-DD HH:MM:SS"），从头按下标
    取会整体错位一格。v1 行缺失的三个字段返回 None（而非 0/""），让调用方能区分
    "值为 0" 与"该行根本没有这个字段"，避免旧行被当成 count=0 误判。
    """
    parts = ln.split()
    n = len(parts)
    try:
        if n == _ANCHOR_V2_TOKENS:
            return {
                "version": 2,
                "ts": " ".join(parts[:-6]),
                "min_id": int(parts[-6]),
                "max_id": int(parts[-5]),
                "count": int(parts[-4]),
                "purge_total": int(parts[-3]),
                "head": parts[-2],
                "prev_line_hash": parts[-1],
            }
        if n == _ANCHOR_V1_TOKENS:
            return {
                "version": 1,
                "ts": " ".join(parts[:-3]),
                "min_id": int(parts[-3]),
                "max_id": int(parts[-2]),
                "head": parts[-1],
                "count": None,
                "purge_total": None,
                "prev_line_hash": None,
            }
    except ValueError:
        return None
    return None


def _read_anchor_lines(path):
    """锚点文件的全部非空行（保持顺序）。文件不存在/不可读返回 None（区别于 []）。"""
    lines, _status = _read_anchor_lines_ex(path)
    return lines


def _read_anchor_lines_ex(path):
    """同 `_read_anchor_lines`，但把"为什么没读到"一并返回：`(lines, status)`。

    status 取值：
      "ok"            读到了（可能为空列表）；
      "missing"       文件不存在；
      "io-error"      文件存在但不可读（权限/IO 错误）；
      "decode-error"  内容不是合法 UTF-8。

    区分四态是必需的：若让 `UnicodeDecodeError` 抛出，调用方的兜底 except 会把它
    吞成一条 WARNING ⇒ **当日整段校验不执行**（一条非法字节就能让审计自检静默）；
    把 "decode-error"/"io-error" 与 "missing" 混为一谈又会把"读不出"印成"无异常"。
    调用方对所有非 "ok" 一律按"无法定论"处理，绝不当无异常。
    """
    if not os.path.exists(path):
        return None, "missing"
    try:
        with open(path, encoding="utf-8") as f:
            return [ln.strip() for ln in f if ln.strip()], "ok"
    except UnicodeDecodeError:
        return None, "decode-error"
    except OSError:
        return None, "io-error"


def _get_anchor_meta():
    """读库内锚点指纹，原样返回写入方存进 app_meta 的那份 dict
    {"lines","last_hash","ts"}；无记录/缺表/JSON 损坏/非 dict → {}。

    三个键并非都有读者：包内唯一的调用方 `_anchor_file_state` 只读 `lines` 与
    `last_hash`，`ts` **自写入后无人读取**——它与同一事务里写入的 `audit_anchor_last`
    键逐字相同（见 `_record_anchor_trace`），"锚点曾存在"这条判据由那一键承担。
    `db._get_anchor_meta` 是本函数的转发别名，全仓无人经它调用（含 tests/）。
    删这个键会改变库内 JSON 的形状（属行为变更），故这里保持原样返回整份 dict，
    既不补消费方，也不在读取侧改写它。
    """
    raw = _facade().get_meta(_ANCHOR_META_KEY, "")
    if not raw:
        return {}
    try:
        val = json.loads(raw)
    except ValueError:
        return {}
    return val if isinstance(val, dict) else {}


def _anchor_meta_line_count(meta):
    """库内锚点指纹的 `lines` 字段 → `(计数, 状态)`，status ∈ ok/corrupt。

    `lines` 住在**应用可写**的 app_meta（audit_anchor_meta），与独立见证文件里的
    同名整数字段同等对待：非数字值（手工损坏/拼接/旧格式）时若直接 int() 会抛
    ValueError，而两个调用方分别位于 `_anchor_status` 与 `audit_health` 的 try 之外
    ——一次手工损坏即让每日体检整体抛异常、被 web 日线线程吞成 WARNING，当日校验
    静默不跑（正是"把查不动印成无异常"的失败形态）。故就地降级为 corrupt，由调用方
    转成"无法定论 ⇒ 不健康"，绝不外抛。
    """
    if not meta:
        return 0, "ok"
    raw = meta.get("lines")
    if raw is None:
        return 0, "ok"
    try:
        return int(raw), "ok"
    except (TypeError, ValueError):
        return 0, "corrupt"


def _audit_purge_total(conn):
    """累计"已留痕的物理删除条数"（audit_logs 口径）。缺表/缺键 → 0。

    本函数刻意不抛也不吞出锁：调用方要么持有 _conn_lock 并传入共享连接，
    要么走 get_meta（自取锁）。清理留痕的写入方见 _record_purge_event。
    """
    try:
        row = conn.execute(
            "SELECT value FROM app_meta WHERE key=?", (_AUDIT_PURGE_TOTAL_KEY,)
        ).fetchone()
    except sqlite3.Error:
        return 0
    if row is None or row["value"] is None:
        return 0
    try:
        return int(str(row["value"]).strip() or 0)
    except ValueError:
        return 0


def _meta_json_list(conn, key):
    """读 app_meta 里 key 的 JSON 列表；缺表/缺键/JSON 损坏/非列表 → []。

    `_audit_purge_events` 与 `_rechain_events` 是它的两个包装：读取口径只有这一处，
    改动 sqlite3.Error 兜底或 isinstance 列表校验时，两个调用方的"损坏按空列表"承诺
    一起变（缺一个都会让留痕判据把损坏误当"没有留痕"）。
    """
    try:
        row = conn.execute(
            "SELECT value FROM app_meta WHERE key=?", (key,)
        ).fetchone()
    except sqlite3.Error:
        return []
    if row is None or not row["value"]:
        return []
    try:
        val = json.loads(row["value"])
    except ValueError:
        return []
    return val if isinstance(val, list) else []


def _meta_int(key):
    """读 app_meta 里 key 的整数值（走 get_meta 自取锁）；缺失/损坏 → 0。

    包装与直查同构：`audit_persisted_write_failures`（审计欠账）与 `audit_purge_total`
    （清理留痕累计）共用这一处整数解析与 ValueError 兜底，改一处须两处的"读不到按 0"
    一起改——两处都是告警判据的输入，语义漂移会让体检结果对不上。
    """
    try:
        return int(str(_facade().get_meta(key, "0")).strip() or 0)
    except ValueError:
        return 0


def _audit_purge_events(conn):
    """物理删除留痕事件列表（app_meta JSON）。缺表/JSON 损坏 → []。"""
    return _meta_json_list(conn, _AUDIT_PURGE_EVENTS_KEY)


def audit_purge_total():
    """audit_logs 累计物理删除条数（公开只读，供体检/排障；无记录 → 0）。"""
    return _meta_int(_AUDIT_PURGE_TOTAL_KEY)


def audit_purge_events():
    """物理删除留痕事件（公开只读，最新在末尾；表缺失/损坏 → []）。"""
    try:
        with _facade()._conn_lock:
            conn = _facade().get_conn()
            return _audit_purge_events(conn)
    except Exception as e:
        logger.warning("读取物理删除留痕失败: %s", e)
        return []


def record_audit_anchor(path=None):
    """把当前审计链状态追加到外部锚点文件（v2 行格式见上方注释）。

    返回写入的锚点行；无审计记录、链头读取失败或写入失败返回 None（不阻断调用方）。
    建议在每日清理之后调用，使锚点反映清理后的合法状态。

    链头为空时**拒绝写行**而非写一条空字段：少一个 token 的行会被后续解析整体
    错位（与 _last_audit_anchor 的"从行尾取字段"纪律冲突），宁缺毋滥。
    """
    path = path or audit_anchor_path()
    try:
        with _facade()._conn_lock:
            conn = _facade().get_conn()
            # 顺序有讲究：先读**单调计数器** purge_total，再取行快照。反过来的话，
            # 两次读之间发生的物理删除会被算进"锚点之前"的额度，而锚点记的行数却是
            # 删除后的——校验时"少了行却没有对应留痕"，合法的保留期清理会被判成篡改。
            purge_total = _facade()._audit_purge_total(conn)
            row = conn.execute(
                "SELECT MIN(id) AS min_id, MAX(id) AS max_id, COUNT(*) AS n FROM audit_logs"
            ).fetchone()
            # 链头必须**按 max_id 取值**，不能另取"当前最后一行"：后者是第二次读，
            # 并发写入落在两次读之间时，锚点行的 max_id 与 head 指向不同行，此后每次
            # 校验都会报"链尾内容被篡改"（旧判据在 max_id 不等时会跳过比对，反而不报）。
            anchored = (conn.execute("SELECT hash FROM audit_logs WHERE id=?",
                                     (int(row["max_id"]),)).fetchone()
                        if row and row["max_id"] is not None else None)
        if not row or row["max_id"] is None:
            return None
        min_id, max_id, count = int(row["min_id"]), int(row["max_id"]), int(row["n"])
        head = (anchored["hash"] or "") if anchored else ""
        if not head:
            logger.warning("审计链头读取失败（空值），本次不写锚点行")
            return None
        ts = clock.now().strftime("%Y-%m-%d %H:%M:%S")
        # 行间链：prev_line_hash 取**前一行原文**的哈希，旧格式（v1）行同样参与
        # 链——否则攻击者只要删掉文件末尾的 v1 行，剩余行依然自洽，无从发现。
        lines, read_state = _read_anchor_lines_ex(path)
        if read_state not in ("ok", "missing"):
            # 读不出（非法 UTF-8/权限）时**拒绝追加**：按空列表续写会把行间链接到
            # 一个读不出来的前缀上（GENESIS 假前驱），既掩盖现场又让文件此后永远
            # 解析失败。无法定论状态下宁缺毋滥，由每日告警催人修文件。
            logger.error("锚点文件不可解析（%s），拒绝追加新锚点行以免破坏行间链", read_state)
            return None
        lines = lines or []
        prev_line_hash = _anchor_line_sha(lines[-1]) if lines else _ANCHOR_GENESIS
        line = f"{ts} {min_id} {max_id} {count} {purge_total} {head} {prev_line_hash}"
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        # 锚点落盘成功后在 app_meta 留痕——锚点文件本身可被整删
        # （删掉后校验降级为"通过"），库内元数据使其"应存在却消失"可被检出
        _record_anchor_trace(path, ts)
        return line
    except Exception as e:
        logger.warning("审计链锚点写入失败: %s", e)
        return None


def _record_anchor_trace(path, ts):
    """在 app_meta 留下锚点文件的指纹与"曾经存在"的痕迹。

    两把钥匙各有分工：
    - audit_anchor_last（仅 ts，历史兼容键）：锚点文件被**整体删除**时，
      verify_audit_anchor 仍能判"曾写过锚点却不见了"；
    - audit_anchor_meta（行数 + 末行哈希）：锚点文件被**截断/改写末行**时检出。
      行间链对"删掉最后一行"无效（剩余行彼此仍自洽，没有后继行去哈希它），
      必须有库内指纹交叉对照。行数只增不减——被截断后即使补写一行把长度凑回来，
      本次指纹仍停留在更高的历史值上（见 _anchor_file_state）。
    """
    lines, read_state = _read_anchor_lines_ex(path)
    if read_state not in ("ok", "missing"):
        # 读不出时不能按空列表写指纹：那会把库内行数高水位冲成 0、末行哈希冲成空，
        # 之后正常修复好的锚点反而因"行数变多"被判红。宁可本次不更新指纹。
        logger.error("锚点文件不可解析（%s），本次不更新库内指纹（保持原高水位）", read_state)
        return
    lines = lines or []
    try:
        with _facade()._conn_lock:
            conn = _facade().get_conn()
            _facade()._begin_immediate(conn)
            prev_raw = conn.execute(
                "SELECT value FROM app_meta WHERE key=?", (_ANCHOR_META_KEY,)
            ).fetchone()
            recorded = 0
            if prev_raw and prev_raw["value"]:
                with contextlib.suppress(ValueError):
                    recorded = int(json.loads(prev_raw["value"]).get("lines") or 0)
            payload = json.dumps(
                {
                    "lines": max(recorded, len(lines)),
                    "last_hash": _anchor_line_sha(lines[-1]) if lines else "",
                    "ts": ts,
                },
                ensure_ascii=False,
            )
            conn.execute(
                "INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)",
                (_ANCHOR_META_KEY, payload),
            )
            conn.execute(
                "INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)",
                ("audit_anchor_last", ts),
            )
            conn.commit()
    except Exception as meta_err:
        logger.warning("锚点元数据留痕失败（不影响锚点本身）: %s", meta_err)


def _parse_anchor_lines(lines):
    """把锚点行文本列表解析成已解析行列表（不可解析的行被丢弃，保序）。"""
    out = []
    for ln in lines:
        parsed = _parse_anchor_line(ln)
        if parsed is not None:
            out.append(parsed)
    return out


def _last_anchor_of(lines):
    """最后一条可解析的锚点行；没有返回 None。"""
    parsed = _parse_anchor_lines(lines)
    return parsed[-1] if parsed else None


def _max_anchor_of(lines):
    """`max_id` 最大的那条可解析锚点行（并列取靠后的）。

    校验基准取"最大真行"而不是"最后一行"：追加一行**旧状态**的锚点（max_id 更小）
    即可把基准换成一个与现状自洽的旧记录，让删尾判据对着错的锚点算。取最大 max_id
    使基准只会被更强的记录替换；伪造一条更大的 max_id 也无用——那一行在库内不存在
    或哈希对不上，定点判据随即为红。

    并列必须取靠后（最新自报）而非靠前：清理后补锚、链尾重签后重锚都会追加
    max_id 相同的新行，只有最新一行描述库内现状，取靠前会拿陈旧 head 定点、把
    合法重锚误判成篡改。`max()` 并列返回首个，故这里显式 >= 扫描。
    """
    parsed = _parse_anchor_lines(lines)
    if not parsed:
        return None
    best = parsed[0]
    for p in parsed[1:]:
        if p["max_id"] >= best["max_id"]:
            best = p
    return best


def _last_audit_anchor(path):
    """读取最后一条有效锚点行；无锚点或格式不符返回 None。

    行格式见 _ANCHOR_V1_TOKENS / _ANCHOR_V2_TOKENS 附近说明。字段**从行尾**取——
    时间戳本身含空格，从头按下标取会整体错位一格。

    兼容三种形态：v2（8 token，含 count/purge_total/prev_line_hash）、
    v1（5 token）、更旧的 `ts head`（3 token，int() 转换失败即跳过，
    不参与判定，避免升级后误报）。
    """
    lines = _read_anchor_lines(path)
    if not lines:
        return None
    return _last_anchor_of(lines)


def _read_anchor_fingerprint(path):
    """读独立见证指纹：`(state, data)`，state ∈ missing/unreadable/corrupt/ok。

    损坏与不可读**不返回 None**：那会被上层当成"从未写过见证"而静默降级，
    正是"把读不出印成无异常"的等价类。
    """
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        return "missing", None
    except OSError:
        return "unreadable", None
    try:
        val = json.loads(raw)
    except ValueError:
        return "corrupt", None
    if not isinstance(val, dict) or not isinstance(val.get("lines"), int):
        return "corrupt", None
    return "ok", val


def _witness_db_path():
    """独立见证回库核对所用的库路径（与 app 侧同一解析口径）。

    `env_io.resolve_path` 走 **进程环境 → YIBAN_ENV_FILE 指到的 .env → 默认值**：root
    侧 cron 已 `cd APP_DIR` 并设 `YIBAN_ENV_FILE`，所以这里读到的是这台部署实际在用的
    那个库，而不是按脚本 cwd 猜。未声明 `YIBAN_DB_FILE` 时回落到应用默认相对路径
    （`yiban.db`，相对 cwd=APP_DIR）——纯状态文件部署该文件不存在，走无库降级。
    """
    declared = env_io.resolve_path("YIBAN_DB_FILE", "")
    return declared or _connection.DB_DEFAULT


def _witness_db_snapshot(db_path=None):
    """只读回库取**真实**链尾：`{db_backed, db_max_id, db_head_hash, db_prev_hash}`。

    锚点行记的 max_id/head 是应用自报值；应用身份把锚点与库内指纹一起改写后两者彼此
    自洽，锚点自检看不出"锚点之后新增的审计被删"。本函数由 root 侧见证进程调用，**以
    `file:...?mode=ro` 只读 URI** 打开库（`PRAGMA query_only` 第二道），把库内真实
    `max(id)` 与该行的 `hash`/`prev_hash` 记进见证 JSON——回库判据据此检出删尾。

    只读是硬约束：见证进程一旦写库，就等于把独立见证接回被审计面（root 侧写库会
    同时改掉库内指纹，双写掩盖重新成立）。库文件不存在（纯状态文件部署形态）→
    `db_backed=False`，只记锚点字段，判据走文档化降级；库存在但读不出 → 记错误日志
    并同样 `db_backed=False`（不拿读失败冒充"没有库"的结论，故调用方在库已声明时
    会把这种降级当作需人工核查的形态）。
    """
    info = {"db_backed": False, "db_max_id": None,
            "db_head_hash": None, "db_prev_hash": None}
    path = db_path or _witness_db_path()
    if not path or not os.path.exists(path):
        return info
    try:
        uri = Path(os.path.abspath(path)).as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
    except (sqlite3.Error, ValueError) as e:
        logger.error("独立见证回库打开失败（只读），本次不记库内真值: %s", e)
        return info
    try:
        conn.execute("PRAGMA query_only=1")
        row = conn.execute(
            "SELECT id, hash, prev_hash FROM audit_logs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error as e:
        logger.error("独立见证回库读取失败（只读），本次不记库内真值: %s", e)
        return info
    finally:
        conn.close()
    info["db_backed"] = True
    if row is not None and row[0] is not None:
        info["db_max_id"] = int(row[0])
        info["db_head_hash"] = row[1] or ""
        info["db_prev_hash"] = row[2] or ""
    return info


def record_audit_anchor_witness(anchor_path=None, fingerprint_path=None, db_path=None):
    """独立见证：把锚点文件当前指纹写进库外、**与锚点不同属主**的独立文件。

    与 `record_audit_anchor` 的分工：后者以应用身份写锚点行并同步库内指纹，两者
    同属一个身份；拿到应用写权限的人可以同时改写锚点行与库内指纹，把"最近 N 条
    审计被删"伪装成自洽状态。本函数由部署方 root 侧定时进程调用，只读锚点与只读
    回库取真值、只写独立文件——改写它需要另一份权限，双写掩盖才会留下缺口。

    单调写，只前进不后退，且**见证记录的那一行必须还在原位且内容未变**：行数变少、
    或该行内容变化（含把整份锚点重写成更多行的填充变体）时**拒绝覆盖**并记错误日志
    ——那正是篡改现场，覆盖掉等于替攻击者擦证据。合法追加（原前缀完整）照旧放行。

    除锚点自报字段外，还经 `_witness_db_snapshot` **只读回库**记下真实 `max(id)` 与该行
    的 `hash`/`prev_hash`（`db_backed/db_max_id/db_head_hash/db_prev_hash`）：锚点记的
    max_id/head 是应用自报值，锚点之后新增的审计（可在锚点后约 24h 内）对锚点不可见，
    只删这些行即可抹掉最近审计——回库真值把该窗口缩到本进程的 cron 间隔。无库部署
    （纯状态文件形态）记 `db_backed=False`，走文档化降级。

    返回 `(state, message)`，state ∈ written/unchanged/regression/rewritten/chmod-failed/
    unparseable/corrupt/unreadable/no-anchor。
    """
    ap = anchor_path or audit_anchor_path()
    fp = fingerprint_path or audit_anchor_fingerprint_path()
    lines, status = _read_anchor_lines_ex(ap)
    if status != "ok" or not lines:
        return "no-anchor", f"锚点不可用（{status}），本次不写独立见证"
    last = _parse_anchor_line(lines[-1])
    if last is None:
        return "unparseable", "锚点末行无法解析，本次不写独立见证（不拿坏值当真值）"
    curr = {
        "lines": len(lines),
        "line_hash": _anchor_line_sha(lines[-1]),
        "max_id": last["max_id"],
        "head": last["head"],
        "count": last.get("count"),
        "purge_total": last.get("purge_total"),
        "ts": clock.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    # 回库记真实链尾，与锚点自报字段并列写进见证 JSON（删尾判据的输入）。
    curr.update(_witness_db_snapshot(db_path))
    prev_state, prev = _read_anchor_fingerprint(fp)
    if prev_state in ("corrupt", "unreadable"):
        logger.error("独立见证指纹文件%s，拒绝覆盖（需人工核查）: %s",
                     "损坏" if prev_state == "corrupt" else "不可读", fp)
        return prev_state, f"独立见证文件{prev_state}，拒绝覆盖"
    if prev:
        # 前缀不变性：**只要行数不减，见证记录的那一行就必须还在原位且内容未变**。
        # 旧实现只比"行数相等"时的末行哈希，行数变多则无内容校验地覆盖——把锚点整份
        # 重写（删审计后伪造自洽）并填充到比见证更多行，root cron 便把伪造态 bless 成
        # 新见证，此后永久 healthy。合法追加（原前缀完整）照旧放行。
        pn = int(prev.get("lines", 0))
        if curr["lines"] < pn:
            logger.error("锚点文件行数 %s 少于独立见证记录的 %s，拒绝回退覆盖（疑似篡改现场）",
                         curr["lines"], pn)
            return "regression", "锚点行数少于独立见证，拒绝覆盖"
        if pn < 1 or len(lines) < pn or _anchor_line_sha(lines[pn - 1]) != prev.get("line_hash"):
            logger.error("独立见证记录的第 %s 行已不在原位或内容已变，拒绝覆盖（疑似篡改现场）", pn)
            return "rewritten", "见证记录的那一行必须还在原位且内容未变，拒绝覆盖"
        if (curr["lines"] == pn
                # 锚点本身无变化；若见证尚未带库内真值、或库内链尾已推进，则仍需重写
                # 见证（回库窗口靠这里从 ~24h 缩到 cron 间隔）——否则返回 unchanged。
                and prev.get("db_backed") == curr["db_backed"]
                and (not curr["db_backed"] or prev.get("db_max_id") == curr["db_max_id"])
                and prev.get("max_id") == curr["max_id"]
                and prev.get("head") == curr["head"]):
            return "unchanged", ""
    d = os.path.dirname(fp)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = fp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(curr, f, ensure_ascii=False, sort_keys=True)
    # 0644：应用身份要能**读**（校验需要），但只有写入者（root）能改。权限设不上就
    # 拒绝替换——落一份应用读不到的见证，等于独立见证静默失效（校验降级为 absent）。
    try:
        os.chmod(tmp, 0o644)
    except OSError as e:
        logger.error("独立见证文件权限设置失败（应用将读不到），拒绝替换: %s", e)
        with contextlib.suppress(OSError):
            os.remove(tmp)
        return "chmod-failed", "见证文件权限设置失败，拒绝替换"
    os.replace(tmp, fp)
    # 落位后复核最终权限：替换路径上仍可能被父目录默认 ACL/umask 改写。
    try:
        mode = os.stat(fp).st_mode & 0o777
    except OSError as e:
        logger.error("独立见证文件落位后 stat 校验失败: %s", e)
        return "chmod-failed", "见证文件落位后无法确认权限"
    if os.name != "nt" and mode != 0o644:
        logger.error("独立见证文件落位后权限为 %o（期望 644），应用可能读不到", mode)
        with contextlib.suppress(OSError):
            os.chmod(fp, 0o644)
        with contextlib.suppress(OSError):
            if os.stat(fp).st_mode & 0o777 == 0o644:
                return "written", ""
        return "chmod-failed", "见证文件落位后权限仍不符（应用可能读不到）"
    return "written", ""


def _witness_owner_state(anchor_path, fingerprint_path):
    """独立文件与锚点文件是否属主分离：separate / same-owner / unknown。

    只做"两者 st_uid 是否不同"这一条可移植判据（Windows 无 st_uid → unknown）。
    不拿"目录是否 root"推断：文件可能被部署到任意受控目录，属主比对才是事实。
    """
    try:
        a_uid = os.stat(anchor_path).st_uid
        f_uid = os.stat(fingerprint_path).st_uid
    except (OSError, AttributeError):
        return "unknown"
    return "separate" if a_uid != f_uid else "same-owner"


def _witness_dir_installed(fingerprint_path):
    """独立见证的**部署目录**是否已由安装器预建（区分"未装"与"装了但坏"）。

    deploy/prod/install.sh 预建该目录（POSIX `/var/lib/yiban-audit`，root 属主 0755，
    应用只读）。目录存在而见证文件缺失/读不出 ⇒ 生产形态的独立见证控制面不可用，
    按安装损坏判不健康；目录不存在 ⇒ 从未安装独立见证（开发/单用户/纯状态文件形态），
    保持文档化降级（只提示）。目录才是安装器留下的可判事实——只比文件存在分不开
    "从未安装"与"装好却被删"。
    """
    d = os.path.dirname(fingerprint_path) or "."
    try:
        return os.path.isdir(d)
    except OSError:
        return False


def _anchor_witness_state(lines, meta, fingerprint_path, anchor_path=None):
    """独立见证与锚点文件/库内指纹的三方一致性；返回 `(status, msg, state, data)`。

    三方（库内指纹 / 锚点旁路即文件末行自报 / 独立文件）任一不一致即红。判据在
    锚点文件一侧只做两件事：见证记录的那一行必须还在原位且内容未变；库内指纹不得
    落后于见证。见证自己记的 max_id/head 与库内行的比对在 `_anchor_status` 内做
    （那里已有库连接）；同一见证记的 `db_max_id`（root 侧只读回库的真值）与该行
    哈希也在那里回库核对——`db_max_id` 小于其自报 `max_id` 的倒挂且在此处即判红。

    state（anchor_witness）区分独立文件是否真的"跨权限"：separate（属主不同，生产
    形态）/ same-owner（**降级形态**）/ anchor-missing（锚点不可用时的显式降级值）/
    absent / unreadable / corrupt。

    残余风险（same-owner 与 absent）：独立文件与锚点同属主时，同 uid 双写者能把两份
    一起改，本判据只剩"抗误删/抗意外损坏"的价值，挡不住有意掩盖；生产必须让
    install.sh 用 root 建目录、root 侧进程写文件，才能拿到跨权限保护。
    """
    state, fp = _read_anchor_fingerprint(fingerprint_path)
    if state == "missing":
        return "ok", "", "absent", None
    if state in ("unreadable", "corrupt"):
        return (
            "indeterminate",
            f"独立见证指纹文件存在但{'损坏' if state == 'corrupt' else '不可读'}"
            "——无法判断锚点是否被改写，校验无法定论（不等于无异常）",
            state, None,
        )
    owner = _witness_owner_state(anchor_path or audit_anchor_path(), fingerprint_path)
    # 数值字段先就地判可解析：JSON 合法但字段非数字（手工损坏/拼接）时 int() 会抛
    # ValueError，而本函数由 `_anchor_status` 的 try 之外调用——那样一次手工损坏就把
    # 每日体检印成一次崩溃，而不是"控制面损坏 ⇒ 不健康"。归类为 corrupt 降级。
    try:
        n = int(fp["lines"])
    except (TypeError, ValueError):
        return (
            "indeterminate",
            "独立见证指纹文件的行数字段无法解析为整数（文件被手工损坏或伪造）"
            "——校验无法定论（不等于无异常），请立即核查",
            "corrupt", fp,
        )
    if len(lines) < n:
        return (
            "tampered",
            f"锚点文件仅 {len(lines)} 行，早于独立见证记录的 {n} 行——锚点被截断"
            "（见证由应用之外的身份写入，非应用可改写）",
            owner, fp,
        )
    if _anchor_line_sha(lines[n - 1]) != fp.get("line_hash"):
        return (
            "tampered",
            f"独立见证记录的第 {n} 行内容已变——锚点历史被改写",
            owner, fp,
        )
    recorded, meta_state = _anchor_meta_line_count(meta)
    if meta_state == "corrupt":
        return (
            "indeterminate",
            "库内锚点指纹的行数字段无法解析为整数（app_meta 被手工损坏或改写）"
            "——校验无法定论（不等于无异常），请立即核查",
            "corrupt", fp,
        )
    if recorded and recorded < n:
        return (
            "tampered",
            f"库内锚点指纹行数 {recorded} 落后于独立见证的 {n} 行——库内指纹被回滚",
            owner, fp,
        )
    if recorded == n and meta.get("last_hash") and meta["last_hash"] != fp.get("line_hash"):
        return (
            "tampered",
            "库内锚点指纹末行哈希与独立见证不符——库内指纹被改写",
            owner, fp,
        )
    # 见证自身跨字段一致性：库内真实 max(id) 不可能小于同一时刻锚点自报的 max_id
    # （锚点记的就是库内链尾）。两者倒挂说明见证 JSON 被伪造或拼接，不是自洽记录。
    # 两个字段同样可能被手工改坏成非数字——就地按 corrupt 降级（见上）。
    try:
        max_id = int(fp["max_id"]) if fp.get("max_id") is not None else None
        db_max_id = int(fp["db_max_id"]) if fp.get("db_max_id") is not None else None
    except (TypeError, ValueError):
        return (
            "indeterminate",
            "独立见证指纹文件的 max_id/db_max_id 字段无法解析为整数"
            "（文件被手工损坏或伪造）——校验无法定论（不等于无异常），请立即核查",
            "corrupt", fp,
        )
    if fp.get("db_backed") and db_max_id is not None and max_id is not None \
            and db_max_id < max_id:
        return (
            "tampered",
            "独立见证自身不一致：库内真实 max_id 小于其锚点自报 max_id——见证被伪造",
            owner, fp,
        )
    return "ok", "", owner, fp


def _anchor_file_state_ex(path=None, lines=None, meta=None, fingerprint_path=None):
    """锚点文件自身完整性：行数三支 + 行间链 + 库内指纹 + 独立见证。

    返回 `(status, msg, witness_state, witness_data)`，status ∈ ok/tampered/indeterminate。

    行数判据必须三支齐全。只判"变少"与"相等"会漏掉"变多"：**仅追加 1 条垃圾行**
    就让两道判据同时返回"无异常"——行数变多无人管、相等分支又因末行变了却只比
    last_hash 时被跳过。此处"变多"与"变少"同等判红：正常写入路径每写一行都会把库内
    指纹的行数高水位同步抬高，因此"锚点比库内指纹多行"本身就是应用写入之外的动作。

    不可解析的行（合法 UTF-8 但不是锚点行格式）判"无法定论"而非"无异常"：它既可能
    是磁盘损坏也可能是有人在试探判据，处置方向不同，不能压成同一结论；也**不**当
    普通红——那会把一次编码事故误报成确证的篡改。
    """
    if lines is None:
        lines, status = _read_anchor_lines_ex(path)
        if status != "ok" or lines is None:
            # 缺失/读不出由调用方（_anchor_status）判定，这里不重复下结论
            return "ok", "", "absent", None
    if meta is None:
        meta = _get_anchor_meta()
    recorded, meta_state = _anchor_meta_line_count(meta)
    if meta_state == "corrupt":
        return (
            "indeterminate",
            "库内锚点指纹的行数字段无法解析为整数（app_meta 被手工损坏或改写）"
            "——校验无法定论（不等于无异常），请立即核查",
            "absent", None,
        )
    if recorded:
        if len(lines) < recorded:
            return (
                "tampered",
                f"锚点文件行数由库内指纹记录的 {recorded} 减至 {len(lines)}"
                "——锚点文件被截断（删掉最后一行不会被行间链发现，正是为绕过锚点而设计）",
                "absent", None,
            )
        if len(lines) > recorded:
            return (
                "tampered",
                f"锚点文件行数由库内指纹记录的 {recorded} 增至 {len(lines)}"
                "——应用写入之外被追加了行（仅追加 1 条垃圾行即可同时骗过"
                "「变少/相等」两支判据，故此处与减少同等判红）",
                "absent", None,
            )
    for i, ln in enumerate(lines):
        if _parse_anchor_line(ln) is None:
            return (
                "indeterminate",
                f"锚点文件第 {i + 1} 行不是合法锚点行（内容损坏或被人为写入）"
                "——校验无法定论，不等于无异常，请人工核查该行",
                "absent", None,
            )
    for i, ln in enumerate(lines):
        parsed = _parse_anchor_line(ln)
        if parsed["prev_line_hash"] is None:
            continue  # 旧格式行没有行间链字段：作为前驱参与哈希，自身不做链校验
        expect = _anchor_line_sha(lines[i - 1]) if i > 0 else _ANCHOR_GENESIS
        if parsed["prev_line_hash"] != expect:
            where = _anchor_line_sha(lines[i - 1])[:12] if i > 0 else _ANCHOR_GENESIS[:12]
            return (
                "tampered",
                f"锚点文件第 {i + 1} 行的行间哈希不符（期望前驱行 {where}）"
                "——锚点历史被改写或删除过整行",
                "absent", None,
            )
    if recorded == len(lines) and meta.get("last_hash") and _anchor_line_sha(lines[-1]) != meta["last_hash"]:
        return "tampered", "锚点文件末行与库内指纹不符——末行内容被改写", "absent", None
    w_status, w_msg, w_state, w_data = _anchor_witness_state(
        lines, meta, fingerprint_path or audit_anchor_fingerprint_path(), path)
    if w_status != "ok":
        return w_status, w_msg, w_state, w_data
    return "ok", "", w_state, w_data


def _anchor_file_state(path):
    """`_anchor_file_state_ex` 的兼容包装：返回异常描述或 ''（旧调用面）。"""
    return _anchor_file_state_ex(path)[1]


def _anchor_status(path=None, fingerprint_path=None):
    """锚点自检结论：`(status, message)`，status ∈ ok/tampered/indeterminate/none。

    "ok" 与 "none" 都不是异常（none = 从未写过锚点，首次运行不判定）；
    "indeterminate"（无法定论）必须与两者都区分开——它既不是"无异常"，也不是
    "确证篡改"，而是"这次没验成"：处置方向不同（修文件/查编码 vs 查攻击），
    混进任一侧都会让告警或静默失效。`audit_health` 对它一律判不健康。
    """
    path = path or audit_anchor_path()
    lines, read_state = _read_anchor_lines_ex(path)
    if read_state == "decode-error":
        return (
            "indeterminate",
            "锚点文件含非法 UTF-8 字节，整份文件无法解析——校验无法定论（不等于"
            "无异常），当日自检不执行；请人工核查该文件内容",
        )
    if read_state == "io-error":
        return (
            "indeterminate",
            "锚点文件存在但不可读（权限或 IO 错误）——校验无法定论（不等于无异常）",
        )
    if read_state == "missing" or not lines:
        try:
            with _facade()._conn_lock:
                conn = _facade().get_conn()
                r = conn.execute(
                    "SELECT value FROM app_meta WHERE key='audit_anchor_last'"
                ).fetchone()
            if r is not None and r["value"]:
                return "tampered", (
                    f"审计锚点文件缺失或不可读，但应用元数据记录曾于 "
                    f"{r['value']} 写入锚点——疑似锚点文件被删除，"
                    "删尾/清空检测已失效，请立即核查"
                )
        except Exception:
            pass  # app_meta 不存在（旧库/跳过迁移）→ 维持旧行为
        return "none", ""
    anchor = _max_anchor_of(lines)
    if anchor is None:
        return (
            "indeterminate",
            "锚点文件存在但没有一行是合法锚点行——校验无法定论（不等于无异常）",
        )
    file_status, file_msg, _w_state, w_fp = _anchor_file_state_ex(
        path, lines=lines, fingerprint_path=fingerprint_path)
    if file_status != "ok":
        # 锚点文件自身不可信时，后面所有"拿末行与库内比对"的判据都是拿伪造值
        # 在校验伪造值——必须先判失败。
        return file_status, file_msg
    try:
        with _facade()._conn_lock:
            conn = _facade().get_conn()
            row = conn.execute(
                "SELECT MIN(id) AS min_id, MAX(id) AS max_id, COUNT(*) AS n FROM audit_logs"
            ).fetchone()
            anchored = conn.execute(
                "SELECT hash FROM audit_logs WHERE id=?", (anchor["max_id"],)
            ).fetchone()
            appended = conn.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE id > ?", (anchor["max_id"],)
            ).fetchone()[0]
            purge_total = _facade()._audit_purge_total(conn)
            events = _audit_purge_events(conn)
            witnessed = None
            if w_fp is not None and w_fp.get("max_id") is not None:
                witnessed = conn.execute(
                    "SELECT hash FROM audit_logs WHERE id=?", (int(w_fp["max_id"]),)
                ).fetchone()
            db_witnessed = None
            if w_fp is not None and w_fp.get("db_max_id") is not None:
                db_witnessed = conn.execute(
                    "SELECT hash, prev_hash FROM audit_logs WHERE id=?",
                    (int(w_fp["db_max_id"]),)
                ).fetchone()
        n = int(row["n"] or 0)
        cur_min = int(row["min_id"]) if row["min_id"] is not None else 0
        cur_max = int(row["max_id"]) if row["max_id"] is not None else 0
        anchor_pt = anchor.get("purge_total")      # v1 行 → None
        anchor_count = anchor.get("count")         # v1 行 → None
        # 留痕累计数只增不减：倒退意味着 app_meta 的删除留痕被人清过/改过，
        # 而这正是"批量删除后自证清白"唯一的通路，必须当场判失败。
        if anchor_pt is not None and purge_total < anchor_pt:
            return "tampered", (
                f"物理删除留痕累计数由锚点记录的 {anchor_pt} 倒退为 {purge_total}"
                "——app_meta 删除留痕被清除或改写，删除追溯已失效，请立即核查"
            )
        # 锚点之后新发生的、有留痕的物理删除条数（v1 锚点无从得知基线）
        explained = (purge_total - anchor_pt) if anchor_pt is not None else None

        if n == 0:
            if explained is not None and anchor_count and explained >= anchor_count:
                return "ok", (
                    f"审计链 {anchor_count} 条已按保留期全部清理完毕"
                    f"（有留痕，锚点以来累计删除 {explained} 条），非告警"
                )
            return "tampered", (
                f"审计表为空，但锚点（{anchor['ts']}）记录曾有 {anchor['max_id']} 条"
                f"——疑似整表被清空（无任何清理留痕可解释，留痕累计={explained}）"
            )

        # ---- 独立见证与库内真值比对 ----
        # 见证记的是"某时刻锚点行自报的链尾行 id 与哈希"。拿它回库核对，是为了抓
        # "删最近 N 条 + 改写锚点行与库内指纹，让两者对彼此自洽"这一类：库内那行
        # 已经不在或哈希已变，而见证由另一属主持有，改不动。
        if w_fp is not None and w_fp.get("max_id") is not None:
            wid = int(w_fp["max_id"])
            if witnessed is None and not _purge_event_covers(events, w_fp.get("purge_total"), wid):
                return "tampered", (
                    f"独立见证记录的链尾行 id={wid} 在库内已不存在，且无清理留痕可"
                    "解释——最近审计被删除后用改写锚点掩盖，请立即核查"
                )
            if witnessed is not None and (witnessed["hash"] or "") != (w_fp.get("head") or ""):
                return "tampered", (
                    f"独立见证记录的链尾行 id={wid} 哈希与库内不符——审计链被抹后"
                    "重签，请立即核查"
                )

        # ---- 独立见证回库真值比对（删尾窗口：锚点之后的 ~24h → 见证 cron 间隔）----
        # 锚点与旧见证都只看"锚点当时"的状态；锚点之后新增的审计行对两者都不可见，
        # 仅删除这些行（无需双写）即可抹掉最近审计而 healthy=True。root 侧见证进程
        # 只读回库记下真实 max(id) 与该行哈希，判据据此闭合：当前 max_id 不得低于见证
        # 值，且见证记录的那一行必须仍在、内容未变。
        #
        # 被见证行**不适用**清理留痕豁免（曾用 _purge_event_covers 放行）。留痕住在
        # 应用可写的 app_meta 里：删掉这些行后再种一条"把该 id 删掉了"的假事件，判据就
        # 被自己的解释开关静默——连 audit_purge_total 都不用动。而按构造，被见证行至多
        # 一个见证间隔（cron 间隔）之旧；合法保留期清理只删月级窗口，永远够不到它。
        # 真正要整库/手工清理属 root 级维护，其运行手册步骤是重置独立见证文件（root
        # 操作）再由下一轮 cron 重新播种——应用身份做不到这一步。所以一条声称覆盖被
        # 见证行的清理事件本身就是篡改证据，不能拿来放行。
        if w_fp is not None and w_fp.get("db_backed") and w_fp.get("db_max_id") is not None:
            wdbid = int(w_fp["db_max_id"])
            if db_witnessed is None:
                return "tampered", (
                    f"独立见证记录的库内链尾行 id={wdbid} 在库内已不存在——见证之后"
                    "新增的审计被删除（仅需库写权限即可掩盖），请立即核查"
                )
            if cur_max < wdbid:
                return "tampered", (
                    f"库内 max_id={cur_max} 低于独立见证记录的 {wdbid}——见证之后新增的"
                    f"{wdbid - cur_max} 条审计被删除，删尾检测已失效，请立即核查"
                )
            if db_witnessed is not None and (db_witnessed["hash"] or "") != (w_fp.get("db_head_hash") or ""):
                return "tampered", (
                    f"独立见证记录的库内行 id={wdbid} 哈希与库内不符——该行被改写或"
                    "整表被重签，请立即核查"
                )
            if db_witnessed is not None and w_fp.get("db_prev_hash") is not None and \
                    (db_witnessed["prev_hash"] or "") != (w_fp.get("db_prev_hash") or ""):
                return "tampered", (
                    f"独立见证记录的库内行 id={wdbid} 前驱哈希与库内不符——链被删行后"
                    "重接，请立即核查"
                )

        # ---- 判据一：定点 ----
        # 锚点 max_id 那一行必须还在、哈希必须还对得上。原判据是
        # "cur_max == anchor.max_id 时才比 head"，于是"删掉链尾若干条 + 再写一条"
        # 就足以让整套比对静默（新行 id 更大，head 比对被跳过）。
        if anchored is None and not _purge_event_covers(events, anchor_pt, anchor["max_id"]):
            trend = (
                f"当前 max_id={cur_max} 小于锚点 max_id={anchor['max_id']}（条数减少，"
                f"疑似删掉最近 {anchor['max_id'] - cur_max} 条）"
                if cur_max < anchor["max_id"]
                else f"当前 max_id={cur_max} 反而更大——删尾后用新写入掩盖"
            )
            return "tampered", (
                f"锚点记录的链尾行 id={anchor['max_id']} 已不存在，且无清理留痕可解释："
                f"{trend}；锚点以来有留痕的删除累计={explained}"
            )
        if anchored is not None and anchored["hash"] != anchor["head"]:
            return "tampered", (
                f"审计链尾行 id={anchor['max_id']} 的哈希与锚点不符（链尾内容被篡改或被"
                f"全表重签）{_rechain_hint(anchor)}"
            )
        if anchored is None:
            # 定点被留痕事件解释掉了（长期空闲后保留期清理删到了链尾）——
            # 这条锚点已不再描述当前链尾，后续判据照常执行
            logger.info("锚点链尾行 id=%s 已由清理留痕解释，跳过 head 比对", anchor["max_id"])

        # ---- 判据二：稠密（v1 锚点无 count，降级跳过）----
        if anchor_count is not None:
            era_rows = n - int(appended)          # 锚点当时那批行里现在还剩下的
            missing = anchor_count - era_rows     # 锚点以来消失的行数
            exp = explained if explained is not None else 0
            if missing > exp:
                return "tampered", (
                    f"审计记录条数减少且无清理留痕：锚点（{anchor['ts']}）记录 {anchor_count} 条，"
                    f"当前该批仅剩 {era_rows} 条（此后新增 {appended} 条），"
                    f"消失 {missing} 条而有留痕的物理删除仅 {exp} 条——"
                    f"疑似删除了 {missing - exp} 条历史记录；min_id 由 {anchor['min_id']} "
                    f"变为 {cur_min}，max_id 由 {anchor['max_id']} 变为 {cur_max}"
                )
            if missing < exp:
                return "tampered", (
                    f"清理留痕与链实际状态不符：留痕声称锚点以来删除 {exp} 条，"
                    f"实际仅消失 {missing} 条——留痕被伪造/重复写入，判为异常"
                )

        # ---- 判据三：留痕（min_id 跃迁必须有事件精确对上）----
        if cur_min > anchor["min_id"]:
            if anchor_count is None:
                # v1 锚点：没有 count 可核对，维持旧的"信息"定性（不因此判失败）
                return "ok", (
                    f"审计链最早记录由 id={anchor['min_id']} 回收至 {cur_min}"
                    "（旧版锚点无 count 字段，无法核对清理留痕，非告警；"
                    "本次已按新格式重写锚点，明日恢复完整判据）"
                )
            if not _purge_event_sets_min(events, anchor_pt, cur_min):
                return "tampered", (
                    f"审计链 min_id 由 {anchor['min_id']} 跃迁至 {cur_min}（跃迁 "
                    f"{cur_min - anchor['min_id']} 条），但没有任何一条清理留痕事件的"
                    f"删除后 min_id 与之相符——前缀删除无留痕，判为非法删除"
                )
            return "ok", (
                f"审计链最早记录由 id={anchor['min_id']} 回收至 {cur_min}"
                "（有清理留痕，保留期清理的正常现象，非告警）"
            )
        if cur_min < anchor["min_id"]:
            return "tampered", (
                f"审计链 min_id 由 {anchor['min_id']} 倒退至 {cur_min}——"
                "锚点之后不可能凭空出现更早的记录，判为异常（库被替换或 id 被重写）"
            )
        return "ok", ""
    except Exception as e:
        # 校验过程自身异常（库锁/连接等）也算"无法定论"：既不能印成通过，也不该
        # 冒充一次成功的取证。调用方据 status 分开处置。
        return "indeterminate", f"锚点校验异常: {e}"


def verify_audit_anchor(path=None, fingerprint_path=None):
    """与库外锚点比对，检出「删尾 / 清空整表 / 篡改链尾 / 锚点文件被改写」。

    返回 (ok: bool, message: str)：
    - ok=False → 非"通过"（确证异常或无法定论，message 里注明是哪一种）；
    - ok=True 且 message 非空 → 提示性信息（如合法清理造成的前缀回收），记录即可。
    无可用锚点时返回 (True, "")——首次运行或从未记录过锚点不做判定。

    需要区分三态（无异常 / 无法定论 / 确证异常）的调用方改用 `_anchor_status`；
    本函数保留二元返回是为了不破坏既有调用面，"无法定论"时 message 以
    「无法定论」开头，且 ok=False（绝不显示成通过）。

    app_meta 记录过锚点（audit_anchor_last）而锚点文件此刻缺失/不可读 → 判定异常。
    这项元数据交叉检查对**显式路径同样生效**：只查默认路径时，web 每日线程改传
    显式路径就会把它整个跳过，锚点被删仍判通过（致盲换个形式复发）。
    """
    status, msg = _anchor_status(path, fingerprint_path)
    if status == "indeterminate":
        return False, "无法定论：" + msg
    return status in ("ok", "none"), msg


def _purge_events_after_anchor(events, anchor_pt):
    """锚点之后新发生的 audit_logs 物理删除留痕（按 audit_seq 精确切分）。"""
    out = []
    for ev in events:
        if ev.get("table") != "audit_logs":
            continue
        seq = ev.get("audit_seq")
        if seq is None:
            continue  # 升级前的旧清理没有序号，无法定位与锚点的先后——不参与解释
        if anchor_pt is None or int(seq) > int(anchor_pt):
            out.append(ev)
    return out


def _purge_event_covers(events, anchor_pt, row_id):
    """是否有一条锚点之后的留痕事件恰好把 id=row_id 这条删掉了。

    调用边界：只用于解释**比被见证行更旧**的行（锚点定点行及其以前）。被见证行
    （见证 JSON 的 `db_max_id`）不得走本豁免——留痕住在应用可写的 app_meta 里，一条
    伪造事件即可把删尾翻成通过；被见证行按构造至多一个见证间隔之旧，月级保留期清理
    够不到它，能删它的事件本身即是篡改证据（详见 `_anchor_status` 的回库判据注释）。
    """
    for ev in _purge_events_after_anchor(events, anchor_pt):
        before_max = ev.get("before_max")
        after_max = ev.get("after_max")
        if before_max is None or int(row_id) > int(before_max):
            continue
        if after_max is None or int(row_id) > int(after_max):
            return True
    return False


def _purge_event_sets_min(events, anchor_pt, cur_min):
    """是否有一条锚点之后的留痕事件，其"删除后 min_id"恰好等于当前 min_id。"""
    for ev in _purge_events_after_anchor(events, anchor_pt):
        if ev.get("after_min") is None:
            continue  # 删空后重新累积：min 由新行决定，不用于解释这次跃迁
        if int(ev["after_min"]) == int(cur_min):
            return True
    return False


def _rechain_events(conn):
    """全表重链留痕列表（app_meta JSON）。缺表/损坏 → []。"""
    return _meta_json_list(conn, _RECHAIN_EVENTS_KEY)


def _rechain_hint(anchor):
    """锚点之后若发生过全表重链，给出可诊断的留痕摘要（无则空串）。

    链尾哈希与锚点不符有两个成因，处置完全不同：内容被篡改 vs 启动路径用当前密钥
    重签了整条链（后者要求有人把 user_version 拨回 v3 之前，或换过 YIBAN_AUDIT_KEY）。
    留痕让运维一眼看出是哪一种，而不是对着同一句"疑似篡改"猜。
    """
    try:
        with _facade()._conn_lock:
            conn = _facade().get_conn()
            events = _rechain_events(conn)
    except Exception:
        return ""
    ts = (anchor or {}).get("ts") or ""
    recent = [e for e in events if str(e.get("ts") or "") > ts]
    if not recent:
        return "；锚点之后无全表重链留痕，按内容篡改处理"
    e = recent[-1]
    return (
        f"；锚点之后有 {len(recent)} 次全表重链留痕"
        f"（最近一次 {e.get('ts')} 来源版本 v{e.get('from_version')} "
        f"行数 {e.get('rows')} 重链后 head={str(e.get('head_after'))[:12]}…）"
        "——若非预期的 v3 升级/密钥轮换，即视同篡改"
    )


def audit_health(path=None, fingerprint_path=None):
    """审计可追溯性综合体检（供每日线程 / audit_verify.py 调用）。

    返回 dict：
      chain_ok      链内哈希自洽（False = 有记录被篡改/删除）
      broken        链内断链条数（-1 = 校验异常或密钥缺失）
      anchor_ok     与库外锚点一致（False = 删尾 / 清空 / 篡改链尾 / 无痕删除 /
                    无法定论；"无法定论"不是"无异常"，也不是确证篡改）
      anchor_status 锚点自检的三态结论：ok/none/tampered/indeterminate
                    （none = 从未写过锚点，首次运行不判定）
      anchor_witness 独立见证形态：separate/same-owner/anchor-missing/absent/unreadable/corrupt
                    （absent/unreadable/corrupt 且见证目录已预建 = 控制面不可用，
                    见 anchor_witness_unhealthy）
      anchor_witness_unhealthy 独立见证控制面不可用（生产形态下缺失/读不出/损坏）
                    ——计入 healthy 结论（fail-closed）
      anchor_msg    锚点判定的说明或提示信息
      write_failures 累计的审计写入失败次数（>0 = 有操作未留痕；落库不随重启归零）
      rechain_events app_meta 里的全表重链留痕（诊断用，最新在末尾）
      empty_hash_rows 链内 hash 为空的行数（>0 = 有人清空签名等着被重签）
      purge_total   累计**有留痕的** audit_logs 物理删除条数（保留期清理口径）
      last_cleanup  最近一次 audit_logs 清理留痕事件（含 cutoff 与删除条数；无 → None）
      note          附加诊断文本（无异常时为空串）
      healthy       综合结论（上述全部正常）

    为什么要把 purge_total / last_cleanup 放到体检结果里：本机自校验防不住**本机时钟**
    ——守卫的参照点每次成功都会推进，容差内每天小幅拨快即可在真实时间数十天内合法清掉
    整段保留期审计，且不触发任何告警。这两个数字的用途是**随日报出箱**：异机侧看"累计
    删除量"与"最近 cutoff 是否持续前移"，本机看不到的异常清理在外部就能看出来。

    `fingerprint_path` 是独立见证文件路径（默认 `audit_anchor_fingerprint_path()`）；
    显式传入供测试与多部署定位，生产走默认。
    """
    path = path or audit_anchor_path()
    fp_path = fingerprint_path or audit_anchor_fingerprint_path()
    chain_ok, broken, _first = verify_audit_chain()
    anchor_status, anchor_msg = _anchor_status(path, fingerprint_path)
    # 三态压成二态：只有 ok/none 算"通过"。"无法定论"必须落到 anchor_ok=False，
    # 否则一条非法字节就能让当日自检在"healthy=True"里静默消失。
    anchor_ok = anchor_status in ("ok", "none")
    # 锚点文件缺失/不可读时不给 same-owner/separate 这类"跨权限保护在位"的假结论：
    # 见证形态无从比对，显式记 anchor-missing（锚点自身结论由 anchor_status 负责）。
    anchor_lines, anchor_read_state = _read_anchor_lines_ex(path)
    if anchor_read_state != "ok" or not anchor_lines:
        anchor_witness = "anchor-missing"
    else:
        anchor_witness = _anchor_witness_state(
            anchor_lines, _get_anchor_meta(), fp_path, path)[2]
    # 控制面可用性纳入不健康判定（fail-closed）：生产形态（安装器已预建见证目录）下
    # 见证缺失/不可读/损坏 = 安装损坏，独立见证整体空转，必须告警；目录不存在 =
    # 从未安装独立见证的文档化降级形态（开发/单用户/纯状态文件），只提示不判红。
    witness_control_lost = (
        anchor_witness in ("absent", "unreadable", "corrupt")
        and _witness_dir_installed(fp_path)
    )
    write_failures = audit_write_failures()
    rechain_events, empty_hash_rows = _rechain_diagnostics()
    notes = []
    if anchor_status == "indeterminate":
        notes.append("锚点自检无法定论（既非通过也非确证篡改）：" + anchor_msg)
    if witness_control_lost:
        notes.append(
            "独立见证控制面不可用（%s，见证目录已由安装器预建）——生产形态下缺失即"
            "安装损坏：请检查 deploy/prod 见证安装与 root 侧 cron，独立见证未运行则"
            "双写掩盖删除无独立证据" % anchor_witness
        )
    elif anchor_witness in ("absent", "unreadable", "corrupt"):
        # 单用户/开发部署的降级形态：独立见证未启用（或读不出）时，锚点与库内指纹
        # 同属一个身份，拿到该身份写权限者可双写掩盖删审计——这是已声明的残余风险。
        notes.append(
            "独立见证指纹未启用或不可读（%s）：锚点与库内指纹同属一个身份，"
            "双写掩盖删除仍无独立证据，强烈建议按 deploy/prod 部署 root 侧见证" % anchor_witness
        )
    rechain_after_anchor = False
    if rechain_events:
        anchor = _last_audit_anchor(path)
        anchor_ts = (anchor or {}).get("ts") or ""
        # 合法的全表重链只会发生在"任何锚点存在之前"（升级那一次）。锚点之后
        # 再出现重链，意味着启动路径在已锚定的链上动过手——即使链此刻自洽、
        # head 也巧合同值，这个动作本身就是异常。
        late = [e for e in rechain_events if anchor_ts and str(e.get("ts") or "") > anchor_ts]
        if late:
            rechain_after_anchor = True
            e = late[-1]
            notes.append(
                f"锚点（{anchor_ts}）之后存在 {len(late)} 次全表重链留痕"
                f"（最近 {e.get('ts')} 来源版本 v{e.get('from_version')}，"
                f"行数 {e.get('rows')}）——非预期升级即视为链被重写"
            )
    if empty_hash_rows:
        notes.append(
            f"审计链存在 {empty_hash_rows} 条 hash 为空的记录——签名被清空后等待启动路径"
            "重签整条链（migrate_v3 即此形态），请立即核查"
        )
    # 清理量随体检结果出箱：本机自校验防不住本机时钟（参照点每天推进、容差内的小幅
    # 拨快即可合法清掉整段保留期审计），异机侧只能靠这两个数字判断"清理是否异常"。
    purge_total = audit_purge_total()
    last_cleanup = next(
        (e for e in reversed(audit_purge_events()) if e.get("table") == "audit_logs"),
        None,
    )
    return {
        "chain_ok": chain_ok,
        "broken": broken,
        "anchor_ok": anchor_ok,
        "anchor_status": anchor_status,
        "anchor_witness": anchor_witness,
        "anchor_witness_unhealthy": bool(witness_control_lost),
        "anchor_msg": anchor_msg,
        "write_failures": write_failures,
        # 自上次确认以来新增的欠账：告警按"账目变化"触发的输入（见
        # audit_write_failures_unnotified 处的口径注释）。总账仍单调，不改取证事实。
        "write_failures_new": audit_write_failures_unnotified(),
        "rechain_events": rechain_events,
        "empty_hash_rows": empty_hash_rows,
        "purge_total": purge_total,
        "last_cleanup": last_cleanup,
        "note": "；".join(notes),
        "healthy": bool(
            chain_ok and anchor_ok and write_failures == 0
            and not rechain_after_anchor and not empty_hash_rows
            and not witness_control_lost
        ),
    }


def _rechain_diagnostics():
    """(全表重链留痕, 链内空 hash 行数)——体检的附加信号，读失败按无异常处理。"""
    try:
        with _facade()._conn_lock:
            conn = _facade().get_conn()
            events = _rechain_events(conn)
            try:
                empty = conn.execute(
                    "SELECT COUNT(*) FROM audit_logs WHERE hash=''"
                ).fetchone()[0]
            except sqlite3.Error:
                empty = 0
        return events, int(empty or 0)
    except Exception as e:
        logger.warning("读取重链/空 hash 诊断信息失败: %s", e)
        return [], 0
