# -*- coding: utf-8 -*-
"""`run.sh` 的日志与退出码契约：**任何**退出路径都留一行带退出码的日志。

标签：J · 运维：部署/备份/发布
覆盖：flock 弹开、当日已签到成功、当日已收尾、timeout 击杀、正常收尾五条路径的留痕；
    行首触发来源前缀（排程/手工，按 stdin 有无控制终端自动判，`YIBAN_TRIGGER` 可覆盖）；
    退出码 0/1/2/3/10 逐字透传；状态文件口径（0 写 SUCCESS、2 写 SKIPPED、其余不写）。
对应实现：`run.sh`（shell 侧，无 Python 对应物）。
关键断言：① 每条退出路径都有 `=== run.sh 退出，退出码: N ===`——原先只有正常收尾
    那一行，日志里看不出"这轮到底跑过没有、为什么没签"；② 加日志不得顺手改退出码。
依赖：需要 bash（class 级 skipIf，本机无 bash 时整类跳过）；子进程里真跑 run.sh，
    `flock`/`timeout` 用 PATH 上的假件，不真签到、不碰网络；pty 那条仅 POSIX，
    Windows 上逐条 skipTest。

`_run` 把 stdin 钉成 DEVNULL：触发来源看的是 stdin 有没有控制终端，宿主在终端里跑
pytest 时不钉死的话"排程"这条断言会随宿主而变。
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

#: 假装拿到了锁（run.sh 继续往下走）
FAKE_FLOCK_OK = "#!/usr/bin/env bash\nexit 0\n"
#: 假装锁被别的进程占着（run.sh 走"已有签到进程在运行，本次跳过"）
FAKE_FLOCK_BUSY = "#!/usr/bin/env bash\nexit 1\n"
#: 记录参数并回一个可控退出码：退出码契约与超时击杀路径都由此驱动
FAKE_TIMEOUT = r"""#!/usr/bin/env bash
{
  echo "ARGS=$*"
} >> "$FAKE_TIMEOUT_LOG"
exit ${FAKE_EXIT_CODE:-0}
"""


def _today():
    return datetime.now().strftime("%Y-%m-%d")


@unittest.skipIf(shutil.which("bash") is None, "需要 bash（Git Bash/WSL）")
class RunShExitTrailTest(unittest.TestCase):
    def setUp(self):
        self.bash = shutil.which("bash")
        self.tmp = tempfile.mkdtemp(prefix="runsh-trail-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.app_dir = os.path.join(self.tmp, "app")
        os.makedirs(self.app_dir)
        self.fakebin = os.path.join(self.tmp, "fakebin")
        os.makedirs(self.fakebin)
        self.calls = os.path.join(self.tmp, "timeout-calls.log")
        self._install_fakes(flock_body=FAKE_FLOCK_OK)
        # 封存前置：run.sh 封存当日收尾标记前须以库内事实判"确实无未了结"
        # （$PY scripts/signin.py --second-run-check），判定不可得 ⇒ 不封存并把
        # "成功"收场的退出码升 1。本文件钉的是 rc 契约透传与日志留痕，应用目录
        # 须备好恒答 0（无未了结）的桩件 + 转调当前解释器的 $PY 包装（双宿主确定）。
        scripts = os.path.join(self.app_dir, "scripts")
        os.makedirs(scripts, exist_ok=True)
        with io.open(os.path.join(scripts, "signin.py"), "w", encoding="utf-8",
                     newline="\n") as f:
            f.write("import sys\nsys.exit(0)\n")
        venv_bin = os.path.join(self.app_dir, ".venv", "bin")
        os.makedirs(venv_bin, exist_ok=True)
        pyw = os.path.join(venv_bin, "python3")
        with io.open(pyw, "w", encoding="utf-8", newline="\n") as f:
            f.write('#!/bin/sh\nexec "%s" "$@"\n' % sys.executable.replace("\\", "/"))
        os.chmod(pyw, os.stat(pyw).st_mode | 0o755)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("YIBAN_")}  #剥掉宿主 YIBAN_*：否则本机 .env 直接决定 run.sh 走哪条分支
        self.env.update({
            "YIBAN_APP_DIR": self.app_dir,
            "FAKE_TIMEOUT_LOG": self.calls,
            # 补签轮默认关闭：本文件钉的是"退出时留痕"，多跑一轮只会让退出码来源变糊
            "YIBAN_HOST_SECOND_ROUND": "0",
        })
        conv = subprocess.run(
            [self.bash, "-c", 'cygpath -u "$1" 2>/dev/null || echo "$1"', "_", self.fakebin],
            capture_output=True, text=True)
        self.env["PATH"] = (conv.stdout.strip() or self.fakebin) + os.pathsep + \
            self.env.get("PATH", "")

    def _install_fakes(self, *, flock_body, timeout_body=FAKE_TIMEOUT):
        for name, body in (("flock", flock_body), ("timeout", timeout_body)):
            path = os.path.join(self.fakebin, name)
            with io.open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(body)
            os.chmod(path, 0o755)  #假件必须可执行且抢在 PATH 前面，否则 run.sh 调到真 flock/timeout

    def _run(self, state, extra_env=None):
        """跑一次 run.sh；返回 (退出码, 日志文本)。状态目录由调用方准备。

        stdin 显式钉成 DEVNULL：触发来源判据看的是 stdin 有没有控制终端，而用例宿主
        自身的 stdin 可能是终端（开发者在终端里跑 pytest）——不钉死的话"排程"这条
        断言会随宿主的终端而变。
        """
        env = dict(self.env)
        env["YIBAN_STATE_DIR"] = state
        env["YIBAN_LOG_FILE"] = os.path.join(state, "sign.log")
        env.update(extra_env or {})
        r = subprocess.run([self.bash, RUN_SH], capture_output=True, env=env,
                           cwd=self.tmp, timeout=120, stdin=subprocess.DEVNULL)
        log_path = os.path.join(state, f"sign-{_today()}.log")
        text = ""
        if os.path.exists(log_path):
            with io.open(log_path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        return r.returncode, text

    @staticmethod
    def _exit_line(code, tag="排程"):
        return f"[{tag}] === run.sh 退出，退出码: {code} ==="

    def _assert_exit_line(self, text, code, tag="排程"):
        self.assertIn(self._exit_line(code, tag), text,
                      f"退出路径必须留痕（退出码 {code}）：\n{text}")


class ExitTrailTest(RunShExitTrailTest):
    """四条跳过/收场路径都必须留痕（原先一条都没有）。"""

    def test_flock_busy_path_logs_exit_zero(self):
        self._install_fakes(flock_body=FAKE_FLOCK_BUSY)
        state = tempfile.mkdtemp(prefix="state-", dir=self.tmp)
        code, text = self._run(state)
        self.assertEqual(code, 0, "被 flock 弹开仍是 0（既有语义）")
        self.assertIn("已有签到进程在运行，本次跳过", text)
        self._assert_exit_line(text, 0)

    def _seed_success_facts(self):
        """同批更新：SUCCESS 现在要与库内当日事实交叉核对后才采信。

        给 run.sh 一个可执行的解释器（$APP_DIR/.venv/bin/python3 包装，交叉核对
        走 $PY 直查 sqlite，不经假 timeout）+ 一个种了当日 done 行的临时库。
        """
        db = os.path.join(self.tmp, "facts-%d.db" % len(os.listdir(self.tmp)))
        con = sqlite3.connect(db)
        try:
            con.execute("CREATE TABLE sign_tasks (phone TEXT, day TEXT, state TEXT)")
            con.execute("INSERT INTO sign_tasks VALUES (?,?,?)",
                        ("138****0000", _today(), "done"))
            con.commit()
        finally:
            con.close()
        venv_bin = os.path.join(self.app_dir, ".venv", "bin")
        os.makedirs(venv_bin, exist_ok=True)
        pyw = os.path.join(venv_bin, "python3")
        with io.open(pyw, "w", encoding="utf-8", newline="\n") as f:
            f.write('#!/bin/sh\nexec "%s" "$@"\n' % sys.executable.replace("\\", "/"))
        os.chmod(pyw, os.stat(pyw).st_mode | 0o755)
        return db

    def test_already_success_path_logs_exit_zero(self):
        state = tempfile.mkdtemp(prefix="state-", dir=self.tmp)
        db = self._seed_success_facts()
        with io.open(os.path.join(state, f"sign-status-{_today()}.txt"), "w",
                     encoding="utf-8") as f:
            f.write("SUCCESS")
        code, text = self._run(state, {"YIBAN_DB_FILE": db})
        self.assertEqual(code, 0)
        self.assertIn("今天已签到成功，跳过执行", text)
        self._assert_exit_line(text, 0)

    def test_forged_success_status_is_not_the_skip_path(self):
        """活体反例（本文件侧的钉）：手写 SUCCESS、库内当日无完成 ⇒
        该路径不再是"已签到成功跳过"——拒绝采信 + 告警，本轮照常执行。"""
        state = tempfile.mkdtemp(prefix="state-", dir=self.tmp)
        db = self._seed_success_facts()
        con = sqlite3.connect(db)          # 清空当日事实：只留空表
        try:
            con.execute("DELETE FROM sign_tasks")
            con.commit()
        finally:
            con.close()
        with io.open(os.path.join(state, f"sign-status-{_today()}.txt"), "w",
                     encoding="utf-8") as f:
            f.write("SUCCESS")
        code, text = self._run(state, {"YIBAN_DB_FILE": db})
        self.assertEqual(code, 0, "伪造件被拒后本轮（假 timeout 退 0）正常收场")
        self.assertNotIn("今天已签到成功，跳过执行", text,
                         "无库内事实支撑的 SUCCESS 不得走幂等跳过")
        self.assertIn("拒绝采信", text)
        self.assertIn("run.sh 开始执行", text, "拒绝采信 ⇒ 按未完成继续跑本轮")

    def test_settled_marker_path_logs_exit_zero(self):
        state = tempfile.mkdtemp(prefix="state-", dir=self.tmp)
        open(os.path.join(state, f"yiban-settled-{_today()}.marker"), "w").close()  #收尾标记只看存在性，内容不参与判定
        code, text = self._run(state)
        self.assertEqual(code, 0)
        self.assertIn("今天已完成签到收尾", text)
        self._assert_exit_line(text, 0)

    def test_timeout_kill_path_logs_the_killed_code(self):
        """timeout 击杀（124）是原先最静默的一条：状态文件也不写，日志全无。"""
        state = tempfile.mkdtemp(prefix="state-", dir=self.tmp)
        code, text = self._run(state, {"FAKE_EXIT_CODE": "124"})
        self.assertEqual(code, 124, "超时击杀的退出码照原样透传给 cron")
        self._assert_exit_line(text, 124)

    def test_normal_completion_logs_exit_code(self):
        state = tempfile.mkdtemp(prefix="state-", dir=self.tmp)
        code, text = self._run(state, {"FAKE_EXIT_CODE": "0"})
        self.assertEqual(code, 0)
        self.assertIn("run.sh 开始执行", text)
        self._assert_exit_line(text, 0)


class ExitCodeContractTest(RunShExitTrailTest):
    """退出码契约 0/1/2/3/10 逐字不变——加日志不得顺手改码。"""

    def test_signin_codes_pass_through_verbatim(self):
        for code in (0, 1, 2, 3, 10):
            with self.subTest(code=code):
                state = tempfile.mkdtemp(prefix="state-", dir=self.tmp)
                got, text = self._run(state, {"FAKE_EXIT_CODE": str(code)})
                self.assertEqual(got, code, f"退出码 {code} 必须原样透传")
                self._assert_exit_line(text, code)

    def test_success_status_file_only_written_for_zero(self):
        """留痕逻辑不得改变状态文件口径：0 写 SUCCESS，2 写 SKIPPED，其余不写。"""
        for code, want in ((0, "SUCCESS"), (2, "SKIPPED"), (1, None), (3, None)):
            with self.subTest(code=code):
                state = tempfile.mkdtemp(prefix="state-", dir=self.tmp)
                self._run(state, {"FAKE_EXIT_CODE": str(code)})
                path = os.path.join(state, f"sign-status-{_today()}.txt")
                if want is None:
                    self.assertFalse(os.path.exists(path), f"退出码 {code} 不该写状态文件")
                else:
                    with io.open(path, encoding="utf-8") as f:
                        self.assertEqual(f.read().strip(), want)


class TriggerTagTest(RunShExitTrailTest):
    """触发来源前缀：无控制终端（stdin 非终端）= 排程；有 tty 或 YIBAN_TRIGGER = 手工/显式值。"""

    def test_no_tty_is_scheduled(self):
        state = tempfile.mkdtemp(prefix="state-", dir=self.tmp)
        _code, text = self._run(state)
        self.assertIn("[排程] === run.sh 开始执行 ===", text,
                      f"子进程没有控制终端，应判为排程触发：\n{text}")

    def test_manual_run_with_redirected_output_is_not_scheduled(self):
        """手工执行但把输出重定向进日志 ⇒ 仍判「手工」：判据只看 stdin 有没有控制终端。

        按 stdout/stderr 判会把这种最常见的排障姿势（`./run.sh > sign.log`）误报成
        排程——那正是"这条日志到底是谁触发的"要回答的问题。
        """
        if os.name == "nt":
            self.skipTest("pty 仅 POSIX")
        import pty  # 仅 POSIX：Windows 上无此模块（上一行已跳过）

        state = tempfile.mkdtemp(prefix="state-", dir=self.tmp)
        env = dict(self.env)
        env["YIBAN_STATE_DIR"] = state
        env["YIBAN_LOG_FILE"] = os.path.join(state, "sign.log")
        master, slave = pty.openpty()
        try:
            with io.open(os.path.join(self.tmp, "redirected.log"), "wb") as out:
                subprocess.run([self.bash, RUN_SH], stdin=slave, stdout=out,
                               stderr=subprocess.STDOUT, env=env, cwd=self.tmp,
                               timeout=120)
        finally:
            os.close(slave)
            os.close(master)
        with io.open(os.path.join(state, f"sign-{_today()}.log"), encoding="utf-8",
                     errors="replace") as f:
            text = f.read()
        self.assertIn("[手工] === run.sh 开始执行 ===", text,
                      f"stdin 是终端 ⇒ 人在终端里执行，输出重定向不该改变这个判断：\n{text}")

    def test_explicit_override_wins(self):
        state = tempfile.mkdtemp(prefix="state-", dir=self.tmp)
        code, text = self._run(state, {"YIBAN_TRIGGER": "手工"})
        self.assertEqual(code, 0)
        self.assertIn("[手工] === run.sh 开始执行 ===", text)
        self._assert_exit_line(text, 0, tag="手工")

    def test_override_value_is_used_verbatim(self):
        """容器/CI 可注入自己的来源标签，日志里原样出现（便于跨系统对齐）。"""
        state = tempfile.mkdtemp(prefix="state-", dir=self.tmp)
        _code, text = self._run(state, {"YIBAN_TRIGGER": "容器"})
        self.assertIn("[容器] === run.sh 开始执行 ===", text)


class SourceContractTest(unittest.TestCase):
    """源码级钉子：trap 与统一日志出口必须在（删掉就静默丢退出码留痕）。"""

    @classmethod
    def setUpClass(cls):
        with io.open(RUN_SH, encoding="utf-8") as f:
            cls.src = f.read()

    def test_traps_installed(self):
        self.assertIn("trap '_on_exit $?' EXIT", self.src, "EXIT trap 是留痕的唯一保证")
        self.assertIn("trap 'exit 143' TERM", self.src, "被 TERM 杀掉也要留一行现场")
        self.assertIn("trap 'exit 130' INT", self.src)

    def test_trap_installed_before_every_early_exit(self):
        """trap 必须装在第一条早退之前：否则 flock/SUCCESS 两条跳过路径照旧无痕。"""
        trap_at = self.src.index("trap '_on_exit $?' EXIT")
        self.assertLess(trap_at, self.src.index("今天已完成签到收尾（含补签轮）"),
                        "trap 装得比早退晚，跳过路径就漏了")
        self.assertLess(trap_at, self.src.index("已有签到进程在运行"))

    def test_all_log_writes_carry_the_tag(self):
        """所有日志走 _log（唯一带前缀的出口），不再散落裸 echo "[时间戳]"。"""
        self.assertEqual(self.src.count('echo "[$(date '), 2,
                         "带时间戳的 echo 只应剩 _log 与 _on_exit 两处（其余走 _log）")
        self.assertEqual(self.src.count("[$TRIGGER_TAG]"), 2,
                         "两处时间戳日志都必须带触发来源前缀")

    def test_trigger_tag_judges_on_stdin(self):
        """判据取 stdin（fd 0）：手工执行常把输出重定向进日志，按 stdout/stderr 判会误报排程。"""
        self.assertIn("elif [ -t 0 ]; then", self.src,
                      "触发来源判据必须是 stdin 有没有控制终端")
        self.assertNotIn("[ -t 1 ]", self.src,
                         "按 stdout 判会把「手工执行但重定向了输出」误判成排程")


if __name__ == "__main__":
    unittest.main(verbosity=2)
