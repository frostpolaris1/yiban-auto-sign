# -*- coding: utf-8 -*-
"""路径键优先级只有一个口径：进程环境 → `.env` → 默认值（工单 71z7）。

标签：A · 配置：邮件/推送读侧
覆盖：四个路径键（`YIBAN_ENV_FILE` / `YIBAN_DB_FILE` / `YIBAN_STATE_DIR` /
   `YIBAN_LOG_FILE`）在 `env_io.PATH_KEYS`、`resolve_path`、`child_env.build_child_env`
   与三个 bash 载体里的同一口径；非路径键保持 `.env` 优先（M27）。
对应实现：`yiban/adapters/config/env_io.py`、`scripts/child_env.py`、
   `run.sh` / `scripts/yiban-fallback.sh` / `run_probe.sh`。
关键断言：① 路径键集合只有一处定义，CLI 视图引用它；② 四个路径键在进程环境与
   `.env` 冲突时取进程环境；③ 抽一个非路径键，冲突时取 `.env`（翻优先级会红）。
依赖：临时 `.env`；不触网、不建库。bash 载体做源码级守卫（行为级覆盖见报告）。
"""
import io
import os
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(BASE, "scripts") not in sys.path:
    sys.path.insert(0, os.path.join(BASE, "scripts"))

import child_env  # noqa: E402

from yiban.adapters.config import env_io  # noqa: E402

#: 三个**可配置**路径键（进程环境优先）。指针 `YIBAN_ENV_FILE` 单独一条：
#: 它只由进程环境给，`.env` 里写它一律忽略（否则"读哪份配置"会自指）。
PATH_KEYS = ("YIBAN_DB_FILE", "YIBAN_STATE_DIR", "YIBAN_LOG_FILE")
ENV_FILE_KEY = "YIBAN_ENV_FILE"


class PathEnvPriorityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="yiban-path-env-")
        self.addCleanup(self.tmp.cleanup)
        self.env_file = os.path.join(self.tmp.name, ".env")
        with io.open(self.env_file, "w", encoding="utf-8", newline="\n") as fh:
            for key in PATH_KEYS:
                fh.write(f"{key}=dotenv-{key}\n")
            fh.write("YIBAN_PROXY=http://dotenv-proxy:3128\n")

    def test_path_keys_have_one_definition_point(self):
        self.assertEqual(tuple(env_io.PATH_KEYS), PATH_KEYS)
        from yiban.entry.cli import _ENV_FIRST_PATH_KEYS
        self.assertEqual(tuple(_ENV_FIRST_PATH_KEYS), tuple(env_io.PATH_KEYS),
                         "CLI 视图必须引用同一元组，不得另抄一份键名")

    def test_resolve_path_prefers_process_env(self):
        for key in PATH_KEYS:
            with self.subTest(key=key):
                got = env_io.resolve_path(
                    key, "default", env={key: f"env-{key}"}, env_file=self.env_file)
                self.assertEqual(got, f"env-{key}",
                                 "三个可配置路径键：进程环境必须压过 .env")

    def test_resolve_pointer_ignores_dotenv_self_reference(self):
        """指针键不得由 `.env` 自指：只认进程环境，否则 web 与 run.sh 会读两份配置。"""
        got = env_io.resolve_path(
            ENV_FILE_KEY, "default.env", env={}, env_file=self.env_file)
        self.assertEqual(got, "default.env",
                         ".env 里写的指针被当成了事实（自指）")
        got2 = env_io.resolve_path(
            ENV_FILE_KEY, "default.env",
            env={ENV_FILE_KEY: "from-env.env"}, env_file=self.env_file)
        self.assertEqual(got2, "from-env.env", "进程环境给的指针必须生效")

    def test_child_env_prefers_process_env_for_path_keys(self):
        base = {key: f"env-{key}" for key in PATH_KEYS}
        env = child_env.build_child_env(self.env_file, base=base)
        for key in PATH_KEYS:
            self.assertEqual(env.get(key), f"env-{key}",
                             f"{key}：子进程注入必须与 resolve_path 同口径")

    def test_child_env_keeps_dotenv_first_for_non_path_keys(self):
        env = child_env.build_child_env(
            self.env_file, base={"YIBAN_PROXY": "http://env-proxy:8080"})
        self.assertEqual(env.get("YIBAN_PROXY"), "http://dotenv-proxy:3128",
                         "非路径键保持 .env 优先（M27）：翻成进程环境优先会静默"
                         "压过网页设置页写入的 .env")

    def test_child_env_pointer_is_not_taken_from_dotenv(self):
        """`YIBAN_ENV_FILE` 是指针：`.env` 里写它一律忽略，子进程读父进程那份。"""
        env = child_env.build_child_env(self.env_file, base={})
        self.assertEqual(env.get("YIBAN_ENV_FILE"), self.env_file,
                         "父进程没给指针时，子进程必须读父进程读的同一份 .env")
        env2 = child_env.build_child_env(
            self.env_file, base={"YIBAN_ENV_FILE": "parent.env"})
        self.assertEqual(env2.get("YIBAN_ENV_FILE"), "parent.env",
                         "父进程给的指针优先于 .env 里的同名键")

    def test_bash_carriers_apply_the_same_exception(self):
        """三个 bash 载体都必须对三个可配置路径键做"进程环境优先、.env 补缺"。

        `YIBAN_ENV_FILE` 是"读哪个文件"的指针，三个脚本都固定 `$APP_DIR/data/.env`
        （p12-13），故不在 .env 覆盖范围内。
        """
        for rel in ("run.sh", "scripts/yiban-fallback.sh", "run_probe.sh"):
            with self.subTest(script=rel):
                with io.open(os.path.join(BASE, rel), encoding="utf-8") as fh:
                    text = fh.read()
                self.assertIn("YIBAN_DB_FILE|YIBAN_STATE_DIR|YIBAN_LOG_FILE", text,
                              f"{rel} 必须把三个可配置路径键从 .env 覆盖里排除")


if __name__ == "__main__":
    unittest.main(verbosity=2)
