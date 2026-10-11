# -*- coding: utf-8 -*-
"""签到日历状态可见性：状态表单一源（日期格渲染 + 图例）与急停/周末门真值显示。

标签：F · 前端与界面守卫
覆盖：状态显示表 `yiban.status.DISPLAY` 覆盖全部状态码并被日历图例与前端状态行两侧消费；`global_paused`/`no_position` 不再渲染成"排队待签"；日历格对有结论的日子渲染语气档色点（色彩即语义，emoji 不上界面）；急停/周末门在 web 侧读 `.env` 真值（与引擎同源）
对应实现：`yiban/status.py`（`DISPLAY` / `legend_items` / `display_payload`）、`web/routes/pages.py`
（`_calendar_page_context` 把表与图例同车下发）、`web/templates/partials/page_sign_calendar.html`
（内联载荷）、`frontend/src/calendar/model.js`（`statusLine` / `stateEntry` / `dayCell`）、
`frontend/src/calendar/CalendarPage.vue`（图例与状态行渲染）、`web/services/signstatus.py`
（`_day_off_reason` / `day_off_text`）
关键断言：状态枚举与图例同源——图例项由 `DISPLAY` 生成并随载荷下发，往表里加一个新状态码即
自动进载荷与图例（用例直接改表断言，不靠"人记得改两处"）；`statusLine` 对 `global_paused`/
未知码绝不再回落成"排队待签"；web 侧门判定与引擎 `schedule.day_off` 读同一份 `.env` 真值
（置位急停 ⇒ 两侧同时为"暂停"，复位 ⇒ 同时恢复）
依赖：⚠ **需要 node 真跑**——`statusLine` / `dayCell` / `stateEntry` 按花括号配对从
`frontend/src/calendar/model.js` 抽出后交给 node 执行，`shutil.which("node")` 取不到时这两个
类整类 `skipUnless`。其余为纯本地 Flask test client + 临时 `.env`/SQLite；不联网、不访问真实
易班接口

**为什么需要**："急停在面板上不可见"整改的四处病灶里，两处是"同一事实两份定义"——
①前端状态行 `sign-calendar-view.js:30-44` 逐码手写文案但漏了 `global_paused`/`no_position`，
  于是急停被渲染成"待签到 · 前方排队 N 人"（面板给的就是安心假信号）；
②日历图例是模板里手写的四个 `<li>`，12 个状态码只覆盖到 2 个，状态行认得的与图例认得的
  是两份清单，必然漂移。
判别的方式只有一条：让两侧消费**同一份表**，并把"新增状态自动进两侧"做成可执行的断言；
再加"web 侧门判定读 `.env` 真值"——引擎读 `.env` 会真暂停，而 web 侧原来经
`day_off(env=None)` 落回 `os.environ`，在 web 进程里恒不生效，于是"实际暂停、界面说没有"。

**2026-10-03 换锚（日历对迁到 Vue）**：前端实现从 `web/static/js/{calendar.js,
components/sign-calendar-view.js}` 迁到 `frontend/src/calendar/{model.js,CalendarPage.vue}`，
判据逐条换锚、意图不变：Node 对拍改成抽 `model.js` 的同名函数（**刻意保留纯 JS**，正是为了
让这两组"真跑"用例继续跑真实交付代码，见该文件头部说明）；日期格的返回从 HTML 串改为数据
（类名/读屏名/角标三个字段），断言随之改为按字段比对——比原先的子串匹配更严（还能直接断言
"状态符号本身不上界面"）；图例改为前端按载荷渲染，服务端侧的判据落到"载荷必须带全量图例
（由 `legend_items()` 生成）"上，页面侧则钉住"由载荷驱动、不得写死"。

**不变量边界**：手动腿不受急停/周末门的豁免是写进 UI 契约的设计（`tests/test_global_pause.py`
锁定）。本文件只钉"显示与计数"，一行门语义都不碰。
"""
import contextlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

from yiban import status as yiban_status  # noqa: E402

#: 日历前端口径（**刻意保留纯 JS**，Node 对拍用例按字面量抽函数——见该文件头部说明）。
#: 迁移前这里是 legacy 的 components/sign-calendar-view.js 与 js/calendar.js。
MODEL_JS = os.path.join(BASE, "frontend", "src", "calendar", "model.js")
CAL_PAGE_VUE = os.path.join(BASE, "frontend", "src", "calendar", "CalendarPage.vue")
PAGES_PY = os.path.join(BASE, "web", "routes", "pages.py")
NODE = shutil.which("node")

#: 本地时间 2026-09-26 是周六（周末门用例用它固定"周六"这一事实）
SATURDAY = datetime(2026, 9, 26, 6, 40)
SUNDAY = datetime(2026, 9, 27, 6, 40)
WEDNESDAY = datetime(2026, 9, 23, 6, 40)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _extract_function(src, name):
    """从源码里按花括号配对抽出 `function <name>(...) { ... }` 整段。

    测试专用、够用即可——刻意不做通用 JS 解析。已知脆弱（改动被测 JS 时若命中即抛错、
    不会静默抓错段，故失效方向是红不是假绿）：
    - 定位靠字面量 `"function <name>("`：目标若被格式化（`function name (`、箭头函数、
      对象属性式 `name: function(`）或该字面量在更早的注释/字符串里先出现过，会 ValueError
      或抓错段——被抽函数须保持这一书写形式；
    - 只数 `{`/`}`、不数 `()`/`[]`：字符串/模板串/注释里的裸花括号被计入，会提前或延后闭合；
    - 参数默认值带对象解构（`function f(a, {b}=…)`）时，首个 `{` 落在参数表内、配对起点即偏。
    """
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


_ESC_JS = (
    "function esc(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;')"
    ".replace(/>/g,'&gt;').replace(/\"/g,'&quot;'); }\n"
)


class StatusDisplayTableTest(unittest.TestCase):
    """状态显示表：覆盖全部状态码、符号与既有映射一致、图例由表生成。"""

    def test_table_covers_every_status_code(self):
        """`DISPLAY` 必须与 `ALL_STATUSES` 一一对应——漏一格就意味着某状态无人认领。"""
        self.assertEqual(set(yiban_status.DISPLAY), set(yiban_status.ALL_STATUSES))

    def test_table_symbols_agree_with_the_existing_maps(self):
        """表里的符号必须与既有的 SYMBOL/ICON 同一取值（日历格与状态行看的是同一个符号）。"""
        for code in yiban_status.ALL_STATUSES:
            with self.subTest(code=code):
                expected = yiban_status.SYMBOL.get(code) or yiban_status.ICON.get(code, "")
                self.assertTrue(expected, f"{code} 在 SYMBOL/ICON 两侧都没有符号")
                self.assertEqual(yiban_status.DISPLAY[code]["symbol"], expected)

    def test_table_entries_are_complete(self):
        """每格都要有符号 / 文案 / 图例名 / 语气档；语气档必须落在状态行既有的五个类里。"""
        tones = {"ok", "warn", "bad", "muted", "busy"}
        for code in yiban_status.ALL_STATUSES:
            entry = yiban_status.DISPLAY[code]
            with self.subTest(code=code):
                self.assertTrue(entry["symbol"])
                self.assertTrue(entry["text"])
                self.assertTrue(entry["legend"])
                self.assertIn(entry["tone"], tones)

    def test_tone_taxonomy_follows_the_color_scheme(self):
        """色彩语义定版（emoji 不上界面）：绿=成功、红=失败、灰=有意不签、黄=其余异常、
        蓝呼吸=正在签到。语气档一旦漂移，这里的逐码钉住会先红。"""
        taxonomy = {
            yiban_status.STATUS_SUCCESS: "ok",
            yiban_status.STATUS_ALREADY: "ok",
            yiban_status.STATUS_FAILED: "bad",
            yiban_status.STATUS_NO_TASK: "muted",
            yiban_status.STATUS_SKIPPED_WINDOW: "muted",
            yiban_status.STATUS_SKIPPED_NORANGE: "muted",
            yiban_status.STATUS_USER_CANCELLED: "muted",
            yiban_status.STATUS_PENDING: "warn",
            yiban_status.STATUS_NO_POSITION: "warn",
            # 补签中（平台 State=5）：结果未定，与"待签/无点位"同档——需要人看着，
            # 且不许渲染成已完成（它不是 `ok`，也不是"有意不签"的 `muted`）。
            yiban_status.STATUS_SUPPLEMENTING: "warn",
            yiban_status.STATUS_PAUSED: "warn",
            yiban_status.STATUS_GLOBAL_PAUSED: "warn",
            yiban_status.STATUS_RETRYING: "busy",
        }
        for code, tone in taxonomy.items():
            with self.subTest(code=code):
                self.assertEqual(yiban_status.DISPLAY[code]["tone"], tone)

    def test_legend_covers_every_tone_in_the_table(self):
        """图例按语气档归组（色彩即语义）：表里出现的**每一个**档位都必须有一条图例，
        且同档只一条——"渲染认得、图例不认得"正是本条要杜绝的。"""
        table_tones = {yiban_status.DISPLAY[c]["tone"] for c in yiban_status.ALL_STATUSES}
        items = yiban_status.legend_items()
        legend_tones = {item["tone"] for item in items}
        self.assertEqual(legend_tones, table_tones)
        self.assertEqual(len(items), len(table_tones),
                         "同一档位只应出现一条图例（重复条会让图例变成噪声）")
        for item in items:
            with self.subTest(tone=item["tone"]):
                self.assertTrue(item["label"], "图例条目缺中文短名")

    def test_new_status_code_enters_the_payload_automatically(self):
        """新增状态码 ⇒ 前端载荷与图例自动跟上：直接往表里塞一格，两侧都要出现。
        图例按档归组后，落进既有档位的新码不添新条（颜色已覆盖）；**新语气档**则
        必须冒出一条（实现用档位名兜底，故意难看，逼着去 `_LEGEND_TONES` 补短名）。"""
        fake = "brand_new_state"
        patched = dict(yiban_status.DISPLAY)
        patched[fake] = {"symbol": "🆕", "text": "出厂新状态", "legend": "新状态",
                         "tone": "magic"}
        try:
            yiban_status.DISPLAY = patched
            tones = [it["tone"] for it in yiban_status.legend_items()]
            self.assertIn("magic", tones, "新语气档未进图例——说明它不是从表生成的")
            payload = yiban_status.display_payload()
            self.assertIn(fake, payload["by_code"])
            self.assertIn("🆕", payload["by_symbol"])
            self.assertEqual(payload["by_symbol"]["🆕"]["tone"], "magic")
        finally:
            yiban_status.DISPLAY = {k: v for k, v in patched.items() if k != fake}

    def test_emergency_stop_and_no_position_are_not_pending_semantics(self):
        """急停/无点位两态必须有自己的语义，且语气档不是"待签"的中性档。"""
        paused = yiban_status.DISPLAY[yiban_status.STATUS_GLOBAL_PAUSED]
        self.assertIn("急停", paused["text"])
        self.assertNotEqual(paused["tone"], "muted")
        nopos = yiban_status.DISPLAY[yiban_status.STATUS_NO_POSITION]
        self.assertIn("点位", nopos["text"])
        for code in (yiban_status.STATUS_GLOBAL_PAUSED, yiban_status.STATUS_NO_POSITION):
            self.assertNotIn("排队", yiban_status.DISPLAY[code]["text"])

    def test_symbol_lookup_has_no_tone_conflict(self):
        """同一符号的不同状态码必须共享同一语气档（日期格按符号反查档位，会随状态抖动）。"""
        codes_by_symbol = {}
        for code in yiban_status.ALL_STATUSES:
            entry = yiban_status.DISPLAY[code]
            codes_by_symbol.setdefault(entry["symbol"], []).append(code)
        by_symbol = yiban_status.display_payload()["by_symbol"]
        for sym, codes in codes_by_symbol.items():
            with self.subTest(symbol=sym):
                tones = {yiban_status.DISPLAY[c]["tone"] for c in codes}
                self.assertEqual(len(tones), 1, f"符号 {sym} 跨语气档")
                self.assertEqual(by_symbol[sym]["tone"], tones.pop())


@unittest.skipUnless(NODE, "node 不可用：跳过前端状态行/日期格的 JS 行为用例")
class StatusLineJsTest(unittest.TestCase):
    """`statusLine`（账号卡状态行）真跑：从表取文案与语气档，杜绝"排队待签"假信号。"""

    @classmethod
    def setUpClass(cls):
        cls.fn_src = _extract_function(_read(MODEL_JS), "statusLine")

    def _run(self, cases):
        script = (
            self.fn_src + "\n"
            "var cases = " + json.dumps(cases, ensure_ascii=False) + ";\n"
            "console.log(JSON.stringify(cases.map(function (c) { return statusLine(c.a, c.ctx); })));\n"
        )
        proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise AssertionError("node 执行失败：%s" % (proc.stderr or proc.stdout))
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def _ctx(self, **over):
        ctx = json.loads(json.dumps(yiban_status.display_payload()))
        ctx["day_off"] = None
        ctx.update(over)
        return ctx

    def test_emergency_stop_is_visible_not_queueing(self):
        """急停：无当日记录的账号显示急停文案（当前红：落回"待签到 · 前方排队 N 人"）。"""
        ctx = self._ctx(day_off={"reason": "paused", "text": "全局暂停（急停）：今日自动签到已停止",
                                 "tone": "warn"})
        out = self._run([{"a": {"state_status": "pending", "queue_ahead": 3}, "ctx": ctx}])[0]
        self.assertIn("急停", out["text"])
        self.assertNotIn("排队", out["text"])
        self.assertEqual(out["cls"], "state-line--warn")

    def test_global_paused_status_code_uses_the_table(self):
        """`state_status=global_paused`（历史/异常数据）：走表文案，不是"排队待签"。"""
        out = self._run([{"a": {"state_status": "global_paused", "queue_ahead": 1},
                          "ctx": self._ctx()}])[0]
        self.assertIn("急停", out["text"])
        self.assertNotIn("排队", out["text"])

    def test_no_position_status_uses_the_table(self):
        """`no_position`：无点位语义（当前红：也落回"排队待签"）。"""
        out = self._run([{"a": {"state_status": "no_position", "queue_ahead": 2},
                          "ctx": self._ctx()}])[0]
        self.assertIn("点位", out["text"])
        self.assertNotIn("排队", out["text"])

    def test_pending_without_gate_still_shows_the_queue(self):
        """没有门挡下时，pending 保持既有排队口径（不得把正常待签也说成异常）。"""
        out = self._run([{"a": {"state_status": "pending", "queue_ahead": 3,
                                "state_message": "计划 06:40"}, "ctx": self._ctx()}])[0]
        self.assertIn("排队 3 人", out["text"])
        self.assertIn("计划 06:40", out["text"])
        self.assertEqual(out["cls"], "state-line--muted")

    def test_unknown_status_code_does_not_claim_queueing(self):
        """未知状态码：如实报告未知，绝不冒充"排队待签"（新增状态而前端未更新时）。"""
        out = self._run([{"a": {"state_status": "wat", "queue_ahead": 0},
                          "ctx": self._ctx()}])[0]
        self.assertNotIn("排队", out["text"])
        self.assertNotIn("待签到", out["text"])

    def test_failed_keeps_the_message_suffix(self):
        """失败态保留"：原因"后缀（既有可见契约不回归）。"""
        out = self._run([{"a": {"state_status": "failed", "state_message": "网络异常"},
                          "ctx": self._ctx()}])[0]
        self.assertIn("签到失败：网络异常", out["text"])
        self.assertEqual(out["cls"], "state-line--bad")


@unittest.skipUnless(NODE, "node 不可用：跳过前端状态行/日期格的 JS 行为用例")
class CalendarCellJsTest(unittest.TestCase):
    """日期格：语气档与标签取自注入的状态表；有结论的日子都要有色底（emoji 不上界面）。

    换锚（2026-10-03）：`dayCell` 从返回 HTML 串改为返回数据（`cls` / `label` / `offBadge`），
    故断言按字段比对而不是在 HTML 里找子串——更严，且能直接断言"状态符号本身不进界面"。
    `ctx` 由用例显式传入（迁移前读的是全局 `window.YB_CALENDAR_STATE`），故无需伪造 window。
    """

    @classmethod
    def setUpClass(cls):
        src = _read(MODEL_JS)
        cls.entry_src = _extract_function(src, "stateEntry")
        cls.cell_src = _extract_function(src, "dayCell")

    def _run(self, cells, payload):
        script = (
            "var ctx = " + json.dumps(payload, ensure_ascii=False) + ";\n"
            + _ESC_JS + self.entry_src + "\n" + self.cell_src + "\n"
            "var cells = " + json.dumps(cells, ensure_ascii=False) + ";\n"
            "console.log(JSON.stringify(cells.map(function (c) { return dayCell(c, ctx); })));\n"
        )
        proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise AssertionError("node 执行失败：%s" % (proc.stderr or proc.stdout))
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def _cell(self, **over):
        base = {"d": 12, "date": "2026-09-12", "state": "", "off": False, "offDay": "",
                "isToday": False, "selected": False}
        base.update(over)
        return base

    def _symbols_are_not_rendered(self, cell, payload):
        """状态符号（emoji）只是按日状态文件的存储口径，反查完语气档即弃，不进界面。"""
        for entry in payload["by_code"].values():
            self.assertNotIn(entry["symbol"], cell["label"])
        self.assertNotIn("sc-sym", cell["cls"])
        self.assertNotIn("sc-dot", cell["cls"])

    def test_ok_and_bad_cells_get_their_tone_background(self):
        payload = yiban_status.display_payload()
        ok, bad = self._run(
            [self._cell(state="✅"), self._cell(state="❌")], payload)
        self.assertIn("sc-cell--ok", ok["cls"])
        self.assertIn("sc-cell--bad", bad["cls"])
        for cell in (ok, bad):
            self._symbols_are_not_rendered(cell, payload)

    def test_other_states_get_a_tone_background(self):
        """时段外/无点位/正在签到不再渲染成空白格——底色即状态（emoji 与色点都不上界面）。"""
        payload = yiban_status.display_payload()
        no_pos, skipped, retrying = self._run(
            [self._cell(state="🚫"), self._cell(state="⛔"), self._cell(state="🔄")], payload)
        self.assertIn("sc-cell--warn", no_pos["cls"])
        self.assertIn("sc-cell--muted", skipped["cls"])
        self.assertIn("sc-cell--busy", retrying["cls"])
        for cell in (no_pos, skipped, retrying):
            self._symbols_are_not_rendered(cell, payload)
        self.assertIn("点位", no_pos["label"], "读屏名必须说明这一格发生了什么")

    def test_empty_day_has_no_state_color(self):
        payload = yiban_status.display_payload()
        empty = self._run([self._cell()], payload)[0]
        self.assertIn("sc-cell--none", empty["cls"])
        self.assertNotIn("sc-dot", empty["cls"])
        self.assertIn("查看签到记录", empty["label"])

    def test_off_day_keeps_the_neutral_background(self):
        """周末停签格：中性底 + 「休」角标，不叠状态底色（该格本就无当日结论）。"""
        payload = yiban_status.display_payload()
        off = self._run([self._cell(state="🚫", off=True, offDay="六")], payload)[0]
        self.assertIn("sc-cell--off", off["cls"])
        self.assertNotIn("sc-cell--warn", off["cls"])
        self.assertNotIn("sc-cell--none", off["cls"])
        self.assertEqual(off["offBadge"], "六")
        self.assertIn("（周六不签到）", off["label"])


class _WebAppMixin:
    """web/app.py 隔离加载（独立 .env / db / 状态目录）与页面渲染助手。"""

    @classmethod
    def _isolate_env(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-cal-state-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.state_dir = os.path.join(cls.tmp, "state")
        os.makedirs(cls.state_dir, exist_ok=True)
        cls._write_env("YIBAN_SIGN_START=06:30\nYIBAN_SIGN_END=07:50\n"
                       "YIBAN_SATURDAY_SIGN=1\nYIBAN_SUNDAY_SIGN=1\n")
        os.environ["YIBAN_ACCOUNTS_KEY"] = "a" * 64
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.state_dir
        os.environ["YIBAN_ACCOUNTS_FILE"] = os.path.join(cls.tmp, "accounts.json")
        import db as _db
        cls._db = _db
        spec = importlib.util.spec_from_file_location(
            "webapp_cal_state", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_cal_state"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)

    @classmethod
    def _write_env(cls, text):
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(text)

    @classmethod
    def _teardown_env(cls):
        if cls._db._conn is not None:
            with contextlib.suppress(Exception):
                cls._db._conn.close()
            cls._db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_DB_FILE",
                  "YIBAN_STATE_DIR", "YIBAN_ACCOUNTS_FILE"):
            os.environ.pop(k, None)

    def _calendar_html(self):
        """以普通用户身份取 /user/calendar 的渲染产物。"""
        email = "cal-state-user@example.com"
        user = self._db.find_user(email)
        if user is None:
            self._db.create_user(email, self.webapp.generate_password_hash("UserPass123!"))
            user = self._db.find_user(email)
        c = self.webapp.create_app().test_client()
        with c.session_transaction() as s:
            s["auth"] = True
            s["role"] = "user"
            s["username"] = email
            s["auth_source"] = "user"
            s["pw_version"] = user.get("pw_version", 1)
            s["login_ts"] = int(time.time())
            s["sid"] = "0" * 32
        r = c.get("/user/calendar")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:400])
        return r.get_data(as_text=True)

    @staticmethod
    def _inline_context(html):
        m = re.search(r"window\.YB_CALENDAR_STATE\s*=\s*(\{.*?\});</script>", html, re.S)
        assert m, "日历页未渲染内联状态上下文 window.YB_CALENDAR_STATE"
        return json.loads(m.group(1))


class CalendarPageRendersTableTest(_WebAppMixin, unittest.TestCase):
    """日历页：图例与内联状态上下文都由服务端从同一份表生成。

    换锚（2026-10-03）：图例的 markup 改由前端按载荷渲染（服务端仍负责"档位清单与中文短名"
    这一半——它来自 `legend_items()`，随载荷同车下发）。故判据分两半：
    · 服务端：内联载荷必须带**全量**图例项（每档一条、含中文短名），逐条与 `legend_items()`
      比对——这正是原先"渲染认得、图例不认得"要杜绝的漂移点；
    · 页面侧：图例必须由载荷驱动（`data-tone` / 色块类名都由循环变量拼），不得写死；
      两条结构性常驻项（周末停签/今天）不是状态码，留在组件模板里。
    """

    @classmethod
    def setUpClass(cls):
        cls._isolate_env()

    @classmethod
    def tearDownClass(cls):
        cls._teardown_env()

    def test_payload_carries_every_legend_item_of_the_table(self):
        ctx = self._inline_context(self._calendar_html())
        self.assertEqual(ctx["legend"], yiban_status.legend_items(),
                         "内联载荷的图例项与 yiban.status.legend_items() 分叉")
        for item in yiban_status.legend_items():
            with self.subTest(tone=item["tone"]):
                self.assertTrue(item["label"], "图例条目缺中文短名")

    def test_legend_is_generated_not_hand_written(self):
        """页面不得再手写图例项：档位键与色块类名都必须由载荷循环驱动。"""
        # 服务端：图例由表生成后随载荷下发（pages.py 是唯一注入点）
        self.assertIn("legend", _read(PAGES_PY), "日历页上下文未下发图例")
        self.assertIn("legend_items()", _read(PAGES_PY), "图例必须由表生成的列表驱动")
        partial = _read(os.path.join(BASE, "web", "templates", "partials", "page_sign_calendar.html"))
        self.assertIn("calendar_state | tojson", partial, "图例载荷必须随内联上下文渲染进页面")
        # 页面侧：档位键与色块类名由循环变量拼，不得写死
        vue = _read(CAL_PAGE_VUE)
        self.assertIn(':data-tone="item.tone"', vue,
                      "图例档位键必须由循环变量驱动，不得写死")
        self.assertIn("sc-swatch--${item.tone}", vue,
                      "图例色块必须由循环变量驱动，不得写死")
        # 结构性常驻项（不是状态码，刻意不走状态表）
        self.assertIn("周末停签", vue)
        self.assertIn("今天", vue)
        html = self._calendar_html()
        self.assertNotIn("data-symbol", html, "图例仍按符号陈列（应按语气档归组）")

    def test_inline_context_carries_the_same_table(self):
        html = self._calendar_html()
        ctx = self._inline_context(html)
        payload = yiban_status.display_payload()
        self.assertEqual(set(ctx["by_code"]), set(payload["by_code"]))
        self.assertEqual(set(ctx["by_symbol"]), set(payload["by_symbol"]))
        for code, entry in payload["by_code"].items():
            with self.subTest(code=code):
                self.assertEqual(ctx["by_code"][code]["text"], entry["text"])
                self.assertEqual(ctx["by_code"][code]["tone"], entry["tone"])


class DayOffTruthTest(_WebAppMixin, unittest.TestCase):
    """web 侧门判定读 `.env` 真值：与引擎同源（显示与真值同源，门语义一行不动）。"""

    @classmethod
    def setUpClass(cls):
        cls._isolate_env()

    @classmethod
    def tearDownClass(cls):
        cls._teardown_env()

    def test_global_pause_env_file_is_visible_to_web_display(self):
        """置位 `.env` 里的急停 ⇒ web 侧显示判定与引擎行为同为"暂停"；复位即恢复。"""
        from yiban.engine import schedule as yb_schedule

        self._write_env("YIBAN_SIGN_START=06:30\nYIBAN_SIGN_END=07:50\n"
                        "YIBAN_SATURDAY_SIGN=1\nYIBAN_SUNDAY_SIGN=1\nYIBAN_GLOBAL_PAUSE=1\n")
        # 引擎侧：真值来自同一份 .env
        self.assertEqual(yb_schedule.day_off(WEDNESDAY, env=self_env_file()), "paused")
        # web 侧：显示判定与页面上下文
        self.assertEqual(self.webapp._day_off_reason(WEDNESDAY), "paused")
        ctx = self._inline_context(self._calendar_html())
        self.assertEqual(ctx["day_off"]["reason"], "paused")
        self.assertIn("急停", ctx["day_off"]["text"])

        self._write_env("YIBAN_SIGN_START=06:30\nYIBAN_SIGN_END=07:50\n"
                        "YIBAN_SATURDAY_SIGN=1\nYIBAN_SUNDAY_SIGN=1\nYIBAN_GLOBAL_PAUSE=0\n")
        self.assertEqual(yb_schedule.day_off(WEDNESDAY, env=self_env_file()), "")
        self.assertEqual(self.webapp._day_off_reason(WEDNESDAY), "")
        ctx = self._inline_context(self._calendar_html())
        self.assertIsNone(ctx["day_off"])

    def test_weekend_gate_env_file_is_visible_to_web_display(self):
        """周末门同源：`.env` 关周六签到 ⇒ 周六 web 侧判"周六不签"（原先恒不生效）。"""
        self._write_env("YIBAN_SIGN_START=06:30\nYIBAN_SIGN_END=07:50\n"
                        "YIBAN_SATURDAY_SIGN=0\nYIBAN_SUNDAY_SIGN=0\n")
        self.assertEqual(self.webapp._day_off_reason(SATURDAY), "saturday")
        self.assertEqual(self.webapp._day_off_reason(SUNDAY), "sunday")
        self.assertEqual(self.webapp._day_off_reason(WEDNESDAY), "")

    def test_weekend_gate_open_when_env_file_says_so(self):
        """`.env` 开启周六签到 ⇒ 周六不再被判"不签"（读到的是真值而不是缺省）。"""
        self._write_env("YIBAN_SIGN_START=06:30\nYIBAN_SIGN_END=07:50\n"
                        "YIBAN_SATURDAY_SIGN=1\nYIBAN_SUNDAY_SIGN=1\n")
        self.assertEqual(self.webapp._day_off_reason(SATURDAY), "")
        self.assertEqual(self.webapp._day_off_reason(SUNDAY), "")

    def test_day_off_reason_never_raises_on_missing_env(self):
        """`.env` 读不到 ⇒ 按"照常"返回空串（展示侧不得因为读不到配置而 500）。"""
        with contextlib.suppress(OSError):
            os.remove(self.env_file)
        self.assertEqual(self.webapp._day_off_reason(WEDNESDAY), "")


def self_env_file():
    """测试期读当前 `.env` 的映射（与 web 侧同一份解析口径）。"""
    from yiban.infra import env_io
    return env_io.parse_env_file(os.environ["YIBAN_ENV_FILE"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
