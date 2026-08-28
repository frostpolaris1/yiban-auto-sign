# -*- coding: utf-8 -*-
"""容器内签到调度器（docker/scheduler.py）回归测试。

用法（在项目根目录）：
    py -m pytest tests/test_container_scheduler.py -v
    py tests/test_container_scheduler.py          # 无 pytest 也可直接运行

覆盖 2026-08-28 对抗性审查批次 5 的两个阻断级缺陷：

- **F1 补签去重恒失效**：`_signed_today()` 原读 `sign-status-<date>.txt`，而该文件
  只有**宿主** `run.sh:67` 会写；容器内 `signin.py:1801` 写的是 `sign-state-<date>.json`。
  导致容器内去重判断恒为 False，07:10 补签永不跳过 —— **每天全量签到执行两遍**，
  请求量翻倍，直接放大风控暴露（与调度 v2 的设计目标相悖）。
- **F2 子进程环境只在启动时构造一次**：`build_child_env()` 原先写在 `while True` 之外，
  管理员在 Web 后台改的 `YIBAN_GLOBAL_PAUSE`（一键暂停）/ `YIBAN_SUNDAY_SIGN` /
  `YIBAN_PROBE_*` 在容器重启前**静默不生效**。
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))

_SCHED_PATH = os.path.join(BASE, "docker", "scheduler.py")


def _load_sched():
    """按文件路径加载调度器（它位于 docker/ 而非包内）。"""
    spec = importlib.util.spec_from_file_location("container_scheduler", _SCHED_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Stop(Exception):
    """测试中用于打断 main_loop 的无限循环（在首次 sleep 处抛出）。"""


def _stop_sleep(_seconds):
    """替换 sched.time.sleep：首次调用即抛出 _Stop，用于跳出无限循环。"""
    raise _Stop()


def _stub_module(**members):
    """构造一个只含指定成员的模块桩，避免污染真实的 time / subprocess。"""
    return type("_Stub", (), {k: staticmethod(v) for k, v in members.items()})()


def _today():
    return datetime.now().strftime("%Y-%m-%d")


class SignedTodayTest(unittest.TestCase):
    """F1：补签去重必须同时认得容器内的 sign-state-*.json 与宿主的旧 txt。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sched-")
        self.sched = _load_sched()
        self.sched.STATEDIR = self.tmp
        self.sched.ENV_FILE = os.path.join(self.tmp, ".env")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers --
    def _state_path(self):
        return os.path.join(self.tmp, f"sign-state-{_today()}.json")

    def _legacy_path(self):
        return os.path.join(self.tmp, f"sign-status-{_today()}.txt")

    def _write_state(self, data):
        with open(self._state_path(), "w", encoding="utf-8") as f:
            json.dump(data, f)

    # -- F1 核心 --
    def test_container_json_success_marks_signed(self):
        """容器内首签已成功 → 补签必须跳过（F1 的主断言）。"""
        self._write_state({"13800000000": {"status": "success", "message": "签到成功"}})
        self.assertTrue(
            self.sched._signed_today(),
            "容器内 sign-state-*.json 含 success 时 _signed_today() 应为 True，否则补签不会跳过",
        )

    def test_container_json_already_marks_signed(self):
        """服务器返回『今日已签到』同样视为已完成。"""
        self._write_state({"13800000000": {"status": "already"}})
        self.assertTrue(self.sched._signed_today())

    def test_container_json_failed_allows_retry(self):
        """首签失败 → 补签应当执行（不能一刀切跳过）。"""
        self._write_state({"13800000000": {"status": "failed"}})
        self.assertFalse(self.sched._signed_today())

    def test_container_json_retrying_allows_retry(self):
        self._write_state({"13800000000": {"status": "retrying"}})
        self.assertFalse(self.sched._signed_today())

    def test_one_success_among_many_marks_signed(self):
        """任一账号成功即视为当日已签到（补签是整批行为）。"""
        self._write_state({
            "13800000001": {"status": "failed"},
            "13800000002": {"status": "success"},
        })
        self.assertTrue(self.sched._signed_today())

    def test_no_state_file_not_signed(self):
        self.assertFalse(self.sched._signed_today())

    def test_legacy_txt_still_honored(self):
        """宿主 run.sh 写的旧格式仍然有效（向后兼容，不可回归）。"""
        with open(self._legacy_path(), "w", encoding="utf-8") as f:
            f.write("SUCCESS")
        self.assertTrue(self.sched._signed_today())

    def test_legacy_txt_other_value_not_signed(self):
        with open(self._legacy_path(), "w", encoding="utf-8") as f:
            f.write("FAILED")
        self.assertFalse(self.sched._signed_today())

    def test_corrupt_json_safely_not_signed(self):
        """状态文件损坏不应抛异常，按『未签到』处理（触发补签，而非崩调度器）。"""
        with open(self._state_path(), "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertFalse(self.sched._signed_today())

    def test_json_non_dict_safely_not_signed(self):
        with open(self._state_path(), "w", encoding="utf-8") as f:
            json.dump(["unexpected", "list"], f)
        self.assertFalse(self.sched._signed_today())


class EnvReloadTest(unittest.TestCase):
    """F2：main_loop 必须在每次触发时重新解析 .env，而不是用启动快照。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sched-")
        self.sched = _load_sched()
        self.sched.STATEDIR = self.tmp
        self.env_file = os.path.join(self.tmp, ".env")
        self.sched.ENV_FILE = self.env_file
        # 三个触发点全部设为 (0,0)：hm >= (0,0) 恒真 → 首轮即全部触发
        self.sched.FIRST = (0, 0)
        self.sched.SECOND = (0, 0)
        self.sched.PROBE_AT = (0, 0)
        self.sched.LOGDIR = os.path.join(self.tmp, "logs")  # 目录不存在 → 清理直接返回

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_env_reread_on_every_trigger(self):
        """每次触发都应重新读盘；否则 Web 后台改的设置要重启容器才生效。"""
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write("YIBAN_TEST_MARKER=v1\n")

        seen = []
        real_build = self.sched.build_child_env

        def build_spy(*_a, **_kw):
            env = real_build(env_file=self.env_file, base={"PATH": "/x"})
            seen.append(env.get("YIBAN_TEST_MARKER", ""))
            # 每次调用后改写 .env：下次若仍返回旧值，即证明没有重新读盘
            with open(self.env_file, "w", encoding="utf-8") as f:
                f.write(f"YIBAN_TEST_MARKER=v{len(seen) + 1}\n")
            return env

        self.sched.build_child_env = build_spy
        # 记录子进程调用（不真的 spawn）
        runs = []
        self.sched.subprocess = _stub_module(run=lambda cmd, **kw: runs.append(cmd))
        # 首次 sleep 即跳出循环
        self.sched.time = _stub_module(sleep=_stop_sleep)

        with self.assertRaises(_Stop):
            self.sched.main_loop(sleep_seconds=1)

        self.assertEqual(len(runs), 3, "首签 / 补签 / 探针 三个触发点都应执行")
        self.assertEqual(
            seen, ["v1", "v2", "v3"],
            "三次触发必须各自重新解析 .env（依次读到 v1/v2/v3），"
            "若出现重复值说明仍在复用启动时的环境快照",
        )

    def test_env_file_missing_does_not_crash(self):
        """.env 缺失时安全退化为纯继承，不应抛异常。"""
        if os.path.exists(self.env_file):
            os.remove(self.env_file)
        # 保持真实实现（只替换子进程与 sleep）
        self.sched.subprocess = _stub_module(run=lambda cmd, **kw: None)
        self.sched.time = _stub_module(sleep=_stop_sleep)
        with self.assertRaises(_Stop):
            self.sched.main_loop(sleep_seconds=1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
