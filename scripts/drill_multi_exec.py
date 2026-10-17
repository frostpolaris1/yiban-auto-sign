# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""本地多执行体演练靶场（零外联）。

**做什么**
把"多执行体怎么协调"这类性质放进一个**本地、零后果**的环境里验掉。
靶场不起 docker、不碰生产、不连真平台：它起一个本地假易班上游
（`yiban/adapters/protocol_fake/__init__.py --no-tls`），拉起**真实的监督进程 ＋ 真实执行体子进程**
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

**一条前提（缺了它，上面几条都测不到东西）**
账号集必须**铺满每个执行体**。账号归谁由 HRW 纯函数 `H(手机号 ‖ 日期 ‖ 执行体名)`
决定，而执行体名含**主机名**，故"哪个执行体分到账号"随 (主机名, 日期) 变。实测
（口径：200 个合成 runner 主机名 × 31 天 ＝ 6200 对）**14.69% 的对会让某个执行体一件活
都没有**，其中 7.21% 是"槽位 0 零活"、7.48% 是"槽位 0 全包"。2026-10-17 本机实测即前
者：4 个固定号的虚分片 32/38/52/56 全落在槽位 1 的 31 个分片里，槽位 0 零领取 ⇒ 屏障
恒不就绪 ⇒ 用例红在"前提没搭起来"。故造号由 `spread_phones` 按**同一日的 HRW 归属
反查**，不挑号碰运气（红绿随主机名与日期漂移 = 日历/主机敏感，与负载无关）。

**边界（必须如实说明）**
假上游的响应**是我们的模型**，不是平台事实。靶场全绿只证明"协调层自洽"，
**不产生任何协议证据**。协议正确性由别的证据面负责。

**归属**
不属生产代码：只在 `scripts/` 下、只被演练入口与它的 e2e 用例调用。

**通信**
输入：命令行旋钮与环境（解释器路径由 `scripts/drill-multi-exec.sh` 固定）。
输出：证据目录（`run.json` / `monitor.jsonl` / `mock.jsonl` / 子进程日志）与退出码
（0 = 全部判据通过；1 = 有不变式或断言失败；2 = 前提不满足）。
调用谁：`yiban/adapters/protocol_fake/__init__.py`（假上游）、`yiban.engine.executor_v3`
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
MOCK = os.path.join(REPO, "yiban", "adapters", "protocol_fake", "__init__.py")

#: 采样间隔（秒）：I1 的"没经释放的换手"要求采样足够密才不误判为合法换手。
SAMPLE_SEC = 0.02

#: 持有闸门的硬上限（秒）。闸门是"等驱动动手"的：正常一两拍就放行。给个上限是**兜底**——
#: 驱动那一手若没落地（最典型：`find_child_pids` 没找到 pid ⇒ 杀注入放空枪），持有者不能
#: 一直卡到用例超时。**与用例的 `timeout_sec` 无关**：卡满一次就会吃掉 CI 作业的整份预算，
#: 表现成"作业被取消"，真因被藏住（实测踩到）。
HOLD_MAX_SEC = 45.0

#: 等"目标执行体领到第一行"的预算（秒）。这个等的是**执行体启动**，不是驱动——
#: CI 里 4 个 pytest 工作进程同抢 2~4 核，执行体起来并走到第一次领取可以很慢
#: （实测 45 秒不够：取证显示 45 秒时全部行仍是 `pending`、连执行体进程都还没出现）。
#: 故这一档给足；真正的"防卡死"由 HOLD_MAX_SEC（一次性、45 秒）负责。
BARRIER_WAIT_SEC = 150.0

#: 传输层改写 ＋ 可选变异的 shim。作为 `sitecustomize.py` 落到 shim 目录、
#: 进 `PYTHONPATH`，被监督进程与每个执行体子进程在**解释器启动时**导入。
#:
#: 只改两件事：
#: 1. `requests.Session` 换成把 https 钉回回环假上游的子类（保留 Host 头）——
#:    结构性保证零外联，不是靠"记得别连"。
#: 2. `DRILL_MUTATE` / `DRILL_SLOW_*` 非空时，按名对协调层做变异或让位
#:    注入（只服务有牙证明，不参与生产）。
SHIM_SRC = r'''# -*- coding: utf-8 -*-
"""sitecustomize：把子进程的 https 钉回回环假上游 ＋ 可选协调层变异 ＋ 持有闸门。

注入面只服务靶场的**有牙证明**，不参与生产。每处变异都打 `_drill` 标记，供
哨兵进程断言"变异真的贴上了"——红的时候必须先分清"变异没贴"还是"不变式没破"。

持有闸门（`DRILL_HOLD_*`）：命中目标执行体在**领到一行之后**写就绪标记、再等放行
标记。驱动侧据此确定性地制造"持有者在飞"这一状态，不靠调度运气。
"""
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


def _slot_of_self():
    eid = os.environ.get("YIBAN_EXECUTOR_ID", "")
    if not eid.startswith("worker-"):
        return None
    return eid.split("-", 1)[1].split("@", 1)[0]


def _hold_target():
    want = os.environ.get("DRILL_HOLD_SLOT", "")
    if not want:
        return False
    if want == "*":
        return True
    return _slot_of_self() == want


def _apply_injections():
    import time
    slow_id = os.environ.get("DRILL_SLOW_ID", "")
    slow_sec = float(os.environ.get("DRILL_SLOW_SEC", "0") or 0)
    mutate = os.environ.get("DRILL_MUTATE", "")
    hold = _hold_target()
    if not slow_sec and not mutate and not hold:
        return
    import yiban.store.queue_store as qs

    chain = qs.claim_batch

    if slow_sec and (slow_id == "*" or os.environ.get("YIBAN_EXECUTOR_ID") == slow_id):
        _inner = chain

        def _slow_claim(owner, day, vshards, now=None, limit=1, elbow_sec=0.0, phones=None):
            rows = _inner(owner, day, vshards, now=now, limit=limit,
                          elbow_sec=elbow_sec, phones=phones)
            if rows:
                time.sleep(slow_sec)   # 持有在飞行，卡住——"上一个持有者还活着"
            return rows

        _slow_claim._drill = "slow"
        chain = _slow_claim

    if hold:
        _inner_h = chain
        _ready = os.environ.get("DRILL_HOLD_READY", "")
        _release = os.environ.get("DRILL_HOLD_RELEASE", "")
        _max = float(os.environ.get("DRILL_HOLD_MAX", "60") or 60)
        _held = []             # **一次性**：只卡第一行（见下）

        def _hold_claim(owner, day, vshards, now=None, limit=1, elbow_sec=0.0, phones=None):
            rows = _inner_h(owner, day, vshards, now=now, limit=limit,
                            elbow_sec=elbow_sec, phones=phones)
            # **只卡一次**：屏障的用途是"让驱动有机会在**某一刻**动手"（杀它 / 让同伴
            # 抢它）。若每次领取都卡，一个 N 账号的用例就要等 N × 上限——实测 4 账号
            # 一轮 146 秒，把 CI 作业预算吃光（真因被藏进"作业被取消"）。一次性即可：
            # 驱动要观测的"持有者在飞"这一态，第一行就给了。
            if rows and not _held:
                _held.append(True)
                # 领到了：先让外界知道"我持着在飞行"，再等放行——确定性屏障。
                try:
                    with open(_ready, "w") as fh:
                        fh.write("%s\n" % os.environ.get("YIBAN_EXECUTOR_ID", ""))
                except OSError:
                    pass
                deadline = time.monotonic() + _max
                while _release and not os.path.exists(_release) and time.monotonic() < deadline:
                    time.sleep(0.02)
            return rows

        _hold_claim._drill = "hold"
        chain = _hold_claim

    qs.claim_batch = chain

    if mutate == "reap-off":
        from yiban.engine import workers
        _noop = lambda slot: 0  # noqa: E731
        _noop._drill = "reap-off"
        workers._reap_dead_worker = _noop
    elif mutate == "claim-steal":
        _orig_claim = chain
        _stole = []            # 一次性：每个进程只抢一次，造一次干净的并发双持有

        def _steal_claim(owner, day, vshards, now=None, limit=1, elbow_sec=0.0, phones=None):
            rows = _orig_claim(owner, day, vshards, now=now, limit=limit,
                               elbow_sec=elbow_sec, phones=phones)
            if rows or _stole:
                return rows
            # 变异**只放门一处**：把领取 SQL 的 `state='pending'` 放宽成
            # `state IN ('pending','claimed')`——分片与账号允许集照旧honored。于是
            # "在飞行（claimed）的行也能被再领走"，即两台执行体同时持有同一行。
            # 只改这一处是有意的：变异与真码的差**只有互斥判据**，别的一律逐字一致
            # （否则"变异生效"就混进了别的行为改变，哨兵判不出到底改了哪一条）。
            shards = tuple(vshards or ())
            if not shards:
                return rows
            ph = ",".join("?" for _ in shards)
            phone_filter, phone_params = "", []
            if phones is not None:
                allowed = tuple(phones)
                if not allowed:
                    return rows
                phone_filter = "AND phone IN (%s) " % ",".join("?" for _ in allowed)
                phone_params = list(allowed)
            try:
                cutoff = qs._shift_stamp(now or qs.clock.ts(), -float(elbow_sec or 0.0))
            except ValueError:
                return None
            sql = ("UPDATE sign_tasks SET state='claimed', owner=?, epoch=epoch+1 "
                   "WHERE (phone, day) IN ("
                   "SELECT phone, day FROM sign_tasks "
                   "WHERE day=? AND vshard IN (" + ph + ") "
                   "AND state IN ('pending','claimed') AND run_at<=? "
                   + phone_filter +
                   "ORDER BY priority, run_at LIMIT ?) "
                   "RETURNING phone, run_at, attempts, epoch")
            try:
                conn, lock = qs._queue_conn()
                with lock:
                    got = conn.execute(sql, (owner, day, *shards, cutoff,
                                             *phone_params, int(limit))).fetchall()
                    conn.commit()
            except Exception:
                return None
            _stole.append(True)
            return [{"phone": r["phone"], "run_at": r["run_at"], "attempts": r["attempts"],
                     "epoch": r["epoch"]} for r in got]

        _steal_claim._drill = "claim-steal"
        qs.claim_batch = _steal_claim

    elif mutate == "window-off":
        # 把窗口门钉成"永远已关"。执行体的通道（`_lane`）与协调循环都在**领取之前**
        # 判它（`schedule._window_closed`）⇒ 本轮零领取、零真登录、监督进程 rc=2。
        # 用途只有一个：给"确实发生过"守卫造一个**确定性的"什么都没跑"**场景，
        # 证明该守卫真的会红（它不是被测性质，是守卫的牙）。
        from yiban.engine import schedule as _sched
        _closed = lambda *_a, **_k: True  # noqa: E731
        _closed._drill = "window-off"
        _sched._window_closed = _closed


try:
    _apply_injections()
except Exception:
    import traceback
    traceback.print_exc()
'''


#: 哨兵/同伴子进程脚本：**证明变异真的贴上了**（不证明不变式）。放进临时目录、以
#: 与执行体**同一套 env**（含 shim 的 PYTHONPATH）拉起，故它看到的是执行体看到的那份
#: 已注入代码。两种变异各给一个**行为**判据，不只看标记：
#: - `claim-steal`：在**本次演练的库**上，用同伴身份对"已被持有的那一行"再领一次。
#:   真码回空（state 非 pending）；变异抢到 ⇒ 互斥判据已不在。这一发同时就是 I1 的
#:   触发（被抢的那行 owner 变、epoch 只增一代）。
#: - `reap-off`：在**独立的 scratch 库**上领一行、调收尸、看它是否被回退。
#:   真码回退（pending）；变异留 claimed ⇒ 收尸已成空操作。
PROBE_SRC = r'''# -*- coding: utf-8 -*-
"""哨兵：断言变异已生效（行为面）。"""
import json
import os

out = {"mutate": os.environ.get("DRILL_MUTATE", "")}
from yiban.store import db
import yiban.store.queue_store as qs
from yiban.engine import workers

out["claim_marker"] = getattr(qs.claim_batch, "_drill", None)
out["reap_marker"] = getattr(workers._reap_dead_worker, "_drill", None)
mutate = out["mutate"]

if mutate == "claim-steal":
    db.init_db(os.environ["DRILL_PROBE_DB"], env_file=os.environ.get("DRILL_PROBE_ENV") or None,
               cleanup=False, migrate=True)
    day = os.environ["DRILL_PROBE_DAY"]
    phone = os.environ["DRILL_PROBE_PHONE"]
    conn = db.get_conn()
    before = conn.execute("SELECT state, owner, epoch FROM sign_tasks WHERE phone=? AND day=?",
                          (phone, day)).fetchone()
    out["target_phone"] = phone
    out["state_before_peer_claim"] = before[0] if before else None
    out["owner_before_peer_claim"] = before[1] if before else None
    rows = qs.claim_batch(os.environ["DRILL_PROBE_OWNER"], day,
                          tuple(json.loads(os.environ["DRILL_PROBE_SHARDS"])),
                          phones=[phone], limit=1)
    out["peer_claim_rows"] = [r["phone"] for r in (rows or [])]
    # 行为判据必须**限定在"被抢的行本来就是 claimed"**：否则同伴只是合法地领了一条
    # pending 行（真码也会返回它），那不是"互斥已不在"的证据（实测踩到：6 账号下探针
    # 随手抢了一条屏障之后才被领的行，假绿）。
    out["behavioral_steal"] = bool(before is not None and before[0] == "claimed" and rows)

if mutate == "reap-off":
    scratch = os.environ["DRILL_PROBE_SCRATCH_DB"]
    db.init_db(scratch, env_file=os.environ.get("DRILL_PROBE_ENV") or None,
               cleanup=False, migrate=True)
    day = os.environ["DRILL_PROBE_DAY"]
    phone = "19900000002"
    past = "2000-01-01 00:00:00.000"
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO sign_tasks (phone, day, vshard, owner, run_at, priority, state, "
        "attempts, lease_until, result, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (phone, day, 0, "", past, 5, "pending", 0, "", "", past))
    conn.commit()
    stable = os.environ["DRILL_PROBE_STABLE"]
    qs.claim_batch(stable + ":sentinel", day, (0,), phones=[phone], limit=1)
    before = conn.execute("SELECT state FROM sign_tasks WHERE phone=? AND day=?",
                          (phone, day)).fetchone()
    workers._reap_dead_worker(0)
    after = conn.execute("SELECT state FROM sign_tasks WHERE phone=? AND day=?",
                         (phone, day)).fetchone()
    out["state_before_reap"] = before[0] if before else None
    out["state_after_reap"] = after[0] if after else None
    out["behavioral_reap_noop"] = bool(after and after[0] == "claimed")

if mutate == "window-off":
    # 行为判据：本轮的窗口是**整日开着**的（见 `seed`），真码下 `_window_closed` 必为假；
    # 哨兵看到真 ⇒ 变异确已把窗口门钉死。这是"什么都没跑"的**原因**证据，与"跑没跑"的
    # 事实（零真登录）分开报。
    from yiban import clock as _clock
    from yiban.engine import schedule as _sched
    out["window_marker"] = getattr(_sched._window_closed, "_drill", None)
    out["window_closed_forced"] = bool(
        _sched._window_closed(_sched._schedule_config(), _clock.now()))

with open(os.environ["DRILL_PROBE_OUT"], "w", encoding="utf-8") as fh:
    json.dump(out, fh, ensure_ascii=False)
'''


def now_dt() -> datetime.datetime:
    """业务时间（naive）：与引擎 `yiban.clock` **同一口径**（固定 +8 北京时间）。

    **不得用 `datetime.datetime.now()`**：那是宿主本地时区。CI（ubuntu runner）是 UTC，
    而引擎一律按北京时间判窗口，两者差 8 小时。用宿主本地时区写出的窗口，在引擎看来
    永远"已结束"：执行体零领取、零真登录、监督进程 rc=2，两条不变式天然全绿（假绿），
    牙齿测试红得与"被测性质"无关。本地之所以看起来正常，只因本机时区恰好是北京时间
    （与 `clock` 逐秒相等）——不是靶场做对了什么（实测：把宿主时区设成 UTC 即复现）。
    """
    from yiban import clock
    return clock.now()


def make_manifest(n: int) -> str:
    rows = [{"slot": i, "type": "worker", "proxy": ""} for i in range(n)]
    return json.dumps(rows, separators=(",", ":"))


def write_shim(shimdir: str) -> None:
    os.makedirs(shimdir, exist_ok=True)
    with open(os.path.join(shimdir, "sitecustomize.py"), "w", encoding="utf-8", newline="\n") as f:
        f.write(SHIM_SRC)


def write_probe(tmp: str) -> str:
    """把哨兵脚本落盘，返回路径（以**同一套含 shim 的 env** 拉起它）。"""
    path = os.path.join(tmp, "drill_probe.py")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(PROBE_SRC)
    return path


def _await_barrier(ready_file: str, timeout: float, monitor: "Monitor",
                   target_prefix: str, proc: "subprocess.Popen | None" = None
                   ) -> tuple[bool, str | None]:
    """等屏障就绪，**同时持续采样**：让 I1 的"换手前样本"（目标持有者）先落定。

    返回 `(就绪?, 被观测到的那一行 phone)`。**必须把这一行回传**：探针只能抢"已经
    被采样为 claimed 的那一行"，抢别的行会让 I1 的"换手前样本"停在 pending（实测：
    6 账号时槽位 0 的其它通道在屏障之后又领了几行，探针随手抢了其中一行，监控看到
    的前一态仍是 pending ⇒ 假绿）。

    `proc`：监督进程。它一旦退出就**没有**"目标会在飞"可言了，立刻停等——否则会把
    预算全烧在一个已经死掉的场景上（诊断还会少一条"进程已退"的事实）。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        monitor.sample()
        held = sorted(p for p, r in _rows(monitor.dbfile, monitor.day).items()
                      if r["state"] == "claimed" and r["owner"].startswith(target_prefix))
        if os.path.exists(ready_file) and held:
            monitor.sample()          # 让"目标持这一行"这态确实进过采样
            return True, held[0]
        if proc is not None and proc.poll() is not None:
            return False, None        # 监督进程已退：再等不会出现持有者
        time.sleep(SAMPLE_SEC)
    return False, None


def _barrier_diag(ready_file: str, dbfile: str, day: str, target_id: str,
                  sup_log: str = "", proc: "subprocess.Popen | None" = None,
                  engine_log: str = "") -> dict:
    """屏障不就绪时**当场取证**：两条前提各报事实，供一眼定位。

    就绪需要同时满足两件事，修法完全不同：
      ① 目标执行体写下了就绪标记（＝注入真的贴上了、且它确实领到了行）；
      ② 库里能看见"目标持有某一行"（＝双方对 owner 身份的口径一致）。
    再加两样**头号证据**：监督进程的输出去向与**引擎自己的按天日志**。

    为什么必须带 `engine_log`：引擎把日志写进 `YIBAN_LOG_FILE` 派生的按天文件
    （`sign-<日期>.log`），**不写 stdout**。故 `supervisor_log_tail` 为空是**常态**，
    不构成"没跑"的证据（实测踩到：把空 stdout 读成"什么都没发生"）。真证据在
    `engine_log_tail`（窗口关闭时那里有"签到时段已结束，跳过执行"与子进程退出码）。
    """
    diag: dict = {"ready_file": ready_file, "target_id": target_id,
                  "target_prefix": target_id + ":"}
    try:
        diag["supervisor_rc"] = None if proc is None else proc.poll()
    except Exception as e:                                  # 取证不许再抛
        diag["supervisor_rc_error"] = "%s: %s" % (type(e).__name__, e)
    try:
        diag["ready_exists"] = os.path.exists(ready_file)
        if diag["ready_exists"]:
            with open(ready_file, encoding="utf-8", errors="replace") as fh:
                diag["ready_content"] = fh.read().strip()
    except OSError as e:
        diag["ready_error"] = "%s: %s" % (type(e).__name__, e)
    try:
        rows = _rows(dbfile, day)
        diag["rows"] = {p: {"state": r["state"], "owner": r["owner"]} for p, r in rows.items()}
        diag["claimed_owners"] = sorted({r["owner"] for r in rows.values()
                                         if r["state"] == "claimed"})
        diag["pids_for_target"] = find_child_pids(target_id)
    except Exception as e:                                  # 取证不许再抛
        diag["rows_error"] = "%s: %s" % (type(e).__name__, e)
    if sup_log and os.path.exists(sup_log):
        try:
            with open(sup_log, encoding="utf-8", errors="replace") as fh:
                diag["supervisor_log_tail"] = fh.read()[-3000:]
        except OSError as e:
            diag["supervisor_log_error"] = "%s: %s" % (type(e).__name__, e)
    if engine_log and os.path.exists(engine_log):
        try:
            with open(engine_log, encoding="utf-8", errors="replace") as fh:
                diag["engine_log_tail"] = fh.read()[-3000:]
        except OSError as e:
            diag["engine_log_error"] = "%s: %s" % (type(e).__name__, e)
    return diag


def _run_probe(childenv: dict, tmp: str, mutate: str, info: dict,
               monitor: "Monitor", timeout_sec: float,
               target_phone: str | None = None) -> dict:
    """拉起哨兵/同伴子进程（同一套含 shim 的 env）：证明变异已生效，并（claim-steal）
    在本次演练的库上以同伴身份**抢那一行已被目标持有的行**——这一发就是 I1 的触发。"""
    probe_path = write_probe(tmp)
    out = os.path.join(tmp, "probe.out.json")
    # 哨兵要看到**变异**，但**不得**继承持有闸门／慢注入：否则它领到行后卡在闸门上
    # 等一个永不来的放行标记（它本身就是"领取者"）。剥掉这几键。
    env = {k: v for k, v in childenv.items()
           if k not in ("DRILL_HOLD_SLOT", "DRILL_HOLD_READY", "DRILL_HOLD_RELEASE",
                        "DRILL_HOLD_MAX", "DRILL_SLOW_ID", "DRILL_SLOW_SEC")}
    env.update({
        "DRILL_PROBE_OUT": out,
        "DRILL_PROBE_DAY": info["day"],
        "DRILL_PROBE_ENV": info["env_file"],
        "DRILL_PROBE_STABLE": _worker_id(0),
        "DRILL_MUTATE": mutate,      # 变异只进哨兵/同伴进程（见 childenv 处注释）
    })
    if mutate == "claim-steal":
        phone = target_phone or ""
        env.update({
            "DRILL_PROBE_DB": info["dbfile"],
            "DRILL_PROBE_OWNER": _worker_id(1) + ":peer",
            "DRILL_PROBE_SHARDS": json.dumps(
                list(range(_max_shard(info["dbfile"], info["day"]) + 1))),
            "DRILL_PROBE_PHONE": phone,
        })
    if mutate == "reap-off":
        env["DRILL_PROBE_SCRATCH_DB"] = os.path.join(tmp, "scratch.db")
    try:
        r = subprocess.run([sys.executable, probe_path], env=env, cwd=REPO,
                           capture_output=True, timeout=timeout_sec)
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, e)}
    if os.path.exists(out):
        try:
            with open(out, encoding="utf-8") as f:
                return json.load(f)
        except ValueError as e:
            return {"error": "哨兵输出非 JSON: %s" % e}
    return {"error": "哨兵无输出", "rc": r.returncode,
            "stderr": (r.stderr or b"").decode("utf-8", "replace")[-500:]}


def _mutation_applied(mutate: str, probe: dict) -> bool:
    """变异是否真的生效：**标记 ＋ 行为**双判据（行为优先）。"""
    if mutate == "claim-steal":
        return (probe.get("claim_marker") == "claim-steal"
                and bool(probe.get("behavioral_steal")))
    if mutate == "reap-off":
        return (probe.get("reap_marker") == "reap-off"
                and bool(probe.get("behavioral_reap_noop")))
    if mutate == "window-off":
        # 标记在窗口门上（不是 `claim_batch` 上），故标记与行为**都要**判。
        return (probe.get("window_marker") == "window-off"
                and bool(probe.get("window_closed_forced")))
    return True


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


#: 造号基数：`138` ＋ 8 位序号 ⇒ 11 位，与真手机号同形。
PHONE_PREFIX = "138"

#: `spread_phones` 的候选扫描上限。分片集非空（约 V/2 个片）时命中期望在几十次以内；
#: 给个硬上限是把"哈希退化导致死循环"变成响亮失败，而不是把用例挂住。
_PHONE_SCAN_MAX = 200000


def spread_phones(executors: list[str], day: str, n: int) -> list[str]:
    """选 n 个手机号，使**每个执行体名下都至少有一个账号**（确定性，不靠运气）。

    **为什么要保这条**：本靶场的被测性质是"多执行体怎么协调"。账号归谁由 HRW 纯函数
    `H(手机号 ‖ 日期 ‖ 执行体名)` 决定（`yiban.core.hrw`）。若固定用一批手机号，某些
    日期会把全部账号算给同一个执行体——2026-10-17 实测：4 个固定号的虚分片 32/38/52/56
    全落在槽位 1 的 31 个分片里，槽位 0 一件活都领不到。那时"多执行体"只剩一个在跑，
    屏障（"目标执行体持着在飞行"）**恒不就绪**，三条用例红在"前提没搭起来"，与被测性质
    无关；且红绿随 (主机名, 日期) 漂移（**日历/主机敏感**，不是负载敏感）。

    **做法**：按**同一个纯函数**反查输入，而不是碰运气挑号。先给每个执行体各挑一个落在
    它分片集里的号，再补足到 n 个。日期一变选的号跟着变；同一日期内结果完全确定
    （纯函数 ⇒ 可重放、可缓存）。

    `n < len(executors)` 时无解（一个账号只归一个执行体）⇒ 抛 `ValueError`。响亮拒绝，
    不许悄悄退化成"只有一部分执行体在跑"。
    """
    from yiban.core import hrw

    if n < len(executors):
        raise ValueError(
            "账号数 %d 少于执行体数 %d：无法让每个执行体都有活，本靶场测不到多执行体协调"
            % (n, len(executors)))
    v = hrw.v_for(n)
    own = {e: set(hrw.shards_of(e, executors, day, v)) for e in executors}
    empty = sorted(e for e, s in own.items() if not s)
    if empty:
        raise ValueError(
            "执行体 %s 在 %s 名下零分片（不在 HRW 候选集 %s 内？）" % (empty, day, executors))
    chosen: list[str] = []
    taken: set[str] = set()

    def _scan(accept):
        for k in range(_PHONE_SCAN_MAX):
            cand = PHONE_PREFIX + "%08d" % k
            if cand in taken:
                continue
            if accept(hrw.vshard_of(cand, day, v)):
                taken.add(cand)
                return cand
        raise ValueError("扫过 %d 个候选仍没找到满足条件的号（哈希退化？）" % _PHONE_SCAN_MAX)

    for e in executors:
        chosen.append(_scan(lambda shard, _own=own[e]: shard in _own))
    while len(chosen) < n:
        chosen.append(_scan(lambda shard: True))
    return chosen


def seed(tmp: str, accounts: int, executors: int, manifest: str) -> dict:
    """造数：自造账号 ＋ 真计划（走 `executor_v3._ensure_plan`）＋ 全部行置为立即可领。

    账号的手机号由 `spread_phones` 按当日 HRW 归属反查，保证**每个执行体都有活**
    （见该函数的说明：这是"多执行体协调"这条被测性质的前提）。
    """
    env_file = os.path.join(tmp, ".env")
    dbfile = os.path.join(tmp, "yiban.db")
    state = os.path.join(tmp, "state")
    os.makedirs(state, exist_ok=True)
    t = now_dt()
    day = t.strftime("%Y-%m-%d")
    cfg_env = {
        "YIBAN_ACCOUNTS_KEY": KEY,
        "YIBAN_ENV_FILE": env_file,
        "YIBAN_DB_FILE": dbfile,
        "YIBAN_STATE_DIR": state,
        "YIBAN_LOG_FILE": os.path.join(tmp, "sign.log"),
        # 窗口取**当天整日**（00:00~23:59，北京时间）。靶场只要求"窗口开着"——它验的是
        # 协调层（领取/收尾/收尸/换代/肘界），不验窗口边界。为什么不用 `now±delta`：
        # ① 那会把窗口绑在"宿主本地时区"上（见 `now_dt` 的说明；CI 上直接判窗口已结束）；
        # ② `now±delta` 在临近午夜时跨零点，`window.parse_window` 判非法并回退默认窗口
        #    （06:30~07:50），窗口随即关闭。整日窗口对任何取时都合法且开着，两坑一起消掉。
        "YIBAN_SIGN_START": "00:00",
        "YIBAN_SIGN_END": "23:59",
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
    # 执行体身份串的**唯一构造处**仍是 `egress`（经 `schedule._executor_ids` 按清单取）。
    # 这里不自己拼 `worker-<i>@<主机>`：计划里的 owner 必须与执行体算出的名字逐字一致，
    # 否则分片归属对不上（`_executor_ids` 的 docstring 即此纪律）。
    phones = spread_phones(sched._executor_ids(cfg_env), day, accounts)
    for i, phone in enumerate(phones):
        email = "drill%05d@mock.invalid" % i
        with contextlib.suppress(Exception):
            db.create_user(email, "mock-hash", role="user")
        with contextlib.suppress(Exception):
            db.add_account({
                "name": "drill%05d" % i, "phone": phone,
                "password": "drill-pass-%d" % i, "phone_model": "MockPhone",
                "phone_code": "", "owner": email, "status": "active", "reject_reason": "",
            })
    accounts_obj = db.load_accounts()
    cfg = sched.planner_config()
    executor_v3._ensure_plan(accounts_obj, cfg.get("day", day), cfg)
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

    I1：一行的 `state` 连续两次采样都是 `claimed`，而 `owner` 变了 ⇒ 发生了"没经释放的
    换手"——持有者还在飞，另一个执行体把它抢走了（合法换手必先回 `pending` 或进终态）。
    **不用** `epoch` 增量当判据：一次抢占只增一代、两代连抢会增两代，"只增一代"会漏判
    多代连抢（实测踩到：执行体自己的腿先抢一次、同伴哨兵再抢一次，`epoch` 1→3）。
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
            if prev and prev[0] == "claimed" and cur[0] == "claimed" and cur[1] != prev[1]:
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


def _spawn_supervisor(childenv: dict, log_path: str | None = None) -> subprocess.Popen:
    """拉起监督进程（真 `yiban.cli sign`）。

    **输出必须落文件，不能挂 PIPE**：监督进程与它拉起的执行体每次演练会打不少行
    （迁移 INFO、逐账号日志、注入异常回溯），量过管道缓冲即**阻塞写**——写不进就
    不再前进，表现为"演练卡住"，而真因（谁卡在写）反而看不见。落文件同时把
    诊断材料留了下来（见 `barrier_diag`）。
    """
    fh = open(log_path, "wb") if log_path else subprocess.DEVNULL
    proc = subprocess.Popen([sys.executable, "-m", "yiban.cli", "sign"],
                            env=childenv, cwd=REPO,
                            stdout=fh, stderr=subprocess.STDOUT)
    if log_path:
        fh.close()          # 父进程这份可以关：子进程持有自己的 dup
    return proc


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
        if mutate in ("reap-off", "window-off"):
            # 这两条变异必须进整棵进程树：收尸在**监督进程**里发生（reap-off）；
            # 窗口门在**执行体**的领取之前判（window-off）。claim-steal 反之（见下）。
            childenv["DRILL_MUTATE"] = mutate
        # claim-steal **不进执行体进程**：它只在哨兵/同伴进程里贴（见 `_run_probe`）。
        # 理由：变异若同时贴进真执行体的腿，执行体会互相抢自己分片里的在飞行行、
        # 造出一串与本次断言无关的换手（6 账号时实测把目标行淹掉、I1 假绿）。
        # 只让"同伴"带上放宽后的领取谓词去抢真执行体持有的那一行，场景干净且确定。
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
        # **确定性屏障**：杀注入与 claim-steal 牙齿都要"目标执行体确实持着在飞行"。
        # 靠轮询 claimed 行是**概率**的（CI 负载下会输），故让目标执行体领到后**卡在
        # 闸门上**等放行：驱动侧见到就绪标记才动手，不靠调度运气。
        need_barrier = bool(kill_at == "claim" or mutate == "claim-steal")
        hold_ready = os.path.join(tmp, "hold.ready")
        hold_release = os.path.join(tmp, "hold.release")
        if need_barrier:
            childenv.update({
                "DRILL_HOLD_SLOT": "0",
                "DRILL_HOLD_READY": hold_ready,
                "DRILL_HOLD_RELEASE": hold_release,
                # **持有必须有硬上限**：闸门是"等驱动动手"的，正常一两拍就放行。若驱动
                # 那一手没落地（如 `find_child_pids` 没找到 pid ⇒ 杀注入放空枪），**不能**
                # 让它一直卡到用例超时——实测：卡满一次就把 CI 的 10 分钟作业预算吃光，
                # 表现成"作业被取消"，看不出真因。故上限取 HOLD_MAX_SEC（秒），与用例
                # 超时无关。
                "DRILL_HOLD_MAX": str(HOLD_MAX_SEC),
            })
        if kill_at:
            kill_plan = {"armed": True, "executor_id": target_id,
                         "owner_prefix": target_id + ":", "killed_pid": None,
                         "killed_phone": None}
            if kill_at == "claim":
                kill_plan["when"] = "claim"
            else:
                kill_plan["when"] = "sec"
                kill_plan["at"] = time.time() + float(kill_at)
        if slow_sec:
            childenv["DRILL_SLOW_ID"] = target_id
            childenv["DRILL_SLOW_SEC"] = str(slow_sec)
        result["target_executor"] = target_id
        result["slow_sec"] = slow_sec
        result["barrier"] = need_barrier

        # 驱动进程自己的 env 也带上（供子进程继承），且驱动不加载 shim（不加 shimdir）。
        os.environ.update(childenv)

        # 持有期探针（肘界让位判向）：只要"目标确实持着在飞行"就跑一次——慢执行体或
        # 屏障持有都算；变异轮不跑（变异会把判据本身改掉）。
        elbow_probe: dict = {"ran": False}
        on_hold = None
        if (slow_sec or need_barrier) and not mutate:
            def on_hold(phone, _res=elbow_probe):
                _res.update({"ran": True})
                _res.update(_probe_elbow(info["dbfile"], day, phone, target_id + ":"))
        sup_log = os.path.join(tmp, "supervisor.log")
        # 引擎自己的**按天日志**路径：格式的唯一事实源在 `cli_support._signin_log_path`
        # （按 `YIBAN_LOG_FILE` 派生），此处不重抄一遍 `sign-<日期>.log`。重抄一份既是
        # shared-facts 名册「sign-<day>.log」键要抓的重复，也会在格式改版后指向空气。
        # 此刻 `os.environ` 已并进 childenv（含 `YIBAN_LOG_FILE`）。
        from yiban.engine import cli_support as _cli_support
        engine_log = _cli_support._signin_log_path()
        sup = _spawn_supervisor(childenv, sup_log)
        result["supervisor_pid"] = sup.pid

        # ① 等屏障：目标执行体持着在飞行（同时采样，让 I1 的"换手前样本"落定）。
        #    等待上限＝持有闸门的硬上限：持有者 45 秒后自行放行，那之后 `held` 必然空，
        #    再等就是白等（且会把用例拖到超时，把真因藏进"作业被取消"）。
        target_phone = None
        if need_barrier:
            ready, target_phone = _await_barrier(hold_ready, BARRIER_WAIT_SEC, monitor,
                                                 target_id + ":", sup)
            result["barrier_ready"] = ready
            if not ready:
                # 不就绪时**当场取证**：是"就绪标记没写出来"（注入没贴上 / 执行体没起来）
                # 还是"写了但库里看不到目标持有者"（身份对不上）——两者的修法完全不同，
                # 不许靠猜（CI 上已因猜错方向白跑两轮）。头号证据有两处：监督进程的
                # stdout（回溯）与**引擎自己的按天日志**（窗口关闭等业务判定的唯一落点，
                # stdout 上没有）。
                result["barrier_diag"] = _barrier_diag(
                    hold_ready, info["dbfile"], day, target_id, sup_log, sup,
                    engine_log=engine_log)
            result["barrier_phone"] = target_phone

        # ② 变异**哨兵**：先证明变异真的贴上了，再谈不变式红不红。claim-steal 的哨兵
        #    同时在**本次演练的库**上以同伴身份抢那一行（**屏障观测到的那一行**）——
        #    这一发就是 I1 的触发。
        if mutate:
            # 哨兵的超时上限＝持有闸门上限：claim-steal 的同伴必须**在目标还持着的时候**
            # 抢那一行，而目标最多持 HOLD_MAX_SEC 就自行放行。给哨兵超过这个数的预算，
            # 只会让"放行后再抢"变成合法领取——判据失效，且白等。
            probe = _run_probe(childenv, tmp, mutate, info, monitor,
                               min(timeout_sec, HOLD_MAX_SEC),
                               target_phone=target_phone)
            result["mutation_probe"] = probe
            result["mutation_applied"] = _mutation_applied(mutate, probe)
            monitor.sample()

        # ③ 杀注入：屏障保证此刻目标确实持着在飞行，`_wait_supervisor` 的第一拍即可命中。
        # ④ claim-steal：抢完之后放行目标，让它走完（其收尾会被 epoch fence 拒）。
        if need_barrier and mutate == "claim-steal":
            with contextlib.suppress(OSError):
                open(hold_release, "w").close()

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
            # 恢复轮**必须去掉注入键**：留着 `DRILL_HOLD_*` 会让它的槽位 0 领到后再等
            # 一个永不出现的放行标记（杀注入那一轮不放行），恢复轮被自己拖死。
            rec_env = {k: v for k, v in childenv.items()
                       if k not in ("DRILL_SLOW_ID", "DRILL_SLOW_SEC",
                                    "DRILL_HOLD_SLOT", "DRILL_HOLD_READY",
                                    "DRILL_HOLD_RELEASE", "DRILL_HOLD_MAX")}
            sup2 = _spawn_supervisor(rec_env, os.path.join(tmp, "supervisor-recover.log"))
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
        # **"到底跑了没有"的事实字段**（供测试的"确实发生过"守卫）：真领取行数与真签到
        # 次数。两者都为 0 就是"靶场什么都没跑"——此时两条不变式天然全绿（假绿），
        # 只断言"不变式红"会把"没跑"误报成"有牙"。
        # `claimed_rows_seen`：监控采样期间**确实进过 claimed** 的行（领取真的发生过的证据）；
        # `mock_signin_count`：假上游收到的 `/signIn` 真登录次数（零外联，只可能是子进程发的）。
        result["claimed_rows_seen"] = sorted({t["phone"] for t in monitor.trace
                                              if t["state"] == "claimed"})
        # 假上游按隐私口径**不记手机号**（见 `yiban/adapters/protocol_fake/__init__.py` 的 record）：故"同一账号
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
    ap.add_argument("--mutate", default="", choices=["", "reap-off", "claim-steal",
                                                     "window-off"])
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
    if args.mutate:
        # 变异run 的判据是「变异生效 ＋ 不变式被打破」，两条分开报。
        checks["mutation_applied"] = res.get("mutation_applied")
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
    # 变异run 期望"红"：这里只报事实，不当失败（牙齿测试断言"红"）。
    ok = (bool(checks["mutation_applied"]) if args.mutate
          else all(bool(v) for v in checks.values()))
    shown = {k: res[k] for k in (
        "executors", "accounts", "supervisor_rc", "recover_rc", "done_count",
        "mock_requests", "mock_signin_count", "signin_excess", "i1_ok", "i2_ok",
        "claimed_rows_seen", "i1_violations", "i2_claimed_left", "terminal_or_pending",
        "first_round_pending",
        "kill", "stale_write", "elbow_probe", "mutate", "barrier", "barrier_ready",
        "mutation_applied", "mutation_probe", "barrier_diag") if k in res}
    shown["checks"] = checks
    print(json.dumps(shown, ensure_ascii=False, indent=2))
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_cli())
