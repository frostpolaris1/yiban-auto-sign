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
终态判定取 `yiban.store.migrations.terminal_task_state`（与 v20 补账同一份逻辑），
池状态词表取 `yiban.store.queue_store.STATES`。

**复用**
无对外可复用函数。

**通信**
用法：`python3 scripts/ledger_check.py [--day YYYY-MM-DD | --all-days N]`
输入：命令行 `--day`（缺省今天）/ `--all-days N`（最近 N 天，含今天）；库路径与状态
目录走环境变量与 `.env`（`YIBAN_DB_FILE` / `YIBAN_STATE_DIR`）。
输出：逐日结论与差异行到 stdout 并逐个打一遍结论，差异行的手机号经
`yiban.masking.mask_phone` 脱敏（对外输出不得含完整号码）。
退出码：`0`=对账平；`1`=有差异（已逐行打印）；`2`=无法定论（状态目录缺失 /
库文件缺失 / 库不可用（含库尚未升到 `sign_tasks` 存在的那一版）/ 任何未预期异常 /
零覆盖——一天的处理文件都没有，或处理文件里一条终态记录都没有）。`1` 只留给
**明确探测到的差异**，其余一律 `2`：把一次崩溃或空覆盖读成"对账不平/对账平"都会误导运维。
调用谁：`yiban.store.db`（`init_db(migrate=False, cleanup=False)` 的只读口径）、
`yiban.store.migrations`、`yiban.store.queue_store`、`yiban.infra.env_io`、
`yiban.clock`。
谁调用：运维在库升级后手工跑一次；无其它调用点（web 与 `yiban.cli` 都不调它）。
"""
import argparse
import json
import os
import re
import sys
from datetime import timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from yiban import clock  # noqa: E402
from yiban.infra import env_io  # noqa: E402
from yiban.masking import mask_phone, sanitize_text  # noqa: E402
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

    文件在但一条终态都没有时回空 dict（**不是** None）：这种窗口与"没有文件"一样
    没有可对账的东西，调用方据此判零覆盖，而不是把空集上的"通过"当成平账。

    终态判定复用 `migrations.terminal_task_state`（v20 补账用的同一份逻辑）：只共用
    常量表而各写一遍规范化，仍会在细节上漂移，对账据此报出并不存在的差异（或漏报
    真差异）。JSON 状态串原样带出，供差异行打印。
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
        if migrations.terminal_task_state(entry) is None:
            continue
        out[phone] = str(entry.get("status") or "").strip()
    return out


def _check_day(conn, state_dir, day, report):
    """核对单日，把结论行追加进 `report`。

    返回 `(差异处数, 是否有按日状态文件, 是否有可对账的终态)`——后两项分开带出，
    好让调用方把"没有文件"与"文件在但没有终态"都判成零覆盖：两者都没有可对账的
    输入，三项检查在空集上"通过"不说明台账对不对。
    """
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
    return problems, terminals is not None, bool(terminals)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="台账对账：JSON 终态是否都在 sign_tasks、状态值是否合法、计数是否自洽")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--day", type=_day_arg, default=None,
                       help="只核对某一天（YYYY-MM-DD，缺省今天）")
    group.add_argument("--all-days", type=_positive_int, default=None,
                       help="核对最近 N 天（含今天）")
    args = parser.parse_args(argv)
    try:
        return _reconcile(args)
    except Exception as e:
        # 兜底必须是"任何异常"：只捕 sqlite3.Error 时，.env 解析 / 时钟 / 连接层的异常
        # 会让解释器以 exit 1 退出，与"探测到差异"同码——运维会把一次崩溃误读成
        # "对账不平"。exit 1 只留给明确探测到的差异。
        print(f"对账无法定论：未预期异常 {type(e).__name__}: {sanitize_text(e)}")
        return 2


def _reconcile(args):
    """执行对账并返回退出码：`0`=平 / `1`=有差异 / `2`=无法定论。"""
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
    days_with_file = 0
    days_with_terminal = 0
    # 只读口径：不迁移（迁移会写库，且 v20 会顺手补行，使被核对对象在校验过程中
    # 被改动）、不做启动清理。
    conn = db.init_db(db_file=db_path, cleanup=False, migrate=False)
    for day in days:
        day_problems, has_file, has_terminal = _check_day(conn, state_dir, day, report)
        problems += day_problems
        days_with_file += 1 if has_file else 0
        days_with_terminal += 1 if has_terminal else 0

    print(f"库：{db_path}")
    print(f"状态目录：{state_dir}")
    for line in report:
        print(line)
    if problems:
        print(f"对账不平：{len(days)} 天里 {problems} 处差异")
        return 1
    if not days_with_terminal:
        # 没有一条可对账的终态 ⇒ 三项检查全在空集上"通过"，这盏绿灯说明不了台账
        # 对不对。空 dict / 全是无 status 的条目与"没有文件"同判，否则只留一个
        # 空状态文件的窗口就会报平账。
        why = ("没有一天存在按日状态文件" if not days_with_file
               else "按日状态文件里没有任何终态记录")
        print(f"对账无法定论：{len(days)} 天里{why}（零覆盖）")
        return 2
    print(f"对账平：{len(days)} 天，终态覆盖 / 状态词表 / 计数三项均无差异")
    return 0


if __name__ == "__main__":
    sys.exit(main())
