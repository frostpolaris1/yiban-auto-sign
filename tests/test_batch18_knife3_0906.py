# -*- coding: utf-8 -*-
"""批次18 刀3 回归测试（2026-09-06）。

覆盖：
- M6：run.sh「当日已触发」标记前移到 flock 之前——被 flock 弹开的触发同样留痕，
  其后的补签触发读到标记即以 second_run 身份运行（YIBAN_SECOND_RUN=1），解除
  「部分成功 + 窗口外」告警被首签轮身份压制的缺陷；STATUS_FILE SUCCESS 幂等
  检查保持在标记块之后；
- P3-2：run.sh 显式 YIBAN_RUN_TIMEOUT_SEC 钳位——必须 ≥600 的整数（与
  docker/scheduler.py _child_timeout 的 max(600,·) 同口径），非法/过小回退动态
  计算默认值并打 WARNING，空值走默认分支；
- P3-3：_schedule_blocks 有效窗口被前后裁剪吃空时回退默认窗口并入一次性管理员
  汇总邮件（自有去重标记，镜像 _schedule_config F3 模式）；
- P3-13：signin 日志装配延迟到 main()——import web.app + import signin 模块导入
  零副作用，create_app 后 root 上指向 sign 日志目录的 FileHandler 只有一个
  （生产症状：每条日志写两遍）。

run.sh 相关用例需要 bash（Git Bash/WSL），无 bash 环境跳过。
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))

import signin  # noqa: E402

RUN_SH = os.path.join(BASE, "run.sh")


def _today():
    return datetime.now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# run.sh 外壳行为（bash 驱动，假 flock / 假 timeout shim）
# ---------------------------------------------------------------------------
FAKE_FLOCK = "#!/usr/bin/env bash\nexit ${FAKE_FLOCK_EXIT:-0}\n"

FAKE_TIMEOUT = r"""#!/usr/bin/env bash
{
  echo "SECOND_RUN=${YIBAN_SECOND_RUN-UNSET}"
  echo "TIMEOUT_ARG=$1"
} >> "$FAKE_TIMEOUT_LOG"
exit ${FAKE_TIMEOUT_EXIT:-0}
"""


@unittest.skipIf(shutil.which("bash") is None, "需要 bash（Git Bash/WSL）")
class RunShMarkerTest(unittest.TestCase):
    """M6：标记前移——flock 弹开留痕 → 次轮 second_run。"""

    def setUp(self):
        self.bash = shutil.which("bash")
        self.tmp = tempfile.mkdtemp(prefix="knife3-m6-")
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.state)
        self.fakebin = os.path.join(self.tmp, "fakebin")
        os.makedirs(self.fakebin)
        for name, body in (("flock", FAKE_FLOCK), ("timeout", FAKE_TIMEOUT)):
            p = os.path.join(self.fakebin, name)
            with io.open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(body)
            os.chmod(p, 0o755)
        self.timeout_log = os.path.join(self.tmp, "timeout-calls.log")
        self.env = dict(os.environ)
        self.env.update({
            "YIBAN_STATE_DIR": self.state,
            "FAKE_TIMEOUT_LOG": self.timeout_log,
            "FAKE_FLOCK_EXIT": "0",
        })
        for k in ("YIBAN_SECOND_RUN", "YIBAN_RUN_TIMEOUT_SEC", "YIBAN_SIGN_END",
                  "YIBAN_LOG_FILE"):
            self.env.pop(k, None)
        # PATH 注入假命令（Git Bash 需 POSIX 路径；无 cygpath 时做朴素转换）
        conv = subprocess.run(
            [self.bash, "-c", 'cygpath -u "$1" 2>/dev/null || echo "$1"', "_", self.fakebin],
            capture_output=True, text=True)
        posix_fakebin = conv.stdout.strip() or self.fakebin
        self.env["PATH"] = posix_fakebin + os.pathsep + self.env.get("PATH", "")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self):
        return subprocess.run([self.bash, RUN_SH], capture_output=True,
                              env=self.env, cwd=self.tmp, timeout=120)

    def _log_text(self):
        path = os.path.join(self.state, f"sign-{_today()}.log")
        if not os.path.exists(path):
            return ""
        with io.open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    def _timeout_calls(self):
        if not os.path.exists(self.timeout_log):
            return []
        with io.open(self.timeout_log, "r", encoding="utf-8", errors="replace") as f:
            return [ln for ln in f.read().splitlines() if ln]

    def _marker_path(self):
        return os.path.join(self.state, f"yiban-run-today-{_today()}.marker")

    def test_first_trigger_runs_as_first_run(self):
        """当日首次触发：创建标记且不以补签轮身份运行。"""
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(self._marker_path()), "首触发必须留痕")
        calls = self._timeout_calls()
        self.assertEqual([c for c in calls if c.startswith("SECOND_RUN=")],
                         ["SECOND_RUN=UNSET"], "首触发不得带 YIBAN_SECOND_RUN")

    def test_flocked_out_trigger_leaves_marker(self):
        """被 flock 弹开（exit 0）发生在标记写入之后：弹开也留痕、不执行签到。"""
        self.env["FAKE_FLOCK_EXIT"] = "1"
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(self._marker_path()),
                        "被 flock 弹开的触发同样必须留下当日标记（M6 核心）")
        self.assertIn("已有签到进程在运行", self._log_text())
        self.assertEqual(self._timeout_calls(), [], "弹开路径不得执行签到")

    def test_second_trigger_after_flock_bounce_is_second_run(self):
        """核心链路：06:31 被 flock 弹开留痕 → 次轮（07:10）以 second_run 身份运行。"""
        self.env["FAKE_FLOCK_EXIT"] = "1"
        r1 = self._run()
        self.assertEqual(r1.returncode, 0)
        self.env["FAKE_FLOCK_EXIT"] = "0"
        r2 = self._run()
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        calls = self._timeout_calls()
        self.assertIn("SECOND_RUN=1", calls,
                      f"次轮必须以补签轮身份运行，实际: {calls}")

    def test_success_status_check_still_skips_after_marker(self):
        """STATUS_FILE SUCCESS 幂等检查保持在标记块之后：已成功的当日直接跳过。"""
        self.assertTrue(os.path.exists(self._marker_path()) is False)
        with io.open(self._marker_path(), "w", encoding="utf-8") as f:
            f.write("")
        with io.open(os.path.join(self.state, f"sign-status-{_today()}.txt"),
                     "w", encoding="utf-8") as f:
            f.write("SUCCESS")
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("今天已签到成功", self._log_text())
        self.assertEqual(self._timeout_calls(), [], "SUCCESS 幂等跳过不得执行签到")


@unittest.skipIf(shutil.which("bash") is None, "需要 bash（Git Bash/WSL）")
class RunShTimeoutClampTest(unittest.TestCase):
    """P3-2：显式 YIBAN_RUN_TIMEOUT_SEC 钳位矩阵。"""

    def setUp(self):
        self.bash = shutil.which("bash")
        self.tmp = tempfile.mkdtemp(prefix="knife3-p32-")
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.state)
        self.fakebin = os.path.join(self.tmp, "fakebin")
        os.makedirs(self.fakebin)
        for name, body in (("flock", FAKE_FLOCK), ("timeout", FAKE_TIMEOUT)):
            p = os.path.join(self.fakebin, name)
            with io.open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(body)
            os.chmod(p, 0o755)
        self.timeout_log = os.path.join(self.tmp, "timeout-calls.log")
        self.env = dict(os.environ)
        self.env.update({
            "YIBAN_STATE_DIR": self.state,
            "FAKE_TIMEOUT_LOG": self.timeout_log,
            "FAKE_FLOCK_EXIT": "0",
        })
        for k in ("YIBAN_SECOND_RUN", "YIBAN_RUN_TIMEOUT_SEC", "YIBAN_SIGN_END",
                  "YIBAN_LOG_FILE"):
            self.env.pop(k, None)
        conv = subprocess.run(
            [self.bash, "-c", 'cygpath -u "$1" 2>/dev/null || echo "$1"', "_", self.fakebin],
            capture_output=True, text=True)
        posix_fakebin = conv.stdout.strip() or self.fakebin
        self.env["PATH"] = posix_fakebin + os.pathsep + self.env.get("PATH", "")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self):
        return subprocess.run([self.bash, RUN_SH], capture_output=True,
                              env=self.env, cwd=self.tmp, timeout=120)

    def _log_text(self):
        path = os.path.join(self.state, f"sign-{_today()}.log")
        if not os.path.exists(path):
            return ""
        with io.open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    def _timeout_arg(self):
        with io.open(self.timeout_log, "r", encoding="utf-8", errors="replace") as f:
            for ln in f.read().splitlines():
                if ln.startswith("TIMEOUT_ARG="):
                    return ln.split("=", 1)[1]
        return None

    def _case(self, raw_value):
        if os.path.exists(self.timeout_log):
            os.remove(self.timeout_log)
        marker = os.path.join(self.state, f"yiban-run-today-{_today()}.marker")
        # 每个用例复用同一 state：前一轮已 SUCCESS 会幂等短路，须清状态文件
        status = os.path.join(self.state, f"sign-status-{_today()}.txt")
        for p in (marker, status):
            if os.path.exists(p):
                os.remove(p)
        if raw_value is not None:
            self.env["YIBAN_RUN_TIMEOUT_SEC"] = raw_value
        else:
            self.env.pop("YIBAN_RUN_TIMEOUT_SEC", None)
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return self._timeout_arg(), self._log_text()

    def test_valid_explicit_value_passes_through(self):
        arg, log = self._case("1234")
        self.assertEqual(arg, "1234")
        self.assertNotIn("YIBAN_RUN_TIMEOUT_SEC=", log, "合法值不得打警告")

    def test_too_small_clamps_to_dynamic_default(self):
        arg, log = self._case("300")
        self.assertNotEqual(arg, "300")
        self.assertGreaterEqual(int(arg), 600, "过小值必须回退 ≥600 的动态默认")
        self.assertIn("警告: YIBAN_RUN_TIMEOUT_SEC=300", log)

    def test_non_numeric_falls_back(self):
        arg, log = self._case("abc")
        self.assertGreaterEqual(int(arg), 600)
        self.assertIn("警告: YIBAN_RUN_TIMEOUT_SEC=abc", log)

    def test_negative_falls_back(self):
        arg, log = self._case("-5")
        self.assertGreaterEqual(int(arg), 600)
        self.assertIn("警告: YIBAN_RUN_TIMEOUT_SEC=-5", log)

    def test_zero_falls_back(self):
        arg, log = self._case("0")
        self.assertGreaterEqual(int(arg), 600)
        self.assertIn("警告: YIBAN_RUN_TIMEOUT_SEC=0", log)

    def test_whitespace_padded_value_trimmed(self):
        arg, log = self._case(" 800 ")
        self.assertEqual(arg, "800", "首尾空白应裁剪（与 scheduler .strip() 对齐）")
        self.assertNotIn("警告: YIBAN_RUN_TIMEOUT_SEC", log)

    def test_empty_uses_dynamic_default_without_warning(self):
        arg, log = self._case("")
        self.assertGreaterEqual(int(arg), 600)
        self.assertNotIn("警告: YIBAN_RUN_TIMEOUT_SEC", log)

    def test_unset_uses_dynamic_default_without_warning(self):
        arg, log = self._case(None)
        self.assertGreaterEqual(int(arg), 600)
        self.assertNotIn("警告: YIBAN_RUN_TIMEOUT_SEC", log)


class EdgeEmptyWindowMailTest(unittest.TestCase):
    """P3-3：有效窗口被前后裁剪吃空 → 回退默认窗口 + 一次性管理员汇总邮件。"""

    def setUp(self):
        signin._edge_empty_window_notified = False
        signin._mail_summary.clear()

    def tearDown(self):
        signin._edge_empty_window_notified = False
        signin._mail_summary.clear()

    @staticmethod
    def _overflow_cfg():
        # 06:30~06:40 共 10 分钟窗口，前后各裁 5 分钟 → 有效窗口为空
        return {
            "sign_start": (6, 30),
            "sign_end": (6, 40),
            "edge_front_sec": 300,
            "edge_back_sec": 300,
        }

    def test_empty_window_collects_admin_mail_once(self):
        cfg = self._overflow_cfg()
        with mock.patch.object(signin, "_collect_admin_mail") as m_mail:
            blocks1, lo1, hi1 = signin._schedule_blocks(dict(cfg))
            blocks2, lo2, hi2 = signin._schedule_blocks(dict(cfg))
        m_mail.assert_called_once()  # 去重标记必须保证多轮调用只收集一次
        title, text = m_mail.call_args[0]
        self.assertEqual(title, "签到窗口配置异常")
        self.assertIn("有效签到窗口为空", text)
        self.assertIn("YIBAN_WINDOW_EDGE_FRONT_SEC", text)
        # 回退默认窗口：06:30+60s ~ 07:50-60s
        self.assertEqual((lo1, hi1), (391.0, 469.0))
        self.assertEqual((lo2, hi2), (391.0, 469.0))
        self.assertTrue(blocks1, "回退后必须产出非空块列表")
        self.assertEqual(len(blocks1), len(blocks2))

    def test_normal_window_no_mail(self):
        cfg = {
            "sign_start": (6, 30),
            "sign_end": (7, 50),
            "edge_front_sec": 60,
            "edge_back_sec": 60,
        }
        with mock.patch.object(signin, "_collect_admin_mail") as m_mail:
            blocks, lo, hi = signin._schedule_blocks(cfg)
        m_mail.assert_not_called()
        self.assertTrue(blocks)
        self.assertEqual((lo, hi), (391.0, 469.0))


class CliLoggingSingleHandlerTest(unittest.TestCase):
    """P3-13：signin 日志装配延迟到 main()——模块导入零副作用，root 单 FileHandler。"""

    def test_import_webapp_and_signin_single_filehandler(self):
        tmp = tempfile.mkdtemp(prefix="knife3-log-")
        try:
            logs = os.path.join(tmp, "logs")
            state = os.path.join(tmp, "state")
            os.makedirs(logs)
            os.makedirs(state)
            env_file = os.path.join(tmp, ".env")
            with io.open(env_file, "w", encoding="utf-8") as f:
                f.write("")
            env = dict(os.environ)
            env.update({
                "YIBAN_LOG_FILE": os.path.join(logs, "sign.log"),
                "YIBAN_STATE_DIR": state,
                "YIBAN_ENV_FILE": env_file,
                "YIBAN_DB_FILE": os.path.join(tmp, "yiban.db"),
                "YIBAN_ACCOUNTS_FILE": os.path.join(tmp, "accounts.json"),
                "YIBAN_DISABLE_PURGE_LOOP": "1",
                "YIBAN_MAIL_ENABLE": "0",
                "PYTHONIOENCODING": "utf-8",
            })
            code = (
                "import sys, os, logging\n"
                f"BASE = {BASE!r}\n"
                "sys.path.insert(0, BASE)\n"
                "import web.app  # noqa  （其内部再 import signin）\n"
                "import signin  # noqa\n"
                "pre = [h for h in logging.getLogger().handlers "
                "if isinstance(h, logging.FileHandler)]\n"
                "web.app.create_app()\n"
                "post = [h for h in logging.getLogger().handlers "
                "if isinstance(h, logging.FileHandler)]\n"
                "print(f'PRE={len(pre)}')\n"
                "print(f'POST={len(post)}')\n"
                "if post:\n"
                "    print('LOGDIR=' + os.path.dirname(post[0].baseFilename))\n"
            )
            r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                               env=env, cwd=BASE, timeout=180, text=True,
                               encoding="utf-8", errors="replace")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            out = dict(
                ln.split("=", 1) for ln in r.stdout.splitlines() if "=" in ln
            )
            # 核心断言：模块导入零副作用（旧实现此处=1，且 create_app 后=2 双写）
            self.assertEqual(out.get("PRE"), "0",
                             "import web.app + import signin 不得向 root 挂任何文件 handler")
            self.assertEqual(out.get("POST"), "1",
                             "create_app 后 root 上指向 sign 日志目录的 FileHandler 必须只有一个")
            self.assertIn(
                os.path.normcase(os.path.abspath(logs)),
                os.path.normcase(os.path.abspath(out.get("LOGDIR", ""))),
                "唯一的 FileHandler 必须指向 sign 日志目录",
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
