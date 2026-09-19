# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""手动签到域路由：按账号触发 `signin.py --only` 子进程（单个 / 批量）。

**功能**
`POST /api/signin` 触发单账号手动签到；`POST /api/signin/batch` 触发批量手动签到
（合并为一个队列子进程、只发一封汇总邮件）。

**归属**
`web.app.create_app` 的"手动签到"面。工厂骨架、跨域中间件（前置限速、登录守卫、CSRF、
同源校验、安全响应头）与库访问层仍留在 `web/app.py`；本模块自带子进程管理。

**复用**
`register(app)` 供 `web.routes.register_all` 装配，并在此时把本域可变状态（防抖表、
子进程表、批量互斥与冷却基准）登记进 `current_app.extensions`——它们原本是 create_app
的工厂局部，必须保持"每个 app 实例一份"，不能做成模块级（测试进程会反复 create_app）。

**通信**
视图体不直接读 web.app 的模块级名字，一律经 `web.routes.appmod()` 按属性取——测试用
`mock.patch.object(web.app, …)` 打桩（subprocess.Popen / os.open / db / logger /
load_accounts / log_path_for / ENV_FILE / STATE_DIR 等）必须继续生效。批量队列在后台
线程执行，线程内不得再取 `current_app`：模块对象与状态字典在请求期解析后随闭包传入。
"""
import contextlib
import os
import subprocess
import sys
import threading
import time

from flask import current_app, jsonify, session

from web.routes import appmod as _appmod


def _signin_state():
    """手动签到的每 app 实例状态（防抖表 / 子进程表 / 批量互斥与冷却基准）。

    对应原 create_app 的六个工厂局部；背景线程拿不到 Flask 应用上下文，故一律
    在请求期取出后作为参数传给子进程助手。
    """
    return current_app.extensions["yiban_signin_state"]


def _signin_run_lock_busy(m):
    """非阻塞探测 signin 运行锁是否被其他进程持有。

    全量签到/cron 运行期间，--only 子进程会拿锁失败并 exit 3 静默退出——
    若照样返回"已触发"，用户侧无感；spawn 前先探测，忙时直接
    429 如实提示（POSIX flock 试探；Windows 无 fcntl 返回 False 走旧行为，
    与 _acquire_run_lock 的降级策略一致）。
    """
    path = os.path.join(m.STATE_DIR, "signin-run.lock")
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return False
    try:
        try:
            import fcntl
        except ImportError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True  # 被其他签到进程持有
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


def _reap_signin(m, state, phone, proc):
    """子进程结束后在锁内从子进程表移除同对象（防僵尸记录/重复 terminate）。"""
    try:
        proc.wait()
    finally:
        with state["lock"]:
            if state["procs"].get(phone) is proc:
                state["procs"].pop(phone, None)
    # 退出码透传：exit 3（队列忙/运行锁占用）等非 0 退出若被静默吞掉，
    # 用户看到"已触发"实际没签——真实原因写入签到日志（日志页可见）
    m._log_manual_sign_exit(m._mask_phone(phone), proc.returncode)


def _launch_signin_proc(m, only_arg):
    """起一个 `signin.py --only <only_arg>` 子进程；only_arg 可为逗号分隔多号。

    环境与定时签到同口径（进程环境为底座、.env 的 YIBAN_* 覆盖注入）；密钥经
    YIBAN_ENV_FILE 由子进程自读，不注入明文。
    返回 Popen；脚本缺失等启动失败返回 None。
    """
    base = os.path.dirname(os.path.dirname(os.path.abspath(m.__file__)))
    script = os.path.join(base, "scripts", "signin.py")
    env = m.child_env.build_child_env(m.ENV_FILE, base=dict(os.environ))
    # 启动延迟已废弃，不再注入 YIBAN_START_DELAY_MAX=0；账号间隔
    # YIBAN_ACCOUNT_GAP_MAX 也不强制清零——按全局设置对自动+手动一致生效
    # （手动单账号本无相邻请求，批量手动按 .env/缺省 10 生效，与文档口径一致）。
    env["YIBAN_DB_FILE"] = m.DB_FILE
    env["YIBAN_ENV_FILE"] = m.ENV_FILE
    log_fh = None
    with contextlib.suppress(OSError):
        log_fh = open(m.log_path_for(), "a", encoding="utf-8", buffering=1)
    try:
        return subprocess.Popen(
            [sys.executable, script, "--only", only_arg],
            cwd=base, env=env, stdout=log_fh, stderr=subprocess.STDOUT,
        )
    except FileNotFoundError:
        return None
    finally:
        if log_fh is not None:
            log_fh.close()


def _spawn_signin_many(m, state, phones):
    """批量手动签到合并为单个队列子进程（--only 逗号分隔）。

    底层 signin.py --only 本就支持多号：一次登录批处理、一次队列重试、只发一封
    汇总邮件——替代原"逐账号各起一个子进程、各发一封汇总"，N 个账号从 N 封降到 1 封。
    返回 (ok: bool, msg: str, proc: Popen|None)。
    """
    if not phones:
        return False, "无可签到账号", None
    if _signin_run_lock_busy(m):
        return False, "签到队列忙（定时签到进行中），请稍后再试", None
    proc = _launch_signin_proc(m, ",".join(phones))
    if proc is None:
        return False, "批量手动签到启动失败，请稍后重试", None
    # 冷却基准单源化——只有 spawn 真正成功才刷新（若写在 _run_batch 的 finally
    # 里，spawn 失败也刷新基准，失败后 30 分钟内合法重试会被拒）
    with state["batch_lock"]:
        state["last_batch_ts"] = time.time()
    m.logger.info("触发批量手动签到（单队列）: %s 个账号", len(phones))
    return True, "", proc


def _spawn_signin(m, state, phone, accounts=None):
    """触发单账号手动签到子进程（signin.py --only）。

    防抖：30 秒内同账号不重复触发（SIGN_MIN_INTERVAL）；仍在运行的旧进程先终止。
    手动签到冷却单源化——单条与批量共用同一冷却计数
    （YIBAN_BATCH_SIGN_COOLDOWN_SEC，默认 1800s，0=关闭）：spawn 成功前检查
    冷却（与批量端点同口径拒绝），spawn 成功后刷新冷却基准。
    单条手动签到同样受全局冷却约束（被盗会话循环触发
    单号真实登录同样打爆易班风控）；30 秒 per-phone 防抖语义保持不变。
    返回 (ok: bool, msg: str)。
    """
    accounts = accounts if accounts is not None else m.load_accounts()
    idx = m.find_account_index(accounts, phone)
    if idx is None:
        return False, f"账号 {phone} 不在配置中"
    acc = accounts[idx]
    if acc.get("deleted") or acc.get("status") != m.ACCOUNT_STATUS_ACTIVE:
        return False, f"账号 {phone} 不可手动签到（未生效或已删除）"
    if _signin_run_lock_busy(m):
        return False, "签到队列忙（定时签到进行中），请稍后再试"
    with state["batch_lock"]:
        cooldown = m.load_env_int(m.ENV_FILE, "YIBAN_BATCH_SIGN_COOLDOWN_SEC", 1800)
        if cooldown > 0:
            elapsed = time.time() - state["last_batch_ts"]
            if elapsed < cooldown:
                remain = int(cooldown - elapsed)
                return False, f"签到冷却中（约 {remain // 60} 分 {remain % 60} 秒后可重试）"
    with state["lock"]:  # 原子检查+占位：并发请求不能同时通过防抖
        now = time.time()
        if phone in state["last_trigger"] and now - state["last_trigger"][phone] < m.SIGN_MIN_INTERVAL:
            remain = int(m.SIGN_MIN_INTERVAL - (now - state["last_trigger"][phone]))
            return False, f"账号 {phone} 正在签到，请 {remain} 秒后再试"
        old = state["procs"].get(phone)
        if old and old.poll() is None:
            old.terminate()  # 仍在运行 → 终止旧进程，防止同账号并发签到
        state["last_trigger"][phone] = now

    proc = _launch_signin_proc(m, phone)
    if proc is None:
        with state["lock"]:
            state["last_trigger"].pop(phone, None)
        return False, f"账号 {phone} 手动签到启动失败，请稍后重试"
    with state["lock"]:
        state["procs"][phone] = proc  # 记录子进程，供下次触发时终止旧进程
    # daemon 回收线程：进程退出后自动从子进程表移除同对象
    threading.Thread(target=_reap_signin, args=(m, state, phone, proc), daemon=True).start()
    with state["batch_lock"]:
        state["last_batch_ts"] = time.time()  # spawn 成功 = 冷却基准（单条/批量同源）
    m.logger.info("触发手动签到: %s", m._mask_phone(phone))
    return True, f"已触发 {phone} 手动签到（后台执行，日志约 30 秒内刷新）"


def api_signin():
    """手动签到指定账号：子进程执行 signin.py --only。"""
    m = _appmod()
    state = _signin_state()
    data = m._json_body()
    phone = str(data.get("phone", "")).strip()
    ok, msg = _spawn_signin(m, state, phone)
    if not ok:
        if "不在配置中" in msg:
            return jsonify({"error": msg}), 404
        if "不可手动签到" in msg:
            return jsonify({"error": msg}), 400
        if "冷却中" in msg:  # 单条与批量共用的全局签到冷却
            return jsonify({"error": msg}), 429
        if "正在签到" in msg or "签到队列忙" in msg:
            return jsonify({"error": msg}), 429
        return jsonify({"error": msg}), 500
    m.db.audit(session.get("username") or "?", "signin_manual", m._mask_phone(phone), "手动签到")
    return jsonify({"ok": True, "msg": msg})


def api_signin_batch():
    """批量手动签到：顺序逐个触发（与自动签到同语义，防风控）。

    全局互斥（同时只允许一个批量队列在跑）；防抖冲突的账号自动跳过。
    接口立即返回，实际执行在后台线程。
    """
    m = _appmod()
    state = _signin_state()
    data = m._json_body()
    ids = data.get("ids")
    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "请先勾选要签到的账号"}), 400
    accounts = m.load_accounts()
    if _signin_run_lock_busy(m):
        return jsonify({"error": "签到队列忙（定时签到进行中），请稍后再试"}), 429
    # 防错位 + bool 混淆（同 /api/accounts/batch）：
    # 签到用错位下标取到的会是他人账号的凭据，危害比管理操作更直接
    phones_in = data.get("phones")
    expect = {}
    if isinstance(phones_in, list) and len(phones_in) == len(ids):
        expect = {
            i: str(phones_in[k]).strip() for k, i in enumerate(ids) if type(i) is int
        }
    phones = []
    for i in ids:
        if not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < len(accounts):
            continue
        acc = accounts[i]
        # 双侧 _mask_phone 归一（出站为脱敏号，见 _stale_idx_guard 注释）
        if i in expect and m._mask_phone(expect[i]) != m._mask_phone(str(acc.get("phone", ""))):
            return jsonify({"error": "账号列表已变化，请刷新页面后重试"}), 409
        if acc.get("deleted") or acc.get("status") != m.ACCOUNT_STATUS_ACTIVE:
            continue
        phone = str(acc.get("phone", "")).strip()
        if phone:
            phones.append(phone)
    if not phones:
        return jsonify({"error": "选中的账号均不可手动签到（未生效或已删除）"}), 400
    # 单次批量签到账号数与 /api/accounts/batch 同口径（BATCH_OP_LIMIT）。
    # 队列子进程的等待超时按账号数缩放，无上限的"全选"会把后台队列线程长时间占死；
    # 超出上限 400，管理员分批触发（每批 ≤10 个）。
    if len(phones) > m.BATCH_OP_LIMIT:
        return jsonify({"error": f"单次批量签到最多 {m.BATCH_OP_LIMIT} 个账号"}), 400
    with state["batch_lock"]:
        if state["batch_running"]:
            return jsonify({"error": "已有批量签到正在执行，请稍后再试"}), 429
        # 批量签到冷却（防循环触发全量真实登录）——队列完成后窗口内拒绝
        cooldown = m.load_env_int(m.ENV_FILE, "YIBAN_BATCH_SIGN_COOLDOWN_SEC", 1800)
        if cooldown > 0:
            elapsed = time.time() - state["last_batch_ts"]
            if elapsed < cooldown:
                remain = int(cooldown - elapsed)
                return jsonify({
                    "error": f"批量签到冷却中（约 {remain // 60} 分 {remain % 60} 秒后可重试）"
                }), 429
        state["batch_running"] = True

    def _run_batch():
        try:
            ok, msg, proc = _spawn_signin_many(m, state, phones)
            if not ok:
                m.logger.warning("批量手动签到未启动: %s", msg)
                return
            # 等待超时按账号数缩放（默认 300s 仅够单号，多号队列会被误杀）
            m._wait_signin_proc(proc, timeout=m._batch_wait_timeout(len(phones)))
            m.logger.info("批量手动签到完成: %s 个账号（单队列、单封汇总邮件）", len(phones))
            # 非 0 退出码（如 exit 3 队列忙）如实留痕到签到日志，不冒充成功
            m._log_manual_sign_exit(
                f"批量签到 {len(phones)} 个账号", proc.returncode)
        finally:
            # 此处不再无条件重置冷却基准——它由 _spawn_signin_many 在 spawn 成功
            # 时刷新（否则失败也刷新基准，spawn 失败后 30 分钟内合法重试会被拒）。
            with state["batch_lock"]:
                state["batch_running"] = False

    threading.Thread(target=_run_batch, daemon=True).start()
    m.db.audit(
        session.get("username") or "?",
        "signin_batch",
        ",".join(m._mask_phone(p) for p in phones),
        f"批量签到 {len(phones)} 个",
    )
    return jsonify({
        "ok": True,
        "msg": f"已加入批量签到队列（{len(phones)} 个账号，合并为一个队列执行、只发一封汇总邮件，日志约几分钟内刷新）",
    })


def register(app):
    """登记本域每实例状态并注册两条手动签到路由；endpoint 取函数名（url_for 依赖）。"""
    app.extensions["yiban_signin_state"] = {
        # phone -> 上次触发时间戳（30 秒防抖）
        "last_trigger": {},
        # phone -> Popen（新触发时终止仍在运行的旧进程，防重复签到触发风控）
        "procs": {},
        # 防抖检查+赋值原子化（TOCTOU 竞态防护）
        "lock": threading.Lock(),
        # 批量签到队列互斥：同时只允许一个在跑
        "batch_running": False,
        "batch_lock": threading.Lock(),
        # 批量签到冷却基准（spawn 成功时刻；单条与批量共用同一计数）
        "last_batch_ts": 0.0,
    }
    app.add_url_rule("/api/signin", view_func=api_signin, methods=["POST"])
    app.add_url_rule("/api/signin/batch", view_func=api_signin_batch, methods=["POST"])
