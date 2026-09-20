# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""手动签到子进程族：等待与回收、队列超时缩放、退出码语义与留痕。

**功能**
等待手动签到子进程 `_wait_signin_proc`（超时先 terminate 再 kill 兜底）；批量签到队列
的等待超时按账号数缩放 `_batch_wait_timeout`；子进程非 0 退出码 → 用户可见原因
`_manual_sign_failure_reason`（含退出码表 `_SIGNIN_EXIT_REASONS`）与退出留痕
`_log_manual_sign_exit`（写当天签到日志）。

**归属**
原 `web/app.py` 的模块级手动签到辅助，唯一真源在本模块；`web/app.py` 只保留名字面与
转发，把它自己持有、而本模块需要的入口——按天日志文件路径 `log_path_for`——在调用
时刻现取后注入（它内部还要现取本进程的 `LOG_FILE`，会被测试赋值改写）。

**复用**
`_log_manual_sign_exit` 复用 `_manual_sign_failure_reason` 的退出码词表，两处不会各写
一套原因文案；`_wait_signin_proc` 的默认超时保持 300s，批量场景由调用方用
`_batch_wait_timeout` 显式传参缩放，不改默认值。

**通信**
本模块不反向导入 `web.app`（本仓测试以别名加载 `app.py`，普通 import 会再执行一份副本
模块）。`log_path_for` 作为显式参数接收：它在 `web.app` 上是转发包装（现取 `LOG_FILE`），
本模块另持绑定会让改日志目录的测试静默失效。本族函数**不读 `session` / `request`**：
`_log_manual_sign_exit` 由 `web/routes/signin_api.py` 的批量队列后台线程与子进程回收
线程调用（非请求上下文），只依赖时钟与日志路径两样进程级事实。子进程模块经
`import subprocess` 取用（测试以 `webapp.subprocess.Popen` 打桩，同一模块对象）。
"""

import logging
import subprocess

from yiban import clock

# 与 web.app 同名的日志通道：本族的告警落回既有通道，便于运维沿用同一处过滤
logger = logging.getLogger("web")


def _wait_signin_proc(proc, timeout=300):
    """等待手动签到子进程；超时则终止并回收，避免批量签到队列被卡死。

    原 proc.wait(timeout=300) 超时抛出 TimeoutExpired 后未回收子进程，
    队列仍会继续触发下一个账号，造成并发签到。超时后先 terminate，再等待
    回收；仍不退则 kill 兜底。
    """
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _batch_wait_timeout(count):
    """批量签到队列的等待超时按账号数缩放。

    固定 300s 在多账号场景过紧：每号真实登录+网络余量约 2 分钟，10 号队列
    原本会被 300s 截断误杀。公式 max(300, 120 * count + 300)——单账号 420s、
    10 号（BATCH_OP_LIMIT 上限）1500s；300s 下限兜底空队列/边界。
    _wait_signin_proc 的默认参数保持 300 不动，由调用方传参缩放。
    """
    return max(300, 120 * count + 300)


# 手动签到子进程非 0 退出码 → 用户可见原因（与 signin.py 退出码表同口径）
_SIGNIN_EXIT_REASONS = {
    2: "子进程自行退出（时段外跳过/暂停/存在未了结账号），本轮未实际签到",
    3: "签到队列忙（运行锁被其他签到进程持有），本轮未执行",
}


def _manual_sign_failure_reason(returncode):
    """手动签到子进程退出码的用户可见原因；0/None 返回 None（正常）。"""
    if not returncode:
        return None
    return _SIGNIN_EXIT_REASONS.get(
        int(returncode), f"签到子进程异常退出（退出码 {returncode}）"
    )


def _log_manual_sign_exit(phone_label, returncode, log_path_for):
    """手动签到子进程异常退出写入签到日志——前端「结果稍后出现在日志」的
    唯一结果通道：exit 3（队列忙）等此前被静默吞掉，用户看到已触发实际没签。

    日志路径由调用方传入（`web.app` 的 `log_path_for`）：它现取本进程的 `LOG_FILE`，
    而该路径会被测试赋值改写、也会随运行方式变化；本函数另持绑定会让那些改写失效。
    """
    reason = _manual_sign_failure_reason(returncode)
    if not reason:
        return
    try:
        with open(log_path_for(), "a", encoding="utf-8") as fh:
            fh.write(f"[{clock.now():%Y-%m-%d %H:%M:%S}] "
                     f"[{phone_label}] ⚠️ 手动签到未完成: {reason}\n")
    except OSError:
        logger.warning("手动签到退出码留痕失败: %s (returncode=%s)", phone_label, returncode)
