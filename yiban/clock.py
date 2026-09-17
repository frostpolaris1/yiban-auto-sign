# -*- coding: utf-8 -*-
"""时间基准：全系统的"业务时间"一律按**北京时间**（UTC+8）计算。

签到窗口、"今天"（按日状态文件 / 签到事件 / 容量口径 / 日志保留期）、周末门禁、
注销与保留期都定义在北京时间上，而 `datetime.now()` 取的是**宿主本地时区**。
部署在 UTC 主机（GitHub Actions runner、多数海外 VPS）时，北京时间 06:40 在宿主
看来是前一日 22:40——窗口闸门会把"还没开始"判成"已结束"，当天全部账号零请求，
按日文件与日统计也会落到错的日期上。故取时点一律走本模块。

固定 +8 偏移而非 zoneinfo：中国自 1991 年起无夏令时、全国单一时区，固定偏移与
tzdata 等价且省掉一项依赖——Windows 无系统 tz 数据库，缺 `tzdata` 时 `zoneinfo`
会直接抛 ZoneInfoNotFoundError。

`now()` 返回 **naive** 时间（与既有全部 naive datetime 运算兼容）；宿主本就运行
在 Asia/Shanghai 时它与 `datetime.now()` 逐秒相等，故对现有部署零行为变更。
"""
import datetime

TZ = datetime.timezone(datetime.timedelta(hours=8), name="CST")
TZ_NAME = "Asia/Shanghai"


def now():
    """当前北京时间（naive datetime，秒级精度与 datetime.now() 一致）。"""
    return datetime.datetime.now(TZ).replace(tzinfo=None)


def today():
    """当前北京日期（YYYY-MM-DD）。"""
    return now().strftime("%Y-%m-%d")


def ts():
    """当前北京时间戳串（YYYY-MM-DD HH:MM:SS，全库统一的字符串时间格式）。"""
    return now().strftime("%Y-%m-%d %H:%M:%S")
