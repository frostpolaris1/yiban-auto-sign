# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""统计数据对接测试脚本（只测数据层，不实现 WebUI 功能）。

用法：
    python3 scripts/generate_demo_data.py
    python3 scripts/test_stats_integration.py [--db demo-log/demo.db]

说明：
- 调用 Phase 4 新增的只读聚合函数，验证大数据量下能返回预期结构。
- 同时校验审计哈希链，确认大量写入后链仍完整。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db


def main():
    parser = argparse.ArgumentParser(description="统计数据对接测试")
    parser.add_argument("--db", default="demo-log/demo.db", help="demo 数据库路径")
    parser.add_argument("--env", default="demo-log/demo.env", help="demo .env 路径")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"未找到 demo 数据库：{args.db}")
        print("请先运行：python3 scripts/generate_demo_data.py")
        sys.exit(1)

    db.init_db(args.db, env_file=args.env)

    print("=== sign_event_stats(days=30) ===")
    sign_stats = db.sign_event_stats(days=30)
    print(f"返回 {len(sign_stats)} 行")
    for row in sign_stats[:10]:
        print(f"  {row}")
    if sign_stats:
        assert "day" in sign_stats[0] and "status" in sign_stats[0] and "cnt" in sign_stats[0]

    print("\n=== page_visit_stats(days=30) ===")
    page_stats = db.page_visit_stats(days=30)
    print(f"返回 {len(page_stats)} 行")
    for row in page_stats[:10]:
        print(f"  {row}")
    if page_stats:
        assert "day" in page_stats[0] and "pv" in page_stats[0] and "uv" in page_stats[0]

    print("\n=== server_metric_history(hours=24) ===")
    history = db.server_metric_history(hours=24)
    print(f"返回 {len(history)} 行")
    for row in history[:5]:
        print(f"  {row}")
    if history:
        assert "ts" in history[0] and "cpu" in history[0]

    print("\n=== verify_audit_chain() ===")
    ok, broken, first = db.verify_audit_chain()
    print(f"ok={ok}, broken={broken}, first_broken_id={first}")
    assert ok, "审计哈希链校验失败"

    print("\n统计数据对接测试通过 [OK]")


if __name__ == "__main__":
    main()
