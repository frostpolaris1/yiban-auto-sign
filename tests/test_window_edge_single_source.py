# -*- coding: utf-8 -*-
"""窗口边界 env 解析的唯一入口守卫。

标签：A · 调度：计划与分片
覆盖：`yiban.window.parse_edges` 的六格取值（两侧新键齐 / 只有一侧新键 / 只有旧键 /
    新键越界 / 旧键取到上界 600 / 三键全缺）；四个读者（`window.parse_edges`、
    `window.from_env`、`web.render.edge_config`、`schedule._schedule_config`）在同一份
    env 输入下必须给出同一个 (front, back)；生产树的 Python 代码里按键名读这三个 env 键的
    语句只准出现在 `yiban/window.py::parse_edges` 内；`parse_edges` 的 docstring 主张的
    先后次序与两条合法域必须与实测一致；取值被拒（越界或无法解析）必须出一条 WARNING，
    点名键、原值、合法域与实际采用的值；合法值与缺席必须不出声；旧键 0~600、新键 0~300
    的域分叉必须留在具名常量、docstring 与 web 侧口径注释里（工单 yz90）。
对应实现：yiban/window.py（parse_edges、from_env、_int_or_none、EDGE_MAX_SEC、
    LEGACY_EDGE_MAX_SEC）、web/render.py（edge_config）、
    yiban/engine/schedule.py（_schedule_config）。
关键断言：窗口边界只有一个 env 解析入口。第二份解析接进任何读者 ⇒ 取值表当场对不上；
    第二份解析只是被复制出来、还没接线 ⇒ AST 位置判据当场点名。
    注释与 docstring 里的键名不进判定（AST 只认代码），本文件用两个量具自证：
    合成夹具里五处键名写法只点名两处真取值；次序分类器抓得住订正前那句假话。
已知盲点：出声判据只认 `yiban.window` 这一枚 logger 名。解析若搬家、告警发到别的
    logger 上，本判据红；这是刻意的——搬家要走本文件，不许悄悄换出口。
依赖：纯本地：打桩 os.environ，不建库、不发网络请求、不读 .env 文件。
"""
import ast
import contextlib
import os
import re
import unittest
from unittest import mock

from web import render
from yiban import window
from yiban.engine import schedule

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

EDGE_KEYS = ("YIBAN_WINDOW_EDGE_FRONT_SEC", "YIBAN_WINDOW_EDGE_BACK_SEC",
             "YIBAN_WINDOW_EDGE_SEC")
#: 唯一许可的 env 解析入口：（相对路径，函数名）。越界判据以此为轴。
SINGLE_ENTRY = ("yiban/window.py", "parse_edges")
#: AST 扫描的生产树目录，与 `scripts/check-shared-facts.sh` 的 DEFAULT_SCOPE 同族。
SCAN_DIRS = ("yiban", "web", "docker", "scripts")
SKIP_DIRS = ("__pycache__",)  # vendored 目录已退役：生产树整体受扫，不再有豁免面
#: 只在注释里写这三个键名的生产模块；它们必须被扫到、且必须零命中。
COMMENT_ONLY_MODULES = ("web/app.py", "web/render.py", "yiban/engine/schedule.py")

#: 「新键与旧键同时存在」这一格；docstring 的次序判据也用它。
BOTH_KEYS_CASE = ({"YIBAN_WINDOW_EDGE_FRONT_SEC": "90", "YIBAN_WINDOW_EDGE_BACK_SEC": "30",
                   "YIBAN_WINDOW_EDGE_SEC": "120"}, (90, 30), "新键与旧键同时存在：新键赢")

#: (env 覆盖, 期望 (front, back), 这一格的主张)
CASES = (
    BOTH_KEYS_CASE,
    ({"YIBAN_WINDOW_EDGE_FRONT_SEC": "90", "YIBAN_WINDOW_EDGE_SEC": "120"},
      (90, 120), "只有一侧新键：旧键只补另一侧那一格"),
    ({"YIBAN_WINDOW_EDGE_SEC": "120"}, (120, 120), "只有旧键：旧键补两侧两格"),
    ({"YIBAN_WINDOW_EDGE_FRONT_SEC": "900", "YIBAN_WINDOW_EDGE_SEC": "120"},
      (120, 120), "新键越界（>300）视同缺席：旧键补两格"),
    # 工单 yz90 的域分叉：600 只在旧键合法。这一格把"旧键上界 600"钉在四个读者上——
    # 有人把 600 顺手改成 300（未裁先动），本格与下面的 DomainDivergenceTest 一起红。
    ({"YIBAN_WINDOW_EDGE_SEC": "600"}, (600, 600), "旧键合法域到 600：600 必须照样生效"),
    ({}, (window.DEFAULT_EDGE_SEC, window.DEFAULT_EDGE_SEC), "三键全缺：两侧取默认"),
)


@contextlib.contextmanager
def _overlay_env(env):
    """把这一格的 env 覆盖进 `os.environ`，先把三个边界键清干净。"""
    base = {k: v for k, v in os.environ.items() if k not in EDGE_KEYS}
    base.update({k: str(v) for k, v in env.items()})
    with mock.patch.dict(os.environ, base, clear=True):
        yield


def _via_parse_edges(env):
    return window.parse_edges(env)


def _via_from_env(env):
    """引擎与网页共用的 `Window` 入口（过 `bounds`，取视图上的 front/back）。"""
    win = window.from_env(env)
    return (win.front_sec, win.back_sec)


def _via_render_edge_config(env):
    return render.edge_config(env)


def _via_schedule_config(env):
    """引擎入口：`_schedule_config` 自读 `os.environ`，故把这一格覆盖进去再取 cfg。"""
    with _overlay_env(env):
        cfg = schedule._schedule_config()
    return (cfg["edge_front_sec"], cfg["edge_back_sec"])


#: 各读者：同一个 env 映射进，同一个 (front, back) 出。
READERS = (
    ("yiban.window.parse_edges", _via_parse_edges),
    ("yiban.window.from_env", _via_from_env),
    ("web.render.edge_config", _via_render_edge_config),
    ("yiban.engine.schedule._schedule_config", _via_schedule_config),
)


def _is_edge_key(node):
    return isinstance(node, ast.Constant) and node.value in EDGE_KEYS


def _callee_name(func):
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


class _SiteFinder(ast.NodeVisitor):
    """记录「按键名从 env 取值」的位置：`.get(KEY)`、`obj[KEY]`（读）、`_int_or_none(_, KEY)`。

    三条口径（都是刻意的，勿"顺手放宽"）：

    1. 写侧（`updates[KEY] = ...`）与纯键名清单（`_ENV_RELOAD_KEYS` 那类 frozenset
       字面量）不算取值，故不进判定；注释与 docstring 由 AST 天然排除。
    2. 宁误报不漏报：任何以这三个键名调 `.get` 的位置都点名，哪怕只是取值展示——
       那已经是绕过唯一入口取边界值，修法就是改调 `parse_edges`。
    3. 已知盲点：键名经变量传入（`for k in KEYS: env.get(k)`）不进本判定。这种入口
       若被接进任一读者，由 `EdgeValueSingleSourceTest` 的取值表兜住；未接线的副本
       属死码，如实登记，不假称全覆盖。
    """

    def __init__(self, rel):
        self.rel = rel
        self.sites = []
        self._stack = []

    def visit_FunctionDef(self, node):
        self._stack.append(node.name)
        self.generic_visit(node)
        self._stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def _record(self, node, key):
        self.sites.append((self.rel, self._stack[-1] if self._stack else "<module>",
                           node.lineno, key))

    def visit_Call(self, node):
        if _callee_name(node.func) in ("get", "_int_or_none"):
            for arg in list(node.args) + [kw.value for kw in node.keywords]:
                if _is_edge_key(arg):
                    self._record(node, arg.value)
        self.generic_visit(node)

    def visit_Subscript(self, node):
        if isinstance(node.ctx, ast.Load) and _is_edge_key(node.slice):
            self._record(node, node.slice.value)
        self.generic_visit(node)


def _sites_in_source(source, rel):
    """一段源码里的取值位置。位置判据与合成夹具走同一个函数，不搞两套量具。"""
    finder = _SiteFinder(rel)
    finder.visit(ast.parse(source))
    return finder.sites


def _scan_production_tree():
    """扫生产树的 .py，返回（命中位置列表, 扫过的相对路径集合）。"""
    sites, scanned = [], set()
    for root in SCAN_DIRS:
        abs_root = os.path.join(BASE, root)
        if not os.path.isdir(abs_root):
            continue
        for dirpath, dirnames, filenames in os.walk(abs_root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in filenames:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                rel = os.path.relpath(path, BASE).replace(os.sep, "/")
                with open(path, encoding="utf-8") as fh:
                    sites.extend(_sites_in_source(fh.read(), rel))
                scanned.add(rel)
    return sites, scanned


class EdgeValueSingleSourceTest(unittest.TestCase):
    def test_every_reader_returns_the_same_pair(self):
        """同一份 env 输入，各读者必须得到同一个 (front, back)。"""
        for env, expected, note in CASES:
            for name, read in READERS:
                with self.subTest(case=note, reader=name):
                    self.assertEqual(expected, read(env),
                                     "读者 %s 在「%s」下应得 %s" % (name, note, expected))


#: 合成夹具：一份「第二入口」的五种写法。判据必须只点名两处真取值。
SECOND_ENTRY_FIXTURE = '''
# 注释里写 YIBAN_WINDOW_EDGE_SEC，不算取值。
YIBAN_EDGE_ROSTER = frozenset({"YIBAN_WINDOW_EDGE_SEC", "YIBAN_WINDOW_EDGE_FRONT_SEC"})


def edge_from_env(env):
    """docstring 里写 YIBAN_WINDOW_EDGE_BACK_SEC，也不算取值。"""
    updates = {}
    updates["YIBAN_WINDOW_EDGE_SEC"] = "60"
    return env.get("YIBAN_WINDOW_EDGE_SEC")


def edge_from_env_again(env):
    return _int_or_none(env, "YIBAN_WINDOW_EDGE_FRONT_SEC")
'''


class SingleParseEntryTest(unittest.TestCase):
    def test_only_parse_edges_reads_the_edge_keys(self):
        """第二份解析入口（接线与否都算）必须被点名。"""
        sites, _scanned = _scan_production_tree()
        outside = [s for s in sites if (s[0], s[1]) != SINGLE_ENTRY]
        detail = "; ".join("%s:%d 在 %s() 读 %s" % (r, ln, fn, k)
                           for r, fn, ln, k in outside)
        self.assertEqual(
            [], outside,
            "窗口边界的 env 键只准在 yiban/window.py::parse_edges 里读；"
            "以下位置各算各的：" + detail)

    def test_the_fixture_second_entry_gets_named(self):
        """量具自证：合成夹具里的两处真取值必须被点名，三处假形状一处不许点。"""
        sites = _sites_in_source(SECOND_ENTRY_FIXTURE, "fixture.py")
        self.assertEqual(2, len(sites),
                         "夹具该点两处（env.get 与 _int_or_none），实点：%s" % sites)
        self.assertEqual({"YIBAN_WINDOW_EDGE_SEC", "YIBAN_WINDOW_EDGE_FRONT_SEC"},
                         {s[3] for s in sites})
        self.assertEqual(2, len([s for s in sites if (s[0], s[1]) != SINGLE_ENTRY]),
                         "夹具里的第二处解析不该落进许可入口")

    def test_the_gate_is_not_an_empty_gate(self):
        """防废门：扫描面不许缩水，且许可入口确实仍在读这三个键。"""
        for root in SCAN_DIRS:
            self.assertTrue(os.path.isdir(os.path.join(BASE, root)),
                            "%s 不在了，位置判据的扫描面已缩水" % root)
        sites, scanned = _scan_production_tree()
        self.assertGreaterEqual(len(scanned), 50,
                                "只扫到 %d 个文件，位置判据不可信" % len(scanned))
        inside = {s[3] for s in sites if (s[0], s[1]) == SINGLE_ENTRY}
        self.assertEqual(set(EDGE_KEYS), inside,
                         "parse_edges 必须仍在读这三个 env 键，否则本门恒绿")
        for rel in COMMENT_ONLY_MODULES:
            self.assertIn(rel, scanned, "%s 没进扫描面，判据不可信" % rel)
            self.assertEqual([], [s for s in sites if s[0] == rel],
                             "%s 只在注释里写键名，不该被判命中" % rel)


#: docstring 里主张先后次序的说法（出现即必须与实测一致）。
PRECEDENCE_WORDS = ("优先", "覆盖", "取代", "胜过", "压过")


def _precedence_claims(doc):
    """逐句判出 docstring 的次序主张，返回被点名的那一族键名（"新键"或"旧键"）。

    一句里两族都点名、或都没点名时判不出主张，跳过该句。
    """
    claims = []
    for sentence in re.split(r"[。；;\n]", doc):
        if not any(word in sentence for word in PRECEDENCE_WORDS):
            continue
        names_new = ("EDGE_FRONT_SEC" in sentence) or ("EDGE_BACK_SEC" in sentence)
        names_legacy = "YIBAN_WINDOW_EDGE_SEC" in sentence
        if names_new == names_legacy:
            continue
        claims.append("新键" if names_new else "旧键")
    return claims


class DocstringTruthTest(unittest.TestCase):
    def test_docstring_precedence_matches_behavior(self):
        """docstring 主张哪一族键赢，实测就必须是哪一族键赢。"""
        env, expected, _note = BOTH_KEYS_CASE
        winner = "新键" if window.parse_edges(env) == expected else "旧键"
        claims = _precedence_claims(window.parse_edges.__doc__ or "")
        self.assertTrue(claims, "docstring 写不出可判定的次序主张，本条会恒绿")
        for claimed in claims:
            self.assertEqual(winner, claimed,
                             "docstring 主张「%s优先」，实测是「%s」生效" % (claimed, winner))

    def test_the_classifier_has_teeth(self):
        """反空转：判据要抓得住订正前那句假话，也不许给无主张的句子编出主张。"""
        self.assertEqual(["旧键"], _precedence_claims(
            "→ (front_sec, back_sec)。旧键 YIBAN_WINDOW_EDGE_SEC（前后对称）优先映射。"))
        self.assertEqual(["新键"], _precedence_claims(
            "同一边的新键 YIBAN_WINDOW_EDGE_FRONT_SEC / _BACK_SEC 优先。"))
        self.assertEqual([], _precedence_claims(
            "旧键 YIBAN_WINDOW_EDGE_SEC（前后对称）只补对应新键缺席的那一边。"))


#: 出声句式的判据（工单 yz90）。句式取自仓内既有先例
#: `yiban/engine/schedule.py::_env_int`：`配置 <键>=<原值> 超出范围 [<下界>, <上界>] …<采用值>`。
REJECTED_SHAPE = re.compile(
    r"配置 (?P<key>YIBAN_WINDOW_EDGE\w*)=(?P<raw>\S+) 超出范围 "
    r"\[(?P<lo>\d+), (?P<hi>\d+)\].*?(?P<adopted>\d+)(?!.*\d)")
#: 原值无法解析（`6o` 这类）也是"键写了但没生效"，与越界同一条缺陷族。
INVALID_SHAPE = re.compile(
    r"配置 (?P<key>YIBAN_WINDOW_EDGE\w*)=(?P<raw>'[^']*'|\"[^\"]*\") "
    r"非法.*?(?P<adopted>\d+)(?!.*\d)")


def _warn_lines(cm):
    return "\n".join(cm.output)


class EdgeRejectionAnnounceTest(unittest.TestCase):
    """取值被拒必须出声（工单 yz90）。

    判据是"有没有一条告警把键、原值、合法域、最终采用的值说全"，不是字面措辞。
    静默回落的后果是配置与运行不一致：`.env` 写 400，引擎按 60 排计划，日志里没有原因。
    """

    def test_new_key_out_of_range_announces_all_four_facts(self):
        """新键 400 越界：告警要含键名、400、上界 300、采用的 60。"""
        with self.assertLogs("yiban.window", level="WARNING") as cm:
            pair = window.parse_edges({"YIBAN_WINDOW_EDGE_FRONT_SEC": "400"})
        self.assertEqual((window.DEFAULT_EDGE_SEC, window.DEFAULT_EDGE_SEC), pair)
        for token in ("YIBAN_WINDOW_EDGE_FRONT_SEC", "400", "300", "60"):
            self.assertIn(token, _warn_lines(cm), "越界告警缺 %s：%s" % (token, cm.output))

    def test_legacy_key_out_of_its_own_domain_announces_600(self):
        """旧键 700 越界：告警要含旧键自己那一条合法域的上界 600。"""
        with self.assertLogs("yiban.window", level="WARNING") as cm:
            pair = window.parse_edges({"YIBAN_WINDOW_EDGE_SEC": "700"})
        self.assertEqual((window.DEFAULT_EDGE_SEC, window.DEFAULT_EDGE_SEC), pair)
        out = _warn_lines(cm)
        self.assertIn("YIBAN_WINDOW_EDGE_SEC", out)
        self.assertIn("600", out, "旧键告警没写它自己的上界：%s" % cm.output)

    def test_engine_reader_path_announces(self):
        """e2e：人手把 400 写进 `.env`，引擎取配置那一刀就要出声。"""
        with _overlay_env({"YIBAN_WINDOW_EDGE_FRONT_SEC": "400"}), \
                self.assertLogs("yiban.window", level="WARNING") as cm:
            cfg = schedule._schedule_config()
        self.assertEqual(window.DEFAULT_EDGE_SEC, cfg["edge_front_sec"])
        self.assertIn("YIBAN_WINDOW_EDGE_FRONT_SEC", _warn_lines(cm))

    def test_unparseable_value_also_announces(self):
        """原值无法解析（`6o`）与越界同族：键写了、值没生效，同样不许静默。"""
        with self.assertLogs("yiban.window", level="WARNING") as cm:
            pair = window.parse_edges({"YIBAN_WINDOW_EDGE_BACK_SEC": "6o"})
        self.assertEqual((window.DEFAULT_EDGE_SEC, window.DEFAULT_EDGE_SEC), pair)
        self.assertIn("YIBAN_WINDOW_EDGE_BACK_SEC", _warn_lines(cm))

    def test_accepted_and_absent_values_stay_silent(self):
        """合法值与缺席必须不出声：告警不能变成每轮都有的常态噪音。"""
        for env in ({"YIBAN_WINDOW_EDGE_SEC": "600"},
                    {"YIBAN_WINDOW_EDGE_FRONT_SEC": "300", "YIBAN_WINDOW_EDGE_BACK_SEC": "0"},
                    {"YIBAN_WINDOW_EDGE_FRONT_SEC": "", "YIBAN_WINDOW_EDGE_SEC": "60"},
                    {}):
            with self.subTest(env=env), \
                    self.assertNoLogs("yiban.window", level="WARNING"):
                window.parse_edges(env)

    def test_announced_adopted_value_is_the_one_actually_used(self):
        """告警点名的"采用值"必须是真采用的那个：新键被拒、旧键顶上时不许写成默认。"""
        env = {"YIBAN_WINDOW_EDGE_FRONT_SEC": "400", "YIBAN_WINDOW_EDGE_SEC": "120"}
        with self.assertLogs("yiban.window", level="WARNING") as cm:
            pair = window.parse_edges(env)
        self.assertEqual((120, 120), pair)
        matched = [m for m in (REJECTED_SHAPE.search(line) for line in cm.output) if m]
        self.assertEqual(1, len(matched), "新键被拒应恰有一条告警：%s" % cm.output)
        self.assertEqual("120", matched[0]["adopted"],
                         "告警写的采用值与实际生效值不符：%s" % cm.output)

    def test_both_rejection_paths_share_one_shape(self):
        """新旧两条路径同源：同一句式正则各命中一行，且逐格取值对得上。"""
        for env, key, raw, hi in (({"YIBAN_WINDOW_EDGE_FRONT_SEC": "400"},
                                   "YIBAN_WINDOW_EDGE_FRONT_SEC", "400",
                                   str(window.EDGE_MAX_SEC)),
                                  ({"YIBAN_WINDOW_EDGE_SEC": "700"},
                                   "YIBAN_WINDOW_EDGE_SEC", "700",
                                   str(window.LEGACY_EDGE_MAX_SEC))):
            with self.assertLogs("yiban.window", level="WARNING") as cm:
                window.parse_edges(env)
            matched = [m for m in (REJECTED_SHAPE.search(line) for line in cm.output) if m]
            self.assertEqual(1, len(matched), "越界告警句式不统一：%s" % cm.output)
            self.assertEqual(key, matched[0]["key"])
            self.assertEqual(raw, matched[0]["raw"])
            self.assertEqual(hi, matched[0]["hi"])
            self.assertEqual(str(window.DEFAULT_EDGE_SEC), matched[0]["adopted"])
        with self.assertLogs("yiban.window", level="WARNING") as bad_cm:
            window.parse_edges({"YIBAN_WINDOW_EDGE_BACK_SEC": "6o"})
        self.assertEqual(1, len([m for m in (INVALID_SHAPE.search(line)
                                            for line in bad_cm.output) if m]),
                         "非法值句式不统一：%s" % bad_cm.output)


class DomainDivergenceTest(unittest.TestCase):
    """域分叉是**已知、待收口**的：旧键 0~600、新键 0~300（工单 yz90）。

    本类不裁要不要统一，只钉"分叉必须留痕"。有人不动测试就改档位，这里立刻红。
    """

    def test_two_domains_are_named_constants(self):
        """两条上界各有一枚具名常量，不许再把 600 写成裸字面量。"""
        self.assertEqual(300, window.EDGE_MAX_SEC)
        self.assertEqual(600, window.LEGACY_EDGE_MAX_SEC,
                         "旧键上界 600 被改了：统一新旧合法域是产品决定，先裁 yz90")
        with open(os.path.join(BASE, "yiban", "window.py"), encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("LEGACY_EDGE_MAX_SEC",
                      body.split("def parse_edges", 1)[1].split("def retry_hm", 1)[0],
                      "parse_edges 读旧键那行没引用具名上界")

    def test_docstring_registers_both_domains(self):
        """docstring 登记的必须就是实测的上界。"""
        doc = window.parse_edges.__doc__ or ""
        self.assertIn(str(window.EDGE_MAX_SEC), doc, "docstring 没写新键上界")
        self.assertIn(str(window.LEGACY_EDGE_MAX_SEC), doc, "docstring 没写旧键上界")

    def test_web_duplicate_comments_do_not_claim_one_range(self):
        """同族口径注释（web/app.py、web/render.py）不许再对旧键说"范围 0~300"。"""
        for rel in ("web/app.py", "web/render.py"):
            with open(os.path.join(BASE, rel), encoding="utf-8") as fh:
                src = fh.read()
            self.assertNotIn("范围 0~300 秒", src,
                             "%s 还在用一条 0~300 覆盖两族键，旧键的 0~600 没登记" % rel)
            self.assertIn(str(window.LEGACY_EDGE_MAX_SEC), src,
                          "%s 没登记旧键的上界" % rel)


if __name__ == "__main__":
    unittest.main()
