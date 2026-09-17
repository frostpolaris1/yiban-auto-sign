# -*- coding: utf-8 -*-
"""日志落盘处理器：跨进程互斥 + 按天滚动。

签到进程与 web 进程写同一批 `sign-YYYY-MM-DD.log`（web 的「日志」页与运维排查
都读它），必须：

- **跨进程互斥**：并发写入时行不交错——经 `yiban/infra/locks.py` 统一加锁，真无法
  加锁时由 locks 告警留痕；
- **按天滚动**：常驻的 web 进程跨天自动换文件，与签到子进程"按天分文件"同口径。

日期口径取 `yiban.clock`（北京时间），与签到事件/状态文件一致。
"""
import logging
import os

from yiban.infra import locks

from . import clock


class FlockFileHandler(logging.FileHandler):
    """带文件锁的日志处理器：防止多进程并发写入同一日志文件时行交错。"""

    def emit(self, record):
        try:
            with locks.file_lock(self.baseFilename):
                super().emit(record)
        except Exception:
            self.handleError(record)


class DailyFlockFileHandler(FlockFileHandler):
    """按天滚动 + 跨进程互斥的 web 日志 handler。

    常驻的 web 进程（gunicorn 走 create_app()、不执行 main()）必须与签到子进程写
    同一批按天文件：

    - 继承 flock 版 FileHandler：与 cron 子进程并发写同一文件时行不交错；
    - emit 时按当前日期切换目标文件（常驻进程跨天自动滚动），rollover 后先重开
      文件再交父类 emit，保证 flock 覆盖本次写入（否则首条日志逃过跨进程互斥）。
    """

    def __init__(self, log_dir):
        self._log_dir = log_dir
        self._day = clock.now().strftime("%Y-%m-%d")
        super().__init__(
            os.path.join(log_dir, f"sign-{self._day}.log"), encoding="utf-8"
        )

    def emit(self, record):
        # 滚动分支（close/baseFilename 更新/_open）整体 try 兜底——跨天滚动 +
        # 日志目录故障（如目录被删）时，FileNotFoundError 不得传播到业务请求线程
        # 引发 500；失败仅 handleError（降级不阻断业务），且 _day/stream 状态保证
        # 下一条日志仍会重试 _open（stream 置 None → 重新打开）。
        try:
            today = clock.now().strftime("%Y-%m-%d")
            if today != self._day:
                self._day = today
                self.close()  # 关闭旧日期文件句柄
                # 换目标文件：FileHandler 在 stream 为 None 时按 baseFilename 惰性重开
                self.baseFilename = os.path.abspath(
                    os.path.join(self._log_dir, f"sign-{today}.log")
                )
                self.stream = None
            if self.stream is None:
                # 先打开再交给父类 emit：父类的 flock 依赖已打开的 stream
                self._open()
        except Exception:
            self.handleError(record)
            return
        super().emit(record)
