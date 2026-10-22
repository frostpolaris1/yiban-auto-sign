# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""Playwright e2e 的「空管理员」临时实例（F4 首次启动引导）。

**为什么单独一个实例**：`/setup` 只在 .env 还没有管理员时可达；主实例
（`e2e/server.py`）必须预置管理员来跑登录/管理页用例。两个状态不能在同一进程里
共存（`web.app` 的 ENV_FILE 是模块级全局），故另起一个临时 Flask 实例与端口。
本脚本不写任何 `YIBAN_ADMIN_*` 键，其余初始化顺序与主实例一致：临时目录 +
YIBAN_* 指向其中 + importlib 装载 `web/app.py` + `db.init_db()` + `create_app()`。

用法（由 `playwright.config.ts` 的第二个 webServer 拉起）：
    YB_E2E_PYTHON=<解释器> python e2e/setup_server.py   # 默认 127.0.0.1:8766
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _p in (ROOT, ROOT / "scripts", ROOT / "web"):
    sys.path.insert(0, str(_p))

PORT = int(os.environ.get("YB_E2E_SETUP_PORT", "8766"))
TEST_KEY = "b" * 64


def main():
    tmp = Path(tempfile.mkdtemp(prefix="yiban-e2e-setup-"))
    (tmp / "state").mkdir()
    # 刻意不写 YIBAN_ADMIN_USER / YIBAN_ADMIN_PASSWORD / YIBAN_ADMIN_PASSWORD_HASH：
    # 空管理员状态就是本实例的被测前提。SECRET_KEY 由应用首启自动生成。
    (tmp / ".env").write_text(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n", encoding="utf-8")
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

    spec = importlib.util.spec_from_file_location("webapp_e2e_setup", str(ROOT / "web" / "app.py"))
    webapp = importlib.util.module_from_spec(spec)
    sys.modules["webapp_e2e_setup"] = webapp
    spec.loader.exec_module(webapp)

    import db  # 裸模块名：sys.path 已含 scripts

    db.init_db(
        os.environ["YIBAN_DB_FILE"],
        migrate_from=os.environ["YIBAN_ACCOUNTS_FILE"],
        env_file=os.environ["YIBAN_ENV_FILE"],
    )
    app = webapp.create_app()

    # 与主实例同一理由：首配的 POST 也经全局限速；e2e 只跑少数几次，避免复用
    # 同一 IP 时与主实例的计数互相干扰。
    from web import security as _security_mod

    _orig_bump = _security_mod._bump_window_count

    def _bump_e2e(store, key, now, window, limit=None):
        if window == 60 and limit == 10:
            limit = 10_000
        return _orig_bump(store, key, now, window, limit)

    _security_mod._bump_window_count = _bump_e2e

    print(f"[e2e] 空管理员实例就绪 http://127.0.0.1:{PORT}", flush=True)
    app.run(host="127.0.0.1", port=PORT, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
