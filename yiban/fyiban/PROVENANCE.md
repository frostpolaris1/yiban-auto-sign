# 衍生来源声明（PROVENANCE）

本目录是**第三方隔离层**：收录沿用自上游开源项目的算法与协议特征，以便独立核对、
替换或升级。本项目的其余部分（调度、重试、通知、数据库、Web 界面）为原创。

## 上游

| 项 | 值 |
|----|-----|
| 项目 | [`onefeifan/fyiban`](https://github.com/onefeifan/fyiban) |
| 语言 | Kotlin（Android 库，**无 Python 实现**——本项目为 Python 复刻，不是移植工具链产物） |
| 许可 | **AGPL-3.0**（全文副本见同目录 `LICENSE`） |
| 核对版本 | `d854182`（"增加网络请求重试机制"，2026-09-16 拉取核对） |

## 逐块对照

| 本目录文件 | 上游位置 | 关系 |
|------------|----------|------|
| `algo.py::point_in_polygon` | `yiban/src/main/java/com/feifan/yiban/tool/Point.kt::isPointInPolygon`（第 77–93 行） | **算法一致**（射线法，同样的交叉判定式）。**差异**：本实现给分母加 `1e-12` 防水平边除零（上游无此保护）。 |
| `algo.py::generate_position_in_polygon` | 同上 `Point.kt::createScaledPolygon`（62–76，缩放系数 0.7）+ `generateRandomPointInCenter`（95–121）+ `generateRandomPointsInCenter`（123–136） | **数学骨架一致**（缩放质心 0.7，采样点须同时落在缩放多边形与原始多边形内）。**差异（有意，勿改回）**：上游按正态分布偏移、最多 100 次尝试；本实现改为**均匀分布 ±0.1 跨度**、5000 次尝试，并加"质心 + 1% 抖动"兜底——目的是避免多账号多次签到聚在质心附近形成行为指纹。 |
| `headers.py::KILLYIBAN_HEADERS`（Origin/User-Agent/AppVersion 三项） | `Core/SchoolBased.kt::headers()`（第 31–36 行） | **特征取自上游**；`AppVersion` 已随易班官方客户端升到 5.2.3（上游为 5.1.2，按安装包清单实测更新）。 |
| `headers.py::HEADERS`（iOS UA 全文） | 上游无此 UA | 本地补充：旧版 iOS 登录流程使用的客户端指纹（`YibanClient` 的 `use_killyiban=False` 分支）。 |
| `waf.py::solve_ydclearance` | `Core/BaseReq.kt`（cookie 注入与跳转跟随，第 20–60 行区段） | **任务相同**（处理易盾 `https_ydclearance` 挑战、取回 cookie 与跳转目标）。**差异**：上游交给 OkHttp/CookieJar 与 JS 运行时；本实现是**不执行远程代码**的纯 Python 确定性解析（正则提取常量 + 复刻三步变换）。 |

**不在本层的第三方内容**：上游的 Android UI、Gradle/Kotlin 构建、持久化 Cookie 实现、
网络重试机制均未使用（本项目的对应实现为原创：`yiban/attempt`、`yiban/schedule`、
`yiban/alerting`、`yiban/store`、`web/`）。

## 本项目自有的安全与合规约束（**不得下沉进本层**）

1. **URL 白名单**：`waf.solve_ydclearance(text, allow_url=...)` 的 `allow_url` 由调用方注入
   （生产为 `yiban.security` 的 f.yiban.cn 白名单），第三方层不内联安全校验。
2. **脱敏与日志**：本层不打日志、不带账号标识；日志口径由调用方决定。
3. **许可**：AGPL-3.0 全文随代码分发（`LICENSE`），并在仓库根 `README.md` 的致谢与
   《用户协议》中向使用者说明"核心算法来自上游、本项目为衍生的完整源码提供者"
   （AGPL §5 修改声明与 §13 源码提供义务）。

## 变更纪律

改本层文件时：
- **同步更新本文件**的对照表（新增/删除/差异）；
- 若上游算法被替换或升级，先改 `algo.py`/`headers.py`/`waf.py` 并让
  `tests/test_fyiban_isolation.py` 的行为断言先红后绿——该文件钉住了当前采样分布、
  版本号一致性与挑战解析的可接受/拒绝边界。
