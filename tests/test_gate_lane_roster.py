# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""车道名册守卫：每个测试文件必须**恰好**归一条道，一个都不许两边都不在。

标签：J · 运维：部署/备份/发布
覆盖：工单 vckk 阶段 2-a 的守卫。CI 的 `verify` 此前跑 `-k` 关键词子集，改动落在关键词
    命中面之外时子集绿、全量红，而**不重合本身是静默的**（AGENTS.md §十三 第 29 条）。
    本批把"哪条用例进哪条道"写成数据（`tests/lane-roster.tsv`），本条守卫把数据与
    两件**真实**事实对账：
      ① 真实文件系统扫描（`tests/**/test_*.py`）；
      ② 真实 pytest 收集（`pytest --collect-only`）。
    合规的定义：名册里每个测试文件恰好出现一次，车道只取 `fast`/`slow`/`serial`；
    名册的文件集 == 文件系统扫描集 == pytest 收集集。

    为什么必须"两件真实事实都查"：只查名册本身等于查散文（名册可以自洽而与套件脱节）；
    只查文件系统不查收集，会在"文件在但 pytest 收不到"时假绿（例如 testpath 改窄）。
    两者都查，且要求两者互相一致——任一不一致即红，且**不重合是响亮的**。

    为什么"扫空不得当绿"（AGENTS §30）：扫描或收集因环境问题返回空集时，"名册 == 空集"
    这种断言会**恒真**。故本文件钉死下限（`MIN_FILES`）并要求非空。

关键断言：① 名册与真实文件系统扫描、真实 pytest 收集三方一致；② 扫描/收集不得为空，
    且不得少于 `MIN_FILES`；③ 同一文件不得进两条道；④ 未知车道、重复行、悬空行（文件
    不存在）各自判红；⑤ **本守卫自己必须是 `fast` 行**（否则它自己被漏在快集之外，等于
    没这道门）；⑥ `scripts/dev-verify.sh` 的排除项**由名册派生**（不另写名单）——这样
    "守卫看的东西"与"实际跑的东西"结构上不会分叉；⑦ 检查函数本身做突变自证：喂进
    "新增文件没归类"等合成输入必须判红（不做这条，前六条可能是一组恒真断言）。

依赖：只读文件 + 本机 python/pytest；无网络、无第三方库。
"""
import io
import os
import re
import subprocess
import sys
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROSTER = os.path.join(BASE, "tests", "lane-roster.tsv")
ENTRY = os.path.join(BASE, "scripts", "dev-verify.sh")

#: 合法车道。新增车道必须同批改这里与 scripts/dev-verify.sh 的 read_lane_roster，
#: 否则"车道名未知"判红（这是有意的：车道是判据的一部分）。
LANES = ("fast", "slow", "serial")

#: 扫描/收集的下限（AGENTS §30：扫空不得当绿）。本树实测 299 个测试文件。
#: 下限取 100：真实套件远在其上，环境坏掉/路径写错时远在其下。
MIN_FILES = 100

#: 名册表头里声明的列数（lane / path / 可选理由）
_ROW = re.compile(r"^(fast|slow|serial)\s*\t\s*([^\t]+?)\s*(?:\t.*)?$")


def _scan_test_files():
    """真实文件系统扫描：tests 下所有 `test_*.py`（与 pytest 的收集口径同名）。

    不读名册、不读散文——这个函数是"真实事实"的那一半。
    """
    root = os.path.join(BASE, "tests")
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            if fn.startswith("test_") and fn.endswith(".py"):
                rel = os.path.relpath(os.path.join(dirpath, fn), BASE)
                out.append(rel.replace(os.sep, "/"))
    return sorted(out)


def _collect_test_files():
    """真实 pytest 收集：把 `--collect-only -q` 的 node id 前缀去重成本文件集。

    这一半防的是"文件在盘上但 pytest 收不到"（testpaths/命名口径被改窄）时假绿。
    用与当前解释器同一个 `sys.executable`：口径必须与本套件自身一致。
    """
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "--collect-only", "-q",
         "-p", "no:randomly", "--no-header"],
        cwd=BASE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=600,
    )
    files = set()
    for raw in proc.stdout.decode("utf-8", "replace").splitlines():
        line = raw.strip()
        if "::" not in line or not line.startswith("tests/"):
            continue
        files.add(line.split("::", 1)[0].strip())
    return sorted(files)


def _parse_roster(text):
    """解析名册正文（**纯函数**，便于突变自证）。

    返回 (rows, errors)：rows = [(lane, path), ...]（按出现顺序，含重复）；
    errors = 结构/格式问题的字符串列表（注释行与空行不算）。
    """
    rows, errors = [], []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip("\r")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _ROW.match(line)
        if m is None:
            errors.append("第 %d 行不是 `lane<TAB>path` 形状：%r" % (lineno, line[:80]))
            continue
        rows.append((m.group(1), m.group(2)))
    return rows, errors


def _lane_violations(roster_text, on_disk, collected):
    """纯函数：给名册正文与两件真实事实，返回违规清单（空 = 合规）。

    单独成函数是为了能被**突变自证**调用：喂合成输入必须判出对应的违规。
    判据里不出现任何用例名/文件名清单——名册是唯一名单。
    """
    rows, errors = _parse_roster(roster_text)
    bad = list(errors)
    lane_of = {}
    for lane, path in rows:
        if path in lane_of:
            bad.append("同一文件在名册里出现多次（%s：先 %s 后 %s）"
                       % (path, lane_of[path], lane))
        else:
            lane_of[path] = lane
    named = set(lane_of)
    disk, coll = set(on_disk), set(collected)
    for p in sorted(disk - named):
        bad.append("文件未被归类（在文件系统上但名册里没有）——新增测试文件必须加一行：%s" % p)
    for p in sorted(named - disk):
        bad.append("名册悬空（名册里有但文件系统上没有）——改名/删文件时须同批改名册：%s" % p)
    for p in sorted(disk - coll):
        bad.append("文件在盘上但 pytest 收不到（收集口径与文件命名口径不一致）：%s" % p)
    for p in sorted(coll - disk):
        bad.append("pytest 收到盘上没有的文件（扫描口径漏了它）：%s" % p)
    return bad, rows, lane_of


def _empty_lane_violations(rows):
    """空/残集自证用的纯函数：名册与扫描都必须够厚，且三条道都要有内容。"""
    bad = []
    if len(rows) < MIN_FILES:
        bad.append("名册只有 %d 行（下限 %d）：空集/残集不得当绿" % (len(rows), MIN_FILES))
    return bad


class LaneRosterGuardTest(unittest.TestCase):
    """① 名册与现实三方一致（这是本批的主守卫）。"""

    def setUp(self):
        with io.open(ROSTER, encoding="utf-8") as f:
            self.text = f.read()
        self.disk = _scan_test_files()
        self.collected = _collect_test_files()
        self.rows, self.row_errors = _parse_roster(self.text)
        self.lane_of = {}
        for lane, path in self.rows:
            # 重复行取**首次**出现的车道；重复本身由主断言判红，这里不静默择一
            self.lane_of.setdefault(path, lane)

    # -- 前置：两件真实事实本身不得为空或互相打架 --------------------------
    def test_scan_is_not_empty(self):
        """扫空不得当绿：扫描返回空集时，上面的对账会**恒真**。"""
        self.assertGreaterEqual(
            len(self.disk), MIN_FILES,
            "文件系统扫描只找到 %d 个测试文件（下限 %d）——扫描口径坏了，"
            "此时'名册 == 扫描集'是恒真断言，这道门等于不存在" % (len(self.disk), MIN_FILES))

    def test_collection_is_not_empty(self):
        self.assertGreaterEqual(
            len(self.collected), MIN_FILES,
            "pytest 收集只收到 %d 个测试文件（下限 %d）——收集口径坏了（testpaths？），"
            "对账不可信" % (len(self.collected), MIN_FILES))

    def test_scan_and_collection_agree(self):
        """两个口径必须互相一致；不一致时**必须响亮**，不许取交集后当绿。"""
        self.assertEqual(
            self.disk, self.collected,
            "文件系统扫描与 pytest 收集不一致："
            "盘上多出 %s ／ 收集多出 %s"
            % (sorted(set(self.disk) - set(self.collected)),
               sorted(set(self.collected) - set(self.disk))))

    # -- 主断言 -----------------------------------------------------------
    def test_every_test_file_is_classified_exactly_once(self):
        bad, rows, _ = _lane_violations(self.text, self.disk, self.collected)
        self.assertEqual(bad, [], "车道名册与现实不一致：\n  - " + "\n  - ".join(bad))
        self.assertGreaterEqual(
            len(rows), MIN_FILES,
            "名册只有 %d 行（下限 %d）：空集/残集不得当绿" % (len(rows), MIN_FILES))
        # 三条道都必须有内容（否则"这条道存在"只是文件里的一句话）
        for lane in LANES:
            self.assertGreater(
                len(self._of_lane(lane)), 0,
                "名册里没有任何 %s 行：该车道不存在于数据里，"
                "docs/dev/dev-verify.md 与 scripts/dev-verify.sh 对它的描述就是空话" % lane)

    def _of_lane(self, lane):
        return [p for ln, p in self.rows if ln == lane]

    # -- 守卫自身必须在快集里（否则它自己被漏掉） --------------------------
    def test_this_guard_is_in_the_fast_lane(self):
        """守卫必须在快集里常跑——它自己被归进慢集就等于没这道门。

        这条不是形式主义：本批的整个动机是"静默的不重合"，而"守卫不在任何一条自动轨上"
        正是同一形状。故守卫的**自己那一行**必须是 fast。
        """
        rel = os.path.relpath(os.path.abspath(__file__), BASE).replace(os.sep, "/")
        self.assertEqual(
            self.lane_of.get(rel), "fast",
            "%s 必须由名册归到 fast 道（它要在 CI 与本地 --fast 里常跑）：实得 %r"
            % (rel, self.lane_of.get(rel)))

    # -- 名册是排除项的唯一事实源 -----------------------------------------
    def test_entry_script_derives_exclusions_from_the_roster(self):
        """`scripts/dev-verify.sh` 的排除项必须**由名册派生**，不许另写一份名单。

        这是"守卫看的东西 == 实际改的东西"（AGENTS §十三 第 29 条）在本批的落点：
        名册既驱动实跑（--ignore 参数），又驱动本守卫。若脚本内另写一份硬编码文件名单，
        两者会漂移，而漂移是静默的。
        """
        with io.open(ENTRY, encoding="utf-8") as f:
            src = f.read()
        self.assertIn('LANE_ROSTER_REL="tests/lane-roster.tsv"', src,
                      "入口脚本没有把名册路径写成单一常量：排除项的事实源就不止一处了")
        self.assertRegex(
            src, r"(?m)^read_lane_roster$",
            "入口脚本没有在顶层调用 read_lane_roster：名册读了也可能没被用上")
        self.assertRegex(
            src, r"(?m)^build_lane_ignore_args$",
            "入口脚本没有在顶层调用 build_lane_ignore_args：--ignore 参数没由名册派生")
        # 装配点：--ignore 只能由名册数组逐条装配（硬编码文件名会在这里露出来）
        build = [ln for ln in src.splitlines()
                 if 'LANE_IGNORE_ARGS+=("--ignore=' in ln]
        self.assertEqual(len(build), 1,
                         "LANE_IGNORE_ARGS 的装配点应只有一处、且从名册数组取值")
        for arr in ("LANE_SLOW_FILES", "LANE_SERIAL_FILES"):
            self.assertIn(arr, src,
                          "入口脚本没有 %s：名册的一条道没有被用上" % arr)
        # 真跑命令必须拼这个数组（拼了不引用＝排除项不生效，且是静默的）
        self.assertIn('LANE_IGNORE_ARGS[@]', src,
                      "入口脚本没有任何地方引用 LANE_IGNORE_ARGS：排除项装配了但没生效")


class LaneRosterCheckerMutationTest(unittest.TestCase):
    """② 检查函数自身的突变自证：合成输入必须判出对应违规。

    为什么要有这一组：上面 ① 的断言若因某种原因恒真（例如解析正则吞掉一切、或对账方向
    写反），整道门就是摆设。这里喂**必须判红**的合成输入，把恒真排除掉。
    """

    DISK = ("tests/test_a.py", "tests/test_b.py", "tests/protocol/test_c.py")
    COLL = DISK

    def _text(self, *rows):
        return "\n".join(rows) + "\n"

    def test_green_baseline(self):
        bad, _, _ = _lane_violations(
            self._text("fast\ttests/test_a.py", "fast\ttests/test_b.py",
                       "slow\ttests/protocol/test_c.py"),
            self.DISK, self.COLL)
        self.assertEqual(bad, [], "合规名册被判红：判据写反了？")

    def test_new_file_without_a_row_is_red(self):
        """核心突变：新增一个测试文件而没归类 ⇒ 必须红（本批的立项理由）。"""
        disk = (*self.DISK, "tests/test_newly_added.py")
        bad, _, _ = _lane_violations(
            self._text("fast\ttests/test_a.py", "fast\ttests/test_b.py",
                       "slow\ttests/protocol/test_c.py"),
            disk, disk)
        self.assertTrue(any("未被归类" in b for b in bad),
                        "新增文件没归类必须判红，实得：%r" % bad)


    def test_stale_row_is_red(self):
        bad, _, _ = _lane_violations(
            self._text("fast\ttests/test_a.py", "fast\ttests/test_b.py",
                       "slow\ttests/protocol/test_c.py", "slow\ttests/test_gone.py"),
            self.DISK, self.COLL)
        self.assertTrue(any("悬空" in b for b in bad),
                        "名册指向不存在的文件必须判红，实得：%r" % bad)

    def test_duplicate_row_is_red(self):
        bad, _, _ = _lane_violations(
            self._text("fast\ttests/test_a.py", "slow\ttests/test_a.py",
                       "fast\ttests/test_b.py", "slow\ttests/protocol/test_c.py"),
            self.DISK, self.COLL)
        self.assertTrue(any("出现多次" in b for b in bad),
                        "同一文件进两条道必须判红，实得：%r" % bad)

    def test_unknown_lane_is_red(self):
        bad, _, _ = _lane_violations(
            self._text("fast\ttests/test_a.py", "sometimes\ttests/test_b.py",
                       "slow\ttests/protocol/test_c.py"),
            self.DISK, self.COLL)
        self.assertTrue(any("不是 `lane<TAB>path` 形状" in b for b in bad),
                        "未知车道必须判红（车道名是判据的一部分），实得：%r" % bad)

    def test_collection_narrowing_is_red(self):
        """收集口径被改窄（盘上有、pytest 收不到）必须红，不许取交集后当绿。"""
        bad, _, _ = _lane_violations(
            self._text("fast\ttests/test_a.py", "fast\ttests/test_b.py",
                       "slow\ttests/protocol/test_c.py"),
            self.DISK, ["tests/test_a.py"])
        self.assertTrue(any("pytest 收不到" in b for b in bad),
                        "收集口径变窄必须判红，实得：%r" % bad)

    def test_empty_roster_is_red_and_not_silently_green(self):
        """扫空不得当绿：空名册 + 空扫描不得判绿（两条上限都要挡）。"""
        bad, rows, _ = _lane_violations("", [], [])
        self.assertEqual(rows, [])
        self.assertEqual(bad, [],
                         "空名册对空扫描在**对账**层面确实无差异——所以下限必须由"
                         "独立的一条来挡（见下），不能只靠对账")
        self.assertTrue(_empty_lane_violations(rows),
                        "空/残集名册必须被判红：下限断言失效会让'名册==空集'恒真")



if __name__ == "__main__":
    unittest.main()
