# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""不可逆操作的「倒计时确认」前端行为测试（node 真跑，非静态扫描）。

## 为什么需要

后端把「危险操作一律输口令」改成三档（`YIBAN_PW_GATE`），档位**只存在于后端**：前端不再
自己判断档位，而是按响应体的 `reason` 分流——`delay_ack_required` 弹倒计时确认框、确认后带
`confirm_delay_ack: true` 重发；`password_required` / `password_incorrect` 弹既有口令框。
这条分流的载体是 `web/static/js/core.js` 的 `YB.dangerousSubmit`，倒计时框是
`YB.openDelayAckModal`。

静态扫描只能证明函数存在，证明不了**行为**：倒计时真的逐秒递减并在归零后才放行、重发的
请求体真的带上 `confirm_delay_ack`、凭据只补一次不无限重发、弹窗关闭后定时器真的被清掉。
故本文件照 `tests/test_logs_by_date.py` / `tests/test_dashboard_stats_caliber_js.py` 的做法，
把这两个函数从源码里按花括号配对抽出来，配一套最小 DOM/模态/计时器替身在 node 里真跑。

node 不可用时跳过（本套件其余部分不引入硬性 node 依赖）。
"""

import json
import os
import re
import shutil
import subprocess
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE_JS = os.path.join(BASE, "web", "static", "js", "core.js")
USER_OPS_JS = os.path.join(BASE, "web", "static", "js", "components", "user-ops.js")
ACCOUNT_OPS_JS = os.path.join(BASE, "web", "static", "js", "components", "account-ops.js")
SWITCHES_JS = os.path.join(BASE, "web", "static", "js", "components", "settings-switches.js")
JS_DIR = os.path.join(BASE, "web", "static", "js")
NODE = shutil.which("node")

PW_INPUT = "MasterPass#2026"


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _extract_function(src, name):
    """按花括号配对抽出 `function <name>(...) { ... }` 整段。"""
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
    raise AssertionError("JS 函数 %s 未找到匹配的右花括号" % name)


def _delay_seconds(src):
    m = re.search(r"var\s+DELAY_ACK_SECONDS\s*=\s*(\d+)\s*;", src)
    if not m:
        raise AssertionError("core.js 未找到 `var DELAY_ACK_SECONDS = <n>;`")
    return int(m.group(1))


#: node 里的最小替身 + 场景驱动。抽出函数依赖的名字（el/openModal/api/setInterval…）
#: 全部在此定义：这不是"复述实现"，而是把浏览器侧的唯一外部依赖替换成可观测的假件，
#: 让被测函数按真实控制流跑起来。
_HARNESS = r"""
var OUT = {};
var modalRecords = [], cleared = [], timers = {}, timerSeq = 0;
var calls = [], script = [], pwCalls = [], pwMode = "submit";
var PW_INPUT = __PW__;
var DELAY_ACK_SECONDS = __DELAY_SEC__;

function reset() {
  modalRecords = []; cleared = []; timers = {}; timerSeq = 0;
  calls = []; script = []; pwCalls = []; pwMode = "submit";
}
function setInterval(fn) { timerSeq += 1; timers[timerSeq] = fn; return timerSeq; }
function clearInterval(id) { cleared.push(id); delete timers[id]; }
function tick() { Object.keys(timers).forEach(function (id) { var fn = timers[id]; if (fn) fn(); }); }
function flush() { return new Promise(function (r) { setTimeout(r, 0); }); }
function gate(reason) { return { message: "gate:" + reason, data: { reason: reason } }; }

function api(method, path, body) {
  calls.push({ method: method, path: path, body: body });
  var step = script.shift();
  if (!step) return Promise.reject(new Error("no scripted response #" + calls.length));
  return step.err ? Promise.reject(step.err) : Promise.resolve(step.ok);
}
function openConfirmPasswordModal(desc, cb, onCancel) {
  pwCalls.push(desc);
  if (pwMode === "cancel") { if (onCancel) onCancel(); return Promise.resolve(); }
  return Promise.resolve().then(function () { return cb(PW_INPUT); });
}

function matchSel(node, sel) {
  if (!sel || sel.charAt(0) !== ".") return false;
  return (node.className || "").split(/\s+/).indexOf(sel.slice(1)) !== -1;
}
function find(node, sel) {
  var out = [];
  (function walk(n) {
    (n.children || []).forEach(function (c) { if (matchSel(c, sel)) out.push(c); walk(c); });
  })(node);
  return out;
}
function el(tag, attrs, children) {
  var node = {
    tagName: tag, className: "", textContent: "", disabled: false,
    children: [], attrs: {}, _handlers: {},
    classList: { add: function () {}, remove: function () {}, toggle: function () {} },
    setAttribute: function (k, v) { this.attrs[k] = v; if (k === "disabled") this.disabled = true; },
    removeAttribute: function (k) { delete this.attrs[k]; if (k === "disabled") this.disabled = false; },
    appendChild: function (c) { this.children.push(c); return c; },
    addEventListener: function (k, fn) { (this._handlers[k] = this._handlers[k] || []).push(fn); },
    click: function () {
      var self = this;
      (self._handlers.click || []).forEach(function (fn) { fn({ target: self }); });
    },
    querySelector: function (sel) { return find(this, sel)[0] || null; },
    querySelectorAll: function (sel) { return find(this, sel); },
    focus: function () {}
  };
  if (attrs) Object.keys(attrs).forEach(function (k) {
    var v = attrs[k];
    if (v == null || v === false) return;
    if (k === "class" || k === "className") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k === "dataset" || k === "style" || k === "html" || k === "for") { /* 本测试用不到 */ }
    else if (k.indexOf("on") === 0 && typeof v === "function") node.addEventListener(k.slice(2).toLowerCase(), v);
    else node.setAttribute(k, v === true ? "" : v);
  });
  if (children != null) [].concat(children).forEach(function (c) { if (c != null) node.appendChild(c); });
  return node;
}
function openModal(cfg) {
  var panel = el("div", { class: "pm-panel" });
  var foot = el("div", { class: "modal-foot" });
  var closed = false;
  function closeIt() { if (closed) return; closed = true; if (cfg.onClose) cfg.onClose(); }
  var handle = { panel: panel, close: closeIt };
  (cfg.actions || []).forEach(function (a) {
    var b = el("button", { type: "button", class: "btn btn--" + (a.variant || "ghost"), text: a.label || "" });
    b.addEventListener("click", function () {
      var res = a.onClick ? a.onClick(handle) : undefined;
      if (res !== false && a.close !== false) closeIt();
    });
    foot.appendChild(b);
  });
  panel.appendChild(foot);
  modalRecords.push(handle);
  return handle;
}

__FUNCS__

(async function () {
  // A：delay_ack_required → 倒计时框逐秒递减 → 归零后确认 → 重发带 confirm_delay_ack
  reset();
  script = [ { err: gate("delay_ack_required") }, { ok: { msg: "done" } } ];
  var pA = dangerousSubmit({ path: "/api/x", body: { a: 1 }, desc: "DESC" });
  await flush();
  OUT.a_modals = modalRecords.length;
  var okBtn = modalRecords[0].panel.querySelector(".modal-foot").querySelector(".btn--danger");
  OUT.a_initial_disabled = okBtn.disabled;
  OUT.a_initial_label = okBtn.textContent;
  OUT.a_labels = [];
  for (var i = 0; i < 4; i++) { tick(); OUT.a_labels.push(okBtn.textContent); }
  OUT.a_mid_disabled = okBtn.disabled;
  tick();
  OUT.a_final_disabled = okBtn.disabled;
  OUT.a_final_label = okBtn.textContent;
  OUT.a_cleared_at_zero = cleared.length > 0;
  okBtn.click();
  OUT.a_data = await pA;
  OUT.a_calls = calls.length;
  OUT.a_first_body = calls[0] && calls[0].body;
  OUT.a_resend = calls[1] && calls[1].body;

  // B：倒计时框取消 → 以 canceled 拒绝、不重发、定时器被清
  reset();
  script = [ { err: gate("delay_ack_required") } ];
  var pB = dangerousSubmit({ path: "/api/x", body: { b: 2 }, desc: "D" });
  await flush();
  modalRecords[0].panel.querySelector(".modal-foot").querySelector(".btn--ghost").click();
  var bErr = null;
  try { await pB; } catch (e) { bErr = { canceled: !!e.canceled, message: e.message }; }
  OUT.b_err = bErr;
  OUT.b_calls = calls.length;
  OUT.b_cleared = cleared.length;

  // C：password_required → 口令框 → 重发带 confirm_password（不弹倒计时框）
  reset();
  script = [ { err: gate("password_required") }, { ok: { msg: "pw-ok" } } ];
  OUT.c_data = await dangerousSubmit({ path: "/api/y", body: { c: 3 }, desc: "PW-DESC" });
  OUT.c_pw_calls = pwCalls.length;
  OUT.c_pw_desc = pwCalls[0];
  OUT.c_resend = calls[1] && calls[1].body;
  OUT.c_modals = modalRecords.length;

  // D：其余错误原样上抛，不弹框、不重发
  reset();
  script = [ { err: { message: "boom", data: {} } } ];
  var dErr = null;
  try { await dangerousSubmit({ path: "/api/z", body: { d: 4 }, desc: "D" }); }
  catch (e) { dErr = e.message; }
  OUT.d_err = dErr;
  OUT.d_calls = calls.length;
  OUT.d_modals = modalRecords.length;

  // E：凭据已补一次后后端仍拒绝 → 上抛，绝不无限重发
  reset();
  script = [ { err: gate("delay_ack_required") }, { err: gate("delay_ack_required") } ];
  var pE = dangerousSubmit({ path: "/api/w", body: { e: 5 }, desc: "D" });
  await flush();
  modalRecords[0].panel.querySelector(".modal-foot").querySelector(".btn--danger").click();
  var eErr = null;
  try { await pE; } catch (e) { eErr = { reason: e.data && e.data.reason }; }
  OUT.e_err = eErr;
  OUT.e_calls = calls.length;

  console.log(JSON.stringify(OUT));
})().catch(function (e) { console.error(e && e.stack || e); process.exit(1); });
"""


def _run_harness(core_src):
    funcs = "\n\n".join(
        _extract_function(core_src, name)
        for name in ("pwGateReason", "delayAckLabel", "openDelayAckModal", "dangerousSubmit")
    )
    script = (
        _HARNESS
        .replace("__PW__", json.dumps(PW_INPUT))
        .replace("__DELAY_SEC__", str(_delay_seconds(core_src)))
        .replace("__FUNCS__", funcs)
    )
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise AssertionError("node 执行失败：%s" % (proc.stderr or proc.stdout))
    return json.loads(proc.stdout.strip().splitlines()[-1])


@unittest.skipUnless(NODE, "node 不可用：跳过倒计时确认的前端行为测试")
class DelayAckFrontendTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = _read(CORE_JS)
        cls.out = _run_harness(cls.core)

    # ---- 倒计时框本身 ----
    def test_countdown_is_five_seconds(self):
        """倒计时秒数是契约里的 5 秒（前端常量；后端不校验秒数）。"""
        self.assertEqual(_delay_seconds(self.core), 5)

    def test_modal_starts_disabled_with_countdown_label(self):
        """框一出现，确认按钮禁用且显示剩余秒数（5s），先挡住手滑连点。"""
        self.assertEqual(self.out["a_modals"], 1, "delay_ack_required 必须弹出倒计时确认框")
        self.assertTrue(self.out["a_initial_disabled"], "倒计时内确认按钮必须禁用")
        self.assertEqual(self.out["a_initial_label"], "确认（5s）")

    def test_label_decrements_each_second(self):
        """按钮文案逐秒递减（4s→3s→2s→1s），且递减期间保持禁用。"""
        self.assertEqual(self.out["a_labels"], ["确认（4s）", "确认（3s）", "确认（2s）", "确认（1s）"])
        self.assertTrue(self.out["a_mid_disabled"], "归零前按钮始终禁用")

    def test_button_enables_at_zero(self):
        """归零后按钮启用并换成无秒数的确认文案。"""
        self.assertFalse(self.out["a_final_disabled"], "倒计时归零后按钮必须启用")
        self.assertEqual(self.out["a_final_label"], "确认执行")

    def test_timer_is_cleared_at_zero(self):
        """归零时立刻清掉定时器（否则弹窗已可点、interval 还在空转）。"""
        self.assertTrue(self.out["a_cleared_at_zero"], "归零路径必须 clearInterval")

    def test_timer_is_cleared_on_cancel(self):
        """取消关闭时也必须清掉定时器（否则弹窗关了 interval 还在跑）。"""
        self.assertEqual(self.out["b_cleared"], 1, "取消路径必须 clearInterval")
        self.assertEqual(self.out["b_err"], {"canceled": True, "message": ""},
                         "取消倒计时框应以 canceled 标记拒绝（调用方据此静默，不报失败）")

    # ---- 提交分流 ----
    def test_resend_carries_confirm_delay_ack(self):
        """倒计时确认后重发同一请求，且请求体带 confirm_delay_ack: true。"""
        self.assertEqual(self.out["a_calls"], 2, "只应重发一次（首发 + 补凭据重发）")
        self.assertEqual(self.out["a_first_body"], {"a": 1},
                         "首发不得自带 confirm_delay_ack（凭据由后端索要后才补）")
        self.assertEqual(self.out["a_resend"], {"a": 1, "confirm_delay_ack": True})
        self.assertEqual(self.out["a_data"], {"msg": "done"}, "重发成功后应把响应交给调用方")

    def test_cancel_does_not_resend(self):
        """取消倒计时框不重发请求（操作未发生）。"""
        self.assertEqual(self.out["b_calls"], 1)

    def test_password_required_uses_password_modal(self):
        """password_required 仍走既有口令框，重发带 confirm_password，不弹倒计时框。"""
        self.assertEqual(self.out["c_pw_calls"], 1, "应弹一次口令框")
        self.assertEqual(self.out["c_pw_desc"], "PW-DESC", "口令框文案应沿用调用方给的 desc")
        self.assertEqual(self.out["c_resend"], {"c": 3, "confirm_password": PW_INPUT})
        self.assertEqual(self.out["c_modals"], 0, "口令路径不得弹倒计时框")
        self.assertEqual(self.out["c_data"], {"msg": "pw-ok"})

    def test_other_errors_pass_through(self):
        """其余失败原样上抛，不弹框、不重发（沿用既有失败处理）。"""
        self.assertEqual(self.out["d_err"], "boom")
        self.assertEqual(self.out["d_calls"], 1)
        self.assertEqual(self.out["d_modals"], 0)

    def test_no_infinite_resend(self):
        """凭据补过一次后后端仍拒绝 ⇒ 上抛，绝不无限重发。"""
        self.assertEqual(self.out["e_calls"], 2, "最多首发 + 一次补凭据重发")
        self.assertEqual(self.out["e_err"], {"reason": "delay_ack_required"})


class IrreversibleCallSitesTest(unittest.TestCase):
    """静态钉点：不可逆操作改走新 helper，其余受门禁操作保持口令框流程。"""

    def test_core_exports_helper_and_countdown_modal(self):
        core = _read(CORE_JS)
        self.assertIn("function dangerousSubmit(", core)
        self.assertIn("function openDelayAckModal(", core)
        self.assertIn("dangerousSubmit: dangerousSubmit", core)
        self.assertIn("openDelayAckModal: openDelayAckModal", core)

    def test_ack_field_is_only_injected_by_helper(self):
        """`confirm_delay_ack` 只允许由 helper 注入：调用点不得手写该字段。

        调用点若各自拼一份，字段名/类型一旦漂移就会静默变成「没有凭据」被后端一直拒。
        """
        offenders = []
        for dirpath, _dirs, files in os.walk(JS_DIR):
            if os.sep + "vendor" + os.sep in dirpath + os.sep:
                continue
            for name in sorted(files):
                if not name.endswith(".js"):
                    continue
                path = os.path.join(dirpath, name)
                if os.path.abspath(path) == os.path.abspath(CORE_JS):
                    continue
                if "confirm_delay_ack" in _read(path):
                    offenders.append(os.path.relpath(path, BASE))
        self.assertEqual(offenders, [],
                         "confirm_delay_ack 只允许出现在 core.js 的 dangerousSubmit 里")

    def test_irreversible_ops_use_helper(self):
        """四类不可逆操作（删用户 / 清空账号 / purge / 批量删除清除）改走 dangerousSubmit。"""
        user_ops = _read(USER_OPS_JS)
        self.assertEqual(user_ops.count("YB.dangerousSubmit("), 4,
                         "user-ops 的 deleteUser / purge / batchDelete / batchPurge 都应改走 helper")
        self.assertEqual(_read(ACCOUNT_OPS_JS).count("YB.dangerousSubmit("), 2,
                         "account-ops 的单条 purge 与批量 purge 都应改走 helper")
        self.assertEqual(_read(SWITCHES_JS).count("YB.dangerousSubmit("), 1,
                         "急停（global_pause 0→1）应改走 helper")

    def test_non_irreversible_ops_keep_password_modal(self):
        """非不可逆的受门禁操作保持现状口令框流程（不擅自扩大软摩擦范围）。"""
        for name in ("settings-executors.js", "settings-mail.js", "settings-notify.js",
                     "settings-health.js", "settings-quota.js", "settings-schedule.js",
                     "account-form.js"):
            src = _read(os.path.join(JS_DIR, "components", name))
            self.assertIn("openConfirmPasswordModal", src, "%s 应保留口令框流程" % name)
            self.assertNotIn("dangerousSubmit", src, "%s 不应改走软摩擦提交" % name)

    def test_switches_keeps_password_modal_for_other_directions(self):
        """急停之外的开关方向（恢复签到 / 注册开关）仍走口令框。"""
        src = _read(SWITCHES_JS)
        self.assertIn("YB.openConfirmPasswordModal(", src)
        self.assertIn("body.confirm_password = pw;", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
