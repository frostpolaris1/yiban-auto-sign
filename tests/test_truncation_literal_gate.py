# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""截断长度字面量门：审计 detail 与领取结果摘要不得再写裸 200。

标签：G · 安全：脱敏/审计/配置注入
覆盖：`claims.RESULT_MAX`、`queue_store.RESULT_MAX`、`audit_chain.AUDIT_DETAIL_LIMIT`、
    `db.AUDIT_DETAIL_LIMIT`，以及九个写入/读取面的裸字面量。
对应实现：两个上限各有一个唯一定义点——领取结果摘要（`claims.RESULT_MAX`）与
    审计 detail（`audit_chain.AUDIT_DETAIL_LIMIT`）。其余模块只引用别名。
关键断言：
  ① 扫描面九个文件都存在且总行数非零（扫空不得当绿）；
  ② 扫描面任何 `[... 200 ...]` 切片都不得出现（截断参数位）；
  ③ 两族常量的别名与定义点相等，且基线值仍是 200（本批零行为）；
  ④ 检测器对合成裸 200 报红、对常量引用放行。
面外登记（**不是**审计 detail 或领取结果摘要，故不扫）：
  · `yiban/adapters/persistence/run_events.py`、`yiban/app/round.py`、
    `yiban/engine/executor_v3.py`：运行进度 `message` 的展示截断，独立语义；
  · `web/routes/run_events_api.py`、`web/routes/data.py`：分页上限，归
    `tests/test_pagination_contract_gate.py` 与契约 §1.8 管。
依赖：只读源码与模块级常量；不起服务、不发网络请求。
"""

import os
import re
import unittest

from yiban.adapters.persistence import audit_chain, claims, db, queue_store

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 截断语境：方括号里出现 200。页面大小写 `_MAX_PAGE_SIZE = 200` 不在方括号内，不命中。
BARE_200 = re.compile(r"\[[^\[\]]*\b200\b[^\[\]]*\]")

#: 扫描面：两个上限的全部写入/读取点（含同族审计写点）。
SCAN_FILES = (
    "yiban/adapters/persistence/claims.py",
    "yiban/adapters/persistence/queue_store.py",
    "yiban/adapters/persistence/audit_chain.py",
    "yiban/adapters/persistence/purge_guard.py",
    "web/routes/audit_api.py",
    "web/routes/settings_api.py",
    "web/routes/users_api.py",
    "web/routes/accounts_api.py",
    "web/services/channel_health.py",
)

#: 面外读者登记：文件 → 为什么不属于这两个上限。
NOT_SCANNED = {
    "yiban/adapters/persistence/run_events.py": "运行进度 message 展示截断，独立语义",
    "yiban/app/round.py": "运行进度 message 展示截断，独立语义",
    "yiban/engine/executor_v3.py": "运行进度 message 展示截断，独立语义",
    "web/routes/run_events_api.py": "分页上限，归 §1.8 分页档位门",
    "web/routes/data.py": "分页上限，归 §1.8 分页档位门",
}


def _read(rel):
    with open(os.path.join(BASE, rel), encoding="utf-8") as fh:
        return fh.read()


def offenders(text):
    """源码 → 含裸 200 切片的位置列表 `[(行号, 行文本)]`。空文本回空列表。"""
    return [(no, line) for no, line in enumerate(text.splitlines(), 1)
            if BARE_200.search(line)]


class TruncationConstantTest(unittest.TestCase):
    """常量别名同值，扫描面无裸字面量。"""

    def test_scan_face_exists_and_is_not_empty(self):
        self.assertEqual(len(SCAN_FILES), 9)
        total = 0
        for rel in SCAN_FILES:
            self.assertTrue(os.path.isfile(os.path.join(BASE, rel)), f"扫描面缺文件：{rel}")
            total += len(_read(rel).splitlines())
        self.assertGreater(total, 500, "扫描面总行数异常偏小，门可能扫空")

    def test_no_bare_200_in_truncation_position(self):
        problems = []
        for rel in SCAN_FILES:
            for no, line in offenders(_read(rel)):
                problems.append(f"  {rel}:{no}: {line.strip()}")
        self.assertEqual(problems, [],
                         "截断参数位出现裸 200（应引用 RESULT_MAX / AUDIT_DETAIL_LIMIT）：\n"
                         + "\n".join(problems))

    def test_aliases_share_one_definition_and_baseline(self):
        self.assertEqual(claims.RESULT_MAX, queue_store.RESULT_MAX)
        self.assertEqual(audit_chain.AUDIT_DETAIL_LIMIT, db.AUDIT_DETAIL_LIMIT)
        # 基线值（2026-10-25，origin/develop=3b1fa8bd）：本批只换引用，不改宽度。
        self.assertEqual(claims.RESULT_MAX, 200)
        self.assertEqual(audit_chain.AUDIT_DETAIL_LIMIT, 200)

    def test_not_scanned_registry_points_at_real_files(self):
        for rel, reason in NOT_SCANNED.items():
            self.assertTrue(os.path.isfile(os.path.join(BASE, rel)), f"登记失效：{rel}")
            self.assertTrue(reason.strip())


class DetectorTeethTest(unittest.TestCase):
    """检测器必须认出裸 200、放行常量引用。"""

    def test_detects_bare_literal(self):
        self.assertEqual(len(offenders('x = (value or "")[:200]\n')), 1)

    def test_accepts_constant_reference(self):
        self.assertEqual(offenders('x = (value or "")[:AUDIT_DETAIL_LIMIT]\n'), [])

    def test_detects_literal_inside_max_expression(self):
        self.assertEqual(len(offenders("x = (detail[: max(0, 200 - len(tag))] + tag)[:200]\n")), 1)


if __name__ == "__main__":
    unittest.main()
