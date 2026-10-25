# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""配置名册 CI 门禁 `scripts/check-config-registry.py` 的元测试（含突变验证）。

标签：J · 运维：部署/备份/发布
覆盖：门禁的三条判据各配一对"合法样本放行 / 污染样本必红"的活体反例——
    ① 名册自校验（域无序、默认值越域、类型未知、路径键可网页编辑）；
    ② 已登记键的默认值字面量在代码内清零（读取点又写死字面量、常量被重新赋字面量）；
    ③ 未登记键不得出现在生产代码（新增一枚未登记键的读取点）；另有防废门三条
    （空名册、名册缺失、键数不足）与 CI 接线、部署件到位两条。
对应实现：`scripts/check-config-registry.py`、`config/registry.json`、
    `scripts/dev-verify.sh` 的 `run_ci`、`docker/Dockerfile`。
关键断言：**门禁对故意污染样本必须红**。只测"名册合法时门禁绿"是废断言——门禁
    的实现若写错方向（判据恒真），真被污染也照绿；本文件每一条放行断言都配一条
    同形状的污染断言。
依赖：起子进程跑门禁脚本；临时夹具树自带名册与源码；无网络、无 docker CLI。
"""
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE = os.path.join(BASE, "scripts", "check-config-registry.py")
REGISTRY = os.path.join(BASE, "config", "registry.json")
DOCKERFILE = os.path.join(BASE, "docker", "Dockerfile")
ENTRY = os.path.join(BASE, "scripts", "dev-verify.sh")

GOOD_REGISTRY = {
    "_meta": {"version": 1},
    "YIBAN_EGRESS_RATE": {
        "type": "float", "domain": [0.01, 100.0], "default": 1.0,
        "group": ["执行体", "并发"], "doc": "每出口目标速率（次尝试/s）",
        "sensitive": False, "web_editable": False, "tier": "master",
        "reload": "next-round", "default_literal_gate": True,
        "default_symbols": ["RATE_DEFAULT", "_DEFAULT_BUCKET_RATE"],
    },
    "YIBAN_ACCOUNT_GAP_MAX": {
        "type": "seconds", "domain": [0, 3600], "default": 10,
        "group": ["执行体", "节流"], "doc": "相邻两次签到请求的最小间隔",
        "sensitive": False, "web_editable": True, "tier": "master",
        "reload": "next-round",
    },
    "YIBAN_DB_FILE": {
        "type": "path", "default": "yiban.db", "group": ["路径"],
        "doc": "数据库文件", "sensitive": False, "web_editable": False,
        "tier": "master", "reload": "restart",
    },
}

# 合法的源码样本：默认值取自名册访问器，键名在册，两个常量都不再写字面量。
GOOD_SOURCE = '''\
# -*- coding: utf-8 -*-
"""样本：桶速率默认值只在名册里定义一次。"""
import logging

from yiban.adapters.config import loader as config_loader

RATE_DEFAULT = config_loader.default_of("YIBAN_EGRESS_RATE")
_DEFAULT_BUCKET_RATE = RATE_DEFAULT
# 注释里提到 YIBAN_NOT_IN_REGISTRY 不算读取点（注释被剥离）
GAP = 10


def read(env):
    """YIBAN_EGRESS_RATE 的读取点：缺省来自 RATE_DEFAULT，不写字面量。"""
    logging.getLogger("yiban").warning("gap=%s", GAP)
    return float(env.get("YIBAN_EGRESS_RATE", RATE_DEFAULT))
'''

BAD_UNREGISTERED = GOOD_SOURCE + '''

def extra(env):
    """新增一枚未登记键的读取点 ⇒ 门禁必须红。"""
    return env.get("YIBAN_BRAND_NEW_KNOB", "1")
'''

BAD_READ_SITE_LITERAL = GOOD_SOURCE.replace(
    'return float(env.get("YIBAN_EGRESS_RATE", RATE_DEFAULT))',
    'return float(env.get("YIBAN_EGRESS_RATE", 1.0))')

BAD_CONSTANT_LITERAL = GOOD_SOURCE.replace(
    "_DEFAULT_BUCKET_RATE = RATE_DEFAULT", "_DEFAULT_BUCKET_RATE = 1.0")


#: 缺省字面量的模板载体（判据 ④ 的固定读取面，与 --scan 无关）。
TEMPLATE_CARRIERS = (".env.example", ".env.docker.example", "README.md")


def _run_gate(root, registry, *args):
    """显式给 --scan，免得撞上"扫描面不存在即拒判"。

    三个模板载体也进扫描面：判据 ③（未登记键）同样要看它们。判据 ④ 自己按
    固定载体读取，不受 --scan 影响；`--min-template-gated 0` 让夹具的小名册
    不撞真实仓的覆盖下限。
    """
    cmd = [sys.executable, GATE, "--root", root, "--registry", registry,
           "--min-keys", "1", "--min-files", "1", "--min-template-gated", "0",
           "--scan", "yiban"]
    for carrier in TEMPLATE_CARRIERS:
        cmd += ["--scan", carrier]
    cmd += list(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=BASE)


def _template_value(default):
    """缺省值在模板里的规范写法（bool 写 1/0，其余 str）。"""
    if isinstance(default, bool):
        return "1" if default else "0"
    return str(default)


def _auto_register(registry):
    """按夹具名册生成处置册：有缺省即开门，无缺省即豁免（no_default）。"""
    out = {"_meta": {"version": 1, "doc": "夹具自动生成的处置册"}}
    for key, spec in registry.items():
        if key == "_meta":
            continue
        if spec.get("default") is None:
            out[key] = {"default_literal_gate_exempt": {
                "class": "no_default", "reason": "夹具键无缺省，字面量无从断言"}}
        else:
            out[key] = {"default_literal_gate": True}
    return out


def _auto_templates(registry):
    """按夹具名册生成三份模板：每个有缺省的键写一行 `# KEY=缺省`。"""
    lines = ["# %s=%s" % (key, _template_value(spec["default"]))
             for key, spec in registry.items()
             if key != "_meta" and spec.get("default") is not None]
    text = "\n".join(lines) + ("\n" if lines else "")
    return {carrier: text for carrier in TEMPLATE_CARRIERS}


class _Fixture:
    """临时夹具树：`config/registry.json` ＋ 处置册 ＋ 三份模板 ＋ `yiban/probe.py`。

    `register=None` 按名册自动生成处置册；传 dict 即用它；传 False 不写
    （测"处置册缺失"）。`templates=None` 按名册自动生成；传 dict 覆盖同名载体；
    传 False 不写（测载体缺失/塌陷）。
    """

    def __init__(self, registry=None, source=GOOD_SOURCE, files=("yiban/probe.py",),
                 register=None, templates=None):
        self.root = tempfile.mkdtemp(prefix="yiban-registry-gate-")
        os.makedirs(os.path.join(self.root, "config"), exist_ok=True)
        os.makedirs(os.path.join(self.root, "yiban"), exist_ok=True)
        with open(os.path.join(self.root, "yiban", "__init__.py"), "w",
                  encoding="utf-8") as f:
            f.write("")
        self.registry_path = os.path.join(self.root, "config", "registry.json")
        self.register_path = os.path.join(self.root, "config",
                                          "default-gate-register.json")
        if registry is not None:
            with open(self.registry_path, "w", encoding="utf-8") as f:
                json.dump(registry, f, ensure_ascii=False)
        registry = registry if registry is not None else {}
        if register is None:
            register = _auto_register(registry)
        if register is not False:
            with open(self.register_path, "w", encoding="utf-8") as f:
                json.dump(register, f, ensure_ascii=False)
        if templates is None:
            templates = _auto_templates(registry)
        elif templates is not False:
            merged = _auto_templates(registry)
            merged.update(templates)
            templates = merged
        if templates is not False:
            for name, text in templates.items():
                with open(os.path.join(self.root, name), "w",
                          encoding="utf-8") as f:
                    f.write(text)
        if source is not None:
            for rel in files:
                path = os.path.join(self.root, rel.replace("/", os.sep))
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(source)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.root, ignore_errors=True)
        return False


class GateHappyPathTest(unittest.TestCase):
    """合法样本放行——不配这条，下面的红断言分不清"判据生效"与"门禁恒红"。"""

    def test_clean_fixture_passes(self):
        with _Fixture(registry=GOOD_REGISTRY) as fx:
            r = _run_gate(fx.root, fx.registry_path)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("config-registry ok", r.stdout)

    def test_real_repo_passes(self):
        r = subprocess.run([sys.executable, GATE, "--root", BASE], capture_output=True,
                           text=True, timeout=300, cwd=BASE)
        self.assertEqual(r.returncode, 0, r.stdout[-4000:] + r.stderr[-2000:])


class GateDefaultLiteralMutationTest(unittest.TestCase):
    """判据 ②：已登记键的默认值字面量在代码内清零（两种回潮形状各一条）。"""

    def test_read_site_literal_turns_gate_red(self):
        with _Fixture(registry=GOOD_REGISTRY, source=BAD_READ_SITE_LITERAL) as fx:
            r = _run_gate(fx.root, fx.registry_path)
            self.assertEqual(r.returncode, 1, "读取点又写死默认值字面量必须红：" + r.stdout)
            self.assertIn("YIBAN_EGRESS_RATE", r.stdout)

    def test_constant_literal_turns_gate_red(self):
        with _Fixture(registry=GOOD_REGISTRY, source=BAD_CONSTANT_LITERAL) as fx:
            r = _run_gate(fx.root, fx.registry_path)
            self.assertEqual(r.returncode, 1, "常量被重新赋字面量必须红：" + r.stdout)
            self.assertIn("_DEFAULT_BUCKET_RATE", r.stdout)

    def test_domain_literal_is_not_a_violation(self):
        """域字面量（上下界）不在本批判据内：域消费点收口归后续批次。"""
        src = GOOD_SOURCE.replace(
            'return float(env.get("YIBAN_EGRESS_RATE", RATE_DEFAULT))',
            'def clamp(v):\n'
            '    """域边界仍是既有行为，本批不动。"""\n'
            '    return min(max(float(v), 0.01), 100.0)')
        with _Fixture(registry=GOOD_REGISTRY, source=src) as fx:
            r = _run_gate(fx.root, fx.registry_path)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_bare_int_elsewhere_on_key_line_is_not_a_violation(self):
        """假红守卫：键名同行出现独立的 `1`（下标/`k=1`）不是缺省值回潮。

        浮点缺省 1.0 曾同时认裸整数写法 `1`，于是任何同行有 `1` 的行都判红。
        """
        src = GOOD_SOURCE + (
            '\n\ndef probe(env, k=1):\n'
            '    """YIBAN_EGRESS_RATE 的序号参数与缺省值无关。"""\n'
            '    return env.get("YIBAN_EGRESS_RATE", RATE_DEFAULT), k, [0, 1][1]\n')
        with _Fixture(registry=GOOD_REGISTRY, source=src) as fx:
            r = _run_gate(fx.root, fx.registry_path)
            self.assertEqual(r.returncode, 0, "假红：" + r.stdout + r.stderr)

    def test_symbol_assigned_bare_int_is_still_a_violation(self):
        """反向守卫：常量名紧跟 `= 1`（浮点缺省的裸整数写法）仍判红。"""
        with _Fixture(registry=GOOD_REGISTRY,
                      source=GOOD_SOURCE.replace(
                          "_DEFAULT_BUCKET_RATE = RATE_DEFAULT",
                          "_DEFAULT_BUCKET_RATE = 1")) as fx:
            r = _run_gate(fx.root, fx.registry_path)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)


class GateUnregisteredKeyTest(unittest.TestCase):
    """判据 ③：未在名册登记的键不得新增读取点。"""

    def test_unregistered_key_in_code_turns_gate_red(self):
        with _Fixture(registry=GOOD_REGISTRY, source=BAD_UNREGISTERED) as fx:
            r = _run_gate(fx.root, fx.registry_path)
            self.assertEqual(r.returncode, 1, "未登记键必须红：" + r.stdout)
            self.assertIn("YIBAN_BRAND_NEW_KNOB", r.stdout)

    def test_comment_mention_is_not_a_read_point(self):
        """注释里提到未登记键不算读取点（门禁剥注释，与"文本命中"口径分开）。"""
        src = GOOD_SOURCE + "\n# 旧键 YIBAN_RETIRED_KNOB 已删除，仅留一句说明\n"
        with _Fixture(registry=GOOD_REGISTRY, source=src) as fx:
            r = _run_gate(fx.root, fx.registry_path)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_family_prefix_is_not_a_key(self):
        """`YIBAN_MAIL_` 一类族前缀（startswith 前缀串）不是键，不该判红。"""
        src = GOOD_SOURCE + '\n_PREFIX = "YIBAN_MAIL_"\n\n\ndef k(name):\n    return _PREFIX + name\n'
        with _Fixture(registry=GOOD_REGISTRY, source=src) as fx:
            r = _run_gate(fx.root, fx.registry_path)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_shell_case_pattern_line_is_not_treated_as_comment(self):
        """shell 的 `case` 分支写 `*)`：那不是注释，不许把整行判据吞掉（假绿）。"""
        src = ('case "$1" in\n'
               '    *) KEY="YIBAN_BRAND_NEW_KNOB" ;;\n'
               'esac\n')
        with _Fixture(registry=GOOD_REGISTRY, source=src, files=("scripts/x.sh",)) as fx:
            r = _run_gate(fx.root, fx.registry_path, "--scan", "scripts")
            self.assertEqual(r.returncode, 1, "`*)` 行被当注释剥掉了：" + r.stdout + r.stderr)
            self.assertIn("YIBAN_BRAND_NEW_KNOB", r.stdout)

    def test_css_id_selector_line_is_not_treated_as_comment(self):
        """CSS 的 `#id` 选择器不是注释：`.vue`/`.html` 里的键不许被吞（假绿）。"""
        src = '<style>\n#card { --knob: "YIBAN_BRAND_NEW_KNOB"; }\n</style>\n'
        with _Fixture(registry=GOOD_REGISTRY, source=src, files=("frontend/a.vue",)) as fx:
            r = _run_gate(fx.root, fx.registry_path, "--scan", "frontend")
            self.assertEqual(r.returncode, 1, "`#id` 行被当注释剥掉了：" + r.stdout + r.stderr)
            self.assertIn("YIBAN_BRAND_NEW_KNOB", r.stdout)


class GateRegistrySelfCheckTest(unittest.TestCase):
    """判据 ①：名册自校验——类型合法、域有序、默认值在域内。"""

    def _mutate(self, key, **fields):
        data = {k: dict(v) for k, v in GOOD_REGISTRY.items()}
        data[key].update(fields)
        return data

    def test_unordered_domain_turns_gate_red(self):
        with _Fixture(registry=self._mutate("YIBAN_EGRESS_RATE", domain=[100.0, 0.01])) as fx:
            r = _run_gate(fx.root, fx.registry_path)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("域", r.stdout)

    def test_default_outside_domain_turns_gate_red(self):
        with _Fixture(registry=self._mutate("YIBAN_ACCOUNT_GAP_MAX", default=99999)) as fx:
            r = _run_gate(fx.root, fx.registry_path)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)

    def test_unknown_type_turns_gate_red(self):
        with _Fixture(registry=self._mutate("YIBAN_ACCOUNT_GAP_MAX", type="second")) as fx:
            r = _run_gate(fx.root, fx.registry_path)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)

    def test_path_key_web_editable_turns_gate_red(self):
        with _Fixture(registry=self._mutate("YIBAN_DB_FILE", web_editable=True)) as fx:
            r = _run_gate(fx.root, fx.registry_path)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)

    def test_default_less_gated_key_turns_gate_red(self):
        """开了默认值字面量门却不声明缺省 = 判据无处可判，门禁必须红。"""
        data = {k: dict(v) for k, v in GOOD_REGISTRY.items()}
        del data["YIBAN_EGRESS_RATE"]["default"]
        with _Fixture(registry=data) as fx:
            r = _run_gate(fx.root, fx.registry_path)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)


class GateDeadGateGuardTest(unittest.TestCase):
    """防废门：静默的"零命中"不许被读成合规。"""

    def test_empty_registry_is_environment_error(self):
        with _Fixture(registry={}) as fx:
            r = _run_gate(fx.root, fx.registry_path)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn("check-config-registry", r.stderr,
                          "必须是门禁自己的拒判，不是脚本缺失等外部退出码碰巧也是 2")

    def test_missing_registry_is_environment_error(self):
        with _Fixture(registry=None) as fx:
            r = _run_gate(fx.root, fx.registry_path)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn("check-config-registry", r.stderr)

    def test_malformed_json_is_environment_error(self):
        with _Fixture(registry=GOOD_REGISTRY) as fx:
            with open(fx.registry_path, "w", encoding="utf-8") as f:
                f.write("{ 这不是 JSON")
            r = _run_gate(fx.root, fx.registry_path)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn("check-config-registry", r.stderr)

    def test_too_few_keys_is_environment_error(self):
        with _Fixture(registry=GOOD_REGISTRY) as fx:
            cmd = [sys.executable, GATE, "--root", fx.root,
                   "--registry", fx.registry_path, "--min-keys", "99",
                   "--min-files", "1", "--min-template-gated", "0",
                   "--scan", "yiban"]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=BASE)
            self.assertEqual(r.returncode, 2, "键数不足即拒判（防名册被删成空壳）"
                             + r.stdout + r.stderr)
            self.assertIn("check-config-registry", r.stderr)

    def test_scan_surface_missing_is_environment_error(self):
        """扫描面在 --root 下不存在时拒判，不许"没扫到文件"⇒全绿。

        `--scan yiban` 必须先给全：只给 `no-such-dir` 会先被扫描面残缺守卫拦住，
        本用例就变成"因为另一道守卫而红"，目标分支（存在性拒判）无人钉住
        （委托审查 medium-2）。故断言点名的必须是存在性那条。
        """
        with _Fixture(registry=GOOD_REGISTRY) as fx:
            cmd = [sys.executable, GATE, "--root", fx.root,
                   "--registry", fx.registry_path, "--min-keys", "1",
                   "--min-files", "1", "--min-template-gated", "0",
                   "--scan", "yiban", "--scan", "no-such-dir"]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=BASE)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn("不存在", r.stderr, "红的必须是扫描面存在性拒判：" + r.stderr)
            self.assertNotIn("顶层目录", r.stderr,
                             "不许被扫描面残缺守卫抢答（那说明本用例没走到目标分支）")


_IMPORT_SCOPE = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _module_level_key_reads(source, keys):
    """→ 在**模块导入期**读取的配置键集合（函数/类体内的读取不算）。

    `keys` 是关心的键名集合。判据只认"赋值语句的右值里出现键名字面量"这一形态：
    `CAP = parse_env_int("KEY", 1)` 与 `X = os.environ["KEY"]` 都会被抓住；
    函数体内的同一写法是"读时取"，按设计放行。
    """
    tree = ast.parse(source)
    found = set()

    def scan(node):
        if isinstance(node, _IMPORT_SCOPE):
            return
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            for sub in ast.walk(node.value):
                if isinstance(sub, ast.Call):
                    for arg in sub.args:
                        if isinstance(arg, ast.Constant) and arg.value in keys:
                            found.add(arg.value)
                elif isinstance(sub, ast.Subscript):
                    sl = sub.slice
                    if isinstance(sl, ast.Constant) and sl.value in keys:
                        found.add(sl.value)
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                scan(child)

    for stmt in tree.body:
        scan(stmt)
    return found


class HotKeyImportSnapshotTest(unittest.TestCase):
    """`reload: hot` 的键不得在模块导入期快照（工单 ba-p04-07 的形状门）。

    常驻进程（兜底执行体）运行期改 `.env` 只经 `workers._reload_env_into_environ()`
    并回进程环境；键若在导入期取过一次，白名单再全也改变不了已求值的常量。
    `hot` 的语义就是"改了立刻生效"，故这是一条任何时刻都不该被违反的性质，
    不是某条调用链的编舞。
    """

    SCAN_DIRS = ("yiban", "web", "scripts", "docker")

    def _hot_keys(self):
        with open(REGISTRY, encoding="utf-8") as f:
            data = json.load(f)
        return {k for k, v in data.items()
                if isinstance(v, dict) and v.get("reload") == "hot"}

    def test_no_hot_key_is_snapshotted_at_import(self):
        hot = self._hot_keys()
        self.assertGreaterEqual(len(hot), 40, "名册的 reload=hot 集塌了，门会假绿")
        offenders = {}
        scanned = 0
        for sub in self.SCAN_DIRS:
            for dirpath, dirs, files in os.walk(os.path.join(BASE, sub)):
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                for name in files:
                    if not name.endswith(".py"):
                        continue
                    path = os.path.join(dirpath, name)
                    scanned += 1
                    with open(path, encoding="utf-8", errors="ignore") as f:
                        src = f.read()
                    try:
                        hits = _module_level_key_reads(src, hot)
                    except SyntaxError:
                        continue
                    if hits:
                        rel = os.path.relpath(path, BASE).replace(os.sep, "/")
                        offenders[rel] = sorted(hits)
        self.assertGreaterEqual(scanned, 100, "扫描面塌了 ⇒ 零命中不等于合规")
        self.assertEqual(
            offenders, {},
            "hot 键在导入期被快照；常驻进程改 .env 永远不生效（见 ba-p04-07）")

    def test_detector_has_teeth(self):
        hot = {"YIBAN_MAIL_USER_FAIL_DAILY_CAP"}
        bad = 'CAP = parse_env_int("YIBAN_MAIL_USER_FAIL_DAILY_CAP", 1)\n'
        self.assertEqual(_module_level_key_reads(bad, hot), hot)
        bad_sub = 'CAP = int(os.environ["YIBAN_MAIL_USER_FAIL_DAILY_CAP"])\n'
        self.assertEqual(_module_level_key_reads(bad_sub, hot), hot)
        # 函数体内读取＝读时取，放行
        good = ('def cap():\n'
                '    return parse_env_int("YIBAN_MAIL_USER_FAIL_DAILY_CAP", 1)\n')
        self.assertEqual(_module_level_key_reads(good, hot), set())
        # 非 hot 档（restart）允许导入期快照，不在本判据范围
        restart = 'X = parse_env_int("YIBAN_QUOTA_STATE_FLUSH_MAX", 64)\n'
        self.assertEqual(_module_level_key_reads(restart, hot), set())


class GateRealRegistryContentTest(unittest.TestCase):
    """真实名册的内容底线：三枚多副本默认值键在册且开了默认值字面量门。"""

    def setUp(self):
        with open(REGISTRY, encoding="utf-8") as f:
            self.data = json.load(f)

    def test_gated_keys_are_registered_with_gate_flag(self):
        for key in ("YIBAN_EGRESS_RATE", "YIBAN_ACCOUNT_GAP_MAX",
                    "YIBAN_MIN_EXEC_GAP"):
            self.assertIn(key, self.data)
            spec = self.data[key]
            self.assertIs(spec.get("default_literal_gate"), True,
                          "%s 是多副本默认值键，必须开门" % key)
            self.assertTrue(spec.get("default_symbols"),
                            "%s 要登记承载缺省的常量名，否则常量回潮抓不住" % key)
            self.assertIn("default", spec)
            self.assertIn("domain", spec)

    def test_registry_covers_a_wide_key_universe(self):
        keys = [k for k in self.data if k.startswith("YIBAN_")]
        self.assertGreaterEqual(len(keys), 120,
                                "名册是键宇宙的唯一定义点，覆盖率过低即名册残缺")
        for key, spec in self.data.items():
            if key.startswith("_"):
                continue
            self.assertIn("doc", spec, "%s 缺 doc" % key)
            self.assertIn("group", spec, "%s 缺 group" % key)
            self.assertIn("sensitive", spec, "%s 缺 sensitive" % key)
            self.assertIn("reload", spec, "%s 缺 reload" % key)
            self.assertIn("tier", spec, "%s 缺 tier" % key)

    def test_real_registry_is_tracked_and_shipped(self):
        """零改动启动前提：名册必须入库，且进镜像（否则引擎 import 期读不到）。"""
        self.assertTrue(os.path.isfile(REGISTRY))
        with open(DOCKERFILE, encoding="utf-8") as f:
            dockerfile = f.read()
        self.assertRegex(dockerfile, r"(?m)^\s*COPY\s+config/?\s",
                         "docker/Dockerfile 必须 COPY config/（漏了容器里名册不存在）")
        if shutil.which("git"):
            r = subprocess.run(["git", "ls-files", "--error-unmatch",
                                "config/registry.json"], cwd=BASE,
                               capture_output=True, text=True)
            if r.returncode != 0 and os.path.isdir(os.path.join(BASE, ".git")):
                self.fail("config/registry.json 未入库：%s" % (r.stderr or r.stdout))


class GateScanSurfaceTest(unittest.TestCase):
    """扫描面残缺守卫：新开的生产目录不许整片漏出判据（漏了门禁照旧判绿）。

    生产面的判据是 **git 跟踪集**：生产目录必被跟踪入库（部署只带跟踪文件），
    被忽略/未入库的本地工具目录永非生产面。放行断言与"新开且被跟踪的目录仍红"
    的反向断言成对，防止把假阳性修成"真门一起废掉"。
    """

    SRC = 'KEY = "YIBAN_EGRESS_RATE"\n'

    def _add_top_dir(self, fx, name="newprod"):
        os.makedirs(os.path.join(fx.root, name), exist_ok=True)
        with open(os.path.join(fx.root, name, "a.py"), "w",
                  encoding="utf-8") as f:
            f.write(self.SRC)

    def _git(self, root, *args):
        return subprocess.run(["git", "-C", root, *args], capture_output=True,
                              text=True, timeout=60)

    def _init_repo(self, root):
        self.assertEqual(self._git(root, "init", "-q").returncode, 0)
        self._git(root, "config", "user.email", "gate@example.invalid")
        self._git(root, "config", "user.name", "gate")

    def _exclude_in_info(self, fx, pattern):
        """.git/info/exclude 追加一条（.beads 的形状：忽略规则不在 .gitignore 里）。"""
        info = os.path.join(fx.root, ".git", "info")
        os.makedirs(info, exist_ok=True)
        with open(os.path.join(info, "exclude"), "a", encoding="utf-8") as f:
            f.write(pattern + "\n")

    def _track_scan_face(self, fx):
        """把夹具的扫描面（config/ + yiban/）入库，得到**混合**跟踪集。

        只 `git init` 而不 add 会让索引全空——那时"按跟踪集判生产面"对任何目录都得
        不出结论。现实的形状是"扫描面被跟踪、工具目录未被跟踪"，夹具必须照它搭。
        """
        r = self._git(fx.root, "add", "config", "yiban")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotEqual(self._git(fx.root, "ls-files").stdout.strip(), "",
                            "夹具跟踪集不得为空（否则验的是「全空索引」而非混合形状）")

    def _add_undecodable_file(self, fx):
        """造一个名字含非法 UTF-8 字节的**入库**文件；文件系统不接受时返回 None。

        名字用 `os.fsdecode(b"...")` 表示（与本用例要复现的 git 原始字节同形）。
        """
        rel = os.fsdecode(b"bad\xff.py")
        try:
            with open(os.path.join(fx.root, rel), "w", encoding="utf-8") as f:
                f.write("")
        except (OSError, UnicodeError):
            return None
        if self._git(fx.root, "add", "--", rel).returncode != 0:
            return None
        return rel

    def test_unscanned_top_dir_with_source_turns_gate_red(self):
        with _Fixture(registry=GOOD_REGISTRY) as fx:
            self._add_top_dir(fx)
            r = _run_gate(fx.root, fx.registry_path)
            self.assertEqual(r.returncode, 2,
                             "未覆盖的顶层目录必须拒判：" + r.stdout + r.stderr)
            self.assertIn("newprod", r.stderr)

    def test_extra_dir_scanned_explicitly_passes(self):
        with _Fixture(registry=GOOD_REGISTRY) as fx:
            self._add_top_dir(fx)
            r = _run_gate(fx.root, fx.registry_path, "--scan", "newprod")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    @unittest.skipUnless(shutil.which("git"), "本用例需要 git CLI（门按跟踪集判生产面）")
    def test_untracked_tool_dir_with_source_passes(self):
        """未入库（无被跟踪文件）的顶层目录不是生产面，门禁必须放行。

        复现本机恒伪红：`.beads`/`.qoder`/`.tmp`/`.zcode` 这些本地工具目录里有被扫
        扩展名的文件，但它们一个都没入库 ⇒ 不是生产面。夹具是**混合**跟踪集（扫描面
        已入库），故放行不是因为"索引全空"。放行它们不是放宽门：真正的生产目录被
        跟踪，仍由 test_tracked_top_dir_with_source_still_red 钉住。
        """
        with _Fixture(registry=GOOD_REGISTRY) as fx:
            self._init_repo(fx.root)
            self._track_scan_face(fx)
            self._add_top_dir(fx, "toolcache")
            r = _run_gate(fx.root, fx.registry_path)
            self.assertEqual(r.returncode, 0,
                             "未入库的工具目录不该让门拒判：" + r.stdout + r.stderr)

    @unittest.skipUnless(shutil.which("git"), "本用例需要 git CLI（门按跟踪集判生产面）")
    def test_tracked_tool_dir_matching_ignore_rule_still_red(self):
        """**被跟踪**的顶层目录即使命中忽略规则，仍是生产面 ⇒ 必须拒判。

        承载的不变式：判据是**跟踪集**，不是忽略规则。忽略规则只写在
        `.git/info/exclude`（`.beads` 的形状）时尤其要看清——dev-verify 的副本用源索引
        重建 `.git`，副本的 info/exclude 是空的，任何"按忽略规则判生产面"的实现都会
        在副本与工作树之间摇摆。被跟踪件必进提交与部署，忽略规则不改变这一点。

        （前一版此用例用的是**未跟踪**目录＋info/exclude：真正让它绿的是
        `git add` 与否，那条 info/exclude 对断言无因果，等于第二条
        `test_untracked_tool_dir_with_source_passes`。改为被跟踪形状才承重。）
        """
        with _Fixture(registry=GOOD_REGISTRY) as fx:
            self._init_repo(fx.root)
            self._track_scan_face(fx)
            self._add_top_dir(fx, "toolcache")
            self.assertEqual(self._git(fx.root, "add", "toolcache").returncode, 0)
            self._exclude_in_info(fx, "toolcache/")   # 忽略规则不豁免被跟踪目录
            r = _run_gate(fx.root, fx.registry_path)
            self.assertEqual(r.returncode, 2,
                             "被跟踪的目录命中忽略规则也必须拒判：" + r.stdout + r.stderr)
            self.assertIn("toolcache", r.stderr)

    @unittest.skipUnless(shutil.which("git"), "本用例需要 git CLI（门按跟踪集判生产面）")
    def test_undecodable_tracked_name_does_not_crash_gate(self):
        """索引含非法 UTF-8 文件名 ⇒ 门必须照常判，不得以 traceback 崩（exit 1）。

        跟踪集查询原先用 `subprocess.run(..., text=True)`：git 输出的文件名若是原始
        非 UTF-8 字节，解码当场抛 `UnicodeDecodeError`。该异常不在 `except` 面内 ⇒ 冒到
        顶层 ⇒ Python 打 traceback 并以 **exit 1** 收场。exit 1 在本门的语义是"有违规"，
        而真实情形是"门压根没跑成"——fail 方向错（正确方向是 fail-closed：不跳过任何
        目录照旧判，或以 exit 2 拒判）。
        """
        with _Fixture(registry=GOOD_REGISTRY) as fx:
            self._init_repo(fx.root)
            self._track_scan_face(fx)
            rel = self._add_undecodable_file(fx)
            if rel is None:
                self.skipTest("文件系统不接受非法 UTF-8 名（本用例面向 POSIX 检出）")
            r = _run_gate(fx.root, fx.registry_path)
            self.assertNotIn("Traceback", r.stderr,
                             "门不得以 Python traceback 崩：" + r.stderr[-2000:])
            self.assertEqual(r.returncode, 0,
                             "非法 UTF-8 名不得改变判据结论：" + r.stdout + r.stderr)

    @unittest.skipUnless(shutil.which("git"), "本用例需要 git CLI（门按跟踪集判生产面）")
    def test_empty_index_new_prod_dir_still_red(self):
        """空索引的检出（`git init` 后一个文件都没入库）⇒ 门必须退回 fail-closed。

        空索引下"按跟踪集判生产面"会得出"没有任何目录是生产面"。若据此跳过全部候选，
        本应拒判的新生产目录反而放绿——这是 fail-open，方向错在危险的一边。无法判定
        生产面时退回旧行为（每个顶层目录都算候选）才安全。
        """
        with _Fixture(registry=GOOD_REGISTRY) as fx:
            self._init_repo(fx.root)              # 只 init、不 add ⇒ 索引为空
            self.assertEqual(self._git(fx.root, "ls-files").stdout, "")
            self._add_top_dir(fx, "newprod")
            r = _run_gate(fx.root, fx.registry_path)
            self.assertEqual(r.returncode, 2,
                             "空索引不得让新生产目录放绿：" + r.stdout + r.stderr)
            self.assertIn("newprod", r.stderr)

    @unittest.skipUnless(shutil.which("git"), "本用例需要 git CLI（门按跟踪集判生产面）")
    def test_tracked_top_dir_with_source_still_red(self):
        """反向守卫：新开且**被跟踪**的顶层生产目录必须仍拒判（防修假阳性把门废掉）。"""
        with _Fixture(registry=GOOD_REGISTRY) as fx:
            self._init_repo(fx.root)
            self._add_top_dir(fx, "newprod")
            self.assertEqual(self._git(fx.root, "add", "newprod").returncode, 0)
            r = _run_gate(fx.root, fx.registry_path)
            self.assertEqual(r.returncode, 2,
                             "被跟踪的新生产目录必须仍拒判：" + r.stdout + r.stderr)
            self.assertIn("newprod", r.stderr)


class GateStringDefaultTest(unittest.TestCase):
    """字符串缺省值的两种引号写法都在判据内（只认双引号会漏掉一半回潮）。"""

    def _registry(self):
        return {
            "_meta": {"version": 1},
            "YIBAN_SIGN_ORDER": {
                "type": "enum", "domain": ["sequence", "random"], "default": "sequence",
                "group": ["调度"], "doc": "排序模式", "sensitive": False,
                "web_editable": True, "tier": "admin", "reload": "next-round",
                "default_literal_gate": True, "default_symbols": ["SIGN_ORDER_DEFAULT"],
            },
        }

    def test_double_quoted_restatement_turns_gate_red(self):
        src = ('SIGN_ORDER_DEFAULT = config_loader.default_of("YIBAN_SIGN_ORDER")\n'
               'def read(env):\n'
               '    return env.get("YIBAN_SIGN_ORDER", "sequence")\n')
        with _Fixture(registry=self._registry(), source=src) as fx:
            r = _run_gate(fx.root, fx.registry_path)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)

    def test_single_quoted_restatement_turns_gate_red(self):
        src = ("SIGN_ORDER_DEFAULT = config_loader.default_of('YIBAN_SIGN_ORDER')\n"
               "def read(env):\n"
               "    return env.get('YIBAN_SIGN_ORDER', 'sequence')\n")
        with _Fixture(registry=self._registry(), source=src) as fx:
            r = _run_gate(fx.root, fx.registry_path)
            self.assertEqual(r.returncode, 1,
                             "单引号写法必须同样判红：" + r.stdout + r.stderr)

    def test_constant_restatement_turns_gate_red(self):
        src = ('SIGN_ORDER_DEFAULT = "sequence"\n'
               'def read(env):\n'
               '    return env.get("YIBAN_SIGN_ORDER", SIGN_ORDER_DEFAULT)\n')
        with _Fixture(registry=self._registry(), source=src) as fx:
            r = _run_gate(fx.root, fx.registry_path)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)


class GateTemplateDefaultEqualityTest(unittest.TestCase):
    """判据 ④：开门键在模板载体里的字面量必须等于名册缺省（改任一侧必红）。

    模板载体（`.env.example` / `.env.docker.example` / `README.md`）里的
    `KEY=值` 是**用户可见的缺省声明**：复制模板即得到该值。名册是唯一定义点，
    故任一侧漂移都必须红。消亡/塌陷（载体为空、载体缺失、开门键在模板里消失、
    开门键数跌破下限）一律拒判（退出码 2 或 1），不许静默变成零命中。
    """

    SOURCE = 'KEY = "YIBAN_PROBE_TIME"\n'

    def _registry(self):
        return {
            "_meta": {"version": 1},
            "YIBAN_PROBE_TIME": {
                "type": "hhmm", "default": "20:00", "group": ["探针"],
                "doc": "探针触发时刻", "sensitive": False, "web_editable": True,
                "tier": "admin", "reload": "next-round",
            },
            "YIBAN_MAIL_SMTP_PORT": {
                "type": "int", "default": 465, "group": ["通知"],
                "doc": "SMTP 端口", "sensitive": False, "web_editable": False,
                "tier": "master", "reload": "restart",
            },
            "YIBAN_SUNDAY_SIGN": {
                "type": "bool", "default": False, "group": ["调度"],
                "doc": "周日签到开关", "sensitive": False, "web_editable": True,
                "tier": "admin", "reload": "next-round",
            },
        }

    def _fixture(self, **kwargs):
        return _Fixture(registry=self._registry(), source=self.SOURCE, **kwargs)

    def test_clean_templates_pass(self):
        with self._fixture() as fx:
            r = _run_gate(fx.root, fx.registry_path)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("模板缺省字面量门: 3 键", r.stdout)

    def test_registry_default_change_turns_gate_red(self):
        """突变 ①：改名册缺省，模板不动 ⇒ 必红。"""
        with self._fixture() as fx:
            with open(fx.registry_path, encoding="utf-8") as f:
                data = json.load(f)
            data["YIBAN_PROBE_TIME"]["default"] = "21:00"
            with open(fx.registry_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            r = _run_gate(fx.root, fx.registry_path)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("YIBAN_PROBE_TIME", r.stdout)

    def test_env_example_value_change_turns_gate_red(self):
        """突变 ③：改 `.env.example` 的覆盖值 ⇒ 必红。"""
        with self._fixture(templates={
                ".env.example": "# YIBAN_PROBE_TIME=20:00\n"
                                "# YIBAN_MAIL_SMTP_PORT=465\n"
                                "# YIBAN_SUNDAY_SIGN=1\n"}) as fx:
            r = _run_gate(fx.root, fx.registry_path)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn(".env.example", r.stdout)
            self.assertIn("YIBAN_SUNDAY_SIGN", r.stdout)

    def test_readme_value_change_turns_gate_red(self):
        """README 里的 `KEY=值` 同样是缺省声明，改它必须红。"""
        with self._fixture(templates={
                "README.md": "YIBAN_MAIL_SMTP_PORT=587\n"}) as fx:
            r = _run_gate(fx.root, fx.registry_path)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("README.md", r.stdout)
            self.assertIn("YIBAN_MAIL_SMTP_PORT", r.stdout)

    def test_bool_true_word_is_accepted(self):
        """值写法等价不算漂移：bool 缺省 true 接受模板写 `true`（也接受 `1`）。"""
        registry = self._registry()
        registry["YIBAN_SUNDAY_SIGN"]["default"] = True
        with _Fixture(registry=registry, source=self.SOURCE, templates={
                ".env.docker.example": "# YIBAN_SUNDAY_SIGN=true\n"}) as fx:
            r = _run_gate(fx.root, fx.registry_path)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_gate_key_absent_from_templates_turns_gate_red(self):
        """开门键在模板里消失 ⇒ 门变空转，必须红（不许静默降级）。"""
        with self._fixture(templates={
                ".env.example": "# YIBAN_MAIL_SMTP_PORT=465\n"
                                "# YIBAN_SUNDAY_SIGN=0\n",
                ".env.docker.example": "# YIBAN_MAIL_SMTP_PORT=465\n"
                                      "# YIBAN_SUNDAY_SIGN=0\n",
                "README.md": "YIBAN_MAIL_SMTP_PORT=465\nYIBAN_SUNDAY_SIGN=0\n",
        }) as fx:
            r = _run_gate(fx.root, fx.registry_path)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("YIBAN_PROBE_TIME", r.stdout)

    def test_example_diverges_exemption_passes(self):
        """显式豁免（示例值刻意不同）放行；理由不参与判定但要存在。"""
        register = _auto_register(self._registry())
        register["YIBAN_PROBE_TIME"] = {"default_literal_gate_exempt": {
            "class": "example_diverges", "reason": "夹具演示刻意不同的示例值"}}
        with self._fixture(register=register, templates={
                ".env.example": "# YIBAN_PROBE_TIME=23:59\n"}) as fx:
            r = _run_gate(fx.root, fx.registry_path)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_exempt_without_reason_is_environment_error(self):
        register = _auto_register(self._registry())
        register["YIBAN_PROBE_TIME"] = {"default_literal_gate_exempt": {
            "class": "example_diverges", "reason": "  "}}
        with self._fixture(register=register) as fx:
            r = _run_gate(fx.root, fx.registry_path)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn("reason", r.stderr)

    def test_exempt_unknown_class_is_environment_error(self):
        register = _auto_register(self._registry())
        register["YIBAN_PROBE_TIME"] = {"default_literal_gate_exempt": {
            "class": "looks_fine_to_me", "reason": "自造类别"}}
        with self._fixture(register=register) as fx:
            r = _run_gate(fx.root, fx.registry_path)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn("class", r.stderr)

    def test_register_missing_key_turns_gate_red(self):
        register = _auto_register(self._registry())
        del register["YIBAN_PROBE_TIME"]
        with self._fixture(register=register) as fx:
            r = _run_gate(fx.root, fx.registry_path)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("YIBAN_PROBE_TIME", r.stdout)

    def test_register_stale_key_turns_gate_red(self):
        register = _auto_register(self._registry())
        register["YIBAN_RETIRED_KNOB"] = {"default_literal_gate_exempt": {
            "class": "no_default", "reason": "已不在名册"}}
        with self._fixture(register=register) as fx:
            r = _run_gate(fx.root, fx.registry_path)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("YIBAN_RETIRED_KNOB", r.stdout)

    def test_register_with_both_fields_is_environment_error(self):
        register = _auto_register(self._registry())
        register["YIBAN_PROBE_TIME"]["default_literal_gate_exempt"] = {
            "class": "no_default", "reason": "两个字段同时给"}
        with self._fixture(register=register) as fx:
            r = _run_gate(fx.root, fx.registry_path)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)

    def test_register_file_missing_is_environment_error(self):
        """处置册缺失 ⇒ 无法判"154 键逐键处置"，必须拒判（不是放绿）。"""
        with self._fixture(register=False) as fx:
            r = _run_gate(fx.root, fx.registry_path)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn("处置册", r.stderr)

    def test_empty_carrier_is_environment_error(self):
        with self._fixture(templates={".env.docker.example": ""}) as fx:
            r = _run_gate(fx.root, fx.registry_path)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn(".env.docker.example", r.stderr)

    def test_missing_carrier_is_environment_error(self):
        with self._fixture(templates=False) as fx:
            r = _run_gate(fx.root, fx.registry_path)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn(".env.example", r.stderr)

    def test_min_template_gated_floor_is_environment_error(self):
        """开门键数跌破下限 ⇒ 拒判（防把处置册整体改成豁免来废掉门）。"""
        with self._fixture() as fx:
            r = _run_gate(fx.root, fx.registry_path, "--min-template-gated", "4")
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn("min-template-gated", r.stderr)

    def test_template_default_is_not_treated_as_code_restatement(self):
        """模板里的 `KEY=缺省` 是**允许且要求**的写法，不得撞判据 ②（代码清零）。"""
        with self._fixture(templates={
                ".env.example": "# YIBAN_MAIL_SMTP_PORT=465\n"}) as fx:
            r = _run_gate(fx.root, fx.registry_path)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertNotIn("默认值字面量:", r.stdout)

    def test_symbol_assigned_other_literal_turns_red(self):
        """突变 ②：承载常量被赋**任何**字面量都红（不止"等于缺省"那一种）。"""
        src = GOOD_SOURCE.replace(
            "_DEFAULT_BUCKET_RATE = RATE_DEFAULT", "_DEFAULT_BUCKET_RATE = 7")
        with _Fixture(registry=GOOD_REGISTRY, source=src) as fx:
            r = _run_gate(fx.root, fx.registry_path)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("_DEFAULT_BUCKET_RATE", r.stdout)


class GateRealRegisterContentTest(unittest.TestCase):
    """真实处置册的内容底线：154 键逐键有处置，开门面不许被掏空。"""

    REGISTER = os.path.join(BASE, "config", "default-gate-register.json")

    def setUp(self):
        with open(REGISTRY, encoding="utf-8") as f:
            self.registry = json.load(f)
        with open(self.REGISTER, encoding="utf-8") as f:
            self.register = json.load(f)

    def test_every_registry_key_has_exactly_one_disposition(self):
        known = {k for k in self.registry if k != "_meta"}
        entries = {k for k in self.register if k != "_meta"}
        self.assertEqual(known, entries, "处置册与名册的键集合必须完全一致")
        for key, entry in self.register.items():
            if key == "_meta":
                continue
            self.assertEqual(len(entry), 1, "%s 必须恰好一种处置" % key)
            self.assertIn(next(iter(entry)),
                          ("default_literal_gate", "default_literal_gate_exempt"))

    def test_template_gate_keeps_a_wide_face(self):
        gated = [k for k, v in self.register.items()
                 if v.get("default_literal_gate") is True]
        self.assertGreaterEqual(len(gated), 60,
                                "开门键数过少即处置册被整体掏空：%d" % len(gated))


class GateCiWiringTest(unittest.TestCase):
    """门禁必须真进 CI，不许只当仓里的摆设。"""

    def test_gate_and_metatest_are_in_ci_subset(self):
        with open(ENTRY, encoding="utf-8") as f:
            text = f.read()
        self.assertIn('"$py" scripts/check-config-registry.py', text,
                      "门禁未接入 scripts/dev-verify.sh 的 run_ci")
        self.assertIn("tests/test_config_registry_gate.py", text,
                      "门禁元测试未接入 run_ci")


if __name__ == "__main__":
    unittest.main(verbosity=2)
