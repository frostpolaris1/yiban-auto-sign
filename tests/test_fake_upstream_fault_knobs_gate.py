# -*- coding: utf-8 -*-
"""假上游故障旋钮覆盖门：每枚旋钮要么被用例消费，要么显式登记（140 §六-8／§六-12）。

标签：A · 判据：覆盖显性化

**本门挡的缺陷形状**：`yiban/adapters/protocol_fake/__init__.py` 提供了故障注入面（失败阶段、
人工延迟、失败概率、表单严格度…），而**没人开的旋钮等于不存在的替身能力**——140 §五
S12 现量：6 枚故障旋钮里 5 枚无人消费。这比"没有这个能力"更坏：能力在、文档在、
看着像覆盖了，实际没有任何用例走过那条路。

**本门钉的性质（一条）**：假上游的每一枚 CLI 旋钮，都必须在**库内某处被真的用过**，
或者在 `UNCONSUMED_KNOBS` 里**显式登记**（带理由与工单）。两条都不满足即红。
登记表反向陈旧也红：旋钮被某用例用上之后，条目必须删掉。

**为什么按旋钮而不按"某条用例"**：旋钮是**能力**的边界（`add_argument` 是唯一
定义点）。按用例记账会随用例改名而漂移。

**消费面口径**：库内全部被 git 跟踪的文本件，**排除** `yiban/adapters/protocol_fake/__init__.py`
自己（它当然"提到"自己声明的旋钮）。故 `scripts/e2e/`、`frontend/e2e/`、
`.github/workflows/` 里的用法都算消费。两条消费形态都认：**CLI 字面量**
（`--delay-ms`，驱动脚本/文档用）与**进程内关键字**（`delay_ms=`，用例直接起假服务端
时用；只查 CLI 字面量会把 `tests/test_login_e2e_mock.py` 的正当用法误判成无人消费
——本门实测踩到过）。

**本门不挡什么（如实登记）**：
  · 它**不**判断"某条用例是否真的开到了那条故障路径"（用了旋钮 ≠ 断言了故障语义）。
  · 检索是**文本级**的，不做语义归属：`--config` / `--log` 这类常见旗标可能在别的
    工具（如 `scripts/check-import-linter.py` 的同名旗标）里命中，从而被误判成"有人用"。
    短而通用的旗标上本门偏宽——宁可漏判不误判。

对应实现：`yiban/adapters/protocol_fake/__init__.py` 的 `argparse` 旋钮面与 `FAIL_STAGES`。
关键断言：钉"每枚旋钮有主"这一性质；不钉任何用例行为。
依赖：纯静态——AST 解析 `yiban/adapters/protocol_fake/__init__.py` ＋ `git ls-files` 枚举跟踪面 ＋ 文本检索。
    不跑服务、不触网。整文件在本机执行，无 skip。
"""
import ast
import os
import subprocess
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE = os.path.join(BASE, "yiban", "adapters", "protocol_fake", "__init__.py")
FAKE_REL = "yiban/adapters/protocol_fake/__init__.py"

#: 被检索的文本件后缀（消费面的载体形态）。
_TEXT_SUFFIXES = (".py", ".sh", ".bash", ".yml", ".yaml", ".md", ".ts", ".js", ".json",
                  ".toml", ".cfg", ".ini", ".conf")
#: 检索时跳过的目录（第三方与产物）。
_SKIP_DIR_PARTS = ("node_modules", ".git", "__pycache__", ".pytest_cache",
                   ".ruff_cache", ".mypy_cache", "vendor")

#: 登记为"今天无人消费"的旋钮：`{旋钮: (理由, 工单)}`。
#: 条目必须在库内仍然**无人消费**，否则本门判陈旧 ⇒ 红（逼人删条目）。
UNCONSUMED_KNOBS = {
    # ---- 时序面（能注入，但没人真的注入：140 §六-2 底数 B 的口径）----
    # `--delay-ms` 已于工单 `6mru` 被消费：`scripts/drill_multi_exec.py` 的
    # `start_mock(delay_ms=...)` 透传它，供引擎级一轮负载用例（
    # `tests/test_multi_executor_drill_e2e.py::EngineRoundLoadWithInjectedDelayTest`）
    # 让"等待行为"有真实载体。按本门的反向陈旧规则，条目删除。
    "--tail-delay-ms": ("尾延迟（长尾）模型：无用例开过", "140 §六-2"),
    "--tail-every": ("尾延迟的间隔参数：无用例开过", "140 §六-2"),
    # ---- 进程形状面 ----
    "--ipv6-host": ("IPv6 回环监听地址：无用例/驱动脚本开过", "140 §六-8"),
    "--keep-alive": ("长连接旋钮：无用例开过 ⇒ 连接复用那一维零载体", "140 §六-8"),
    "--pubkey-file": ("设备公钥文件：无用例开过", "140 §六-8"),
    # ---- 故障注入面（140 §六-12：6 枚旋钮 5 枚无人消费）----
    "--fail-stage=none": ("默认档（不注入）：无用例显式传过。等价于缺省，"
                          "登记以免被当成漏覆盖", "140 §六-12"),
    "--fail-stage=login": ("账密错形态：无用例开过 ⇒ `attempts.AUTH_*` 失败分类那一维"
                           "零行为载体", "140 §六-12"),
    "--fail-stage=signPosition": ("拉任务失败：无用例开过", "140 §六-12"),
    "--fail-stage=waf": ("ydclearance 挑战页：无用例开过 ⇒ 挑战识别支路零行为载体"
                         "（且该编造页与实拍页形态矛盾）", "140 §五 S3"),
    "--fail-stage=nonjson": ("长拦截 HTML：无用例开过 ⇒ `is_waf_blocked` 的 len 短路"
                             "那一维零载体", "140 §五 S12"),
    "--fail-stage=login-shallow": ("'假成功'（code==0 无 data）：无用例开过 ⇒ "
                                   "'只判 code 的旧登录门被假成功骗过'那一维零载体",
                                   "140 §五 S12"),
}


def cli_knobs(path=FAKE):
    """从假上游源码取 CLI 旋钮名（`add_argument("--x", ...)` 的第一个字符串实参）。"""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", "")
        if name != "add_argument" or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            out.append(first.value)
    return out


def fail_stages(path=FAKE):
    """取 `FAIL_STAGES` 元组里的阶段名（`--fail-stage` 的取值域）。"""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for t in node.targets:
            if getattr(t, "id", "") == "FAIL_STAGES" and isinstance(node.value, ast.Tuple):
                return [e.value for e in node.value.elts
                        if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return []


def tracked_text_files():
    """库内被 git 跟踪的文本件（相对路径，POSIX 分隔）。"""
    out = subprocess.run(["git", "ls-files"], cwd=BASE, capture_output=True, text=True)
    if out.returncode != 0:
        raise AssertionError("前置就绪：`git ls-files` 跑不起来（rc=%s，stderr=%r）"
                            % (out.returncode, (out.stderr or "")[-300:]))
    files = []
    for rel in (out.stdout or "").splitlines():
        rel = rel.strip()
        if not rel or not rel.endswith(_TEXT_SUFFIXES):
            continue
        if any(part in _SKIP_DIR_PARTS for part in rel.split("/")):
            continue
        files.append(rel)
    return files


#: 检索时必须排除的文件：假上游自己（它当然"提到"自己声明的旋钮），
#: 以及**本门自己**（`UNCONSUMED_KNOBS` 按构造就写着这些旋钮名——不排除会让
#: "无人消费"的登记被自己命中，门自己把自己判红。本仓实测踩到）。
_EXCLUDE_REL = frozenset({
    FAKE_REL,
    "tests/test_fake_upstream_fault_knobs_gate.py",
})


def consumed(needles, files):
    """这组证据形态里**任意一条**是否被消费面里的某个文件提到（不含排除面）。"""
    if isinstance(needles, str):
        needles = [needles]
    for rel in files:
        if rel in _EXCLUDE_REL:
            continue
        path = os.path.join(BASE, *rel.split("/"))
        try:
            with open(path, encoding="utf-8") as f:
                body = f.read()
        except (OSError, UnicodeDecodeError):
            continue
        if any(n in body for n in needles):
            return rel
    return None


def config_kwargs(path=FAKE):
    """取 `MockConfig.__init__` 的参数名（**进程内**消费面的那一半）。"""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "MockConfig":
            for m in node.body:
                if isinstance(m, ast.FunctionDef) and m.name == "__init__":
                    return [a.arg for a in m.args.args if a.arg != "self"]
    return []


def knob_needles(knob, stages, kwargs):
    """旋钮的"被消费"证据形态。

    两条路都算消费：
      · **CLI 形态**：`--delay-ms` 字面量（shell / 文档 / 驱动脚本）；
      · **进程内形态**：`MockConfig` 的同名关键字（`delay_ms=`）——用例直接起假服务端
        时走的是这条，只查 CLI 字面量会把它误判成"无人消费"（本门实测踩到过）。
    `--fail-stage` 按**档**记账：档才是真正的能力（`--fail-stage=signIn` 与
    `--fail-stage=waf` 覆盖的是两条完全不同的支路）。
    """
    if knob == "--fail-stage":
        out = []
        for s in stages:
            out.append(["--fail-stage=" + s, 'fail_stage="%s"' % s, "fail_stage='%s'" % s])
        return out
    snake = knob.lstrip("-").replace("-", "_")
    needles = [knob]
    if snake in kwargs:
        needles.append(snake + "=")
    return [needles]


class FakeUpstreamKnobCoverageTest(unittest.TestCase):
    """每枚故障旋钮要么有主，要么显式登记。"""

    def setUp(self):
        self.files = tracked_text_files()
        self.knobs = cli_knobs()
        self.stages = fail_stages()
        self.kwargs = config_kwargs()
        self.keys = {k: knob_needles(k, self.stages, self.kwargs) for k in self.knobs}

    def test_前置就绪_旋钮面与消费面都解析出来了(self):
        self.assertGreater(len(self.knobs), 5,
                           "假上游的 CLI 旋钮解析出 %d 枚：判定面失效" % len(self.knobs))
        self.assertIn("--fail-stage", self.knobs)
        self.assertIn("fail_stage", self.kwargs,
                      "`MockConfig.__init__` 的参数没解析到：进程内消费面失效")
        self.assertGreater(len(self.stages), 3,
                           "`FAIL_STAGES` 解析出 %d 档：判定面失效" % len(self.stages))
        self.assertGreater(len(self.files), 50,
                           "跟踪面只枚举出 %d 个文本件：消费面判定失效" % len(self.files))

    def test_每枚旋钮有主或已登记(self):
        orphans = []
        for knob in self.knobs:
            for needles in self.keys[knob]:
                key = needles[0]
                if key in UNCONSUMED_KNOBS:
                    continue
                if consumed(needles, self.files):
                    continue
                orphans.append(key)
        self.assertEqual(
            orphans, [],
            "这些假上游旋钮既没有用例消费、也没登记：%s。\n"
            "没人开的旋钮＝不存在的替身能力（看着像覆盖了，实际没有）。"
            "请给出一条用例真的开它，或在 `UNCONSUMED_KNOBS` 登记理由与工单。"
            % orphans)

    def test_登记表不得陈旧(self):
        stale = []
        for key, (why, ticket) in sorted(UNCONSUMED_KNOBS.items()):
            self.assertTrue(why, "登记 %s 缺理由" % key)
            self.assertTrue(ticket, "登记 %s 缺工单" % key)
            needles = next((n for group in self.keys.values() for n in group
                            if n[0] == key), None)
            if needles and consumed(needles, self.files):
                stale.append(key)
        self.assertEqual(
            stale, [],
            "这些旋钮已登记为'无人消费'，但库内已经有地方在用它们了——"
            "请把条目删掉：%s" % stale)

    def test_登记键必须是真旋钮(self):
        valid = {n[0] for group in self.keys.values() for n in group}
        bad = sorted(set(UNCONSUMED_KNOBS) - valid)
        self.assertEqual(
            bad, [],
            "登记表里有键不对应任何真实旋钮（旋钮被改名/删除）：%s" % bad)

    def test_检查器有牙(self):
        """合成一个不存在的旋钮：`consumed` 必须报"没人用"；真在用的必须报"有人用"。"""
        self.assertIsNone(consumed(["--definitely-not-a-real-knob-xyz"], self.files),
                          "消费面检索对'不存在的旋钮'报成有人用了——检索已失效")
        self.assertIsNotNone(
            consumed(["--ready-file", "ready_file="], self.files),
            "消费面检索对'库内确实在用的旋钮'（--ready-file，驱动脚本在用）报成没人用"
            "——检索已失效")
        self.assertIsNotNone(
            consumed(['fail_stage="signIn"'], self.files),
            "进程内消费形态（`fail_stage=\"signIn\"`）没被检索到——本门会把"
            "test_login_e2e_mock.py 的正当用法误判成无人消费")


if __name__ == "__main__":
    unittest.main(verbosity=2)
