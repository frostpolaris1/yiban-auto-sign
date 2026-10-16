# -*- coding: utf-8 -*-
"""账号页 / 用户页「分页接线」的静态守卫（工单 p37f 前端面）。

标签：F · 前端与界面守卫
覆盖：`frontend/src/accounts/Accounts.vue`、`frontend/src/users/Users.vue`、
     `frontend/src/lib/pager.ts`
关键断言（钉接线不变式，不钉编舞）：
    ① 两张表的 tbody 行集来自分页切片（`pageRows` / `pageRowsOf`），**不是**整组
       （`rowsOf` / `userRows` 直接渲染）——否则 5k 行下首屏 DOM 会退回 11 万节点；
    ② 每页行集经 `lib/pager` 的 `pageSlice`（唯一切片实现），页大小可选值来自同一模块；
    ③ 分页控件存在且走 EP `el-pagination`（原生 `<select>`/`<input type=range>` 的弹出层
       由 UA 渲染、样式不可控，属既有禁令）。
**不测**：具体页码/控件排布（会随实现变化）——那些由 e2e `frontend/e2e/paging.spec.ts`
在真浏览器 5k 数据下量（首屏 DOM、翻页字节）。本文件只钉「接线还在」，防静默退化。
依赖：纯文本读取，无 node、无网络。
"""
import os
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACCOUNTS = os.path.join(BASE, "frontend", "src", "accounts", "Accounts.vue")
USERS = os.path.join(BASE, "frontend", "src", "users", "Users.vue")
PAGER = os.path.join(BASE, "frontend", "src", "lib", "pager.ts")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class FrontendPagingWiringTest(unittest.TestCase):
    def test_accounts_rows_come_from_pager(self):
        src = _read(ACCOUNTS)
        self.assertIn('v-for="row in pageRows(', src,
                      "账号表行集必须来自分页切片（pageRows），不得直接渲染整组")
        self.assertIn('from "../lib/pager"', src, "账号页必须消费 lib/pager 的唯一切片实现")
        self.assertIn("data-acct-pager", src, "账号页必须有分页控件挂点")
        self.assertIn("<el-pagination", src, "分页控件必须走 EP（原生控件弹出层不可控）")

    def test_users_rows_come_from_pager(self):
        src = _read(USERS)
        self.assertIn('v-for="row in pageRowsOf(', src,
                      "用户表行集必须来自分页切片（pageRowsOf），不得直接渲染整组")
        self.assertIn('from "../lib/pager"', src, "用户页必须消费 lib/pager 的唯一切片实现")
        self.assertIn("data-usr-pager", src, "用户页必须有分页控件挂点")
        self.assertIn("<el-pagination", src, "分页控件必须走 EP（原生控件弹出层不可控）")

    def test_pager_module_is_single_slice_point(self):
        src = _read(PAGER)
        self.assertIn("export function pageSlice", src)
        self.assertIn("export function clampPage", src)
        self.assertIn("export const PAGE_SIZES", src)

    def test_no_native_select_or_range_in_pager_pages(self):
        """原生选择/滑条控件的弹出层不可控：两页的分页控件不得出现原生 <select>/type=range。"""
        for path in (ACCOUNTS, USERS):
            src = _read(path)
            self.assertNotIn("<select", src, f"{os.path.basename(path)} 不得用原生 <select>")
            self.assertNotIn('type="range"', src, f"{os.path.basename(path)} 不得用原生 range")


if __name__ == "__main__":
    unittest.main()
