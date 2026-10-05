# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""演练树红线（ba-p12-01）：携带生产凭据的树必须被拒，合法演练树不得被误拦。

标签：J · 运维：部署/备份/发布
覆盖：e2e 为准——全部真跑 `bash scripts/drill-container.sh` 子进程 + 真实文件系统，
    逐面：
    ① 容器形态凭据：树根 `data/` 下出现 .env / yiban.db / accounts.json（compose 把
       `./data` bind 到 `/data`，生产凭据与库就在那里）⇒ exit 2 且**点名**该文件。
       修好前这里全过（旧红线的唯一判据是检出根三件），于是 C2 的整体 mv、当日
       `sign_tasks` DELETE、`.env` 覆写都会落到生产树的数据上。
    ② 红线必须先于环境前提：同一棵带凭据的树，即使还缺 docker / 宿主解释器 /
       openssl，也必须报凭据而不是报环境——否则"被红线拒绝"的验收断言分不清是谁
       拦的。对照组：同参数跑一棵干净树，停的是环境前提而非凭据。
    ③ 合法演练树不被误拦：全新树（无 data/）过红线；`data/.env` 首行带自造头的树
       （本脚本 C2 造的，或 2026-10-02 手造的旧演练树）也过红线；**同一棵树实跑第二轮**
       仍过红线（C2 自己造的合成凭据不得把自己人拦下）。豁免判据是 `.env` 的内容而不是
       树内标记文件：残留/被拷来的标记不得豁免（见 StaleMarkerTest）。
    ④ 检出根三件仍拦（既有行为不退化）。
    ⑤ `--help` 的用法区间（`usage()` 里的 sed 行号段）必须恰好覆盖脚本头——加行时
       漏改行号会让帮助文档静默截尾。
对应实现：`scripts/drill-container.sh`
关键断言：一律以真子进程的**退出码 + stderr 文案**为准（同在 exit 2 的还有别的
    "前提不满足"分支，只断退出码分不清是谁拦的）。
依赖：bash（`skipIf` 整文件）；③实跑轮还需宿主 python（用本测试自身的解释器）与
    保留 POSIX mode 的文件系统；docker / openssl 用 stub 替身（只替外部命令，脚本与
    文件系统都是真的）；③的时钟前提用 TZ 钉住（脚本要求本机时钟已过 06:31，否则
    凌晨跑必挂 P0，测试不可重复）。
"""
import datetime
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASH = shutil.which("bash")
SCRIPT = os.path.join(BASE, "scripts", "drill-container.sh")

# P0 断言的四件树形文件（缺任一即"不在演练树根目录"）
TREE_FILES = ("docker/Dockerfile", "yiban/__init__.py", "docker/scheduler.py",
              "tests/fake_yiban_server.py")
# 红线要拦的三件名字：检出根一份（宿主形态）、`data/` 下一份（容器形态）
CRED_NAMES = (".env", "yiban.db", "accounts.json")
# 红线拒绝文案的公共片段（两处红线共用）
CRED_MARK = "疑似携带生产凭据"
# 环境前提的拒绝文案（红线之后的门）
ENV_MARKS = ("docker 不可用", "宿主解释器不可用", "openssl 不可用")
# 旧演练树的 .env 自造头（C2 写的与 2026-10-02 手造的那棵树同前缀）——红线的唯一豁免信号
LEGACY_ENV_HEAD = "# 容器演练专用 .env"
# 修好前的候选信号：树内标记文件。红线**不认它**（见 StaleMarkerTest）。
STALE_MARKER = "data/.drill-tree"


def _write(path, text, mode=0o600):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.chmod(path, mode)


def _fs_preserves_modes():
    r = subprocess.run([BASH, "-c",
                        'f=$(mktemp); chmod 600 "$f"; stat -c %a "$f"; rm -f "$f"'],
                       capture_output=True, text=True, timeout=60)
    return r.stdout.strip() == "600"


def _tz_past_latch():
    """一个把当地墙钟推到 12 点档的固定偏移 TZ。

    脚本 P0 要求本机时钟已过 06:31（容器调度器首签闩锁无上界判定，未过则整轮不触发）。
    这是环境事实，测试要可重复就必须钉住它。POSIX TZ 的偏移是"加到当地时刻得 UTC 的
    值"，故当地 = UTC + off 写作 `DRILL-<off>`。
    """
    utc_h = datetime.datetime.now(datetime.timezone.utc).hour
    off = (12 - utc_h) % 24
    if off > 12:
        off -= 24
    return None if off == 0 else "DRILL%+d" % (-off)


class _TmpBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="drill-redline-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def make_tree(self, name, root_leaks=(), data_leaks=(), marker=False,
                  legacy_header=False, real_pkg=False):
        """造一棵最小演练树：P0 要的四件在位，按需放"生产凭据"形态的文件。"""
        tree = os.path.join(self.tmp, name)
        if real_pkg:
            # 实跑轮：C2 造数要真 yiban 包与 scripts/（含被测脚本自身）
            for sub in ("yiban", "scripts"):
                shutil.copytree(os.path.join(BASE, sub), os.path.join(tree, sub),
                                ignore=shutil.ignore_patterns("__pycache__"))
            _write(os.path.join(tree, "docker", "Dockerfile"), "")
            _write(os.path.join(tree, "docker", "scheduler.py"), "")
            _write(os.path.join(tree, "tests", "fake_yiban_server.py"), "")
        else:
            for rel in TREE_FILES:
                _write(os.path.join(tree, rel), "")
        if marker:
            _write(os.path.join(tree, STALE_MARKER), "drill-container.sh\n", 0o644)
        for n in root_leaks:
            _write(os.path.join(tree, n), "PROD-CREDENTIAL\n")
        for n in data_leaks:
            body = "PROD-CREDENTIAL\n"
            if n == ".env" and legacy_header:
                body = LEGACY_ENV_HEAD + "（2026-10-02 演练，密钥自造）\n"
            _write(os.path.join(tree, "data", n), body)
        return tree

    def run_script(self, tree, args=(), tz=None, stub_bin=None, script=None):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("YIBAN_", "DRILL_"))}
        if tz:
            env["TZ"] = tz
        if stub_bin:
            env["PATH"] = stub_bin + os.pathsep + env.get("PATH", "")
        return subprocess.run([BASH, script or SCRIPT, *args], cwd=tree, env=env,
                              capture_output=True, timeout=600)

    def out(self, r):
        return ((r.stdout or b"") + (r.stderr or b"")).decode("utf-8", "replace")

    def assert_refused_by_redline(self, r, named):
        """红线拒绝：exit 2 + 公共文案 + 点名该文件 + 不是环境前提拦的。"""
        o = self.out(r)
        self.assertEqual(2, r.returncode, o)
        self.assertIn(CRED_MARK, o)
        self.assertIn(named, o)
        for m in ENV_MARKS:
            self.assertNotIn(m, o, f"红线未先于环境前提触发（撞上「{m}」）：{o}")

    def assert_passed_redline(self, r):
        """过红线：仍是 exit 2 前提不满足，但拦的是环境前提而不是凭据。"""
        o = self.out(r)
        self.assertEqual(2, r.returncode, o)
        self.assertNotIn(CRED_MARK, o, f"合法演练树被红线误拦：{o}")
        self.assertTrue(any(m in o for m in ENV_MARKS),
                        f"未停在任何已知的后续前提上：{o}")


@unittest.skipIf(BASH is None, "需要 bash（Git Bash/WSL）")
class DataCredentialsRedlineTest(_TmpBase):
    """① + ②：容器形态凭据（`<tree>/data/` 三件）必须被红线拦下并点名。"""

    def test_each_data_credential_is_refused_and_named(self):
        for leak in CRED_NAMES:
            with self.subTest(leak=leak):
                tree = self.make_tree("solo-" + leak.replace(".", "_"), data_leaks=(leak,))
                # 故意给一个不存在的宿主解释器：旧顺序下会先撞"宿主解释器不可用"
                r = self.run_script(tree, ("--host-python", "/nonexistent-python3"))
                self.assert_refused_by_redline(r, os.path.join("data", leak))
                self.assertIn("演练树 data/", self.out(r))

    def test_production_shaped_tree_is_refused(self):
        """生产容器树的完整形态：data/ 下凭据与库齐（外加 state/logs）。"""
        tree = self.make_tree("prod-shape", data_leaks=CRED_NAMES)
        os.makedirs(os.path.join(tree, "data", "state"))
        os.makedirs(os.path.join(tree, "data", "logs"))
        r = self.run_script(tree)
        # 名册逐名判，先命中的先拒（此处 .env 在名册首位）
        self.assert_refused_by_redline(r, os.path.join("data", ".env"))

    def test_accounts_file_in_data_is_refused_unconditionally(self):
        """accounts.json 本脚本从不产出 ⇒ 无豁免：连自造头的树也照拦它。"""
        tree = self.make_tree("prod-accounts", data_leaks=CRED_NAMES,
                              legacy_header=True)
        r = self.run_script(tree)
        o = self.out(r)
        self.assertEqual(2, r.returncode, o)
        self.assertIn(CRED_MARK, o)
        self.assertIn(os.path.join("data", "accounts.json"), o)
        for m in ENV_MARKS:
            self.assertNotIn(m, o)

    def test_production_db_and_env_pair_is_refused(self):
        """无 accounts.json 的生产形态（.env + 库）同样拒，点名 data/.env。"""
        tree = self.make_tree("prod-db-env", data_leaks=(".env", "yiban.db"))
        r = self.run_script(tree)
        self.assert_refused_by_redline(r, os.path.join("data", ".env"))

    def test_clean_tree_stops_at_later_precondition_control(self):
        """对照组：同参数跑干净树 ⇒ 拦的是环境前提，证明上一条断的确实是红线。"""
        tree = self.make_tree("clean")
        r = self.run_script(tree, ("--host-python", "/nonexistent-python3"))
        self.assert_passed_redline(r)


@unittest.skipIf(BASH is None, "需要 bash（Git Bash/WSL）")
class LegitDrillTreeTest(_TmpBase):
    """③：合法演练树（data/.env 首行有自造头）不得被红线拦。"""

    def test_legacy_drill_tree_passes_redline(self):
        """修好前的旧演练树（2026-10-02 那轮的 .env 首行自造头）⇒ 不得误拦。"""
        tree = self.make_tree("legacy", data_leaks=(".env", "yiban.db"),
                              legacy_header=True)
        r = self.run_script(tree, ("--host-python", "/nonexistent-python3"))
        self.assert_passed_redline(r)

    def test_header_tree_passes_with_all_three_names(self):
        """自造头 + 三件齐（.env / 库 / accounts.json 之外的两枚）⇒ 只拦 accounts.json。"""
        tree = self.make_tree("header", data_leaks=(".env", "yiban.db"),
                              legacy_header=True)
        for rel in ("data/state", "data/logs"):
            os.makedirs(os.path.join(tree, rel))
        r = self.run_script(tree, ("--host-python", "/nonexistent-python3"))
        self.assert_passed_redline(r)

    def test_header_must_be_the_first_line(self):
        """自造头不在首行 ⇒ 不算本脚本所造（判据钉在"首行"）。"""
        tree = self.make_tree("header-late", data_leaks=(".env", "yiban.db"))
        with io.open(os.path.join(tree, "data", ".env"), "w",
                     encoding="utf-8", newline="\n") as f:
            f.write("YIBAN_GLOBAL_PAUSE=0\n" + LEGACY_ENV_HEAD + "（后加的头）\n")
        r = self.run_script(tree)
        self.assert_refused_by_redline(r, os.path.join("data", ".env"))

    def test_legacy_header_does_not_excuse_a_production_env(self):
        """生产 .env 首行不是自造头 ⇒ 照拦（自造头只豁免头在首行的那棵树）。"""
        tree = self.make_tree("prod-env", data_leaks=(".env",))
        with io.open(os.path.join(tree, "data", ".env"), "a",
                     encoding="utf-8", newline="\n") as f:
            f.write("# 生产凭据（运维手写）\n")
        r = self.run_script(tree)
        self.assert_refused_by_redline(r, os.path.join("data", ".env"))

    def test_legacy_header_does_not_excuse_the_accounts_file(self):
        tree = self.make_tree("legacy-accounts", data_leaks=(".env", "accounts.json"),
                              legacy_header=True)
        r = self.run_script(tree, ("--host-python", "/nonexistent-python3"))
        self.assert_refused_by_redline(r, os.path.join("data", "accounts.json"))


@unittest.skipIf(BASH is None, "需要 bash（Git Bash/WSL）")
class StaleMarkerTest(_TmpBase):
    """残留/被拷来的树内标记不得豁免：判据是 data/.env 的内容，不是标记文件存在。"""

    def test_marker_without_header_is_refused(self):
        tree = self.make_tree("stale-marker", data_leaks=(".env", "yiban.db"),
                              marker=True)
        r = self.run_script(tree, ("--host-python", "/nonexistent-python3"))
        self.assert_refused_by_redline(r, os.path.join("data", ".env"))

    def test_marker_does_not_excuse_the_accounts_file(self):
        tree = self.make_tree("stale-marker-accounts", data_leaks=("accounts.json",),
                              marker=True)
        r = self.run_script(tree, ("--host-python", "/nonexistent-python3"))
        self.assert_refused_by_redline(r, os.path.join("data", "accounts.json"))


@unittest.skipIf(BASH is None, "需要 bash（Git Bash/WSL）")
class RootCredentialsRedlineTest(_TmpBase):
    """④：检出根三件仍拦（既有行为不退化）。"""

    def test_each_root_credential_is_refused(self):
        for leak in CRED_NAMES:
            with self.subTest(leak=leak):
                tree = self.make_tree("root-" + leak.replace(".", "_"), root_leaks=(leak,))
                r = self.run_script(tree)
                o = self.out(r)
                self.assert_refused_by_redline(r, leak)
                self.assertIn("演练树根出现", o)

    def test_root_credential_refused_even_on_marked_tree(self):
        tree = self.make_tree("root-marked", root_leaks=(".env",), marker=True)
        r = self.run_script(tree)
        self.assert_refused_by_redline(r, ".env")
        self.assertIn("演练树根出现", self.out(r))


@unittest.skipIf(BASH is None, "需要 bash（Git Bash/WSL）")
class SymlinkFaceTest(_TmpBase):
    """悬空符号链接也算"在场"：否则一个指向树外的链接会溜过红线，C2 随后顺着它写出去。"""

    def test_dangling_symlink_is_refused(self):
        for face, rel in (("data", os.path.join("data", ".env")), ("root", ".env")):
            with self.subTest(face=face):
                tree = self.make_tree("dangling-" + face)
                link = os.path.join(tree, rel)
                os.makedirs(os.path.dirname(link), exist_ok=True)
                try:
                    os.symlink(os.path.join(self.tmp, "outside-tree-does-not-exist"),
                               link)
                except (OSError, NotImplementedError):
                    self.skipTest("宿主不允许建符号链接")
                self.assertFalse(os.path.exists(link), "夹具须是悬空链接")
                r = self.run_script(tree)
                self.assert_refused_by_redline(r, rel)


@unittest.skipIf(BASH is None, "需要 bash（Git Bash/WSL）")
class HelpTextTest(_TmpBase):
    """⑤：`--help` 的用法区间必须恰好覆盖脚本头（sed 行号段不会静默截尾）。"""

    def test_help_covers_whole_header(self):
        r = subprocess.run([BASH, SCRIPT, "--help"], cwd=BASE,
                           capture_output=True, timeout=120)
        o = self.out(r)
        self.assertEqual(0, r.returncode, o)
        self.assertIn("退出码", o)            # 脚本头最后一段
        self.assertNotIn("set -Eeuo pipefail", o)   # 头之后的正文不得进帮助
        # 头**末行**必须进帮助且仍在末位：头末的分割线与头首（第 2 行）同形，
        # 只查"出现在输出里"会被头首顶替（无牙）。头与正文分界都从脚本自身读，
        # 不硬编行号与那串等号；谁再给头加行却不改 usage 的 sed 区间，这里就红。
        head_lines = []
        with io.open(SCRIPT, encoding="utf-8") as f:
            for ln in f:
                if ln.strip() == "set -Eeuo pipefail":   # 正文首行，头到此为止
                    break
                head_lines.append(ln.rstrip("\n"))
        head_last = head_lines[-1]
        self.assertTrue(head_last.strip(), "脚本头末行为空，无法钉边界")
        self.assertEqual(head_last, o.strip("\n").splitlines()[-1],
                         "usage 的 sed 区间未恰好覆盖到脚本头末行")


@unittest.skipIf(BASH is None, "需要 bash（Git Bash/WSL）")
class RealSeedRoundsTest(_TmpBase):
    """③ 实跑轮：真跑 C0~C2 造数（docker/openssl 只替外部命令），复跑第二轮不得被红线拒。

    C2 造 data/.env 时在**首行**写自造头，红线又必须先于 C2 判定——这条用例把两端钉在
    一起：谁删掉 C2 的首行自造头写入（或让 write_env_keys 覆掉首行），第二轮就会被自己的
    红线拦下（本用例红）。豁免判据是 .env 的**内容**，不是任何树内标记文件。
    停在 C3：stub 的 openssl 必失败，故不需要真 docker / 真证书 / 真容器。
    """

    def _stub_bin(self):
        d = os.path.join(self.tmp, "bin")
        _write(os.path.join(d, "docker"), "#!/usr/bin/env bash\nexit 0\n", 0o755)
        _write(os.path.join(d, "openssl"), "#!/usr/bin/env bash\nexit 1\n", 0o755)
        return d

    def test_seed_round_keeps_second_round_out_of_the_redline(self):
        if not _fs_preserves_modes():
            self.skipTest("宿主文件系统不保留 POSIX mode（C2 断言 0600，无法实跑）")
        tree = self.make_tree("real", real_pkg=True)
        tz, stub = _tz_past_latch(), self._stub_bin()
        script = os.path.join(tree, "scripts", "drill-container.sh")
        args = ("--host-python", sys.executable)

        r1 = self.run_script(tree, args, tz=tz, stub_bin=stub, script=script)
        o1 = self.out(r1)
        self.assertNotIn(CRED_MARK, o1, o1)
        self.assertEqual(1, r1.returncode, o1)      # 判据失败（C3 stub）才是应当的
        self.assertIn("C3", o1, o1)                 # 行到了 C3 ⇒ C2 已跑完
        for rel in ("data/.env", "data/yiban.db"):
            self.assertTrue(os.path.exists(os.path.join(tree, rel)),
                            f"第一轮 C2 未产出 {rel}：{o1}")
        # C2 写的自造头就是它们第二轮不被拦的唯一凭据——它必须还在（写键时被保留）
        with io.open(os.path.join(tree, "data", ".env"), encoding="utf-8") as f:
            self.assertTrue(f.readline().startswith(LEGACY_ENV_HEAD),
                            "data/.env 首行自造头在 C2 写键后丢失")

        r2 = self.run_script(tree, args, tz=tz, stub_bin=stub, script=script)
        o2 = self.out(r2)
        self.assertNotIn(CRED_MARK, o2, f"同一棵演练树第二轮被红线拒：{o2}")
        self.assertEqual(1, r2.returncode, o2)
        self.assertIn("C3", o2, o2)


if __name__ == "__main__":
    unittest.main()
