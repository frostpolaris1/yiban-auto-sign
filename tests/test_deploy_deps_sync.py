# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""裸机部署的依赖同步与一致性门（工单 j06f）。

标签：J · 运维：部署/发布
覆盖：全部真跑 bash 子进程 + 真文件系统，不 grep 被测脚本源码。逐面：
    ① `scripts/check-deps-lock.sh`（venv 与 requirements.lock 一致性门）：
       lock 钉住的包与 venv 同版 ⇒ 0；某包版本落后 ⇒ 1 并点名双侧版本；
       某包缺失 ⇒ 1 并点名未安装；目标 venv 多装无关包 ⇒ 仍 0；
       marker 判假的条目缺席 ⇒ 不算漂移；清单出现未知 marker ⇒ 2（fail-closed）；
       venv 解释器缺失 ⇒ 2（环境错误，不是漂移）。
       活体反例：把 urllib3 从 lock 版本降一档 ⇒ 必红。门若只判"包在不在"，此例假绿。
    ② `deploy/prod/install.sh` 的依赖同步步（本仓唯一的裸机依赖同步点）：
       以 requirements.lock 为参调用**同一个** venv 的 pip；调用**之前**先落 pip freeze
       快照（回滚点）；同步后跑同一道门；门红 ⇒ 安装器非 0 且打印回滚命令；
       DESTDIR 暂存 ⇒ 跳过且不调用 pip；无 venv ⇒ 告警后继续（方式 B 不受本步管辖）；
       "venv 缺 bin/pip"的回退分支上，**打印出来的回滚命令必须跑得通**（复审 F2）。
       该步的检出内触点（门脚本、venv 入口、锁定清单）由 M01 判据同批收全——
       那三条活体反例住 tests/test_deploy_prod_artifacts.py 的 M01 族。
对应实现：`scripts/check-deps-lock.sh`、`deploy/prod/install.sh`。
关键断言：子进程退出码 + 真实产出的快照文件 + stub pip 记录的真实 argv 与调用次序。
判据宿主隔离：壳解释器带 `-S`（不加载 site-packages），假 dist-info 经 `PYTHONPATH`
注入。隔离的理由：本套件判"venv 装了什么"，不许受跑测机自己装了什么的干扰。
依赖：bash（`skipIf` 整文件）；无网络；不装任何包（pip 与解释器都是桩件）。
"""
import glob
import io
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASH = shutil.which("bash")
GATE = os.path.join(BASE, "scripts", "check-deps-lock.sh")
LOCK = os.path.join(BASE, "requirements.lock")
INSTALL = os.path.join(BASE, "deploy", "prod", "install.sh")

# pip 桩件：记录每次调用的 argv 与"调用时刻快照文件是否已存在"，freeze 打印固定内容。
# 为什么记录快照状态：判"回滚点先于同步落位"只能靠调用时刻的观测，不靠读源码。
PIP_STUB = """#!/bin/sh
snap=$(ls "$DRILL_CHECKOUT"/deps-snapshot-*.txt 2>/dev/null | head -1)
[ -n "$snap" ] || snap=none
{
  printf 'argv: %s\\n' "$*"
  printf 'snapshot_at_call: %s\\n' "$snap"
} >> "$DRILL_PIP_RECORD"
case "$1" in
  freeze) printf 'flask==3.1.3\\nurllib3==2.8.0\\n' ;;
esac
exit "${DRILL_PIP_EXIT:-0}"
"""

# venv 解释器桩件：真解释器 + `-S`（不加载 site-packages，只看 PYTHONPATH）。
# 额外代理 `-m pip` 到 pip 桩件——"venv 缺 bin/pip"那条回退分支要靠它才跑得动
# （真实 venv 的 bin/python3 带 pip 模块，这里用桩件等价替代）。
PY_STUB = """#!/bin/sh
if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then
  shift 2
  exec "$DRILL_PIP_STUB" "$@"
fi
exec %s -S "$@"
"""


def _write(path, text, mode=None):
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    if mode is not None:
        os.chmod(path, mode)


def _clean_env():
    """剥掉宿主上会干扰本套件的变量（配置域前缀 + 暂存前缀 + 解释器路径）。"""
    return {k: v for k, v in os.environ.items()
            if not k.startswith(("YIBAN_", "BACKUP_", "REMOTE_"))
            and k not in ("DESTDIR", "PYTHONPATH")}


def _out(r):
    return ((r.stdout or b"") + (r.stderr or b"")).decode("utf-8", errors="replace")


def _lock_pins():
    """requirements.lock 的非注释行 ⇒ [(名字, 版本)]。"""
    pins = []
    with io.open(LOCK, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            body = line.partition(";")[0].strip()
            name, sep, ver = body.partition("==")
            if not sep:
                continue
            pins.append((name.strip(), ver.strip()))
    return pins


def _write_fake_dists(root, overrides=None, drop=()):
    """按真 lock 造假 dist-info（目录名用 lock 原拼写，故意不归一化）。"""
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root)
    for name, ver in _lock_pins():
        if name in drop:
            continue
        ver = (overrides or {}).get(name, ver)
        d = os.path.join(root, "%s-%s.dist-info" % (name, ver))
        os.makedirs(d)
        _write(os.path.join(d, "METADATA"),
               "Metadata-Version: 2.1\nName: %s\nVersion: %s\n" % (name, ver))


# ------------------------------------------------------------------① 一致性门
@unittest.skipIf(BASH is None, "需要 bash")
class DepsLockGateTest(unittest.TestCase):
    """门的判据宿主是目标 venv 自己的解释器。本类用壳解释器 + 假 dist-info 造该环境。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="deps-lock-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.dists = os.path.join(self.tmp, "dist")
        self.venv = os.path.join(self.tmp, "venv")
        os.makedirs(os.path.join(self.venv, "bin"))
        # 壳解释器：真解释器 + `-S`（不加载 site-packages，只看 PYTHONPATH）
        _write(os.path.join(self.venv, "bin", "python3"),
               '#!/bin/sh\nexec %s -S "$@"\n' % sys.executable, 0o755)

    def _run(self, args=None):
        """默认查本类的假 venv；args 给定时逐字使用（用于环境错误类用例）。"""
        env = _clean_env()
        env["PYTHONPATH"] = self.dists
        argv = ["--venv", self.venv] if args is None else list(args)
        return subprocess.run([BASH, GATE, *argv], capture_output=True,
                              env=env, timeout=120)

    def test_gate_green_when_venv_matches_lock(self):
        _write_fake_dists(self.dists)
        r = self._run()
        self.assertEqual(r.returncode, 0, _out(r))

    def test_gate_red_when_a_locked_version_lags(self):
        """活体反例：锁定版本降一档 ⇒ 必红并点名双侧版本。"""
        locked = dict(_lock_pins())["urllib3"]
        lagged = "2.7.0"
        self.assertNotEqual(locked, lagged)
        _write_fake_dists(self.dists, overrides={"urllib3": lagged})
        r = self._run()
        out = _out(r)
        self.assertEqual(r.returncode, 1, out)
        self.assertIn("urllib3", out)
        self.assertIn(locked, out, "报告必须给出 lock 侧版本")
        self.assertIn(lagged, out, "报告必须给出 venv 侧版本")

    def test_gate_red_when_a_locked_package_is_missing(self):
        _write_fake_dists(self.dists, drop=("certifi",))
        r = self._run()
        out = _out(r)
        self.assertEqual(r.returncode, 1, out)
        self.assertIn("certifi", out)
        self.assertIn("未安装", out)

    def test_gate_ignores_packages_outside_the_lock(self):
        """多余包不算漂移：判据是"lock 的每个包都在且同版"，不是两集合相等。

        为什么必须有这条：CI 与本地门禁 venv 里另有 ruff/pytest/mypy 等工具包，
        若判"两集合相等"，门在那些环境里必然假红，于是没人会跑它。
        """
        _write_fake_dists(self.dists)
        extra = os.path.join(self.dists, "extra-tool-9.9.dist-info")
        os.makedirs(extra)
        _write(os.path.join(extra, "METADATA"),
               "Metadata-Version: 2.1\nName: extra-tool\nVersion: 9.9\n")
        r = self._run()
        self.assertEqual(r.returncode, 0, _out(r))

    def test_platform_marker_entry_absent_is_not_drift(self):
        """marker 判假的条目缺席不算漂移；判真时缺席必红。

        lock 里 colorama 带 `platform_system == "Windows"`。Linux 生产上它本就不装，
        若门不看 marker，每次生产巡检都会假红。
        """
        _write_fake_dists(self.dists, drop=("colorama",))
        r = self._run()
        if platform.system() == "Windows":
            self.assertEqual(r.returncode, 1, _out(r))
            self.assertIn("colorama", _out(r))
        else:
            self.assertEqual(r.returncode, 0, _out(r))

    def test_gate_fails_closed_on_unknown_marker(self):
        """清单出现本门不认识的 marker ⇒ 退 2 并点名，不静默跳过任何条目。"""
        odd = os.path.join(self.tmp, "odd.lock")
        _write(odd, '# 合成清单\nblinker==1.9.0\nfoo==1.0 ; python_version < "3.9"\n')
        _write_fake_dists(self.dists)
        r = self._run(["--venv", self.venv, "--lock", odd])
        out = _out(r)
        self.assertEqual(r.returncode, 2, out)
        self.assertIn('python_version < "3.9"', out)

    def _hint_entry(self, out):
        """从门的处置提示里抽出命令的入口（第一个 token）。"""
        m = re.search(r"处置：(.*)", out)
        self.assertIsNotNone(m, "门没有给出处置提示：%s" % out)
        return m.group(1).split()[0]

    def test_hint_command_matches_the_venv_entry_point(self):
        """处置提示里的命令必须抄得动（与 install.sh 回滚命令同一缺陷形状）。

        本类 venv 没有 bin/pip（真实形态之一）⇒ 提示必须走 `<解释器> -m pip`。
        反例：旧写法写死 `<venv>/bin/pip`，抄下来即 rc=127。
        """
        _write_fake_dists(self.dists, overrides={"urllib3": "2.7.0"})
        r = self._run()
        out = _out(r)
        self.assertEqual(r.returncode, 1, out)
        self.assertNotIn(os.path.join(self.venv, "bin", "pip"), out,
                         "提示给了不存在的 bin/pip ⇒ 照抄即 rc=127：%s" % out)
        self.assertIn("%s -m pip install -r" % os.path.join(self.venv, "bin", "python3"), out)
        self.assertTrue(os.access(self._hint_entry(out), os.X_OK),
                        "提示的入口没有执行位 ⇒ 照抄跑不动：%s" % out)

    def test_hint_command_prefers_bin_pip_when_present(self):
        """对侧：bin/pip 在场且可执行时提示用它（与 install.sh 的 pip 入口判据同口径）。"""
        pip_path = os.path.join(self.venv, "bin", "pip")
        _write(pip_path, "#!/bin/sh\nexit 0\n", 0o755)
        _write_fake_dists(self.dists, overrides={"urllib3": "2.7.0"})
        r = self._run()
        out = _out(r)
        self.assertEqual(r.returncode, 1, out)
        self.assertIn("%s install -r" % pip_path, out)
        self.assertTrue(os.access(self._hint_entry(out), os.X_OK), out)

    def test_hint_command_falls_back_when_bin_pip_has_no_exec_bit(self):
        """bin/pip 在但**无执行位** ⇒ 提示必须回落解释器（复审 B1，阻断项）。

        活体反例：谓词用 `os.path.isfile` 时，mode 644 的 bin/pip 满足"存在且是普通
        文件"⇒ 门印出 `<venv>/bin/pip install -r …`，照抄执行得 **rc=126
        Permission denied**。谓词必须答"能不能抄"（`os.access(..., X_OK)`），
        不是"在不在"。
        """
        pip_path = os.path.join(self.venv, "bin", "pip")
        _write(pip_path, "#!/bin/sh\nexit 0\n", 0o644)
        _write_fake_dists(self.dists, overrides={"urllib3": "2.7.0"})
        r = self._run()
        out = _out(r)
        self.assertEqual(r.returncode, 1, out)
        self.assertNotEqual(self._hint_entry(out), pip_path,
                            "提示给了无执行位的 bin/pip ⇒ 照抄即 rc=126：%s" % out)
        self.assertIn("%s -m pip install -r" % os.path.join(self.venv, "bin", "python3"), out)
        self.assertTrue(os.access(self._hint_entry(out), os.X_OK), out)

    def test_gate_reports_environment_error_without_venv(self):
        """venv 解释器缺失是环境错误（2），不是漂移（1）。"""
        r = self._run(["--venv", os.path.join(self.tmp, "nope")])
        out = _out(r)
        self.assertEqual(r.returncode, 2, out)
        self.assertIn("找不到 venv 解释器", out)


# --------------------------------------------------② install.sh 的依赖同步步
@unittest.skipIf(BASH is None, "需要 bash")
class InstallDepsSyncTest(unittest.TestCase):
    """裸机安装器的依赖同步步。同步点、回滚点、门的接线都在这里钉住。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="deps-sync-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.co = os.path.join(self.tmp, "checkout")
        os.makedirs(os.path.join(self.co, "scripts"))
        os.makedirs(os.path.join(self.co, "deploy", "prod"))
        # 最小检出：安装器只读 manifest 的源、执行两枚检出内脚本
        shutil.copyfile(INSTALL, os.path.join(self.co, "deploy", "prod", "install.sh"))
        shutil.copyfile(GATE, os.path.join(self.co, "scripts", "check-deps-lock.sh"))
        shutil.copyfile(LOCK, os.path.join(self.co, "requirements.lock"))
        _write(os.path.join(self.co, "scripts", "check-cron-provenance.sh"),
               "#!/usr/bin/env bash\nexit 0\n")
        _write(os.path.join(self.co, "scripts", "payload.sh"), "#!/bin/bash\ntrue\n")
        self.dest = os.path.join(self.tmp, "payload.sh")
        _write(os.path.join(self.co, "deploy", "prod", "manifest.tsv"),
               "0700\tscripts/payload.sh\t%s\n" % self.dest)
        self.record = os.path.join(self.tmp, "pip-record.txt")
        self.dists = os.path.join(self.tmp, "dist")

    # -- 夹具 ---------------------------------------------------------------
    def _make_venv(self, pip_exit=0, with_pip=True):
        """造假 venv。

        with_pip=False 复现"venv 缺 bin/pip"的形态——本脚本对它有回退分支
        （走 `<venv>/bin/python3 -m pip`），打印的回滚命令必须与之一致。
        """
        venv = os.path.join(self.co, ".venv")
        os.makedirs(os.path.join(venv, "bin"), exist_ok=True)
        # 桩件本体放检出外：`-m pip` 代理要靠它，而 with_pip=False 时
        # <venv>/bin/pip 本来就不存在（那正是本条要复现的形态）。
        self.pip_stub = os.path.join(self.tmp, "pip-stub.sh")
        _write(self.pip_stub, PIP_STUB, 0o755)
        _write(os.path.join(venv, "bin", "python3"),
               PY_STUB % sys.executable, 0o755)
        if with_pip:
            _write(os.path.join(venv, "bin", "pip"), PIP_STUB, 0o755)
        self.pip_exit = pip_exit

    def _run(self, extra_env=None, record=None):
        env = _clean_env()
        env["PYTHONPATH"] = self.dists
        env["DRILL_CHECKOUT"] = self.co
        env["DRILL_PIP_RECORD"] = record or self.record
        env["DRILL_PIP_EXIT"] = str(getattr(self, "pip_exit", 0))
        env["DRILL_PIP_STUB"] = getattr(self, "pip_stub", "")
        env.update(extra_env or {})
        return subprocess.run(
            [BASH, os.path.join(self.co, "deploy", "prod", "install.sh")],
            capture_output=True, env=env, timeout=180)

    def _snapshots(self):
        return sorted(glob.glob(os.path.join(self.co, "deps-snapshot-*.txt")))

    def _records(self, record=None):
        path = record or self.record
        if not os.path.isfile(path):
            return []
        with io.open(path, encoding="utf-8") as f:
            return f.read().splitlines()

    # -- 用例 ---------------------------------------------------------------
    def test_syncs_same_venv_to_lock_and_snapshots_first(self):
        """同步 = 以锁文件为参调用同一 venv 的 pip；回滚快照先落位且有内容。"""
        _write_fake_dists(self.dists)
        self._make_venv()
        r = self._run()
        out = _out(r)
        self.assertEqual(r.returncode, 0, out)

        recs = self._records()
        self.assertTrue(recs, "安装器必须真调用该 venv 的 pip：%s" % out)
        freeze_idx = [i for i, x in enumerate(recs) if x.startswith("argv: freeze")]
        install_idx = [i for i, x in enumerate(recs) if x.startswith("argv: install ")]
        self.assertEqual(1, len(freeze_idx), recs)
        self.assertEqual(1, len(install_idx), recs)
        self.assertLess(freeze_idx[0], install_idx[0],
                        "回滚快照必须先于同步：%s" % recs)
        self.assertIn(os.path.join(self.co, "requirements.lock"), recs[install_idx[0]],
                      "同步必须以检出内的 requirements.lock 为参：%s" % recs)
        snap_at_install = recs[install_idx[0] + 1]
        self.assertIn("snapshot_at_call: %s" % os.path.join(self.co, "deps-snapshot-"),
                      snap_at_install,
                      "同步那一刻快照必须已在：%s" % recs)

        snaps = self._snapshots()
        self.assertEqual(1, len(snaps), "必须留下唯一回滚快照：%s" % snaps)
        with io.open(snaps[0], encoding="utf-8") as f:
            self.assertEqual("flask==3.1.3\nurllib3==2.8.0\n", f.read(),
                             "快照必须是 pip freeze 的真实输出")

    def test_gate_red_after_sync_fails_installer_with_rollback(self):
        """同步后仍不一致 ⇒ 安装器非 0，且打印回滚命令。

        活体反例：venv 里 urllib3 停在旧版。pip 桩件返回 0（模拟"装了一遍"），
        门照旧判红——"装过"不许当"装对了"。
        """
        _write_fake_dists(self.dists, overrides={"urllib3": "2.7.0"})
        self._make_venv()
        r = self._run()
        out = _out(r)
        self.assertNotEqual(r.returncode, 0, "门红却放行安装：%s" % out)
        self.assertIn("urllib3", out, "必须能看到门的结论：%s" % out)
        self.assertIn("不一致", out, out)
        self.assertIn("回滚", out, out)
        snaps = self._snapshots()
        self.assertEqual(1, len(snaps), out)
        self.assertIn(snaps[0], out, "回滚命令必须点名快照文件：%s" % out)

    def test_destdir_staging_skips_sync(self):
        """DESTDIR 暂存不碰现网 venv：不调用 pip、不落快照。"""
        _write_fake_dists(self.dists)
        self._make_venv()
        destroot = os.path.join(self.tmp, "destdir")
        os.makedirs(destroot)
        r = self._run({"DESTDIR": destroot})
        out = _out(r)
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("跳过依赖同步", out)
        self.assertEqual([], self._records(), "暂存安装不得调用 pip：%s" % out)
        self.assertEqual([], self._snapshots(), "暂存安装不得往检出落快照")

    def test_rollback_command_runs_when_venv_has_no_bin_pip(self):
        """打印的回滚命令在其自身支持的分支上必须跑得通（复审 F2，必改项）。

        活体反例（复审实测）：旧写法把回滚命令硬编码成 `<venv>/bin/pip`，而本脚本
        在 venv 缺 `bin/pip` 时走 `<venv>/bin/python3 -m pip`。那条分支上打印的命令
        跑起来 rc=127（文件不存在），回滚点形同虚设。本条解析安装器**打印的那一行**，
        原样执行它：必须 rc=0，且记录到一次真实 install。
        """
        _write_fake_dists(self.dists)
        self._make_venv(with_pip=False)
        r = self._run()
        out = _out(r)
        self.assertEqual(r.returncode, 0, out)
        # 同步走了回退入口（记录里应有一次 install）
        self.assertTrue(any(x.startswith("argv: install ") for x in self._records()),
                        f"回退入口没被走到：{self._records()}")
        snaps = self._snapshots()
        self.assertEqual(1, len(snaps), out)
        m = re.search(r"（回滚：([^）]+)）", out)
        self.assertIsNotNone(m, f"安装器没打印回滚命令：{out}")
        cmd = m.group(1)
        self.assertIn("install -r %s" % snaps[0], cmd, cmd)
        self.assertNotIn(os.path.join(self.co, ".venv", "bin", "pip"), cmd,
                         f"回滚命令不许硬编码 bin/pip（该分支里它不存在）：{cmd}")
        # 原样执行打印的那一行：旧写法在这里 rc=127
        rollback_record = os.path.join(self.tmp, "rollback-record.txt")
        env = _clean_env()
        env.update({"PYTHONPATH": self.dists, "DRILL_CHECKOUT": self.co,
                    "DRILL_PIP_RECORD": rollback_record,
                    "DRILL_PIP_STUB": self.pip_stub})
        rr = subprocess.run([BASH, "-c", cmd], capture_output=True, env=env, timeout=120)
        self.assertEqual(rr.returncode, 0,
                         f"打印的回滚命令跑不通（旧写法 rc=127）：{_out(rr)}")
        self.assertTrue(any(x.startswith("argv: install ") for x in self._records(rollback_record)),
                        "回滚命令没有真跑到 pip" )

    def test_missing_venv_warns_and_continues(self):
        """无 venv ⇒ 告警后继续（方式 B 无 venv，本步不该拦它），且不调用 pip。"""
        _write_fake_dists(self.dists)
        r = self._run()
        out = _out(r)
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("找不到 venv 解释器", out)
        self.assertIn("跳过依赖同步", out)
        self.assertEqual([], self._records(), out)
        self.assertTrue(os.path.isfile(self.dest), "执行件落位不该被本步影响：%s" % out)


if __name__ == "__main__":
    unittest.main()
