# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""B0.5 §14 共享事实名册门禁的元测试：名册同源 / CI 接线 / 活体反例。

标签：J · 运维：部署/备份/发布
覆盖：`scripts/check-shared-facts.sh` + `scripts/gate/shared-facts.tsv` 这**一对**
    产物（名册是数据、脚本是执行器）的三条不变量——
    ① **名册与脚本同源**：脚本只从名册数据文件读"模式/上限"，自己不得内嵌第二
       份名册。判据：脚本文本里不得出现名册中任何一枚判定模式（内嵌即现形），且
       必须真的指向名册文件路径。
    ② **门在 CI**：`.github/workflows/ci.yml` 的 `verify` job 里必须有一步调用
       `scripts/check-shared-facts.sh`（且元测试自身也被跑）——否则门只是仓里的
       一个摆设，PR 上永不执行。
    ③ **活体反例**（防"废断言"，本文件的重心）：把合成树喂给真脚本——
       允许上限 = 实际命中数 ⇒ 绿；多一个定义点 ⇒ 红并点名 文件:行；
       把多出来的那处改成注释 ⇒ 又绿（注释不计）；删掉定义点 ⇒ 仍绿；
       名册/根目录不存在 ⇒ 退出码 2（门禁自身的环境错误，不是判红）。
    另加一条**当前合规树跑绿**（真树真跑，不是 mock）：门禁必须对今天的仓库判
       绿，否则它是"出生即红"的摆设。
    另一条钉住工单的诚实边界：`pending` 行不得带一个"假上限"凑数，且"每账号
    请求数 6"这枚**零定义点**键必须以 pending 显式登记。

对应实现：`scripts/check-shared-facts.sh`、`scripts/gate/shared-facts.tsv`、
    名册事实源 `D:/code/_census/out/{ROSTER,POINTS}.csv`（仓外、只读，一次性
    裁剪进仓；运行期不依赖它）。
关键断言：一律以真 bash 子进程的**退出码 + stdout** 为准；不 grep 被测脚本的
    内部实现细节，只 grep 它的数据源接线。
依赖：bash（`skipIf` 整文件缺 bash 时跳过）、真实临时目录；无网络、无第三方库
    （不引 PyYAML：本测试只用正则切 ci.yml 文本）。
"""
import io
import os
import re
import shutil
import subprocess
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(BASE, "scripts", "check-shared-facts.sh")
ROSTER = os.path.join(BASE, "scripts", "gate", "shared-facts.tsv")
CI_YML = os.path.join(BASE, ".github", "workflows", "ci.yml")
BASH = shutil.which("bash")

#: 名册列序（与 shared-facts.tsv 的表头逐字一致）
COLUMNS = ("族", "键", "状态", "判定模式", "扫描范围", "允许上限", "口径备注")
#: 本刀钉的三族（§14 原文：路径配置 / 状态件 / 语义常量）
FAMILIES = ("路径配置", "状态件", "语义常量")
#: 显式登记为 pending 的"零定义点"键：今天无可计数载体，不许硬编上限
ZERO_DEF_KEY = "每账号 HTTP 请求数 6"
#: 合成反例用的假模式（绝不出现在真名册里）
SYNTH_TOKEN = "synth-shared-fact-token"
SYNTH_KEY = "synth-key"


def _read(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def _rows():
    """读名册数据行（跳过空行与 `#` 注释）；返回 dict 列表。列序见 COLUMNS。"""
    rows = []
    for raw in _read(ROSTER).splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        cells = raw.split("\t")
        assert len(cells) == len(COLUMNS), f"名册列数不符（应为 {len(COLUMNS)}）: {raw!r}"
        rows.append(dict(zip(COLUMNS, cells, strict=True)))
    return rows


def _job_body(text, job):
    """从 ci.yml 文本里切出某个 job 的正文（缩进 2 空格的 `name:` 起、到下一个同级）。"""
    m = re.search(r"^  %s:\s*$" % re.escape(job), text, re.M)
    if m is None:
        return ""
    rest = text[m.end():]
    nxt = re.search(r"^  [A-Za-z0-9_-]+:\s*$", rest, re.M)
    return rest[:nxt.start()] if nxt else rest


def _run(script, *args):
    return subprocess.run([BASH, script, *args], capture_output=True, text=True,
                          cwd=BASE, timeout=120)


@unittest.skipUnless(BASH, "需要 bash（Git Bash / WSL）")
class SharedFactsRosterShapeTest(unittest.TestCase):
    """① 名册↔脚本同源；名册形状与诚实边界。"""

    def test_roster_and_script_both_exist(self):
        self.assertTrue(os.path.isfile(ROSTER),
                        "名册数据文件缺失——门禁必须自包含（CI runner 上没有仓外 _census）")
        self.assertTrue(os.path.isfile(SCRIPT), "门禁脚本缺失")

    def test_script_references_the_roster_and_embeds_no_second_copy(self):
        text = _read(SCRIPT)
        self.assertIn("scripts/gate/shared-facts.tsv", text,
                      "脚本必须指向仓内名册数据文件（名册是唯一事实源）")
        rows = _rows()
        self.assertGreaterEqual(len(rows), 20, "名册登记行数太少，覆盖不成门")
        embedded = [r["判定模式"] for r in rows
                    if r["状态"] == "active" and r["判定模式"] in text]
        self.assertEqual(embedded, [],
                         "脚本里出现了名册内的判定模式——等于内嵌了第二份名册，"
                         f"与数据文件会各自漂移：{embedded}")

    def test_roster_rows_are_wellformed(self):
        rows = _rows()
        seen = set()
        for r in rows:
            self.assertIn(r["族"], FAMILIES, f"本刀只钉三族，越界: {r}")
            self.assertIn(r["状态"], ("active", "pending"), f"状态取值非法: {r}")
            self.assertNotIn(r["键"], seen, f"键重复登记: {r['键']}")
            seen.add(r["键"])
            if r["状态"] == "active":
                self.assertTrue(r["判定模式"].strip(), f"active 行必须有判定模式: {r}")
                self.assertTrue(r["扫描范围"].strip(), f"active 行必须有扫描范围: {r}")
                self.assertGreaterEqual(int(r["允许上限"]), 0,
                                        f"允许上限必须是非负整数: {r}")
            else:
                self.assertEqual(r["允许上限"].strip(), "-",
                                 f"pending 行不许带假上限凑数（工单 §四.5）: {r}")
                self.assertTrue(r["口径备注"].strip(), f"pending 行必须写明原因: {r}")

    def test_zero_definition_point_key_is_registered_pending(self):
        """『每账号请求数 6』是零定义点键：必须显式登记 pending 并指向 B3，不许静默丢弃。"""
        hit = [r for r in _rows() if r["键"].startswith(ZERO_DEF_KEY)]
        self.assertEqual(len(hit), 1,
                         "『每账号请求数 6』必须以 pending 显式登记（静默丢弃=缺陷）")
        self.assertEqual(hit[0]["状态"], "pending")
        self.assertIn("B3", hit[0]["口径备注"])


@unittest.skipUnless(BASH, "需要 bash（Git Bash / WSL）")
class SharedFactsCiWiringTest(unittest.TestCase):
    """② 门在 CI 里，且在 `verify` job 内、审的是真脚本名。"""

    def setUp(self):
        self.ci = _read(CI_YML)
        self.verify = _job_body(self.ci, "verify")

    def test_verify_job_runs_the_gate_script(self):
        self.assertIn("scripts/check-shared-facts.sh", self.ci,
                      "ci.yml 没有调用门禁脚本——门在仓里但 PR 上永不执行")
        self.assertIn("scripts/check-shared-facts.sh", self.verify,
                      "门禁脚本必须在 verify job 内（job 名 verify 是分支保护引用的名字）")

    def test_verify_job_runs_this_meta_test(self):
        self.assertIn("tests/test_shared_facts_gate.py", self.verify,
                      "元测试必须与门禁同一步跑——否则名册/脚本漂移无人发现")

    def test_job_name_is_still_verify(self):
        """job 名不许改（改则必需检查静默失效，见 ci.yml 文件头批 1 约束 6）。"""
        self.assertIn("  verify:", self.ci)


@unittest.skipUnless(BASH, "需要 bash（Git Bash / WSL）")
class SharedFactsGateLiveTest(unittest.TestCase):
    """③ 活体反例：真脚本跑合成树（红/绿两侧）+ 当前合规树跑绿。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-sharedfacts-")
        self.root = os.path.join(self.tmp, "tree")
        os.makedirs(os.path.join(self.root, "src"))
        self.roster = os.path.join(self.tmp, "roster.tsv")
        self.file = os.path.join(self.root, "src", "a.py")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- 合成名册：一个 active 行，模式/上限由调用方给 -------------------------
    def _write_roster(self, allowed):
        with io.open(self.roster, "w", encoding="utf-8", newline="\n") as f:
            f.write("# 合成名册（元测试用）\n")
            f.write("\t".join(COLUMNS) + "\n")
            f.write("\t".join(("路径配置", SYNTH_KEY, "active", SYNTH_TOKEN,
                               "src", str(allowed), "合成")) + "\n")

    def _write_tree(self, body):
        with io.open(self.file, "w", encoding="utf-8", newline="\n") as f:
            f.write(body)

    def _gate(self):
        return _run(SCRIPT, "--root", self.root, "--roster", self.roster,
                    "--min-files", "1")

    def test_compliant_tree_is_green(self):
        self._write_tree('A = "%s"\nB = "%s"\n' % (SYNTH_TOKEN, SYNTH_TOKEN))
        self._write_roster(2)
        r = self._gate()
        self.assertEqual(r.returncode, 0, f"合规树必须绿：\n{r.stdout}\n{r.stderr}")
        self.assertIn("命中 2/2", r.stdout, "绿也要报出逐键计数（防静默零命中）")

    def test_one_extra_definition_point_turns_red_and_is_named(self):
        self._write_tree('A = "%s"\nB = "%s"\nC = "%s"\n'
                         % (SYNTH_TOKEN, SYNTH_TOKEN, SYNTH_TOKEN))
        self._write_roster(2)
        r = self._gate()
        self.assertEqual(r.returncode, 1,
                         f"多一个定义点必须判红：\n{r.stdout}\n{r.stderr}")
        self.assertIn(SYNTH_KEY, r.stdout, "判红必须点名键")
        self.assertIn("src/a.py:3", r.stdout, "判红必须点名 文件:行")
        self.assertIn("命中 3/2", r.stdout, "判红必须给出实际命中数/允许数")

    def test_comment_occurrences_do_not_count(self):
        # 第 3 处命中藏在注释里 ⇒ 非注释命中仍是 2 ⇒ 绿
        self._write_tree('A = "%s"\nB = "%s"\n# C = "%s"\n'
                         % (SYNTH_TOKEN, SYNTH_TOKEN, SYNTH_TOKEN))
        self._write_roster(2)
        r = self._gate()
        self.assertEqual(r.returncode, 0,
                         f"注释里的命中不计入（Python `#`）：\n{r.stdout}\n{r.stderr}")
        self.assertIn("命中 2/2", r.stdout)

    def test_trailing_comment_hit_does_not_count(self):
        self._write_tree('A = "%s"\nB = "%s"  # 又一个 %s\n'
                         % (SYNTH_TOKEN, SYNTH_TOKEN, SYNTH_TOKEN))
        self._write_roster(2)
        r = self._gate()
        self.assertEqual(r.returncode, 0,
                         f"行尾注释里的命中不计入：\n{r.stdout}\n{r.stderr}")
        self.assertIn("命中 2/2", r.stdout)

    def test_shell_and_js_and_html_comments_do_not_count(self):
        os.makedirs(os.path.join(self.root, "src"), exist_ok=True)
        with io.open(os.path.join(self.root, "src", "b.sh"), "w",
                     encoding="utf-8", newline="\n") as f:
            f.write("# %s\nX=%s\n" % (SYNTH_TOKEN, SYNTH_TOKEN))
        with io.open(os.path.join(self.root, "src", "c.js"), "w",
                     encoding="utf-8", newline="\n") as f:
            f.write("// %s\nconst y = '%s';\n" % (SYNTH_TOKEN, SYNTH_TOKEN))
        with io.open(os.path.join(self.root, "src", "d.html"), "w",
                     encoding="utf-8", newline="\n") as f:
            f.write("<!-- %s -->\n<p>%s</p>\n" % (SYNTH_TOKEN, SYNTH_TOKEN))
        self._write_tree('A = "%s"\n' % SYNTH_TOKEN)
        self._write_roster(4)  # 4 个文件各 1 处非注释命中（各自的注释行都不算）
        r = self._gate()
        self.assertEqual(r.returncode, 0,
                         f"bash `#`/JS `//`/HTML `<!-- -->` 注释都不计入：\n{r.stdout}\n{r.stderr}")
        self.assertIn("命中 4/4", r.stdout)

    def test_block_comment_spanning_lines_does_not_count(self):
        """跨行块注释（JS `/* */` 与 HTML `<!-- -->`）也必须整段不算。"""
        with io.open(os.path.join(self.root, "src", "e.js"), "w",
                     encoding="utf-8", newline="\n") as f:
            f.write("/*\n  %s\n*/\nconst ok = '%s';\n" % (SYNTH_TOKEN, SYNTH_TOKEN))
        self._write_tree('A = "%s"\n' % SYNTH_TOKEN)
        self._write_roster(2)
        r = self._gate()
        self.assertEqual(r.returncode, 0,
                         f"跨行 /* */ 块注释里的命中不计入：\n{r.stdout}\n{r.stderr}")
        self.assertIn("命中 2/2", r.stdout)

    def test_deleting_definition_points_stays_green(self):
        self._write_tree('A = "%s"\n' % SYNTH_TOKEN)
        self._write_roster(2)
        r = self._gate()
        self.assertEqual(r.returncode, 0,
                         f"删到不足上限仍绿（门只在超过时红）：\n{r.stdout}\n{r.stderr}")
        self.assertIn("命中 1/2", r.stdout)

    def test_missing_roster_is_an_env_error_exit_2(self):
        self._write_tree('A = "%s"\n' % SYNTH_TOKEN)
        r = _run(SCRIPT, "--root", self.root,
                 "--roster", os.path.join(self.tmp, "nope.tsv"), "--min-files", "1")
        self.assertEqual(r.returncode, 2,
                         f"名册不存在属门禁自身环境错误，退出码必须是 2：\n{r.stdout}\n{r.stderr}")

    def test_unknown_flag_is_an_env_error_exit_2(self):
        r = _run(SCRIPT, "--nope")
        self.assertEqual(r.returncode, 2, "--nope 应报未知参数并返回 2")

    def test_empty_tree_does_not_pass_silently(self):
        """不许因为"一个文件都没扫到"而零命中即通过。"""
        self._write_roster(2)
        r = self._gate()  # 树里除 roster 外无任何 .py/.sh/... 文件
        self.assertEqual(r.returncode, 2,
                         f"扫不到文件必须当环境错误（防空转）：\n{r.stdout}\n{r.stderr}")

    def test_current_repo_tree_is_green(self):
        """真树真跑：门禁对今天的仓库必须判绿（不是 mock、不是 skip）。"""
        r = _run(SCRIPT)
        self.assertEqual(r.returncode, 0,
                         f"当前合规树必须绿（否则门出生即红）：\n{r.stdout}\n{r.stderr}")
        self.assertRegex(r.stdout, r"扫描 \d+ 个文件")


if __name__ == "__main__":
    unittest.main(verbosity=2)
