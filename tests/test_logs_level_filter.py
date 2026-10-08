# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""`/api/logs` 级别过滤：默认收起 INFO，巡检只看 WARN／ERROR。

巡检第一眼要的是"哪里不对"。`/api/logs` 此前把 yiban 家族全部级别（含 INFO/DEBUG）
一次下发，WARN／ERROR 被过程行冲散。本批给接口加 `level` 参数：
- `warn`（**默认**）：只留 WARNING／ERROR／CRITICAL；
- `all`：保留可见性口径下的全部级别（INFO/DEBUG 照旧可见）。

级别档次只有一份（`web/services/logs.py::LEVEL_RANK`，jmer 统一过的级别语义）：
`_log_line_visible`（组件过滤）与 `_filter_log_level`（档次过滤）共用它。
两层是不同的事实：前者判"哪条 logger 的哪一级进日志页"，后者判"人看面从哪一级起"。

覆盖（四条）：
1. 默认档收起 INFO/DEBUG，只留 WARNING 及以上；响应回显 `level` 与 `collapsed_lines`；
2. `level=all` 恢复 INFO/DEBUG；`collapsed_lines` 归零；
3. 非法 `level` 一律 400（白名单外的取值不得被静默当成某一档）；
4. 导出 `/api/logs/export` **不受**级别过滤影响（它是全量脱敏副本，口径不变）。

标签：F · 前端与界面守卫
覆盖：`web/services/logs.py`（级别档次与过滤）、`web/routes/data.py::api_logs`。
对应实现：同覆盖清单；号码脱敏仍由 `_mask_log_phones` 单出口完成。
关键断言：默认档的内容（不含 INFO）、`collapsed_lines` 计数、`all` 档的恢复、
    非法取值 400、导出不受影响、两处级别判据同源。
依赖：临时 `.env`/SQLite + 临时按天日志文件 + Flask test client；不联网。
"""
import contextlib
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

from yiban import clock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64
ADMIN_PASS = "TestPass1234!"
RAW_PHONE = "13800138001"

#: 各行的**无号标记**：响应里手机号已遮，故断言只用不含号的片段。
M_INFO = "✅ 签到成功"
M_DEBUG = "探针跳过"
M_WARN = "单次尝试耗时偏长"
M_ERROR = "登录失败"
M_WERKZEUG = "GET /api/logs HTTP/1.1"


def _line(date, level, name, msg):
    return f"[{date} 06:31:01] [{level}] {name}: {msg}"


class LogsLevelFilterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-loglevel-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls.users_file = os.path.join(cls.tmp, "users.json")
        with open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
            )
        cls._old_env = {k: os.environ.get(k) for k in (
            "YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE",
            "YIBAN_USERS_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR", "YIBAN_LOG_FILE")}
        os.environ.update({
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_ENV_FILE": cls.env_file,
            "YIBAN_ACCOUNTS_FILE": cls.accounts_file,
            "YIBAN_USERS_FILE": cls.users_file,
            "YIBAN_DB_FILE": cls.db_file,
            "YIBAN_STATE_DIR": cls.tmp,
            "YIBAN_LOG_FILE": os.path.join(cls.tmp, "sign.log"),
        })
        global db
        import db
        spec = importlib.util.spec_from_file_location(
            "webapp_loglevel", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_loglevel"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)
        # 口令明文→哈希的启动迁移显式做掉：否则它的 WARNING 会落进当天日志文件，
        # 让本文件的 `collapsed_lines` 计数随执行顺序漂移（同族先例见 test_logs_by_date）。
        cls.webapp.migrate_admin_password_to_hash(cls.env_file)

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k, old in cls._old_env.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old

    def setUp(self):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        with open(self.accounts_file, "w", encoding="utf-8") as f:
            json.dump([], f)
        db.init_db(self.db_file, migrate_from=self.accounts_file, env_file=self.env_file)
        # 清理临时目录里既有按天日志（跨用例隔离）
        for n in os.listdir(self.tmp):
            if n.startswith("sign-") and n.endswith(".log"):
                os.remove(os.path.join(self.tmp, n))
        self.today = clock.now().strftime("%Y-%m-%d")
        self.lines = [
            _line(self.today, "INFO", "yiban", f"[{RAW_PHONE}] ✅ 签到成功"),
            _line(self.today, "DEBUG", "yiban", "[13800138001] 探针跳过"),
            _line(self.today, "WARNING", "yiban", "单次尝试耗时偏长"),
            _line(self.today, "ERROR", "yiban", "登录失败"),
            # 非 yiban 组件的 INFO 本就不可见（可见性口径），与本档无关
            _line(self.today, "INFO", "werkzeug", '127.0.0.1 - - "GET /api/logs HTTP/1.1" 200 -'),
        ]

    def _write_log(self):
        """写当天日志文件。

        **必须在 create_app 之后**调用：起应用时可能落自己的 WARNING（口令迁移、
        密钥形态告警），那些行也在可见面内，会随执行顺序让计数漂移。先建应用、
        再覆写文件，计数才只含本夹具的行。
        """
        with open(os.path.join(self.tmp, f"sign-{self.today}.log"), "w",
                  encoding="utf-8") as f:
            f.write("\n".join(self.lines) + "\n")

    def _admin_client(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "admin", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:400])
        # 写文件放在建应用与登录**之后**：起应用与登录都可能落自己的告警行
        # （口令迁移、密钥形态），它们在可见面内，会让计数随执行顺序漂移。
        self._write_log()
        return c

    # ---- 行为一：默认档收起 INFO ----
    def test_default_level_hides_info_and_debug(self):
        body = self._admin_client().get("/api/logs").get_json()
        self.assertEqual(body["level"], "warn", "默认档必须是 warn（巡检只看 WARN／ERROR）")
        joined = "\n".join(body["logs"])
        self.assertIn(M_WARN, joined)
        self.assertIn(M_ERROR, joined)
        self.assertNotIn(M_INFO, joined, "INFO 行默认必须收起")
        self.assertNotIn(M_DEBUG, joined, "DEBUG 行默认必须收起")
        self.assertNotIn(M_WERKZEUG, joined, "非 yiban 组件的 INFO 行本就不可见")
        self.assertEqual(body["collapsed_lines"], 2, "被档次收起的是 INFO 与 DEBUG 两条")

    def test_all_level_restores_info_and_debug(self):
        body = self._admin_client().get("/api/logs?level=all").get_json()
        self.assertEqual(body["level"], "all")
        joined = "\n".join(body["logs"])
        for marker in (M_INFO, M_DEBUG, M_WARN, M_ERROR):
            self.assertIn(marker, joined, f"all 档必须放出该行：{marker}")
        self.assertNotIn(M_WERKZEUG, joined, "非 yiban 组件的 INFO 行仍不可见（可见性口径）")
        self.assertEqual(body["collapsed_lines"], 0, "all 档不收起任何行")

    def test_masked_phone_survives_the_default_level(self):
        """档次过滤不得绕开脱敏：默认档内容里仍无 11 位明文号。"""
        text = self._admin_client().get("/api/logs").get_data(as_text=True)
        self.assertNotIn(RAW_PHONE, text)

    def test_unknown_level_is_rejected(self):
        r = self._admin_client().get("/api/logs?level=verbose")
        self.assertEqual(r.status_code, 400)
        self.assertIn("level", r.get_json()["error"])

    def test_export_is_not_level_filtered(self):
        """导出是全量脱敏副本：级别过滤只作用于页面接口，导出口径不变。"""
        r = self._admin_client().get(f"/api/logs/export?date={self.today}")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:300])
        body = r.get_data(as_text=True)
        self.assertIn("签到成功", body, "导出仍应含 INFO 行（口径不变）")
        self.assertNotIn(RAW_PHONE, body, "导出仍是脱敏副本")


class LevelCaliberTest(unittest.TestCase):
    """两级判据同源：`_log_line_visible` 与 `_filter_log_level` 共用一份级别档次。"""

    def test_default_level_constant_and_whitelist(self):
        from web.services import logs as logs_svc
        self.assertEqual(logs_svc.LOG_LEVEL_DEFAULT, "warn")
        self.assertEqual(set(logs_svc.LOG_LEVELS), {"warn", "all"})
        self.assertLess(logs_svc.level_rank("INFO"), logs_svc.level_rank("WARNING"))
        self.assertLess(logs_svc.level_rank("DEBUG"), logs_svc.level_rank("INFO"))
        self.assertLess(logs_svc.level_rank("WARNING"), logs_svc.level_rank("ERROR"))
        self.assertLess(logs_svc.level_rank("ERROR"), logs_svc.level_rank("CRITICAL"))

    def test_visibility_uses_the_same_rank_table(self):
        """可见性口径 == "档次不低于 WARNING"：改档次表即改可见性（同源，不是两份判据）。"""
        from web.services import logs as logs_svc
        for level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            self.assertEqual(
                logs_svc._log_line_visible(level, "mailer"),
                logs_svc.level_rank(level) >= logs_svc.level_rank("WARNING"),
                f"{level} 的组件可见性必须由同一份档次表推出")

    def test_filter_helper_counts_collapsed_lines(self):
        from web.services import logs as logs_svc
        lines = [
            "[2026-10-08 06:31:01] [INFO] yiban: a",
            "[2026-10-08 06:31:02] [WARNING] yiban: b",
            "[2026-10-08 06:31:03] [ERROR] yiban: c",
        ]
        kept, collapsed = logs_svc._filter_log_level(lines, "warn")
        self.assertEqual(len(kept), 2)
        self.assertEqual(collapsed, 1)
        kept_all, collapsed_all = logs_svc._filter_log_level(lines, "all")
        self.assertEqual(len(kept_all), 3)
        self.assertEqual(collapsed_all, 0)


class FrontendDefaultLevelGuardTest(unittest.TestCase):
    """跨语言对拍：级别档默认值只有后端一份，前端不得内联默认档位。

    后端单一出处：`web/services/logs.py::LOG_LEVEL_DEFAULT`。前端只认两档**取值**
    （warn / all），不认哪一档是默认——首屏不带 `level`，档位以服务端回执为准
    （响应已回 `level` 与 `collapsed_lines`）；用户切换后才显式下发。
    本用例是跨语言守卫：把前端默认值加回去即红。
    """

    FRONTEND_LOGS = ("format.ts", "run-events.ts", "Logs.vue")
    FORBIDDEN_TOKENS = ("DEFAULT_LOG_LEVEL",)
    #: "档位取值声明"的合法标识符。它们声明**取值**（warn / all 两个档名），
    #: 不是声明哪一档是**默认值**，必须放行（否则守卫会咬住合法取值声明）。
    ALLOWED_LEVEL_VALUE_NAMES = ("WARN_LOG_LEVEL", "ALL_LOG_LEVEL")
    #: 内联默认档位的写法。V2 加固：从窄"形参默认值"扩到更宽的常见形状——
    #: ① 标识符赋值 / 对象字面量：`const level = "warn"` / `{ level: "warn" }`
    #: ② 响应式挂起值：`ref("warn")`
    #: ③ 查询串直接内联：`params.set("level", "warn")`
    #: 复审实测：旧正则（仅 `level:…= "warn"`）对上述形状全部不命中（弱牙）。
    #: **宽口径**（PM 2026-10-08 裁决，恢复 L6 之前的口径）：首条正则命中**任意**
    #: 标识符赋 `"warn"`/`"all"` 字面量，**不按标识符名收窄**。L6 曾把它收窄成
    #: "标识符须含 level 子串"，于是 `const lv = "warn";` 这类形状被放跑；漏报的代价
    #: （前端悄悄内联默认档位）远高于误报的代价（一条响亮失败，人工几秒判掉）。
    #: 已知噪声（刻意保留，不修）：宽口径会咬与档位无关的赋值，如
    #: `const scope = "all";`（见 `KNOWN_NOISE_HITS`）——属可接受的响亮失败，需人工判。
    #: 已知盲区（本正则不咬，不假装覆盖完整）：反引号模板 `const level = `warn`;`、
    #: `` ref(`warn`) `` 新旧正则都不命中；剥注释只认 ' / " / ` 三种引号
    #: （见 `_strip_comments`），模板里的 `${}` 内注释也不另剥。
    #: **行为面的真守卫**在 `frontend/src/logs/format.spec.ts` 的 `buildLogsQuery`
    #: 断言（首屏不带 `level`、切换后才下发，那条有牙）；本正则只是源码文本层的行程碑。
    FORBIDDEN_DEFAULT_RES = (
        re.compile(r"([A-Za-z_$][\w$]*)\s*[:=]\s*[\"'](?:warn|all)[\"']"),
        re.compile(r"\bref\(\s*[\"'](?:warn|all)[\"']\s*\)"),
        re.compile(r"\.set\(\s*[\"']level[\"']\s*,\s*[\"'](?:warn|all)[\"']"),
    )
    #: 覆盖不许回退（F1）：宽口径必须命中的**标识符**集合。含不含 level 子串的
    #: （`lv` / `dflt`）。上一轮把首条正则改窄成"标识符须含 level"、又把 `must_hit`
    #: 的 `lv` 例改写成 `level` 例，覆盖因此静默回退；本集合就是那次回退的守卫。
    REQUIRED_HIT_IDENTIFIERS = ("level", "lv", "dflt", "defLevel", "LEVEL")
    #: 宽口径下的**已知噪声**（刻意保留的命中）：与档位无关的赋值也会被咬。
    #: 判决依据见 PM 2026-10-08 裁决：响亮失败 + 人工判掉，代价可接受。
    #: 它不进 `must_pass`（宽口径下它确实命中），改放这里显式登记。
    KNOWN_NOISE_HITS = (
        'const scope = "all";',   # 与档位无关的赋值
    )

    @staticmethod
    def _strip_comments(src):
        """剥掉 JS/TS 注释，**认字符串字面量**。只影响匹配输入，不改判定对象。

        为什么必须剥注释：注释里的 `level = "warn"` 是**说明文字**，不是内联默认档位。
        复审实测：不剥注释时 `// 默认档 level = "warn" 是后端定的` 被咬成命中（假阳性）。
        为什么必须**认字符串**（F2）：裸正则剥注释会吞掉字符串里的 `//` 与 `/*`——
        `const BASE = "https://api"; const level = "warn";` 里 `//api...` 被当成行注释，
        后面的真赋值被吞（漏报）；`const s = "/*"; const level = "warn"; */ x` 里字符串里的
        `/*` 被当成块注释起始，吞到下一个 `*/`（漏报）。两条都是新开的盲区，必须关闭。
        做法：逐字符扫描（不是正则），识别 ' / " / ` 三种引号并处理 `\\` 转义；
        字符串内部一律不参与注释识别；字符串外才剥 `/* */`（可跨行）与 `//`（到行尾）。
        """
        out = []
        i, n = 0, len(src)
        quote = ""
        while i < n:
            ch = src[i]
            if quote:
                out.append(ch)
                if ch == "\\" and i + 1 < n:
                    out.append(src[i + 1])
                    i += 2
                    continue
                if ch == quote:
                    quote = ""
                i += 1
                continue
            if ch in "'\"`":
                quote = ch
                out.append(ch)
                i += 1
                continue
            if ch == "/" and i + 1 < n and src[i + 1] == "*":
                j = src.find("*/", i + 2)
                if j == -1:
                    break
                out.append(" ")
                i = j + 2
                continue
            if ch == "/" and i + 1 < n and src[i + 1] == "/":
                j = src.find("\n", i)
                if j == -1:
                    break
                out.append(" ")
                i = j
                continue
            out.append(ch)
            i += 1
        return "".join(out)

    def test_backend_is_the_single_source_of_the_default_level(self):
        from web.services import logs as logs_svc
        self.assertEqual(logs_svc.LOG_LEVEL_DEFAULT, "warn")
        self.assertIn(logs_svc.LOG_LEVEL_DEFAULT, logs_svc.LOG_LEVELS)

    def _inline_default_offenders(self, name, src):
        """→ 该源文件里"内联默认档位"的命中清单（空 = 干净）。匹配前剥离注释。"""
        src = self._strip_comments(src)
        out = []
        for tok in self.FORBIDDEN_TOKENS:
            if tok in src:
                out.append(f"{name}: {tok}")
        for rx in self.FORBIDDEN_DEFAULT_RES:
            for m in rx.finditer(src):
                # 放行合法的"取值声明"：那是档名，不是默认档。
                if m.groups() and m.group(1) in self.ALLOWED_LEVEL_VALUE_NAMES:
                    continue
                out.append(f"{name}: {m.group(0)}")
        return out

    def test_frontend_does_not_inline_the_default_level(self):
        base = os.path.join(BASE, "frontend", "src", "logs")
        offenders = []
        for name in self.FRONTEND_LOGS:
            with open(os.path.join(base, name), encoding="utf-8") as fh:
                offenders += self._inline_default_offenders(name, fh.read())
        self.assertEqual(
            offenders, [],
            "前端不得内联默认档位（档位默认值只有后端一份）：" + repr(offenders),
        )

    def test_inline_default_shapes_are_actually_caught(self):
        """守卫自身的牙齿：复审点名的内联形状必须被命中，且注释文本不误咬。

        没有这条，下一个把正则改窄的人会让守卫静默失效（弱牙复发）。本用例钉住：
        ① 必修形状全部命中（含 `lv` 等不含 level 子串的标识符，见 F1）；
        ② "合法取值声明放行"（咬住 `WARN_LOG_LEVEL = "warn"` 会把守卫变成噪声）；
        ③ **注释文本**不命中（`//` 与 `/* */` 两种）。
        宽口径的已知噪声（与档位无关的 `= "all"`）另由
        `test_known_noise_of_the_wide_caliber_is_accepted` 登记，不在此处冒充"放行"。
        """
        must_hit = (
            'const level = "warn";',                # 初始赋值
            'const lv = "warn";',                   # F1 加回：不含 level 子串的标识符
            'if (!lv) lv = "warn";',                # F1 加回：lv 形态的回退赋值
            'let dflt = "warn";',                   # F1 新增
            'const defLevel = "all";',              # F1 新增
            'const levelOverride = ref("warn");',   # 响应式挂起值
            'if (!level) level = "warn";',          # 回退赋值
            'const LEVEL = "warn";',                # 大写标识符
            'params.set("level", "warn");',         # 查询串内联
            'const opt = { level: "all" };',        # 对象字面量
            # F2：字符串里的 `//` 与 `/*` 不得吞掉其后的真赋值
            'const BASE = "https://api"; const level = "warn";',
            'const s = "/*"; const level = "warn"; */ x',
        )
        for snippet in must_hit:
            self.assertTrue(
                self._inline_default_offenders("sample.ts", snippet),
                f"守卫必须命中内联默认档位形状：{snippet!r}",
            )
        must_pass = (
            'export const WARN_LOG_LEVEL = "warn";',   # 取值声明
            'export const ALL_LOG_LEVEL = "all";',     # 取值声明
            "const warnOnly = ref(true);",             # 布尔挂起值
            'const levelOverride = ref("");',          # 空串挂起值
            'if (lv) params.set("level", lv);',        # 变量下发
            '// 默认档 level = "warn" 是后端定的',      # 行注释不是定义
            '/* 说明：level = "warn" */',              # 块注释不是定义
        )
        for snippet in must_pass:
            self.assertEqual(
                self._inline_default_offenders("sample.ts", snippet), [],
                f"必须放行（取值声明 / 变量下发 / 注释）：{snippet!r}",
            )

    def test_the_wide_caliber_matches_any_identifier_name(self):
        """覆盖不许回退（F1）：首条正则必须命中**任意**标识符赋 `"warn"`/`"all"`。

        上一轮把首条正则收窄成"标识符须含 level 子串"（`(\\w*[Ll][Ee][Vv][Ee][Ll]\\w*)`），
        于是 `const lv = "warn";` 不再命中；同一笔又把 `must_hit` 里的 `lv` 例改写成
        `level` 例，自测跟着调口径，覆盖静默回退（复审活体突变实测：写进源码仍绿）。
        本断言直接钉住"任意标识符都命中"这条性质，改窄正则即红。
        """
        for name in self.REQUIRED_HIT_IDENTIFIERS:
            snippet = f'const {name} = "warn";'
            self.assertTrue(
                self._inline_default_offenders("sample.ts", snippet),
                f"宽口径必须命中任意标识符赋值（含不含 level 子串的）：{snippet!r}")
        self.assertTrue(
            self._inline_default_offenders("sample.ts", 'if (!lv) lv = "all";'),
            "宽口径必须命中 lv 形态的回退赋值")

    def test_known_noise_of_the_wide_caliber_is_accepted(self):
        """宽口径的已知噪声被显式登记（F1）：与档位无关的赋值会命中，属刻意保留。

        宽口径下 `const scope = "all";` 确实命中，故它不进 `must_pass`；本断言钉住
        这条噪声，下一个想"顺手消掉它"的人必须显式改这条断言与 docstring 口径，
        不许悄悄把正则收窄回去（那正是上一轮放跑 `lv` 形状的路径）。
        """
        for snippet in self.KNOWN_NOISE_HITS:
            self.assertTrue(
                self._inline_default_offenders("sample.ts", snippet),
                f"已知噪声应被宽口径命中（要消掉它须走显式口径变更）：{snippet!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
