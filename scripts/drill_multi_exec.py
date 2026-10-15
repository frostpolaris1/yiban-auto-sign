# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""本地多执行体演练靶场（零外联）。

**做什么**
把"多执行体怎么协调"这类性质放进一个**本地、零后果**的环境里验掉。
靶场不起 docker、不碰生产、不连真平台：它起一个本地假易班上游
（`tests/fake_yiban_server.py --no-tls`），拉起**真实的监督进程 ＋ 真实执行体子进程**
（`python -m yiban.cli sign`，即 `workers.run_worker_supervisor` →
`executor_v3` → `signin`），并用一条**传输层改写**把子进程的全部 https 请求钉回回环
假上游。协调逻辑（领取 / 收尾 / 收尸 / 换代 / 肘界）走的是真码，只有"网络去往哪里"
被改写。

**为什么要它**
真平台不给注入崩溃、不给慢执行体、不给多执行体并发、不许重跑。凡是
"执行体之间怎么协调"的性质，只能在这样的靶场里验。本靶场是全栈演练
（`scripts/drill-container.sh`）之外的一条**本地**腿：不要 docker、不要测试机、
不要改 hosts、不发布端口。

**两条不变式（本靶场的判据核心）**
- **I1**：同一账号在同一时刻不得被两个执行体持有。观测法：`sign_tasks` 一行的
  `state` 连续为 `claimed` 而 `owner` 变了、且 `epoch` 只增一代 ⇒ 发生了"没经释放的
  换手"（合法换手必先回 `pending`，epoch 至少增两代）。
- **I2**：任何时刻都不该有"无人持有且不可再领"的账号（那等于漏签）。观测法：一轮
  结束后，`state='claimed'` 的行数必须为 0——留下 `claimed` 的就是死执行体的在飞行，
  当天再没人能领到它。

**四个旋钮**
- `--executors N`：注入 `YIBAN_EXECUTORS` 清单，让 N 个执行体领同一批账号。
- `--kill-executor-at <claim|秒>`：在指定时刻对目标执行体发 SIGKILL。断言它的在飞行
  被收尸（回退待领 ＋ `epoch+1`），其余账号不重不丢。
- `--slow-executor <身份> <秒>`：让指定执行体每次领到后**卡住**指定秒数（造"上一个
  持有者还活着"的起跑竞态），据此验肘界让位方向。
- `--stale-write`：在该执行体被收尸后，用**旧 owner ＋ 旧 epoch** 发一条收尾写，
  断言被 `settle_tasks` fence 掉（0 行命中、行不变）。

**边界（必须如实说明）**
假上游的响应**是我们的模型**，不是平台事实。靶场全绿只证明"协调层自洽"，
**不产生任何协议证据**。协议正确性由别的证据面负责。

**归属**
不属生产代码：只在 `scripts/` 下、只被演练入口与它的 e2e 用例调用。

**通信**
输入：命令行旋钮与环境（解释器路径由 `scripts/drill-multi-exec.sh` 固定）。
输出：证据目录（`run.json` / `monitor.jsonl` / `mock.jsonl` / 子进程日志）与退出码
（0 = 全部判据通过；1 = 有不变式或断言失败；2 = 前提不满足）。
调用谁：`tests/fake_yiban_server.py`（假上游）、`yiban.engine.executor_v3`
（建计划）、`yiban.egress`（执行体身份）、`yiban.store.queue_store`（库直读）与
`yiban.store.db`。
谁调用：`scripts/drill-multi-exec.sh` 与 `tests/test_multi_executor_drill_e2e.py`。
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import glob
import json
import os
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO, os.path.join(REPO, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

#: 每次演练现生成的随机账号密钥（64 hex）：既不是仓库示例钥、也不是单字节重复钥，
#: 故不会命中 `account_crypto` 的两道拒绝/告警判据。
KEY = secrets.token_hex(32)
MOCK = os.path.join(REPO, "tests", "fake_yiban_server.py")

#: 采样间隔（秒）：I1 的"没经释放的换手"要求采样足够密才不误判为合法换手。
SAMPLE_SEC = 0.02

#: 传输层改写 ＋ 可选变异的 shim。作为 `sitecustomize.py` 落到 shim 目录、
#: 进 `PYTHONPATH`，被监督进程与每个执行体子进程在**解释器启动时**导入。
#:
#: 只改两件事：
#: 1. `requests.Session` 换成把 https 钉回回环假上游的子类（保留 Host 头）——
#:    结构性保证零外联，不是靠"记得别连"。
#: 2. `DRILL_MUTATE` / `DRILL_SLOW_*` 非空时，按名对协调层做变异或让位
#:    注入（只服务有牙证明，不参与生产）。
SHIM_SRC = r'''# -*- coding: utf-8 -*-
"""sitecustomize：把子进程的 https 钉回回环假上游 ＋ 可选协调层变异。"""
import os

_port = os.environ.get("DRILL_MOCK_PORT")
if _port:
    import requests
    from urllib.parse import urlsplit

    _BASE = "http://127.0.0.1:" + _port

    class _Rewrite(requests.adapters.BaseAdapter):
        def __init__(self):
            super().__init__()
            self._inner = requests.adapters.HTTPAdapter()

        def send(self, request, **kwargs):
            logical = request.url
            parts = urlsplit(request.url)
            request.headers["Host"] = parts.netloc
            request.url = _BASE + parts.path + (("?" + parts.query) if parts.query else "")
            resp = self._inner.send(request, **kwargs)
            resp.url = logical
            return resp

        def close(self):
            self._inner.close()

    _Orig = requests.Session

    class _Session(_Orig):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.mount("https://", _Rewrite())
            self.trust_env = False
            self.proxies = {}

    requests.Session = _Session


def _apply_injections():
    slow_id = os.environ.get("DRILL_SLOW_ID", "")
    slow_sec = float(os.environ.get("DRILL_SLOW_SEC", "0") or 0)
    mutate = os.environ.get("DRILL_MUTATE", "")
    # `*` = 对所有执行体生效（teeth 用：让"被抢的持有者"卡住，双持有才可被观测到）。
    if slow_id and slow_sec and not (slow_id == "*"
                                     or os.environ.get("YIBAN_EXECUTOR_ID") == slow_id):
        slow_sec = 0
    if not slow_sec and not mutate:
        return
    import time
    if slow_sec:
        import yiban.store.queue_store as qs
        _real_claim = qs.claim_batch

        def _slow_claim(owner, day, vshards, now=None, limit=1, elbow_sec=0.0, phones=None):
            rows = _real_claim(owner, day, vshards, now=now, limit=limit,
                               elbow_sec=elbow_sec, phones=phones)
            if rows:
                time.sleep(slow_sec)   # 持有在飞行，卡住——"上一个持有者还活着"
            return rows

        qs.claim_batch = _slow_claim
    if mutate == "reap-off":
        from yiban.engine import workers
        workers._reap_dead_worker = lambda slot: 0
    elif mutate == "claim-steal":
        import yiban.store.queue_store as qs
        _orig_claim = qs.claim_batch
        _stole = []            # 一次性：每个进程只抢一次，造一次干净的并发双持有

        def _steal_claim(owner, day, vshards, now=None, limit=1, elbow_sec=0.0, phones=None):
            rows = _orig_claim(owner, day, vshards, now=now, limit=limit,
                               elbow_sec=elbow_sec, phones=phones)
            if rows or _stole:
                return rows
            # 变异：无视分片与 state 门，抢任何"在飞行"的行——模拟"两台执行体都认为该行
            # 归自己"（如兜底 claim_all 与计划轮并发）。这是 I1 的反面：同一行被第二台
            # 执行体在持有者在飞时抢走（owner 变、epoch 只增一代），并因此多发一次真登录。
            try:
                cutoff = qs._shift_stamp(now or qs.clock.ts(), -float(elbow_sec or 0.0))
            except ValueError:
                return None
            sql = ("UPDATE sign_tasks SET state='claimed', owner=?, epoch=epoch+1 "
                   "WHERE (phone, day) IN (SELECT phone, day FROM sign_tasks "
                   "WHERE day=? AND state='claimed' AND run_at<=? "
                   "ORDER BY priority, run_at LIMIT ?) "
                   "RETURNING phone, run_at, attempts, epoch")
            try:
                conn, lock = qs._queue_conn()
                with lock:
                    got = conn.execute(sql, (owner, day, cutoff, int(limit))).fetchall()
                    conn.commit()
            except Exception:
                return None
            _stole.append(True)
            return [{"phone": r["phone"], "run_at": r["run_at"], "attempts": r["attempts"],
                     "epoch": r["epoch"]} for r in got]

        qs.claim_batch = _steal_claim


try:
    _apply_injections()
except Exception:
    import traceback
    traceback.print_exc()
'''


def now_dt() -> datetime.datetime:
    # 业务本地时间（naive）：与引擎 `yiban.clock` 同一口径，窗口/业务日都按北京时间。
    return datetime.datetime.now()  # noqa: DTZ005


def make_manifest(n: int) -> str:
    rows = [{"slot": i, "type": "worker", "proxy": ""} for i in range(n)]
    return json.dumps(rows, separators=(",", ":"))


def write_shim(shimdir: str) -> None:
    os.makedirs(shimdir, exist_ok=True)
    with open(os.path.join(shimdir, "sitecustomize.py"), "w", encoding="utf-8", newline="\n") as f:
        f.write(SHIM_SRC)


def start_mock(tmp: str) -> tuple[subprocess.Popen, int]:
    ready = os.path.join(tmp, "mock.ready")
    log = os.path.join(tmp, "mock.jsonl")
    proc = subprocess.Popen(
        [sys.executable, MOCK, "--no-tls", "--port", "0", "--no-ipv6",
         "--ready-file", ready, "--log", log],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    deadline = time.time() + 30
    while time.time() < deadline:
        if os.path.exists(ready) and os.path.getsize(ready):
            return proc, int(open(ready, encoding="utf-8").read().strip())
        if proc.poll() is not None:
            raise RuntimeError("假上游提前退出: " + proc.stderr.read().decode("utf-8", "replace"))
        time.sleep(0.05)
    proc.kill()
    raise RuntimeError("假上游 30s 内未就绪")


def seed(tmp: str, accounts: int, executors: int, manifest: str) -> dict:
    """造数：自造账号 ＋ 真计划（走 `executor_v3._ensure_plan`）＋ 全部行置为立即可领。"""
    env_file = os.path.join(tmp, ".env")
    dbfile = os.path.join(tmp, "yiban.db")
    state = os.path.join(tmp, "state")
    os.makedirs(state, exist_ok=True)
    t = now_dt()
    cfg_env = {
        "YIBAN_ACCOUNTS_KEY": KEY,
        "YIBAN_ENV_FILE": env_file,
        "YIBAN_DB_FILE": dbfile,
        "YIBAN_STATE_DIR": state,
        "YIBAN_LOG_FILE": os.path.join(tmp, "sign.log"),
        "YIBAN_SIGN_START": (t - datetime.timedelta(minutes=2)).strftime("%H:%M"),
        "YIBAN_SIGN_END": (t + datetime.timedelta(minutes=40)).strftime("%H:%M"),
        "YIBAN_SATURDAY_SIGN": "1", "YIBAN_SUNDAY_SIGN": "1", "YIBAN_GLOBAL_PAUSE": "0",
        "YIBAN_ACCOUNT_GAP_MAX": "0", "YIBAN_SIGN_ORDER": "sequence",
        "YIBAN_SIGN_DIST": "front", "YIBAN_WINDOW_EDGE_FRONT_SEC": "0",
        "YIBAN_WINDOW_EDGE_BACK_SEC": "0", "YIBAN_MIN_EXEC_GAP": "1",
        "YIBAN_EXEC_GAP_MIN": "0", "YIBAN_RETRY_MIN_INTERVAL": "1",
        "YIBAN_DISABLE_PURGE_LOOP": "1",
        "YIBAN_EXECUTORS": manifest,
    }
    os.environ.update(cfg_env)

    import db  # scripts/db.py 兼容壳

    from yiban.engine import executor_v3
    from yiban.engine import schedule as sched
    from yiban.infra import env_io

    # `.env` 由**持锁的写入口**建档与写入（`env_io.write_env_keys`）：裸 `open(...,"w")`
    # 会绕开 `env_write_lock`，被 `test_env_writers_take_lock` 的 grep 格判红。
    env_io.write_env_keys(env_file, cfg_env)
    # 进程内 `db` 连接可能被同 worker 的上一个用例留着（`init_db` 见到既有连接即**原样
    # 返回**，不会切到本轮的临时库）⇒ 必须先关掉，否则本轮造数写进别人的库、本轮的
    # 库根本没建 `sign_tasks`（实测：全量并发跑时 `no such table: sign_tasks`）。
    if getattr(db, "_conn", None) is not None:
        db._conn.close()
        db._conn = None
    db.init_db(dbfile, env_file=env_file, cleanup=False, migrate=True)
    for i in range(accounts):
        email = "drill%05d@mock.invalid" % i
        with contextlib.suppress(Exception):
            db.create_user(email, "mock-hash", role="user")
        with contextlib.suppress(Exception):
            db.add_account({
                "name": "drill%05d" % i, "phone": "138001380%03d" % i,
                "password": "drill-pass-%d" % i, "phone_model": "MockPhone",
                "phone_code": "", "owner": email, "status": "active", "reject_reason": "",
            })
    accounts_obj = db.load_accounts()
    cfg = sched.planner_config()
    executor_v3._ensure_plan(accounts_obj, cfg["day"] if "day" in cfg else t.strftime("%Y-%m-%d"), cfg)
    day = t.strftime("%Y-%m-%d")
    due = (t - datetime.timedelta(seconds=2)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    conn = db.get_conn()
    with db._conn_lock:
        conn.execute("UPDATE sign_tasks SET run_at=? WHERE day=?", (due, day))
        conn.commit()
    return {"env_file": env_file, "dbfile": dbfile, "state": state, "day": day,
            "cfg_env": cfg_env, "accounts": len(accounts_obj), "path": tmp}


# ---------------------------------------------------------------------------
# DB 直读（驱动进程侧，独立连接；WAL 允许与子进程并发）
# ---------------------------------------------------------------------------
def _rows(dbfile: str, day: str) -> dict[str, dict]:
    con = sqlite3.connect(dbfile, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        return {r["phone"]: dict(r) for r in con.execute(
            "SELECT phone, state, owner, epoch, result, run_at FROM sign_tasks WHERE day=?",
            (day,))}
    finally:
        con.close()


class Monitor:
    """连续采样 `sign_tasks`，按两条不变式判红。

    I1：一行的 `state` 连续为 `claimed` 而 `owner` 变了、且 `epoch` 只增一代 ⇒
    发生了"没经释放的换手"。合法换手必先回 `pending`（收尸换代）再被领（再换代），
    故 `epoch` 至少增两代；只增一代的换手意味着另一个执行体在持有者还在飞时把它抢了。
    I2：一轮结束后不得留下 `state='claimed'` 的行；留下的就是死执行体的在飞行。
    """

    def __init__(self, dbfile: str, day: str):
        self.dbfile = dbfile
        self.day = day
        self.last: dict[str, tuple] = {}
        self.i1: list[dict] = []
        self.trace: list[dict] = []
        self._n = 0

    def sample(self) -> None:
        self._n += 1
        for phone, row in _rows(self.dbfile, self.day).items():
            cur = (row["state"], row["owner"], int(row["epoch"]))
            prev = self.last.get(phone)
            if prev and prev[0] == "claimed" and cur[0] == "claimed" \
                    and cur[1] != prev[1] and cur[2] - prev[2] == 1:
                self.i1.append({"phone": phone, "prev": prev, "cur": cur,
                                "at_sample": self._n})
            if prev != cur:
                self.trace.append({"n": self._n, "phone": phone, "state": cur[0],
                                   "owner": cur[1], "epoch": cur[2]})
            self.last[phone] = cur

    def final_i2(self) -> list[dict]:
        return [{"phone": p, "row": r} for p, r in _rows(self.dbfile, self.day).items()
                if r["state"] == "claimed"]


def find_child_pids(executor_id: str) -> list[int]:
    """按 `/proc/<pid>/environ` 找持有该执行体身份的子进程（同 uid，无需 root）。"""
    target = ("YIBAN_EXECUTOR_ID=" + executor_id).encode()
    out = []
    for path in glob.glob("/proc/[0-9]*/environ"):
        try:
            with open(path, "rb") as f:
                blob = f.read()
        except OSError:
            continue
        if target in blob.split(b"\x00"):
            with contextlib.suppress(ValueError):
                out.append(int(path.split("/")[2]))
    return out


def mock_trace(mock_log: str) -> list[dict]:
    if not os.path.exists(mock_log):
        return []
    rows = []
    for line in open(mock_log, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return [r for r in rows if r.get("path") not in ("/__health", "/__stats")]


def _wait_supervisor(proc: subprocess.Popen, monitor: Monitor, kill_plan: dict,
                     deadline: float, on_hold=None, on_hold_owner: str = "") -> int | None:
    """轮询监督进程：持续采样；到点执行杀注入与"持有期探针"；返回退出码（超时 None）。

    `on_hold` / `on_hold_owner`：一旦观测到 `owner` 以 `on_hold_owner` 开头且 `state='claimed'`
    的行，就调一次 `on_hold(phone)`（慢执行体持有期探针用它跑肘界判向）。
    """
    killed = False
    probed = False
    while proc.poll() is None:
        monitor.sample()
        if on_hold and not probed:
            held = [p for p, r in _rows(monitor.dbfile, monitor.day).items()
                    if r["state"] == "claimed" and r["owner"].startswith(on_hold_owner)]
            if held:
                on_hold(held[0])
                probed = True
        if not killed and kill_plan.get("armed"):
            if kill_plan["when"] == "claim":
                target_rows = [(p, r) for p, r in _rows(monitor.dbfile, monitor.day).items()
                               if r["state"] == "claimed"
                               and r["owner"].startswith(kill_plan["owner_prefix"])]
                if target_rows:
                    pids = find_child_pids(kill_plan["executor_id"])
                    if pids:
                        phone, row = target_rows[0]
                        os.kill(pids[0], signal.SIGKILL)
                        killed = True
                        kill_plan["killed_pid"] = pids[0]
                        kill_plan["killed_phone"] = phone
                        kill_plan["killed_epoch"] = int(row["epoch"])
                        kill_plan["killed_owner"] = row["owner"]
            elif time.time() >= kill_plan["at"]:
                pids = find_child_pids(kill_plan["executor_id"])
                if pids:
                    # 与 claim 形态同口径：记下此刻该执行体名下的在飞行（供收尸/旧代写断言）。
                    held = [(p, r) for p, r in _rows(monitor.dbfile, monitor.day).items()
                            if r["state"] == "claimed"
                            and r["owner"].startswith(kill_plan["owner_prefix"])]
                    os.kill(pids[0], signal.SIGKILL)
                    killed = True
                    kill_plan["killed_pid"] = pids[0]
                    if held:
                        kill_plan["killed_phone"] = held[0][0]
                        kill_plan["killed_epoch"] = int(held[0][1]["epoch"])
                        kill_plan["killed_owner"] = held[0][1]["owner"]
        if time.time() > deadline:
            proc.kill()
            return None
        time.sleep(SAMPLE_SEC)
    for _ in range(20):   # 收尾再采几拍，捕到最后一跳
        monitor.sample()
        time.sleep(SAMPLE_SEC)
    return proc.returncode


def _spawn_supervisor(childenv: dict) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", "yiban.cli", "sign"],
                            env=childenv, cwd=REPO,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _probe_elbow(dbfile: str, day: str, held_phone: str, slow_owner: str) -> dict:
    """肘界让位判向探针：慢执行体持有在飞行时，兜底身份带 elbow=60s 必须领不到。

    用的是**真实的** `queue_store.claim_batch`（生产领取原语）：它把肘界下推成
    `run_at <= now - elbow`。行是刚到的（`run_at` 在 2 秒内），故 60s 肘界下应当空手；
    正方向由慢执行体自己用 elbow=0 领到的那一行作证。
    """
    from yiban import egress
    from yiban.store import queue_store
    rows = _rows(dbfile, day)
    shards = tuple(range(_max_shard(dbfile, day) + 1))
    fb = egress.fallback_owner()
    before = rows.get(held_phone)
    stolen = queue_store.claim_batch(fb, day, shards, elbow_sec=60.0, limit=5)
    after = _rows(dbfile, day).get(held_phone)
    return {
        "probe_owner": fb,
        "elbow_sec": 60.0,
        "fallback_claimed_rows": [] if stolen is None else [r["phone"] for r in stolen],
        "held_row_before": before,
        "held_row_after": after,
        "held_still_owned_by_slow": bool(
            after and after["state"] == "claimed" and after["owner"].startswith(slow_owner)),
        "no_yield_violation": (not stolen) and before == after,
    }


def _worker_id(slot: int) -> str:
    """槽位号 → 该槽位的稳定身份串（**单点封装** egress 的唯一构造处）。"""
    from yiban import egress
    return egress.worker_owner(slot)


def _max_shard(dbfile: str, day: str) -> int:
    con = sqlite3.connect(dbfile, timeout=10)
    try:
        row = con.execute("SELECT MAX(vshard) FROM sign_tasks WHERE day=?", (day,)).fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    finally:
        con.close()


def run_drill(*, executors: int = 2, accounts: int = 6, kill_at: str = "",
              slow: str = "", stale_write: bool = False, mutate: str = "",
              timeout_sec: float = 120, evidence_dir: str = "", tmp: str = "",
              keep_tree: bool = False, recover: bool | None = None) -> dict:
    """跑一轮靶场演练，返回结果 dict（含两条不变式的判红明细与全部证据路径）。

    `recover`：杀注入后是否再跑一轮，验"被收尸的账号在下一轮被领走"（缺省：有杀注入时开）。
    """
    own_tmp = not tmp
    tmp = tmp or tempfile.mkdtemp(prefix="drill-multi-exec-")
    manifest = make_manifest(executors)
    shimdir = os.path.join(tmp, "shim")
    write_shim(shimdir)
    env_snapshot = dict(os.environ)
    result: dict = {"executors": executors, "accounts": accounts, "mutate": mutate,
                    "kill_at": kill_at, "slow": slow, "stale_write": stale_write,
                    "tmp": tmp, "started": now_dt().isoformat()}
    try:
        mock_proc, port = start_mock(tmp)
        info = seed(tmp, accounts, executors, manifest)
        day = info["day"]
        monitor = Monitor(info["dbfile"], day)
        result.update({"day": day, "seeded_accounts": info["accounts"]})

        childenv = dict(os.environ)
        childenv.update(info["cfg_env"])
        childenv.update({
            "YIBAN_EXECUTORS": manifest,
            "PYTHONPATH": shimdir + os.pathsep + REPO,
            "DRILL_MOCK_PORT": str(port),
        })
        if mutate:
            childenv["DRILL_MUTATE"] = mutate
        # slow / kill 目标：默认第 0 槽执行体；`all`/`*` = 对所有执行体生效。
        target_id = _worker_id(0)
        slow_sec = 0.0
        if slow:
            ident, sec = ([*slow.split(":", 1), "0"])[:2] if ":" in slow else (slow, "0")
            if ident in ("all", "*", ""):
                target_id = "*"
            elif "@" in ident:
                target_id = ident
            else:
                target_id = _worker_id(int(ident))
            slow_sec = float(sec)
        kill_plan: dict = {"armed": False}
        if kill_at:
            kill_plan = {"armed": True, "executor_id": target_id,
                         "owner_prefix": target_id + ":", "killed_pid": None,
                         "killed_phone": None}
            if kill_at == "claim":
                kill_plan["when"] = "claim"
                slow_sec = slow_sec or 20.0    # 让目标持住在飞行，杀点在"claim 被观测到"时可命中
            else:
                kill_plan["when"] = "sec"
                kill_plan["at"] = time.time() + float(kill_at)
        if slow_sec:
            childenv["DRILL_SLOW_ID"] = target_id
            childenv["DRILL_SLOW_SEC"] = str(slow_sec)
        result["target_executor"] = target_id
        result["slow_sec"] = slow_sec

        # 驱动进程自己的 env 也带上（供子进程继承），且驱动不加载 shim（不加 shimdir）。
        os.environ.update(childenv)

        # 慢执行体持有期探针（肘界让位判向）：有慢、无变异时跑一次（可与杀注入同场）。
        elbow_probe: dict = {"ran": False}
        on_hold = None
        if slow_sec and not mutate:
            def on_hold(phone, _res=elbow_probe):
                _res.update({"ran": True})
                _res.update(_probe_elbow(info["dbfile"], day, phone, target_id + ":"))
        sup = _spawn_supervisor(childenv)
        result["supervisor_pid"] = sup.pid
        rc = _wait_supervisor(sup, monitor, kill_plan, time.time() + timeout_sec,
                              on_hold=on_hold, on_hold_owner=target_id + ":")
        result["supervisor_rc"] = rc
        result["timed_out"] = rc is None
        if kill_plan.get("armed"):
            result["kill"] = {k: kill_plan.get(k) for k in
                              ("executor_id", "when", "killed_pid", "killed_phone",
                               "killed_epoch", "killed_owner")}
        if elbow_probe.get("ran"):
            result["elbow_probe"] = elbow_probe

        # 迟到旧代写注入：用被杀执行体的旧 owner ＋ 旧 epoch 发收尾写。
        if stale_write:
            stale = {"attempted": False}
            killed_phone = (kill_plan or {}).get("killed_phone")
            if killed_phone:
                hist = [t for t in monitor.trace
                        if t["phone"] == killed_phone and t["state"] == "claimed"]
                if hist:
                    old_owner = hist[-1]["owner"]
                    old_epoch = hist[-1]["epoch"]
                    stale = _stale_write(info["dbfile"], day, killed_phone,
                                         old_owner, old_epoch)
            result["stale_write"] = stale

        result["i1_violations"] = monitor.i1
        result["i2_claimed_left"] = monitor.final_i2()
        rows_after_first = _rows(info["dbfile"], day)
        result["first_round_rows"] = rows_after_first
        result["first_round_pending"] = sorted(
            p for p, r in rows_after_first.items() if r["state"] == "pending")

        # 恢复轮：杀注入后是否再跑一轮，验"被收尸的账号下一轮被领走"（不丢）。
        if (recover is True) or (recover is None and kill_at):
            rec_env = {k: v for k, v in childenv.items()
                       if k not in ("DRILL_SLOW_ID", "DRILL_SLOW_SEC")}
            sup2 = _spawn_supervisor(rec_env)
            rc2 = _wait_supervisor(sup2, monitor, {"armed": False},
                                   time.time() + timeout_sec)
            result["recover_rc"] = rc2

        # 判据汇总
        rows = _rows(info["dbfile"], day)
        result["final_rows"] = rows
        result["i2_ok"] = not result["i2_claimed_left"]
        result["i1_ok"] = not result["i1_violations"]
        trace = mock_trace(os.path.join(tmp, "mock.jsonl"))
        result["mock_requests"] = len(trace)
        result["mock_signin_count"] = sum(
            1 for r in trace if r.get("path", "").endswith("/signIn"))
        result["done_count"] = sum(1 for r in rows.values() if r["state"] == "done")
        # 假上游按隐私口径**不记手机号**（见 fake_yiban_server 的 record）：故"同一账号
        # 被签了几次"不能按行分账号数。用**总次数 − 账号数**当超额重复登录的观测量：
        # 正常一轮（＋恢复轮）每账号恰好一次 ⇒ 超额 ≤ 0；变异放行并发双持有 ⇒ 超额 > 0。
        result["signin_excess"] = result["mock_signin_count"] - len(rows)
        result["terminal_or_pending"] = all(r["state"] in ("done", "skipped", "pending", "failed")
                                            for r in rows.values())

        # 证据落盘
        if evidence_dir:
            os.makedirs(evidence_dir, exist_ok=True)
            with open(os.path.join(evidence_dir, "monitor.jsonl"), "w", encoding="utf-8") as f:
                for t in monitor.trace:
                    f.write(json.dumps(t, ensure_ascii=False) + "\n")
            if os.path.exists(os.path.join(tmp, "mock.jsonl")):
                shutil.copy(os.path.join(tmp, "mock.jsonl"),
                            os.path.join(evidence_dir, "mock.jsonl"))
            for name in os.listdir(os.path.join(info["state"])):
                src = os.path.join(info["state"], name)
                if os.path.isfile(src):
                    shutil.copy(src, os.path.join(evidence_dir, "state-" + name))
            for p in glob.glob(os.path.join(tmp, "*.log")):
                shutil.copy(p, os.path.join(evidence_dir, os.path.basename(p)))
            out, err = sup.communicate()
            with open(os.path.join(evidence_dir, "supervisor.out"), "wb") as f:
                f.write(out or b"")
                f.write(b"\n--- stderr ---\n")
                f.write(err or b"")
            with open(os.path.join(evidence_dir, "run.json"), "w", encoding="utf-8") as f:
                json.dump({k: v for k, v in result.items()
                           if k not in ("final_rows",)}, f, ensure_ascii=False, indent=2)
            result["evidence_dir"] = evidence_dir

        mock_proc.terminate()
        try:
            mock_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            mock_proc.kill()
        return result
    finally:
        # 驱动器进程的库连接与进程环境**必须复原**：本函数会被同进程的多个用例连着调
        # （pytest 用），残留的连接会把下个用例指向上一次的临时库，残留的环境键会
        # 让别的用例读到本靶场的窗口/清单。
        try:
            import db as _db
            if getattr(_db, "_conn", None) is not None:
                _db._conn.close()
                _db._conn = None
        except Exception:
            pass
        os.environ.clear()
        os.environ.update(env_snapshot)
        if own_tmp and not keep_tree:
            shutil.rmtree(tmp, ignore_errors=True)
        elif own_tmp:
            result["tmp_kept"] = tmp


def _stale_write(dbfile: str, day: str, phone: str, old_owner: str, old_epoch: int) -> dict:
    """用旧 owner ＋ 旧 epoch 发一条收尾写，返回是否被 fence（0 行命中 ＋ 行不变）。"""
    from yiban.store import queue_store
    before = _rows(dbfile, day).get(phone)
    n = queue_store.settle_tasks(old_owner, day, [(phone, "迟到旧代写")],
                                 state=queue_store.STATE_DONE, epochs={phone: old_epoch})
    after = _rows(dbfile, day).get(phone)
    return {"attempted": True, "phone": phone, "old_owner": old_owner,
            "old_epoch": old_epoch, "rows_affected": n,
            "before": before, "after": after,
            "fenced": (n == 0 and before == after)}


def _cli() -> int:
    ap = argparse.ArgumentParser(description="本地多执行体演练靶场（零外联）")
    ap.add_argument("--executors", type=int, default=2)
    ap.add_argument("--accounts", type=int, default=6)
    ap.add_argument("--kill-executor-at", default="", metavar="claim|SEC")
    ap.add_argument("--slow-executor", default="", metavar="IDENTITY:SEC")
    ap.add_argument("--stale-write", action="store_true")
    ap.add_argument("--mutate", default="", choices=["", "reap-off", "claim-steal"])
    ap.add_argument("--timeout-sec", type=float, default=120)
    ap.add_argument("--evidence-dir", default="")
    ap.add_argument("--keep-tree", action="store_true")
    ap.add_argument("--no-recover", action="store_true",
                    help="杀注入后不再跑恢复轮")
    args = ap.parse_args()
    res = run_drill(executors=args.executors, accounts=args.accounts,
                    kill_at=args.kill_executor_at, slow=args.slow_executor,
                    stale_write=args.stale_write, mutate=args.mutate,
                    timeout_sec=args.timeout_sec, evidence_dir=args.evidence_dir,
                    keep_tree=args.keep_tree,
                    recover=False if args.no_recover else None)

    checks = {
        "i1": res.get("i1_ok"),
        "i2": res.get("i2_ok"),
        "all_accounted": res.get("terminal_or_pending"),
        "not_timed_out": not res.get("timed_out"),
        "no_extra_signin": (res.get("signin_excess", 1) <= 0),
    }
    kill = res.get("kill") or {}
    if args.kill_executor_at:
        checks["kill_fired"] = kill.get("killed_pid") is not None
        if args.stale_write:
            checks["stale_fenced"] = bool((res.get("stale_write") or {}).get("fenced"))
        if not args.no_recover:
            checks["recovered_all"] = res.get("done_count") == args.accounts
    elif args.slow_executor:
        probe = res.get("elbow_probe") or {}
        checks["elbow_probe_ran"] = bool(probe.get("ran"))
        checks["elbow_no_yield_violation"] = bool(probe.get("no_yield_violation"))
    ok = all(bool(v) for v in checks.values())
    shown = {k: res[k] for k in (
        "executors", "accounts", "supervisor_rc", "recover_rc", "done_count",
        "mock_requests", "mock_signin_count", "signin_excess", "i1_ok", "i2_ok",
        "i1_violations", "i2_claimed_left", "terminal_or_pending", "first_round_pending",
        "kill", "stale_write", "elbow_probe", "mutate") if k in res}
    shown["checks"] = checks
    print(json.dumps(shown, ensure_ascii=False, indent=2))
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_cli())
