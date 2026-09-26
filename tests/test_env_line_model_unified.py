# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
r""".env 单一行模型 + 单一校验器 + 写入前后键集合 diff（web 与引擎两侧同源）。

**背景**：`.env` 过去有两套行模型——web 侧读-改-写用**宽** `str.splitlines()`
（实测把 10 个字符当行边界），引擎侧 `write_env_keys` 只认 `\n\r`。差集 8 个字符即潜伏面：
一条注释尾部藏 U+0085、其后紧跟 `YIBAN_GLOBAL_PAUSE=1` 文本时，一次"只改签到模式"的
保存会把后半截**实体化成真配置行**（不过口令门、不进审计，写后 `find_env_key_collisions`
还归零）。反向：引擎写入口对传入 value/key 一字不校验；前端对行分隔符零校验。

**本文件钉住的修复**：
- 一处行模型：`yiban.infra.env_io.split_env_lines`（窄行：`\n` / `\r\n` / `\r`），
  web 服务层不得再出现第二份 `splitlines(`；读取侧对历史文件里的换行族字符**容忍读取**，
  只在写入/校验路径拒绝；
- 一处校验器：键名白名单 + 值禁换行族 + 值长度上限，web 与引擎两侧消费同一函数
  ⇒ 同一入参两侧**同一句**拒绝；
- 写入前后各做一次键集合 diff：任何"本次未请求的键发生变化"⇒ 回滚到写入前内容 + 审计 + 拒绝；
- V2 活体反例：U+0085 潜伏注释不得被实体化成 `YIBAN_GLOBAL_PAUSE=1` /
  `ADMIN_PASSWORD_HASH=pwned`。

**实测 10 分隔符清单的原始出处**：`python -c` 遍历全部 Unicode 码位，
`len(("A"+chr(c)+"B").splitlines()) > 1` 恰好命中 10 个：
`U+000A U+000B U+000C U+000D U+001C U+001D U+001E U+0085 U+2028 U+2029`
（后 8 个是相对 `\n\r` 的差集，即本次新增覆盖面）。

标签：G · 安全：脱敏/审计/配置注入
覆盖：`.env` 行模型单源（web 服务层无第二份 splitlines）、10 分隔符实测清单、含 U+0085 潜伏注释的 V2 实体化反例（GLOBAL_PAUSE 与 ADMIN_PASSWORD_HASH 两变体）、两侧同一句拒绝、写入前后键集合 diff 强制与回滚审计、值长度上限、读取容忍与合法流逐字节回归
对应实现：`yiban/infra/env_io.py` 的 `split_env_lines` / `env_key_values` / `validate_env_key` / `validate_env_value` / `render_env_write` / `write_env_keys` / `EnvWriteRefused`，以及 `web/services/env_io.py` 的 `write_env_batch` / `ensure_secret_key`、`web/app.py:write_env_batch`
关键断言：拒绝必须**同时**断"抛错 + .env 字节不变 + 不实体化出未请求的键 + 有审计记录"；只断抛错会漏掉"先实体化再报错"的半生效；两侧同一句拒绝要断言**消息字符串相等**而不是各自含关键词，否则两份校验器可以各写一句都过
依赖：临时 `.env` + Flask test client + 临时 DB，无网络、无 skip；分隔符按码位逐个枚举
"""
import contextlib
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import unittest

from yiban.infra import env_io

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# str.splitlines() 认定的全部行分隔符（Python 语言层保证；实测恰好 10 个）
ALL_BREAKS = ["\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"]
EXTRA_BREAKS = [c for c in ALL_BREAKS if c not in ("\n", "\r")]      # 差集 8 个

TEST_KEY = "a" * 64
ADMIN_PASS = "MasterPass#2026"
SUB_PASS = "SubAdmin#2026"
SUB_ADMIN = "sub-admin@test.local"


def _load_webapp(tag):
    import db as _db
    spec = importlib.util.spec_from_file_location(
        f"webapp_{tag}", os.path.join(BASE, "web", "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"webapp_{tag}"] = mod
    with contextlib.suppress(Exception):
        spec.loader.exec_module(mod)
    assert hasattr(mod, "write_env_batch"), "web/app.py 加载失败（exec_module 异常被抑制）"
    return _db, mod


def _read_text(path):
    with io.open(path, encoding="utf-8-sig") as f:
        return f.read()


def _read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def _physical_lines(path):
    """磁盘物理行数（按 `\\n` 计）：潜伏分隔符不增行，实体化才 +1。"""
    return _read_bytes(path).count(b"\n")


# ---------------------------------------------------------------------------
# 1. 行模型清单纯代码断言（无需 webapp）
# ---------------------------------------------------------------------------
class SeparatorLineModelTest(unittest.TestCase):
    """行分隔符清单必须与 `str.splitlines()` 的真行为逐字符一致。"""

    def test_splitlines_boundary_set_is_exactly_ten(self):
        derived = [chr(c) for c in range(0x110000)
                   if len(("A" + chr(c) + "B").splitlines()) > 1]
        self.assertEqual(len(derived), 10, f"实测分隔符数应为 10，得到 {derived!r}")
        self.assertEqual(set(derived), set(ALL_BREAKS),
                         "本文件写死的清单与 str.splitlines() 实测不一致")
        self.assertEqual(env_io.ENV_LINE_BREAK_CHARS, frozenset(ALL_BREAKS),
                         "实现常量的字符集与实测清单不同步（漏一个字符 = 留一条注入链）")

    def test_extra_breaks_are_the_eight_beyond_crlf(self):
        self.assertEqual(len(EXTRA_BREAKS), 8)
        self.assertEqual(
            set(hex(ord(c)) for c in EXTRA_BREAKS),
            {"0xb", "0xc", "0x1c", "0x1d", "0x1e", "0x85", "0x2028", "0x2029"})

    def test_line_model_predicate_matches_splitlines(self):
        for ch in ALL_BREAKS:
            with self.subTest(ch=hex(ord(ch))):
                self.assertTrue(env_io.has_line_break(f"A{ch}B"))


class SingleLineModelSourceTest(unittest.TestCase):
    """web 服务层不得再持有第二份 .env 行模型（读写都收敛到 yiban.infra.env_io）。"""

    def test_engine_exposes_the_canonical_line_model(self):
        for name in ("split_env_lines", "env_key_values", "validate_env_key",
                     "validate_env_value", "render_env_write", "EnvWriteRefused"):
            self.assertTrue(hasattr(env_io, name),
                            f"yiban.infra.env_io 缺少单一行模型/校验器的公开面：{name}")

    def test_web_env_service_has_no_second_splitlines(self):
        src = _read_text(os.path.join(BASE, "web", "services", "env_io.py"))
        self.assertNotIn(
            "splitlines(", src,
            "web/services/env_io.py 又出现 splitlines() —— .env 的读写行模型只允许"
            "yiban.infra.env_io 一份（宽模型会把潜伏分隔符实体化成新配置行）")


# ---------------------------------------------------------------------------
# 2. web 侧 V2 活体反例：潜伏注释不得被一次无关保存实体化
# ---------------------------------------------------------------------------
class _WebBase(unittest.TestCase):
    """临时 .env/DB + webapp；每例从带合法密钥/盐的基线起步。"""

    PRISTINE = (
        f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
        "YIBAN_SECRET_KEY=" + "b" * 64 + "\n"
        "YIBAN_TRACK_SALT=" + "c" * 64 + "\n"
        "YIBAN_ADMIN_USER=admin@test.local\n"
        "YIBAN_ADMIN_PASSWORD_HASH=scrypt:frozen-hash-not-used-in-login\n"
        "YIBAN_SIGN_ORDER=sequence\n"
    )

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-envmodel-")
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls.users_file = os.path.join(cls.tmp, "users.json")
        for k, v in (("YIBAN_ACCOUNTS_KEY", TEST_KEY), ("YIBAN_ENV_FILE", cls.env_file),
                     ("YIBAN_ACCOUNTS_FILE", cls.accounts_file),
                     ("YIBAN_USERS_FILE", cls.users_file),
                     ("YIBAN_DB_FILE", cls.db_file), ("YIBAN_STATE_DIR", cls.tmp),
                     ("YIBAN_LOG_FILE", os.path.join(cls.tmp, "sign.log"))):
            os.environ[k] = v
        cls.db, cls.webapp = _load_webapp(cls.__name__)

    @classmethod
    def tearDownClass(cls):
        if cls.db._conn is not None:
            with contextlib.suppress(Exception):
                cls.db._conn.close()
            cls.db._conn = None
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE",
                  "YIBAN_USERS_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR",
                  "YIBAN_LOG_FILE"):
            os.environ.pop(k, None)

    def setUp(self):
        if self.db._conn is not None:
            with contextlib.suppress(Exception):
                self.db._conn.close()
            self.db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write(self.PRISTINE)
        self.webapp.ENV_FILE = self.env_file
        self.db.init_db(self.db_file, migrate_from=self.accounts_file,
                        env_file=self.env_file)
        # 预热审计链：把审计密钥（与盐）先落进 env_file，否则拒绝路径里的
        # db.audit 会去写 env_file，而那时文件已含潜伏分隔符 → 审计写被拒、无留痕
        self.db.audit("setup", "warmup", "", "")
        self.base_env = _read_text(self.env_file)
        conn = self.db.get_conn()
        conn.execute("DELETE FROM audit_logs")
        conn.commit()

    # ---- 辅助 ----
    def _write_fixture(self, text):
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write(text)

    def _inject_after_first_line(self, injected):
        """在基线第二行前插入一行（保持首行密钥行在前，贴近真实 .env）。"""
        lines = self.base_env.split("\n")
        lines.insert(1, injected)
        self._write_fixture("\n".join(lines))

    def _audit_actions(self):
        conn = self.db.get_conn()
        return [r[0] for r in conn.execute("SELECT action FROM audit_logs")]

    def _audit_details(self):
        conn = self.db.get_conn()
        return [r[0] for r in conn.execute("SELECT detail FROM audit_logs")]

    def _sub_admin_client(self):
        self.db.create_user(SUB_ADMIN, self.webapp.generate_password_hash(SUB_PASS),
                            role="admin")
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": SUB_ADMIN, "password": SUB_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c, {"X-CSRF-Token": c.get("/api/me").get_json()["csrf_token"]}


class V2LatentCommentTest(_WebBase):
    """含 U+0085 潜伏注释的 .env + 一次无关保存 ⇒ 拒绝、字节不变、审计留痕。"""

    def _attack(self, injected_key_line, expect_key):
        marker = "materialized-" + expect_key
        # 注释尾部潜伏 U+0085，其后是攻击者希望被实体化的配置文本
        self._inject_after_first_line(
            f"# 例行备注\u0085{expect_key}={marker}")
        before_bytes = _read_bytes(self.env_file)
        before_lines = _physical_lines(self.env_file)
        before_parsed = dict(self.webapp.read_env(self.env_file))
        self.assertNotEqual(before_parsed.get(expect_key), marker,
                            "前置条件：潜伏载荷在窄模型下还不是该键的生效值")

        with self.assertRaises(ValueError):
            # "只改签到模式"的例行保存——攻击的实体化触发点
            self.webapp.write_env_batch(self.env_file, {"YIBAN_SIGN_ORDER": "random"})

        self.assertEqual(_read_bytes(self.env_file), before_bytes,
                         "拒绝写入时 .env 必须一个字节都不改")
        self.assertEqual(_physical_lines(self.env_file), before_lines,
                         "物理行数 +1 = 潜伏分隔符被实体化")
        after_parsed = self.webapp.read_env(self.env_file)
        self.assertNotEqual(after_parsed.get(expect_key), marker,
                            f"{expect_key} 被实体化成攻击者的值 = V2 攻击成功")
        self.assertEqual(after_parsed.get("YIBAN_SIGN_ORDER"), "sequence",
                         "被拒绝的保存不得改动任何配置")
        self.assertIn("env_write_refused", self._audit_actions(),
                      "写入拒绝必须进审计（不留痕的拒绝等于没发生）")
        for detail in self._audit_details():
            self.assertNotIn(marker, detail, "审计明细不得回带载荷原文")

    def test_u0085_latent_comment_cannot_materialize_global_pause(self):
        self._attack("YIBAN_GLOBAL_PAUSE", "YIBAN_GLOBAL_PAUSE")

    def test_u0085_latent_comment_cannot_materialize_admin_hash(self):
        self._attack("YIBAN_ADMIN_PASSWORD_HASH", "YIBAN_ADMIN_PASSWORD_HASH")

    def test_route_level_unrelated_save_is_refused(self):
        """路由级同型：一次"只改签到模式"的 POST /api/settings 也必须被拒。"""
        self._inject_after_first_line("# 例行备注\u0085YIBAN_GLOBAL_PAUSE=1")
        before_bytes = _read_bytes(self.env_file)
        c, h = self._sub_admin_client()
        r = c.post("/api/settings", json={"sign_order": "random"}, headers=h)
        self.assertNotEqual(r.status_code, 200,
                            f"被污染的 .env 上写入必须失败，实际 {r.status_code}")
        self.assertEqual(_read_bytes(self.env_file), before_bytes,
                         "被拒绝的设置保存不得改动 .env")
        self.assertNotIn("YIBAN_GLOBAL_PAUSE", self.webapp.read_env(self.env_file),
                         "急停键被静默实体化")
        self.assertIn("env_write_refused", self._audit_actions())


class BothSidesSameSentenceTest(_WebBase):
    """同一含宽分隔符入参：web 写入口与引擎 write_env_keys 被**同一句**拒绝。"""

    def test_each_extra_break_refused_with_identical_message(self):
        for ch in EXTRA_BREAKS:
            with self.subTest(ch=hex(ord(ch))):
                self._write_fixture(self.PRISTINE)
                payload = f"x{ch}y"
                with self.assertRaises(ValueError) as cw:
                    self.webapp.write_env_batch(self.env_file,
                                                {"YIBAN_SIGN_ORDER": payload})
                with self.assertRaises(ValueError) as ce:
                    env_io.write_env_keys(self.env_file,
                                          {"YIBAN_SIGN_ORDER": payload})
                self.assertEqual(str(cw.exception), str(ce.exception),
                                 "两侧拒绝文案必须同源（同一校验器同一句）")
                self.assertIn("YIBAN_SIGN_ORDER", str(cw.exception))


# ---------------------------------------------------------------------------
# 3. 引擎侧：传入值/键校验 + 值长度上限
# ---------------------------------------------------------------------------
class _EngineBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-envmodel-eng-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env_file = os.path.join(self.tmp, ".env")

    def _write(self, text):
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write(text)

    def _bytes(self):
        return _read_bytes(self.env_file)


class EngineIncomingValidationTest(_EngineBase):
    """引擎写入口对传入 value/key 一格都不放过（旧实现一字不校验）。"""

    def test_every_break_in_value_is_refused_and_disk_untouched(self):
        for ch in ALL_BREAKS:
            with self.subTest(ch=hex(ord(ch))):
                self._write("YIBAN_OTHER=1\n")
                before = self._bytes()
                with self.assertRaises(ValueError):
                    env_io.write_env_keys(self.env_file,
                                          {"YIBAN_SECRET_KEY": f"a{ch}b"})
                self.assertEqual(self._bytes(), before, "拒绝后磁盘不得改动")

    def test_illegal_key_is_refused(self):
        self._write("YIBAN_OTHER=1\n")
        before = self._bytes()
        for bad in ("yiban_x", "YIBAN X", "1YIBAN", "#YIBAN", "YIBAN-X"):
            with self.subTest(key=bad), self.assertRaises(ValueError):
                env_io.write_env_keys(self.env_file, {bad: "1"})
        self.assertEqual(self._bytes(), before)

    def test_value_length_cap(self):
        self._write("YIBAN_OTHER=1\n")
        before = self._bytes()
        with self.assertRaises(ValueError):
            env_io.write_env_keys(self.env_file,
                                  {"YIBAN_BIG": "x" * (env_io.ENV_VALUE_MAX_LEN + 1)})
        self.assertEqual(self._bytes(), before)
        # 上限之内仍可写（不得把正常长值顺手拒掉）
        env_io.write_env_keys(self.env_file,
                              {"YIBAN_BIG": "x" * env_io.ENV_VALUE_MAX_LEN})
        self.assertEqual(env_io.parse_env_file(self.env_file)["YIBAN_BIG"],
                         "x" * env_io.ENV_VALUE_MAX_LEN)


# ---------------------------------------------------------------------------
# 4. 键集合 diff 强制：写入器把未请求的键改了 ⇒ 回滚 + 审计 + 拒绝
# ---------------------------------------------------------------------------
class KeySetDiffTest(_EngineBase):
    """写入前后各做一次键集合 diff；未请求的键变化一律 fail-closed。"""

    def test_writer_added_unrequested_key_rolls_back_and_audits(self):
        self._write("YIBAN_SIGN_ORDER=sequence\nYIBAN_KEEP=1\n")
        before = self._bytes()
        state = {"n": 0}

        def corrupting_writer(path, text):
            # 第一次提交模拟"写入器 bug"：夹带一条本次未请求的急停键；
            # 回滚再次调用时按传入原文落盘
            state["n"] += 1
            extra = "YIBAN_GLOBAL_PAUSE=1\n" if state["n"] == 1 else ""
            with io.open(path, "w", encoding="utf-8") as f:
                f.write(text + extra)

        audits = []
        with self.assertRaises(env_io.EnvWriteRefused):
            env_io.write_env_keys(self.env_file, {"YIBAN_SIGN_ORDER": "random"},
                                  write_text=corrupting_writer,
                                  audit=lambda code, detail: audits.append((code, detail)))
        self.assertEqual(self._bytes(), before,
                         "检出未请求键变化后必须回滚到写入前内容（字节级）")
        self.assertNotIn("YIBAN_GLOBAL_PAUSE", env_io.parse_env_file(self.env_file))
        self.assertTrue(audits, "键集合 diff 失败必须进审计")
        self.assertTrue(any("requested" in code for code, _ in audits),
                        f"审计 code 应指明未请求键变化，实际 {audits!r}")

    def test_requested_change_passes_without_false_positive(self):
        self._write("YIBAN_SIGN_ORDER=sequence\nYIBAN_KEEP=1\n")
        audits = []
        env_io.write_env_keys(self.env_file, {"YIBAN_SIGN_ORDER": "random"},
                              audit=lambda code, detail: audits.append((code, detail)))
        parsed = env_io.parse_env_file(self.env_file)
        self.assertEqual(parsed["YIBAN_SIGN_ORDER"], "random")
        self.assertEqual(parsed["YIBAN_KEEP"], "1")
        self.assertEqual(audits, [], "合法写入不得触发拒绝审计")

    def test_requested_delete_passes(self):
        self._write("YIBAN_SIGN_ORDER=sequence\nYIBAN_KEEP=1\n")
        # web 侧 delete_empty=True；引擎侧保持追加语义，两者都不得误报
        env_io.write_env_keys(self.env_file, {"YIBAN_KEEP": ""}, delete_empty=True)
        self.assertNotIn("YIBAN_KEEP", env_io.parse_env_file(self.env_file))


# ---------------------------------------------------------------------------
# 5. 读取容忍 + 合法流逐字节回归
# ---------------------------------------------------------------------------
class ReadToleranceAndLegalFlowTest(_WebBase):
    """读取侧容忍历史脏文件（否则现网文件读不了即全站瘫）；合法写只动目标行。"""

    def test_read_tolerates_existing_wide_separators(self):
        self._inject_after_first_line("# 备注\u0085YIBAN_GLOBAL_PAUSE=1")
        # 读取侧不得抛（容忍边界：只在写入/校验路径拒绝）
        parsed = self.webapp.read_env(self.env_file)
        self.assertEqual(parsed.get("YIBAN_SIGN_ORDER"), "sequence")
        self.assertEqual(env_io.parse_env_file(self.env_file).get("YIBAN_ADMIN_USER"),
                         "admin@test.local")

    def test_legal_change_touches_only_target_line(self):
        before = self.base_env
        self.webapp.write_env_batch(self.env_file, {"YIBAN_SIGN_ORDER": "random"})
        expected = before.replace("YIBAN_SIGN_ORDER=sequence",
                                  "YIBAN_SIGN_ORDER=random")
        self.assertEqual(_read_text(self.env_file), expected,
                         "合法保存只应改动目标行（逐字节回归）")

    def test_legal_add_and_delete(self):
        self.webapp.write_env_batch(self.env_file, {"YIBAN_NEW": "v1"})
        self.assertEqual(self.webapp.read_env(self.env_file)["YIBAN_NEW"], "v1")
        self.webapp.write_env_batch(self.env_file, {"YIBAN_NEW": ""})
        self.assertNotIn("YIBAN_NEW", self.webapp.read_env(self.env_file))
        # 其他键一个不动
        self.assertEqual(self.webapp.read_env(self.env_file)["YIBAN_SIGN_ORDER"],
                         "sequence")


if __name__ == "__main__":
    unittest.main()
