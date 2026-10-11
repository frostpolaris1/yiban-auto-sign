# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""结构门禁（import-linter 四条依赖方向契约）的守卫测试。

标签：J · 运维：部署/备份/发布
覆盖对象是一对产物：`.importlinter`（契约本体）与 `scripts/check-import-linter.py`
（执行器）。本文件钉四条不变量：
① **契约本体在位**：四条契约的 id、类型、模块集合与 `docs/refactor/128` 三节一致。
   契约 2 的目标模块写成 `yiban.*` 而不是 `yiban`——写后者会被 import-linter 的
   "重叠对跳过"规则整条吃掉，本文件把这条陷阱钉死（改回 `yiban` 即红）。
② **门禁被真执行**：`ci.yml` 装 import-linter，`scripts/dev-verify.sh` 的 `run_ci`
   真跑本门与本文件。写在注释里不算。
③ **门禁承重（活体反例）**：合成树上真跑执行器。干净树退 0；植入违规退 1 并点名
   导入者与被导入者；违规进基线后退 0；基线条目消失时报"降档"。
④ **反空转**：配置缺失、基线列数不对、root_packages 指空、配置里零契约——一律退 2。
   退 2 与"有违规"（1）严格分开。不许把门自己的错读成通过。
关键断言：① ② 只读文本；③ ④ 真跑子进程，树是临时目录里的三文件合成包。
依赖：需要测试解释器里装有 import-linter。它由 CI 的
    `pip install import-linter==2.15`（`ci.yml`）与本地 WSL 门禁 venv 提供。
    缺件时③判红并点名——门禁工具必须在场，不许静默跳过（先例：mypy 门）。
"""
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(BASE, ".importlinter")
GATE = os.path.join(BASE, "scripts", "check-import-linter.py")
BASELINE = os.path.join(BASE, "scripts", "gate", "import-linter-baseline.txt")
ENTRY = os.path.join(BASE, "scripts", "dev-verify.sh")
CI_YML = os.path.join(BASE, ".github", "workflows", "ci.yml")

#: 四条契约的 id（与 `.importlinter` 逐字一致）。少一条即红：契约被删等于门被拆。
CONTRACT_IDS = (
    "yiban-no-web",
    "protocol-clean-room",
    "no-reverse-import-of-scripts",
    "core-layer-independence",
)
#: 契约 4 的 core 级模块（128 二节 L0）。四个都必须在册。
CORE_MODULES = ("yiban.clock", "yiban.window", "yiban.status", "yiban.masking")
#: run_ci 里必须逐字出现的两步（门 + 门的元测试）。
CI_GATE_CMD = '"$py" scripts/check-import-linter.py'
CI_META_CMD = '"$py" -m pytest tests/test_import_linter_gate.py -q -p no:randomly'
#: 门禁工具版本（与 ci.yml 的 pip 行一致）。升版必须同批改这里并写理由。
PINNED_TOOL = "import-linter==2.15"


def _read(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def _section(text, contract_id):
    """切出某个契约段正文（到下一个 `[` 段头为止）。抽不到返回空串。"""
    m = re.search(r"^\[importlinter:contract:%s\]\s*$(.*?)(?=^\[|\Z)"
                  % re.escape(contract_id), text, re.M | re.S)
    return m.group(1) if m else ""


def _field(section, key):
    """取某个多行字段的值（缩进行），返回去空白的行列表。"""
    m = re.search(r"^%s\s*=\s*$((?:\n[ \t]+\S.*)*)" % re.escape(key), section, re.M)
    if not m:
        return []
    return [ln.strip() for ln in m.group(1).splitlines() if ln.strip()]


def _func_body(text, func):
    """切出 bash 脚本里某个函数的正文（列 1 的 `func() {` 起、到列 1 的 `}`）。"""
    m = re.search(r"^%s\(\) \{\s*(?:#.*)?$" % re.escape(func), text, re.M)
    if m is None:
        return ""
    rest = text[m.end():]
    end = re.search(r"^\}$", rest, re.M)
    return rest[:end.start()] if end else ""


def _run_gate(root, config, baseline, extra=(), timeout=120):
    """真跑执行器，返回 (退出码, stdout+stderr 合并)。"""
    args = [sys.executable, GATE, "--root", root, "--config", config,
            "--baseline", baseline, "--min-modules", "1"]
    p = subprocess.run(args + list(extra), stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, timeout=timeout, text=True)
    return p.returncode, p.stdout


class ContractConfigTest(unittest.TestCase):
    """① 契约本体：四条契约的类型与模块集合。"""

    def setUp(self):
        self.text = _read(CONFIG)

    def test_four_contracts_are_declared(self):
        for cid in CONTRACT_IDS:
            with self.subTest(contract=cid):
                self.assertNotEqual(_section(self.text, cid), "",
                                    f"`.importlinter` 里缺契约 {cid}（契约被删等于门被拆）")

    def test_root_packages_cover_backend_frontend_and_shell(self):
        # root_packages 少一个，对应的契约就判不到东西（图里没有那个包）。
        header = self.text.split("[importlinter:contract:", 1)[0]
        for pkg in ("yiban", "web", "scripts"):
            with self.subTest(pkg=pkg):
                self.assertIn(pkg, _field(header, "root_packages"),
                              f"root_packages 缺 {pkg}：该包不在图里，相关契约静默失效")

    def test_contract_types_match_the_plan(self):
        want = {
            "yiban-no-web": "forbidden",
            "protocol-clean-room": "forbidden",
            "no-reverse-import-of-scripts": "forbidden",
            "core-layer-independence": "independence",
        }
        for cid, ctype in want.items():
            with self.subTest(contract=cid):
                self.assertIn(f"type = {ctype}", _section(self.text, cid),
                              f"{cid} 的契约类型变了：128 三节定的是 {ctype}")

    def test_contract_one_direction(self):
        sec = _section(self.text, "yiban-no-web")
        self.assertEqual(_field(sec, "source_modules"), ["yiban"])
        self.assertEqual(_field(sec, "forbidden_modules"), ["web"])

    def test_protocol_contract_uses_wildcard_not_bare_package(self):
        """契约 2 的目标模块必须是 `yiban.*`，不是 `yiban`。

        import-linter 的 forbidden 契约会**跳过重叠对**：源 `yiban.protocol` 与目标
        `yiban` 同属一棵包树，该对被跳过，整条契约静默失效（实测：写 `yiban` 时
        契约恒 KEPT，连真违规都不报）。写 `yiban.*` 才展开成互不重叠的兄弟，判定生效。
        """
        sec = _section(self.text, "protocol-clean-room")
        self.assertEqual(_field(sec, "source_modules"), ["yiban.protocol"])
        forbidden = _field(sec, "forbidden_modules")
        self.assertIn("yiban.*", forbidden, "契约 2 的目标模块必须写 `yiban.*`")
        self.assertNotIn("yiban", forbidden,
                         "写裸 `yiban` 会被重叠对跳过规则吃掉：契约会变成假门")
        for other in ("web", "scripts"):
            with self.subTest(module=other):
                self.assertIn(other, forbidden,
                              f"契约 2 要覆盖 protocol → {other}，不能只靠别的契约替它兜")

    def test_scripts_contract_forbids_reverse_import_only(self):
        sec = _section(self.text, "no-reverse-import-of-scripts")
        self.assertEqual(sorted(_field(sec, "source_modules")), ["web", "yiban"])
        self.assertEqual(_field(sec, "forbidden_modules"), ["scripts"])

    def test_core_modules_are_all_listed(self):
        sec = _section(self.text, "core-layer-independence")
        got = _field(sec, "modules")
        self.assertEqual(sorted(got), sorted(CORE_MODULES),
                         "core 级模块集合变了：128 二节 L0 定的是这四个")


class GateWiringTest(unittest.TestCase):
    """② 门与元测试都被真执行。"""

    def test_ci_installs_the_tool_at_the_pinned_version(self):
        ci = _read(CI_YML)
        self.assertIn(PINNED_TOOL, ci,
                      f"ci.yml 的 pip 行必须装 {PINNED_TOOL}：CI runner 上没这个库，"
                      "门会退 2（环境错误）而让 verify job 假红")

    def test_run_ci_really_runs_the_gate_and_its_meta_test(self):
        body = _func_body(_read(ENTRY), "run_ci")
        self.assertGreater(len(body), 100, "抽不出 run_ci 函数体——结构改了？")
        for cmd in (CI_GATE_CMD, CI_META_CMD):
            with self.subTest(cmd=cmd):
                self.assertIn(cmd, body,
                              f"run_ci 必须**执行**这一行（写在注释里不算）：{cmd}")

    def test_entry_script_frozen_command_list_has_the_new_step(self):
        """入口测试的冻结清单必须同步。少一行，CI 命令表就与脚本漂移。"""
        frozen = _read(os.path.join(BASE, "tests", "test_dev_verify_entry.py"))
        for cmd in (CI_GATE_CMD, CI_META_CMD):
            with self.subTest(cmd=cmd):
                self.assertIn(cmd, frozen,
                              "tests/test_dev_verify_entry.py 的 EXPECTED_CI_COMMANDS "
                              f"缺 {cmd}——CI 命令表是逐字冻结的")

    def test_gate_is_tracked_with_its_contract_and_baseline(self):
        for path in (".importlinter", "scripts/check-import-linter.py",
                     "scripts/gate/import-linter-baseline.txt"):
            with self.subTest(path=path):
                self.assertTrue(os.path.isfile(os.path.join(BASE, path)),
                                f"缺产物 {path}")

    def test_committed_baseline_header_matches_the_writer(self):
        """入库基线的文件头必须等于执行器写它时用的文件头。

        头里写着"契约 id 还是契约 name"这条身份口径。两处漂移过一次（改了口径忘了
        重生成文件），基线读起来就与判据不符。用执行器的常量逐行比对，最省事也最紧。
        """
        spec = importlib.util.spec_from_file_location("check_import_linter", GATE)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        body = _read(BASELINE)
        self.assertTrue(body.startswith(mod.BASELINE_HEADER),
                        "入库基线的文件头与执行器 BASELINE_HEADER 不一致："
                        "改了口径就要跑 --write-baseline 重生成它")
        self.assertIn("契约 id", mod.BASELINE_HEADER, "身份口径必须是契约 id")


class GateLiveTest(unittest.TestCase):
    """③ ④ 合成树上真跑执行器：红/绿两侧 + 反空转。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-implinter-")
        self.root = os.path.join(self.tmp, "tree")
        os.makedirs(os.path.join(self.root, "pkg"))
        for name in ("__init__.py", "core.py", "gateway.py"):
            with io.open(os.path.join(self.root, "pkg", name), "w",
                         encoding="utf-8", newline="\n") as f:
                f.write("# synthetic\n")
        self.config = os.path.join(self.root, "contracts.ini")
        with io.open(self.config, "w", encoding="utf-8", newline="\n") as f:
            f.write("[importlinter]\nroot_packages =\n    pkg\n\n"
                    "[importlinter:contract:no-upward]\n"
                    "name = pkg.core must not import pkg.gateway\n"
                    "type = forbidden\n"
                    "source_modules =\n    pkg.core\n"
                    "forbidden_modules =\n    pkg.gateway\n")
        self.baseline = os.path.join(self.tmp, "baseline.txt")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_baseline(self, rows):
        with io.open(self.baseline, "w", encoding="utf-8", newline="\n") as f:
            f.write("# 合成基线\n")
            for row in rows:
                f.write(row + "\n")

    def _plant(self, code="from pkg import gateway  # noqa: F401\n"):
        with io.open(os.path.join(self.root, "pkg", "core.py"), "a",
                     encoding="utf-8", newline="\n") as f:
            f.write(code)

    def _require_tool(self, out):
        """缺 import-linter 时判红并点名，不许静默跳过（先例：mypy 门）。"""
        if "import-linter 不可用" in out:
            self.fail("测试解释器里没有 import-linter：本门不许静默跳过。"
                      f"装上再跑：{sys.executable} -m pip install {PINNED_TOOL}\n{out}")

    def test_clean_tree_is_green(self):
        self._write_baseline([])
        rc, out = _run_gate(self.root, self.config, self.baseline)
        self._require_tool(out)
        self.assertEqual(rc, 0, f"干净树必须退 0：\n{out}")
        self.assertIn("KEPT", out)

    def test_planted_violation_reds_and_names_the_pair(self):
        self._plant()
        self._write_baseline([])
        rc, out = _run_gate(self.root, self.config, self.baseline)
        self._require_tool(out)
        self.assertEqual(rc, 1, f"植入违规必须退 1：\n{out}")
        self.assertIn("pkg.core -> pkg.gateway", out, "必须点名导入者与被导入者")
        self.assertIn("新增违规", out)

    def test_baselined_violation_is_green(self):
        self._plant()
        self._write_baseline(["no-upward\tpkg.core\tpkg.gateway"])
        rc, out = _run_gate(self.root, self.config, self.baseline)
        self._require_tool(out)
        self.assertEqual(rc, 0, f"已登记进基线的违规不许再红：\n{out}")

    def test_stale_baseline_entry_is_reported_for_ratchet(self):
        """基线多余条目：不红（门只对新增违规红），但必须报出来要求收紧。"""
        self._write_baseline(["no-upward\tpkg.core\tpkg.gateway"])
        rc, out = _run_gate(self.root, self.config, self.baseline)
        self._require_tool(out)
        self.assertEqual(rc, 0, f"基线多余条目不该判红（ratchet 提示而已）：\n{out}")
        self.assertIn("降档", out, "基线条目已消失必须报出来，否则基线只长不缩")
        self.assertIn("--write-baseline", out, "必须给出收紧命令")

    def test_write_baseline_records_the_current_set(self):
        self._plant()
        rc, out = _run_gate(self.root, self.config, self.baseline,
                            extra=("--write-baseline",))
        self._require_tool(out)
        self.assertEqual(rc, 0, f"--write-baseline 必须成功：\n{out}")
        body = _read(self.baseline)
        self.assertIn("no-upward\tpkg.core\tpkg.gateway", body)
        self.assertIn("#", body, "基线必须带文件头（语义写在头里）")
        # 写完之后再跑一遍必须绿（收紧过的基线对得上当前违规）。
        rc2, out2 = _run_gate(self.root, self.config, self.baseline)
        self.assertEqual(rc2, 0, f"收紧后的基线必须与当前违规集一致：\n{out2}")

    def test_malformed_baseline_row_is_an_environment_error(self):
        self._write_baseline(["no-upward\tpkg.core"])
        rc, out = _run_gate(self.root, self.config, self.baseline)
        self.assertEqual(rc, 2, f"基线列数不对必须退 2（门自己的错）：\n{out}")

    def test_missing_config_is_an_environment_error(self):
        self._write_baseline([])
        rc, out = _run_gate(self.root, os.path.join(self.tmp, "nope.ini"), self.baseline)
        self.assertEqual(rc, 2, f"配置缺失必须退 2：\n{out}")

    def test_config_without_contracts_is_an_environment_error(self):
        """配置里零契约：等于门被拆空，必须退 2 而不是"零违规通过"。"""
        empty = os.path.join(self.root, "empty.ini")
        with io.open(empty, "w", encoding="utf-8", newline="\n") as f:
            f.write("[importlinter]\nroot_packages =\n    pkg\n")
        self._write_baseline([])
        rc, out = _run_gate(self.root, empty, self.baseline)
        self._require_tool(out)
        self.assertEqual(rc, 2, f"零契约必须退 2：\n{out}")

    def test_vacuous_contract_target_is_an_environment_error(self):
        """契约的目标模块在图里零命中 ⇒ 退 2。

        import-linter 对"目标模块不在图里"**不报错**：它把该目标从判定面滤掉，契约恒
        KEPT、退出 0。实测（2026-10-11）过 `forbidden_modules = pkg.nonexistent`。
        一条死契约被读成合规，是本门最该拦的假绿。故执行器自己查每条契约的命中数。
        """
        vacuous = os.path.join(self.root, "vacuous.ini")
        with io.open(vacuous, "w", encoding="utf-8", newline="\n") as f:
            f.write("[importlinter]\nroot_packages =\n    pkg\n\n"
                    "[importlinter:contract:dead-target]\n"
                    "name = 目标模块不存在\n"
                    "type = forbidden\n"
                    "source_modules =\n    pkg.core\n"
                    "forbidden_modules =\n    pkg.nonexistent\n")
        self._write_baseline([])
        rc, out = _run_gate(self.root, vacuous, self.baseline)
        self._require_tool(out)
        self.assertEqual(rc, 2, f"契约空转必须退 2，绝不许读成合规：\n{out}")
        self.assertIn("空转", out, "拒绝信息必须说清契约空转")
        self.assertIn("pkg.nonexistent", out, "拒绝信息必须点名那个零命中的表达式")

    def test_vacuous_wildcard_target_is_an_environment_error(self):
        """通配目标同理：`pkg.nosuchpkg.*` 零命中也要退 2。"""
        vacuous = os.path.join(self.root, "vacuous-wild.ini")
        with io.open(vacuous, "w", encoding="utf-8", newline="\n") as f:
            f.write("[importlinter]\nroot_packages =\n    pkg\n\n"
                    "[importlinter:contract:dead-wildcard]\n"
                    "name = 通配目标零命中\n"
                    "type = forbidden\n"
                    "source_modules =\n    pkg.core\n"
                    "forbidden_modules =\n    pkg.nosuchpkg.*\n")
        self._write_baseline([])
        rc, out = _run_gate(self.root, vacuous, self.baseline)
        self._require_tool(out)
        self.assertEqual(rc, 2, f"通配契约空转必须退 2：\n{out}")

    def test_broken_root_package_is_an_environment_error(self):
        bad = os.path.join(self.root, "bad.ini")
        with io.open(bad, "w", encoding="utf-8", newline="\n") as f:
            f.write("[importlinter]\nroot_packages =\n    no_such_pkg\n\n"
                    "[importlinter:contract:x]\nname = x\ntype = forbidden\n"
                    "source_modules =\n    no_such_pkg.a\n"
                    "forbidden_modules =\n    no_such_pkg.b\n")
        self._write_baseline([])
        rc, out = _run_gate(self.root, bad, self.baseline)
        self._require_tool(out)
        self.assertEqual(rc, 2, f"图建不起来必须退 2：\n{out}")


class CurrentTreeTest(unittest.TestCase):
    """真树必须绿：冻结的契约与冻结的基线相互对得上。"""

    def test_current_repo_tree_is_green(self):
        rc, out = _run_gate(BASE, CONFIG, BASELINE, timeout=300)
        if "import-linter 不可用" in out:
            self.fail("测试解释器里没有 import-linter：本门不许静默跳过。"
                      f"装上再跑：{sys.executable} -m pip install {PINNED_TOOL}\n{out}")
        self.assertEqual(rc, 0, f"真树跑执行器必须退 0：\n{out}")
        self.assertIn("契约数 4", out, "四条契约必须全部被跑（少一条等于门被拆）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
