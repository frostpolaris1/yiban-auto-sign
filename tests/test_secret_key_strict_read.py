# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""`ensure_secret_key` 的"全新部署/已有密钥"判定必须走严格读 + 同源一次读（MF-84）。

背景：同一形状的决策，`account_crypto`（账号密钥）与 `audit_chain`（审计密钥）两支
都采纳了 `parse_env_file(strict=True)`——"文件存在但读失败"绝不许被误判为"未配置"
（误判 ⇒ 生成新钥覆盖旧钥 ⇒ 存量密文/审计链永久不可解）；唯独 web 会话密钥支曾用
宽松读做判定：宽松读把读失败吞成空 dict ⇒ 判"全新部署"、生成新钥、经单一写入口把
旧 `YIBAN_SECRET_KEY` 行**折叠顶掉**并追加 `YIBAN_REGISTRATION_PAUSE=1`（盖过残留的
`=0` 行）⇒ 旧钥静默作废、注册被静默关闭。判定读与落盘读还是**两次不同源**的读取：
判定看到"空"、落盘看到"恢复后的旧文件"，窗口期（upsert 截断、编辑器 unlink+新建、
占用）即可两头各看一版。

本文件钉四格：
1. **活体反例对**：同一枚"首次读失败、后续读成功"的一次性窗口垫片——旧判定形状
   （宽松读吞 OSError）在垫片下把旧密钥文件换钥+翻转注册开关（证窗口真实有害）；
   新 `ensure_secret_key` 同垫片下**零写盘**、文件逐字节不变、返回进程内随机密钥并告警。
2. **空/残缺文件窗口不产生并存行**：空文件（截断窗）与仅注释文件都按"全新部署"建钥，
   写后每个键恰好一行（影子行机制：同名键多行时后写覆盖先写、生效值由落盘顺序决定，
   折叠由单一写入口 `render_env_write`/`key_line_pattern` 承担）。
3. **同源一次读**（AST 形状判据）：函数体内 `parse_env_file` 调用恰好一处且带
   `strict=True`；不得再有 `read_env`/`os.path.exists` 参与判定。
4. **正常首启行为与现状一致**：文件不存在/空 ⇒ 自动建钥 + `PAUSE=1`；既有键 ⇒ 原样
   返回、零落盘。（既有钉同步跑绿：test_web_boundary / test_web_security_gates /
   test_registration_pause。）

标签：G · 安全：脱敏/审计/配置注入
覆盖：一次性读窗口垫片下的新旧判定形状对照（换钥顶盘 vs 零写盘降级）、空/仅注释
    文件的建钥折叠（键行数=1 不并存）、AST 同源单读判据、既有密钥原样返回零落盘。
对应实现：`web/services/env_io.py` 的 `ensure_secret_key`（`parse_env_file(strict=True)`
    单源判定 + 读败降级不写盘）；折叠口径在 `yiban/infra/env_io.py` 的
    `render_env_write`/`key_line_pattern`（影子行折叠单一实现）。
关键断言：降级格必须**同时**断"返回随机钥 + 落盘打桩零调用 + 磁盘字节不变 + WARNING
    点名 YIBAN_SECRET_KEY"——只断返回值会漏"照样写盘顶掉旧钥"；反例对必须用**同一枚**
    垫片分别跑旧形状与新实现，否则"窗口不成立"无从反驳。
依赖：临时 .env + builtins.open 一次性垫片（不 mock 被测逻辑本身）；无网络、无 skip。
"""
import ast
import builtins
import contextlib
import inspect
import io
import os
import secrets
import tempfile
import unittest
from unittest import mock

from web.services import env_io as svc
from yiban.infra import env_io as infra_env_io

OLD_KEY = "0d" * 32
LEGACY_FILE = ("YIBAN_ADMIN_USER=admin\n"
               "YIBAN_SECRET_KEY=" + OLD_KEY + "\n"
               "YIBAN_REGISTRATION_PAUSE=0\n")


class _OneShotReadWindow:
    """对目标文件的**前 1 次**读模式 open 抛 PermissionError，其后放行——模拟
    "判定读取恰好落在截断/占用窗口里、而稍后的写入读又成功"的瞬态残缺窗。"""

    def __init__(self, path):
        self.target = os.path.abspath(path)
        self.n = 0
        self._real = builtins.open

    def __call__(self, file, *args, **kwargs):
        try:
            resolved = os.path.abspath(file)
        except (TypeError, ValueError):
            resolved = None
        mode = str(args[0]) if args else str(kwargs.get("mode", "r"))
        if resolved == self.target and resolved is not None and not any(c in mode for c in "wax"):
            self.n += 1
            if self.n == 1:
                raise PermissionError(13, "simulated transient read window")
        return self._real(file, *args, **kwargs)

    def __enter__(self):
        self._patch = mock.patch("builtins.open", self)
        self._patch.__enter__()
        return self

    def __exit__(self, *exc):
        self._patch.__exit__(*exc)
        return False


class EnsureSecretKeyStrictReadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-seckey-")

    def tearDown(self):
        with contextlib.suppress(OSError):
            for name in os.listdir(self.tmp):
                p = os.path.join(self.tmp, name)
                if os.path.isfile(p):
                    os.unlink(p)
            os.rmdir(self.tmp)

    def _mk_env(self, text=LEGACY_FILE):
        path = os.path.join(self.tmp, "web.env")
        with io.open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def _stub_atomic_write(self, sink):
        def _write(path, content, chmod_priv=False):
            sink.append(content)
            with io.open(path, "w", encoding="utf-8") as f:
                f.write(content)
        return _write

    # ---- 1. 活体反例对：同一窗口垫片，旧形状顶钥，新实现零写盘 ----

    def _old_shape_ensure(self, env_path, atomic_write):
        """修复前的判定形状（宽松读吞读失败 + exists 二次判源），仅用于反例。"""
        env = infra_env_io.parse_env_file(env_path)          # 宽松：OSError → {}
        new_deployment = not os.path.exists(env_path) or not env
        key = env.get("YIBAN_SECRET_KEY", "").strip()
        if key:
            return key
        key = secrets.token_hex(32)
        updates = {"YIBAN_SECRET_KEY": key}
        if new_deployment:
            updates["YIBAN_REGISTRATION_PAUSE"] = "1"
        infra_env_io.write_env_keys(env_path, updates, write_text=atomic_write,
                                    delete_empty=True)
        return key

    def test_window_counterexample_old_shape_swaps_key(self):
        """旧判定形状在瞬态读窗里把"读失败"当"未配置"：宽松读吞掉失败 ⇒ 判全新
        ⇒ 写侧稍后读到恢复的旧文件 ⇒ 旧钥被折叠顶掉、PAUSE 0→1——登记机理的活体
        复现（证明该窗口真实有害，不是理论风险）。"""
        path = self._mk_env()
        written = []
        with _OneShotReadWindow(path):
            got = self._old_shape_ensure(path, self._stub_atomic_write(written))
        self.assertEqual(len(written), 1, "旧形状在窗口内会落一次盘")
        self.assertNotIn(OLD_KEY, written[0], "旧钥行正是被这次落盘折叠顶掉的")
        self.assertIn("YIBAN_REGISTRATION_PAUSE=1", written[0])
        self.assertNotEqual(got, OLD_KEY)
        on_disk = io.open(path, encoding="utf-8").read()
        self.assertNotIn(OLD_KEY, on_disk, "伤害落到了真盘：旧钥已不在文件里")

    def test_window_new_impl_never_touches_disk(self):
        """新实现：严格读失败 ⇒ 判定不可信 ⇒ 一次都不写；返回进程内随机钥 + 告警。
        与上一例**同一枚垫片**、同一份旧文件——对照即验收。"""
        path = self._mk_env()
        before = io.open(path, "rb").read()
        written = []
        with _OneShotReadWindow(path), self.assertLogs("web", level="WARNING") as logs:
            got = svc.ensure_secret_key(path, self._stub_atomic_write(written))
        self.assertEqual(written, [], "读失败判定下绝不允许落盘")
        self.assertEqual(io.open(path, "rb").read(), before, "磁盘必须逐字节不变")
        self.assertEqual(len(got), 64)
        self.assertNotEqual(got, OLD_KEY, "读不到旧钥时返回的是进程内随机钥")
        self.assertTrue(any("YIBAN_SECRET_KEY" in ln for ln in logs.output), logs.output)

    # ---- 2. 空/残缺文件窗口：建钥后每个键恰好一行（影子行机制） ----

    def test_truncated_window_folds_to_single_lines(self):
        """截断成空文件 = 合法"全新部署"（与既有裁决一致：无有效键即判新）；
        写后 SECRET_KEY 与 REGISTRATION_PAUSE 各只有**一行**——单一写入口的
        key_line_pattern 折叠保证不出现新/旧并存或 =1/=0 并存（影子行机制）。"""
        path = self._mk_env(text="")
        got = svc.ensure_secret_key(path, self._stub_atomic_write([]))
        text = io.open(path, encoding="utf-8").read()
        self.assertEqual(infra_env_io.count_key_lines(path, "YIBAN_SECRET_KEY"), 1)
        self.assertEqual(infra_env_io.count_key_lines(path, "YIBAN_REGISTRATION_PAUSE"), 1)
        self.assertIn("YIBAN_SECRET_KEY=" + got, text)
        self.assertIn("YIBAN_REGISTRATION_PAUSE=1", text)
        self.assertNotIn("YIBAN_REGISTRATION_PAUSE=0", text,
                         "=1 行不得与旧 =0 行并存（计数已=1，此处按整行钉死）")

    def test_comment_only_file_treated_as_fresh_single_key(self):
        """仅注释的文件 = 无有效键 = 全新部署：建钥 + PAUSE=1，注释保留。"""
        path = self._mk_env(text="# 部署者留下的说明\n")
        got = svc.ensure_secret_key(path, self._stub_atomic_write([]))
        text = io.open(path, encoding="utf-8").read()
        self.assertIn("# 部署者留下的说明", text)
        self.assertIn("YIBAN_SECRET_KEY=" + got, text)
        self.assertEqual(infra_env_io.count_key_lines(path, "YIBAN_REGISTRATION_PAUSE"), 1)

    # ---- 3. 同源一次读的形状判据（AST：判定只允许一处严格读） ----

    def test_decision_is_one_strict_read(self):
        tree = ast.parse(inspect.getsource(svc.ensure_secret_key))
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef))

        def callee_name(call):
            if isinstance(call.func, ast.Name):
                return call.func.id
            if isinstance(call.func, ast.Attribute):
                return call.func.attr
            return ""

        parses = [n for n in ast.walk(fn)
                  if isinstance(n, ast.Call) and callee_name(n) == "parse_env_file"]
        self.assertEqual(len(parses), 1, "判'全新部署'与取旧钥必须共用同一次读取")
        strict_true = any(k.arg == "strict" and isinstance(k.value, ast.Constant)
                          and k.value.value is True for k in parses[0].keywords)
        self.assertTrue(strict_true, "唯一的判定读取必须显式 strict=True（宽松读会把"
                                     "读败吞成空 dict=误判未配置）")
        callees = {callee_name(n) for n in ast.walk(fn) if isinstance(n, ast.Call)}
        self.assertNotIn("read_env", callees, "判定不得再走宽松读第二源")
        self.assertNotIn("exists", callees, "判定不得再靠 os.path.exists 分源")

    # ---- 4. 正常路径：既有键原样返回、零落盘 ----

    def test_existing_key_returned_without_any_write(self):
        path = self._mk_env()
        written = []
        got = svc.ensure_secret_key(path, self._stub_atomic_write(written))
        self.assertEqual(got, OLD_KEY, "读得到的既有密钥必须原样返回")
        self.assertEqual(written, [], "已有密钥时不得再落盘（不得翻转 PAUSE/换钥）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
