# -*- coding: utf-8 -*-
"""pytest 全局配置：默认禁用 web.create_app 的每日清理后台线程。

原因：全量测试会调用 122+ 次 create_app()，若每次都启动 daily-purge 守护线程，
60 秒后大量线程会并发访问共享 SQLite 单例 db._conn，与各测试 teardown 的关库/重建
竞争，导致 Windows fatal exception access violation。生产环境不设置该变量，
因此默认行为不受影响；仅测试进程通过 conftest 在导入任何测试模块前启用。
"""
import os

os.environ.setdefault("YIBAN_DISABLE_PURGE_LOOP", "1")
