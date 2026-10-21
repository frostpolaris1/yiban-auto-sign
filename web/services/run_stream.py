# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""SSE 进度流的进程内广播器与帧序列化（125 §3.2 的"每进程一份"）。

**功能**
把"每连接每拍查一次库"改成"每进程每拍查一次、扇出给全部订阅者"。一个后台线程
按 `YIBAN_SSE_POLL_SEC` 节拍做一次 `run_events` 增量读（`id > ? LIMIT 200`，
12 万行下 ~0ms），把新事件投给每个订阅者的**有界队列**（满则丢最旧）。快照按
`YIBAN_SSE_SNAPSHOT_SEC` 缓存：50 个管理员同时刷新时只跑一次定向聚合。

**成立前提**：本仓 web 恒为 `--workers 1`（限速/锁定/防抖状态存进程内存），故
"每进程一份"就是"全局一份"。多 worker 会让每个进程各推一份、游标各走各的，
那正是本设计要避免的形状。

**纪律（125 §3.2 / §5.2）**
- 常态只做增量读；快照只在建连/重同步时做，且**走定向聚合**（`run_events.snapshot`），
  绝不调 `summarize()`（12 万行 407ms 会随 `_conn_lock` 把同进程 DB 访问串死）；
- 广播器**不写审计**：只读面留痕由端点在**建连时**调一次 `read_audit_trace()`；
- 扇出**不阻塞**：订阅者队列有界，写满丢最旧，慢消费者拖不动广播线程。

**归属**
`web/routes/stream_api.py` 是唯一消费者。广播器是进程内单例（模块级）；测试若需
隔离，用 `reset_hub()` 换一份新的。

**通信**
入：`yiban.adapters.persistence.run_events` 的读取面（`stream_events` / `snapshot` / `max_id`）。
出：SSE 帧字符串（`sse_frame`）。谁调用：`web/routes/stream_api.py`。
"""
import collections
import json
import logging
import threading
import time

from yiban.adapters.config import loader as config_loader
from yiban.adapters.persistence import run_events as _run_events

logger = logging.getLogger("web.run_stream")

#: 每个订阅者队列上限（事件条数）：正常两拍之间远达不到；写满说明消费者卡住，丢最旧。
QUEUE_MAX = 500

#: 广播器增量读一次最多取几行（复用读取层的增量上限）。
_BATCH = _run_events.STREAM_LIMIT

#: 快照缓存时长（秒）：50 个管理员同时建连时避免 50 次聚合并发。
SNAPSHOT_CACHE_SEC = 1.0


def sse_frame(event, data, event_id=None):
    """→ 一帧 SSE 文本（`event:` / 可选 `id:` / `data:` + 空行）。`data` 走紧凑 JSON。

    `id` 只在"推进游标"的事件上给（`snapshot` 给 cursor、`progress` 给自己的行 id）；
    `resync` / `end` / `heartbeat` 里带不带 id 见端点处注释——`resync` 绝不能带 id，
    否则浏览器会把断点续传游标推到一个"未真正送达事件"的位置。
    """
    lines = []
    if event:
        lines.append("event: %s" % event)
    if event_id is not None:
        lines.append("id: %s" % event_id)
    lines.append("data: %s" % json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(lines) + "\n\n"


class Subscriber:
    """一个 SSE 连接的收件箱：有界队列 + 条件变量（广播线程投递，连接线程取走）。

    队里存**原始行**（含 phone）；`phones` 非 None 时在入队处按本人手机号过滤
    （用户端流只推本人）。原始行出流前必须过 `run_events.render_stream_event`。
    """

    def __init__(self, maxlen=QUEUE_MAX, phones=None):
        self._cond = threading.Condition()
        self._buf = collections.deque()
        self._maxlen = max(1, int(maxlen))
        self._closed = False
        self._phones = None if phones is None else {str(p) for p in phones if str(p)}

    def offer(self, rows):
        """广播线程投递一批原始行；队列满丢**最旧**（绝不阻塞广播线程）。"""
        with self._cond:
            for r in rows:
                if self._phones is not None and str(r.get("phone") or "") not in self._phones:
                    continue
                if len(self._buf) >= self._maxlen:
                    self._buf.popleft()
                self._buf.append(r)
            self._cond.notify_all()

    def take(self, timeout):
        """取走当前全部事件；空队列时最多阻塞 `timeout` 秒。→ list。"""
        with self._cond:
            if not self._buf and not self._closed:
                self._cond.wait(timeout)
            out = list(self._buf)
            self._buf.clear()
            return out

    def close(self):
        """标记关闭并唤醒等待者（连接结束时调用，让 `take` 立刻返回）。"""
        with self._cond:
            self._closed = True
            self._cond.notify_all()


class RunStreamHub:
    """进程内单例广播器：一个后台线程 + 全部订阅者。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._subs = []          # [(sub, session_key, kind)]；kind ∈ {"admin","user"}
        self._cursor = None
        self._thread = None
        self._poll = 1.0
        self._snap_cache = {}    # day -> (ts, payload)
        self._snap_sec = SNAPSHOT_CACHE_SEC

    # ---- 快照（带缓存）----
    def snapshot(self, day=None, *, max_age=None):
        """→ 进度快照（`run_events.snapshot` 的产物，带缓存）。"""
        age = SNAPSHOT_CACHE_SEC if max_age is None else float(max_age)
        key = day or ""
        now = time.time()
        hit = self._snap_cache.get(key)
        if hit is not None and now - hit[0] < age:
            return hit[1]
        payload = _run_events.snapshot(day=day)
        self._snap_cache[key] = (now, payload)
        return payload

    # ---- 订阅（原子查上限 + 登记）----
    def try_subscribe(self, session_key="", *, kind="admin", poll=1.0, batch=_BATCH,
                      queue_max=QUEUE_MAX, phones=None, session_max=0, connections_max=0):
        """原子「查上限 + 订阅」。→ (Subscriber, None) 成功；(None, reason) 超限。

        上限判定与登记必须在**同一把锁**内完成。分成两步（先 `session_count` /
        `subscriber_count` 查计数，再另调订阅）会有 TOCTOU：两个并发请求各自通过检查、
        各自登记，实际连接数越过上限（长连接下这个窗口可达秒级）。本方法复用本类既有的
        `self._lock`（订阅表与广播游标都由它守），**不新增锁名**。

        `kind` 是流别（`admin` / `user`）：`connections_max` 只数**同流别**的订阅，故
        管理员流与用户流预算各自独立（125 §6.3），一方占满不阻塞另一方。`session_max`
        按 `session_key` 计（同会话总连接数，不分流别）。

        `session_max` / `connections_max` 为 0 表示该维不设限。`reason` 取值与端点发出的
        `end` 原因一致：`too_many_connections`（每会话超限）/ `server_busy`（本流别全局超限）。

        线程首次启动时把游标定在**当前最大 id**：历史事件由各连接自己重放/取快照，
        广播器只负责"此后新增"的事件（否则新连接会把整表灌给所有人）。
        `phones` 非 None = 用户端流：只投本人手机号的行（本人过滤）。
        """
        with self._lock:
            if session_max and self._session_count_locked(session_key) >= session_max:
                return None, "too_many_connections"
            if connections_max and self._kind_count_locked(kind) >= connections_max:
                return None, "server_busy"
            sub = Subscriber(queue_max, phones=phones)
            self._subs.append((sub, session_key, kind))
            self._poll = max(0.1, float(poll))
            self._batch = max(1, int(batch))
            if self._thread is None or not self._thread.is_alive():
                self._cursor = _run_events.max_id()
                self._thread = threading.Thread(
                    target=self._loop, daemon=True, name="run-stream-hub")
                self._thread.start()
            return sub, None

    def _session_count_locked(self, session_key):
        """同会话订阅数（**须已持 `self._lock`**：供 `try_subscribe` 内部调用）。"""
        return sum(1 for (_s, k, _kd) in self._subs if k == session_key)

    def _kind_count_locked(self, kind):
        """同流别订阅数（**须已持 `self._lock`**）。`connections_max` 按它判（预算分家）。"""
        return sum(1 for (_s, _k, kd) in self._subs if kd == kind)

    def unsubscribe(self, sub):
        """注销订阅者（连接结束）。线程在无订阅者时自行退出。"""
        with self._lock:
            self._subs = [(s, k, kd) for (s, k, kd) in self._subs if s is not sub]

    def subscriber_count(self):
        with self._lock:
            return len(self._subs)

    def session_count(self, session_key):
        with self._lock:
            return self._session_count_locked(session_key)

    def _loop(self):
        while True:
            time.sleep(self._poll)
            with self._lock:
                if not self._subs:
                    self._thread = None
                    return
                cursor = self._cursor
                batch = self._batch
            try:
                rows = _run_events.stream_rows(cursor, batch)
            except Exception as e:      # 读失败只跳过本拍，绝不退出广播线程
                logger.warning("进度流增量读失败（跳过本拍）: %s", e)
                continue
            if not rows:
                continue
            with self._lock:
                self._cursor = max(self._cursor or 0, rows[-1]["id"])
                subs = [s for (s, _k, _kd) in self._subs]
            for s in subs:
                s.offer(rows)


#: 进程内单例（`--workers 1` 下即全局一份）。
_HUB = RunStreamHub()


def hub():
    """→ 进程内广播器单例。"""
    return _HUB


def reset_hub():
    """换一份新的广播器（测试隔离用；生产不调用）。"""
    global _HUB
    with _HUB._lock:
        _HUB._subs = []
        _HUB._thread = None
    _HUB = RunStreamHub()
    return _HUB


# ---------------------------------------------------------------------------
# 配置（默认值只在名册里；本函数只做"名册缺省 + .env 覆盖"的合成）
# ---------------------------------------------------------------------------
_KEY_ENABLED = "YIBAN_SSE_ENABLED"
_KEY_POLL = "YIBAN_SSE_POLL_SEC"
_KEY_HEARTBEAT = "YIBAN_SSE_HEARTBEAT_SEC"
_KEY_GAP = "YIBAN_SSE_GAP_MAX"
_KEY_SESSION_MAX = "YIBAN_SSE_SESSION_MAX"
_KEY_CONNECTIONS = "YIBAN_SSE_CONNECTIONS_MAX"
_KEY_USER_CONNECTIONS = "YIBAN_SSE_USER_CONNECTIONS_MAX"
_KEY_IDLE = "YIBAN_SSE_IDLE_SEC"
_KEY_RECHECK = "YIBAN_SSE_SESSION_RECHECK_SEC"
_KEY_MAX_SEC = "YIBAN_SSE_CONNECTION_MAX_SEC"


def _flag(raw, default):
    """`.env` 开关值 → 布尔（认 1/true/on/yes）；未设/空用名册缺省。"""
    if raw is None or not str(raw).strip():
        return bool(default)
    from yiban.adapters.config import env_io as _env_io
    return _env_io.parse_env_flag(raw, default=False)


def _int(raw, default):
    """`.env` 整数值 → 非负整数；非法/未设用名册缺省。"""
    try:
        value = int(str(raw).strip())
        return value if value >= 0 else int(default)
    except (TypeError, ValueError):
        return int(default)


def stream_config(env_file, read_env):
    """→ 进度流配置（名册缺省 + `.env` 覆盖）。`read_env` 由调用方按调用时刻现取注入。

    键与缺省全部来自 `config/registry.json`（代码里不写第二份缺省字面量）。
    """
    env = read_env(env_file) or {}

    def d(key):
        return config_loader.default_of(key)

    return {
        "enabled": _flag(env.get(_KEY_ENABLED), d(_KEY_ENABLED)),
        "poll_sec": _int(env.get(_KEY_POLL), d(_KEY_POLL)),
        "heartbeat_sec": _int(env.get(_KEY_HEARTBEAT), d(_KEY_HEARTBEAT)),
        "gap_max": _int(env.get(_KEY_GAP), d(_KEY_GAP)),
        "session_max": _int(env.get(_KEY_SESSION_MAX), d(_KEY_SESSION_MAX)),
        "connections_max": _int(env.get(_KEY_CONNECTIONS), d(_KEY_CONNECTIONS)),
        "user_connections_max": _int(env.get(_KEY_USER_CONNECTIONS),
                                     d(_KEY_USER_CONNECTIONS)),
        "idle_sec": _int(env.get(_KEY_IDLE), d(_KEY_IDLE)),
        "recheck_sec": _int(env.get(_KEY_RECHECK), d(_KEY_RECHECK)),
        "connection_max_sec": _int(env.get(_KEY_MAX_SEC), d(_KEY_MAX_SEC)),
    }
