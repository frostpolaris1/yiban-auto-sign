# -*- coding: utf-8 -*-
"""批次14 第一档回归测试（2026-08-29）：密钥来源去 cwd 依赖 + rekey 迁移推送密文。

覆盖两条已活体复现的缺陷：

- P2-5：`db._audit_key()` / `db._track_salt()` 原为 `env_file = _env_file or ".env"`，
  取证/恢复类 CLI（rekey_accounts / audit_verify / clock_guard_reset /
  list_duplicate_owners）未传 env_file 时，密钥来源随当前工作目录漂移：在应用根
  之外运行读不到旧钥 → 就地生成新钥落盘 → 既留下"游离的 .env"，又用错密钥签这条
  审计行，真实哈希链从此判破（恢复工具反过来破坏恢复对象）。
  修复：有序回落 init_db(env_file=…) → YIBAN_ENV_FILE → cwd ".env"；来源只能靠 cwd
  兜底且该文件不存在时拒绝生成；四个 CLI 补传 env_file（其中三个新增 --env）。

- P2-2：rekey 换 YIBAN_ACCOUNTS_KEY 后，.env 里用同一把钥加密的
  YIBAN_NOTIFY_SECRET_ENC 仍是旧钥密文 → notify.get_secret() 解不开返回空 →
  消息通道静默死亡（最需要告警的时候没告警）。
  修复：轮换时旧钥解密 → 新钥重加密 → 与账号密钥同一次原子回写；--skip-notify 可
  跳过；任何失败都不让轮换失败，只在收尾自检行提示"需重新配置"。

评审后补的修复轮1（同样由本文件钉住）：
  ① 推送密文的 .env 读取纳入 try/except（读失败只报"需重新配置"，不得抛穿轮换）；
  ② 读-解密-重加密-写回收进同一把 env 写锁（--force 不停服时不用陈旧快照盖新值）；
  ③ 四条取证 CLI 对**显式** --env 做存在性校验，路径打错直接非零退出、不新建文件；
  ④ rekey 的 env_path 与 key_source 统一 strip 后解析一次（账号钥与审计钥同源）；
  ⑤ 补"什么都不传 + 空 cwd"与"--env 指向不存在路径"两条负例。

Task 3（本文件末尾两组用例，2026-08-29 活体复现的 P1-1）：PUT /api/mail-config 与
PUT /api/notify-config 原本只校验"是否内置主管理员"，拿到 Cookie 就能两步关掉两条告警
通道且零外发。现补：关闭/清钥/换钥三类动作二次鉴权 + 高危限速（429）、变更告警
urgent=True、cooldown=0 显式落盘、紧急账每日上限写入口、每日通道健康日报与
"推送额度已用尽"补发一封（一次 pop 拿全列表）。

Task 3 修复轮 1（评审必修 4 项 + 顺带 2 项，对应 HighRiskGateOrderB14Test 与
ChannelHealthReportB14Test / AlertChannelGateB14Test 内标了"评审 ①~⑥"的用例）：
  ① 日报邮件通道改用 mailer.is_enabled() 判可用——ENABLE=1 而缺 USER/PASS 时
    _send 静默跳过，旧写法会误报"一切正常"；现输出"⚠ 已开启但不可用"并触发降级 urgent。
  ② 限速额度只被"口令校验通过"的高危动作消耗（先鉴权、后计数），错口令尝试不得
    把主管理员预算刷满造成运维 DoS；同口径覆盖批次13 三处高危删除。
  ③ 加密（缺键时 load_key 会自行生成密钥并写 .env）移到闸门之后，鉴权失败零写入。
  ④ 日报"不依赖被改配置本身"补齐：app_meta 落库做每日至多一封（跨重启有效）；
    通道降级时先落 db.audit 痕迹再尝试发信，两通道同时被拆也留得住证据。
  ⑤ 关闭邮件通道的二次鉴权动作标签按字段区分（全局 vs 主管理员个人接收）。
  ⑥ 补断言：日报跨重启只发一封、配置端点审计详情包含数值项变更。

Task 3 修复轮 2（复评压缩后的 1 项 Important + 1 项 Minor）：
  Important-1：degraded 判据不再由"正文含 ⚠"驱动，改为直接取自
    web._alert_channel_status() 的结构化字段（邮件侧不可用 / 收件人为空、
    推送侧未配置或被关 / 额度耗尽）；审计痕迹摘要改为无条件记录两侧事实。
    组合变体"只关 admin_notify + 关闭推送 + 无其他接收管理员"（邮件侧与推送侧
    两行恰好都不含 ⚠）由 BothChannelsDeadCombinationVariantB14Test 钉住；
    判定与文案的解耦由 test_degraded_not_driven_by_warning_glyph_in_body 钉住。
    原 test_health_report_states_both_channels_even_when_closed 把「未配置」当
    正常文本钉死，已按新口径纠正为「⚠ 未配置」。
  Minor：健康日报的"今日已播"去重标记改到 send_notification 成功返回之后落，
    发信抛异常当日可重试（由 test_dedupe_marker_written_only_after_successful_send 钉住）。

Task 4（本文件末尾三组用例，2026-08-29 活体复现的 P1-2 / P3-2）：批次13 的三层加固
（告警节流 / 删除冷却 / 二次鉴权）只接在"删除用户"上，账号（易班凭据）侧的物理清除
链路一处都没接——普通注册管理员会话凭 Cookie 即可 `POST /api/accounts/batch
{"action":"purge","ids":[0]*10}`（不带 phones 连防错位都跳过）200 通过，单条
`/api/accounts/<idx>/purge` 连发全部 200 且一条告警都不发；另 PUT /api/accounts/<idx>
是同族写端点里唯一没接 _stale_idx_guard 的，目标行被物理清除后用旧 idx 提交会静默
改写另一账号并返回 200。现补：两处 purge 走统一高危门禁（先验口令、通过后才占额度）、
单条 purge 补同标题 urgent 告警、PUT 补防错位 409、前端"彻底删除"改走密码模态、
编辑态取不到乐观锁快照时禁止提交。

Task 5（本文件末尾 LoginTrailB14Test，2026-08-29 生产只读实测的 PROD-2）：盗号事件里
"会话何时从哪个 IP 结束"与"恢复入口建立的会话"在 audit_logs 里完全无迹可查（logout 类
动作 0 条，恢复即登录只留 user_self_delete_restore），page_visits 也 0 行；登录侧自批次7
A6 起就有留痕，但动作名 login 与 login_failed 不成组。现补：① 成功登录的动作名由 login
收敛为 login_ok（写入点仍是两条成功分支的 `if role:` 汇合点，auth_source 落在 detail；
username 补 64 字截断）；② /api/me/restore 的"恢复即登录"同样留 login_ok，
detail 标「恢复登录」，sid 签发与 pw_version 语义一字未动；③ /api/logout 留
logout_ok。三元组口径与既有 forbidden_path 逐字同构（target 只存 IP 的 HMAC、
不写密码/token/Cookie），失败登录仍只有既有的阈值留痕，不冒领成功留痕。注意本组用例
只钉改名后的新名：跨版本取证须写 action IN ('login','login_ok')，改名前写入的历史行
动作名是 login。

用法（项目根目录，勿设 PYTHONIOENCODING）：
    py -m pytest tests/test_batch14_fixes_0829.py -v
"""
import contextlib
import importlib.util
import inspect
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))

import account_crypto  # noqa: E402
import db  # noqa: E402
import notify  # noqa: E402

OLD_KEY = "a" * 64
NEW_KEY = "b" * 64
AUDIT_KEY = "c" * 64
# 三把不同值的审计密钥分放三个 env 文件，用于区分回落顺序命中了哪一档
KEY_VIA_ENV_FILE = "1" * 64
KEY_VIA_ENV_VAR = "2" * 64
KEY_VIA_CWD = "3" * 64
SCT_KEY = "SCT406257BATCH14TEST"
PHONE = "13800000001"
PW_PLAIN = "pw-甲"

_CHILD_POP_KEYS = (
    "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_KEY", "YIBAN_AUDIT_KEY", "YIBAN_TRACK_SALT",
    "YIBAN_DB_FILE", "YIBAN_STATE_DIR", "YIBAN_LOG_FILE",
)


def _clear_caches():
    """清进程内密钥缓存：跨用例/跨测试文件的全局态会掩盖"拿错密钥"的真相。"""
    account_crypto._KEY_CACHE = None
    db._AUDIT_KEY_CACHE = None
    db._TRACK_SALT_CACHE = None


@contextlib.contextmanager
def _cwd(path):
    """临时切换工作目录（notify 取钥按 _env_path() 解析：YIBAN_ENV_FILE 优先，未设时才落到 cwd/.env）。"""
    prev = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(prev)


@contextlib.contextmanager
def _db_env_file(path):
    """临时设定 db._env_file（等价于 init_db(env_file=…) 的效果），退出时还原。"""
    prev = db._env_file
    db._env_file = path
    _clear_caches()
    try:
        yield
    finally:
        db._env_file = prev
        _clear_caches()


@contextlib.contextmanager
def _os_env(**pairs):
    """临时设置环境变量（值 None = 删除），退出时还原。"""
    prev = {k: os.environ.get(k) for k in pairs}
    for k, v in pairs.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    _clear_caches()
    try:
        yield
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _clear_caches()


def _write_env(path, lines):
    with io.open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _read_env(path):
    with io.open(path, encoding="utf-8-sig") as f:
        return f.read()


def _env_value(path, key):
    for ln in _read_env(path).splitlines():
        if ln.strip().startswith(f"{key}="):
            return ln.split("=", 1)[1].strip()
    return None


def _notify_enc(key_hex):
    """按 web 设置页同口径生成 YIBAN_NOTIFY_SECRET_ENC 的值（固定 AAD 的密文 JSON）。"""
    enc = account_crypto.encrypt_text(SCT_KEY, account_crypto._decode_key(key_hex))
    return json.dumps(enc, ensure_ascii=False)


def _close_db():
    if db._conn is not None:
        with contextlib.suppress(Exception):
            db._conn.close()
        db._conn = None


def _run_cli(script, cli, cwd, extra_env=None):
    """在指定 cwd 下运行 scripts/ 工具；子进程输出固定 UTF-8（Windows 默认 GBK 会乱码）。"""
    env = dict(os.environ)
    for k in _CHILD_POP_KEYS:
        env.pop(k, None)
    for k in [k for k in env if k.startswith("YIBAN_NOTIFY_")]:
        env.pop(k, None)
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, os.path.join(BASE, "scripts", script), *cli],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=cwd, env=env, timeout=180, input="n\n",
    )


class _B14Fixture(unittest.TestCase):
    """公共夹具：一个干净的临时工作目录 + 一份"部署式" .env + 已建链的库。

    ENV_IN_CWD=False：.env 放在工作目录之外（取证场景——工具在应用根之外运行）；
    ENV_IN_CWD=True ：.env 就在 cwd 下（未设 YIBAN_ENV_FILE 时，notify 取钥落到 cwd/.env）。
    """

    ENV_IN_CWD = False

    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.mkdtemp(prefix="b14-work-")     # 模拟"应用根之外的任意 cwd"
        cls.keydir = tempfile.mkdtemp(prefix="b14-env-")    # 模拟真正的部署目录
        cls.env_file = os.path.join(cls.work if cls.ENV_IN_CWD else cls.keydir, ".env")
        cls.db_file = os.path.join(cls.work, "yiban.db")

    @classmethod
    def tearDownClass(cls):
        _close_db()
        shutil.rmtree(cls.work, ignore_errors=True)
        shutil.rmtree(cls.keydir, ignore_errors=True)

    def setUp(self):
        for k in _CHILD_POP_KEYS:
            os.environ.pop(k, None)
        for k in [k for k in os.environ if k.startswith("YIBAN_NOTIFY_")]:
            os.environ.pop(k, None)
        _clear_caches()
        _close_db()
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        for p in (self.env_file, os.path.join(self.work, ".env")):
            if os.path.exists(p):
                os.remove(p)

    def tearDown(self):
        _close_db()
        _clear_caches()

    def seed(self, notify_line=None):
        """写 .env（账号钥/审计钥/可选推送密文）并建库：1 个加密账号 + 2 行审计留痕。"""
        lines = [f"YIBAN_ACCOUNTS_KEY={OLD_KEY}", f"YIBAN_AUDIT_KEY={AUDIT_KEY}",
                 "YIBAN_OTHER_KEEP=1"]
        if notify_line:
            lines.append("YIBAN_NOTIFY_TYPE=serverchan")
            lines.append(f"YIBAN_NOTIFY_SECRET_ENC={notify_line}")
        _write_env(self.env_file, lines)
        _clear_caches()
        db.init_db(db_file=self.db_file, env_file=self.env_file, cleanup=False)
        db.replace_accounts([{"phone": PHONE, "password": PW_PLAIN,
                              "phone_code": "code-1", "owner": ""}])
        self.assertTrue(db.audit("seed-admin", "seed_login", "", "链上第一行"))
        self.assertTrue(db.audit("seed-admin", "seed_logout", "", "链上第二行"))
        n = db.get_conn().execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
        self.assertGreaterEqual(n, 2, "前置条件失败：链上须有留痕行（空链校验恒通过，断言无意义）")
        ok, broken, first = db.verify_audit_chain()
        self.assertTrue(ok, f"前置条件失败：种子链应自洽（broken={broken} id={first}）")
        _close_db()

    def verify_chain_with_prod_env(self, db_file=None):
        """用部署 .env 里的 YIBAN_AUDIT_KEY 重开库校验链（返回 (ok, broken, first)）。"""
        _clear_caches()
        db.init_db(db_file=db_file or self.db_file, env_file=self.env_file,
                   cleanup=False, migrate=False)
        try:
            return db.verify_audit_chain()
        finally:
            _close_db()

    def fresh_empty_cwd(self):
        """造一个"空"临时目录，只把已建链的库按默认名 yiban.db 复制进去。

        给"CLI 什么都不传（既无 --env 也无 YIBAN_ENV_FILE）、cwd 下没有 .env"的负例用：
        此时 db.DB_DEFAULT 解析到该目录里的这份拷贝，拷贝上的链是否仍完好，
        就是"工具到底有没有另起一把钥匙"的证据。
        """
        d = tempfile.mkdtemp(prefix="b14-empty-cwd-", dir=self.keydir)
        shutil.copy(self.db_file, os.path.join(d, "yiban.db"))
        for suffix in ("-wal", "-shm"):
            src = self.db_file + suffix
            if os.path.exists(src):
                shutil.copy(src, os.path.join(d, "yiban.db" + suffix))
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        return d

    def assert_db_account_readable(self, key_hex):
        obj_row = None
        conn = sqlite3.connect(self.db_file)
        conn.row_factory = sqlite3.Row
        try:
            obj_row = conn.execute("SELECT phone, password FROM accounts WHERE phone=?",
                                   (PHONE,)).fetchone()
        finally:
            conn.close()
        obj = json.loads(obj_row["password"])
        self.assertEqual(
            account_crypto.decrypt_password(obj, account_crypto._decode_key(key_hex),
                                            obj_row["phone"]),
            PW_PLAIN, "轮换本身必须仍然正常完成（账号凭据不丢是首要目标）")


class KeySourceFallbackB14Test(unittest.TestCase):
    """P2-5 之一：_audit_key/_track_salt 的 .env 路径回落顺序与防游离落盘判据。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="b14-resolve-")
        cls.cwd_dir = os.path.join(cls.tmp, "approot")      # 内有 .env（cwd 兜底档）
        os.makedirs(cls.cwd_dir, exist_ok=True)
        cls.f_env_file = os.path.join(cls.tmp, "via-init-db.env")
        cls.f_env_var = os.path.join(cls.tmp, "via-yiban-env-file.env")
        _write_env(cls.f_env_file, [f"YIBAN_AUDIT_KEY={KEY_VIA_ENV_FILE}",
                                    "YIBAN_TRACK_SALT=salt-via-init-db"])
        _write_env(cls.f_env_var, [f"YIBAN_AUDIT_KEY={KEY_VIA_ENV_VAR}",
                                   "YIBAN_TRACK_SALT=salt-via-env-var"])
        _write_env(os.path.join(cls.cwd_dir, ".env"),
                   [f"YIBAN_AUDIT_KEY={KEY_VIA_CWD}", "YIBAN_TRACK_SALT=salt-via-cwd"])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        os.environ.pop("YIBAN_AUDIT_KEY", None)
        os.environ.pop("YIBAN_TRACK_SALT", None)
        _clear_caches()

    def tearDown(self):
        _clear_caches()

    def test_audit_key_fallback_order(self):
        """审计密钥来源：init_db(env_file=…) > YIBAN_ENV_FILE > 当前目录 .env。"""
        with _cwd(self.cwd_dir):
            with _db_env_file(self.f_env_file), _os_env(YIBAN_ENV_FILE=self.f_env_var):
                self.assertEqual(db._resolve_key_env_file(), (self.f_env_file, False))
                self.assertEqual(db._audit_key(), bytes.fromhex(KEY_VIA_ENV_FILE))
            with _db_env_file(None), _os_env(YIBAN_ENV_FILE=self.f_env_var):
                self.assertEqual(db._resolve_key_env_file(), (self.f_env_var, False))
                self.assertEqual(db._audit_key(), bytes.fromhex(KEY_VIA_ENV_VAR))
            # 两者皆无：靠 cwd 兜底——文件存在时行为完全不变（仍读到旧钥）
            with _db_env_file(None), _os_env(YIBAN_ENV_FILE=None):
                self.assertEqual(db._resolve_key_env_file(), (".env", True))
                self.assertEqual(db._audit_key(), bytes.fromhex(KEY_VIA_CWD))

    def test_track_salt_uses_same_fallback(self):
        """追踪盐与审计密钥共用同一回落链（换部署目录不能一半对一半错）。"""
        with _cwd(self.cwd_dir):
            with _db_env_file(self.f_env_file), _os_env(YIBAN_ENV_FILE=self.f_env_var):
                self.assertEqual(db._track_salt(), "salt-via-init-db")
            with _db_env_file(None), _os_env(YIBAN_ENV_FILE=self.f_env_var):
                self.assertEqual(db._track_salt(), "salt-via-env-var")
            with _db_env_file(None), _os_env(YIBAN_ENV_FILE=None):
                self.assertEqual(db._track_salt(), "salt-via-cwd")

    def test_refuses_stray_generation_when_source_undetermined(self):
        """来源只能靠 cwd 兜底且该 .env 不存在：拒绝生成、什么都不写、抛 ValueError。"""
        stray_dir = tempfile.mkdtemp(prefix="b14-stray-", dir=self.tmp)
        with _cwd(stray_dir), _db_env_file(None), _os_env(YIBAN_ENV_FILE=None):
            with self.assertRaises(ValueError) as cm:
                db._audit_key()
            self.assertEqual(
                str(cm.exception),
                "审计密钥来源不确定，拒绝生成新密钥；请用 --env 或 YIBAN_ENV_FILE 指定 .env 路径",
            )
            self.assertFalse(os.path.exists(os.path.join(stray_dir, ".env")),
                             "拒绝生成就必须真的什么都不写——游离 .env 正是本次要治的病")
            with self.assertRaises(ValueError) as cm2:
                db._track_salt()
            self.assertIn("追踪盐来源不确定", str(cm2.exception))
            self.assertFalse(os.path.exists(os.path.join(stray_dir, ".env")))

    def test_readonly_verify_path_still_returns_none(self):
        """create=False（校验路径）在同样场景仍返回 None：只读工具不得被新检查打断。"""
        stray_dir = tempfile.mkdtemp(prefix="b14-verify-", dir=self.tmp)
        with _cwd(stray_dir), _db_env_file(None), _os_env(YIBAN_ENV_FILE=None):
            self.assertIsNone(db._audit_key(create=False))

    def test_explicit_source_still_generates_on_first_run(self):
        """显式来源（--env/YIBAN_ENV_FILE）时首启生成行为不变，包括目标文件不存在。"""
        target = os.path.join(self.tmp, "first-run", ".env")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        self.assertFalse(os.path.exists(target))
        with _cwd(self.cwd_dir), _db_env_file(target), _os_env(YIBAN_ENV_FILE=None):
            key = db._audit_key()
        self.assertEqual(len(key), 32)
        self.assertIn("YIBAN_AUDIT_KEY=", _read_env(target))

    def test_existing_env_without_key_still_generates(self):
        """cwd 有 .env 但没配密钥：照常就地生成（判据只看文件是否存在，行为不变）。"""
        d = tempfile.mkdtemp(prefix="b14-exists-", dir=self.tmp)
        _write_env(os.path.join(d, ".env"), ["YIBAN_OTHER=1"])
        with _cwd(d), _db_env_file(None), _os_env(YIBAN_ENV_FILE=None):
            key = db._audit_key()
        content = _read_env(os.path.join(d, ".env"))
        self.assertEqual(len(key), 32)
        self.assertIn("YIBAN_AUDIT_KEY=", content)
        self.assertIn("YIBAN_OTHER=1", content, "生成不得丢掉 .env 里的其它配置")


class ForensicCliKeySourceB14Test(_B14Fixture):
    """P2-5 之二：四个 CLI 在应用根之外运行（--env 指定密钥源）不得污染密钥与链。"""

    def setUp(self):
        super().setUp()
        # 每例重新播种：这些用例断言的是"链仍然自洽"，必须有已建链的库与部署 .env
        self.seed()

    def test_rekey_from_foreign_cwd_leaves_no_stray_env(self):
        """rekey 子进程在临时 cwd 运行后：该目录无新建 .env，原审计链仍校验通过。"""
        r = _run_cli("rekey_accounts.py",
                     ["--db", self.db_file, "--env", self.env_file,
                      "--new-key", NEW_KEY, "--force"], cwd=self.work)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(_env_value(self.env_file, "YIBAN_ACCOUNTS_KEY"), NEW_KEY)
        self.assertEqual(_env_value(self.env_file, "YIBAN_AUDIT_KEY"), AUDIT_KEY,
                         "轮换只换账号密钥，审计密钥必须原样保留")
        residues = [n for n in os.listdir(self.work) if n.startswith(".env")]
        self.assertEqual(residues, [], f"游离/临时密钥文件落在临时 cwd 上: {residues}")
        ok, broken, first = self.verify_chain_with_prod_env()
        self.assertTrue(ok, f"轮换留痕用错了密钥，真实链被签坏：broken={broken} first={first}")
        self.assert_db_account_readable(NEW_KEY)

    def test_clock_guard_reset_keeps_chain_intact(self):
        """clock_guard_reset 会写审计行：来源解析错目录时该步会用游离密钥签名，链必断。"""
        r = _run_cli("clock_guard_reset.py",
                     ["--db", self.db_file, "--env", self.env_file, "--confirm"], cwd=self.work)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("审计留痕失败", r.stdout)
        self.assertEqual([n for n in os.listdir(self.work) if n.startswith(".env")], [])
        ok, broken, first = self.verify_chain_with_prod_env()
        self.assertTrue(ok, f"重置留痕用错了密钥：broken={broken} first={first}")
        conn = sqlite3.connect(self.db_file)
        try:
            n = conn.execute("SELECT COUNT(*) FROM audit_logs "
                             "WHERE action='clock_guard_reset'").fetchone()[0]
        finally:
            conn.close()
        self.assertGreaterEqual(n, 1, "重置动作应留痕（留痕成功才是链路正确的证据）")

    def test_audit_verify_reads_key_from_env_flag(self):
        """audit_verify --env：换目录也能读到正确密钥并报"校验通过"（此前报"过程异常"）。"""
        r = _run_cli("audit_verify.py", ["--db", self.db_file, "--env", self.env_file],
                     cwd=self.work)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("校验通过", r.stdout)
        self.assertEqual([n for n in os.listdir(self.work) if n.startswith(".env")], [])

    def test_list_duplicate_owners_reads_key_from_env_flag(self):
        """list_duplicate_owners 会跑迁移（v3 重链要用审计密钥）：同样不得依赖 cwd。"""
        r = _run_cli("list_duplicate_owners.py",
                     ["--db", self.db_file, "--env", self.env_file], cwd=self.work)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([n for n in os.listdir(self.work) if n.startswith(".env")], [])
        ok, broken, first = self.verify_chain_with_prod_env()
        self.assertTrue(ok, f"只读清点改动了审计链：broken={broken} first={first}")

    def test_cli_honours_yiban_env_file_without_flag(self):
        """不传 --env 时 YIBAN_ENV_FILE 同样生效（回落链第二档），仍无游离 .env。"""
        r = _run_cli("audit_verify.py", ["--db", self.db_file], cwd=self.work,
                     extra_env={"YIBAN_ENV_FILE": self.env_file})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("校验通过", r.stdout)
        self.assertEqual([n for n in os.listdir(self.work) if n.startswith(".env")], [])

    def test_cli_with_no_env_source_at_all_refuses_generation(self):
        """修复轮1⑤：什么都不传 + cwd 下没有 .env → 拒绝生成密钥，且不落游离 .env。

        这是其余用例都绕开的那一档：既无 --env 也无 YIBAN_ENV_FILE，密钥来源只能靠
        cwd 兜底。此前工具会就地生成一把新审计密钥写进新 .env 并用它签留痕——
        现在必须"什么都不写、宁可不签"。
        """
        empty = self.fresh_empty_cwd()
        cases = [
            ("audit_verify.py", []),                      # create=False → fail-closed
            ("clock_guard_reset.py", ["--confirm"]),      # create=True → 拒绝生成
            ("list_duplicate_owners.py", []),             # 会跑迁移（重链要用审计密钥）
        ]
        for script, cli in cases:
            with self.subTest(script=script):
                r = _run_cli(script, cli, cwd=empty)
                residues = [n for n in os.listdir(empty) if n.startswith(".env")]
                self.assertEqual(
                    residues, [],
                    f"{script} 在来源不确定时仍落了游离 .env: {residues}\n"
                    f"rc={r.returncode}\n{r.stdout}\n{r.stderr}")
                # 用部署 .env 的密钥校验拷贝库：链仍自洽 = 没有一行是用游离新钥签的
                copy_db = os.path.join(empty, "yiban.db")
                ok, broken, first = self.verify_chain_with_prod_env(copy_db)
                self.assertTrue(ok, f"{script} 用错密钥签了审计行：broken={broken} first={first}")
        # 部署 .env 里的审计密钥原样未动（没有被"就地生成第二把钥"顶替）
        self.assertEqual(_env_value(self.env_file, "YIBAN_AUDIT_KEY"), AUDIT_KEY)

    def test_explicit_missing_env_is_rejected(self):
        """修复轮1③+⑤：显式 --env 指向不存在的文件 → 非零退出，且绝不创建该文件。

        打错路径时若继续执行，四条 CLI 都会把该路径当作"来源已确定"，在那里新建
        .env + 生成新审计密钥，把这次留痕用第三把钥匙签坏（正是本任务要治的病症
        的新入口）。未显式给 --env 时不受本用例影响（见上一条用例）。
        """
        missing = os.path.join(self.keydir, "typo-deploy.env")   # 目录存在、文件不存在
        self.assertFalse(os.path.exists(missing))
        cases = [
            ("rekey_accounts.py", ["--db", self.db_file, "--env", missing,
                                   "--new-key", NEW_KEY, "--force"]),
            ("audit_verify.py", ["--db", self.db_file, "--env", missing]),
            ("clock_guard_reset.py", ["--db", self.db_file, "--env", missing, "--confirm"]),
            ("list_duplicate_owners.py", ["--db", self.db_file, "--env", missing]),
        ]
        for script, cli in cases:
            with self.subTest(script=script):
                r = _run_cli(script, cli, cwd=self.work)
                out = r.stdout + r.stderr
                self.assertNotEqual(r.returncode, 0,
                                    f"{script} 对不存在的 --env 仍照常执行: {out}")
                self.assertIn("--env 指定的 .env 不存在", out, f"{script} 未给出明确错误: {out}")
                self.assertFalse(os.path.exists(missing), f"{script} 在该路径新建了 .env")
                self.assertFalse(os.path.exists(missing + ".rekey-staging"),
                                 f"{script} 在错误位置留下了暂存密钥文件")
        # 中止必须是"什么都没发生"：部署密钥、账号密文与审计链都保持原状
        self.assertEqual(_env_value(self.env_file, "YIBAN_ACCOUNTS_KEY"), OLD_KEY)
        self.assertEqual(_env_value(self.env_file, "YIBAN_AUDIT_KEY"), AUDIT_KEY)
        ok, broken, first = self.verify_chain_with_prod_env()
        self.assertTrue(ok, f"被拒绝的执行仍改动了链：broken={broken} first={first}")
        self.assert_db_account_readable(OLD_KEY)

    def test_padded_yiban_env_file_resolves_to_one_file(self):
        """修复轮1④+⑤：YIBAN_ENV_FILE 带空白时，账号钥落点与审计钥读点必须是同一个文件。

        此前 rekey 的 env_path 不 strip、key_source 走 strip，" path/.env " 会让两者
        指向不同文件——账号钥读不到（报"未找到当前密钥"）或账号钥写 A、审计钥读 B。
        """
        r = _run_cli("rekey_accounts.py",
                     ["--db", self.db_file, "--new-key", NEW_KEY, "--force"],
                     cwd=self.work, extra_env={"YIBAN_ENV_FILE": f" {self.env_file} "})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(_env_value(self.env_file, "YIBAN_ACCOUNTS_KEY"), NEW_KEY,
                         "账号密钥必须写进 strip 后的那个文件")
        self.assertEqual(_env_value(self.env_file, "YIBAN_AUDIT_KEY"), AUDIT_KEY)
        self.assertEqual([n for n in os.listdir(self.work) if n.startswith(".env")], [],
                         "不得因路径带空白而在 cwd 另落一份 .env")
        ok, broken, first = self.verify_chain_with_prod_env()
        self.assertTrue(ok, f"留痕读的密钥与写的不是同一份：broken={broken} first={first}")

    def test_rotated_notify_secret_is_re_read_inside_env_lock(self):
        """修复轮1②+⑤：读现值必须在写锁内——锁外快照会盖掉期间设置页的修改。

        做法：把真实的 env 写锁包一层，在**拿到锁之后**替设置页改写
        YIBAN_NOTIFY_SECRET_ENC（换成另一把 SendKey）。正确实现（读-改-写同锁）
        迁移的必须是改写后的值；若读发生在锁外（修复前的 main 流程），
        落盘的就是改写前的陈旧值。
        """
        import env_lock
        import rekey_accounts

        secret_v1 = _notify_enc(OLD_KEY)
        secret_v2 = json.dumps(account_crypto.encrypt_text(
            "SCT406257CHANGEDBY-WEB", account_crypto._decode_key(OLD_KEY)), ensure_ascii=False)
        tmp = tempfile.mkdtemp(prefix="b14-lock-")
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        env = os.path.join(tmp, ".env")
        _write_env(env, [f"YIBAN_ACCOUNTS_KEY={OLD_KEY}", f"YIBAN_NOTIFY_SECRET_ENC={secret_v1}"])
        real_lock = env_lock.env_write_lock
        held = {"n": 0}

        @contextlib.contextmanager
        def lock_that_sees_web_edit(path):
            with real_lock(path):
                held["n"] += 1
                try:
                    # 模拟工具启动后才发生的设置页修改（发生在读之前才对测试有意义）
                    _write_env(path, [f"YIBAN_ACCOUNTS_KEY={OLD_KEY}",
                                      f"YIBAN_NOTIFY_SECRET_ENC={secret_v2}"])
                    yield
                finally:
                    held["n"] -= 1

        with mock.patch.object(env_lock, "env_write_lock", lock_that_sees_web_edit):
            state = rekey_accounts.rotate_and_write_env(
                env, account_crypto._decode_key(NEW_KEY), account_crypto._decode_key(OLD_KEY))
        self.assertEqual(state, "rotated")
        self.assertEqual(held["n"], 0, "锁必须已释放")
        self.assertEqual(_env_value(env, "YIBAN_ACCOUNTS_KEY"), NEW_KEY)
        entry = json.loads(_env_value(env, "YIBAN_NOTIFY_SECRET_ENC"))
        self.assertEqual(
            account_crypto.decrypt_text(entry, account_crypto._decode_key(NEW_KEY)),
            "SCT406257CHANGEDBY-WEB",
            "迁移必须基于写锁内读到的现值，不能是启动时的陈旧快照")


class RekeyNotifySecretB14Test(_B14Fixture):
    """P2-2：换钥时 YIBAN_NOTIFY_SECRET_ENC 必须随轮换重加密（否则通道静默死亡）。

    ENV_IN_CWD=True：notify.get_secret() 按 _env_path() 取钥——YIBAN_ENV_FILE 优先、
    未设时才落到 cwd/.env。本夹具全程不设 YIBAN_ENV_FILE（子进程环境里该键被剥掉，
    走的正是 --env 指定的文件），所以把 .env 放在 cwd 才能按生产口径读回明文。
    """

    ENV_IN_CWD = True

    def _run_rekey(self, *extra):
        return _run_cli("rekey_accounts.py",
                        ["--db", self.db_file, "--env", self.env_file,
                         "--new-key", NEW_KEY, "--force", *extra], cwd=self.work)

    def test_secret_survives_rotation(self):
        """正路：轮换后 get_secret() 仍解出原 SendKey，自检行报"已随换钥迁移"。"""
        self.seed(notify_line=_notify_enc(OLD_KEY))
        r = self._run_rekey()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("推送通道自检：已随换钥迁移", r.stdout)
        self.assertEqual(_env_value(self.env_file, "YIBAN_ACCOUNTS_KEY"), NEW_KEY)
        self.assertIn("YIBAN_NOTIFY_TYPE=serverchan", _read_env(self.env_file),
                      "原子回写必须保留 .env 其它行")
        new_enc = _env_value(self.env_file, "YIBAN_NOTIFY_SECRET_ENC")
        entry = json.loads(new_enc)
        self.assertEqual(
            account_crypto.decrypt_text(entry, account_crypto._decode_key(NEW_KEY)), SCT_KEY,
            "密文必须已换成新钥可解")
        with self.assertRaises(ValueError):
            account_crypto.decrypt_text(entry, account_crypto._decode_key(OLD_KEY))
        _clear_caches()
        with _cwd(self.work):
            self.assertEqual(notify.get_secret(), SCT_KEY,
                             "P2-2 未修好：换钥后推送密钥解不开，通道静默死亡")
            cfg = notify.get_config()
        self.assertTrue(cfg["enabled"], "设置页应仍显示通道可用")
        self.assertEqual(cfg["type"], "serverchan")
        self.assert_db_account_readable(NEW_KEY)

    def test_skip_notify_keeps_old_ciphertext(self):
        """--skip-notify：轮换照常成功、不报错，但密文保持旧钥（get_secret 变空）。"""
        old_enc = _notify_enc(OLD_KEY)
        self.seed(notify_line=old_enc)
        r = self._run_rekey("--skip-notify")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("推送通道自检：需重新配置", r.stdout)
        self.assertEqual(_env_value(self.env_file, "YIBAN_NOTIFY_SECRET_ENC"), old_enc,
                         "跳过迁移就应保持原密文不动")
        self.assertEqual(_env_value(self.env_file, "YIBAN_ACCOUNTS_KEY"), NEW_KEY)
        _clear_caches()
        with _cwd(self.work):
            self.assertEqual(notify.get_secret(), "",
                             "旧钥密文在新钥下必须解不开（这正是默认路径要治的静默死亡）")

    def test_corrupted_secret_does_not_abort_rotation(self):
        """密文损坏：不中止轮换（首要目标是账号凭据不丢），只提示需重新配置。"""
        self.seed(notify_line="not-a-json-ciphertext")
        r = self._run_rekey()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("需重新配置", r.stdout)
        self.assertEqual(_env_value(self.env_file, "YIBAN_ACCOUNTS_KEY"), NEW_KEY)
        self.assertEqual(_env_value(self.env_file, "YIBAN_NOTIFY_SECRET_ENC"),
                         "not-a-json-ciphertext", "解不开时不得改写坏值")
        self.assert_db_account_readable(NEW_KEY)
        ok, broken, first = self.verify_chain_with_prod_env()
        self.assertTrue(ok, f"轮换留痕链坏：broken={broken} first={first}")

    def test_unconfigured_notify_stays_unset(self):
        """未配置推送：自检行报"未配置"，且不得往 .env 里塞空密钥键。"""
        self.seed()
        r = self._run_rekey()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("推送通道自检：未配置", r.stdout)
        self.assertNotIn("YIBAN_NOTIFY_SECRET_ENC", _read_env(self.env_file))

    def test_env_only_completion_also_migrates_secret(self):
        """崩溃补完（--env-only）同样迁移推送密文：该路径也会换新钥。"""
        self.seed(notify_line=_notify_enc(OLD_KEY))
        # 模拟第 2 步提交后中断：库内已是新钥密文，.env 仍是旧钥
        conn = sqlite3.connect(self.db_file)
        try:
            enc = json.dumps(account_crypto.encrypt_password(
                PW_PLAIN, account_crypto._decode_key(NEW_KEY), PHONE), ensure_ascii=False)
            conn.execute("UPDATE accounts SET password=?, phone_code='' WHERE phone=?",
                         (enc, PHONE))
            conn.commit()
        finally:
            conn.close()
        r = _run_cli("rekey_accounts.py",
                     ["--db", self.db_file, "--env", self.env_file, "--new-key", NEW_KEY,
                      "--env-only", "--force"], cwd=self.work)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("推送通道自检：已随换钥迁移", r.stdout)
        _clear_caches()
        with _cwd(self.work):
            self.assertEqual(notify.get_secret(), SCT_KEY)


class RekeyBestEffortB14Test(unittest.TestCase):
    """修复轮1①/③/④ 的边界单测（无需建库，直接打函数）。"""

    def test_unreadable_env_file_reports_failed_instead_of_raising(self):
        """①：.env 读失败必须转成 failed 状态，而不是抛穿 main()。

        account_crypto._parse_env_file 对"文件存在但读取失败"（权限/占用）刻意重抛
        OSError（防 load_key 误判未配置而生成新钥覆盖旧钥）；但轮换工具此时正处在
        "库里已是新钥、.env 尚未写"的窗口——异常穿透 main 就留下 库=新钥/env=旧钥
        的不一致态，违反"推送密文迁移是尽力而为"的约束。
        这里用一个目录当 env 路径制造 OSError（Windows PermissionError / POSIX
        IsADirectoryError，两者都是 OSError），跨平台且不需要真去改文件权限。
        """
        import rekey_accounts

        tmp = tempfile.mkdtemp(prefix="b14-best-effort-")
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        old = account_crypto._decode_key(OLD_KEY)
        new = account_crypto._decode_key(NEW_KEY)
        self.assertEqual(rekey_accounts.rotate_notify_secret(tmp, old, new), ("failed", None))
        # --skip-notify 时本就不迁移，读失败也不改变结论（仍报"跳过"）
        self.assertEqual(rekey_accounts.rotate_notify_secret(tmp, old, new, skip=True),
                         ("skipped", None))

    def test_require_existing_env_file_checks_only_explicit_source(self):
        """③+④：只校验显式 --env（去空白后比较），未显式给出时保持回落链现状。"""
        tmp = tempfile.mkdtemp(prefix="b14-require-env-")
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        existing = os.path.join(tmp, ".env")
        _write_env(existing, [f"YIBAN_AUDIT_KEY={AUDIT_KEY}"])
        missing = os.path.join(tmp, "typo-deploy.env")
        with _os_env(YIBAN_ENV_FILE=None):
            self.assertIsNone(db.require_existing_env_file(None), "无显式来源 → 交回落链")
            self.assertIsNone(db.require_existing_env_file("   "), "全空白 = 未指定")
            self.assertEqual(db.require_existing_env_file(existing), existing)
            self.assertEqual(db.require_existing_env_file(f" {existing} "), existing,
                             "④：显式值与回落值统一 strip，避免账号钥/审计钥指向不同文件")
            with self.assertRaises(ValueError) as cm:
                db.require_existing_env_file(missing)
            self.assertIn("不存在", str(cm.exception))
        with _os_env(YIBAN_ENV_FILE=missing):
            # 未显式给 --env 时 YIBAN_ENV_FILE 指向缺失文件不在此拦截（既有行为不变，
            # 由 _assert_key_source_certain 在真正要生成密钥时兜底）
            self.assertEqual(db.require_existing_env_file(None), missing)


ADMIN_PASS = "MasterPass#2026"


class _B14AlertGateBase(unittest.TestCase):
    """Task 3 的 web 夹具：隔离 .env + 数据库 + 内置主管理员会话。

    send_notification 默认替换为记录 (title, content, urgent) 的假实现——本批次
    要钉住的正是"变更告警是否 urgent"（非紧急在「仅重要告警」下不推手机 = 致盲），
    同时顺带杜绝任何真实 SMTP/网络出口。需要走真实 send_notification 的用例
    （额度耗尽接线）把 PATCH_NOTIFY 置 False。

    定义在本文件最后：pytest 按定义顺序执行，本夹具会改写 os.environ 里的
    YIBAN_* 全组变量，放在末尾才不污染前面那几组密钥来源用例。
    """

    PATCH_NOTIFY = True

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="b14-gate-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        # 种子内容：每例 setUp 都按它整体复位（见 _reset_state 的说明）
        cls.env_seed = [
            f"YIBAN_ACCOUNTS_KEY={OLD_KEY}",
            f"YIBAN_AUDIT_KEY={AUDIT_KEY}",
            "YIBAN_ADMIN_USER=admin",
            f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}",
            "YIBAN_MAIL_ADMIN_TO=admin@test.local",
        ]
        _write_env(cls.env_file, cls.env_seed)
        cls.db_file = os.path.join(cls.tmp, "yiban.db")
        cls.accounts_file = os.path.join(cls.tmp, "accounts.json")
        cls.users_file = os.path.join(cls.tmp, "users.json")
        os.environ["YIBAN_ACCOUNTS_KEY"] = OLD_KEY
        os.environ["YIBAN_ENV_FILE"] = cls.env_file
        os.environ["YIBAN_ACCOUNTS_FILE"] = cls.accounts_file
        os.environ["YIBAN_USERS_FILE"] = cls.users_file
        os.environ["YIBAN_DB_FILE"] = cls.db_file
        os.environ["YIBAN_STATE_DIR"] = cls.tmp
        os.environ["YIBAN_LOG_FILE"] = os.path.join(cls.tmp, "sign.log")
        spec = importlib.util.spec_from_file_location(
            "webapp_b14", os.path.join(BASE, "web", "app.py"))
        cls.webapp = importlib.util.module_from_spec(spec)
        sys.modules["webapp_b14"] = cls.webapp
        spec.loader.exec_module(cls.webapp)

    @classmethod
    def tearDownClass(cls):
        _close_db()
        sys.modules.pop("webapp_b14", None)
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k in ("YIBAN_ACCOUNTS_KEY", "YIBAN_LOG_FILE", "YIBAN_STATE_DIR",
                  "YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_FILE", "YIBAN_USERS_FILE",
                  "YIBAN_DB_FILE"):
            os.environ.pop(k, None)

    def setUp(self):
        _close_db()
        for suffix in ("", "-wal", "-shm"):
            p = self.db_file + suffix
            if os.path.exists(p):
                os.remove(p)
        with io.open(self.accounts_file, "w", encoding="utf-8") as f:
            json.dump([], f)
        db.init_db(self.db_file, migrate_from=self.accounts_file, env_file=self.env_file)
        _clear_caches()
        self._reset_state()
        self.alerts = []
        if self.PATCH_NOTIFY:
            p = mock.patch.object(
                self.webapp, "send_notification",
                side_effect=lambda t, c, urgent=False: self.alerts.append((t, c, urgent)),
            )
            p.start()
            self.addCleanup(p.stop)

    # ---- 工具 ----
    def _reset_state(self):
        """每例整体复位 .env 与推送内部状态。

        只清 YIBAN_NOTIFY_ 前缀不够：用例会往同一个 .env 里追加
        YIBAN_ADMIN_DELETE_MAX / YIBAN_MAIL_ENABLE 等值，残留到下一例就把
        "第二次门禁应 400"跑成"429"（限速被前一例设成 1 次）。整体按种子重写。
        """
        _write_env(self.env_file, list(self.env_seed))
        for k in [k for k in os.environ if k.startswith("YIBAN_NOTIFY_")]:
            os.environ.pop(k, None)
        n = self.webapp.notify
        n._throttle_ts.clear()
        for led in n._LEDGERS.values():
            led["state"].update({"date": "", "count": 0})
            led["notice"].update({"pending": False, "notified": False, "warned": False})
        self.webapp._mail_alert_ts.clear()

    def _client(self):
        return self.webapp.create_app().test_client()

    def _login(self, c, username, password):
        r = c.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c.get("/api/me").get_json()["csrf_token"]

    def _csrf(self, token):
        return {"X-CSRF-Token": token}

    def _append_env(self, text):
        with io.open(self.env_file, "a", encoding="utf-8") as f:
            f.write(text)

    def _audit_rows(self, action):
        """按 action 读取审计行（dict 列表，id 升序）——钉住"痕迹确实落进审计链"。"""
        with db._conn_lock:
            conn = db.get_conn()
            rows = conn.execute(
                "SELECT id, username, action, target, detail FROM audit_logs "
                "WHERE action=? ORDER BY id", (action,),
            ).fetchall()
        return [dict(r) for r in rows]


class AlertChannelGateB14Test(_B14AlertGateBase):
    """P1-1 门禁：关闭/换钥必须二次鉴权 + 限速，纯数值改动不受影响。"""

    def test_mail_close_without_password_400_and_env_untouched(self):
        """无口令关闭邮件通道 → 400，且 .env 一个字节都没改、没有变更告警。

        这是活体复现的攻击链首步（拿到内置主管理员 Cookie 即可一步静默全部告警）。
        """
        c = self._client()
        t = self._login(c, "admin", ADMIN_PASS)
        # 快照必须在 create_app/登录之后取：启动会迁移管理员口令哈希并补 YIBAN_SECRET_KEY
        before = _read_env(self.env_file)
        for body in ({"enabled": False}, {"admin_notify": False}):
            with self.subTest(body=body):
                r = c.put("/api/mail-config", json=body, headers=self._csrf(t))
                self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
                self.assertIn("当前密码不正确", r.get_json()["error"])
        self.assertEqual(_read_env(self.env_file), before, "鉴权未通过不得留下任何写入")
        self.assertEqual(self.alerts, [], "被拒绝的关闭不应发出变更告警")

    def test_mail_close_wrong_password_alerts_on_third_try(self):
        """错口令：连续 3 次触发"高危操作二次鉴权失败告警"（与批次13 同阈值/同计数）。"""
        c = self._client()
        t = self._login(c, "admin", ADMIN_PASS)
        # 快照必须在 create_app/登录之后取：启动会迁移管理员口令哈希并补 YIBAN_SECRET_KEY
        before = _read_env(self.env_file)
        for _ in range(3):
            r = c.put("/api/mail-config",
                      json={"enabled": False, "confirm_password": "wrong-pass"},
                      headers=self._csrf(t))
            self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertEqual(_read_env(self.env_file), before)
        fails = [a for a in self.alerts if a[0] == "高危操作二次鉴权失败告警"]
        self.assertEqual(len(fails), 1, f"第 3 次失败应告警一次，实际 {self.alerts}")
        self.assertTrue(fails[0][2], "二次鉴权失败告警必须是 urgent（邮件通道正被攻击者盯着关）")

    def test_mail_close_with_password_200_alert_is_urgent(self):
        """对口令关闭 → 200 + 落盘 + 变更告警 urgent=True，文案写明关的是哪一路。"""
        c = self._client()
        t = self._login(c, "admin", ADMIN_PASS)
        r = c.put("/api/mail-config",
                  json={"enabled": False, "confirm_password": ADMIN_PASS},
                  headers=self._csrf(t))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertFalse(r.get_json()["enabled"])
        self.assertIn("YIBAN_MAIL_ENABLE=0", _read_env(self.env_file))
        title, content, urgent = self.alerts[-1]
        self.assertEqual(title, "邮件配置变更告警")
        self.assertTrue(urgent, "配置变更告警必须 urgent：开着「仅重要告警」时非紧急不推手机=致盲")
        self.assertIn("全局邮件通知：关闭", content)

    def test_mail_open_and_enable_need_no_password(self):
        """纯"开启"方向保持原成功路径：不带口令也 200（不得给正常操作加摩擦）。"""
        c = self._client()
        t = self._login(c, "admin", ADMIN_PASS)
        r = c.put("/api/mail-config", json={"enabled": True}, headers=self._csrf(t))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        r2 = c.put("/api/mail-config", json={"admin_notify": True}, headers=self._csrf(t))
        self.assertEqual(r2.status_code, 200, r2.get_data(as_text=True))
        env = _read_env(self.env_file)
        self.assertIn("YIBAN_MAIL_ENABLE=1", env)
        self.assertIn("YIBAN_MAIL_ADMIN_NOTIFY=1", env)

    def test_mail_invalid_second_flag_writes_nothing(self):
        """先全量校验再统一落盘：第二个字段非法时第一个字段也不得已写入。"""
        c = self._client()
        t = self._login(c, "admin", ADMIN_PASS)
        # 快照必须在 create_app/登录之后取：启动会迁移管理员口令哈希并补 YIBAN_SECRET_KEY
        before = _read_env(self.env_file)
        r = c.put("/api/mail-config",
                  json={"enabled": True, "admin_notify": "yes", "confirm_password": ADMIN_PASS},
                  headers=self._csrf(t))
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertEqual(_read_env(self.env_file), before, "校验失败必须零写入（不留半套配置）")

    def test_mail_close_rate_limited_second_429(self):
        """限速复用高危删除同一套计数：窗口内超限 → 429，且仍然零写入。"""
        self._append_env("YIBAN_ADMIN_DELETE_MAX=1\n")
        c = self._client()
        t = self._login(c, "admin", ADMIN_PASS)
        r = c.put("/api/mail-config",
                  json={"enabled": False, "confirm_password": ADMIN_PASS},
                  headers=self._csrf(t))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        r2 = c.put("/api/mail-config",
                   json={"admin_notify": False, "confirm_password": ADMIN_PASS},
                   headers=self._csrf(t))
        self.assertEqual(r2.status_code, 429, r2.get_data(as_text=True))
        self.assertIn("YIBAN_MAIL_ENABLE=0", _read_env(self.env_file), "第一次成功的写入须保留")
        self.assertNotIn("YIBAN_MAIL_ADMIN_NOTIFY=0", _read_env(self.env_file),
                         "被限速拒绝的第二次不得写入")

    def test_notify_close_and_swap_secret_need_password(self):
        """推送侧三类动作（关闭/清钥/换钥）都要口令；无口令 400 且零写入。"""
        c = self._client()
        t = self._login(c, "admin", ADMIN_PASS)
        self._append_env("YIBAN_NOTIFY_TYPE=serverchan\n")
        before = _read_env(self.env_file)
        for body in (
            {"type": ""},                                                # 关闭推送
            {"secret": ""},                                              # 清空密钥
            {"type": "serverchan", "secret": "SCT406257NEWNEWWWWWWWWW"},  # 换密钥
        ):
            with self.subTest(body=body):
                r = c.put("/api/notify-config", json=body, headers=self._csrf(t))
                self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
                self.assertIn("当前密码不正确", r.get_json()["error"])
        self.assertEqual(_read_env(self.env_file), before)

    def test_notify_close_with_password_200_alert_urgent(self):
        c = self._client()
        t = self._login(c, "admin", ADMIN_PASS)
        r = c.put("/api/notify-config", json={
            "type": "serverchan", "secret": "SCT406257TESTTESTTESTTEST",
            "confirm_password": ADMIN_PASS,
        }, headers=self._csrf(t))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(r.get_json()["enabled"])
        r2 = c.put("/api/notify-config",
                   json={"type": "", "confirm_password": ADMIN_PASS}, headers=self._csrf(t))
        self.assertEqual(r2.status_code, 200, r2.get_data(as_text=True))
        self.assertFalse(r2.get_json()["enabled"])
        env = _read_env(self.env_file)
        self.assertNotIn("YIBAN_NOTIFY_SECRET_ENC=", env, "关闭须连密钥一起清掉")
        title, content, urgent = self.alerts[-1]
        self.assertEqual(title, "消息推送配置变更告警")
        self.assertTrue(urgent)
        self.assertIn("通道：关闭", content)

    def test_notify_numeric_only_needs_no_password(self):
        """只改 cooldown/urgent_only/daily_max/urgent_daily_max → 无口令 200，也不占高危额度。"""
        self._append_env("YIBAN_ADMIN_DELETE_MAX=1\n")  # 额度只给 1 次：数值改动不得消耗它
        c = self._client()
        t = self._login(c, "admin", ADMIN_PASS)
        for body in ({"cooldown": 30}, {"urgent_only": True},
                     {"daily_max": 7}, {"urgent_daily_max": 2}):
            with self.subTest(body=body):
                r = c.put("/api/notify-config", json=body, headers=self._csrf(t))
                self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        env = _read_env(self.env_file)
        self.assertIn("YIBAN_NOTIFY_COOLDOWN=30", env)
        self.assertIn("YIBAN_NOTIFY_URGENT_ONLY=1", env)
        self.assertIn("YIBAN_NOTIFY_DAILY_MAX=7", env)
        self.assertIn("YIBAN_NOTIFY_URGENT_DAILY_MAX=2", env)
        # 数值改动不占额度 → 紧随其后的高危关闭仍应放行（而不是被限速 429）
        r = c.put("/api/notify-config",
                  json={"type": "", "confirm_password": ADMIN_PASS}, headers=self._csrf(t))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_notify_cooldown_zero_is_persisted_not_deleted(self):
        """cooldown 口径修正：0 显式落盘（原实现删键 → 回落默认 60，"关不掉节流"）。"""
        c = self._client()
        t = self._login(c, "admin", ADMIN_PASS)
        self._append_env("YIBAN_NOTIFY_COOLDOWN=30\n")
        r = c.put("/api/notify-config", json={"cooldown": 0}, headers=self._csrf(t))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        env = _read_env(self.env_file)
        self.assertIn("YIBAN_NOTIFY_COOLDOWN=0", env, "0 必须显式落盘，不能删键回落默认")
        self.assertEqual(self.webapp.notify.get_config()["cooldown"], 0)

    def test_notify_urgent_daily_max_write_rules(self):
        """追加 B：urgent_daily_max 与 daily_max 同规则——整数、0 显式落盘、非法 400、缺省不写。"""
        c = self._client()
        t = self._login(c, "admin", ADMIN_PASS)
        r = c.put("/api/notify-config", json={"urgent_daily_max": 0}, headers=self._csrf(t))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIn("YIBAN_NOTIFY_URGENT_DAILY_MAX=0", _read_env(self.env_file))
        self.assertIsNone(self.webapp.notify.get_config()["urgent_daily_remaining"],
                         "0=不限 → 剩余应为 None（get_config 口径）")
        r2 = c.put("/api/notify-config", json={"urgent_daily_max": -5}, headers=self._csrf(t))
        self.assertEqual(r2.status_code, 200, r2.get_data(as_text=True))  # max(0,…) 与 daily_max 一致
        self.assertIn("YIBAN_NOTIFY_URGENT_DAILY_MAX=0", _read_env(self.env_file))
        r3 = c.put("/api/notify-config", json={"urgent_daily_max": "many"}, headers=self._csrf(t))
        self.assertEqual(r3.status_code, 400, r3.get_data(as_text=True))
        # 不携带该字段时不写：只改 daily_max 不得动紧急账
        self._append_env("YIBAN_NOTIFY_URGENT_DAILY_MAX=9\n")
        r4 = c.put("/api/notify-config", json={"daily_max": 11}, headers=self._csrf(t))
        self.assertEqual(r4.status_code, 200, r4.get_data(as_text=True))
        self.assertIn("YIBAN_NOTIFY_URGENT_DAILY_MAX=9", _read_env(self.env_file))
        self.assertIn("YIBAN_NOTIFY_DAILY_MAX=11", _read_env(self.env_file))

    def test_notify_auth_failure_does_not_generate_key_into_env(self):
        """评审 ③：加密（含缺键时的 .env 写入）必须在闸门之后，鉴权失败 = 零写入。

        account_crypto.load_key 在环境变量与 .env 都没有 YIBAN_ACCOUNTS_KEY 时会
        "生成随机密钥并原子写回 .env"。原实现把加密放在二次鉴权之前，于是一个被拒绝
        的高危请求也会留下写盘痕迹（与报告"鉴权失败零写入"的表述相反）。
        """
        # 用"去掉 ACCOUNTS_KEY 的种子"重写 .env，并把环境变量与进程内密钥缓存一并清掉
        _write_env(self.env_file,
                   [ln for ln in self.env_seed if not ln.startswith("YIBAN_ACCOUNTS_KEY=")])
        self.addCleanup(_clear_caches)
        with mock.patch.dict(os.environ):
            os.environ.pop("YIBAN_ACCOUNTS_KEY", None)
            _clear_caches()
            c = self._client()
            t = self._login(c, "admin", ADMIN_PASS)
            before = _read_env(self.env_file)
            self.assertNotIn("YIBAN_ACCOUNTS_KEY=", before,
                             "前置条件不成立：启动/登录阶段已把密钥写进 .env，本用例失去意义")
            r = c.put("/api/notify-config",
                      json={"type": "serverchan", "secret": "SCT406257NOPW0000000000"},
                      headers=self._csrf(t))
            self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
            after = _read_env(self.env_file)
            self.assertEqual(after, before, "鉴权失败路径不得触碰任何写盘（含 .env 密钥生成）")
            self.assertNotIn("YIBAN_ACCOUNTS_KEY=", after, "缺键时不得被 load_key 顺手生成并落盘")
            self.assertNotIn("YIBAN_NOTIFY_SECRET_ENC=", after, "被拒绝的换钥不得写入密文")

    def test_mail_close_alert_label_distinguishes_which_flag(self):
        """评审 ⑤：关全局开关与只关主管理员个人接收危害面不同，告警文案必须分得清。

        标签只在"高危操作二次鉴权失败告警"正文里可见（对「<label>」连续 N 次口令验证
        失败），是运维判断"对方当时想拆哪一路报警器"的唯一线索。
        """
        for field, want, unwant in (
            ("enabled", "全局邮件通知", "主管理员个人接收"),
            ("admin_notify", "主管理员个人接收", "全局邮件通知"),
        ):
            with self.subTest(field=field):
                self.alerts.clear()  # 两个 subTest 共用记录列表，须各算各的
                c = self._client()  # _login_fails / 限速表都是 create_app 内的，需新会话
                t = self._login(c, "admin", ADMIN_PASS)
                for _ in range(3):  # 第 3 次失败触发告警（LOGIN_FAIL_NOTIFY）
                    r = c.put("/api/mail-config",
                              json={field: False, "confirm_password": "wrong-pass"},
                              headers=self._csrf(t))
                    self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
                fails = [a for a in self.alerts if a[0] == "高危操作二次鉴权失败告警"]
                self.assertEqual(len(fails), 1, f"应恰好告警一次，实际 {self.alerts}")
                self.assertIn(want, fails[0][1], f"告警文案须点明关的是{want}")
                self.assertNotIn(unwant, fails[0][1], "不得把另一路也写成被关闭")

    def test_audit_details_include_numeric_and_flag_changes(self):
        """评审 ⑥：两个配置端点的审计详情须含具体变更项（只有 type 时事后无法还原）。"""
        c = self._client()
        t = self._login(c, "admin", ADMIN_PASS)
        r = c.put("/api/notify-config", headers=self._csrf(t), json={
            "cooldown": 0, "daily_max": 7, "urgent_daily_max": 1, "urgent_only": True})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        rows = self._audit_rows("notify_config")
        self.assertTrue(rows, "notify_config 变更须留审计")
        detail = json.loads(rows[-1]["detail"])
        self.assertEqual(detail["type"], "off", "未提交 type → 按 off 记录（既有口径）")
        self.assertEqual(detail["cooldown"], 0, "数值项 0 也须入审计（正是「关节流」这次动作）")
        self.assertEqual(detail["daily_max"], 7)
        self.assertEqual(detail["urgent_daily_max"], 1)
        self.assertIs(detail["urgent_only"], True)
        r2 = c.put("/api/mail-config", headers=self._csrf(t),
                   json={"enabled": False, "confirm_password": ADMIN_PASS})
        self.assertEqual(r2.status_code, 200, r2.get_data(as_text=True))
        mail_detail = json.loads(self._audit_rows("mail_config")[-1]["detail"])
        self.assertEqual(mail_detail, {"enabled": False})

    def test_registered_admin_still_403_on_both_endpoints(self):
        """权限前置不变：注册管理员（非内置主管理员）连口令带上也仍是 403，且在鉴权之前。"""
        db.create_user("radmin@test.local",
                       self.webapp.generate_password_hash(ADMIN_PASS), role="admin")
        c = self._client()
        t = self._login(c, "radmin@test.local", ADMIN_PASS)
        before = _read_env(self.env_file)
        r = c.put("/api/mail-config",
                  json={"enabled": False, "confirm_password": ADMIN_PASS}, headers=self._csrf(t))
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        r2 = c.put("/api/notify-config",
                   json={"type": "", "confirm_password": ADMIN_PASS}, headers=self._csrf(t))
        self.assertEqual(r2.status_code, 403, r2.get_data(as_text=True))
        self.assertEqual(_read_env(self.env_file), before)


class HighRiskGateOrderB14Test(_B14AlertGateBase):
    """评审 ②：门禁顺序 = 先二次鉴权、通过之后才占用高危限速额度。

    旧顺序（先判后增再鉴权）下，一个只拿到 Cookie、不知道口令的被盗会话可以用
    错口令尝试把主管理员"删除 + 通道变更"预算（默认 5 次 / 60 秒）全部吃掉，
    反过来让合法运维每次高危操作都撞 429 —— 限速设施被反向用成了运维 DoS。
    口令暴力的防护本就由 _login_fails 承担（第 3 次告警、第 5 次锁定），
    额度只该被真实执行过的高危动作消耗。
    """

    def test_mail_wrong_password_tries_do_not_consume_budget(self):
        self._append_env("YIBAN_ADMIN_DELETE_MAX=1\n")  # 窗口内只允许 1 次高危动作
        c = self._client()
        t = self._login(c, "admin", ADMIN_PASS)
        for _ in range(2):
            r = c.put("/api/mail-config",
                      json={"enabled": False, "confirm_password": "wrong-pass"},
                      headers=self._csrf(t))
            self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertNotIn("YIBAN_MAIL_ENABLE=0", _read_env(self.env_file), "错口令不得关闭通道")
        # 关键：两次失败尝试没有吃掉那唯一一格额度 → 合法管理员的关闭仍应放行
        r2 = c.put("/api/mail-config",
                   json={"enabled": False, "confirm_password": ADMIN_PASS},
                   headers=self._csrf(t))
        self.assertEqual(r2.status_code, 200,
                         f"错口令尝试不得挤占高危额度（旧顺序此处为 429）："
                         f"{r2.get_data(as_text=True)}")
        self.assertIn("YIBAN_MAIL_ENABLE=0", _read_env(self.env_file))

    def test_mail_budget_still_enforced_after_auth(self):
        """反向保护：顺序调整不得放宽限速——口令正确但超额度，仍然 429 且零写入。"""
        self._append_env("YIBAN_ADMIN_DELETE_MAX=1\n")
        c = self._client()
        t = self._login(c, "admin", ADMIN_PASS)
        r = c.put("/api/mail-config",
                  json={"enabled": False, "confirm_password": ADMIN_PASS}, headers=self._csrf(t))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        r2 = c.put("/api/mail-config",
                   json={"admin_notify": False, "confirm_password": ADMIN_PASS},
                   headers=self._csrf(t))
        self.assertEqual(r2.status_code, 429, "占过额度的成功动作之后仍须按上限拦下")
        self.assertNotIn("YIBAN_MAIL_ADMIN_NOTIFY=0", _read_env(self.env_file))

    def test_batch_delete_wrong_password_does_not_consume_budget(self):
        """既有三处高危删除同口径（批次13）：错口令尝试不得挤占删除额度。"""
        self._append_env("YIBAN_ADMIN_DELETE_MAX=1\n")
        db.create_user("victim@test.local",
                       self.webapp.generate_password_hash(ADMIN_PASS))
        c = self._client()
        t = self._login(c, "admin", ADMIN_PASS)
        for _ in range(2):
            r = c.post("/api/users/batch", headers=self._csrf(t), json={
                "action": "delete", "emails": ["victim@test.local"],
                "confirm_password": "wrong-pass"})
            self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIsNotNone(db.find_user("victim@test.local"), "错口令不得删除用户")
        r2 = c.post("/api/users/batch", headers=self._csrf(t), json={
            "action": "delete", "emails": ["victim@test.local"],
            "confirm_password": ADMIN_PASS})
        self.assertEqual(r2.status_code, 200,
                         f"删除侧同样不得被错口令刷爆额度：{r2.get_data(as_text=True)}")
        self.assertIsNone(db.find_user("victim@test.local"),
                          "口令通过的这一次须真的执行（find_user 只返回未注销用户）")


class ChannelHealthReportB14Test(_B14AlertGateBase):
    """追加 A + brief 第 6 点：每日通道健康日报与"推送额度耗尽"补发。"""

    def test_health_report_states_both_channels_even_when_closed(self):
        """两条通道都被关掉时，日报仍须逐条写出"被关"——状态行不依赖被改配置本身。

        注：通道开/关按用例用 os.environ 显式控制——conftest 把 YIBAN_MAIL_ENABLE
        钉成了 "0"（防测试真实发信），而 mailer._get 是环境变量优先于 .env。
        """
        self._append_env("YIBAN_MAIL_ENABLE=0\n")
        c = self._client()
        self._login(c, "admin", ADMIN_PASS)
        with mock.patch.object(self.webapp.notify, "pop_exhaustion_notice",
                               return_value=["general"]) as pop, \
             mock.patch.dict(os.environ, {"YIBAN_MAIL_ENABLE": "0"}):
            self.webapp._send_channel_health_report()
        title, content, urgent = self.alerts[-1]
        self.assertEqual(title, "告警通道健康日报")
        self.assertTrue(urgent, "有通道处于 ⚠ 状态时日报必须按 urgent 发，"
                                "否则开着「仅重要告警」= 唯一活着的推送通道也不播报")
        self.assertIn("邮件通道：⚠ 已关闭", content)
        # 修复轮 2：「未配置」不再是正常文本。手机推送这路不存在本身就是致盲风险
        # （邮件一挂就零告警），旧写法把它当"一切正常"、又不带 ⚠，才让
        # "只关 admin_notify + 关闭推送" 这个组合变体躲过了全部痕迹。
        self.assertIn("推送通道：⚠ 未配置", content)
        self.assertIn("非紧急剩余", content)
        self.assertIn("紧急剩余", content)
        self.assertIn("额度今日已用尽", content)
        pop.assert_called_once()

    def test_health_report_not_urgent_on_healthy_day(self):
        """两条通道都**真的可用**的健康日：日报按非紧急发，不占用每日紧急额度。

        修复轮 1 ① 后"健康"的判据收紧为 mailer.is_enabled()：除 ENABLE=1 外还必须
        发件 USER 与授权码 PASS 齐全——只拨开关不配凭据属于"已开启但不可用"。
        """
        c = self._client()
        t = self._login(c, "admin", ADMIN_PASS)
        c.put("/api/mail-config", json={"enabled": True}, headers=self._csrf(t))
        c.put("/api/notify-config", json={
            "type": "serverchan", "secret": "SCT406257HEALTHYDAY00000",
            "confirm_password": ADMIN_PASS,
        }, headers=self._csrf(t))
        self.alerts.clear()
        with mock.patch.object(self.webapp.notify, "pop_exhaustion_notice", return_value=[]), \
             mock.patch.dict(os.environ, {"YIBAN_MAIL_ENABLE": "1",
                                          "YIBAN_MAIL_USER": "alert@test.local",
                                          "YIBAN_MAIL_PASS": "smtp-auth-code-fake"}):
            # conftest 把 YIBAN_MAIL_ENABLE 钉成 0（防真实发信），环境变量优先于 .env，
            # 故此处显式覆盖才能构造"两条通道都健康"的日报场景（SMTP 出口由上面的
            # 假凭据 + 全 mock 保证不会真发）
            self.webapp._send_channel_health_report()
        _title, content, urgent = self.alerts[-1]
        self.assertFalse(urgent, f"例行日报每天吃掉一格紧急额度，反而会把真紧急告警挤出预算；"
                                 f"实际日报正文：\n{content}")
        self.assertNotIn("⚠", content)
        self.assertIn("邮件通道：已开启", content)
        self.assertIn("推送通道：已开启", content)

    def test_health_report_flags_unusable_channel(self):
        """配了类型却解不出密钥（批次14 P2-2 病症）→ 日报须标"已配置但不可用"。"""
        self._append_env("YIBAN_NOTIFY_TYPE=serverchan\n")
        lines = "\n".join(self.webapp._channel_status_lines())
        self.assertIn("已配置但不可用", lines)
        self.assertIn("邮件通道：", lines)

    def test_health_report_flags_mail_enabled_but_unusable(self):
        """评审 ①：ENABLE=1 但发件邮箱/授权码缺失 → 日报不得报"已开启"（误报一切正常）。

        真正的可用性判据是 mailer.is_enabled()（还要求 USER 与 PASS 都在），缺任一项
        mailer._send 就静默跳过、一封都不发；此时必须与推送侧同口径输出 degraded。
        """
        self._append_env("YIBAN_MAIL_ENABLE=1\n")
        with mock.patch.dict(os.environ, {"YIBAN_MAIL_ENABLE": "1",
                                          "YIBAN_MAIL_USER": "alert@test.local"}), \
             mock.patch.object(self.webapp.notify, "pop_exhaustion_notice", return_value=[]):
            os.environ.pop("YIBAN_MAIL_PASS", None)  # 刻意缺授权码：开关开 = 发不出去
            self.assertTrue(self.webapp._send_channel_health_report())
        _title, content, urgent = self.alerts[-1]
        self.assertIn("邮件通道：⚠ 已开启但不可用", content)
        self.assertNotIn("邮件通道：已开启（", content, "不得把「发不出去」报成一切正常")
        self.assertTrue(urgent, "这种状态属降级：邮件通道实际已死，日报必须仍能推手机")

    def test_health_report_sent_at_most_once_per_day_across_restart(self):
        """评审 ④a：日报去重必须落在库内 app_meta，而不是进程内状态。

        每日线程在启动 60 秒后即跑第一轮：状态若在内存，每次重启都会各发一封外发邮件
        （_mail_alert_due 的 300s 窗口重启即失效，兜不住）。本用例不靠 sleep 改日期——
        第二次调用即"进程重启后读同一份库"（去重判定的唯一数据来源就是 app_meta）。
        """
        today = self.webapp.datetime.now().strftime("%Y-%m-%d")
        with mock.patch.object(self.webapp.notify, "pop_exhaustion_notice", return_value=[]):
            self.assertTrue(self.webapp._send_channel_health_report())
            self.assertEqual(len(self.alerts), 1)
            self.webapp._mail_alert_ts.clear()  # 抹掉所有进程内状态，模拟重启
            self.assertFalse(self.webapp._send_channel_health_report(),
                             "同一日第二次调用必须跳过（今日已播）")
        self.assertEqual(len(self.alerts), 1, "跨重启同日只允许一封外发日报")
        # 标记确实在磁盘上：另开一条独立连接读 app_meta
        conn = sqlite3.connect(self.db_file)
        try:
            row = conn.execute(
                "SELECT value FROM app_meta WHERE key='channel_health_last'").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, "去重标记必须落库（进程内 dict 重启即失效）")
        self.assertEqual(json.loads(row[0])["date"], today)
        # 换到次日：不 sleep，直接把标记写成"历史日期"，日报应重新播报
        db.set_meta("channel_health_last", json.dumps({"date": "2000-01-01"}))
        with mock.patch.object(self.webapp.notify, "pop_exhaustion_notice", return_value=[]):
            self.assertTrue(self.webapp._send_channel_health_report())
        self.assertEqual(len(self.alerts), 2, "换了日子就该重新播报，不能永久静默")

    def test_health_report_leaves_audit_trail_when_delivery_impossible(self):
        """评审 ④b：两条通道同时被拆、日报本身发不出去时，仍必须留下本地痕迹。

        没有痕迹就无法在事后证明"系统曾检测到通道被拆"，攻击者的拔线与运维正常停机
        在取证上完全同形。痕迹刻意用 db.audit（进 HMAC 链、被库外锚点覆盖）而不是
        只写 app_meta，且落在尝试发信**之前**——send_notification 抛错也吞不掉它。
        """
        c = self._client()
        t = self._login(c, "admin", ADMIN_PASS)
        # 活体攻击链本身：两步把两条告警出口都拆掉（门禁是第一层，本例验第二层痕迹）
        self.assertEqual(c.put("/api/mail-config", headers=self._csrf(t), json={
            "enabled": False, "confirm_password": ADMIN_PASS}).status_code, 200)
        self.assertEqual(c.put("/api/notify-config", headers=self._csrf(t), json={
            "type": "", "confirm_password": ADMIN_PASS}).status_code, 200)
        self.alerts.clear()
        with mock.patch.object(self.webapp.notify, "pop_exhaustion_notice", return_value=[]), \
             mock.patch.object(self.webapp, "send_notification",
                               side_effect=RuntimeError("模拟两条通道同时失效")), \
             self.assertRaises(RuntimeError):
            self.webapp._send_channel_health_report()
        rows = self._audit_rows("channel_health")
        self.assertEqual(len(rows), 1, "降级痕迹必须落审计（否则发不出去的日报等于没发生）")
        self.assertEqual(rows[0]["username"], "system", "系统自检留痕，不得伪装成管理员操作")
        # 修复轮 2：detail 不再"只拼正文里含 ⚠ 的行"，而是无条件把两侧结构化事实各记
        # 一段（旧写法在双通道全断时只剩邮件侧，推送侧被关这个事实直接丢掉）
        self.assertIn("degraded=1", rows[0]["detail"])
        self.assertIn("邮件通道=已关闭", rows[0]["detail"])
        self.assertIn("推送通道=未配置", rows[0]["detail"], "痕迹必须同时记到推送侧")
        ok, broken, _first = db.verify_audit_chain()
        self.assertTrue(ok, f"痕迹须被既有审计链覆盖（可校验、不可静默删改），broken={broken}")

    def test_degraded_not_driven_by_warning_glyph_in_body(self):
        """安全判定不得靠"正文里有没有 ⚠ 字符"（修复轮 2 复评 Important-1）。

        模拟一次纯文案改版：把日报正文里所有 ⚠ 拿掉，通道事实不变。降级判定若
        仍正确（urgent + 落审计），说明它读的是结构化状态；旧写法
        （`degraded = any("⚠" in ln for ln in lines)`）在这里会直接判成"一切正常"，
        既不 urgent 也不落一条痕迹。
        """
        self._append_env("YIBAN_MAIL_ENABLE=0\n")
        real_lines = self.webapp._channel_status_lines

        def _reworded(status=None):  # 文案改版：去掉 ⚠，语义一字未动
            return [ln.replace("⚠ ", "") for ln in real_lines(status)]

        with mock.patch.object(self.webapp.notify, "pop_exhaustion_notice", return_value=[]), \
             mock.patch.object(self.webapp, "_channel_status_lines", side_effect=_reworded):
            self.assertTrue(self.webapp._send_channel_health_report())
        _title, content, urgent = self.alerts[-1]
        self.assertNotIn("⚠", content, "前置条件：本次日报正文确实一个 ⚠ 都没有")
        self.assertTrue(urgent, "正文没有 ⚠ 不等于通道健康：判定必须来自结构化状态")
        rows = self._audit_rows("channel_health")
        self.assertEqual(len(rows), 1, "落不落痕迹同样不得依赖正文字符")
        self.assertIn("邮件通道=已关闭", rows[0]["detail"])
        self.assertIn("推送通道=未配置", rows[0]["detail"])

    def test_audit_detail_records_both_sides_even_when_one_healthy(self):
        """痕迹摘要必须"两侧都记"（修复轮 2）：另一路当时是不是好的也得在案。

        只拆推送、邮件保持可用（且确有收件人）：旧写法只拼含 ⚠ 的行，detail 里
        邮件侧整段缺席，事后无法回答"坏的是哪一路"。

        修复轮 3 场景迁移（改的是构造方式，不是断言强度）：推送侧由"配好后走设置页
        整个关闭"换成"配好后清掉密钥、type 留着"。前者会把 YIBAN_NOTIFY_TYPE 与
        YIBAN_NOTIFY_SECRET_ENC 两个键行一并删掉，在新口径下与"从未启用手机推送"同形
        ⇒ 不再挂降级旗标（该动作当时已有 notify_config 审计 + urgent 播报）；后者 .env
        仍留一个非空 type ⇒ 属"曾配置而现在不可用"= 降级条件 (c)，痕迹照旧必须落。
        """
        c = self._client()
        t = self._login(c, "admin", ADMIN_PASS)
        c.put("/api/mail-config", json={"enabled": True}, headers=self._csrf(t))
        self.assertEqual(c.put("/api/notify-config", headers=self._csrf(t), json={
            "type": "serverchan", "secret": "SCT406257HEALTHYMAIL000",
            "confirm_password": ADMIN_PASS}).status_code, 200)
        self.assertEqual(c.put("/api/notify-config", headers=self._csrf(t), json={
            "type": "serverchan", "confirm_password": ADMIN_PASS}).status_code, 200)
        self.assertIn("YIBAN_NOTIFY_TYPE=serverchan", _read_env(self.env_file),
                      "前置：清钥后 .env 仍有非空 type ⇒ 属「曾配置被拆」而不是从未配置")
        self.assertNotIn("YIBAN_NOTIFY_SECRET_ENC=", _read_env(self.env_file))
        self.alerts.clear()
        with mock.patch.object(self.webapp.notify, "pop_exhaustion_notice", return_value=[]), \
             mock.patch.dict(os.environ, {"YIBAN_MAIL_ENABLE": "1",
                                          "YIBAN_MAIL_USER": "alert@test.local",
                                          "YIBAN_MAIL_PASS": "smtp-auth-code-fake"}):
            self.webapp._send_channel_health_report()
        rows = self._audit_rows("channel_health")
        self.assertEqual(len(rows), 1, "推送侧曾配置却被清钥属降级，必须落痕迹")
        detail = rows[0]["detail"]
        self.assertIn("degraded=1", detail)
        self.assertIn("邮件通道=可用", detail, "健康的另一路也要落痕，否则无法定位坏的是哪路")
        self.assertIn("收件人1", detail)
        self.assertIn("推送通道=已配置但不可用", detail)

    def test_dedupe_marker_written_only_after_successful_send(self):
        """修复轮 2 Minor：发信抛异常（SMTP 瞬断等）不得把当天日报一起吞掉。

        旧顺序是"先落今日已播标记 → 再发信"：一次瞬断就让当天彻底静默到明天，
        与实现取向"宁可多播不少播"自相矛盾。现改为发信成功后才落标记——
        "跨重启至多一封"的目标不变（成功即落），但失败那一次不占名额，当日稍后
        （进程重启后的下一轮 / 人工补发）仍可重试。
        """
        meta_key = self.webapp._HEALTH_REPORT_META_KEY
        with mock.patch.object(self.webapp.notify, "pop_exhaustion_notice", return_value=[]), \
             mock.patch.object(self.webapp, "send_notification",
                               side_effect=RuntimeError("模拟 SMTP 瞬断")), \
             self.assertRaises(RuntimeError):
            self.webapp._send_channel_health_report()
        self.assertEqual(db.get_meta(meta_key, ""), "", "发信失败不得落去重标记")
        # 当日稍后重试：仍应排出一封，成功后才补上标记
        with mock.patch.object(self.webapp.notify, "pop_exhaustion_notice", return_value=[]):
            self.assertTrue(self.webapp._send_channel_health_report())
        self.assertEqual(len(self.alerts), 1, "失败那一次不占名额，重试须真的再发一封")
        self.assertTrue(json.loads(db.get_meta(meta_key, ""))["date"], "成功后标记照旧落库")
        self.webapp._mail_alert_ts.clear()  # 抹掉进程内状态，模拟重启
        with mock.patch.object(self.webapp.notify, "pop_exhaustion_notice", return_value=[]):
            self.assertFalse(self.webapp._send_channel_health_report(),
                             "每日至多一封的既有语义不得放宽")

    def test_mail_only_deployment_push_never_configured_is_not_degraded(self):
        """修复轮 3：邮件单通道（推送从未配置）是合法终态，不得天天判降级。

        轮 2 把"推送未配置"一并计入降级，后果就是本用例钉住的反面：这样一套部署每天
        落一条 channel_health 降级痕迹、日报每天挂 urgent —— 天天喊降级就是告警疲劳，
        真出事时这条痕迹反而没人看。新口径下三件事必须同时成立：
          - 日报照旧每天一封（发送不以 degraded 为前提），只是不按 urgent 发；
          - 不降级 ≠ 看不见：正文照旧写清推送侧事实，app_meta.summary 两侧事实一字不少；
          - 不落降级审计痕迹（这正是要消掉的噪音）。
        """
        c = self._client()
        t = self._login(c, "admin", ADMIN_PASS)
        c.put("/api/mail-config", json={"enabled": True}, headers=self._csrf(t))
        self.assertNotIn("YIBAN_NOTIFY_TYPE=", _read_env(self.env_file),
                         "前置：种子 .env 里没有推送键 = 从未配置过手机推送")
        self.assertNotIn("YIBAN_NOTIFY_SECRET_ENC=", _read_env(self.env_file))
        self.alerts.clear()
        with mock.patch.object(self.webapp.notify, "pop_exhaustion_notice", return_value=[]), \
             mock.patch.dict(os.environ, {"YIBAN_MAIL_ENABLE": "1",
                                          "YIBAN_MAIL_USER": "alert@test.local",
                                          "YIBAN_MAIL_PASS": "smtp-auth-code-fake"}):
            status = self.webapp._alert_channel_status()
            self.assertTrue(status["mail_usable"], "前置：邮件这路确实可用")
            self.assertGreater(status["mail_recipients"], 0, "前置：确实有人可收")
            self.assertFalse(status["push_usable"])
            self.assertFalse(status["push_ever_configured"])
            self.assertFalse(
                self.webapp._channel_health_degraded(status),
                "邮件单通道不算降级：降级只回答「本应可用的出口现在不可用」")
            self.assertTrue(self.webapp._send_channel_health_report(),
                            "日报每天一封，不以降级为前提")
        title, content, urgent = self.alerts[-1]
        self.assertEqual(title, "告警通道健康日报")
        self.assertFalse(urgent, "不降级就别占用每日紧急额度（例行日报天天 urgent "
                                 "反而会挤出真紧急告警的预算）")
        self.assertIn("邮件通道：已开启", content)
        self.assertIn("推送通道：⚠ 未配置", content, "不降级不等于不告知：事实仍须看得见")
        self.assertEqual(self._audit_rows("channel_health"), [],
                         "刻意的终态配置不得每天落一条降级痕迹（告警疲劳）")
        meta = json.loads(db.get_meta(self.webapp._HEALTH_REPORT_META_KEY, ""))
        self.assertFalse(meta["degraded"])
        self.assertIn("邮件通道=可用", meta["summary"], "两侧事实的记录不因不降级而缺一侧")
        self.assertIn("推送通道=未配置", meta["summary"])
        self.assertIn("收件人1", meta["summary"])

    def test_push_ever_configured_requires_non_empty_env_value(self):
        """「曾配置」的唯一事实来源 = .env 两键存在**且值非空**（修复轮 3 的边界）。

        两键都不存在、或都在而值都为空/纯空白 ⇒ 从未配置（邮件单通道合法终态，不降级）；
        任一有值 ⇒ 曾配置，此后通道不可用就属"被拆"（条件 (c)）。刻意不新增 app_meta 键、
        不新建状态存储：设置页关闭通道会删掉两个键行，该动作当时已有 notify_config 审计
        行 + urgent 播报覆盖（批次14 P1-1 门禁），日报不再重复定性。
        """
        cases = (
            ("两键都不存在（种子态）", [], False),
            ("只有 type 有值", ["YIBAN_NOTIFY_TYPE=serverchan"], True),
            ("只有密文有值（type 已被清）", ['YIBAN_NOTIFY_SECRET_ENC={"v": 1}'], True),
            ("两键都在但值都为空", ["YIBAN_NOTIFY_TYPE=", "YIBAN_NOTIFY_SECRET_ENC="], False),
            ("值仅空白", ["YIBAN_NOTIFY_TYPE=   ", "YIBAN_NOTIFY_SECRET_ENC= "], False),
        )
        for label, lines, want in cases:
            with self.subTest(case=label):
                self._reset_state()
                self._append_env("".join(f"{ln}\n" for ln in lines))
                self.assertIs(self.webapp._push_ever_configured(), want)

    def test_ever_configured_with_undecryptable_secret_is_degraded(self):
        """曾配置（type + 密文都在而密文解不出）= 批次14 P2-2 病症 ⇒ 新口径不得豁免。

        与"从未配置"的唯一差别就是 .env 里那两个键还有没有值：这条若一并判成健康，
        "配过又被拆"（换钥后解不开 / 密文被截断）就重新退回静默，(c) 白写。
        """
        c = self._client()
        t = self._login(c, "admin", ADMIN_PASS)
        c.put("/api/mail-config", json={"enabled": True}, headers=self._csrf(t))
        # 密文按另一把账户密钥生成（.env 里是 OLD_KEY）⇒ 结构合法但解不开
        self._append_env("YIBAN_NOTIFY_TYPE=serverchan\n"
                         f"YIBAN_NOTIFY_SECRET_ENC={_notify_enc(NEW_KEY)}\n")
        self.alerts.clear()
        with mock.patch.object(self.webapp.notify, "pop_exhaustion_notice", return_value=[]), \
             mock.patch.dict(os.environ, {"YIBAN_MAIL_ENABLE": "1",
                                          "YIBAN_MAIL_USER": "alert@test.local",
                                          "YIBAN_MAIL_PASS": "smtp-auth-code-fake"}):
            status = self.webapp._alert_channel_status()
            self.assertTrue(status["mail_usable"] and status["mail_recipients"] > 0,
                            "前置：邮件侧健康，降级只可能来自推送侧")
            self.assertTrue(status["push_ever_configured"])
            self.assertTrue(status["push_configured"], "密文在 ⇒ 曾配置（不是从未配置）")
            self.assertFalse(status["push_usable"], "密文解不出 ⇒ 现在不可用")
            self.assertTrue(self.webapp._channel_health_degraded(status))
            self.webapp._send_channel_health_report()
        rows = self._audit_rows("channel_health")
        self.assertEqual(len(rows), 1, "曾配置而被拆必须落降级痕迹")
        self.assertIn("推送通道=已配置但不可用", rows[0]["detail"])
        _title, _content, urgent = self.alerts[-1]
        self.assertTrue(urgent, "这种降级必须按 urgent 发")

    def test_daily_loop_is_wired_to_health_report(self):
        """接线检查：日报调用确实挂在每日线程里（否则以上两条只是死代码）。"""
        src = inspect.getsource(self.webapp.create_app)
        self.assertIn("_send_channel_health_report()", src)
        self.assertIn("_daily_purge_loop", src)


class BothChannelsDeadCombinationVariantB14Test(_B14AlertGateBase):
    """修复轮 2 复评点名的组合变体（跑真实 send_notification，钉"一封都发不出去"）。

    场景 = 只关 YIBAN_MAIL_ADMIN_NOTIFY（全局开关与 SMTP 凭据都还在）+ 关闭手机
    推送 + 库里没有其他开启接收的管理员。此时 send_notification 的 recipients 为空
    → 邮件不发、推送不推；而修复轮 1 的实现里 degraded 由"正文含 ⚠"驱动，该变体
    两侧文案恰好都不含 ⚠ → degraded=False → 一条本地痕迹都不落，④b 正好落空。
    """

    PATCH_NOTIFY = False

    def test_admin_notify_off_plus_push_closed_still_leaves_full_audit_trail(self):
        c = self._client()
        t = self._login(c, "admin", ADMIN_PASS)
        # 邮件通道"看起来全绿"：全局开关开着、凭据齐备，只摘掉主管理员个人接收
        self.assertEqual(c.put("/api/mail-config", headers=self._csrf(t), json={
            "enabled": True}).status_code, 200)
        self.assertEqual(c.put("/api/mail-config", headers=self._csrf(t), json={
            "admin_notify": False, "confirm_password": ADMIN_PASS}).status_code, 200)
        # 推送侧关闭（type 置空，连密钥一起清）；库里没有注册管理员 → 无人可收
        self.assertEqual(c.put("/api/notify-config", headers=self._csrf(t), json={
            "type": "", "confirm_password": ADMIN_PASS}).status_code, 200)
        with mock.patch.dict(os.environ, {"YIBAN_MAIL_ENABLE": "1",
                                          "YIBAN_MAIL_USER": "alert@test.local",
                                          "YIBAN_MAIL_PASS": "smtp-auth-code-fake"}), \
             mock.patch.object(self.webapp.notify, "pop_exhaustion_notice", return_value=[]), \
             mock.patch.object(self.webapp.mailer, "send_admin_alert") as mail, \
             mock.patch.object(self.webapp.notify.requests, "post") as post:
            self.assertTrue(self.webapp._send_channel_health_report())
            status = self.webapp._alert_channel_status()
        self.assertEqual(status["mail_recipients"], 0, "前置：这封日报确实无人可收")
        mail.assert_not_called()  # 收件人为空 → 邮件这路一封都发不出去
        post.assert_not_called()  # 推送已关 → 也没有任何外联
        rows = self._audit_rows("channel_health")
        self.assertEqual(len(rows), 1,
                         "两条通道皆不可用时日报发不出去，但审计必须留痕（④b 该变体）")
        detail = rows[0]["detail"]
        self.assertIn("degraded=1", detail)
        self.assertIn("邮件通道=可用但收件人为空", detail)
        self.assertIn("收件人0", detail)
        self.assertIn("主管理员接收=否", detail)
        self.assertIn("推送通道=未配置", detail, "摘要必须完整记录两侧事实")
        meta = json.loads(db.get_meta(self.webapp._HEALTH_REPORT_META_KEY, ""))
        self.assertTrue(meta["degraded"])
        self.assertIn("推送通道=未配置", meta["summary"], "标记里的摘要同样要含推送侧")
        ok, broken, _first = db.verify_audit_chain()
        self.assertTrue(ok, f"痕迹须进既有哈希链，broken={broken}")


class ExhaustionNoticeWiringB14Test(_B14AlertGateBase):
    """追加 A：send_notification 里接线 pop_exhaustion_notice()（必须跑真实实现）。"""

    PATCH_NOTIFY = False

    def test_exhaustion_notice_pops_once_and_sends_one_mail(self):
        """一次 pop 拿全列表 → 一封邮件写两本账；不得循环 pop 到空（同日两封）。"""
        mails = []
        with mock.patch.object(self.webapp.notify, "pop_exhaustion_notice",
                               return_value=["general", "urgent"]) as pop, \
             mock.patch.object(self.webapp.notify, "send") as ns, \
             mock.patch.object(self.webapp.mailer, "send_admin_alert",
                               side_effect=lambda s, t, to=None: mails.append((s, t, to))), \
             mock.patch.object(self.webapp, "_mail_alert_due", return_value=True):
            self.webapp.send_notification("测试告警一", "正文一")
            ns.assert_called_once()
        titles = [m[0] for m in mails]
        self.assertEqual(titles.count("手机推送额度已用尽告警"), 1,
                         f"两本账同日只补一封，实际 {titles}")
        self.assertEqual(pop.call_count, 1, "每次告警最多 pop 一次（循环 pop 会同日发两封）")
        body = next(m[1] for m in mails if m[0] == "手机推送额度已用尽告警")
        self.assertIn("非紧急", body)
        self.assertIn("紧急", body)
        self.assertIn("YIBAN_NOTIFY_URGENT_DAILY_MAX", body, "须给出可操作的调整指引")
        # 收件人必须是 A 线同一批人（主管理员 + 开启接收的管理员）
        self.assertTrue(all(m[2] for m in mails), "耗尽告知须落到管理员收件人")

    def test_exhaustion_notice_absent_when_no_ledger_exhausted(self):
        """无耗尽（pop 返回空列表）→ 只有主告警一封，不多发。"""
        mails = []
        with mock.patch.object(self.webapp.notify, "pop_exhaustion_notice",
                               return_value=[]), \
             mock.patch.object(self.webapp.notify, "send"), \
             mock.patch.object(self.webapp.mailer, "send_admin_alert",
                               side_effect=lambda s, t, to=None: mails.append((s, t, to))), \
             mock.patch.object(self.webapp, "_mail_alert_due", return_value=True):
            self.webapp.send_notification("测试告警三", "正文三")
        self.assertEqual([m[0] for m in mails], ["测试告警三"])

    def test_exhaustion_notice_failure_does_not_break_alert(self):
        """notify 侧异常绝不影响主告警：pop 抛错时主告警邮件仍照常发出、函数不外抛。"""
        mails = []
        with mock.patch.object(self.webapp.notify, "pop_exhaustion_notice",
                               side_effect=RuntimeError("boom")), \
             mock.patch.object(self.webapp.notify, "send"), \
             mock.patch.object(self.webapp.mailer, "send_admin_alert",
                               side_effect=lambda s, t, to=None: mails.append((s, t, to))), \
             mock.patch.object(self.webapp, "_mail_alert_due", return_value=True):
            self.webapp.send_notification("测试告警二", "正文二")
        self.assertEqual([m[0] for m in mails], ["测试告警二"])


# ==================== Task 4：账号物理清除门禁 + PUT 防错位（P1-2 / P3-2）====================

# 注册管理员口令：刻意与内置主管理员口令不同，否则"各走各自验密路径"这条断言没有意义
REG_ADMIN_PASS = "RegAdmin#2026pw"


def _snapshot_of(acc):
    """按 db.update_account 的乐观锁口径拼账号指纹（name/phone/phone_model/status/deleted）。

    字段与比较逻辑必须同源：db 侧是逐键构造后整体 == 比较，多一个键或少一个键
    都会让"合法编辑"变成 409（web 端点把不匹配的快照判为并发冲突）。
    """
    return json.dumps({
        "name": acc.get("name", ""),
        "phone": acc.get("phone", ""),
        "phone_model": acc.get("phone_model", ""),
        "status": acc.get("status", ""),
        "deleted": bool(acc.get("deleted")),
    }, ensure_ascii=False)


class _B14AccountBase(_B14AlertGateBase):
    """Task 4 夹具：在 Task 3 的隔离 web 之上补账号造数与管理员会话。

    注意额度/失败计数（_admin_delete_limits、_login_fails）都是 create_app 内的
    app 级闭包状态——所以"连续第 N 次应 429"这类用例必须在同一个 client 里跑完，
    而每例一个新 client 又天然给了干净的额度（不必再手工清表）。
    """

    def _add_account(self, phone, name="A", deleted=False, owner="admin"):
        acc_id = db.add_account({"name": name, "phone": phone, "password": "pw-1",
                                 "status": "active", "owner": owner})
        if deleted:
            db.set_account_deleted(
                acc_id, 1,
                self.webapp.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                deleted_by="admin")
        return acc_id

    def _master(self):
        """内置主管理员会话（返回 client 与 CSRF 头）。"""
        c = self._client()
        t = self._login(c, "admin", ADMIN_PASS)
        return c, self._csrf(t)

    def _rows(self):
        """服务端视图下的账号列表（idx 口径与端点一致，password 已解密）。"""
        return self.webapp.load_accounts()

    def _titles(self):
        return [a[0] for a in self.alerts]


class AccountBatchPurgeGateB14Test(_B14AccountBase):
    """P1-2 之一：批量 purge 必须二次鉴权 + 限速；可逆动作不得顺带加门禁。"""

    def test_batch_purge_without_password_400_and_rows_intact(self):
        """活体复现的攻击请求：不带 confirm_password（也就不带 phones）的批量清除。

        改前：200 且整段跳过防错位校验（`{"action":"purge","ids":[0]*10}` 都能照发）；
        改后：400、一行凭据都没掉、且没有"操作成功"式告警（不伪造已处置）。
        """
        self._add_account("13800138001", name="A1", deleted=True)
        self._add_account("13800138002", name="A2", deleted=True)
        c, h = self._master()
        r = c.post("/api/accounts/batch", json={"action": "purge", "ids": [0, 1]}, headers=h)
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn("当前密码不正确", r.get_json()["error"])
        rows = self._rows()
        self.assertEqual(len(rows), 2, "鉴权未通过不得物理清除任何易班凭据")
        self.assertTrue(all(a.get("deleted") for a in rows))
        self.assertEqual(self.alerts, [], "被拒绝的清除不应发出任何高危操作告警")

    def test_batch_purge_with_password_200_rows_gone_and_urgent_alert(self):
        """口令正确 → 200、行真的消失、并补一条 urgent 高危告警（正文含脱敏号）。"""
        self._add_account("13800138001", name="A1", deleted=True)
        self._add_account("13800138002", name="A2", deleted=True)
        phones = [a["phone"] for a in self._rows()]
        c, h = self._master()
        r = c.post("/api/accounts/batch",
                   json={"action": "purge", "ids": [0, 1], "phones": phones,
                         "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self._rows(), [], "彻底删除应物理清除行")
        self.assertEqual(self._titles().count("高危管理操作告警"), 1, f"实际告警 {self.alerts}")
        title, content, urgent = self.alerts[-1]
        self.assertEqual(title, "高危管理操作告警")
        self.assertTrue(urgent, "物理清除不可逆，必须推手机（非紧急在「仅重要告警」下不送达）")
        self.assertIn("批量彻底删除账号 ×2", content)
        self.assertIn(self.webapp._mask_phone(phones[0]), content)

    def test_batch_purge_gate_does_not_displace_stale_phones_409(self):
        """门禁不得顶掉既有防错位：口令对了、phones 与列表漂移，仍应 409 而非 200。"""
        self._add_account("13800138001", name="A1", deleted=True)
        self._add_account("13800138002", name="A2", deleted=True)
        c, h = self._master()
        r = c.post("/api/accounts/batch",
                   json={"action": "purge", "ids": [0, 1], "phones": ["13900139000", "13900139001"],
                         "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        self.assertEqual(len(self._rows()), 2, "409 后不得有任何物理清除")

    def test_batch_soft_delete_and_restore_still_need_no_password(self):
        """反向保护：软删/恢复是可逆动作，不得被本次改动顺带要求口令。"""
        self._add_account("13800138001", name="A1")
        self._add_account("13800138002", name="A2")
        phones = [a["phone"] for a in self._rows()]
        c, h = self._master()
        r = c.post("/api/accounts/batch",
                   json={"action": "delete", "ids": [0, 1], "phones": phones}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(all(a.get("deleted") for a in self._rows()))
        r2 = c.post("/api/accounts/batch",
                    json={"action": "restore", "ids": [0, 1], "phones": phones}, headers=h)
        self.assertEqual(r2.status_code, 200, r2.get_data(as_text=True))
        self.assertFalse(any(a.get("deleted") for a in self._rows()))
        self.assertEqual(self.alerts, [], "可逆动作既不要求口令，也不该触发高危告警")

    def test_batch_param_errors_still_checked_before_gate(self):
        """既有 400 校验优先级不变：未知动作/空选择/超上限都不该被改成交给门禁判。"""
        c, h = self._master()
        cap = self.webapp.BATCH_OP_LIMIT
        for body in ({"action": "nope", "ids": [0]},
                     {"action": "purge", "ids": []},
                     {"action": "purge", "ids": list(range(cap + 1))}):
            with self.subTest(body=body):
                r = c.post("/api/accounts/batch", json=body, headers=h)
                self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
                self.assertNotIn("密码", r.get_json()["error"], "应先报参数错，不该先要口令")


class AccountSinglePurgeGateB14Test(_B14AccountBase):
    """P1-2 之二：单条 purge 二次鉴权 + 限速 + 即时 urgent 告警（此前一处都没有）。"""

    def _one_deleted(self, phone="13800138001", name="A1"):
        self._add_account(phone, name=name, deleted=True)
        return phone

    def test_single_purge_without_password_400_row_intact(self):
        self._one_deleted()
        c, h = self._master()
        r = c.post("/api/accounts/0/purge", json={"phone": "13800138001"}, headers=h)
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertEqual(len(self._rows()), 1, "无口令的物理清除必须被挡住且不动数据")
        self.assertEqual(self.alerts, [])

    def test_single_purge_with_password_200_and_urgent_alert(self):
        """改前实测：连发多条单条 purge 全部 200 且零告警（比批量更安静，破坏无痕）。"""
        self._one_deleted()
        c, h = self._master()
        r = c.post("/api/accounts/0/purge",
                   json={"phone": "13800138001", "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self._rows(), [])
        self.assertEqual(self._titles(), ["高危管理操作告警"])
        _title, content, urgent = self.alerts[-1]
        self.assertTrue(urgent, "单条物理清除同样不可逆，必须 urgent 送达")
        self.assertIn(self.webapp._mask_phone("13800138001"), content)
        self.assertIn("操作者 admin", content)
        self.assertEqual(len(self._audit_rows("account_purge")), 1, "审计留痕不得少")

    def test_purge_alert_title_matches_batch_so_throttle_window_is_shared(self):
        """单条与批量必须同标题：send_notification 的邮件节流按标题计窗（_mail_alert_due），

        同标题才共享窗口——被盗会话快速连删不会被刷爆 SMTP 额度，
        合法运维的批量清理也只留一封。标题各写一份就会两路各计一窗。
        """
        self._add_account("13800138001", name="A1", deleted=True)
        self._add_account("13800138002", name="A2", deleted=True)
        self._add_account("13800138003", name="A3", deleted=True)
        c, h = self._master()
        rb = c.post("/api/accounts/batch",
                    json={"action": "purge", "ids": [0, 1],
                          "phones": ["13800138001", "13800138002"],
                          "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(rb.status_code, 200, rb.get_data(as_text=True))
        rs = c.post("/api/accounts/0/purge",
                    json={"phone": "13800138003", "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(rs.status_code, 200, rs.get_data(as_text=True))
        self.assertEqual(len(self.alerts), 2, f"实际 {self.alerts}")
        self.assertEqual({t for t, _c, _u in self.alerts}, {"高危管理操作告警"},
                         "两次清除的告警标题必须完全一致，否则节流窗口各算一份")
        self.assertTrue(all(u for _t, _c, u in self.alerts))

    def test_sixth_purge_in_window_hits_cooldown_429(self):
        """默认额度（5 次 / 60 秒）下第 6 次物理清除应 429，且第 6 条凭据仍在。"""
        phones = [f"1380013800{i}" for i in range(1, 7)]
        for p in phones:
            self._add_account(p, name="X", deleted=True)
        c, h = self._master()
        for k in range(5):
            r = c.post("/api/accounts/0/purge",
                       json={"phone": phones[k], "confirm_password": ADMIN_PASS}, headers=h)
            self.assertEqual(r.status_code, 200, f"第 {k + 1} 次应放行：{r.get_data(as_text=True)}")
        self.assertEqual(len(self._rows()), 1, "前 5 次应已各清除一条")
        r6 = c.post("/api/accounts/0/purge",
                    json={"phone": phones[5], "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r6.status_code, 429, r6.get_data(as_text=True))
        self.assertIn("删除操作过于频繁", r6.get_json()["error"])
        self.assertEqual([a["phone"] for a in self._rows()], [phones[5]],
                         "429 时最后一条凭据必须原样保留（改前此处 200 且无痕）")

    def test_wrong_password_tries_do_not_consume_purge_budget(self):
        """批次14 评审 ② 口径延伸到账号侧：错口令尝试不得吃掉合法运维的额度。"""
        self._append_env("YIBAN_ADMIN_DELETE_MAX=1\n")
        self._one_deleted()
        c, h = self._master()
        for _ in range(2):
            r = c.post("/api/accounts/0/purge",
                       json={"phone": "13800138001", "confirm_password": "wrong-pass"}, headers=h)
            self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertEqual(len(self._rows()), 1)
        r2 = c.post("/api/accounts/0/purge",
                    json={"phone": "13800138001", "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(
            r2.status_code, 200,
            f"错口令尝试不得挤占高危额度（旧顺序此处 429）：{r2.get_data(as_text=True)}")

    def test_registered_admin_purges_on_own_password_path(self):
        """权限口径不放宽也不收紧：注册管理员仍可清除账号，但只能用自己的口令。

        _reconfirm_admin_password 对内置主管理员走 verify_admin(.env)、对注册用户
        走 users 表 password_hash；两条路径不得互相顶用。
        """
        db.create_user("radmin2@test.local",
                       self.webapp.generate_password_hash(REG_ADMIN_PASS), role="admin")
        self._one_deleted()
        c = self._client()
        t = self._login(c, "radmin2@test.local", REG_ADMIN_PASS)
        h = self._csrf(t)
        r = c.post("/api/accounts/0/purge",
                   json={"phone": "13800138001", "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertEqual(len(self._rows()), 1, "拿主管理员口令冒用注册管理员会话不得清除成功")
        r2 = c.post("/api/accounts/0/purge",
                    json={"phone": "13800138001", "confirm_password": REG_ADMIN_PASS}, headers=h)
        self.assertEqual(r2.status_code, 200, r2.get_data(as_text=True))
        self.assertEqual(self._rows(), [])


class AccountUpdateStaleIdxB14Test(_B14AccountBase):
    """P3-2：PUT /api/accounts/<idx> 补 _stale_idx_guard（同族写端点里唯一漏接的一个）。"""

    def test_put_after_target_row_purged_blocked_409(self):
        """活体复现的主用例：目标行被物理清除后，旧列表的 idx 已指向另一账号。

        改前：此请求静默改写现在排在 idx0 的 B 并返回 200（对照同 idx 的
        /restore 正确返回 409）；改后：409 引导刷新，B 的凭据一字未动。
        """
        self._add_account("13800138000", name="A", deleted=True)
        self._add_account("13900139000", name="B")
        stale_phone = self._rows()[0]["phone"]
        c, h = self._master()
        rp = c.post("/api/accounts/0/purge",
                    json={"phone": stale_phone, "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(rp.status_code, 200, rp.get_data(as_text=True))
        r = c.put("/api/accounts/0",
                  json={"name": "HIJACK", "phone": stale_phone, "password": ""}, headers=h)
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["error"], "账号列表已变化，请刷新页面后重试",
                         "409 文案必须与 /restore /review /move 等同族端点逐字一致")
        self.assertEqual([a["name"] for a in self._rows()], ["B"], "B 不得被旧 idx 的编辑改写")

    def test_put_with_mismatched_phone_409_no_write(self):
        self._add_account("13800138000", name="A")
        self._add_account("13900139000", name="B")
        c, h = self._master()
        r = c.put("/api/accounts/0",
                  json={"name": "HIJACK", "phone": "13900139000", "password": ""}, headers=h)
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        self.assertEqual([a["name"] for a in self._rows()], ["A", "B"], "409 后不得有任何改写")

    def test_put_without_phone_keeps_previous_behavior(self):
        """红线：守卫只在 data 带 phone 时生效，不带 phone 的旧客户端行为不得改变。

        不带 phone 仍由 validate_account 报"手机号为必填项"（400）——关键是不得被
        新守卫改判成 409（那等于把兼容路径顺手改成 fail-closed）。
        """
        self._add_account("13800138000", name="A")
        c, h = self._master()
        r = c.put("/api/accounts/0", json={"name": "A2"}, headers=h)
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["error"], "手机号为必填项")
        self.assertEqual(self._rows()[0]["name"], "A")

    def test_put_with_matching_phone_still_200(self):
        """浏览器真实路径（表单回带完整手机号）：对齐的 phone 必须放行，不得恒 409。"""
        self._add_account("13800138000", name="A")
        c, h = self._master()
        r = c.put("/api/accounts/0",
                  json={"name": "A2", "phone": "13800138000", "password": ""}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self._rows()[0]["name"], "A2")

    def test_put_rebind_new_phone_with_snapshot_still_200(self):
        """改绑手机号是合法编辑（表单提示"如需修改请填写完整新号码"，db 侧还专门重加密）。

        故比对基准优先取乐观锁快照里的 phone：快照 = 打开表单时的视图指纹，
        它与服务端当前行一致就说明没漂移，此时 data["phone"] 是"要改成的新值"。
        直接拿 data["phone"] 比会把这条路径全部打死。
        """
        self._add_account("13800138000", name="A")
        snap = _snapshot_of(self._rows()[0])
        c, h = self._master()
        r = c.put("/api/accounts/0",
                  json={"name": "A", "phone": "13900139000", "password": "",
                        "_snapshot": snap}, headers=h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self._rows()[0]["phone"], "13900139000")

    def test_put_rebind_new_phone_without_snapshot_409(self):
        """偏离的边界刻意钉住：没有快照可作"原始身份"基准时退回 data["phone"] 比对 → 409。

        前端已保证编辑态必带快照（快照取不到即禁提交），受影响面只有手搓请求；
        宁可 409 引导刷新，也不放开"旧 idx 静默改写他人凭据"。
        """
        self._add_account("13800138000", name="A")
        c, h = self._master()
        r = c.put("/api/accounts/0",
                  json={"name": "A", "phone": "13900139000", "password": ""}, headers=h)
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        self.assertEqual(self._rows()[0]["phone"], "13800138000")

    def test_put_stale_snapshot_of_purged_row_blocked_409(self):
        """漂移后旧快照的 phone 与当前行不一致 → 守卫先 409（原先靠 db 乐观锁兜同一件事）。"""
        self._add_account("13800138000", name="A", deleted=True)
        self._add_account("13900139000", name="B")
        stale = self._rows()[0]
        stale_snap = _snapshot_of(stale)
        c, h = self._master()
        rp = c.post("/api/accounts/0/purge",
                    json={"phone": stale["phone"], "confirm_password": ADMIN_PASS}, headers=h)
        self.assertEqual(rp.status_code, 200, rp.get_data(as_text=True))
        r = c.put("/api/accounts/0",
                  json={"name": "HIJACK", "phone": "13900139000", "password": "",
                        "_snapshot": stale_snap}, headers=h)
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        self.assertEqual(self._rows()[0]["name"], "B", "B 的账号名不得被旧快照编辑改写")


# ==================== Task 5：登录/登出成功留痕（PROD-2 取证缺口）====================

TRAIL_PASS = "TrailPass#2026"


class LoginTrailB14Test(_B14AlertGateBase):
    """批次14/PROD-2：成功登录与登出成对留痕，三元组与 forbidden_path 逐字同构。

    登录成功审计自批次7 A6 起就写在两条成功分支的 `if role:` 汇合点上，动作名 login；
    本组用例钉的是 PROD-2 补齐后的口径——登出端与恢复入口此前一行留痕都没有（audit_logs
    里 logout 类动作 0 条、恢复即登录只留 user_self_delete_restore），且登录侧动作名与
    login_failed 不成组。现网查不到 login_ok 只说明改动上线后还没人重新走过 /api/login，
    不代表旧库里没有登录行——那些行的动作名是 login，跨版本取证须
    IN ('login','login_ok')。本组用例钉住补齐后的六条口径：
      ① 两条成功分支（内置管理员走 .env 口令/哈希、注册用户走 users.password_hash）
        各留一行 login_ok，注册管理员同样在内；
      ② 恢复即登录（/api/me/restore）同样留 login_ok，detail 用「恢复登录」区分入口，
        且既有 sid 签发/吊销语义一字未动；
      ③ 登出留 logout_ok，且重复登出（会话已清空）不得再产生第二行；
      ④ target 只存 IP 的 HMAC（64 位 hex）、username 长度 ≤64、整行不含口令/CSRF/sid、
        不含明文 IP、不含 User-Agent；
      ⑤ 失败登录一行 login_ok 都不产生（失败侧已有 login_failed 阈值留痕与锁定）；
      ⑥ 写入这些行后 HMAC 链仍自洽。
    """

    def _user(self, email="trail-user@test.local", password=TRAIL_PASS, role="user"):
        db.create_user(email, self.webapp.generate_password_hash(password), role=role)
        return email, password

    @staticmethod
    def _row_text(row):
        return "".join(str(v) for v in row.values())

    def _assert_triple(self, row, want_username):
        """三类留痕共用的口径断言（与 forbidden_path 同构）：用户名、匿名 target、长度上限。"""
        self.assertEqual(row["username"], want_username)
        self.assertLessEqual(len(row["username"]), 64, "username 必须截断到 64")
        self.assertRegex(row["target"], r"^[0-9a-f]{64}$",
                         "target 必须是 hash_ip 的 HMAC 输出，不落明文 IP")
        self.assertNotIn("127.0.0.1", self._row_text(row), "整行都不得出现明文 IP")

    # ---- ① 成功登录 ----
    def test_registered_user_login_writes_single_login_ok(self):
        email, pw = self._user()
        c = self._client()
        token = self._login(c, email, pw)
        rows = self._audit_rows("login_ok")
        self.assertEqual(len(rows), 1, f"成功登录恰好一行留痕，实际 {rows}")
        self._assert_triple(rows[0], email)
        self.assertIn("user", rows[0]["detail"], "detail 的 auth_source 须指明走注册用户分支")
        self.assertNotIn(pw, self._row_text(rows[0]), "审计任何字段都不得写入口令明文")
        self.assertNotIn(token, self._row_text(rows[0]), "审计任何字段都不得写入 CSRF token")
        ok, broken, first = db.verify_audit_chain()
        self.assertTrue(ok, f"留痕行必须在既有 HMAC 链上，broken={broken} first={first}")

    def test_builtin_admin_login_writes_login_ok(self):
        """内置管理员分支（.env 口令/哈希）同样留痕——auth_source 记为 builtin。"""
        c = self._client()
        token = self._login(c, "admin", ADMIN_PASS)
        rows = self._audit_rows("login_ok")
        self.assertEqual(len(rows), 1, f"实际 {rows}")
        self._assert_triple(rows[0], "admin")
        self.assertIn("builtin", rows[0]["detail"])
        self.assertNotIn(ADMIN_PASS, self._row_text(rows[0]))
        self.assertNotIn(token, self._row_text(rows[0]))

    def test_registered_admin_login_writes_login_ok(self):
        """注册管理员（users.role=admin）也要留痕：认证来源仍是 user，但会话角色是 admin。"""
        email, pw = self._user("trail-admin@test.local", role="admin")
        c = self._client()
        token = self._login(c, email, pw)
        self.assertEqual(c.get("/api/me", headers=self._csrf(token)).get_json()["role"], "admin")
        rows = self._audit_rows("login_ok")
        self.assertEqual(len(rows), 1, f"实际 {rows}")
        self._assert_triple(rows[0], email)
        self.assertIn("user", rows[0]["detail"])

    def test_two_logins_write_exactly_two_rows(self):
        """每次登录一行：两次成功登录 = 两行，既不翻倍（无重复写）也不缺行。"""
        email, pw = self._user()
        for _ in range(2):
            self._login(self._client(), email, pw)
        self.assertEqual(len(self._audit_rows("login_ok")), 2)

    # ---- ⑤ 失败登录不得冒领成功留痕 ----
    def test_wrong_password_writes_no_login_ok(self):
        email, pw = self._user()
        c = self._client()
        bad = "wrong-" + pw
        for _ in range(self.webapp.LOGIN_FAIL_NOTIFY):   # 3 次：只到告警阈值，不触发锁定
            r = c.post("/api/login", json={"username": email, "password": bad})
            self.assertEqual(r.status_code, 401, r.get_data(as_text=True))
        self.assertEqual(self._audit_rows("login_ok"), [], "失败登录绝不产生成功留痕")
        fails = self._audit_rows("login_failed")
        self.assertEqual(len(fails), 1, "既有阈值失败留痕（第 3 次一条）不受本任务影响")
        self._assert_triple(fails[0], email)
        self.assertNotIn(bad, self._row_text(fails[0]), "失败留痕同样不得带上尝试的口令")

    def test_lockout_response_codes_unchanged(self):
        """红线：留痕不得改动锁定与返回码语义——第 5 次仍 429，其后仍 429 且无成功行。"""
        email, pw = self._user("lockme@test.local")
        c = self._client()
        for _ in range(self.webapp.LOGIN_MAX_FAILS):
            r = c.post("/api/login", json={"username": email, "password": "nope-" + pw})
        self.assertEqual(r.status_code, 429, r.get_data(as_text=True))
        r2 = c.post("/api/login", json={"username": email, "password": pw})
        self.assertEqual(r2.status_code, 429, "锁定期内正确口令也须保持 429（既有语义）")
        self.assertEqual(self._audit_rows("login_ok"), [])

    # ---- ③ 登出 ----
    def test_logout_writes_logout_ok_and_keeps_sid_rotation(self):
        email, pw = self._user()
        c = self._client()
        token = self._login(c, email, pw)
        sid_before = (db.find_user(email) or {}).get("sid") or ""
        self.assertEqual(self._audit_rows("logout_ok"), [], "登出前不应有 logout_ok")
        r = c.post("/api/logout", json={}, headers=self._csrf(token))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        rows = self._audit_rows("logout_ok")
        self.assertEqual(len(rows), 1, f"登出恰好一行，实际 {rows}")
        self._assert_triple(rows[0], email)
        self.assertIn("user", rows[0]["detail"])
        sid_after = (db.find_user(email) or {}).get("sid") or ""
        self.assertNotEqual(sid_after, sid_before, "P3-5 sid 轮换语义不得因留痕而改变")
        self.assertRegex(sid_after, r"^[0-9a-f]{32}$")
        self.assertEqual(c.get("/api/me").status_code, 401, "登出后会话确已清空")
        self.assertNotIn(sid_after, self._row_text(rows[0]), "留痕不得写入 sid")
        self.assertNotIn(token, self._row_text(rows[0]), "留痕不得写入 CSRF token")
        ok, broken, _first = db.verify_audit_chain()
        self.assertTrue(ok, f"登出留痕须进链，broken={broken}")

    def test_logout_of_builtin_admin_writes_logout_ok(self):
        """内置管理员（无 sid）同样要留登出痕迹，detail 记 builtin 以便与注册用户分账。"""
        c = self._client()
        token = self._login(c, "admin", ADMIN_PASS)
        self.assertEqual(c.post("/api/logout", json={}, headers=self._csrf(token)).status_code, 200)
        rows = self._audit_rows("logout_ok")
        self.assertEqual(len(rows), 1, f"实际 {rows}")
        self._assert_triple(rows[0], "admin")
        self.assertIn("builtin", rows[0]["detail"])

    def test_repeated_logout_writes_no_second_logout_ok(self):
        """重复登出只留一行：会话已清空，第二次请求在认证守卫处就 401，取不到 username。

        钉的是既有语义而非新写的语义——`/api/logout` 落在 require_login 的"已登录才可
        访问"之内（`session.clear()` 后 session 里连 auth 都没有），第二发进不到留痕
        那一行；留痕写在 `session.clear()` 之前，所以第一发取得到 username。
        """
        email, pw = self._user()
        c = self._client()
        token = self._login(c, email, pw)
        first = c.post("/api/logout", json={}, headers=self._csrf(token))
        self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
        self.assertEqual(len(self._audit_rows("logout_ok")), 1)
        for _ in range(2):   # 带旧 token 重放：仍只有一行
            again = c.post("/api/logout", json={}, headers=self._csrf(token))
            self.assertEqual(again.status_code, 401, again.get_data(as_text=True))
        # 无会话的匿名登出同样不该留下登出痕迹（本人并未结束任何会话）
        anon = self._client().post("/api/logout", json={})
        self.assertEqual(anon.status_code, 401, anon.get_data(as_text=True))
        rows = self._audit_rows("logout_ok")
        self.assertEqual(len(rows), 1, f"重复/匿名登出不得产生第二行，实际 {rows}")
        self.assertEqual(len(self._audit_rows("login_ok")), 1, "登录侧行数不受登出影响")
        ok, broken, _first = db.verify_audit_chain()
        self.assertTrue(ok, f"重放请求不得签坏链，broken={broken}")

    # ---- ② 恢复即登录 ----
    def test_restore_writes_login_ok_marked_as_recovery(self):
        email, pw = self._user("recover@test.local")
        c = self._client()
        self._login(c, email, pw)                       # 先有一条普通 login_ok 作对照
        self.assertEqual(len(self._audit_rows("login_ok")), 1)
        # 直连 DB 造冷静期态（不经 /api/me/delete，避免注销冷却记录挡住恢复的 60s 窗口）
        db.soft_delete_user_with_accounts(email)
        sid_before = db.find_user_any(email)["sid"]
        r = c.post("/api/me/restore", json={"email": email, "password": pw})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        rows = self._audit_rows("login_ok")
        self.assertEqual(len(rows), 2, "恢复即登录也须留下一行 login_ok")
        self._assert_triple(rows[-1], email)
        self.assertIn("恢复登录", rows[-1]["detail"], "detail 必须区分得出这是恢复入口的登录")
        self.assertNotIn("恢复登录", rows[0]["detail"], "普通登录那行不得被写成恢复登录")
        self.assertNotIn(pw, self._row_text(rows[-1]))
        self.assertTrue(self._audit_rows("user_self_delete_restore"),
                        "原有恢复动作留痕不得被本次改动顶掉")
        sid_after = db.find_user(email)["sid"]
        self.assertRegex(sid_after, r"^[0-9a-f]{32}$", "恢复即登录须重新签发 sid（批次11 N1）")
        self.assertNotEqual(sid_after, sid_before, "恢复不得复用注销前被窃取的旧 sid")
        self.assertEqual(c.get("/api/me").status_code, 200, "恢复后的新会话须立即可用")
        ok, broken, _first = db.verify_audit_chain()
        self.assertTrue(ok, f"恢复留痕须进链，broken={broken}")

    def test_restore_wrong_password_writes_no_login_ok(self):
        """恢复失败同样不得冒领成功留痕（该入口密码正确即取得完整会话，是爆破目标）。"""
        email, pw = self._user("recover2@test.local")
        db.soft_delete_user_with_accounts(email)
        c = self._client()
        r = c.post("/api/me/restore", json={"email": email, "password": "bad-" + pw})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertEqual(self._audit_rows("login_ok"), [])
        self.assertEqual(self._audit_rows("user_self_delete_restore"), [])

    # ---- ⑥ 时间线可重建且链自洽 ----
    def test_forensic_timeline_is_reconstructable_and_chain_valid(self):
        """一次完整的"登录→登出→再登录→再登出"在链上留下 2+2 行且顺序可重建。"""
        email, pw = self._user("timeline@test.local")
        for _ in range(2):
            c = self._client()
            token = self._login(c, email, pw)
            self.assertEqual(c.post("/api/logout", json={}, headers=self._csrf(token)).status_code,
                             200)
        with db._conn_lock:
            rows = db.get_conn().execute(
                "SELECT action, username FROM audit_logs "
                "WHERE action IN ('login_ok','logout_ok') ORDER BY id").fetchall()
        self.assertEqual([r["action"] for r in rows],
                         ["login_ok", "logout_ok"] * 2,
                         "起止两端必须成对且按时间顺序可重建")
        self.assertTrue(all(r["username"] == email for r in rows))
        ok, broken, first = db.verify_audit_chain()
        self.assertTrue(ok, f"新增留痕不得签坏既有链，broken={broken} first={first}")


# ================= Task 6（用户补充需求）：口令策略口径统一与前后端防漂移 =================
# 用户原话："用户自己设置密码的时候可以使用数字、大小写字母和符号，现在只要求两种，
# 应当是两种及以上。" 读码核实：后端与前端四处判的都是 `命中类别数 < 2`，四类为
# 大写/小写/数字/符号，符号自成一类——"至少两类且符号算一类"这一规则本就成立，
# 把门槛改成"必须三类/必须含符号"反而会收紧判定语义。真正的缺陷是另外三条：
#   ① 文案歧义：一处写成容易被读成"三类起"的中文比较词、一处简写成读起来像"恰好
#     两类"，用户据此以为数字/大小写/符号必须凑满三种；
#   ② 后台密码模态 set 分支只判长度不判类别 → 前端放行、后端 400，"提交后才报错"；
#   ③ 后端 1 处 + 前端 4 处共五份内联类别正则，无任何防漂移保护。
# 本组用例钉的就是：判定语义一字未动（≥10 位、≥2 类）+ 五份实现锁成一处事实源。
TEMPLATES_DIR = os.path.join(BASE, "web", "templates")
PW_TEMPLATES = ("login.html", "user.html", "index.html")
# 统一后的整句口径（后端 _PASSWORD_POLICY_HINT 与三个模板的 PW_POLICY_HINT 必须逐字相等）
PW_HINT = "至少 10 位，且包含大写字母、小写字母、数字、符号中的至少两类"
PW_CLASS_SENTENCE = "大写字母、小写字母、数字、符号中的至少两类"
# 期望的四类正则：顺序与标签数组一一对应，元测试按序严格比对（换序也算漂移）
PW_EXPECTED_PATTERNS = [r"[A-Z]", r"[a-z]", r"\d", r"[^A-Za-z0-9]"]
_PW_JS_ARRAY_RE = re.compile(r"const\s+PW_CLASS_PATTERNS\s*=\s*\[([^\n]*?)\]\s*;")
_PW_JS_REGEX_RE = re.compile(r"/([^/]+)/")
_PW_JS_HINT_RE = re.compile(r"const\s+PW_POLICY_HINT\s*=\s*'([^']*)'")
_PW_JS_LIMITS_RE = re.compile(
    r"const\s+PW_MIN_LEN\s*=\s*(\d+)\s*,\s*PW_MIN_CLASSES\s*=\s*(\d+)")


def _read_text(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read()


class PasswordPolicyParityB14Test(_B14AlertGateBase):
    """Task 6：口令策略前后端同口径，谁先漂移谁判红。

    夹具复用 Task 3 的 `_B14AlertGateBase`（隔离 .env + 独立库 + send_notification
    全 mock）：本组既要按源码文本比对模板，也要真的走 /api/register、/api/me/password、
    /api/users/<email>/password 三个端点确认"只改措辞、不改判定"。
    """

    # ---- ③ 之后端半边：单一事实源成立 ----
    def test_backend_patterns_and_thresholds_are_the_single_source(self):
        w = self.webapp
        self.assertEqual(list(w._PASSWORD_CLASS_PATTERNS), PW_EXPECTED_PATTERNS,
                         "后端四类正则与期望漂移：改这里等于改判定语义，须单独立项")
        self.assertEqual(len(w._PASSWORD_CLASS_LABELS), len(w._PASSWORD_CLASS_PATTERNS),
                         "类别标签与类别正则须一一对应（文案由标签派生）")
        self.assertEqual(w._PASSWORD_MIN_CLASSES, 2,
                         "判定下限仍是 2 类：不得改成 3 类，也不得要求必须含符号")
        self.assertEqual(w.PASSWORD_MIN_LEN, 10, "长度下限不变")
        src = _read_text(os.path.join(BASE, "web", "app.py"))
        self.assertEqual(src.count("A-Za-z0-9"), 1,
                         "web/app.py 里符号类正则只能出现在 _PASSWORD_CLASS_PATTERNS "
                         "一处；出现第二处即回到多份内联重复的老问题")
        fn = inspect.getsource(w._password_policy_error)
        self.assertIn("_PASSWORD_CLASS_PATTERNS", fn,
                      "_password_policy_error 必须由模块级常量派生，不得自带一份正则")
        self.assertNotIn("A-Za-z0-9", fn, "_password_policy_error 内不得内联类别正则")

    # ---- ③ 之前端半边（元测试核心）：三份内联数组与后端常量逐字同序同串 ----
    def test_templates_class_regexes_match_backend_constant(self):
        backend = list(self.webapp._PASSWORD_CLASS_PATTERNS)
        for name in PW_TEMPLATES:
            src = _read_text(os.path.join(TEMPLATES_DIR, name))
            arr = _PW_JS_ARRAY_RE.search(src)
            self.assertIsNotNone(
                arr,
                f"{name} 找不到 `const PW_CLASS_PATTERNS = [...]`：模板内的字符类别判定"
                f"必须以这一个数组声明（既不得退回逐处内联，也不得删掉——元测试要读得到它）")
            found = _PW_JS_REGEX_RE.findall(arr.group(1))
            self.assertEqual(
                found, backend,
                f"{name} 的类别判定正则与后端漂移：模板 {found} != 后端 {backend}"
                f"（后端定义见 web/app.py 的 _PASSWORD_CLASS_PATTERNS）。两侧须同序同串，"
                f"要改判定就同时改 web/app.py 与 {', '.join(PW_TEMPLATES)}")
            self.assertEqual(
                src.count("[^A-Za-z0-9]"), 1,
                f"{name} 有多处内联类别正则：符号类只允许写在 PW_CLASS_PATTERNS 里一次")
            self.assertIn("function passwordClasses(", src,
                          f"{name} 缺少本地 passwordClasses(v) helper（helper 须替代内联正则）")

    def test_templates_copy_and_limits_match_backend(self):
        """文案与两个下限常量的前后端一致性：JS 拿不到 Python 常量，只能靠本用例锁。"""
        self.assertEqual(self.webapp._PASSWORD_POLICY_HINT, PW_HINT,
                         "后端 _PASSWORD_POLICY_HINT 与统一口径文案漂移")
        for name in PW_TEMPLATES:
            src = _read_text(os.path.join(TEMPLATES_DIR, name))
            hint = _PW_JS_HINT_RE.search(src)
            self.assertIsNotNone(hint, f"{name} 缺少 `const PW_POLICY_HINT = '...'` 文案常量")
            self.assertEqual(
                hint.group(1), PW_HINT,
                f"{name} 的 PW_POLICY_HINT 与后端 _PASSWORD_POLICY_HINT 文案漂移："
                f"模板「{hint.group(1)}」vs 后端「{self.webapp._PASSWORD_POLICY_HINT}」")
            limits = _PW_JS_LIMITS_RE.search(src)
            self.assertIsNotNone(
                limits, f"{name} 的 PW_MIN_LEN / PW_MIN_CLASSES 须在同一行成对声明")
            self.assertEqual(int(limits.group(1)), self.webapp.PASSWORD_MIN_LEN,
                             f"{name} 的长度下限与后端 PASSWORD_MIN_LEN 漂移")
            self.assertEqual(int(limits.group(2)), self.webapp._PASSWORD_MIN_CLASSES,
                             f"{name} 的类别下限与后端 _PASSWORD_MIN_CLASSES 漂移")

    def test_ambiguous_wording_is_gone(self):
        """①文案歧义：旧措辞在随代码发布的四处文本里一律不得再现（含注释，防其回流）。"""
        targets = [("web/app.py", _read_text(os.path.join(BASE, "web", "app.py")))]
        targets += [(n, _read_text(os.path.join(TEMPLATES_DIR, n))) for n in PW_TEMPLATES]
        for name, src in targets:
            for bad in ("两类以上", "含两类字符"):
                self.assertNotIn(
                    bad, src,
                    f"{name} 仍含歧义措辞「{bad}」：口径须是「…中的至少两类」，"
                    f"「两类以上」易被读成三类起、「含两类字符」易被读成恰好两类")
            bare = re.findall(r"(?<!至少)两类", src)
            self.assertEqual(
                bare, [],
                f"{name} 出现不带「至少」的「两类」表述：下限一律写成「至少两类」，"
                f"否则用户无法从文案判断这是下限还是恰好值")

    # ---- 后端返回文案逐字钉住（只允许措辞变，分支归属不得变）----
    def test_policy_error_messages_are_verbatim(self):
        pp = self.webapp._password_policy_error
        self.assertEqual(pp("Abcdefg1"), f"密码{PW_HINT}",
                         "长度不足分支：整句口径须逐字如此（含中文「至少两类」）")
        self.assertEqual(pp("abcdefghijkl"), f"密码需包含{PW_CLASS_SENTENCE}",
                         "类别不足分支：长度够而类别不足，须走类别文案")
        self.assertEqual(pp(""), f"密码{PW_HINT}", "空口令按长度分支拒绝")
        for bad in ("abcdefghijkl", "Abcdefg1", ""):
            msg = pp(bad)
            self.assertIsNotNone(msg)
            self.assertIn("至少两类", msg, f"报错文案未使用无歧义口径：{msg}")
        self.assertIsNone(pp("Abcdefgh12"), "10 位两类必须放行（判定语义不得收紧）")

    def test_class_acceptance_matrix(self):
        """接受矩阵：符号算一类、任意两类即过；单类一律拒；长度优先于类别。"""
        pp = self.webapp._password_policy_error
        cases = [
            ("Abcdefghij", True, "大写+小写两类"),
            ("abcdefg123", True, "小写+数字两类"),
            ("ABCDEFGH12", True, "大写+数字两类"),
            ("!@#$%^&*()12", True, "纯符号+数字两类（旧文案曾被读成必须含字母）"),
            ("!@#$%^&*()ab", True, "符号+小写两类"),
            ("!@#$%^&*()AB", True, "符号+大写两类"),
            ("aaaaaaaaa中", True, "中文计入符号类"),
            ("aaaaaaaaa ", True, "空格计入符号类"),
            ("aaaaaaaaa@", True, "@ 计入符号类"),
            ("Abcdefgh12", True, "长度下界 10 位"),
            ("abcdefghijkl", False, "12 位单类字母"),
            ("ABCDEFGHIJKL", False, "12 位单类大写字母"),
            ("1234567890", False, "纯数字单类"),
            ("!@#$%^&*()", False, "纯符号单类（符号算一类，但凑不满两类）"),
            ("          ", False, "十个空格单类"),
            ("Abcdefg1", False, "9 位（长度下界以下）"),
            ("Ab1xxxxxx", False, "9 位但四类齐全仍拒——长度优先于类别"),
            ("", False, "空口令"),
        ]
        for pw, expected_ok, why in cases:
            with self.subTest(pw=pw, why=why):
                err = pp(pw)
                if expected_ok:
                    self.assertIsNone(err, f"{why}：应放行，实际被拒「{err}」")
                else:
                    self.assertIsNotNone(err, f"{why}：应拒绝，实际放行")
                    self.assertIn("至少两类", err)

    def test_unicode_digit_divergence_is_recorded_not_silently_changed(self):
        """已知差异留档（本轮不改判定语义）：Python 的 \\d 覆盖全角数字，JS 的不覆盖。

        实测口径（3 万+ 随机串跑 node 复算前端 helper 对拍后端判定）：纯 ASCII 口令
        前后端零差异；所有差异都由非 ASCII 数字（如全角 １）引起，且方向恒为
        "后端放行 / 前端拦下"——前端更严，不会放进弱口令，只是用户会被自己的浏览器挡住。
        要彻底对齐须显式给 \\d 加 ASCII 限定，那属于改判定语义，得另轮处理并同步本用例
        与三个模板的 PW_CLASS_PATTERNS。
        """
        self.assertIsNone(self.webapp._password_policy_error("１２３４５６７８９０"))

    def test_admin_password_modal_validates_classes(self):
        """②漏检修复：后台密码模态 set 分支必须按完整策略校验，不得只判长度。"""
        src = _read_text(os.path.join(TEMPLATES_DIR, "index.html"))
        self.assertIn("function passwordPolicyOk(", src,
                      "index.html 缺少 passwordPolicyOk(v) 组合判定 helper")
        line = re.search(r"if \(_pwModalMode === 'set'[^\n]*", src)
        self.assertIsNotNone(line, "找不到密码模态 set 分支的校验行")
        self.assertIn("passwordPolicyOk(pw)", line.group(0),
                      f"密码模态 set 分支未经完整策略校验：{line.group(0)}")
        self.assertNotIn("pw.length < 10", line.group(0),
                         "set 分支又变成只判长度——类别判定必须一起走")

    # ---- 端点半边：判定语义与报错前缀不变，只有措辞随统一口径更新 ----
    def test_register_endpoint_semantics_and_copy(self):
        c = self._client()
        r = c.post("/api/register", json={"email": "pwcweak@test.local",
                                          "password": "abcdefghijkl", "agree": True})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["error"], f"密码需包含{PW_CLASS_SENTENCE}",
                         "注册端点只允许措辞变化：状态码与整句口径逐字钉住")
        self.assertIsNone(db.find_user("pwcweak@test.local"), "被拒注册不得落库")
        r2 = c.post("/api/register", json={"email": "pwcsym@test.local",
                                           "password": "!@#$%^&*()12", "agree": True})
        self.assertEqual(r2.status_code, 200, r2.get_data(as_text=True))
        self.assertIsNotNone(db.find_user("pwcsym@test.local"),
                             "符号+数字两类须照常注册成功（未引入「必须含字母」这类收紧）")

    def test_me_password_endpoint_prefix_unchanged(self):
        email, old = "pwcopyme@test.local", "OldPass#2026"
        db.create_user(email, self.webapp.generate_password_hash(old))
        c = self._client()
        token = self._login(c, email, old)
        r = c.post("/api/me/password",
                   json={"old_password": old, "new_password": "abcdefghij",
                         "confirm_password": "abcdefghij"}, headers=self._csrf(token))
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["error"], f"新密码不符合要求：密码需包含{PW_CLASS_SENTENCE}",
                         "端点前缀「新密码不符合要求：」不得随文案订正而变")
        self.assertTrue(self.webapp.check_password_hash(
            db.find_user(email)["password_hash"], old), "校验失败不得改到存量哈希")

    def test_admin_reset_password_endpoint_prefix_unchanged(self):
        email = "pwcopyadmin@test.local"
        db.create_user(email, self.webapp.generate_password_hash("OldPass#2026"))
        c = self._client()
        token = self._login(c, "admin", ADMIN_PASS)
        r = c.post(f"/api/users/{email}/password", json={"password": "1234567890"},
                   headers=self._csrf(token))
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["error"], f"新密码不符合要求：密码需包含{PW_CLASS_SENTENCE}",
                         "管理员重置端点前缀与状态码不变")
        self.assertEqual((db.find_user(email) or {}).get("pw_version"), 1,
                         "被拒重置不得动 pw_version（旧会话不得被无谓吊销）")
        r2 = c.post(f"/api/users/{email}/password", json={"password": "!@#$%^&*()12"},
                    headers=self._csrf(token))
        self.assertEqual(r2.status_code, 200, r2.get_data(as_text=True))
        self.assertEqual((db.find_user(email) or {}).get("pw_version"), 2,
                         "两类口令（纯符号+数字）仍按原语义放行并递增 pw_version")


if __name__ == "__main__":
    unittest.main()
