# -*- coding: utf-8 -*-
"""约束登记门：在册约束必须配门或显式豁免，门必须真的绿（工单 tef8）。

标签：J · 运维：部署/备份/发布
覆盖：`docs/dev/constraints.tsv`（只在册**已经咬过人**的约束，不追求穷尽）与
    `tests/test_constraint_registry_gate.py` 的检查器注册表。四条初始约束：
      ① `protocol-zero-third-party`：洁净室整包不得 import 第三方（下游是逐字节
         复制的导出器，第三方会静默进公开库）；
      ② `supervisord-single-worker`：容器 web 只跑 `-w 1`（限速/防抖状态在进程内存）；
      ③ `m01-deploy-ownership`：root 安装时 M01 门覆盖依赖同步步的三个检出内触点；
      ④ `requirements-audited-equals-deployed`：被审计的产物＝被部署的产物。
关键断言：① 登记表格式正确、id 唯一、载体存在、行数下限；② 每个 `checker:` 都能
    真跑且**零违规**；③ `EXEMPT` 必须写理由与工单；④ 每个检查器都有突变自证
    （喂坏输入必须判出违规）；⑤ 空表/坏行/未知检查器/重复 id 一律红。
依赖：只读仓库文本（AST/正则），不起子进程、无网络。
"""
import ast
import io
import os
import re
import sys
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRY = os.path.join(BASE, "docs", "dev", "constraints.tsv")

COLUMNS = ("id", "constraint", "carrier", "gate", "ticket", "reason")
MIN_ROWS = 4


# ---------------------------------------------------------------------------
# 纯检查器（可被突变测试直接喂坏输入）
# ---------------------------------------------------------------------------
def _walk_with_scope(node, in_func=False):
    """产出 (节点, 是否在函数体内)。用于区分"模块级导入"与"函数内惰性导入"。"""
    for child in ast.iter_child_nodes(node):
        nested = in_func or isinstance(
            child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
        yield child, in_func
        yield from _walk_with_scope(child, nested)


def _protocol_import_violations(sources):
    """sources: {相对路径: 源码} → 违规列表。

    SPEC §2「设计约束」原文：核心**仅依赖标准库**；`crypto.py` 为可选附加件
    （`pycryptodome`），其余模块不得在**模块级** import 它。`__init__.py` 的
    `__getattr__` 里有函数内的惰性导入（SPEC 明写的用法），不算违规。
    """
    bad = []
    for rel, src in sorted(sources.items()):
        is_crypto = rel.endswith("/crypto.py") or rel == "crypto.py"
        try:
            tree = ast.parse(src)
        except SyntaxError as e:
            bad.append(f"{rel}: 解析失败: {e}")
            continue
        for node, in_func in _walk_with_scope(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level:
                    if not is_crypto and not in_func and (
                        (node.module or "") == "crypto"
                        or any(a.name == "crypto" for a in node.names)
                    ):
                        bad.append(f"{rel}: 核心模块不得在模块级 import 可选附加件 crypto")
                    continue
                names = [node.module or ""]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                if not top or top in sys.stdlib_module_names:
                    continue
                if name.startswith("yiban.adapters.protocol_yiban"):
                    if name.endswith(".crypto") and not is_crypto and not in_func:
                        bad.append(f"{rel}: 核心模块不得在模块级 import 可选附加件 crypto")
                    continue
                if is_crypto and top == "Crypto":
                    continue
                bad.append(f"{rel}: import {name}")
    return bad


def _supervisord_violations(text):
    commands = [ln.strip() for ln in text.splitlines()
                if ln.strip().startswith("command=") and "gunicorn" in ln]
    if len(commands) != 1:
        return [f"必须恰有一条 gunicorn command，实得 {len(commands)} 条"]
    if not re.search(r"(?:^|\s)-w\s+1(?:\s|$)", commands[0]):
        return [f"gunicorn command 必须 -w 1：{commands[0]}"]
    return []


def _m01_violations(text):
    required = (
        'gate_path "$REPO_ROOT/scripts/check-deps-lock.sh"',
        'gate_path "$REPO_ROOT/requirements.lock"',
        'gate_path "$_venv_entry"',
    )
    return [f"M01 门缺触点: {item}" for item in required if item not in text]


_REQ_RE = re.compile(r"^([A-Za-z0-9_.-]+)\s*>=\s*([0-9][^\s#]*)")
_LOCK_RE = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s#]+)")


def _canon(name):
    return name.strip().lower().replace("_", "-")


def _version_key(text):
    return tuple(int(x) for x in re.findall(r"\d+", text))


def _requirements_violations(req_text, lock_text):
    reqs = {}
    for raw in req_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _REQ_RE.match(line)
        if not m:
            return [f"无法解析 requirements.txt 行: {line!r}"]
        reqs[_canon(m.group(1))] = m.group(2)
    lock = {}
    for raw in lock_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _LOCK_RE.match(line)
        if m:
            lock[_canon(m.group(1))] = m.group(2)
    bad = [f"requirements.lock 缺 {pkg}" for pkg in sorted(reqs) if pkg not in lock]
    for pkg, lower in sorted(reqs.items()):
        if pkg in lock and _version_key(lock[pkg]) < _version_key(lower):
            bad.append(f"{pkg}: lock {lock[pkg]} 低于 requirements.txt 下限 {lower}")
    return bad


# ---------------------------------------------------------------------------
# 真实载体读取
# ---------------------------------------------------------------------------
def _read(rel):
    with io.open(os.path.join(BASE, rel), encoding="utf-8") as f:
        return f.read()


def _protocol_sources():
    root = os.path.join(BASE, "yiban", "adapters", "protocol_yiban")
    sources = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in sorted(filenames):
            if name.endswith(".py"):
                path = os.path.join(dirpath, name)
                rel = os.path.relpath(path, BASE).replace(os.sep, "/")
                with io.open(path, encoding="utf-8") as f:
                    sources[rel] = f.read()
    return sources


CHECKERS = {
    "protocol_zero_third_party": lambda: _protocol_import_violations(_protocol_sources()),
    "supervisord_single_worker": lambda: _supervisord_violations(
        _read("docker/supervisord.conf")),
    "m01_deploy_ownership": lambda: _m01_violations(_read("deploy/prod/install.sh")),
    "requirements_audited_is_deployed": lambda: _requirements_violations(
        _read("requirements.txt"), _read("requirements.lock")),
}


# ---------------------------------------------------------------------------
# 登记表解析与校验（纯函数，突变测试直接喂合成表）
# ---------------------------------------------------------------------------
def _parse_registry(text):
    rows, bad = [], []
    lines = [ln.rstrip("\r") for ln in text.splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        return rows, ["登记表为空"]
    header = tuple(lines[0].split("\t"))
    if header != COLUMNS:
        bad.append(f"表头应为 {COLUMNS}，实得 {header}")
    for lineno, line in enumerate(lines[1:], start=2):
        cells = line.split("\t")
        if len(cells) != len(COLUMNS):
            bad.append(f"第 {lineno} 行应有 {len(COLUMNS)} 列，实得 {len(cells)}: {line[:80]!r}")
            continue
        rows.append(dict(zip(COLUMNS, cells, strict=True)))
    return rows, bad


def _carrier_exists(spec):
    if spec.endswith("/**"):
        root = os.path.join(BASE, spec[:-3])
        if not os.path.isdir(root):
            return False
        return any(
            name.endswith(".py")
            for _dirpath, _dirs, files in os.walk(root) for name in files
        )
    return os.path.exists(os.path.join(BASE, spec))


def _validate(rows, checkers, base=BASE):
    bad = []
    if len(rows) < MIN_ROWS:
        bad.append(f"登记表只有 {len(rows)} 行（下限 {MIN_ROWS}）：空/残表不得当绿")
    seen = set()
    for row in rows:
        rid = row["id"].strip()
        if not rid:
            bad.append("登记行缺 id")
            continue
        if rid in seen:
            bad.append(f"重复 id: {rid}")
        seen.add(rid)
        for key in ("constraint", "carrier", "gate", "ticket"):
            if not row[key].strip():
                bad.append(f"{rid}: 缺 {key}")
        gate = row["gate"].strip()
        if gate == "EXEMPT":
            if not row["reason"].strip():
                bad.append(f"{rid}: EXEMPT 必须写理由")
        elif gate.startswith("checker:"):
            name = gate.split(":", 1)[1]
            if name not in checkers:
                bad.append(f"{rid}: 未知检查器 {name}")
        else:
            bad.append(f"{rid}: gate 必须是 checker:<name> 或 EXEMPT，实得 {gate!r}")
        carrier = row["carrier"].strip()
        if carrier and not _carrier_exists(carrier):
            bad.append(f"{rid}: 载体不存在 {carrier}")
    return bad


class ConstraintRegistryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with io.open(REGISTRY, encoding="utf-8") as f:
            cls.text = f.read()
        cls.rows, cls.parse_errors = _parse_registry(cls.text)

    def test_registry_is_well_formed_and_not_empty(self):
        self.assertEqual(self.parse_errors, [], "登记表格式错误")
        self.assertGreaterEqual(len(self.rows), MIN_ROWS,
                                "在册约束至少要有已咬过人的四条（tef8）")
        bad = _validate(self.rows, CHECKERS)
        self.assertEqual(bad, [], "约束登记表不合格：\n  - " + "\n  - ".join(bad))

    def test_every_checker_really_runs_and_is_green(self):
        """`checker:` 不是名字占位：逐个真跑，违规必须为零。"""
        ran = 0
        for row in self.rows:
            gate = row["gate"].strip()
            if not gate.startswith("checker:"):
                continue
            name = gate.split(":", 1)[1]
            with self.subTest(constraint=row["id"], checker=name):
                violations = CHECKERS[name]()
                self.assertEqual(violations, [], f"{name} 判红：{violations}")
                ran += 1
        self.assertGreaterEqual(ran, MIN_ROWS, "至少四条在册约束必须有门（不许全豁免）")

    def test_registry_covers_the_four_initial_constraints(self):
        ids = {row["id"] for row in self.rows}
        for cid in ("protocol-zero-third-party", "supervisord-single-worker",
                    "m01-deploy-ownership", "requirements-audited-equals-deployed"):
            with self.subTest(constraint=cid):
                self.assertIn(cid, ids)


class ConstraintRegistryMutationTest(unittest.TestCase):
    """登记表自身的突变自证：坏表必须判红。"""

    GOOD = (
        "id\tconstraint\tcarrier\tgate\tticket\treason\n"
        "a\t约束A\tdocs/dev/README.md\tchecker:supervisord_single_worker\tT1\t\n"
        "b\t约束B\tdocs/dev/README.md\tchecker:protocol_zero_third_party\tT2\t\n"
        "c\t约束C\tdocs/dev/README.md\tchecker:m01_deploy_ownership\tT3\t\n"
        "d\t约束D\tdocs/dev/README.md\tchecker:requirements_audited_is_deployed\tT4\t\n"
    )

    def test_good_table_is_green(self):
        rows, bad = _parse_registry(self.GOOD)
        self.assertEqual(bad, [])
        self.assertEqual(_validate(rows, CHECKERS), [])

    def test_unknown_checker_is_red(self):
        rows, _ = _parse_registry(self.GOOD.replace(
            "checker:supervisord_single_worker", "checker:no_such_checker"))
        self.assertTrue(any("未知检查器" in x for x in _validate(rows, CHECKERS)))

    def test_exempt_without_reason_is_red(self):
        rows, _ = _parse_registry(self.GOOD.replace(
            "checker:supervisord_single_worker\tT1\t", "EXEMPT\tT1\t"))
        self.assertTrue(any("EXEMPT 必须写理由" in x for x in _validate(rows, CHECKERS)))

    def test_exempt_with_reason_is_green(self):
        rows, _ = _parse_registry(self.GOOD.replace(
            "checker:supervisord_single_worker\tT1\t", "EXEMPT\tT1\t历史原因，已挂单"))
        self.assertEqual(_validate(rows, CHECKERS), [])

    def test_duplicate_id_is_red(self):
        rows, _ = _parse_registry(self.GOOD.replace("\nb\t", "\na\t", 1))
        self.assertTrue(any("重复 id" in x for x in _validate(rows, CHECKERS)))

    def test_malformed_row_is_red(self):
        _rows, bad = _parse_registry(self.GOOD + "e\t少列\n")
        self.assertTrue(bad)

    def test_empty_table_is_red(self):
        rows, bad = _parse_registry("# 只有注释\n")
        self.assertTrue(bad)
        self.assertEqual(rows, [])
        self.assertTrue(_validate(rows, CHECKERS), "空表必须判红（扫空不得当绿）")


class CheckerTeethTest(unittest.TestCase):
    """每个检查器喂坏输入必须判出违规（门的牙）。"""

    def test_protocol_rejects_third_party_import(self):
        self.assertEqual(_protocol_import_violations(
            {"pkg/a.py": "import json\nfrom . import b\n"}), [])
        bad = _protocol_import_violations({"pkg/a.py": "import requests\n"})
        self.assertTrue(any("requests" in x for x in bad))

    def test_protocol_allows_crypto_only_in_the_optional_addon(self):
        self.assertEqual(_protocol_import_violations(
            {"pkg/crypto.py": "from Crypto.Cipher import PKCS1_v1_5\n"}), [])
        # 其他模块不得在模块级 import 可选附加件
        bad = _protocol_import_violations(
            {"pkg/page.py": "from . import crypto\n"})
        self.assertTrue(any("crypto" in x for x in bad))
        # 函数内惰性导入是 SPEC 明写的用法（__init__.__getattr__）
        self.assertEqual(_protocol_import_violations({
            "pkg/__init__.py":
                "def __getattr__(name):\n    from . import crypto\n    return crypto\n",
        }), [])

    def test_supervisord_rejects_multi_worker(self):
        good = 'command=/usr/local/bin/gunicorn -w 1 --threads 32 "web.app:create_app()"\n'
        self.assertEqual(_supervisord_violations(good), [])
        self.assertTrue(_supervisord_violations(good.replace("-w 1", "-w 2")))

    def test_m01_rejects_missing_touchpoint(self):
        good = (
            'gate_path "$REPO_ROOT/scripts/check-deps-lock.sh" || exit 1\n'
            'gate_path "$REPO_ROOT/requirements.lock" || exit 1\n'
            'gate_path "$_venv_entry" || exit 1\n'
        )
        self.assertEqual(_m01_violations(good), [])
        broken = good.replace('gate_path "$REPO_ROOT/requirements.lock" || exit 1\n', "")
        self.assertTrue(_m01_violations(broken))

    def test_requirements_rejects_missing_or_lower_lock(self):
        req = "flask>=3.1.3\n"
        self.assertEqual(_requirements_violations(req, "flask==3.1.3\n"), [])
        self.assertTrue(_requirements_violations(req, ""))
        self.assertTrue(_requirements_violations(req, "flask==3.0.0\n"))


if __name__ == "__main__":
    unittest.main()
