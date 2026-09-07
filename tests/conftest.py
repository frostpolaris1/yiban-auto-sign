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
    """关闭并移除 root logger 上全部文件 handler（_DailyFlockFileHandler /
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
    create_app 之后 root 存在 _DailyFlockFileHandler，setup 清理后再 create_app
    会由幂等逻辑重新挂载。
    """
    _close_root_file_handlers()
    yield
    _close_root_file_handlers()


def pytest_sessionfinish(session, exitstatus):
    """进程收尾兜底：即便个别测试异常中断，也释放全部文件句柄。"""
    _close_root_file_handlers()
