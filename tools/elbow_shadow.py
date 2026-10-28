# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""肘界（elbow）影子验证分析器：离线重放生产台账与进度事件，只读、零改线。

**用途**（elbow 影子验证）：在**不接线到引擎**的前提下，
回答"若当时用 elbow 谓词，实际发生的领取会有哪些改判"。判据是 `yiban/core/elbow.py` 的
同一语义：

    会领  ⇔  提前量 ≤ −肘界        其中 提前量 = 行到期时刻 − 领取时刻（秒）

提前量为正 = 还没到期就被领（elbow 必拒）；提前量足够负 = 过期远超肘界（elbow 放行）。

**输入**（只读打开一个 SQLite 库）：
- `run_events`：进度事件表（`node='claim'` 的 `ts` 即领取时刻，`executor` 即领取者）。
- `sign_tasks`：任务台账（`run_at` 即计划到期时刻，`owner` 即末次领取者角色）。

**输出**（只印聚合，绝不印原始手机号）：
1. 按 (业务日, 角色) 的领取数、"会拒"条数、提前量极值；
2. 全局汇总行 `ELBOW-SHADOW claims=… would_reject=… reject_pct=… elbow_sec=…`；
3. 按 (业务日, 角色) 的**归属吸收**（`sign_tasks.owner` 的分布）——用于回答
   "某日兜底是否吸收了全量轮的工作"。

**只读纪律**：只用 `mode=ro` 打开库；不建库、不建表、不写任何文件；输出不含手机号。
**注意**：`run_events` 只保留最近若干天，更早的日期没有领取时刻，只能看第 3 段归属分布。

用法：
    python3 tools/elbow_shadow.py --db PATH [--elbow-seconds T] [--no-owners]
"""
import argparse
import sqlite3
import sys
from pathlib import Path

#: 角色前缀判定：复刻执行体身份串的角色段形态（取 `@` 之前的名字段的首段）。
#: 这是分析脚本的**局部读法**，只用于把事件归到角色桶；线上角色解析的唯一实现另有其处。
_ROLE_PREFIXES = (
    ("fallback", "fallback"),
    ("worker", "worker"),
    ("single", "single"),
    ("exec", "single"),
)


def open_readonly(db_path):
    """只读打开库（`mode=ro`）：不建库、不建表、不迁移、不切日志模式。"""
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def role_of(executor):
    """把执行体/归属串归到角色桶：fallback / worker / single / other。"""
    name = (executor or "").split("@", 1)[0].strip().lower()
    for prefix, role in _ROLE_PREFIXES:
        if name.startswith(prefix):
            return role
    return "other"


def elbow_for(role, elbow_sec):
    """角色 → 肘界（角色表见本模块的 `_ROLE_PREFIXES`）。

    **只有兜底常驻**用 T_elbow（= 传入值）；定时执行体 / 手动 / 补签一律 0（到期即可领）。
    对全角色套同一个肘界会把"执行体按计划领到期行"误算成"会拒"——那是口径错误，不是发现。
    """
    return float(elbow_sec) if role == "fallback" else 0.0


def classify(margin_sec, elbow_sec):
    """elbow 谓词的影子判定：'claim'（会领）或 'reject'（会拒）。

    提前量（`margin_sec`）≤ −肘界 ⇒ 会领；否则会拒。与 `yiban.core.elbow.may_claim`
    逐例等价（由 tests/test_elbow_shadow.py 的对拍钉住，防止口径漂移）。
    """
    return "claim" if margin_sec <= -elbow_sec else "reject"


def load_records(conn):
    """读全部领取事件，返回 `[{day, role, margin_sec}, …]`（提前量单位：秒）。"""
    sql = (
        "SELECT ev.day AS day, ev.executor AS executor, "
        "(julianday(st.run_at) - julianday(ev.ts)) * 86400.0 AS margin_sec "
        "FROM run_events ev JOIN sign_tasks st "
        "ON st.phone = ev.phone AND st.day = ev.day "
        "WHERE ev.node = 'claim'"
    )
    out = []
    for row in conn.execute(sql):
        out.append({"day": row["day"], "role": role_of(row["executor"]),
                    "margin_sec": float(row["margin_sec"])})
    return out


def analyze(records, elbow_sec):
    """聚合领取记录：总数、会拒数、提前量为正数、按 (日, 角色) 明细。

    肘界**按角色**取（`elbow_for`）：只有兜底用 T_elbow，其余角色 0。
    """
    stats = {"claims": 0, "would_reject": 0, "positive_margin": 0,
             "by_day_role": {}}
    for rec in records:
        stats["claims"] += 1
        verdict = classify(rec["margin_sec"], elbow_for(rec["role"], elbow_sec))
        bucket = stats["by_day_role"].setdefault(
            rec["day"], {}).setdefault(rec["role"], {"claims": 0, "reject": 0})
        bucket["claims"] += 1
        if verdict == "reject":
            stats["would_reject"] += 1
            bucket["reject"] += 1
        if rec["margin_sec"] > 0:
            stats["positive_margin"] += 1
    return stats


def analyze_owners(conn):
    """读任务台账的归属分布：返回 `[{day, role, n}, …]`（不含手机号）。"""
    sql = (
        "SELECT day, owner, COUNT(*) AS n FROM sign_tasks "
        "WHERE day != '' GROUP BY day, owner ORDER BY day, n DESC"
    )
    merged = {}
    for row in conn.execute(sql):
        key = (row["day"], role_of(row["owner"]) if row["owner"] else "empty")
        merged[key] = merged.get(key, 0) + int(row["n"])
    out = [{"day": day, "role": role, "n": n}
           for (day, role), n in sorted(merged.items())]
    return out


def _print_report(stats, elbow_sec, owners):
    pct = (100.0 * stats["would_reject"] / stats["claims"]) if stats["claims"] else 0.0
    print(f"ELBOW-SHADOW claims={stats['claims']} would_reject={stats['would_reject']} "
          f"reject_pct={pct:.1f} positive_margin={stats['positive_margin']} "
          f"elbow_sec={elbow_sec:g}")
    for day in sorted(stats["by_day_role"]):
        for role in sorted(stats["by_day_role"][day]):
            b = stats["by_day_role"][day][role]
            print(f"  claims by day/role: {day} {role} claims={b['claims']} "
                  f"would_reject={b['reject']}")
    if owners:
        print("  owner absorption (sign_tasks.owner 分布):")
        for row in owners:
            print(f"    {row['day']} {row['role']} n={row['n']}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="elbow 影子验证：离线重放领取台账与进度事件（只读）")
    parser.add_argument("--db", required=True, help="SQLite 库路径（只读打开）")
    parser.add_argument("--elbow-seconds", type=float, default=60.0,
                        help="角色肘界（秒）；默认 60")
    parser.add_argument("--no-owners", action="store_true",
                        help="不打印归属吸收段")
    args = parser.parse_args(argv)

    if not Path(args.db).is_file():
        # 前缀写 `elbow_shadow`（下划线）而不是连字符：`tests/test_state_gc.py` 的
        # 按日文件名扫描器认「引号后紧跟 <小写token>-…{表达式}」的形态，连字符写法会
        # 被误判成按日状态文件前缀 `elbow-`（进而要求登记清理策略）。这里只是给人看的
        # 报错文案，**不要**改成连字符——那会重新触发那道守卫。
        print(f"elbow_shadow: 库文件不存在：{args.db}（只读分析，不建库）", file=sys.stderr)
        return 2
    conn = open_readonly(args.db)
    try:
        records = load_records(conn)
        stats = analyze(records, args.elbow_seconds)
        owners = [] if args.no_owners else analyze_owners(conn)
    finally:
        conn.close()
    _print_report(stats, args.elbow_seconds, owners)
    return 0


if __name__ == "__main__":
    sys.exit(main())
