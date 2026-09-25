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
import os
import shutil
import subprocess
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(BASE, "scripts", "backup.sh")


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

    def test_help_exits_zero(self):
        r = self._run("--help")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--require-encrypt", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
