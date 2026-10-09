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
    ③ install.sh：DESTDIR 暂存安装到 fixture 前缀（装的是从 git 名册复制出来的
      合规检出）；安装前后校验和输出；已存在文件校验和不符 ⇒ 拒装（现网 3 行手工
      漂移不得被静默覆盖），--adopt-production 归档现网件后以仓库为准；`.bak-*`
      残留清理并记录；以 root 安装时**无论是否带 DESTDIR** 检出都必须属 root 且
      非组/其他可写，否则在执行检出内脚本之前拒装（M01）；该门**逐级**校验被 root
      读取/执行的每一条路径（顶层合规而 `scripts/` 可写同样拒装——复审点名的绕过）。
      两道门的判据差异另钉三条：root + DESTDIR 拒装、非 root + DESTDIR 照装、
      root + DESTDIR 仍跳过 root:root 属主设置（属主门管写入侧，不看执行来源）。
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
扫描、本地裸仓 fixture 与 ③ 的暂存检出名册）；无特权位格用 setpriv（缺它则该条 skip）；
无网络、不 push。

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
SETPRIV = shutil.which("setpriv")
# WSL Ubuntu 的 nobody/nogroup 位格；只借它的 uid/gid，不碰任何账号数据。
UNPRIVILEGED_ID = 65534

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


def _current_uid():
    """跑测进程的有效 uid（读不到 ⇒ -1）。"""
    r = subprocess.run([BASH, "-c", "id -u"], capture_output=True, timeout=60)
    return int(((r.stdout or b"").decode().strip() or "-1"))


def _unprivileged_prefix():
    """造「非 root 位格」的 argv 前缀。

    ()   ⇒ 当前进程已不是 root，直接跑就是无特权跑法。
    None ⇒ 当前是 root 但宿主没有 setpriv ⇒ 造不出该位格，用例须 skip。
    其余 ⇒ 用 setpriv 降到 nobody 的 uid/gid 并清掉附加组。
    """
    if _current_uid() != 0:
        return []
    if SETPRIV is None:
        return None
    return [SETPRIV, "--reuid=%d" % UNPRIVILEGED_ID,
            "--regid=%d" % UNPRIVILEGED_ID, "--clear-groups"]


def _stage_tracked_checkout(dest_root):
    """按 git 名册复制出一份检出，权限收成 root:755/644。

    为什么要它：M01 门只看 uid。root 执行检出内文件时，被读取或执行的每条路径
    都要属 root 且非组/其他可写。跑测副本由 dev-verify 用 `rsync -a` 从 DrvFs
    生成，全树 0777、属主 uid=0。以 root 直跑暂存安装会被那道门拒掉——那是门在
    正常工作，不是被测件的缺陷。本函数造出的检出等价于生产里执行过
    `chown -R root:root && chmod -R go-w` 的那一份，用例于是照旧断言装出来的
    内容与权限。名册取自 `git ls-files`，所以检出永不会缺件。
    """
    os.makedirs(dest_root, exist_ok=True)
    for raw in _ls_tracked().split(b"\0"):
        if not raw:
            continue
        parts = os.fsdecode(raw).split("/")
        src = os.path.join(BASE, *parts)
        if not os.path.isfile(src):
            continue
        dst = os.path.join(dest_root, *parts)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
        os.chmod(dst, 0o644)
    os.chmod(dest_root, 0o755)
    for root, dirs, _files in os.walk(dest_root):
        for name in dirs:
            os.chmod(os.path.join(root, name), 0o755)
    return dest_root


# 2026-10-02 用户裁定：门禁真名串（见上方 REAL_NAME）为游戏角色名，曾作为
# account-form.js 的示例保留在仓库内（该文件因此被列入豁免）。2026-10-03 账号管理页迁到
# Vue，该 legacy 文件退役；实测全仓已无该真名字面命中——故豁免清单清空，门禁继续对全部
# 跟踪文件生效。（注：accountform.ts 的占位文案「电力123示例站」与门禁真名串无关，不构成
# 命中，勿据此恢复豁免。）
_AUTHORIZED_NAME_PATHS = frozenset()


def _tracked_hit_scan():
    """扫描全部 git 跟踪文件，返回含真名的路径列表（授权豁免路径除外）。"""
    hits = []
    for raw in _ls_tracked().split(b"\0"):
        if not raw:
            continue
        rel = os.fsdecode(raw)
        if rel in _AUTHORIZED_NAME_PATHS:
            continue
        p = os.path.join(BASE, rel)
        try:
            if not os.path.isfile(p) or os.path.getsize(p) > 8 * 1024 * 1024:
                continue
            if _scan_file_for_name(p):
                hits.append(rel)
        except OSError:
            continue
    return hits


@unittest.skipIf(BASH is None, "需要 bash（Git Bash/WSL）")
class _TmpBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="deploy-prod-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env = _clean_subprocess_env()

    def _run_bash(self, script, args=(), env=None, cwd=BASE, prefix=()):
        e = dict(self.env)
        e.update(env or {})
        return subprocess.run([*prefix, BASH, script, *args], capture_output=True,
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
    """DESTDIR 暂存安装的语义（清单落位 / 校验和对峙 / adopt 归档 / 幂等）。

    安装对象是 `_stage_tracked_checkout()` 复制出来的合规检出：M01 门只看 uid，
    跑测时进程就是 root，工作树副本 0777 会被那道门拒装（门在正常工作）。断言的
    内容基准仍取仓库原件，故暂存件与仓库不一致同样会红。
    """

    def setUp(self):
        super().setUp()
        if shutil.which("git") is None:
            self.skipTest("需要 git 取暂存检出的名册")
        self.destroot = os.path.join(self.tmp, "destdir")
        os.makedirs(self.destroot)
        self.checkout = _stage_tracked_checkout(os.path.join(self.tmp, "checkout"))
        self.installer = os.path.join(self.checkout, "deploy", "prod", "install.sh")

    def _install(self, extra_args=(), destroot=None):
        return self._run_bash(self.installer, extra_args,
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


# -------------------------------------------- install.sh 以 root 安装的前置门（M01）
@unittest.skipIf(BASH is None, "需要 bash")
class InstallRootCheckoutGateTest(_TmpBase):
    """M01：以 root 安装时（与 DESTDIR 无关），检出必须属 root 且组/其他不可写。

    威胁：install.sh 以 root 执行检出内的 `scripts/check-cron-provenance.sh` 并把检出件
    root:root 安装；生产里 `/opt/yiban-auto-sign` 对服务账号 yiban 可写
    （web/deploy/yiban-web.service 的 ReadWritePaths），服务账号被拿下后等一次 sudo
    安装即提权 root。门必须**先于**任何检出内脚本的执行（用 stub + marker 证明）。
    清单目标指向 /tmp 下的合成路径（万一门失效也只碰临时区，不碰生产路径）。
    """

    def setUp(self):
        super().setUp()
        # 最小检出：install.sh 只看 manifest 的源是否存在 + 跑 cron 来源断言
        self.checkout = os.path.join(self.tmp, "checkout")
        os.makedirs(os.path.join(self.checkout, "scripts"))
        os.makedirs(os.path.join(self.checkout, "deploy", "prod"))
        shutil.copy2(INSTALL, os.path.join(self.checkout, "deploy", "prod", "install.sh"))
        self.dest = "/tmp/yiban-m01-gate-%s/payload.sh" % os.path.basename(self.tmp)
        _write(os.path.join(self.checkout, "deploy", "prod", "manifest.tsv"),
               "0700\tscripts/payload.sh\t%s\n" % self.dest)
        _write(os.path.join(self.checkout, "scripts", "payload.sh"), "#!/bin/bash\ntrue\n")
        # cron 来源断言桩：被 root 执行即留痕（marker 就是"检出内脚本被执行过"的证据）
        self.marker = os.path.join(self.checkout, "provenance-ran")
        _write(os.path.join(self.checkout, "scripts", "check-cron-provenance.sh"),
               '#!/usr/bin/env bash\necho ran >> "%s"\nexit 0\n' % self.marker)

    def _install_from_checkout(self, destroot=None, extra_env=None, prefix=()):
        env = {}
        if destroot is not None:
            env["DESTDIR"] = destroot
        env.update(extra_env or {})
        return self._run_bash(os.path.join(self.checkout, "deploy", "prod", "install.sh"),
                              (), env=env, cwd=self.checkout, prefix=prefix)

    def _running_as_root(self):
        return _current_uid() == 0

    def _fs_preserves_modes(self):
        r = subprocess.run(
            [BASH, "-c", 'f=$(mktemp); chmod 775 "$f"; stat -c %a "$f"; rm -f "$f"'],
            capture_output=True, text=True, timeout=60)
        return r.stdout.strip() == "775"

    def test_group_writable_checkout_under_root_is_refused_before_any_script(self):
        """活体反例：chmod g+w 的检出上以 root 跑 ⇒ rc=1，且未执行检出内脚本。"""
        if not self._running_as_root():
            self.skipTest("需要以 root 运行（WSL）才能触发 M01 前置门")
        if not self._fs_preserves_modes():
            self.skipTest("宿主文件系统不保留 POSIX mode（Windows 开发机）")
        os.chmod(self.checkout, 0o775)  # 服务账号组可写 = 生产旧形态
        r = self._install_from_checkout()
        out = self._out(r)
        self.assertNotEqual(r.returncode, 0, f"组可写检出以 root 安装必须拒装：{out}")
        self.assertIn("检出必须属 root", out)
        self.assertFalse(os.path.exists(self.marker),
                         "门必须在执行检出内脚本之前拦下（check-cron-provenance 不得被 root 跑）")

    def test_root_readonly_checkout_with_destdir_installs_normally(self):
        """对照：root 属主、非组/其他可写的检出 + DESTDIR ⇒ 照常装进暂存前缀。"""
        if not self._running_as_root() or not self._fs_preserves_modes():
            self.skipTest("需要 root（WSL）+ 保留 POSIX mode 的文件系统")
        os.chmod(self.checkout, 0o755)
        destroot = os.path.join(self.tmp, "destdir")
        os.makedirs(destroot, exist_ok=True)
        r = self._install_from_checkout(destroot=destroot)
        out = self._out(r)
        self.assertEqual(r.returncode, 0, out)
        self.assertTrue(os.path.isfile(
            destroot + self.dest.replace("/", os.sep)), out)
        self.assertTrue(os.path.exists(self.marker), "DESTDIR 路径必须照旧跑 cron 来源断言")


class InstallRootCheckoutSubPathGateTest(InstallRootCheckoutGateTest):
    """M01 复审补全：门必须**逐级**校验，不止看检出顶层一个目录。

    活体反例形态（就是复审点名的绕过）：顶层 `root:755` 完全合规，但 `scripts/`
    组可写 —— 而 `scripts/check-cron-provenance.sh` 正是被 root 执行的那一份。
    只 stat 顶层的旧门在这里会放行，提权路径照旧。
    继承父类夹具：同样的最小检出、同样的 marker 语义（marker 存在 = 检出内脚本
    已被 root 跑过）。注意父类的两条既有用例在本子类里会**原样重跑**一遍——那是
    有意的对照：逐级门不得把"顶层合规 + DESTDIR"的正常路径也拒掉。

    ba-p12-02 追加三条，钉住**门的作用域**与**两道门的判据差异**：
      · 威胁是"root 执行检出内文件"，与装到哪里无关 ⇒ `DESTDIR` 不再豁免 M01 门；
      · 非 root + `DESTDIR` 是受支持的无特权暂存用法 ⇒ 不得被 M01 门挡住；
      · 属主设置门（`install.sh` 第二道门）的判据是**写入侧**行为 ⇒ `DESTDIR`
        暂存必须继续跳过它，两条门不许被"统一"成同一个条件。
    """

    def _require_root_and_modes(self):
        if not self._running_as_root():
            self.skipTest("需要以 root 运行（WSL）才能触发 M01 前置门")
        if not self._fs_preserves_modes():
            self.skipTest("宿主文件系统不保留 POSIX mode（Windows 开发机）")

    def test_group_writable_subdirectory_is_refused_even_when_toplevel_is_clean(self):
        """顶层 root:755 + scripts/ 组可写 ⇒ 必须在执行检出内脚本之前拒装。"""
        self._require_root_and_modes()
        os.chmod(self.checkout, 0o755)          # 顶层完全合规（旧门在这里就放行）
        os.chmod(os.path.join(self.checkout, "scripts"), 0o775)
        r = self._install_from_checkout()
        out = self._out(r)
        self.assertNotEqual(r.returncode, 0, f"scripts/ 组可写必须拒装：{out}")
        self.assertIn("每个被 root 读取/执行的路径", out)
        self.assertFalse(os.path.exists(self.marker),
                         "门必须在执行检出内脚本之前拦下（check-cron-provenance 不得被 root 跑）")

    def test_group_writable_manifest_source_file_is_refused(self):
        """清单里的**源件**本身可写同样拒装（install 以 root 读它并落位）。"""
        self._require_root_and_modes()
        os.chmod(self.checkout, 0o755)
        os.chmod(os.path.join(self.checkout, "scripts", "payload.sh"), 0o666)
        r = self._install_from_checkout()
        out = self._out(r)
        self.assertNotEqual(r.returncode, 0, f"清单源件组/其他可写必须拒装：{out}")
        self.assertIn("payload.sh", out)
        self.assertFalse(os.path.exists(self.marker),
                         "门必须在执行检出内脚本之前拦下")

    def test_destdir_does_not_exempt_root_from_the_provenance_gate(self):
        """活体反例（ba-p12-02）：root + DESTDIR 时 M01 门必须照样拦。

        判据与写入目标无关：被 root 执行的是**检出内**的
        `scripts/check-cron-provenance.sh`，DESTDIR 只改安装落点。顶层合规、
        `scripts/` 组可写 ⇒ 必须在执行那枚脚本**之前**拒装。
        """
        self._require_root_and_modes()
        os.chmod(self.checkout, 0o755)          # 顶层合规（旧门在这里就放行）
        os.chmod(os.path.join(self.checkout, "scripts"), 0o775)  # 篡改面
        destroot = os.path.join(self.tmp, "destdir")
        os.makedirs(destroot, exist_ok=True)
        r = self._install_from_checkout(destroot=destroot)
        out = self._out(r)
        self.assertNotEqual(r.returncode, 0,
                            f"root + DESTDIR 竟跳过 M01 门：{out}")
        self.assertIn("每个被 root 读取/执行的路径", out,
                      f"拒绝原因必须来自 M01 门，不是别的检查：{out}")
        self.assertFalse(os.path.exists(self.marker),
                         "门必须在 root 执行 check-cron-provenance.sh 之前拦下")
        self.assertFalse(os.path.isdir(os.path.join(destroot, "tmp")),
                         "拒装不得往暂存前缀里写任何东西")

    def test_drift_list_is_still_reported_when_the_m01_gate_refuses(self):
        """M01 拒装时，执行件漂移清单**仍必须打印**（工单 yiban-auto-sign-ybg8）。

        生产实测：M01 在检出属主不合时 `exit 1`，而漂移对账原先排在它**之后** ⇒ 现场只
        看到归属报错，看不到"有几个执行件已与仓库不符"。一个哨兵脚本缺失、一份旧版
        wrapper（口令经 `export` 进子进程树）就这么共存两周无人发现——**检测器被门自己
        挡在门外**。

        本门钉住顺序：门拒装**的同时**，输出里必须有每个不符目标的 checksum 行。
        反例（把只读收集段挪回 M01 之后即红）：`checksum mismatch` 断言消失。
        提前收集只读、零改动，故另断言 M01 仍在执行检出内脚本**之前**拦下。
        """
        self._require_root_and_modes()
        os.chmod(self.checkout, 0o755)                          # 顶层合规
        os.chmod(os.path.join(self.checkout, "scripts"), 0o775)  # 让 M01 拒装
        destroot = os.path.join(self.tmp, "destdir")
        destpath = destroot + self.dest.replace("/", os.sep)
        os.makedirs(os.path.dirname(destpath), exist_ok=True)
        _write(destpath, "#!/bin/bash\n# 现网手工漂移件（模拟哨兵缺失这类）\n")
        r = self._install_from_checkout(destroot=destroot)
        out = self._out(r)
        self.assertNotEqual(r.returncode, 0, f"组可写检出必须拒装：{out}")
        self.assertIn("每个被 root 读取/执行的路径", out,
                      f"拒绝原因必须来自 M01 门，不是别的检查：{out}")
        self.assertIn("checksum mismatch", out,
                      f"M01 拒装时漂移清单仍须打印（检测器不得被门挡住）：{out}")
        self.assertIn(self.dest, out, f"漂移行要点名目标路径：{out}")
        self.assertFalse(os.path.exists(self.marker),
                         "门必须在执行检出内脚本之前拦下")

    def test_unprivileged_staging_install_with_destdir_is_not_blocked(self):
        """非 root + DESTDIR = 头注释与 README 承诺的无特权暂存用法 ⇒ 门不得挡它。

        这条挡住反向改法：把两道门"统一"成只看 DESTDIR（或干脆无条件跑门）会把
        受支持的暂存用法一起废掉。检出组/其他可写在非 root 位格下不构成提权路径。
        """
        prefix = _unprivileged_prefix()
        if prefix is None:
            self.skipTest("当前是 root 而宿主没有 setpriv：造不出非 root 位格")
        if not self._fs_preserves_modes():
            self.skipTest("宿主文件系统不保留 POSIX mode（Windows 开发机）")
        if prefix:  # 确认降权真的生效，位格不是假的
            probe = subprocess.run([*prefix, BASH, "-c", "id -u"],
                                   capture_output=True, timeout=60)
            self.assertNotEqual(probe.stdout.decode().strip(), "0",
                                f"setpriv 没降下权来：{probe.stdout!r}")
        os.chmod(self.tmp, 0o755)  # nobody 要能穿进本用例的临时区
        os.chmod(self.checkout, 0o777)
        os.chmod(os.path.join(self.checkout, "scripts"), 0o777)
        os.chmod(os.path.join(self.checkout, "deploy", "prod"), 0o777)
        destroot = os.path.join(self.tmp, "destdir")
        os.makedirs(destroot, exist_ok=True)
        os.chmod(destroot, 0o777)
        r = self._install_from_checkout(destroot=destroot, prefix=prefix)
        out = self._out(r)
        self.assertEqual(r.returncode, 0,
                         f"无特权暂存安装被挡 ⇒ M01 门的作用域写过头了：{out}")
        self.assertNotIn("检出必须属 root", out)
        self.assertTrue(os.path.exists(self.marker), "cron 来源断言照旧要跑")
        self.assertTrue(os.path.isfile(destroot + self.dest.replace("/", os.sep)), out)

    def test_root_staging_install_does_not_force_root_ownership(self):
        """两道门判据不同（ba-p12-02 的边界）：属主设置门仍要看 DESTDIR。

        暂存前缀带 setgid + 组 nobody：不显式 chgrp 时产物继承该组。若有人把属主门
        里的 `-z "$DESTDIR"` 也删掉，root + DESTDIR 会走 `install -o root -g root`
        ⇒ 产物组变成 0、跳过提示消失，两条断言同时红。
        """
        self._require_root_and_modes()
        os.chmod(self.checkout, 0o755)  # 合规检出 ⇒ M01 门放行
        staging = os.path.join(self.tmp, "staging")
        os.makedirs(staging)
        os.chown(staging, 0, UNPRIVILEGED_ID)
        os.chmod(staging, 0o2777)
        r = self._install_from_checkout(destroot=staging)
        out = self._out(r)
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("跳过 root:root 属主设置", out,
                      "root + DESTDIR 必须继续跳过属主设置（那是写入侧的不变量）")
        payload = staging + self.dest.replace("/", os.sep)
        self.assertTrue(os.path.isfile(payload), out)
        self.assertEqual(os.stat(payload).st_gid, UNPRIVILEGED_ID,
                         "暂存件被显式 chgrp 成 root ⇒ 属主门吃掉了 DESTDIR 豁免")


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

    现象原文：cron 表与脚本缺结尾 0x0a。Vixie cron 对无结尾换行的
    /etc/cron.d 表可能丢掉最后一行 ⇒ 排期任务空转。门覆盖
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
        src = os.path.join(CRON_DIR, "yiban-cleanup")
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
