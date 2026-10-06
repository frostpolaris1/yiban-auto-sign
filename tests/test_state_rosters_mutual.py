# -*- coding: utf-8 -*-
"""状态件名册互校：单一权威名册与其余各册的强制对照（census-P1-1）。

标签：D · 状态词汇与账号生命周期

**唯一数据源**
"哪些文件算状态件"的机读权威名册是 `scripts/gate/shared-facts.tsv` 的
`状态件` 族（`gxf4` 立，PR #53）。本文件**不新建第二份名册**：它只读该 tsv，
再把其余各册逐枚对照过去。任一册漂移即红，并点名"哪一册、缺/多了哪一枚"。

**被对照的各册（逐枚归属）**
- R1 清理名册 `yiban/state_gc.py` 的 `ARTIFACTS`（按日件，会被清理）。
- R2 备份收录名册 `scripts/backup.sh` 的 `state_files` + `log_files`。
- R4 测试侧负名册 `tests/test_state_gc.py` 的 `ALLOWED_NON_STATE`。
- R6 文档固定名表 `docs/dev/README.md` 的"运行期状态文件"表。
- R7 状态目录键双源：`scripts/backup.sh` 的 `YIBAN_STATE_DIR` / `SIGN_STATE_DIR`。

**为什么必须互校而不是物理合并**
三册语义不同（清理 / 备份 / 豁免），合并会改掉各自的口径；但"同一事实零互校"
会让新增一枚状态件只登记进其中一册（`mail-user-fail-` 就是清而不备的实例）。
故本文件钉的是**关系**：各册都必须是权威名册的投影，差额必须逐枚写明理由。

**为什么 R1/R2 的差额要冻结成表**
`state_gc` 的按日件与 `backup.sh` 的收录件本就不是同一集合（按日件才清、
非按日件才长期留）。差额不是缺陷，但**差额漂移无人发现**是缺陷。故把今天的
差额逐枚写进本文件；任一册增删一枚，断言即红，写表的人必须同时给理由。

依赖：只读文件文本与正则；不跑子进程、不触网、不改任何落点。
"""
import io
import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TSV = os.path.join(BASE, "scripts", "gate", "shared-facts.tsv")
STATE_FAMILY = "状态件"
COLUMNS = ("族", "键", "状态", "判定模式", "扫描范围", "允许上限", "口径备注")

#: R1 与 R2 的差额（今天实测）。两张表是**冻结值 + 逐枚理由**，不是名册。
#: R1 清而不备 5 枚：按日件的价值只在当天，恢复后重签一次即可，故不入备份。
CLEANED_NOT_BACKED_UP = {
    "mail-user-fail-": "用户失败提醒额度账本：恢复后当日额度归零会重复轰炸——"
                       "属已知待裁项（本单只照出，收口见报告）",
    "sign-status-": "run.sh 当日库内事实交叉核对件：跨日无意义，恢复后重算",
    "yiban-run-today-": "run.sh 当日触发标记：跨日自动失效，恢复后重判即可",
    "yiban-settled-": "run.sh 当日收尾标记：跨日自动失效，恢复后重判即可",
    "wire-": "线路落盘诊断样本（默认关闭）：诊断件，非恢复必需",
}
#: R2 备而不清 4 枚：均非按日（固定名 + 覆盖写），无过期概念，故不进清理名册。
BACKED_NOT_CLEANED = {
    "cred-state.json": "熔断状态：固定名覆盖写；空内容时另有 sweep_empty_cred_state 兜底",
    "notify-ledger.json": "推送额度账本：固定名覆盖写，跨日仍有效",
    "notify-throttle.json": "推送节流表：固定名覆盖写，跨日仍有效",
    "audit-anchor.log": "审计链外部锚点：唯一外部参照，永久保留（只增不删）",
}
#: 权威名册今天**未登记**的固定名状态件（"非按日命名"的结构性缺口）。
#: 这三枚仓里确有写者，但 census 状态件族只有 20 枚键、未收录它们；补登记要改
#: tsv 的覆盖边界与 census 覆盖声明（`SharedFactsCoverageClaimTest` 钉着 20），
#: 属另一单的范围。本单只把缺口照出来：本表与权威名册必须**互补**，谁登记了谁红。
UNREGISTERED_FIXED_NAME = {
    "capacity-measure.json": "写者 web/services/measure.py:44（现场实测冷却占位）；"
                             "census 状态件族未收录（非按日命名天然逃过元测试）",
    "sched-heartbeat.json": "写者 docker/scheduler.py:86（容器健康检查心跳）；"
                            "census 状态件族未收录",
    "cleanup.log": "写者 scripts/state_cleanup.py:91（清理日志）；"
                   "census 状态件族未收录",
}
#: 名字像状态件、但不是状态件的固定名（扫描误命中，逐条给理由）。
NON_STATE_FIXED_NAME = {
    "accounts.json": "scripts/db_export.py 导出的账号清单（仓外产物，不在状态目录）",
    "users.json": "scripts/db_export.py 导出的用户清单（仓外产物，不在状态目录）",
    "manifest.json": "web/services/vue_assets.py 的前端构建产物清单（静态资源，非状态件）",
}
#: 固定名状态件的枚举判据：抓"小写 token + 状态件扩展名"的字面量。
#: 与 `tests/test_state_gc.py` 的按日扫描互补——非按日件从原理上逃过按日正则。
_FIXED_NAME_RE = re.compile(
    r'["\']([a-z][a-z0-9-]+\.(?:json|jsonl|log|txt|marker))["\']')
#: R7：状态目录键的两套拼写。应用侧统一键是前者；后者仅作旧部署回退。
STATE_DIR_KEY = "YIBAN_STATE_DIR"
LEGACY_STATE_DIR_KEY = "SIGN_STATE_DIR"


def _read(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def _rows():
    rows = []
    for raw in _read(TSV).splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        cells = raw.split("\t")
        assert len(cells) == len(COLUMNS), f"名册列数不符: {raw!r}"
        rows.append(dict(zip(COLUMNS, cells, strict=True)))
    return rows


def _state_rows():
    return [r for r in _rows() if r["族"] == STATE_FAMILY]


def _state_text():
    """状态件族的 键 + 判定模式 全文（反斜杠归一），供字面量成员判定用。"""
    return "\n".join(r["键"] + "\t" + r["判定模式"] for r in _state_rows()).replace("\\", "")


def _registered(token):
    """→ token 是否被权威名册登记：状态件族字面量在册，或命中任一行判定模式 ERE。

    模式面取**全族**（不只状态件族）：`sign.log` 这类基名登记在路径配置族，
    但它确实是状态目录里的件；单一数据源是整张 tsv，不是它的某一族。
    """
    if token in _state_text():
        return True
    for r in _rows():
        if r["状态"] != "active":
            continue
        try:
            if re.search(r["判定模式"], token):
                return True
        except re.error:
            continue
    return False


def _artifacts_prefixes():
    """R1：`yiban/state_gc.py` 的 `ARTIFACTS` 元组里的前缀（按定义顺序）。"""
    src = _read(os.path.join(BASE, "yiban", "state_gc.py"))
    body = src.split("ARTIFACTS = (", 1)[1].split("\n)", 1)[0]
    return re.findall(r'Artifact\(\s*"([^"]+)"', body)


def _backup_entries():
    """R2：`scripts/backup.sh` 的 state_files + log_files 收录项（去引号后的 glob）。"""
    src = _read(os.path.join(BASE, "scripts", "backup.sh"))
    names = []
    for var in ("state_files=(", "log_files=("):
        start = src.index(var) + len(var)
        arr = src[start:src.index(")", start)]
        names += re.findall(r'/\*?"?([^"/\s)]+)"?', arr)
    return names


def _backup_core(glob):
    """glob → 用于对照的件身份：通配项取 `*` 前的字面前缀，固定名原样。"""
    return glob[: glob.index("*")] if "*" in glob else glob


def _allowed_non_state():
    """R4：`tests/test_state_gc.py` 的 `ALLOWED_NON_STATE` 键集合。"""
    src = _read(os.path.join(BASE, "tests", "test_state_gc.py"))
    body = src.split("ALLOWED_NON_STATE = {", 1)[1].split("\n}", 1)[0]
    return set(re.findall(r'^\s*"([^"]+)":', body, re.M))


def _docs_fixed_names():
    """R6：`docs/dev/README.md` 的"运行期状态文件"表里点名的文件名。"""
    src = _read(os.path.join(BASE, "docs", "dev", "README.md"))
    body = src.split("（固定名，条数不随时间增长）", 1)[1].split("###", 1)[0]
    return set(re.findall(r"`([a-z][a-z0-9-]*\.(?:json|jsonl|log|txt|marker))`", body))


class AuthoritativeRosterTest(unittest.TestCase):
    """权威名册自身可读、且是唯一数据源。"""

    def test_authoritative_roster_is_the_shared_facts_tsv(self):
        self.assertTrue(os.path.isfile(TSV), "权威名册缺失：状态件事实必须机读")
        self.assertGreaterEqual(len(_state_rows()), 20,
                                "状态件族登记行太少，覆盖不成门")
        self.assertTrue(_registered("cred-state.json"))
        self.assertTrue(_registered("probe-state.json"))


class RosterIsProjectionOfAuthoritativeTest(unittest.TestCase):
    """各册必须是权威名册的投影：漂移即红，并点名哪一册哪一枚。"""

    def test_r1_cleanup_roster_prefixes_are_registered(self):
        """R1：`ARTIFACTS` 每枚前缀必须在权威名册登记（新增按日件不许绕过名册）。"""
        for prefix in _artifacts_prefixes():
            with self.subTest(prefix=prefix):
                self.assertTrue(
                    _registered(prefix),
                    f"R1 清理名册（state_gc.ARTIFACTS）多了未登记的前缀 {prefix!r}——"
                    f"它不在权威名册 shared-facts.tsv 的状态件族里")

    def test_r2_backup_roster_entries_are_registered(self):
        """R2：`backup.sh` 每枚收录项必须在权威名册登记。"""
        for glob in _backup_entries():
            core = _backup_core(glob)
            with self.subTest(glob=glob):
                self.assertTrue(
                    _registered(core) or _registered(glob),
                    f"R2 备份收录名册（backup.sh）多了未登记的件 {glob!r}"
                    f"（核心 {core!r}）——它不在权威名册的状态件族里")

    def test_r4_negative_roster_entries_are_not_cleaned_daily_files(self):
        """R4：豁免名册的条目不得是一枚真按日状态件的前缀（否则豁免把真件挡在清理名册外）。

        对照面是 R1（按日清理名册）而不是"全部登记前缀"：`worker-alive-` 这类
        **非按日**状态件本就该被按日元测试豁免，它不是缺陷。
        """
        cleaned = set(_artifacts_prefixes())
        for prefix in sorted(_allowed_non_state()):
            with self.subTest(prefix=prefix):
                self.assertNotIn(
                    prefix, cleaned,
                    f"R4 豁免名册（ALLOWED_NON_STATE）的 {prefix!r} 恰好是 R1 清理名册"
                    f"里一枚真按日件的前缀——豁免把真件挡在清理名册外")

    def test_r6_docs_fixed_names_are_registered_or_declared_gap(self):
        """R6：文档固定名表点名的件必须在权威名册登记，或在缺口表里逐枚交代。"""
        for name in sorted(_docs_fixed_names()):
            with self.subTest(name=name):
                self.assertTrue(
                    _registered(name) or name in UNREGISTERED_FIXED_NAME,
                    f"R6 文档固定名表（docs/dev/README.md）点名了 {name!r}，"
                    f"但它既不在权威名册的状态件族里、也不在缺口表里")

    def test_declared_gaps_are_really_unregistered(self):
        """缺口表与权威名册必须互补：谁把缺口登记进名册，本表就得同步清掉。"""
        for name in sorted(UNREGISTERED_FIXED_NAME):
            with self.subTest(name=name):
                self.assertFalse(
                    _registered(name),
                    f"缺口表里的 {name!r} 已进权威名册——本表必须同步删除，"
                    f"否则缺口表成了过期台账")


class FixedNameStateIsEnumeratedTest(unittest.TestCase):
    """非按日命名（逃逸 ⑤）的按目录枚举判据：固定名件逐枚必须有交代。"""

    def _fixed_names(self):
        """扫代码目录，收集"小写 token + 状态件扩展名"的字面量。"""
        found = set()
        for sub in ("scripts", "docker", "web", "yiban"):
            for dirpath, _dirs, files in os.walk(os.path.join(BASE, sub)):
                if "__pycache__" in dirpath:
                    continue
                for name in files:
                    if not name.endswith((".py", ".sh")):
                        continue
                    with io.open(os.path.join(dirpath, name), encoding="utf-8",
                                 errors="ignore") as f:
                        found |= set(_FIXED_NAME_RE.findall(f.read()))
        return found

    def test_every_fixed_name_is_registered_declared_or_non_state(self):
        """每枚固定名件必须落进三档之一：已登记 / 缺口表 / 非状态件豁免。"""
        found = self._fixed_names()
        # 防扫描器失效：目录改名/扩展名白名单改坏会让下面的断言恒真（假绿）
        self.assertIn("probe-state.json", found, "扫描器失效：连在册的固定名都扫不到")
        self.assertIn("cred-state.json", found)
        for name in sorted(found):
            with self.subTest(name=name):
                self.assertTrue(
                    _registered(name) or name in UNREGISTERED_FIXED_NAME
                    or name in NON_STATE_FIXED_NAME,
                    f"固定名 {name!r} 没有交代：它既不在权威名册、也不在缺口表、"
                    f"也不在非状态件豁免表——非按日件会就此无界增长且无人发现")

    def test_non_state_exemptions_are_not_registered_state_files(self):
        """非状态件豁免表与权威名册互补（谁被登记进名册，豁免就得清掉）。"""
        for name in sorted(NON_STATE_FIXED_NAME):
            with self.subTest(name=name):
                self.assertFalse(_registered(name),
                                 f"{name!r} 已是权威名册里的状态件，豁免表必须清掉")


class RosterDifferenceIsFrozenTest(unittest.TestCase):
    """R1 与 R2 的差额逐枚冻结：任一册增删一枚即红，改表必须同时给理由。"""

    def _diff(self):
        cleaned = set(_artifacts_prefixes())
        backed = {_backup_core(g) for g in _backup_entries()}
        return cleaned, backed

    def test_cleaned_but_not_backed_up_is_the_frozen_set(self):
        cleaned, backed = self._diff()
        got = {p for p in cleaned if p not in backed}
        self.assertEqual(
            got, set(CLEANED_NOT_BACKED_UP),
            "R1 清而不备的差额漂移了——本表逐枚带理由（AGENTS.md §13："
            "同族读者必须同批给口径）。差的枚数：%r" % (got ^ set(CLEANED_NOT_BACKED_UP)))

    def test_backed_up_but_not_cleaned_is_the_frozen_set(self):
        cleaned, backed = self._diff()
        got = {p for p in backed if p not in cleaned}
        self.assertEqual(
            got, set(BACKED_NOT_CLEANED),
            "R2 备而不清的差额漂移了——本表逐枚带理由（这些件均非按日，"
            "无过期概念）。差的枚数：%r" % (got ^ set(BACKED_NOT_CLEANED)))

    def test_every_difference_entry_carries_a_reason(self):
        """差额表不许留空理由（空白理由=没有交代）。"""
        for name, why in {**CLEANED_NOT_BACKED_UP, **BACKED_NOT_CLEANED}.items():
            with self.subTest(name=name):
                self.assertGreaterEqual(len(why.strip()), 8,
                                        f"{name} 的差额理由过短，等于没有交代")


class StateDirKeyDualSourceTest(unittest.TestCase):
    """R7：状态目录键的双源关系必须钉住（两键不许在脚本内静默分叉）。"""

    def setUp(self):
        self.src = _read(os.path.join(BASE, "scripts", "backup.sh"))

    def test_legacy_key_falls_back_to_the_app_side_key(self):
        """`SIGN_STATE_DIR` 必须以 `YIBAN_STATE_DIR` 为第一档——两键不得指向不同目录。"""
        m = re.search(r"^\s*%s=\"\$\{%s:-\$\{%s:-" % (
            LEGACY_STATE_DIR_KEY, STATE_DIR_KEY, LEGACY_STATE_DIR_KEY), self.src, re.M)
        self.assertIsNotNone(
            m, f"backup.sh 里 {LEGACY_STATE_DIR_KEY} 未以 {STATE_DIR_KEY} 优先派生——"
               f"两套键指向不同目录时备份会静默抓错目录")

    def test_state_block_uses_the_derived_key(self):
        """状态件块必须用派生键取目录（不许另读一枚键，否则与上一条断言脱钩）。"""
        start = self.src.index('if [ -d "${%s}" ]; then' % LEGACY_STATE_DIR_KEY)
        block = self.src[start:start + 1200]
        self.assertIn('"${%s}"/' % LEGACY_STATE_DIR_KEY, block,
                      "状态件块未用派生键取目录——两键的取值关系形同虚设")

    def test_app_side_reader_uses_the_unified_key(self):
        """应用侧读者用统一键：账本解析器读 `YIBAN_STATE_DIR`，不读旧键。"""
        ledger = _read(os.path.join(BASE, "yiban", "notify", "ledger.py"))
        self.assertIn(STATE_DIR_KEY, ledger,
                      "notify 账本未按统一键解析状态目录——与备份侧口径分叉")
        self.assertNotIn(LEGACY_STATE_DIR_KEY, ledger,
                         "notify 账本出现了旧键——两套键在同一仓内并存会静默分叉")


if __name__ == "__main__":
    unittest.main(verbosity=2)
