#!/usr/bin/env python
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""覆盖面对账门（最小版）：改动文件必须 ⊆ 各门覆盖并集，差集必须显式登记。

用法：
    scripts/check-coverage-ledger.py --changed-file LIST [--root DIR]
    scripts/check-coverage-ledger.py --changed PATH... [--root DIR]
    scripts/check-coverage-ledger.py --base REF [--root DIR]

`LIST` 每行一个改动文件；`--base REF` 用 `git diff --name-only REF...HEAD` 取改动集。
**空改动集退 2，不当绿**（AGENTS §十三 第 30 条：扫空不得当绿）。

覆盖判定（逐改动文件）：
  1. 改动文件本身是名册里的测试文件 ⇒ 直接被覆盖；
  2. 有测试文件在文本里提到它的模块路径 / 相对路径 ⇒ 间接覆盖；
  3. 命中 `scripts/gate/coverage-ledger.tsv` 的登记行 ⇒ 按登记处理：
     - `covered_by=<测试文件>`：断言该测试文件存在且在名册里；
     - `uncovered=<理由>`：显式登记"已知未覆盖"。

退出码：0 = 全部被覆盖或已登记；1 = 存在未登记差集；2 = 环境/参数错误。
"""
import argparse
import fnmatch
import io
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROSTER = "tests/lane-roster.tsv"
REGISTRY = "scripts/gate/coverage-ledger.tsv"
TEST_PREFIX = "tests/"
MIN_CHANGED = 1


def _die(msg, code=2):
    print("check-coverage-ledger: %s" % msg, file=sys.stderr)
    sys.exit(code)


def _read(path):
    with io.open(path, encoding="utf-8-sig") as f:
        return f.read()


def _roster_files(root):
    path = os.path.join(root, ROSTER)
    if not os.path.isfile(path):
        _die("名册不存在: %s" % path)
    files = set()
    for line in _read(path).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cells = line.split("\t")
        if len(cells) >= 2:
            files.add(cells[1].strip())
    return files


def _registry_rows(root):
    path = os.path.join(root, REGISTRY)
    if not os.path.isfile(path):
        _die("覆盖登记表不存在: %s" % path)
    rows = []
    for num, raw in enumerate(_read(path).splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        cells = line.split("\t")
        if len(cells) not in (2, 3) or not all(c.strip() for c in cells[:2]):
            _die("登记表 %s 第 %d 行应为 `pattern<TAB>处置[<TAB>理由]`: %r"
                 % (path, num, raw))
        rows.append((cells[0].strip(), cells[1].strip(),
                     cells[2].strip() if len(cells) == 3 else ""))
    return rows


def _module_keys(rel):
    """改动文件的"测试可能提到它"的形态。绝对路径不参与（换机不稳）。"""
    keys = {rel}
    if rel.endswith(".py"):
        dotted = rel[:-3].replace("/", ".")
        keys.add(dotted)
        parts = dotted.split(".")
        if parts and parts[0] in ("scripts", "web", "yiban"):
            keys.add(".".join(parts))
        if len(parts) >= 2:
            keys.add(".".join(parts[-2:]))
        keys.add(parts[-1])
    return keys


def _test_texts(root, roster):
    out = {}
    for rel in sorted(roster):
        path = os.path.join(root, rel)
        if os.path.isfile(path):
            out[rel] = _read(path)
    return out


def _match_registry(rel, rows):
    for pattern, action, reason in rows:
        if fnmatch.fnmatch(rel, pattern):
            return action, reason
    return None


def evaluate(changed, roster, test_texts, registry, root):
    """纯函数：返回 (unregistered, covered, registered)。"""
    unregistered, covered, registered = [], [], []
    for rel in sorted(set(changed)):
        rel = rel.strip().replace("\\", "/")
        if not rel:
            continue
        if rel.startswith(TEST_PREFIX) and rel in roster:
            covered.append((rel, "测试文件自身"))
            continue
        hit = None
        for key in _module_keys(rel):
            for test, text in test_texts.items():
                if key and key in text:
                    hit = (test, key)
                    break
            if hit:
                break
        if hit:
            covered.append((rel, "被 %s 提到（%s）" % hit))
            continue
        action = _match_registry(rel, registry)
        if action:
            expr, note = action
            what, _, value = expr.partition("=")
            if what == "covered_by":
                if value not in roster or not os.path.isfile(os.path.join(root, value)):
                    unregistered.append((rel, "登记 covered_by 指向不存在/未归类的测试: %s" % value))
                else:
                    covered.append((rel, "登记 covered_by=%s" % value))
            elif what == "uncovered":
                registered.append((rel, value or note))
            else:
                unregistered.append((rel, "登记处置未知: %s" % expr))
            continue
        unregistered.append((rel, "没有测试提到它，也没有登记"))
    return unregistered, covered, registered


def _changed_from_git(root, base):
    proc = subprocess.run(
        ["git", "-C", root, "diff", "--name-only", "%s...HEAD" % base],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120)
    if proc.returncode != 0:
        _die("git diff 失败（base=%r）：%s" % (base, proc.stderr.strip()))
    return [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="scripts/check-coverage-ledger.py")
    ap.add_argument("--root", default=REPO_ROOT)
    ap.add_argument("--changed", nargs="*", default=None)
    ap.add_argument("--changed-file", default=None, help="每行一个改动文件")
    ap.add_argument("--base", default=None, help="用 git diff <base>...HEAD 取改动集")
    args = ap.parse_args(argv)

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        _die("根目录不存在: %s" % root)
    if args.changed_file:
        if not os.path.isfile(args.changed_file):
            _die("改动清单不存在: %s" % args.changed_file)
        changed = [ln for ln in _read(args.changed_file).splitlines() if ln.strip()]
    elif args.changed:
        changed = args.changed
    elif args.base:
        changed = _changed_from_git(root, args.base)
    else:
        _die("未给改动集（--changed / --changed-file / --base 三选一）。空集不得当绿")
    if len(changed) < MIN_CHANGED:
        _die("改动集为空：没有可对账的文件，拒绝当绿（AGENTS §十三 第 30 条）")

    roster = _roster_files(root)
    if not roster:
        _die("名册为空：覆盖判定面已失效")
    registry = _registry_rows(root)
    unregistered, covered, registered = evaluate(
        changed, roster, _test_texts(root, roster), registry, root)

    print("改动 %d 个：覆盖 %d，显式未覆盖 %d，未登记 %d"
          % (len(changed), len(covered), len(registered), len(unregistered)))
    for rel, why in covered:
        print("    COVERED   %s（%s）" % (rel, why))
    for rel, why in registered:
        print("    REGISTERED %s（%s）" % (rel, why))
    for rel, why in unregistered:
        print("    UNCOVERED %s（%s）" % (rel, why))
    if unregistered:
        print("差集必须显式登记到 %s（covered_by=... 或 uncovered=理由）" % REGISTRY,
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
