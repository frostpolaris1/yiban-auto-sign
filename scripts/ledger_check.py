# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""**功能**
台账对账工具（只读）：核对按日状态文件里的**终态**是否都进了 `sign_tasks`。

三项检查：
1. **终态一致**：`sign-state-<day>.json` 里每条终态都按 `terminal_task_state` 期望一个
   台账状态，与同名 `sign_tasks` 行的实际状态逐条比对；不一致逐行打印
   `day phone json_status→期望 台账实际`。台账行缺失时**只有补账窗口内才算差异**——
   v20 补账是一次性迁移、只回看 `_BACKFILL_DAYS` 天，窗口外（以及补账日之后引擎不再
   为跳过类终态建行）缺行是常态，报"不平"会恒红、报"平"是给自己发绿灯，一律记
   "无法定论"。之所以必须**比状态而不只是比存在性**：v20 是 `INSERT OR IGNORE`，
   planner 先写的行赢，"JSON 说 success / 台账说 failed" 在存在性判据下永久失明（MF-92）。
2. **状态值合法**：该日 `sign_tasks.state` ⊆ 池的状态词表；
3. **计数自洽**：当日行数 == 平移行（`vshard=-1` 且 `owner<>'backfill'`）+ 补账行
   （`owner='backfill'`）+ 其它来源行（当日只有一个写者、且 v3 执行体未接线时为 0）。
   "其它来源"是**残差**（总数减前两项的推导值）而非独立清点，输出里照此标注——
   对账工具不得把推导值包装成独立证据（MF-91 的证据独立性要求）。

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
零覆盖——一天的处理文件都没有，或处理文件里一条终态记录都没有 /
补账窗口外缺行——有终态输入但因窗口外无法定论且无任何明确差异）。`1` 只留给
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
from datetime import datetime, timedelta

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
#: v20 的版本号：从迁移登记表按名字反查，不在本文件另抄一遍字面量
_V20_VERSION = next(v for v, name, _fn, _core in migrations._MIGRATIONS
                    if name == "v20_backfill_json_terminals")

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
    """该日的终态账号 → `(JSON 状态, 期望台账状态)`；文件缺失/损坏/非 dict → None。

    文件在但一条终态都没有时回空 dict（**不是** None）：与"没有文件"一样没有可对账的
    东西，调用方据此判零覆盖，而不是把空集上的"通过"当成平账。期望台账状态直接取
    `terminal_task_state` 的返回值——它就是 v20 补账写入台账时用的那一份判据，
    对账必须按同一份期望比对（各写一遍会在规范化细节上漂移，报出假差异或漏报真差异）。
    """
    path = os.path.join(state_dir, f"sign-state-{day}.json")
    try:
        # utf-8-sig：手工编辑过的状态文件可能带 BOM，BOM 会让 json.load 抛错、整日判成
        # "无输入"——那是假零覆盖，不是真没跑
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    out = {}
    for phone, entry in data.items():
        want = migrations.terminal_task_state(entry)
        if want is None:
            continue
        out[phone] = (str(entry.get("status") or "").strip(), want)
    return out


def _backfill_window(conn):
    """v20 补账实际覆盖的日窗（含两端 `[起, 应用日]`）；读不到真实应用时间 → None。

    "缺行是不是证据"取决于这一天的台账是谁负责造的：v20 是一次性迁移，只回看
    `_BACKFILL_DAYS` 天；窗口外（以及补账日之后跳过类终态不再被任何写者补进台账）
    缺行是常态而非篡改。继承回填的记录（`applied_at='inherited'`）与记录缺失一样
    给不出真实应用时间——诚实返回 None，缺行一律降级为"无法定论"，不猜窗口。
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (migrations._SCHEMA_MIGRATIONS_TABLE,)).fetchone()
    if not exists:
        return None
    row = conn.execute(
        f"SELECT applied_at FROM {migrations._SCHEMA_MIGRATIONS_TABLE} WHERE version=?",
        (_V20_VERSION,)).fetchone()
    if row is None:
        return None
    stamp = str(row["applied_at"] or "")[:10]
    if not _DAY_RE.match(stamp):
        return None
    end = datetime.strptime(stamp, "%Y-%m-%d")
    start = end - timedelta(days=migrations._BACKFILL_DAYS - 1)
    return start.strftime("%Y-%m-%d"), stamp


def _check_day(conn, state_dir, day, report, window):
    """核对单日，把结论行追加进 `report`。

    返回 `(差异处数, 是否有按日状态文件, 是否有可对账的终态, 是否有无法定论项)`——
    第二、三项分开带出，好让调用方把"没有文件"与"文件在但没有终态"都判成零覆盖：
    两者都没有可对账的输入，三项检查在空集上"通过"不说明台账对不对。第四项是
    "输入在、但该处证据无法定论"（补账窗口外缺行），调用方据此整体降级为
    `2`，既不报平也不报篡改。
    """
    problems = 0
    inconclusive = 0

    # 1) 终态一致：逐条**比状态**，不只比存在性。v20 是 INSERT OR IGNORE、planner
    #    先写的行赢，"JSON 说 success / 台账说 failed" 只有比对状态才探测得到
    terminals = _read_terminals(state_dir, day)
    if terminals is None:
        report.append(f"[{day}] 无按日状态文件，跳过终态覆盖检查")
    else:
        rows = {r["phone"]: r["state"] for r in
                conn.execute("SELECT phone, state FROM sign_tasks WHERE day=?", (day,))}
        for phone in sorted(terminals):
            raw, want = terminals[phone]
            have = rows.get(phone)
            if have is None:
                if window and window[0] <= day <= window[1]:
                    problems += 1
                    report.append(f"{day} {mask_phone(phone)} {raw}（台账缺行）")
                else:
                    inconclusive += 1
                    report.append(f"[{day}] {mask_phone(phone)} {raw} 台账无行，但该日不在"
                                  f"补账窗口内：无法定论（既非差异也非通过）")
            elif have != want:
                problems += 1
                report.append(f"{day} {mask_phone(phone)} 状态不一致："
                              f"JSON={raw}→{want}，台账={have}")

    # 2) 状态值合法：越界值说明有写者用了词表外的状态，统计口径随之失真
    states = {r["state"] for r in
              conn.execute("SELECT DISTINCT state FROM sign_tasks WHERE day=?", (day,))}
    illegal = sorted(states - set(queue_store.STATES))
    if illegal:
        problems += 1
        report.append(f"[{day}] 越界状态值: {', '.join(illegal)}"
                      f"（词表: {'/'.join(queue_store.STATES)}）")

    # 3) 计数自洽：总数只能由平移行 + 补账行解释，多出来的就是"第三个写者"。
    #    "无来源"是残差（推导值）而非独立清点，输出必须照此标注，不得冒充独立证据
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
                      f"补账 {backfilled}（无来源 {other} 行；残差=总数−平移−补账，"
                      f"非独立证据）")
    # 四个态分开：无文件/有文件无终态/有终态/无法定论（补账窗口外缺行）
    return problems, terminals is not None, bool(terminals), inconclusive


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="台账对账：JSON 终态与 sign_tasks 状态是否一致、状态值是否合法、计数是否自洽")
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
    inconclusive = 0
    days_with_file = 0
    days_with_terminal = 0
    # 只读口径：migrate=False 尤其关键——迁移会写库、v20 还顺手补行，被核对对象在
    # 核对过程中被改动，对账就不再是核对
    conn = db.init_db(db_file=db_path, cleanup=False, migrate=False)
    # 补账窗口在开库之后、逐日核对之前取一次：窗口外缺行不是证据，判据必须先确定
    window = _backfill_window(conn)
    for day in days:
        day_problems, has_file, has_terminal, day_inconclusive = _check_day(
            conn, state_dir, day, report, window)
        problems += day_problems
        inconclusive += day_inconclusive
        days_with_file += 1 if has_file else 0
        days_with_terminal += 1 if has_terminal else 0

    print(f"库：{db_path}")
    print(f"状态目录：{state_dir}")
    if window:
        print(f"v20 补账窗口：{window[0]} ~ {window[1]}（窗口内缺行才算差异）")
    else:
        print("v20 补账窗口：应用时间不可读，缺行一律无法定论")
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
    if inconclusive:
        # 窗口外缺行：不得报"平"（没有证据说它对），也不得报"篡改"（没有证据说它错）
        print(f"对账无法定论：{len(days)} 天里 {inconclusive} 处输入因在补账窗口外"
              f"无法定论（未探测到明确差异）")
        return 2
    print(f"对账平：{len(days)} 天，终态一致 / 状态词表 / 计数三项均无差异")
    return 0


if __name__ == "__main__":
    sys.exit(main())
