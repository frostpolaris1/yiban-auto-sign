#!/usr/bin/env python
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""文档编号唯一性巡检：同一 docs 根下 `NNN-` 前缀不得撞号（工单 b7la）。

用法：
    scripts/check-doc-numbering.py [--root DIR] [--min-files N]

口径：
- 匹配 `^(\\d+)-` 的文件名与目录名；**文件与目录同号也算撞号**（`84-assets/` 与
  `84-foo.md` 是同号）。
- 目录递归扫描；`__pycache__` 跳过。
- 编号比较按**十进制数值**（`007` 与 `7` 撞号），输出保留原始名。
- 退出码：0 = 无撞号；1 = 有撞号（逐组点名）；2 = 环境错误（根不存在、扫描面
  少于 `--min-files` 个文件——扫空不得当绿）。`numbered=0` 是合法状态（契约文档
  不带编号），但会显式打印，便于发现"扫错根"。
"""
import argparse
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_NUMBERED = re.compile(r"^(\d+)-")


def _scan(root):
    files, numbered = [], []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for name in sorted(dirnames):
            files.append(os.path.join(dirpath, name))
            m = _NUMBERED.match(name)
            if m:
                numbered.append((int(m.group(1)), name,
                                 os.path.join(dirpath, name)))
        for name in sorted(filenames):
            files.append(os.path.join(dirpath, name))
            m = _NUMBERED.match(name)
            if m:
                numbered.append((int(m.group(1)), name,
                                 os.path.join(dirpath, name)))
    return files, numbered


def find_duplicates(numbered):
    groups = {}
    for num, name, path in numbered:
        groups.setdefault(num, []).append((name, path))
    return {num: items for num, items in groups.items() if len(items) > 1}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="scripts/check-doc-numbering.py")
    ap.add_argument("--root", default=os.path.join(REPO_ROOT, "docs"))
    ap.add_argument("--min-files", type=int, default=1,
                    help="扫描面文件数下限（防扫错根）。默认 1")
    args = ap.parse_args(argv)

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print("check-doc-numbering: 根目录不存在: %s" % root, file=sys.stderr)
        return 2
    files, numbered = _scan(root)
    if len(files) < args.min_files:
        print("check-doc-numbering: 扫描面只有 %d 个条目（下限 %d）——扫空不得当绿: %s"
              % (len(files), args.min_files, root), file=sys.stderr)
        return 2
    dup = find_duplicates(numbered)
    print("扫描 %d 个条目，带编号 %d 个，撞号组 %d 个"
          % (len(files), len(numbered), len(dup)))
    if not dup:
        return 0
    for num in sorted(dup):
        print("撞号 %d：" % num, file=sys.stderr)
        for name, path in dup[num]:
            print("    %s（%s）" % (name, path), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
