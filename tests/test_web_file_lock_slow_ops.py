# -*- coding: utf-8 -*-
"""`_file_lock` 锁内慢操作（SMTP / 口令哈希）的**静态判据**——登记存量，禁止新增。

标签：G · 安全：脱敏/审计/配置注入
覆盖：以 AST 扫 `web/` 全部源码，收集 `with …_file_lock:` 临界区内的五类慢调用
    （send_user / send_admin_alert / send_notification / generate_password_hash /
    check_password_hash），与**冻结的存量清单**逐条对照：不得多（新增即失败）、
    不得少（还掉一处债就更新清单，防"账还在、测试假装看不见"）。
对应实现：判据对象是 `web/services/locks.py` 的 `_file_lock`（进程级 RLock，
    gunicorn `-w 1 --threads 8` 下一堵八条线程全等）与各路由的持锁发信/哈希段。
关键断言：**为什么是静态判据而不是运行时持锁时长断言**——"锁内不做 scrypt"
    在当前架构下尚不成立（存量锁内慢调用是登记在案的债），任何"秒级上限"的计时
    断言都会被一次 SMTP 超时随机打挂，等于没有。本用例把现状冻结成账，慢操作的
    **增长面**归零；异步化落地时按清单逐条销账。另：锁内发信**抛出**的路径已由
    `tests/test_mail_send_never_raises.py` 单独封死（坏字符不再引发锁内 500）。
    本单（`ba-p05-01`）已销账 9 条锁内 SMTP 调用：它们改经 `run_after_file_lock`
    登记、出锁后发出（见 `tests/test_file_lock_no_network.py` 与
    `tests/test_mail_outside_file_lock.py`），故清单只剩 5 条 scrypt。SMTP 三类名
    仍留在 `SLOW_CALLS` 里：裸发信再回到锁内时，本用例照旧红。
依赖：纯 AST + 文件系统遍历，不导入 web 应用、不触网、不落盘。
"""
import ast
import io
import os
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(BASE, "web")

# 慢操作集合：SMTP（三个发信出口）与 scrypt 口令哈希（两个校验/生成分支）。
# 判据与 `web/services/locks.py` 模块头同源："解密、哈希等 CPU 密集动作刻意留在
# 锁外"；SMTP 更甚——单条 15s 超时 × 多收件人可把整站线程冻到分钟级（MF-57）。
SLOW_CALLS = {"send_user", "send_admin_alert", "send_notification",
              "generate_password_hash", "check_password_hash"}

# 冻结的存量清单（文件相对路径, 宿主函数, 被调名）——按当前 HEAD 逐条扫描登记。
# **新增一条 = 本用例失败**；还掉一条时，从清单删一条（清单与代码必须同账）。
# 当前存量 = 5 条 scrypt（`ba-p05-01` 已把 9 条锁内 SMTP 调用改走
# `run_after_file_lock`，逐条销账）。
KNOWN_SLOW_IN_LOCK = {
    ("web/routes/auth.py", "api_register", "generate_password_hash"),
    ("web/routes/me.py", "api_me_password", "check_password_hash"),
    ("web/routes/me.py", "api_me_password", "generate_password_hash"),
    ("web/routes/me.py", "api_me_delete", "check_password_hash"),
    ("web/routes/users_api.py", "api_user_password", "generate_password_hash"),
}


def _callee_name(node):
    """调用目标的可比名：`f(...)` 取 f，`m.send_user(...)` 取 send_user。"""
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return ""


def _is_file_lock(node):
    """with 项是否落在 `_file_lock` 上（`with _file_lock:` 或 `with m._file_lock:`）。"""
    f = node.func if isinstance(node, ast.Call) else node  # 兼容 `with lock():` 工厂形态
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return ""


def _scan_file(path):
    with io.open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    rel = os.path.relpath(path, BASE).replace(os.sep, "/")
    found = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for with_n in ast.walk(fn):
            if not isinstance(with_n, ast.With):
                continue
            if not any(_is_file_lock(it.context_expr) == "_file_lock"
                       for it in with_n.items):
                continue
            for call in ast.walk(with_n):
                if isinstance(call, ast.Call) and _callee_name(call) in SLOW_CALLS:
                    found.add((rel, fn.name, _callee_name(call)))
    return found


class WebFileLockSlowOpsStaticTest(unittest.TestCase):
    def _collect(self):
        out = set()
        for root, dirs, names in os.walk(WEB):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for n in names:
                if n.endswith(".py"):
                    out |= _scan_file(os.path.join(root, n))
        return out

    def test_no_new_slow_call_inside_file_lock(self):
        """增长面归零：扫描结果减去存量清单必须为空。"""
        extra = self._collect() - KNOWN_SLOW_IN_LOCK
        self.assertEqual(
            extra, set(),
            "在 `_file_lock` 临界区内新增了 SMTP/口令哈希调用——慢操作一律留在"
            "锁外（判据见 web/services/locks.py 模块头与 MF-57）；如确属必要，"
            "先按登记流程把该调用点移出锁，不许直接扩清单。")

    def test_known_slow_list_matches_code(self):
        """账实相符：还掉的债必须同步从清单删除（防清单腐烂成"永远绿"）。"""
        gone = KNOWN_SLOW_IN_LOCK - self._collect()
        self.assertEqual(
            gone, set(),
            "这些锁内慢调用点已不在代码里，请把它们从 KNOWN_SLOW_IN_LOCK 删掉"
            "（异步化销账的同步动作）。")


if __name__ == "__main__":
    unittest.main(verbosity=2)
