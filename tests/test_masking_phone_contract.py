# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""census P0-3：手机号遮罩契约化（单源后端遮罩 + 前端零自遮）。

缺陷（`D:/code/_census/REPORT.md` census-P0-3）：同一个手机号要过 9 个出口，
其中 3 个各带一份公式副本；JS 侧判长是 `>=7`，7 位输入保留全部数字、插入 `****`
伪装成已遮（零遮罩还带伪装）。B0 刀7 实测缺口：`+86` 前缀与带空格形态原样穿过。

契约（本文件钉住的三条）：
1. 自由文本里**任何**手机号形态（裸 11 位 / `+86` 前缀 / 空格 / 连字符分段）
   一律遮成 `138****8000`；
2. 遮罩由后端单源完成——前端不再自行遮罩，任何 JS 侧遮罩公式都是缺陷；
3. 下发手机号的**展示面**（列表与日志）已遮——响应里不得出现 11 位连续明文。

标签：G · 安全：脱敏/审计/配置注入
覆盖：`mask_phones_in_text` / `mask_phone` / `sanitize_url` 的号码形态口径；
      `/api/logs` 与 `/api/accounts` 的响应出口；前端遮罩公式的**定义点门禁**
      （core.js / frontend/src / 已入库 Vue 产物）。
对应实现：`yiban/masking.py`（唯一号码口径）、`web/services/logs.py`、
      `web/routes/data.py`、`web/services/accounts_data.py`；
      被删除方：`web/static/js/core.js::maskPhone`、`frontend/src/lib/shell.ts::maskPhone`。
关键断言：号码形态用真函数跑；前端公式门禁是**已知形态的黑名单**（6 条形状正则 × 产品
     逻辑与模板目录）——本批删掉的写法回潮即红，等价改写不在射程内（兜底靠 census 名册与人工普查）。
依赖：纯本地（临时 `.env`/SQLite）；定义点门禁需要 git 无关，直接扫工作树。
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

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64
ADMIN_PASS = "TestPass1234!"
PHONE = "13800138001"
MASKED = "138****8001"

#: 号码遮罩公式的"指纹"正则：任何一处出现即代表又长出一个公式定义点。
#: 只允许命中 `yiban/masking.py`（唯一真源）。
FORMULA_RES = (
    re.compile(r"\[\s*:\s*3\s*\]\s*\+\s*[\"'`]\*{4}"),            # p[:3] + "****"
    re.compile(r"\[\s*:\s*3\s*\]\s*\}\*{4}"),                      # f"{p[:3]}****"
    re.compile(r"[\"'`]\*{4}[\"'`]\s*\+\s*[A-Za-z_$][\w$.]*\[\s*7\s*:"),  # "****" + p[7:]
    re.compile(r"\.slice\(\s*0\s*,\s*3\s*\)\s*\+\s*[\"'`]\*{4}"),  # p.slice(0, 3) + "****"
    re.compile(r"[\"'`]\*{4}[\"'`]\s*\+\s*[A-Za-z_$][\w$.]*\.slice\(\s*-4"),  # "****" + p.slice(-4)
    re.compile(r"slice\(\s*0\s*,\s*3\s*\)\s*\}\*{4}"),             # `${p.slice(0,3)}****${p.slice(-4)}`
)

#: 公式门禁扫描面：产品逻辑与模板目录（含 `.html`，模板里的内联 script 也是出口载体）。
#: 文档/测试/厂商件不在内（它们不是出口）。
FORMULA_SCAN_DIRS = (
    os.path.join("yiban",),
    os.path.join("web", "routes"),
    os.path.join("web", "services"),
    os.path.join("web", "static", "js"),
    os.path.join("web", "static", "vue"),
    os.path.join("web", "templates"),
    os.path.join("frontend", "src"),
)


#: 本文件会覆盖的环境键（收尾按旧值还原，见 `DisplaySurfacesAreMaskedTest`）。
_ENV_KEYS = (
    "YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE",
    "YIBAN_USERS_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR", "YIBAN_LOG_FILE",
)


def _iter_scan_files():
    for rel in FORMULA_SCAN_DIRS:
        root = os.path.join(BASE, rel)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in ("node_modules", ".vite")]
            for name in filenames:
                if name.endswith((".py", ".js", ".ts", ".vue", ".html")):
                    yield os.path.join(dirpath, name)


class PhoneTextFormMaskingTest(unittest.TestCase):
    """行为一：`+86` 前缀与空格/连字符分段形态一律遮成同一规范形。"""

    def _mask(self, text):
        from yiban.masking import mask_phones_in_text
        return mask_phones_in_text(text)

    def test_every_written_form_masks_to_canonical_form(self):
        for raw in (
            "13800138000",
            "+8613800138000",
            "+86 138 0013 8000",
            "138 0013 8000",
            "138-0013-8000",
            "86-138-0013-8000",
            # 首位 `1` 之后的分隔符：分段写在首个数字后同样是"数字之间"
            "1 3800138000",
            "1-3800138000",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(self._mask("账号 " + raw + " 失败"), "账号 138****8000 失败")

    def test_prefix_and_spaced_forms_are_idempotent(self):
        once = self._mask("账号 +86 138 0013 8000 失败")
        self.assertEqual(once, "账号 138****8000 失败")
        self.assertEqual(self._mask(once), once, "重复脱敏不得再变形")

    def test_masked_value_helper_covers_same_forms(self):
        from yiban.masking import mask_phone
        for raw in ("13800138000", "+8613800138000", "+86 138 0013 8000", "138 0013 8000"):
            with self.subTest(raw=raw):
                self.assertEqual(mask_phone(raw), "138****8000")
        # 非号形态原样返回，且**不得**产出"插了星号的伪装串"（JS `>=7` 的老缺陷）
        for raw in ("1380013800", "138001380001", "1234567"):
            with self.subTest(raw=raw):
                self.assertEqual(mask_phone(raw), raw)

    def test_both_calibers_are_not_narrower_than_the_write_side_validator(self):
        """值口径与**文本口径**都必须与写侧校验 `PHONE_RE = ^1\\d{10}$` 同宽。

        写侧收下的号遮罩侧必须遮得住：只放宽值口径时，次位 0–2 的已入库号会从日志出口
        （`_mask_log_phones` → `mask_phones_in_text`）原样下发，而前端已不再自遮。
        """
        from yiban.masking import mask_phone, mask_phones_in_text
        for raw in ("12012345678", "10012345678"):
            want = raw[:3] + "****" + raw[7:]
            with self.subTest(raw=raw, caliber="value"):
                self.assertEqual(mask_phone(raw), want)
            with self.subTest(raw=raw, caliber="text"):
                self.assertEqual(mask_phones_in_text(f"[{raw}] 签到成功"), f"[{want}] 签到成功")

    def test_value_form_is_whole_string_only(self):
        """整串必须是号码：带前后缀散文的值不得被剥离出号码段（否则 `_mask_phone` 会吞正文）。"""
        from yiban.masking import mask_phone
        for raw in ("tel:13800138000", "13800138000 (备用)", "138.0000.8000"):
            with self.subTest(raw=raw):
                self.assertEqual(mask_phone(raw), raw)

    def test_non_phone_digit_runs_still_untouched(self):
        for raw in (
            "1380013800", "138001380001", "20260923063", "1758500000000",
            "[2026-09-23 06:31:01] 定位 118.88459277562808 dur=118.88459277562808",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(self._mask(raw), raw, "坐标/时间戳/非号码数字串不得误伤")


class SanitizeUrlPhoneTest(unittest.TestCase):
    """出口③：URL 参数值里的号码（含 `+86`）必须与文本同口径。"""

    def test_url_value_phone_forms_masked(self):
        from yiban.masking import sanitize_url
        for query in (
            "u=13800138000",
            "u=%2B8613800138000",
            "u=%2B86%20138%200013%208000",
            # 未编码的 `+`：`parse_qsl` 解成空格（`" 8613800138000"`）——值口径必须容忍两侧空白
            "u=+8613800138000",
        ):
            with self.subTest(query=query):
                out = sanitize_url("https://host/p?" + query)
                self.assertNotIn("13800138000", out)
                self.assertNotIn("8613800138000", out)
                self.assertIn("138****8000", out)

    def test_url_value_with_embedded_phone_masked(self):
        """号码嵌在散文里的值同样遮——只认"整值即号码"时，前后缀一加就整串放行入日志。"""
        from yiban.masking import sanitize_url
        for query in ("u=tel:13800138000", "u=13800138000%20(%E5%A4%87%E7%94%A8)"):
            with self.subTest(query=query):
                out = sanitize_url("https://host/p?" + query)
                self.assertNotIn("13800138000", out)
                self.assertIn("138****8000", out)


class NoFrontendMaskFormulaTest(unittest.TestCase):
    """行为二：前端不得再有手机号遮罩公式。

    **覆盖面如实说明**：本类是**已知形态的黑名单**，不是"任何公式都认得出"的证明。
    它用 6 条形状正则（切片拼接、模板串拼接、Python f-string 拼接）+ 一批扩展名扫
    产品逻辑与模板目录，覆盖本批删掉的那几种写法；**等价改写会漏**（`substring` /
    `replace` / `join` 拼出来的遮罩串不在黑名单里），新形态也会漏。真正的兜底有两层：
    上游 census 名册（`D:/code/_census/out/ROSTER.csv`）与人工普查。护栏的价值在
    "回潮即红"，不在"穷举"。
    """

    def test_no_phone_mask_formula_outside_the_single_source(self):
        allowed = os.path.join("yiban", "masking.py")
        offenders = []
        for path in _iter_scan_files():
            rel = os.path.relpath(path, BASE)
            if rel == allowed:
                continue
            try:
                with open(path, encoding="utf-8") as fh:
                    src = fh.read()
            except (OSError, UnicodeDecodeError):
                continue
            if any(rx.search(src) for rx in FORMULA_RES):
                offenders.append(rel)
        self.assertEqual(
            offenders, [],
            "手机号遮罩公式出现第二份定义点（契约：遮罩只由后端单源完成）：" + repr(offenders),
        )

    def test_core_js_no_longer_exposes_mask_phone(self):
        path = os.path.join(BASE, "web", "static", "js", "core.js")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("maskPhone", src, "外壳不得再暴露 maskPhone（前端不再自行遮罩）")

    def test_vue_built_assets_have_no_mask_phone(self):
        """已入库的 Vue 产物也要干净——否则线上跑的还是自遮逻辑。"""
        assets = os.path.join(BASE, "web", "static", "vue", "assets")
        offenders = []
        for name in os.listdir(assets):
            if not name.endswith(".js"):
                continue
            with open(os.path.join(assets, name), encoding="utf-8") as fh:
                if "maskPhone" in fh.read():
                    offenders.append(name)
        self.assertEqual(offenders, [], "已入库 Vue 产物仍带 maskPhone（需重新构建）：" + repr(offenders))


class DisplaySurfacesAreMaskedTest(unittest.TestCase):
    """行为三：下发手机号的展示面在 HTTP 出口处已遮（响应里无 11 位连续明文）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-mask-contract-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls.users_file = os.path.join(cls.tmp, "users.json")
        cls.state_dir = os.path.join(cls.tmp, "state")
        # 日志路径必须指向临时目录：本类**植入**三行未遮形态的日志文本，落在共享/部署
        # 默认路径上会把测试数据写进真实按天日志（同仓其余测试文件同口径，本类不设就是漏）。
        cls.log_file = os.path.join(cls.tmp, "sign.log")
        os.makedirs(cls.state_dir, exist_ok=True)
        with open(cls.env_file, "w", encoding="utf-8") as fh:
            fh.write(
                f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                f"YIBAN_ADMIN_USER=admin\nYIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
            )
        # 账号在 init_db 时由 JSON→SQLite 迁移装载（明文密码随迁移加密）。
        with open(cls.accounts_file, "w", encoding="utf-8") as fh:
            json.dump([{
                "name": "契约账号", "phone": PHONE, "password": "contract-pass",
                "owner": "admin", "status": "active", "sort_order": 0,
            }], fh, ensure_ascii=False)

        # 逐键**记旧值**再覆盖：`tests/conftest.py` 给 ENV_FILE / LOG_FILE / STATE_DIR 设了
        # 会话级临时默认，若收尾直接 pop 就把那层兜底删掉，后续用例会回落到真实 `/var/log/yiban`
        # 与 cwd 的 .env（本类不制造这种跨用例污染）。
        cls._old_env = {k: os.environ.get(k) for k in _ENV_KEYS}
        os.environ["YIBAN_ACCOUNTS_KEY"] = TEST_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_USERS_FILE"] = cls.users_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.state_dir
        os.environ["YIBAN_LOG_FILE"] = cls.log_file

        global db
        import db  # 裸模块名：pyproject 的 pythonpath 已含 scripts

        # 库单例是**进程级**的：先清掉上一个测试模块可能留下的连接。不清就会踩
        # `init_db` 的既有语义——已存在连接时它复用旧连接、跳过 JSON→SQLite 迁移，
        # 于是本类的 accounts.json→SQLite 迁移被整段跳过、`/api/accounts` 回空表
        # （判据见 test_accounts_response_has_no_plaintext_phone 的"账号未按预期装载"断言）。
        # 这是**绕过**，不是根因修法：根因单 `yiban-auto-sign-mgq5` 修好后应删除本段。
        # 口径是「机制复现 ＋ 加固」：机制用已知留下连接的模块确定性复现（旧树红、新树绿），
        # 不是"全量实测伪红"。加固的校验者见
        # tests/test_run_events_inspection.py::LeftoverConnectionFixtureGuardTest。
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None

        spec = importlib.util.spec_from_file_location(
            "webapp_mask_contract", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_mask_contract"] = cls.webapp
        with contextlib.suppress(Exception):
            spec.loader.exec_module(cls.webapp)

        db.init_db(cls.db_file, migrate_from=cls.accounts_file, env_file=cls.env_file)

        from datetime import datetime
        cls.today = datetime.now().strftime("%Y-%m-%d")
        # 日志文本里放四种未遮形态：裸号、次位 0–2 的已入库号（写侧校验收、旧 `1[3-9]` 口径漏）、
        # `+86` 前缀、空格分段。写入**按天文件名**（`log_path_for` 的派生规则），故用它的返回值。
        with open(cls.webapp.log_path_for(cls.today), "w", encoding="utf-8") as fh:
            fh.write("\n".join([
                f"[{cls.today} 06:31:01] [INFO] yiban: [{PHONE}] 签到成功",
                f"[{cls.today} 06:31:02] [WARNING] yiban: 上游回显 +8613800138000 与 138 0013 8000",
                f"[{cls.today} 06:31:03] [WARNING] yiban: 旧口径漏网号 12012345678",
            ]) + "\n")

    @classmethod
    def tearDownClass(cls):
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k, old in cls._old_env.items():   # 还原会话级默认，不做"删掉当清理"
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old

    def _admin_client(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "admin", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:400])
        return c

    def test_log_fixture_is_isolated_to_the_temp_dir(self):
        """植入的日志必须落在临时目录：落在共享/部署默认路径上就是把测试数据写进真实日志。

        同族多数测试自设该键（56/236）；本类不设时，pytest 跑法由 conftest 的会话级临时目录兜底，
        unittest 直跑则落真实部署路径，故仍须自设。
        隔离一旦被改回，这条红；`tearDownClass` 删整个临时目录，故植入行不残留。
        """
        path = self.webapp.log_path_for(self.today)
        self.assertEqual(os.path.dirname(path), self.tmp,
                         f"日志路径未隔离到临时目录：{path}")
        self.assertTrue(os.path.isfile(path), "临时按天日志未按夹具写入")

    def test_logs_response_has_no_plaintext_phone(self):
        # level=all：脱敏必须覆盖**每一级**的行。默认档（warn）会收起含号的 INFO 行，
        # 只测默认档就变成"没测到那些行"（覆盖退化），故显式要全量档。
        body = self._admin_client().get("/api/logs?level=all").get_data(as_text=True)
        self.assertNotIn(PHONE, body, "日志响应含 11 位明文手机号")
        self.assertNotIn("+8613800138000", body, "+86 前缀形态未遮")
        self.assertNotIn("138 0013 8000", body, "空格分段形态未遮")
        self.assertNotIn("12012345678", body, "写侧校验收下的号（次位 0–2）未遮")
        self.assertIn(MASKED, body)
        self.assertIn("120****5678", body)

    def test_accounts_response_has_no_plaintext_phone(self):
        body = self._admin_client().get("/api/accounts").get_data(as_text=True)
        self.assertIn(MASKED, body, "账号未按预期装载，断言会退化成空校验")
        self.assertNotIn(PHONE, body, "/api/accounts 列表含 11 位明文手机号")


if __name__ == "__main__":
    unittest.main()
