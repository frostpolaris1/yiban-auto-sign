# -*- coding: utf-8 -*-
"""按日状态件的保留期必须只有一个 owner：logrotate 与 state_gc 不得同时认领。

标签：J · 运维：部署/备份/发布

**为什么有这条守卫**（工单 yiban-auto-sign-f7va / ai7z，2026-10-12）
`sign-status-<day>.txt` 曾同时被两个清理者认领。logrotate 的段用 `copytruncate`
且 glob 匹配全部日期，次日 03:00 把当日写入的 `SUCCESS` 就地截断为 0 字节；
`notifempty` 又让这个空文件永远不再被轮转。结果任何"事后读某一天的
sign-status"必然看到 0 字节。本文件把 owner 钉成单一来源：

1. `web/deploy/logrotate-yiban.conf` 不得出现 `sign-status` 的**非注释**命中（段）。
2. `yiban/state_gc.py` 的 `ARTIFACTS` 必须仍登记 `sign-status-`（唯一清理者）。

任一被复活即红。口径与 `scripts/check-shared-facts.sh` 一致：**注释一律不算**——
守卫钉的是"不得再有这个段"（边界），不是禁止在 conf 头注释里说明它为什么归 state_gc
单管（讨论是文档，不是定义点）。二选一的理由见 PR body：本仓既有的"注释不算命中"
口径优先于"连注释一起禁"的更硬棘轮，后者会让下一个想加注释说明的人去改守卫。
依赖：只读文件文本与 import；不跑子进程、不触网。
"""
import io
import os
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGROTATE_CONF = os.path.join(BASE, "web", "deploy", "logrotate-yiban.conf")


def _strip_hash_comments(text):
    """剥掉 logrotate conf 的 `#` 注释（整行与行尾）——与 check-shared-facts.sh 同口径。

    logrotate conf 是 `#` 注释语言（`langclass` 归 "hash" 分档）。守卫据此只判
    "有没有这个段"，注释里提到 token 不算命中。
    """
    out = []
    for line in text.splitlines():
        idx = line.find("#")
        out.append(line if idx < 0 else line[:idx])
    return "\n".join(out)


class StatusFileRetentionOwnerTest(unittest.TestCase):
    """sign-status 的保留期只能由 state_gc 一人负责。"""

    @classmethod
    def setUpClass(cls):
        with io.open(LOGROTATE_CONF, encoding="utf-8") as f:
            cls.conf = f.read()

    def test_logrotate_does_not_claim_sign_status(self):
        """logrotate 不得再**轮转** sign-status：非注释命中一旦复活即回滚到 0 字节缺陷。

        口径（与 scripts/check-shared-facts.sh 一致）：注释不算命中——守卫钉边界
        （不许有 sign-status 段），不是禁止在 conf 头注释里说明"为什么归 state_gc 单管"。
        """
        self.assertNotIn(
            "sign-status", _strip_hash_comments(self.conf),
            "web/deploy/logrotate-yiban.conf 又出现 sign-status 段：它会 copytruncate "
            "把当日状态就地清零（f7va 已裁决该文件的保留期归 state_gc 单管）")

    def test_state_gc_still_registers_sign_status(self):
        """删掉 logrotate 段的前提是 state_gc 仍登记它——否则文件永远不被清理。"""
        from yiban import state_gc
        prefixes = {art.prefix for art in state_gc.ARTIFACTS}
        self.assertIn(
            "sign-status-", prefixes,
            "state_gc.ARTIFACTS 必须登记 sign-status-：logrotate 已不再轮转它，"
            "它若也不在清理名册里就永远不被清理")


if __name__ == "__main__":
    unittest.main()
