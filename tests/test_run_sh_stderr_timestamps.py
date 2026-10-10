# -*- coding: utf-8 -*-
"""`run.sh` 面向 stderr 的警告/致命输出必须带时间戳前缀。

标签：J · 运维：部署/备份/发布
覆盖：run.sh 里**全部**直接写 stderr 的警告/致命行（`echo ... >&2`）——统一经
    `_err()` 出口，输出前缀为 `[YYYY-MM-DD HH:MM:SS] `。
背景（工单 yiban-auto-sign-2k1i）：`/var/log/yiban/run-cron.log` 只收 run.sh 的
    stderr，行本身无时间戳。09-30~10-10 每日 cron 各留 1 条同类行后，跨日累积的
    11 条被误读成"同日 06:31 被调用 11 次"。给每一行加时间戳，读者不得再靠行序猜日期。
对应实现：run.sh（`_err` 出口 + 各警告/致命调用点）。
关键断言：①结构门——run.sh 里除 `_err()` 定义行外，不得存在任何直接写 stderr 的
    语句；这条门让"下一个人新加一条裸 `echo ... >&2`"立刻变红，钉住本次框定的边界。
    ②行为门——真跑 run.sh 触发各 stderr 路径，逐行断言前缀形如
    `[YYYY-MM-DD HH:MM:SS] `。
依赖：bash（Git Bash/WSL）；行为用例在临时 APP_DIR 内跑，不连网、不碰生产。
"""

import io
import os
import re
import shutil
import subprocess
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_SH = os.path.join(BASE, "run.sh")

# 与 run.sh 的 `_err` 同口径：`date '+%F %T'` → `YYYY-MM-DD HH:MM:SS`
_TS_PREFIX = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ")


def _read(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read()


class StderrTimestampStructuralTest(unittest.TestCase):
    """结构门：摘掉 `_err()` 定义后，run.sh 不得再有直接写 stderr 的语句。"""

    def _err_defn(self):
        src = _read(RUN_SH)
        m = re.search(r"^_err\(\)\s*\{.*?^\}\s*$", src, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(m, "run.sh 缺少 `_err()` 定义（统一 stderr 出口）")
        return src, m

    def test_only_err_body_writes_stderr(self):
        src, m = self._err_defn()
        rest = src[:m.start()] + src[m.end():]
        offenders = [ln.strip() for ln in rest.splitlines() if ">&2" in ln]
        self.assertEqual(
            offenders, [],
            "run.sh 新增了 _err() 之外的裸 stderr 输出（run-cron.log 将再现无时间戳行）："
            + repr(offenders))

    def test_err_definition_has_timestamp(self):
        _src, m = self._err_defn()
        self.assertIn("date '+%F %T'", m.group(0),
                      "`_err()` 必须用 `date '+%F %T'` 造时间戳前缀")


@unittest.skipIf(shutil.which("bash") is None, "需要 bash（Git Bash/WSL）")
class StderrTimestampBehaviorTest(unittest.TestCase):
    """行为门：真跑 run.sh 触发各 stderr 路径，逐行断言时间戳前缀。"""

    def setUp(self):
        self.bash = shutil.which("bash")
        self.tmp = tempfile.mkdtemp(prefix="runsh-stderr-ts-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, env_extra, cwd=None, timeout=120):
        env = dict(os.environ)
        env.pop("YIBAN_LOG_FILE", None)
        env.update(env_extra)
        r = subprocess.run([self.bash, RUN_SH], capture_output=True, env=env,
                           cwd=cwd or self.tmp, timeout=timeout)
        return r

    def _assert_all_timestamped(self, stderr_text):
        lines = [ln for ln in stderr_text.replace("\r\n", "\n").split("\n") if ln]
        self.assertTrue(lines, "预期 stderr 非空（用例前提）")
        for ln in lines:
            self.assertRegex(ln, _TS_PREFIX,
                             "stderr 行缺时间戳前缀（run-cron.log 误判根因）：%r" % ln)

    def test_missing_app_dir_fatal_is_timestamped(self):
        missing = os.path.join(self.tmp, "no-such-app")
        r = self._run({"YIBAN_APP_DIR": missing})
        self.assertEqual(r.returncode, 1)
        err = r.stderr.decode("utf-8", "replace")
        self.assertIn("致命: 无法进入应用目录", err)
        self._assert_all_timestamped(err)

    def test_env_parse_warnings_are_timestamped(self):
        app = os.path.join(self.tmp, "app")
        state = os.path.join(self.tmp, "state")
        os.makedirs(app)
        os.makedirs(state)
        # 非法键名（含空格）+ 合法键名但非 YIBAN_ 前缀：各触发一条 stderr 警告
        with io.open(os.path.join(app, ".env"), "w", encoding="utf-8", newline="\n") as f:
            f.write("bad key=1\nNOTYIBAN=2\n")
        r = self._run({"YIBAN_APP_DIR": app, "YIBAN_STATE_DIR": state})
        err = r.stderr.decode("utf-8", "replace")
        self.assertIn("警告: .env 含非法键名", err)
        self.assertIn("警告: .env 含非 YIBAN_ 前缀键", err)
        self._assert_all_timestamped(err)

    def test_state_dir_fatal_is_timestamped(self):
        app = os.path.join(self.tmp, "app2")
        os.makedirs(app)
        blocker = os.path.join(self.tmp, "blocker")
        with io.open(blocker, "w", encoding="utf-8") as f:
            f.write("x")
        # STATE_DIR 的父路径是普通文件 ⇒ mkdir -p 必然失败 ⇒ 状态目录致命
        state = os.path.join(blocker, "state")
        r = self._run({"YIBAN_APP_DIR": app, "YIBAN_STATE_DIR": state})
        self.assertEqual(r.returncode, 1)
        err = r.stderr.decode("utf-8", "replace")
        self.assertIn("致命: 无法创建状态目录", err)
        self._assert_all_timestamped(err)

    def test_lock_dir_fatal_is_timestamped(self):
        app = os.path.join(self.tmp, "app3")
        state = os.path.join(self.tmp, "state3")
        os.makedirs(app)
        os.makedirs(state)
        blocker = os.path.join(self.tmp, "lockblocker")
        with io.open(blocker, "w", encoding="utf-8") as f:
            f.write("x")
        # LOCK_DIR 的父路径是普通文件 ⇒ mkdir -p 必然失败 ⇒ 锁目录致命
        lock = os.path.join(blocker, "yiban")
        r = self._run({"YIBAN_APP_DIR": app, "YIBAN_STATE_DIR": state,
                       "YIBAN_LOCK_DIR": lock})
        self.assertEqual(r.returncode, 1)
        err = r.stderr.decode("utf-8", "replace")
        self.assertIn("致命: 无法创建锁目录", err)
        self._assert_all_timestamped(err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
