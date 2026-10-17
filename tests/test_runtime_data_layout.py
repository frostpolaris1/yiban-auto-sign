# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""运行数据搬进检出内 `data/`：安装器建目录 + 全部载体指向 data/（bd ybg8 / ddok）。

现象（工单 ybg8，2026-10-09 生产实测）：`deploy/prod/install.sh` 的 M01 前置门要求
检出根属 root 且组/其他不可写；而服务账号要往检出根写 `.env` 与 `yiban.db`——`.env`
的原子写把临时文件建在 `.env` **同目录**（`yiban/infra/env_io.py` 的 `_atomic_replace_env`）。
两条要求在旧布局下互斥 ⇒ 生产只能手工拷贝执行件，哨兵脚本漂移、两条 cron 从未装上。

修法：检出内建 `data/`（属服务账号、0700），`.env` 与 SQLite 库移入其下；检出根改
`root:root` 且 `go-w`。`install.sh` 是 `data/` 的**唯一建立点**——不能靠服务自建：
检出根变 root 只读后 `mkdir` 会失败。

钉住的不变式（非编舞）：
  ① **两者同时成立**：以 root 装完，检出根属 root 且组/其他不可写，且检出内
     `data/` 属服务账号、模式 0700。旧布局下这两条互斥，故它是本批的核心性质。
  ② **写入面与检出根分离**：服务账号写不进检出根，写得到 `data/` 下的 `.env` 与库。
  ③ **载体不落单**：生产树里指向 `.env` / 库的每个载体都落在 `data/`。漏一个 = 它
     静默读到空库或默认配置（界面显示零账号、备份把空库当成功）。
  ④ **升级不丢数据**：既有安装（旧件在检出根）被 `install.sh` 迁入 `data/` 后，服务
     解析到的仍是同一份 `.env` 与库（逐字节相同），旧位置不留第二份权威。
     `LegacyRuntimeDataAdoptionTest` 钉这一条。

对应实现：`deploy/prod/install.sh`（建目录）、`yiban/infra/env_io.py`（默认值单点）、
`yiban/store/connection.py`、`web/app.py`、`config/registry.json`、`run.sh`、
`run_probe.sh`、`scripts/backup.sh`、`scripts/yiban-fallback.sh`、
`scripts/yiban-backup-sentinel.sh`、`deploy/prod/cron.d/yiban-audit`、
`scripts/ledger_check.py`、`yiban/store/purge_guard.py`。

依赖：bash（`skipIf` 整文件）；root + 保留 POSIX mode 的文件系统（WSL，非 root 或
Windows 开发机则该条 skip）；setpriv（缺它则该条 skip）。无网络、不碰生产路径。
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from test_deploy_prod_artifacts import (
    BASH,
    INSTALL,
    SETPRIV,
    _clean_subprocess_env,
    _current_uid,
    _write,
)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICE_UNIT = os.path.join(BASE, "web", "deploy", "yiban-web.service")
REGISTRY = os.path.join(BASE, "config", "registry.json")
UNPRIVILEGED_ID = 65534

#: `data/` 子目录名与两枚默认相对路径（与 `yiban/infra/env_io.py` 的常量同口径；
#: 测试只在此处写一份，避免三处漂移）。
DATA_DIR = "data"
DEFAULT_ENV = "data/.env"
DEFAULT_DB = "data/yiban.db"

#: 载体 → 期望出现的 data/ 形态（正例）。每一条都是"这个文件里那个指向 .env/库的
#: 表达式必须长这样"。新增载体时加一行，否则它落单（本表就是盘查结果的可执行版）。
#: bash 载体写路径字面量；Python 载体（`ledger_check.py` / `purge_guard.py`）**不得**
#: 再抄一份默认值，故其形态钉的是"引用 `env_io` 的单点常量"。
CARRIER_EXPECT = {
    "run.sh": ("$APP_DIR/data/.env", "${YIBAN_DB_FILE:-data/yiban.db}"),
    "run_probe.sh": ("$APP_DIR/data/.env",),
    "scripts/yiban-fallback.sh": ("$APP_DIR/data/.env",),
    "scripts/yiban-backup-sentinel.sh": ("$APP_DIR/data/.env",),
    "scripts/backup.sh": ("${APP_DIR}/data/.env", "data/yiban.db"),
    "deploy/prod/cron.d/yiban-audit": ("--db data/yiban.db", "--env data/.env"),
    "scripts/ledger_check.py": ('resolve_path("YIBAN_DB_FILE", env_io.DEFAULT_DB_FILE)',),
    "yiban/store/purge_guard.py": (
        'resolve_path("YIBAN_DB_FILE", env_io.DEFAULT_DB_FILE)',),
    # 清库留痕的**唯一生产调用点**：必须把 CLI 自己解析出的库路径显式传下去，否则
    # 被调方另解析一次默认值——两者一旦分叉，"拒绝删除"的判据看的是另一个库。
    # 坐标＝批 A1 后的新落点（`yiban/cli.py` 已改成转发壳，不再承载判据）。
    "yiban/entry/cli/__init__.py": ('db_file=_paths(view)["db_file"]',),
}

#: 反向形状：生产树里**不得**再出现"检出根下的 .env / 库"。
#: 判据只看承载路径的表达式（不含散文里的 `.env`/`yiban.db` 字样）。
FORBIDDEN_SHAPES = (
    "$APP_DIR/.env",
    "${APP_DIR}/.env",
    "$APP_DIR/yiban.db",
    "${APP_DIR}/yiban.db",
    "--env .env",
    "--db yiban.db",
)

#: `systemctl` 桩：只回答安装器探测服务状态时用到的那一问。
#: `STUB_UNIT_STATE` 控制回答；非 `active` 时按 systemctl 的约定退 3（不是 0）。
SYSTEMCTL_STUB = """#!/usr/bin/env bash
state="${STUB_UNIT_STATE:-inactive}"
case "${1:-}" in
    is-active) echo "$state"; [ "$state" = "active" ] && exit 0 || exit 3 ;;
    *) exit 0 ;;
esac
"""


def _read(rel):
    """读一个文件（相对检出根；传绝对路径时 `os.path.join` 原样返回该路径）。"""
    with io.open(os.path.join(BASE, rel), encoding="utf-8") as f:
        return f.read()


def _service_account():
    """服务账号 (User, Group)：唯一事实源 = web/deploy/yiban-web.service。"""
    user = group = None
    for line in _read(SERVICE_UNIT).splitlines():
        if line.startswith("User="):
            user = line.split("=", 1)[1].strip()
        elif line.startswith("Group="):
            group = line.split("=", 1)[1].strip()
    return user, group


def _uid_of(name):
    """账号名的 uid（不存在返回 None）。"""
    r = subprocess.run(["id", "-u", name], capture_output=True, text=True, timeout=60)
    return int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else None


def _modes_preserved():
    r = subprocess.run(
        [BASH, "-c", 'f=$(mktemp); chmod 700 "$f"; stat -c %a "$f"; rm -f "$f"'],
        capture_output=True, text=True, timeout=60)
    return r.stdout.strip() == "700"


@unittest.skipIf(BASH is None, "需要 bash（Git Bash/WSL）")
class _LayoutBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="runtime-data-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env = _clean_subprocess_env()

    def _run(self, script, env=None, cwd=BASE, prefix=()):
        e = dict(self.env)
        e.update(env or {})
        return subprocess.run([*prefix, BASH, script], capture_output=True,
                              env=e, cwd=cwd, timeout=300)

    def _out(self, r):
        return ((r.stdout or b"") + (r.stderr or b"")).decode("utf-8", errors="replace")

    def _root_ok(self):
        return _current_uid() == 0 and _modes_preserved()


class DataDirCreatedByInstallerTest(_LayoutBase):
    """`data/` 由 install.sh 建立：属服务账号、0700；且 M01 门照旧放行。"""

    def setUp(self):
        super().setUp()
        # 合成检出：install.sh 只看 manifest 的源是否存在 + 跑 cron 来源断言。
        self.checkout = os.path.join(self.tmp, "checkout")
        os.makedirs(os.path.join(self.checkout, "scripts"))
        os.makedirs(os.path.join(self.checkout, "deploy", "prod"))
        shutil.copy2(INSTALL, os.path.join(self.checkout, "deploy", "prod", "install.sh"))
        self.dest = "/tmp/yiban-data-gate-%s/payload.sh" % os.path.basename(self.tmp)
        _write(os.path.join(self.checkout, "deploy", "prod", "manifest.tsv"),
               "0700\tscripts/payload.sh\t%s\n" % self.dest)
        _write(os.path.join(self.checkout, "scripts", "payload.sh"), "#!/bin/bash\ntrue\n")
        _write(os.path.join(self.checkout, "scripts", "check-cron-provenance.sh"),
               "#!/usr/bin/env bash\nexit 0\n")
        _write(os.path.join(self.checkout, "scripts", "check-deps-lock.sh"),
               "#!/usr/bin/env bash\nexit 0\n")
        _write(os.path.join(self.checkout, "requirements.lock"), "flask==3.1.3\n")
        venv_bin = os.path.join(self.checkout, ".venv", "bin")
        os.makedirs(venv_bin)
        _write(os.path.join(venv_bin, "python3"), "#!/bin/sh\nexit 0\n", 0o755)
        _write(os.path.join(venv_bin, "pip"), "#!/bin/sh\nexit 0\n", 0o755)
        self.data = os.path.join(self.checkout, DATA_DIR)

    def _install(self, extra_env=None):
        return self._run(os.path.join(self.checkout, "deploy", "prod", "install.sh"),
                         env=extra_env, cwd=self.checkout)

    def _service_ids(self):
        """单元文件声明的服务账号的 (uid, gid)；账号在本机不存在时返回 None。"""
        user, group = _service_account()
        if not user or not group:
            return None
        uid = _uid_of(user)
        if uid is None:
            return None
        r = subprocess.run(["id", "-g", group], capture_output=True, text=True, timeout=60)
        if r.returncode != 0 or not r.stdout.strip().isdigit():
            return None
        return uid, int(r.stdout.strip())

    def _skip_unless_usable(self):
        if not self._root_ok():
            self.skipTest("需要 root（WSL）+ 保留 POSIX mode 的文件系统")
        ids = self._service_ids()
        if ids is None:
            self.skipTest("本机没有单元文件声明的服务账号（install -o 会失败）")
        return ids

    def test_root_install_creates_service_owned_data_dir(self):
        want_uid, want_gid = self._skip_unless_usable()
        r = self._install()
        out = self._out(r)
        self.assertEqual(r.returncode, 0, f"install.sh 必须过 M01 门并建 data/：{out}")
        self.assertTrue(os.path.isdir(self.data), f"install.sh 未建 data/：{out}")
        st = os.stat(self.data)
        self.assertEqual(st.st_uid, want_uid, "data/ 属主必须是服务账号")
        self.assertEqual(st.st_gid, want_gid, "data/ 属组必须是服务账号组")
        self.assertEqual(st.st_mode & 0o777, 0o700, "data/ 模式必须是 0700")
        # 不变式①的另一半：检出根仍属 root 且组/其他不可写。
        root_st = os.stat(self.checkout)
        self.assertEqual(root_st.st_uid, 0, "检出根必须属 root")
        self.assertEqual(root_st.st_mode & 0o022, 0, "检出根不得组/其他可写")

    def test_root_install_repairs_wrong_data_owner(self):
        """幂等修复：先被 `chown -R root:root` 带走的 data/ 必须被改回服务账号。"""
        want_uid, want_gid = self._skip_unless_usable()
        os.makedirs(self.data)
        os.chmod(self.data, 0o755)
        r = self._install()
        out = self._out(r)
        self.assertEqual(r.returncode, 0, out)
        st = os.stat(self.data)
        self.assertEqual((st.st_uid, st.st_gid, st.st_mode & 0o777),
                         (want_uid, want_gid, 0o700),
                         f"data/ 属主/模式未被修复：{oct(st.st_mode)}")

    def test_root_install_repairs_wrong_data_owner_of_contents(self):
        """幂等修复必须覆盖 `data/` 的**内容**，不只是目录本身。

        现象（本批 F1）：README 的部署/升级顺序先 `chown -R root:root .`，会把 `data/`
        连同其下的 `.env` 与库一起带走（文件仍是 `root:root 644`）。安装器此前只修目录
        ⇒ 服务写库报 `attempt to write a readonly database`，而安装器照旧 `rc=0` 报
        `complete`（静默假成功）；README 那句"重跑一次安装器即修复"对**文件内容**为假。
        容器形态的同一件事由 `docker/entrypoint.sh` 的 `chown -R yiban:yiban /data`
        承担，故裸机侧也必须递归到内容——两个形态同形。

        含一层子目录：`data/` 下不只有四个顶层文件（容器形态还会用 `data/state`、
        `data/logs`），故判据钉的是"递归到子树"，不是"逐个顶层文件"。
        """
        want_uid, want_gid = self._skip_unless_usable()
        seeded = (".env", "yiban.db", "yiban.db-wal", "yiban.db-shm",
                  os.path.join("state", "sched-run-20260101"))
        for rel in seeded:
            path = os.path.join(self.data, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            _write(path, "x\n")
            os.chown(path, 0, 0)
            os.chmod(path, 0o644)
        os.chown(self.data, 0, 0)
        os.chmod(self.data, 0o755)
        r = self._install()
        out = self._out(r)
        self.assertEqual(r.returncode, 0, out)
        self.assertEqual((os.stat(self.data).st_uid, os.stat(self.data).st_gid),
                         (want_uid, want_gid), "data/ 目录本身未被修复")
        for rel in seeded:
            path = os.path.join(self.data, rel)
            st = os.stat(path)
            self.assertEqual((st.st_uid, st.st_gid), (want_uid, want_gid),
                             f"data/{rel} 属主未被修复（服务写库会 readonly）：{out}")
            self.assertTrue(st.st_mode & 0o200,
                            f"data/{rel} 服务账号不可写（mode={oct(st.st_mode)}）")

    def test_group_writable_checkout_is_refused_and_data_not_created(self):
        """门拒装时不得在检出里留下任何新目录（fail-closed，别把执行点搬门外）。"""
        if not self._root_ok():
            self.skipTest("需要 root（WSL）+ 保留 POSIX mode 的文件系统")
        os.chmod(self.checkout, 0o775)
        r = self._install()
        out = self._out(r)
        self.assertNotEqual(r.returncode, 0, f"组可写检出必须拒装：{out}")
        self.assertFalse(os.path.exists(self.data),
                         "M01 拒装时不得建 data/（门必须整段在前）")


class ServiceRunningGuardTest(DataDirCreatedByInstallerTest):
    """服务在跑时安装器必须拒装（本批 F2）。

    现象：`install.sh` 此前完全不看服务是否在跑（README 只写"务必先停服"，无门）。
    违反后：旧代码服务按旧路径另建空库；下一次 `install.sh` 因"两侧并存"拒装，并建议
    "删掉旧布局那一份（保留 `data/` 下的）" ⇒ 按提示做就丢掉跑飞服务刚写的新数据。

    钉住的不变式（任何时刻都不该被违反）：
      ① **在跑即拒**：能判定服务在跑时，安装器非 0 退出；
      ② **拒装即零改动**：判据排在写入之前——`data/` 不建、旧布局的件原地不动；
      ③ **处置可执行**：拒装输出给出"先停服"的可复制命令（裸机 ＋ 容器两形态）。
    探测量层：`systemctl is-active`（裸机权威）与 `/proc` 进程扫描（无 systemd 时兜底）。
    """

    def _stub_bin(self, state):
        """造一个只回答 `is-active` 的 systemctl 桩目录（PATH 前置用）。"""
        d = os.path.join(self.tmp, "stub-bin-%s" % state)
        os.makedirs(d, exist_ok=True)
        _write(os.path.join(d, "systemctl"), SYSTEMCTL_STUB, 0o755)
        return d

    def _seed_root_files(self):
        bodies = {".env": "YIBAN_SECRET_KEY=legacy\n", "yiban.db": "legacy-db\n"}
        for name, text in bodies.items():
            _write(os.path.join(self.checkout, name), text)
        return bodies

    def test_service_active_is_refused_before_any_mutation(self):
        """systemd 说服务在跑 ⇒ 拒装，且 `data/` 不建、旧布局的件不动。"""
        self._skip_unless_usable()
        bodies = self._seed_root_files()
        stub = self._stub_bin("active")
        r = self._install(extra_env={"PATH": stub + os.pathsep + os.environ["PATH"],
                                     "STUB_UNIT_STATE": "active"})
        out = self._out(r)
        self.assertNotEqual(r.returncode, 0, f"服务在跑必须拒装：{out}")
        self.assertFalse(os.path.exists(self.data),
                         f"拒装时不得建 data/（判据必须在写入之前）：{out}")
        for name, text in bodies.items():
            self.assertEqual(self._read_root(name), text,
                             f"拒装时不得动旧布局的 {name}")

    def test_running_process_from_checkout_is_refused(self):
        """无 systemd（容器形态）时兜底：命令行含本检出的活进程 ⇒ 拒装。

        这一层是给"裸机"与"容器"两形态共用的：容器里没有 systemd，进程扫描仍有效。
        """
        self._skip_unless_usable()
        bodies = self._seed_root_files()
        holder = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)", self.checkout],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: holder.wait(timeout=30))
        self.addCleanup(holder.kill)
        self._wait_for_cmdline(holder.pid, self.checkout)
        stub = self._stub_bin("inactive")
        r = self._install(extra_env={"PATH": stub + os.pathsep + os.environ["PATH"],
                                     "STUB_UNIT_STATE": "inactive"})
        out = self._out(r)
        self.assertNotEqual(r.returncode, 0, f"检出内仍有活进程时必须拒装：{out}")
        self.assertFalse(os.path.exists(self.data), f"拒装时不得建 data/：{out}")
        for name, text in bodies.items():
            self.assertEqual(self._read_root(name), text, f"拒装时不得动 {name}")

    def test_refusal_gives_runnable_remedy_for_both_forms(self):
        """拒装输出必须给出可执行的处置（裸机 systemctl 与容器 compose 各一条）。"""
        self._skip_unless_usable()
        self._seed_root_files()
        stub = self._stub_bin("active")
        r = self._install(extra_env={"PATH": stub + os.pathsep + os.environ["PATH"],
                                     "STUB_UNIT_STATE": "active"})
        out = self._out(r)
        self.assertNotEqual(r.returncode, 0, out)
        self.assertIn("systemctl stop yiban-web", out, f"缺裸机处置命令：{out}")
        self.assertIn("docker compose stop yiban", out, f"缺容器处置命令：{out}")

    def test_staged_install_is_exempt(self):
        """`DESTDIR` 暂存安装不碰现网运行数据，故不受本门约束（显式豁免，有门钉住）。

        豁免的理由是结构性的：带 `DESTDIR` 时 `data/` 段与依赖同步段整段跳过，安装器
        不写任何现网运行数据 ⇒ F2 那条危害（搬动库时服务另建空库）不可能发生。
        """
        self._skip_unless_usable()
        self._seed_root_files()
        stub = self._stub_bin("active")
        r = self._install(extra_env={"PATH": stub + os.pathsep + os.environ["PATH"],
                                     "STUB_UNIT_STATE": "active",
                                     "DESTDIR": os.path.join(self.tmp, "stage")})
        out = self._out(r)
        self.assertEqual(r.returncode, 0, f"DESTDIR 暂存安装必须照旧放行：{out}")

    def test_installer_service_unit_matches_shipped_unit_template(self):
        """安装器探测的单元名必须等于仓内随附的单元模板文件名。

        同一事实（单元名）写在两处就是"同一事实多处定义"——权威件是模板文件名，
        字面量由用例钉住相等（先例同 `SERVICE_USER`/`SERVICE_GROUP` 那条）。
        """
        want = os.path.basename(SERVICE_UNIT)
        text = _read(os.path.join("deploy", "prod", "install.sh"))
        m = re.search(r'^SERVICE_UNIT="([^"]+)"', text, re.M)
        self.assertIsNotNone(m, "install.sh 里找不到 SERVICE_UNIT 字面量")
        self.assertEqual(m.group(1), want,
                         "install.sh 探测的单元名与 web/deploy/ 下的模板文件名不同")

    # ---- 辅助 ----

    def _read_root(self, name):
        with io.open(os.path.join(self.checkout, name), encoding="utf-8") as f:
            return f.read()

    def _wait_for_cmdline(self, pid, needle):
        """等子进程 exec 完（/proc/<pid>/cmdline 出现 needle），超时即失败。"""
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                with io.open("/proc/%d/cmdline" % pid, "rb") as f:
                    if needle.encode() in f.read():
                        return
            except OSError:
                pass
            time.sleep(0.05)
        self.fail("子进程 %d 的 cmdline 未出现 %s（兜底层无法被测到）" % (pid, needle))


class ServiceWritableSurfaceTest(_LayoutBase):
    """服务账号的写入面与检出根分离：写得到 data/，写不进检出根。"""

    def _prefix(self):
        if _current_uid() != 0:
            return []
        if SETPRIV is None:
            return None
        return [SETPRIV, "--reuid=%d" % UNPRIVILEGED_ID,
                "--regid=%d" % UNPRIVILEGED_ID, "--clear-groups"]

    def test_service_account_can_write_env_and_db_only_inside_data(self):
        if not self._root_ok():
            self.skipTest("需要 root（WSL）+ 保留 POSIX mode 的文件系统")
        prefix = self._prefix()
        if prefix is None:
            self.skipTest("需要 setpriv 造非 root 位格")
        co = os.path.join(self.tmp, "co")
        data = os.path.join(co, DATA_DIR)
        os.makedirs(data)
        # 临时根默认 0700 root：降到 nobody 后连路径都进不去，故放开遍历位。
        os.chmod(self.tmp, 0o755)
        os.chown(co, 0, 0)
        os.chmod(co, 0o755)
        os.chown(data, UNPRIVILEGED_ID, UNPRIVILEGED_ID)
        os.chmod(data, 0o700)
        # 服务账号：写 .env 与库（含 SQLite 的 -wal/-shm 同目录创建）成功。
        r = subprocess.run(
            [*prefix, BASH, "-c",
             'set -e; printf "YIBAN_X=1\\n" > "$1/.env"; '
             ': > "$1/yiban.db"; : > "$1/yiban.db-wal"; : > "$1/yiban.db-shm"',
             "sh", data],
            capture_output=True, timeout=60)
        self.assertEqual(r.returncode, 0,
                         "服务账号必须能写 data/ 下的 .env 与库：" + self._out(r))
        # 反向：写不进检出根（旧布局正是靠这里可写才互斥）。
        r = subprocess.run(
            [*prefix, BASH, "-c", ': > "$1/should-not-exist"', "sh", co],
            capture_output=True, timeout=60)
        self.assertNotEqual(r.returncode, 0, "服务账号不得能写检出根")
        self.assertFalse(os.path.exists(os.path.join(co, "should-not-exist")))

    def test_mutation_root_owned_data_dir_blocks_the_service(self):
        """守卫有牙：把 data/ 属主改成 root（＝搬迁失败）时，服务写不进去。"""
        if not self._root_ok():
            self.skipTest("需要 root（WSL）+ 保留 POSIX mode 的文件系统")
        prefix = self._prefix()
        if prefix is None:
            self.skipTest("需要 setpriv 造非 root 位格")
        co = os.path.join(self.tmp, "co")
        data = os.path.join(co, DATA_DIR)
        os.makedirs(data)
        os.chmod(self.tmp, 0o755)
        os.chmod(co, 0o755)
        os.chmod(data, 0o700)  # 属主 = root（变异体：漏了 chown）
        r = subprocess.run(
            [*prefix, BASH, "-c", ': > "$1/.env"', "sh", data],
            capture_output=True, timeout=60)
        self.assertNotEqual(r.returncode, 0, "data/ 属 root 时服务必须写不进（守卫有牙）")


class RuntimeDataCarriersTest(_LayoutBase):
    """③ 载体不落单：Python 默认值、名册缺省、生产树的路径表达式全落在 data/。"""

    _PROBE = r"""
import json, os, sys, types
base = sys.argv[1]
sys.path.insert(0, base)
sys.path.insert(0, os.path.join(base, "scripts"))
import importlib.util
from yiban.infra import env_io
from yiban.store import connection, db
out = {"env_path": env_io.env_path(),
       "connection.DB_DEFAULT": connection.DB_DEFAULT,
       "db.DB_DEFAULT": db.DB_DEFAULT}
spec = importlib.util.spec_from_file_location("webapp_probe",
                                              os.path.join(base, "web", "app.py"))
m = importlib.util.module_from_spec(spec)
sys.modules["webapp_probe"] = m
spec.loader.exec_module(m)
out["web.ENV_DEFAULT"] = m.ENV_DEFAULT
out["web.DB_DEFAULT"] = m.DB_DEFAULT


def _observed_db_default(call):
    '''调用一次 call，返回它传给 resolve_path 的 YIBAN_DB_FILE 默认实参。'''
    seen = []
    real = env_io.resolve_path

    def spy(key, default, **kw):
        if key == "YIBAN_DB_FILE":
            seen.append(default)
        return real(key, default, **kw)

    env_io.resolve_path = spy
    try:
        call()
    finally:
        env_io.resolve_path = real
    return seen[-1] if seen else None


# 两个在役读者把默认实参写在函数体内，模块级看不到"解析后的默认值"：用 spy 观察
# 它们**实际传给 resolve_path 的默认值**（＝它们各自持有的那一份默认值）。
import ledger_check
out["ledger_check.db_default"] = _observed_db_default(
    lambda: ledger_check._reconcile(
        types.SimpleNamespace(day="2000-01-01", all_days=None)))
from yiban.store import purge_guard
out["purge_guard.db_default"] = _observed_db_default(
    lambda: purge_guard.write_purge_audit("probe", "t", "d"))
print(json.dumps(out))
"""

    def test_python_defaults_point_into_data(self):
        """两处都没设键时，引擎侧与 web 侧的 .env/库默认值必须落在 data/。"""
        probe = os.path.join(self.tmp, "probe.py")
        _write(probe, self._PROBE)
        env = _clean_subprocess_env()
        env["PYTHONPATH"] = os.pathsep.join(
            [BASE, os.path.join(BASE, "scripts"), env.get("PYTHONPATH", "")])
        # 台账对账先查状态目录是否存在，不存在就不解析库路径；给一个存在的目录让它走到
        # 库路径解析那一步（库文件不存在 ⇒ 它在解析后即返回，不建库、无副作用）。
        env["YIBAN_STATE_DIR"] = self.tmp
        # 用本进程的解释器（门禁 venv），PATH 上的 python3 未必装了 portalocker。
        r = subprocess.run([sys.executable, probe, BASE],
                           capture_output=True, env=env, cwd=self.tmp, timeout=120)
        out = self._out(r)
        self.assertEqual(r.returncode, 0, f"探针失败：{out}")
        got = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(got["env_path"], DEFAULT_ENV, f"env_path 默认值：{got}")
        self.assertEqual(got["connection.DB_DEFAULT"], DEFAULT_DB, f"库默认值：{got}")
        self.assertEqual(got["db.DB_DEFAULT"], DEFAULT_DB, f"库默认值（门面）：{got}")
        self.assertEqual(got["web.ENV_DEFAULT"], DEFAULT_ENV, f"web .env 默认值：{got}")
        self.assertEqual(got["web.DB_DEFAULT"], DEFAULT_DB, f"web 库默认值：{got}")
        # 两个在役读者：台账对账（scripts/ledger_check.py）与清库留痕
        # （yiban/store/purge_guard.py）各持一份默认值，必须与单点同值。
        self.assertEqual(got["ledger_check.db_default"], DEFAULT_DB,
                         f"ledger_check 库默认值：{got}")
        self.assertEqual(got["purge_guard.db_default"], DEFAULT_DB,
                         f"purge_guard 库默认值：{got}")

    def test_registry_defaults_point_into_data(self):
        reg = json.loads(_read(REGISTRY))
        self.assertEqual(reg["YIBAN_ENV_FILE"]["default"], DEFAULT_ENV)
        self.assertEqual(reg["YIBAN_DB_FILE"]["default"], DEFAULT_DB)

    def test_installer_service_account_matches_unit(self):
        """install.sh 里 `data/` 的服务账号必须等于单元文件的 User=/Group=。

        同一事实（服务账号）写在两处就是"同一事实多处定义"——先例同
        `deploy/prod/tmpfiles.d/yiban.conf`：权威件是单元文件，字面量由用例钉住相等。
        `DataDirCreatedByInstallerTest` 另从**行为**上钉住同一件事（data/ 属主的 uid/gid
        就是单元文件那个账号的）。
        """
        user, group = _service_account()
        self.assertTrue(user and group, "yiban-web.service 缺 User=/Group=")
        text = _read(os.path.join("deploy", "prod", "install.sh"))
        self.assertIn(f'SERVICE_USER="{user}"', text,
                      "install.sh 的 data/ 属主账号必须与单元文件 User= 同值")
        self.assertIn(f'SERVICE_GROUP="{group}"', text,
                      "install.sh 的 data/ 属组必须与单元文件 Group= 同值")

    def test_carriers_all_point_into_data(self):
        """盘查结果的可执行版：每个载体里那个路径表达式必须落在 data/。"""
        for rel, needles in CARRIER_EXPECT.items():
            text = _read(rel.replace("/", os.sep))
            for needle in needles:
                self.assertIn(needle, text, f"{rel} 里缺 data/ 形态：{needle}")

    def test_no_carrier_still_points_at_the_checkout_root(self):
        for rel in CARRIER_EXPECT:
            text = _read(rel.replace("/", os.sep))
            for bad in FORBIDDEN_SHAPES:
                self.assertNotIn(bad, text, f"{rel} 仍指向检出根：{bad}")

    def test_scan_detects_reintroduced_old_literal(self):
        """活体反例：把旧形态放回去，扫描器必须命中（门本身可失败）。"""
        d = os.path.join(self.tmp, "planted")
        os.makedirs(d)
        _write(os.path.join(d, "run.sh"), 'ENV_PATH="$APP_DIR/.env"\n')
        with io.open(os.path.join(d, "run.sh"), encoding="utf-8") as f:
            planted = f.read()
        hits = [b for b in FORBIDDEN_SHAPES if b in planted]
        self.assertTrue(hits, "扫描器抓不住旧形态 = 门是空的")


#: 旧布局留在检出根的运行数据。集合由搬迁提交（a5bc16b7）的默认值改动决定：
#: 只有 `YIBAN_ENV_FILE` 与 `YIBAN_DB_FILE` 两枚默认值从检出根改到 data/，
#: 故旧件就是 `.env` 与库本体连同 SQLite 的同目录 `-wal`/`-shm`。
#: `accounts.json` 的默认值**未动**（仍是检出根的 `accounts.json`），不属本集合。
LEGACY_RUNTIME_FILES = (".env", "yiban.db", "yiban.db-wal", "yiban.db-shm")


class LegacyRuntimeDataAdoptionTest(DataDirCreatedByInstallerTest):
    """升级既有安装：旧布局（检出根）的运行数据必须由 install.sh 迁入 data/。

    现象：布局搬迁把 `.env`/库的默认值改到 `data/` 下，但**既有安装**的这两件仍在
    检出根。升级后服务按新默认值去 `data/` 找——找不到就自建一份空库、读不到 `.env`
    里的密钥与管理员口令，页面显示零账号、备份把空库当成功。故升级路径必须把旧件
    迁入，且迁入幂等。

    钉住的不变式（任何时刻都不该被违反）：
      ① **数据同一性**：升级后 `data/` 下的 `.env` 与库＝升级前检出根那一份（逐字节
         相同），且旧位置不再有它（否则权威分叉：服务读 data/，运维改检出根）；
      ② **迁入件属服务账号**：服务要接着写这两件（含 SQLite 的 `-wal`/`-shm`）；
      ③ **幂等**：已是新布局时重跑不改任何数据；
      ④ **两侧并存即拒**：同名件同时存在于检出根与 data/ 时权威未知 ⇒ fail-closed，
         且不覆盖任何一侧（先例同 manifest 的校验和漂移拒装）。
    """

    def _seed_legacy(self, bodies=None):
        bodies = bodies or {
            ".env": "YIBAN_ADMIN_USER=admin\nYIBAN_SECRET_KEY=legacy-key\n",
            "yiban.db": "SQLite format 3\x00 legacy-db body\n",
            "yiban.db-wal": "legacy-wal body\n",
            "yiban.db-shm": "legacy-shm body\n",
        }
        for name, text in bodies.items():
            _write(os.path.join(self.checkout, name), text)
        return bodies

    def _read_text(self, *parts):
        with io.open(os.path.join(*parts), encoding="utf-8") as f:
            return f.read()

    def test_root_install_adopts_legacy_env_and_db(self):
        want_uid, want_gid = self._skip_unless_usable()
        bodies = self._seed_legacy()
        r = self._install()
        out = self._out(r)
        self.assertEqual(r.returncode, 0, f"升级安装必须放行：{out}")
        for name, text in bodies.items():
            new = os.path.join(self.data, name)
            self.assertTrue(os.path.exists(new), f"data/{name} 未迁入：{out}")
            self.assertEqual(self._read_text(new), text, f"data/{name} 内容被改动")
            self.assertFalse(os.path.exists(os.path.join(self.checkout, name)),
                             f"旧位置的 {name} 必须已迁走（否则权威分叉）")
            st = os.stat(new)
            self.assertEqual((st.st_uid, st.st_gid), (want_uid, want_gid),
                             f"data/{name} 属主必须是服务账号")

    def test_adoption_is_idempotent(self):
        self._skip_unless_usable()
        bodies = self._seed_legacy()
        r1 = self._install()
        self.assertEqual(r1.returncode, 0, self._out(r1))
        r2 = self._install()
        self.assertEqual(r2.returncode, 0, f"第二轮必须照旧通过：{self._out(r2)}")
        for name, text in bodies.items():
            self.assertEqual(self._read_text(self.data, name), text,
                             f"重跑改动了 data/{name}")

    def test_both_copies_present_is_refused_without_overwriting(self):
        if not self._root_ok():
            self.skipTest("需要 root（WSL）+ 保留 POSIX mode 的文件系统")
        if self._service_ids() is None:
            self.skipTest("本机没有单元文件声明的服务账号（install -o 会失败）")
        legacy = self._seed_legacy({".env": "legacy\n"})
        os.makedirs(self.data)
        live = "live\n"
        _write(os.path.join(self.data, ".env"), live)
        r = self._install()
        out = self._out(r)
        self.assertNotEqual(r.returncode, 0, f"两侧并存必须拒装：{out}")
        self.assertEqual(self._read_text(self.checkout, ".env"), legacy[".env"],
                         "拒装时不得改动检出根那一份")
        self.assertEqual(self._read_text(self.data, ".env"), live,
                         "拒装时不得覆盖 data/ 那一份")

    def test_refusal_leaves_no_partial_migration(self):
        """不变式：冲突拒装时**一件都不许搬走**（先全量判冲突、再搬）。

        反例形状：一次循环里边走边搬时，冲突落在后一个名字（`yiban.db`）上，前一个
        名字（`.env`）就已经进了 `data/` —— 拒装却留下"半搬"状态，而下次重跑会因为
        `data/.env` 已存在而再判成冲突，部署者面对的是一个自锁的死结。
        """
        if not self._root_ok():
            self.skipTest("需要 root（WSL）+ 保留 POSIX mode 的文件系统")
        if self._service_ids() is None:
            self.skipTest("本机没有单元文件声明的服务账号（install -o 会失败）")
        bodies = self._seed_legacy({".env": "legacy-env\n", "yiban.db": "legacy-db\n"})
        os.makedirs(self.data)
        _write(os.path.join(self.data, "yiban.db"), "live-db\n")
        r = self._install()
        out = self._out(r)
        self.assertNotEqual(r.returncode, 0, f"冲突必须拒装：{out}")
        self.assertEqual(self._read_text(self.checkout, ".env"), bodies[".env"],
                         "拒装时 .env 不得已搬走（半搬状态）")
        self.assertFalse(os.path.exists(os.path.join(self.data, ".env")),
                         "拒装时 data/ 下不得出现半搬来的 .env")
        self.assertEqual(self._read_text(self.data, "yiban.db"), "live-db\n",
                         "拒装时不得覆盖 data/yiban.db")

    def test_dangling_legacy_symlink_is_adopted_not_silently_skipped(self):
        """断链也是"旧位置留着东西"：迁它，不得静默跳过。

        判据用 `-e || -L` 而不是只看 `-e`：只看 `-e` 时一枚断链会被判成"这里没有东西"
        而静默留在检出根——正是"静默"这一族缺陷的形状。迁入后它仍是链接（`chown -h`
        只改链接自身），故断言用 `islink` 而不是 `exists`。
        """
        self._skip_unless_usable()
        link = os.path.join(self.checkout, "yiban.db-wal")
        os.symlink("/nonexistent/yiban.db-wal", link)
        r = self._install()
        out = self._out(r)
        self.assertEqual(r.returncode, 0, f"断链不该让安装失败：{out}")
        self.assertFalse(os.path.lexists(link), "旧位置的断链必须已迁走")
        self.assertTrue(os.path.islink(os.path.join(self.data, "yiban.db-wal")),
                        f"断链必须原样迁入 data/（仍是指向原目标的链接）：{out}")

    def test_legacy_set_is_derived_from_the_relocated_defaults(self):
        """旧件集合必须与「默认值已落到 data/ 的配置键」同源（防"再搬迁时只改一半"）。

        推导规则：`config/registry.json` 里每个默认值形如 `data/<名字>` 的键，它在旧
        布局的落点就是检出根的 `<名字>`；库还要带上 SQLite 的同目录 `-wal`/`-shm`
        （本项目统一 WAL 模式）。日后新增第三个落到 `data/` 的默认值时本用例红——
        那时 `install.sh` 的 `LEGACY_RUNTIME_FILES` 必须同批跟上，否则升级会静默
        读空（本次修的正是这个形状的缺口）。
        """
        reg = json.loads(_read(REGISTRY))
        relocated = [v["default"] for v in reg.values()
                     if isinstance(v, dict) and isinstance(v.get("default"), str)
                     and v["default"].startswith(DATA_DIR + "/")]
        want = {os.path.basename(p) for p in relocated}
        db_name = os.path.basename(reg["YIBAN_DB_FILE"]["default"])
        want |= {db_name + "-wal", db_name + "-shm"}
        text = _read(os.path.join("deploy", "prod", "install.sh"))
        m = re.search(r"LEGACY_RUNTIME_FILES=\(([^)]*)\)", text)
        self.assertIsNotNone(m, "install.sh 里找不到 LEGACY_RUNTIME_FILES 数组")
        self.assertEqual(set(m.group(1).split()), want,
                         "install.sh 的旧件集合与「默认值已落 data/ 的键」不同源")


if __name__ == "__main__":
    unittest.main()
