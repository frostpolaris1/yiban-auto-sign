# -*- coding: utf-8 -*-
"""清理 cron 与 run.sh 同源读 `.env`：清的是配置里的状态目录（R3-019）。

标签：J · 运维：部署/备份/发布

模拟的真实用户动作：部署者按 README 把 `YIBAN_STATE_DIR` / `YIBAN_LOG_FILE` 写进
`data/.env`，再由 cron 每日 03:00 跑 `scripts/yiban-cleanup.sh`。用户可见结果 =
真实状态目录的过期件被删、默认目录不被碰。

判据不用日志文本，用"哪个目录的过期件被删/被保留"。默认目录 `/var/log/yiban`
在 `unshare -m` 的私有挂载命名空间里换成临时目录，故断言不会触碰宿主。
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLEANUP_SH = os.path.join(BASE, "scripts", "yiban-cleanup.sh")
HOST_PATHS = os.path.join(BASE, "scripts", "lib", "host_paths.sh")
STATE_CLEANUP = os.path.join(BASE, "scripts", "state_cleanup.py")
YIBAN_PKG = os.path.join(BASE, "yiban")
WSL_VENV_PY = "/root/.venv-yiban-wsl/bin/python3"
BASH = shutil.which("bash")
UNSHARE = shutil.which("unshare")

#: cron 的环境：极简 PATH，不带任何继承的 PYTHONPATH。
MIN_ENV = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"}

#: 过期按日件的日期：远早于默认保留期（365 天），任何时点跑都该被清理。
STALE_STAMP = "2020-01-01"


def _write(path, text):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


@unittest.skipIf(BASH is None, "需要 bash（Git Bash/WSL）")
@unittest.skipIf(UNSHARE is None, "需要 unshare 隔离默认状态目录 /var/log/yiban")
class CleanupCronConfigSourceTest(unittest.TestCase):
    """R3-019：清理脚本解析 `.env`，与 run.sh 同口径。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-cleanup-cron-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.app = os.path.join(self.tmp, "app")
        os.makedirs(os.path.join(self.app, "scripts", "lib"))
        os.makedirs(os.path.join(self.app, "data"))
        shutil.copyfile(CLEANUP_SH, os.path.join(self.app, "scripts", "yiban-cleanup.sh"))
        shutil.copyfile(HOST_PATHS, os.path.join(self.app, "scripts", "lib", "host_paths.sh"))
        shutil.copyfile(STATE_CLEANUP, os.path.join(self.app, "scripts", "state_cleanup.py"))
        # 清理策略的真实实现在 yiban 包里：软链到检出，保证跑的是同一份策略。
        os.symlink(YIBAN_PKG, os.path.join(self.app, "yiban"))
        # 解释器与生产同形：优先 <检出>/.venv/bin/python3。整目录软链（不是只链解释器）：
        # 只链解释器会让 venv 的 site-packages 丢失（pyvenv.cfg 不在链接旁），
        # 运行依赖（portalocker）随即 import 失败。
        venv_root = self._venv_root()
        if venv_root is None:
            self.skipTest("需要带运行依赖的项目 venv（当前解释器不是 venv，且固定路径不存在）")
        os.symlink(venv_root, os.path.join(self.app, ".venv"))
        # 配置状态目录（.env 里声明）与默认状态目录（/var/log/yiban，命名空间内换成临时目录）。
        self.custom = os.path.join(self.tmp, "custom-state")
        self.default = os.path.join(self.tmp, "default-state")
        for d in (self.custom, self.default):
            os.makedirs(d)
            _write(os.path.join(d, "sign-%s.log" % STALE_STAMP), "stale\n")
        _write(os.path.join(self.app, "data", ".env"),
               "YIBAN_STATE_DIR=%s\nYIBAN_LOG_FILE=%s/sign.log\n"
               % (self.custom.replace("\\", "/"), self.custom.replace("\\", "/")))

    @staticmethod
    def _venv_root():
        """带运行依赖的解释器根：优先当前 venv，其次固定路径。"""
        if sys.prefix != sys.base_prefix:
            return sys.prefix
        candidate = os.path.dirname(os.path.dirname(WSL_VENV_PY))
        return candidate if os.path.exists(WSL_VENV_PY) else None

    def test_cleanup_follows_dotenv_state_dir(self):
        prefix = [UNSHARE, "-m"] if os.geteuid() == 0 else [UNSHARE, "-m", "-r"]
        pre = subprocess.run([*prefix, "true"], capture_output=True, timeout=30)
        if pre.returncode != 0:
            self.skipTest("需要可用的 unshare 挂载/用户命名空间（CI 非特权环境会跳过）")
        inner = (
            # 不在宿主机建 /var/log/yiban（root 跑测试会留下目录）：挂载点必须已存在；
            # 不存在就显式报 MOUNT_FAILED，由下方 skip，不把环境不足伪装成红。
            '[ -d /var/log/yiban ] || { echo MOUNT_FAILED; exit 0; }; '
            'mount --bind "$DEFAULT_DIR" /var/log/yiban || { echo MOUNT_FAILED; exit 0; }; '
            'bash "$APP/scripts/yiban-cleanup.sh"; rc=$?; '
            'echo "cleanup_rc=$rc"; '
            'test -f "$CUSTOM/sign-DAY.log" && echo CUSTOM_KEPT || echo CUSTOM_DELETED; '
            'test -f "$DEFAULT_DIR/sign-DAY.log" && echo DEFAULT_KEPT || echo DEFAULT_DELETED'
        ).replace("DAY", STALE_STAMP)
        env = dict(MIN_ENV)
        env.update({"HOME": self.tmp, "APP": self.app,
                    "CUSTOM": self.custom, "DEFAULT_DIR": self.default})
        r = subprocess.run([*prefix, BASH, "-c", inner], env=env, cwd=self.tmp,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=180)
        out = (r.stdout or "") + (r.stderr or "")
        if "MOUNT_FAILED" in out:
            self.skipTest("无法在命名空间内挂载 /var/log/yiban（CI 非特权环境会跳过）")
        self.assertIn("cleanup_rc=0", out, "清理脚本未正常结束：\n" + out)
        self.assertIn("CUSTOM_DELETED", out,
                      "配置里的状态目录未被清理（cron 不读 .env）：\n" + out)
        self.assertNotIn("DEFAULT_DELETED", out,
                         "默认 /var/log/yiban 被误清（清错目录）：\n" + out)
        self.assertIn("DEFAULT_KEPT", out, "默认目录的文件被删了：\n" + out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
