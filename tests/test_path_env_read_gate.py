# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""B1 第二刀：在册部署键裸读门禁的守卫测试。

覆盖对象是一对产物：`scripts/check-path-env-reads.py`（AST 执行器）与
`scripts/gate/shared-facts.tsv` 里 `ast:` 路由行的数据（键集/扫描范围/上限）。
本文件钉十三条不变量：
① 执行器不内嵌第二份名册。键集只从 tsv 读。
② 路由行形状合法。状态 active、上限是非负整数、扫描范围非空。
③ 上限等于实测命中数。修掉一处必须同批降一档。
④ 名册备注的"文件=处数"白名单等于真树的逐文件命中数。
   违规点被挪走（不是修掉）时本条要红。
⑤ 真树跑绿，且逐位置点名可解析。
⑥ 扫描文件数等于独立数出来的 .py 文件数。证明门真跑过。
⑦ 反空转：规则名打错、scope 指向不存在的目录、contributing 0 个 .py、
   名册缺失、参数缺值、.py 语法错——一律退出码 2，不是 0。
⑧ 活体反例：把 `resolve_path(` 换成 `os.environ.get(` 必须红并点名 文件:行；
   换回去必须绿。上限有牙：合成树加一处红、删一处绿并报出新当前数。
⑨ 写侧不误报：`os.environ[K] = v`、`del os.environ[K]`、`setdefault` 不计。
⑩ 注释不计、docstring 与字符串字面量算（与名册同口径，两向都给实测）。
⑪ 门在 CI：ci.yml 调入口脚本、入口脚本 run_ci 真调本门与本文件。
⑫ 一道键不得被两个引擎同时计数，也不许有 active 键谁都没数。

tests/ 有意不在门禁扫描面内。理由登记在名册那一行的"口径备注"。
已知弱化点（登记在同一行备注）：键名来自变量或 f-string 时 AST 取不到常量。
"""
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE = os.path.join(BASE, "scripts", "check-path-env-reads.py")
ROSTER = os.path.join(BASE, "scripts", "gate", "shared-facts.tsv")
B05_SCRIPT = os.path.join(BASE, "scripts", "check-shared-facts.sh")
ENTRY = os.path.join(BASE, "scripts", "dev-verify.sh")
CI_YML = os.path.join(BASE, ".github", "workflows", "ci.yml")
BASH = shutil.which("bash")

#: 路由标记：判定模式以此开头的名册行由本门（AST）计数，不由 awk 引擎计数
ENGINE_PREFIX = "ast:"
#: 本刀只实现这一种规则；规则名打错必须硬失败，不许静默变成"没有键要管"
RULE_NAME = "bare_environ_read"
#: 名册列序（与 shared-facts.tsv 表头逐字一致）
COLUMNS = ("族", "键", "状态", "判定模式", "扫描范围", "允许上限", "口径备注")
#: 工单点名的键（P2 并入的两枚 + 现状 10 处里出现的两枚）。这是**冻结的期望**，
#: 不是第二份键集名册：上限与完整键集一律以名册那一行为准，本枚只防"把键集改窄
#: 来把上限做小"。
TICKET_KEYS = ("YIBAN_ENV_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR", "YIBAN_BASE_PATH")
#: 垃圾目录：计数时跳过（执行器必须用同一份，见 test_scanned_file_count_matches）
JUNK_DIRS = {"__pycache__", ".venv", ".pytest_cache", ".ruff_cache", ".git", "node_modules"}
#: run_ci 里必须逐字出现的两步（与本文件同 B0.5 那一对的先例：门 + 门的元测试）
CI_GATE_CMD = '"$py" scripts/check-path-env-reads.py'
CI_META_CMD = '"$py" -m pytest tests/test_path_env_read_gate.py -q -p no:randomly'

#: 判行不许用 `$` 收尾：超标那行后面还跟着"（允许上限 N，超出 M 处…）"的说明。
RE_VERDICT = re.compile(r"^(ok|超标): (\S+) / (.+?) —— 命中 (\d+)/(\d+)", re.M)
RE_POSITION = re.compile(r"^ {4}(\S+):(\d+) (\S+)$", re.M)
RE_NFILES = re.compile(r"^扫描 (\d+) 个文件", re.M)
#: 名册备注里的白名单记法：`<相对路径>.py=<处数>`。字符类刻意不含中文，
#: 否则备注里紧挨着 token 的散文会被吞进路径名（实测过这个形状）。
RE_SITE_TOKEN = re.compile(r"[A-Za-z0-9_./-]+\.py=\d+")


def _read(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def _rows():
    """读名册数据行（跳过空行与 `#` 注释/表头）；返回 dict 列表。"""
    rows = []
    for raw in _read(ROSTER).splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        cells = raw.split("\t")
        assert len(cells) == len(COLUMNS), f"名册列数不符: {raw!r}"
        rows.append(dict(zip(COLUMNS, cells, strict=True)))
    return rows


def _routed_rows():
    """本门负责的登记行：判定模式以 `ast:` 开头者。"""
    return [r for r in _rows() if r["判定模式"].startswith(ENGINE_PREFIX)]


def _route_spec(row):
    """`ast:bare_environ_read:K1,K2` → (规则名, 键元组)。"""
    parts = row["判定模式"][len(ENGINE_PREFIX):].split(":", 1)
    rule = parts[0]
    keys = tuple(k for k in parts[1].split(",") if k) if len(parts) > 1 else ()
    return rule, keys


def _keys():
    return _route_spec(_routed_rows()[0])[1]


def _scope_dirs():
    return tuple(_routed_rows()[0]["扫描范围"].split())


def _py_files(root, dirs):
    """独立数一遍 .py 文件（本文件的判据与执行器的判据互相独立）。"""
    found = []
    for d in dirs:
        base = os.path.join(root, d)
        if not os.path.isdir(base):
            continue
        for cur, dnames, fnames in os.walk(base):
            dnames[:] = [x for x in dnames if x not in JUNK_DIRS]
            for fn in fnames:
                if fn.endswith(".py"):
                    found.append(os.path.relpath(os.path.join(cur, fn), root).replace(os.sep, "/"))
    return sorted(found)


def _run_gate(root=None, roster=None, min_files=None):
    argv = [sys.executable, GATE]
    if root is not None:
        argv += ["--root", root]
    if roster is not None:
        argv += ["--roster", roster]
    if min_files is not None:
        argv += ["--min-files", str(min_files)]
    return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                          cwd=BASE, timeout=180)


def _run_bash(script, *args):
    return subprocess.run([BASH, script, *args], capture_output=True, text=True,
                          encoding="utf-8", cwd=BASE, timeout=300)


def _func_body(text, func):
    """切 bash 函数正文。抽不出必须让调用方判失败，否则断言恒真。"""
    m = re.search(r"^%s\(\) \{\s*(?:#.*)?$" % re.escape(func), text, re.M)
    if m is None:
        return ""
    rest = text[m.end():]
    end = re.search(r"^\}$", rest, re.M)
    return rest[:end.start()] if end else ""


def _parse_gate(out):
    """门禁 stdout → (命中数, 上限, 位置三元组列表, 扫描文件数)。解析不到即失败。"""
    m = RE_VERDICT.search(out)
    assert m, f"门禁输出里没有『ok/超标 …… 命中 N/M』判行：\n{out}"
    nfiles = RE_NFILES.search(out)
    assert nfiles, f"门禁输出里没有『扫描 N 个文件』：\n{out}"
    return int(m.group(4)), int(m.group(5)), RE_POSITION.findall(out), int(nfiles.group(1))


class GateRosterShapeTest(unittest.TestCase):
    """① ② ⑤ 名册同源、路由行合法、真树跑绿。"""

    def test_gate_script_reads_the_roster_and_embeds_no_key(self):
        text = _read(GATE)
        self.assertIn("scripts/gate/shared-facts.tsv", text,
                      "执行器必须指向仓内名册数据文件（名册是唯一事实源）")
        embedded = [k for k in _keys() if k in text]
        self.assertEqual(embedded, [],
                         f"执行器里出现了在册键名——等于内嵌第二份名册：{embedded}")
        self.assertNotIn(_routed_rows()[0]["判定模式"], text,
                         "执行器里出现了完整判定模式——等于内嵌第二份名册")

    def test_routed_rows_are_active_and_wellformed(self):
        routed = _routed_rows()
        self.assertEqual(len(routed), 1, f"本刀只登记一枚 AST 路由键，实得 {len(routed)}")
        for r in routed:
            self.assertEqual(r["状态"], "active", f"路由行必须是 active: {r['键']}")
            self.assertEqual(_route_spec(r)[0], RULE_NAME, f"未知规则名（门会静默不管）: {r}")
            self.assertTrue(r["扫描范围"].strip(), "路由行必须有扫描范围")
            self.assertGreaterEqual(int(r["允许上限"]), 0, "上限必须是非负整数")
            self.assertTrue(r["口径备注"].strip(), "路由行必须写口径与白名单理由")
            keys = _route_spec(r)[1]
            self.assertGreaterEqual(len(keys), len(TICKET_KEYS), "在册键集不许比工单点名的还窄")
            self.assertTrue(all(k.startswith("YIBAN_") for k in keys), f"键名形状不对: {keys}")
            for k in TICKET_KEYS:
                self.assertIn(k, keys, f"工单点名的 {k} 不在了册键集里（改窄键集=放水）")

    def test_scan_surface_includes_docker_and_declares_tests_exemption(self):
        """扫描面必须含 docker/（ruff 面不含，本门不许继承那个缺口）。"""
        dirs = _scope_dirs()
        self.assertIn("docker", dirs, "扫描面漏了 docker/——继承 ruff 的已知缺口")
        self.assertNotIn("tests", dirs, "tests/ 若进出扫描面，必须同时改名册备注与本条")
        note = _routed_rows()[0]["口径备注"]
        self.assertIn("tests/", note, "tests/ 的豁免理由必须写在名册备注里，不许静默排除")

    def test_current_repo_is_green_and_cap_equals_live_count(self):
        r = _run_gate()
        self.assertEqual(r.returncode, 0, f"真树必须绿：\n{r.stdout}\n{r.stderr}")
        hits, cap, positions, _nfiles = _parse_gate(r.stdout)
        self.assertEqual(hits, cap,
                         f"上限必须钉在实测值（每修一处同批降一档、并从备注白名单删掉那一处）："
                         f"实测 {hits}、名册上限 {cap}")
        self.assertGreater(hits, 0, "真树命中 0——判定模式或扫描面已失效（门形同虚设）")
        self.assertEqual(len(positions), hits,
                         f"命中数与点名位置数必须一致：{hits} vs {len(positions)}")


class GateRatchetTest(unittest.TestCase):
    """④ ⑥ 白名单逐处对得上、门真跑过。"""

    def test_per_file_sites_match_the_roster_whitelist(self):
        """名册备注的 `文件=处数` 白名单必须与真树逐文件命中数一模一样。"""
        r = _run_gate()
        self.assertEqual(r.returncode, 0, f"真树必须绿：\n{r.stdout}\n{r.stderr}")
        _hits, _cap, positions, _nfiles = _parse_gate(r.stdout)
        per_file = {}
        for f, _line, _key in positions:
            per_file[f] = per_file.get(f, 0) + 1
        listed = {}
        for tok in RE_SITE_TOKEN.findall(_routed_rows()[0]["口径备注"]):
            path, n = tok.rsplit("=", 1)
            listed[path] = int(n)
        self.assertEqual(per_file, listed,
                         "违规点搬家了（一处没少、但文件分布与名册白名单不符）——"
                         "必须同批改名册那一行的白名单并写明新落点为什么允许")

    def test_every_whitelisted_site_carries_a_reason(self):
        """每一处放行都必须自带"为什么允许"：只写 `文件=处数` 不给理由 ⇒ 红。

        这一条取代早先的"白名单不少于 5 个文件"那条——那是**处数**下限，收口每降一档
        它就过期一次，而它拦的其实不是缩面：真缩面由 ③（命中数必须 > 0）与 ④（白名单
        必须逐文件等于真树）两侧夹住。改成判**形态**，收口收到只剩一处也不会放水。
        """
        note = _routed_rows()[0]["口径备注"]
        bare = []
        for m in RE_SITE_TOKEN.finditer(note):
            tail = note[m.end():]
            if not (tail.startswith("(") and not tail[1:].startswith(")")):
                bare.append(m.group())
        self.assertEqual(bare, [], "白名单里有光秃秃的放行条目（没写为什么允许）")

    def test_scanned_file_count_matches_an_independent_count(self):
        """反空转：门自己报的扫描文件数必须等于本文件独立数出来的 .py 数。"""
        r = _run_gate()
        self.assertEqual(r.returncode, 0, f"真树必须绿：\n{r.stdout}\n{r.stderr}")
        _hits, _cap, _pos, nfiles = _parse_gate(r.stdout)
        mine = _py_files(BASE, _scope_dirs())
        self.assertEqual(nfiles, len(mine),
                         f"门扫到的文件数与独立计数不符（{nfiles} vs {len(mine)}）——"
                         "要么扫描面漂了，要么门根本没打开目录")
        self.assertGreaterEqual(nfiles, 50, "少于 50 个 .py 说明扫描面被悄悄缩小了")


class GateLiveCaseTest(unittest.TestCase):
    """⑦ ⑧ ⑨ ⑩ 合成树活体反例（一律在仓外副本里做）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-pathenv-")
        self.root = os.path.join(self.tmp, "tree")
        self.roster = os.path.join(self.tmp, "roster.tsv")
        self.key = _keys()[0]
        self.alt = _keys()[1]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_roster(self, cap, scope="pkg"):
        row = dict(_routed_rows()[0])
        row["扫描范围"] = scope
        row["允许上限"] = str(cap)
        with io.open(self.roster, "w", encoding="utf-8", newline="\n") as f:
            f.write("\t".join(COLUMNS) + "\n")
            f.write("\t".join(row[c] for c in COLUMNS) + "\n")

    def _write(self, rel, body):
        path = os.path.join(self.root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with io.open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(body)
        return rel

    def _gate(self):
        return _run_gate(root=self.root, roster=self.roster, min_files=1)

    def _assert_green(self, expect_hits):
        r = self._gate()
        self.assertEqual(r.returncode, 0, f"必须绿：\n{r.stdout}\n{r.stderr}")
        hits, cap, positions, _nfiles = _parse_gate(r.stdout)
        self.assertEqual((hits, len(positions)), (expect_hits, expect_hits))
        self.assertEqual(cap, expect_hits)
        return hits, positions

    def _assert_red(self, expect_hits, cap):
        r = self._gate()
        self.assertEqual(r.returncode, 1, f"必须判红：\n{r.stdout}\n{r.stderr}")
        hits, got_cap, positions, _nfiles = _parse_gate(r.stdout)
        self.assertEqual((hits, got_cap, len(positions)), (expect_hits, cap, expect_hits))
        return positions

    # -- ⑧ 活体反例：resolve_path 换成裸读 ⇒ 红并点名；换回 ⇒ 绿 ----------------
    def test_resolve_path_swapped_to_bare_read_turns_red_and_back(self):
        good = ('import os\n'
                'from yiban.infra import env_io\n'
                'DB = env_io.resolve_path("%s", "yiban.db")\n' % self.key)
        self._write("pkg/a.py", good)
        self._write_roster(0)
        self._assert_green(0)
        self._write("pkg/a.py", good.replace("env_io.resolve_path(", "os.environ.get("))
        positions = self._assert_red(1, 0)
        self.assertEqual([p[0] for p in positions], ["pkg/a.py"], "判红必须点名文件")
        self.assertEqual(positions[0][1], "3", "判红必须点名行号")
        self.assertEqual(positions[0][2], self.key, "判红必须点名是哪个键")
        self._write("pkg/a.py", good)
        self._assert_green(0)

    # -- ⑧ 上限有牙：多加一处 ⇒ 红；删掉一处 ⇒ 绿且报出新当前数 -----------------
    def test_cap_ties_on_a_copy_of_the_production_tree(self):
        """真文件副本 + 真名册那一行：多加一处红、读数必须是 上限+1 并点名新落点。"""
        cap = int(_routed_rows()[0]["允许上限"])
        for rel in _py_files(BASE, _scope_dirs()):
            self._write(rel, _read(os.path.join(BASE, rel)))
        self._write_roster(cap, scope=" ".join(_scope_dirs()))
        r = self._gate()
        self.assertEqual(r.returncode, 0, f"真文件副本必须绿：\n{r.stdout}\n{r.stderr}")
        hits, _got_cap, positions, _nfiles = _parse_gate(r.stdout)
        self.assertEqual(hits, cap, "副本的真树命中数必须等于名册上限")
        self.assertTrue(positions, "副本上必须有点名位置（没有位置=门没真扫这些文件）")
        extra = self._write("yiban/_synth_extra.py",
                            'import os\nX = os.environ.get("%s")\n' % self.alt)
        r = self._gate()
        self.assertEqual(r.returncode, 1, f"多加一处必须红：\n{r.stdout}\n{r.stderr}")
        hits, got_cap, positions2, _n = _parse_gate(r.stdout)
        self.assertEqual((hits, got_cap), (cap + 1, cap), "红的读数必须是 上限+1")
        self.assertIn((extra, "2", self.alt), positions2,
                      f"判红必须点名新增那一处 文件:行:键，实得 {positions2}")

    def test_removing_one_site_stays_green_and_reports_new_count(self):
        cap = int(_routed_rows()[0]["允许上限"])
        for rel in _py_files(BASE, _scope_dirs()):
            self._write(rel, _read(os.path.join(BASE, rel)))
        self._write_roster(cap, scope=" ".join(_scope_dirs()))
        r = self._gate()
        self.assertEqual(r.returncode, 0, f"基线必须绿：\n{r.stdout}\n{r.stderr}")
        _hits, _cap, positions, _n = _parse_gate(r.stdout)
        path, line, _key = positions[0]
        full = os.path.join(self.root, path.replace("/", os.sep))
        lines = _read(full).splitlines(True)
        idx = int(line) - 1
        before = lines[idx]
        lines[idx] = before.replace("os.environ", "_env").replace("os.getenv", "getenv")
        self.assertNotEqual(lines[idx], before,
                            f"改不动第 {line} 行——量具失效，这一处的命中不是代码读法（{before!r}）")
        with io.open(full, "w", encoding="utf-8", newline="\n") as f:
            f.write("".join(lines))
        r = self._gate()
        self.assertEqual(r.returncode, 0, f"删一处应当仍绿：\n{r.stdout}\n{r.stderr}")
        hits, got_cap, _p, _n = _parse_gate(r.stdout)
        self.assertEqual((hits, got_cap), (cap - 1, cap),
                         "删一处必须报出新的当前数（上限不动，等下一批收口一起降档）")

    # -- ⑨ 写侧不算：赋值 / del / setdefault 都不许判红 -------------------------
    def test_write_side_is_not_counted(self):
        self._write("pkg/w.py", "import os\n"
                                'os.environ["%s"] = "x"\n'
                                'del os.environ["%s"]\n'
                                'os.environ.setdefault("%s", "1")\n'
                                % (self.key, self.alt, self.key))
        self._write_roster(0)
        self._assert_green(0)

    # -- ⑩ 口径实测：注释不计、docstring 与字符串字面量算 -----------------------
    def test_comments_excluded_but_docstrings_and_strings_counted(self):
        self._write("pkg/c.py", '"""说明里抄一句 os.environ.get("%s") 作为反例。"""\n'
                                '# 注释里也抄一句 os.environ.get("%s")\n'
                                'VAL = os.environ.get("%s")\n'
                                'TXT = "字符串里也抄 os.environ.get(\\"%s\\")"\n'
                                % ((self.key,) * 4))
        self._write_roster(3)
        _hits, positions = self._assert_green(3)
        got = sorted((p[0], p[1]) for p in positions)
        self.assertEqual(got, [("pkg/c.py", "1"), ("pkg/c.py", "3"), ("pkg/c.py", "4")],
                         "docstring(1)/真调用(3)/字符串字面量(4) 三处必须计，注释(2) 必须不计")
        self._write_roster(2)
        positions = self._assert_red(3, 2)
        self.assertNotIn("2", [p[1] for p in positions], "第 2 行是注释，判红名单里不许出现")

    def test_comment_only_occurrence_is_not_counted(self):
        self._write("pkg/n.py", 'import os\n'
                                '# x = os.environ.get("%s")\n'
                                'Y = 1\n' % self.key)
        self._write_roster(0)
        self._assert_green(0)

    # -- ⑦ 反空转：门自己的环境错误一律退出码 2，不许算"0 命中即绿" --------------
    def test_env_errors_exit_two_not_zero(self):
        self._write("pkg/a.py", 'X = os.environ.get("%s")\n' % self.key)
        self._write_roster(1)
        cases = {
            "roster_missing": ["--root", self.root, "--roster",
                               os.path.join(self.tmp, "nope.tsv"), "--min-files", "1"],
            "unknown_flag": ["--nope"],
            "bare_option": ["--root"],
            "bad_min_files": ["--root", self.root, "--roster", self.roster,
                              "--min-files", "x"],
        }
        for name, extra in cases.items():
            with self.subTest(case=name):
                r = subprocess.run([sys.executable, GATE, *extra], capture_output=True,
                                   text=True, encoding="utf-8", cwd=BASE, timeout=60)
                self.assertEqual(r.returncode, 2,
                                 f"[{name}] 环境错误必须是 2（0 会被读成合规）：\n"
                                 f"{r.stdout}\n{r.stderr}")

    def test_scope_pointing_at_missing_dir_is_an_env_error(self):
        self._write("pkg/a.py", 'X = os.environ.get("%s")\n' % self.key)
        self._write_roster(1, scope="pkg nope")
        r = self._gate()
        self.assertEqual(r.returncode, 2,
                         f"scope 指向不存在的目录必须硬失败（该键会静默变废键）：\n"
                         f"{r.stdout}\n{r.stderr}")
        self.assertIn("nope", r.stdout + r.stderr, "报错要点名是哪个 scope")

    def test_scope_with_zero_py_files_is_an_env_error(self):
        self._write("pkg/a.py", 'X = os.environ.get("%s")\n' % self.key)
        os.makedirs(os.path.join(self.root, "assets"), exist_ok=True)
        with io.open(os.path.join(self.root, "assets", "note.txt"), "w",
                     encoding="utf-8") as f:
            f.write("x\n")
        self._write_roster(1, scope="assets")
        r = self._gate()
        self.assertEqual(r.returncode, 2,
                         f"scope 贡献 0 个 .py 必须硬失败：\n{r.stdout}\n{r.stderr}")

    def test_unparseable_file_is_an_env_error(self):
        self._write("pkg/bad.py", "def (\n")
        self._write_roster(0)
        r = self._gate()
        self.assertEqual(r.returncode, 2,
                         f"语法错的 .py 必须响亮失败，不许跳过它继续报绿：\n"
                         f"{r.stdout}\n{r.stderr}")
        self.assertIn("pkg/bad.py", r.stdout + r.stderr, "报错要点名是哪个文件")

    def test_undecodable_file_is_an_env_error_not_a_violation(self):
        """非 UTF-8 的 .py ⇒ 退出码 2。让它冒 1 会被读成"有键超标"（判据混淆）。"""
        path = os.path.join(self.root, "pkg")
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "bin.py"), "wb") as f:
            f.write(b"# \xff\xfe bad bytes\nX = 1\n")
        self._write_roster(0)
        r = self._gate()
        self.assertEqual(r.returncode, 2,
                         f"读不出编码必须当环境错误（2），不是超标（1）：\n"
                         f"{r.stdout}\n{r.stderr}")
        self.assertIn("pkg/bin.py", r.stdout + r.stderr, "报错要点名是哪个文件")

    def test_unknown_rule_name_is_an_env_error(self):
        row = dict(_routed_rows()[0])
        row["判定模式"] = ENGINE_PREFIX + "no_such_rule:" + ",".join(_keys())
        with io.open(self.roster, "w", encoding="utf-8", newline="\n") as f:
            f.write("\t".join(COLUMNS) + "\n")
            f.write("\t".join(row[c] for c in COLUMNS) + "\n")
        self._write("pkg/a.py", 'X = os.environ.get("%s")\n' % self.key)
        r = self._gate()
        self.assertEqual(r.returncode, 2,
                         f"规则名打错必须硬失败（否则本门静默变成不管任何键）：\n"
                         f"{r.stdout}\n{r.stderr}")

    def test_too_few_files_is_an_env_error(self):
        self._write("pkg/a.py", 'X = os.environ.get("%s")\n' % self.key)
        self._write_roster(1)
        r = _run_gate(root=self.root, roster=self.roster, min_files=1000)
        self.assertEqual(r.returncode, 2,
                         f"扫不到足够文件必须当环境错误（防空转）：\n{r.stdout}\n{r.stderr}")


class GateCiWiringTest(unittest.TestCase):
    """⑪ 门在 CI 里：两跳都审，且审的是真调用行不是注释。"""

    def test_verify_job_calls_the_entry_script(self):
        ci = _read(CI_YML)
        m = re.search(r"^  verify:\s*$", ci, re.M)
        self.assertIsNotNone(m, "ci.yml 的 verify job 不见了")
        rest = ci[m.end():]
        nxt = re.search(r"^  [A-Za-z0-9_-]+:\s*$", rest, re.M)
        body = rest[:nxt.start()] if nxt else rest
        self.assertIn("bash scripts/dev-verify.sh --ci", body,
                      "verify job 必须调仓内入口脚本（本门不另建第二入口）")

    def test_run_ci_really_runs_the_gate_and_its_meta_test(self):
        body = _func_body(_read(ENTRY), "run_ci")
        self.assertGreater(len(body), 100, "抽不出 run_ci 函数体——本条会因此变废断言")
        lines = [ln.strip() for ln in body.splitlines()]
        for cmd in (CI_GATE_CMD, CI_META_CMD):
            self.assertIn(cmd, lines,
                          f"run_ci 必须**执行**这一行（写在注释里不算）：{cmd}")
        self.assertLess(lines.index(CI_GATE_CMD), lines.index(CI_META_CMD),
                        "门禁脚本必须先跑，元测试随后（与 B0.5 那一对同序）")


@unittest.skipUnless(BASH, "需要 bash（Git Bash / WSL）")
class GateNoDoubleCountTest(unittest.TestCase):
    """⑫ 一枚键只准一个引擎计数，也不许有 active 键没人计数。"""

    def test_routed_key_is_counted_by_exactly_one_engine(self):
        b05 = _run_bash(B05_SCRIPT)
        self.assertEqual(b05.returncode, 0, f"B0.5 门必须绿：\n{b05.stdout}\n{b05.stderr}")
        mine = _run_gate()
        self.assertEqual(mine.returncode, 0, f"本门必须绿：\n{mine.stdout}\n{mine.stderr}")
        verdict = re.compile(r"^(?:ok|超标|提示): (\S+) / (.+?) —— ", re.M)
        bash_keys = [k for _f, k in verdict.findall(b05.stdout)]
        ast_keys = [k for _f, k in verdict.findall(mine.stdout)]
        active = [r["键"] for r in _rows() if r["状态"] == "active"]
        routed = _routed_rows()
        self.assertTrue(all(r["键"] in ast_keys for r in routed),
                        f"路由键必须由 AST 引擎计数：{[r['键'] for r in routed]}")
        self.assertEqual(set(bash_keys) & set(ast_keys), set(),
                         "同一枚键被两个引擎同时计数（上限会被重复扣）")
        self.assertEqual(sorted(bash_keys + ast_keys), sorted(active),
                         "有 active 键没人计数，或被凭空多计")


if __name__ == "__main__":
    unittest.main(verbosity=2)
