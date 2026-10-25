# 原理与实现

> 本文由仓库根 `README.md` 的「原理与实现」一节拆出，正文逐字保留。

<details>
<summary>🔬 展开：模块树 / 签到流程 / 定位算法 / 重试机制 / WAF 检测 / 续期</summary>

### 项目架构

```
web/            Flask 管理后台（账号管理/审核/用户管理/日历/手动签到）
   │
   ├── scripts/signin.py        兼容壳 → yiban/app/runner.py（签到引擎）
   ├── scripts/db.py            兼容壳 → yiban/adapters/persistence/db.py（SQLite 连接与迁移）
   └── yiban/                   共享包（web 与签到引擎共用）
            ├── entry/cli/       统一命令行入口（`python -m yiban.entry.cli <子命令>`）
            │                     ← 旧入口壳已于 145-E 退役
            ├── app/             用例编排：runner 编排 / round 队列重试 / probe 探针
            ├── core/clock.py    业务时间唯一入口（北京时间）
            ├── core/window.py   签到窗口唯一事实源（排计划/判关闭/算容量同源）
            ├── core/status.py   签到状态词汇表
            ├── core/masking.py  脱敏（手机号/日志文本/URL）
            ├── security.py      风控拦截判定 + 跳转白名单
            ├── egress.py        出口分配与执行体身份串（单/并行/兜底三类角色的唯一口径）
            ├── client.py        易班客户端外观（凭据/会话缓存/代理/设备绑定）
            ├── adapters/statefiles/state_gc.py  按日状态文件的保留期策略与清理
            ├── logging_ext.py   日志落盘（跨进程互斥 + 按天滚动）
            ├── platform.py      易班平台事实（端点/请求头/版本）+ 登录与签到编排
            ├── adapters/protocol_yiban/ ★ 洁净室协议库（纯解析/构造；AGPL-3.0-only，见其 LICENSE/SPEC.md）
            ├── adapters/protocol_fake/  测试假上游（e2e 与演练用；生产链路不引）
            ├── adapters/notifier/       通知适配：`notify/`（推送）与 `mail/`（邮件）
            │                            ← 旧坐标 `yiban/notify/` 与 `yiban/mail/` 的转发壳已于 145-E 退役
            ├── geo.py           多边形内定位点采样（偶奇射线法 + 拒绝采样 + 显式兜底）
            ├── challenge.py     风控挑战检测（只检测不求解）
            ├── infra/           叶子工具：文件锁 / 私有 JSON 落盘 / 线材转储
            ├── adapters/config/ 配置名册加载 / .env 读写 / 凭据加密
            ├── adapters/persistence/ 表级数据访问（db.py 连接与迁移、claims 签到分工记录表）
            ├── adapters/statefiles/  状态文件与凭据熔断（state_io / cred_state / state_gc）
            ├── engine/          签到引擎（按"执行一轮"切分）：schedule 排期与容量 / attempts 单账号尝试
            │                     / executor_v3 调度 v3 执行体 / planner 每日计划 / token_bucket 出口令牌桶
            │                     / accounts 装载 / cli_support / config_check 等支撑件
            ├── workers/         多执行体监督进程与兜底常驻执行体
            └── attempt/jobs.py  在线校验异步任务（排队/看门狗/收口）

（目标分层目录 entry / app / core / ports / adapters / workers 已落位；
engine/ 仍是迁移期形态：成员按 `docs/dev/architecture-target.md` 第 3 节逐批搬入。）
```

> 依赖方向单向：`web` / `scripts` → `yiban`（`yiban` 不反向依赖调用方）。
> 新文件规模建议按类型自设目标（py 600 / js·css 800 / html·sh 400 行）——只是建议，无门禁；文件该不该拆以"拆了是否更好维护"判断，不逐行较劲。
> 命令行有两条等价通道：人类按本文的命令（`bash run.sh`、`.venv/bin/python3 scripts/signin.py ...`）照旧可用；统一入口与机器可读输出见 [docs/dev/cli.md](cli.md)。
> **`scripts/` 命名约定（2026-10-21 成文）**：`.sh` 一律连字符（CLI 入口：`check-*.sh`、`drill-*.sh`、`dev-verify.sh`）；
> `.py` 一律下划线（可被 import 的模块：`audit_verify.py`、`drill_multi_exec.py`）。
> 开发期构建与代码生成工具已移入 `tools/`（不进镜像）；`email_policy` 已移入 `yiban/core/`。
> **唯一例外 `deploy_gate.sh`**（连字符化要同步改 10 处读者与 deploy-gate 工作流，收益不足；此处显式登记）。
> 顶层形状由 `tests/test_repo_structure_gate.py` 的白名单钉住：新增顶层条目必须登记并写用途。

### 签到流程

```
登录易班 (OAuth + RSA 加密)
    ↓
获取签到任务范围 (nightAttendance/signPosition)
    ↓
解析签到多边形 Points
    ↓
在多边形内生成随机定位点（围栏内拒绝采样）
    ↓
提交签到 (nightAttendance/signIn)
```

### 定位生成算法

在签到围栏（`Points` 顶点环）内生成随机定位点，取点为**外接矩形内的拒绝采样**：
点在围栏内**精确均匀**，贴角与贴边区域与围栏中心一样可采到。（旧实现按三角形内缩
0.7，角与边的采样概率为零，真实围栏覆盖率只有 57%–66%；本批已换掉。）

1. 解析签到范围返回的多边形顶点 `Points`（先折掉相邻重复点与闭合环的重复首点）；
2. **退化判定**：顶点不足 3 个、外接矩形面积为零、面积为零、自交——命中即走第 5 步；
3. 在围栏外接矩形内均匀取点，用**偶奇射线法**判定是否在围栏内；在内部即采用；
4. 重复第 3 步，最多 `MAX_DRAWS`（10000）次仍未命中（极凹或极狭长围栏）即走第 5 步；
5. **兜底**：返回顶点质心附近的坐标并落一条 WARNING。兜底点**不保证**落在围栏内
   ——退化围栏没有可判定的内部，自交围栏的"内/外"也没有公认定义；仍返回坐标是因为
   调用方契约要求"非空围栏必返回一个点"（`yiban/client.py` 直接解包）。原因是
   **响亮**记录在日志里的，不静默。

每次签到的定位点都不同，非退化围栏的取点都落在有效范围内，避免被识别为异常定位。
算法语义与边界条件清单见 `yiban/geo.py` 的模块文档串。

### 重试机制

失败账号放回队尾分散重试，同账号两次尝试间隔不小于 60 秒；每次重试附加 0~30 秒随机延迟。尝试次数按失败类型分档：

| 失败类型 | 最多尝试 |
|---|---|
| 普通（网络/超时/临时错误） | 3 次（1 初始 + 2 重试） |
| 风控类（WAF 拦截） | 2 次（1 初始 + 1 重试） |
| 密码错误 / 易班账号锁定 | 1 次（不重试——确定性失败，重复尝试只会加速易班侧锁定账号） |
| 会话陈旧（登录状态被服务端作废） | 2 次（1 初始 + 1 重试，并清除缓存重新登录） |
| 易班未返回签到点位 | 1 次（不重试，属任务未配置/当日已关闭，与账号无关） |

### WAF 拦截检测

易班 WAF 风控会返回包含「风险访问服务禁用」等关键词的页面。检测方式：

- **长度检查**：响应超过 2000 字符直接判定为非拦截（正常页面通常较长）；
- **关键词匹配**：检测「风险访问」「风控」「访问服务禁用」「WAF」「拦截」；
- **Unicode 解码**：WAF 返回 JSON 时中文会被 `\uXXXX` 转义，先解码再匹配。

### 定时工作流自动续期

GitHub 官方政策：**仓库连续 60 天无活动，定时工作流会被自动禁用**。工作流里集成了 [`liskin/gh-workflow-keepalive@v1`](https://github.com/liskin/gh-workflow-keepalive)：每次定时触发签到时，keepalive job 通过 GitHub API 检查并重新启用被禁用的工作流，重置 60 天计时器——无需额外 commit，不污染提交历史。

</details>
