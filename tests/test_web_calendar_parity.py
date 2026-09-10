# -*- coding: utf-8 -*-
"""回归守卫（2026-09-10）：签到日历在 admin 与 user 两份实现之间不得再漂移。

背景（V3-4 实测）：
「签到日历」有两份几乎独立的实现 —— admin 在管理端脚本里（A4-2 前是 `web/static/js/app.js`，现按内容查找到 `web/static/js/pages/mine.js`），
user 在 `web/templates/user.html` 的内联脚本里（**user 端不引管理端脚本**，无共享 JS 文件）。
两份是各自维护的，于是长期漂移，V3-4 前实测到的差异包括：

    · 日期格：user 端**没有**「休」角标、aria-label 只报「今天/已签到」，
      admin 端则只报「周末不签到/查看记录」——**两边各缺一半信息**；
    · 标题：admin `font-semibold` vs user `font-medium`（同类 14px 小节标题）；
    · 月份切换按钮：user 端有 `aria-label="上个月/下个月"`，admin 端**完全没有**；
    · 星期表头与「日历加载失败」文案：user 端曾用写反的色对（V3-3 修）。

V3-4 的处置：把日期格的样式与 a11y 输出抽成**两份逐字相同的 `calDayCell(o)`**
（连同解释性注释一起钉住），并统一标题字重与月份按钮的读屏名称。

本测试钉住：
1. `calDayCell` 共享块在两个文件里**逐字相同**（含其上方注释）—— 这是"再也不能各改一边"的
   硬约束。失败信息会指出首个不同字符的位置与上下文，便于直接对齐。
2. 两份实现都包含若干**必须同时存在**的片段（标题层级、月份按钮读屏名称、
   星期表头、失败提示），防止某一边被单独改动后无声漂移。
3. 两份都不再残留旧的「内联拼日期格」写法（`border-transparent'} flex …`），
   证明调用点已真正收敛到 `calDayCell`。

注意：本测试**只钉"两份一致"这件事**，不规定视觉长什么样 —— 视觉规范在
`calDayCell` 里（含"为什么不用 daisyUI 的实心主色底表示今天"的说明）。
"""

import os
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_DIR = os.path.join(BASE, "web", "static", "js")
USER = os.path.join(BASE, "web", "templates", "user.html")


def _find_admin_calendar_src():
    """按**内容**找 admin 侧那份 `calDayCell` 所在文件。

    A4-2 之后管理端脚本已按连续区间拆成 7 个模块（`web/static/js/` 下），
    `app.js` 不再存在。写死文件名会让本测试随拆分而碎，故改为按内容定位。
    """
    for dirpath, _dirnames, filenames in os.walk(JS_DIR):
        for name in sorted(filenames):
            if not name.endswith(".js"):
                continue
            path = os.path.join(dirpath, name)
            with open(path, encoding="utf-8") as fh:
                if "function calDayCell(o)" in fh.read():
                    return path
    raise AssertionError(
        f"在 {os.path.relpath(JS_DIR, BASE)} 下找不到含 `function calDayCell(o)` 的文件 ——"
        "admin 侧的签到日历实现去哪了？"
    )


ADMIN = _find_admin_calendar_src()

# 共享块的起止标记
BLOCK_START = "// ==== 签到日历 · 日期格"
BLOCK_END = "\n}\n"

# 两份实现必须同时包含的片段（改其一而漏另一者，本测试即红）
REQUIRED_IN_BOTH = (
    # 标题：14px 小节层级（与 V2 确立的两级标题体系一致：主标题 16px 粗、小节 14px 中）
    'text-sm font-medium text-zinc-700 dark:text-zinc-200">签到日历',
    # 月份切换按钮的读屏名称
    'aria-label="上个月"',
    'aria-label="下个月"',
    # 星期表头（色对由 test_web_text_contrast 守卫）
    "text-center text-xs text-zinc-500 dark:text-zinc-400 py-1",
    # 日历加载失败提示（同一句文案、同一套色）
    'py-4">日历加载失败，请稍后重试</div>',
    # 共享块本体
    "function calDayCell(o)",
)

# 旧的内联拼装写法（应已消失）
OLD_INLINE_MARKUP = "border-transparent'} flex items-center justify-center text-xs"


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _extract_shared_block(src, path):
    """取出 calDayCell 共享块（含其上方注释），到函数体第一个列 0 的 `}` 为止。"""
    try:
        start = src.index(BLOCK_START)
    except ValueError:
        raise AssertionError(
            f"{os.path.relpath(path, BASE)} 里找不到共享块标记 {BLOCK_START!r}"
            "（被改名或删除？该块是两个文件共享的事实源，不能单独改）"
        ) from None
    end = src.find(BLOCK_END, start)
    if end < 0:
        raise AssertionError(f"{os.path.relpath(path, BASE)} 里共享块没有正常闭合")
    return src[start : end + len(BLOCK_END)]


class CalendarParityTest(unittest.TestCase):
    def test_shared_day_cell_block_is_byte_identical(self):
        """共享块必须逐字相同（这是"改一边必须同步改另一边"的硬约束）。"""
        admin_block = _extract_shared_block(_read(ADMIN), ADMIN)
        user_block = _extract_shared_block(_read(USER), USER)
        if admin_block != user_block:
            detail = ""
            if len(admin_block) != len(user_block):
                detail = f"\n长度不同：admin={len(admin_block)} user={len(user_block)}"
            for i, (a, b) in enumerate(zip(admin_block, user_block, strict=False)):
                if a != b:
                    detail = (
                        f"\n首个差异 @{i}："
                        f"\n  admin: …{admin_block[max(0, i - 60):i + 60]!r}"
                        f"\n  user : …{user_block[max(0, i - 60):i + 60]!r}"
                    )
                    break
            self.fail(
                "签到日历的 calDayCell 共享块在 admin 侧脚本与 user(user.html) 之间不一致 ——"
                f"请把两边改成逐字相同{detail}"
            )

    def test_required_snippets_present_in_both_implementations(self):
        """两份实现都必须包含这些片段（防止只在一边改了）。"""
        admin, user = _read(ADMIN), _read(USER)
        missing = []
        for snippet in REQUIRED_IN_BOTH:
            if snippet not in admin:
                missing.append(f"  admin 侧脚本 缺: {snippet!r}")
            if snippet not in user:
                missing.append(f"  user(user.html) 缺: {snippet!r}")
        if missing:
            self.fail("签到日历两份实现不再同步：\n" + "\n".join(missing))

    def test_old_inline_cell_markup_is_gone(self):
        """调用点应已收敛到 calDayCell，不再有内联拼日期格。"""
        for path in (ADMIN, USER):
            src = _read(path)
            self.assertNotIn(
                OLD_INLINE_MARKUP,
                src,
                f"{os.path.relpath(path, BASE)} 仍残留内联拼日期格的旧写法，"
                "应改为调用 calDayCell(o)",
            )


if __name__ == "__main__":
    unittest.main()
