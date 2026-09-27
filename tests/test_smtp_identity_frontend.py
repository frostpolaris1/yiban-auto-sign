# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""SMTP 条目稳定 id 与额度/空态显示口径的前端行为测试（node 真跑）。

标签：F · 前端与界面守卫
覆盖：settings-mail.js 的行身份（data-smtp-id 随行不随位：渲染→删中间行→collect 的 id 序列必须跟着行走）、newSmtpId 形状与后端校验正则同口径、host 漂移时授权码占位文案改口、空发信清单的状态行提示；settings-notify.js 的今日额度"已用/上限（剩）"计数口径三分支；settings-executors.js 对 workers.configured=0 的如实显示与 single_mode 提示显隐（静态钉点，文案在 work_settings.html #set-exec-solo）
对应实现：`web/static/js/components/settings-mail.js`（newSmtpId/smtpRow/renderSmtps/collectSmtps/driftTargets/load）、`web/static/js/components/settings-notify.js`（renderStatus/quotaPart）、`web/static/js/components/settings-executors.js`（paintKpis 的 single_mode 显隐 + work_settings.html 的 #set-exec-solo 文案）
关键断言：删中间行后 collectSmtps 返回的 id 序列必须是 [第1条,第3条]（位置身份的回归形态是第3条滑到索引1）；无 id 旧条目渲染出的临时 id 必须匹配后端 `^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$` 且行间互不相同；改 host 后 pass 占位必须变成"服务器已更换"话术、改回则恢复；额度可见时输出含"已用 x/y（剩 r）"且**不再有**只显余额的旧文案；quota_visible=false 与 null 余量（不限）两个分支不得混读
依赖：⚠ **需要 node 真跑**——函数按花括号配对从组件源码抽出，配最小 DOM 替身执行；node 缺失时整类 skipUnless，静态钉点与 node 用例同类，故本机无 node 时本文件零用例执行。不联网
"""
import json
import os
import re
import shutil
import subprocess
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPONENTS = os.path.join(BASE, "web", "static", "js", "components")
NODE = shutil.which("node")

# 与 web/routes/notify.py 的 _SMTP_ID_RE 同口径（后端正则若变动，本文件形状断言即红）
ID_SHAPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _extract_function(src, name):
    """按花括号配对抽出 `function <name>(...) { ... }`（与 test_delay_ack_frontend 同法）。"""
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
    raise AssertionError("花括号未配对：" + name)


_MAIL_FUNCS = ("clean", "newSmtpId", "cellInput", "maskedCellInput", "emptyRow",
               "smtpRow", "renumber", "driftTargets", "renderSmtps", "collectSmtps")

# 最小 DOM 替身：只实现 settings-mail 用到的属性/查询/子节点操作。
# 替身刻意"什么都能建"，但断言面收窄到 id 与文案——DOM 仿真不达标会红，不会假绿。
_MAIL_HARNESS = r"""
var OUT = {};
function camelToKebab(k) { return k.replace(/[A-Z]/g, function (m) { return "-" + m.toLowerCase(); }); }
function makeNode(tag) {
  var n = {
    tag: tag, attrs: {}, dataset: {}, children: [], parentNode: null, value: "",
    placeholder: "", textContent: "", listeners: {},
    setAttribute: function (k, v) { this.attrs[k] = String(v); },
    getAttribute: function (k) { return (k in this.attrs) ? this.attrs[k] : null; },
    appendChild: function (c) { c.parentNode = this; this.children.push(c); return c; },
    removeChild: function (c) {
      var i = this.children.indexOf(c);
      if (i >= 0) this.children.splice(i, 1);
      c.parentNode = null; return c;
    },
    addEventListener: function (t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); },
    fire: function (t) { (this.listeners[t] || []).forEach(function (fn) { fn(); }); },
    querySelectorAll: function (sel) {
      var out = [];
      (function walk(node) {
        node.children.forEach(function (c) {
          if (matches(c, sel)) out.push(c);
          walk(c);
        });
      })(this);
      return out;
    },
    querySelector: function (sel) { return this.querySelectorAll(sel)[0] || null; }
  };
  Object.defineProperty(n, "firstChild", { get: function () { return this.children[0] || null; } });
  return n;
}
function matches(node, sel) {
  var m = /^(?:([a-zA-Z0-9]+))?(\[([a-zA-Z0-9-]+)(?:="([^"]*)")?\])?$/.exec(sel);
  if (!m) throw new Error("替身不支持的选择器: " + sel);
  if (m[1] && node.tag !== m[1]) return false;
  if (m[3] && !(m[3] in node.attrs)) return false;
  if (m[4] != null && node.attrs[m[3]] !== m[4]) return false;
  return !!(m[1] || m[3]);
}
var BODY = makeNode("tbody");
var document = { querySelector: function (sel) { return sel === "#sm-smtps tbody" ? BODY : null; } };
function tbody() { return document.querySelector("#sm-smtps tbody"); }
var ctx = { isMaster: true };
function markDirty() {}
var YB = {
  el: function (tag, opts) {
    var n = makeNode(tag);
    opts = opts || {};
    Object.keys(opts).forEach(function (k) {
      var v = opts[k];
      if (k === "class") { n.attrs["class"] = v; }
      else if (k === "text") { n.textContent = String(v); }
      else if (k === "placeholder") { n.attrs[k] = v; n.placeholder = v; }
      else if (k === "dataset") { Object.keys(v).forEach(function (d) { n.dataset[d] = String(v[d]); n.attrs["data-" + camelToKebab(d)] = String(v[d]); }); }
      else { n.attrs[k] = v; }
    });
    return n;
  }
};

__FUNCS__

var three = [
  { id: "smtp-aaaa00000001", host: "a.io", port: 465, user: "a***@io", has_pass: true },
  { id: "smtp-bbbb00000002", host: "b.io", port: 587, user: "b***@io", has_pass: true },
  { id: "smtp-cccc00000003", host: "c.io", port: 465, user: "c***@io", has_pass: false }
];
renderSmtps(three);
OUT.rendered = collectSmtps().map(function (e) { return e.id; });
// 删除中间行（走真实 click 处理器），collect 的 id 序列必须跟着行走而不是滑位
BODY.querySelectorAll("tr")[1].querySelector("button").fire("click");
OUT.afterDelete = collectSmtps().map(function (e) { return e.id + "|" + e.host + "|" + e.port; });
// 旧配置（id=null）渲染：必须现造形状合法、彼此不同的 id
renderSmtps([
  { id: null, host: "a.io", port: 465, user: "a***@io", has_pass: true },
  { id: undefined, host: "b.io", port: 465, user: "b***@io", has_pass: false }
]);
var legacyIds = collectSmtps().map(function (e) { return e.id; });
OUT.legacyIds = legacyIds;
OUT.idShapeOk = legacyIds.length === 2 && legacyIds[0] !== legacyIds[1] &&
  legacyIds.every(function (v) { return /^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$/.test(v); });
// host 漂移：改 host 后授权码占位必须当场改口（后端对漂移行不会沿用旧授权码）
renderSmtps(three);
var row0 = BODY.querySelectorAll("tr")[0];
var passInp = row0.querySelector('[data-f="pass"]');
OUT.placeholderBefore = passInp.placeholder;
row0.querySelector('[data-f="host"]').value = "evil-new.io";
driftTargets();
OUT.placeholderDrift = passInp.placeholder;
row0.querySelector('[data-f="host"]').value = "a.io";
driftTargets();
OUT.placeholderRestored = passInp.placeholder;
// 空清单渲染：占位行明确提示（不再静默）
renderSmtps([]);
OUT.emptyRowText = BODY.querySelector("p") ? BODY.querySelector("p").textContent : "";
console.log(JSON.stringify(OUT));
"""

_NOTIFY_HARNESS = r"""
var OUT = {};
var CAP = { textContent: "" };
function $(id) { return id === "sn-status" ? CAP : null; }

__FUNCS__

renderStatus({
  enabled: true, type: "serverchan", secret_masked: "SCT1***", urgent_only: false,
  configured: true, quota_visible: true,
  daily_max: 5, daily_remaining: 3, urgent_daily_max: 3, urgent_daily_remaining: 2
});
OUT.visible = CAP.textContent;
renderStatus({
  enabled: true, type: "serverchan", secret_masked: "SCT1***",
  configured: true, quota_visible: true,
  daily_max: 0, daily_remaining: null, urgent_daily_max: 0, urgent_daily_remaining: null
});
OUT.unlimited = CAP.textContent;
renderStatus({
  enabled: false, configured: false, quota_visible: false,
  daily_max: 5, daily_remaining: null, urgent_daily_max: 3, urgent_daily_remaining: null
});
OUT.hidden = CAP.textContent;
console.log(JSON.stringify(OUT));
"""


def _run(node_src):
    if not NODE:
        raise AssertionError("node 不可用")
    proc = subprocess.run([NODE, "-e", node_src], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise AssertionError("node 执行失败：%s" % (proc.stderr or proc.stdout))
    return json.loads(proc.stdout.strip().splitlines()[-1])


@unittest.skipUnless(NODE, "node 不可用：跳过 SMTP 行身份的前端行为测试")
class SmtpRowIdentityJsTest(unittest.TestCase):
    """collectSmtps 的身份必须随行不随位（MF-51 前端侧反例）。"""

    @classmethod
    def setUpClass(cls):
        src = _read(os.path.join(COMPONENTS, "settings-mail.js"))
        funcs = "\n\n".join(_extract_function(src, name) for name in _MAIL_FUNCS)
        cls.out = _run(_MAIL_HARNESS.replace("__FUNCS__", funcs))

    def test_rendered_rows_carry_ids(self):
        self.assertEqual(self.out["rendered"],
                         ["smtp-aaaa00000001", "smtp-bbbb00000002", "smtp-cccc00000003"])

    def test_middle_row_delete_shifts_nothing(self):
        """删中间行后，剩余两条的 id 序列必须是 [第1条, 第3条]。

        位置身份时代的回归形态：第3条滑到索引1——后端按索引取旧凭据时就会
        沿用被删那条的授权码。id 随行，这条路径在渲染层就断掉。
        """
        self.assertEqual(self.out["afterDelete"],
                         ["smtp-aaaa00000001|a.io|465", "smtp-cccc00000003|c.io|465"])

    def test_legacy_null_ids_get_fresh_valid_ids(self):
        """旧格式条目（id=null）渲染时现造 id：形状与后端校验同口径且行间不撞。"""
        self.assertTrue(self.out["idShapeOk"], self.out["legacyIds"])

    def test_host_drift_placeholder_follows(self):
        """改了 host 的行，授权码占位必须当场改口；改回则恢复原案。"""
        self.assertIn("已配置，留空沿用", self.out["placeholderBefore"])
        self.assertIn("服务器已更换", self.out["placeholderDrift"])
        self.assertIn("旧授权码不再沿用", self.out["placeholderDrift"])
        self.assertEqual(self.out["placeholderRestored"], "已配置，留空沿用")

    def test_empty_list_has_explicit_hint(self):
        """空清单不再静默：占位行必须明确说出"尚未配置发件 SMTP、告警不可送达"。"""
        self.assertIn("尚未配置发件 SMTP", self.out["emptyRowText"])
        self.assertIn("告警邮件才可送达", self.out["emptyRowText"])


@unittest.skipUnless(NODE, "node 不可用：跳过额度显示口径的前端行为测试")
class NotifyQuotaCaliberJsTest(unittest.TestCase):
    """额度状态行从"只显余额"改为"已用/上限（剩）"的占用计数口径。"""

    @classmethod
    def setUpClass(cls):
        src = _read(os.path.join(COMPONENTS, "settings-notify.js"))
        funcs = "\n\n".join(_extract_function(src, name) for name in ("renderStatus", "quotaPart"))
        cls.out = _run(_NOTIFY_HARNESS.replace("__FUNCS__", funcs))

    def test_used_count_visible_not_only_remaining(self):
        text = self.out["visible"]
        self.assertIn("非紧急 已用 2/5（剩 3）", text)
        self.assertIn("紧急 已用 1/3（剩 2）", text)
        self.assertIn("未必等于已送达", text, "占用口径与送达口径的差别必须写在脸上")
        self.assertNotIn("剩余 3 条", text, "旧的「只显余额」文案不得残留")

    def test_unlimited_and_hidden_branches(self):
        self.assertIn("非紧急 不限", self.out["unlimited"])
        self.assertIn("仅主管理员可见", self.out["hidden"],
                      "quota_visible=false 不得被读成「不限」")


class SmtpMailEmptyStatusStaticTest(unittest.TestCase):
    """静态钉点：开关开着但清单为空时，状态行必须自己说破（不靠表格小字）。"""

    def test_status_line_names_the_empty_list(self):
        src = _read(os.path.join(COMPONENTS, "settings-mail.js"))
        self.assertIn("无发信 SMTP，告警邮件一封都发不出去", src)

    def test_collect_carries_row_id(self):
        src = _read(os.path.join(COMPONENTS, "settings-mail.js"))
        body = _extract_function(src, "collectSmtps")
        self.assertIn('getAttribute("data-smtp-id")', body,
                      "collectSmtps 必须携带行 id——按位置提交就是回归")

    def test_save_status_gate_unchanged(self):
        """留空沿用/门禁等既有提交面不因 id 改动：smtps 仍只在 tableDirty 时提交。"""
        src = _read(os.path.join(COMPONENTS, "settings-mail.js"))
        self.assertIn("if (tableDirty) body.smtps = collectSmtps();", src)


class ExecutorsZeroWorkersStaticTest(unittest.TestCase):
    """静态钉点：0 并行执行体的如实显示与单执行体形态提示。

    提示分两半钉（settings-executors.js 登记上限已满）：组件按后端
    workers.single_mode 显隐模板里的 #set-exec-solo 文案——两边都在，
    任何一侧被删都红。
    """

    def test_single_mode_hint_present(self):
        src = _read(os.path.join(COMPONENTS, "settings-executors.js"))
        self.assertIn("workers.single_mode", src, "显隐判据必须取后端 single_mode，不自算")
        self.assertIn('$("set-exec-solo")', src, "组件必须挂上模板里的单执行体提示")
        tpl = _read(os.path.join(BASE, "web", "templates", "pages", "work_settings.html"))
        self.assertIn('id="set-exec-solo"', tpl)
        self.assertIn("清单中没有「并行」执行体", tpl)
        self.assertIn("hidden", tpl.split('id="set-exec-solo"')[0][-400:] +
                      tpl.split('id="set-exec-solo"')[1][:120],
                      "提示默认必须藏起（hidden 在标签附近）")

    def test_kpi_still_reads_backend_configured(self):
        src = _read(os.path.join(COMPONENTS, "settings-executors.js"))
        self.assertIn("workers.configured", src, "KPI 分母仍须与后端同口径，不自算")


if __name__ == "__main__":
    unittest.main()
