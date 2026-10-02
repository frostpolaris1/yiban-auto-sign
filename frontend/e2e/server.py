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
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _p in (ROOT, ROOT / "scripts", ROOT / "web"):
    sys.path.insert(0, str(_p))

PORT = int(os.environ.get("YB_E2E_PORT", "8765"))
SEED_ROWS = int(os.environ.get("YB_E2E_SEED_ROWS", "60"))
ADMIN_USER = "admin"
ADMIN_PASS = "TestPass1234!"  # 满足主管理员 12 位三类策略
TEST_KEY = "a" * 64


def _seed_log_file(webapp):
    """写当天的假日志（日志页要有一个可渲染的正文）。

    **故意写入完整手机号**：e2e 据此断言"端到端拿不到未脱敏号码"（展示层必须只见
    138****8001）。行格式与 `tests/test_logs_by_date.py::_log_line` 一致
    （`[date time] [LEVEL] logger: msg`，yiban 组件全级别入列）。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    log_path = Path(webapp.log_path_for(today))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "\n".join([
            f"[{today} 06:31:01] [INFO] yiban: [13800138001] ✅ 签到成功",
            f"[{today} 06:31:02] [INFO] yiban.client: [13800138001] 生成定位: (118.8, 31.9)",
            f"[{today} 06:31:03] [WARNING] yiban: [13800138001] 单次尝试耗时偏长",
        ]) + "\n",
        encoding="utf-8",
    )


def main():
    tmp = Path(tempfile.mkdtemp(prefix="yiban-e2e-"))
    (tmp / "state").mkdir()
    (tmp / ".env").write_text(
        f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
        f"YIBAN_ADMIN_USER={ADMIN_USER}\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n",
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
    with db.audit_unit(ADMIN_USER, "e2e_seed_open", target="e2e", detail="seed batch") as conn:
        for i in range(SEED_ROWS):
            db.record_in_txn(conn, ADMIN_USER, f"e2e_seed_{i}", target=f"acct-{i}", detail=f"seed detail {i}")

    app = webapp.create_app()
    print(f"[e2e] 临时实例就绪 http://127.0.0.1:{PORT}（种子 {SEED_ROWS} 条审计行）", flush=True)
    app.run(host="127.0.0.1", port=PORT, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
