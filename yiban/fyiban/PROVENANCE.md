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
| `algo.py::generate_position_in_polygon` | 同上 `Point.kt::createScaledPolygon`（62–76，缩放系数 0.7）+ `generateRandomPointInCenter`（95–121）+ `generateRandomPointsInCenter`（123–136） | **差异（有意，勿改回）**：上游（及本层旧实现）绕**顶点算术平均**撒点，凹围栏（L 形、门形）的算术平均落在围栏外，主采样的双验收一次都过不了，终极兜底还会交出未校验的形外点。本实现改为**剪耳三角剖分（`_ear_clip_triangles`）+ 按面积加权选三角形 + 顶点向三角形内心内缩 `TRI_SHRINK_RATIO` + 三角形内 sqrt 形变均匀采样**：三角形是凸集，顶点向内心靠拢后仍整体在原三角形内，因此取点不可能出界，凹性被剖分吸收掉。内缩比例取 0.7 的理由是 GPS 有几十米漂移、贴边的采样点容易被判在围栏外（与 `SCALE_FACTOR` 同量级）。取出的点仍与 `point_in_polygon` 核一次，核不上就交回旧路径。**保留**：`SCALE_FACTOR = 0.7`、**均匀分布 ±0.1 跨度**（上游为正态偏移、最多 100 次尝试）、5000 次尝试、"质心 + 1% 抖动"兜底——它们只服务回退路径（避免多账号多次签到聚在质心附近形成行为指纹）。随机源仍是 `secrets.SystemRandom()`。**局限**：① 非简单多边形（自交，如一笔画五角星）与共线退化顶点不在支持范围，剖分被拒后**回退旧采样、可能出界与旧实现同档**；② 顶点顺序视为围栏数据的一部分（服务端给有序环），乱序输入让"内/外"判定本身失去意义，不保证界内；③ 采样在围栏内均匀，真人的位置分布偏向入口——要不要加权属产品决定。 |
| `headers.py::KILLYIBAN_HEADERS`（Origin/User-Agent/AppVersion 三项） | `Core/SchoolBased.kt::headers()`（第 31–36 行） | **特征取自上游**；`AppVersion` 已随易班官方客户端升到 5.2.3（上游为 5.1.2，按安装包清单实测更新）。 |
| `headers.py::HEADERS`（iOS UA 全文） | 上游无此 UA | 本地补充：旧版 iOS 登录流程使用的客户端指纹（`YibanClient` 的 `use_killyiban=False` 分支）。 |
| `waf.py::solve_ydclearance` | `Core/BaseReq.kt`（cookie 注入与跳转跟随，第 20–60 行区段） | **任务相同**（处理易盾 `https_ydclearance` 挑战、取回 cookie 与跳转目标）。**差异**：上游交给 OkHttp/CookieJar 与 JS 运行时；本实现是**不执行远程代码**的纯 Python 确定性解析（正则提取常量 + 复刻三步变换）。 |
| `waf.py::looks_like_challenge` | 同上（上游按响应特征判断是否进挑战分支） | **特征一致**（`Set-Cookie` 含 `https_ydclearance`，或页面含 `window.onload=setTimeout` + `eval("qo=eval;qo(po);")`）。"是不是挑战页"属平台识别留本层；"这个跳转能不能信"仍靠注入的白名单。 |
| `protocol.py` 全部端点/参数/正则 | `Core/*Req.kt`、`Core/SchoolBased.kt`、登录流程（KillYiBan `p101w2/b.java`） | **平台事实**：端点与客户端标识、OAuth 五步（旧流程）/四步（KillYiBan）顺序、页面正则、`scope`/`display` 取值、成功标志（`code == "s200"`）、`OutState` 取值（`1` vs `1.0`）。均为复刻，非本项目发明。 |
| `protocol.py::encrypt_password` | 同上（RSA-1024 + PKCS1_v1_5 + base64 提交密码） | **编码方式一致**。**差异（有意）**：提交前加"密码超过 117 字节"的显式报错——上游让 pycryptodome 的底层异常直接冒出，用户看不懂也改不了。 |

**不在本层的第三方内容**：上游的 Android UI、Gradle/Kotlin 构建、持久化 Cookie 实现、
网络重试机制均未使用（本项目的对应实现为原创：`yiban/attempt`、`yiban/schedule`、
`yiban/alerting`、`yiban/store`、`web/`）。

## 本项目自有的安全与合规约束（**不得下沉进本层**）

1. **URL 白名单**：`waf.solve_ydclearance(text, allow_url=...)` 的 `allow_url` 由调用方注入
   （生产为 `yiban.security` 的 f.yiban.cn 白名单），第三方层不内联安全校验。
   `protocol.py` 的登录握手同理：每一步的跳转都要过 `policy.require_trusted` /
   `policy.require_fyiban`，WAF 拦截判定走 `policy.require_not_blocked`
   （`tests/test_fyiban_isolation.py::ProtocolLayerTest` 钉住"协议层不得自算裁决"）。
2. **脱敏与日志**：本层不打账号标识、不做脱敏——错误消息里的 URL/文本一律经
   `policy.sanitize` / `policy.describe_location`；本层只保留进程内的步骤日志
   （`logger.info("登录成功")` 之类，日志口径由调用方配置）。
3. **会话缓存**：协议层只通过注入的 `session_store`（`restore`/`save`/`clear`）使用它，
   不 import `yiban.store`、不读 `db`——会话缓存是本市集的手段（少登录=少风控暴露面），
   不是上游概念。
4. **许可**：AGPL-3.0 全文随代码分发（`LICENSE`），并在仓库根 `README.md` 的致谢与
   《用户协议》中向使用者说明"核心算法来自上游、本项目为衍生的完整源码提供者"
   （AGPL §5 修改声明与 §13 源码提供义务）。

## 变更纪律

改本层文件时：
- **同步更新本文件**的对照表（新增/删除/差异）；
- 若上游算法被替换或升级，先改 `algo.py`/`headers.py`/`waf.py`/`protocol.py` 并让
  `tests/test_fyiban_isolation.py`（隔离边界）与 `tests/test_login_protocol_shape.py`
  （登录五步的请求形状、WAF 分支、白名单拒绝边界）先红后绿；
- 登录路径是**钱路**：改动后除全量测试外，还要用 `scripts/loadtest/mock_yiban.py`
  做一次端到端假服务端演练。
