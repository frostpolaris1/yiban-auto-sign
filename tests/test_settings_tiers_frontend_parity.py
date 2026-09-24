# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""档位表与设置页前端控件的双向对拍。

背景：`POST /api/settings` 的口令门禁、403 判定与变更告警全部由
`MASTER_ONLY_KEYS | GATED_KEYS | {GLOBAL_PAUSE_KEY}` 驱动，前端则按自己的控件清单决定
"要不要先问口令"。两边各写一遍就必然漂移，而漂移的后果是双向的：

- 前端多写一个键（拼错、或自造）→ 那个键永远不进 403 名单、不要口令、不发变更告警；
- 后端新增一个档位键而前端不知道 → 前端不带口令提交，用户看到 403 或"没反应"。

所以本文件同时钉两个方向，且**不允许靠注释满足**：判定只认代码里的赋值形态
（`body.<键> = …`）与带引号的键字面量（动态键写法 `body[field]` 的配套处），
头注里列一串键名不算——那正是漂移的老家。
"""

import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_DIR = os.path.join(BASE, "web", "static", "js")
COMPONENTS = os.path.join(JS_DIR, "components")

# 会写 /api/settings 的前端组件。少登记一个，对拍就漏一个文件——由
# `test_settings_writers_are_all_covered` 反向守住。
_SETTINGS_WRITERS = (
    "settings-schedule.js",
    "settings-quota.js",
    "settings-health.js",
    "settings-switches.js",
)

# 写请求体里**不是**设置项的字段（协议字段，与档位无关）。这些字段由受门禁提交 helper
# 按后端下发的 `reason` 注入，设置写组件不再各自直写（各写一份必然漂移）——故"该字段仍被
# 声明"的证据是 helper 里出现它，而不是组件里的 `body.<键> = …`。
_NON_SETTING_FIELDS = {
    "confirm_password": "口令复核字段：受门禁提交 helper 在 password_required 时注入",
}

# 档位表里前端**没有**控件的键：豁免必须写理由，且日后有 UI 时必须删掉这条（见下方向一）。
_NO_UI_KEYS = {
    "start_delay_max": "启动随机延迟上限只有 .env 与接口入口，设置页未提供控件",
    "sign_mode": "签到模式已无 UI 控件（后端同口径）",
    "window_edge_sec": "旧前端兼容键：新前端只提交独立的 edge_front_sec/edge_back_sec，"
                       "该键在前端只读回填、不作为控件提交",
}

# `body.<键> = …` 赋值形态（前端承诺的直写形态，供本对拍静态读取）
_ASSIGN_RE = re.compile(r"\bbody\.([a-z_][a-z0-9_]*)\s*=")
# 带引号的字面量，与档位键取交集后作为"该键在前端有落点"的证据（动态键写法
# `body[field] = …` 的键从调用参数/比较式进来，只能这样认）。
# 与档位键取交集是关键：裸扫小写标识符会把 "click"/"change" 这类事件名当成键。
_LITERAL_RE = re.compile(r"[\"']([a-z_][a-z0-9_]*)[\"']")
# 真正写设置的调用形态（只认 YB.api("POST", "/api/settings"，不认注释与 GET 回填）
_POST_SETTINGS_RE = re.compile(r"YB\.api\(\s*[\"']POST[\"']\s*,\s*[\"']/api/settings[\"']")
# 注入表达式：`confirm_password: pw` / `confirm_password = pw` 这类**真的把值传下去**的
# 形态（右侧必须有内容）。只认"整份源码里出现过该键"会把注释、文案里的键名也算成证据。
_INJECTION_RE = r"\b%s\s*[:=]\s*\S"


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _strip_js_comments(src):
    """剥掉 `//` 与 `/* */` 注释，只留可执行代码（字符串内的 `//` 不动）。

    "该字段仍被声明"的证据必须是代码：注释里写一个键名不算——那正是防空挂豁免要防的
    形态（把注入删掉、只在注释里留个键名，登记就永远是"有效"的）。只删不增，故最坏
    情况是判得更严（漏判会在这里响亮地失败），不会把注释放行成证据。
    """
    out, i, n, quote = [], 0, len(src), None
    while i < n:
        c = src[i]
        if quote:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(src[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in "\"'`":
            quote = c
            out.append(c)
            i += 1
            continue
        if src[i:i + 2] == "//":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if src[i:i + 2] == "/*":
            end = src.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _injected_fields(src):
    """源码里以注入表达式出现的非设置字段（右侧为空不算），供"登记不得空挂"判定。"""
    code = _strip_js_comments(src)
    return {k for k in _NON_SETTING_FIELDS
            if re.search(_INJECTION_RE % re.escape(k), code)}


def _tier_keys(webapp):
    """后端两张表 + 急停键（与门禁判定同源，不另抄一份）。"""
    return (set(webapp.MASTER_ONLY_KEYS) | set(webapp.GATED_KEYS)
            | {webapp.GLOBAL_PAUSE_KEY})


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 只读 web/app.py 拿常量：不建 app、不碰 .env/DB（对拍是静态契约）
        import contextlib
        import importlib.util
        import sys
        spec = importlib.util.spec_from_file_location(
            "webapp_tier_parity", os.path.join(BASE, "web", "app.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["webapp_tier_parity"] = mod
        # 只取模块级常量：create_app 不执行（模块导入期的副作用与常量无关）
        with contextlib.suppress(Exception):
            spec.loader.exec_module(mod)
        cls.webapp = mod
        cls.keys = _tier_keys(mod)
        cls.sources = {name: _read(os.path.join(COMPONENTS, name))
                       for name in _SETTINGS_WRITERS}

    def _assigned_keys(self):
        """四个设置写组件里以 `body.<键> = …` 直写形态声明的键（严格形态）。"""
        found = set()
        for src in self.sources.values():
            found |= set(_ASSIGN_RE.findall(src))
        return found

    def _present_keys(self):
        """"该键在前端有落点"的证据：直写键 ∪ 与档位键相交的字面量。"""
        found = self._assigned_keys()
        for src in self.sources.values():
            found |= set(_LITERAL_RE.findall(src)) & self.keys
        return found


class WriterCoverageTest(_Base):
    """新增一个写 /api/settings 的组件，必须同时登记进对拍清单。"""

    def test_settings_writers_are_all_covered(self):
        offenders = []
        for root, _dirs, files in os.walk(JS_DIR):
            for name in files:
                if not name.endswith(".js"):
                    continue
                text = _read(os.path.join(root, name))
                if _POST_SETTINGS_RE.search(text):
                    rel = os.path.relpath(os.path.join(root, name), COMPONENTS)
                    if rel.replace(os.sep, "/") not in _SETTINGS_WRITERS:
                        offenders.append(rel)
        self.assertEqual(offenders, [],
                         f"这些前端文件也写 /api/settings，但没进对拍清单：{offenders}")


class FrontendToTierTest(_Base):
    """方向一：前端直写声明的每个键都必须是档位键（或已登记的非设置字段）。"""

    def test_every_frontend_key_is_tiered(self):
        unknown = self._assigned_keys() - self.keys - set(_NON_SETTING_FIELDS)
        self.assertEqual(
            unknown, set(),
            "这些键由前端直写提交但不在档位表里——不会进 403 名单、不要口令、不发变更告警；"
            f"拼错同样会落到这里：{sorted(unknown)}",
        )

    def test_non_setting_fields_are_still_declared(self):
        """登记的非设置字段若连 helper 也不再注入，登记本身就该删（防空挂豁免）。

        证据只认注释被剥掉之后的**注入表达式**（`confirm_password: pw`）：注释或文案里
        提到该键不算——否则把注入删掉、只在注释里留个键名，这条豁免就永远"有效"。
        """
        injected = _injected_fields(_read(os.path.join(JS_DIR, "core.js")))
        stale = sorted(set(_NON_SETTING_FIELDS) - injected)
        self.assertEqual(stale, [],
                         f"这些非设置字段已无处声明（注释里的键名不算证据），豁免失去意义：{stale}")

    def test_comment_mention_is_not_evidence(self):
        """判别力自检：只在注释里出现的键名不得被认成"仍被声明"。"""
        self.assertEqual(_injected_fields("// confirm_password: 由 helper 注入\n"), set())
        self.assertEqual(_injected_fields("var x = 1; /* confirm_password = pw */"), set())
        self.assertEqual(_injected_fields("withExtra(extra, { confirm_password: pw })"),
                         {"confirm_password"})


class TierToFrontendTest(_Base):
    """方向二：每个档位键要么有前端落点，要么在带理由的豁免表里。"""

    def test_every_tier_key_has_ui_or_exemption(self):
        missing = self.keys - self._present_keys() - set(_NO_UI_KEYS)
        self.assertEqual(
            missing, set(),
            "这些档位键在前端没有任何控件，也没有登记豁免——新增档位键时必须同步设置页，"
            f"否则用户提交时只会看到 403：{sorted(missing)}",
        )

    def test_exemptions_are_still_needed(self):
        """豁免表不能空挂：一旦某个键有了 UI，必须把豁免删掉（否则它脱离对拍）。"""
        stale = [k for k in _NO_UI_KEYS if k in self._present_keys()]
        self.assertEqual(stale, [],
                         f"这些键已经有前端落点了，请从豁免表里删掉：{sorted(stale)}")

    def test_exemptions_have_reasons(self):
        for key, reason in _NO_UI_KEYS.items():
            self.assertTrue(str(reason).strip(), f"{key} 的豁免必须写理由")


if __name__ == "__main__":
    unittest.main()
