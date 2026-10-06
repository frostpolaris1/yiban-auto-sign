# -*- coding: utf-8 -*-
"""状态词汇表单一事实源：`yiban.status` 定义，signin / web 只做别名引用。

标签：D · 状态词汇与账号生命周期
覆盖：12 个状态码常量在 `signin` 侧与 `yiban.status` 等值、`STATUS_SYMBOL`/
    `UNDONE_STATUSES` 是**同一对象**；web 侧常量与 `STATUS_ICON`/`STATUS_TEXT` 同为
    别名；未了结集合的成员口径；两张展示映射刻意不合并；**前端三页的状态中文已收敛为
    唯一事实源 `frontend/src/lib/status-vocab.js`**（键集合 == ALL_STATUSES，三页 import 它）。
对应实现：权威定义 = `yiban/status.py`（`STATUS_*`、`SYMBOL`、`ICON`、`TEXT`、
    `UNDONE_STATUSES`）；`scripts/signin.py` 与 `web/app.py` 里的同名符号是引用别名；
    前端词表 = `frontend/src/lib/status-vocab.js` 的 `STATUS_VOCAB`。
关键断言：用 `assertIs` 而不是 `assertEqual`——值相等也可能是各复制一份，那正是曾经
    的病灶（web 侧缺 `no_position`/`global_paused`，signin 符号表缺 `pending`）。
    未了结集合里不得出现成功类状态，否则会无限触发补签轮。
依赖：importlib 隔离加载 `web/app.py` + 直接 import `signin`；临时 .env/DB/STATE，
    `YIBAN_DISABLE_PURGE_LOOP=1` 关掉导入期清理循环；不触网、不起子进程。

**两张映射刻意不合并**（服务不同消费方，合并会改变前端可见表现，属前后端协同改动），
所以这里断言的是"别名指向同一对象"，而不是"内容相同"。
"""
import contextlib
import importlib.util
import os
import re
import shutil
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import db  # noqa: E402

from yiban import status as yiban_status  # noqa: E402

TEST_KEY = "a" * 64


class StatusSingleSourceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-status-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n")
        os.environ.update({
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_ENV_FILE": cls.env_file,
            "YIBAN_DB_FILE": os.path.join(cls.tmp, "yiban.db"),
            "YIBAN_STATE_DIR": cls.tmp,
            "YIBAN_DISABLE_PURGE_LOOP": "1",  #导入 web 模块会带起清理循环：测试进程里必须关掉，否则动到真实状态目录
        })
        import signin
        cls.signin = signin
        spec = importlib.util.spec_from_file_location(
            "webapp", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_signin_is_alias_not_duplicate(self):
        for name in ("STATUS_SUCCESS", "STATUS_ALREADY", "STATUS_NO_TASK", "STATUS_FAILED",
                     "STATUS_RETRYING", "STATUS_SKIPPED_WINDOW", "STATUS_SKIPPED_NORANGE",
                     "STATUS_NO_POSITION", "STATUS_PAUSED", "STATUS_USER_CANCELLED",
                     "STATUS_PENDING", "STATUS_GLOBAL_PAUSED"):
            with self.subTest(name=name):
                self.assertEqual(getattr(self.signin, name), getattr(yiban_status, name))
        self.assertIs(self.signin.STATUS_SYMBOL, yiban_status.SYMBOL,  #is 而非 ==：复制一份内容相同的字典同样能骗过等值断言
                      "符号表必须是同一对象（别名），不是重新构造的副本")
        self.assertIs(self.signin.UNDONE_STATUSES, yiban_status.UNDONE_STATUSES)

    def test_web_is_alias_not_duplicate(self):
        for name in ("STATUS_SUCCESS", "STATUS_ALREADY", "STATUS_NO_TASK", "STATUS_FAILED",
                     "STATUS_RETRYING", "STATUS_SKIPPED_WINDOW", "STATUS_SKIPPED_NORANGE",
                     "STATUS_PAUSED", "STATUS_USER_CANCELLED", "STATUS_PENDING"):
            with self.subTest(name=name):
                self.assertEqual(getattr(self.webapp, name), getattr(yiban_status, name))
        self.assertIs(self.webapp.STATUS_ICON, yiban_status.ICON)
        self.assertIs(self.webapp.STATUS_TEXT, yiban_status.TEXT)

    def test_undone_statuses_contents(self):
        """未了结集合的口径（补签闸门依赖它：落此集合即判定本轮未跑完）。"""
        self.assertEqual(
            set(yiban_status.UNDONE_STATUSES),
            {"failed", "retrying", "pending", "skipped_window", "skipped_norange",
             "no_position"},
        )
        # 成功类状态不得进集合（否则会无限触发补签轮）
        for done in (yiban_status.STATUS_SUCCESS, yiban_status.STATUS_ALREADY,
                     yiban_status.STATUS_NO_TASK, yiban_status.STATUS_PAUSED,
                     yiban_status.STATUS_USER_CANCELLED):
            self.assertNotIn(done, yiban_status.UNDONE_STATUSES)

    def test_word_and_display_maps_are_separate_by_design(self):
        """两张映射服务不同消费方，**当前确实不同**——记录差异，合并须前后端协同。"""
        self.assertIn(yiban_status.STATUS_NO_POSITION, yiban_status.SYMBOL)
        self.assertNotIn(yiban_status.STATUS_NO_POSITION, yiban_status.ICON)
        self.assertIn(yiban_status.STATUS_PENDING, yiban_status.ICON)
        self.assertNotIn(yiban_status.STATUS_PENDING, yiban_status.SYMBOL)

    # ---- 前端"第二份表"的漂移门（清扫单⑪：急停可见性整改明列不属其范围、"同一事实 N 份定义"同族）----
    # 2026-10-04（收账）：仪表盘图表短名 / 日志徽标 / 账号表状态列三份中文原各写一份、
    # 已互相漂移（paused 三处为「账密暂停 / 已暂停 / 账号暂停」）。本轮把它们收敛为
    # **前端唯一事实源** `frontend/src/lib/status-vocab.js` 的 `STATUS_VOCAB`
    # （每码一条 {full, short, icon, tone}，三页从它取词）。原「dashboard STATUS_LABEL
    # 独立副本」的台账销账，改为钉：①键集合 == 唯一事实源 ALL_STATUSES；②三页都 import
    # 该模块、不再各留字面量表。可见文案随收敛的逐条变化见提交说明。
    VOCAB_JS = os.path.join(BASE, "frontend", "src", "lib", "status-vocab.js")
    DASH_JS = os.path.join(BASE, "frontend", "src", "dashboard", "model.js")
    LOGS_TS = os.path.join(BASE, "frontend", "src", "logs", "format.ts")
    ACCOUNTS_JS = os.path.join(BASE, "frontend", "src", "accounts", "model.js")
    # 2026-10-03：账号页迁到 Vue 后，状态表副本随之搬走（frontend/src/myaccounts/model.ts
    # 的 todayStateText）。本测试钉的「同一事实多份定义」台账跟着换路径，判据与字面量不变。
    MYACC_JS = os.path.join(BASE, "frontend", "src", "myaccounts", "model.ts")

    def test_status_vocab_keys_cover_the_status_enum(self):
        """前端唯一事实源 STATUS_VOCAB 的**键集合**必须等于 `ALL_STATUSES`。

        三页（图表短名 / 日志徽标 / 账号表）都从它取词；新增状态码若漏进本表即假绿
        （分布图会把没有的状态码静默归「跳过」并显示英文原文）。每条词条须四字段齐备。
        """
        with open(self.VOCAB_JS, encoding="utf-8") as fh:
            src = fh.read()
        block = re.search(r"STATUS_VOCAB\s*=\s*\{(.*)\};", src, re.S)
        self.assertIsNotNone(block, "frontend/src/lib/status-vocab.js 未找到 STATUS_VOCAB 表")
        body = block.group(1)
        keys = set(re.findall(r"([A-Za-z_]\w*)\s*:\s*\{", body))
        self.assertEqual(keys, set(yiban_status.ALL_STATUSES),
                         "前端状态词表与唯一事实源的状态码集合已分叉")
        for field in ("full", "short", "icon", "tone"):
            self.assertIn(field + ":", body, "STATUS_VOCAB 词条缺 %s 字段" % field)

    def test_frontend_pages_import_the_single_source(self):
        """三页必须从 status-vocab 取词，且不得再各留一份字面量表。"""
        for path in (self.DASH_JS, self.LOGS_TS, self.ACCOUNTS_JS):
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
            self.assertIn('"../lib/status-vocab.js"', src,
                          "%s 未 import 状态词表唯一事实源" % os.path.basename(path))
        with open(self.DASH_JS, encoding="utf-8") as fh:
            dash = fh.read()
        self.assertNotRegex(dash, r"STATUS_LABEL\s*=\s*\{",
                            "dashboard 不得再留第二份字面量状态表（STATUS_LABEL 须派生自单源）")
        with open(self.ACCOUNTS_JS, encoding="utf-8") as fh:
            acct = fh.read()
        self.assertNotRegex(acct, r"STATE_TEXT\s*=\s*\{",
                            "账号表不得再留第二份字面量状态表（须派生自单源）")
        # 2026-10-04（收尾小件）：日志页的 SIGN_STATUS_MAP 同样须派生自单源（此前是
        # 逐码手写表），补反向断言防其复活。
        with open(self.LOGS_TS, encoding="utf-8") as fh:
            logs = fh.read()
        self.assertNotRegex(logs, r"SIGN_STATUS_MAP\s*=\s*\{",
                            "日志页不得再留第二份字面量状态表（SIGN_STATUS_MAP 须派生自单源）")

    # 2026-10-04（收尾小件）：反查面从「三个具名文件」扩到 frontend/src 全域——任何文件
    # 只要重新写出一张「状态码 → 引号字符串」的字面量映射，即在此拦下（不限于已知三页）。
    FRONTEND_SRC = os.path.join(BASE, "frontend", "src")
    #: 非展示的页面级映射，整块剥离后再扫（它们不是"第二份展示词表"）：
    #: dashboard 的 STATUS_TOKEN 是「状态码 → 主题色彩令牌」，随枚举走但语义是颜色配置，
    #: 非 full/short/icon/tone 中任何一项；其展示短名 STATUS_LABEL 另有专项断言钉死。
    SANCTIONED_MAPS = (r"STATUS_TOKEN\s*=\s*\{",)

    @staticmethod
    def _strip_braced_block(src, marker):
        """按花括号配对整块移除一个 `NAME = { ... }` 字面量，返回剩余文本。

        只为让扫描忽略已登记的非展示映射；不解析字符串/注释（这些对象的键值都是简单
        字面量，够用）。
        """
        pat = re.compile(marker)
        while True:
            m = pat.search(src)
            if not m:
                return src
            start = src.index("{", m.start())
            depth = 0
            for i in range(start, len(src)):
                if src[i] == "{":
                    depth += 1
                elif src[i] == "}":
                    depth -= 1
                    if depth == 0:
                        src = src[:m.start()] + src[i + 1:]
                        break
            else:  # 括号不配对（源码语法错误）：保持原样，交给别的测试去报
                return src

    #: 字面量状态映射扫描器：键可为裸键或 JSON/单引号引号键（`"success":` / `'success':`），
    #: 值首字符为引号或反引号（`"…"` / `'…'` / 模板串）。词边界防止 `global_paused` 内命中
    #: `paused`。
    @classmethod
    def _status_map_key_re(cls):
        codes = sorted(yiban_status.ALL_STATUSES, key=len, reverse=True)
        return re.compile(
            r"(?<![A-Za-z0-9_$])[\"']?("
            + "|".join(re.escape(c) for c in codes)
            + r")[\"']?\s*:\s*[\"'`]")

    @staticmethod
    def _literal_status_codes(src, key_re):
        return sorted({m.group(1) for m in key_re.finditer(src)})

    def test_literal_map_scanner_catches_quote_variants(self):
        """扫描器自证：JSON 双引号键 / 单引号键 / 反引号值都须命中（防漏检回归）。"""
        key_re = self._status_map_key_re()
        for sample in (
            '{ "success": "成功", "failed": "失败" }',      # JSON 风格：双引号键
            "{ 'success': '成功', 'failed': '失败' }",       # 单引号键
            "{ success: `成功`, failed: `失败` }",           # 模板串值
            '{"global_paused":"全局暂停","no_position":"无点位"}',
        ):
            codes = self._literal_status_codes(sample, key_re)
            self.assertGreaterEqual(len(codes), 2, "漏检：%r → %r" % (sample, codes))
        # 单键不算"表"（≥2 判据），普通空串值仍只是单键
        self.assertEqual(self._literal_status_codes('{ pending: "" }', key_re), ["pending"])

    def test_no_literal_status_map_across_frontend_src(self):
        """frontend/src 全域：不得再出现「状态码 → 引号字符串」的字面量映射表。

        判据（三道误报控制）：
          ① 键用 `ALL_STATUSES` 的精确名匹配（带词边界、容 JSON/单引号引号键），值容
             引号与反引号；不用宽泛的 `\\w+`；
          ② 同一文件须出现 **≥2 个不同状态码** 作引号键才算"表"——单键命中是
             分组搜索态（`pending: ""`）、横幅图标（`success:`）之类，不是状态表；
          ③ 已登记的非展示页面级映射（见 `SANCTIONED_MAPS`）先整块剥离再扫。
        排除唯一事实源 `lib/status-vocab.js` 本身；测试夹具（*.spec.*）不在展示面，亦排除。
        """
        key_re = self._status_map_key_re()
        offenders = {}
        for root, _dirs, files in os.walk(self.FRONTEND_SRC):
            for name in files:
                ext = os.path.splitext(name)[1]
                if ext not in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".mts", ".cjs", ".vue"):
                    continue
                if name.endswith((".spec.js", ".spec.ts", ".test.js", ".test.ts")):
                    continue
                path = os.path.join(root, name)
                if os.path.abspath(path) == os.path.abspath(self.VOCAB_JS):
                    continue  # 唯一事实源本身
                with open(path, encoding="utf-8") as fh:
                    src = fh.read()
                for marker in self.SANCTIONED_MAPS:
                    src = self._strip_braced_block(src, marker)
                found = self._literal_status_codes(src, key_re)
                if len(found) >= 2:
                    offenders[os.path.relpath(path, BASE)] = found
        self.assertEqual(
            offenders, {},
            "frontend/src 出现新的字面量状态映射表（须改用 lib/status-vocab.js 单源）："
            + repr(offenders))

    def test_my_accounts_done_text_mirrors_display(self):
        """my-accounts 账号卡「今日状态」完成行的字面量必须等于 DISPLAY 成功态文案。

        它是 DISPLAY 之外的第二处"今日已完成签到"文案（不含急停/周末门判定）；
        DISPLAY 改这句而这里漏改即两侧显示分叉——本用例把它钉回唯一事实源。
        """
        with open(self.MYACC_JS, encoding="utf-8") as fh:
            src = fh.read()
        done_text = yiban_status.DISPLAY[yiban_status.STATUS_SUCCESS]["text"]
        self.assertIn(done_text, src,
                      "my-accounts 今日完成文案与 DISPLAY 成功态文案已分叉")

    def test_frontend_global_pause_color_stays_anomaly_tier(self):
        """急停（global_paused）在前端图表色板里必须落异常档，不得落"无异常"档。

        census P1-5：Vue 重写不得复刻旧 `data_dashboard.js` 的暂停假安心。权威口径是
        `yiban.status.DISPLAY`——它把急停定为 `warn`（非正常，见 `_DISPLAY_ROWS`）。
        前端 `dashboard/model.js` 的 `STATUS_TOKEN` 是图表配色（非展示词表，见
        `SANCTIONED_MAPS`）；修复前它把急停自持为 `"muted"`（与「用户取消」同色 = 无异常
        档），于是分布图上"管理员急停"与"用户自己取消"无从区分。此门钉两条：
          ① 急停的色板档位与账密暂停（同为账号级暂停）同档；
          ② 急停不得落 `muted` / `light` 两个低强调档。
        """
        self.assertEqual(
            yiban_status.DISPLAY[yiban_status.STATUS_GLOBAL_PAUSED]["tone"], "warn",
            "权威口径：yiban.status 定急停语气为 warn（非正常）")
        with open(self.DASH_JS, encoding="utf-8") as fh:
            src = fh.read()
        block = re.search(r"STATUS_TOKEN\s*=\s*\{(.*?)\};", src, re.S)
        self.assertIsNotNone(block, "dashboard/model.js 未找到 STATUS_TOKEN 表")
        tokens = dict(re.findall(r"([A-Za-z_]\w*)\s*:\s*\"([^\"]*)\"", block.group(1)))
        self.assertNotIn(
            tokens.get("global_paused"), ("muted", "light"),
            "急停不得用低强调色（muted/light = 无异常档）——census P1-5 的假安心形态")
        self.assertEqual(
            tokens.get("global_paused"), tokens.get("paused"),
            "急停与账密暂停同档（两者都是账号级暂停，都由 status.py 定为 warn）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
