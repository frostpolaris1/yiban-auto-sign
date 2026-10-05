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

**另有来源（已换核）**：`waf.py` 原 `solve_ydclearance` 的挑战形状提取（四段正则）曾源自
[`sdk250/Auto-Test`](https://github.com/sdk250/Auto-Test)（`parse.py::ydclearance`）。该仓库
**无 LICENSE（默认全权保留，约束严于 AGPL-3.0）**。B' 换核后该求解器（生产全历史零触发）
已按裁决删除，本目录不再含 Auto-Test 的代码/正则。

**洁净室协议库（vendored）**：`protocol.py` 的纯解析/构造（授权页、usersure 表单、
verify_request、签到体）与 `waf.py` 的检测本体已换核到 `yiban/_vendor/yiban_protocol`
（MIT；来源 commit `c20ad68`、版本 `0.1.0`，同步纪律见 `yiban/_vendor/VENDORED.md`）。

## 逐块对照

| 本目录文件 | 上游位置 | 关系 |
|------------|----------|------|
| `algo.py::point_in_polygon` | `yiban/src/main/java/com/feifan/yiban/tool/Point.kt::isPointInPolygon`（第 77–93 行） | **算法一致**（射线法，同样的交叉判定式）。**差异**：本实现给分母加 `1e-12` 防水平边除零（上游无此保护）。 |
| `algo.py::generate_position_in_polygon` | 同上 `Point.kt::createScaledPolygon`（62–76，缩放系数 0.7）+ `generateRandomPointInCenter`（95–121）+ `generateRandomPointsInCenter`（123–136） | **差异（有意，勿改回）**：上游（及本层旧实现）绕**顶点算术平均**撒点，凹围栏（L 形、门形）的算术平均落在围栏外，主采样的双验收一次都过不了，终极兜底还会交出未校验的形外点。本实现改为**确定性自交判定（`_has_self_intersection`，不相邻边真交叉检测）+ 剪耳三角剖分（`_ear_clip_triangles`）+ 按面积加权选三角形 + 顶点向三角形内心内缩 `_TRI_SHRINK_RATIO` + 三角形内 sqrt 形变均匀采样**：三角形是凸集，顶点向内心靠拢后仍整体在原三角形内，因此取点不可能出界，凹性被剖分吸收掉。内缩比例取 0.7 的理由是 GPS 有几十米漂移、贴边的采样点容易被判在围栏外（与 `SCALE_FACTOR` 同量级）。剖分很贵（O(n²)）而围栏按学校固定、同一轮多账号共用同一多边形，故**进程内按顶点浮点元组缓存**剖分与面积前缀和（模块级 LRU 8 条 `_TRIANGULATION_CACHE`），命中即跳过剪耳——本地差异，上游无此缓存。取点**不再逐点复核**：改为建表时抽 200 点自检（`_SELF_CHECK_SAMPLES`）作为第二道防线；**是否走新路径由建表期的确定性自交判定决定**——判定通过者才走新路径，未通过（自交）一律回退旧采样，判定结果随采样表缓存，不可用者记负缓存免得反复重建。**保留**：`SCALE_FACTOR = 0.7`、**均匀分布 ±0.1 跨度**（上游为正态偏移、最多 100 次尝试）、5000 次尝试、"质心 + 1% 抖动"兜底——它们只服务回退路径（避免多账号多次签到聚在质心附近形成行为指纹）。随机源仍是 `secrets.SystemRandom()`。**局限**：① 自交检测为建造期的确定性边相交判定（只查真交叉，端点在边上/共线重叠这类退化由剪耳面积守恒与建表自检兜底）；未通过者回退旧采样，旧采样在凹围栏上可能出界、与旧实现同档；② 顶点顺序视为围栏数据的一部分（服务端给有序环），乱序输入让"内/外"判定本身失去意义，不保证界内；③ 采样在围栏内均匀，真人的位置分布偏向入口——要不要加权属产品决定。 |
| `headers.py::KILLYIBAN_HEADERS`（Origin/User-Agent/AppVersion 三项） | `Core/SchoolBased.kt::headers()`（第 31–36 行） | **特征取自上游**；`AppVersion` 已随易班官方客户端升到 5.2.3（上游为 5.1.2，按安装包清单实测更新）。 |
| `headers.py::HEADERS`（iOS UA 全文） | 上游无此 UA | 本地补充：旧版 iOS 登录流程使用的客户端指纹（`YibanClient` 的 `use_killyiban=False` 分支）。 |
| `waf.py::solve_ydclearance` | 原 `sdk250/Auto-Test` `parse.py::ydclearance` | **已删除（B' 裁决）**：生产全历史零触发的纯 Python 挑战求解器；检测命中不再求解，改由协议层 `CHALLENGE_DETECTED_MESSAGE` 响亮失败（不可重试硬失败档）。删除后本目录不再含 Auto-Test 派生代码。 |
| `waf.py::looks_like_challenge` | 本层既有特征 + vendored `yiban_protocol.looks_like_challenge` | **检测委托 vendored 库**，适配层保留本项目既有特征（`Set-Cookie` 含 `https_ydclearance`，或页面含 `window.onload=setTimeout` + `eval("qo=eval;qo(po);")`），取"现实现特征集 ∪ 库特征集"并集（只加不减）。"是不是挑战页"属平台识别留本层；求解与跳转白名单注入面已随求解器删除。 |
| `protocol.py` 端点/参数/请求编排 | `Core/*Req.kt`、`Core/SchoolBased.kt`、登录流程（KillYiBan `p101w2/b.java`）+ TASK-C 六跳实拍契约 | **平台事实**：端点与客户端标识、`scope`/`display` 取值、成功标志（`code == "s200"`）；六跳顺序以 TASK-C《引擎真实链路逐端点分析》实拍为准。**归因修正**：① 旧流程实测 **6 次请求**；② 旧流第 4 步原挑战分支已删（不再求解）；③ `OutState` 的 `1`/`1.0` 是两个参考实现各自的取值（KillYiBan `1` / 旧 Auto-Test 脚本 `1.0`），官方 App 真值未取得，**不属平台事实**。 |
| `protocol.py` 纯解析/构造（授权页、usersure 表单、verify_request、签到体） | 换核到 `yiban/_vendor/yiban_protocol`（MIT，commit `c20ad68`） | **薄委托**：`parse_login_page`→`parse_authorize_page`、`build_usersure_form`、`extract_verify_request`、`build_sign_in_body`；本文件不再内联页面正则与表单字段表。`encrypt_password` 委托 `crypto.encrypt_password`（PEM 入参）。 |
| `protocol.py::encrypt_password` | 同上（RSA-1024 + PKCS1_v1_5 + base64 提交密码） | **编码方式一致**。**差异（有意）**：提交前加"密码超过 117 字节"的显式报错——库底层 pycryptodome 超长时抛 `ValueError`，我们换成可执行的处置建议。 |
| `protocol.py::login_killyiban` 尾部签发回执判据 | 上游 `Core/SchoolBasedAuth.kt::auth`（核对版本 `d854182`）最终认证**只判 `code != 0`** | **本项目自有的假成功防线，非上游知识**：`code==0` 但 `data` 载荷缺失/为 null 一律拒认登录成功（不落成功日志、不写会话缓存）。形状依据：同一端点的入口步应答与同族签到接口的成功载荷都带 `data`（`login_legacy`/`client` 的读法即在此），`mock_yiban` 录制成功形状为 `{"code":0,"data":{},"msg":""}`——空容器是录制到的真实成功形状之一，故判据只拒"无回执"、不拒空载荷。拒绝文案的词元经 `security.HARD_FAIL_TOKENS` 落不可重试档（判断本体在本项目安全层，本层只按形状把关）。 |

**不在本层的第三方内容**：上游的 Android UI、Gradle/Kotlin 构建、持久化 Cookie 实现、
网络重试机制均未使用（本项目的对应实现为原创：`yiban/attempt`、`yiban/schedule`、
`yiban/alerting`、`yiban/store`、`web/`）。

## 本项目自有的安全与合规约束（**不得下沉进本层**）

1. **URL 白名单**：协议层每一步的跳转都要过 `policy.require_trusted`，WAF 拦截判定走
   `policy.require_not_blocked`；挑战**检测**命中由协议层用 `waf.CHALLENGE_DETECTED_MESSAGE`
   响亮失败（不求解，白名单注入面随求解器删除）。
   （`tests/test_fyiban_isolation.py::ProtocolLayerTest` 钉住"协议层不得自算裁决"）。
2. **脱敏与日志**：本层不做脱敏、也不自带打码规则——账号标识入日志与错误消息前一律经
   注入的 `policy.mask_account`（`logger.info(f"[{policy.mask_account(phone)}] 登录成功")`
   之类），URL/文本一律经 `policy.sanitize` / `policy.describe_location`；`policy` 未注入时
   即"不脱敏"，由调用方承担。
3. **会话缓存**：协议层只通过注入的 `session_store`（`restore`/`save`/`clear`）使用它，
   不 import `yiban.store`、不读 `db`——会话缓存是本市集的手段（少登录=少风控暴露面），
   不是上游概念。
4. **许可**：AGPL-3.0 全文随代码分发（`LICENSE`），并在仓库根 `README.md` 的致谢与
   《用户协议》中向使用者说明"核心算法来自上游、本项目为衍生的完整源码提供者"
   （AGPL §5 修改声明与 §13 源码提供义务）。vendored 协议库为 MIT，其许可文本随
   代码置于 `yiban/_vendor/yiban_protocol/LICENSE`。

## 变更纪律

改本层文件时：
- **同步更新本文件**的对照表（新增/删除/差异）；
- 若上游算法被替换或升级，先改 `algo.py`/`headers.py`/`waf.py`/`protocol.py` 并让
  `tests/test_fyiban_isolation.py`（隔离边界）与 `tests/test_login_protocol_shape.py`
  （旧流 6 次请求的形状、WAF 分支、白名单拒绝边界）先红后绿；
- 登录路径是**钱路**：改动后除全量测试外，还要用 `tests/fake_yiban_server.py`
  做一次端到端假服务端演练（原 `scripts/loadtest/mock_yiban.py` 已于 2026-09 迁入 tests/）。
