# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""B1：路径类部署键必须走 `env_io.resolve_path` 读取（工单 yiban-auto-sign-census-P1-2）。

病：只认进程环境的路径读法，对**只写进 `.env`** 的部署无效。两侧后果不同。
库文件一侧：引擎与 web 可以打开两个不同的库。日志一侧：每日清理扫错目录。
`state_gc.sweep(STATEDIR, LOGDIR)` 只在 `LOGDIR` 里找按天签到日志。
`LOGDIR` 落在缺省目录时，`.env` 声明的那个目录永不清理。

范围：读侧不认 `.env` 的绕过点共 7 处，分两刀收口。第一刀（PR #39）改 5 处——`web/app.py` 的
`ENV_DEFAULT` 与 `DB_DEFAULT`、`yiban/store/connection.py` 的 `DB_DEFAULT`、
`docker/scheduler.py` 的 `STATEDIR` 与 `ENV_FILE`。第二刀的前一半改余下 2 处——
`docker/scheduler.py` 的 `LOGDIR` 与 `web/app.py` 的 `ACCOUNTS_DEFAULT`。
本文件的用例逐处钉住"读 `.env`"这件事。

标签：J · 运维：部署/备份/发布
覆盖：上述 7 处绕过点的读法；再导出入口 `yiban/store/db.py` 的 `DB_DEFAULT` 与定义点
   同值；进程环境与 `.env` 都没有该键时各处默认值的字面值（默认值由部署形态决定，
   收口只统一读法，不许顺手统一默认值）；`_cleanup_state()` 递给 `state_gc.sweep`
   的日志目录。
对应实现：`yiban/infra/env_io.py` 的 `resolve_path` 与 `env_path`；
   `yiban/state_gc.py` 的 `sweep`；`docker/scheduler.py` 的 `_cleanup_state`。
关键断言：① 引擎侧与 web 侧实际打开的是同一个库文件（主断言）；② `.env` 里的
   `YIBAN_STATE_DIR` 对容器调度器生效；③ `.env` 里的 `YIBAN_ENV_FILE` 对 web 与容器
   调度器生效；④ `db.DB_DEFAULT` 与 `connection.DB_DEFAULT` 同值；⑤ 默认值逐字不变；
   ⑥ `.env` 里的 `YIBAN_LOG_FILE` 与 `YIBAN_ACCOUNTS_FILE` 对 `LOGDIR` 与
   `ACCOUNTS_DEFAULT` 生效；⑦ `_cleanup_state()` 传出去的日志目录是 `.env` 声明的
   那个，`state_gc.plan` 在那里能列出过期按天日志。
为什么不进程内打桩：这些都是模块级常量，导入期求值一次。用例每个判据起一个真进程，
   在受控环境与 cwd 下重新导入——进程内改 `os.environ` 再加模块会复用既有 `sys.modules`
   条目，测不出"导入期读到什么"。
⑦ 为什么记实参而不真删：`_cleanup_state()` 走真删除。用例把 `state_gc.sweep` 换成
   记录实参的替身，再拿记到的实参调只读的 `plan`。缺省目录 `/data/logs` 因此不会被
   测试扫到。实参次序 `sweep(STATEDIR, LOGDIR)` 由 `tests/test_state_gc.py` 的源码
   断言钉住，本文件不重复。
不在本文件范围内：`YIBAN_LOG_FILE` / `YIBAN_ACCOUNTS_FILE` 在 bash 侧的读法。
   `run.sh` / `run_probe.sh` / `scripts/yiban-fallback.sh` 先 export `.env` 再算路径，
   `scripts/backup.sh` 走 `env_get` 回落——四处本已认 `.env`。
   登记路径键里那些仍带 `.env` 回落的裸 `os.environ.get`（`yiban/notify/ledger.py` 的
   `_state_dir()`、`scripts/audit_verify.py` 的库路径取值）不是本刀的病发点：它们读得到
   `.env`，只是读法二手。哪些处仍未收、为什么允许，一律以名册
   `env_io.resolve_path 唯一入口与绕过点` 那一行的白名单为准（AST 门禁
   `scripts/check-path-env-reads.py` 按那一行计数），本文件不复制那份处数。
   `YIBAN_ENV_FILE` 的指针读法已连根收进 `env_io.env_path`——那里是全仓唯一一处对
   该键的裸读，即唯一解析器的定义点。
依赖：临时目录里的 `.env` 与真实 SQLite 库文件；不发网络请求。整文件在本机执行，无 skip。
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 64 位十六进制假密钥：让 init_db 的建表路径不去生成新钥，也就不回写测试用 .env。
_FAKE_KEY = "0" * 64

#: 探针脚本：在**新进程**里按 `want` 装载模块并回报观察值（JSON 到 stdout）。
#: 参数：argv[1]=仓库根，argv[2]=want 列表的 JSON。
_PROBE = r"""
import importlib.util
import json
import os
import sys

base = sys.argv[1]
want = json.loads(sys.argv[2])
sys.path.insert(0, base)
sys.path.insert(0, os.path.join(base, "scripts"))


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(base, rel))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


out = {}
if "constants" in want:
    webapp = load("webapp_probe", os.path.join("web", "app.py"))
    sched = load("sched_probe", os.path.join("docker", "scheduler.py"))
    from yiban.store import connection, db
    out["web.ENV_DEFAULT"] = webapp.ENV_DEFAULT
    out["web.DB_DEFAULT"] = webapp.DB_DEFAULT
    out["web.ACCOUNTS_DEFAULT"] = webapp.ACCOUNTS_DEFAULT
    out["web.ACCOUNTS_FILE"] = webapp.ACCOUNTS_FILE
    out["web.STATE_DIR"] = webapp.STATE_DIR
    out["web.LOG_FILE"] = webapp.LOG_FILE
    out["connection.DB_DEFAULT"] = connection.DB_DEFAULT
    out["db.DB_DEFAULT"] = db.DB_DEFAULT
    out["scheduler.STATEDIR"] = sched.STATEDIR
    out["scheduler.ENV_FILE"] = sched.ENV_FILE
    out["scheduler.LOGDIR"] = sched.LOGDIR
if "db_engine" in want:
    from yiban.store import connection, db
    db.init_db(cleanup=False, migrate=False)
    out["db_engine.opened"] = connection.current_db_file()
if "db_web" in want:
    webapp = load("webapp_probe", os.path.join("web", "app.py"))
    from yiban.store import connection, db
    db.init_db(webapp.DB_FILE, env_file=webapp.ENV_FILE, cleanup=False, migrate=False)
    out["db_web.opened"] = connection.current_db_file()
    out["db_web.declared"] = webapp.DB_FILE
if "sweep" in want:
    # 真入口 _cleanup_state() + 记录实参的 sweep 替身：只读，不在机器目录上删东西。
    sched = load("sched_probe", os.path.join("docker", "scheduler.py"))
    from yiban import state_gc
    seen = {}

    def _spy(state_dir, log_dir=None, env=None, now=None):
        seen["state_dir"] = state_dir
        seen["log_dir"] = log_dir
        return 0, []

    state_gc.sweep = _spy
    sched._cleanup_state()
    out["sweep.args"] = seen
    # 拿记到的实参调只读 plan：证明那个目录真被扫、过期按天日志真被列出来。
    out["sweep.planned"] = state_gc.plan(seen.get("state_dir"), seen.get("log_dir"))[1]
json.dump(out, sys.stdout)
"""


def _clean_env(**overrides):
    """进程环境里去掉全部 YIBAN_ 键，再叠上 overrides（tests/conftest.py 的会话级
    默认键不得泄漏进探针）。"""
    env = {k: v for k, v in os.environ.items() if not k.startswith("YIBAN_")}
    env.update({k: str(v) for k, v in overrides.items()})
    return env


def _probe(want, cwd, env):
    """跑探针，返回它回报的 dict。探针非零退出即失败并带 stderr（不许静默空结果）。"""
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, BASE, json.dumps(list(want))],
        cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        raise AssertionError(
            f"探针进程退出码 {proc.returncode}：{want}；stderr 尾部：{proc.stderr[-800:]}")
    return json.loads(proc.stdout)


class _ProbeBase(unittest.TestCase):
    """公共底座：临时目录 + 受控 `.env` + 一个空的 run 子目录（作探针 cwd）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-resolve-path-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cwd = os.path.join(self.tmp, "run")
        os.makedirs(self.cwd)
        self.env_file = os.path.join(self.tmp, ".env")

    def _write_env(self, text):
        with io.open(self.env_file, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)


class EngineAndWebOpenSameDbTest(_ProbeBase):
    """主断言：`.env` 声明的库路径，引擎侧与 web 侧开到的是同一个文件。"""

    def test_db_file_from_env_file_is_the_one_both_sides_open(self):
        declared = os.path.join(self.tmp, "from-env-file.db")
        self._write_env(f"YIBAN_ACCOUNTS_KEY={_FAKE_KEY}\n"
                        f"YIBAN_DB_FILE={declared}\n")
        env = _clean_env(YIBAN_ENV_FILE=self.env_file)

        engine = _probe(["db_engine"], self.cwd, env)
        web = _probe(["db_web"], self.cwd, env)

        self.assertEqual(os.path.abspath(engine["db_engine.opened"]),
                         os.path.abspath(declared),
                         "引擎侧必须打开 .env 声明的库（这条本来就对，作对照基线）")
        self.assertEqual(os.path.abspath(web["db_web.opened"]),
                         os.path.abspath(declared),
                         "web 侧必须打开 .env 声明的库，而不是默认 yiban.db")
        self.assertEqual(os.path.abspath(engine["db_engine.opened"]),
                         os.path.abspath(web["db_web.opened"]),
                         "两侧开到的必须是同一个库文件（工单点名的病）")
        # 病的确凿物证：默认库不该被创建出来
        self.assertFalse(os.path.exists(os.path.join(self.cwd, "yiban.db")),
                         "web 侧读到 .env 后，cwd 下不应出现第二个库文件")

    def test_process_env_db_file_still_wins_for_both_sides(self):
        """优先级不许被收口翻掉：进程环境 > .env > 默认值，两侧同则。"""
        from_file = os.path.join(self.tmp, "from-env-file.db")
        from_proc = os.path.join(self.tmp, "from-process-env.db")
        self._write_env(f"YIBAN_ACCOUNTS_KEY={_FAKE_KEY}\nYIBAN_DB_FILE={from_file}\n")
        env = _clean_env(YIBAN_ENV_FILE=self.env_file, YIBAN_DB_FILE=from_proc)

        engine = _probe(["db_engine"], self.cwd, env)
        web = _probe(["db_web"], self.cwd, env)

        self.assertEqual(os.path.abspath(engine["db_engine.opened"]),
                         os.path.abspath(from_proc))
        self.assertEqual(os.path.abspath(web["db_web.opened"]),
                         os.path.abspath(from_proc))


class FacadeEntryPointsTest(_ProbeBase):
    """第二入口（再导出别名）必须与定义点同值，且同认 .env。"""

    def test_db_default_is_one_value_across_definition_and_reexport(self):
        declared = os.path.join(self.tmp, "from-env-file.db")
        self._write_env(f"YIBAN_ACCOUNTS_KEY={_FAKE_KEY}\nYIBAN_DB_FILE={declared}\n")
        env = _clean_env(YIBAN_ENV_FILE=self.env_file)

        got = _probe(["constants"], self.cwd, env)

        self.assertEqual(got["connection.DB_DEFAULT"], declared,
                         "定义点 connection.DB_DEFAULT 必须认 .env")
        self.assertEqual(got["db.DB_DEFAULT"], got["connection.DB_DEFAULT"],
                         "门面再导出 db.DB_DEFAULT 必须与定义点同值")
        self.assertEqual(got["web.DB_DEFAULT"], got["connection.DB_DEFAULT"],
                         "web 侧常量必须与引擎定义点同值（不许两份读法）")
        self.assertEqual(got["web.ENV_DEFAULT"], self.env_file,
                         "web.ENV_DEFAULT 在进程环境已给指针时取该指针")


class StateDirFromEnvFileTest(_ProbeBase):
    """容器调度器的状态目录必须认 .env（compose 未注入该键的部署形态）。"""

    def test_scheduler_statedir_follows_env_file(self):
        declared = os.path.join(self.tmp, "custom-state")
        os.makedirs(declared)
        self._write_env(f"YIBAN_STATE_DIR={declared}\n")
        env = _clean_env(YIBAN_ENV_FILE=self.env_file)

        got = _probe(["constants"], self.cwd, env)

        self.assertEqual(got["scheduler.STATEDIR"], declared,
                         "容器调度器的状态目录必须认 .env 里的 YIBAN_STATE_DIR")
        self.assertEqual(got["web.STATE_DIR"], declared,
                         "web 侧同键同口径（此前已收口，作对照基线）")


class EnvFilePointerFromEnvFileTest(_ProbeBase):
    """`.env` 里写的 YIBAN_ENV_FILE 指针必须对 web 与容器调度器生效。

    自举：定位"要读哪一个 .env"按约定 = 进程环境的 YIBAN_ENV_FILE，否则 cwd 下的
    `.env`。本用例刻意不在进程环境设该键，让基线落在 cwd。
    """

    def test_pointer_in_bootstrap_env_file_is_honoured(self):
        pointer = os.path.join(self.tmp, "nested", "deploy.env")
        os.makedirs(os.path.dirname(pointer))
        with io.open(pointer, "w", encoding="utf-8", newline="\n") as f:
            f.write("YIBAN_ADMIN_USER=whoever\n")
        # 基线 .env 落在探针的 cwd（不是 self.env_file），才走得到 env_path() 的回落
        with io.open(os.path.join(self.cwd, ".env"), "w",
                     encoding="utf-8", newline="\n") as f:
            f.write(f"YIBAN_ENV_FILE={pointer}\n")
        env = _clean_env()

        got = _probe(["constants"], self.cwd, env)

        self.assertEqual(got["web.ENV_DEFAULT"], pointer,
                         "web 的 .env 指针必须认基线 .env 里写的 YIBAN_ENV_FILE")
        self.assertEqual(got["scheduler.ENV_FILE"], pointer,
                         "容器调度器的 .env 指针同上")


class LogAndAccountsFromEnvFileTest(_ProbeBase):
    """第二刀前两处：`.env`-only 声明的日志路径与账号文件路径必须被读侧取到。

    compose 未注入这两枚键的部署形态（裸机 + 只改 `.env`）是它们的病发形态。
    进程环境刻意不设这两个键，只写进 `.env`。
    """

    def test_scheduler_logdir_follows_env_file(self):
        declared = os.path.join(self.tmp, "custom-logs", "sign.log")
        self._write_env(f"YIBAN_LOG_FILE={declared}\n")
        env = _clean_env(YIBAN_ENV_FILE=self.env_file)

        got = _probe(["constants"], self.cwd, env)

        self.assertEqual(got["scheduler.LOGDIR"], os.path.join(self.tmp, "custom-logs"),
                         "容器调度器的日志目录必须认 .env 里的 YIBAN_LOG_FILE")
        self.assertEqual(got["web.LOG_FILE"], declared,
                         "web 侧同键同口径（第一刀已收口，作对照基线）")

    def test_web_accounts_default_follows_env_file(self):
        declared = os.path.join(self.tmp, "deploy", "accounts.json")
        self._write_env(f"YIBAN_ACCOUNTS_FILE={declared}\n")
        env = _clean_env(YIBAN_ENV_FILE=self.env_file)

        got = _probe(["constants"], self.cwd, env)

        self.assertEqual(got["web.ACCOUNTS_DEFAULT"], declared,
                         "web 的账号文件默认值必须认 .env 里的 YIBAN_ACCOUNTS_FILE")
        self.assertEqual(got["web.ACCOUNTS_FILE"], declared,
                         "派生常量 ACCOUNTS_FILE 与默认值同源（同缺陷读者不许只修一半）")


class CleanupSweepFollowsEnvFileTest(_ProbeBase):
    """本刀的实际收益：`.env`-only 声明时，每日清理扫的是 `.env` 里那个目录。

    判据走真入口 `_cleanup_state()`。`state_gc.sweep` 被换成记录实参的替身，
    所以缺省目录 `/data/logs` 不会被测试扫到、也不会删任何文件。
    """

    def test_cleanup_state_hands_the_env_file_log_dir_to_sweep(self):
        state_dir = os.path.join(self.tmp, "custom-state")
        log_dir = os.path.join(self.tmp, "custom-logs")
        os.makedirs(state_dir)
        os.makedirs(log_dir)
        expired = os.path.join(log_dir, "sign-2020-01-01.log")
        with open(expired, "w", encoding="utf-8") as f:
            f.write("x")
        self._write_env(f"YIBAN_STATE_DIR={state_dir}\n"
                        f"YIBAN_LOG_FILE={os.path.join(log_dir, 'sign.log')}\n")
        env = _clean_env(YIBAN_ENV_FILE=self.env_file)

        got = _probe(["sweep"], self.cwd, env)

        args = got["sweep.args"]
        self.assertTrue(args, "替身没被调用 = 清理链根本没走，判据不能空转")
        self.assertEqual(args.get("log_dir"), log_dir,
                         "_cleanup_state() 递给 sweep 的日志目录必须是 .env 声明的那个")
        self.assertEqual(args.get("state_dir"), state_dir,
                         "同一入口传出的状态目录也要对得上 .env")
        self.assertIn("sign-2020-01-01.log（log 过期）", got["sweep.planned"],
                       "拿记到的目录调只读 plan，必须真列出该目录里的过期按天日志")
        self.assertTrue(os.path.exists(expired),
                        "替身只记实参；本用例不得删除任何文件")


class DefaultsDoNotDriftTest(_ProbeBase):
    """两处都没有该键时，默认值必须与收口前逐字相同（默认值差是部署形态差）。"""

    def test_every_default_literal_is_unchanged(self):
        env = _clean_env()  # 无 YIBAN_* ；cwd 下不建 .env

        got = _probe(["constants"], self.cwd, env)

        expected = {
            "web.ENV_DEFAULT": ".env",
            "web.DB_DEFAULT": "yiban.db",
            "connection.DB_DEFAULT": "yiban.db",
            "db.DB_DEFAULT": "yiban.db",
            "web.ACCOUNTS_DEFAULT": "accounts.json",
            "scheduler.STATEDIR": "/data/state",
            "scheduler.ENV_FILE": "/data/.env",
            # 宿主形态与容器形态的默认值本就不同：收口只统一读法，不许统一默认值
            "web.STATE_DIR": "/var/log/yiban",
            "web.LOG_FILE": "/var/log/yiban/sign.log",
            "scheduler.LOGDIR": "/data/logs",
        }
        drifted = {k: (got.get(k), v) for k, v in expected.items() if got.get(k) != v}
        self.assertEqual(
            drifted, {},
            "默认值漂动（实测, 期望）：" + "; ".join(
                f"{k}: {a!r} != {b!r}" for k, (a, b) in drifted.items()))


if __name__ == "__main__":
    unittest.main()
