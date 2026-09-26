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
import pathlib
import sqlite3

#: 清库要看的表（指纹口径与"清空对象"对齐）：缺失的表按 0 计，不因旧 schema 报错。
PURGE_TABLES = ("accounts", "users", "audit_logs", "sign_events",
                "session_cache", "time_prefs")

_FP_PREFIX = "PURGE-"


def _digest(payload):
    """目标本体的规范序列 → 短指纹（可抄写长度，仍随内容变化）。"""
    return _FP_PREFIX + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _readonly_uri(abs_path):
    """只读连接 URI：pathlib 转 `file://` URI 再挂 `mode=ro`。

    不能直接拼 `file:{abs_path}?mode=ro`：Windows 盘符路径（`D:\\...`）与含空格/
    特殊字符的路径会拼出坏 URI，`sqlite3.connect` 报错后旧实现会兜底成**读写**连接
    ——"只看目标"这一步就悄悄放弃了只读保证。
    """
    return pathlib.Path(abs_path).as_uri() + "?mode=ro"


def _readonly_conn(abs_path):
    """→ 只读连接；打不开返回 None（绝不放宽为读写连接）。"""
    try:
        return sqlite3.connect(_readonly_uri(abs_path), uri=True, timeout=5)
    except sqlite3.Error:
        return None


def _count_table_rows(db_path, tables=PURGE_TABLES):
    """→ (counts, readable)。只读计数；库缺失或只读连接打不开时 counts 全 0。"""
    counts = {t: 0 for t in tables}
    abs_path = os.path.abspath(db_path)
    if not os.path.exists(abs_path):
        return counts, True
    conn = _readonly_conn(abs_path)
    if conn is None:
        return counts, False
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
    return counts, True


def table_counts(db_path, tables=PURGE_TABLES):
    """→ {表: 行数}。只读计数；库/表缺失一律按 0，绝不建库建表。"""
    return _count_table_rows(db_path, tables)[0]


def db_content_fingerprint(db_path, tables=PURGE_TABLES):
    """→ (指纹, 人类可读摘要行)。按库**内容**（各表行数 + 文件规模）派生，不建库。

    库文件不存在时指纹取"库缺失 + 绝对路径"，仍给出摘要——调用方据此报告目标。
    只读连接：绝不在"看目标"这一步触发 init_db/建表/迁移（那会让指纹本身改变目标）。
    """
    abs_path = os.path.abspath(db_path)
    size = os.path.getsize(abs_path) if os.path.exists(abs_path) else 0
    counts, readable = _count_table_rows(abs_path, tables)
    payload = "db|%s|%d|%s" % (
        abs_path, size, ",".join("%s=%d" % (t, counts.get(t, 0)) for t in tables))
    lines = [
        f"目标库: {abs_path}",
        f"文件大小: {size} 字节",
        "表行数: " + " ".join(f"{t}={counts.get(t, 0)}" for t in tables),
    ]
    if not readable:
        # 只读连接打不开（权限/损坏/被占用）时把"行数不可信"显式说出来，而不是
        # 悄悄用读写连接读出数字、让人以为目标已核清。
        lines.append("不可读：只读连接打不开本库（行数按 0 计，仅文件大小/路径可辨）")
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

    **fail-closed 口径**：库文件不存在、初始化失败、连接没指向目标库、审计写入失败
    （`audit` 返回 False 或其内部抛出）一律返回 False，调用方必须据此放弃删除。库不存在
    时**不建库**（`init_db` 会 `sqlite3.connect` 出空库，等于给一次拒绝留下新库文件）。
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
        # `init_db` 在单例连接已存在时会直接复用它（只刷新声明的 `_db_file`）：
        # 若那个连接指向另一个库，这条留痕会落到别的库上、目标库反而无痕。按**实际
        # 连接**核对目标，不一致即视为不可写（fail-closed）。
        if not _conn_points_at(db_file):
            return False
        # audit_or_refuse = fail-closed 审计（失败抛 AuditWriteRefused）：本函数正是它的
        # 典型调用面——清库/删除留痕写不进去就必须放弃删除，不能"删了却无痕"。
        store_db.audit_or_refuse("purge-guard", action, target, str(detail)[:200])
        return True
    except Exception:  # 初始化/审计任一失败（含 AuditWriteRefused）都不得放行删除
        return False


def _conn_points_at(db_file):
    """当前单例连接实际指向的库是否就是 db_file（realpath + normcase 归一）。"""
    from yiban.store import connection
    actual = connection.current_db_file()
    if not actual:
        return False
    return os.path.normcase(os.path.realpath(actual)) == os.path.normcase(
        os.path.realpath(os.path.abspath(db_file)))
