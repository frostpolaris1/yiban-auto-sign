# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""conftest 的「逐用例还原进程 umask」保险必须真的合上。

**来历（2026-10-07 实测）**：`yiban/engine/runner.py:207` 与 `yiban/cli.py:633` 把进程
umask 收到 `077`（生产入口的既定行为，自己不还原）。有若干用例在**进程内**直调
`runner.main` / `cli.main`，于是把 `077` 泄漏给同 worker 的后续用例：那些用例
`os.makedirs` 建出的目录变 `0700`、`_write` 建出的文件变 `0600`，凡带降权（setpriv 到
nobody）或按位格断言的用例就会爆——`tests/test_deploy_prod_artifacts.py` 的非 root 暂存
用例报 `126 Permission denied`（或推到下一步的"清单为空"），而它单跑全绿。触发与否只取决于
`--dist loadfile` 把哪些文件分进同一个 worker，故表现为随机红。

保险在 `tests/conftest.py` 的 `_restore_process_umask`（autouse、function 级、yield 前后
各一次）。本文件用**子进程里的一次最小会话**钉住它：同一进程内先让一条用例把 umask 收紧且
不还原，再断言下一条用例看到的 umask 仍是它自己的起始值。删掉 conftest 的这条 fixture，
本文件必红。

为什么用子进程而不是"就在本进程里前后两条用例"：本仓装了 pytest-randomly 且全量跑带
`-n auto --dist loadfile`，同一起进程内的用例顺序与分片都不由本文件决定——那样的断言会
时真时空转。子会话里 `-p no:randomly` + 同文件两条用例，顺序与进程都被钉死。

**子会话的起始 umask 必须由启动器钉住**（`_pin_child_umask`）：否则它继承本 worker 的
umask，而本 worker 可能已被前一条用例泄漏成 `077`——那时探针 A 的"收紧到 077"是空操作、
A/B 两值相等，守卫在 fixture 已被改坏的前提下照样假绿。实测见该函数 docstring。
"""
import os
import shutil
import subprocess
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL_CONFTEST = os.path.join(BASE, "tests", "conftest.py")

#: 子会话里的两条用例：A 收紧 umask 且**故意不还原**（复现"进程内直调生产入口"的形态），
#: B 断言自己看到的 umask 仍是 A 起始时的那一个（= conftest 逐用例还原生效）。
_PROBE_TESTS = '''# -*- coding: utf-8 -*-
import os

_BEFORE = {}


def test_a_leak_umask_without_restoring():
    before = os.umask(0o022)
    os.umask(before)
    _BEFORE["v"] = before
    os.umask(0o077)          # 泄漏：与 runner.main 的形态一致（不还原）


def test_b_umask_is_restored_before_the_next_case():
    current = os.umask(0o022)
    os.umask(current)
    assert _BEFORE, "用例顺序被随机化了：子会话必须带 -p no:randomly"
    assert current == _BEFORE["v"], (
        "上一条用例收紧的 umask 泄漏到了本条（conftest 的 _restore_process_umask 失效）")
'''


def _pin_child_umask():
    """子进程启动前把它的 umask 钉成 022（POSIX 专用）。

    **少了这一步，本守卫会假绿**：`subprocess.run` 继承本 worker 的 umask，而本 worker
    可能已经被前一条用例（进程内直调 `runner.main` 的那类）泄漏成 `077`——那时探针 A 的
    `os.umask(0o077)` 是空操作、A/B 两值相等，守卫在"fixture 已被改坏"的前提下照样通过。
    实测：变异 conftest + 父 umask 077 时，行为探针**假绿**（只有源码那条兼职抓到）。
    钉住子会话的起始 umask 后，"A 收紧到 077"必然与起始值不同，A/B 差值不再可能消失。
    """
    import os as _os
    _os.umask(0o022)


def test_umask_leak_does_not_reach_the_next_case():
    """子会话里 A 泄漏 `077` ⇒ B 必须仍看到起始 umask（fixture 在起作用）。"""
    with tempfile.TemporaryDirectory(prefix="yiban-umask-guard-") as tmp:
        # 复制**真** conftest：换掉它或删掉那条 fixture，本用例必红。
        shutil.copy(REAL_CONFTEST, os.path.join(tmp, "conftest.py"))
        with open(os.path.join(tmp, "test_probe.py"), "w", encoding="utf-8") as f:
            f.write(_PROBE_TESTS)
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly",
             "-p", "no:cacheprovider", tmp],
            capture_output=True, text=True, timeout=180, cwd=tmp,
            preexec_fn=_pin_child_umask if os.name == "posix" else None)
    out = (r.stdout or "") + (r.stderr or "")
    assert r.returncode == 0, (
        "conftest 的逐用例 umask 还原不成立：删掉/改坏 `_restore_process_umask` 即可复现\n"
        + out)


def test_the_real_conftest_still_defines_the_restoring_fixture():
    """保险的另一半：fixture 本身还在、还是 autouse（改名/去掉 autouse 同样会让上一条空转）。

    上一条子会话用真 conftest 跑，故它已经覆盖"fixture 存在"；这条只看 autouse 这一维——
    fixture 存在但不是 autouse 时，子会话里的探针用例不会被它包住，上一条会红，但红了之后
    难看出是"漏了 autouse"。单列一条把归因钉死。
    """
    with open(REAL_CONFTEST, encoding="utf-8") as f:
        src = f.read()
    assert "def _restore_process_umask():" in src, "conftest 里没有 umask 还原 fixture"
    idx = src.index("def _restore_process_umask():")
    decorator = src.rfind("@pytest.fixture", 0, idx)
    assert decorator != -1, "umask 还原 fixture 上没有 @pytest.fixture 装饰器"
    assert "autouse=True" in src[decorator:idx], "umask 还原 fixture 不是 autouse"
