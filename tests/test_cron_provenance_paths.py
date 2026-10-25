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


#: 白名单 `^/[A-Za-z0-9._/-]+$` 之外的检出根（二审 R1 逐类覆盖）。
#: 每条都必须按环境错误（rc=2）拒判——黑名单枚举不完，故按字符类竖表钉住。
BAD_APP_ROOTS = (
    "/root/a b",      # 空格：sh 拆词
    "/root/a\\b",     # 反斜杠：转义
    "/opt/x;rm",      # 分号：命令分隔
    "/opt/$HOME",     # 美元：参数展开
    '/opt/a"b',       # 双引号：改变引号状态
    "/opt/a'b",       # 单引号：改变引号状态
    "/opt/a`b`",      # 反引号：命令替换
    "/opt/a&b",       # &：后台
    "/opt/a|b",       # |：管道
    "/opt/a\tb",      # 制表符
    "/opt/a\rb",      # 回车
    "/opt/a\nb",      # 换行：截断 cron 表
    "/opt/中文",       # 非 ASCII（白名单外）
    "relative/path",  # 非绝对路径
)


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

    def _check(self, command, env_extra=None):
        _write(os.path.join(self.cron, "yiban-x"), command + "\n")
        env = dict(os.environ)
        env.update(env_extra or {})
        return subprocess.run(
            [BASH, GATE, "--cron-dir", self.cron, "--manifest", self.manifest],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120, env=env)

    def _out(self, r):
        return (r.stdout or "") + (r.stderr or "")

    def test_app_root_outside_whitelist_is_refused(self):
        """检出根不匹配白名单 `^/[A-Za-z0-9._/-]+$` ⇒ 拒判（rc=2）。

        这不是"有违规"（1），而是环境错误（2）——门对这类路径给不出有意义的
        "可回指"结论。与 install.sh 的入口门、scripts/pull-prod-backup.sh 同口径
        （本单二审 R1：白名单优先于黑名单，竖表逐类覆盖）。
        """
        for bad in BAD_APP_ROOTS:
            with self.subTest(app_root=bad):
                r = self._check(
                    "5 6 * * * yiban @YIBAN_APP_DIR@/run.sh",
                    env_extra={"YIBAN_APP_DIR": bad})
                self.assertEqual(r.returncode, 2,
                                 "白名单外的检出根未按环境错误拒判：rc=%d\n%s"
                                 % (r.returncode, self._out(r)))
                self.assertIn("白名单", self._out(r))

    def test_app_root_inside_whitelist_is_green(self):
        """正常路径必须照旧判绿：标准部署与含 `._-/数字` 的临时路径。"""
        for good in ("/opt/yiban-auto-sign", "/tmp/a_b.c-1"):
            with self.subTest(app_root=good):
                r = self._check(
                    "5 6 * * * yiban @YIBAN_APP_DIR@/run.sh",
                    env_extra={"YIBAN_APP_DIR": good})
                self.assertEqual(r.returncode, 0,
                                 "白名单内的检出根被误拒：rc=%d\n%s"
                                 % (r.returncode, self._out(r)))

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

    # ---- 模板占位符：展开后与硬写路径同判（2026-10-25 部署件参数化） ----------

    def test_app_dir_placeholder_resolves_to_checkout(self):
        """`@YIBAN_APP_DIR@/run.sh` 展开成检出根后必须可回指。"""
        r = self._check("5 6 * * * yiban @YIBAN_APP_DIR@/run.sh")
        self.assertEqual(r.returncode, 0,
                         "模板占位符未按检出根解析：\n" + self._out(r))

    def test_app_dir_placeholder_cd_relative_is_green(self):
        """生产 yiban-audit 表的模板形态：cd 占位符目录后走相对路径。"""
        r = self._check(
            "30 2 * * * yiban cd @YIBAN_APP_DIR@/ && .venv/bin/python3"
            " scripts/audit_verify.py --db data/yiban.db --env data/.env"
            " >> /var/log/yiban/audit-verify.log 2>&1")
        self.assertEqual(r.returncode, 0, self._out(r))

    def test_app_dir_placeholder_env_value_is_checked(self):
        """`APP_DIR=@YIBAN_APP_DIR@` 是路径值，展开后必须可回指（哨兵表形态）。"""
        r = self._check(
            "5 8 * * * root APP_DIR=@YIBAN_APP_DIR@ /usr/local/sbin/yiban-backup.sh")
        self.assertEqual(r.returncode, 0, self._out(r))

    def test_app_dir_placeholder_missing_repo_file_is_red(self):
        r = self._check("5 5 * * * yiban @YIBAN_APP_DIR@/no-such-script.sh")
        self.assertNotEqual(r.returncode, 0,
                            "占位符下的无来源件仍放行：\n" + self._out(r))
        self.assertIn("no-such-script.sh", self._out(r))

    def test_app_dir_placeholder_escape_is_red(self):
        # 注意：`/etc/*` 在门的判据里是放行的配置路径；越界样本要落到**不受信**的
        # 位置（/tmp）才能证明"展开后仍按真实路径判"。
        r = self._check("5 6 * * * yiban @YIBAN_APP_DIR@/../../tmp/x.sh")
        self.assertNotEqual(r.returncode, 0,
                            "占位符下的相对越界仍放行：\n" + self._out(r))

    def test_unknown_placeholder_is_red(self):
        r = self._check("5 6 * * * yiban @OTHER_DIR@/run.sh")
        self.assertNotEqual(r.returncode, 0,
                            "未知占位符被当路径放行（不得靠猜测）：\n" + self._out(r))

    def test_sibling_dir_with_app_root_prefix_is_red(self):
        """`/opt/yiban-auto-sign-forged` 不是检出根的子路径（前缀相似不算）。"""
        r = self._check("5 6 * * * yiban /opt/yiban-auto-sign-forged/run.sh")
        self.assertNotEqual(r.returncode, 0, self._out(r))

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

    def test_scheme_suffix_does_not_hide_absolute_path(self):
        """`/tmp/x.sh://` 不是 scheme URL：`://` 尾巴不得绕过绝对路径判定。"""
        r = self._check(
            "5 6 * * * yiban %s/run.sh --opt=/tmp/x.sh://" % APP_ROOT)
        self.assertNotEqual(r.returncode, 0, "绝对路径藏在 `://` 尾巴后仍放行：\n" + self._out(r))
        self.assertIn("/tmp/x.sh", self._out(r))

    def test_scheme_suffix_does_not_hide_relative_escape(self):
        """`../../etc/passwd://` 不是 scheme URL：相对越界仍判红。"""
        r = self._check(
            "5 6 * * * yiban %s/run.sh --opt=../../etc/passwd://" % APP_ROOT)
        self.assertNotEqual(r.returncode, 0, "相对越界藏在 `://` 尾巴后仍放行：\n" + self._out(r))
        self.assertIn("../../etc/passwd://", self._out(r))

    def test_file_scheme_payload_is_still_a_path(self):
        """`file://` 的载荷是路径，仍须判定（不得当普通 URL 放过）。"""
        r = self._check(
            "5 6 * * * yiban %s/run.sh --opt=file:///tmp/x.sh" % APP_ROOT)
        self.assertNotEqual(r.returncode, 0, "file:// 载荷里的 /tmp 仍放行：\n" + self._out(r))

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
