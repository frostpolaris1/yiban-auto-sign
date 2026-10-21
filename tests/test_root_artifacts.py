# -*- coding: utf-8 -*-
"""检出根不得出现运行时产物（NoRootArtifacts，128-P0-B 处置）。

标签：J · 运维：部署/备份/发布
覆盖：仓库根目录里的数据库、通知账本/节流状态、按日状态件、运行日志与运行锁。
    它们属于 `data/`、`YIBAN_STATE_DIR` 或 `YIBAN_LOG_FILE` 指向的目录，不该落在
    检出根：根目录一旦出现这些文件，备份链按 `SIGN_STATE_DIR` 收账本就会静默漏收，
    两棵树（dev / 生产）的落点也会再次分叉。
对应实现：`yiban/adapters/config/env_io.py` 的默认落点（`data/yiban.db`、
    `data/.env`）、`yiban/adapters/notifier/notify/ledger._state_dir()`。
关键断言：扫描**真实的仓库根**必须为空；扫描器对合成目录里的产物必须命中
    （"扫空不得当绿"）。`.env` 是配置输入（部署面在 `APP_DIR/.env`），不属运行产物，
    因此不在判据里。
依赖：纯 `glob` + 临时目录；不启动进程、不联网。
"""
import glob
import os
import shutil
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 运行产物：写到检出根就是落点错误。模式按写入方的真实文件名列（不要写成
#: 宽泛的 `*.json`——配置名册、package.json 等合法文件也住在这里）。
RUNTIME_GLOBS = (
    "yiban.db", "yiban.db-wal", "yiban.db-shm",
    "notify-ledger.json*", "notify-throttle.json*",
    "accounts.json", "accounts.json.bak-*",
    "cred-state.json",
    "sign-state-*.json", "sign-daily-*.json",
    "sched-run-*.json", "sched-slot-*.json", "sched-snapshot-*.json",
    "mail-user-fail-*.json",
    "signin-run.lock*",
    "*.log",
)

#: 仓库根必须存在的文件：证明扫描器看的是真的仓库根，而不是一个空目录。
_ROOT_MARKERS = ("pyproject.toml", "README.md", "run.sh")


def root_artifacts(root):
    """→ 仓库根命中的运行产物文件名（排序）。"""
    found = []
    for pattern in RUNTIME_GLOBS:
        found.extend(glob.glob(os.path.join(root, pattern)))
    return sorted(os.path.basename(path) for path in found)


class NoRootArtifactsTest(unittest.TestCase):
    """运行时产物必须落在状态目录/数据目录，不得落在检出根。"""

    def test_repo_root_has_no_runtime_artifacts(self):
        for marker in _ROOT_MARKERS:
            self.assertTrue(os.path.isfile(os.path.join(BASE, marker)),
                            "扫描面不对：仓库根缺少 %s" % marker)
        self.assertEqual(
            root_artifacts(BASE), [],
            "运行时产物落在检出根：删除或迁入 data/ 与 YIBAN_STATE_DIR/"
            "YIBAN_LOG_FILE 指向的目录（128-P0-B）")

    def test_detector_has_teeth(self):
        tmp = tempfile.mkdtemp(prefix="yiban-rootart-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        for name in ("yiban.db", "yiban.db-wal", "notify-ledger.json.lock",
                     "sign-state-2026-01-01.json", "sign.log"):
            with open(os.path.join(tmp, name), "w", encoding="utf-8") as f:
                f.write("x")
        # 合法文件：配置输入与应当入库的仓库文件都不许误报
        for name in (".env", "pyproject.toml", "registry.json"):
            with open(os.path.join(tmp, name), "w", encoding="utf-8") as f:
                f.write("x")
        self.assertEqual(
            root_artifacts(tmp),
            ["notify-ledger.json.lock", "sign-state-2026-01-01.json",
             "sign.log", "yiban.db", "yiban.db-wal"])


if __name__ == "__main__":
    unittest.main()
