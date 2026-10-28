# -*- coding: utf-8 -*-
"""领取写路径单源门：三条写入口合流于同一条领取写语句。

标签：A · 判据：领取单源

**本门钉的不变式（一句话）**：领取这件事全仓只有一条写路径；三条写入口（兜底常驻 /
网页手动 / 调度器）都经它；抢不到返回空而不是抛错。

**为什么必须有它**：2026-10-16 的测试瘦身批（`c49067fc`，PR #117）删掉了
`tests/test_vs1_claim_atomicity.py`。该文件当时是这条子不变式的**唯一**承重者
（`_ALLOWED_STATE_WRITERS` 名册 ＋ "抢不到就跳过"）。删后无人接替 ⇒ 静默失去覆盖
（AGENTS.md §五.13／§14：不许静默失去覆盖、修根因不留复发路径）。本门按**不变式口径**
重建，不照抄旧测试的编舞口径。

**旧测试里被本门接过的断言（逐条）**：
  · `SingleClaimWriteSiteTest::test_领取写入只有claim_batch一处` → `ClaimWriteSiteTest`
    的 `test_领取写入只有一处`；
  · `::test_state写点全在受控面内_参数化新写点fail_closed` → `test_状态写点全在受控名册内`；
  · `Axis3SkipNotFailureTest`（抢不到回空、不抛异常）→ `LostRaceIsSkipNotFailureTest`；
  · `Axis1AtomicityTest::test_判据与写入在同一条语句里` 的**语句形状**部分（单条
    `UPDATE`、`SET state='claimed'` 与 `WHERE state='pending'` 同句）→ 行为组的
    `test_三条入口发出同一条领取语句`。
  旧测试的轴2（同名重入不放行）与轴4（库异常 fail-closed 哨兵）**不重建**：它们今天
  各有承重者（`tests/test_store_queue.py::ClaimBatchTest::test_second_call_returns_empty`、
  `::test_missing_table_returns_sentinel_with_warning`），重建即冗余。

**本门比旧测试多出来的两面**（旧门自己的洞，本门一并堵上）：
  ① 旧门只扫**函数体**。以**字符串常量**形态存在的第二写路径它看不见——本仓现有一例
     （`scripts/drill_multi_exec.py` 的故障注入复刻）。本门把该面纳入判定，并要求复刻
     显式登记；
  ② 旧门没有任何"三条入口合流"的**行为**判据。本门用真 `executor_v3` 真库对拍三条入口
     发出的领取语句。

**判定手法**：
  · 静态面：AST 扫生产目录（`yiban` / `web` / `scripts`，`tests/` 刻意排除——用例本身
    要写"破坏版领取"当牙），折叠空白/大小写/引号/`+` 拼接后按归一化模式判定；
    名册 fail-closed：值不在判定面内即红，新增写点必须显式登记。
  · 行为面：真临时 sqlite ＋ 真 `executor_v3` ＋ 真 `queue_store`；用 `sqlite3` 的
    `set_trace_callback` 在 **SQL 边界**取语句（不按任何 Python 名字打桩），故"走同一条"
    是按**发出的语句**判的，不是按实现长相判的。上游 HTTP 调用点打桩（本门测的是领取写
    路径，不是网络）。
  · 每条行为用例内部有**前置就绪**断言（"本轮真的发出了领取语句""这一行真的被领到并
    执行"）。少了它，配置写错时"什么都没跑"会被读成绿——本仓踩过两次。

**已知挡不住**（静态面之限，如实登记）：名册内的写点若被喂进一个静态不可见的 `"claimed"`
值，本门不报。故名册只登记本仓既有的收尾/回收写点。

对应实现：yiban/adapters/persistence/queue_store.py（`claim_batch` 的 `UPDATE … RETURNING`）、
    yiban/engine/executor_v3.py（`_claim_one` 是执行体唯一领取点）、
    yiban/app/runner.py（调度器与手动两条入口）、yiban/workers/workers.py（兜底常驻入口）。
关键断言：钉"领取写入全仓一处""三条入口发同一条领取语句""抢不到＝回空不抛错"三条性质；
    不钉任何调用顺序。
依赖：临时 sqlite ＋ 真执行体 ＋ 打桩上游尝试函数与限速三件；无网络、无子进程。
    整文件在本机执行，无 skip。
"""
import ast
import os
import re
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import db  # noqa: E402
from _live_executor_harness import (  # noqa: E402
    DAY,
    ENTRIES,
    ENTRY_SEED,
    OTHER_OWNER,
    OWNER,
    PHONE,
    PHONE2,
    Harness,
    claim_statements,
    data_statements,
    shape,
)

from yiban.adapters.persistence import queue_store  # noqa: E402

# ---- 归一化模式（静态面）----
#: `UPDATE sign_tasks SET state`（折叠空白/引号/大小写之后）
_STATE_WRITE = "updatesign_taskssetstate"
#: 领取写入：SET 侧写死字面量 `claimed`
_CLAIM_WRITE = _STATE_WRITE + "=claimed"
#: 领取判据：WHERE 侧的 pending 门（归一化文本里引号已剥）
_PENDING_GATE = "state=pending"

#: 生产目录（`tests/` 刻意排除：用例要写"破坏版领取"当牙，那是用例的正当形态）
_PROD_DIRS = ("yiban", "web", "scripts")

#: 允许出现 `UPDATE sign_tasks … SET state` 的**非领取**写点（fail-closed 名册）。
#: 只登记本仓既有的收尾/回收写点：它们写 pending/done/failed，值由调用方给或走 CASE。
#: 新增任何 state 写点都必须在此显式登记——值不在判定面内即红。
_ALLOWED_STATE_WRITERS = frozenset({
    ("yiban/adapters/persistence/queue_store.py", "settle_tasks"),    # 收尾：state 由调用方给（done/failed）
    ("yiban/adapters/persistence/queue_store.py", "requeue_task"),    # 重排回落 pending
    ("yiban/adapters/persistence/queue_store.py", "reclaim_tasks"),   # 显式重签：终态/在飞行翻回 pending（兜底锁持有时跳过在飞行）
    ("yiban/adapters/persistence/queue_store.py", "reap_abandoned"),  # 死亡执行体名下在飞行回退 pending
})

#: 唯一的领取写点。
_CLAIM_SITE = ("yiban/adapters/persistence/queue_store.py", "claim_batch")

#: 以**字符串常量**形态存在的第二写路径（故障注入复刻），按 (文件, 常量名) 登记。
#: 靶场的用途就是"复刻一条变异领取"来证明它有牙，故必须复刻；但它**不得**静默存在。
_ALLOWED_STRING_REPLICAS = {
    ("scripts/drill_multi_exec.py", "SHIM_SRC"):
        "靶场故障注入复刻：把领取 SQL 的互斥判据放宽成 state IN ('pending','claimed')，"
        "用于证明靶场用例有牙。只在子进程 sitecustomize 里生效，不参与生产。",
}

#: 行为面判据：一条语句的 **SET 侧**写死 `claimed`（原始文本）。必须锚在 `SET state=` 上：
#: `reap_abandoned` 的 WHERE 侧也含 `state='claimed'`（参数展开后的值），只按值匹配会误认。
_CLAIM_SET_RE = re.compile(r"\bset\s+state\s*=\s*'claimed'", re.I)


def _norm(text):
    """归一化：折叠全部空白、转小写、剥引号。挡大小写/空格/换行/引号改写。"""
    return "".join(ch for ch in text.lower() if ch not in " \t\r\n'\"")


def _fold(node):
    """把表达式折叠成归一化文本；折不动（含变量）返回 None。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _norm(node.value)
    if isinstance(node, ast.JoinedStr):
        parts = []
        for part in node.values:
            folded = _fold(part)
            parts.append(folded if folded is not None else "\x00")  # 占位隔断
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _fold(node.left), _fold(node.right)
        if left is None or right is None:
            return None
        return left + right                  # 折叠 `+` 拼接，令拆开的常量重连
    return None


def scan_source(rel, src):
    """扫一份源码，返回 `(函数写点, 字符串复刻点)`。

    函数写点＝`{(rel, 函数名): 归一化文本块}`，只含出现 `SET state` 写点的函数；
    只看函数**代码**（跳过 docstring），并折叠 `+` 拼接与 f-string 字面段；
    f-string 的变量位用 `\\x00` 隔断，避免模式跨占位假配。

    字符串复刻点＝`{(rel, 常量名)}`，只含**模块级**以字符串常量承载 `SET state='claimed'`
    的赋值（这是"代码藏在字符串里"的面）。
    """
    tree = ast.parse(src, filename=rel)
    sites, replicas = {}, set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        body = node.body
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body = body[1:]                       # 跳过 docstring
        blob = "\x00".join(
            folded for folded in
            (_fold(sub) for stmt in body for sub in ast.walk(stmt))
            if folded is not None)
        if _STATE_WRITE in blob:
            sites[(rel, node.name)] = blob
    for stmt in tree.body:                        # 模块级赋值：只这一层
        if not isinstance(stmt, ast.Assign):
            continue
        for target in stmt.targets:
            if not isinstance(target, ast.Name):
                continue
            folded = _fold(stmt.value)
            if folded is not None and _CLAIM_WRITE in folded:
                replicas.add((rel, target.id))
    return sites, replicas


def scan_repo(dirs=_PROD_DIRS):
    """扫生产目录，返回 `(函数写点, 字符串复刻点, 解析失败文件)`。

    第三个返回值是**fail-closed**用的：解析不了的文件**不许静默跳过**——跳过等于把
    它从判定面里悄悄摘掉（AGENTS.md §十三.29"门看的东西 ＝ 实际改的东西吗"）。
    调用方必须断言它为空。
    """
    sites, replicas, unparsed = {}, set(), []
    for top in dirs:
        root = os.path.join(BASE, top)
        for dirpath, _dirs, files in os.walk(root):
            if os.sep + "test" in dirpath:
                continue
            for name in sorted(files):
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                with open(path, encoding="utf-8") as fh:
                    src = fh.read()
                rel = os.path.relpath(path, BASE).replace(os.sep, "/")
                try:
                    s, r = scan_source(rel, src)
                except (SyntaxError, UnicodeDecodeError):
                    unparsed.append(rel)
                    continue
                sites.update(s)
                replicas |= r
    return sites, replicas, unparsed


# ---------------------------------------------------------------------------
# 静态面 · 领取写入唯一 ＋ 状态写点名册
# ---------------------------------------------------------------------------
class ClaimWriteSiteTest(unittest.TestCase):
    """领取写入必须恰一处，且所有 state 写点必须在显式名册内（fail-closed）。"""

    def test_领取写入只有一处(self):
        sites, _replicas, unparsed = scan_repo()
        self.assertEqual(unparsed, [], "这些生产文件解析不了，判定面已静默缩小：%s" % unparsed)
        claim_sites = {k for k, blob in sites.items() if _CLAIM_WRITE in blob}
        self.assertEqual(
            claim_sites, {_CLAIM_SITE},
            "把 state 置为 claimed 的生产写点必须恰是 claim_batch 一处，且为字面量。"
            "参数化成 `SET state=?` 会让它从判定面消失 ⇒ 红；改大小写/空格/引号/拆常量"
            "也都被归一化挡下。实际：%s" % sorted(claim_sites))

    def test_状态写点全在受控名册内(self):
        sites, _replicas, unparsed = scan_repo()
        self.assertEqual(unparsed, [], "这些生产文件解析不了，判定面已静默缩小：%s" % unparsed)
        allowed = set(_ALLOWED_STATE_WRITERS) | {_CLAIM_SITE}
        extra = sorted(set(sites) - allowed)
        self.assertEqual(
            extra, [],
            "这些函数写了 sign_tasks 的 state 却不在受控名册内：%s。新增 state 写点"
            "（含参数化 `SET state=?`）必须显式登记——值不在判定面内即红。" % extra)
        for key in sorted(_ALLOWED_STATE_WRITERS):
            self.assertIn(key, sites,
                          "名册登记了 %s，但它已不再写 state——名册要同步删" % (key,))
            self.assertNotIn(_CLAIM_WRITE, sites[key],
                             "%s 变成了领取写入（写死 claimed）：领取写点只许 claim_batch"
                             % (key,))

    def test_字符串形态的第二写路径必须登记(self):
        """函数体之外（模块级字符串常量）的领取写路径不得静默存在。"""
        _sites, replicas, _unparsed = scan_repo()
        unregistered = sorted(replicas - set(_ALLOWED_STRING_REPLICAS))
        stale = sorted(set(_ALLOWED_STRING_REPLICAS) - replicas)
        self.assertEqual(
            unregistered, [],
            "这些模块级字符串常量里含一条领取写路径（代码藏在字符串里），且未登记：%s。"
            "故障注入复刻必须显式登记（见 `_ALLOWED_STRING_REPLICAS`）；"
            "非复刻的第二写路径一律不许存在。" % unregistered)
        self.assertEqual(
            stale, [],
            "名册登记了这些字符串复刻点，但它们已不含领取写路径——名册要同步删：%s"
            % stale)

    def test_静态面有牙_新函数写点(self):
        """把一份合成源码喂进同一判定函数：第二条领取写路径必须被抓到。"""
        sneaky = (
            "def steal(conn, phone):\n"
            "    conn.execute(\"UPDATE sign_tasks SET state='claimed', owner='x' \"\n"
            "                 \"WHERE phone=? AND state IN ('pending','claimed')\", (phone,))\n"
        )
        sites, _r = scan_source("yiban/engine/sneaky.py", sneaky)
        self.assertIn(("yiban/engine/sneaky.py", "steal"), sites,
                      "判定函数对'新加的第二条领取写路径'无牙：它必须出现在写点集里，"
                      "才会被 test_领取写入只有一处 判红")
        self.assertIn(_CLAIM_WRITE, sites[("yiban/engine/sneaky.py", "steal")],
                      "判定函数必须认出这是领取写入（写死 claimed）")

    def test_静态面有牙_字符串形态(self):
        """合成一份"代码藏在字符串里"的源码：必须被抓到。"""
        sneaky = (
            "SHIM = r'''\n"
            "def steal(conn):\n"
            "    conn.execute(\"UPDATE sign_tasks SET state='claimed' WHERE state='pending'\")\n"
            "'''\n"
        )
        _s, replicas = scan_source("scripts/fake_drill.py", sneaky)
        self.assertIn(("scripts/fake_drill.py", "SHIM"), replicas,
                      "字符串面判定无牙：模块级字符串里的领取写路径必须被认出来")

    def test_扫空不得当绿_且全部生产文件都解析成功(self):
        """扫面为空时报红——"一个文件都没扫到"不等于"没有第二写点"。

        同时钉住"扫面 ＝ 全部生产 .py 文件"：解析不了的文件**不许静默跳过**，
        跳过等于把它从判定面里悄悄摘掉。
        """
        sites, replicas, unparsed = scan_repo()
        self.assertEqual(unparsed, [],
                         "这些生产文件解析不了，判定面已静默缩小（漏扫＝门有洞）：%s"
                         % unparsed)
        self.assertIn(_CLAIM_SITE, sites, "唯一的领取写点没扫到：判定面已失效（门在空转）")
        self.assertTrue(replicas,
                        "字符串复刻面扫到 0 条：该面已失效（本仓现有 drill 复刻一例）")


# ---------------------------------------------------------------------------
# 行为面 · 三条写入口合流于同一条领取语句
# ---------------------------------------------------------------------------
# 靶场（真库 ＋ 真执行体 ＋ SQL 边界观察面）住在 `tests/_live_executor_harness.py`：
# 本文件与 `tests/test_fallback_elbow_discrimination_e2e.py` 共用同一份角色参数，
# 免得"三条入口的参数"出现第二个定义点。

class ThreeEntriesOneWritePathTest(unittest.TestCase):
    """三条写入口各自真跑一轮：它们发出的领取语句必须**出自同一条写路径**。

    判据基准是**唯一写点直调**时能发出的语句集合。入参不同（肘界下推 / 宽分片 /
    账号允许集）会让同一条语句的**参数**与**可选收窄子句**不同——那是设计，不是另一条
    写路径。故判据是"⊆ 写点模板集"，不是"逐字相同"。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-claimsrc-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, entry):
        """跑一条入口，返回本轮发出的领取语句列表。"""
        with Harness(self.tmp) as h:
            state, offset = ENTRY_SEED[entry]
            h.add_task(state=state, run_at_offset=offset)
            h.run_entry(entry)
            claims = h.claim_statements()
            # 前置就绪：这一轮真的发出了领取语句，且这一行真的被领到并执行。
            # （多通道各自试领 ⇒ 语句数 ≥1；判"真领到"的承重证据是"恰一次执行"。）
            self.assertGreaterEqual(
                len(claims), 1,
                "前置就绪：入口 %s 本轮一条领取语句都没发出——靶场没跑起来，"
                "此时任何'合流'断言都是空转" % entry)
            self.assertEqual(
                h.attempted, [PHONE],
                "前置就绪：入口 %s 领取到的行必须真的交到执行面，且恰一次（实际 %r）"
                % (entry, h.attempted))
            self.assertEqual(h.row()["state"], "done",
                             "前置就绪：这一行必须被这一轮了结（实际 %r）" % (h.row(),))
            return claims

    def _write_site_templates(self):
        """唯一写点**直调**时能发出的领取语句形状集合（`phones=None` 与给允许集两种）。

        这是本用例的判据基准：三条入口发出的语句必须落在**这个集合**里，才算"都经它"。
        """
        out = set()
        with Harness(self.tmp) as h:
            for phone, phones in ((PHONE, None), (PHONE2, [PHONE2])):
                h.add_task(phone=phone, state="pending")
                now = h.fc.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                before = len(h.statements)
                rows = queue_store.claim_batch(OWNER, DAY, (0,), now=now, phones=phones)
                self.assertEqual(len(rows), 1,
                                 "前置：写点直调必须真领到一行（phones=%r）" % (phones,))
                stmts = claim_statements(h.statements[before:])
                self.assertEqual(len(stmts), 1,
                                 "前置：写点直调必须恰发一条领取语句，实际 %d" % len(stmts))
                out.add(shape(stmts[0]))
        return out

    def test_三条入口的领取语句都出自唯一写点(self):
        raw = {entry: self._run(entry) for entry in ENTRIES}
        observed = {entry: {shape(s) for s in stmts} for entry, stmts in raw.items()}
        templates = self._write_site_templates()
        self.assertEqual(len(templates), 2,
                         "唯一写点应当有两种入参形态（不限账号 / 带账号允许集），实际 %d 种"
                         % len(templates))
        for entry, shapes in observed.items():
            self.assertEqual(len(shapes), 1,
                             "入口 %s 本轮发出了不止一种形状的领取语句：%s" % (entry, shapes))
            extra = shapes - templates
            self.assertEqual(
                extra, set(),
                "入口 %s 发出的领取语句不在唯一写点能发出的语句集合内 ⇒ 它走了**另一条**"
                "写路径。多出来的形状：\n%s" % (entry, "\n".join(sorted(extra))))
        self.assertEqual(
            observed["scheduler"], observed["fallback"],
            "调度器与兜底常驻必须发出**逐字同形**的领取语句（两者的差别只在入参："
            "肘界与分片集）。实际：%s" % {e: sorted(observed[e]) for e in ("scheduler", "fallback")})
        manual_only = observed["manual"] - observed["scheduler"]
        self.assertEqual(
            len(manual_only), 1,
            "手动入口与调度器的领取语句差必须恰是**一条收窄子句**（本轮账号允许集），"
            "实际差集：%s" % sorted(manual_only))
        self.assertIn(
            "and phone in (?)", next(iter(manual_only)),
            "手动入口多出来的那条子句必须是账号允许集收窄（只领本轮传进来的那几个账号）；"
            "若它变成别的判据，说明手动走了另一条写路径：%s" % sorted(manual_only))
        for entry, stmts in raw.items():
            text = stmts[0]
            self.assertTrue(_CLAIM_SET_RE.search(text),
                            "入口 %s 的领取语句 SET 侧必须写死 claimed：%r"
                            % (entry, text[:200]))
            self.assertIn("state='pending'", text,
                          "入口 %s 的领取语句 WHERE 侧必须带 pending 判据——"
                          "判据搬走就等于无条件改写：%r" % (entry, text[:200]))
            self.assertIn("owner=", _norm(text),
                          "入口 %s 的领取语句必须就地改写持有者：%r" % (entry, text[:200]))

    def test_行为面发出的语句与唯一写点同形(self):
        """行为面发出的语句必须与静态面认定的唯一写点同形。"""
        sites, _r, _unparsed = scan_repo()
        self.assertIn(_CLAIM_SITE, sites)
        claim_blob = sites[_CLAIM_SITE]
        observed = _norm(self._run("scheduler")[0])
        self.assertIn(_CLAIM_WRITE, claim_blob, "唯一写点的静态文本必须含 SET 侧判据")
        self.assertIn(_PENDING_GATE, claim_blob, "唯一写点的静态文本必须含 WHERE 侧判据")
        self.assertEqual(
            {k for k, b in sites.items() if _CLAIM_WRITE in b}, {_CLAIM_SITE},
            "静态面认定的领取写点必须恰一处")
        self.assertIn(_CLAIM_WRITE, observed,
                      "行为面发出的语句必须含 SET 侧判据（claimed）")
        self.assertIn(_PENDING_GATE, observed,
                      "行为面发出的语句必须含 WHERE 侧判据（pending）")


class SingleStatementPerClaimTest(unittest.TestCase):
    """一次领取只发一条语句：判据与写入之间没有跨语句窗口。

    这是旧 `test_vs1_claim_atomicity.py::Axis1AtomicityTest::test_判据与写入在同一条语句里`
    的**直译**（该断言随整文件删除而失去承重者）。判别力：把领取拆成"先查后写"时，
    语句数变 2 ⇒ 红；把 WHERE 判据搬走 ⇒ 红。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-claimstmt-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_单次领取只发一条语句(self):
        with Harness(self.tmp) as h:
            h.add_task(state="pending")
            now = h.fc.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            before = len(h.statements)
            rows = queue_store.claim_batch(OWNER, DAY, (0,), now=now)
            self.assertEqual(len(rows), 1,
                             "前置：这一条应当被领到，否则后面的断言无意义")
            stmts = data_statements(h.statements[before:])
            self.assertEqual(
                len(stmts), 1,
                "领取发出的语句数必须恰好 1：%d 条就是'比较'与'设置'之间有跨语句窗口"
                "（先查后写），多执行体下两个执行体会同时放行同一账号：%r"
                % (len(stmts), stmts))
            self.assertTrue(stmts[0].lstrip().upper().startswith("UPDATE"),
                            "唯一的领取语句必须是一条 UPDATE，实际：%r" % stmts[0][:80])
            self.assertIn("state='claimed'", stmts[0], "SET 侧必须写入 claimed 态")
            self.assertIn("state='pending'", stmts[0],
                          "WHERE 侧必须带 pending 判据——判据搬走就等于无条件改写")


class LostRaceIsSkipNotFailureTest(unittest.TestCase):
    """抢不到＝跳过（回空），不是失败（不抛异常）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-claimlost-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_调度器与兜底抢不到不抛错且不动别人的行(self):
        """行被别人持有时：本轮照常发领取语句，但不放行、不改归属、不外抛。

        **手动入口不在本用例的范围内**：它的 `reclaim` 是**有界的显式逃生口**
        （用户主动点的那一下对卡死行必须有效），会把在飞行行翻回待领——那是明写的语义，
        不是"抢不到"。三条入口共享同一条写路径这一点由上一条用例钉住。
        """
        for entry in ("scheduler", "fallback"):
            with self.subTest(entry=entry), Harness(self.tmp) as h:
                h.add_task(state="claimed", run_at_offset=-600)
                conn = db.get_conn()
                conn.execute("UPDATE sign_tasks SET owner=? WHERE phone=?",
                             (OTHER_OWNER, PHONE))
                conn.commit()
                try:
                    h.run_entry(entry)
                except Exception as e:
                    self.fail("入口 %s 把'抢不到'透出成了异常（%r）：调用方会把它当"
                              "'失败'而不是'跳过'" % (entry, e))
                self.assertGreaterEqual(
                    len(h.claim_statements()), 1,
                    "前置就绪：入口 %s 必须真的发过领取语句，否则本用例是空转" % entry)
                self.assertEqual(h.attempted, [], "行被别人持有，本轮不该执行任何账号")
                self.assertEqual(h.row()["owner"], OTHER_OWNER,
                                 "别人的行不得被换手")

    def test_抢不到与真的没活同形(self):
        """竞争失败在调用方眼里必须与"确实没活"同形：空列表，不抛异常、不回哨兵。

        这是旧 `Axis3SkipNotFailureTest` 的直译。判别力：把"抢不到"改成回哨兵 `None`
        或抛异常 ⇒ 红——那会让调用方把"这一拍没抢到"读成"库读不通"，
        计入有界放弃闸门，整轮被误判为故障。
        """
        with Harness(self.tmp) as h:
            now = h.fc.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            h.add_task(state="claimed", run_at_offset=-600)
            conn = db.get_conn()
            conn.execute("UPDATE sign_tasks SET owner=? WHERE phone=?",
                         (OTHER_OWNER, PHONE))
            conn.commit()
            lost = queue_store.claim_batch(OWNER, DAY, (0,), now=now)
            h.add_task(phone=PHONE2, state="pending", run_at_offset=3600)   # 真的没活：未到点
            none_due = queue_store.claim_batch(OWNER, DAY, (0,), now=now)
            self.assertIsInstance(
                lost, list,
                "竞争失败不得透出成失败（异常/哨兵）——失败会进重试/告警/退出码面，"
                "语义完全不同：%r" % (lost,))
            self.assertEqual(lost, [], "竞争失败＝跳过，必须回空列表")
            self.assertEqual(
                lost, none_due,
                "竞争失败与真的没活必须同形，调用方无从区分、也不该区分：%r vs %r"
                % (lost, none_due))

    def test_库读不通不答可执行(self):
        """库读不通时领取面回哨兵 `None`，绝不回"可执行"的列表。

        这是"抢不到＝跳过"的另一半：跳过与故障必须是两个可判的值（`[]` vs `None`），
        否则"读不通"会被当"今天没活了"，整轮静默空转。
        """
        with Harness(self.tmp) as h:
            h.add_task(state="pending")
            now = h.fc.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            with (mock.patch.object(queue_store, "_queue_conn",
                                    side_effect=sqlite3.OperationalError("database is locked")),
                  self.assertLogs("yiban.store.queue_store", "WARNING")):
                got = queue_store.claim_batch("x@y:1:a", DAY, (0,), now=now)
            self.assertIsNone(got, "读不通必须回 None 哨兵；回 [] 会与'真的没活'同形")
            self.assertNotEqual(got, [], "None 与 [] 必须是两个可判的值")


if __name__ == "__main__":
    unittest.main(verbosity=2)
