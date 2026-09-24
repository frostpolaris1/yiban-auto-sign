# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""受门禁操作提交的前端行为测试（node 真跑，非静态扫描）。

## 为什么需要

后端把「危险操作一律输口令」改成三档（`YIBAN_PW_GATE`），档位**只存在于后端**：前端不再
自己判断档位，而是**先不带任何凭据发请求**，再按响应体的 `reason` 分流——
`delay_ack_required` 弹倒计时确认框、确认后带 `confirm_delay_ack: true` 重发；
`password_required` / `password_incorrect` 弹既有口令框、口令随重发提交。
这条分流的载体是 `web/static/js/core.js` 的 `YB.dangerousSubmit`，倒计时框是
`YB.openDelayAckModal`。

静态扫描只能证明函数存在，证明不了**行为**：倒计时真的逐秒递减并在归零后才放行、重发的
请求体真的带上 `confirm_delay_ack`、普通受门禁操作首发真的不带凭据、一次点击要发多个受
门禁请求时凭据真的从失败那一步续上、凭据只补一次不无限重发、弹窗关闭后定时器真的被清掉。
故本文件照 `tests/test_logs_by_date.py` / `tests/test_dashboard_stats_caliber_js.py` 的做法，
把这些函数从源码里按花括号配对抽出来，配一套最小 DOM/模态/计时器替身在 node 里真跑。

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
COMPONENTS = os.path.join(BASE, "web", "static", "js", "components")
PAGES = os.path.join(BASE, "web", "static", "js", "pages")
JS_DIR = os.path.join(BASE, "web", "static", "js")
NODE = shutil.which("node")

PW_INPUT = "MasterPass#2026"

# 受门禁写操作所在组件：每个都必须经统一 helper，不得再自带无条件口令框管道
_GATED_COMPONENTS = (
    "user-ops.js",
    "account-ops.js",
    "account-form.js",
    "settings-executors.js",
    "settings-mail.js",
    "settings-notify.js",
    "settings-health.js",
    "settings-quota.js",
    "settings-schedule.js",
    "settings-switches.js",
    "my-mail-notify.js",
)

# 唯一允许在 core.js 之外直接弹口令框的文件：自助域收的是**本人账号口令**
# （`/api/me/delete`、`/api/me/restore` 的 `password` 字段），不经敏感口令门、后端也不下发
# reason，故没有"先发后补"的余地。新增任何一条都要先问后端有没有 reason 协议。
_PW_MODAL_ALLOWED = {
    "my-accounts-page.js": "自助注销/撤销注销收本人账号口令，不经敏感口令门",
}

# 直接开出口令框的写法：`openConfirmPasswordModal(` 直呼，或 `openPwModal(..., "confirm")`
# ——后者是同一个框的另一条入口（mode="confirm" 就是"要当前口令"），只钉前者会被它绕过。
_OPEN_PW_MODAL_RE = re.compile(r"\bopenPwModal\s*\(")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _call_args(src, open_idx):
    """从 `(` 处按括号配对取出实参文本（跳过字符串），用于判字面量模式。"""
    depth, quote, i = 0, None, open_idx
    while i < len(src):
        c = src[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"'`":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return src[open_idx + 1:i]
        i += 1
    return src[open_idx + 1:]


def _pw_modal_calls(src):
    """源码里"直接开当前口令框"的证据（调用写法），空列表 = 没这条管道。"""
    hits = []
    if "openConfirmPasswordModal" in src:
        hits.append("openConfirmPasswordModal")
    for m in _OPEN_PW_MODAL_RE.finditer(src):
        args = _call_args(src, m.end() - 1)
        if '"confirm"' in args or "'confirm'" in args:
            hits.append('openPwModal(..., "confirm")')
    return hits


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

  // B：倒计时框取消 → 以 canceled 拒绝、不重发、定时器被清，且已提交步数为 0
  reset();
  script = [ { err: gate("delay_ack_required") } ];
  var pB = dangerousSubmit({ path: "/api/x", body: { b: 2 }, desc: "D" });
  await flush();
  modalRecords[0].panel.querySelector(".modal-foot").querySelector(".btn--ghost").click();
  var bErr = null;
  try { await pB; } catch (e) { bErr = { canceled: !!e.canceled, message: e.message, completed: e.completed }; }
  OUT.b_err = bErr;
  OUT.b_calls = calls.length;
  OUT.b_cleared = cleared.length;

  // C：password_required → 口令框 → 重发带 confirm_password（不弹倒计时框）
  reset();
  script = [ { err: gate("password_required") }, { ok: { msg: "pw-ok" } } ];
  OUT.c_data = await dangerousSubmit({ path: "/api/y", body: { c: 3 }, desc: "PW-DESC" });
  OUT.c_pw_calls = pwCalls.length;
  OUT.c_pw_desc = pwCalls[0];
  OUT.c_first_body = calls[0] && calls[0].body;
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

  // F：普通受门禁操作（非不可逆）首发不带凭据；后端直接放行 ⇒ 一次请求、零弹窗
  reset();
  script = [ { ok: { msg: "plain-ok" } } ];
  OUT.f_data = await dangerousSubmit({ path: "/api/plain", body: { f: 6 }, desc: "D" });
  OUT.f_calls = calls.length;
  OUT.f_body = calls[0] && calls[0].body;
  OUT.f_modals = modalRecords.length;
  OUT.f_pw_calls = pwCalls.length;

  // G：一次点击两个受门禁请求：第一步要口令 ⇒ 从失败那一步续上，已成功的第一步不重发
  reset();
  script = [ { err: gate("password_required") }, { ok: { msg: "s1" } }, { ok: { msg: "s2" } } ];
  OUT.g_data = await dangerousSubmit({
    requests: [
      { method: "PUT", path: "/api/r1", body: { r: 1 } },
      { method: "PUT", path: "/api/r2", body: { r: 2 } }
    ], desc: "D"
  });
  OUT.g_calls = calls.length;
  OUT.g_paths = calls.map(function (c) { return c.path; });
  OUT.g_bodies = calls.map(function (c) { return c.body; });
  OUT.g_notes = (OUT.g_data || []).map(function (d) { return d && d.msg; });

  // H：口令框取消 ⇒ 以 canceled 拒绝、不重发
  reset();
  pwMode = "cancel";
  script = [ { err: gate("password_required") } ];
  var hErr = null;
  try { await dangerousSubmit({ path: "/api/h", body: { h: 7 }, desc: "D" }); }
  catch (e) { hErr = { canceled: !!e.canceled }; }
  OUT.h_err = hErr;
  OUT.h_calls = calls.length;

  // I：多段提交的第一步已成功、第二步索要口令而用户取消 ⇒ 错误必须带回"已提交 1 步"。
  // 调用方（执行体保存）据此才能告诉用户"部分修改已提交、不会回滚"，否则那次真写入
  // 既没人刷新视图、也没人提示——库里已经变了而页面还显示旧值。
  reset();
  pwMode = "cancel";
  script = [ { ok: { msg: "s1" } }, { err: gate("password_required") } ];
  var iErr = null;
  try {
    await dangerousSubmit({
      requests: [
        { method: "PUT", path: "/api/r1", body: { r: 1 } },
        { method: "PUT", path: "/api/r2", body: { r: 2 } }
      ], desc: "D"
    });
  } catch (e) { iErr = { canceled: !!e.canceled, completed: e.completed }; }
  OUT.i_err = iErr;
  OUT.i_calls = calls.length;

  // J：同上，但第二步索要的是倒计时确认（不可逆那一路）——两条取消路径口径必须一致
  reset();
  script = [ { ok: { msg: "s1" } }, { err: gate("delay_ack_required") } ];
  var pJ = dangerousSubmit({
    requests: [
      { method: "PUT", path: "/api/r1", body: { r: 1 } },
      { method: "PUT", path: "/api/r2", body: { r: 2 } }
    ], desc: "D"
  });
  await flush();
  modalRecords[0].panel.querySelector(".modal-foot").querySelector(".btn--ghost").click();
  var jErr = null;
  try { await pJ; } catch (e) { jErr = { canceled: !!e.canceled, completed: e.completed }; }
  OUT.j_err = jErr;
  OUT.j_calls = calls.length;

  // K：第一步就索要口令而用户取消 ⇒ 一步都没提交（completed 必须是 0，不能谎报部分写入）
  reset();
  pwMode = "cancel";
  script = [ { err: gate("password_required") } ];
  var kErr = null;
  try {
    await dangerousSubmit({
      requests: [{ method: "PUT", path: "/api/r1", body: { r: 1 } }], desc: "D"
    });
  } catch (e) { kErr = { canceled: !!e.canceled, completed: e.completed }; }
  OUT.k_err = kErr;
  OUT.k_calls = calls.length;

  // L：多段提交的第一步已成功、第二步被后端打回（**非取消失败**）⇒ 错误同样带回"已提交 1 步"。
  // 与取消路径是同一个洞：调用方拿不到这个数，就只能把错误显示在横幅里而页面照旧停在旧数据上——
  // 库里那一半改动既没人刷新也没人提示。
  reset();
  script = [ { ok: { msg: "s1" } }, { err: { message: "boom2", data: {} } } ];
  var lErr = null;
  try {
    await dangerousSubmit({
      requests: [
        { method: "PUT", path: "/api/r1", body: { r: 1 } },
        { method: "PUT", path: "/api/r2", body: { r: 2 } }
      ], desc: "D"
    });
  } catch (e) { lErr = { canceled: !!e.canceled, message: e.message, completed: e.completed }; }
  OUT.l_err = lErr;
  OUT.l_calls = calls.length;

  // M：第一步就失败（非取消）⇒ 一步都没提交，completed 必须是 0（不得谎报部分写入）
  reset();
  script = [ { err: { message: "boom3", data: {} } } ];
  var mErr = null;
  try {
    await dangerousSubmit({
      requests: [
        { method: "PUT", path: "/api/r1", body: { r: 1 } },
        { method: "PUT", path: "/api/r2", body: { r: 2 } }
      ], desc: "D"
    });
  } catch (e) { mErr = { canceled: !!e.canceled, message: e.message, completed: e.completed }; }
  OUT.m_err = mErr;
  OUT.m_calls = calls.length;

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


@unittest.skipUnless(NODE, "node 不可用：跳过受门禁提交的前端行为测试")
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
        self.assertEqual(self.out["b_err"], {"canceled": True, "message": "", "completed": 0},
                         "取消倒计时框应以 canceled 标记拒绝（调用方据此静默）；"
                         "单请求形式一步都没提交，已提交步数必须是 0")

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
        self.assertEqual(self.out["c_first_body"], {"c": 3},
                         "首发不得自带 confirm_password（普通受门禁操作也是先发后补）")
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

    # ---- 普通受门禁操作（非不可逆）与多请求提交 ----
    def test_plain_gated_op_sends_without_credentials_first(self):
        """后端放行时（off / risk 同出口）一次点击只发一次请求，且**零弹窗、零凭据**。

        这正是"摩擦减掉了"的可证伪形态：若某调用点仍自带无条件口令框管道，这里会出现
        第二次请求或一次弹窗；若把凭据预先拼进请求体，`f_body` 就不再是原样。
        """
        self.assertEqual(self.out["f_calls"], 1, "后端未索要凭据时不得重发")
        self.assertEqual(self.out["f_body"], {"f": 6}, "首发请求体不得含任何凭据字段")
        self.assertEqual(self.out["f_modals"], 0, "后端未索要凭据时不得弹任何框")
        self.assertEqual(self.out["f_pw_calls"], 0)
        self.assertEqual(self.out["f_data"], {"msg": "plain-ok"})

    def test_multi_request_submission_resumes_at_failing_step(self):
        """一次点击要发多个受门禁请求时：凭据从**失败那一步**续上，已成功的步骤不重发。

        否则"改出口 + 同时拨故障转移开关"这类保存要么把第一步写两遍（两次审计），
        要么各弹一次口令框。
        """
        self.assertEqual(self.out["g_paths"], ["/api/r1", "/api/r1", "/api/r2"],
                         "第一步补口令后重发，成功后接第二步；第一步不得再发第三次")
        self.assertEqual(self.out["g_bodies"], [{"r": 1}, {"r": 1, "confirm_password": PW_INPUT},
                                                {"r": 2, "confirm_password": PW_INPUT}],
                         "凭据对整串共用：重发与后续步骤都带同一次口令")
        self.assertEqual(self.out["g_notes"], ["s1", "s2"],
                         "多请求形式 resolve 各步响应（调用方按步取 msg/note）")

    def test_password_modal_cancel_is_marked_and_does_not_resend(self):
        """取消口令框 ⇒ 以 canceled 拒绝、不重发（取消不是失败，调用方据此静默）。"""
        self.assertEqual(self.out["h_err"], {"canceled": True})
        self.assertEqual(self.out["h_calls"], 1)

    # ---- 取消≠什么都没发生：多段提交的已提交步数 ----
    def test_multi_request_cancel_reports_completed_steps(self):
        """多段提交的第一步已落库、第二步取消 ⇒ 错误带回 `completed`=1 且不重发。

        这是"部分写入不得被静默"的可证伪形态：调用方（执行体保存）只有拿到这个数，
        才能重载视图并告诉用户"部分修改已提交、不会回滚"；没有它，那次真写入既没人
        刷新也没人提示——库里已改而页面仍旧。
        """
        self.assertEqual(self.out["i_err"], {"canceled": True, "completed": 1},
                         "已成功提交的步数必须随取消回传")
        self.assertEqual(self.out["i_calls"], 2, "取消后不得重发第二步")

    def test_countdown_cancel_also_reports_completed_steps(self):
        """两条取消路径（口令框 / 倒计时框）口径一致：都回传已提交步数。"""
        self.assertEqual(self.out["j_err"], {"canceled": True, "completed": 1})
        self.assertEqual(self.out["j_calls"], 2)

    def test_cancel_before_any_step_reports_zero_completed(self):
        """首步被取消 ⇒ 一步都没提交，`completed` 必须是 0（不得谎报部分写入）。"""
        self.assertEqual(self.out["k_err"], {"canceled": True, "completed": 0})
        self.assertEqual(self.out["k_calls"], 1)

    def test_multi_request_plain_failure_also_reports_completed_steps(self):
        """多段提交的第一步已落库、第二步被后端打回（**非取消**）⇒ 错误同样带回 `completed`=1。

        失败与取消在这一点上没有区别：被打回之前成功的步骤已经落盘，调用方拿不到这个数
        就只能把错误显示在横幅里、页面照旧停在旧数据上——库里那一半改动既没人刷新也没人
        提示。差别只在提示语，收尾必须走同一条路。
        """
        self.assertEqual(self.out["l_err"],
                         {"canceled": False, "message": "boom2", "completed": 1},
                         "非取消失败同样必须回传已成功提交的步数")
        self.assertEqual(self.out["l_calls"], 2, "失败后不得重发第二步")

    def test_plain_failure_before_any_step_reports_zero_completed(self):
        """首步就失败 ⇒ 一步都没提交，`completed` 必须是 0（"无需重载"的判据）。"""
        self.assertEqual(self.out["m_err"],
                         {"canceled": False, "message": "boom3", "completed": 0})
        self.assertEqual(self.out["m_calls"], 1)


class ExecutorSaveCancelTest(unittest.TestCase):
    """执行体保存的收尾：多段提交被打断后必须重载视图并交代已生效的部分。

    node 侧的用例能钉住 helper 回传的步数，钉不住调用方拿这个数做了什么（组件依赖
    真实 DOM 与模态，不在本文件的替身覆盖范围内）——故这里对组件源码做一次结构化钉点：
    取消分支必须走 `canceledAfter`、非取消失败分支必须走 `failedAfter`，而两者必须复用
    同一个收尾实现（重载视图 + 把已提交步数讲出来）。
    """

    def setUp(self):
        self.src = _read(os.path.join(COMPONENTS, "settings-executors.js"))

    def test_cancel_branch_routes_to_dedicated_handler(self):
        self.assertIn("if (e && e.canceled) return canceledAfter(e);", self.src,
                      "取消弹窗不得静默 return false：必须先刷新视图再提示")

    def test_plain_failure_branch_routes_to_reload_handler(self):
        """非取消失败不得只落一条横幅：第二步回 400 时第一步已经落盘，同样是"已改一半"。"""
        self.assertIn("return failedAfter(e, failWord);", self.src,
                      "非取消失败分支必须走重载收尾，而不是停在 failTip")

    def test_both_handlers_share_the_partial_commit_closing(self):
        """两条收尾共用 `partialAfter`：必须重载视图、讲明已落盘步数、且顺序不能反。

        各写一份就会各自漂移：一处把提示落在重载之后，另一处照旧被 `load()` 末尾的清屏抹掉。
        """
        self.assertIn("function partialAfter(", self.src)
        tail = _extract_function(self.src, "partialAfter")
        self.assertIn("load()", tail, "收尾必须重载视图（用户据此后看到库里的真实状态）")
        self.assertIn("setTip(", tail, "部分提交必须给一条明确提示")
        self.assertIn("已经写入配置", tail, "提示必须讲明那几步已经落盘、不会回滚")
        for name in ("canceledAfter", "failedAfter"):
            self.assertIn("function " + name + "(", self.src)
            body = _extract_function(self.src, name)
            self.assertIn("completed", body, name + " 必须读 helper 回传的已提交步数")
            self.assertIn("partialAfter(", body, name + " 必须复用同一收尾")


class GatedCallSitesTest(unittest.TestCase):
    """静态钉点：所有受门禁操作都经统一 helper，口令框管道不再各自为政。"""

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

    def test_every_gated_component_routes_through_helper(self):
        """受门禁写操作所在组件都必须出现 `YB.dangerousSubmit(`，不得只有无条件口令框。"""
        missing = [name for name in _GATED_COMPONENTS
                   if "YB.dangerousSubmit(" not in _read(os.path.join(COMPONENTS, name))]
        self.assertEqual(missing, [], "这些组件仍有受门禁操作没走统一 helper：%s" % missing)

    def test_irreversible_ops_use_helper(self):
        """不可逆操作（删用户 / 清空账号 / 批量删除清除 / purge / 急停）走 dangerousSubmit。

        计数是**下界**：每个不可逆落点至少一次，多出来的正是同一 helper 承接的普通受门禁操作。
        """
        user_ops = _read(os.path.join(COMPONENTS, "user-ops.js"))
        self.assertGreaterEqual(user_ops.count("YB.dangerousSubmit("), 4,
                                "user-ops 的 deleteUser / purge / batchDelete / batchPurge 都应改走 helper")
        account_ops = _read(os.path.join(COMPONENTS, "account-ops.js"))
        self.assertGreaterEqual(account_ops.count("YB.dangerousSubmit("), 2,
                                "account-ops 的单条 purge 与批量 purge 都应改走 helper")
        switches = _read(os.path.join(COMPONENTS, "settings-switches.js"))
        self.assertGreaterEqual(switches.count("YB.dangerousSubmit("), 1,
                                "急停（global_pause 0→1）应改走 helper")

    def test_password_modal_pipeline_only_remains_for_self_service(self):
        """core.js 之外只剩自助域直接弹口令框：它们收的是本人账号口令，没有 reason 协议。

        这条钉住的是"别再长出第二条无条件口令框管道"——新增一条就会在这里失败，
        提示先确认后端是否有对应的 reason 字段（有则改走 helper）。判定同时覆盖
        `openPwModal(..., "confirm")`：那是同一个"要当前口令"的框，只钉
        `openConfirmPasswordModal(` 会留下一条等效旁路（helper 与它都已导出）。
        """
        offenders = {}
        for dirpath, _dirs, files in os.walk(JS_DIR):
            if os.sep + "vendor" + os.sep in dirpath + os.sep:
                continue
            for name in sorted(files):
                if not name.endswith(".js"):
                    continue
                path = os.path.join(dirpath, name)
                if os.path.abspath(path) == os.path.abspath(CORE_JS):
                    continue
                hits = _pw_modal_calls(_read(path))
                if hits:
                    offenders[name] = (os.path.relpath(path, BASE), hits)
        self.assertEqual(sorted(offenders), sorted(_PW_MODAL_ALLOWED),
                         "口令框管道只剩登记的自助域；其余受门禁操作请改走 helper：%s" % offenders)

    def test_confirm_mode_pw_modal_is_detected(self):
        """判别力自检：`openPwModal(..., "confirm")` 必须被认成口令框管道，set 模式不算。"""
        confirm = 'YB.openPwModal("DESC", function (pw) { return send(pw); }, "confirm");'
        self.assertEqual(_pw_modal_calls(confirm), ['openPwModal(..., "confirm")'])
        self.assertEqual(_pw_modal_calls('YB.openPwModal("d", cb, "set");'), [])
        self.assertEqual(_pw_modal_calls("YB.openPasswordModal('d', cb);"), [])
        self.assertEqual(_pw_modal_calls("YB.openConfirmPasswordModal('d', cb);"),
                         ["openConfirmPasswordModal"])

    def test_allowed_self_service_sites_are_really_self_service(self):
        """豁免不能空挂：登记的每个文件都要真的在（文件改名/删除时豁免必须一起处置）。"""
        for name, reason in _PW_MODAL_ALLOWED.items():
            self.assertTrue(reason.strip(), "%s 的豁免必须写理由" % name)
            self.assertTrue(os.path.exists(os.path.join(COMPONENTS, name)),
                            "%s 已不存在，请从豁免表里删掉" % name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
