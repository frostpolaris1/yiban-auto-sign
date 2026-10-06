# -*- coding: utf-8 -*-
"""结构守卫：`with _file_lock` 临界区内不得出现裸的网络调用（工单 ba-p05-01）。

标签：E · 门禁：锁纪律
覆盖：`web/` 下全部 Python 文件的锁区间扫描；扫描器自身的承重证明（变异体）。
关键断言：`mailer.send_user` / `send_admin_alert` / `send_notification` /
`notify_admin_entry` / `notify.send` 这类网络入口出现在锁区间时，**必须**以
`run_after_file_lock(...)` 的登记参数形态出现，否则判红。
立此守卫的理由（AGENTS.md §15）：下一个改这里的人把发信挪回锁内时，本文件必须变红。
依赖：只用标准库 `ast` 读源码，不导入被测模块。
"""
import ast
import os
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 锁区间内禁止裸调的网络入口（按点号名的末段匹配）
NETWORK_ENTRIES = {
    "send_user", "send_admin_alert", "send_notification",
    "notify_admin_entry", "send_test", "send",
}
#: 锁内唯一的合法形态：登记到出锁后执行
SHIELD = "run_after_file_lock"
#: 同族但语义无关的锁（文件状态锁），不得被当成全局账号锁扫进来
NOT_THE_LOCK = ("_state_file_lock", "_ledger_file_lock", "_rate_lock")

#: 名册里的 5 个落点文件（工单 §3）：扫描面必须覆盖它们，
#: 否则文件被改名/挪走会让守卫静默空扫。
ROSTER_FILES = (
    "web/routes/accounts_api.py",
    "web/routes/my.py",
    "web/routes/me.py",
    "web/routes/users_api.py",
    "web/routes/auth.py",
)
#: 例外：汇合机制的定义点本身
SKIP = {"web/services/locks.py"}


def _dotted(node):
    """把 `m.mailer.send_user` / `notify.send` 这类表达式压成点号名。"""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _locks_here(with_node):
    """该 `with` 的上下文里是否有全局 `_file_lock`。"""
    for item in with_node.items:
        name = _dotted(item.context_expr)
        if name and "_file_lock" in name and not any(n in name for n in NOT_THE_LOCK):
            return True
    return False


def scan_lock_regions(path, source):
    """返回锁区间内裸网络调用的 `[(行号, 源码行, 点号名)]`。"""
    tree = ast.parse(source, filename=path)
    lines = source.splitlines()
    hits = []

    def check(node, in_lock):
        """查 node 自身（它可能就是那次调用），再查它的子树。"""
        if isinstance(node, ast.Call) and in_lock:
            name = _dotted(node.func)
            last = name.split(".")[-1]
            if last == SHIELD:
                # 登记形态：首参是被推迟的可调用对象（合法）；**其余实参仍在锁内求值**，
                # 所以逐个查——把裸发信塞进实参里不算合法登记。
                for arg in list(node.args[1:]) + [kw.value for kw in node.keywords]:
                    check(arg, True)
                return
            if last in NETWORK_ENTRIES:
                hits.append((node.lineno, lines[node.lineno - 1].strip(), name))
                return
        for child in ast.iter_child_nodes(node):
            nested = in_lock
            if isinstance(child, (ast.With, ast.AsyncWith)) and _locks_here(child):
                nested = True
            check(child, nested)

    check(tree, False)
    return hits


def scan_web():
    """扫描 `web/` 全部 Python 文件，返回 (违规清单, 已扫文件清单)。"""
    violations, scanned = [], []
    for root, dirs, files in os.walk(os.path.join(BASE, "web")):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", "node_modules", "static")]
        for fn in sorted(files):
            if not fn.endswith(".py"):
                continue
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, BASE).replace(os.sep, "/")
            if rel in SKIP:
                continue
            with open(full, encoding="utf-8") as f:
                src = f.read()
            scanned.append(rel)
            for lineno, text, name in scan_lock_regions(rel, src):
                violations.append("%s:%d  %s()  ::  %s" % (rel, lineno, name, text))
    return violations, scanned


class FileLockNoNetworkTest(unittest.TestCase):
    def test_no_bare_network_call_inside_file_lock(self):
        violations, scanned = scan_web()
        self.assertGreaterEqual(len(scanned), 10, "扫描面异常收缩：守卫要红给自己看")
        for rel in ROSTER_FILES:
            self.assertIn(rel, scanned, "名册文件必须落在扫描面内: %s" % rel)
        self.assertEqual(violations, [],
                         "以下发信调用坐在全局锁临界区内（SMTP/Webhook 阻塞全站读快照）:\n"
                         + "\n".join(violations))

    def test_scanner_detects_a_planted_violation(self):
        """变异体：证明主用例承重，不是空扫（把发信挪回锁内必须红）。"""
        src = (
            "def view(m):\n"
            "    with m._file_lock:\n"
            "        m.mailer.send_user('u', 's', 'body')\n"
            "        m.send_notification('t', 'c', urgent=True)\n"
        )
        self.assertEqual(len(scan_lock_regions("web/routes/planted.py", src)), 2)

    def test_scanner_detects_a_naked_call_hidden_in_registered_args(self):
        """登记形态的实参仍在锁内求值：把裸发信塞进去同样判红。

        报出的名字是点号名（`m.send_notification`），与主用例的违规文案同一形态。
        """
        src = (
            "def view(m):\n"
            "    with m._file_lock:\n"
            "        m.run_after_file_lock(m.mailer.send_user, 'u',\n"
            "                               m.send_notification('t', 'c'))\n"
        )
        hits = scan_lock_regions("web/routes/hidden.py", src)
        self.assertEqual([n for _, _, n in hits], ["m.send_notification"])

    def test_scanner_accepts_the_registered_form(self):
        """合法登记不判红，否则修法无处安放。"""
        src = (
            "def view(m):\n"
            "    with m._file_lock:\n"
            "        m.run_after_file_lock(m.mailer.send_user, 'u', 's', 'body')\n"
            "        with m._file_lock:\n"
            "            m.run_after_file_lock(m.send_notification, 't', 'c')\n"
            "            m.db.audit('a', 'b', 'c', 'd')\n"
        )
        self.assertEqual(scan_lock_regions("web/routes/registered.py", src), [])

    def test_scanner_ignores_the_state_file_lock_family(self):
        """同族但无关的文件状态锁不得被当成全局账号锁（防误红、防把守卫写宽）。"""
        src = (
            "def flush(ledger_mod):\n"
            "    with ledger_mod._state_file_lock('notify-throttle.json'):\n"
            "        ledger_mod.send('t', 'c')\n"
        )
        self.assertEqual(scan_lock_regions("yiban/notify/ledger.py", src), [])


if __name__ == "__main__":
    unittest.main()
