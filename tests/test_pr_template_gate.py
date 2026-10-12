# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""PR 模板守卫：`.github/pull_request_template.md` 必须在库、未被忽略、且点名三项自检。

标签：J · 运维：部署/备份/发布

**来历**：`develop` 分支保护要求所有变更经 PR（out/n4c 工单第一刀，
`allow_force_pushes=false` / `allow_deletions=false` / 必需检查 `verify` + `frontend`
/ 必需审批数 0）。"合并必须经 PR"这条纪律的入口就是 PR 模板：它承载"e2e 先行 /
门禁 / changelog"三项自检。模板只有**入库**才生效，而它此前被 `.gitignore` 第 160 行
显式忽略（"协作备用文件未启用"）——本地写一份、`git add` 静默跳过，PR 上永远没有清单。

覆盖三道易失守（本守卫即"下一个改这里的人越界时变红"的那道门）：
    ① 模板文件被删或改名 ⇒ 退回"没有自检清单的 PR"；
    ② 模板重新被 `.gitignore` 命中 ⇒ 入库失败，或退化成"已跟踪却仍被忽略"的怪状态
       （`dev-verify.sh` 文件头点名过这类形状）；
    ③ 清单里被点名的三项（e2e 先行 / 门禁 / changelog）被删 ⇒ 模板在但等于没写。

对应实现：`.github/pull_request_template.md`、`.gitignore`。
关键断言：读文件 + 真 `git check-ignore` / `git ls-files` 子进程。不调 GitHub API、
    不依赖网络（分支保护的实际键值只能在所有者侧核，见 work/n4c-protection/）。
依赖：需要 git 可执行文件；无 git 时整体跳过（不假绿）。
"""
import os
import shutil
import subprocess
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GIT = shutil.which("git")
TEMPLATE_REL = ".github/pull_request_template.md"
TEMPLATE = os.path.join(BASE, ".github", "pull_request_template.md")
#: 工单点名的三项自检，各自在模板里必须出现的锚点
REQUIRED_ITEMS = {
    "e2e 先行": "e2e",
    "门禁（仓内跑测入口）": "scripts/dev-verify.sh",
    "changelog": "CHANGELOG.md",
}


def _git(*args):
    return subprocess.run([GIT, "-C", BASE, *args], capture_output=True, text=True)


@unittest.skipUnless(GIT, "git 不可用：跳过 PR 模板守卫")
class PullRequestTemplateTest(unittest.TestCase):
    def test_template_exists_and_is_not_a_stub(self):
        self.assertTrue(os.path.isfile(TEMPLATE),
                        f"{TEMPLATE_REL} 缺失——PR 上不再有自检清单")
        with open(TEMPLATE, encoding="utf-8") as fh:
            text = fh.read()
        self.assertGreater(len(text), 400,
                           f"{TEMPLATE_REL} 太短，等于空模板（没有清单就没有约束力）")

    def test_template_is_not_gitignored(self):
        """模板不许被 `.gitignore` 命中：忽略规则会让它静默留在本地。"""
        r = _git("check-ignore", "-q", TEMPLATE_REL)
        self.assertEqual(r.returncode, 1,
                         f"{TEMPLATE_REL} 被 .gitignore 命中（check-ignore 退出码 "
                         f"{r.returncode}）——`git add` 会静默跳过它，PR 上永远没有模板。"
                         f"核对 {r.stdout.strip() or '.gitignore'}")

    def test_template_is_tracked_by_git(self):
        """模板必须在库里：只有工作树有它 = 新检出与 GitHub 都拿不到。"""
        r = _git("ls-files", "--error-unmatch", TEMPLATE_REL)
        self.assertEqual(r.returncode, 0,
                         f"{TEMPLATE_REL} 未入库（git ls-files 退出码 {r.returncode}）："
                         f"{r.stderr.strip()}")

    def test_template_names_the_three_required_items(self):
        """三项自检必须逐项在模板里出现——删一项即红。"""
        with open(TEMPLATE, encoding="utf-8") as fh:
            text = fh.read()
        missing = [name for name, anchor in REQUIRED_ITEMS.items() if anchor not in text]
        self.assertEqual(missing, [], f"模板缺以下自检项：{missing}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
