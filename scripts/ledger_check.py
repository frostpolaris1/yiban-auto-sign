# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""**功能**
台账对账工具（只读）：核对按日状态文件里的**终态**是否都进了 `sign_tasks`。

三项检查：
1. **终态覆盖**：`sign-state-<day>.json` 里的终态账号集合 ⊆ 该日 `sign_tasks` 的账号
   集合；差集逐行打印 `day phone json_status`；
2. **状态值合法**：该日 `sign_tasks.state` ⊆ 池的状态词表；
3. **计数自洽**：当日行数 == 平移行（`vshard=-1` 且 `owner<>'backfill'`）+ 补账行
   （`owner='backfill'`）+ 其它来源行（当日只有一个写者、且 v3 执行体未接线时为 0）。

**归属**
运维/取证侧脚本（`scripts/`）。台账口径的落地在别处，本脚本只核对不再造第二份判据：
终态映射取 `yiban.store.migrations.migrate_v20` 的常量表，池状态词表取
`yiban.store.queue_store.STATES`。

**复用**
无对外可复用函数。

**通信**
用法：`python3 scripts/ledger_check.py [--day YYYY-MM-DD | --all-days N]`
输入：命令行 `--day`（缺省今天）/ `--all-days N`（最近 N 天，含今天）；库路径与状态
目录走环境变量与 `.env`（`YIBAN_DB_FILE` / `YIBAN_STATE_DIR`）。
输出：逐日结论与差异行到 stdout 并逐个打一遍结论，差异行的手机号经
`yiban.masking.mask_phone` 脱敏（对外输出不得含完整号码）。
退出码：`0`=对账平；`1`=有差异（已逐行打印）；`2`=无法定论（状态目录缺失 /
库文件缺失 / 库不可用，含库尚未升到 `sign_tasks` 存在的那一版）。
调用谁：`yiban.store.db`（`init_db(migrate=False, cleanup=False)` 的只读口径）、
`yiban.store.migrations`、`yiban.store.queue_store`、`yiban.infra.env_io`、
`yiban.clock`。
谁调用：运维在库升级后手工跑一次；无其它调用点（web 与 `yiban.cli` 都不调它）。
"""
import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from yiban import clock  # noqa: E402
from yiban.infra import env_io  # noqa: E402
from yiban.masking import mask_phone  # noqa: E402
from yiban.store import db, migrations, queue_store  # noqa: E402

#: 台账里"平移行"的标记（v18 平移的 `sign_claims` 行取 `vshard=-1`）
_TRANSLATED_SHARD = -1
#: 补账行（v20 backfill）的 owner 标记
_BACKFILL_OWNER = migrations._BACKFILL_OWNER

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _day_arg(value):
    """`--day` 取值校验：日期串打错时按"无法定论"中止，不静默对出一天空结论。"""
    if not _DAY_RE.match(value):
        raise argparse.ArgumentTypeError(f"日期格式应为 YYYY-MM-DD: {value!r}")
    return value


def _positive_int(value):
    """`--all-days` 取值校验：0/负数会扫 0 天而报"对账平"，属假结论。"""
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"天数应为正整数: {value!r}") from None
    if n < 1:
        raise argparse.ArgumentTypeError(f"天数应为正整数: {value!r}")
    return n


def _target_days(args):
    """待核对的日子：`--day` 单日 / `--all-days N` 最近 N 天（含今天）/ 缺省今天。"""
    if args.day:
        return [args.day]
    anchor = clock.now()
    return [(anchor - timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(args.all_days or 1)]


def _read_terminals(state_dir, day):
    """该日的终态账号 → JSON 状态；文件缺失/损坏/非 dict → None（该日无输入）。

    终态判定与 `migrate_v20` 共用同一张常量表：各写一份必然漂移，对账会据此报出
    并不存在的差异（或漏报真差异）。
    """
    path = os.path.join(state_dir, f"sign-state-{day}.json")
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    out = {}
    for phone, entry in data.items():
        if not isinstance(entry, dict):
            continue
        st = str(entry.get("status") or "").strip()
        if st in migrations._JSON_TERMINAL_TO_TASK_STATE:
            out[phone] = st
    return out


def _check_day(conn, state_dir, day, report):
    """核对单日，把结论行追加进 `report`，返回差异处数。"""
    problems = 0

    # 1) 终态覆盖：JSON 里有终态、台账里没有该账号 → 对账不平
    terminals = _read_terminals(state_dir, day)
    if terminals is None:
        report.append(f"[{day}] 无按日状态文件，跳过终态覆盖检查")
    else:
        known = {r["phone"] for r in
                 conn.execute("SELECT phone FROM sign_tasks WHERE day=?", (day,))}
        for phone in sorted(terminals):
            if phone not in known:
                problems += 1
                report.append(f"{day} {mask_phone(phone)} {terminals[phone]}")

    # 2) 状态值合法：越界值说明有写者用了词表外的状态，统计口径随之失真
    states = {r["state"] for r in
              conn.execute("SELECT DISTINCT state FROM sign_tasks WHERE day=?", (day,))}
    illegal = sorted(states - set(queue_store.STATES))
    if illegal:
        problems += 1
        report.append(f"[{day}] 越界状态值: {', '.join(illegal)}"
                      f"（词表: {'/'.join(queue_store.STATES)}）")

    # 3) 计数自洽：总数只能由平移行 + 补账行解释，多出来的就是"第三个写者"
    total, translated, backfilled = conn.execute(
        "SELECT COUNT(*), "
        "COALESCE(SUM(vshard = ? AND owner <> ?), 0), "
        "COALESCE(SUM(owner = ?), 0) FROM sign_tasks WHERE day = ?",
        (_TRANSLATED_SHARD, _BACKFILL_OWNER, _BACKFILL_OWNER, day),
    ).fetchone()
    other = total - translated - backfilled
    if other:
        problems += 1
        report.append(f"[{day}] 计数不自洽: 总数 {total} ≠ 平移 {translated} + "
                      f"补账 {backfilled}（无来源 {other} 行）")
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="台账对账：JSON 终态是否都在 sign_tasks、状态值是否合法、计数是否自洽")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--day", type=_day_arg, default=None,
                       help="只核对某一天（YYYY-MM-DD，缺省今天）")
    group.add_argument("--all-days", type=_positive_int, default=None,
                       help="核对最近 N 天（含今天）")
    args = parser.parse_args(argv)

    state_dir = env_io.resolve_path("YIBAN_STATE_DIR", "/var/log/yiban")
    if not os.path.isdir(state_dir):
        print(f"对账无法定论：状态目录不存在 {state_dir}")
        return 2
    db_path = env_io.resolve_path("YIBAN_DB_FILE", "yiban.db")
    # 库文件必须已存在：sqlite3.connect 缺库即建空库，空库上三项检查全部"通过"，
    # 对"台账对不对"什么都没说（路径写错时静默误报平账）。
    if not os.path.exists(db_path):
        print(f"对账无法定论：数据库文件不存在 {db_path}（拒绝新建空库误报平账）")
        return 2

    days = _target_days(args)
    report = []
    problems = 0
    try:
        # 只读口径：不迁移（迁移会写库，且 v20 会顺手补行，使被核对对象在校验过程中
        # 被改动）、不做启动清理。
        conn = db.init_db(db_file=db_path, cleanup=False, migrate=False)
        for day in days:
            problems += _check_day(conn, state_dir, day, report)
    except sqlite3.Error as e:
        # 表缺失（库还没升到 v18）与库损坏都归"无法定论"，不用 exit 1 冒充一次对账
        print(f"对账无法定论：库不可用 {e}")
        return 2

    print(f"库：{db_path}")
    print(f"状态目录：{state_dir}")
    for line in report:
        print(line)
    if problems:
        print(f"对账不平：{len(days)} 天里 {problems} 处差异")
        return 1
    print(f"对账平：{len(days)} 天，终态覆盖 / 状态词表 / 计数三项均无差异")
    return 0


if __name__ == "__main__":
    sys.exit(main())
