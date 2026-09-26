# -*- coding: utf-8 -*-
"""签到日历状态可见性：状态表单一源（日期格渲染 + 图例）与急停/周末门真值显示。

标签：F · 前端与界面守卫
覆盖：状态显示表 `yiban.status.DISPLAY` 覆盖全部状态码并被日历图例与前端状态行两侧消费；`global_paused`/`no_position` 不再渲染成"排队待签"；日历格对非成功/失败状态显示状态符号；急停/周末门在 web 侧读 `.env` 真值（与引擎同源）
对应实现：`yiban/status.py`（`DISPLAY` / `legend_items` / `display_payload`）、`web/templates/partials/page_sign_calendar.html`、`web/static/js/components/sign-calendar-view.js`（`statusLine`）、`web/static/js/calendar.js`（`dayCell`）、`web/services/signstatus.py`（`_day_off_reason` / `day_off_text`）、`web/routes/pages.py`
关键断言：状态枚举与图例同源——图例项由 `DISPLAY` 生成，往表里加一个新状态码即自动进图例（用例直接改表断言，不靠"人记得改两处"）；`statusLine` 对 `global_paused`/未知码绝不再回落成"排队待签"；web 侧门判定与引擎 `schedule.day_off` 读同一份 `.env` 真值（置位急停 ⇒ 两侧同时为"暂停"，复位 ⇒ 同时恢复）
依赖：⚠ **需要 node 真跑**——`statusLine` / `dayCell` / `stateEntry` 按花括号配对从源码抽出后交给 node 执行，`shutil.which("node")` 取不到时这两个类整类 `skipUnless`。其余为纯本地 Flask test client + 临时 `.env`/SQLite；不联网、不访问真实易班接口

**为什么需要**：登记表 MF-45 的四处病灶里，两处是"同一事实两份定义"——
①前端状态行 `sign-calendar-view.js:30-44` 逐码手写文案但漏了 `global_paused`/`no_position`，
  于是急停被渲染成"待签到 · 前方排队 N 人"（面板给的就是安心假信号）；
②日历图例是模板里手写的四个 `<li>`，12 个状态码只覆盖到 2 个，状态行认得的与图例认得的
  是两份清单，必然漂移。
判别的方式只有一条：让两侧消费**同一份表**，并把"新增状态自动进两侧"做成可执行的断言；
再加"web 侧门判定读 `.env` 真值"——引擎读 `.env` 会真暂停，而 web 侧原来经
`day_off(env=None)` 落回 `os.environ`，在 web 进程里恒不生效，于是"实际暂停、界面说没有"。

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

SIGN_CAL_VIEW = os.path.join(BASE, "web", "static", "js", "components", "sign-calendar-view.js")
CALENDAR_JS = os.path.join(BASE, "web", "static", "js", "calendar.js")
CAL_PARTIAL = os.path.join(BASE, "web", "templates", "partials", "page_sign_calendar.html")
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
        """每格都要有符号 / 文案 / 图例名 / 语气档；语气档必须落在状态行既有的四个类里。"""
        tones = {"ok", "warn", "bad", "muted"}
        for code in yiban_status.ALL_STATUSES:
            entry = yiban_status.DISPLAY[code]
            with self.subTest(code=code):
                self.assertTrue(entry["symbol"])
                self.assertTrue(entry["text"])
                self.assertTrue(entry["legend"])
                self.assertIn(entry["tone"], tones)

    def test_legend_covers_every_symbol_in_the_table(self):
        """图例项覆盖表里出现的**每一个**符号——"渲染认得、图例不认得"正是本条要杜绝的。"""
        table_symbols = {yiban_status.DISPLAY[c]["symbol"] for c in yiban_status.ALL_STATUSES}
        legend_symbols = {item["symbol"] for item in yiban_status.legend_items()}
        self.assertEqual(legend_symbols, table_symbols)
        self.assertEqual(len(yiban_status.legend_items()), len(table_symbols),
                         "同一符号只应出现一条图例（重复条会让图例变成噪声）")

    def test_new_status_code_enters_the_legend_automatically(self):
        """新增状态码 ⇒ 图例自动含：直接往表里塞一格，图例与前端载荷两侧都要出现。"""
        fake = "brand_new_state"
        patched = dict(yiban_status.DISPLAY)
        patched[fake] = {"symbol": "🆕", "text": "出厂新状态", "legend": "新状态",
                         "tone": "warn"}
        try:
            yiban_status.DISPLAY = patched
            labels = [it["label"] for it in yiban_status.legend_items()]
            self.assertIn("新状态", labels, "图例未随表变化——说明它不是从表生成的")
            self.assertIn("🆕", [it["symbol"] for it in yiban_status.legend_items()])
            payload = yiban_status.display_payload()
            self.assertIn(fake, payload["by_code"])
            self.assertIn("🆕", payload["by_symbol"])
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
        """同一符号的不同状态码必须共享同一语气档（否则日期格按符号取档会随状态抖动）。"""
        by_symbol = yiban_status.display_payload()["by_symbol"]
        for item in yiban_status.legend_items():
            with self.subTest(symbol=item["symbol"]):
                self.assertIn(item["symbol"], by_symbol)


@unittest.skipUnless(NODE, "node 不可用：跳过前端状态行/日期格的 JS 行为用例")
class StatusLineJsTest(unittest.TestCase):
    """`statusLine`（账号卡状态行）真跑：从表取文案与语气档，杜绝"排队待签"假信号。"""

    @classmethod
    def setUpClass(cls):
        cls.fn_src = _extract_function(_read(SIGN_CAL_VIEW), "statusLine")

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
    """日期格：语气档与标签取自注入的状态表；非成功/失败状态要留下符号角标。"""

    @classmethod
    def setUpClass(cls):
        src = _read(CALENDAR_JS)
        cls.entry_src = _extract_function(src, "stateEntry")
        cls.cell_src = _extract_function(src, "dayCell")

    def _run(self, cells, payload):
        script = (
            "var window = { YB_CALENDAR_STATE: " + json.dumps(payload, ensure_ascii=False) + " };\n"
            + _ESC_JS + self.entry_src + "\n" + self.cell_src + "\n"
            "var cells = " + json.dumps(cells, ensure_ascii=False) + ";\n"
            "console.log(JSON.stringify(cells.map(dayCell)));\n"
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

    def test_ok_and_bad_cells_keep_their_dots(self):
        payload = yiban_status.display_payload()
        ok, bad = self._run(
            [self._cell(state="✅"), self._cell(state="❌")], payload)
        self.assertIn("sc-cell--ok", ok)
        self.assertIn("sc-dot--ok", ok)
        self.assertIn("sc-cell--bad", bad)
        self.assertIn("sc-dot--bad", bad)

    def test_other_states_show_a_symbol_badge(self):
        """时段外/无点位等不再渲染成空白格——那是日历侧的另一半"看起来没发生任何事"。"""
        payload = yiban_status.display_payload()
        no_pos, skipped = self._run(
            [self._cell(state="🚫"), self._cell(state="⛔")], payload)
        self.assertIn("sc-sym", no_pos)
        self.assertIn("🚫", no_pos)
        self.assertNotIn("sc-cell--ok", no_pos)
        self.assertNotIn("sc-cell--bad", no_pos)
        self.assertIn("点位", no_pos, "读屏名必须说明这一格发生了什么")
        self.assertIn("sc-sym", skipped)

    def test_empty_day_has_no_badge(self):
        payload = yiban_status.display_payload()
        empty = self._run([self._cell()], payload)[0]
        self.assertNotIn("sc-sym", empty)
        self.assertIn("查看签到记录", empty)

    def test_off_day_does_not_overlay_a_state_badge(self):
        """周末停签格已有「休」角标，状态符号不得叠在同一角（角标互挤不可读）。"""
        payload = yiban_status.display_payload()
        off = self._run([self._cell(state="🚫", off=True, offDay="六")], payload)[0]
        self.assertIn("sc-off", off)
        self.assertNotIn("sc-sym", off)


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
    """日历页：图例与内联状态上下文都由服务端从同一份表渲染。"""

    @classmethod
    def setUpClass(cls):
        cls._isolate_env()

    @classmethod
    def tearDownClass(cls):
        cls._teardown_env()

    def test_legend_renders_every_item_of_the_table(self):
        html = self._calendar_html()
        for item in yiban_status.legend_items():
            with self.subTest(item=item["symbol"]):
                self.assertIn(item["label"], html, "图例缺了表里的一条")
                self.assertIn(item["symbol"], html)
        self.assertIn("周末停签", html)   # 非状态通道（周末停签/今天）仍在
        self.assertIn("今天", html)

    def test_legend_is_generated_not_hand_written(self):
        """模板不得再手写图例项：表里没有的符号不会凭空出现在图例里，反之亦然。"""
        src = _read(CAL_PARTIAL)
        self.assertIn("status_legend", src, "图例必须由表生成的列表驱动")
        html = self._calendar_html()
        for stale in ("sc-swatch--ok", "sc-swatch--bad"):
            self.assertNotIn(stale, html, "图例仍在用写死的成功/失败色块")

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
