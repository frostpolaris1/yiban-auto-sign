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

from yiban import config_loader

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


def _run_gate(root, registry, *args):
    """夹具树只有 yiban/ 一个扫描面：显式给 --scan，免得撞上"扫描面不存在即拒判"。"""
    cmd = [sys.executable, GATE, "--root", root, "--registry", registry,
           "--min-keys", "1", "--min-files", "1", "--scan", "yiban", *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=BASE)


class _Fixture:
    """临时夹具树：`config/registry.json` + `yiban/probe.py`。"""

    def __init__(self, registry=None, source=GOOD_SOURCE, files=("yiban/probe.py",)):
        self.root = tempfile.mkdtemp(prefix="yiban-registry-gate-")
        os.makedirs(os.path.join(self.root, "config"), exist_ok=True)
        os.makedirs(os.path.join(self.root, "yiban"), exist_ok=True)
        with open(os.path.join(self.root, "yiban", "__init__.py"), "w",
                  encoding="utf-8") as f:
            f.write("")
        self.registry_path = os.path.join(self.root, "config", "registry.json")
        if registry is not None:
            with open(self.registry_path, "w", encoding="utf-8") as f:
                json.dump(registry, f, ensure_ascii=False)
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
                   "--min-files", "1", "--scan", "yiban"]
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
                   "--min-files", "1", "--scan", "yiban", "--scan", "no-such-dir"]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=BASE)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn("不存在", r.stderr, "红的必须是扫描面存在性拒判：" + r.stderr)
            self.assertNotIn("顶层目录", r.stderr,
                             "不许被扫描面残缺守卫抢答（那说明本用例没走到目标分支）")


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
    """扫描面残缺守卫：新开的生产目录不许整片漏出判据（漏了门禁照旧判绿）。"""

    SRC = 'KEY = "YIBAN_EGRESS_RATE"\n'

    def _add_top_dir(self, fx):
        os.makedirs(os.path.join(fx.root, "newprod"), exist_ok=True)
        with open(os.path.join(fx.root, "newprod", "a.py"), "w",
                  encoding="utf-8") as f:
            f.write(self.SRC)

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
