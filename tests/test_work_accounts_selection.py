# -*- coding: utf-8 -*-
"""账号页选中集的身份键：手机号，而非列表 index。

标签：F · 前端与界面守卫（Vue 线）
覆盖：`frontend/src/accounts/model.js` 的选中解析在 node 里真跑——列表重排后仍指向同一账号
对应实现：`byPhone(accounts, phone)` / `selectedAccounts(accounts, sel, group)` /
      `selectedIds` / `selectedPhones`（选中集以手机号为键）
关键断言：`move` 改变持久化顺序（index 全变）后，选中仍解析到同一手机号（及其新 index）
依赖：⚠ **需要 node 真跑**——四个纯函数（纯 JS 模块，非 TS）抽出后在 node 中执行，
      `shutil.which("node")` 取不到时整类 skip

**为什么需要**：index 是列表位置，不是账号身份。`move` 改持久化顺序后同一 index 指向
另一个账号——按 index 记选中会让"上移一个账号"把勾选粘到邻居身上，随后批量删除/清除
即误伤他人。选中必须锚在稳定身份（手机号）上。
"""
import json
import os
import shutil
import subprocess
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACCOUNTS_MODEL_JS = os.path.join(BASE, "frontend", "src", "accounts", "model.js")
NODE = shutil.which("node")

_FUNCS = ("byPhone", "selectedAccounts", "selectedIds", "selectedPhones")


def _extract_function(src, name):
    start = src.index("function " + name + "(")
    i = src.index("{", start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError("函数 %s 未找到匹配的右花括号" % name)


def _run(accounts, sel):
    with open(ACCOUNTS_MODEL_JS, encoding="utf-8") as fh:
        src = fh.read()
    fns = "\n".join(_extract_function(src, n) for n in _FUNCS)
    script = (
        "var accounts = %s, sel = %s;\n" % (json.dumps(accounts), json.dumps(sel))
        + fns + "\n"
        + "console.log(JSON.stringify({ids: selectedIds(accounts, sel, 'active'), "
          "phones: selectedPhones(accounts, sel, 'active')}));\n"
    )
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30,
                          encoding="utf-8")
    if proc.returncode != 0:
        raise AssertionError("node 执行失败：%s" % (proc.stderr or proc.stdout))
    return json.loads(proc.stdout.strip().splitlines()[-1])


@unittest.skipUnless(NODE, "node 不可用：跳过账号页选中身份键的 JS 行为测试")
class SelectionIdentityTest(unittest.TestCase):
    def test_selection_survives_reorder(self):
        """重排前后同一选中项解析到同一手机号 / 其新 index。"""
        accounts = [
            {"index": 0, "phone": "111****0001"},
            {"index": 1, "phone": "222****0002"},
            {"index": 2, "phone": "333****0003"},
        ]
        before = _run(accounts, {"active": {"222****0002": True}})
        self.assertEqual(before["phones"], ["222****0002"])
        self.assertEqual(before["ids"], [1])

        # move：222 上移到首位 ⇒ index 全部重排，index 1 现在是 111
        reordered = [
            {"index": 0, "phone": "222****0002"},
            {"index": 1, "phone": "111****0001"},
            {"index": 2, "phone": "333****0003"},
        ]
        after = _run(reordered, {"active": {"222****0002": True}})
        self.assertEqual(after["phones"], ["222****0002"], "重排后仍指向同一账号")
        self.assertEqual(after["ids"], [0], "index 随之更新，而不是钉在原位置")

    def test_stale_selection_is_dropped(self):
        """账号已不在列表时，选中项不解析出任何目标（不回落成别人的 index）。"""
        out = _run([{"index": 0, "phone": "111****0001"}], {"active": {"999****9999": True}})
        self.assertEqual(out, {"ids": [], "phones": []})


if __name__ == "__main__":
    unittest.main(verbosity=2)
