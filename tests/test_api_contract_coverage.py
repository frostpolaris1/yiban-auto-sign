# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""API 契约覆盖率守卫：`docs/dev/api-contract.md` 与真实路由表逐条对账。

标签：G · 安全：脱敏/审计/配置注入
覆盖：前后端解耦后，`docs/dev/api-contract.md` 是前端唯一依据。本守卫把两份**真实
    事实**对账：
      ① `create_app().url_map`（后端实际注册的每一条「方法 + 路径」）；
      ② 契约文档里每条端点小节标题（`#### `METHOD /path``）。
    合规定义：两侧集合**相等**。少一条（文档漏写）即红并点名；多一条（文档写了个
    不存在的路径）也即红并点名。

    为什么必须两侧都判：只判"文档 ⊇ 路由"会放过文档里凭空多写的端点（前端照着调，
    必然 404）；只判"路由 ⊇ 文档"会放过漏写（前端不知道这个接口存在）。两个方向
    都是静默失败，故两侧各自成一条断言。

    为什么"扫空不得当绿"（AGENTS §30）：url_map 读到 0 条、或文档解析出 0 条时，
    "两侧相等"这种断言会**恒真**。故先钉下限（`MIN_ENDPOINTS`），两侧都必须非空
    且不少于下限。解析器对空文本返回 0 条这一行为由用例自身突变自证。

关键断言：① url_map 非空且不少于 `MIN_ENDPOINTS`；② 文档解析端点非空且不少于
    `MIN_ENDPOINTS`；③ 路由 ⊆ 文档；④ 文档 ⊆ 路由；⑤ 解析器对空文本回 0 条、
    能点名合成输入里的缺失/多余项（不做这条，前四条可能是一组恒真断言）；⑥ 契约
    文档必须在 git 索引里（`docs/dev/*` 默认被 gitignore，未 `git add -f` 则前端
    拿不到——这是"入库自证"，不是可选项）。

依赖：进程内加载 `web/app.py`（importlib 隔离）+ 只读契约文档 + `git ls-files`。
    无网络、无第三方库、不起常驻服务器。
"""
import contextlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTRACT = os.path.join(BASE, "docs", "dev", "api-contract.md")
CONTRACT_RELPATH = "docs/dev/api-contract.md"

#: 两侧的下限（AGENTS §30：扫空不得当绿）。本树实测 101 条。
#: 下限取 50：真实路由表远在其上，环境坏掉/正则写错时远在其下。
MIN_ENDPOINTS = 50

#: 端点小节的标题形态：`#### `METHOD /path``。方法大写；路径里可含 Flask 转换器
#: （`<int:idx>`）与静态段（`<path:filename>`）。
_ENDPOINT_HEADING = re.compile(
    r"^####\s+`(GET|POST|PUT|DELETE|PATCH)\s+(\S+)`\s*$", re.MULTILINE)

#: fenced code block（``` 或 ~~~）：解析前先剔除，避免文档里的**示例**被当成端点。
_FENCE = re.compile(r"^(```|~~~).*?^\1\s*$", re.MULTILINE | re.DOTALL)

#: Flask 自动补的方法（不是视图声明的），对账时剔除。
_AUTO_METHODS = frozenset({"HEAD", "OPTIONS"})


def _strip_fences(text):
    """剔除围栏代码块：示例代码里的 `METHOD /path` 不得被当成端点小节。"""
    return _FENCE.sub("", text)


def parse_contract_endpoints(text):
    """契约文档文本 → `{(方法, 路径), …}`。

    只认 `#### `METHOD /path`` 形态的小节标题（先剔除围栏代码块）。空文本回空集
    ——调用方必须把空集判红（扫空不得当绿）。
    """
    return {(m.group(1), m.group(2))
            for m in _ENDPOINT_HEADING.finditer(_strip_fences(text))}


def url_map_endpoints(app):
    """Flask app → `{(方法, 路径), …}`（剔除 HEAD / OPTIONS）。

    一条 rule 的多个方法各自成一项。rule 只有自动方法时按 GET 计（Flask 不会产生
    这种 rule，兜底防漏）。
    """
    out = set()
    for rule in app.url_map.iter_rules():
        methods = {m for m in rule.methods if m not in _AUTO_METHODS}
        if not methods:
            methods = {"GET"}
        for method in methods:
            out.add((method, rule.rule))
    return out


def diff_endpoints(route_set, doc_set):
    """→ `(missing_in_doc, extra_in_doc)`，两者都是排序后的列表。

    纯函数：突变自证用例直接喂合成集合，不依赖真实 app 或真实文档。
    """
    missing = sorted(route_set - doc_set)
    extra = sorted(doc_set - route_set)
    return missing, extra


def _fmt(items):
    return "\n".join("  %s %s" % (m, p) for m, p in items)


class ApiContractCoverageTest(unittest.TestCase):
    """契约文档与 url_map 双向对账 + 解析器突变自证 + 入库自证。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-api-contract-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write("YIBAN_ACCOUNTS_KEY=%s\n" % ("a" * 64))
        cls._old_env = {k: os.environ.get(k) for k in (
            "YIBAN_ENV_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR",
            "YIBAN_ACCOUNTS_FILE", "YIBAN_USERS_FILE", "YIBAN_ACCOUNTS_KEY",
            "YIBAN_LOG_FILE")}
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_DB_FILE"] = os.path.join(cls.tmp, "yiban.db")
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_ACCOUNTS_FILE"] = os.path.join(cls.tmp, "accounts.json")
        os.environ["YIBAN_USERS_FILE"] = os.path.join(cls.tmp, "users.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = "a" * 64
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        spec = importlib.util.spec_from_file_location(
            "webapp_apicontract", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_apicontract"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k, v in cls._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # ------------------------------------------------------------------
    # 1. 扫空不得当绿（两侧下限）
    # ------------------------------------------------------------------
    def test_url_map_is_non_empty(self):
        """url_map 必须非空且不少于下限（否则"两侧相等"会恒真）。"""
        got = url_map_endpoints(self.webapp.create_app())
        self.assertGreaterEqual(
            len(got), MIN_ENDPOINTS,
            "url_map 只读到 %d 条端点（下限 %d）——路由装配坏了或环境不对，"
            "此时“两侧相等”是恒真断言，不得当绿" % (len(got), MIN_ENDPOINTS))

    def test_contract_is_non_empty(self):
        """契约文档必须解析出非空且不少于下限的端点（防文档被清空后静默当绿）。"""
        with open(CONTRACT, encoding="utf-8") as f:
            got = parse_contract_endpoints(f.read())
        self.assertGreaterEqual(
            len(got), MIN_ENDPOINTS,
            "契约只解析出 %d 条端点（下限 %d）——文档被清空或标题形态改了，"
            "此时“两侧相等”是恒真断言，不得当绿" % (len(got), MIN_ENDPOINTS))

    # ------------------------------------------------------------------
    # 2. 双向对账
    # ------------------------------------------------------------------
    def test_every_route_is_documented(self):
        """url_map 里每一条「方法 + 路径」都必须在契约里出现（少一条即红并点名）。"""
        routes = url_map_endpoints(self.webapp.create_app())
        with open(CONTRACT, encoding="utf-8") as f:
            documented = parse_contract_endpoints(f.read())
        missing, _extra = diff_endpoints(routes, documented)
        self.assertEqual(
            missing, [],
            "契约漏写 %d 条端点（后端已注册但 %s 里没有）：\n%s"
            % (len(missing), CONTRACT_RELPATH, _fmt(missing)))

    def test_every_documented_endpoint_exists(self):
        """契约里每条端点都必须存在于 url_map（防文档写了个不存在的路径）。"""
        routes = url_map_endpoints(self.webapp.create_app())
        with open(CONTRACT, encoding="utf-8") as f:
            documented = parse_contract_endpoints(f.read())
        _missing, extra = diff_endpoints(routes, documented)
        self.assertEqual(
            extra, [],
            "契约写了 %d 条 url_map 里不存在的端点（前端照调必然 404）：\n%s"
            % (len(extra), _fmt(extra)))

    # ------------------------------------------------------------------
    # 3. 解析器突变自证（不做这条，前四条可能是一组恒真断言）
    # ------------------------------------------------------------------
    def test_parser_mutations_are_detected(self):
        """解析器与差分器做突变自证：空文本回 0 条；删一条 ⇒ 点名缺失；加假路径 ⇒ 点名多余。"""
        # 空文本必须回空集（"扫空不得当绿"的底座）
        self.assertEqual(parse_contract_endpoints(""), set(),
                         "空文档必须解析出 0 条（否则“扫空”会被当成合规）")
        # 只有散文、没有端点小节 → 同样 0 条
        self.assertEqual(
            parse_contract_endpoints("## 标题\n\n只有正文，没有端点小节。\n"), set(),
            "没有 `#### `METHOD /path`` 小节时不得凭空产出端点")
        # 围栏代码块里的示例不得被当成端点
        self.assertEqual(
            parse_contract_endpoints("```\n#### `GET /api/fake`\n```\n"), set(),
            "围栏代码块里的示例不得被当成端点小节")

        base = ("#### `GET /api/a`\n\n- x\n\n"
                "#### `POST /api/b`\n\n- y\n")
        routes = {("GET", "/api/a"), ("POST", "/api/b")}
        self.assertEqual(parse_contract_endpoints(base), routes,
                         "解析器必须逐字还原标题里的方法与路径")

        # 突变 1：从契约删掉一条 → 差分器点名"文档缺失"
        removed = base.replace("#### `POST /api/b`\n\n- y\n", "")
        missing, extra = diff_endpoints(routes, parse_contract_endpoints(removed))
        self.assertEqual(missing, [("POST", "/api/b")],
                         "删掉一条端点后必须点名缺失项")
        self.assertEqual(extra, [])

        # 突变 2：给契约加一条假路径 → 差分器点名"文档多余"
        added = base + "\n#### `GET /api/nope`\n\n- z\n"
        missing, extra = diff_endpoints(routes, parse_contract_endpoints(added))
        self.assertEqual(missing, [])
        self.assertEqual(extra, [("GET", "/api/nope")],
                         "加一条假路径后必须点名多余项")

    # ------------------------------------------------------------------
    # 4. 入库自证（docs/dev/* 默认被 gitignore；未入库 = 前端拿不到）
    # ------------------------------------------------------------------
    def test_contract_is_tracked_in_git(self):
        """契约文档必须在 git 索引里——否则它只在本地，前端拿不到（AGENTS §24 同族）。"""
        try:
            proc = subprocess.run(
                ["git", "ls-files", "--error-unmatch", CONTRACT_RELPATH],
                cwd=BASE, capture_output=True, text=True)
        except OSError as e:
            self.fail("无法调用 git（%s）——入库自证无法执行，按红处理（跳过 ≠ 绿）" % e)
        self.assertEqual(
            proc.returncode, 0,
            "%s 不在 git 索引里（`git ls-files --error-unmatch` 退出码 %d，stderr=%s）"
            "——docs/dev/* 默认被 gitignore，必须加白名单并 `git add -f`，"
            "否则文件只在本地，前端拿不到"
            % (CONTRACT_RELPATH, proc.returncode, proc.stderr.strip()))


if __name__ == "__main__":
    unittest.main()
