# -*- coding: utf-8 -*-
"""backup.sh 旗标解析：全参数扫描、未知/移位即拒绝、`--require-encrypt` 位置无关。

标签：J · 运维：部署/备份/发布
覆盖：`scripts/backup.sh` 的参数解析——`--require-encrypt` 出现在任意位置都生效；
    未知参数一律拒绝（非 0，不再只看 `${1}` 后静默放过）；`--restore` 缺参给出明确
    拒绝而不是拿空串继续。
对应实现：`scripts/backup.sh` 顶部的 `while [ $# -gt 0 ]` + `case "$1"` 解析段。
关键断言：旧实现按 `${1}` 只认首位旗标，`--require-encrypt` 移位即门禁静默失效；
    本用例用"首位未知/次位旗标"这类活体反例钉住"解析全部参数"这一行为。
依赖：文本级断言（不跑子进程）+ bash 行为断言（`bash -n` 与早退路径）；
    未知参数与 `--restore` 缺参都在任何重活之前退出，不会真备份；无 bash 时行为
    用例 skipTest。
"""
import io
import os
import shutil
import subprocess
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(BASE, "scripts", "backup.sh")
RUN_SH = os.path.join(BASE, "run.sh")


def _read():
    with open(SCRIPT, encoding="utf-8") as f:
        return f.read()


class BackupFlagParsingTextTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = _read()

    def test_no_single_position_flag_checks_remain(self):
        body = "\n".join(
            ln for ln in self.src.splitlines()
            if not ln.strip().startswith("#") and "旧实现" not in ln)
        self.assertNotIn('if [ "${1:-}" = "--restore" ]', body,
                         "仍按位置只看 ${1} 判 --restore")
        self.assertNotIn('if [ "${1:-}" = "--require-encrypt" ]', body,
                         "仍按位置只看 ${1} 判 --require-encrypt（移位即门禁失效）")

    def test_parses_all_arguments_in_a_loop(self):
        self.assertIn("while [ $# -gt 0 ]", self.src, "必须全参数扫描")
        self.assertIn('case "$1" in', self.src)
        self.assertIn("--require-encrypt)", self.src)
        self.assertIn("--restore)", self.src)
        self.assertIn("未知参数", self.src, "未知参数必须显式拒绝")


class BackupFlagParsingBehaviorTest(unittest.TestCase):
    def setUp(self):
        self.bash = shutil.which("bash")
        if not self.bash:
            self.skipTest("无 bash 环境")

    def _run(self, *args):
        return subprocess.run([self.bash, SCRIPT, *args],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60)

    def test_bash_syntax_ok(self):
        r = subprocess.run([self.bash, "-n", SCRIPT], capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", errors="replace"))

    def test_unknown_flag_is_rejected(self):
        r = self._run("--bogus")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("未知参数", r.stderr)

    def test_unknown_first_flag_does_not_hide_later_require_encrypt(self):
        """旧实现只看 ${1}：首位不是旗标时次位的 --require-encrypt 被静默忽略。"""
        r = self._run("--bogus", "--require-encrypt")
        self.assertNotEqual(r.returncode, 0, "未知参数必须拒绝，不能静默放过")
        self.assertIn("未知参数", r.stderr)

    def test_require_encrypt_then_unknown_is_rejected(self):
        r = self._run("--require-encrypt", "--bogus")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("未知参数", r.stderr)

    def test_restore_without_args_is_rejected(self):
        r = self._run("--restore")
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue("备份包不存在" in r.stderr or "恢复目标" in r.stderr, r.stderr)

    def test_restore_with_missing_target_is_rejected_with_message(self):
        """`--restore <库>` 只剩 1 个位置参数：不得因解析尾部 shift 归零而静默退 1。"""
        r = self._run("--restore", "/tmp/yiban-no-such-archive.tar.gz")
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue(r.stderr.strip(),
                        "缺目标目录必须给出诊断，不能零输出地 rc=1（解析期 shift 失败）")
        self.assertTrue("备份包不存在" in r.stderr or "恢复目标" in r.stderr, r.stderr)

    def test_restore_with_two_args_is_rejected_with_message(self):
        """`--restore a b`（包不存在）：拒绝且带明确诊断，不静默中止。"""
        r = self._run("--restore", "a", "b")
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue("备份包不存在" in r.stderr or "恢复目标" in r.stderr, r.stderr)

    def test_restore_with_extra_arg_is_rejected(self):
        """`--restore a b c` 多出的 c 必须报错，不得被静默丢弃。"""
        r = self._run("--restore", "a", "b", "c")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("未知参数", r.stderr)
        self.assertIn("c", r.stderr)

    def test_help_exits_zero(self):
        r = self._run("--help")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--require-encrypt", r.stdout)


class EnvGetMatchesRunShTest(unittest.TestCase):
    """M26：backup.sh 的 env_get 必须与 run.sh 的 .env 解析逐字同口径。

    旧实现 `sed -n "s/^$1=//p"` 不剥 BOM、不认键名前导空白、不做两侧 strip：`.env` 写成
    `\\ufeff  YIBAN_STATE_DIR = /srv/x` 时 run.sh 取到 /srv/x，backup.sh 取空并回落到
    /var/log/yiban——归档缺真锚点且只打一行提示。本用例用**两份脚本的真源码**（各自的
    解析函数/解析块原样抽出，不改写）跑同一份 .env，要求输出逐字相同。
    """

    def setUp(self):
        self.bash = shutil.which("bash")
        if not self.bash:
            self.skipTest("无 bash 环境")
        self.tmp = tempfile.mkdtemp(prefix="envget-parity-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env_file = os.path.join(self.tmp, ".env")
        with io.open(self.env_file, "w", encoding="utf-8", newline="\n") as f:
            # BOM + 键名前导空白 + 值两侧空白（Windows 记事本/手改 .env 的真实形态）
            f.write("\ufeff  YIBAN_STATE_DIR = /srv/x  \n"
                    "YIBAN_LOG_FILE=/srv/x/sign.log\n"
                    "# 注释行\n")

    def _harness(self, name, prelude, body, tail):
        path = os.path.join(self.tmp, "harness-%s.sh" % name)
        with io.open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write("#!/bin/bash\n" + prelude + "\n" + body + "\n" + tail + "\n")
        return path

    def _backup_env_get_body(self):
        """backup.sh 的 env_get 函数原文（含其 sed 解析口径）。"""
        src = _read()
        start = src.index("env_get() {")
        return src[start:src.index("\n}\n", start) + 2]

    def _run_sh_parse_body(self):
        """run.sh 的 .env 解析块原文（与 yiban/infra/env_io 同口径的那段）。"""
        with io.open(RUN_SH, encoding="utf-8") as f:
            src = f.read()
        start = src.index('if [ -r "$ENV_PATH" ]; then')
        return src[start:src.index("\nfi\n", start) + 3]

    def _run(self, script, arg):
        env = {k: v for k, v in os.environ.items() if not k.startswith("YIBAN_")}
        r = subprocess.run([self.bash, script, arg], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.splitlines()

    def test_backup_env_get_matches_run_sh_parsing(self):
        os.makedirs(os.path.join(self.tmp, "app"), exist_ok=True)
        shutil.copy2(self.env_file, os.path.join(self.tmp, "app", ".env"))
        key_tail = ("printf '%s\\n' \"$(env_get YIBAN_STATE_DIR)\"\n"
                    "printf '%s\\n' \"$(env_get YIBAN_LOG_FILE)\"")
        var_tail = ("printf '%s\\n' \"${YIBAN_STATE_DIR:-}\"\n"
                    "printf '%s\\n' \"${YIBAN_LOG_FILE:-}\"")
        backup_script = self._harness("backup", 'APP_DIR="$1"',
                                      self._backup_env_get_body(), key_tail)
        run_script = self._harness("runsh", 'ENV_PATH="$1"',
                                   self._run_sh_parse_body(), var_tail)
        got_backup = self._run(backup_script, os.path.join(self.tmp, "app"))
        got_run_sh = self._run(run_script, self.env_file)
        self.assertEqual(["/srv/x", "/srv/x/sign.log"], got_backup,
                         "backup.sh 的 env_get 未按 BOM+空白口径解析（旧实现取空回落默认）")
        self.assertEqual(got_run_sh, got_backup,
                         "backup.sh 与 run.sh 对同一份 .env 必须读出逐字相同的值")


if __name__ == "__main__":
    unittest.main(verbosity=2)
