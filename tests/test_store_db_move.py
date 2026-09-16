# -*- coding: utf-8 -*-
"""`yiban/store/db.py` 迁入后的两条结构守卫：分层边界 + 旧路径只剩壳。

背景：SQLite 数据访问层原先在 `scripts/db.py`，`yiban/` 里的模块（client、
attempt/jobs、store/*）通过函数内裸 `import db` 取它的连接与进程内锁——那条路径依赖
`scripts/` 在 sys.path 里，等于让包内代码反向依赖入口脚本目录。数据层入包后，包内
一律走 `from yiban.store import db`，本文件把这条边界钉住。

同时锁住"只有一份实现"：`scripts/db.py` 必须只剩兼容壳（转发到 `yiban.store.db`），
真正的 `init_db` 定义只能在 `yiban/store/db.py`——否则就会出现本项目反复踩过的
"两份实现"，改了一份而另一份照旧跑。
"""
import io
import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 包内不得以裸名导入的 scripts/ 顶级模块（`from yiban.store import db` 不受影响：
# 下面的正则要求模块名紧跟在 import / from 之后，故 `from yiban... import db` 不匹配）
BARE_IMPORT_RE = re.compile(
    r"(?m)^\s*(?:import|from)\s+(?:db|mailer|notify|signin)(?:\s|\.|,|$)"
)

# 兼容壳必须显著小于实现（实测壳 71 行、实现 3782 行；这里只设"明显变小"的下限，
# 不与具体行数较劲）
SHELL_MAX_LINES = 300


def _iter_py(root):
    for dirpath, dirnames, filenames in os.walk(os.path.join(BASE, root)):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in sorted(filenames):
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


def _read(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def _count_lines(path):
    return sum(1 for _ in io.open(path, encoding="utf-8"))


class StoreDbLayoutTest(unittest.TestCase):
    def test_yiban_does_not_bare_import_scripts_modules(self):
        """`yiban/**` 不得裸 `import db/mailer/notify/signin`（那依赖 scripts/ 在 sys.path）。"""
        bad = []
        for path in _iter_py("yiban"):
            for m in BARE_IMPORT_RE.finditer(_read(path)):
                rel = os.path.relpath(path, BASE).replace(os.sep, "/")
                bad.append(f"{rel}: {m.group(0).strip()}")
        self.assertEqual(
            bad, [],
            "包内代码必须以 `from yiban.store import db`（或 `from yiban import notify`）"
            "的形式导入，不得裸名依赖 scripts/：" + "; ".join(bad),
        )

    def test_no_other_db_implementation(self):
        """真正的实现在 `yiban/store/db.py`；`scripts/db.py` 只是转发壳。"""
        impl_path = os.path.join(BASE, "yiban", "store", "db.py")
        shell_path = os.path.join(BASE, "scripts", "db.py")
        self.assertTrue(os.path.exists(impl_path), "yiban/store/db.py 缺失")
        self.assertTrue(os.path.exists(shell_path), "scripts/db.py 兼容壳缺失")
        impl_src, shell_src = _read(impl_path), _read(shell_path)
        self.assertRegex(impl_src, r"(?m)^def init_db\(", "实现里应有真正的 init_db 定义")
        self.assertNotRegex(
            shell_src, r"(?m)^def init_db\(",
            "scripts/db.py 又出现了一份实现（应为转发到 yiban.store.db 的兼容壳）",
        )
        self.assertIn("yiban.store import db", shell_src, "壳应转发到 yiban.store.db")
        self.assertIn("_ForwardingModule", shell_src, "壳须把属性写入转发到实现模块")
        impl_lines, shell_lines = _count_lines(impl_path), _count_lines(shell_path)
        self.assertLess(shell_lines, impl_lines,
                        f"壳（{shell_lines} 行）不该与实现（{impl_lines} 行）同量级")
        self.assertLessEqual(shell_lines, SHELL_MAX_LINES,
                             f"壳膨胀到 {shell_lines} 行，已不像转发壳")

    def test_shell_forwards_the_same_objects(self):
        """打桩仍生效的充要条件：壳里的名字与实现是同一对象，且写入会落到实现。

        既有用例大量以 `db.<名字> = 替身` / `mock.patch.object(db, ...)` 打桩；若壳只是
        复制了一份绑定（不转发写入），打桩会落在壳上而实现内部照旧调真名——静默失效。
        """
        import importlib
        import sys

        sys.path.insert(0, os.path.join(BASE, "scripts"))
        shell = importlib.import_module("db")
        impl = importlib.import_module("yiban.store.db")
        self.assertIs(shell.get_conn, impl.get_conn)
        self.assertIs(shell._conn_lock, impl._conn_lock)

        original = impl.audit
        try:
            shell.audit = _sentinel = object()
            self.assertIs(impl.audit, _sentinel, "壳上的打桩没有转发到实现模块")
            impl.audit = original
            self.assertIs(shell.audit, original, "实现上的改动没有回落到壳的读取")
        finally:
            impl.audit = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
