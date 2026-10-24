# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""多执行体演练的 `--mutate` 不得豁免两条不变式（工单 495-v1z）。

标签：B · 调度：领取/队列/执行体
覆盖：`scripts/drill_multi_exec.py::_cli` 的结论判定——`--mutate` 模式下 `i1`/`i2`
    违例必须让 ok=False、退出码非零。旧实现只看 `mutation_applied`，于是
    `scripts/drill-multi-exec.sh` 注释宣称的「有牙证明（应 FAIL）」实际回 PASS。
关键断言：突变生效且 i1 红 ⇒ rc=1；突变生效且 i2 红 ⇒ rc=1；两者都绿（`window-off`
    这一类不靠违反不变式的变异）⇒ rc=0；突变未生效 ⇒ rc=1。另有非变异档基线。
依赖：只跑纯判定路径——`run_drill` 用桩替换。被测性质是「检查字典 → ok → 退出码」，
    属纯计算；真起子进程的靶场 e2e 由 `tests/test_multi_executor_drill_e2e.py` 承担。
"""
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import drill_multi_exec as drill  # noqa: E402


def _res(*, i1=True, i2=True, mutation_applied=True):
    """最小靶场结论：只给 `_cli` 判定会读的键。"""
    return {
        "executors": 2,
        "accounts": 4,
        "done_count": 4,
        "signin_excess": 0,
        "i1_ok": i1,
        "i2_ok": i2,
        "terminal_or_pending": True,
        "timed_out": False,
        "mutation_applied": mutation_applied,
        "mutation_probe": {},
    }


class DrillMutateExitGateTest(unittest.TestCase):
    """`--mutate` 的退出码必须吃 i1/i2。"""

    def _cli(self, res, *argv):
        with mock.patch.object(drill, "run_drill", return_value=res), \
                mock.patch.object(sys, "argv", ["drill_multi_exec.py", *argv]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            rc = drill._cli()
        return rc, out.getvalue()

    def test_i1_violation_exits_nonzero(self):
        """变异 `claim-steal` 打破 I1 ⇒ 退出码非零（有牙证明的「应 FAIL」）。"""
        rc, out = self._cli(_res(i1=False), "--mutate", "claim-steal")
        self.assertEqual(rc, 1, "i1 违例必须让退出码非零；输出=%s" % out[-300:])
        self.assertIn("FAIL", out)

    def test_i2_violation_exits_nonzero(self):
        """变异 `reap-off` 打破 I2 ⇒ 退出码非零。"""
        rc, out = self._cli(_res(i2=False), "--mutate", "reap-off")
        self.assertEqual(rc, 1, "i2 违例必须让退出码非零；输出=%s" % out[-300:])
        self.assertIn("FAIL", out)

    def test_window_off_without_violation_stays_green(self):
        """`window-off` 不靠违反不变式：变异贴上且 i1/i2 绿时仍按绿收（边界）。"""
        rc, out = self._cli(_res(), "--mutate", "window-off")
        self.assertEqual(rc, 0, "window-off 不违反不变式，应保持 rc=0；输出=%s" % out[-300:])

    def test_mutation_not_applied_exits_nonzero(self):
        """变异没贴上时不能报绿：否则「跑了个空变异」会被当成有牙证明。"""
        rc, _ = self._cli(_res(mutation_applied=False), "--mutate", "claim-steal")
        self.assertEqual(rc, 1)

    def test_baseline_mode_still_checks_every_invariant(self):
        """非变异档逐项全绿才 rc=0；任一不变式红即 rc=1（旧行为不变）。"""
        rc, _ = self._cli(_res(), )
        self.assertEqual(rc, 0)
        rc, _ = self._cli(_res(i1=False))
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
