# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""生产执行件入库与部署断言（M3 批次0：执行件入版本控制（高）/ 基线拉不到（仓内半条）/ 真名示例）。

标签：J · 运维：部署/备份/发布
覆盖：e2e 为准——全部真跑 bash 子进程 + 真实文件系统，不 grep 被测脚本源码。逐面：
    ① cron 路径来源断言：`scripts/check-cron-provenance.sh` 解析 `deploy/prod/cron.d/`
      三张表的每个绝对路径，必须能回指仓库来源（manifest dest 或 /opt/yiban-auto-sign
      仓库相对路径）。活体反例：往临时拷贝的 cron 表加一个无来源路径 ⇒ 非 0 并点名。
    ② wrapper 口令单跳：`deploy/prod/yiban-backup-wrapper.sh` 经 stub 记录子进程
      环境 ⇒ 无 BACKUP_GPG_PASSPHRASE / GPG_PASSPHRASE 键、无任何值含口令明文；
      口令确实经 stdin 到达。活体反例：旧 export 式 wrapper（测试内构造）经同一
      检查器必须判脏；wrapper 还会先 unset 遗留 env 注入（第二道活体反例）。
      正例加深：真 backup.sh + 真 gpg（有则跑）闭环，产出 .gpg 可用该口令解开。
    ③ install.sh：DESTDIR 无特权安装到 fixture 前缀；安装前后校验和输出；
      已存在文件校验和不符 ⇒ 拒装（现网 3 行手工漂移不得被静默覆盖），
      --adopt-production 归档现网件后以仓库为准；`.bak-*` 残留清理并记录。
    ④ check-deploy-target.sh：本地裸仓 fixture 远端（无网络）——目标提交在远端
      分支 ⇒ 0；不在 ⇒ 非 0 且输出人类可读结论。不做任何 push。
    ⑤ 真名示例门（合法的字面量门——字符串本身就是缺陷）：真实姓名（此处仅以转义
      拼接出现，测试文件自身不得命中）在**所有 git 跟踪文件**里零命中；同一扫描
      器对含名 fixture 必须命中（活体反例）。
对应实现：`scripts/check-cron-provenance.sh`、`deploy/prod/yiban-backup-wrapper.sh`、
`deploy/prod/install.sh`、`scripts/check-deploy-target.sh`、`scripts/backup.sh`（②正例）
与 git 跟踪树扫描器（⑤）。
关键断言：一律以真子进程的**退出码/产物/stdin/环境**为准（含旧 export 式 wrapper 必判脏、
校验和不符必拒装、无来源路径必点名、含名 fixture 必命中）——不 grep 被测脚本源码字符串。
依赖：bash（`skipIf` 整文件；Git Bash/WSL）；②正例真 gpg（无则单条 skip）；git（跟踪树
扫描与本地裸仓 fixture）；无网络、不 push。

测试夹具里的口令全部是明显的假值（"e2e-"前缀），不含任何真实凭据。
"""
import io
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASH = shutil.which("bash")

PROD_DIR = os.path.join(BASE, "deploy", "prod")
WRAPPER = os.path.join(PROD_DIR, "yiban-backup-wrapper.sh")
INSTALL = os.path.join(BASE, "deploy", "prod", "install.sh")
CRON_DIR = os.path.join(BASE, "deploy", "prod", "cron.d")
MANIFEST = os.path.join(BASE, "deploy", "prod", "manifest.tsv")
CHECK_CRON = os.path.join(BASE, "scripts", "check-cron-provenance.sh")
CHECK_TARGET = os.path.join(BASE, "scripts", "check-deploy-target.sh")
BACKUP_SH = os.path.join(BASE, "scripts", "backup.sh")

SYNTH_PASSPHRASE = "e2e-fd0-single-hop-passphrase"  # 合成假口令
# 真实姓名（待裁决 #6②）：只用转义拼，本文件自身永远不命中扫描门
REAL_NAME = "\u5e84\u65b9\u5b9c"
REAL_NAME_BYTES = REAL_NAME.encode("utf-8")

# 子进程环境里绝不允许出现的口令键（②的检查器契约）
PASSPHRASE_ENV_KEYS = {"BACKUP_GPG_PASSPHRASE", "BACKUP_AGE_PASSPHRASE", "GPG_PASSPHRASE"}

# stub：扮演 yiban-backup.sh——记录自身环境/stdin/argv，按 STUB_EXIT 退出
STUB_BACKUP = """#!/usr/bin/env bash
{ compgen -e | while IFS= read -r k; do printf '%s=%s\\n' "$k" "${!k}"; done; } > "$STUB_ENV_FILE"
IFS= read -r line || line=""
printf '%s' "$line" > "$STUB_STDIN_FILE"
printf '%s\\n' "$@" > "$STUB_ARGS_FILE"
exit "${STUB_EXIT:-0}"
"""

# 旧生产形态（登记的现象原文）：export 把口令带进整棵子进程树——活体反例用
LEGACY_EXPORT_WRAPPER = """#!/usr/bin/env bash
set -euo pipefail
export BACKUP_GPG_PASSPHRASE="$(cat "$YIBAN_BACKUP_PASSPHRASE_FILE")"
"$YIBAN_BACKUP_SCRIPT" --require-encrypt
"""


def _clean_subprocess_env():
    """剥掉宿主上可能干扰本套件的 YIBAN_/BACKUP_/REMOTE_ 变量。"""
    return {k: v for k, v in os.environ.items()
            if not k.startswith(("YIBAN_", "BACKUP_", "REMOTE_"))
            and k not in PASSPHRASE_ENV_KEYS | {"APP_DIR", "BACKUP_DIR", "RETENTION_DAYS",
                                                "KEY_FILE", "DESTDIR"}}


def _write(path, text, mode=None):
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    if mode is not None:
        os.chmod(path, mode)


def _parse_env_record(path):
    """stub 记录的 KEY=VALUE 行 → dict（值里可以含 =，按首个 = 切）。"""
    pairs = {}
    with io.open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if "=" in line:
                k, v = line.split("=", 1)
                pairs[k] = v
    return pairs


def _scan_file_for_name(path):
    """在单个文件里找真名字面量（字节级，二进制安全）。"""
    with open(path, "rb") as f:
        return REAL_NAME_BYTES in f.read()


def _missing_final_newline(path):
    """文本文件是否缺少结尾换行（空文件与二进制文件不算）。

    Vixie cron 对 /etc/cron.d 的表在没有结尾换行时可能丢掉最后一行；shell 脚本同理
    （末行命令可能不被执行）。deploy/prod 下的执行件全是文本，一律以换行收尾。
    """
    with open(path, "rb") as f:
        data = f.read()
    if not data or b"\x00" in data:
        return False
    return not data.endswith(b"\n")


def _ls_tracked():
    """git ls-files -z。WSL 里跑 Windows git worktree 时，.git 文件里的 gitdir
    是 Windows 绝对路径（D:/...），WSL git 无法解析——换算成 /mnt/<盘>/... 重试。"""
    listing = subprocess.run(["git", "-C", BASE, "ls-files", "-z"],
                             capture_output=True, timeout=120)
    if listing.returncode == 0:
        return listing.stdout
    gitfile = os.path.join(BASE, ".git")
    if os.path.isfile(gitfile):
        with io.open(gitfile, encoding="utf-8") as f:
            gitdir = f.read().strip()
        if gitdir.startswith("gitdir:"):
            gitdir = gitdir[len("gitdir:"):].strip().replace("\\", "/")
        if len(gitdir) > 2 and gitdir[1] == ":":
            gitdir = "/mnt/" + gitdir[0].lower() + gitdir[2:]
        listing = subprocess.run(["git", "--git-dir", gitdir,
                                  "--work-tree", BASE, "ls-files", "-z"],
                                 capture_output=True, timeout=120)
    assert listing.returncode == 0, listing.stderr.decode("utf-8", "replace")
    return listing.stdout


def _tracked_hit_scan():
    """扫描全部 git 跟踪文件，返回含真名的路径列表。"""
    hits = []
    for raw in _ls_tracked().split(b"\0"):
        if not raw:
            continue
        p = os.path.join(BASE, os.fsdecode(raw))
        try:
            if not os.path.isfile(p) or os.path.getsize(p) > 8 * 1024 * 1024:
                continue
            if _scan_file_for_name(p):
                hits.append(os.fsdecode(raw))
        except OSError:
            continue
    return hits


@unittest.skipIf(BASH is None, "需要 bash（Git Bash/WSL）")
class _TmpBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="deploy-prod-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env = _clean_subprocess_env()

    def _run_bash(self, script, args=(), env=None, cwd=BASE):
        e = dict(self.env)
        e.update(env or {})
        return subprocess.run([BASH, script, *args], capture_output=True,
                              env=e, cwd=cwd, timeout=300)

    def _out(self, r):
        return ((r.stdout or b"") + (r.stderr or b"")).decode("utf-8", errors="replace")


# ----------------------------------------------------------------① cron 来源断言
@unittest.skipIf(BASH is None, "需要 bash")
class CronProvenanceTest(_TmpBase):
    def _check(self, cron_dir, manifest=MANIFEST):
        return self._run_bash(CHECK_CRON,
                              ("--cron-dir", cron_dir, "--manifest", manifest))

    def test_repo_cron_tables_all_have_provenance(self):
        r = self._check(CRON_DIR)
        self.assertEqual(r.returncode, 0,
                         f"仓库三张 cron 表的路径必须全部可回指来源：{self._out(r)}")

    def test_manifest_sources_all_exist(self):
        # 用一个只含"仓内不存在来源"行的 manifest 反证来源校验真实执行
        bad = os.path.join(self.tmp, "manifest-bad.tsv")
        _write(bad, "0700\tscripts/definitely-not-here.sh\t/usr/local/sbin/x.sh\n")
        r = self._check(CRON_DIR, bad)
        self.assertNotEqual(r.returncode, 0,
                            f"manifest 指向仓内不存在的来源必须非 0：{self._out(r)}")
        self.assertIn("definitely-not-here.sh", self._out(r))

    def test_live_counterexample_unprovenanced_path_goes_red(self):
        """活体反例：往 cron 表加一个无来源可执行路径 ⇒ 非 0 且点名。"""
        d = os.path.join(self.tmp, "cron.d")
        shutil.copytree(CRON_DIR, d)
        with io.open(os.path.join(d, "yiban-sign"), "a", encoding="utf-8", newline="\n") as f:
            f.write("0 4 * * * yiban /usr/local/sbin/ghost-not-in-manifest.sh\n")
        r = self._check(d)
        self.assertNotEqual(r.returncode, 0,
                            "无来源路径未被打红——来源断言门形同虚设")
        self.assertIn("/usr/local/sbin/ghost-not-in-manifest.sh", self._out(r))

    def test_repo_relative_app_paths_must_exist(self):
        """/opt/yiban-auto-sign/<rel> 型路径：rel 在仓库不存在 ⇒ 非 0。"""
        d = os.path.join(self.tmp, "cron.d")
        os.makedirs(d)
        _write(os.path.join(d, "yiban-x"),
               "5 5 * * * yiban /opt/yiban-auto-sign/no-such-script.sh\n")
        r = self._check(d)
        self.assertNotEqual(r.returncode, 0, self._out(r))
        self.assertIn("no-such-script.sh", self._out(r))


# ------------------------------------------------------------------② wrapper 单跳
@unittest.skipIf(BASH is None, "需要 bash")
class WrapperPassphraseTest(_TmpBase):
    def setUp(self):
        super().setUp()
        self.stub = os.path.join(self.tmp, "stub-backup.sh")
        _write(self.stub, STUB_BACKUP, 0o755)
        self.pp_file = os.path.join(self.tmp, "passphrase")
        _write(self.pp_file, SYNTH_PASSPHRASE + "\n", 0o600)
        self.rec_env = os.path.join(self.tmp, "env.txt")
        self.rec_stdin = os.path.join(self.tmp, "stdin.txt")
        self.rec_args = os.path.join(self.tmp, "args.txt")

    def _run_wrapper(self, wrapper, env_extra=None):
        e = {
            "YIBAN_BACKUP_SCRIPT": self.stub,
            "YIBAN_BACKUP_PASSPHRASE_FILE": self.pp_file,
            "STUB_ENV_FILE": self.rec_env,
            "STUB_STDIN_FILE": self.rec_stdin,
            "STUB_ARGS_FILE": self.rec_args,
        }
        e.update(env_extra or {})
        return self._run_bash(wrapper, (), e)

    def _assert_child_env_clean(self):
        """对 stub 记录的真实子进程环境做判据——不是对源码文本。"""
        pairs = _parse_env_record(self.rec_env)
        leaked = PASSPHRASE_ENV_KEYS & set(pairs)
        self.assertEqual(set(), leaked,
                         f"口令键泄漏进子进程环境：{sorted(leaked)}")
        for k, v in pairs.items():
            self.assertNotIn(SYNTH_PASSPHRASE, v, f"口令值出现在子进程环境变量 {k} 里")

    def test_wrapper_child_env_has_no_passphrase_and_stdin_carries_it(self):
        r = self._run_wrapper(WRAPPER)
        self.assertEqual(r.returncode, 0, self._out(r))
        self._assert_child_env_clean()
        with io.open(self.rec_stdin, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), SYNTH_PASSPHRASE,
                             "口令必须经 stdin（fd 0）单跳到达 backup.sh")
        # 单跳标记本身不是秘密，允许出现在环境里
        self.assertIn("YIBAN_READ_PASSPHRASE_STDIN", _parse_env_record(self.rec_env))
        with io.open(self.rec_args, encoding="utf-8") as f:
            self.assertIn("--require-encrypt", f.read().splitlines(),
                          "cron 入口必须固定带 --require-encrypt")

    def test_wrapper_propagates_backup_rc(self):
        r = self._run_wrapper(WRAPPER, {"STUB_EXIT": "5"})
        self.assertEqual(r.returncode, 5, f"backup.sh 退出码必须原样传播：{self._out(r)}")

    def test_wrapper_strips_legacy_env_injection(self):
        """现网 crontab 若还残留 BACKUP_GPG_PASSPHRASE=... 前缀，wrapper 必须先 unset。"""
        r = self._run_wrapper(WRAPPER,
                              {"BACKUP_GPG_PASSPHRASE": "legacy-env-injection-value"})
        self.assertEqual(r.returncode, 0, self._out(r))
        self._assert_child_env_clean()

    def test_legacy_export_wrapper_goes_red_under_same_checker(self):
        """活体反例：把旧 export 形态喂给同一检查器 ⇒ 必须判脏。"""
        legacy = os.path.join(self.tmp, "legacy-wrapper.sh")
        _write(legacy, LEGACY_EXPORT_WRAPPER, 0o755)
        r = self._run_wrapper(legacy)
        pairs = _parse_env_record(self.rec_env)
        self.assertIn("BACKUP_GPG_PASSPHRASE", pairs,
                      f"检查器抓不住旧 export 形态（rc={r.returncode}），门是假的")
        self.assertEqual(pairs["BACKUP_GPG_PASSPHRASE"], SYNTH_PASSPHRASE)

    def test_wrapper_refuses_group_readable_passphrase_file(self):
        mode_probe = subprocess.run(
            [BASH, "-c", 'f=$(mktemp); chmod 600 "$f"; stat -c %a "$f"; rm -f "$f"'],
            capture_output=True, text=True, timeout=60)
        if mode_probe.stdout.strip() != "600":
            self.skipTest("宿主文件系统不保留 POSIX mode（Windows 开发机）")
        _write(self.pp_file, SYNTH_PASSPHRASE + "\n", 0o644)
        r = self._run_wrapper(WRAPPER)
        self.assertNotEqual(r.returncode, 0, "口令文件组可读却照跑 ⇒ 护栏失效")
        self.assertIn("passphrase", (self._out(r) + self._out(r)).lower())

    def test_wrapper_real_backup_gpg_roundtrip(self):
        """正例加深：wrapper→真 backup.sh→真 gpg 单跳闭环（缺 gpg/sqlite3 则降级断言）。"""
        if shutil.which("gpg") is None:
            self.skipTest("需要系统 gpg")
        app = os.path.join(self.tmp, "app")
        backups = os.path.join(self.tmp, "backups")
        os.makedirs(app)
        os.makedirs(backups)
        _write(os.path.join(app, ".env"),
               "YIBAN_ACCOUNTS_KEY=" + "f" * 64 + "\n"
               "ADMIN_USER=e2e-admin\nADMIN_PASSWORD_HASH=e2e-no-plain-credential\n")
        pp = os.path.join(self.tmp, "pp")
        _write(pp, SYNTH_PASSPHRASE + "\n", 0o600)
        e = {
            "YIBAN_BACKUP_SCRIPT": BACKUP_SH,
            "YIBAN_BACKUP_PASSPHRASE_FILE": pp,
            "APP_DIR": app,
            "BACKUP_DIR": backups,
            "KEY_FILE": os.path.join(self.tmp, "no-accounts-key"),
            "YIBAN_STATE_DIR": os.path.join(self.tmp, "state"),
            "YIBAN_LOG_FILE": os.path.join(self.tmp, "logs", "sign.log"),
        }
        r = self._run_bash(WRAPPER, (), e)
        out = self._out(r)
        self.assertIn(r.returncode, (0, 5),
                      f"真加密轮应 0（无 sqlite3 降级为 5）：{out}")
        gpg_art = [n for n in os.listdir(backups) if n.endswith(".tar.gz.gpg")]
        self.assertEqual(1, len(gpg_art), f"应产出唯一密文归档：{out}")
        plain = [n for n in os.listdir(backups) if n.endswith(".tar.gz")]
        self.assertEqual([], plain, "可解才删明文——明文不得留存")
        dec = subprocess.run(
            [BASH, "-c", 'gpg --batch --yes --decrypt --passphrase-fd 0 -o "$1.out" "$1"',
             "_", os.path.join(backups, gpg_art[0])],
            input=(SYNTH_PASSPHRASE + "\n").encode(), capture_output=True, timeout=120)
        self.assertEqual(dec.returncode, 0,
                         f"归档必须能用注入 stdin 的同一口令解开：{dec.stderr}")
        wrong = subprocess.run(
            [BASH, "-c", 'gpg --batch --yes --decrypt --passphrase-fd 0 -o /dev/null "$1"',
             "_", os.path.join(backups, gpg_art[0])],
            input=b"e2e-wrong-passphrase\n", capture_output=True, timeout=120)
        self.assertNotEqual(wrong.returncode, 0, "错口令竟能解 ⇒ 加密没吃到口令")


# --------------------------------------------------------------------③ install.sh
@unittest.skipIf(BASH is None, "需要 bash")
class InstallScriptTest(_TmpBase):
    def setUp(self):
        super().setUp()
        self.destroot = os.path.join(self.tmp, "destdir")
        os.makedirs(self.destroot)

    def _install(self, extra_args=(), destroot=None):
        return self._run_bash(INSTALL, extra_args,
                              {"DESTDIR": destroot or self.destroot})

    def _manifest_rows(self):
        rows = []
        with io.open(MANIFEST, encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line or line.startswith("#"):
                    continue
                mode, src, dest = line.split("\t")
                rows.append((mode, src, dest))
        self.assertGreaterEqual(len(rows), 5)
        return rows

    def _dest(self, dest):
        return os.path.join(self.destroot, dest.lstrip("/").replace("/", os.sep))

    def test_fresh_install_places_all_manifest_entries_with_modes(self):
        r = self._install()
        out = self._out(r)
        self.assertEqual(r.returncode, 0, out)
        for mode, src, dest in self._manifest_rows():
            d = self._dest(dest)
            self.assertTrue(os.path.isfile(d), f"未安装 {dest}：{out}")
            with open(d, "rb") as a, open(os.path.join(BASE, src.replace("/", os.sep)), "rb") as b:
                self.assertEqual(a.read(), b.read(), f"{dest} 内容与仓库源不一致")
            m = stat.S_IMODE(os.stat(d).st_mode)
            self.assertEqual(m, int(mode, 8), f"{dest} 权限 {oct(m)} ≠ {mode}")
        import re
        self.assertGreaterEqual(len(re.findall(r"\b[0-9a-f]{64}\b", out)),
                                len(self._manifest_rows()),
                                f"安装后校验和输出缺失：{out}")

    def test_refuses_checksum_mismatch_without_flag(self):
        """现网 3 行手工漂移：不得被静默覆盖（仓库=唯一来源，但先对峙再裁决）。"""
        entry = next((row for row in self._manifest_rows() if row[2].endswith("yiban-backup.sh")),
                     None)
        self.assertIsNotNone(entry, "manifest 必须收编 /usr/local/sbin/yiban-backup.sh")
        d = self._dest(entry[2])
        os.makedirs(os.path.dirname(d), exist_ok=True)
        _write(d, "#!/bin/bash\n# drifted hand-copy line1\n# line2\n")
        r = self._install()
        out = self._out(r)
        self.assertNotEqual(r.returncode, 0, f"校验和不符却放行安装：{out}")
        self.assertIn("checksum", (out + "").lower())
        with open(d, encoding="utf-8") as f:
            self.assertIn("drifted", f.read(), "拒装时现网文件必须原样保留")

    def test_adopt_production_installs_repo_and_archives_drift(self):
        entry = next(row for row in self._manifest_rows() if row[2].endswith("yiban-backup.sh"))
        d = self._dest(entry[2])
        os.makedirs(os.path.dirname(d), exist_ok=True)
        _write(d, "#!/bin/bash\n# drifted hand-copy\n")
        # 现场残留（现象原文：.bak-20260923 旧件）
        residue = d + ".bak-20260923"
        _write(residue, "# old piece\n")
        r = self._install(("--adopt-production",))
        out = self._out(r)
        self.assertEqual(r.returncode, 0, out)
        with open(d, "rb") as a, open(BACKUP_SH, "rb") as b:
            self.assertEqual(a.read(), b.read(), "adopt 后仓库版本必须落位")
        self.assertFalse(os.path.exists(residue), f".bak-* 残留未清理：{out}")
        self.assertIn(".bak-20260923", out, "清理动作必须留痕")
        archives = [n for n in os.listdir(os.path.dirname(d)) if ".reconcile-" in n]
        self.assertEqual(1, len(archives), f"漂移件必须归档留存（*.reconcile-时间戳）：{out}")
        with open(os.path.join(os.path.dirname(d), archives[0]), encoding="utf-8") as f:
            self.assertIn("drifted", f.read(), "归档内容必须是原现网漂移件")

    def test_idempotent_second_run_changes_nothing(self):
        self.assertEqual(self._install().returncode, 0)
        r = self._install()
        self.assertEqual(r.returncode, 0, self._out(r))
        for _, _, dest in self._manifest_rows():
            d = self._dest(dest)
            self.assertEqual([], [n for n in os.listdir(os.path.dirname(d))
                                  if ".reconcile-" in n],
                             "无漂移的第二轮不应产生归档")


# ----------------------------------------------------------④ check-deploy-target.sh
@unittest.skipIf(BASH is None, "需要 bash")
class CheckDeployTargetTest(_TmpBase):
    def setUp(self):
        super().setUp()
        self.git = shutil.which("git")
        if self.git is None:
            self.skipTest("需要 git")
        self.origin = os.path.join(self.tmp, "origin.git")
        self.work = os.path.join(self.tmp, "work")
        self._git(["init", "--bare", "-b", "main", self.origin])
        self._git(["clone", "-q", self.origin, self.work])
        self._commit_in_work("first")
        self._git(["-C", self.work, "push", "-q", "origin", "main"])
        self.sha_on_remote = self._git(["-C", self.work, "rev-parse", "HEAD"]).stdout.strip()
        self._commit_in_work("local-only")
        self.sha_local_only = self._git(["-C", self.work, "rev-parse", "HEAD"]).stdout.strip()

    def _git(self, args):
        r = subprocess.run([self.git, *args], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, f"git {' '.join(args)}: {r.stderr}")
        return r

    def _commit_in_work(self, tag):
        p = os.path.join(self.work, f"f-{tag}.txt")
        _write(p, tag + "\n")
        self._git(["-C", self.work, "add", f"f-{tag}.txt"])
        self._git(["-C", self.work, "-c", "user.email=t@t", "-c", "user.name=t",
                   "commit", "-qm", tag])

    def _check(self, sha, remote="origin", branch="main"):
        return self._run_bash(CHECK_TARGET, (remote, branch, sha), cwd=self.work)

    def test_target_on_remote_branch_is_reachable(self):
        r = self._check(self.sha_on_remote)
        self.assertEqual(r.returncode, 0, self._out(r))
        out = self._out(r)
        self.assertIn(self.sha_on_remote[:7], out)

    def test_target_missing_on_remote_goes_red(self):
        """活体反例：整改前现状（gitee 停在旧提交 ⇒ 部署不可达）必须红。"""
        r = self._check(self.sha_local_only)
        self.assertNotEqual(r.returncode, 0, "目标提交不在远端却报可达 ⇒ 断言是假的")
        out = self._out(r)
        self.assertIn("不可达", out)

    def test_unknown_remote_or_branch_fails_with_readable_line(self):
        # 用不存在的路径型 remote（绝不触发网络/ssh 交互）
        r = self._check(self.sha_on_remote, remote=os.path.join(self.tmp, "missing.git"))
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue(self._out(r).strip(), "失败必须给人话结论，不能静默")


# ------------------------------------------------- ⑥ deploy/prod 文本件尾换行门
class DeployTextNewlineGateTest(unittest.TestCase):
    """deploy/prod 下每个文本文件必须以换行结尾（cron 表丢末行 = 缓解整体空转）。

    现象原文：审计见证的 cron 表与脚本缺结尾 0x0a。Vixie cron 对无结尾换行的
    /etc/cron.d 表可能丢掉最后一行 ⇒ 根侧见证从不运行，独立见证空转。门覆盖
    deploy/prod 全部文本件，并用现有 cron 表作为活体夹具。
    """

    def test_all_deploy_prod_text_files_end_with_newline(self):
        """deploy/prod 下每个文本件都以换行结尾（cron 表丢末行 = 缓解整体空转）。"""
        offenders = []
        for root, _dirs, files in os.walk(PROD_DIR):
            for name in files:
                p = os.path.join(root, name)
                if _missing_final_newline(p):
                    offenders.append(os.path.relpath(p, BASE).replace(os.sep, "/"))
        self.assertEqual([], offenders, f"以下 deploy/prod 文本件缺结尾换行：{offenders}")

    def test_cron_tables_end_with_newline_as_live_fixtures(self):
        """/etc/cron.d 的每张表都必须换行结尾（Vixie cron 可能丢末行）。"""
        tables = sorted(n for n in os.listdir(CRON_DIR)
                        if os.path.isfile(os.path.join(CRON_DIR, n)))
        self.assertGreaterEqual(len(tables), 3, "cron.d 下应至少有三张表")
        for name in tables:
            p = os.path.join(CRON_DIR, name)
            self.assertFalse(_missing_final_newline(p),
                             f"cron 表缺结尾换行（末行可能被丢弃）: {name}")

    def test_gate_catches_stripped_newline(self):
        """活体反例：去掉 cron 表的结尾换行，同一检查器必须判脏。"""
        d = tempfile.mkdtemp(prefix="newline-gate-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        src = os.path.join(CRON_DIR, "yiban-audit-witness")
        with open(src, "rb") as f:
            data = f.read()
        stripped = os.path.join(d, "stripped")
        with open(stripped, "wb") as f:
            f.write(data.rstrip(b"\n"))
        self.assertTrue(_missing_final_newline(stripped),
                        "扫描器抓不住缺结尾换行，门是假的")


# -------------------------------------------------------------------⑤ 真名示例门
class RealNameGateTest(unittest.TestCase):
    def test_real_name_never_appears_in_tracked_files(self):
        hits = _tracked_hit_scan()
        self.assertEqual([], hits,
                         "真实姓名示例不得出现在任何 git 跟踪文件（MF-42②/待裁决 #6②）")

    def test_scanner_detects_planted_name(self):
        """活体反例：扫描器抓得住 planted 文件（门本身可失败）。"""
        d = tempfile.mkdtemp(prefix="name-gate-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        p = os.path.join(d, "planted.js")
        _write(p, "// 示例：" + "名" + REAL_NAME + "\n")
        self.assertTrue(_scan_file_for_name(p))


if __name__ == "__main__":
    unittest.main()
