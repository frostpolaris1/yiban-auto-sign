# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""时钟守卫人工重置工具（批次12 B12-9，用户裁决 2026-08-29：告警 + 显式人工重置）。

背景：库内 5 处物理清理（软删账号/注销用户/注销请求/审计/事件）接入时钟跳变
守卫（2026-08-28 审查 M3）——系统时间前进 >72h 或回拨 >1h 时跳过清理，防
"拨快后刚软删的数据被立即物理清除"。守卫失败不更新参照点，因此清理会**保持
冻结**直至人工重置（合法停机 >3 天后即触发；告警由 web 每日线程读取
app_meta 的 clock_guard_alert 留痕并发送邮件）。

本工具在人工核实系统时间已正确（NTP 正常）后，把 5 个守卫参照点重置为当前
时间并清除告警标记，清理随之恢复。**刻意不提供自动恢复**：自动把参照点拨到
当前时间，等于给攻击者"拨快一次、下一轮全部洗白"留通道。

用法：
    python3 scripts/clock_guard_reset.py [--db yiban.db]        # 仅显示当前状态
    python3 scripts/clock_guard_reset.py [--db yiban.db] --confirm  # 执行重置

重置动作写入审计链（actor=clock-guard-reset），供事后追溯。
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db

# 与 db._clock_jump_guard 的调用点一一对应（新增守卫时同步维护此清单）
GUARD_KEYS = (
    "purge_accounts_clock",
    "purge_users_clock",
    "purge_requests_clock",
    "audit_cleanup_clock",
    "event_cleanup_clock",
)
ALERT_KEY = "clock_guard_alert"


def _open_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def show_status(db_path):
    """只读展示：各守卫参照点当前值 + 未清除的告警内容。"""
    conn = _open_conn(db_path)
    try:
        print(f"数据库: {db_path}")
        print(f"当前系统时间: {datetime.now():%Y-%m-%d %H:%M:%S}")
        print("守卫参照点（与当前时间差距过大即为冻结状态）：")
        for key in GUARD_KEYS:
            r = conn.execute(
                "SELECT value FROM app_meta WHERE key=?", (key,)
            ).fetchone()
            print(f"  {key:24s} = {r['value'] if r else '（无记录，守卫从未运行）'}")
        r = conn.execute(
            "SELECT value FROM app_meta WHERE key=?", (ALERT_KEY,)
        ).fetchone()
        if r and r["value"]:
            try:
                data = json.loads(r["value"])
                print(f"未清除告警（{data.get('ts', '?')}）：{data.get('note', '')}")
            except ValueError:
                print(f"未清除告警：{r['value']}")
        else:
            print("未清除告警：无")
        print("\n重置前请核实：系统时间与 NTP 同步正确、非攻击者拨快后的假时间。")
        print("确认后执行：python3 scripts/clock_guard_reset.py --confirm")
    finally:
        conn.close()


def reset(db_path):
    """重置全部守卫参照点为当前时间 + 清除告警 + 审计留痕。"""
    db.init_db(db_file=db_path, cleanup=False, migrate=False)
    conn = _open_conn(db_path)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn.execute("BEGIN IMMEDIATE")
        for key in GUARD_KEYS:
            conn.execute(
                "INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)", (key, now)
            )
        conn.execute("DELETE FROM app_meta WHERE key=?", (ALERT_KEY,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    # 重置动作留痕审计链（尽力而为；失败由每日写入欠账告警兜住）
    try:
        db.audit("clock-guard-reset", "clock_guard_reset", "", f"守卫参照点重置为 {now}")
    except Exception as e:  # noqa: BLE001
        print(f"提示：审计留痕失败（不影响重置）: {e}")
    print(f"已重置 {len(GUARD_KEYS)} 个守卫参照点为 {now}，并清除告警标记。")
    print("物理清理将随下一次调度（web 每日线程 / 启动清理）恢复执行。")


def main():
    parser = argparse.ArgumentParser(
        description="时钟守卫人工重置：核实系统时间正确后恢复被冻结的物理清理"
    )
    parser.add_argument("--db", default=None, help="yiban.db 路径（默认 YIBAN_DB_FILE/默认路径）")
    parser.add_argument("--confirm", action="store_true", help="执行重置（缺省仅显示状态）")
    args = parser.parse_args()

    db_path = args.db or os.environ.get("YIBAN_DB_FILE", db.DB_DEFAULT)
    if not os.path.exists(db_path):
        print(f"错误：数据库不存在: {db_path}（拒绝新建空库）")
        sys.exit(2)
    if args.confirm:
        reset(db_path)
    else:
        show_status(db_path)


if __name__ == "__main__":
    main()
