# -*- coding: utf-8 -*-
"""账号写操作的防重入（在途时后续触发早退）。

标签：F · 前端与界面守卫
覆盖：`frontend/src/accounts/ops.js` 在 node 里真跑——`restore`/`move` 与批量 `batch` 重入被挡、实时在途结束
对应实现：`create(ctx)` 内的 `inflight` 守卫（`run` / `submit` / `signin` 共用）
关键断言：在途中的第二次触发不得再发请求；在途结束后再次触发必须放行（守卫不粘住）
依赖：⚠ **需要 node 真跑**——整份组件源码在 node 中执行，`shutil.which("node")` 取不到时整类 skip

**为什么需要**：`ctx.busy` 只被页面用来暂停轮询，不挡重复触发。`restore`/`move` 这类
无确认弹窗的入口双击即重发同一请求；批量入口的确认弹窗也只挡一次点击。守卫必须在
组件内、且必须释放——粘住的守卫会让后续操作静默失效，比不用守卫更坏。
"""
import json
import os
import shutil
import subprocess
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACCOUNT_OPS_JS = os.path.join(BASE, "frontend", "src", "accounts", "ops.js")
NODE = shutil.which("node")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _run(driver):
    """把整份 account-ops.js 交给 node 执行（桩只给 YB 命名空间与 ctx）。"""
    harness = (
        "var apiCalls = [];\n"
        "var window = {YB: {\n"
        "  api: function (m, p) { apiCalls.push([m, p]); return Promise.resolve({}); },\n"
        "  toast: { success: function () {}, error: function () {} },\n"
        "  confirmDialog: function () { return Promise.resolve(true); },\n"
        "  promptDialog: function () { return Promise.resolve(null); },\n"
        "  dangerousSubmit: function (o) { apiCalls.push(['DANGER', o.path]); return Promise.resolve({}); }\n"
        "}};\n"
        "var YB = window.YB;\n"
        "var ctx = { busy: function () {}, refresh: function () { return Promise.resolve(); },\n"
        "            onBatchSuccess: function () {}, onBatchDelete: function () {} };\n"
    )
    script = harness + _read(ACCOUNT_OPS_JS) + "\nvar ops = window.YB.accountOps.create(ctx);\n" + driver + (
        "\nsetTimeout(function () { console.log('RESULT' + JSON.stringify({calls: apiCalls})); }, 60);\n"
    )
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30,
                          encoding="utf-8")
    if proc.returncode != 0:
        raise AssertionError("node 执行失败：%s\n%s" % (proc.stderr, script[:300]))
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT")]
    if not line:
        raise AssertionError("node 无结果输出：%r" % proc.stdout)
    return json.loads(line[-1][len("RESULT"):])


@unittest.skipUnless(NODE, "node 不可用：跳过账号写操作防重入的 JS 行为测试")
class AccountOpsReentryTest(unittest.TestCase):
    def test_single_op_blocks_reentry_but_releases(self):
        """同步双击只发一次；首个请求结束后守卫释放，下一次触发可发。"""
        out = _run(
            "ops.restore({index: 1, phone: '138****0000'});\n"
            "ops.restore({index: 1, phone: '138****0000'});\n"
            "ops.move({index: 1, phone: '138****0000'}, -1);\n"
            "setTimeout(function () { ops.move({index: 1, phone: '138****0000'}, 1); }, 20);\n"
        )
        calls = out["calls"]
        self.assertEqual(calls[0], ["POST", "/api/accounts/1/restore"])
        # 同 tick 的第二次 restore 与 move 都必须被挡；await 后 move 放行
        self.assertEqual(
            calls, [["POST", "/api/accounts/1/restore"], ["POST", "/api/accounts/1/move"]],
            "在途中的重复触发不得发请求，在途结束后必须放行")

    def test_batch_blocks_reentry_after_confirm(self):
        """批量入口在确认后重入同样被挡——弹窗只挡一次点击。"""
        out = _run(
            "ops.batch('approve', [1], ['138****0000']);\n"
            "ops.batch('approve', [1], ['138****0000']);\n"
        )
        self.assertEqual(out["calls"], [["POST", "/api/accounts/batch"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
