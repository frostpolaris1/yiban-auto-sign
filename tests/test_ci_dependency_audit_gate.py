# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""CI/工作流面的静态契约：依赖审计门在、action 全 SHA 固定、危险 workflow 不回潮。

标签：J · 运维：部署/备份/发布
覆盖：`.github/workflows/` 下三条不变量——
    ① **依赖审计门存在且审的是 requirements.lock**（M28 批次补缺）：本批手工抬
       urllib3/Werkzeug 关掉 4 个 CVE 之后，`docker/Dockerfile` 注释承诺的
       "CI pip-audit 审计的就是它"曾经是一句空话（工作流里根本没有 pip-audit）。
       `requirements.lock` 是手工维护的精确锁定、全程不含 `--hash=`，没有哈希锁
       可复核 ⇒ **pip-audit 是这里唯一能挡住依赖漂移的机制**，缺了它这条注释又
       变回空话。
    ② **所有 `uses:` 都是完整 40 位 commit SHA**（既有纪律，新 job 不得破例：
       可变 tag 被篡改即供应链入口）。
    ③ **M42/M24 的可自动触发 workflow 不许回潮**：`signin.yml`（真签到，跑在
       CI runner 上）与带 Gitee 私钥的 `mirror.yml` 在 develop 上已删除，不得被
       再加回来；`nightly.yml` 的排程只在默认分支生效这件事必须在文件头写明
       （main 分支治理属所有者动作，代码侧只能留证据）。

对应实现：`.github/workflows/ci.yml`、`nightly.yml`、`docker/Dockerfile`。
关键断言：纯静态（读文本 + 轻量解析，**不调 GitHub API、不跑 yaml 解析器**——
PyYAML 不在运行依赖里，门禁不得为一条注释引入一个新依赖）；活体反例：对同一
检查器喂一份"可变 tag / 无 pip-audit / 带私钥"的合成内容必须判红。
依赖：纯文件读取，无网络、无 skip。
"""
import io
import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS = os.path.join(BASE, ".github", "workflows")
DOCKERFILE = os.path.join(BASE, "docker", "Dockerfile")

#: `uses: owner/repo@ref`；ref 允许 40 位十六进制（SHA）或任意可变量（tag/分支）。
_USES = re.compile(r"^\s*-?\s*uses:\s*([^\s#]+)\s*(#.*)?$", re.M)
#: 40 位小写十六进制
_SHA = re.compile(r"^[0-9a-f]{40}$")


def _read(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def _workflow_files():
    return sorted(n for n in os.listdir(WORKFLOWS) if n.endswith((".yml", ".yaml")))


def _unpinned_refs(text):
    """返回该文本里所有**不是 40 位 SHA** 的 `uses:` 引用。"""
    bad = []
    for match in _USES.finditer(text):
        ref = match.group(1).split("@")[-1]
        if not _SHA.match(ref):
            bad.append(match.group(1))
    return bad


class DependencyAuditGateTest(unittest.TestCase):
    """① 依赖漏洞审计门：存在、审 lock、门禁工具钉版。"""

    def setUp(self):
        self.ci = _read(os.path.join(WORKFLOWS, "ci.yml"))

    def test_ci_has_a_pip_audit_step_on_the_locked_requirements(self):
        self.assertIn("pip-audit", self.ci,
                      "ci.yml 里没有 pip-audit——Dockerfile 那句「CI pip-audit 审计的"
                      "就是它」就是空话，依赖漂移无人拦截")
        # 审的是**锁定清单**（镜像实际安装 / 现网实际跑的版本），不是下限清单
        self.assertRegex(self.ci, r"pip-audit\s+-r\s+requirements\.lock",
                         "pip-audit 必须审 requirements.lock（锁定版本才是实际安装的那个）")
        # 审的是传递依赖（默认行为）：显式不出现 --no-deps / --no-deps-pinned
        for forbidden in ("--no-deps", "--no-deps-pinned"):
            self.assertNotIn(forbidden, self.ci,
                             f"pip-audit {forbidden} 会跳过传递依赖，等于关掉这道门")

    def test_pip_audit_tool_version_is_pinned(self):
        """门禁工具必须钉版：否则"昨天绿今天红"分不清是漏洞库更新还是工具行为变化。"""
        self.assertRegex(self.ci, r"pip install\s+pip-audit==[0-9]",
                         "pip-audit 必须钉版本（与 ruff/pytest 的钉版纪律一致）")

    def test_dockerfile_comment_points_at_the_real_gate(self):
        """Dockerfile 的注释要指到**真实存在**的 job 名，别再写一句悬空的话。"""
        text = _read(DOCKERFILE)
        m = re.search(r"dependency-audit", text)
        self.assertIsNotNone(m, "Dockerfile 注释应点名 ci.yml 里的 dependency-audit job")
        self.assertIn("dependency-audit", self.ci,
                      "Dockerfile 点名的 job 在 ci.yml 里不存在")


class WorkflowShaPinTest(unittest.TestCase):
    """② 所有 `uses:` 固定到完整 commit SHA（既有纪律，不得被新 job 破例）。"""

    def test_every_workflow_pins_uses_to_a_full_sha(self):
        bad = {}
        for name in _workflow_files():
            offenders = _unpinned_refs(_read(os.path.join(WORKFLOWS, name)))
            if offenders:
                bad[name] = offenders
        self.assertEqual(bad, {},
                         "workflow 的 uses 必须固定 40 位 commit SHA（可变 tag 即供应链入口）："
                         f"{bad}")

    def test_the_pin_checker_catches_a_mutable_tag(self):
        """活体反例：同一检查器对合成内容必须判红——否则这条断言可能整个是废的。"""
        offender = _unpinned_refs("jobs:\n  x:\n    steps:\n      - uses: acme/act@v1\n")
        self.assertEqual(offender, ["acme/act@v1"],
                         "检查器必须能抓住可变 tag")

    def test_the_pin_checker_accepts_a_sha_pinned_ref(self):
        sha = "3d3c42e5aac5ba805825da76410c181273ba90b1"
        self.assertEqual(_unpinned_refs(f"      - uses: actions/checkout@{sha} # v7\n"), [])


class RetiredWorkflowGuardTest(unittest.TestCase):
    """③ M42/M24：已退役的自动触发 workflow 不得回潮，排程限制必须写在文件头。"""

    #: develop 上已删除（commit 7c7ccf7），回潮即重新引入两个审查发现
    RETIRED = ("signin.yml", "mirror.yml")

    def test_retired_signin_and_mirror_workflows_are_absent(self):
        present = [n for n in self.RETIRED if os.path.isfile(os.path.join(WORKFLOWS, n))]
        self.assertEqual(present, [],
                         f"这些 workflow 已退役（见 commit 7c7ccf7）：{present}")
        # main 分支上仍有它们（默认分支治理未完成）——本仓检出基于 develop，
        # 这里钉的是"别在 develop 上加回来"，不是 main 的现状。

    def test_no_workflow_references_a_gitee_private_key(self):
        """`mirror.yml` 的危害面是带 Gitee 私钥；任何 workflow 都不得再引这个 secret。"""
        offenders = []
        for name in _workflow_files():
            text = _read(os.path.join(WORKFLOWS, name))
            if "GITEE_PRIVATE_KEY" in text or "gitee/frostpolaris" in text:
                offenders.append(name)
        self.assertEqual(offenders, [],
                         "不得有 workflow 引用 Gitee 私钥/推送到 Gitee：%s" % offenders)

    def test_nightly_documents_that_schedule_only_fires_on_default_branch(self):
        """M24：`schedule` 是 workflow 级触发且**只在默认分支生效**。

        默认分支是 main，而 nightly.yml 只在 develop 上 ⇒ 这条排程永不触发。
        修法是 main 分支治理（所有者动作），代码侧唯一能做的就是**把这件事写在
        文件头**——否则下一个来改 cron 的人会以为夜间门在跑。
        """
        text = _read(os.path.join(WORKFLOWS, "nightly.yml"))
        self.assertIn("schedule", text)
        self.assertRegex(text, r"默认分支",
                         "nightly.yml 头部必须写明 schedule 只在默认分支触发（M24）")
        self.assertIn("M24", text, "要指名发现编号，便于回查处置清单")


if __name__ == "__main__":
    unittest.main(verbosity=2)
