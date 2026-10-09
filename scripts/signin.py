#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# 易班自动签到脚本（AGPL-3.0，见项目根 LICENSE）
# 本项目的易班协议与定位算法为洁净室自研实现：平台事实（端点/请求头/版本）取自平台
# 实拍与客户端安装包，六跳登录链路的顺序取自本项目自己的逐端点实拍记录，多边形定位
# 采样算法取自教科书偶奇射线法定义（详见 yiban/geo.py 与 yiban/platform.py 的文档串）。
"""
易班自动签到（**兼容壳**）

签到引擎已按"执行一轮"的边界切分到 `yiban/engine/`：
`{cli_support,accounts,schedule,state_io,alerts,probe,config_check,attempts,round,workers,runner}.py`。
本文件只剩三件事：

1. **包导入引导**：本文件仍会被 run.sh / cron / 容器调度器按**文件路径**直接执行
   （`python3 scripts/signin.py --workers 4`），那时 `sys.path[0]` 是 `scripts/`，
   仓库根必须在其中才找得到 `yiban/`；
2. **全量转发**：把上述实现模块的模块级名字（含下划线名）逐个绑定到本模块，并把本模块
   的类换成 `_ForwardingModule`：这样既有调用方与测试的 `signin.<名字> = 替身` /
   `mock.patch.object(signin, ...)` 会落到**真正持有该名字的实现模块**，实现内部的调用点
   也能看到替身。不转发就会出现"打桩静默失效"——打桩只改了壳，实现内部照旧调真名，
   测试表面通过、实则什么都没测到。
3. **入口 `main()`**：把命令行参数转交 `yiban.engine.runner.main`，并把返回的退出码交给
   `sys.exit`（runner 自身不再 `sys.exit`；退出码语义逐字不变，见 `docs/dev/cli.md` §3）。

新代码请直接 `from yiban.engine import ...`，命令行请走 `python -m yiban.cli sign`；
本壳只为部署面（run.sh / cron / 容器调度器直接执行本文件）与既有调用方保留。

**外部集成不得调用 `run_queue_retry`**：它是旧领取池（`sign_claims`）时代的**冻结**执行
路径（无生产调用点），裸名转发会让它看起来仍可用，但调用它会**静默写冻结的 `sign_claims`、
绕开唯一台账 `sign_tasks`**（当日计划与了结事实都以新表为准 ⇒ 台账分裂、补签判定失真）。
部署与第三方集成只应走 `main()`（内部经 `runner.main` → `executor_v3`）；`run_queue_retry`
留在壳上仅供既有单测按裸名取用、覆盖四柱 v2 语义。
"""


import os
import sys
import types

# 包导入引导：`yiban/` 在仓库根，而直接运行本脚本时 sys.path[0] 是 scripts/。这是
# **部署契约**（run.sh / cron / 容器调度器都按文件路径调用本脚本），故保留；命令行
# 侧的等价物是 `python -m yiban.cli sign`（由仓库根执行，无需引导）。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# 说明：`round` 模块名与内置函数同名，isort 会把带别名的导入单独成块（见下一条）。
from yiban.engine import (  # noqa: E402
    accounts,
    alerts,
    attempts,
    cli_support,
    config_check,
    probe,
    runner,
    schedule,
    state_io,
    workers,
)
from yiban.engine import (  # noqa: E402
    round as round_mod,
)

# ---------------------------------------------------------------------------
# 兼容壳机制（两条转发纪律见模块说明第 2 条）
# ---------------------------------------------------------------------------
_IMPLS = (accounts, alerts, attempts, cli_support, config_check, probe,
          round_mod, runner, schedule, state_io, workers)

#: 本壳**自己拥有**的名字（读取时优先取本模块的绑定，不被实现模块覆盖）：
#: `main` 是"转发 + 退出码"的唯一入口，而 `runner.main` 返回 int、不抛 SystemExit——
#: `signin.main()` 的既有调用方（测试与 web 子进程脚本）要的正是后者。
_SHELL_OWNED = frozenset({"main"})


def _forward_all_names(impls):
    """把实现模块的模块级名字（含下划线名）逐个绑定到本壳，转发的是同一对象。

    跳过 dunder：`__doc__` / `__name__` / `__file__` 等必须留在壳自己身上，否则
    `import signin` 拿到的模块元信息会指向实现模块，命令行与日志里的来源也会错位。
    名字用程序化方式逐个拉取，不手抄——手抄上百个名字迟早与实现漂移。
    """
    for impl in impls:
        for name, value in list(vars(impl).items()):
            if name.startswith("__") and name.endswith("__"):
                continue
            globals()[name] = value


_forward_all_names(_IMPLS)


class _ForwardingModule(types.ModuleType):
    """壳模块：读取与写入都落到真正持有该名字的实现模块（见模块说明第 2 条）。

    模块级赋值走 `__dict__` 直写、不触发 `__setattr__`，故本转发只影响外部打桩
    （`setattr`），不影响本文件自身的名字绑定；而 `setattr` 同时写壳自己的字典，
    使本文件内部函数里的裸名调用也能看到替身。
    """

    def __getattribute__(self, name):
        if name.startswith("__") and name.endswith("__"):
            return types.ModuleType.__getattribute__(self, name)
        if name in _SHELL_OWNED:
            return types.ModuleType.__getattribute__(self, name)
        for impl in types.ModuleType.__getattribute__(self, "_IMPLS"):
            try:
                return getattr(impl, name)
            except AttributeError:
                continue
        return types.ModuleType.__getattribute__(self, name)

    def __setattr__(self, name, value):
        if not name.startswith("__"):
            for impl in types.ModuleType.__getattribute__(self, "_IMPLS"):
                if hasattr(impl, name):
                    setattr(impl, name, value)
        types.ModuleType.__setattr__(self, name, value)


sys.modules[__name__].__class__ = _ForwardingModule


def main(argv=None):
    """兼容壳入口：转交实现模块，并把返回的退出码交给 `sys.exit`。

    `signin.main()`（无参）读取 `sys.argv[1:]`，与迁移前的签名逐字兼容；退出码语义
    （0/1/2/3/10）与迁移前完全一致，见 `docs/dev/cli.md` §3。
    """
    return sys.exit(runner.main(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
