# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""部署路径的解析口径与恢复件核验的结论分类。

标签：J · 运维：部署/备份/发布
覆盖：`YIBAN_STATE_DIR`/`YIBAN_LOG_FILE` 在 `.env` 与进程环境两种来源下的解析优先级、
    引擎与 web 常量是否跟随 .env、backup.sh `--restore` 的四种结论（通过/篡改/工具崩溃/
    工具缺失）、备份日志目录跟随配置、pull-prod-backup 脚本的参数/只读/新鲜度契约与
    环境变量注入护栏（全部行为断言，ssh/scp 桩记录 argv 并真执行远端命令串）。
对应实现：路径解析在 `yiban/infra/paths.py` 与 `run.sh`/`web/app.py`；恢复核验与结论
    分类在 `scripts/backup.sh` 的 `--restore` 分支。
关键断言：① 进程环境优先于 .env，空值继续回落到默认值；② 恢复核验退出码 0=通过、
    1=检出篡改、2=无法定论（含工具缺失/崩溃），**退出码 1 不得当"无法核验"用**——
    解释器缺依赖时 Python 也以 1 退出，会把合法演练报成"审计被改写"。
依赖：`ResolvePathTest`/`StateDirHonoursEnvFileTest` 纯 Python；`RestoreVerdictTest`、
    `BackupLogDirTest` 需要 bash（class 级 skipIf）；`PullProdBackupBehaviorTest`/
    `PullProdBackupInjectionTest` 起 bash 子进程真跑 pull-prod-backup.sh，ssh/scp 用
    fakebin 桩替身（桩会真的执行收到的远端命令串，注入探针文件即失守证据），
    不碰网络、无真实主机名；`--restore` 相关用例含 tar 解包与 SQLite integrity_check。

两条都来自"真实部署者只照 README 做"的演练，且都会给出**错误结论**而不报错：
`.env` 里的路径过去只对 `run.sh` 那条路生效，web 进程静默回落到 `/var/log/yiban`——
同机第二份部署因此与第一份共用状态目录、锁与磁盘外锚点。
"""

import datetime
import io
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tests"))

from yiban.engine import state_io  # noqa: E402
from yiban.infra import env_io  # noqa: E402


class ResolvePathTest(unittest.TestCase):
    """`env_io.resolve_path`：进程环境 → .env → 默认值，三者优先级钉死。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="resolve-path-")
        self.env_file = os.path.join(self.tmp, ".env")
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write("YIBAN_STATE_DIR=/from/env-file\nYIBAN_LOG_FILE=/from/env-file/sign.log\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_env_file_is_second_source(self):
        self.assertEqual(
            env_io.resolve_path("YIBAN_STATE_DIR", "/fallback", env={}, env_file=self.env_file),
            "/from/env-file")
        self.assertEqual(
            env_io.resolve_path("YIBAN_LOG_FILE", "/fallback", env={}, env_file=self.env_file),
            "/from/env-file/sign.log")

    def test_process_env_wins_over_env_file(self):
        self.assertEqual(
            env_io.resolve_path("YIBAN_STATE_DIR", "/fallback",
                                env={"YIBAN_STATE_DIR": "/from/env"}, env_file=self.env_file),
            "/from/env")

    def test_blank_values_fall_through_to_default(self):
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write("YIBAN_STATE_DIR=   \n")
        self.assertEqual(
            env_io.resolve_path("YIBAN_STATE_DIR", "/fallback", env={}, env_file=self.env_file),
            "/fallback")
        self.assertEqual(
            env_io.resolve_path("YIBAN_STATE_DIR", "/fallback",
                                env={"YIBAN_STATE_DIR": "  "},
                                env_file=os.path.join(self.tmp, "missing.env")),
            "/fallback")

    def test_missing_env_file_is_not_fatal(self):
        self.assertEqual(
            env_io.resolve_path("YIBAN_STATE_DIR", "/fallback", env={},
                                env_file=os.path.join(self.tmp, "nope.env")),
            "/fallback")


class StateDirHonoursEnvFileTest(unittest.TestCase):
    """引擎与 web 的路径常量都必须认 .env（不只认进程环境）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="state-dir-env-")
        self.state = os.path.join(self.tmp, "custom-state")
        os.makedirs(self.state, exist_ok=True)
        self.env_file = os.path.join(self.tmp, ".env")
        with io.open(self.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_STATE_DIR={self.state}\n"
                    f"YIBAN_LOG_FILE={self.state}/sign.log\n")
        self._old = {k: os.environ.get(k) for k in
                     ("YIBAN_ENV_FILE", "YIBAN_STATE_DIR", "YIBAN_LOG_FILE")}
        for k in ("YIBAN_STATE_DIR", "YIBAN_LOG_FILE"):
            os.environ.pop(k, None)
        os.environ["YIBAN_ENV_FILE"] = self.env_file

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_engine_state_dir_follows_env_file(self):
        self.assertEqual(state_io._state_dir(), self.state,
                         "引擎的状态目录必须认 .env 里的 YIBAN_STATE_DIR")

    def test_process_env_still_wins(self):
        os.environ["YIBAN_STATE_DIR"] = "/from/process-env"
        try:
            self.assertEqual(state_io._state_dir(), "/from/process-env")
        finally:
            os.environ.pop("YIBAN_STATE_DIR", None)

    def test_web_module_constants_follow_env_file(self):
        """web 是模块级常量，必须在导入期就解析对（gunicorn 走 create_app() 不跑 main）。"""
        import contextlib
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "webapp_state_dir", os.path.join(BASE, "web", "app.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["webapp_state_dir"] = mod
        with contextlib.suppress(Exception):
            spec.loader.exec_module(mod)
        if not hasattr(mod, "STATE_DIR"):
            raise AssertionError("web/app.py 未能加载，路径常量无从比对")
        self.assertEqual(os.path.normpath(mod.STATE_DIR), os.path.normpath(self.state))
        self.assertEqual(os.path.normpath(mod.LOG_FILE),
                         os.path.normpath(os.path.join(self.state, "sign.log")))


@unittest.skipIf(shutil.which("bash") is None, "需要 bash")
class RestoreVerdictTest(unittest.TestCase):
    """恢复件核验：崩溃 / 检出篡改 / 通过 / 无法核验，四态互不混淆。"""

    SCRIPT = os.path.join(BASE, "scripts", "backup.sh")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="restore-verdict-")
        self.app = os.path.join(self.tmp, "app")
        os.makedirs(os.path.join(self.app, "scripts"), exist_ok=True)
        os.makedirs(os.path.join(self.app, ".venv", "bin"), exist_ok=True)
        # 桩"部署自带解释器"：行为由 STUB_MODE 决定，用来分别模拟崩溃与真实结论
        stub = os.path.join(self.app, ".venv", "bin", "python")
        with io.open(stub, "w", encoding="utf-8", newline="\n") as f:
            f.write('#!/bin/bash\n'
                    'case "${STUB_MODE:-ok}" in\n'
                    '  ok) echo "链内自洽：通过"; echo "审计可追溯性校验通过" ; exit 0 ;;\n'
                    '  tamper) echo "锚点比对：失败"; '
                    'echo "审计可追溯性校验失败：审计记录可能被篡改/删除"; exit 1 ;;\n'
                    '  crash) echo "Traceback (most recent call last):"; '
                    'echo "ModuleNotFoundError: No module named \'Crypto\'" >&2; exit 1 ;;\n'
                    'esac\n')
        os.chmod(stub, 0o755)  #桩解释器必须可执行：backup.sh 是直接调 $APP_DIR/.venv/bin/python 的
        with io.open(os.path.join(self.app, "scripts", "audit_verify.py"),
                     "w", encoding="utf-8") as f:
            f.write("# 桩：真实脚本由上面的解释器桩替代\n")  #audit_verify.py 只需存在——判定看的是桩的退出码与输出文案
        # 备份包：含库 + .env + 锚点
        self.archive = os.path.join(self.tmp, "pkg.tar.gz")
        src = os.path.join(self.tmp, "src")
        os.makedirs(os.path.join(src, "data"))
        os.makedirs(os.path.join(src, "state"))
        # 必须是**真**的 SQLite 库：恢复路径要跑 PRAGMA integrity_check，假文件会以
        # "读取失败"把通过路径也判成失败（测的就不是核验分类了）。
        import sqlite3
        conn = sqlite3.connect(os.path.join(src, "data", "yiban.db"))
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        with io.open(os.path.join(src, "data", ".env"), "w", encoding="utf-8") as f:
            f.write("YIBAN_AUDIT_KEY=" + "b" * 64 + "\n")
        with io.open(os.path.join(src, "state", "audit-anchor.log"), "w",
                     encoding="utf-8") as f:
            f.write("2026-09-18 00:00:00 1 1 1 0 " + "a" * 64 + " " + "0" * 64 + "\n")
        with tarfile.open(self.archive, "w:gz") as t:
            t.add(src, arcname=".")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _restore(self, mode):
        env = dict(os.environ)
        env["APP_DIR"] = "app"          # 相对 cwd：Git Bash 的 tar 不认 C:\ 形式
        env["STUB_MODE"] = mode
        r = subprocess.run(["bash", self.SCRIPT, "--restore", "pkg.tar.gz",
                            "restored-" + mode],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, cwd=self.tmp, timeout=180)
        return r, (r.stdout or "") + (r.stderr or "")

    def test_clean_verdict_passes(self):
        r, out = self._restore("ok")
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("审计校验通过", out)

    def test_tamper_verdict_is_reported_as_tamper(self):
        r, out = self._restore("tamper")
        self.assertEqual(r.returncode, 1, out)
        self.assertIn("检出异常", out)
        self.assertNotIn("无法定论", out)

    def test_tool_crash_is_not_reported_as_tamper(self):
        """解释器缺依赖（ImportError，退出码同样是 1）绝不能报成"被篡改"。"""
        r, out = self._restore("crash")
        self.assertEqual(r.returncode, 2, out)
        self.assertIn("无法定论", out)
        self.assertNotIn("链被改写/删除", out)

    def test_missing_tooling_is_not_a_pass(self):
        """包内含库但机器上没有可用解释器/脚本：不能算作"恢复成功"。"""
        shutil.rmtree(os.path.join(self.app, ".venv"))  #解释器和脚本一起删才叫"机器上没有工具"：只删一个仍会走到核验分支
        shutil.rmtree(os.path.join(self.app, "scripts"))
        env = dict(os.environ)
        env["APP_DIR"] = "app"
        r = subprocess.run(["bash", self.SCRIPT, "--restore", "pkg.tar.gz", "restored-none"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, cwd=self.tmp, timeout=180)
        out = (r.stdout or "") + (r.stderr or "")
        self.assertEqual(r.returncode, 2, out)
        self.assertIn("未经审计核验", out)


@unittest.skipIf(shutil.which("bash") is None, "需要 bash")
class BackupLogDirTest(unittest.TestCase):
    """备份抓的日志目录必须跟随配置，而不是硬编码 /var/log/yiban。"""

    SCRIPT = os.path.join(BASE, "scripts", "backup.sh")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="backup-logdir-")
        self.app = os.path.join(self.tmp, "app")
        self.logs = os.path.join(self.tmp, "my-logs")
        os.makedirs(self.logs, exist_ok=True)
        os.makedirs(self.app, exist_ok=True)
        self.marker = "sign-2026-01-01.log"
        with io.open(os.path.join(self.logs, self.marker), "w", encoding="utf-8") as f:
            f.write("本轮签到日志\n")
        with io.open(os.path.join(self.app, ".env"), "w", encoding="utf-8") as f:
            f.write("YIBAN_ACCOUNTS_KEY=" + "a" * 64 + "\n"
                    f"YIBAN_LOG_FILE={self.logs}/sign.log\n")
        self.backups = os.path.join(self.tmp, "backups")
        os.makedirs(self.backups, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_backup_follows_configured_log_dir(self):
        env = dict(os.environ)
        env.pop("YIBAN_LOG_FILE", None)          # 只从 .env 取，模拟 cron 直跑
        env["APP_DIR"] = "app"
        env["BACKUP_DIR"] = "backups"
        env["BACKUP_PLAINTEXT"] = "1"            # 免 gpg，只验抓哪个目录
        r = subprocess.run(["bash", self.SCRIPT], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env,
                           cwd=self.tmp, timeout=180)
        out = (r.stdout or "") + (r.stderr or "")
        archives = [os.path.join(self.backups, n) for n in os.listdir(self.backups)
                    if n.endswith(".tar.gz")]
        self.assertTrue(archives, f"没有产出归档：{out}")
        with tarfile.open(archives[0]) as t:
            names = t.getnames()
        self.assertTrue(any(n.endswith(self.marker) for n in names),
                        f"归档未包含配置目录里的日志：{names}")
        self.assertFalse([n for n in names if n.endswith(".log")
                          and self.marker not in n and "sign-" in n],
                         f"归档混入了配置目录之外的签到日志：{names}")


SCRIPT = os.path.join(BASE, "scripts", "pull-prod-backup.sh")

# ---- 行为测试脚手架：记录 argv 且**真的执行**远端命令串的 ssh/scp 桩 ----
# 桩把收到的远端命令串原样交给 bash -c 跑：注入若成立（探针文件出现）即护栏失守；
# 护栏生效 ⇒ 桩根本不会被调用（校验前置），或被调用的串里只有 ls/sha256sum 等合法命令。
# 主机名与路径全部合成（prod.example / /tmp 夹具），无任何真实凭据或现网名。

_FAKE_SSH = """#!/bin/bash
# ssh 桩：记录 argv（\\x1e 分隔），再把最后一个参数当作"远端命令"就地执行。
{ printf 'SSH'; printf '\\036%s' "$@"; printf '\\n'; } >> "$SSH_STUB_LOG"
_last=""
for _a in "$@"; do _last="$_a"; done
exec bash -c "$_last"
"""

_FAKE_SCP = """#!/bin/bash
# scp 桩：记录 argv；把 host:/path 当作源、绝对路径参数当目标（只支持下载方向）。
{ printf 'SCP'; printf '\\036%s' "$@"; printf '\\n'; } >> "$SSH_STUB_LOG"
pos=()
for _a in "$@"; do
    case "$_a" in
        -*) ;;
        *) pos+=("$_a") ;;
    esac
done
src=""
dst=""
for _a in "${pos[@]}"; do
    case "$_a" in
        *:*) src="${_a#*:}" ;;
        */*) dst="$_a" ;;
    esac
done
[ -n "$src" ] && [ -n "$dst" ] || exit 1
if [ "${SCP_STUB_MODE:-copy}" = "corrupt" ]; then
    cp -f -- "$src" "$dst" && printf 'GARBAGE\\n' >> "$dst"
else
    cp -f -- "$src" "$dst"
fi
"""


class _PullRunBase(unittest.TestCase):
    """隔离的"生产端"夹具目录 + fakebin PATH 里的 ssh/scp 桩，子进程真跑脚本。"""

    def setUp(self):
        self.bash = shutil.which("bash")
        self.tmp = tempfile.mkdtemp(prefix="pull-prod-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.remote = os.path.join(self.tmp, "remote")   # 生产备份目录替身
        self.mirror = os.path.join(self.tmp, "mirror")   # 本地镜像目录
        self.home = os.path.join(self.tmp, "home")
        self.fakebin = os.path.join(self.tmp, "fakebin")
        self.pwned = os.path.join(self.tmp, "PWNED")     # 注入被执行探针
        for d in (self.remote, self.mirror, self.home, self.fakebin):
            os.makedirs(d, exist_ok=True)
        self.stub_log = os.path.join(self.tmp, "stub-calls.log")
        self._fake("ssh", _FAKE_SSH)
        self._fake("scp", _FAKE_SCP)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("YIBAN_", "REMOTE_", "PULL_", "LOCAL_", "STALE_"))}
        env.update({
            "PATH": self._u(self.fakebin) + os.pathsep + os.environ.get("PATH", ""),
            "SSH_STUB_LOG": self._u(self.stub_log),
            "YIBAN_SSH_HOST": "prod.example",            # 合成别名
            "REMOTE_BACKUP_DIR": self._u(self.remote),
            "LOCAL_MIRROR_DIR": self._u(self.mirror),
            "HOME": self._u(self.home),
        })
        self.env = env

    def _u(self, path):
        """cygpath 归一（Git Bash 下 Windows 路径进不了 bash 世界），WSL 原样返回。"""
        conv = subprocess.run([self.bash, "-c", 'cygpath -u "$1" 2>/dev/null || echo "$1"',
                               "_", path], capture_output=True, text=True, timeout=60)
        return conv.stdout.strip() or path

    def _fake(self, name, body):
        path = os.path.join(self.fakebin, name)
        with io.open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(body)
        os.chmod(path, 0o755)

    def _remote_archive(self, days_ago=0, name=None):
        """在生产端夹具里放一个合成密文包（名字/内容全假），返回文件名。"""
        n = name or "yiban-%s.tar.gz.gpg" % (
            (datetime.date.today() - datetime.timedelta(days=days_ago)).isoformat())
        p = os.path.join(self.remote, n)
        with open(p, "wb") as f:
            f.write(os.urandom(64))
        ts = time.time() - days_ago * 86400
        os.utime(p, (ts, ts))
        return n

    def _run(self, args=(), extra_env=None, omit=()):
        env = dict(self.env)
        for k in omit:
            env.pop(k, None)
        env.update(extra_env or {})
        r = subprocess.run([self.bash, SCRIPT, *args], capture_output=True,
                           env=env, cwd=self.tmp, timeout=180)
        out = ((r.stdout or b"") + (r.stderr or b"")).decode("utf-8", errors="replace")
        return r.returncode, out

    def _stub_calls(self):
        """解析桩记录：[('SSH', argv...), ('SCP', argv...)]；未被调用则空列表。"""
        if not os.path.exists(self.stub_log):
            return []
        calls = []
        with io.open(self.stub_log, encoding="utf-8", errors="replace") as f:
            for raw in f:
                parts = raw.rstrip("\n").split("\x1e")
                if parts and parts[0] in ("SSH", "SCP"):
                    calls.append(parts)
        return calls

    def _ssh_cmds(self):
        """每次 ssh 调用实际送达"远端"的命令串。"""
        return [c[-1] for c in self._stub_calls() if c[0] == "SSH"]

    def _scp_downloads(self):
        """scp 下载对 (host:path, 本地目标)——桩里唯一可能的方向就是生产→工作站。"""
        out = []
        for c in self._stub_calls():
            if c[0] != "SCP":
                continue
            src = [a for a in c[1:] if ":" in a and not a.startswith("-") and "=" not in a]
            dst = [a for a in c[1:] if "/" in a and ":" not in a
                   and not a.startswith("-") and "=" not in a]
            out.append((src[0] if src else "", dst[-1] if dst else ""))
        return out


class PullProdBackupBehaviorTest(_PullRunBase):
    """既有 10 条 assertIn 源码文本契约（假绿族整改项）改造为行为断言。"""

    def test_bash_syntax_ok(self):
        r = subprocess.run([self.bash, "-n", SCRIPT], capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))

    def test_unknown_arg_exits_2(self):
        rc, _ = self._run(("--nope",))
        self.assertEqual(rc, 2, "未知参数应返回 2（用法错误）")

    def test_help_prints_usage(self):
        r = subprocess.run([self.bash, SCRIPT, "-h"], capture_output=True, env=self.env)
        self.assertEqual(r.returncode, 0)
        self.assertIn("用法", r.stdout.decode("utf-8", "replace"))

    def test_happy_path_pulls_verifies_and_exits_zero(self):
        """合法值 ⇒ 行为不变：列目录、取哈希、下载、复算一致后入库并写随行清单。"""
        self._remote_archive(days_ago=0)
        self._remote_archive(days_ago=1)
        rc, out = self._run()
        self.assertEqual(rc, 0, out)
        names = sorted(os.listdir(self.mirror))
        today = datetime.date.today().isoformat()
        yest = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        want = ["yiban-%s.tar.gz.gpg" % today, "yiban-%s.tar.gz.gpg" % yest]
        for n in want:
            self.assertIn(n, names)
            self.assertIn(n + ".sha256", names)
        import hashlib
        with open(os.path.join(self.mirror, want[0]), "rb") as f:
            got = hashlib.sha256(f.read()).hexdigest()
        with io.open(os.path.join(self.mirror, want[0] + ".sha256"), encoding="utf-8") as f:
            self.assertEqual(f.read().split()[0], got, "随行清单必须是本地复算的哈希")

    def test_remote_side_is_readonly_ls_and_sha256_only(self):
        """只读契约的**行为**版：送达远端的每条命令只许 ls/sha256sum 与 head/awk 管道，
        且 scp 只有下载方向（host:path → 本地镜像）。读源码文本证明不了这个。"""
        self._remote_archive()
        rc, out = self._run()
        self.assertEqual(rc, 0, out)
        self.assertTrue(self._ssh_cmds(), "至少要有一次远端 ls")
        for cmd in self._ssh_cmds():
            for seg in cmd.split("|"):
                verb = seg.strip().split()[0]
                self.assertIn(verb, ("ls", "sha256sum", "head", "awk"),
                              "远端出现白名单外动词：%s" % cmd)
            # 只允许吞诊断用的 2>/dev/null；其余重定向/复合执行片段一律算越界
            stripped = cmd.replace("2>/dev/null", "")
            for bad in (";", ">", "<", "&&", "||", "`", "$(", "rm ", "mv ", "dd "):
                self.assertNotIn(bad, stripped, "远端命令串含写入/复合执行片段：%s" % cmd)
        downloads = self._scp_downloads()
        self.assertTrue(downloads, "应有 scp 下载")
        for src, dst in downloads:
            self.assertIn("prod.example:", src)
            self.assertTrue(dst.startswith(self._u(self.mirror)),
                            "scp 目标必须在本地镜像目录：%s" % dst)

    def test_plaintext_archives_are_never_pulled(self):
        """只搬密文：明文 yiban-*.tar.gz 即使在生产目录里，也不得出现在任何远端命令
        或 scp 下载中。"""
        self._remote_archive(days_ago=0, name="yiban-%s.tar.gz" %
                             datetime.date.today().isoformat())
        self._remote_archive(days_ago=0)
        rc, out = self._run()
        self.assertEqual(rc, 0, out)
        for src, _dst in self._scp_downloads():
            self.assertTrue(src.endswith(".tar.gz.gpg"), "拉了非密文件：%s" % src)
        plain = [n for n in os.listdir(self.mirror)
                 if n.endswith(".tar.gz") or (n.endswith(".gpg") and ".tar.gz.gpg" not in n)]
        self.assertEqual(plain, [], "明文包混进镜像目录：%s" % plain)

    def test_corrupted_download_is_quarantined_as_bad(self):
        """三向哈希校验的行为版：下载内容与远端哈希不一致 ⇒ 改名 .bad 保留证据、
        不入正式件、整轮非 0 退出。"""
        self._remote_archive()
        rc, out = self._run(extra_env={"SCP_STUB_MODE": "corrupt"})
        self.assertEqual(rc, 1, out)
        bad = [n for n in os.listdir(self.mirror) if n.endswith(".bad")]
        self.assertEqual(len(bad), 1, "应留下且只留下一份 .bad：%s" % out)
        self.assertEqual([n for n in os.listdir(self.mirror) if n.endswith(".tar.gz.gpg")],
                         [], "校验失败的文件不得以正式名入库")
        self.assertIn("校验失败", out)

    def test_status_flags_stale_remote(self):
        """新鲜度自检的行为版①：本地与远端同为 10 天前的副本（不落后）、但远端超过
        STALE_DAYS 未更新 ⇒ --status 非 0 并点名疑似中断（备份链路停摆告警）。"""
        old = self._remote_archive(days_ago=10)
        shutil.copyfile(os.path.join(self.remote, old), os.path.join(self.mirror, old))
        rc, out = self._run(("--status",))
        self.assertEqual(rc, 1, out)
        self.assertIn("疑似中断", out)

    def test_status_flags_local_behind_remote(self):
        """新鲜度自检的行为版②：本地落后于远端 ⇒ 非 0 并点名落后。"""
        import hashlib
        old = self._remote_archive(days_ago=5)
        self._remote_archive(days_ago=0)
        data = open(os.path.join(self.remote, old), "rb").read()
        dst = os.path.join(self.mirror, old)
        with open(dst, "wb") as f:
            f.write(data)
        with io.open(dst + ".sha256", "w", encoding="utf-8", newline="\n") as f:
            f.write("%s  %s\n" % (hashlib.sha256(data).hexdigest(), old))
        rc, out = self._run(("--status",))
        self.assertEqual(rc, 1, out)
        self.assertIn("落后于远端", out)

    def test_incremental_backfill_second_run_redoes_nothing(self):
        """补齐式增量的行为版：首轮把缺的历史副本全部拉回，次轮零下载、按已同步收尾。"""
        self._remote_archive(days_ago=0)
        self._remote_archive(days_ago=1)
        self._remote_archive(days_ago=2)
        rc, out = self._run()
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self._scp_downloads()), 3, "首轮应补齐 3 份")
        scp_before = len([c for c in self._stub_calls() if c[0] == "SCP"])
        rc2, out2 = self._run()
        self.assertEqual(rc2, 0, out2)
        self.assertEqual(len([c for c in self._stub_calls() if c[0] == "SCP"]),
                         scp_before, "次轮不该再下载")
        self.assertIn("已同步 3 份", out2)

    def test_max_fetch_caps_download_count(self):
        self._remote_archive(days_ago=0)
        self._remote_archive(days_ago=1)
        self._remote_archive(days_ago=2)
        rc, out = self._run(extra_env={"PULL_MAX_FETCH": "2"})
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self._scp_downloads()), 2, "MAX_FETCH 上限必须生效：%s" % out)

    @unittest.skipUnless(platform.system() == "Linux", "生产机防呆判定依赖 uname -s=Linux")
    def test_refuses_to_run_on_production_host(self):
        """防呆的行为版：本机存在 /opt/yiban-auto-sign（=生产机特征）⇒ 拒绝、非 0、
        零远端调用。"""
        sentinel = "/opt/yiban-auto-sign"
        if os.path.exists(sentinel):
            self.skipTest("%s 已存在（可能真在生产机上）" % sentinel)
        if not os.access("/opt", os.W_OK):
            self.skipTest("/opt 不可写，无法模拟生产机特征")
        os.makedirs(sentinel)
        self.addCleanup(os.rmdir, sentinel)
        self._remote_archive()
        rc, out = self._run()
        self.assertEqual(rc, 1, out)
        self.assertIn("异机副本必须在另一台机器上拉取", out)
        self.assertEqual(self._stub_calls(), [], "防呆生效后不得有任何远端调用")

    def test_default_mirror_dir_is_neutral(self):
        """默认镜像目录落在 $HOME/yiban-prod-mirror（行为验证：不传 LOCAL_MIRROR_DIR，
        真 HOME 下应出现镜像与日志），不得内嵌任何运维者个人目录布局。"""
        self._remote_archive()
        rc, out = self._run(omit=("LOCAL_MIRROR_DIR",))
        self.assertEqual(rc, 0, out)
        default_mirror = os.path.join(self.home, "yiban-prod-mirror")
        self.assertTrue(os.path.isfile(os.path.join(default_mirror, "pull.log")),
                        "默认镜像目录未按 $HOME/yiban-prod-mirror 创建：%s" % out)
        self.assertTrue([n for n in os.listdir(default_mirror)
                         if n.startswith("yiban-") and n.endswith(".tar.gz.gpg")],
                        "默认镜像目录里应有密文副本")
        # 防回退靠行为兜住：若有人把默认值改回个人目录（D:/code/... 等），此断言即红。


class PullProdBackupInjectionTest(_PullRunBase):
    """活体反例：三个进远端命令串的环境变量都必须被白名单挡在门外（rc=3，
    任何远端命令都不许被执行——ssh 桩记录必须为空、注入探针文件必须不出现）。"""

    def _assert_refused(self, extra_env, var_name, rc_expected=3):
        rc, out = self._run(extra_env=extra_env)
        self.assertEqual(rc, rc_expected,
                         "%s 非法值必须拒绝执行并退 %d（实际 rc=%d）：%s"
                         % (var_name, rc_expected, rc, out))
        self.assertIn(var_name, out, "stderr 必须点名违规变量 %s：%s" % (var_name, out))
        self.assertEqual(self._stub_calls(), [],
                         "校验必须前置：%s 非法时不得有任何 ssh/scp 调用" % var_name)
        self.assertFalse(os.path.exists(self.pwned),
                         "注入命令被执行了！探针文件 %s 出现" % self.pwned)

    def test_remote_dir_injection_refused_before_any_ssh(self):
        """登记表原文反例：REMOTE_BACKUP_DIR='x; id #' 一步成立注入——必须拒绝。"""
        self._assert_refused({"REMOTE_BACKUP_DIR": "x; touch %s #" % self._u(self.pwned)},
                             "REMOTE_BACKUP_DIR")

    def test_max_fetch_injection_refused(self):
        self._assert_refused({"PULL_MAX_FETCH": "1; id"}, "PULL_MAX_FETCH")

    def test_ssh_host_option_injection_refused(self):
        """本机 RCE 臂：-oProxyCommand 作单 argv 可被 ssh 解析成执行体——必须拒绝，
        连 ssh 都不许被调起来。"""
        self._assert_refused(
            {"YIBAN_SSH_HOST": "-oProxyCommand=touch %s" % self._u(self.pwned)},
            "YIBAN_SSH_HOST")

    def test_reflowed_filename_with_quote_is_quarantined(self):
        """远端 ls 文件名回流支：含单引号的"文件名"可闭合 :93-96 的 sha256sum 拼接。
        白名单校验必须在进入任何远端命令/本地变量之前把它隔离；合法件照常拉取，
        整轮因隔离计数而非 0（fail-closed，不静默）。"""
        self._remote_archive(days_ago=1)  # 合法件先落，保证 ls 列表非空
        # 合成注入文件名：含单引号与分号（闭合 sha256sum 的单引号拼接），touch 走相对
        # 路径——桩 ssh 的 cwd 继承自脚本（=self.tmp），命中 PWNED 探针；文件名本身不含 /。
        crafted = "yiban-2026-09-20.tar.gz.gpg'; touch PWNED; echo x #.tar.gz.gpg"
        with open(os.path.join(self.remote, crafted), "wb") as f:
            f.write(b"not-really-a-backup")
        rc, out = self._run()
        self.assertFalse(os.path.exists(self.pwned),
                         "回流文件名里的注入被执行了（护栏失守）：%s" % out)
        for cmd in self._ssh_cmds():
            self.assertNotIn("touch", cmd, "远端命令串被回流名污染：%s" % cmd)
        self.assertEqual(rc, 1, "隔离了非法回流行 ⇒ 整轮必须非 0（fail-closed）：%s" % out)
        legit = "yiban-%s.tar.gz.gpg" % (
            datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        self.assertIn(legit, os.listdir(self.mirror), "合法件仍应正常拉取：%s" % out)
        self.assertIn(crafted[:20], out, "隔离动作必须留痕可查")

    def test_valid_values_still_pass_whitelist(self):
        """护栏不得误伤合法配置：默认值与嵌套路径都要放行（行为=整轮 rc 0）。"""
        for good in ("/var/backups", self._u(self.remote), self._u(self.remote) + "/"):
            rc, out = self._run(("--status",), extra_env={"REMOTE_BACKUP_DIR": good})
            self.assertNotEqual(rc, 3, "合法值 %r 被误拒：%s" % (good, out))
        for good in ("1", "30"):
            rc, out = self._run(("--status",), extra_env={"PULL_MAX_FETCH": good})
            self.assertNotEqual(rc, 3, "合法值 %r 被误拒：%s" % (good, out))
        for good in ("yiban", "prod.example", "host-1.internal"):
            rc, out = self._run(("--status",), extra_env={"YIBAN_SSH_HOST": good})
            self.assertNotEqual(rc, 3, "合法值 %r 被误拒：%s" % (good, out))


if __name__ == "__main__":
    unittest.main()
