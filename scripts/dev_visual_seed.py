# -*- coding: utf-8 -*-
"""本地「实拍」用演示实例：造一批有代表性的账号/事件/日志 + 建库建管理员，然后起服务。

    用途：前端审查需要**看真实渲染结果**（而不是读代码猜），本脚本把应用跑在一个临时
    环境里，用真实数据渲染各页面，供无头浏览器截图。

    与生产/测试的关系：完全隔离。所有文件写在 --tmp 目录（默认仓库 .tmp/visual-seed），
    不碰仓库根目录的 .env / yiban.db / accounts.json。数据库沿用
`db.init_db`，账号写入走 `db.add_account`（真实加密路径），故与线上数据结构一致。

用法：
    python scripts/dev_visual_seed.py --port 17899 [--tmp DIR] [--reset]

退出：Ctrl-C。重复运行会复用同一临时目录（除非 --reset）。
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import os
import random
import shutil
import sys
import time
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 直接执行本脚本时 sys.path[0] 是 scripts/，仓库根不在路径上——先补上才能 `from yiban...`。
if BASE not in sys.path:
    sys.path.insert(0, BASE)

# 演示环境的落盘一律走既有的锁 + 原子写路径，不再裸 `open(path, "w")`：
#   · `.env` → `env_io.write_env_keys`：内部自持 `env_lock.env_write_lock` 跨进程写锁
#     并做原子 0600 替换。裸 open 既无锁又可被并发写方读到半截内容；更重要的是它是
#     生产上**唯一**的 .env 写入口（`tests/test_env_writers_take_lock.py` 的 grep 格
#     就按这条断言扫全仓运行时代码），演示脚本另开一条裸写路径会让那条守卫永远红。
#   · 账号 JSON / 日志 / 按日状态 → `web.security._atomic_write`：tmp + fsync +
#     os.replace，与 web/引擎侧落盘同一份实现，读者（web 端按天读日志、按日读日历）
#     永远读不到半写状态。
from web.security import _atomic_write  # noqa: E402
from yiban.infra import env_io  # noqa: E402
from yiban.infra.account_crypto import (  # noqa: E402
    PUBLISHED_DEMO_ACCOUNTS_KEY as TEST_KEY,
)

# 演示钥与 `account_crypto` 的公开钥黑名单是**同一个常量**（单一事实源）：避免两处各写
# 一份、下次换钥只改一处导致黑名单漂移。它随仓库公开，照抄进真实 .env 会被启动拒绝。

ADMIN_USER = "admin"
ADMIN_PASS = "VisualPass1234!"
USER_PASS = "VisualUser123!"

# 演示用户（普通用户，登录后可看用户端两页）
DEMO_USERS = [
    "zhangsan@example.com",
    "lisi@example.com",
]

# (name, phone, model, status) —— 覆盖前端能渲染出的全部状态分支
ACCOUNTS = [
    ("张三", "13800138001", "Xiaomi 14", "active", False, False),
    ("李四", "13800138002", "iPhone 15", "active", False, False),
    ("王五", "13800138003", "HUAWEI Mate 60", "active", False, False),
    ("赵六（用户暂停）", "13800138004", "OPPO Find X7", "active", True, False),
    ("钱七（管理员暂停）", "13800138005", "vivo X100", "active", False, False),
    ("孙八（待审核）", "13800138006", "Redmi K70", "pending", False, False),
    ("周九（被拒）", "13800138007", "iPhone 14", "rejected", False, False),
    ("吴十", "13800138008", "Xiaomi 13", "active", False, False),
    ("郑十一（无记录）", "13800138009", "Honor Magic6", "active", False, False),
    ("王十二（无记录）", "13800138010", "OnePlus 12", "active", False, False),
    ("冯十三", "13800138011", "Samsung S24", "active", False, False),
    ("陈十四（待删除）", "13800138012", "Nothing Phone 2", "active", False, True),
]

# 签到状态码（唯一事实源 yiban/status.py）：success/already/no_task/failed/
# retrying/skipped_window/skipped_norange/paused/user_cancelled
EVENT_STATUSES = ["success", "already", "no_task", "failed", "retrying", "skipped_window", "skipped_norange"]

# 生成日志的时间跨度（天）
LOG_DAYS = 12


def _write_env(env_file: str) -> None:
    """建演示实例的 .env——走生产唯一写入口（内持跨进程写锁 + 原子 0600 替换）。

    空值键（`YIBAN_GLOBAL_PAUSE=` / `YIBAN_REGISTRATION_PAUSE=`）保持"键在、值为空"
    的形态：它们是设置页的开关位，留空即"未暂停"，删掉整行会让回显面失准。
    """
    env_io.write_env_keys(env_file, {
        "YIBAN_ACCOUNTS_KEY": TEST_KEY,
        "YIBAN_ADMIN_USER": ADMIN_USER,
        "YIBAN_ADMIN_PASSWORD": ADMIN_PASS,
        "YIBAN_SIGN_START": "06:30",
        "YIBAN_SIGN_END": "07:50",
        "YIBAN_MAX_USERS": "50",
        "YIBAN_MAX_ACCOUNTS": "200",
        "YIBAN_WORKERS": "2",
        "YIBAN_ANNOUNCEMENT": "实拍演示环境：本实例仅用于前端视觉核对，数据为随机生成。",
        "YIBAN_GLOBAL_PAUSE": "",
        "YIBAN_REGISTRATION_PAUSE": "",
        "YIBAN_ACCOUNT_VERIFY": "1",
        "YIBAN_ALLOW_TIME_PREF": "1",
        "YIBAN_PROBE_ENABLE": "1",
        "YIBAN_PROBE_TIME": "08:00",
        "YIBAN_PROBE_INTERVAL_DAYS": "3",
    })


def _log_line(ts: datetime, idx: int, name: str, phone: str, status: str) -> str:
    """造一行与生产同格式的签到日志（按天文件，见 run.sh 的 sign-YYYY-MM-DD.log）。"""
    model = {
        "success": f"签到成功 | {name}({phone}) 第一阶段完成，耗时 12.4s",
        "already": f"今日已签到 | {name}({phone}) 无需重复操作",
        "no_task": f"今日无任务 | {name}({phone}) 无待签到的任务",
        "failed": f"签到失败 | {name}({phone}) 登录态失效，重试 2 次后放弃",
        "retrying": f"重试中 | {name}({phone}) 第 2 次尝试",
        "skipped_window": f"已过窗口 | {name}({phone}) 超过签到截止时间",
        "skipped_norange": f"不在偏好内 | {name}({phone}) 当前时间不在自选时段",
    }[status]
    return f"{ts.strftime('%Y-%m-%d %H:%M:%S')} [{status.upper()}] {model}"


def seed(tmp: str, reset: bool = False) -> dict:
    if reset and os.path.isdir(tmp):
        shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)

    env_file = os.path.join(tmp, ".env")
    db_file = os.path.join(tmp, "yiban.db")
    accounts_file = os.path.join(tmp, "accounts.json")
    users_file = os.path.join(tmp, "users.json")
    state_dir = os.path.join(tmp, "state")
    log_dir = os.path.join(state_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)

    _write_env(env_file)
    _atomic_write(accounts_file, "[]")

    os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
    os.environ["YIBAN_ENV_FILE"] = env_file
    os.environ["YIBAN_ACCOUNTS_FILE"] = accounts_file
    os.environ["YIBAN_USERS_FILE"] = users_file
    os.environ["YIBAN_DB_FILE"] = db_file
    os.environ["YIBAN_STATE_DIR"] = state_dir
    os.environ["YIBAN_LOG_FILE"] = os.path.join(log_dir, "sign.log")
    # 演示实例不起清理线程、不发邮件（与测试同口径）
    os.environ.setdefault("YIBAN_DISABLE_PURGE_LOOP", "1")
    os.environ.setdefault("YIBAN_MAIL_ENABLE", "0")

    sys.path.insert(0, os.path.join(BASE, "scripts"))
    import db  # noqa: E402  裸模块名，pyproject pythonpath 已含 scripts

    spec = importlib.util.spec_from_file_location("webapp", os.path.join(BASE, "web", "app.py"))
    webapp = importlib.util.module_from_spec(spec)
    sys.modules["webapp"] = webapp
    with contextlib.suppress(Exception):
        spec.loader.exec_module(webapp)

    if reset and os.path.exists(db_file):
        os.remove(db_file)
    db.init_db(db_file, migrate_from=accounts_file, env_file=env_file)

    # 管理员由 .env 提供（check_admin_configured 读它）；普通用户要真实入库
    for email in DEMO_USERS:
        with contextlib.suppress(Exception):
            db.create_user(email, webapp.generate_password_hash(USER_PASS), role="user")
    with contextlib.suppress(Exception):
        db.create_user("admin@example.com", webapp.generate_password_hash(ADMIN_PASS), role="admin")

    existing = db.load_accounts()
    if not existing:
        for idx, (name, phone, model, status, user_paused, deleted) in enumerate(ACCOUNTS):
            # 业务约束：一个用户只能有一个未删除账号（db.DuplicateOwnerError）。
            # 故只有第 1 条挂到演示用户，其余归内置管理员，否则每第 3 条都会被拒。
            owner = DEMO_USERS[0] if idx == 0 else "admin"
            with contextlib.suppress(Exception):
                aid = db.add_account(
                    {
                        "name": name,
                        "phone": phone,
                        "password": "DemoPass123!",
                        "phone_model": model,
                        "phone_code": f"C{idx:04d}",
                        "owner": owner,
                        "status": "pending",
                    }
                )
                if status != "pending":
                    with contextlib.suppress(Exception):
                        db.update_account_status(
                            aid, status, reject_reason=("识别码与机型不匹配" if status == "rejected" else None)
                        )
                if user_paused:
                    with contextlib.suppress(Exception):
                        db.set_user_paused(aid, True)
                if deleted:
                    with contextlib.suppress(Exception):
                        db.set_account_deleted(aid, True, deleted_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), deleted_by="admin")

    # 签到事件 + 按天日志
    rng = random.Random(20260921)
    now = datetime.now()
    rows = []
    for day in range(LOG_DAYS):
        base_day = now - timedelta(days=day)
        for idx, (name, phone, _model, _status, _up, _del) in enumerate(ACCOUNTS):
            # 让「郑十一/王十二」两人最近几天没有记录，形成「无数据」分支
            if idx in (8, 9) and day < 4:
                continue
            status = rng.choice(EVENT_STATUSES)
            ts = base_day.replace(
                hour=6 + (idx % 2), minute=rng.randint(30, 59), second=rng.randint(0, 59)
            )
            if ts > now:
                ts = now - timedelta(minutes=rng.randint(1, 60))
            rows.append(
                {
                    "ts": ts.strftime("%Y-%m-%d %H:%M:%S"),
                    "phone": phone,
                    "status": status,
                    "message": {
                        "success": "签到成功", "already": "今日已签到",
                        "no_task": "今日无任务", "failed": "登录态失效",
                        # 消息格式对齐引擎真实口径（round.py：待重试（已 N 次）: 原因）——
                        # 此前写"重试中（第 2 次）"，与前端 attempt 标注叠加成双份
                        "retrying": "待重试（已 2 次）: 登录态失效", "skipped_window": "已过签到窗口",
                        "skipped_norange": "不在时间偏好内",
                    }[status],
                    "stage": rng.choice(["", "probe", "sign"]),
                    "attempt": rng.randint(0, 2),
                }
            )
    with contextlib.suppress(Exception):
        if not db.sign_events_since("1970-01-01 00:00:00", limit=1):
            db.add_sign_events_batch(rows)

    # 日志文件按天写（web 端按日期直接读文件）
    txt_by_day: dict[str, list[str]] = {}
    for i, r in enumerate(rows):
        day = r["ts"][:10]
        txt_by_day.setdefault(day, []).append(
            _log_line(datetime.strptime(r["ts"], "%Y-%m-%d %H:%M:%S"), i, "演示账号", r["phone"], r["status"])
        )
    for i, day in enumerate(sorted(txt_by_day)):
        path = os.path.join(log_dir, f"sign-{day}.log")
        _atomic_write(path, "\n".join(txt_by_day[day]) + "\n")
    # 今天必须有日志文件，否则日志页空态
    today_path = os.path.join(log_dir, f"sign-{now.strftime('%Y-%m-%d')}.log")
    if not os.path.exists(today_path):
        _atomic_write(today_path, _log_line(now, 0, "演示账号", ACCOUNTS[0][1], "success") + "\n")

    # 按日状态文件（sign-daily-YYYY-MM-DD.json，{phone: 状态符号}）——/api/my-calendar
    # 直接读的就是这一族（结构化 sign-state 是 /api/accounts 状态行的另一条事实源，
    # 两者别混，符号统一取自 yiban.status.DISPLAY 不另抄字面量）。此前种子根本没写
    # 这族文件、日历格一直空白。
    # 覆盖策略（色彩语义定版后图例按语气档收敛）：
    # - **全状态轮转** DAILY_CYCLE：账号 idx 在第 day 天取 (day + idx*7) % len——
    #   纯随机会漏掉低频码（无点位/已取消/待签），轮转保证近 35 天内 12 个状态码
    #   在日历上都有出处，"今天"一行也能同时看到七八种色点；
    # - 郑十一/王十二（idx 8/9）近 4 天不写（"无记录"分支）；周末不写（格子显示"休"）；
    # - 赵六（用户暂停）近 10 天记 paused（账密暂停·黄色点有出处）。
    from yiban.status import DISPLAY as _DISPLAY

    _sym = {code: e["symbol"] for code, e in _DISPLAY.items()}
    DAILY_CYCLE = ["success", "already", "no_task", "failed", "retrying",
                   "skipped_window", "skipped_norange", "no_position",
                   "user_cancelled", "pending"]
    for day in range(35):
        base_day = now - timedelta(days=day)
        if base_day.weekday() >= 5:
            continue
        date = base_day.strftime("%Y-%m-%d")
        day_data = {}
        for idx, (name, phone, _model, _status, user_paused, _del) in enumerate(ACCOUNTS):
            if idx in (8, 9) and day < 4:
                continue
            if user_paused and day < 10:
                code = "paused"
            else:
                code = DAILY_CYCLE[(day + idx * 7) % len(DAILY_CYCLE)]
            sym = _sym.get(code)
            if sym:
                day_data[phone] = sym
        if not day_data:
            continue
        _atomic_write(os.path.join(state_dir, f"sign-daily-{date}.json"),
                      json.dumps(day_data, ensure_ascii=False))

    return {
        "tmp": tmp, "env_file": env_file, "db_file": db_file,
        "state_dir": state_dir, "log_dir": log_dir,
        "webapp": webapp, "db": db,
        "accounts": len(db.load_accounts()),
        "events": len(rows),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="前端实拍用的演示实例")
    ap.add_argument("--port", type=int, default=17899)
    ap.add_argument("--host", default="127.0.0.1")
    # 默认落在仓库 .tmp/（.gitignore 已忽略），不写系统临时目录——临时文件纪律见 .tmp/README.md
    ap.add_argument("--tmp", default=os.path.join(BASE, ".tmp", "visual-seed"))
    ap.add_argument("--reset", action="store_true", help="重建演示数据")
    ap.add_argument("--slow-ms", type=int, default=0, help="给每个 /api/ 请求加固定延迟（只为实拍加载态）")
    args = ap.parse_args()

    info = seed(args.tmp, reset=args.reset)
    print(f"[seed] tmp={info['tmp']}")
    print(f"[seed] accounts={info['accounts']} events={info['events']} log_dir={info['log_dir']}")
    print(f"[seed] admin={ADMIN_USER} / {ADMIN_PASS}")
    print(f"[seed] user={DEMO_USERS[0]} / {USER_PASS}")
    app = info["webapp"].create_app()
    # 慢速模式（只为实拍加载态）：--slow-ms N 给每个 /api/ 请求加 N 毫秒延迟。
    # 本地回环下接口通常 <10ms，加载骨架一闪而过根本捕不到；这个开关让"慢链路/大文件"
    # 的加载占位可以被真实看到与截图。**只存在于本演示脚本**，不碰 web/app.py。
    if args.slow_ms > 0:
        import time as _time
        from flask import request as _req
        _delay = args.slow_ms / 1000.0

        @app.before_request
        def _visual_slow():  # noqa: D401
            if _req.path.startswith("/api/"):
                _time.sleep(_delay)
        print(f"[serve] 慢速模式：每个 /api/ 请求 +{args.slow_ms}ms")
    print(f"[serve] http://{args.host}:{args.port}/login")
    sys.stdout.flush()
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
