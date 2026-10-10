# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""「签到分布」默认值与取值域的跨载体一致性守卫。

标签：F · 前端与界面守卫
覆盖：分布键 YIBAN_SIGN_DIST 的缺省值在**全部载体**上同一个值；
    `front` 在两个取值域里都在（名册 domain + settings_api 取值域门）。
对应实现：`config/registry.json` 的 YIBAN_SIGN_DIST、`yiban/engine/schedule.py`
    的 `DEFAULT_SIGN_DIST` 与 `_schedule_config` 派生式子、`yiban/engine/planner.py`
    的 build_plan / plan_stats 回退、`web/` 四处派生式子（me / settings_api /
    env_io / accounts_data）、`.env.example`、`.env.docker.example`、
    `frontend/src/settings/model.js` 的 `SCHEDULE_DEFAULTS.dist`。
关键断言：默认值不是散在各处的字面量，而是**一个值**；谁只改一处（例如只改名册、
    忘了 web 回显式子），此处必红。`front` 必须在名册 domain 与写入门取值域里都登记，
    防"加了模式却存不进去"。
依赖：纯本地——读源码文本 + import 引擎取常量，不执行 JS、不联网、不建库。

背景：默认值这条"设计常量"在仓里有 9 类载体（名册、引擎常量、引擎回退、四个 web
    派生式子、两个 .env 模板、前端默认表）。它们必须逐值相等；只改一处会让回显
    （web）与生效（引擎）分裂——用户看到"均匀分布"而引擎按"提前铺完"排期。
    本守卫是"下一个人只改一处就会红"的那道门。
"""
import json
import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRY = os.path.join(BASE, "config", "registry.json")

#: 会写"分布默认值"派生式子的 web 文件（仓库相对路径）。
_WEB_CARRIERS = (
    os.path.join("web", "routes", "me.py"),
    os.path.join("web", "routes", "settings_api.py"),
    os.path.join("web", "services", "env_io.py"),
    os.path.join("web", "services", "accounts_data.py"),
)
#: 缺省值必须出现的 env 模板。
_ENV_CARRIERS = (".env.example", ".env.docker.example")
#: 前端默认表的源文件。
_FRONTEND_MODEL = os.path.join("frontend", "src", "settings", "model.js")

#: 会被本文件改动、逐个还原的环境键（旧模式键与新键都在内：老配置会派生分布值）。
_ENV_TOUCHED = ("YIBAN_SIGN_DIST", "YIBAN_SIGN_ORDER", "YIBAN_SIGN_MODE")


def _read(rel):
    with open(os.path.join(BASE, rel), encoding="utf-8") as f:
        return f.read()


def _norm(src):
    """压平空白：跨文件的换行/缩进写法不同，逐字符比对会假红。"""
    return re.sub(r"\s+", " ", src)


class SignDistCarrierParityTest(unittest.TestCase):
    """九个载体的分布默认值必须逐值相等，且都等于同一个真实值。"""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _ENV_TOUCHED}
        for k in _ENV_TOUCHED:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _default(self):
        """期望默认值的单源：名册。"""
        with open(REGISTRY, encoding="utf-8") as f:
            return json.load(f)["YIBAN_SIGN_DIST"]["default"]

    def test_registry_default_is_front(self):
        spec = json.loads(_read("config/registry.json"))["YIBAN_SIGN_DIST"]
        self.assertEqual(spec["default"], "front",
                         "分布默认值已定为「提前铺完」front，名册是本值单源")

    def test_engine_constant_and_runtime_fallback_agree(self):
        from yiban.engine import schedule
        default = self._default()
        self.assertEqual(schedule.DEFAULT_SIGN_DIST, default,
                         "引擎常量与名册默认值分叉")
        self.assertEqual(schedule._schedule_config()["dist"], default,
                         "环境无该键时 _schedule_config 的派生值不等于名册默认值")
        # planner 的两处回退必须引用同一常量，而不是各写一份字面量
        src = _read(os.path.join("yiban", "engine", "planner.py"))
        self.assertGreaterEqual(src.count("schedule.DEFAULT_SIGN_DIST"), 2,
                                "build_plan / plan_stats 的回退未引用单源常量")

    def test_four_web_derivations_use_the_default(self):
        default = self._default()
        want = '"normal" if mode == "normal" else "%s"' % default
        for rel in _WEB_CARRIERS:
            self.assertIn(want, _norm(_read(rel)),
                          "%s 的分布派生式子未跟随默认值 %s" % (rel, default))

    def test_env_templates_use_the_default(self):
        default = self._default()
        pat = re.compile(r"^\s*#?\s*YIBAN_SIGN_DIST=%s\s*$" % re.escape(default), re.M)
        for rel in _ENV_CARRIERS:
            self.assertRegex(_read(rel), pat,
                             "%s 的 YIBAN_SIGN_DIST 缺省未跟随 %s" % (rel, default))

    def test_frontend_defaults_table_uses_the_default(self):
        default = self._default()
        self.assertRegex(_read(_FRONTEND_MODEL), r'dist:\s*"%s"' % re.escape(default),
                         "前端 SCHEDULE_DEFAULTS.dist 未跟随默认值 %s" % default)


class SignDistDomainTest(unittest.TestCase):
    """`front` 必须在两个取值域里都在——加了模式却存不进去是最常见的一半修。"""

    def test_front_is_in_registry_domain(self):
        spec = json.loads(_read("config/registry.json"))["YIBAN_SIGN_DIST"]
        self.assertIn("front", spec["domain"], "名册 domain 未收录 front")
        for name in ("uniform", "normal"):
            self.assertIn(name, spec["domain"], "既有取值 %s 不得被挤掉" % name)

    def test_front_is_accepted_by_settings_write_gate(self):
        src = _norm(_read(os.path.join("web", "routes", "settings_api.py")))
        self.assertIn('not in ("uniform", "normal", "front")', src,
                      "设置页写入门取值域未收录 front ⇒ 选了也存不进去")

    def test_front_is_in_planner_and_engine_whitelists(self):
        planner_src = _read(os.path.join("yiban", "engine", "planner.py"))
        self.assertIn("schedule.SIGN_DIST_CHOICES", planner_src,
                      "planner 的分布白名单未走单源词表")
        sched_src = _read(os.path.join("yiban", "engine", "schedule.py"))
        self.assertIn('SIGN_DIST_CHOICES = ("uniform", "normal", "front")', sched_src,
                      "引擎分布词表未收录 front（或不再单源）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
