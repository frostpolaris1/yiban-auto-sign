# -*- coding: utf-8 -*-
"""loadtest 工具的 .env 快速失败与会话缓存清空门（防误指生产库的真实重登风暴）。

标签：J · 运维：部署/备份/发布
覆盖：`scale_driver` / `concurrency_probe` 的 `load_env_file` 读不到即报错（不再静默
    返回空 dict 并继承宿主环境）；`clear_session_cache` 必须显式声明目标指纹且目标库
    内容像压测库（owner 属 `@mock.invalid`）才允许 DELETE；两个入口未声明指纹即退 2。
对应实现：`scripts/loadtest/isolation.py`（`loadtest_db_fingerprint` /
    `assert_loadtest_target`）与 `scale_driver.py` / `concurrency_probe.py` 的同名函数。
关键断言：`--dry-run`/缺参数不得静默；"读不到 .env"必须响亮；清会话缓存前必须能区分
    "压测库"与"生产库"——三条都用活体反例钉住（缺指纹、非压测账号、文件不可读）。
依赖：临时目录 + 数据层真 schema 库；不起子进程、不连网络、不碰 /etc/hosts。
"""
from __future__ import annotations

import importlib
import os
import sqlite3
import sys

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOADTEST = os.path.join(BASE, "scripts", "loadtest")
if os.path.dirname(_LOADTEST) not in sys.path:
    sys.path.insert(0, os.path.dirname(_LOADTEST))

isolation = importlib.import_module("loadtest.isolation")
scale_driver = importlib.import_module("loadtest.scale_driver")
concurrency_probe = importlib.import_module("loadtest.concurrency_probe")


def _make_db(path, env_file, owner):
    """建真 schema 库并写一个账号 + 两行会话缓存。"""
    from yiban.store import connection
    from yiban.store import db as store_db
    conn = connection.current()
    if conn is not None:
        conn.close()
    connection.reset_conn()
    store_db.init_db(db_file=str(path), env_file=str(env_file), cleanup=False)
    store_db.add_account({
        "name": "lt", "phone": "13100000000", "password": "p",
        "phone_model": "", "phone_code": "", "owner": owner,
        "status": "active", "reject_reason": "",
    })
    c = store_db.get_conn()
    c.execute("INSERT OR REPLACE INTO session_cache "
              "(phone, cookies_ct, csrf, created_at, updated_at) VALUES (?,?,?,?,?)",
              ("13100000000", "ct", "csrf", "2026-01-01 00:00:00", "2026-01-01 00:00:00"))
    c.commit()
    c.close()
    connection.reset_conn()


def _session_rows(path):
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("SELECT COUNT(*) FROM session_cache").fetchone()[0]
    finally:
        conn.close()


@pytest.fixture()
def loadtest_db(tmp_path):
    db = tmp_path / "yiban.db"
    env = tmp_path / "test.env"
    env.write_text("YIBAN_ACCOUNTS_KEY=" + "a" * 64 + "\n"
                   "YIBAN_AUDIT_KEY=" + "b" * 64 + "\n", encoding="utf-8")
    _make_db(db, env, "loadtest00000@mock.invalid")
    yield db, env
    from yiban.store import connection
    conn = connection.current()
    if conn is not None:
        conn.close()
    connection.reset_conn()


# ---------------------------------------------------------------------------
# load_env_file：读不到即报错（不再静默继承宿主环境）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("mod", [scale_driver, concurrency_probe])
def test_load_env_file_missing_raises(tmp_path, mod):
    with pytest.raises(OSError):
        mod.load_env_file(str(tmp_path / "no-such.env"))


@pytest.mark.parametrize("mod", [scale_driver, concurrency_probe])
def test_load_env_file_unreadable_raises(tmp_path, mod):
    d = tmp_path / "adir"
    d.mkdir()
    with pytest.raises(OSError):
        mod.load_env_file(str(d))


@pytest.mark.parametrize("mod", [scale_driver, concurrency_probe])
def test_load_env_file_reads_existing(tmp_path, mod):
    p = tmp_path / ".env"
    p.write_text('YIBAN_DB_FILE="/x.db"\n', encoding="utf-8")
    assert mod.load_env_file(str(p))["YIBAN_DB_FILE"] == "/x.db"


# ---------------------------------------------------------------------------
# clear_session_cache：目标指纹 + 压测内容门
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("mod", [scale_driver, concurrency_probe])
def test_clear_session_cache_requires_fingerprint(loadtest_db, mod):
    db, _env = loadtest_db
    before = _session_rows(db)
    with pytest.raises(isolation.IsolationError):
        mod.clear_session_cache(str(db))
    assert _session_rows(db) == before, "缺指纹必须零删除"


@pytest.mark.parametrize("mod", [scale_driver, concurrency_probe])
def test_clear_session_cache_refuses_non_loadtest_db(tmp_path, mod):
    db = tmp_path / "prod.db"
    env = tmp_path / "prod.env"
    env.write_text("YIBAN_ACCOUNTS_KEY=" + "a" * 64 + "\n", encoding="utf-8")
    _make_db(db, env, "real-user@corp.example")
    fp = isolation.loadtest_db_fingerprint(str(db))
    before = _session_rows(db)
    with pytest.raises(isolation.IsolationError):
        mod.clear_session_cache(str(db), fp)
    assert _session_rows(db) == before, "误指生产库必须零删除（防真实重登风暴）"
    from yiban.store import connection
    conn = connection.current()
    if conn is not None:
        conn.close()
    connection.reset_conn()


@pytest.mark.parametrize("mod", [scale_driver, concurrency_probe])
def test_clear_session_cache_ok_with_fingerprint(loadtest_db, mod):
    db, _env = loadtest_db
    fp = isolation.loadtest_db_fingerprint(str(db))
    mod.clear_session_cache(str(db), fp)
    assert _session_rows(db) == 0


# ---------------------------------------------------------------------------
# 入口：未声明/错指纹即退 2（早于任何重活）
# ---------------------------------------------------------------------------
def _patch_isolation(monkeypatch):
    monkeypatch.setattr(isolation, "assert_no_proxy", lambda *a, **k: True)


def test_scale_driver_main_requires_db_fingerprint(tmp_path, monkeypatch):
    _patch_isolation(monkeypatch)
    db = tmp_path / "yiban.db"
    env = tmp_path / "test.env"
    env.write_text("", encoding="utf-8")
    rc = scale_driver.main([
        "--repo", str(tmp_path), "--env", str(env), "--db", str(db), "--n", "1",
        "--ca", str(tmp_path / "ca.pem"), "--mock-log", str(tmp_path / "mock.jsonl"),
        "--outdir", str(tmp_path / "out"),
    ])
    assert rc == 2, "未声明目标指纹必须退 2"


def test_concurrency_probe_main_requires_db_fingerprint(tmp_path, monkeypatch):
    _patch_isolation(monkeypatch)
    db = tmp_path / "yiban.db"
    env = tmp_path / "test.env"
    env.write_text("", encoding="utf-8")
    rc = concurrency_probe.main([
        "--repo", str(tmp_path), "--env", str(env), "--db", str(db),
        "--ca", str(tmp_path / "ca.pem"), "--outdir", str(tmp_path / "out"),
    ])
    assert rc == 2, "未声明目标指纹必须退 2"
