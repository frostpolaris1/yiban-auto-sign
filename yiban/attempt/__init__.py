# -*- coding: utf-8 -*-
"""`yiban.attempt`：一次"对易班外呼校验/签到尝试"的编排（闸门、任务队列、收口）。

与 `yiban.store` 的分工：这里管行为与并发，store 只管表；当前只有异步校验任务 `jobs`。
"""
