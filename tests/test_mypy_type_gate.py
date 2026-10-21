# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""静态类型门禁（mypy）的契约：配置在位、门禁在 CI/入口内、且门禁真的会红。

标签：J · 运维：部署/备份/发布
覆盖：本批给仓内 Python 加 mypy 门禁（`pyproject.toml` 的 `[tool.mypy]` + 入口脚本
    的 mypy 步）。四条不变量，缺一条门禁就是摆设：
    ① **收编名单单一事实源**：`[tool.mypy]` 的 `files` 白名单逐字等于本文件冻结的
       `ADOPTED`；改名单（收编新模块）必须同批改这里。
    ② **严格档在位**：`disallow_untyped_defs` 等关键键真出现（否则"strict 起步"
       只是一句注释）。
    ③ **门禁被真执行**：入口脚本 `dev-verify.sh` 的 ruff 之后有 `-m mypy` 步，且
       静态检查（ruff+mypy）任一经 `lint_rc` 归一到统一裁决点。
    ④ **门禁承重（活体反例）**：收编模块真跑 mypy 必须绿；往临时模块塞一个类型错，
       用同一份配置跑必须红——且**换成默认档（无配置）跑又不红**，证明红是配置里
       的严格键带来的，不是 mypy 自身默认就能拦。
关键断言：①–③ 只读文本（pyproject / 入口脚本）；④ 真跑 `python -m mypy` 子进程。
依赖：需要测试解释器里装有 mypy。mypy 由 CI 的 `pip install mypy==1.19.0`（`ci.yml`）
    与本地门禁 venv 提供；**从未进 `requirements.lock`**（生产镜像不背门禁工具，先例
    同 ruff/pytest）。缺 mypy 时④判红并点名——门禁工具必须在场，不许静默跳过。
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYPROJECT = os.path.join(BASE, "pyproject.toml")
ENTRY = os.path.join(BASE, "scripts", "dev-verify.sh")

#: 收编名单（第一批）：与 `[tool.mypy]` 的 `files` 逐字对齐。
ADOPTED = (
    "yiban/core/clock.py",
    "yiban/core/status.py",
    "yiban/egress.py",
    "yiban/engine/schedule.py",
    "yiban/engine/planner.py",
    "yiban/adapters/persistence/queue_store.py",
    "yiban/engine/token_bucket.py",
)

#: 严格档里**必须**出现的键（缺任一，"strict 起步"就没落实）。逐键与
#: `[tool.mypy]` 里 `= true` 的严格键一一对应，顺序同配置——这 11 键是名单内严格档的
#: 全部，别处不再另立清单。
STRICT_KEYS = (
    "disallow_untyped_defs",
    "disallow_incomplete_defs",
    "check_untyped_defs",
    "disallow_any_generics",
    "disallow_subclassing_any",
    "disallow_untyped_decorators",
    "no_implicit_optional",
    "strict_equality",
    "warn_redundant_casts",
    "warn_unused_ignores",
    "warn_unused_configs",
)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _mypy_section(text):
    """切出 `[tool.mypy]` 段正文（到下一个 `[` 段头为止）。抽不到返回空串。"""
    m = re.search(r"^\[tool\.mypy\]\s*$(.*?)(?=^\[)", text, re.M | re.S)
    return m.group(1) if m else ""


def _run_mypy(args, cwd):
    """真跑 `python -m mypy`，返回 (退出码, 合并输出)。"""
    p = subprocess.run([sys.executable, "-m", "mypy", *args], cwd=cwd,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=300)
    return p.returncode, p.stdout


class MypyConfigTest(unittest.TestCase):
    """①②：配置在 pyproject，收编名单与严格档都真在。"""

    def setUp(self):
        self.text = _read(PYPROJECT)
        self.section = _mypy_section(self.text)

    def test_tool_mypy_section_exists(self):
        self.assertGreater(len(self.section), 50,
                           "pyproject.toml 里没有 [tool.mypy] 段——类型门禁的配置缺口未补")

    def test_python_version_targets_the_production_floor(self):
        self.assertIn('python_version = "3.13"', self.section,
                      "mypy 目标版本必须是生产下限 3.13（按它判语法/库可用性）")

    def test_files_whitelist_equals_the_frozen_adopted_list(self):
        m = re.search(r"files\s*=\s*\[(.*?)\]", self.section, re.S)
        self.assertIsNotNone(m, "[tool.mypy] 缺 files 白名单：收编边界没有单一事实源")
        got = tuple(re.findall(r'"([^"]+)"', m.group(1)))
        self.assertEqual(got, ADOPTED,
                         "收编名单漂了：改 [tool.mypy].files 必须同批改本文件的 ADOPTED 常量")

    def test_strict_keys_are_all_present(self):
        for key in STRICT_KEYS:
            self.assertRegex(self.section, r"(?m)^%s\s*=\s*true" % re.escape(key),
                             f"严格档缺 {key} = true：'strict 起步'没落实")

    def test_exempt_boundary_is_documented(self):
        """名单外豁免与收编路线必须写进配置（否则后人不知边界在哪、下一批收谁）。"""
        self.assertIn("名单外豁免清单", self.section,
                      "配置里没有'名单外豁免'登记：边界不可读")
        self.assertIn("收编路线", self.section,
                      "配置里没有'收编路线'：下一批收谁没有入口")


class MypyGateWiringTest(unittest.TestCase):
    """③：入口脚本真跑 mypy，且静态检查归一进统一裁决点。"""

    def setUp(self):
        self.text = _read(ENTRY)

    def test_full_mode_runs_mypy_after_ruff(self):
        ruff_at = self.text.find('echo "DEV-VERIFY ruff_exit=')
        mypy_at = self.text.find("DEV-VERIFY mypy_exit=")
        self.assertGreater(ruff_at, 0, "入口脚本没有 ruff 退出码回显")
        self.assertGreater(mypy_at, ruff_at,
                           "mypy 步必须在 ruff 之后、且回显 mypy_exit=（在 ruff_exit 之前不算）")
        self.assertIn('"$PY" -m mypy', self.text, "入口脚本没有真跑 mypy 执行行")

    def test_ci_subset_runs_mypy(self):
        m = re.search(r"^run_ci\(\) \{(.*?)^\}", self.text, re.M | re.S)
        self.assertIsNotNone(m, "抽不出 run_ci 函数体——结构改了？")
        self.assertIn('"$py" -m mypy', m.group(1), "CI 关键子集没有 mypy 步：门禁没进 CI")

    def test_static_check_result_is_folded_into_the_verdict(self):
        self.assertIn("lint_rc=$ruff_rc", self.text, "静态检查码没有被归一")
        self.assertIn('[ "$mypy_rc" = "0" ] || lint_rc=1', self.text,
                      "mypy 非 0 没有并入 lint 码：mypy 红会被淹没")
        self.assertIn('resolve_exit_code "$lint_rc" "$py_rc" "${FAST_EMPTY:-0}"', self.text,
                      "统一裁决点没收到归一后的静态检查码")

    def test_mypy_uses_an_isolated_cache_in_both_modes(self):
        """工单 dq2n：mypy 的判定不得取决于 `.mypy_cache` 新旧。

        mypy 在 `(path, size, mtime)` 相同时**不校验内容哈希**，直接信任缓存里的
        错误 ⇒ 同一份代码旧缓存红、新缓存绿。门每次运行都必须用**新建**的缓存目录，
        把上一轮（甚至 Windows 侧）的缓存排除在判定之外。
        """
        self.assertRegex(
            self.text, r'MYPY_CACHE="\$\(mktemp -d\)"',
            "全量/fast 模式必须每次新建缓存目录（mktemp -d），不得复用 .mypy_cache")
        self.assertIn('"$PY" -m mypy --cache-dir "$MYPY_CACHE"', self.text,
                      "全量/fast 模式的 mypy 步必须显式指定隔离缓存目录")
        m = re.search(r"^run_ci\(\) \{(.*?)^\}", self.text, re.M | re.S)
        self.assertIsNotNone(m, "抽不出 run_ci 函数体——结构改了？")
        self.assertIn('"$py" -m mypy --cache-dir "$CI_MYPY_CACHE"', m.group(1),
                      "--ci 的 mypy 步也必须用隔离缓存（就地跑最容易踩到脏缓存）")
        self.assertIn('CI_MYPY_CACHE="$(mktemp -d)"', m.group(1),
                      "--ci 的缓存目录必须每次新建")


class MypyGateLiveTest(unittest.TestCase):
    """④：活体正反例——收编模块绿；注入类型错必红；换成默认档又不红。"""

    def test_adopted_modules_are_clean_under_the_repo_config(self):
        # 隔离缓存：本用例的结论也不得取决于仓内 `.mypy_cache` 的新旧（工单 dq2n）。
        with tempfile.TemporaryDirectory() as cache:
            rc, out = _run_mypy(["--cache-dir", cache], BASE)
        self.assertEqual(rc, 0,
                         "收编模块在 [tool.mypy] 下必须绿（真跑 mypy 的输出如下）：\n" + out)
        # 缺 mypy 时 rc 也非 0：点明是"工具不在场"，不是"代码有错"。
        self.assertNotIn("No module named mypy", out,
                         "测试解释器里没有 mypy：门禁工具不在场（由 CI 的 pip install "
                         "mypy==1.19.0 与本地门禁 venv 提供；不进 requirements.lock）")

    def test_a_type_error_turns_the_gate_red(self):
        """注入一个**未标注**的函数：配了 disallow_untyped_defs 才该红。"""
        with tempfile.TemporaryDirectory() as tmp:
            mod = os.path.join(tmp, "mutant.py")
            with open(mod, "w", encoding="utf-8") as f:
                # 默认档 mypy 放行未标注 def；只有开了 disallow_untyped_defs 才红。
                f.write("def g(x):\n    return x\n")
            # 显式喂仓内配置（全局严格键照作用，files 白名单被显式路径覆盖）
            rc_strict, out_strict = _run_mypy(["--config-file", PYPROJECT, mod], tmp)
            # 同一文件、默认档（cwd 换成 tmp，避免自动发现 pyproject）：不该红
            rc_default, out_default = _run_mypy([mod], tmp)
        self.assertNotEqual(rc_strict, 0,
                            "严格档没抓住未标注 def：门禁不承重\n" + out_strict)
        self.assertIn("no-untyped-def", out_strict,
                      "红的理由不是 no-untyped-def：说明红的来源不是配置里的严格键\n" + out_strict)
        self.assertEqual(rc_default, 0,
                         "默认档本该放行未标注 def——它却红了，本用例的对照失效\n" + out_default)


class MypyCacheDeterminismTest(unittest.TestCase):
    """工单 dq2n：同一份代码，旧缓存/新缓存必须给同一结论。

    mypy 的缓存校验只看 `(path, size, mtime)`：三者都相同时**不算内容哈希**，直接
    信任缓存里记录的错误。于是"先跑错误版本（缓存记下红）→ 还原正确版本（同字节数、
    同 mtime）"会让同一份正确代码在旧缓存下红、新缓存下绿。门的处置是每次运行新建
    缓存目录：上一轮的判定不可能进入这一轮。
    """

    _GOOD = "def f(x: int) -> int:\n    return x\n"
    _BAD = "def f(x: int) -> int:\n    return y\n"   # 同字节数，只换名字

    def _sandbox(self):
        tmp = tempfile.mkdtemp(prefix="mypy-cache-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with open(os.path.join(tmp, "pyproject.toml"), "w", encoding="utf-8") as f:
            f.write('[tool.mypy]\npython_version = "3.10"\nfiles = ["m.py"]\n'
                    "disallow_untyped_defs = true\n")
        return tmp

    def _write(self, tmp, text, mtime=1700000000):
        path = os.path.join(tmp, "m.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        os.utime(path, (mtime, mtime))
        return path

    def test_stale_cache_can_flip_the_verdict_but_isolated_caches_cannot(self):
        tmp = self._sandbox()
        poisoned = os.path.join(tmp, "poisoned")
        # 1) 错误版本 + 新缓存 ⇒ 红（缓存里记下 name-defined）
        self._write(tmp, self._BAD)
        rc_bad, out_bad = _run_mypy(["--cache-dir", poisoned], tmp)
        self.assertNotEqual(rc_bad, 0, "错误版本必须红：\n" + out_bad)
        # 2) 还原正确版本：字节数相同、mtime 相同 ⇒ 旧缓存不失效（缺陷本体）
        self._write(tmp, self._GOOD)
        rc_stale, out_stale = _run_mypy(["--cache-dir", poisoned], tmp)
        # 3) 同一份正确文本 + 新缓存 ⇒ 绿（门每次都走这条）
        fresh = os.path.join(tmp, "fresh")
        rc_fresh, out_fresh = _run_mypy(["--cache-dir", fresh], tmp)
        self.assertEqual(rc_fresh, 0, "正确文本在新缓存下必须绿：\n" + out_fresh)
        self.assertNotEqual(
            rc_stale, 0,
            "旧缓存没能复现非确定性——本用例的前提失效（mypy 改了缓存校验策略？）")
        self.assertIn("name-defined", out_stale,
                      "旧缓存的红必须来自上一轮的错误（否则前提不成立）：\n" + out_stale)

    def test_gate_creates_a_new_cache_dir_on_every_run(self):
        """接线判据：门里的缓存目录来自 mktemp，不是固定路径。

        固定路径（哪怕在 /tmp）会把上一轮的缓存带进这一轮，等价于没修。
        """
        text = _read(ENTRY)
        self.assertRegex(text, r'MYPY_CACHE="\$\(mktemp -d\)"',
                         "全量/fast 的缓存目录必须 mktemp -d 新建")
        m = re.search(r"^run_ci\(\) \{(.*?)^\}", text, re.M | re.S)
        self.assertIsNotNone(m, "抽不出 run_ci")
        self.assertRegex(m.group(1), r'CI_MYPY_CACHE="\$\(mktemp -d\)"',
                         "--ci 的缓存目录必须 mktemp -d 新建")


if __name__ == "__main__":
    unittest.main()
