# -*- coding: utf-8 -*-
"""Vue 源码入库守卫：`frontend/src/**` 不得有被 .gitignore 忽略的文件。

**来历（2026-10-03 实测踩过）**：仓库既有的 `logs/`（日志目录）规则把同名的源码目录
`frontend/src/logs/` 一并吞掉，而 `git add -A` 会**静默跳过**被忽略的路径——于是分支上
只有构建产物（`web/static/vue/assets/logs-*.js`）、没有源码：新检出无法 `npm run build`
（缺 `src/logs/main.ts`），`tests/test_logs_by_date.py` 引用的 `date-guard.js` 也不存在。
发现时该缺陷已在库里躺了两笔提交，Python 契约测试与 Vitest 全绿——因为两者都只读工作树，
**不关心文件是否入库**。

标签：F · 前端与界面守卫
覆盖：忽略面（`git status --ignored` 扫 frontend/src）+ 入口可达性（vite 入口 HTML 及其
      引用的 src 入口文件都必须在库里）
依赖：需要 git 可执行文件；无 git 时整体跳过（不假绿）
"""
import os
import re
import shutil
import subprocess
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GIT = shutil.which("git")
VITE_CONFIG = os.path.join(BASE, "frontend", "vite.config.ts")


def _git(*args):
    return subprocess.run([GIT, "-C", BASE, *args], capture_output=True, text=True)


@unittest.skipUnless(GIT, "git 不可用：跳过 Vue 源码入库守卫")
class VueSourcesTrackedTest(unittest.TestCase):
    def test_no_ignored_files_under_frontend_src(self):
        """`frontend/src` 下不允许出现被忽略的文件——那意味着源码不会随提交入库。"""
        out = _git("status", "--porcelain", "--ignored", "frontend/src").stdout
        ignored = sorted(line[3:] for line in out.splitlines() if line.startswith("!!"))
        self.assertEqual(
            ignored,
            [],
            "frontend/src 下有被 .gitignore 忽略的文件（源码会被静默排除在提交之外）："
            f"{ignored}——若是同名目录规则误伤，在 .gitignore 里补 `!<path>/` 负向规则",
        )

    def test_vite_entry_sources_are_tracked(self):
        """每个 Vite 入口 HTML 及其引用的 `/src/...` 入口文件都必须已被 git 跟踪。

        新检出要能 `npm run build`：入口 HTML 在库里、入口 TS 不在库里 = 构建直接失败。
        """
        with open(VITE_CONFIG, encoding="utf-8") as fh:
            cfg = fh.read()
        html_names = re.findall(r'new URL\("\./([A-Za-z0-9_.-]+\.html)"', cfg)
        self.assertTrue(html_names, "vite.config.ts 里没解析出任何 HTML 入口（改过写法？）")

        missing = []
        for name in html_names:
            html_rel = f"frontend/{name}"
            if _git("ls-files", "--error-unmatch", html_rel).returncode != 0:
                missing.append(html_rel)
                continue
            with open(os.path.join(BASE, "frontend", name), encoding="utf-8") as fh:
                entry_srcs = re.findall(r'src="(/src/[^"]+)"', fh.read())
            for src in entry_srcs:
                src_rel = "frontend" + src
                if _git("ls-files", "--error-unmatch", src_rel).returncode != 0:
                    missing.append(src_rel)

        self.assertEqual(missing, [], f"以下入口资源未入库（新检出无法构建）：{missing}")


if __name__ == "__main__":
    unittest.main()
