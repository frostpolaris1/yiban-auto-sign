# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""`python -m yiban.adapters.protocol_fake` 的入口件（包目录要 `__main__.py` 才能 `-m`）。

本件只做一件事：把 `main()` 的返回值交给 `sys.exit`。
假上游本体与它的命令行开关住在 `yiban/adapters/protocol_fake/__init__.py`。
"""
import sys

from yiban.adapters.protocol_fake import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
