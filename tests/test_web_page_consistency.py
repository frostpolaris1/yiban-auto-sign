# -*- coding: utf-8 -*-
"""页面级一致性守卫（2026-09-10，V3-6）：整页唯一元素每页只应出现 **一次**。

## 起因（逐页截图时发现的真实缺陷）

V3-6 按计划的 V3 逐页（accounts / logs / settings / users / mine + login / user）出图核对，
在**每一张 admin 页图**上都看到同一个问题：**版本号与开源入口出现了两次** ——

  · 侧边栏底部一份（`index.html` 的 aside 内，桌面宽度下常驻可见）；
  · 页面底部 `<footer>` 一份（`md:ml-64` 对齐正文区，移动端也只有它可见）。

user / login 两页只有一份，所以这是 index 独有的漂移。已删除侧栏那份，
**版本/开源入口统一留在页脚**（它同时是移动端唯一可见的那份，且与 ICP/备案信息同处）。

## 为什么这类缺陷需要守卫

它是「只在某个断点下才暴露的重复」：`hidden`/`md:` 之类让同一块在 A 断点显示、
在 B 断点隐藏；一旦有人改错断点或新增一份，**页面上就多出一个入口，但任何静态检查都不会报**。
金标准只比结构指纹、不判"语义上重复"，故本文件单独钉住。

## 判据

对三个整页模板，逐个断言这些「整页唯一」的标记**恰好出现 1 次**（不是 0、也不是 ≥2）。
标记必须同时覆盖"版本号"与"开源入口"，因为两者是并排出现的两块。

注意：`tabs/*.html` 会被 include 进 index，但版本/开源入口只应出现在**页面级**模板里，
故这里只扫 `index.html` / `user.html` / `login.html`，并**断言其它模板一处都没有** ——
免得有人把入口挪进某个 tab，导致它随 tab 显隐而消失。
"""

import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(BASE, "web", "templates")

PAGE_TEMPLATES = ("index.html", "user.html", "login.html")

# 整页唯一标记 → 说明
UNIQUE_MARKERS = {
    "开源（AGPL": "开源/源码入口",
    "易班自动签到 v{{": "版本号（点击看更新日志）",
}


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class PageConsistencyTest(unittest.TestCase):
    def test_version_and_source_appear_exactly_once_per_page(self):
        """每个整页模板里，版本号与开源入口都必须**恰好 1 处**。"""
        problems = []
        for name in PAGE_TEMPLATES:
            text = _read(os.path.join(TEMPLATES, name))
            for marker, label in UNIQUE_MARKERS.items():
                n = len(re.findall(re.escape(marker), text))
                if n != 1:
                    problems.append(
                        f"  {name}: {label}（{marker!r}）出现 {n} 次，应为 1 次"
                        + ("（重复：同页会出现两个入口）" if n > 1 else "（缺失：页脚入口不见了？）")
                    )
        if problems:
            self.fail(
                "整页唯一元素的数量不对 —— 这类重复只在特定断点暴露，"
                "静态检查不报，靠本测试钉住：\n" + "\n".join(problems)
            )

    def test_no_page_level_entry_moved_into_a_tab(self):
        """版本/开源入口不得挪进 tab 片段（会随 tab 显隐而消失）。"""
        offenders = []
        for dirpath, _dirnames, filenames in os.walk(TEMPLATES):
            for filename in filenames:
                if not filename.endswith(".html") or filename in PAGE_TEMPLATES:
                    continue
                path = os.path.join(dirpath, filename)
                text = _read(path)
                for marker in UNIQUE_MARKERS:
                    if marker in text:
                        offenders.append(f"  {os.path.relpath(path, TEMPLATES)}: 含 {marker!r}")
        if offenders:
            self.fail(
                "版本/开源入口只应出现在页面级模板（index/user/login）里，"
                "不该出现在 tab 或 partial 片段中：\n" + "\n".join(offenders)
            )


if __name__ == "__main__":
    unittest.main()
