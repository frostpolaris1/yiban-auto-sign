# -*- coding: utf-8 -*-
"""cron 来源门只放行"可回指仓库"的路径（R3-022）。

标签：J · 运维：部署/备份/发布

缺陷形状：门对 `/tmp/*` 一路放行，且只检查以 `/` 开头的 token——`./evil.sh`
这类相对路径完全不进判定。门因此只拦"未登记的仓库件"，不拦任意可执行路径。

判据（跑真脚本、真 cron 表夹具）：
  ① `/tmp/x.sh` ⇒ 红；
  ② 相对路径但命令行没有先 `cd` 到检出 ⇒ 红（无法定位）；
  ③ `cd` 到检出外再走相对路径 ⇒ 红；
  ④ 检出内但仓库里没有的绝对路径 ⇒ 红（既有判据，防回归）；
  ⑤ `cd <检出>/ && .venv/bin/python3 scripts/audit_verify.py` ⇒ 绿。
     这是生产 yiban-audit 表的真实形态；`.venv/` 与 `data/` 是部署期创建、
     不入库的路径，登记为显式豁免。
  ⑥ manifest 已登记的 /usr/local/sbin 件 ⇒ 绿；未登记 ⇒ 红（防回归）。
  ⑦ 值里的 URL 与纯数字比值不是路径（B-1）；贴紧式重定向目标不判（B-2）。
"""
import io
import os
import shutil
import subprocess
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE = os.path.join(BASE, "scripts", "check-cron-provenance.sh")
BASH = shutil.which("bash")

APP_ROOT = "/opt/yiban-auto-sign"

#: manifest 夹具：一行 = 一个已登记的生产执行件。
MANIFEST = (
    "0700\tscripts/backup.sh\t/usr/local/sbin/yiban-backup.sh\n"
)


def _write(path, text, mode=None):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    if mode is not None:
        os.chmod(path, mode)


@unittest.skipIf(BASH is None, "需要 bash（Git Bash/WSL）")
class CronProvenancePathScopeTest(unittest.TestCase):
    """R3-022：/tmp 与相对路径必须判红。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cron-prov-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cron = os.path.join(self.tmp, "cron.d")
        os.makedirs(self.cron)
        self.manifest = os.path.join(self.tmp, "manifest.tsv")
        _write(self.manifest, MANIFEST)

    def _check(self, command):
        _write(os.path.join(self.cron, "yiban-x"), command + "\n")
        return subprocess.run(
            [BASH, GATE, "--cron-dir", self.cron, "--manifest", self.manifest],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120)

    def _out(self, r):
        return (r.stdout or "") + (r.stderr or "")

    def test_tmp_path_is_red(self):
        r = self._check("5 6 * * * yiban /tmp/x.sh")
        self.assertNotEqual(r.returncode, 0, "cron 指向 /tmp 仍放行：\n" + self._out(r))
        self.assertIn("/tmp/x.sh", self._out(r))

    def test_relative_path_without_cd_is_red(self):
        r = self._check("5 6 * * * yiban ./evil.sh")
        self.assertNotEqual(r.returncode, 0,
                            "相对路径未先 cd 到检出仍放行：\n" + self._out(r))
        self.assertIn("./evil.sh", self._out(r))

    def test_relative_path_after_cd_outside_checkout_is_red(self):
        r = self._check("5 6 * * * yiban cd /tmp && ./evil.sh")
        self.assertNotEqual(r.returncode, 0,
                            "cd 到检出外再走相对路径仍放行：\n" + self._out(r))

    def test_missing_repo_file_under_app_root_is_red(self):
        r = self._check("5 6 * * * yiban %s/no-such-script.sh" % APP_ROOT)
        self.assertNotEqual(r.returncode, 0, self._out(r))
        self.assertIn("no-such-script.sh", self._out(r))

    def test_cd_into_checkout_then_relative_is_green(self):
        """生产 yiban-audit 表的真实形态：cd 检出后用相对路径。"""
        r = self._check(
            "30 2 * * * yiban cd %s/ && .venv/bin/python3 scripts/audit_verify.py"
            " --db data/yiban.db --env data/.env >> /var/log/yiban/audit-verify.log 2>&1"
            % APP_ROOT)
        self.assertEqual(r.returncode, 0,
                         "检出内 cd 后的相对路径被误判：\n" + self._out(r))

    def test_manifest_registered_and_ghost_paths(self):
        ok = self._check("0 2 * * * root /usr/local/sbin/yiban-backup.sh")
        self.assertEqual(ok.returncode, 0, self._out(ok))
        ghost = self._check("0 2 * * * root /usr/local/sbin/ghost.sh")
        self.assertNotEqual(ghost.returncode, 0, self._out(ghost))
        self.assertIn("/usr/local/sbin/ghost.sh", self._out(ghost))

    def test_tmp_path_in_flag_value_is_red(self):
        """`--opt=value` 的值也是路径面：/tmp 不得藏在等号右边。"""
        r = self._check(
            "5 6 * * * yiban cd %s/ && .venv/bin/python3 scripts/audit_verify.py"
            " --env=/tmp/x.sh" % APP_ROOT)
        self.assertNotEqual(r.returncode, 0, "旗标值里的 /tmp 仍放行：\n" + self._out(r))
        self.assertIn("/tmp/x.sh", self._out(r))

    def test_relative_escape_in_flag_value_is_red(self):
        r = self._check(
            "5 6 * * * yiban cd %s/ && .venv/bin/python3 scripts/audit_verify.py"
            " --db=../../../etc/passwd" % APP_ROOT)
        self.assertNotEqual(r.returncode, 0,
                            "旗标值里的相对越界仍放行：\n" + self._out(r))

    def test_tmp_path_in_env_assignment_is_red(self):
        r = self._check(
            "5 6 * * * yiban cd %s/ && YIBAN_DB_FILE=/tmp/x.db"
            " .venv/bin/python3 scripts/audit_verify.py" % APP_ROOT)
        self.assertNotEqual(r.returncode, 0,
                            "环境前缀里的 /tmp 仍放行：\n" + self._out(r))
        self.assertIn("/tmp/x.db", self._out(r))

    # ---- B-1：值里的 `/` 不是路径的充分条件 ---------------------------------

    def test_url_value_is_not_a_path(self):
        """`--url=https://…` 的 `/` 是 URL 分隔符，不是文件路径（B-1 误报）。"""
        r = self._check(
            "5 6 * * * yiban %s/run.sh --url=https://api.example.com/v1" % APP_ROOT)
        self.assertEqual(r.returncode, 0, "URL 值被当成路径判红：\n" + self._out(r))

    def test_numeric_ratio_value_is_not_a_path(self):
        """`--retention=30/7` 是数字比值，不是文件路径（B-1 误报）。"""
        r = self._check(
            "5 6 * * * yiban %s/run.sh --retention=30/7" % APP_ROOT)
        self.assertEqual(r.returncode, 0, "数字比值被当成路径判红：\n" + self._out(r))

    def test_relative_path_value_without_cd_is_still_red(self):
        """值里的真相对路径仍判红：`--db=data/yiban.db` 没有可达的 cwd（B-1 不放宽）。"""
        r = self._check(
            "5 6 * * * yiban %s/run.sh --db=data/yiban.db" % APP_ROOT)
        self.assertNotEqual(r.returncode, 0, "旗标值里的相对路径仍放行：\n" + self._out(r))
        self.assertIn("data/yiban.db", self._out(r))

    # ---- B-2：贴紧式重定向目标不判 -------------------------------------------

    def test_attached_fd_redirect_is_not_a_path(self):
        """`2>/dev/null` 是重定向目标，不是命令行路径 token（B-2 误报）。"""
        r = self._check(
            "5 6 * * * yiban %s/run.sh 2>/dev/null" % APP_ROOT)
        self.assertEqual(r.returncode, 0, "贴紧式重定向被当成路径：\n" + self._out(r))

    def test_attached_append_redirect_is_not_a_path(self):
        """`2>>/var/log/x.log` 同属重定向目标，不判。"""
        r = self._check(
            "5 6 * * * yiban %s/run.sh 2>>/var/log/x.log" % APP_ROOT)
        self.assertEqual(r.returncode, 0, "贴紧式追加重定向被当成路径：\n" + self._out(r))


if __name__ == "__main__":
    unittest.main(verbosity=2)
