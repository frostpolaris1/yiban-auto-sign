"""私钥入库门（AGENTS §28／docs/dev/release-signing.md §2）。

钉住的性质（任何时刻都不该被违反）：git 跟踪的文件里不得出现 PEM 私钥块。
判据按内容扫描，不按文件名——改个名字不能绕过。
扫空不得当绿：断言至少扫到 1200 个跟踪文件（低于快照即红，防扫描器失效静默放行）。
有牙自证：内嵌一份假私钥块，扫描器必须命中；公钥块必须不命中。
git 不可用（Windows worktree 在 WSL 下常见）时显性 SKIP，不算绿。
"""
import os
import subprocess
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_PRIVATE_KEY_MARKERS = (
    b"-----BEGIN PRIVATE KEY-----",
    b"-----BEGIN RSA PRIVATE KEY-----",
    b"-----BEGIN EC PRIVATE KEY-----",
    b"-----BEGIN OPENSSH PRIVATE KEY-----",
    b"-----BEGIN ENCRYPTED PRIVATE KEY-----",
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

    def test_scanner_has_teeth(self):
        fake = b"x\n-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n"
        self.assertEqual(scan_bytes(fake), ["-----BEGIN PRIVATE KEY-----"])
        self.assertEqual(scan_bytes(b"-----BEGIN PUBLIC KEY-----\nAAAA\n"), [],
                         "公钥块不得误报")

    def test_gitignore_ignores_private_key_files(self):
        for name in ("yiban-release-key.pem", "release/manifest.json.sig"):
            out = subprocess.run(
                ["git", "-C", REPO_ROOT, "check-ignore", "-q", name], capture_output=True)
            if out.returncode == 128:
                self.skipTest("git 不可用（check-ignore 失败）；忽略规则 DID NOT verify")
            self.assertEqual(out.returncode, 0, "%s 未被 .gitignore 忽略" % name)
