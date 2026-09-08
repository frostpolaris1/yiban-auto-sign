# -*- coding: utf-8 -*-
r""".env 行分隔符注入提权回归（2026-09-07 安全审查，CRITICAL，活体复现）。

根因一句话：**校验用的行模型与写入用的行模型不是同一个**。
write_env_batch 的注入校验只挡 `\n` / `\r`，而它自己读文件用的是
`str.splitlines()`——该 API 额外把 `\v \f \x1c \x1d \x1e \x85 \u2028 \u2029`
也当行分隔符。于是含这些字符的值能过校验，作为**潜伏分隔符**留在同一物理行里；
下一次任何代码读-改-写 .env（`splitlines()` + `"\n".join()`）就把它**实体化**成
两行真配置。env_io.parse_env_file 按文件顺序建 dict、后写覆盖先写（last-wins），
实体化出来的行排在真哈希之后 → 直接生效。

活体复现的杀链（普通管理员即可发动，无需任何主管理员凭据）：
  1) PUT /api/announcement  text = "notify" + U+2028 + "YIBAN_ADMIN_PASSWORD_HASH=<攻击者哈希>"
     （公告是自由文本，路由自己的校验同样只挡 \n / \r）
  2) POST /api/settings     {"sunday_sign": false}   ← 任意管理员都做的日常自服务写入，
     本次写入把潜伏行实体化到真哈希之后
  3) 攻击者用自己的口令登录内置主管理员；合法主管理员 401

本文件钉住的修复（三处，缺一即链未死）：
- 单一来源的 `_has_line_break`：与 `str.splitlines()` 同字符集，
  write_env_batch 与公告路由共用同一个"会不会注入出一行配置"的判据；
- 更新键的旧行折叠认得 `KEY = value` 写法（键两侧可有空白、`=` 前可有空白），
  且不误伤更长的同前缀键（YIBAN_MAX_USERS vs YIBAN_MAX_USERS_EXTRA）；
- verify_admin 对"非恰好一行 YIBAN_ADMIN_PASSWORD_HASH（多行或统计读取失败）"
  fail-closed：主凭据歧义要么是配错、要么就是上面的注入，两种都不能继续当作
  认证成功。

全程 mock / 纯本地（Flask test client + 临时 .env/DB），无任何网络请求。
用法（项目根目录，勿设 PYTHONIOENCODING）：
    py -m pytest tests/test_env_line_break_injection.py -v
"""
import contextlib
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64
ADMIN_PASS = "MasterPass#2026"       # 主管理员：12 位四类，满足口令策略
SUB_PASS = "SubAdmin#2026"           # 注册（普通）管理员口令
ATTACK_PASS = "Attacker#2026"        # 攻击者想要的"新主管理员口令"

# str.splitlines() 认定的全部行分隔符（Python 语言层保证，非项目自定集合）。
ALL_BREAKS = ["\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"]
# 旧校验（只挡 \n / \r）漏掉的 8 个——本次修复的新增覆盖面，逐个都要有负例。
EXTRA_BREAKS = [c for c in ALL_BREAKS if c not in ("\n", "\r")]

SUB_ADMIN = "sub-admin@test.local"


def _load_webapp(tag):
    import db as _db
    spec = importlib.util.spec_from_file_location(
        f"webapp_{tag}", os.path.join(BASE, "web", "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"webapp_{tag}"] = mod
    with contextlib.suppress(Exception):
        spec.loader.exec_module(mod)
    # 加载失败不能静默滑过去：届时报的是后文的 AttributeError，与真因隔了十万八千里
    assert hasattr(mod, "write_env_batch"), "web/app.py 加载失败（exec_module 异常被抑制）"
    return _db, mod


def _read(path):
    with io.open(path, encoding="utf-8-sig") as f:
        return f.read()


def _physical_lines(path):
    """.env 的**物理行数**（按磁盘上的 \\n 计），独立于任何 Python 行模型。

    断言"物理行数不变"才是注入未被实体化的硬证据：潜伏分隔符不增加物理行，
    一旦某次读-改-写把它变成真行，本数字就 +1。
    """
    with open(path, "rb") as f:
        return f.read().count(b"\n")


def _wide_lines(path):
    """按写入侧的宽行模型（str.splitlines()，比文件迭代多认 8 个分隔符）取出行列表。"""
    return _read(path).splitlines()


class _Base(unittest.TestCase):
    """共享脚手架：临时 .env/DB + webapp 加载 + 主管理员/普通管理员会话。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-env-inj-")
        # tearDownClass 在 setUpClass 抛错时不会执行：清理挂 addClassCleanup 才不漏
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        cls.env_file = os.path.join(cls.tmp, ".env")
        cls._pristine_env = (
            f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
            "YIBAN_ADMIN_USER=admin@test.local\n"
            f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
        )
        with io.open(cls.env_file, "w", encoding="utf-8") as f:
            f.write(cls._pristine_env)
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
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE",
                  "YIBAN_USERS_FILE", "YIBAN_DB_FILE", "YIBAN_STATE_DIR",
                  "YIBAN_LOG_FILE"):
            os.environ.pop(k, None)

    def setUp(self):
        # 每例从干净的 .env 起步：本文件的断言大量依赖"某键只有 1 行"，
        # 上一例留下的注入行/重复行会让结论失真。
        if self.db._conn is not None:
            with contextlib.suppress(Exception):
                self.db._conn.close()
            self.db._conn = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write(self._pristine_env)
        self.webapp.ENV_FILE = self.env_file
        self.webapp._announcement_cache[0] = None
        self.db.init_db(self.db_file, migrate_from=self.accounts_file,
                        env_file=self.env_file)

    # ---- 会话辅助 ----
    def _login(self, c, username, password):
        r = c.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c.get("/api/me").get_json()["csrf_token"]

    def _csrf(self, token):
        return {"X-CSRF-Token": token}

    def _sub_admin(self):
        """注册管理员（普通管理员）会话：主管理员凭据对它不可见，正是攻击者视角。"""
        self.db.create_user(SUB_ADMIN, self.webapp.generate_password_hash(SUB_PASS),
                            role="admin")
        c = self.webapp.create_app().test_client()
        return c, self._csrf(self._login(c, SUB_ADMIN, SUB_PASS))

    def _master_hash_line(self):
        """当前 .env 生效的主管理员哈希（解析器口径，非物理行口径）。"""
        return self.webapp.read_env(self.env_file).get("YIBAN_ADMIN_PASSWORD_HASH", "")

    def _write_env(self, *lines):
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# 1. write_env_batch / 谓词：与 splitlines() 同字符集
# ---------------------------------------------------------------------------
class LineBreakPredicateTest(_Base):
    """`_has_line_break` 必须恰好等于"`str.splitlines()` 会把这段文本拆开"。"""

    def test_predicate_exists_and_rejects_all_breaks(self):
        self.assertTrue(
            callable(getattr(self.webapp, "_has_line_break", None)),
            "缺少模块级谓词 _has_line_break（校验与写入口径须单源）")
        for ch in ALL_BREAKS:
            with self.subTest(ch=hex(ord(ch))):
                self.assertTrue(
                    self.webapp._has_line_break(f"A{ch}B"),
                    f"U+{ord(ch):04X} 是 splitlines 的行分隔符，谓词必须判 True")
                self.assertTrue(self.webapp._has_line_break(ch), "单独一个分隔符也要判 True")

    def test_predicate_matches_splitlines_over_whole_charspace(self):
        """谓词与 splitlines 逐字符等价（抽样覆盖：其他空白/控制符不得误伤）。

        被误判为行分隔符的合法字符会拒掉正常配置（把功能改坏），
        漏判的字符就是这次的漏洞本身——两侧必须一模一样。
        """
        # 其他空白/控制/格式字符：不是 splitlines 的分隔符，谓词若误伤就会拒掉正常配置
        probe = ["\t", " ", "\x00", "\x01", "\x07", "\x08", "\xa0", "\u1680",
                 "\u2000", "\u200b", "\u202f", "\u205f", "\u3000", "\ufeff",
                 "\uff00", "\U0001f600", "=", "#", '"', "'", "$", "&", "|"]
        probe += [chr(c) for c in range(0x00, 0x0600)]
        probe += ["\u2028", "\u2029"]
        for ch in probe:
            expect = len(("A" + ch + "B").splitlines()) > 1
            self.assertEqual(
                self.webapp._has_line_break("A" + ch + "B"), expect,
                f"U+{ord(ch):04X}：谓词判据与 splitlines 不一致（应为 {expect}）")

    def test_constant_is_frozenset_of_the_documented_chars(self):
        """.env 写入侧用 splitlines()，常量字符集必须与它同步（漏一个字符=留一条链）。"""
        const = getattr(self.webapp, "_ENV_LINE_BREAK_CHARS", None)
        self.assertIsNotNone(const, "缺少 _ENV_LINE_BREAK_CHARS 常量")
        self.assertIsInstance(const, frozenset)
        self.assertEqual(
            const, frozenset(ALL_BREAKS),
            "_ENV_LINE_BREAK_CHARS 与 str.splitlines() 的行分隔符集不同步："
            "写入侧用 splitlines() 拆行，校验侧漏一个字符就留一条注入链")


class WriteEnvBatchInjectionTest(_Base):
    """兜底硬校验：值/键含任意行分隔符都必须 ValueError 且零写盘。"""

    def _poisoned_env(self):
        self._write_env(
            f"YIBAN_ACCOUNTS_KEY={TEST_KEY}",
            "YIBAN_ADMIN_USER=admin@test.local",
            "YIBAN_ADMIN_PASSWORD_HASH=scrypt:real-master-hash",
        )

    def test_rejects_every_break_in_value(self):
        for ch in ALL_BREAKS:
            with self.subTest(ch=hex(ord(ch))):
                self._poisoned_env()
                before = _read(self.env_file)
                payload = f"notify{ch}YIBAN_ADMIN_PASSWORD_HASH=scrypt:attacker"
                with self.assertRaises(ValueError) as cm:
                    self.webapp.write_env_batch(self.env_file, {"YIBAN_ANNOUNCEMENT": payload})
                self.assertIn("YIBAN_ANNOUNCEMENT", str(cm.exception))
                self.assertEqual(_read(self.env_file), before,
                                 "拒绝就必须零写盘（不能留下半成品配置）")
                self.assertEqual(_physical_lines(self.env_file), 3)

    def test_rejects_every_break_in_key(self):
        for ch in ALL_BREAKS:
            with self.subTest(ch=hex(ord(ch))):
                self._poisoned_env()
                before = _read(self.env_file)
                with self.assertRaises(ValueError):
                    self.webapp.write_env_batch(
                        self.env_file, {f"YIBAN_X{ch}Y": "1"})
                self.assertEqual(_read(self.env_file), before)

    def test_ordinary_values_still_write(self):
        """不得为安全把 .env 写成只能填 ASCII 单字——空格/制表/中文/URL 都要能存。"""
        self._poisoned_env()
        val = "维护通知 https://example.com/a?b=1#c\t尾注 空格"
        self.webapp.write_env_batch(self.env_file, {"YIBAN_ANNOUNCEMENT": val})
        self.assertEqual(self.webapp.read_env(self.env_file)["YIBAN_ANNOUNCEMENT"], val)
        self.assertEqual(_physical_lines(self.env_file), 4)

    def test_empty_value_still_deletes_key(self):
        """.env 删除语义（空值=删行）不得被新校验改掉。"""
        self._poisoned_env()
        self.webapp.write_env_batch(self.env_file, {"YIBAN_ANNOUNCEMENT": "abc"})
        self.webapp.write_env_batch(self.env_file, {"YIBAN_ANNOUNCEMENT": ""})
        self.assertNotIn("YIBAN_ANNOUNCEMENT", _read(self.env_file))


# ---------------------------------------------------------------------------
# 2. 杀链端到端：注入 → 例行的自服务写入 → 提权
# ---------------------------------------------------------------------------
class EnvInjectionKillChainTest(_Base):
    """活体复现链必须整条死掉（普通管理员发起）。"""

    def _attacker_hash(self):
        return self.webapp.generate_password_hash(
            ATTACK_PASS, method=self.webapp.SCRYPT_METHOD)

    def test_announcement_then_settings_write_cannot_take_over_master(self):
        # create_app() 会执行明文→哈希迁移：先建立"合法主管理员"基线
        c, h = self._sub_admin()
        master_hash = self._master_hash_line()
        self.assertTrue(master_hash.startswith("scrypt:"),
                        "前置条件失败：主管理员哈希应已由启动迁移落盘")
        lines_before = _physical_lines(self.env_file)
        atk_hash = self._attacker_hash()   # 每次生成加随机盐：须固定下来才能回查是否落盘
        payload = f"notify\u2028YIBAN_ADMIN_PASSWORD_HASH={atk_hash}"
        self.assertLessEqual(len(payload), 200, "payload 须在公告 200 字上限内")

        # 步骤 1：埋潜伏分隔符
        r = c.put("/api/announcement", json={"text": payload}, headers=h)
        self.assertEqual(r.status_code, 400,
                         f"含 U+2028 的公告必须 400（当前 {r.status_code}："
                         f"{r.get_data(as_text=True)}）")
        self.assertIn("行分隔符", r.get_json()["error"],
                      "400 必须来自行分隔符守卫而非其他校验")
        self.assertEqual(_physical_lines(self.env_file), lines_before,
                         "被拒绝的请求不得改动 .env 物理行数")
        # 步骤 2：任意管理员都会做的例行自服务写入——修复前它负责"实体化"
        r = c.post("/api/settings", json={"sunday_sign": False}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        # 断言一：主管理员哈希原样未动（既没被追加、也没被覆盖）
        self.assertEqual(self._master_hash_line(), master_hash,
                         "主管理员哈希被注入行覆盖 = 提权成功")
        self.assertEqual(_physical_lines(self.env_file), lines_before,
                         "潜伏分隔符被某次读-改-写实体化成了新的物理配置行")
        self.assertNotIn(atk_hash, _read(self.env_file), "攻击者的哈希不得出现在 .env 任何位置")
        # 断言二：攻击者口令登不上内置主管理员，合法主管理员仍可登录
        ca = self.webapp.create_app().test_client()
        r = ca.post("/api/login", json={"username": "admin@test.local", "password": ATTACK_PASS})
        self.assertEqual(r.status_code, 401, "攻击者口令不得登录主管理员")
        cm = self.webapp.create_app().test_client()
        r = cm.post("/api/login", json={"username": "admin@test.local", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, f"合法主管理员应仍可登录：{r.status_code}")

    def test_route_rejects_every_extra_break(self):
        """路由级守卫与 write_env_batch 同字符集：8 个新增分隔符逐个 400。"""
        c, h = self._sub_admin()
        base_lines = _physical_lines(self.env_file)
        base_hash = self._master_hash_line()
        atk = self._attacker_hash()
        for ch in EXTRA_BREAKS:
            with self.subTest(ch=hex(ord(ch))):
                r = c.put("/api/announcement",
                          json={"text": f"x{ch}YIBAN_ADMIN_PASSWORD_HASH={atk}"}, headers=h)
                self.assertEqual(r.status_code, 400,
                                 f"U+{ord(ch):04X} 未被路由守卫拦下（{r.status_code}）")
                # 状态码不够锋利：载荷也在 200 字上限内，若哈希串变长，400 会来自
                # 长度校验而守卫漏了也照常绿。必须点名守卫、且不是"过长"
                err = r.get_json()["error"]
                self.assertIn("行分隔符", err,
                              f"400 须来自行分隔符守卫而非长度上限（U+{ord(ch):04X}）：{err}")
                self.assertNotIn("过长", err, f"400 来自长度校验 = 守卫没拦住 U+{ord(ch):04X}")
                self.assertEqual(self._master_hash_line(), base_hash)
                self.assertEqual(_physical_lines(self.env_file), base_lines)
        # 拦下的所有尝试之后，再走一次例行写入也不得冒出注入行
        r = c.post("/api/settings", json={"sunday_sign": False}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(_physical_lines(self.env_file), base_lines)
        self.assertEqual(self._master_hash_line(), base_hash)

    def test_legitimate_announcement_and_settings_flows_still_work(self):
        """正常单行公告可存可读、正常设置保存可持久化（安全加固不得顺手做坏事）。"""
        c, h = self._sub_admin()
        r = c.put("/api/announcement", json={"text": "服务器今晚 23:00 维护"}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.webapp.read_env(self.env_file)["YIBAN_ANNOUNCEMENT"],
                         "服务器今晚 23:00 维护")
        anon = self.webapp.create_app().test_client()   # 公告 GET 公开
        self.assertEqual(anon.get("/api/announcement").get_json()["text"],
                         "服务器今晚 23:00 维护")
        r = c.post("/api/settings", json={"sunday_sign": True, "account_verify": True},
                   headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        env = self.webapp.read_env(self.env_file)
        self.assertEqual(env["YIBAN_SUNDAY_SIGN"], "1")
        self.assertEqual(env["YIBAN_ACCOUNT_VERIFY"], "1")
        got = c.get("/api/settings", headers=h).get_json()
        self.assertEqual(got["sunday_sign"], 1)
        # 清空公告（空值 = 删键）仍是常规能力
        r = c.put("/api/announcement", json={"text": ""}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertNotIn("YIBAN_ANNOUNCEMENT", _read(self.env_file))


# ---------------------------------------------------------------------------
# 3. 重复键：折叠要认得 `KEY = value` 写法，且不误伤同前缀长键
# ---------------------------------------------------------------------------
class EnvDuplicateKeyFoldingTest(_Base):
    """`KEY = value`（= 号前有空白）同样是配置行——折叠漏掉它就会积累出覆盖行。"""

    def test_padded_key_line_is_replaced_not_shadowed(self):
        self._write_env(
            "# 注释里的 YIBAN_MAX_USERS=99 不算配置行",
            "YIBAN_MAX_USERS  =  3",
            "YIBAN_KEEP=1",
        )
        self.webapp.write_env_batch(self.env_file, {"YIBAN_MAX_USERS": "5"})
        hits = [ln for ln in _wide_lines(self.env_file)
                if ln.strip().split("=", 1)[0].strip() == "YIBAN_MAX_USERS"]
        self.assertEqual(len(hits), 1, f"旧行未被折叠，留下重复行: {hits}")
        self.assertEqual(hits[0], "YIBAN_MAX_USERS=5")
        env = self.webapp.read_env(self.env_file)
        self.assertEqual(env["YIBAN_MAX_USERS"], "5")
        self.assertEqual(env["YIBAN_KEEP"], "1", "折叠不得丢掉无关配置")

    def test_similar_longer_key_is_not_folded(self):
        self._write_env("YIBAN_MAX_USERS=3", "YIBAN_MAX_USERS_EXTRA=9")
        self.webapp.write_env_batch(self.env_file, {"YIBAN_MAX_USERS": "5"})
        env = self.webapp.read_env(self.env_file)
        self.assertEqual(env["YIBAN_MAX_USERS"], "5")
        self.assertEqual(env["YIBAN_MAX_USERS_EXTRA"], "9",
                         "同前缀的更长键是另一个键，不得被折叠掉")

    def test_duplicate_admin_hash_lines_all_collapse_to_one(self):
        """主管理员哈希多次改密不得积累出多行（歧义行是 fail-closed 的触发源）。"""
        self._write_env("YIBAN_ADMIN_USER=admin@test.local",
                        "YIBAN_ADMIN_PASSWORD_HASH = scrypt:old",
                        "YIBAN_SECRET_KEY=x")
        self.webapp.write_env_batch(
            self.env_file, {"YIBAN_ADMIN_PASSWORD_HASH": "scrypt:new"})
        hits = [ln for ln in _wide_lines(self.env_file)
                if ln.strip().split("=", 1)[0].strip() == "YIBAN_ADMIN_PASSWORD_HASH"]
        self.assertEqual(len(hits), 1, f"改密后仍留下多行主凭据: {hits}")


# ---------------------------------------------------------------------------
# 4. 主凭据歧义：verify_admin fail-closed
# ---------------------------------------------------------------------------
class AmbiguousAdminHashTest(_Base):
    """.env 里该键非恰好一行（多行或统计读取失败）→ 拒绝认证 + ERROR 日志 + 紧急告警。

    用例的"锋利度"要求：歧义态下解析器 last-wins 生效的那一行必须是**合法**哈希——
    否则修复前 verify_admin 也会因口令不匹配而返回 False，断言就成了白过。
    """

    def _hash_of(self, password):
        return self.webapp.generate_password_hash(password, method=self.webapp.SCRYPT_METHOD)

    def _two_hash_lines(self, legal_last=True):
        """写两行主凭据（legal_last=True 时后写生效的正是合法行）。返回生效行哈希。"""
        good, other = self._hash_of(ADMIN_PASS), self._hash_of(ATTACK_PASS)
        first, last = (other, good) if legal_last else (good, other)
        self._write_env(
            f"YIBAN_ACCOUNTS_KEY={TEST_KEY}",
            "YIBAN_ADMIN_USER=admin@test.local",
            f"YIBAN_ADMIN_PASSWORD_HASH={first}",
            "YIBAN_SECRET_KEY=x",
            f"YIBAN_ADMIN_PASSWORD_HASH={last}",   # 影子/注入行（last-wins → 生效的是它）
        )
        return last

    def test_verify_admin_refuses_even_for_first_hash_password(self):
        effective = self._two_hash_lines(legal_last=True)
        self.assertEqual(self._master_hash_line(), effective,
                         "前置条件失败：生效行应是合法哈希（修复前本例应能登录成功）")
        self.assertTrue(self.webapp.check_password_hash(effective, ADMIN_PASS),
                        "前置条件失败：口令与生效哈希本身要匹配")
        with mock.patch.object(self.webapp, "send_notification") as sn, \
                self.assertLogs("web", level="ERROR") as logs:
            self.assertFalse(
                self.webapp.verify_admin("admin@test.local", ADMIN_PASS),
                "主凭据歧义时不得判定认证成功（生效行是谁由写入顺序决定，不可信）")
        self.assertTrue(any("YIBAN_ADMIN_PASSWORD_HASH" in m for m in logs.output),
                        f"ERROR 日志应点名该键，实际: {logs.output}")
        self.assertTrue(sn.called, "主凭据歧义必须发紧急告警")
        self.assertTrue(sn.call_args.kwargs.get("urgent"),
                        f"告警须为紧急，实际: {sn.call_args}")

    def test_verify_admin_refuses_injected_last_wins_hash(self):
        """注入行排在后面（提权成功态）时同样必须拒绝——两条口令都不给过。"""
        effective = self._two_hash_lines(legal_last=False)
        self.assertEqual(self._master_hash_line(), effective)
        self.assertTrue(self.webapp.check_password_hash(effective, ATTACK_PASS),
                        "前置条件失败：修复前正是这一行让攻击者登录成功")
        with mock.patch.object(self.webapp, "send_notification"):
            self.assertFalse(self.webapp.verify_admin("admin@test.local", ATTACK_PASS),
                             "歧义态下攻击者哈希不得带来认证成功")
            self.assertFalse(self.webapp.verify_admin("admin@test.local", ADMIN_PASS))

    def test_padded_duplicate_line_counts_as_ambiguous(self):
        """`KEY = v` 写法与紧凑写法重复 = 同一处歧义（折叠口径与检测口径须一致）。"""
        good = self._hash_of(ADMIN_PASS)
        self._write_env("YIBAN_ADMIN_USER=admin@test.local",
                        "YIBAN_ADMIN_PASSWORD_HASH = scrypt:stale-but-shadowing",
                        f"YIBAN_ADMIN_PASSWORD_HASH={good}")
        self.assertEqual(self._master_hash_line(), good,
                         "前置条件失败：紧凑写法一行应是生效行")
        with mock.patch.object(self.webapp, "send_notification"):
            self.assertFalse(self.webapp.verify_admin("admin@test.local", ADMIN_PASS))

    def test_latent_u2028_payload_counts_as_ambiguous(self):
        """升级前埋下的潜伏载荷：解析器仍取到合法哈希（证明潜伏），宽模型计数照样数出第二行。

        防线对这类载荷的全部价值就在这里——不等某次读-改-写实体化，verify_admin
        就先拒绝认证（解析侧看不到注入行、宽模型看得到，两者必须同时成立）。
        """
        good = self._hash_of(ADMIN_PASS)
        evil = self._hash_of(ATTACK_PASS)
        self._write_env(
            "YIBAN_ADMIN_USER=admin@test.local",
            f"YIBAN_ADMIN_PASSWORD_HASH={good}",
            f"YIBAN_ANNOUNCEMENT=notify\u2028YIBAN_ADMIN_PASSWORD_HASH={evil}",
        )
        # 解析侧（普适换行）看不到 U+2028 撑出的第二行 → 生效哈希仍是合法那份
        self.assertEqual(self._master_hash_line(), good,
                         "前置条件失败：潜伏态下解析器应仍取到合法哈希")
        # 宽模型计数已能看到它：任何一次实体化之前认证就被拒
        with mock.patch.object(self.webapp, "send_notification") as sn, \
                self.assertLogs("web", level="ERROR"):
            self.assertFalse(
                self.webapp.verify_admin("admin@test.local", ADMIN_PASS),
                "潜伏分隔符撑出的第二行哈希 = 主凭据歧义，必须 fail-closed")
        self.assertTrue(sn.called, "潜伏载荷构成的歧义同样要发紧急告警")

    def test_comment_line_is_not_counted_as_duplicate(self):
        """注释掉的同名行不是配置行：不得因它误判歧义而把主管理员锁在门外。"""
        good = self._hash_of(ADMIN_PASS)
        self._write_env("YIBAN_ADMIN_USER=admin@test.local",
                        "# YIBAN_ADMIN_PASSWORD_HASH=scrypt:old-rotated-out",
                        f"YIBAN_ADMIN_PASSWORD_HASH={good}")
        with mock.patch.object(self.webapp, "send_notification") as sn:
            self.assertTrue(self.webapp.verify_admin("admin@test.local", ADMIN_PASS))
        self.assertFalse(sn.called)

    def test_single_hash_still_authenticates(self):
        """唯一行必须照常通过（fail-closed 不得顺手把正常部署锁死）。"""
        good = self._hash_of(ADMIN_PASS)
        self._write_env("YIBAN_ADMIN_USER=admin@test.local",
                        f"YIBAN_ADMIN_PASSWORD_HASH={good}")
        with mock.patch.object(self.webapp, "send_notification") as sn:
            self.assertTrue(self.webapp.verify_admin("admin@test.local", ADMIN_PASS))
            self.assertFalse(self.webapp.verify_admin("admin@test.local", ATTACK_PASS),
                             "口令不匹配仍应照常拒绝")
        self.assertFalse(sn.called, "正常单行配置不得触发歧义告警")

    def test_ambiguity_recovery_is_one_write_away(self):
        """「不 brick」的另一半：把 .env 折叠成唯一一行即恢复登录（无需重启/改代码）。"""
        effective = self._two_hash_lines(legal_last=True)
        with mock.patch.object(self.webapp, "send_notification"):
            self.assertFalse(self.webapp.verify_admin("admin@test.local", ADMIN_PASS))
        # 旧行折叠（同一处修复）保证一次写入就能清掉重复行
        self.webapp.write_env_batch(self.env_file, {"YIBAN_ADMIN_PASSWORD_HASH": effective})
        hits = [ln for ln in _wide_lines(self.env_file)
                if ln.strip().split("=", 1)[0].strip() == "YIBAN_ADMIN_PASSWORD_HASH"]
        self.assertEqual(len(hits), 1, f"折叠后仍有多行主凭据: {hits}")
        with mock.patch.object(self.webapp, "send_notification") as sn:
            self.assertTrue(self.webapp.verify_admin("admin@test.local", ADMIN_PASS),
                            "修成唯一一行后应恢复正常认证能力")
        self.assertFalse(sn.called, "恢复后不得继续告警")


if __name__ == "__main__":
    unittest.main(verbosity=2)
