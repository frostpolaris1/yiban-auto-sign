#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""**功能**
把 `tests/test_*.py` 文件头的五字段标签块汇成机器可读的测试索引，并提供"索引没过期"的自检。

一条记录一行五列：`文件 / 用例方法名 / 类别(A~M) / 是否需要 node、bash / 一句话守什么`。

**归属**
开发期工具（`scripts/`），与被测代码同仓、只读 `tests/`，不导入 `yiban`/`web`（工具与被测面
隔开，工具崩了不该让测试跑不起来）。索引产物归 `docs/dev/`。

**复用**
`scan(root)` 返回 (records, notices, stats)，可被其它统计脚本直接调用；命令行是主入口。

**通信**
输入：`tests/test_*.py` 的 AST —— 模块 docstring 的 `标签：`/`覆盖：`/`对应实现：`/
`关键断言：`/`依赖：`，加每个 `test*` 方法名与其单行 docstring。
输出：`--write` 覆盖写索引文件；`--check` 不写任何文件，只比对并打印差异（不一致退出码 1）。
调用点：人工执行，以及 CI 在跑测试之前执行 `python scripts/test_index.py --check`
（标签或用例一改、索引就红，杜绝手写清单悄悄过期）。
"""
import argparse
import ast
import difflib
import glob
import os
import re
import sys

MISSING = "??"                      # 唯一允许的"解析不到"标记：绝不拿猜测去填空
COLS = ("file", "case", "category", "needs", "guards")
FIELDS = ("标签", "覆盖", "对应实现", "关键断言", "依赖")   # 字段名与顺序由测试文件头约定
GROUP_LETTERS = frozenset("ABCDEFGHIJKLM")                # 标签组的合法字母（A~M）
RUNTIMES = ("node", "bash")         # 只有这两种外部解释器会被标进 needs 列（其它子进程形态不算）
# 只有 which() 的入参和 argv 首位算"真需要"：注释与 依赖 字段里提到 node 不代表会起进程
SPAWN_FUNCS = frozenset({"run", "Popen", "call", "check_call", "check_output",
                         "getoutput", "getstatusoutput"})
FIELD_LIKE = re.compile(r"^(标签|覆盖|对应实现|关键断言|依赖)\s*[:：]")
HEADER = "# " + "\t".join(COLS)
MAX_DIFF_LINES = 60                 # 差异打印上限：全量重排时也别刷屏


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _one_line(doc):
    """取 docstring 的首个非空行并压成一行（多行详述留在源码里，不进索引列）。"""
    for line in (doc or "").splitlines():
        s = re.sub(r"\s+", " ", line).strip()
        if s:
            return s
    return None


def parse_tag_block(doc):
    """切标签块为 {字段: 值}，返回 (fields, notices)。

    字段名行（全角冒号紧跟）之后的行一律算续行，不论缩进——各文件的折行缩进并不统一
    （有的续行缩进三格，有的直接顶格折行），靠缩进判块尾会把顶格折行的文件误判成缺字段。
    块尾判据：见到空行且最后的「依赖」已开始 ⇒ 块到此结束，否则标签块后面的散文段会被
    并进依赖（索引五列都不取依赖的文本，受影响的只有下面那条 依赖↔代码 对账提示）。
    """
    fields, notices = {}, []
    cur, last_idx = None, None
    for line in doc.splitlines():
        s = line.strip()
        if not s:
            if "依赖" in fields:
                break                   # 最后一个字段已闭合，空行后是散文
            continue
        hit = next((f for f in FIELDS if s.startswith(f + "：")), None)
        if hit:
            if hit in fields:
                notices.append(f"字段「{hit}」出现两次，取值以第一条为准")
                cur = None              # 重复字段后的行不再归属任何字段
                continue
            if last_idx is not None and FIELDS.index(hit) < last_idx:
                notices.append(f"字段顺序不合约定：{hit} 出现在 {FIELDS[last_idx]} 之后")
            last_idx = FIELDS.index(hit)
            cur = hit
            fields[hit] = s[len(hit) + 1:].strip()
            continue
        if cur:
            fields[cur] += "\n" + s
    return fields, notices


def suspect_field_names(doc):
    """像是字段名却不合约定的行（半角冒号、字段名后带空格）。

    写错的字段会被 parse_tag_block 静默丢掉，等于索引少一格；宁可单独报出来。
    """
    bad = []
    for line in doc.splitlines()[:40]:
        s = line.strip()
        if FIELD_LIKE.match(s) and not any(s.startswith(f + "：") for f in FIELDS):
            bad.append(s[:40])
    return bad


def category_of(fields):
    """类别列只认 `标签：` 首行的组字母（形如「F · 前端与界面守卫」），认不出就 MISSING。"""
    tag = (fields.get("标签") or "").splitlines()
    head = tag[0].split("·")[0].strip() if tag else ""
    return head if len(head) == 1 and head in GROUP_LETTERS else MISSING


def _callee(call):
    f = call.func
    if isinstance(f, ast.Name):
        return f.id                     # 裸名调用：run(...)
    return f.attr if isinstance(f, ast.Attribute) else ""   # 属性调用：subprocess.run(...)


def _tool(word):
    """命令名对表：取 basename（兼容 Windows 反斜杠路径）后认 node / bash，其余不算证据。"""
    word = word.strip().replace("\\", "/").rsplit("/", 1)[-1]
    return word if word in RUNTIMES else None


def _tool_of(node):
    """表达式节点若是字面量字符串才判命令名——变量、f-string 一律不猜。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _tool(node.value)
    return None


def detect_runtimes(tree):
    """判"这个文件真会起 node / bash"：证据只有 which("node") 这类取路径、
    以及 list/tuple 字面量的首位（覆盖 run(["bash", …]) 与 cmd=["bash", …] 两种写法）
    和子进程函数的字符串命令首词。三处都在 AST 上取，不刮源码文本。"""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
            t = _tool_of(node.elts[0])
            if t:
                found.add(t)
        elif isinstance(node, ast.Call):
            name = _callee(node)
            if name == "which":
                found.update(t for t in map(_tool_of, node.args) if t)
            elif name in SPAWN_FUNCS and node.args:
                cmd = node.args[0]
                if isinstance(cmd, ast.Constant) and isinstance(cmd.value, str) and cmd.value.split():
                    t = _tool(cmd.value.split()[0])   # 字符串命令形态：`bash -c …`
                    if t:
                        found.add(t)
    return found


def collect_cases(tree):
    """按定义顺序取用例：模块级 test* 函数 + 类内 test* 方法（含 async）。

    不下钻用例体内的闭包——那里的 test* 不是 pytest 眼里的用例；类内方法用
    `类名.方法名` 限定，否则跨类同名方法在索引里会撞成两行无法区分。
    """
    out = []
    for node in tree.body:
        kind = (ast.FunctionDef, ast.AsyncFunctionDef)
        if isinstance(node, kind) and node.name.startswith("test"):
            out.append((node.name, _one_line(ast.get_docstring(node))))
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, kind) and sub.name.startswith("test"):
                    out.append((f"{node.name}.{sub.name}", _one_line(ast.get_docstring(sub))))
    return out


def _needs_of(found, ok):
    if not ok:
        return MISSING
    return "+".join(sorted(found)) if found else "-"


def scan(root):
    """扫 tests/test_*.py，返回 (records, notices, stats)。records 已排序，逐条五元组。"""
    paths = sorted(glob.glob(os.path.join(root, "tests", "test_*.py")))
    records, notices = [], []
    stats = {
        "files": len(paths), "unparsed": 0, "files_full_tags": 0, "records": 0,
        "guard_from_docstring": 0, "guard_missing": 0, "case_missing": 0,
        "category_missing": 0, "needs_none": 0, "needs_node": 0, "needs_bash": 0,
        "needs_both": 0, "needs_missing": 0,
    }
    for path in paths:
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        try:
            tree = ast.parse(_read(path))
        except (SyntaxError, UnicodeDecodeError, ValueError) as exc:
            # 解析失败时整行都是 ??：留一行占位并登记，别让整个工具挂掉、也别猜内容
            stats["unparsed"] += 1
            records.append((rel, MISSING, MISSING, MISSING, MISSING))
            notices.append(f"{rel}: AST 解析失败（{type(exc).__name__}: {exc}），该行全部为 ??")
            continue
        doc = ast.get_docstring(tree) or ""
        fields, field_notes = parse_tag_block(doc)
        for note in field_notes:
            notices.append(f"{rel}: {note}")
        missing = [f for f in FIELDS if f not in fields]
        if missing:
            notices.append(f"{rel}: 标签块缺字段 {','.join(missing)}")
        else:
            stats["files_full_tags"] += 1
        for bad in suspect_field_names(doc):
            notices.append(f"{rel}: 疑似字段名写法不合约定 -> {bad}")
        cat = category_of(fields)
        if cat == MISSING:
            stats["category_missing"] += 1
            notices.append(f"{rel}: 标签首行取不到 A~M 组字母 -> {MISSING}")
        found = detect_runtimes(tree)
        needs = _needs_of(found, True)
        stats["needs_node" if needs == "node" else "needs_bash" if needs == "bash"
              else "needs_both" if needs == "bash+node" else "needs_none"] += 1
        dep = (fields.get("依赖") or "").replace("\n", "")
        for tool in sorted(found):
            if tool not in dep:
                # 只判"代码用了、依赖字段整段没提"这一个方向：字段是散文，反向往 negate 里钻
                notices.append(f"{rel}: 代码真起 {tool}，但「依赖」字段未提及 {tool}")
        cases = collect_cases(tree)
        if not cases:
            stats["case_missing"] += 1
            records.append((rel, MISSING, cat, needs, MISSING))
            notices.append(f"{rel}: 未发现 test* 用例，仅登记文件行")
            continue
        for case, guard in cases:
            guard = guard or MISSING
            stats["guard_from_docstring" if guard != MISSING else "guard_missing"] += 1
            records.append((rel, case, cat, needs, guard))
    records.sort(key=lambda r: (r[0], r[1]))
    stats["records"] = len(records)
    return records, notices, stats


def render(records):
    """索引文件的行序列：一行表头 + 一条记录一行，制表符分隔（列内不许再出现制表符）。"""
    lines = [HEADER]
    for rec in records:
        clean = [re.sub(r"[\t\r\n]+", " ", str(c)).strip() for c in rec]
        lines.append("\t".join(clean))
    return lines


def write_index(path, lines):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")


def compare_index(path, lines):
    """返回差异行；空列表表示索引与代码一致。索引不存在也算差异（退出码 1）。"""
    if not os.path.exists(path):
        return [f"（索引文件不存在：{path}，先跑 --write）"]
    old = [ln.rstrip("\n") for ln in _read(path).splitlines()]
    if old == lines:
        return []
    diff = list(difflib.unified_diff(old, lines, "现有索引", "按代码重建", lineterm="", n=1))
    if len(diff) > MAX_DIFF_LINES:
        kept = diff[:MAX_DIFF_LINES]
        kept.append(f"…… 差异共 {len(diff)} 行，只列前 {MAX_DIFF_LINES} 行")
        diff = kept
    return diff


def print_stats(records, notices, stats):
    """收尾计数：多少条来自字段、多少条是 ??，全打印出来，人才会去查为什么。"""
    n = stats["records"]
    print(f"文件 {stats['files']} 个（AST 解析失败 {stats['unparsed']} 个）"
          f"，五字段齐全 {stats['files_full_tags']} 个")
    print(f"索引记录 {n} 条")
    print(f"  类别列取自「标签」字段 {n - stats['category_missing']} 条，{MISSING} {stats['category_missing']} 条")
    print(f"  守什么列取自用例 docstring {stats['guard_from_docstring']} 条，"
          f"{MISSING} {stats['guard_missing']} 条（命名已自解释、无函数级说明的用例）")
    print(f"  node/bash 列：无需 {stats['needs_none']} / 仅 node {stats['needs_node']} / "
          f"仅 bash {stats['needs_bash']} / node+bash {stats['needs_both']} / {MISSING} {stats['needs_missing']}")
    print(f"标签块可疑处 {len(notices)} 条（只登记、不改测试、不影响退出码）：")
    for note in notices:
        print(f"  ! {note}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="从 tests/ 文件头标签生成测试索引并自检")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="生成/覆盖索引文件")
    mode.add_argument("--check", action="store_true", help="不写文件，比对索引与代码，不一致退出码 1")
    ap.add_argument("--root", default=None, help="仓库根（默认本脚本上一级）")
    ap.add_argument("--out", default=None, help=f"索引路径（默认 <root>/{'docs/dev/test-index.tsv'}）")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    root = args.root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = args.out or os.path.join(root, "docs", "dev", "test-index.tsv")
    records, notices, stats = scan(root)
    lines = render(records)
    if args.write:
        write_index(out, lines)
        print(f"已写入 {os.path.relpath(out, root).replace(os.sep, '/')}")
        print_stats(records, notices, stats)
        return 0
    diff = compare_index(out, lines)
    if diff:
        print(f"索引与代码不一致：{os.path.relpath(out, root).replace(os.sep, '/')}")
        print("\n".join(diff))
    else:
        print("索引与代码一致")
    print_stats(records, notices, stats)
    return 1 if diff else 0


if __name__ == "__main__":
    sys.exit(main())
