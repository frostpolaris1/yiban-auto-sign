# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""backup.sh 的活体端到端契约（M3 批次0：MF-76 / MF-79 / MF-80 / MF-77 / MF-10）。

标签：J · 运维：部署/备份/发布
覆盖：以真实 bash 子进程跑 `scripts/backup.sh`（gpg/tar/find 按需注入故障桩），钉四条
    验收不变量：
    ① 可解才删明文（MF-76）——加密产物必须过"解密→解包→integrity"回环才允许删除
      明文与出清单；回环失败 ⇒ 明文仍在、无清单、rc=7；加密整体失败 ⇒ 明文仍在、
      rc=6（不再是静默 0）。正例用真实 gpg 加密 + 真实 `--restore` 闭环。
    ② 明文模式护栏（MF-79）——BACKUP_PLAINTEXT=1 ⇒ stderr 大字告警 + 专用退出码 6
      + 备份目录 chmod 0700；且产物**不被哨兵计为健康**。
    ③ tar 护栏判码（MF-80）——tar 列目录失败时护栏必须执行并让脚本失败（旧实现
      pipefail 下三条护栏整体被跳过）；符号链接/路径穿越/设备节点逐类真包反证。
    ④ 轮转下界（MF-77）——RETENTION_DAYS<1 或非数字 ⇒ 拒绝执行；按文件名日期保留
      最近 K 组地板；明文包过期从紧（2 天）；轮转误删当日包 ⇒ rc=8。
对应实现：`scripts/backup.sh`（纯 shell）、`scripts/backup_sentinel.py`（②的哨兵断言）。
关键断言：全部是**行为断言**——执行真脚本、检查真产物、读真退出码——不 grep backup.sh
源码（源码文本级断言正是"假绿"教训：改了脚本字符串就同步改掉测试，测不到真实行为）。

测试夹具里的口令/密钥全部是明显的假值（"e2e-*"前缀），不含任何真实凭据。
依赖：bash（skipIf 整文件）；① 正例需系统 gpg（无则单条 skip）；WSL 下 sqlite3 缺失
时回环自检自动退化为"解密+解包"两层（与脚本同口径），不断言 integrity 行。
"""
import datetime
import glob
import io
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(BASE, "scripts", "backup.sh")
SENTINEL = os.path.join(BASE, "scripts", "backup_sentinel.py")

BASH = shutil.which("bash")
FAKE_PASSPHRASE = "e2e-not-a-real-passphrase"  # 假口令，仅测试桩/对称加密回路使用

# gpg 桩：只认 backup.sh 实际用到的参数形态；FAKE_GPG_MODE 选择行为。
FAKE_GPG = """#!/usr/bin/env bash
# 测试桩 gpg：--passphrase-fd 出现时先排空 stdin（否则 printf 侧 SIGPIPE 会把
# "加密成功"变成"管道失败"，测的就不是回环自检了）。
out="" in="" mode="" drain=0
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2;;
    --passphrase-fd) drain=1; shift 2;;
    --cipher-algo|--recipient) shift 2;;
    --symmetric|--encrypt) mode=enc; shift;;
    --decrypt) mode=dec; shift;;
    *) if [ -f "$1" ]; then in="$1"; fi; shift;;
  esac
done
[ "$drain" -eq 1 ] && cat > /dev/null
case "${FAKE_GPG_MODE:-garbage}" in
  garbage)
    if [ "$mode" = enc ]; then head -c 400 /dev/urandom > "$out"; exit 0; fi
    exit 1;;
  fail) exit 2;;
esac
exit 3
"""

# tar 桩：任何调用一律失败（列目录/解包都读不到东西）。
FAKE_TAR_LIST_FAIL = """#!/usr/bin/env bash
echo "fake tar: simulated read failure" >&2
exit 2
"""

# find 桩：透传给真 find；第一次带 -mtime 的调用（= 轮转开始）后把"当日归档"删掉，
# 模拟"轮转把当天件也删了"的事故，钉 rc=8 自检。
FAKE_FIND_KILL_TODAY = """#!/usr/bin/env bash
"$FAKE_FIND_REAL" "$@"; rc=$?
case "$*" in
  *-mtime*)
    if [ ! -e "$FAKE_FIND_TRIGGER" ]; then
      touch "$FAKE_FIND_TRIGGER"
      rm -f "$FAKE_FIND_KILL"
    fi;;
esac
exit $rc
"""


def _today():
    return datetime.datetime.now().strftime("%Y-%m-%d")


def _day_str(offset_days):
    d = datetime.date.today() - datetime.timedelta(days=offset_days)
    return d.strftime("%Y-%m-%d")


def _age_file(path, days, bytes_=None):
    """写文件并把 mtime 回拨 days 天（轮转用例的时间夹具）。"""
    with open(path, "wb") as f:
        f.write(bytes_ or b"x" * 300)
    ts = (datetime.datetime.now() - datetime.timedelta(days=days)).timestamp()
    os.utime(path, (ts, ts))


@unittest.skipIf(BASH is None, "需要 bash（Git Bash/WSL）")
class _BackupRunBase(unittest.TestCase):
    """公共脚手架：隔离的 APP_DIR/BACKUP_DIR/状态目录 + fakebin PATH 注入。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="backup-e2e-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.app = os.path.join(self.tmp, "app")
        self.backups = os.path.join(self.tmp, "backups")
        self.state = os.path.join(self.tmp, "state")
        self.logs = os.path.join(self.tmp, "logs")
        for d in (self.app, self.backups, self.state, self.logs):
            os.makedirs(d, exist_ok=True)
        with io.open(os.path.join(self.app, ".env"), "w", encoding="utf-8", newline="\n") as f:
            # 假密钥/假口令哈希：只为让打包路径完整走通，无任何真实凭据
            f.write("YIBAN_ACCOUNTS_KEY=" + "f" * 64 + "\n"
                    "YIBAN_SECRET_KEY=e2e-not-a-real-secret\n"
                    "ADMIN_USER=e2e-admin\nADMIN_PASSWORD_HASH=e2e-no-plain-credential\n")
        self.fakebin = os.path.join(self.tmp, "fakebin")
        os.makedirs(self.fakebin, exist_ok=True)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("YIBAN_", "BACKUP_", "REMOTE_"))
                    and k not in ("RETENTION_DAYS", "GPG_RECIPIENT", "KEY_FILE",
                                  "SIGN_STATE_DIR", "SIGN_LOG_DIR", "DB_FILE")}
        self.env.update({
            "APP_DIR": self.app,
            "BACKUP_DIR": self.backups,
            "YIBAN_STATE_DIR": self.state,
            "YIBAN_LOG_FILE": os.path.join(self.logs, "sign.log"),
            "KEY_FILE": os.path.join(self.tmp, "no-accounts-key"),
        })
        # cygpath 归一（Git Bash 下 Windows 路径进不了 PATH），WSL 原样返回
        conv = subprocess.run([BASH, "-c", 'cygpath -u "$1" 2>/dev/null || echo "$1"',
                               "_", self.fakebin], capture_output=True, text=True, timeout=60)
        self.env["PATH"] = (conv.stdout.strip() or self.fakebin) + os.pathsep + \
            self.env.get("PATH", "")

    def _fake(self, name, body):
        path = os.path.join(self.fakebin, name)
        with io.open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(body)
        os.chmod(path, 0o755)
        return path

    def _run(self, args=(), extra_env=None):
        env = dict(self.env)
        env.update(extra_env or {})
        return subprocess.run([BASH, SCRIPT, *args], capture_output=True,
                              env=env, cwd=self.tmp, timeout=300)

    def _out(self, r):
        return ((r.stdout or b"") + (r.stderr or b"")).decode("utf-8", errors="replace")

    def _stdout(self, r):
        return (r.stdout or b"").decode("utf-8", errors="replace")

    def _stderr(self, r):
        return (r.stderr or b"").decode("utf-8", errors="replace")

    def _archive_name(self, suffix=""):
        return os.path.join(self.backups, f"yiban-{_today()}.tar.gz{suffix}")

    def _run_plaintext(self, extra_env=None):
        env = {"BACKUP_PLAINTEXT": "1"}
        env.update(extra_env or {})
        r = self._run((), env)
        return r, self._out(r)


class PlaintextGuardTest(_BackupRunBase):
    """MF-79 修法2：明文模式要"看得见、认得出、目录不裸奔"，且产物不算健康。"""

    def test_plaintext_run_warns_on_stderr_exits_dedicated_rc6(self):
        r, out = self._run_plaintext()
        self.assertEqual(r.returncode, 6, f"明文轮必须以专用 rc=6 结束：{out}")
        self.assertIn("BACKUP_PLAINTEXT=1", self._stderr(r), "大字告警必须走 stderr")
        self.assertIn("明文", self._stderr(r))
        # 归档确实产出且为可读 tar.gz（不是"报了警没干活"）
        self.assertTrue(os.path.isfile(self._archive_name()), f"明文归档未落盘：{out}")
        with tarfile.open(self._archive_name()) as t:
            self.assertTrue(t.getnames())
        self.assertTrue(os.path.isfile(self._archive_name() + ".sha256"),
                        "明文轮仍应产出与落盘产物对应的清单")

    def test_backup_dir_locked_down_to_0700(self):
        os.chmod(self.backups, 0o755)  # 模拟历史部署留下的可读目录
        r, out = self._run_plaintext()
        self.assertIn(r.returncode, (0, 6), out)
        mode = stat.S_IMODE(os.stat(self.backups).st_mode)
        self.assertEqual(mode, 0o700, f"备份目录必须收紧到 0700（MF-79），实际 {oct(mode)}")

    def test_plaintext_artifact_not_healthy_for_sentinel(self):
        """MF-79 验收不变量的 e2e 侧：backup.sh 真产出的明文包，哨兵判不健康并发告警。"""
        r, out = self._run_plaintext()
        self.assertEqual(r.returncode, 6, out)
        import importlib.util
        from unittest import mock
        spec = importlib.util.spec_from_file_location("_sentinel_artifact", SENTINEL)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mails = []
        with mock.patch.dict(os.environ, {
            "BACKUP_DIR": self.backups,
            "APP_DIR": self.app,
            "YIBAN_BACKUP_INSTALLED": os.path.join(self.tmp, "sbin", "absent.sh"),
            "YIBAN_STATE_DIR": self.state,
            "YIBAN_ENV_FILE": os.path.join(self.tmp, "no.env"),
        }), mock.patch.object(mod, "_send_admin_alert",
                              side_effect=lambda title, mail: mails.append((title, mail)) or True):
            rc = mod.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(len(mails), 1,
                         f"真实产出的明文归档被哨兵计为健康/或漏报：{mails}")
        self.assertIn("明文", mails[0][1].to_plain())


class EncryptRoundtripTest(_BackupRunBase):
    """MF-76 修法1：可解才删明文；不可解 ⇒ 明文仍在、不出清单、非 0 退出。"""

    def _pass_env(self):
        return {"BACKUP_GPG_PASSPHRASE": FAKE_PASSPHRASE}

    def test_real_gpg_roundtrip_then_restore_closes_loop(self):
        """正例：真 gpg 对称加密 ⇒ 回环自检过 ⇒ 删明文留密文；且 --restore 真能解回。"""
        if shutil.which("gpg") is None:
            self.skipTest("需要系统 gpg")
        r = self._run((), self._pass_env())
        out = self._out(r)
        self.assertEqual(r.returncode, 0, f"健康加密轮应 0 退出：{out}")
        self.assertFalse(os.path.exists(self._archive_name()),
                         "回环通过后明文应被删除")
        self.assertTrue(os.path.isfile(self._archive_name(".gpg")), out)
        self.assertTrue(os.path.isfile(self._archive_name(".gpg.sha256")),
                        "自检通过后才允许出清单")
        dest = os.path.join(self.tmp, "restore-back")
        r2 = self._run(("--restore", self._archive_name(".gpg"), dest), self._pass_env())
        out2 = self._out(r2)
        self.assertEqual(r2.returncode, 0, f"真实 --restore 闭环失败：{out2}")
        self.assertTrue(os.path.isfile(os.path.join(dest, "data", ".env")), out2)

    def test_gpg_stub_garbage_ciphertext_keeps_plaintext_no_manifest_rc7(self):
        """活体反例：gpg"成功"但产物不可解 ⇒ 明文仍在、密文删除、无清单、rc=7。"""
        self._fake("gpg", FAKE_GPG)
        r = self._run((), dict(self._pass_env(), FAKE_GPG_MODE="garbage"))
        out = self._out(r)
        self.assertEqual(r.returncode, 7, f"密文不可解必须 rc=7（非 0 且不谎报）：{out}")
        self.assertIn("回环自检", out, "必须点名是哪道护栏拦下的")
        self.assertTrue(os.path.isfile(self._archive_name()),
                        f"可解才删明文——不可解时明文必须仍在：{out}")
        self.assertEqual([], glob.glob(os.path.join(self.backups, "*.gpg")),
                         "不可解的半成品密文不得留存")
        self.assertEqual([], glob.glob(os.path.join(self.backups, "*.sha256")),
                         "自检未过不得出任何清单（'出了清单'本身就是假绿信号）")

    def test_gpg_stub_hard_fail_keeps_plaintext_nonzero_rc(self):
        """验收不变量原文："gpg stub 必败 ⇒ 明文仍在 + rc 非 0"（回退明文轮 rc=6）。"""
        self._fake("gpg", FAKE_GPG)
        r = self._run((), dict(self._pass_env(), FAKE_GPG_MODE="fail"))
        out = self._out(r)
        self.assertNotEqual(r.returncode, 0, f"加密失败绝不允许静默报成成功：{out}")
        self.assertEqual(r.returncode, 6, "加密不可用 ⇒ 本轮为明文产物，专用码 6")
        self.assertTrue(os.path.isfile(self._archive_name()), f"明文仍在：{out}")

    def test_garbage_ciphertext_smaller_than_floor_detected(self):
        """尺寸下限（对齐 docker/backup-docker.sh B12-1）：小短密文不配被称为'已加密'。"""
        stub = FAKE_GPG.replace("head -c 400", "head -c 50")
        self._fake("gpg", stub)
        r = self._run((), dict(self._pass_env(), FAKE_GPG_MODE="garbage"))
        out = self._out(r)
        self.assertEqual(r.returncode, 7, f"疑似空包必须拦下：{out}")
        self.assertIn("疑似空包", out)


class RestoreGuardTest(_BackupRunBase):
    """MF-80 修法4：tar 失败时三条护栏必须执行且脚本失败；恶意条目逐类真包反证。"""

    def _pkg(self, members):
        """members: [(name, type|None, content)]，type∈{None,'l','c'}。"""
        path = os.path.join(self.tmp, "pkg.tar.gz")
        with tarfile.open(path, "w:gz") as t:
            for name, kind, content in members:
                ti = tarfile.TarInfo(name)
                if kind is None:
                    data = content.encode()
                    ti.size = len(data)
                    ti.type = tarfile.REGTYPE
                    t.addfile(ti, io.BytesIO(data))
                elif kind == "l":
                    ti.type = tarfile.SYMTYPE
                    ti.linkname = "/etc/passwd"
                    t.addfile(ti)
                elif kind == "c":
                    ti.type = tarfile.CHRTYPE
                    ti.devmajor, ti.devminor = 1, 3
                    t.addfile(ti)
        return path

    def _restore(self, pkg, dest_name="dest", extra_env=None):
        env = {"APP_DIR": self.app}
        env.update(extra_env or {})
        # 无库无审计核验需求：包内不放 data/*.db，恢复走到"解包"即可判定护栏
        return self._run(("--restore", pkg, os.path.join(self.tmp, dest_name)), env)

    def test_tar_listing_failure_is_guarded_not_skipped(self):
        """tar 列目录失败 ⇒ 护栏判码执行并报"读包列表失败"；不得静默放行到解包。"""
        pkg = self._pkg([("data/.env", None, "X=1\n")])
        self._fake("tar", FAKE_TAR_LIST_FAIL)
        r = self._restore(pkg)
        out = self._out(r)
        self.assertEqual(r.returncode, 1, f"tar 失败必须让脚本失败：{out}")
        self.assertIn("读包列表失败", out,
                      "旧实现里 pipefail 把三条护栏整体跳过（GUARD_SKIPPED）；"
                      "失败必须在护栏处被点名，而不是拖到解包处撞运气")

    def test_symlink_entry_rejected(self):
        pkg = self._pkg([("data/.env", None, "X=1\n"), ("data/evil", "l", None)])
        r = self._restore(pkg)
        out = self._out(r)
        self.assertEqual(r.returncode, 1, out)
        self.assertIn("符号链接", out)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "dest", "data", "evil")))

    def test_traversal_entry_rejected(self):
        pkg = self._pkg([("../escape.txt", None, "boom\n")])
        r = self._restore(pkg)
        out = self._out(r)
        self.assertEqual(r.returncode, 1, out)
        self.assertIn("路径穿越", out)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "escape.txt")))

    def test_char_device_entry_rejected(self):
        pkg = self._pkg([("data/nullcopy", "c", None)])
        r = self._restore(pkg)
        out = self._out(r)
        self.assertEqual(r.returncode, 1, out)
        self.assertIn("设备/FIFO", out)

    def test_good_package_still_restores(self):
        """正对照：护栏不得过敏——干净包正常恢复。"""
        pkg = self._pkg([("data/.env", None, "X=1\n"), ("keys/k.txt", None, "dummy\n")])
        r = self._restore(pkg)
        out = self._out(r)
        self.assertEqual(r.returncode, 0, out)
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "dest", "data", ".env")), out)


class RotationGuardTest(_BackupRunBase):
    """MF-77 修法5：RETENTION 校验、最近 K 组下界、明文从紧、逐件日志、当日件自检。"""

    def _seed_group(self, day, days_old, suffix=".tar.gz.gpg"):
        base = os.path.join(self.backups, f"yiban-{day}{suffix}")
        _age_file(base, days_old)
        _age_file(base + ".sha256", days_old, b"e2e hash line\n")
        return base

    def test_retention_days_zero_refuses_before_touching_anything(self):
        r = self._run((), {"BACKUP_PLAINTEXT": "1", "RETENTION_DAYS": "0"})
        out = self._out(r)
        self.assertEqual(r.returncode, 1, f"RETENTION_DAYS=0 必须拒绝执行：{out}")
        self.assertIn("RETENTION_DAYS", out)
        self.assertEqual([], os.listdir(self.backups), "拒绝执行不得先写后拒")

    def test_retention_days_nonnumeric_refuses(self):
        r = self._run((), {"BACKUP_PLAINTEXT": "1", "RETENTION_DAYS": "30d"})
        self.assertEqual(r.returncode, 1, self._out(r))
        self.assertIn("RETENTION_DAYS", self._out(r))

    def test_rotation_keeps_newest_k_groups_regardless_of_age(self):
        # 5 组密文历史（10~14 天前），RETENTION=3 ⇒ 全部过期；下界 K=2 只许留今天+最近一组
        # _day_str(10) 比 _day_str(14) 新，所以 seeded[0] 是"最近的历史组"（受下界保护）
        seeded = [self._seed_group(_day_str(d), d) for d in range(10, 15)]
        r = self._run((), {"BACKUP_PLAINTEXT": "1", "RETENTION_DAYS": "3",
                           "BACKUP_MIN_KEEP": "2"})
        out = self._out(r)
        self.assertIn(r.returncode, (0, 6), out)
        newest = seeded[0]   # _day_str(10)：与今天并列进最近 K=2 组
        self.assertTrue(os.path.isfile(newest),
                        f"最近一组（{os.path.basename(newest)}）过了 mtime 也不许删（下界）：{out}")
        self.assertTrue(os.path.isfile(newest + ".sha256"), "侧车同组共命运")
        for p in seeded[1:]:
            self.assertFalse(os.path.exists(p), f"过期且出下界应被删：{p}")
            self.assertFalse(os.path.exists(p + ".sha256"))
        self.assertEqual(out.count("轮转删除"), 8, f"每次删除必须留一行日志：{out}")
        self.assertTrue(os.path.isfile(self._archive_name()), "当日件不得被轮转波及")

    def test_plaintext_archives_expire_tighter_than_encrypted(self):
        """明文包（含全量密钥）保留期从紧：**默认 K=7 下界也压不过 ≤2 天承诺**。

        2026-09-25 终审 Important①：旧实现里 rotate_pass 的"最近 K 组"下界对明文 pass
        同样生效——日备机器上 3 天前的明文包仍在最近 7 组内，"≤2 天"（脚本头注释、
        rc=6 大字告警、预案 Task-2 方向2）被静默压成 ~7 天。旧测试用
        BACKUP_MIN_KEEP=1 把下界关掉才通过，恰是掩盖。现按**默认 K** 钉死：
        明文过期必删、即使其日期组仍在最近 7 组内；同组密文受下界保护照常留存。
        """
        day = _day_str(3)
        plain = os.path.join(self.backups, f"yiban-{day}.tar.gz")
        enc = plain + ".gpg"
        for p in (plain, plain + ".sha256", enc, enc + ".sha256"):
            _age_file(p, 3)
        r = self._run((), {"BACKUP_PLAINTEXT": "1"})
        out = self._out(r)
        self.assertIn(r.returncode, (0, 6), out)
        self.assertFalse(
            os.path.exists(plain),
            f"明文包过期(2天)必须删除——不得被默认 K=7 轮转下界静默豁免：{out}")
        self.assertFalse(os.path.exists(plain + ".sha256"))
        self.assertTrue(os.path.exists(enc), "密文包 3 天 << 30 天保留期，不得删")
        self.assertTrue(os.path.exists(enc + ".sha256"))

    def test_today_archive_deleted_by_rotation_detected_rc8(self):
        """删除后自检"当日包仍在"：轮转误删当天件 ⇒ 大声失败 rc=8，不许绿灯收工。"""
        real_find = shutil.which("find")
        if real_find is None:
            self.skipTest("需要 find")
        self._fake("find", FAKE_FIND_KILL_TODAY)
        r = self._run((), {"BACKUP_PLAINTEXT": "1",
                           "FAKE_FIND_REAL": real_find,
                           "FAKE_FIND_KILL": self._archive_name(),
                           "FAKE_FIND_TRIGGER": os.path.join(self.tmp, "find-trigger")})
        out = self._out(r)
        self.assertEqual(r.returncode, 8, f"轮转后当日包失踪必须非 0：{out}")
        self.assertIn("当日", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
