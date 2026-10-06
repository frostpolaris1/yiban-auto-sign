# -*- coding: utf-8 -*-
"""ba-p08-01 回归：**"空出口 = 该执行体直连"必须在环境写入那一层落实**。

标签：B · 调度：领取/队列/执行体
覆盖：`egress.resolve` 的契约到"空串就是空串"为止（工单原文），本文件钉它的**落实层**
   ——把出口交给执行体的那一次环境写入：
   ①监督进程按槽位拉起执行体时，父环境里的 `YIBAN_PROXY` 不得留在被显式配成直连
      （清单行 `proxy` 为空）的那个槽位上；旧口径 `YIBAN_PROXY_LIST` 的空元素同判；
   ②同一份槽位配置经 `.env` 覆盖注入（`scripts/child_env.build_child_env`——web 手动
      签到与容器调度器共用的环境底座）交进来，也必须落到直连；
   ③兜底常驻执行体（角色 `fallback`，写的是本进程 `os.environ`）同判；
   ④`describe()` 给管理员看的显示必须等于执行体实际出网的出口（工单点名的"两侧都
      看不见偏差"）；
   ⑤非空出口照旧生效——本单不得把另一头改成直连；什么都没配时不得凭空长出代理；
   ⑥边界守卫：生产树里把出口写进环境的地方只许有一处（`yiban/egress.py` 的单点
      `apply_egress`），两个调用点都必须调它。
对应实现：`yiban/egress.py::apply_egress`（唯一落实点）、`yiban/engine/workers.py`
   （两处调用点：并行执行体按槽位、兜底常驻按角色）。
关键断言：改前①②③④⑥红——旧代码只在解析出**非空**串时写键，空出口那一格什么都不做，
   继承来的父级代理原地留存，配置被静默吞掉。④单独钉"显示直连、实际走代理"这个最危险
   的形状。⑥钉住"两处各修一次就是两份实现"：任何人在别处再手写一份 `env["YIBAN_PROXY"]`
   即红。
依赖：临时目录 + mock；不拉真子进程、不连库、无网络、无 skip。
用法（项目根目录）：bash scripts/dev-verify.sh --target "tests/test_egress_direct_applied.py"
"""
import datetime
import io
import os
import re
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import child_env  # noqa: E402  # scripts/ 在 pytest 的 pythonpath 里

from yiban import egress  # noqa: E402
from yiban.engine import workers  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 父环境/`.env` 里那个"不该被直连槽位继承"的代理
PARENT_PROXY = "http://inherited.example:8080"
#: 另一个槽位的正常出口（对照用）
SLOT_B_PROXY = "http://slotb.example:3128"

#: 用例之间必须互不污染：这几枚键若在被测环境之外有值，一律先摘掉
_EGRESS_KEYS = (egress.ENV_SINGLE, egress.ENV_WORKER_LIST, egress.ENV_FALLBACK,
                egress.ENV_WORKER_COUNT, egress.ENV_MANIFEST,
                "YIBAN_EXECUTOR_ID", "YIBAN_RUN_LOCK_NAME")

#: 执行体清单：槽位 0 显式直连（`proxy` 空串）、槽位 1 有出口、槽位 2 是显式直连的兜底行
MANIFEST_TEXT = egress.dump_manifest([
    {"slot": 0, "type": egress.TYPE_WORKER, "proxy": ""},
    {"slot": 1, "type": egress.TYPE_WORKER, "proxy": SLOT_B_PROXY},
    {"slot": 2, "type": egress.TYPE_FALLBACK, "proxy": ""},
])


def _env_without_egress_keys(**extra):
    """干净底座：继承 conftest 的环境设置，只摘掉与本判据有关的键。"""
    env = {k: v for k, v in os.environ.items() if k not in _EGRESS_KEYS}
    env.update(extra)
    return env


class DirectSlotChildEnvTest(unittest.TestCase):
    """①②④⑤：并行执行体的子进程环境必须如实反映"这一格配成直连"。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-egress-direct-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _spawn(self, base_env, slots=(0, 1)):
        """用假 `Popen` 收每个执行体子进程**实际拿到的环境**（不真拉进程）。"""
        captured = []

        class _FakeProc:
            def __init__(self, cmd, env=None, cwd=None):
                captured.append(dict(env or {}))

            def poll(self):
                return 0

        env = _env_without_egress_keys(**base_env)
        env["YIBAN_STATE_DIR"] = self.tmp
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(workers.cli_support, "_acquire_run_lock",
                                  return_value=None), \
                mock.patch.object(workers.accounts_mod, "load_accounts",
                                  return_value=[SimpleNamespace(phone="13800000000")]), \
                mock.patch.object(workers.subprocess, "Popen", _FakeProc), \
                mock.patch.object(workers.time, "sleep"), \
                mock.patch.object(workers.state_io, "mark_worker_started",
                                  lambda slot, **kw: None):
            rc = workers.run_worker_supervisor(len(slots), ["--workers", str(len(slots))],
                                               slots=list(slots))
        self.assertEqual(rc, 0, "监督进程应正常收尾（否则环境断言无意义）")
        by_slot = {}
        for child_env_map in captured:
            index = egress.parse_owner(child_env_map["YIBAN_EXECUTOR_ID"])["index"]
            by_slot[index] = child_env_map
        self.assertEqual(sorted(by_slot), sorted(slots), "每个槽位都应被拉起一次")
        return by_slot

    def test_direct_slot_child_env_does_not_keep_inherited_proxy(self):
        """①：父环境带 `YIBAN_PROXY` ⇒ 显式直连的槽位 0 不得留着它，槽位 1 照旧。"""
        envs = self._spawn({egress.ENV_SINGLE: PARENT_PROXY,
                            egress.ENV_MANIFEST: MANIFEST_TEXT})
        self.assertEqual(envs[0].get(egress.ENV_SINGLE, "").strip(), "",
                         "清单里配成空出口的槽位，子进程环境不得留着父级代理")
        self.assertIn(egress.ENV_SINGLE, envs[0],
                      "「直连」必须是写进环境的显式空值，不只是把键删掉（见 apply_egress）")
        self.assertEqual(envs[1].get(egress.ENV_SINGLE), SLOT_B_PROXY,
                         "非空出口必须照旧写进子进程（本单不得把另一头改成直连）")

    def test_written_empty_value_beats_the_file_fallback(self):
        """⑤：写空串（而不是删键）的理由必须钉住——`.env` 补缺不得把代理请回来。

        `yiban/engine/probe._egress_env` 先铺 `.env`、再让进程环境压过它。删掉键就查不到
        "进程环境"那一项，合并结果退回 `.env` 里的代理：同一格"直连"被吞第二次。
        """
        env_file = os.path.join(self.tmp, ".env")
        with io.open(env_file, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(f"YIBAN_PROXY={PARENT_PROXY}\n")
        envs = self._spawn({egress.ENV_SINGLE: PARENT_PROXY,
                            egress.ENV_MANIFEST: MANIFEST_TEXT})
        child_env_map = dict(envs[0])
        child_env_map["YIBAN_ENV_FILE"] = env_file
        with mock.patch.dict(os.environ, child_env_map, clear=True):
            from yiban.engine import probe
            self.assertEqual(
                egress.resolve(egress.ROLE_SINGLE, env=probe._egress_env()).strip(), "",
                "子进程里按 `.env` 补缺合并出的出口必须仍是直连")

    def test_displayed_egress_matches_child_effective_egress(self):
        """④：`describe()` 给管理员看的"直连（本机出口）"必须等于子进程实际出网代理。"""
        base = {egress.ENV_SINGLE: PARENT_PROXY, egress.ENV_MANIFEST: MANIFEST_TEXT}
        envs = self._spawn(base)
        displayed = egress.describe(egress.resolve(egress.ROLE_WORKER, 0, base))
        actual = egress.describe(envs[0].get(egress.ENV_SINGLE, ""))
        self.assertEqual(displayed, "直连（本机出口）")
        self.assertEqual(actual, displayed,
                         f"显示 {displayed} 与实际 {actual} 分叉（工单点名的静默偏差）")

    def test_file_overlay_env_also_lands_on_direct(self):
        """②：经 `child_env.build_child_env`（web 手动签到 / 容器调度器的环境底座）那条路。

        `.env` 同时写着 `YIBAN_PROXY` 与清单 ⇒ 覆盖注入后环境里**确有**父级代理；这份环境
        交给监督进程后，直连槽位的子进程仍必须没有它。只补"进程环境那一侧"的写法漏这条。
        """
        env_file = os.path.join(self.tmp, ".env")
        with io.open(env_file, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(f"YIBAN_PROXY={PARENT_PROXY}\n")
            fh.write(f"{egress.ENV_MANIFEST}={MANIFEST_TEXT}\n")
        overlay = child_env.build_child_env(env_file, base={
            "PATH": os.environ.get("PATH", ""), "YIBAN_STATE_DIR": self.tmp})
        self.assertEqual(overlay.get(egress.ENV_SINGLE), PARENT_PROXY,
                         "前提：`.env` 覆盖注入确实把父级代理带进了环境（M27 口径）")
        self.assertEqual(overlay.get(egress.ENV_MANIFEST), MANIFEST_TEXT)
        envs = self._spawn(overlay)
        self.assertEqual(envs[0].get(egress.ENV_SINGLE, "").strip(), "",
                         "经 `.env` 覆盖注入进来的代理，同样必须被直连槽位清掉")
        self.assertEqual(envs[1].get(egress.ENV_SINGLE), SLOT_B_PROXY)

    def test_legacy_list_empty_slot_also_direct(self):
        """①旧口径：`YIBAN_PROXY_LIST` 的空元素同样表达直连，必须一并落实。"""
        envs = self._spawn({egress.ENV_SINGLE: PARENT_PROXY,
                            egress.ENV_WORKER_LIST: f",{SLOT_B_PROXY}",
                            egress.ENV_WORKER_COUNT: "2"})
        self.assertEqual(envs[0].get(egress.ENV_SINGLE, "").strip(), "",
                         "旧表里的空元素同样是直连，不留第二条尾巴")
        self.assertEqual(envs[1].get(egress.ENV_SINGLE), SLOT_B_PROXY)


class DirectFallbackRoleEnvTest(unittest.TestCase):
    """③⑤：兜底常驻执行体（角色 `fallback`）写的是本进程环境，同一判据。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-egress-direct-fb-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _run_fallback(self, base_env):
        """跑一次 `run_fallback_worker`，让它写完环境立刻退出，返回退出后的 `YIBAN_PROXY`。"""
        now = datetime.datetime(2026, 10, 6, 7, 0)
        env = _env_without_egress_keys(YIBAN_STATE_DIR=self.tmp, **base_env)
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(workers.clock, "now", lambda: now), \
                mock.patch.object(workers, "_reload_env_into_environ", lambda: None), \
                mock.patch.object(workers.schedule, "_schedule_config",
                                  lambda *a, **k: None), \
                mock.patch.object(workers.state_io, "_clear_fallback_alive",
                                  lambda: None):
            # deadline == 当前时刻：首轮立刻判"已到截止"退出，只留下环境写入那一步
            workers.run_fallback_worker(["--fallback"], deadline=now)
            return os.environ.get(egress.ENV_SINGLE, "<<absent>>")

    def test_direct_fallback_row_clears_inherited_proxy(self):
        base = {egress.ENV_SINGLE: PARENT_PROXY, egress.ENV_MANIFEST: MANIFEST_TEXT}
        self.assertEqual(self._run_fallback(base).strip(), "",
                         "兜底行配成空出口 ⇒ 本进程的 `YIBAN_PROXY` 不得留着继承值")

    def test_fallback_proxy_still_written(self):
        """⑤对照：兜底行有出口时照旧生效（本单不得把非空出口改成直连）。"""
        rows = [{"slot": 0, "type": egress.TYPE_FALLBACK, "proxy": SLOT_B_PROXY}]
        base = {egress.ENV_SINGLE: PARENT_PROXY,
                egress.ENV_MANIFEST: egress.dump_manifest(rows)}
        self.assertEqual(self._run_fallback(base), SLOT_B_PROXY)

    def test_direct_without_any_egress_config_stays_direct(self):
        """⑤对照：什么都没配 ⇒ 结果仍是直连，不得凭空长出代理。"""
        self.assertEqual(self._run_fallback({}).strip(), "")


class EgressEnvWriteSinglePointTest(unittest.TestCase):
    """⑥：边界守卫——"空 = 直连"的落实只许有一处实现。

    工单点名的形状是"两处各修一次就是两份实现"。本类把这条框定钉成门禁：生产树里任何
    把 `YIBAN_PROXY` 写进环境字典的地方（`[…] =` / `setdefault` / `pop`），只允许出现在
    `yiban/egress.py` 的单点内。
    """

    _WRITE_RE = re.compile(
        r"""\[\s*(?:"YIBAN_PROXY"|'YIBAN_PROXY'|ENV_SINGLE|egress\.ENV_SINGLE)\s*\]\s*="""
        r"""|\.setdefault\(\s*(?:"YIBAN_PROXY"|ENV_SINGLE|egress\.ENV_SINGLE)"""
        r"""|\.pop\(\s*(?:"YIBAN_PROXY"|ENV_SINGLE|egress\.ENV_SINGLE)""")
    _ALLOWED = os.path.normcase(os.path.join("yiban", "egress.py"))
    _DIRS = ("yiban", "scripts", "web", "docker")

    def _offenders(self):
        out = []
        for d in self._DIRS:
            for root, _dirs, files in os.walk(os.path.join(BASE, d)):
                if "__pycache__" in root:
                    continue
                for name in files:
                    if not name.endswith(".py"):
                        continue
                    path = os.path.join(root, name)
                    if os.path.normcase(os.path.relpath(path, BASE)) == self._ALLOWED:
                        continue
                    with io.open(path, encoding="utf-8") as fh:
                        if self._WRITE_RE.search(fh.read()):
                            out.append(os.path.relpath(path, BASE))
        return sorted(out)

    def test_only_egress_writes_proxy_into_env(self):
        self.assertEqual(self._offenders(), [],
                         "把出口写进环境的地方必须只有一处（yiban/egress.py）")

    def test_supervisor_and_fallback_call_the_single_point(self):
        """两个调用点必须经单点，不得各自保留一份 if/else 写法。"""
        with io.open(os.path.join(BASE, "yiban", "engine", "workers.py"),
                     encoding="utf-8") as fh:
            text = fh.read()
        self.assertEqual(text.count("egress.apply_egress("), 2,
                         "并行执行体与兜底常驻两处都必须调 egress.apply_egress")


if __name__ == "__main__":
    unittest.main(verbosity=2)
