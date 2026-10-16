# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""`python -m yiban.entry.cli` 的入口件（包目录要 `__main__.py` 才能 `-m`）。

本件只做一件事：把 `main()` 的返回值交给 `sys.exit`。
判据与用法面住在 `yiban/entry/cli/__init__.py`，本件不重复一份。
"""
import sys

from yiban.entry.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
