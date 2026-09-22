# -*- coding: utf-8 -*-
"""pytest 全局配置：默认禁用 web.create_app 的每日清理后台线程。

原因：全量测试会调用 122+ 次 create_app()，若每次都启动 daily-purge 守护线程，
60 秒后大量线程会并发访问共享 SQLite 单例 db._conn，与各测试 teardown 的关库/重建
竞争，导致 Windows fatal exception access violation。生产环境不设置该变量，
因此默认行为不受影响；仅测试进程通过 conftest 在导入任何测试模块前启用。
"""
import logging
import os

import pytest

os.environ.setdefault("YIBAN_DISABLE_PURGE_LOOP", "1")

# 测试默认禁用邮件通知：mailer._get 环境变量优先于 .env 文件，此处设为 0
# 可防止 signin/web 测试（如 send_notification("t","c",...)）意外真实发信。
# test_mailer.py 自带 _isolate_env 清理 YIBAN_MAIL_* 后按用例显式设置，不受影响。
os.environ.setdefault("YIBAN_MAIL_ENABLE", "0")

# v0.26.3：web.create_app 会给 root logger 挂按天文件 handler（sign-*.log）。
# 测试进程默认重定向到会话级临时目录——否则 Windows 开发机会意外创建
# C:\var\log\yiban\（默认路径按当前盘符解析）。各测试类可显式覆盖该变量。
if "YIBAN_LOG_FILE" not in os.environ:
    import tempfile
    os.environ["YIBAN_LOG_FILE"] = os.path.join(
        tempfile.mkdtemp(prefix="yiban-test-logs-"), "sign.log"
    )


def _close_root_file_handlers():
    """关闭并移除 root logger 上全部文件 handler（DailyFlockFileHandler /
    普通 FileHandler 均为 logging.FileHandler 子类）。

    create_app 会给 root logger 挂按天文件 handler，测试后不关闭会在 Windows 上
    持有 sign-*.log 文件句柄——test_logs_by_date 的 setUp 删除临时按天日志时抛
    PermissionError（全量回归 11 failed）。移除 handler 后，下一轮 create_app 的
    幂等逻辑会重新挂载，行为不受影响。

    signin 的 handler 装配延迟到 main()（模块导入零副作用），
    测试进程不再出现"游离于 root 之外"的 signin._handler；历史版本需显式关闭它
    （import 期 basicConfig 被 pytest 捕获 handler 顶成 no-op、handler 未挂 root
    却已打开日志文件）。
    """
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, logging.FileHandler):
            try:
                h.close()
            finally:
                root.removeHandler(h)


@pytest.fixture(autouse=True)
def _close_root_file_handlers_after_each():
    """每个测试前后清理 root logger 残留文件 handler。

    测试前清理：create_app 挂到 root 的按天 handler 若指向已删除目录会反复写失败；
    历史版本还需处理 signin 导入期游离打开的日志文件句柄（P3-13 后装配延迟到
    main()，导入零副作用，此问题已消失）。测试后清理：移除本轮 create_app 挂到
    root 的 handler。清理不破坏 test_registration_pause.py 的断言：其断言的是
    create_app 之后 root 存在 DailyFlockFileHandler，setup 清理后再 create_app
    会由幂等逻辑重新挂载。
    """
    _close_root_file_handlers()
    yield
    _close_root_file_handlers()


def _clear_sign_claim_pool():
    """清空**当前生效连接**上的签到领取池（多执行体协调表）；异常一律不影响用例。

    为什么用"当前连接"而不是"环境变量指向的库"：连接是模块级单例，而多数测试类
    直接复用上一个类留下的连接（自己不设 `YIBAN_DB_FILE`）——引擎在新用例里用的
    就是这个连接，池子里的行就是它挡住的。清一个已属于旧类的临时库无害。
    （连接为空时**不**在此处开库：否则会顺手创建默认库，把后续类的库选择带偏。）
    """
    try:
        import db
        conn = db._conn
        if conn is None:
            return
        with db._conn_lock:
            conn.execute("DELETE FROM sign_claims")
            conn.commit()
    except Exception:  # 库/表不存在 → 该用例与领取池无关
        pass


@pytest.fixture(autouse=True)
def _reset_sign_claim_pool():
    """每个用例前清空签到领取池（`sign_claims`，多执行体协调表）。

    为什么需要：领取池的语义是"**一个账号一个业务日只被一个执行体做一次**"
    （成功/已签到/今日无任务记 `done`，其余落 `failed` 并放开租约可被接手）。
    而单元测试普遍在**同一个库、同一天、同一个手机号**上反复调用
    `run_queue_retry` 来验证不同分支——不清池的话，第二条用例的账号会直接
    "已被领取"而根本不发起尝试，断言会以"没发请求/没写状态"的形式失败，
    看起来像实现坏了，其实是池子记住了上一轮。

    这也正是生产语义：同一账号同一天的重复自动执行本就该被拒（防重复登录），
    只有补签轮（接手 `failed`）与手动指定账号（`reclaim=True`）才继续做。

    清理放在**用例之后**：不少测试类在 setUp 里才把 `YIBAN_DB_FILE` 指向临时库
    （夹具先跑就看不到那个库），而用例结束时环境与连接都还是该类的，判定最准。
    """
    yield
    _clear_sign_claim_pool()


@pytest.fixture(autouse=True, scope="class")
def _restore_environ_around_class():
    """类级 os.environ 快照：setUpClass 的改动在 tearDownClass 之后整体还原。

    大量测试类在 setUpClass 里把 YIBAN_STATE_DIR / YIBAN_ENV_FILE / YIBAN_DB_FILE
    等指向各自的临时目录，tearDownClass 只 rmtree、不还原环境变量（个别类干脆 pop
    conftest 设的会话默认值）。这些键被带进后续用例后指向已删目录，会让无关断言
    以间歇形式失败——实测串行组合
    tests/test_mailer.py + test_scheduler_gate 的锚点默认路径断言
    必挂；xdist `-n 8` 下同一 worker 跨文件执行时表现为随机 1~2 项失败
    （已复现并修复：test_scheduler_gate 锚点默认路径、test_web_auth_security 的
    purge 线程门）。

    在类边界统一快照/还原，既保留 setUpClass 为类内用例准备的隔离环境，
    又不把该环境泄漏给其它文件；单个文件里忘还原的类也一并被兜住。
    """
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


def pytest_sessionfinish(session, exitstatus):
    """进程收尾兜底：即便个别测试异常中断，也释放全部文件句柄。"""
    _close_root_file_handlers()
