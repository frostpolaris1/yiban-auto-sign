# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""配置名册加载器 `yiban/config_loader.py` 的行为契约（104 第一批·A）。

标签：A · 配置：名册与加载器
覆盖：① **优先级链**——进程环境 → `.env` → 名册缺省（F18 / 工单 71z7 裁决口径，
    "写错即静默改行为"，故三层各一条断言 + 来源标注）；② **超域键只告警不改行为**：
    越界值原样返回，不夹取、不回退默认；③ **未知键告警不静默**；④ **零配置启动**：
    空 `.env` + 空进程环境不炸，取值恒等于名册缺省（本单验收"现有部署零改动启动"）；
    ⑤ `reload` 字段**仅声明**，不改变既有重启语义（本批不实现热重载）。
对应实现：`yiban/config_loader.py`、`config/registry.json`。
关键断言：优先级链的方向由测试钉住，不许由实现自述——顺序写反（名册缺省压过 `.env`）
    在单键取值上表现为"配置改了不生效"，正是本模块存在的理由。
依赖：无网络；临时 `.env` 文件 + 注入的 registry 映射。**一处子进程**——"零配置启动"
    那条起一次 `python -c "import yiban.engine..."`，证明 `.env` 缺失时引擎照旧可导入。
"""
import ast
import logging
import os
import tempfile
import unittest

from yiban import config_loader as CL

# 合成名册：故意与真实 config/registry.json 解耦，让行为断言不受名册内容漂移影响。
# 真实名册的**结构合法性**由 scripts/check-config-registry.py 判（见 gate 自测）。
REGISTRY = {
    "_meta": {"version": 1},
    "YIBAN_WORKERS": {
        "type": "int", "domain": [1, 64], "default": 1, "group": ["执行体", "并发"],
        "doc": "并行执行体数上限", "sensitive": False, "web_editable": True,
        "tier": "master", "reload": "restart",
    },
    "YIBAN_ACCOUNT_GAP_MAX": {
        "type": "seconds", "domain": [0, 3600], "default": 10, "group": ["执行体", "节流"],
        "doc": "相邻两次签到请求的最小间隔", "sensitive": False, "web_editable": True,
        "tier": "master", "reload": "next-round",
    },
    "YIBAN_SIGN_ORDER": {
        "type": "enum", "domain": ["sequence", "random"], "default": "sequence",
        "group": ["调度"], "doc": "排序模式", "sensitive": False, "web_editable": True,
        "tier": "admin", "reload": "next-round",
    },
    "YIBAN_SIGN_START": {
        "type": "hhmm", "default": "06:30", "group": ["调度"],
        "doc": "签到窗口开始时刻", "sensitive": False, "web_editable": True,
        "tier": "admin", "reload": "next-round",
    },
    "YIBAN_MAIL_ENABLE": {
        "type": "bool", "default": False, "group": ["通知"], "doc": "邮件通知开关",
        "sensitive": False, "web_editable": True, "tier": "master", "reload": "hot",
    },
    "YIBAN_SECRET_KEY": {
        "type": "string", "default": None, "group": ["密钥"], "doc": "会话签名密钥",
        "sensitive": True, "web_editable": False, "tier": "master", "reload": "restart",
    },
    "YIBAN_CAPACITY_MAX": {
        "derived": "capacity_max(workers, window, rate, gap, avg)",
        "group": ["容量"], "doc": "派生键：不许手填", "sensitive": False,
        "web_editable": False, "tier": "master", "reload": "restart",
    },
}


def _dotenv(text):
    """写一个临时 `.env`，返回其路径。"""
    fd, path = tempfile.mkstemp(prefix="yiban-cl-env-", suffix=".env")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    return path


class _WarningCapture:
    """捕获加载器通道上的 WARNING（告警文案与键名都要能读）。"""

    def __init__(self):
        self.records = []

    def __enter__(self):
        self._handler = logging.Handler()
        self._handler.emit = self.records.append
        self._logger = logging.getLogger("yiban.config_loader")
        self._logger.addHandler(self._handler)
        self._old_level = self._logger.level
        self._logger.setLevel(logging.WARNING)
        return self

    def __exit__(self, *exc):
        self._logger.removeHandler(self._handler)
        self._logger.setLevel(self._old_level)
        return False

    @property
    def text(self):
        return "\n".join(r.getMessage() for r in self.records)


class PriorityChainTest(unittest.TestCase):
    """行为一：解析优先级 = 进程环境 → `.env` → 名册缺省（三层各一条）。"""

    def setUp(self):
        self.env_file = _dotenv("YIBAN_WORKERS=8\nYIBAN_SIGN_ORDER=random\n")

    def tearDown(self):
        os.unlink(self.env_file)

    def test_process_env_beats_dotenv(self):
        cfg = CL.load(["YIBAN_WORKERS"], env={"YIBAN_WORKERS": "4"},
                      env_file=self.env_file, registry=REGISTRY)
        self.assertEqual(cfg["YIBAN_WORKERS"], 4)
        self.assertEqual(cfg.source("YIBAN_WORKERS"), "process_env",
                         "进程环境优先是工单 71z7 的裁决口径，写反即静默改行为")

    def test_dotenv_beats_registry_default(self):
        cfg = CL.load(["YIBAN_WORKERS"], env={}, env_file=self.env_file,
                      registry=REGISTRY)
        self.assertEqual(cfg["YIBAN_WORKERS"], 8)
        self.assertEqual(cfg.source("YIBAN_WORKERS"), "dotenv")

    def test_registry_default_is_last(self):
        cfg = CL.load(["YIBAN_WORKERS"], env={}, env_file=self.env_file,
                      registry=REGISTRY)
        # .env 有本键（8）⇒ 名册缺省不生效；换一枚 .env 里没有的键看第三层。
        cfg2 = CL.load(["YIBAN_ACCOUNT_GAP_MAX"], env={}, env_file=self.env_file,
                       registry=REGISTRY)
        self.assertEqual(cfg["YIBAN_WORKERS"], 8)
        self.assertEqual(cfg2["YIBAN_ACCOUNT_GAP_MAX"], 10)
        self.assertEqual(cfg2.source("YIBAN_ACCOUNT_GAP_MAX"), "registry_default")

    def test_single_key_resolve_follows_same_chain(self):
        """单键 `resolve` 与批量 `load` 必须同链（两套顺序就是两个事实源）。"""
        self.assertEqual(CL.resolve("YIBAN_WORKERS", env={"YIBAN_WORKERS": "4"},
                                    env_file=self.env_file, registry=REGISTRY), 4)
        self.assertEqual(CL.resolve("YIBAN_WORKERS", env={}, env_file=self.env_file,
                                    registry=REGISTRY), 8)
        self.assertEqual(CL.resolve("YIBAN_MAIL_ENABLE", env={}, env_file=self.env_file,
                                    registry=REGISTRY), False)
        self.assertIsNone(CL.resolve("YIBAN_SECRET_KEY", env={}, env_file=self.env_file,
                                     registry=REGISTRY))

    def test_process_env_wins_without_dotenv_present(self):
        """`.env` 不存在（全新部署）时进程环境照样生效，不因读文件失败而丢值。"""
        missing = os.path.join(tempfile.gettempdir(), "yiban-cl-nonexistent-.env")
        cfg = CL.load(["YIBAN_WORKERS"], env={"YIBAN_WORKERS": "3"},
                      env_file=missing, registry=REGISTRY)
        self.assertEqual(cfg["YIBAN_WORKERS"], 3)


class OutOfDomainTest(unittest.TestCase):
    """行为二：超域键只告警、值不改（不夹取、不回退默认值）。"""

    def setUp(self):
        self.env_file = _dotenv("YIBAN_WORKERS=999\n")

    def tearDown(self):
        os.unlink(self.env_file)

    def test_over_domain_warns_and_keeps_value(self):
        with _WarningCapture() as cap:
            cfg = CL.load(["YIBAN_WORKERS"], env={}, env_file=self.env_file,
                          registry=REGISTRY)
        self.assertEqual(cfg["YIBAN_WORKERS"], 999,
                         "超域键只告警不改行为：夹到 64 或回退 1 都是静默改值")
        self.assertIn("YIBAN_WORKERS", cap.text, "超域必须点名键")
        self.assertTrue(cap.records, "超域必须出声，不许静默")

    def test_under_domain_also_warns(self):
        path = _dotenv("YIBAN_WORKERS=0\n")
        try:
            with _WarningCapture() as cap:
                val = CL.resolve("YIBAN_WORKERS", env={}, env_file=path,
                                 registry=REGISTRY)
            self.assertEqual(val, 0)
            self.assertIn("YIBAN_WORKERS", cap.text)
        finally:
            os.unlink(path)

    def test_enum_out_of_domain_keeps_value(self):
        path = _dotenv("YIBAN_SIGN_ORDER=sideways\n")
        try:
            with _WarningCapture() as cap:
                val = CL.resolve("YIBAN_SIGN_ORDER", env={}, env_file=path,
                                 registry=REGISTRY)
            self.assertEqual(val, "sideways", "枚举越界同样只告警")
            self.assertIn("YIBAN_SIGN_ORDER", cap.text)
        finally:
            os.unlink(path)

    def test_in_domain_value_is_silent(self):
        path = _dotenv("YIBAN_WORKERS=64\n")
        try:
            with _WarningCapture() as cap:
                val = CL.resolve("YIBAN_WORKERS", env={}, env_file=path,
                                 registry=REGISTRY)
            self.assertEqual(val, 64, "域的上界是闭区间（与代码里的 1~64 同口径）")
            self.assertEqual(cap.records, [], "域内取值不该出声")
        finally:
            os.unlink(path)


class UnparseableValueTest(unittest.TestCase):
    """不可解析的写法：告警后按"未设"处理（回落下一层），不抛异常。"""

    def setUp(self):
        self.env_file = _dotenv("YIBAN_WORKERS=abc\n")

    def tearDown(self):
        os.unlink(self.env_file)

    def test_unparseable_warns_and_falls_back(self):
        with _WarningCapture() as cap:
            cfg = CL.load(["YIBAN_WORKERS"], env={}, env_file=self.env_file,
                          registry=REGISTRY)
        self.assertEqual(cfg["YIBAN_WORKERS"], 1, "解析不了 ⇒ 视为未设 ⇒ 回落名册缺省")
        self.assertEqual(cfg.source("YIBAN_WORKERS"), "registry_default")
        self.assertIn("YIBAN_WORKERS", cap.text)

    def test_hhmm_accept_set_is_exactly_the_engines(self):
        """`hhmm` 的接受集必须与引擎 `window.parse_hhmm` **逐样本一致**。

        期望值从引擎现算，不在测试里另写一套"更严/更松"的判据——这正是补审
        medium 指出的形状：加载器曾自带一条更严的正则，于是 `.env` 里手写的
        `006:30`、`6 :30` 这类写法**引擎认、名册不认**，加载器回落缺省而引擎按原值跑。
        样本 13 个：7 个分叉输入（`006:30` / `021:00` / `+6:30` / `6 :30` / `6: 30` /
        `1_0:30` / `6:0030`，引擎都收）+ 4 个两边都拒（`99:99` / `abc` / `12:` / `60`）
        + 2 个两边都收（`7:05` / `23:59`）。
        """
        from yiban import window
        sentinel = object()
        samples = ("7:05", "23:59", "006:30", "021:00", "+6:30", "6 :30",
                   "6: 30", "1_0:30", "6:0030", "99:99", "abc", "12:", "60")
        for raw in samples:
            engine_ok = window.parse_hhmm(raw, sentinel) is not sentinel
            path = _dotenv("YIBAN_SIGN_START=%s\n" % raw)
            try:
                with _WarningCapture() as cap:
                    val = CL.resolve("YIBAN_SIGN_START", env={}, env_file=path,
                                     registry=REGISTRY)
            finally:
                os.unlink(path)
            if engine_ok:
                self.assertEqual(val, raw, "引擎收的写法加载器也要收：%r" % raw)
                self.assertEqual(cap.records, [], "引擎收的写法加载器不许出声：%r" % raw)
            else:
                self.assertEqual(val, "06:30", "引擎不收的写法也不收（回落缺省）：%r" % raw)
                self.assertIn("YIBAN_SIGN_START", cap.text)

    def test_hhmm_registry_end_uses_the_same_accept_set(self):
        """名册缺省端与取值端同判：两侧都走同一个接受集（名册端换判据必红）。

        补审 M4：把 `_check_default` 的 hhmm 校验换成一条更严的**非委托**判据，当时
        全部用例仍绿——名册端没有守卫。本用例的期望值同样从引擎现算，13 个样本逐条
        构造名册条目：引擎收的缺省必须通过名册自校验，引擎不收的必须抛 `RegistryError`。
        """
        from yiban import window
        sentinel = object()
        samples = ("7:05", "23:59", "006:30", "021:00", "+6:30", "6 :30",
                   "6: 30", "1_0:30", "6:0030", "99:99", "abc", "12:", "60")
        for raw in samples:
            engine_ok = window.parse_hhmm(raw, sentinel) is not sentinel
            spec = {
                "type": "hhmm", "default": raw, "group": ["调度"],
                "doc": "签到窗口开始时刻", "sensitive": False,
                "web_editable": True, "tier": "master", "reload": "next-round",
            }
            if engine_ok:
                CL.validate_registry({"_meta": {"version": 1},
                                      "YIBAN_SIGN_START": spec})
            else:
                with self.assertRaises(CL.RegistryError):
                    CL.validate_registry({"_meta": {"version": 1},
                                          "YIBAN_SIGN_START": spec})

    def test_hhmm_default_is_validated_in_the_registry(self):
        """名册里 `hhmm` 键的缺省同样按该口径校验（`99:99` 不是合法缺省）。"""
        with self.assertRaises(CL.RegistryError):
            CL.validate_registry({
                "_meta": {"version": 1},
                "YIBAN_SIGN_START": {
                    "type": "hhmm", "default": "99:99", "group": ["调度"],
                    "doc": "签到窗口开始时刻", "sensitive": False,
                    "web_editable": True, "tier": "master", "reload": "next-round"},
            })


class UnknownKeyTest(unittest.TestCase):
    """行为三：未知键告警，不静默忽略。"""

    def setUp(self):
        self.env_file = _dotenv("YIBAN_NOT_IN_REGISTRY=1\nYIBAN_WORKERS=8\n")

    def tearDown(self):
        os.unlink(self.env_file)

    def test_unknown_key_in_dotenv_warns(self):
        with _WarningCapture() as cap:
            cfg = CL.load(env={}, env_file=self.env_file, registry=REGISTRY)
        self.assertIn("YIBAN_NOT_IN_REGISTRY", cap.text, "未知键必须点名")
        self.assertNotIn("YIBAN_NOT_IN_REGISTRY", cfg.values,
                         "未知键不进入强类型对象")
        self.assertEqual(cfg["YIBAN_WORKERS"], 8)

    def test_unknown_key_in_process_env_warns(self):
        with _WarningCapture() as cap:
            CL.load(env={"YIBAN_NOT_IN_REGISTRY": "1"}, env_file=self.env_file,
                    registry=REGISTRY)
        self.assertIn("YIBAN_NOT_IN_REGISTRY", cap.text)

    def test_resolve_of_unknown_key_warns_and_returns_none(self):
        with _WarningCapture() as cap:
            val = CL.resolve("YIBAN_NOT_IN_REGISTRY", env={"YIBAN_NOT_IN_REGISTRY": "1"},
                             env_file=self.env_file, registry=REGISTRY)
        self.assertIsNone(val)
        self.assertIn("YIBAN_NOT_IN_REGISTRY", cap.text)

    def test_non_yiban_keys_are_ignored_silently(self):
        """普通进程环境里的非 YIBAN_ 键（PATH 等）不是配置，不许刷告警。"""
        clean = _dotenv("")
        try:
            with _WarningCapture() as cap:
                CL.load(env={"PATH": "/usr/bin", "HOME": "/root"}, env_file=clean,
                        registry=REGISTRY)
            self.assertEqual(cap.records, [], "非 YIBAN_ 键不该出声")
        finally:
            os.unlink(clean)


class ZeroConfigStartTest(unittest.TestCase):
    """行为四：零配置（空 `.env` + 空进程环境）启动不炸，取值 = 名册缺省。"""

    def setUp(self):
        self.env_file = _dotenv("")

    def tearDown(self):
        os.unlink(self.env_file)

    def test_no_dotenv_no_env_yields_registry_defaults(self):
        missing = os.path.join(tempfile.gettempdir(), "yiban-cl-absent-.env")
        cfg = CL.load(env={}, env_file=missing, registry=REGISTRY)
        self.assertEqual(cfg["YIBAN_WORKERS"], 1)
        self.assertEqual(cfg["YIBAN_ACCOUNT_GAP_MAX"], 10)
        self.assertEqual(cfg["YIBAN_SIGN_ORDER"], "sequence")
        self.assertEqual(cfg["YIBAN_MAIL_ENABLE"], False)
        self.assertIsNone(cfg["YIBAN_SECRET_KEY"], "无缺省的键取值 None，不炸")
        self.assertIsNone(cfg["YIBAN_CAPACITY_MAX"], "派生键由派生式给值，不手填")
        self.assertEqual(cfg.source("YIBAN_SECRET_KEY"), "unset")

    def test_empty_registry_is_a_hard_error(self):
        """名册一个键都没有 = 废门，响亮失败（静默通过会被读成"合规"）。"""
        with self.assertRaises(CL.RegistryError):
            CL.validate_registry({})

    def test_derived_key_may_not_be_hand_filled(self):
        """§5.3 硬规则二：派生键不许手填——手填即回到两个定义点。"""
        path = _dotenv("YIBAN_CAPACITY_MAX=9999\n")
        try:
            with _WarningCapture() as cap:
                cfg = CL.load(env={}, env_file=path, registry=REGISTRY)
            self.assertIsNone(cfg["YIBAN_CAPACITY_MAX"])
            self.assertEqual(cfg.source("YIBAN_CAPACITY_MAX"), "unset")
            self.assertIn("YIBAN_CAPACITY_MAX", cap.text)
        finally:
            os.unlink(path)

    def test_loading_twice_is_idempotent(self):
        cfg1 = CL.load(env={}, env_file=self.env_file, registry=REGISTRY)
        cfg2 = CL.load(env={}, env_file=self.env_file, registry=REGISTRY)
        self.assertEqual(cfg1.values, cfg2.values)
        self.assertIsNot(cfg1, cfg2, "纯函数式：每次调用产出新对象，不共享可变状态")


class RegistryValidationTest(unittest.TestCase):
    """名册自校验：类型合法、域有序、默认值在域内（加载器与 CI 门禁同一实现）。"""

    def _with(self, key, spec):
        data = {k: dict(v) for k, v in REGISTRY.items()}
        data[key] = spec
        return data

    def test_unordered_domain_is_rejected(self):
        with self.assertRaises(CL.RegistryError):
            CL.validate_registry(self._with("YIBAN_WORKERS", {
                "type": "int", "domain": [64, 1], "default": 1, "group": ["x"],
                "doc": "d", "sensitive": False, "web_editable": True,
                "tier": "master", "reload": "restart"}))

    def test_default_outside_domain_is_rejected(self):
        with self.assertRaises(CL.RegistryError):
            CL.validate_registry(self._with("YIBAN_WORKERS", {
                "type": "int", "domain": [1, 64], "default": 99, "group": ["x"],
                "doc": "d", "sensitive": False, "web_editable": True,
                "tier": "master", "reload": "restart"}))

    def test_unknown_type_is_rejected(self):
        with self.assertRaises(CL.RegistryError):
            CL.validate_registry(self._with("YIBAN_WORKERS", {
                "type": "intt", "default": 1, "group": ["x"], "doc": "d",
                "sensitive": False, "web_editable": True, "tier": "master",
                "reload": "restart"}))

    def test_unknown_tier_and_reload_are_rejected(self):
        with self.assertRaises(CL.RegistryError):
            CL.validate_registry(self._with("YIBAN_WORKERS", {
                "type": "int", "default": 1, "group": ["x"], "doc": "d",
                "sensitive": False, "web_editable": True, "tier": "root",
                "reload": "restart"}))
        with self.assertRaises(CL.RegistryError):
            CL.validate_registry(self._with("YIBAN_WORKERS", {
                "type": "int", "default": 1, "group": ["x"], "doc": "d",
                "sensitive": False, "web_editable": True, "tier": "master",
                "reload": "sometimes"}))

    def test_default_and_derived_are_mutually_exclusive(self):
        bad = self._with("YIBAN_CAPACITY_MAX", {
            "derived": "f()", "default": 1, "group": ["x"], "doc": "d",
            "sensitive": False, "web_editable": False, "tier": "master",
            "reload": "restart"})
        with self.assertRaises(CL.RegistryError):
            CL.validate_registry(bad)

    def test_path_and_proxy_keys_may_not_be_web_editable(self):
        """§5.4：路径类与代理类默认不入网页白名单（改路径=任意文件写）。

        夹具必须带 `default`，否则先撞上"既非派生键也没有缺省值"那条判据，本用例就
        变成了"因为别的守卫而红"——**断言与被测行为脱钩**（委托审查 medium-1）。
        """
        for key in ("YIBAN_DB_FILE", "YIBAN_STATE_DIR", "YIBAN_PROXY",
                    "YIBAN_ACCOUNTS_FILE"):
            with self.assertRaises(CL.RegistryError) as ctx:
                CL.validate_registry(self._with(key, {
                    "type": "path" if key != "YIBAN_PROXY" else "string",
                    "default": None, "group": ["x"], "doc": "d", "sensitive": False,
                    "web_editable": True, "tier": "master", "reload": "restart"}))
            self.assertIn("网页白名单", str(ctx.exception),
                          "红的必须是路径/代理白名单守卫本身：%s" % ctx.exception)
        # 反向：同一枚路径键把 web_editable 改成 false 即通过（否则上面的红说明不了什么）
        CL.validate_registry(self._with("YIBAN_DB_FILE", {
            "type": "path", "default": None, "group": ["x"], "doc": "d",
            "sensitive": False, "web_editable": False, "tier": "master",
            "reload": "restart"}))

    def test_enum_domain_must_list_choices(self):
        with self.assertRaises(CL.RegistryError):
            CL.validate_registry(self._with("YIBAN_SIGN_ORDER", {
                "type": "enum", "domain": [1, 3], "default": "sequence",
                "group": ["x"], "doc": "d", "sensitive": False,
                "web_editable": True, "tier": "master", "reload": "restart"}))

    def test_domain_must_match_type(self):
        """数值域配字符串类型的键判不合法（不许在域内比较处抛 TypeError）。

        `hhmm` / `string` / `bool` / `json` 都不接受域声明——只有数值型与 enum 能声明。
        """
        for key, type_name, default in (("YIBAN_SIGN_START", "hhmm", "06:30"),
                                        ("YIBAN_ADMIN_USER", "string", "admin"),
                                        ("YIBAN_MAIL_ENABLE", "bool", False),
                                        ("YIBAN_ACCOUNTS_JSON", "json", [1])):
            with self.assertRaises(CL.RegistryError):
                CL.validate_registry(self._with(key, {
                    "type": type_name, "domain": [0, 10], "default": default,
                    "group": ["x"], "doc": "d", "sensitive": False,
                    "web_editable": False, "tier": "master", "reload": "restart"}))
        with self.assertRaises(CL.RegistryError):
            CL.validate_registry(self._with("YIBAN_SIGN_DIST", {
                "type": "enum", "domain": ["uniform", "normal"], "default": 1,
                "group": ["x"], "doc": "d", "sensitive": False,
                "web_editable": True, "tier": "master", "reload": "restart"}))

    def test_json_default_must_be_json_value(self):
        """`json` 键的缺省必须是可序列化的 JSON 值（对象/数组/标量）。"""
        with self.assertRaises(CL.RegistryError):
            CL.validate_registry(self._with("YIBAN_ACCOUNTS_JSON", {
                "type": "json", "default": object(), "group": ["x"], "doc": "d",
                "sensitive": False, "web_editable": False, "tier": "master",
                "reload": "restart"}))
        CL.validate_registry(self._with("YIBAN_ACCOUNTS_JSON", {
            "type": "json", "default": [{"phone": "1"}], "group": ["x"], "doc": "d",
            "sensitive": False, "web_editable": False, "tier": "master",
            "reload": "restart"}))

    def test_real_registry_is_valid_and_covers_the_gate_keys(self):
        """真实名册自身合法，且三枚多副本默认值键都在册。"""
        registry = CL.load_registry()
        CL.validate_registry(registry)
        for key in ("YIBAN_EGRESS_RATE", "YIBAN_ACCOUNT_GAP_MAX",
                    "YIBAN_MIN_EXEC_GAP"):
            self.assertIn(key, registry, "多副本默认值键必须登记在册")
            self.assertIsNotNone(CL.default_of(key, registry=registry),
                                 "登记了就要有缺省（该键的默认值唯一来源）")


class ZeroChangeDefaultsTest(unittest.TestCase):
    """验收：**现有部署零改动启动**。收口只是把默认值的定义点搬进名册，值逐位不变。"""

    def test_collapsed_defaults_equal_pre_change_literals(self):
        """收口前代码里的字面量（grep 实测）：桶速率 1.0、账号间隔 10、最小执行间隔 5。"""
        from yiban.engine import schedule, token_bucket
        self.assertEqual(token_bucket.RATE_DEFAULT, 1.0)
        self.assertEqual(schedule._DEFAULT_BUCKET_RATE, 1.0)
        self.assertEqual(token_bucket.DEFAULT_ACCOUNT_GAP_SEC, 10)
        self.assertEqual(token_bucket.DEFAULT_MIN_EXEC_GAP_SEC, 5)
        self.assertEqual(schedule._DEFAULT_MIN_EXEC_GAP, 5)

    def test_default_required_fails_loud_on_missing_or_empty(self):
        """默认常量取不到缺省即抛：名册残件不许静默变成 None 默认值。"""
        with self.assertRaises(CL.RegistryError):
            CL.default_required("YIBAN_NOT_IN_REGISTRY", registry=REGISTRY)
        with self.assertRaises(CL.RegistryError):
            CL.default_required("YIBAN_SECRET_KEY", registry=REGISTRY)
        with self.assertRaises(CL.RegistryError):
            CL.default_required("YIBAN_CAPACITY_MAX", registry=REGISTRY)
        self.assertEqual(CL.default_required("YIBAN_WORKERS", registry=REGISTRY), 1)

    def test_engine_imports_with_absent_env_file(self):
        """`.env` 不存在（全新部署/容器首启）时引擎照旧可导入——启动不因配置缺失而炸。"""
        import subprocess
        import sys
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        absent = os.path.join(tempfile.gettempdir(), "yiban-cl-absent-startup-.env")
        env = dict(os.environ)
        env["YIBAN_ENV_FILE"] = absent
        env["PYTHONPATH"] = base + os.pathsep + env.get("PYTHONPATH", "")
        r = subprocess.run(
            [sys.executable, "-c",
             "from yiban.engine import runner, schedule, token_bucket;"
             "print(token_bucket.RATE_DEFAULT, token_bucket.DEFAULT_ACCOUNT_GAP_SEC,"
             " token_bucket.DEFAULT_MIN_EXEC_GAP_SEC, schedule._DEFAULT_BUCKET_RATE)"],
            cwd=base, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        self.assertIn("1.0 10 5 1.0", r.stdout, r.stdout + r.stderr[-400:])


class ReloadDeclarationTest(unittest.TestCase):
    """行为五：`reload` 仅声明，不改变既有重启语义。"""

    def setUp(self):
        self.env_file = _dotenv("YIBAN_WORKERS=8\n")

    def tearDown(self):
        os.unlink(self.env_file)

    def test_reload_field_is_exposed_per_key(self):
        self.assertEqual(CL.reload_of("YIBAN_WORKERS", registry=REGISTRY), "restart")
        self.assertEqual(CL.reload_of("YIBAN_MAIL_ENABLE", registry=REGISTRY), "hot")
        self.assertEqual(CL.reload_of("YIBAN_ACCOUNT_GAP_MAX", registry=REGISTRY),
                         "next-round")

    def test_loaded_config_does_not_follow_later_dotenv_edits(self):
        """本批不实现热重载：已产出的 Config 不因 `.env` 后续改动而变。"""
        cfg = CL.load(["YIBAN_WORKERS"], env={}, env_file=self.env_file,
                      registry=REGISTRY)
        before = cfg["YIBAN_WORKERS"]
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write("YIBAN_WORKERS=32\n")
        self.assertEqual(cfg["YIBAN_WORKERS"], before,
                         "reload 只声明可重载方式，重载动作归后续批次")
        self.assertEqual(cfg.values["YIBAN_WORKERS"], before)
        self.assertEqual(CL.resolve("YIBAN_WORKERS", env={}, env_file=self.env_file,
                                    registry=REGISTRY), 32,
                         "重新解析才读新值（重启语义不变）")


class InternalComponentTest(unittest.TestCase):
    """加载器是内部件，不作为使用者概念导出。"""

    def test_not_exported_from_package_root(self):
        """包根不导入也不导出它（`from yiban import config_loader` 是子模块绑定，
        不是"暴露为使用者概念"——判据看包根的源码文本与 `__all__`）。"""
        import yiban
        base = os.path.dirname(os.path.dirname(os.path.abspath(CL.__file__)))
        with open(os.path.join(base, "yiban", "__init__.py"), encoding="utf-8") as f:
            init_src = f.read()
        self.assertNotIn("config_loader", init_src,
                         "包根不得 import/导出加载器（它是内部件）")
        self.assertNotIn("config_loader", getattr(yiban, "__all__", ()))
        self.assertIn("内部件", (CL.__doc__ or "")[:400],
                      "模块 docstring 必须写明内部件定位")

    def test_docstring_declares_priority_chain(self):
        doc = CL.__doc__ or ""
        for token in ("进程环境", ".env", "名册缺省"):
            self.assertIn(token, doc, "优先级链是契约，必须写在 docstring 里：%s" % token)


class PriorityWiringTest(unittest.TestCase):
    """多副本默认值收口后，消费点的默认值必须逐字等于名册缺省（零改动启动的钉子）。"""

    def test_engine_constants_equal_registry_defaults(self):
        from yiban.engine import schedule, token_bucket
        registry = CL.load_registry()
        self.assertEqual(token_bucket.RATE_DEFAULT,
                         CL.default_of("YIBAN_EGRESS_RATE", registry=registry))
        self.assertEqual(token_bucket.DEFAULT_ACCOUNT_GAP_SEC,
                         CL.default_of("YIBAN_ACCOUNT_GAP_MAX", registry=registry))
        self.assertEqual(token_bucket.DEFAULT_MIN_EXEC_GAP_SEC,
                         CL.default_of("YIBAN_MIN_EXEC_GAP", registry=registry))
        self.assertEqual(schedule._DEFAULT_BUCKET_RATE,
                         CL.default_of("YIBAN_EGRESS_RATE", registry=registry))
        self.assertEqual(schedule._DEFAULT_MIN_EXEC_GAP,
                         CL.default_of("YIBAN_MIN_EXEC_GAP", registry=registry))

    def test_signature_defaults_equal_registry_defaults(self):
        import inspect

        from yiban.engine import schedule
        registry = CL.load_registry()
        rate = CL.default_of("YIBAN_EGRESS_RATE", registry=registry)
        for fn in (schedule.capacity_accounts_v3, schedule.capacity_of,
                   schedule.executor_count):
            self.assertEqual(inspect.signature(fn).parameters["bucket_rate"].default,
                             rate, "%s 的桶速率缺省必须取自名册" % fn.__name__)

    def test_web_and_cli_gap_defaults_equal_registry_defaults(self):
        """web 面板、CLI、引擎入口三处的账号间隔缺省同源（收口前是两份常量+两处字面量）。

        三处各一条断言：web 取模块常量、CLI 真跑一次容量计算看它用的间隔、引擎入口按
        AST 看它把名册访问器当缺省传进去。委托审查 low-2 指出原版只查了 web 一处、
        且把断言包进 `except Exception → skipTest`（导入失败即静默跳过）。
        """
        import importlib.util
        import sys
        registry = CL.load_registry()
        gap = CL.default_of("YIBAN_ACCOUNT_GAP_MAX", registry=registry)
        self.assertEqual(gap, 10, "现有部署的账号间隔缺省是 10s，收口不得改值")

        # ① CLI：空环境跑一次容量计算，它用的间隔必须就是名册缺省
        from yiban import cli
        numbers = cli._capacity_numbers({}, 10)
        self.assertEqual(numbers["gap_sec"], gap,
                         "CLI 容量命令的账号间隔缺省没走名册")

        # ② 引擎入口（runner.py）：其默认值必须是名册访问器的调用，不是字面量
        runner_src = os.path.join(os.path.dirname(os.path.abspath(CL.__file__)),
                                  "engine", "runner.py")
        with open(runner_src, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        accessor_calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and len(node.args) >= 2
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "YIBAN_ACCOUNT_GAP_MAX"
            and isinstance(node.args[1], ast.Call)
            and isinstance(node.args[1].func, ast.Attribute)
            and node.args[1].func.attr in ("default_required", "default_of")
        ]
        self.assertEqual(len(accessor_calls), 1,
                         "runner.py 的账号间隔缺省必须是 config_loader 的取值调用"
                         "（改成字面量即红）")

        # ③ web 面板：模块常量同源
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        spec = importlib.util.spec_from_file_location(
            "cl_webapp_probe", os.path.join(base, "web", "app.py"))
        webapp = importlib.util.module_from_spec(spec)
        sys.modules["cl_webapp_probe"] = webapp
        try:
            spec.loader.exec_module(webapp)
        except ImportError as exc:   # 只有依赖缺失才跳过；缺省缺失等错误必须响亮失败
            self.skipTest("web/app.py 的依赖不可用：%s" % exc)
        self.assertEqual(webapp.DEFAULT_ACCOUNT_GAP_MAX, gap)


if __name__ == "__main__":
    unittest.main(verbosity=2)
