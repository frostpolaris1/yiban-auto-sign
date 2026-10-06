# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""B0.5 §14 共享事实名册门禁的元测试：名册同源 / CI 接线 / 活体反例。

标签：J · 运维：部署/备份/发布
覆盖：`scripts/check-shared-facts.sh` + `scripts/gate/shared-facts.tsv` 这**一对**
    产物（名册是数据、脚本是执行器）的三条不变量——
    ① **名册与脚本同源**：脚本只从名册数据文件读"模式/上限"，自己不得内嵌第二
       份名册。判据：脚本文本里不得出现名册中任何一枚判定模式（内嵌即现形），且
       必须真的指向名册文件路径。
    ② **门在 CI**：`.github/workflows/ci.yml` 的 `verify` job 里必须调用仓内入口脚本
       `scripts/dev-verify.sh`，且该脚本 `run_ci` 的关键子集里必须真跑
       `scripts/check-shared-facts.sh`（且元测试自身也被跑）——否则门只是仓里的
       一个摆设，PR 上永不执行。**两跳都审**：只审 ci.yml 文本会被注释满足
       （注释里写个脚本名就能骗过），那是废断言。
    ③ **活体反例**（防"废断言"，本文件的重心）：把合成树喂给真脚本——
       允许上限 = 实际命中数 ⇒ 绿；多一个定义点 ⇒ 红并点名 文件:行；
       把多出来的那处改成注释 ⇒ 又绿（注释不计）；删掉定义点 ⇒ 仍绿；
       名册/根目录不存在 ⇒ 退出码 2（门禁自身的环境错误，不是判红）。
       另钉四条易失效的判据：D1 某行 scope 指向不存在的路径/贡献 0 个文件 ⇒ 该键
       静默变废键，必须 exit 2 并点名键与 scope；D2 未引号 `${#x}`/`${x#y}` 里的
       `#` 不是注释；D3 `.css` 只认 `/* */`、HTML 的 `<script>` 段按 JS 规则剥
       （且**注释里写的** `<script` 不是脚本段起始——N1）；D5 `--root`/`--roster`/
       `--min-files` 后缺值 ⇒ 参数错 exit 2（不是 1）。
    另加一条**当前合规树跑绿**（真树真跑，不是 mock）：门禁必须对今天的仓库判
       绿，否则它是"出生即红"的摆设；且每条 active 键的命中数必须 > 0（某键静默
       变废键时必须红）。再加一条**真名册活体反例**（D6）：临时小树 + 真名册 +
       往某 active 键覆盖的真文件里加一处真定义点 ⇒ 必红并点名——合成 token 的
       反例钉不到"真模式/真上限在真文件上还开火"这条。
    另一条钉住工单的诚实边界：`pending` 行不得带一个"假上限"凑数，且"每账号
    请求数 6"这枚**零定义点**键必须以 pending 显式登记。

    gxf4（B0.5 名册补齐，2026-10-06）补登记 census 余下两族的 25 枚真键。
    信任权限 10 枚，线格式身份 15 枚。线格式身份有 1 枚零定义点键，只能 pending。
    本刀同批立四类守卫。
    ① 两族的在册行数等于 census 真键数。摘掉一枚键，本守卫即红。门自身对摘键无感，
    实证见 `test_gate_is_silent_when_a_registered_key_is_dropped`。
    ② 两族的上限等于登记当天的门实测值。上限与实测的差值就是门被放水的量。
    ③ 一行只许一个口径。生产定义点与测试引用不许混行。
    ④ 文件头的「覆盖边界」段必须与真行相符。散文漂移即读者按假话办事。

    另有一类钉住「docstring 计入定义点」这条口径决定。剥掉 docstring 是放水。
    它会让本册全部上限同时作废。下一个想剥的人必须显式重定全部上限。

    2026-10-06 起名册有**引擎路由**：判定模式以 `ast:` 开头的 active 行由
    `scripts/check-path-env-reads.py` 计数，本 awk 引擎让开并点名让开。"每枚
    active 键都要被计数"这条不变量因此只看本引擎的读数，两引擎的并集判据在
    `tests/test_path_env_read_gate.py`。

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
#: 仓内统一跑测入口（`--ci` 是关键子集；门禁命令原文住在它的 run_ci 里）
ENTRY_SCRIPT = os.path.join(BASE, "scripts", "dev-verify.sh")
BASH = shutil.which("bash")

#: 引擎路由标记：名册"判定模式"以此开头者由 scripts/check-path-env-reads.py 计数，
#: 不由本文件的 awk 引擎计数。并集判据（谁都得数、只许数一遍）见
#: tests/test_path_env_read_gate.py 的 GateNoDoubleCountTest。
AST_ENGINE_PREFIX = "ast:"

#: 名册列序（与 shared-facts.tsv 的表头逐字一致）
COLUMNS = ("族", "键", "状态", "判定模式", "扫描范围", "允许上限", "口径备注")
#: gxf4（B0.5 名册补齐）后本册覆盖的族。census `ROSTER.csv` 共 5 族 86 数据行，
#: 其中 2 行是 `(交付状态)` 元行 ⇒ 84 枚真键；B0.5（§14）只裁了三族 59 枚，
#: 余下 25 枚由 gxf4 补齐——FAMILIES 必须与 census 的族集等势（见
#: `SharedFactsCoverageClaimTest`），漏一族=那族的键没有门。
FAMILIES = ("路径配置", "状态件", "语义常量", "信任权限", "线格式身份")
#: gxf4 新增的两族（下面是它们在 census 里的族名原文）
TRUST_FAMILY = "信任权限"
WIRE_FAMILY = "线格式身份"
#: census 各族的**真键**行数（族名 → 枚数）。数字来处：`ROSTER.csv` 的 86 数据行
#: 减去 2 行 `(交付状态)` 元行（路径配置 1、信任权限 1）⇒ 84 枚；信任权限 11-1=10、
#: 线格式身份 15 枚全为真键。写死是牙——摘掉任意一枚键，本文件
#: `SharedFactsNewFamilyCoverageTest` 即红（门自身对摘键无感，见该测试自证）。
CENSUS_REAL_KEY_COUNTS = {"路径配置": 20, "状态件": 20, "语义常量": 19,
                          TRUST_FAMILY: 10, WIRE_FAMILY: 15}
#: 两族新登记的真键枚数（gxf4 交付面），供逐族断言直接引用
NEW_FAMILY_KEY_COUNTS = {TRUST_FAMILY: CENSUS_REAL_KEY_COUNTS[TRUST_FAMILY],
                         WIRE_FAMILY: CENSUS_REAL_KEY_COUNTS[WIRE_FAMILY]}
#: 线格式身份里唯一一枚**零定义点**键（census def=0）：只能 pending，不许硬编上限。
#: 它是两族 25 枚里唯一允许非 active 的行——`SharedFactsNewFamilyCoverageTest`
#: 把这条例外写死，多出一枚 pending 即红。
WIRE_ZERO_DEF_KEY = "JS 侧状态字符串硬编码比较（17 处消费、零定义点）"
#: census 的族汇总/交付状态元行不是一枚共享事实，不许被登记成键
CENSUS_META_MARKERS = ("(交付状态)", "__STATUS__")
#: 生产扫描路径名（`check-shared-facts.sh` 的 DEFAULT_SCOPE 里的条目）
PRODUCTION_PATHS = ("yiban", "scripts", "web", "docker", "deploy", ".github",
                    "run.sh", "run_probe.sh")
#: 测试路径名——与生产口径混在一行就是 gxf4 要消灭的 `tsv:92` 形状
TEST_PATHS = ("tests",)
#: 显式登记为 pending 的"零定义点"键：今天无可计数载体，不许硬编上限
ZERO_DEF_KEY = "每账号 HTTP 请求数 6"
#: 合成反例用的假模式（绝不出现在真名册里）
SYNTH_TOKEN = "synth-shared-fact-token"
SYNTH_KEY = "synth-key"
#: D6 活体反例：真名册里 scope 收窄到单文件的那条键，以及它的文件与"同形的真定义点"
REAL_KEY = "备份收录名册"
REAL_SCOPE_FILE = "scripts/backup.sh"
REAL_EXTRA_POINT = '        "${SIGN_STATE_DIR}"/zz-meta-test-extra.json\n'


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


def _func_body(text, func):
    """从 bash 脚本文本里切出某个函数的正文（列 1 的 `func() {` 起、到列 1 的 `}`）。

    签名行允许带行尾注释（`run_ci() { # …` 是本仓 bash 的常规写法）——不认注释会
    把正文抽成空串，断言随之失败（不是静默通过，但两跳审就永远红）。
    返回空串表示没抽到——调用方必须把它判成失败，否则"结构改了"会静默让断言恒真。
    """
    m = re.search(r"^%s\(\) \{\s*(?:#.*)?$" % re.escape(func), text, re.M)
    if m is None:
        return ""
    rest = text[m.end():]
    end = re.search(r"^\}$", rest, re.M)
    return rest[:end.start()] if end else ""


def _run(script, *args):
    return subprocess.run([BASH, script, *args], capture_output=True, text=True,
                          cwd=BASE, timeout=120)


def _gate_counts(stdout):
    """从门禁 stdout 解析 {键: (实测命中数, 允许上限)}。

    门对每枚在册 active 键都打印一行 `ok|提示|超标: 族 / 键 —— 命中 h/max`。
    本函数是"上限=实测"这条断言唯一的读数来源——直接取门禁自己的产出，
    守卫不另建第二个计数入口（否则两处口径会各自漂移）。
    """
    pat = re.compile(r"^(?:ok|提示|超标): \S+ / (.+?) —— 命中 (\d+)/(\d+)$", re.M)
    return {m.group(1): (int(m.group(2)), int(m.group(3))) for m in pat.finditer(stdout)}


def _rows_of(family):
    return [r for r in _rows() if r["族"] == family]


def _scope_classes(scope):
    """将一行的扫描范围拆成 (含生产路径, 含测试路径) 两个布尔。`default` 两个都 False。"""
    if scope.strip() == "default":
        return False, False
    toks = scope.split()
    prod = any(tok == p or tok.startswith(p + "/") for tok in toks for p in PRODUCTION_PATHS)
    test = any(tok == t or tok.startswith(t + "/") for tok in toks for t in TEST_PATHS)
    return prod, test


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
    """② 门在 CI 里，且在 `verify` job 内、审的是真脚本名。

    2026-10-05 起接线多了一跳：`verify` job 只调仓内入口脚本 `scripts/dev-verify.sh --ci`，
    真命令在它的 `run_ci` 函数体里（跑法统一到唯一入口，见 docs/dev/dev-verify.md）。
    因此"门在 CI"这条不变量必须**两跳都审**：job 真调入口脚本 + 入口脚本的 run_ci 真调门。
    只审 ci.yml 文本会被注释满足（注释里写个脚本名就能骗过），那是废断言。
    """

    def setUp(self):
        self.ci = _read(CI_YML)
        self.verify = _job_body(self.ci, "verify")
        self.entry = _read(ENTRY_SCRIPT)
        self.entry_ci = _func_body(self.entry, "run_ci")

    def test_verify_job_calls_the_entry_script(self):
        self.assertIn("bash scripts/dev-verify.sh --ci", self.verify,
                      "verify job 必须调仓内入口脚本的关键子集模式 scripts/dev-verify.sh --ci"
                      "（否则跑法又有第二个入口）")

    def test_entry_script_run_ci_really_runs_the_gate_script(self):
        self.assertGreater(len(self.entry_ci), 100,
                           "从入口脚本里抽不出 run_ci 函数体——结构改了？本条会因此变废断言")
        self.assertIn("bash scripts/check-shared-facts.sh", self.entry_ci,
                      "入口脚本的关键子集必须真跑门禁脚本（写在注释里不算：门要被执行）")

    def test_entry_script_run_ci_really_runs_this_meta_test(self):
        self.assertGreater(len(self.entry_ci), 100, "抽不出 run_ci 函数体")
        self.assertIn("tests/test_shared_facts_gate.py", self.entry_ci,
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

    # -- 合成名册：一个 active 行，模式/上限/扫描范围由调用方给 ----------------
    def _write_roster_rows(self, rows):
        """rows = [(键, 扫描范围, 允许上限)]，全部 active、模式固定为 SYNTH_TOKEN。"""
        with io.open(self.roster, "w", encoding="utf-8", newline="\n") as f:
            f.write("# 合成名册（元测试用）\n")
            f.write("\t".join(COLUMNS) + "\n")
            for key, scope, allowed in rows:
                f.write("\t".join(("路径配置", key, "active", SYNTH_TOKEN,
                                   scope, str(allowed), "合成")) + "\n")

    def _write_roster(self, allowed, scope="src"):
        self._write_roster_rows([(SYNTH_KEY, scope, allowed)])

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

    # -- D2：未引号 shell 展开里的 `#` 不是注释起始 ------------------------------
    def test_hash_inside_unquoted_shell_expansion_is_not_a_comment(self):
        """D2：`${#arr[@]}` / `${x#y}` 里的 `#` 不是注释——同行其后的真定义必须计到。

        否则 `if [ ${#arr[@]} -gt 0 ]; then D="/var/log/yiban"; fi` 这类行会被截断，
        同行真定义点漏检（假阴性）。
        """
        with io.open(os.path.join(self.root, "src", "b.sh"), "w",
                     encoding="utf-8", newline="\n") as f:
            f.write('arr=(a b)\n'
                    'if [ ${#arr[@]} -gt 0 ]; then D="%s"; fi\n'
                    'case ${y#%s} in *) :;; esac\n' % (SYNTH_TOKEN, SYNTH_TOKEN))
        self._write_roster(2)  # 两行各一处非注释定义点
        r = self._gate()
        self.assertEqual(r.returncode, 0,
                         "`${#…}`/`${x#…}` 不是注释，行内定义点必须计到：\n%s\n%s"
                         % (r.stdout, r.stderr))
        self.assertIn("命中 2/2", r.stdout)

    # -- D3：注释语言分档（CSS 的 `//` 不是注释；HTML 的 <script> 按 JS 规则）----
    def test_css_double_slash_is_not_a_comment(self):
        """D3：CSS 里 `//` 不是注释起始（只有 `/* */` 是）——不得截断整行。"""
        with io.open(os.path.join(self.root, "src", "s.css"), "w",
                     encoding="utf-8", newline="\n") as f:
            f.write("body{background:url(//cdn.example/%s)}\n" % SYNTH_TOKEN)
        self._write_roster(1)
        r = self._gate()
        self.assertEqual(r.returncode, 0,
                         f"CSS 的 `//` 不是注释，不得当注释截断：\n{r.stdout}\n{r.stderr}")
        self.assertIn("命中 1/1", r.stdout)

    def test_html_script_block_uses_slash_comment_rules(self):
        """D3：`<script>` 段里 `//` 是注释（假阳性必须消失），段内代码与 HTML 正文照算。"""
        with io.open(os.path.join(self.root, "src", "s.html"), "w",
                     encoding="utf-8", newline="\n") as f:
            f.write('<script>\n'
                    '// %s\n'
                    'var real = "%s";\n'
                    '</script>\n'
                    '<!-- %s -->\n'
                    '<p>%s</p>\n'
                    % ((SYNTH_TOKEN,) * 4))
        self._write_roster(2)  # 只有 script 内的代码行 + HTML 正文算
        r = self._gate()
        self.assertEqual(r.returncode, 0,
                         f"<script> 内 `//` 是注释、HTML 注释整段不算：\n{r.stdout}\n{r.stderr}")
        self.assertIn("命中 2/2", r.stdout)

    def test_script_open_tag_inside_html_comment_is_not_a_script_block(self):
        """N1：HTML 注释里的 `<script` 是注释文本，不是脚本段起始。

        若先认 `<script` 再判注释态，被注释掉的 `<script` 会被当成脚本开标签：
        该行既走 JS（`//`）规则，又把 HTML 注释态/脚本态留置成未闭合，污染后续行。
        常见方向是**假阳性**（注释行被计成命中 ⇒ 门噪），特定条件下是**假阴性**
        （泄漏后的 `//` 把紧跟的真定义点吞掉 ⇒ 门变松）。四个面都钉：
        a 单行注释内、b 整段被注释掉的 script、c 注释态泄漏到下一行、d 假阴性面。
        """
        cases = {
            "a": ('<!-- <script> %s -->\n<p>%s</p>\n', 2),
            "b": ('<!-- <script>\nvar x="%s";\n</script> -->\n<p>%s</p>\n', 2),
            "c": ('<!-- <script> -->\n<!-- %s -->\n<p>%s</p>\n', 2),
            "d": ('<!-- <script> -->\n<a href=//cdn/%s>\n', 1),
        }
        for name, (body, ntok) in cases.items():
            with self.subTest(case=name):
                rel = "src/n1-%s.html" % name
                with io.open(os.path.join(self.root, rel), "w",
                             encoding="utf-8", newline="\n") as f:
                    f.write(body % ((SYNTH_TOKEN,) * ntok))
                self._write_roster(1, scope=rel)  # 每个面只扫自己那个文件
                r = self._gate()
                self.assertEqual(r.returncode, 0,
                                 f"[{name}] HTML 注释里的 `<script` 不是脚本段，"
                                 f"真定义点只有正文那 1 处：\n{r.stdout}\n{r.stderr}")
                self.assertIn("命中 1/1", r.stdout,
                              f"[{name}] 必须恰好命中正文那 1 处（注释一律不算）")

    # -- D6：真名册 + 一处真定义点 ⇒ 必红并点名（元测试原本只用合成 token）------
    def test_real_roster_one_extra_real_definition_point_turns_red(self):
        """D6：用**真名册**（--roster 指向仓内 tsv）+ 真树子集，往某 active 键覆盖的
        真文件里加一处真定义点 ⇒ 必红并点名 文件:行（合成 token 的活体反例钉不到这条）。
        """
        tmp = tempfile.mkdtemp(prefix="yiban-sharedfacts-real-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        root = os.path.join(tmp, "tree")
        for d in ("yiban", "scripts", "tests"):
            os.makedirs(os.path.join(root, d))
        # 每个 scope 组至少要有一个可扫文件，否则会先撞上 D1 的"贡献 0 个文件"
        for stub in ("yiban/state_gc.py", "tests/test_state_gc.py"):
            with io.open(os.path.join(root, stub), "w",
                         encoding="utf-8", newline="\n") as f:
                f.write("# meta-test stub：只为占住 scope 面，不含任何判定模式\n")
        with io.open(os.path.join(root, REAL_SCOPE_FILE), "w",
                     encoding="utf-8", newline="\n") as f:
            f.write(_read(os.path.join(BASE, REAL_SCOPE_FILE)))
        pristine = _run(SCRIPT, "--root", root, "--roster", ROSTER, "--min-files", "1")
        self.assertEqual(pristine.returncode, 0,
                         f"未加定义点的真树子集必须绿（否则下面的红不成对照）：\n"
                         f"{pristine.stdout}\n{pristine.stderr}")
        with io.open(os.path.join(root, REAL_SCOPE_FILE), "a",
                     encoding="utf-8", newline="\n") as f:
            f.write(REAL_EXTRA_POINT)  # 第 10 处 > 名册冻结的 9
        r = _run(SCRIPT, "--root", root, "--roster", ROSTER, "--min-files", "1")
        self.assertEqual(r.returncode, 1,
                         f"真名册下多一处真定义点必须判红：\n{r.stdout}\n{r.stderr}")
        self.assertIn(REAL_KEY, r.stdout, "判红必须点名真名册里的那条键")
        self.assertRegex(r.stdout, re.escape(REAL_SCOPE_FILE) + r":\d+",
                         "判红必须点名真文件的 文件:行")

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
        """不许因为"一个文件都没扫到"而零命中即通过（今日由 D1 的 0 文件检查兜住）。"""
        self._write_roster(2)
        r = self._gate()  # 树里除 roster 外无任何 .py/.sh/... 文件
        self.assertEqual(r.returncode, 2,
                         f"扫不到文件必须当环境错误（防空转）：\n{r.stdout}\n{r.stderr}")

    # -- D1：scope 指向不存在的路径 / 贡献 0 个文件 ⇒ 该键静默变废键 -------------
    def test_scope_pointing_at_missing_path_is_an_env_error_exit_2(self):
        """D1：某行声明了非空 scope 但该路径在 --root 下不存在 ⇒ exit 2 并点名键与 scope。

        树里另有一条**命中正常**的键，所以旧行为是整体 rc=0（死键被静默略过）——
        "看着有门其实是废门"正是本门最该防的失效模式，不许只在 stdout 提示一句。
        """
        self._write_tree('X = "%s"\n' % SYNTH_TOKEN)
        self._write_roster_rows([("synth-ok", "src", 1),
                                 ("synth-typo", "scritps", 1)])  # 打错：树里只有 src/
        r = self._gate()
        self.assertEqual(r.returncode, 2,
                         f"scope 指向不存在的路径属门禁配置错误，退出码必须是 2：\n"
                         f"{r.stdout}\n{r.stderr}")
        msg = r.stdout + r.stderr
        self.assertIn("synth-typo", msg, "必须点名是哪个键死了")
        self.assertIn("scritps", msg, "必须点名是哪个 scope")

    def test_scope_contributing_zero_files_is_an_env_error_exit_2(self):
        """D1 同一条：scope 路径存在但贡献 0 个可扫文件，同样必须硬失败并点名。"""
        self._write_tree('X = "%s"\n' % SYNTH_TOKEN)
        os.makedirs(os.path.join(self.root, "assets"))
        with io.open(os.path.join(self.root, "assets", "notes.txt"), "w",
                     encoding="utf-8", newline="\n") as f:
            f.write('X = "%s"\n' % SYNTH_TOKEN)  # 非白名单扩展名 ⇒ 扫不到
        self._write_roster_rows([("synth-ok", "src", 1),
                                 ("synth-empty", "assets", 1)])
        r = self._gate()
        self.assertEqual(r.returncode, 2,
                         f"scope 贡献 0 个可扫文件属门禁配置错误，退出码必须是 2：\n"
                         f"{r.stdout}\n{r.stderr}")
        msg = r.stdout + r.stderr
        self.assertIn("synth-empty", msg, "必须点名是哪个键死了")
        self.assertIn("assets", msg, "必须点名是哪个 scope")

    def test_space_in_path_keeps_the_named_location_intact(self):
        """D4：**文件名**含空格时，判红点名的 文件:行 不许被拆成多个伪位置。"""
        with io.open(os.path.join(self.root, "src", "a b.py"), "w",
                     encoding="utf-8", newline="\n") as f:
            f.write('A = "%s"\nB = "%s"\n' % (SYNTH_TOKEN, SYNTH_TOKEN))
        self._write_roster(1)  # scope=src
        r = self._gate()
        self.assertEqual(r.returncode, 1,
                         f"两处定义点 > 上限 1 必须判红：\n{r.stdout}\n{r.stderr}")
        self.assertIn("src/a b.py:2", r.stdout,
                      "含空格路径的 文件:行 必须整条点名（不得被空格拆散）")
        self.assertIn("命中 2/1", r.stdout)

    def test_too_few_files_is_an_env_error_exit_2(self):
        """--min-files 仍是防线：扫到的文件太少 ⇒ exit 2（防空转，独立于 D1 的 0 文件）。"""
        self._write_tree('A = "%s"\n' % SYNTH_TOKEN)
        self._write_roster(1)
        r = _run(SCRIPT, "--root", self.root, "--roster", self.roster,
                 "--min-files", "1000")
        self.assertEqual(r.returncode, 2,
                         f"文件数不足 --min-files 必须当环境错误：\n{r.stdout}\n{r.stderr}")
        self.assertIn("只扫到 1 个文件", r.stdout + r.stderr)

    def test_bare_trailing_option_is_an_env_error_exit_2(self):
        """D5：`--root`/`--roster`/`--min-files` 后不带值 ⇒ 参数错 ⇒ exit 2（不是 1）。"""
        for opt in ("--root", "--roster", "--min-files"):
            with self.subTest(opt=opt):
                r = _run(SCRIPT, opt)
                self.assertEqual(r.returncode, 2,
                                 f"{opt} 后缺值属参数错误，退出码必须是 2"
                                 f"（rc=1 会被误读成有键超标）：\n{r.stdout}\n{r.stderr}")
                self.assertIn(opt, r.stdout + r.stderr, "报错要点名是哪个选项")

    def test_current_repo_tree_is_green(self):
        """真树真跑：门禁对今天的仓库必须判绿（不是 mock、不是 skip），
        且每条 active 键都还在开火（命中数 > 0）——某键静默变废键时这里要红。"""
        r = _run(SCRIPT)
        self.assertEqual(r.returncode, 0,
                         f"当前合规树必须绿（否则门出生即红）：\n{r.stdout}\n{r.stderr}")
        self.assertRegex(r.stdout, r"扫描 \d+ 个文件")
        self.assertNotIn("提示:", r.stdout,
                         "有 active 键命中 0——该键已静默失效（门形同虚设）")
        active = [row["键"] for row in _rows()
                  if row["状态"] == "active"
                  and not row["判定模式"].startswith(AST_ENGINE_PREFIX)]
        counted = re.findall(r"^ok: (?:%s) / (.+?) —— 命中 (\d+)/\d+$"
                             % "|".join(FAMILIES), r.stdout, re.M)
        self.assertEqual([k for k, _ in counted], active,
                         "逐键计数行必须与本引擎计数的 active 键一一对应（不许多、不许少、不许换序）")
        self.assertTrue(all(int(hits) > 0 for _, hits in counted),
                        "每条 active 键的命中数必须 > 0——否则该键已静默变废键")
        routed = [row["键"] for row in _rows()
                  if row["状态"] == "active"
                  and row["判定模式"].startswith(AST_ENGINE_PREFIX)]
        for key in routed:
            self.assertNotRegex(r.stdout,
                                r"^(?:ok|提示|超标): \S+ / %s —— " % re.escape(key),
                                "路由键不许被本引擎计数（两个引擎各扣一次上限会互相掩盖）")
        if routed:
            self.assertIn("引擎路由", r.stdout,
                          "有键走 ast: 路由时脚本必须点名让开——静默让开=没人发现键没人计数")


@unittest.skipUnless(BASH, "需要 bash（Git Bash / WSL）")
class SharedFactsNewFamilyCoverageTest(unittest.TestCase):
    """gxf4 ①：census 余下两族 25 枚真键必须全部在册，且上限=门禁当日实测值。

    为什么上限必须**等于**实测（比既有三族的"上限冻结"更严）：门只管"不许长出新
    定义点"，`h < max` 也绿。登记当天就把上限写成估算值或往大取整，门出生就带着
    一段静默余量——新增的定义点会落进那段余量里不被发现。两族是首次登记，没有
    历史冻结值要保护，所以按实测钉死。既有三族不纳入本条断言（实测有 5 枚 max
    高于 h，见 gate-baseline 记录；改它们会撞上 B1 其他簇正在修的行）。
    """

    @classmethod
    def setUpClass(cls):
        cls.gate = _run(SCRIPT)

    def test_two_new_families_registered_with_census_key_counts(self):
        for fam, want in NEW_FAMILY_KEY_COUNTS.items():
            got = _rows_of(fam)
            self.assertEqual(len(got), want,
                             f"族 {fam} 在册 {len(got)} 行，census 真键 {want} 枚——"
                             f"少一行=那枚共享事实没有门（摘键的红就在这条）")
        # 两族 25 枚里只许那一枚零定义点键是 pending：多出一枚就是"登记了但不判定"，
        # 等于既没门又占着行数（行数断言会被它喂饱）。
        not_active = [r["键"] for r in _rows_of(TRUST_FAMILY) + _rows_of(WIRE_FAMILY)
                      if r["状态"] != "active"]
        self.assertEqual(not_active, [WIRE_ZERO_DEF_KEY],
                         f"两族的非 active 行只许那一枚零定义点键: {not_active}")

    def test_wire_zero_definition_point_key_is_registered_pending(self):
        """census def=0 的那枚键必须显式 pending 且不许带假上限（与『每账号请求数 6』同规）。"""
        hit = [r for r in _rows_of(WIRE_FAMILY) if r["键"] == WIRE_ZERO_DEF_KEY]
        self.assertEqual(len(hit), 1, f"{WIRE_ZERO_DEF_KEY} 必须以 pending 显式登记")
        self.assertEqual(hit[0]["状态"], "pending")
        self.assertEqual(hit[0]["允许上限"].strip(), "-",
                         "零定义点键不许硬编上限凑数")
        self.assertIn("B3", hit[0]["口径备注"],
                      "零定义点键必须写明由谁先建定义点（B3），否则无人接手")

    def test_census_meta_rows_are_not_registered_as_keys(self):
        """census 的 `(交付状态)`/`__STATUS__` 元行不是一枚共享事实，不许混进名册凑数。"""
        for r in _rows():
            for marker in CENSUS_META_MARKERS:
                self.assertNotIn(marker, r["键"],
                                 f"census 元行被当成键登记了: {r['键']!r}")

    def test_cap_equals_measured_hit_count_for_both_new_families(self):
        self.assertEqual(self.gate.returncode, 0,
                         f"真树必须绿：\n{self.gate.stdout}\n{self.gate.stderr}")
        counts = _gate_counts(self.gate.stdout)
        for fam, want in NEW_FAMILY_KEY_COUNTS.items():
            self.assertEqual(len(_rows_of(fam)), want)
            for r in _rows_of(fam):
                if r["状态"] != "active":
                    self.assertNotIn(r["键"], counts,
                                     f"键 {r['键']} 是 pending 行，不该有计数行"
                                     f"（pending 不判定、不占上限）")
                    continue
                self.assertIn(r["键"], counts,
                              f"键 {r['键']} 在册但门没给它计数行——它没被任何引擎数")
                hits, cap = counts[r["键"]]
                self.assertEqual(hits, cap,
                                 f"键 {r['键']} 的上限 {cap} 不等于实测 {hits}——"
                                 f"差值就是门被放水的量（放宽上限凑绿禁止）")

    def test_gate_is_silent_when_a_registered_key_is_dropped(self):
        """实证门的语义边界：摘掉一行**不会**让门红（少判一枚键没有超标可报）。

        这条不是缺陷断言，是把"为什么必须由本守卫钉住 25 枚齐"写进可执行文档：
        若有人删掉一行并指望门兜住，这里证明兜不住——摘键的红只来自上面那条行数断言。
        """
        victims = _rows_of(TRUST_FAMILY) + _rows_of(WIRE_FAMILY)
        self.assertTrue(victims, "两族还没登记，本条无从取证")
        victim = victims[0]
        tmp = tempfile.mkdtemp(prefix="yiban-sfdrop-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        dropped = os.path.join(tmp, "roster-without-one-key.tsv")
        kept = [ln for ln in _read(ROSTER).splitlines()
                if ln.strip() and not ln.startswith("\t".join(
                    (victim["族"], victim["键"], victim["状态"])) + "\t")]
        with io.open(dropped, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(kept) + "\n")
        r = _run(SCRIPT, "--roster", dropped)
        self.assertEqual(r.returncode, 0,
                         f"预期门对摘键无感（若这条红=门的语义变了，要重读本测试）：\n"
                         f"{r.stdout}\n{r.stderr}")
        self.assertNotIn(victim["键"], _gate_counts(r.stdout),
                         "被摘掉的键不该还有计数行——它已经不在册了")


@unittest.skipUnless(BASH, "需要 bash（Git Bash / WSL）")
class SharedFactsDocstringSurfaceTest(unittest.TestCase):
    """gxf4 ②：口径决定「docstring 计入定义点」必须由活体反例钉住。

    门只剥注释语法，不剥字符串字面量（`check-shared-facts.sh` 文件头与名册文件头
    同一条线）。理由：把共享事实的数字抄进散文正是 census 判名册 B 点名的病——
    改代码不会改散文，所以这类命中**需要被数**。本类钉住这个方向：如果有人把
    docstring 也剥掉（全局口径改动），第二个测试会由红转绿而失败——那正是要求
    "必须显式重定全部上限"的信号。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-docstring-")
        self.root = os.path.join(self.tmp, "tree")
        os.makedirs(os.path.join(self.root, "src"))
        self.roster = os.path.join(self.tmp, "roster.tsv")
        # 第 1 行是 docstring 内的命中，第 4、5 行是代码里的真定义点
        with io.open(os.path.join(self.root, "src", "d.py"), "w",
                     encoding="utf-8", newline="\n") as f:
            f.write('"""模块 docstring：散文里抄了一遍 %s。"""\n'
                    'import os\n'
                    '\n'
                    'A = "%s"\n'
                    'B = "%s"\n' % ((SYNTH_TOKEN,) * 3))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _gate(self, allowed):
        with io.open(self.roster, "w", encoding="utf-8", newline="\n") as f:
            f.write("# 合成名册（docstring 口径反例）\n")
            f.write("\t".join(COLUMNS) + "\n")
            f.write("\t".join(("路径配置", SYNTH_KEY, "active", SYNTH_TOKEN,
                               "src", str(allowed), "合成")) + "\n")
        return _run(SCRIPT, "--root", self.root, "--roster", self.roster,
                    "--min-files", "1")

    def test_docstring_hit_is_counted_alongside_code(self):
        """上限 3 = 2 处代码 + 1 处 docstring ⇒ 绿，且必须报出 3。"""
        r = self._gate(3)
        self.assertEqual(r.returncode, 0,
                         f"docstring 里的命中必须计入（3 处全算才够上限）：\n"
                         f"{r.stdout}\n{r.stderr}")
        self.assertIn("命中 3/3", r.stdout, "报告里就得看见 docstring 被数进来了")

    def test_docstring_hit_is_named_as_an_overflow_point(self):
        """上限 2（只允许那 2 处代码）⇒ 必须红，并点名 docstring 所在的第 1 行。

        反向证据：光说"docstring 计入"不算，得让它在超出时**被点名到具体行**。
        有人把剥取逻辑扩到字符串字面量后，这条会转绿 ⇒ 本断言红 ⇒ 全局口径
        改动必须连带重定全部上限，正是工单要的效果。
        """
        r = self._gate(2)
        self.assertEqual(r.returncode, 1,
                         f"docstring 命中若不计入，此处会误绿：\n{r.stdout}\n{r.stderr}")
        self.assertIn("src/d.py:1", r.stdout,
                      "判红必须点名 docstring 那一行（第 1 行）作为超出的一击")
        self.assertIn("命中 3/2", r.stdout)


@unittest.skipUnless(BASH, "需要 bash（Git Bash / WSL）")
class SharedFactsScopePurityTest(unittest.TestCase):
    """gxf4 ③：一行只许一个口径——生产定义点与测试引用不许混在同一行。

    `tsv:92`（scope=`yiban scripts tests`，上限 41）的备注自述两次上调：21→33
    （docstring + 新 f-string）、33→41（tests 新增测试引用）。两种口径混在一枚键
    上，测试引用的增长会把生产定义点的余量吃掉——新增一处生产绕过点会落进那段
    虚高余量里不被发现。拆成两行后，各自的上限只能各自动，diff 就是留痕点。
    """

    def test_no_active_row_mixes_production_and_test_scope(self):
        mixed = []
        for r in _rows():
            if r["状态"] != "active":
                continue
            prod, test = _scope_classes(r["扫描范围"])
            if prod and test:
                mixed.append("%s（扫描范围=%s）" % (r["键"], r["扫描范围"]))
        self.assertEqual(mixed, [],
                         "以下 active 行把生产定义点与测试引用混进同一口径，"
                         f"上限无法分别归因: {mixed}")


@unittest.skipUnless(BASH, "需要 bash（Git Bash / WSL）")
class SharedFactsCoverageClaimTest(unittest.TestCase):
    """gxf4 ④：名册文件头的「覆盖边界」段必须与真行的族集/枚数/状态数逐项相符。

    为什么单立一类：本册唯一告诉后人"哪几族已登记、还差哪几族"的地方就是那段散文，
    而门与上面三条断言都看不见它（门只数上限、断言只数行）。gxf4 之前那段写着
    "信任权限 10 键与线格式身份 15 键不在本刀范围"——补登记当天它就成了假话。
    散文漂移=读者按假话办事（下一轮会重复登记或漏登记）⇒ 必须红。
    """

    def setUp(self):
        self.header = "\n".join(ln for ln in _read(ROSTER).splitlines()
                                if ln.lstrip().startswith("#"))
        self.rows = _rows()

    def test_header_declares_every_census_family_with_correct_key_counts(self):
        m = re.search(r"路径配置 (\d+) / 状态件 (\d+) / 语义常量 (\d+) / "
                      r"信任权限 (\d+) / 线格式身份 (\d+)", self.header)
        self.assertIsNotNone(m, "文件头「覆盖边界」段必须逐族写出真键数"
                                "（不写=读者无从核对本册缺了哪一族）")
        declared = {"路径配置": int(m.group(1)), "状态件": int(m.group(2)),
                    "语义常量": int(m.group(3)), TRUST_FAMILY: int(m.group(4)),
                    WIRE_FAMILY: int(m.group(5))}
        self.assertEqual(declared, CENSUS_REAL_KEY_COUNTS,
                         "文件头声明的逐族真键数与 census 不符")
        self.assertEqual(set(FAMILIES), set(CENSUS_REAL_KEY_COUNTS),
                         "FAMILIES 必须与 census 的族集等势（本文件两处硬编码不许各说各话）")

    def test_header_declared_counts_match_the_actual_rows(self):
        active = [r for r in self.rows if r["状态"] == "active"]
        pending = [r for r in self.rows if r["状态"] == "pending"]
        keys = sum(CENSUS_REAL_KEY_COUNTS.values())
        m = re.search(r"真键 (\d+) 枚", self.header)
        self.assertIsNotNone(m, "文件头必须写出 census 真键总数")
        self.assertEqual(int(m.group(1)), keys, "文件头声明的真键总数与逐族数之和不等")
        m = re.search(r"\+ (\d+) 行拆分", self.header)
        self.assertIsNotNone(m, "文件头必须写出拆分行数（一枚 census 键登记成多行时读者才对得上账）")
        extra = int(m.group(1))
        self.assertEqual(len(self.rows), keys + extra,
                         f"实有 {len(self.rows)} 行 ≠ 真键 {keys} + 拆分 {extra} 行——"
                         f"行数漂了就得同批改文件头「覆盖边界」段")
        m = re.search(r"合计登记 (\d+) 行", self.header)
        self.assertIsNotNone(m, "文件头必须写出登记总行数")
        self.assertEqual(int(m.group(1)), len(self.rows),
                         f"文件头声明合计 {m.group(1)} 行，实有 {len(self.rows)} 行")
        m = re.search(r"active (\d+) 行、pending (\d+) 行", self.header)
        self.assertIsNotNone(m, "文件头必须写出 active/pending 行数")
        self.assertEqual((int(m.group(1)), int(m.group(2))),
                         (len(active), len(pending)),
                         "文件头声明的 active/pending 行数与实有行数不符")


if __name__ == "__main__":
    unittest.main(verbosity=2)
