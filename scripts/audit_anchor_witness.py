# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""**功能**
审计锚点的**独立见证**：把锚点文件当前指纹写进一个与锚点文件不同属主的库外文件。

锚点文件与"锚点指纹"（app_meta）都由应用身份读写，因此拿到应用写权限的人可以
同时改写两者，把"最近 N 条审计被删"伪装成自洽状态，体检照样 healthy。本脚本由
部署方的 **root 侧** 定时任务调用，只读锚点、只写独立文件——改写它需要另一份权限，
双写掩盖才会在逐日校验里留下缺口。

**归属**
取证/运维侧脚本（`scripts/`）；判据本体与单调写实现在 `yiban.store.audit_chain`
的 `record_audit_anchor_witness`，本脚本只做路径解析与退出码落定。

**复用**
无对外可复用函数；读写入口一律复用 `yiban.store.audit_chain`，不得另写第二套。

**通信**
用法：`python3 scripts/audit_anchor_witness.py`（无参数；路径走 `.env` 与默认值）。
输入：锚点文件（`db.audit_anchor_path()`）、独立指纹文件（`db.audit_anchor_fingerprint_path()`）。
输出：一行状态到 stdout；退出码 0 = 见证已写入或无需更新；1 = 检测到回退/改写/损坏
（锚点被篡改的信号，需告警）；2 = 无法见证（锚点缺失或末行不可解析）。
调用谁：`db`（`yiban.store.db` / `audit_chain` 的兼容壳）。
谁调用：`deploy/prod/cron.d/yiban-audit-witness`（root，每 10 分钟一次）。
只读锚点、只写独立文件，绝不写审计库。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db


def main():
    anchor = db.audit_anchor_path()
    fingerprint = db.audit_anchor_fingerprint_path()
    state, message = db.record_audit_anchor_witness(anchor, fingerprint)
    print(f"锚点={anchor}")
    print(f"独立见证={fingerprint}")
    print(f"见证状态={state}" + (f"（{message}）" if message else ""))
    if state in ("regression", "rewritten", "corrupt", "unreadable"):
        # 这几态是"锚点被回退/改写"的现场：见证拒绝覆盖，退出码给告警，不静默。
        return 1
    if state in ("no-anchor", "unparseable"):
        # 没有可见证的内容（从未写锚点 / 末行是坏行）——本脚本无从下手，但也不是
        # "见证通过"；退出码与"检测到篡改"分开，免得把编码事故当失陷响应。
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
