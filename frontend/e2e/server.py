# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""Playwright e2e 的临时实例启动器（P1c）。

**为什么需要它**：e2e 要一个真实 HTTP 服务 + 干净的临时 `.env`/SQLite + 预置审计行，
且**绝不碰仓库里的真实数据与真实易班接口**。本脚本把这套准备收在一处，由
`playwright.config.ts` 的 `webServer` 拉起（`npm run test:e2e`）。

**与 pytest 夹具的关系**：环境变量与初始化顺序刻意与 `tests/test_web_*.py` 的
setUpClass 保持一致（临时目录 + YIBAN_* 指向其中 + importlib 装载 `web/app.py`），
差别只是最后起真服务器而不是 test_client。

**种子**：登录本身不写审计，故显式经 `db.audit_unit` + `db.record_in_txn` 在一个
事务里写入 `YB_E2E_SEED_ROWS`（默认 60）条——足以触发一条完整的分页（默认 page_size=50
→ has_more=true → 加载更多）。链式 prev_hash 由 record_in_txn 逐条读取，故同事务顺序写
不会分叉。

用法（一般由 playwright 配置调用）：
    YB_E2E_PYTHON=<解释器> python e2e/server.py      # 默认 127.0.0.1:8765
"""
import importlib.util
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _p in (ROOT, ROOT / "scripts", ROOT / "web"):
    sys.path.insert(0, str(_p))

PORT = int(os.environ.get("YB_E2E_PORT", "8765"))
SEED_ROWS = int(os.environ.get("YB_E2E_SEED_ROWS", "60"))
#: V3 渲染层截断守卫的种子：单日执行体分组数必须**超过**读取层轮级上限
#: （`yiban/store/run_events.py::MAX_ROUNDS` = 200）。205 > 200，端点回
#: `rounds_truncated=true`，页面「已截断」说明才有可断言的内容。
CROWDED_ROUNDS = 205
#: 上述拥挤日相对今天的天数偏移。取 5 = 落在 14 天保留窗口内（新的窗口下界不挡它），
#: 且不撞今天 / 三天前（older）/ 取样工作日（_probe_day 取月初）。logs.spec.ts 用同一个偏移。
CROWDED_DAY_OFFSET = 5
ADMIN_USER = "admin"
ADMIN_PASS = "TestPass1234!"  # 满足主管理员 12 位三类策略
E2E_USER_EMAIL = "e2e-user@example.com"
USER_PASS = "UserPass123!"
TEST_KEY = "a" * 64


def _probe_day():
    """当月一个「非今天、非三天前」的**工作日**：日历底色/图例与「点日期→拉日志」的取样日。

    为什么必须是工作日：周末停签格走中性底 + 「休」角标，会把状态底色整个盖掉，且点击只提示
    「周末无需签到」而不查日志——挑周末就测不到日历页最重要的那条链（点日期 → 该日日志）。
    为什么排除今天与三天前：按日状态/日志会被 `/api/my-accounts`（今日状态行）与既有日志断言
    消费，落在那两天上会让别的用例随"今天是周几"漂移。
    """
    from calendar import monthrange
    from datetime import date as _date

    today = _date.today()
    older = today - timedelta(days=3)
    for d in range(1, monthrange(today.year, today.month)[1] + 1):
        cand = _date(today.year, today.month, d)
        if cand.weekday() < 5 and cand not in (today, older):
            return cand
    return None  # 理论上不可达（一个月必有多个工作日）


def _seed_log_file(webapp):
    """写**三天**的假日志（今天 / 三天前 / 取样工作日）。

    为什么不止一天：日期导航（查看该日 / 回到今天 / ?date= 深链）只有在存在"另一天"时才
    可观测——2026-10-03 的 blocking 回归（load 用服务端回显日期覆盖用户选择，导致日期栏整体
    失效）就是因为只种了一天、e2e 从未切换过日期。取样工作日（见 _probe_day）让日历页也能
    断言"点某个非今天的格子 → 面板拉到那天的日志"。日志行含完整手机号，用于断言端到端脱敏。

    行格式与 `tests/test_logs_by_date.py::_log_line` 一致（yiban 组件全级别入列）。
    事件由 `_seed_events()` 另种（须在 db.init_db 之后）。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    older = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
    probe = _probe_day()

    def write_day(date_str: str, marker: str) -> None:
        path = Path(webapp.log_path_for(date_str))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join([
                f"[{date_str} 06:31:01] [INFO] yiban: [13800138001] ✅ 签到成功（{marker}）",
                f"[{date_str} 06:31:02] [INFO] yiban.client: [13800138001] 生成定位: (118.8, 31.9)",
                f"[{date_str} 06:31:03] [WARNING] yiban: [13800138001] 单次尝试耗时偏长",
            ]) + "\n",
            encoding="utf-8",
        )

    write_day(today, "today")
    write_day(older, "older")
    if probe is not None:
        write_day(probe.isoformat(), "probe")



def _seed_events() -> None:
    """当日/历史事件（**须在 `db.init_db()` 之后**调用）。

    今天 2 条签到（含一条 attempt>1 的失败）+ 2 条探针；三天前 1 条签到。
    读取端 `sign_events_on` 按 `stage='sign'` 且 `ts` 落在当日筛选；探针同口径 stage='probe'。
    """
    import db  # 裸模块名：sys.path 已含 scripts

    today = datetime.now().strftime("%Y-%m-%d")
    older = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
    db.add_sign_event(f"{today} 06:31:01", "13800138001", "success", "签到成功", stage="sign", attempt=1)
    db.add_sign_event(f"{today} 06:31:05", "13800138001", "failed", "密码错误", stage="sign", attempt=3)
    db.add_sign_event(f"{older} 06:31:01", "13800138001", "success", "签到成功", stage="sign", attempt=1)
    db.add_sign_event(f"{today} 06:35:00", "13800138001", "ok", "探测正常", stage="probe", attempt=1)
    db.add_sign_event(f"{today} 06:40:00", "13800138001", "failed", "探测异常", stage="probe", attempt=1)


def _seed_run_events() -> None:
    """当日的内核进度事件（**须在 `db.init_db()` 之后**调用）。

    巡检块（`GET /api/admin/run-events`）的端到端种子：**同一业务日两轮**。
    为什么必须两轮：轮级摘要的「点开较早一轮」与「轮询沿用在途选择」两条行为
    只有在存在"另一轮"时才可观测（一轮时点它等于点当前，什么都测不到）。
    最新一轮是 `单执行体`（06:40，耗时 21 秒），较早一轮是 `并行执行体 #3`（06:30）。
    账号用**原始号**写入（表存原值），端到端断言响应里只有掩码形态；
    执行体用带主机名的稳定名（接口只许回角色与槽位）。时刻显式写死（不走 `report`
    的当前钟）：耗时与顺序要确定可断言。
    """
    import db  # 裸模块名：sys.path 已含 scripts

    today = datetime.now().strftime("%Y-%m-%d")
    owner = "single@e2e-host"
    older_owner = "worker-2@e2e-host"
    rows = [
        (f"{today} 06:40:00", today, "claim", owner, "13800138001", ""),
        (f"{today} 06:40:00", today, "claim", owner, "13900139002", ""),
        (f"{today} 06:40:00", today, "claim", owner, "13700137003", ""),
        (f"{today} 06:40:05", today, "start", owner, "13800138001", ""),
        (f"{today} 06:40:06", today, "start", owner, "13900139002", ""),
        (f"{today} 06:40:12", today, "success", owner, "13800138001", "签到成功"),
        (f"{today} 06:40:20", today, "fail", owner, "13900139002", "密码错误"),
        (f"{today} 06:40:21", today, "finalize", owner, "",
         "执行体会话收尾：本轮完成 2 个账号"),
        # 较早一轮（同一业务日、另一执行体）：供"点开较早一轮 + 轮询沿用选择"断言
        (f"{today} 06:30:00", today, "claim", older_owner, "13800138001", ""),
        (f"{today} 06:30:09", today, "success", older_owner, "13800138001", "签到成功"),
    ]
    conn = db.get_conn()
    conn.executemany(
        "INSERT INTO run_events (ts, day, node, executor, phone, message) "
        "VALUES (?,?,?,?,?,?)", rows)
    conn.commit()


def _seed_run_events_crowded() -> None:
    """单日轮数**超过读取层上限**的业务日（**须在 `db.init_db()` 之后**调用）。

    V3 渲染层截断守卫的种子：读取层 `run_events.MAX_ROUNDS` = 200，本种子在
    `今天-CROWDED_DAY_OFFSET` 造 `CROWDED_ROUNDS`（205）个执行体分组，使端点回
    `rounds_truncated=true`。页面据此渲染 `#run-summary-truncated`；日志页其它
    日子（今天 2 轮）不超限，据此断言该元素"不存在"。
    执行体用带主机名的 `worker-N@e2e-host`（接口只回 `worker-N`）。
    """
    import db  # 裸模块名：sys.path 已含 scripts

    day = (datetime.now() - timedelta(days=CROWDED_DAY_OFFSET)).strftime("%Y-%m-%d")
    rows = [
        (f"{day} 06:40:{i % 60:02d}", day, "claim", f"worker-{i}@e2e-host",
         "13800138001", "")
        for i in range(1, CROWDED_ROUNDS + 1)
    ]
    conn = db.get_conn()
    conn.executemany(
        "INSERT INTO run_events (ts, day, node, executor, phone, message) "
        "VALUES (?,?,?,?,?,?)", rows)
    conn.commit()


def _seed_state_files() -> None:
    """取样工作日（见 `_probe_day`）的假签到状态：让日历底色与图例端到端可见。

    文件形态与 web/routes/my.py 的月历读取一致（`sign-daily-YYYY-MM-DD.json`，值 =
    账号键 → 状态符号）。只种这一个账号的状态：管理端日历用另一张卡，正好顺带验证
    "状态按账号过滤"。
    """
    day = _probe_day()
    if day is None:
        return
    state_dir = Path(os.environ["YIBAN_STATE_DIR"])
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / f"sign-daily-{day.isoformat()}.json").write_text(
        json.dumps({"13800138001": "✅"}), encoding="utf-8"
    )


def main():
    tmp = Path(tempfile.mkdtemp(prefix="yiban-e2e-"))
    (tmp / "state").mkdir()
    (tmp / ".env").write_text(
        f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
        f"YIBAN_ADMIN_USER={ADMIN_USER}\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
        # 敏感口令门禁固定为 full：默认 risk 档下"同出口免口令"会把 A 档保存的口令门整段
        # 跳过（登录已写 login_ip=127.0.0.1，判为"没换环境"），e2e 就测不到"保存弹口令"这条
        # 页面行为。full 档让每次受门禁写都确定性要求当次口令——只有设置页 e2e 段做受门禁
        # 写操作，故对其它用例无副作用。
        f"YIBAN_PW_GATE=full\n",
        encoding="utf-8",
    )
    (tmp / "accounts.json").write_text(json.dumps([]), encoding="utf-8")

    os.environ.update({
        "YIBAN_ACCOUNTS_KEY": TEST_KEY,
        "YIBAN_ENV_FILE": str(tmp / ".env"),
        "YIBAN_ACCOUNTS_FILE": str(tmp / "accounts.json"),
        "YIBAN_USERS_FILE": str(tmp / "users.json"),
        "YIBAN_DB_FILE": str(tmp / "yiban.db"),
        "YIBAN_STATE_DIR": str(tmp / "state"),
        "YIBAN_LOG_FILE": str(tmp / "sign.log"),
        "YIBAN_DISABLE_PURGE_LOOP": "1",
    })

    # 当天的假日志文件必须在**应用模块加载之后**写：路径要经应用自己的派生规则
    # （log_path_for 由 YIBAN_LOG_FILE 推出按天文件名），直接写 sign.log 读不到——
    # 那样页面只会渲染应用自己写的少量告警行。见下方 _seed_log_file()。

    spec = importlib.util.spec_from_file_location("webapp_e2e", str(ROOT / "web" / "app.py"))
    webapp = importlib.util.module_from_spec(spec)
    sys.modules["webapp_e2e"] = webapp
    spec.loader.exec_module(webapp)

    _seed_log_file(webapp)

    import db  # 裸模块名：sys.path 已含 scripts

    db.init_db(
        os.environ["YIBAN_DB_FILE"],
        migrate_from=os.environ["YIBAN_ACCOUNTS_FILE"],
        env_file=os.environ["YIBAN_ENV_FILE"],
    )
    # 一个普通用户 + 两个生效账号：用户侧页面（/user/account、/user/calendar）与
    # 账号列表的"有数据"形态需要它；主管理员名下也放一个，便于对照 /my/account。
    db.create_user(E2E_USER_EMAIL, webapp.generate_password_hash(USER_PASS))
    db.add_account({"name": "e2e-user-acct", "phone": "13800138001", "password": "p1",
                    "status": "active", "owner": E2E_USER_EMAIL})
    db.add_account({"name": "e2e-admin-acct", "phone": "13900139002", "password": "p2",
                    "status": "active", "owner": ADMIN_USER})
    # 账号管理页（/work/accounts）e2e 种子：待审 / 已拒绝 / 软删除三态，供三组表格与
    # 批量、行操作链路断言。一律归属 admin（裸账号归属）：既不影响 /work/users 的
    # 「按注册用户分组计数」（admin 不在用户表），也不占用 e2e-user 名下账号。
    # 追加在既有两条之后 ⇒ 管理端 /my/account 首行仍是 e2e-admin-acct。
    db.add_account({"name": "e2e-pending-acct", "phone": "13700137003", "password": "p3",
                    "status": "pending", "owner": ADMIN_USER})
    db.add_account({"name": "e2e-rejected-acct", "phone": "13600136004", "password": "p4",
                    "status": "rejected", "reject_reason": "e2e 驳回示例", "owner": ADMIN_USER})
    _deleted_id = db.add_account({"name": "e2e-deleted-acct", "phone": "13500135005",
                                  "password": "p5", "status": "active", "owner": ADMIN_USER})
    db.set_account_deleted(_deleted_id, 1, deleted_at="2026-09-30 10:00:00", deleted_by="admin")
    _seed_events()
    _seed_run_events()
    _seed_run_events_crowded()
    _seed_state_files()
    with db.audit_unit(ADMIN_USER, "e2e_seed_open", target="e2e", detail="seed batch") as conn:
        for i in range(SEED_ROWS):
            db.record_in_txn(conn, ADMIN_USER, f"e2e_seed_{i}", target=f"acct-{i}", detail=f"seed detail {i}")

    app = webapp.create_app()
    print(f"[e2e] 临时实例就绪 http://127.0.0.1:{PORT}（种子 {SEED_ROWS} 条审计行）", flush=True)
    app.run(host="127.0.0.1", port=PORT, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
