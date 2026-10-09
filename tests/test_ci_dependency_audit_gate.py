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
       再加回来；`nightly.yml` / `e2e-nightly.yml` 的排程**唯一定义点在默认分支
       main**（`schedule` 只在默认分支生效，M24），本分支（develop）不得再持有
       这两个文件，且 `ci.yml` 注释块必须写明该约定与 checkout 的 `ref: develop`。

对应实现：`.github/workflows/ci.yml`、`docker/Dockerfile`，以及默认分支上的
`nightly.yml` / `e2e-nightly.yml`（不在本分支内）。
关键断言：纯静态（读文本 + 轻量解析，**不调 GitHub API、不跑 yaml 解析器**——
PyYAML 不在运行依赖里，门禁不得为一条注释引入一个新依赖）；活体反例：对同一
检查器喂一份"可变 tag / 无 pip-audit / 带私钥"的合成内容必须判红。
依赖：纯文件读取，无网络、无 skip。
"""
import io
import os
import re
import tempfile
import unittest
from unittest import mock

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


#: M24 修后（2026-10-05）：这两条排程的**唯一定义点在默认分支 main**。`schedule` 只在
#: 默认分支生效，故本仓检出（develop）不得再持有它们——一旦回潮，同一个事实就有两个
#: 定义点，两者必然漂移（本仓 B0.5 门禁的既有纪律）。
NIGHTLY_SCHEDULED = ("nightly.yml", "e2e-nightly.yml")

#: `ci.yml` 注释块必须逐一点名的约定要素。带反引号是**必须的**：`nightly.yml` 是
#: `e2e-nightly.yml` 的裸子串，不加锚点时前者会被后者白送通过——只提 e2e 那条、
#: 删掉 nightly 那条也没人红，守卫就漏了一半。
_CI_CONVENTION_TOKENS = ("`nightly.yml`", "`e2e-nightly.yml`", "`ref: develop`", "默认分支")


def _on_default_branch():
    """本次检出是否属于**默认分支线**。本地（无 GITHUB_* 环境变量）返回 False。

    为什么要它：默认分支 main 上这两个排程文件**本就该在**（那是唯一定义点），"不得持有"
    这条只对非默认分支的检出成立。两情形算"属于"：
      ① CI 直接检出默认分支（push 到 main：`GITHUB_REF_NAME=main`）；
      ② PR 以默认分支为基（`GITHUB_BASE_REF=main`）——此时检出是 base(main)+head 的合并
         树，main 侧本就有的定义点在树里，按 develop 口径判会假红。
    本仓门禁以 develop 检出为前提（同 RetiredWorkflowGuardTest）：本地与 develop 的 CI
    都必须真判"不得持有"。
    """
    return os.environ.get("GITHUB_REF_NAME") == "main" or os.environ.get("GITHUB_BASE_REF") == "main"


def _nightly_boundary_violations(workflow_dir, ci_text, on_default_branch=False):
    """返回违反「排程唯一定义点在默认分支」边界的项；空列表 = 未越界。

    两判：① 非默认分支的检出不得出现这两份 workflow（默认分支上它们本就该在）；
    ② `ci.yml` **注释块**须写明该约定（逐一点名两个文件、默认分支、以及排程 checkout
    的 `ref: develop`）。只认注释行：约定要写在人读得到的地方，YAML 正文里的同名串不算。
    """
    bad = []
    if not on_default_branch:
        for name in NIGHTLY_SCHEDULED:
            if os.path.isfile(os.path.join(workflow_dir, name)):
                bad.append("本分支出现了排程定义点：" + name)
    comments = "\n".join(ln for ln in ci_text.splitlines() if ln.lstrip().startswith("#"))
    for token in _CI_CONVENTION_TOKENS:
        if token not in comments:
            bad.append("ci.yml 注释块缺少约定要素：" + token)
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
    """③ M42：已退役的自动触发 workflow 不得回潮。"""

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


class ScheduledWorkflowDefinitionPointTest(unittest.TestCase):
    """③ M24：`schedule` 只在默认分支生效 ⇒ 两条排程的唯一定义点在 main。

    修法（2026-10-05）：`nightly.yml` / `e2e-nightly.yml` 落在默认分支 main，两文件的
    checkout 显式 `ref: develop`（排程跑活跃线）；develop 侧删除副本，避免同一事实两个
    定义点漂移。本类钉住该边界：非默认分支的检出出现副本即红，`ci.yml` 丢掉这条约定也红。
    """

    def test_this_branch_holds_no_scheduled_workflow(self):
        ci = _read(os.path.join(WORKFLOWS, "ci.yml"))
        self.assertEqual(
            _nightly_boundary_violations(WORKFLOWS, ci, _on_default_branch()), [],
            "M24 修后 nightly.yml / e2e-nightly.yml 只住在默认分支 main（schedule 只在"
            "默认分支生效）；非默认分支的检出出现副本 = 同一事实两个定义点，必然漂移。"
            "且任何检出的 ci.yml 注释块都必须写明该约定与排程 checkout 的 ref: develop，"
            "否则后人会以为排程没跑")

    def test_the_default_branch_signal_comes_from_the_ci_environment(self):
        """默认分支线判定读平台自己的信号（push 到 main / PR 以 main 为基）。"""
        with mock.patch.dict(os.environ, {"GITHUB_REF_NAME": "main"}, clear=True):
            self.assertTrue(_on_default_branch(), "push 到 main 必须认成默认分支线，否则假红")
        with mock.patch.dict(os.environ, {"GITHUB_BASE_REF": "main"}, clear=True):
            self.assertTrue(_on_default_branch(), "PR 以 main 为基时检出含 main 的定义点")
        with mock.patch.dict(os.environ, {"GITHUB_REF_NAME": "develop",
                                          "GITHUB_BASE_REF": "develop"}, clear=True):
            self.assertFalse(_on_default_branch(), "develop 上不得认成默认分支线，否则漏判")

    def test_the_boundary_checker_catches_a_workflow_that_came_back(self):
        """活体反例：守卫不承重就是废的——放回一份 nightly.yml 必须判红。"""
        good_ci = "# `nightly.yml` `e2e-nightly.yml` 默认分支 `ref: develop`"
        with tempfile.TemporaryDirectory() as tmp:
            with io.open(os.path.join(tmp, "nightly.yml"), "w", encoding="utf-8") as f:
                f.write("on:\n  schedule:\n    - cron: '0 6 * * *'\n")
            bad = _nightly_boundary_violations(tmp, good_ci)
            # 默认分支上这两个文件本就该在——同一判定不得在那里判红（否则 main 假红）
            on_default = _nightly_boundary_violations(tmp, good_ci, on_default_branch=True)
        self.assertEqual(bad, ["本分支出现了排程定义点：nightly.yml"],
                         "检查器必须抓住回潮到非默认分支的排程文件")
        self.assertEqual(on_default, [], "默认分支上排程文件本就该在，不得判红")

    def test_the_boundary_checker_catches_a_ci_comment_without_the_convention(self):
        """活体反例：`ci.yml` 里的约定被删掉也必须判红（否则这条约定无人守）。"""
        missing_all = [
            "ci.yml 注释块缺少约定要素：`nightly.yml`",
            "ci.yml 注释块缺少约定要素：`e2e-nightly.yml`",
            "ci.yml 注释块缺少约定要素：`ref: develop`",
            "ci.yml 注释块缺少约定要素：默认分支",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(_nightly_boundary_violations(tmp, "# 只有 fast 轨，没别的话"),
                             missing_all, "检查器必须抓住 ci.yml 里丢失的定义点约定")
            # 约定要写在注释里：YAML 正文里的同名串不算（判据是注释行）
            self.assertEqual(_nightly_boundary_violations(
                tmp, "name: `nightly.yml` `e2e-nightly.yml` `ref: develop` 默认分支"),
                missing_all, "非注释行里的名字不算'写进注释块'，不得白送通过")

    def test_the_boundary_checker_is_not_fooled_by_the_substring_pair(self):
        """活体反例：`nightly.yml` 是 `e2e-nightly.yml` 的裸子串——只提后者不得白送前者过。"""
        with tempfile.TemporaryDirectory() as tmp:
            bad = _nightly_boundary_violations(
                tmp, "# `e2e-nightly.yml` 在默认分支，checkout 用 `ref: develop`")
        self.assertEqual(bad, ["ci.yml 注释块缺少约定要素：`nightly.yml`"],
                         "锚点丢了就会被裸子串白送通过，nightly.yml 的提及被删也不红")


if __name__ == "__main__":
    unittest.main(verbosity=2)
