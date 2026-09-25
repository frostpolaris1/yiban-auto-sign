# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""**功能**
清库/删除类入口的公共防线：目标指纹、确认回显、清库留痕。

三件东西对应三条硬约束，缺一条都会让"清库"退化成"误指生产库即不可逆删除"：
- **目标指纹**由目标本体（库内的表行数、规模）派生，不是路径字符串比较——路径写对
  不代表库指对（软链、改名、复制出的副本都会让路径看起来"对"），而行数/规模差异
  天然能区分 demo/压测库与生产库；库缺失时也不建文件。
- **确认回显**要求操作者逐字抄回指纹：单纯 `--yes` 不构成放行。
- **留痕**复用审计链写入助手（`yiban.store.audit_chain.audit`）；审计写不进去就
  返回 False，调用方据此拒绝执行（fail-closed）——否则会"删了但没留痕"。

**归属**
`yiban/store/` 数据层：指纹要直读 SQLite，留痕要走审计链（`_conn` 单例与 `audit`
都属本层）。部署面脚本（demo 数据生成、压测造数、`state` 子命令）与 loadtest
工具链都经本模块，不各自再写一份指纹/确认逻辑。

**复用**
`db_content_fingerprint`（库内容指纹 + 摘要行）、`content_fingerprint`（任意目录/清单
的指纹）、`confirmation_ok`（回显比对）、`write_purge_audit`（留痕，返回是否落库）。
`PURGE_TABLES` 是"清库要看的表"登记点。

**通信**
输入：SQLite 路径、任意目标部件序列、操作者回显的指纹、审计三要素。
输出：指纹字符串（`PURGE-<hex>`）、摘要行列表、确认是否成立、审计是否落库。
调用谁：`sqlite3`（只读计数）与 `yiban.store.db`（审计写入；局部导入避免与门面成环）。
谁调用：`scripts/generate_demo_data.py`、`scripts/loadtest/seed_accounts.py`、
`yiban/cli.py` 的 `state` 子命令。
"""
import hashlib
import os
import sqlite3

#: 清库要看的表（指纹口径与"清空对象"对齐）：缺失的表按 0 计，不因旧 schema 报错。
PURGE_TABLES = ("accounts", "users", "audit_logs", "sign_events",
                "session_cache", "time_prefs")

_FP_PREFIX = "PURGE-"


def _digest(payload):
    """目标本体的规范序列 → 短指纹（可抄写长度，仍随内容变化）。"""
    return _FP_PREFIX + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def table_counts(db_path, tables=PURGE_TABLES):
    """→ {表: 行数}。只读计数；库/表缺失一律按 0，绝不建库建表。"""
    counts = {t: 0 for t in tables}
    abs_path = os.path.abspath(db_path)
    if not os.path.exists(abs_path):
        return counts
    try:
        conn = sqlite3.connect(f"file:{abs_path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        conn = sqlite3.connect(abs_path, timeout=5)
    try:
        conn.row_factory = sqlite3.Row
        for table in tables:
            try:
                counts[table] = int(
                    conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            except sqlite3.Error:
                counts[table] = 0
    finally:
        conn.close()
    return counts


def db_content_fingerprint(db_path, tables=PURGE_TABLES):
    """→ (指纹, 人类可读摘要行)。按库**内容**（各表行数 + 文件规模）派生，不建库。

    库文件不存在时指纹取"库缺失 + 绝对路径"，仍给出摘要——调用方据此报告目标。
    只读连接：绝不在"看目标"这一步触发 init_db/建表/迁移（那会让指纹本身改变目标）。
    """
    abs_path = os.path.abspath(db_path)
    size = os.path.getsize(abs_path) if os.path.exists(abs_path) else 0
    counts = table_counts(abs_path, tables)
    payload = "db|%s|%d|%s" % (
        abs_path, size, ",".join("%s=%d" % (t, counts.get(t, 0)) for t in tables))
    lines = [
        f"目标库: {abs_path}",
        f"文件大小: {size} 字节",
        "表行数: " + " ".join(f"{t}={counts.get(t, 0)}" for t in tables),
    ]
    return _digest(payload), lines


def content_fingerprint(kind, parts):
    """→ 指纹。任意"目标本体"（状态目录条目、将删清单……）的规范序列，kind 作命名空间。"""
    payload = "%s|%s" % (kind, "\x1f".join(str(p) for p in parts))
    return _digest(payload)


def confirmation_ok(expected, provided):
    """回显是否逐字匹配（两侧去空白）。空回显一律不放行。"""
    return bool(provided) and str(provided).strip() == str(expected).strip()


def write_purge_audit(action, target, detail, db_file=None, env_file=None):
    """把一条清库/删除留痕写进部署库的审计链；→ 是否已落库。

    **fail-closed 口径**：库文件不存在、初始化失败、审计写入失败（`audit` 返回
    False 或其内部抛出）一律返回 False，调用方必须据此放弃删除。库不存在时**不建库**
    （`init_db` 会 `sqlite3.connect` 出空库，等于给一次拒绝留下新库文件）。
    `cleanup=False, migrate=False`：留痕是只读之外的最小写入，不得顺带跑启动清理
    或重写审计链（否则"记录这次删除"本身会改动目标库）。
    """
    from yiban.infra import env_io
    if db_file is None:
        db_file = env_io.resolve_path("YIBAN_DB_FILE", "yiban.db")
    if not os.path.exists(os.path.abspath(db_file)):
        return False
    try:
        from yiban.store import db as store_db
        if env_file is None:
            env_file = env_io.env_path()
        store_db.init_db(db_file=db_file, env_file=env_file, cleanup=False, migrate=False)
        return bool(store_db.audit("purge-guard", action, target, str(detail)[:200]))
    except Exception:  # 初始化/审计任一失败都不得放行删除
        return False
