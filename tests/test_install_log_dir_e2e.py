# -*- coding: utf-8 -*-
"""干净安装后 cron 的重定向目标必须存在（R3-009）。

标签：J · 运维：部署/备份/发布

模拟的真实用户动作：按 README 在干净主机上跑 `install.sh`，随后由 cron 在
`$HOME` 下执行 `/etc/cron.d/yiban-*` 的命令行。用户可见结果 = 签到/备份/审计
真的被拉起。

缺陷形状：六张 cron 表都 `>> /var/log/yiban/<x>.log`，而 `install.sh` 不建该目录
（README 让人手工 mkdir）。目录不存在时 `sh` 在**执行命令之前**就因重定向失败退出，
命令一行都不跑，且 cron 只往 mail 里丢一行——发布判据"零手动调整、一次完成"不成立。

判据用真隔离：`unshare -m` 的私有挂载命名空间里，把 `/var/log`、`/usr/local/sbin`、
`/etc/cron.d` 换成临时目录，再**只跑 install.sh**，然后从已安装的 cron 表里取出
命令行原文执行。断言命令真的执行（桩件写下的标记）且 rc=0。
"""
import io
import os
import shutil
import subprocess
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(BASE, "deploy", "prod", "install.sh")
CRON_SRC = os.path.join(BASE, "deploy", "prod", "cron.d", "yiban-probe")
BASH = shutil.which("bash")
UNSHARE = shutil.which("unshare")

CRON_NAME = "yiban-r3b-logdir"


def _write(path, text, mode=None):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    if mode is not None:
        os.chmod(path, mode)


@unittest.skipIf(BASH is None, "需要 bash（Git Bash/WSL）")
@unittest.skipIf(UNSHARE is None, "需要 unshare -m 隔离 /var/log、/usr/local/sbin、/etc/cron.d")
@unittest.skipIf(os.geteuid() != 0, "需要 root 才能在新挂载命名空间里 bind mount")
class InstallCreatesCronLogDirTest(unittest.TestCase):
    """R3-009：install.sh 建 cron 重定向目录，cron 命令因此真的执行。"""

    def setUp(self):
        if subprocess.run(["id", "-u", "yiban"], capture_output=True).returncode != 0:
            self.skipTest("需要服务账号 yiban（install.sh 的 data/ 段要求）")
        self.tmp = tempfile.mkdtemp(prefix="install-logdir-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # 隔离面：三个会被写入的绝对路径
        self.var_log = os.path.join(self.tmp, "var-log")
        self.local_sbin = os.path.join(self.tmp, "local-sbin")
        self.cron_d = os.path.join(self.tmp, "cron-d")
        for d in (self.var_log, self.local_sbin, self.cron_d):
            os.makedirs(d)
        # 最小检出：install.sh + manifest（桩件落 /usr/local/sbin）+ 一张 cron 表
        self.co = os.path.join(self.tmp, "checkout")
        os.makedirs(os.path.join(self.co, "scripts"))
        os.makedirs(os.path.join(self.co, "deploy", "prod"))
        shutil.copyfile(INSTALL, os.path.join(self.co, "deploy", "prod", "install.sh"))
        self.marker = os.path.join(self.tmp, "probe-mark.txt")
        _write(os.path.join(self.co, "scripts", "payload.sh"),
               "#!/bin/sh\nprintf 'probe-ran\\n' >> '%s'\nprintf 'probe-ran\\n'\n" % self.marker,
               0o755)
        _write(os.path.join(self.co, "deploy", "prod", "manifest.tsv"),
               "0700\tscripts/payload.sh\t/usr/local/sbin/yiban-r3b-probe.sh\n"
               "0644\tdeploy/prod/cron.d/%s\t/etc/cron.d/%s\n" % (CRON_NAME, CRON_NAME))
        # cron 表：与仓库原件同形（用户列 + 命令列 + 重定向到 /var/log/yiban/）
        _write(os.path.join(self.co, "deploy", "prod", "cron.d", CRON_NAME),
               "SHELL=/bin/sh\nPATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n"
               "MAILTO=\"\"\n"
               "*/10 * * * * yiban /usr/local/sbin/yiban-r3b-probe.sh"
               " >> /var/log/yiban/probe.log 2>&1\n")
        # cron 来源断言桩：本用例的对象是安装器与目录，不是来源门（另有专门用例）
        _write(os.path.join(self.co, "scripts", "check-cron-provenance.sh"),
               "#!/usr/bin/env bash\nexit 0\n")
        # M01 前置门以 root 收件的三个检出内触点（依赖同步步）：本用例无 venv，
        # 该步只告警跳过；这两件仍会被 stat，故必须存在。
        _write(os.path.join(self.co, "scripts", "check-deps-lock.sh"),
               "#!/usr/bin/env bash\nexit 0\n")
        _write(os.path.join(self.co, "requirements.lock"), "flask==3.1.3\n")

    def _run_in_namespace(self):
        inner = r"""
set -e
mount --bind "$VAR_LOG" /var/log
mount --bind "$LOCAL_SBIN" /usr/local/sbin
mount --bind "$CRON_D" /etc/cron.d
bash "$CO/deploy/prod/install.sh" > "$TMP/install.out" 2>&1 || {
  echo "install_rc=$?"; tail -20 "$TMP/install.out"; exit 20;
}
echo "install_rc=0"
# 从**已安装**的 cron 表取命令行原文（跳过注释/空行/表级 env 行）
line=$(grep -vE '^[[:space:]]*(#|$)' "/etc/cron.d/__CRON__" \
       | grep -vE '^[A-Za-z_][A-Za-z0-9_]*=' | tail -1)
read -r _f1 _f2 _f3 _f4 _f5 _user cmd <<EOF_LINE
$line
EOF_LINE
sh -c "$cmd"
echo "cron_rc=$?"
[ -f /var/log/yiban/probe.log ] && cat /var/log/yiban/probe.log
""".replace("__CRON__", CRON_NAME)
        env = dict(os.environ)
        env.update({"TMP": self.tmp, "CO": self.co, "VAR_LOG": self.var_log,
                    "LOCAL_SBIN": self.local_sbin, "CRON_D": self.cron_d})
        return subprocess.run([UNSHARE, "-m", BASH, "-c", inner], env=env,
                              cwd=self.tmp, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=300)

    def test_cron_command_executes_after_clean_install(self):
        r = self._run_in_namespace()
        out = (r.stdout or "") + (r.stderr or "")
        self.assertIn("install_rc=0", out, "干净安装失败：\n" + out)
        self.assertIn("cron_rc=0", out,
                      "cron 命令没执行成功（重定向目标不存在时 sh 在执行前就退出）：\n" + out)
        self.assertTrue(os.path.exists(self.marker),
                        "cron 命令一行都没跑（桩件没有留下标记）：\n" + out)
        self.assertIn("probe-ran", out, "重定向目标里没有命令输出：\n" + out)
        # 目录语义：属主与服务账号一致、模式 0700（run.sh 的 M07 硬检查要求）
        st = os.stat(os.path.join(self.var_log, "yiban"))
        import pwd
        self.assertEqual(st.st_uid, pwd.getpwnam("yiban").pw_uid,
                         "cron 日志目录属主必须是服务账号")
        self.assertEqual(st.st_mode & 0o777, 0o700, "cron 日志目录模式必须是 0700")


if __name__ == "__main__":
    unittest.main(verbosity=2)
