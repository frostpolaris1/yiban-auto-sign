# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""SMTP 条目稳定 id 与额度/空态显示口径的前端行为测试（node 真跑）。

标签：F · 前端与界面守卫
覆盖：SMTP 行身份（id 随行不随位：渲染→删中间行→collect 的 id 序列必须跟着行走）、
      newSmtpId 形状与后端校验正则同口径、host 漂移时授权码占位文案改口、空发信清单的
      状态行提示；今日额度"已用/上限（剩）"计数口径三分支；执行体对 workers.configured=0
      的如实显示与 single_mode 提示显隐（静态钉点）
对应实现（2026-10-03 设置页整页迁 Vue 后换锚）：口径层
      `frontend/src/settings/model.js`（newSmtpId/smtpRowsFrom/collectSmtps/driftPlaceholder/
      mailStatusText/notifyStatusText/quotaPart）、渲染
      `frontend/src/settings/NotifyCard.vue`、执行体 `frontend/src/settings/ExecutorsCard.vue`
关键断言：删中间行后 collectSmtps 返回的 id 序列必须是 [第1条,第3条]（位置身份的回归形态是
      第3条滑到索引1）；无 id 旧条目渲染出的临时 id 必须匹配后端 `^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$`
      且行间互不相同；改 host 后 pass 占位必须变成"服务器已更换"话术、改回则恢复；额度可见时
      输出含"已用 x/y（剩 r）"且**不再有**只显余额的旧文案；quota_visible=false 与 null 余量
      （不限）两个分支不得混读
依赖：⚠ **需要 node 真跑**——函数按花括号配对从 model.js 抽出执行；node 缺失时整类 skipUnless。
      不联网
"""
import json
import os
import re
import shutil
import subprocess
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS_SRC = os.path.join(BASE, "frontend", "src", "settings")
MODEL_JS = os.path.join(SETTINGS_SRC, "model.js")
NODE = shutil.which("node")

# 与 web/routes/notify.py 的 _SMTP_ID_RE 同口径（后端正则若变动，本文件形状断言即红）
ID_SHAPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _extract_function(src, name):
    """按花括号配对抽出 `function <name>(...) { ... }`（含 `export function` 形态）。"""
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


# 纯函数依赖链：smtpRowsFrom → smtpRowId → newSmtpId + sentinelText（哨兵归一）；
# collectSmtps → clean；driftPlaceholder 独立。
_MAIL_FUNCS = ("clean", "sentinelText", "newSmtpId", "smtpRowId", "smtpRowsFrom",
               "collectSmtps", "driftPlaceholder")

_MAIL_HARNESS = r"""
var OUT = {};

__FUNCS__

var three = [
  { id: "smtp-aaaa00000001", host: "a.io", port: 465, user: "a***@io", has_pass: true },
  { id: "smtp-bbbb00000002", host: "b.io", port: 587, user: "b***@io", has_pass: true },
  { id: "smtp-cccc00000003", host: "c.io", port: 465, user: "c***@io", has_pass: false }
];
var rows = smtpRowsFrom(three);
OUT.rendered = collectSmtps(rows).map(function (e) { return e.id; });
// 删除中间行（数组按行身份重建，等价 legacy 的删 DOM 行），collect 的 id 序列必须跟着行走
rows.splice(1, 1);
OUT.afterDelete = collectSmtps(rows).map(function (e) { return e.id + "|" + e.host + "|" + e.port; });
// 旧配置（id=null）渲染：必须现造形状合法、彼此不同的 id
var legacyRows = smtpRowsFrom([
  { id: null, host: "a.io", port: 465, user: "a***@io", has_pass: true },
  { id: undefined, host: "b.io", port: 465, user: "b***@io", has_pass: false }
]);
var legacyIds = collectSmtps(legacyRows).map(function (e) { return e.id; });
OUT.legacyIds = legacyIds;
OUT.idShapeOk = legacyIds.length === 2 && legacyIds[0] !== legacyIds[1] &&
  legacyIds.every(function (v) { return /^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$/.test(v); });
// 脱敏字段不得回填成可编辑 value
OUT.maskedUserEmpty = rows[0].user === "" && rows[0].pass === "";
// host 漂移：改 host 后授权码占位必须当场改口（后端对漂移行不会沿用旧授权码）
var row0 = smtpRowsFrom(three)[0];
OUT.placeholderBefore = driftPlaceholder(row0, "a.io", 465).pass;
OUT.placeholderDrift = driftPlaceholder(row0, "evil-new.io", 465).pass;
OUT.placeholderRestored = driftPlaceholder(row0, "a.io", 465).pass;
console.log(JSON.stringify(OUT));
"""

_NOTIFY_HARNESS = r"""
var OUT = {};

__FUNCS__

OUT.visible = notifyStatusText({
  enabled: true, type: "serverchan", secret_masked: "SCT1***", urgent_only: false,
  configured: true, quota_visible: true,
  daily_max: 5, daily_remaining: 3, urgent_daily_max: 3, urgent_daily_remaining: 2
});
OUT.unlimited = notifyStatusText({
  enabled: true, type: "serverchan", secret_masked: "SCT1***",
  configured: true, quota_visible: true,
  daily_max: 0, daily_remaining: null, urgent_daily_max: 0, urgent_daily_remaining: null
});
OUT.hidden = notifyStatusText({
  enabled: false, configured: false, quota_visible: false,
  daily_max: 5, daily_remaining: null, urgent_daily_max: 3, urgent_daily_remaining: null
});
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
        src = _read(MODEL_JS)
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

    def test_masked_fields_are_not_backfilled(self):
        """脱敏的 user / pass 绝不回填成可编辑 value（否则按打码串落盘损坏配置）。"""
        self.assertTrue(self.out["maskedUserEmpty"])

    def test_host_drift_placeholder_follows(self):
        """改了 host 的行，授权码占位必须当场改口；改回则恢复原案。"""
        self.assertIn("已配置，留空沿用", self.out["placeholderBefore"])
        self.assertIn("服务器已更换", self.out["placeholderDrift"])
        self.assertIn("旧授权码不再沿用", self.out["placeholderDrift"])
        self.assertEqual(self.out["placeholderRestored"], "已配置，留空沿用")


@unittest.skipUnless(NODE, "node 不可用：跳过额度显示口径的前端行为测试")
class NotifyQuotaCaliberJsTest(unittest.TestCase):
    """额度状态行从"只显余额"改为"已用/上限（剩）"的占用计数口径。"""

    @classmethod
    def setUpClass(cls):
        src = _read(MODEL_JS)
        funcs = "\n\n".join(_extract_function(src, name) for name in ("notifyStatusText", "quotaPart"))
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
        src = _read(MODEL_JS)
        self.assertIn("无发信 SMTP，告警邮件一封都发不出去", src)

    def test_collect_carries_row_id(self):
        src = _read(MODEL_JS)
        body = _extract_function(src, "collectSmtps")
        self.assertIn("row.id", body, "collectSmtps 必须携带行 id——按位置提交就是回归")

    def test_save_status_gate_unchanged(self):
        """留空沿用/门禁等既有提交面不因 id 改动：smtps 仍只在 tableDirty 时提交。"""
        src = _read(MODEL_JS)
        self.assertIn("if (tableDirty) payload.smtps = collectSmtps(form.smtps);", src)

    def test_empty_row_hint_in_component(self):
        src = _read(os.path.join(SETTINGS_SRC, "NotifyCard.vue"))
        self.assertIn("尚未配置发件 SMTP", src)
        self.assertIn("告警邮件才可送达", src)


class ExecutorsZeroWorkersStaticTest(unittest.TestCase):
    """静态钉点：0 并行执行体的如实显示与单执行体形态提示。"""

    def test_single_mode_hint_present(self):
        src = _read(os.path.join(SETTINGS_SRC, "ExecutorsCard.vue"))
        self.assertIn("single_mode", src, "显隐判据必须取后端 single_mode，不自算")
        self.assertIn('id="set-exec-solo"', src, "组件必须挂上单执行体提示")
        self.assertIn("清单中没有「并行」执行体", src)
        self.assertIn(':hidden="!singleMode"', src, "提示默认必须藏起（仅 single_mode 时露出）")

    def test_kpi_still_reads_backend_configured(self):
        src = _read(os.path.join(SETTINGS_SRC, "ExecutorsCard.vue"))
        self.assertIn("configured", src, "KPI 分母仍须与后端同口径，不自算")


if __name__ == "__main__":
    unittest.main()
