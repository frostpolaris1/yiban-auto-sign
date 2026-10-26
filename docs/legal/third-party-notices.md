> [← 返回 README](../../README.md)

### 第三方组件声明

本项目分发包含以下第三方开源组件（按各自许可保留版权声明）：

| 组件 | 用途 | 许可证 |
|------|------|--------|
| Flask / Werkzeug | Web 框架 | BSD-3-Clause |
| requests / urllib3 | HTTP 客户端 | Apache-2.0 / MIT |
| PySocks | SOCKS 代理 | MIT |
| pycryptodome | AES-GCM 加密 | Public Domain（作者声明）+ BSD-2-Clause 条款 |

各组件版权声明与完整许可文本见其官方仓库 LICENSE 文件。本项目仅按各自许可条款使用，未修改上述组件源码。

### 早期参考与来源分类

本项目的易班协议与定位实现经过一次**换核**（2026-10）：早期实现参考
[OneFeiFan/FYIBAN](https://github.com/OneFeiFan/FYIBAN)（AGPL-3.0，Kotlin/Android 库）
把上游算法与协议特征在 Python 中重写；换核后**定位采样算法、平台事实常量（请求头 /
端点）与登录签到编排全部改为洁净室重写**，仓内已不含上游派生代码。

当前实现的来源按**事实**分四类：

| 能力 | 现实现 | 来源分类 |
|------|--------|----------|
| 多边形内定位点采样 | `yiban/geo.py` | **洁净室自研**：偶奇射线法是公有领域教科书算法（本实现只按教科书定义编写）；取点为通用拒绝采样；退化/自交的兜底语义由本项目定义 |
| 请求头字段值与 App 版本（`Yiban` / `AppVersion` / `Origin` / iOS UA） | `yiban/platform.py` | **平台事实**：字段值取自平台客户端实拍请求与官方安装包清单 |
| 端点、`client_id` / `redirect_uri`、`scope` / `display` 取值、成功标志 `s200` | `yiban/platform.py` | **平台事实**：平台接口的固定取值 |
| 六跳登录链路的**顺序与跳转形态** | `yiban/platform.py` | **本项目实拍**：逐端点抓包记录（TASK-C）；顺序是本项目的实现产物 |
| 授权页 / 响应信封 / 表单 / 签到体的解析与构造 | `yiban/adapters/protocol_yiban/` | **洁净室库**（AGPL-3.0-only）：唯一规格来源是第一手旁路实拍，见其 `SPEC.md` 与 `LICENSE` |
| 调度错峰、重试预算与失败分级、账密熔断、通知告警、账号与数据库、Web 管理后台、多执行体并行 | `yiban/engine/` 等 | **本项目原创** |

> 历史沿革（如实记录）：早期版本曾把上游派生代码圈在一个独立隔离层目录内（目录名取自
> 上游项目名）并随代码分发 AGPL 声明。换核后隔离层与目录内声明一并退役——判据不是
> "文档说改写了"，而是**重写验证**：新的射线法与已退役实现在 9 块围栏 × 200 个确定性
> 点位上逐点一致（对拍位串冻结在 `tests/test_geo_sampling.py`），采样分布与兜底语义
> 另有独立行为测试。`OneFeiFan/FYIBAN` 其后声明参考了
> [Qs315490/fyiban](https://github.com/Qs315490/fyiban)（无许可证）；本项目与该无许可证
> 项目无代码关系。

**许可**：本项目整体以 **AGPL-3.0** 分发（根 `LICENSE`），与上游项目同一许可版本；
`yiban/adapters/protocol_yiban/` 是**洁净室库**，与全项目同以 **AGPL-3.0-only** 分发
（许可全文随包分发于 `yiban/adapters/protocol_yiban/LICENSE`，内容＝根 `LICENSE`）。
（2026-10-21 用户裁决：原先协议包单独用 MIT 以利社区复用，现统一改 AGPL。）

## 开源致谢 / Acknowledgements

本项目的签到引擎与网页管理后台建立在一批优秀开源组件之上。以下按「名称 / 用途 / 许可证」列出实际使用的第三方组件；完整版权声明与许可文本见各组件官方仓库 LICENSE，前端资源的来源、版本与哈希另见 [`web/static/vendor/MANIFEST.md`](web/static/vendor/MANIFEST.md)。

### 网页前端（自托管于 `web/static/vendor/`，不引用任何外网 CDN）

| 组件 | 用途 | 许可证 |
|------|------|--------|
| [Adminator](https://github.com/puikinsh/Adminator-admin-dashboard) 4.3.0 | 全站设计系统：设计令牌（含暗色）、外壳布局、组件样式 | MIT |
| [Chart.js](https://www.npmjs.com/package/chart.js) 4.5.1 | 数据总览页折线 / 柱状 / 环形图表（按需注册的裁剪构建） | MIT |
| [Lucide](https://github.com/lucide-icons/lucide) 1.45.0 | 全站线性图标（70 个 symbol 子集；部分图标源自 Feather） | ISC（Feather 部分 MIT） |
| [Inter](https://github.com/rsms/inter) / [Noto Sans SC](https://fonts.google.com/noto/specimen/Noto+Sans+SC) / [JetBrains Mono](https://www.jetbrains.com/lp/mono/) | 自托管界面字体：拉丁 / 中文 / 等宽 | SIL OFL-1.1 |
| md-render.js | 更新日志的迷你 Markdown 渲染（本项目自研、零依赖，非第三方组件） | 随本项目 AGPL-3.0 |

### 后端与运行依赖（Python）

| 组件 | 用途 | 许可证 |
|------|------|--------|
| [Flask](https://flask.palletsprojects.com/) / [Werkzeug](https://werkzeug.palletsprojects.com/) / [Jinja2](https://jinja.palletsprojects.com/) / [click](https://click.palletsprojects.com/) / [itsdangerous](https://itsdangerous.palletsprojects.com/) / [MarkupSafe](https://markupsafe.palletsprojects.com/) / [blinker](https://github.com/pallets-eco/blinker) | Web 框架及其生态（路由 / WSGI / 模板 / CLI / 会话签名 / 转义） | BSD-3-Clause（blinker 为 MIT） |
| [gunicorn](https://gunicorn.org/) | 生产常驻 WSGI 服务 | MIT |
| [requests](https://requests.readthedocs.io/) | HTTP 客户端 | Apache-2.0 |
| [urllib3](https://urllib3.readthedocs.io/) / [charset-normalizer](https://github.com/Ousret/charset_normalizer) | HTTP 底层与字符集探测 | MIT |
| [certifi](https://github.com/certifi/python-certifi) | CA 根证书包 | MPL-2.0 |
| [idna](https://github.com/kjd/idna) | 国际化域名编码 | BSD-3-Clause |
| [PySocks](https://github.com/Anorov/PySocks) | SOCKS 代理（可选，`YIBAN_PROXY`） | BSD |
| [pycryptodome](https://www.pycryptodome.org/) | AES-GCM 账号凭据加密 | Public Domain + BSD-2-Clause |
| [colorama](https://github.com/tartley/colorama) | Windows 终端颜色（传递依赖） | BSD-3-Clause |

> 精确锁定版本见 [`requirements.lock`](requirements.lock)。

### 上游对照（2026-09-15 首次核对；2026-10-07 换核后改判）

上游 [OneFeiFan/FYIBAN](https://github.com/OneFeiFan/FYIBAN) 是一个 **Kotlin/Android 库**
（AGPL-3.0，约 670 行，作者 OneFeiFan）。本项目**早期**按其算法与协议在 Python 中重写
易班客户端，逐项对照如下——"判定"列是**换核后**的现判（2026-10-07 重核）：

| 能力 | 上游实现 | 本项目现实现 | 现判 |
|------|---------|--------------|------|
| App 请求指纹（UA `Yiban` / AppVersion / Origin） | `Core/SchoolBased.kt` | `yiban/platform.py` | **平台事实**：字段值取自平台客户端实拍与安装包清单（版本值随官方客户端更新） |
| CSRF 随机令牌 | `Core/SchoolBased.kt` | `yiban/platform.py` | **平台事实** + 本地加固（每次实例重生成，用密码学安全随机源） |
| 校本化 OAuth 五步登录与全部请求常量 | `Core/SchoolBasedAuth.kt` | `yiban/platform.py` + `yiban/client.py` | **平台事实（端点/参数取值）** + **本项目实拍（六跳顺序）**；另加本地特性：会话缓存分支、URL 白名单、风控识别、脱敏 |
| 密码 RSA/PKCS1v1.5 加密 | `Core/SchoolBasedAuth.kt` | `yiban/platform.py` → `yiban/adapters/protocol_yiban/crypto.py` | **平台事实（协议形态）**；实现属洁净室库（AGPL-3.0-only），另加本地长度守卫 |
| 登录成功判据 `code == "s200"` | `Core/SchoolBasedAuth.kt` | `yiban/platform.py` | **平台事实**（响应取值） |
| `nightAttendance` 的 `signPosition` / `signIn` 请求构造 | `Core/TaskFeedback.kt` | `yiban/platform.py` + `yiban/adapters/protocol_yiban/forms.py` | **平台事实（字段与编码形态，取自实拍）**；构造由洁净室库承担，多任务遍历/容错分支为本项目实现 |
| 定位点算法（缩放质心 + 射线法） | `tool/Point.kt` | `yiban/geo.py` | **洁净室自研**（2026-10-07 重写）：射线法按教科书偶奇定义重写、取点改外接矩形拒绝采样、兜底语义本项目自定；缩放质心骨架已退役 |
| **定位采样分布** | Box-Muller 正态分布 | 围栏内**精确均匀**（拒绝采样） | 本地原创 |
| **调度与错峰**（时间窗分块、锚点/σ、重试落点） | 无 | `yiban/engine/schedule.py` + `yiban/app/round.py` | 本地原创 |
| **重试预算与失败分级、账密熔断、健康探针** | 无（上游仅 HTTP 层 `retryOnConnectionFailure`） | `yiban/engine/attempts.py` + `yiban/app/probe.py` | 本地原创 |
| **通知告警**（webhook / 管理员汇总邮件 / 用户失败提醒） | 无 | `yiban/adapters/notifier/` | 本地原创 |
| **账号存储、会话缓存、审计、Web 管理后台、多执行体并行** | 无（示例里凭据硬编码，单账号） | `yiban/`、`web/`、`scripts/db.py` | 本地原创 |

上游仓库内没有任何调度、通知、Web 或数据库代码（可自行核对：其全库无 Python 文件，且除
`retryOnConnectionFailure` 外无定时/重试实现）。

**许可与署名**：本项目与上游同为 **AGPL-3.0**。换核后仓内已不含上游派生代码（判定依据见
上表"现判"列与《早期参考与来源分类》小节的重写验证），故不再保留"修改声明"式声明；
此处仍如实记录历史沿革与上游项目名。上游未在文件头或 LICENSE 中填写具体版权行，故署名
只能标注项目名与作者身份（OneFeiFan）。

### 参考项目与资料

- [AEtherside/skland-daily-attendance](https://github.com/AEtherside/skland-daily-attendance) - GitHub Actions 工作流结构与 keepalive 方案
- Auto-Test - 早期易班登录流程参考（OAuth + RSA + ydclearance，其请求特征已被本项目采用的标准 App 特征取代；挑战求解器已删除）
- [liskin/gh-workflow-keepalive](https://github.com/liskin/gh-workflow-keepalive) - 定时工作流自动续期（避免 60 天无活动被禁用）

特别感谢 [Lumjiel](https://github.com/Lumjiel) 对本项目的指导。
