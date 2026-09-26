# -*- coding: utf-8 -*-
"""`run.sh` 的多执行体开关（`YIBAN_WORKERS`）外壳行为。

标签：B · 调度：领取/队列/执行体
覆盖：run.sh 外壳的三组行为：YIBAN_WORKERS 透传/默认与 1 不传/非法值留告警、
   标记前移（flock 弹开也留痕、次轮以 second_run 身份运行、SUCCESS
   幂等检查仍在标记块之后）、显式 YIBAN_RUN_TIMEOUT_SEC 的钳位矩阵、
   日志装配延迟到 main() 后 root 只有一个 FileHandler。
   M3 批次0 扩展：决定跑/不跑的写入全部判码并 fail-closed
   （状态目录建不出/不可写、RUN_MARKER noclobber 写失败、_status_write 的
   mktemp/echo/mv、收尾标记），写失败不得等价于"已完成"；sign-status 的
   SUCCESS 须与库内当日事实（_db_settled_today 只读 sqlite）交叉核对才采信，
   伪造件拒绝采信+告警+按未完成继续；.env 缺失/不可读显式告警不静默回落；
   YIBAN_SECOND_RUN / YIBAN_GLOBAL_PAUSE 统一 _is_truthy 一处解析。
对应实现：run.sh（workers 参数拼装、noclobber 标记、flock 分支、timeout
   钳位）、scripts/signin.py 的日志装配。
关键断言：教程里写的 YIBAN_WORKERS=4
   必须真的成立；非法值只能告警回退，绝不为一笔误配置停签。被 flock
   弹开发生在标记写入之后，所以弹开也要留痕——否则 06:31
   撞车的实例次日永远被判成首签。超时钳位的下限保住「距窗口关闭还有多久」这条底线。模块导入必须零副作用（否则
   root 上叠出第二个 FileHandler，日志翻倍）。
依赖：三个 bash 用例类带 skipIf(shutil.which('bash') is None)：本机没有 bash
   时整类skip（Git Bash 在场则真实执行 run.sh，用 fakebin 里的假 flock/timeout
   记录参数）。CliLoggingSingleHandlerTest 不依赖 bash。

部署教程里写"`YIBAN_WORKERS=4` 就会拉起 4 个执行体"，这句话必须真的成立，
所以这里用假 `timeout`（记录被调用的完整参数）去验拼出来的命令行。
"""
import io
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_SH = os.path.join(BASE, "run.sh")


def _write_python_wrapper(app_dir):
    """在 $APP_DIR/.venv/bin/python3 放一个转调当前解释器的包装。

    run.sh 的 sign-status 库内事实交叉核对直接调 $PY（不经假 timeout），
    必须保证它在 Git Bash 与 WSL 下都存在且能跑 sqlite3。
    """
    venv_bin = os.path.join(app_dir, ".venv", "bin")
    os.makedirs(venv_bin, exist_ok=True)
    path = os.path.join(venv_bin, "python3")
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write('#!/bin/sh\nexec "%s" "$@"\n' % sys.executable.replace("\\", "/"))
    os.chmod(path, os.stat(path).st_mode | 0o755)
    return path


def _install_seal_stub(app_dir):
    """放一个恒答 0 的 `scripts/signin.py` 桩（封存前置的"库内事实"判定件）。

    run.sh 在封存当日收尾标记前必须以**库内事实**判"确实无未了结"（前置不是子执行体
    自报退出码）：它调 `$PY scripts/signin.py --second-run-check`，判定不可得
    （文件缺失/解释器不在）按"无法判定"处理 ⇒ 不封存 + 本轮"成功"收场时退出码升 1。
    本文件钉的是外壳参数拼装/超时钳位/标记前移，应用目录若没有这个桩，rc 0 断言会被
    封存闸门升级成 1——与本文件主题无关的劫持。桩答 0 = "当日无未了结"，封存照常。
    """
    scripts = os.path.join(app_dir, "scripts")
    os.makedirs(scripts, exist_ok=True)
    with io.open(os.path.join(scripts, "signin.py"), "w", encoding="utf-8",
                 newline="\n") as f:
        f.write("import sys\nsys.exit(0)\n")
    _write_python_wrapper(app_dir)


def _seed_facts_db(db_path, day, states):
    """建一个最小 sign_tasks 表并写入当日事实行（采信交叉核对的数据源）。

    只建 run.sh 采信查询用到的三列（phone/day/state）——刻意不复用引擎的建库代码，
    这样 run.sh 侧查询与表结构的耦合一旦漂移，这里的用例会红而不是静默放行。
    """
    con = sqlite3.connect(db_path)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS sign_tasks "
                    "(phone TEXT, day TEXT, state TEXT)")
        for st in states:
            con.execute("INSERT INTO sign_tasks VALUES (?,?,?)",
                        ("138****0000", day, st))
        con.commit()
    finally:
        con.close()

FAKE_FLOCK = "#!/usr/bin/env bash\nexit 0\n"
#: 记录被调用的全部参数（`$*`）：要看的就是 "--workers 4" 有没有传下去
FAKE_TIMEOUT = '#!/usr/bin/env bash\necho "$*" >> "$FAKE_TIMEOUT_LOG"\nexit 0\n'


def _today():
    return datetime.now().strftime("%Y-%m-%d")


@unittest.skipIf(shutil.which("bash") is None, "需要 bash（Git Bash/WSL）")
class RunShWorkersTest(unittest.TestCase):
    def setUp(self):
        self.bash = shutil.which("bash")
        self.tmp = tempfile.mkdtemp(prefix="runsh-workers-")
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.state)
        self.fakebin = os.path.join(self.tmp, "fakebin")
        os.makedirs(self.fakebin)
        # 把假 bin 前置进 PATH：flock/timeout 在 Windows 上没有，桩顺便记录收到的参数
        for name, body in (("flock", FAKE_FLOCK), ("timeout", FAKE_TIMEOUT)):
            path = os.path.join(self.fakebin, name)
            with io.open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(body)
            os.chmod(path, 0o755) # Git Bash 同样只看执行位，桩没有它就直接 126
        self.calls = os.path.join(self.tmp, "timeout-calls.log")
        _install_seal_stub(self.tmp)            # 封存前置：--second-run-check 恒答 0
        self.env = dict(os.environ)
        self.env.update({
            "YIBAN_STATE_DIR": self.state,
            "YIBAN_APP_DIR": self.tmp,          # run.sh 要求应用目录存在
            "FAKE_TIMEOUT_LOG": self.calls,
        })
        for k in ("YIBAN_WORKERS", "YIBAN_SECOND_RUN", "YIBAN_LOG_FILE",
                  "YIBAN_SIGN_END", "YIBAN_RUN_TIMEOUT_SEC",
                  "YIBAN_FALLBACK_ENABLE"):
            self.env.pop(k, None)
        conv = subprocess.run(
            [self.bash, "-c", 'cygpath -u "$1" 2>/dev/null || echo "$1"', "_", self.fakebin],
            capture_output=True, text=True)
        self.env["PATH"] = (conv.stdout.strip() or self.fakebin) + os.pathsep + \
            self.env.get("PATH", "")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, workers=None):
        env = dict(self.env)
        # 每次调用用**全新的状态目录**：run.sh 有"当日已触发过"的 noclobber 标记，
        # 复用同一个目录时第二次调用会在标记处提前退出、根本走不到 signin
        # （我第一版就是这么写的，四种子用例全被这个标记吃掉）。
        state = tempfile.mkdtemp(prefix="state-", dir=self.tmp)
        env["YIBAN_STATE_DIR"] = state
        self._state = state
        if workers is not None:
            env["YIBAN_WORKERS"] = workers
        r = subprocess.run([self.bash, RUN_SH], capture_output=True, env=env,
                           cwd=self.tmp, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace")) # 先确认 run.sh 自己退出码为 0，否则「根本没调 signin」会被误读成参数断言失败
        with io.open(self.calls, encoding="utf-8", errors="replace") as f:
            return [ln for ln in f.read().splitlines() if ln]

    def _log(self):
        path = os.path.join(self._state, f"sign-{_today()}.log")
        if not os.path.exists(path):
            return ""
        with io.open(path, encoding="utf-8", errors="replace") as f:
            return f.read()

    def test_workers_flag_is_passed_through(self):
        calls = self._run("4")
        self.assertTrue(calls, "应调用 signin")
        self.assertIn("--workers 4", calls[0], calls)
        self.assertIn("多执行体: 4 个并行执行体", self._log())

    def test_default_and_one_do_not_pass_workers(self):
        for value in (None, "", "1"):
            with self.subTest(YIBAN_WORKERS=value):
                if os.path.exists(self.calls):
                    os.remove(self.calls)
                calls = self._run(value)
                self.assertTrue(calls)
                self.assertNotIn("--workers", calls[0], calls)

    def test_invalid_value_warns_and_falls_back(self):
        for value in ("0", "abc", "999", "-2"):
            with self.subTest(YIBAN_WORKERS=value):
                if os.path.exists(self.calls):
                    os.remove(self.calls)
                calls = self._run(value)
                self.assertTrue(calls)
                self.assertNotIn("--workers", calls[0], calls)
                self.assertIn("YIBAN_WORKERS", self._log(), "非法值应留告警痕迹")


def _today():
    return datetime.now().strftime("%Y-%m-%d")


FAKE_FLOCK_K3 = "#!/usr/bin/env bash\nexit ${FAKE_FLOCK_EXIT:-0}\n"


FAKE_TIMEOUT_K3 = r"""#!/usr/bin/env bash
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
        for name, body in (("flock", FAKE_FLOCK_K3), ("timeout", FAKE_TIMEOUT_K3)):
            p = os.path.join(self.fakebin, name)
            with io.open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(body)
            os.chmod(p, 0o755)
        self.timeout_log = os.path.join(self.tmp, "timeout-calls.log")
        _install_seal_stub(self.tmp)            # 封存前置：--second-run-check 恒答 0
        self.env = dict(os.environ)
        self.env.update({
            "YIBAN_STATE_DIR": self.state,
            # run.sh 现在要求应用目录存在（进不去即拒绝运行，见 APP_DIR 注释）；
            # 测试把 APP_DIR 指向临时目录即可——本类只关心标记/超时语义，
            # 被 fake timeout 拦住的 signin 调用不会真的执行
            "YIBAN_APP_DIR": self.tmp,
            "FAKE_TIMEOUT_LOG": self.timeout_log,
            "FAKE_FLOCK_EXIT": "0",
        })
        for k in ("YIBAN_SECOND_RUN", "YIBAN_RUN_TIMEOUT_SEC", "YIBAN_SIGN_END",
                  "YIBAN_LOG_FILE", "YIBAN_GLOBAL_PAUSE", "YIBAN_DB_FILE",
                  "YIBAN_WORKERS", "YIBAN_FALLBACK_ENABLE"):
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

    def _status_path(self):
        return os.path.join(self.state, f"sign-status-{_today()}.txt")

    def _settled_path(self):
        return os.path.join(self.state, f"yiban-settled-{_today()}.marker")

    def _stderr(self, r):
        return r.stderr.decode("utf-8", "replace")

    def _probe_state_writable(self):
        """True = 状态目录当前仍可创建文件（注入未生效）。"""
        probe = os.path.join(self.state, ".perm-probe")
        try:
            with io.open(probe, "w"):
                os.remove(probe)
            return True
        except OSError:
            return False

    def _make_state_dir_unwritable_or_skip(self):
        """把状态目录变成"实际不可写"：chattr +i（对 root 也生效）→ chmod 0500
        （仅非 root 有意义）→ 都不生效（Windows/drvfs、无 e2fsprogs）时 skip。"""
        if shutil.which("chattr"):
            r = subprocess.run(["chattr", "+i", self.state],
                               capture_output=True, text=True)
            if r.returncode == 0:
                if not self._probe_state_writable():
                    self.addCleanup(subprocess.run, ["chattr", "-i", self.state],
                                    capture_output=True)
                    return
                subprocess.run(["chattr", "-i", self.state], capture_output=True)
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root 且 chattr 不可用：权限位约束不住写入")
        os.chmod(self.state, 0o500)
        self.addCleanup(os.chmod, self.state, 0o700)
        if not self._probe_state_writable():
            return
        os.chmod(self.state, 0o700)
        self.skipTest("当前文件系统不强制 unix 写权限位（如 Windows drvfs）")

    # ---------------- sign-status 采信必须交叉核对库内事实 ----------------

    def test_success_status_check_still_skips_after_marker(self):
        """STATUS_FILE SUCCESS 幂等检查保持在标记块之后：已成功的当日直接跳过。

        同批更新：SUCCESS 现在必须与库内当日事实一致才采信——本用例补种
        一条当日 done 行使"真成功的当日"仍走幂等跳过（旧断言钉的是"文本即采信"）。
        """
        _write_python_wrapper(self.tmp)
        db = os.path.join(self.tmp, "facts.db")
        _seed_facts_db(db, _today(), ["done"])
        self.env["YIBAN_DB_FILE"] = db
        self.assertTrue(os.path.exists(self._marker_path()) is False)
        with io.open(self._marker_path(), "w", encoding="utf-8") as f:
            f.write("")
        with io.open(self._status_path(), "w", encoding="utf-8") as f:
            f.write("SUCCESS")
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("今天已签到成功", self._log_text())
        self.assertEqual(self._timeout_calls(), [], "SUCCESS 幂等跳过不得执行签到")

    def test_forged_success_status_without_db_facts_is_rejected(self):
        """活体反例：手写 SUCCESS 状态文件、临时库当日无完成事实 ⇒
        拒绝采信 + 双声音告警 + 按未完成继续（本轮真实执行），且不得以成功退 0。"""
        _write_python_wrapper(self.tmp)
        db = os.path.join(self.tmp, "facts.db")
        _seed_facts_db(db, _today(), [])        # 库存在但当日 0 条已了结
        self.env["YIBAN_DB_FILE"] = db
        self.env["FAKE_TIMEOUT_EXIT"] = "2"     # 本轮以"未了结"收场：伪造件会被 SKIPPED 覆盖
        with io.open(self._status_path(), "w", encoding="utf-8") as f:
            f.write("SUCCESS")                 # 伪造/搬运来的状态文件
        r = self._run()
        self.assertNotEqual(r.returncode, 0, "伪造 SUCCESS 不得让脚本退 0 报成功")
        self.assertIn("拒绝采信", self._stderr(r))
        self.assertIn("拒绝采信", self._log_text(), "告警必须双声音（stderr + 当日日志）")
        calls = self._timeout_calls()
        self.assertTrue(any(c.startswith("SECOND_RUN=") for c in calls),
                        f"拒绝采信后必须按未完成继续执行，实际调用: {calls}")
        with io.open(self._status_path(), encoding="utf-8") as f:
            self.assertNotEqual(f.read().strip(), "SUCCESS",
                                "本轮失败后不得仍挂着 SUCCESS")

    def test_real_db_facts_day_with_success_status_skips(self):
        """交叉核对的正向对照：当日库内有 skipped 了结行 ⇒ SUCCESS 正常采信。"""
        _write_python_wrapper(self.tmp)
        db = os.path.join(self.tmp, "facts.db")
        _seed_facts_db(db, _today(), ["skipped"])
        self.env["YIBAN_DB_FILE"] = db
        with io.open(self._marker_path(), "w", encoding="utf-8") as f:
            f.write("")
        with io.open(self._status_path(), "w", encoding="utf-8") as f:
            f.write("SUCCESS")
        r = self._run()
        self.assertEqual(r.returncode, 0, self._stderr(r))
        self.assertIn("今天已签到成功", self._log_text())
        self.assertEqual(self._timeout_calls(), [], "可信 SUCCESS 不得触发重复签到")

    # ---------------- 决定跑/不跑的写入全部判码、fail-closed ----------------

    def test_unwritable_state_dir_refuses_run_without_round(self):
        """活体反例：状态目录置不可写 ⇒ 拒绝运行（不跑）、写不出任何
        SUCCESS、也绝不执行签到轮次，并给出带声音的告警。

        注入走 chattr +i（root 也挡）或 chmod 0500；两条拒绝线（STATE_DIR 预检 /
        RUN_MARKER 写失败判码）任一命中都算拒绝——共同口径是"拒绝运行"。"""
        self._make_state_dir_unwritable_or_skip()
        r = self._run()
        self.assertEqual(r.returncode, 1, self._stderr(r))
        self.assertIn("拒绝运行", self._stderr(r))
        self.assertEqual(self._timeout_calls(), [], "状态目录不可写时不得执行签到轮次")
        self.assertFalse(os.path.exists(self._status_path()),
                         "不得留下（更不得写出）SUCCESS")

    def test_uncreatable_state_dir_is_fatal(self):
        """`mkdir -p "$STATE_DIR"` 判码：目录建不出来（父路径是文件）⇒ 不跑并告警。

        不依赖权限位，Windows/WSL 都必须成立。
        """
        blocker = os.path.join(self.tmp, "blocker")
        with io.open(blocker, "w", encoding="utf-8") as f:
            f.write("x")
        self.env["YIBAN_STATE_DIR"] = os.path.join(blocker, "state")
        r = self._run()
        self.assertEqual(r.returncode, 1, self._stderr(r))
        self.assertIn("无法创建状态目录", self._stderr(r))
        self.assertEqual(self._timeout_calls(), [], "状态目录建不出来时不得执行签到")

    def test_run_marker_write_failure_refuses_and_is_not_second_run(self):
        """RUN_MARKER noclobber 写失败且文件不存在（路径被占成目录）⇒ 不得当成
        "当日已触发过"（那会静默关掉进程内补签轮）：必须不跑 + 告警。"""
        os.mkdir(self._marker_path())
        r = self._run()
        self.assertEqual(r.returncode, 1, self._stderr(r))
        self.assertIn("无法写入当日触发标记", self._stderr(r))
        self.assertEqual(self._timeout_calls(), [], "判定不了首签/补签时不得执行签到")
        self.assertIn("拒绝运行本轮", self._log_text(), "告警必须进当日日志")

    def test_status_write_failure_escalates_exit_code(self):
        """_status_write 的 mv 步失败（状态文件路径被占成目录）⇒ "成功"不得静默
        收场：告警 + 退出码升为 1 + 不残留 .tmp 半写件。"""
        os.mkdir(self._status_path())
        r = self._run()                          # 假 timeout 退 0（= 签到"成功"）
        self.assertEqual(r.returncode, 1,
                         "状态写失败时成功不得按 0 收场（写失败==完成的等价类禁止）")
        self.assertIn("状态文件原子替换失败", self._stderr(r))
        self.assertIn("状态文件写入失败", self._log_text())
        leftovers = [n for n in os.listdir(self.state) if ".tmp." in n]
        self.assertEqual(leftovers, [], "失败的临时件必须清掉")

    def test_settled_marker_write_failure_escalates_exit_code(self):
        """收尾标记 `: > "$SECOND_DONE_MARKER"` 写失败（被占成目录）⇒ 不得 || true
        静默：告警 + 退出码升为 1（下一触发重判未收尾的风险必须有声音）。"""
        os.mkdir(self._settled_path())
        r = self._run()
        self.assertEqual(r.returncode, 1, self._stderr(r))
        self.assertIn("当日收尾标记写入失败", self._stderr(r))
        self.assertIn("当日收尾标记写入失败", self._log_text())

    # ---------------- .env 显式告警 / 取值域统一 ----------------

    def test_env_missing_warns_but_continues(self):
        """.env 不存在 ⇒ stderr + 日志显式告警，按默认值继续（不让 cron 天天红）。"""
        r = self._run()
        self.assertEqual(r.returncode, 0, self._stderr(r))
        self.assertIn(".env 不存在", self._stderr(r))
        self.assertIn(".env 不存在", self._log_text())

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root 无视权限位")
    def test_env_unreadable_warns_but_continues(self):
        """.env 存在但不可读 ⇒ 显式告警并继续用默认值。"""
        env_file = os.path.join(self.tmp, ".env")
        with io.open(env_file, "w", encoding="utf-8") as f:
            f.write("YIBAN_SIGN_END=07:50\n")
        try:
            os.chmod(env_file, 0o000)
            if os.access(env_file, os.R_OK):
                self.skipTest("当前文件系统不强制读权限位")
            r = self._run()
        finally:
            os.chmod(env_file, 0o644)
        self.assertEqual(r.returncode, 0, self._stderr(r))
        self.assertIn("不可读", self._stderr(r))

    def test_global_pause_truthy_value_shares_one_domain(self):
        """取值域统一：YIBAN_GLOBAL_PAUSE 与 _need_second_round 统一走 _is_truthy——
        "true" 必须同样写 GLOBAL_PAUSED（旧口径 `= "1"` 会误写成 SKIPPED）。"""
        self.env["YIBAN_GLOBAL_PAUSE"] = "true"
        self.env["FAKE_TIMEOUT_EXIT"] = "2"
        r = self._run()
        self.assertEqual(r.returncode, 2, self._stderr(r))
        with io.open(self._status_path(), encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "GLOBAL_PAUSED")

    def test_second_run_truthy_value_shares_one_domain(self):
        """取值域统一：YIBAN_SECOND_RUN=true 必须以"补签轮身份"被识别——
        旧口径 `= "1"` 会把 true 当成首签轮去评估第三轮（本用例钉住不再评估）。"""
        scripts = os.path.join(self.tmp, "scripts")
        os.makedirs(scripts, exist_ok=True)
        # 桩 signin：--second-run-check 恒答 10（需要补跑）。若 true 未被识别为
        # 补签轮身份，就会真的进入"等待→二轮"分支（00:01 已过点），行为可观测。
        with io.open(os.path.join(scripts, "signin.py"), "w", encoding="utf-8",
                     newline="\n") as f:
            f.write("import sys\nsys.exit(10 if '--second-run-check' in sys.argv else 0)\n")
        _write_python_wrapper(self.tmp)
        self.env["YIBAN_SECOND_RUN"] = "true"
        self.env["YIBAN_SECOND_RUN_TIME"] = "00:01"
        r = self._run()
        self.assertEqual(r.returncode, 0, self._stderr(r))
        calls = self._timeout_calls()
        self.assertEqual([c for c in calls if c.startswith("SECOND_RUN=")],
                         ["SECOND_RUN=true"],
                         f"true 身份下不得再评估第三轮，实际: {calls}")
        self.assertIn("无需补跑", self._log_text())


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
        for name, body in (("flock", FAKE_FLOCK_K3), ("timeout", FAKE_TIMEOUT_K3)):
            p = os.path.join(self.fakebin, name)
            with io.open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(body)
            os.chmod(p, 0o755)
        self.timeout_log = os.path.join(self.tmp, "timeout-calls.log")
        self.env = dict(os.environ)
        self.env.update({
            "YIBAN_STATE_DIR": self.state,
            # run.sh 现在要求应用目录存在（进不去即拒绝运行，见 APP_DIR 注释）；
            # 测试把 APP_DIR 指向临时目录即可——本类只关心标记/超时语义，
            # 被 fake timeout 拦住的 signin 调用不会真的执行
            "YIBAN_APP_DIR": self.tmp,
            "FAKE_TIMEOUT_LOG": self.timeout_log,
            "FAKE_FLOCK_EXIT": "0",
        })
        _install_seal_stub(self.tmp)            # 封存前置：--second-run-check 恒答 0
        for k in ("YIBAN_SECOND_RUN", "YIBAN_RUN_TIMEOUT_SEC", "YIBAN_SIGN_END",
                  "YIBAN_LOG_FILE", "YIBAN_WORKERS", "YIBAN_FALLBACK_ENABLE"):
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
        # 2026-09-10（批次20 B3）：当日收尾标记同理会短路后续触发，一并清理
        settled = os.path.join(self.state, f"yiban-settled-{_today()}.marker")
        for p in (marker, status, settled):
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
