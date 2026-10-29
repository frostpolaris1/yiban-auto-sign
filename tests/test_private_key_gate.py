"""私钥入库门（AGENTS §28／docs/dev/release-signing.md §2）。

钉住的性质（任何时刻都不该被违反）：git 跟踪的文件里不得出现 PEM 私钥块。
判据按内容扫描，不按文件名——改个名字不能绕过。
扫空不得当绿：断言至少扫到 1200 个跟踪文件（低于快照即红，防扫描器失效静默放行）。
有牙自证：内嵌一份假私钥块，扫描器必须命中；公钥块必须不命中。
标记常量与假样本用拼接生成：门自身源码不得含有完整标记字面量，否则扫描
全部跟踪文件时会命中自己（2026-10-29 审查 P1）。
临时仓库自证：在 mktemp 出来的新 git 仓库里 git init／add／ls-files，证明扫描
不依赖当前工作树的 `.git`，WSL／CI 都能真跑。
git 不可用（Windows worktree 在 WSL 下常见）时显性 SKIP，不算绿。
"""
import os
import shutil
import subprocess
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_PEM_BEGIN = b"-----BEGIN "


def _marker(label):
    """拼出 PEM 私钥块起始标记；字面量不完整出现在源码里。"""
    return _PEM_BEGIN + label + b"-----"


_PRIVATE_KEY_MARKERS = (
    _marker(b"PRIVATE KEY"),
    _marker(b"RSA PRIVATE KEY"),
    _marker(b"EC PRIVATE KEY"),
    _marker(b"OPENSSH PRIVATE KEY"),
    _marker(b"ENCRYPTED PRIVATE KEY"),
)

# 2026-10-29 快照：git 跟踪 1588 个文件。下限取 1200，留足删减余量。
_MIN_TRACKED_FILES = 1200

# 显式豁免（§28：要么有门、要么显式豁免）：
# tests/protocol/test_crypto.py 的 _TEST_PRIVATE_KEY_PEM 是旧线加密测试的合成 RSA
# 夹具（变量名与用途均为测试材料，旧线已冻结）；真实私钥仍由本门全量扫描拦截。
_EXEMPT_PATHS = {"tests/protocol/test_crypto.py"}


def _tracked_files(root):
    out = subprocess.run(["git", "-C", root, "ls-files", "-z"], capture_output=True)
    if out.returncode != 0:
        return None
    return [p for p in out.stdout.split(b"\0") if p]


def scan_bytes(data):
    """返回命中的私钥块标记列表；空列表＝干净。"""
    text = data.replace(b"\r\n", b"\n")
    return [m.decode("ascii") for m in _PRIVATE_KEY_MARKERS if m in text]


class PrivateKeyGateTest(unittest.TestCase):
    def test_tracked_files_hold_no_private_key(self):
        files = _tracked_files(REPO_ROOT)
        if files is None:
            self.skipTest("git 不可用（worktree + WSL 常见）；私钥门 DID NOT run，不算绿")
        hits = []
        for raw in files:
            path = raw.decode("utf-8", "replace")
            if path in _EXEMPT_PATHS:
                continue
            try:
                with open(os.path.join(REPO_ROOT, path), "rb") as f:
                    data = f.read()
            except OSError:
                continue
            found = scan_bytes(data)
            if found:
                hits.append((path, found[0]))
        self.assertEqual(hits, [], "跟踪文件里出现私钥块（私钥永不入库）：%r" % (hits,))
        self.assertGreaterEqual(
            len(files), _MIN_TRACKED_FILES,
            "扫描面过小：只看到 %d 个跟踪文件（门可能扫空）" % len(files))

    def test_gate_source_holds_no_marker_literal(self):
        """门自身不得命中：标记用拼接生成，自匹配会让门永远红。

        这是 P1 缺陷的正面钉子。任何把完整标记字面量写回源码的改动都在此红。
        """
        with open(os.path.abspath(__file__), "rb") as f:
            self.assertEqual(
                scan_bytes(f.read()), [],
                "门自身源码出现完整私钥标记字面量（自匹配）；标记必须拼接生成")

    def test_scanner_has_teeth(self):
        fake = (b"x\n" + _marker(b"PRIVATE KEY")
                + b"\nAAAA\n-----END " + b"PRIVATE KEY-----\n")
        self.assertEqual(scan_bytes(fake), [_marker(b"PRIVATE KEY").decode()])
        self.assertEqual(scan_bytes(b"-----BEGIN PUBLIC KEY-----\nAAAA\n"), [],
                         "公钥块不得误报")

    def test_gitignore_ignores_private_key_files(self):
        # --no-index：已跟踪文件默认不参与 check-ignore 判定；本门问的是规则形状。
        for name in ("yiban-release-key.pem", "release/manifest.json.sig"):
            out = subprocess.run(
                ["git", "-C", REPO_ROOT, "check-ignore", "-q", "--no-index", name],
                capture_output=True)
            if out.returncode == 128:
                self.skipTest("git 不可用（check-ignore 失败）；忽略规则 DID NOT verify")
            self.assertEqual(out.returncode, 0, "%s 未被 .gitignore 忽略" % name)
        # 反向钉子：`release/` 规则不得吞掉 `scripts/release/`（2026-10-29 审查 P2）。
        out = subprocess.run(
            ["git", "-C", REPO_ROOT, "check-ignore", "-q", "--no-index",
             "scripts/release/new-probe.sh"], capture_output=True)
        if out.returncode == 128:
            self.skipTest("git 不可用（check-ignore 失败）；忽略规则 DID NOT verify")
        self.assertEqual(
            out.returncode, 1,
            "scripts/release/ 被 .gitignore 命中；release/ 规则必须带前导斜杠")


class TempRepoGateTest(unittest.TestCase):
    """不依赖当前工作树的 `.git`：临时仓库里证明扫描真跑、假私钥真被抓。"""

    def test_fresh_repo_scan_catches_planted_key(self):
        git = shutil.which("git")
        if git is None:
            self.skipTest("需要 git 创建临时仓库；私钥门自证 DID NOT run")
        tmp = tempfile.mkdtemp(prefix="pk-gate-")
        try:
            subprocess.run([git, "-C", tmp, "init", "-q"],
                           check=True, capture_output=True)
            with open(os.path.join(tmp, "clean.txt"), "wb") as f:
                f.write(b"hello\n")
            with open(os.path.join(tmp, "dirty.txt"), "wb") as f:
                f.write(b"x\n" + _marker(b"PRIVATE KEY") + b"\nAAAA\n")
            subprocess.run([git, "-C", tmp, "add", "-A"],
                           check=True, capture_output=True)
            files = _tracked_files(tmp)
            self.assertIsNotNone(files, "临时仓库里 git ls-files 失败")
            self.assertEqual(len(files), 2, "临时仓库跟踪面不符：%r" % (files,))
            hits = {}
            for raw in files:
                path = raw.decode()
                with open(os.path.join(tmp, path), "rb") as f:
                    found = scan_bytes(f.read())
                if found:
                    hits[path] = found
            self.assertEqual(hits, {"dirty.txt": [_marker(b"PRIVATE KEY").decode()]},
                             "临时仓库里的合成私钥块没被抓到（扫描器可能没跑）")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
