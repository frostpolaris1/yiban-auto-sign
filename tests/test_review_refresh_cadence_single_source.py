# -*- coding: utf-8 -*-
"""待审核刷新节拍单一来源守卫（工单 vff0 复查 D2；2026-10-16 按退役账「轮询收编」重锚）。

## 判据（不变式）

**"待审核"这条数在四个取数点上必须同拍**，且**只许有一个节拍来源**：
  1. `web/static/js/core.js` 的 `var POLL_MS = <n>;` 是**全站唯一的节拍定义**
     （前端模块侧经 `YB.POLL_MS` 读取，桥接在 `frontend/src/lib/shell.ts::pollMs()`）；
  2. 四个取数点（账号列表 / 用户列表 / 总览 KPI / 日志列表）**一律**经
     `frontend/src/lib/polling.ts` 的 `usePolling(...)` 起表，并传 `pollMs()`；
  3. 这些文件里**不得**再出现 `setInterval(`，也**不得**出现数字节拍字面量；
  4. 轮询机制（`setInterval(`）在这批文件里只允许住在 `lib/polling.ts` 一处。

## 为什么重锚（原锚点 → 新锚点）

原锚点（工单 vff0 立的版本）是四处**各自的实现文本**：
`Accounts.vue`/`Users.vue` 的 `setInterval(pollTick, <n>)`、`DashboardPage.vue` 的
`setInterval(onPendingTick, <n>)`、`core.js` 的 `var NAV_BADGE_TTL = <n>;`，再断言四个数相等。
它钉的是**形态（编舞）**：四处各写一份定时器，只要形状还在、数字相等就绿。该门自己的
docstring 写着"为什么不做成单一常量：`core.js` 是独立经典脚本，无法 import TS 常量；
抽常量得把 `core.js` 改成模块…故立守卫"——即它等的是"节拍收敛到单一来源"这件事本身。

退役账「手写轮询收编」把这件事做掉了：节拍收成 `core.js::POLL_MS` 一个定义，轮询机制收成
`lib/polling.ts::usePolling` 一个实现。原锚点在重构后**必然全红**（它要的那串文本已不存在），
故本批按 AGENTS.md §一.6a（测不变式不测编舞）把锚点从"四份实现文本 + 数字相等"换成
"一个来源 + 四个消费点 + 无第二实现"。

**新锚点没有削弱判据**：原判据能抓的（四处不同拍）在新结构下**结构上不可能**发生——
只有一处数字；新判据还能抓原判据抓不到的（有人在某一页重新写死节拍、或重新自带一个
定时器）。原判据抓不到的（机制实现漂移：少一处 visibilitychange 补拉、少一处卸载清理）
也一并由"机制只许一处"覆盖。

守卫自证（2026-10-16 实测，三次突变逐个跑）：
  · 把 `Accounts.vue` 的 `pollMs()` 换回字面量 `10000` ⇒ 红（点名该文件）；
  · 给 `Users.vue` 加回一个 `setInterval(` ⇒ 红（点名该文件）；
  · 删掉 `core.js` 的 `var POLL_MS = ...;` ⇒ 红（找不到唯一节拍来源）。
"""
import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CORE_JS = os.path.join("web", "static", "js", "core.js")
POLLING_IMPL = os.path.join("frontend", "src", "lib", "polling.ts")
SHELL_BRIDGE = os.path.join("frontend", "src", "lib", "shell.ts")

#: 唯一节拍定义：core.js 的 `var POLL_MS = <毫秒>;`
CADENCE_DEF_RE = re.compile(r"var\s+POLL_MS\s*=\s*(\d+)\s*;")
#: 桥接层读 `YB.POLL_MS` 的出口名（前端模块侧的唯一访问器）
CADENCE_ACCESSOR = "pollMs()"

#: 四个"待审核"取数点：一律经 usePolling 起表，并传唯一节拍 `pollMs()`。
POLLING_SITES = (
    os.path.join("frontend", "src", "accounts", "Accounts.vue"),
    os.path.join("frontend", "src", "users", "Users.vue"),
    os.path.join("frontend", "src", "dashboard", "DashboardPage.vue"),
    os.path.join("frontend", "src", "logs", "Logs.vue"),
)

#: 其余"周期性 UI 计时"点（各 1 秒档，不是 10 秒节拍）：也一律经 usePolling 起表，
#: 同样不许自带 setInterval —— 机制只许一处。它们**不**要求 `pollMs()`（档位不同）。
MECHANISM_ONLY_SITES = (
    os.path.join("frontend", "src", "settings", "QuotaCard.vue"),   # 冷却倒计时
)

#: 机制唯一性检查的全部成员：取数点 + 1 秒档计时点 + 共享实现本身。
MECHANISM_FILES = POLLING_SITES + MECHANISM_ONLY_SITES + (POLLING_IMPL,)


def _read(rel):
    with open(os.path.join(BASE, rel), encoding="utf-8") as fh:
        return fh.read()


class ReviewRefreshCadenceTest(unittest.TestCase):
    def test_cadence_has_exactly_one_definition(self):
        """节拍定义只许有一处：core.js 的 `var POLL_MS = <n>;`。"""
        core = _read(CORE_JS)
        self.assertIsNotNone(
            CADENCE_DEF_RE.search(core),
            "core.js 找不到唯一节拍定义 `var POLL_MS = <毫秒>;` —— 节拍必须有一个来源",
        )

    def test_shell_bridge_reads_the_single_cadence(self):
        """前端模块侧只许经 `pollMs()` 读节拍；该桥接必须读 `POLL_MS`。"""
        bridge = _read(SHELL_BRIDGE)
        self.assertIn("export function pollMs(", bridge,
                      "lib/shell.ts 缺 pollMs() —— 页面没有读节拍的合法入口")
        self.assertIn("POLL_MS", bridge,
                      "lib/shell.ts::pollMs() 必须读 core.js 的 POLL_MS（唯一来源）")

    def test_polling_sites_consume_the_shared_mechanism(self):
        """取数点一律经 usePolling + pollMs()，且不得自带定时器或数字节拍。"""
        problems = []
        for rel in POLLING_SITES:
            src = _read(rel)
            name = rel.replace(os.sep, "/")
            if "usePolling(" not in src:
                problems.append(f"  {name}: 未走共享轮询实现 usePolling()")
            if CADENCE_ACCESSOR not in src:
                problems.append(f"  {name}: 未取唯一节拍来源 {CADENCE_ACCESSOR}")
            if "setInterval(" in src:
                problems.append(f"  {name}: 自带 setInterval()（第二份轮询机制）")
            if re.search(r"\b10000\b", src):
                problems.append(f"  {name}: 出现数字节拍 10000（节拍只能有一个来源）")
        if problems:
            self.fail(
                "待审核刷新面出现第二份节拍/机制实现 —— 同一件事只许一个数、一种机制：\n"
                + "\n".join(problems)
            )

    def test_polling_mechanism_lives_in_one_file(self):
        """轮询机制（setInterval）在这批文件里只许住在 lib/polling.ts。"""
        owners = [rel.replace(os.sep, "/") for rel in MECHANISM_FILES
                  if "setInterval(" in _read(rel)]
        self.assertEqual(
            owners, ["frontend/src/lib/polling.ts"],
            "轮询机制必须只有一处实现（frontend/src/lib/polling.ts）：%r" % (owners,),
        )

    def test_one_second_timers_also_use_the_shared_mechanism(self):
        """1 秒档的周期 UI 计时（冷却倒计时等）同样只许经 usePolling 起表。"""
        problems = []
        for rel in MECHANISM_ONLY_SITES:
            src = _read(rel)
            name = rel.replace(os.sep, "/")
            if "usePolling(" not in src:
                problems.append(f"  {name}: 未走共享机制 usePolling()")
            if "setInterval(" in src:
                problems.append(f"  {name}: 自带 setInterval()（第二份机制）")
        if problems:
            self.fail(
                "1 秒档周期计时出现第二份机制实现 —— 机制只许一处：\n" + "\n".join(problems)
            )

    def test_polling_implementation_does_not_hardcode_the_cadence(self):
        """共享实现不许把节拍写死：节拍一律由调用方传入。"""
        impl = _read(POLLING_IMPL)
        self.assertIn("intervalMs", impl, "usePolling 必须以 intervalMs 参数收节拍")
        self.assertIsNone(re.search(r"\b10000\b", impl),
                          "lib/polling.ts 不得写死节拍数字（节拍来源只有一个）")


if __name__ == "__main__":
    unittest.main()
