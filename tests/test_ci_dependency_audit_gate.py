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
检查器喂一份"可变 tag / 无 pip-audit / 带私钥"的合成内容必须判红。e2e 接线另有
元测试：spec 递归发现要用合成树钉住；把真实 `ci.yml` 的 Playwright 调用收窄
（位置参数 / `--grep` / `--project`）必须判红（dix C-2）。
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
CI = os.path.join(WORKFLOWS, "ci.yml")
E2E_DIR = os.path.join(BASE, "frontend", "e2e")
PLAYWRIGHT_CONFIG = os.path.join(BASE, "frontend", "playwright.config.ts")

#: `uses: owner/repo@ref`；ref 允许 40 位十六进制（SHA）或任意可变量（tag/分支）。
_USES = re.compile(r"^\s*-?\s*uses:\s*([^\s#]+)\s*(#.*)?$", re.M)
#: 40 位小写十六进制
_SHA = re.compile(r"^[0-9a-f]{40}$")


def _read(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def _workflow_files():
    return sorted(n for n in os.listdir(WORKFLOWS) if n.endswith((".yml", ".yaml")))


def _e2e_specs():
    """`frontend/e2e/` 下的全部 Playwright spec（递归；扫空由调用方判红）。"""
    if not os.path.isdir(E2E_DIR):
        return []
    specs = []
    for root, _dirs, files in os.walk(E2E_DIR):
        for name in files:
            if name.endswith(".spec.ts"):
                rel = os.path.relpath(os.path.join(root, name), E2E_DIR)
                specs.append(rel.replace(os.sep, "/"))
    return sorted(specs)


def _playwright_scope_problems(cfg):
    """配置里出现 testIgnore / testMatch 一律判红（R3-008 审查 P2）。

    覆盖门要证明"每个 spec 都会跑"。ignore/match 可以单行也可以多行数组，
    正则逐项解析容易漏第二项起；这里直接拒绝出现，确需排除先在本门登记豁免。
    """
    return [key for key in ("testIgnore", "testMatch")
            if re.search(r"\b%s\s*:" % key, cfg)]


#: 允许 Playwright 调用点携带的参数（元组）→ 理由。空表 = 一个都不允许。
#: 任何参数都可能缩小 spec 面；确需加参数时在此登记并在 PR 写明它为什么不缩小覆盖。
E2E_INVOCATION_ARGUMENT_EXEMPTIONS: dict[tuple[str, ...], str] = {}


def _job_steps(block):
    """把作业正文切成 step 文本块。

    step 边界＝该作业里缩进最浅的 `- ` 行（更深的 `- ` 是 with: 里的嵌套列表）。
    """
    lines = block.splitlines()
    indents = [len(m.group(1)) for ln in lines
               if (m := re.match(r"^( *)- ", ln))]
    if not indents:
        return []
    step_indent = min(indents)
    steps = []
    cur = []
    for ln in lines:
        m = re.match(r"^( *)- ", ln)
        if m is not None and len(m.group(1)) == step_indent:
            if cur:
                steps.append("\n".join(cur))
            cur = [ln]
        elif cur:
            cur.append(ln)
    if cur:
        steps.append("\n".join(cur))
    return steps


def _step_command(step):
    """取 step 里第一条 `run:` 的命令正文，并剥掉 YAML 行内注释。

    `run: true # npm run test:e2e` 的真实命令是 `true`，注释里的字样不算执行点。
    """
    for ln in step.splitlines():
        m = re.match(r"^\s*run:\s*(.*)$", ln)
        if m is not None:
            return re.split(r"\s+#", m.group(1), maxsplit=1)[0].strip()
    return ""


def _is_e2e_invocation(cmd):
    """命令是否**真会执行**整个 Playwright 面。

    只认以 `npm` / `npx` 开头的命令：`echo npm run test:e2e`、`true # …` 都是
    只把字样放进行里，实际不跑（dix C-2 审查的 fail-open 形态）。
    """
    if not cmd:
        return False
    first = cmd.split()[0]
    if first not in ("npm", "npx"):
        return False
    return "playwright test" in cmd or "test:e2e" in cmd


def _e2e_invocation_args(cmd):
    """取 Playwright 调用点携带的参数；空列表 = 收整个 testDir。

    两种调用形态：`npm run test:e2e -- <args>`（`--` 之后的才是传给 Playwright 的）
    与 `npx playwright test <args>`。认不出形态时按"带了参数"处理（保守判红）。
    """
    m = re.search(r"\btest:e2e\b(.*)$", cmd)
    if m is not None:
        rest = m.group(1).strip()
        if rest.startswith("--"):
            rest = rest[2:].strip()
        return rest.split() if rest else []
    m = re.search(r"\bplaywright\s+test\b(.*)$", cmd)
    if m is not None:
        return m.group(1).split()
    return [cmd]


def _frontend_e2e_wiring_problems(yml):
    """frontend 作业的 e2e 接线判据：空清单 = 真跑整个 e2e 面（dix C-2）。

    判据不是"行里有 Playwright 字样"，而是"真有一步无条件执行整个面"。以下都算红：
    · 执行行被注释掉、写成 `echo …`、或字样只活在行内注释里（没人跑）；
    · 执行步骤带 `if:`（条件不满足就整步跳过）；
    · 位置参数（`e2e/login.spec.ts`）或过滤标志（`--grep` / `--project`）只跑一部分。
    参数豁免必须登记到 `E2E_INVOCATION_ARGUMENT_EXEMPTIONS` 并写明理由。
    """
    block = _job_block(yml, "frontend")
    if not block:
        return ["ci.yml 里找不到 frontend 作业：node 侧测试没有调用点"]
    problems = []
    if "working-directory: frontend" not in block:
        problems.append(
            "frontend 作业没有 working-directory: frontend：npm 脚本会在仓库根解析不到")
    candidates = [(step, _step_command(step)) for step in _job_steps(block)]
    candidates = [(step, cmd) for step, cmd in candidates if _is_e2e_invocation(cmd)]
    if not candidates:
        problems.append(
            "frontend 作业里没有一条会执行的 Playwright 执行行（必须以 npm/npx 开头）："
            "frontend/e2e 的 spec 文件存在但没人调用（R3-008）")
        return problems
    for step, cmd in candidates:
        if re.search(r"(?m)^\s*if:\s*", step):
            problems.append(
                "Playwright 执行步骤带 if:（条件不满足就整步跳过）：%r；"
                "如确需条件执行，先在本门登记豁免并写明不会漏跑" % cmd)
        args = _e2e_invocation_args(cmd)
        if args and tuple(args) not in E2E_INVOCATION_ARGUMENT_EXEMPTIONS:
            problems.append(
                "Playwright 调用点带参数 %s：会把 spec 面收窄，覆盖门无法证明每个 spec "
                "都会跑；确需加参数先登记到 E2E_INVOCATION_ARGUMENT_EXEMPTIONS 并写明"
                "它不缩小覆盖" % args)
    return problems


def _job_block(text, name):
    """切出 `jobs:` 下某个作业的正文（缩进 2 的 `name:` 起，到下一个同级键止）。

    返回空串表示没这个作业——调用方必须判红，否则"作业改名/删掉"会让断言恒真。
    """
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if re.match(r"^  %s:\s*$" % re.escape(name), ln):
            start = i
            break
    if start is None:
        return ""
    out = []
    for ln in lines[start + 1:]:
        if re.match(r"^  [A-Za-z0-9_-]+:\s*$", ln):
            break
        out.append(ln)
    return "\n".join(out)


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


class FrontendE2eCiWiringTest(unittest.TestCase):
    """R3-008：`frontend/e2e/` 的每个 spec 必须有**一条真的会跑**的 CI 调用点。

    缺陷形状：这批 Playwright 用例只挂在 `e2e-nightly.yml`（定义点在默认分支 main，
    且那份文件 checkout 的 `object` 只在 develop）——部署分支 `server-web` 上一条都不跑。
    "文件存在但没人调用"是**静默**的（AGENTS §十三 第 29 条），故这里把两件事对账：
      ① `playwright.config` 的 `testDir` / `testIgnore` 是否覆盖每一个 spec；
      ② `ci.yml` 里是否有一条**未注释**的 Playwright 执行行，且它属于 `frontend` 作业
         （只有该作业有 node_modules 与 `working-directory: frontend`）。
    本组只钉"有没有人跑、跑的是不是这些文件"，不钉步骤顺序——步骤细节错了 CI 会响亮
    变红，不需要再钉一遍（同一判据两个定义点会漂）。
    接线判据不是"有一条 Playwright 执行行"，而是**那一步没有收窄 spec 面**：位置参数
    （`e2e/login.spec.ts`）与过滤标志（`--grep` / `--project`）都只跑一部分。递归发现与
    "真实 ci.yml 收窄即红"各有一枚元测试（dix C-2），改窄 ci.yml 不红＝覆盖门没牙。
    """

    def test_every_spec_is_covered_by_the_playwright_config(self):
        specs = _e2e_specs()
        self.assertGreaterEqual(len(specs), 1, "扫空不得当绿：e2e 目录下一个 spec 都没读到")
        cfg = _read(PLAYWRIGHT_CONFIG)
        m = re.search(r'testDir:\s*"([^"]+)"', cfg)
        self.assertIsNotNone(m, "读不出 playwright.config 的 testDir：口径丢了")
        test_dir = m.group(1).strip().lstrip("./").rstrip("/")
        self.assertTrue(test_dir, "testDir 为空串：配置坏了")
        problems = _playwright_scope_problems(cfg)
        self.assertEqual(
            problems, [],
            "playwright.config 出现 %s：覆盖门无法证明每个 spec 都会跑；"
            "如确需排除，先在本门登记显式豁免并说明原因" % problems)
        for spec in specs:
            rel = "e2e/%s" % spec
            self.assertTrue(
                rel.startswith(test_dir + "/"),
                "%s 不在 testDir=%r 之下：它永远不会被收集（文件存在但没人跑）" % (rel, test_dir))

    def test_ci_invokes_playwright_in_the_frontend_job(self):
        problems = _frontend_e2e_wiring_problems(_read(CI))
        self.assertEqual(
            problems, [],
            "ci.yml 的 frontend 作业没接住整个 e2e 面（R3-008）：%s" % problems)

    def test_scope_guard_catches_multiline_ignore_and_match(self):
        """活体反例：多行 testIgnore、单行 testMatch 都必须被判红。"""
        self.assertEqual(
            _playwright_scope_problems('testIgnore: [\n  "**/a.spec.ts",\n  "**/b.spec.ts"\n]'),
            ["testIgnore"])
        self.assertEqual(
            _playwright_scope_problems('testMatch: "**/*.spec.ts"'), ["testMatch"])
        self.assertEqual(_playwright_scope_problems("testDir: './e2e'"), [])

    def test_spec_discovery_recurses_into_subdirectories(self):
        """元测试：`_e2e_specs` 必须递归扫子目录（dix C-2）。

        旧断言只在真实目录上跑：把子目录里的 spec 静默漏掉，结果同样是"存在且有覆盖"。
        这里造两层临时目录，要求两条 spec 都被发现，且返回值是相对 E2E_DIR 的 posix 路径。
        """
        with tempfile.TemporaryDirectory() as tmp:
            deeper = os.path.join(tmp, "sub", "deeper")
            os.makedirs(deeper)
            for d, name in ((tmp, "top.spec.ts"), (deeper, "nested.spec.ts")):
                with io.open(os.path.join(d, name), "w", encoding="utf-8") as f:
                    f.write("// spec fixture\n")
            with mock.patch.dict(globals(), {"E2E_DIR": tmp}):
                self.assertEqual(_e2e_specs(),
                                 ["sub/deeper/nested.spec.ts", "top.spec.ts"],
                                 "递归发现漏了子目录：子目录里的 spec 会静默不跑")

    def test_the_wiring_checker_catches_a_narrowed_e2e_step(self):
        """活体反例：把 ci.yml 的 e2e 面改窄必须判红（dix C-2）。

        旧判据只要求"有一条未注释的 Playwright 执行行"：把 `npm run test:e2e` 改成
        `npm run test:e2e -- e2e/login.spec.ts` 后仍然绿——一个 spec 跑、其余静默不跑。
        这里在**真实 ci.yml** 上逐个收窄法做突变；真实接线本身必须先判绿。
        """
        yml = _read(CI)
        self.assertEqual(_frontend_e2e_wiring_problems(yml), [],
                         "真实 ci.yml 必须接住整个 e2e 面（夹具前提）")
        self.assertIn("run: npm run test:e2e", yml,
                      "变异锚点漂了：真实调用行不再逐字是 `run: npm run test:e2e`")
        narrows = (
            "npm run test:e2e -- e2e/login.spec.ts",
            "npm run test:e2e -- --grep login",
            "npm run test:e2e -- --project=chromium",
            "npx playwright test e2e/login.spec.ts",
            "npx playwright test --project=chromium",
        )
        for cmd in narrows:
            mutated = yml.replace("run: npm run test:e2e", "run: " + cmd)
            self.assertNotEqual(mutated, yml, f"变异没落到真实调用行上：{cmd}")
            self.assertTrue(
                _frontend_e2e_wiring_problems(mutated),
                "把 e2e 面收窄成 %r 仍判绿：覆盖门没有牙" % cmd)

    def test_the_wiring_checker_catches_a_step_that_does_not_run(self):
        """活体反例：三种「行里有字样、实际没人跑」的形态必须判红（dix C-2 审查）。

        · `if: false`：执行步被条件跳过；
        · `run: echo npm run test:e2e`：只是打印命令，不执行；
        · `run: true # npm run test:e2e`：字样只在行内注释里。
        旧判据按行内子串认，三种都判绿（fail-open：没人跑也过门）。
        """
        yml = _read(CI)
        anchor = "run: npm run test:e2e"
        self.assertEqual(_frontend_e2e_wiring_problems(yml), [],
                         "夹具前提：真实 ci.yml 必须先判绿")
        mutations = {
            "if-false": anchor + "\n        if: false",
            "echo": "run: echo npm run test:e2e",
            "inline-comment": "run: true # npm run test:e2e",
        }
        for label, replacement in mutations.items():
            mutated = yml.replace(anchor, replacement)
            self.assertNotEqual(mutated, yml, f"{label} 变异没落到真实调用行上")
            self.assertTrue(
                _frontend_e2e_wiring_problems(mutated),
                "%s 形态仍判绿：没人跑的步骤被当成调用点" % label)

    def test_the_wiring_checker_catches_a_commented_out_step(self):
        """活体反例：把执行行注释掉，检查器必须判红（否则本门恒真）。"""
        yml = _read(CI)
        block = _job_block(yml, "frontend")
        self.assertTrue(block, "夹具前提不成立：找不到 frontend 作业")
        commented = yml.replace(block, "\n".join(
            ("# " + ln if "test:e2e" in ln or "playwright test" in ln else ln)
            for ln in block.splitlines()))
        self.assertNotEqual(commented, yml, "变异没落到 frontend 作业上：夹具漂了")
        self.assertTrue(_frontend_e2e_wiring_problems(commented),
                        "被注释掉的执行行仍被判成调用点：本门的判据写反了")


if __name__ == "__main__":
    unittest.main(verbosity=2)
