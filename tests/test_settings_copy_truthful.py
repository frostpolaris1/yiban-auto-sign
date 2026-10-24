# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""设置/公告的权限文案必须如实反映默认口令档位（工单 495-D1）。

标签：E · Web：认证/权限/API
覆盖：六处用户/开发者可见文案，加同形状缺陷的五处载体（共 11 个区域）：
      ① `web/services/env_io.py` 的 A/B 档与急停说明；
      ② `web/routes/settings_api.py` 的 A 档注释、公告发布 docstring 与下线注释；
      ③ `README.md` 的公告端点用法；
      ④ `frontend/src/settings/ScheduleCard.vue` 的权限提示；
      ⑤ `frontend/src/settings/AnnouncementCard.vue` 的权限提示与卡头注释；
      ⑥ `docs/dev/api-contract.md` 的 `POST /api/settings` 口令门说明；
      ⑦ `web/app.py` 统一口令门的参数说明与高危门禁说明。
      另加**行级允许清单形状门**：对入库文案载体（py/vue/ts/js/sh/md，含 docs/）扫
      `当次输?口令|需(?:当次)?口令|口令确认`。命中行**一律**要按 file:line 在
      `CLAIM_ALLOWLIST` 登记并写理由才放行；带档位词**不自动放行**（审查员对抗样本：
      「档位说明：A 档保存必须当次输口令。」「缺省 risk 档下，公告发布必须当次输口令。」
      都必须被点名）。清单外的口令断言一律红。
关键断言：① 11 个区域**不得**再宣称「A 档保存 / 公告发布必须当次输口令」，且必须
    同时写出「同出口」与 `full` 档例外；② `api-contract` 的 `global_pause` 不得回到
    「`full` 档要口令 ＋ 不可逆确认」的旧形状；③ 全仓载体零未登记的口令断言；
    ④ 扫描面与命中数都有下限（扫空不得当绿，AGENTS §30）；⑤ 扫描器有牙：
    带档位词的合成失实句必红，已登记句必绿；⑥ 允许清单无死条目。
判据是**行为事实**：缺省 `risk` 档下，同出口的已登录会话直接生效（`web/app.py`
    的早退分支；档位矩阵由 `tests/test_pw_gate_tiers.py` 的 `RISK_EXEMPT` 钉住）。
    文案说「要口令」是缺陷，改文案是修复，不是放松。
告警面的形状门**豁免**（显式登记，AGENTS §28）：执行体写（`settings_api.py:111-144`）
    与公告草稿（`settings_api.py:1628-1636`）仍会外发告警；`变更告警|紧急告警` 一条
    正则分不开真假（实测 50+ 处真语句）。故告警面保留定点区域检查：设置保存不外发
    告警，只留审计行（`settings_api.py:840-843`；`test_web_security_gates.py`
    `ChangeAlertTest` 断言 `alerts == []`）。
    口令面正则口径：`当次输?口令|需(?:当次)?口令|口令确认`，扫 276 个生产载体 ＋
    14 个 docs 文件（2026-10-24 实测，共 289），命中 16 行；16 行全部登记在允许清单。
    构建产物 `web/static/vue/` 不在扫描面：它由 `check-vue-build-reproducible.sh`
    与源码对拍，源码改了不重建即红。
依赖：只读入库文本（UTF-8）；不起服务、无网络、无第三方库。
"""
import ast
import os
import pathlib
import re
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 每处文案都必须同时出现这两个语义锚：同出口免口令；full 档例外。
MARKERS = ("同出口", "full")

#: 旧文案里的失实断言。改文案时逐条删除；重新写回任何一条即红。
FALSE_CLAIMS = (
    "必须当次输口令",
    "403 + 当次口令",
    "仅主管理员 + **当次口令**",
    "（仅主管理员 + 当次口令）",
    "当次口令且不吃豁免",
    "保存时仍需口令确认",
    "（需当次口令）",
    "且需当次口令",
    "当次口令 + 占用高危额度",
    "= 必须当次输口令",
    '本函数走的全部是"必须当次输口令"的动作',
    "主管理员 + 当次口令）",
    "变更仍发告警",
    "紧急告警",
    "变更告警",
)

#: 形状扫描面：入库文案载体（含 docs/）。tests/CHANGELOG/构建产物不在面内。
_CARRIER_DIRS = ("web", "yiban", "frontend/src", "scripts", "docs")
_CARRIER_EXTS = {".py", ".vue", ".ts", ".js", ".sh", ".md"}
_CARRIER_EXCLUDE = ("static/vue/", "node_modules/", "__pycache__/",
                    "protocol_fake/", "tests/", "golden/")

#: 「宣称有口令要求」的文案形状。刻意不收裸「要口令」：它多为流程描述。
_PWD_CLAIM = re.compile(r"当次输?口令|需(?:当次)?口令|口令确认")

#: 扫描面下限（实测 289 ＝ 276 个生产载体 ＋ 14 个 docs 文件，2026-10-24，口径
#: ＝本文件 `_carrier_files`）。下限 285 高于「不含 docs」的 276：docs 掉出扫描面即红。
MIN_CARRIER_FILES = 285
#: 口令句下限（同日实测 16 行）。为 0 说明读错了目录，不许当绿。
MIN_CLAIM_LINES = 3

#: 行级允许清单：(相对路径, 行号) -> (该行必须包含的原文片段, 放行理由)。
#: 只放行「已核实为真」的口令句。新增/挪行必须同步改这里，否则红。
#: 行号是 2026-10-24 实测坐标；`needle` 同时钉住内容，防止行号漂移后静默放行。
CLAIM_ALLOWLIST = {
    ("frontend/src/settings/AnnouncementCard.vue", 97): (
        "full 档每次需口令", "公告卡档位文案：risk 同出口直接生效，full 才要"),
    ("frontend/src/settings/ScheduleCard.vue", 248): (
        "full 档保存时需口令确认", "设置卡档位文案：risk 同出口直接保存，full 才要"),
    ("scripts/backup.sh", 259): (
        "无需口令", "GPG 公钥无口令，非本站登录口令门"),
    ("web/app.py", 2308): (
        "同出口也必须当次输口令", "risk_always 四类：同出口也要口令"),
    ("web/app.py", 2351): (
        "full 档下必须当次输口令", "门禁参数说明：full 才要，risk 同出口免"),
    ("web/app.py", 2388): (
        "在 full 档都要当次输口令", "高危门禁说明：full 才要，risk 同出口免"),
    ("web/routes/accounts_api.py", 608): (
        "同出口也必须当次口令", "改他人易班凭据属 risk_always 四类"),
    ("web/routes/notify.py", 202): (
        "须当次口令并占 creds", "admin_to 改道 risk_always：同出口也要"),
    ("web/routes/notify.py", 204): (
        "在 risk 档同出口也要当次口令", "SMTP 凭据变更 risk_always"),
    ("web/routes/notify.py", 361): (
        "在 risk 档同出口也要当次口令", "SMTP 凭据变更 risk_always"),
    ("web/routes/notify.py", 506): (
        "当次口令**", "关闭通道/自定义推送地址 risk_always：同出口也要"),
    ("web/routes/users_api.py", 391): (
        "**当次口令**", "角色变更属 risk_always 四类"),
    ("web/routes/users_api.py", 393): (
        "同出口也必须当次输口令", "角色变更与提权 risk_always"),
    ("web/routes/users_api.py", 423): (
        "同出口也要当次口令", "risk_always 声明：缺省档同出口也要"),
    ("web/routes/users_api.py", 615): (
        "无当次口令", "非 full 档免口令，仅留事后告警补偿"),
    ("web/security.py", 145): (
        "每个受保护操作都要当次口令", "full 档准绳：逐条走完三段判定"),
}


def _norm(text):
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _read(rel):
    with open(os.path.join(BASE, rel), encoding="utf-8") as f:
        return _norm(f.read())


def _after(text, anchor, lines):
    """从 `anchor` 起取 `lines` 行。锚点缺失返回空串——调用方必须把空串判红。"""
    idx = text.find(anchor)
    if idx < 0:
        return ""
    return "\n".join(text[idx:].splitlines()[:lines])


def _p_body(text, elem_id):
    """取 `id="<elem_id>"` 那个 `<p>` 里的 `<span class="body">` 正文。"""
    m = re.search(
        r'id="%s"[^>]*>.*?<span class="body">(.*?)</span>'
        % re.escape(elem_id), text, re.S)
    return m.group(1) if m else ""


def _docstring(rel, func_name):
    tree = ast.parse(_read(rel))
    for node in ast.walk(tree):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == func_name):
            return ast.get_docstring(node) or ""
    return ""


def _env_io_region():
    return _after(_read("web/services/env_io.py"),
                  "A 档（`MASTER_ONLY_KEYS`）一次改动", 9)


def _env_io_change_detect_doc():
    return _after(_read("web/services/env_io.py"),
                  "只用于\"这次请求到底改没改配置\"的判定", 5)


def _settings_save_comment():
    return _after(_read("web/routes/settings_api.py"),
                  "本次请求的 A 档键集", 5)


def _settings_publish_docstring():
    return _docstring("web/routes/settings_api.py", "api_announcement_publish")


def _settings_save_tail():
    return _after(_read("web/routes/settings_api.py"),
                  "变更不再逐次外发告警", 5)


def _readme_region():
    return _after(_read("README.md"), "# POST /api/announcement/publish", 4)


def _schedule_card_body():
    return _p_body(_read("frontend/src/settings/ScheduleCard.vue"), "ss-perm")


def _announcement_card_body():
    return _p_body(_read("frontend/src/settings/AnnouncementCard.vue"), "set-ann-perm")


def _announcement_card_header():
    return "\n".join(_read("frontend/src/settings/AnnouncementCard.vue").splitlines()[:10])


def _api_contract_region():
    return _after(_read("docs/dev/api-contract.md"),
                  "- 鉴权档：管理员。CSRF：**要**。**分档口令门**", 8)


def _app_gate_param_doc():
    return _after(_read("web/app.py"),
                  "`always_required=True` = full 档下", 3)


def _app_high_risk_doc():
    return _after(_read("web/app.py"),
                  "本函数走的动作在 full 档都要当次输口令", 3)


def _settings_publish_offline_comment():
    return _after(_read("web/routes/settings_api.py"),
                  "双人发布的后半程，权限档完全一致", 2)


#: (名字, 取区域函数)。区域为空即判红（扫空不得当绿，AGENTS §30）。
SITES = (
    ("env_io A/B 档与急停", _env_io_region),
    ("settings_api A 档注释", _settings_save_comment),
    ("settings_api 公告发布 docstring", _settings_publish_docstring),
    ("README 公告端点用法", _readme_region),
    ("ScheduleCard 权限提示", _schedule_card_body),
    ("AnnouncementCard 权限提示", _announcement_card_body),
    ("AnnouncementCard 卡头注释", _announcement_card_header),
    ("api-contract 设置口令门", _api_contract_region),
    ("web/app.py 门禁参数说明", _app_gate_param_doc),
    ("web/app.py 高危门禁说明", _app_high_risk_doc),
    ("settings_api 下线注释", _settings_publish_offline_comment),
)

#: 告警面定点区域：只要求「不得宣称设置变更发告警」，不要求口令档位语义。
ALERT_SITES = (
    ("env_io A/B 档与急停", _env_io_region),
    ("env_io 变更判定说明", _env_io_change_detect_doc),
    ("settings_api 保存尾部", _settings_save_tail),
)


def _excluded(path):
    s = path.as_posix()
    return any(x in s for x in _CARRIER_EXCLUDE)


def _carrier_files(root):
    root = pathlib.Path(root)
    files = []
    readme = root / "README.md"
    if readme.is_file():
        files.append(readme)
    for d in _CARRIER_DIRS:
        base = root / d
        if not base.is_dir():
            continue
        files.extend(p for p in base.rglob("*")
                     if p.suffix in _CARRIER_EXTS and p.is_file()
                     and not _excluded(p.relative_to(root)))
    return sorted(set(files))


def _scan_password_claims(root, allowlist=None):
    """扫载体里的口令要求句。

    返回 (文件数, 命中行数, 放行行数, 违规行, 实际命中的清单键)。
    命中 = 正则命中，**与是否带档位词无关**；放行只看允许清单。
    """
    if allowlist is None:
        allowlist = CLAIM_ALLOWLIST
    root = pathlib.Path(root)
    files = _carrier_files(root)
    claims = 0
    allowed = 0
    bad = []
    seen = set()
    for p in files:
        try:
            lines = _norm(p.read_text(encoding="utf-8")).splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        rel = p.relative_to(root).as_posix()
        for i, ln in enumerate(lines, 1):
            if not _PWD_CLAIM.search(ln):
                continue
            claims += 1
            key = (rel, i)
            seen.add(key)
            entry = allowlist.get(key)
            if entry is None:
                bad.append("%s:%d: 未登记的口令断言: %s" % (rel, i, ln.strip()))
                continue
            needle, _reason = entry
            if needle not in ln:
                bad.append("%s:%d: 清单文本过期（期望含 %r）: %s"
                           % (rel, i, needle, ln.strip()))
                continue
            allowed += 1
    return len(files), claims, allowed, bad, seen


class SettingsCopyTruthfulTest(unittest.TestCase):
    """11 个区域必须同时满足「无失实断言」与「带同出口/full 语义」。"""

    def test_regions_are_non_empty(self):
        """取区域函数本身要有牙：锚点搬家或选择器改名时必须红，不许静默扫空。"""
        for name, get in SITES + ALERT_SITES:
            with self.subTest(site=name):
                self.assertGreater(len(get()), 20, "%s 的文案区域取到空串" % name)

    def test_no_false_password_claim(self):
        """区域不得宣称 A 档保存 / 公告发布必须当次输口令，也不得宣称设置变更发告警。"""
        for name, get in SITES + ALERT_SITES:
            region = get()
            for claim in FALSE_CLAIMS:
                with self.subTest(site=name, claim=claim):
                    self.assertNotIn(
                        claim, region,
                        "%s 仍宣称「%s」；缺省 risk 档同出口不需要口令，设置变更不外发告警"
                        % (name, claim))

    def test_tiered_semantics_present(self):
        """文案必须写出「同出口免口令」与 `full` 档例外，不能反向过度承诺。"""
        for name, get in SITES:
            region = get()
            for marker in MARKERS:
                with self.subTest(site=name, marker=marker):
                    self.assertIn(
                        marker, region,
                        "%s 缺少语义锚「%s」：只写「不要口令」对 full 档同样失实"
                        % (name, marker))

    def test_api_contract_global_pause_semantics(self):
        """D1 反向断言：`global_pause` 不得回到「full 档要口令 ＋ 不可逆确认」的旧形状。

        事实：`confirm_delay_ack` 只在 `tier != PW_GATE_FULL` 分支校验
        （`web/app.py:2296-2303`）。full 档只要求口令，不要求倒计时确认。
        """
        region = _api_contract_region()
        for bad in ("full` 档要口令 + 不可逆确认", "full 档要口令 + 不可逆确认",
                    "full` 档要口令＋不可逆确认", "full 档要口令＋不可逆确认"):
            with self.subTest(bad=bad):
                self.assertNotIn(bad, region, "global_pause 回到了旧失实形状")
        self.assertIn("非 `full` 档同出口免口令", region, "缺少正确的非 full 档口径")
        self.assertIn("另加不可逆确认", region, "缺少不可逆确认的档位限定")


class PasswordClaimAllowlistGateTest(unittest.TestCase):
    """行级允许清单门：每条口令断言都要按 file:line 登记。"""

    def test_real_tree_claims_are_all_allowlisted(self):
        n_files, n_claims, n_allowed, bad, seen = _scan_password_claims(BASE)
        self.assertGreaterEqual(
            n_files, MIN_CARRIER_FILES,
            "扫描面只有 %d 个文件（下限 %d）：目录写错、树被移走或 docs 掉出扫描面"
            % (n_files, MIN_CARRIER_FILES))
        self.assertGreaterEqual(
            n_claims, MIN_CLAIM_LINES,
            "只读到 %d 条口令句（下限 %d）：扫描面失效" % (n_claims, MIN_CLAIM_LINES))
        self.assertEqual(bad, [], "以下口令断言未登记或登记已过期：\n" + "\n".join(bad))
        self.assertEqual(n_allowed, n_claims, "放行数与命中数不等")
        self.assertEqual(sorted(seen), sorted(CLAIM_ALLOWLIST),
                         "允许清单有死条目，或实际命中未同步")

    def test_adversarial_samples_are_rejected(self):
        """审查员两条对抗样本：带档位词也必须红（档位词不自动放行）。"""
        with tempfile.TemporaryDirectory() as d:
            web = pathlib.Path(d, "web")
            web.mkdir()
            (web / "a.py").write_text(
                "# 档位说明：A 档保存必须当次输口令。\n"
                "# 缺省 risk 档下，公告发布必须当次输口令。\n",
                encoding="utf-8")
            _n, n_claims, n_allowed, bad, _seen = _scan_password_claims(d, allowlist={})
            self.assertEqual(n_claims, 2)
            self.assertEqual(n_allowed, 0)
            self.assertEqual(len(bad), 2, "两条对抗样本都必须被点名：%s" % bad)

    def test_third_tier_word_mutation_is_rejected(self):
        """第三条带档位词的合成失实句也必须红。"""
        with tempfile.TemporaryDirectory() as d:
            web = pathlib.Path(d, "web")
            web.mkdir()
            (web / "b.py").write_text(
                "# full 档下，A 档保存必须当次输口令。\n", encoding="utf-8")
            _n, _c, n_allowed, bad, _seen = _scan_password_claims(d, allowlist={})
            self.assertEqual(n_allowed, 0)
            self.assertEqual(len(bad), 1, "带档位词的失实句必须红：%s" % bad)

    def test_registered_line_passes(self):
        """反向自证：已登记且原文匹配的行必须放行，门不是「见口令就红」。"""
        with tempfile.TemporaryDirectory() as d:
            web = pathlib.Path(d, "web")
            web.mkdir()
            (web / "c.py").write_text(
                "x = 1  # full 档下必须当次输口令。\n", encoding="utf-8")
            allow = {("web/c.py", 1): ("full 档下必须当次输口令", "测试用已核实")}
            _n, n_claims, n_allowed, bad, _seen = _scan_password_claims(d, allowlist=allow)
            self.assertEqual((n_claims, n_allowed, bad), (1, 1, []))


if __name__ == "__main__":
    unittest.main()
