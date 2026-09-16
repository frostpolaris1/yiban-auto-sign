# API 契约：执行体与出口（`/api/scheduler/executors`）

给前端做「执行体配置」页用。**后端已就绪，页面未实现**——本文档是唯一契约来源，
字段与语义变了要同步改这里（并与 `tests/test_egress_and_executors_api.py` 的断言对齐）。

## 权限与脱敏（先读这两条）

- **仅主管理员**（`.env` 里的内置管理员）可访问；其他身份一律 `401`/`403`。
- **出口串只以"描述"形式返回**：`scheme://host[:port]`，**不含 userinfo**。
  代理地址里可能带 `user:pass@`，任何页面都**不得**期望看到完整串；写接口接受完整串，
  读接口只回描述。示例：写入 `http://svc:secret@proxy.example:8080` → 读回 `http://proxy.example:8080`。
- **执行体身份一律不回原串**。身份串（库里的 `sign_claims.owner`）形如
  `worker-{序号}@{主机名}` / `fallback@{主机名}` / `single@{主机名}`
  （2026-09-16 之前的旧记录形如 `{主机名}:workers:{进程号}:w{序号}` 或 `exec-…`，仍可解析）——
  主机名与进程号对攻击者就是**资产清单**（哪台机器、哪个 PID、跑了几个进程），
  而页面真正需要的信息只是"这是第几个执行体、它做了多少"。
  故 `activity.by_executor[].slot` 用 **1-based 槽位号**替代原串；
  需要追溯具体进程时在服务器上直接查库，不要指望接口给。
  测试对此有反查断言（拿写入的 owner 去响应 JSON 里搜，搜到即失败）。

## `GET /api/scheduler/executors`

```json
{
  "ok": true,
  "workers": {
    "configured": 3,
    "assignments": [
      {"index": 0, "egress": "http://proxy1.example:8080",
       "role": "worker", "label": "并行执行体 #1"},
      {"index": 1, "egress": "直连（本机出口）",
       "role": "worker", "label": "并行执行体 #2"},
      {"index": 2, "egress": "http://proxy3.example:3128",
       "role": "worker", "label": "并行执行体 #3"}
    ],
    "env_keys": {"list": "YIBAN_PROXY_LIST", "single": "YIBAN_PROXY"}
  },
  "fallback": {
    "egress": "直连（本机出口）",
    "interval_sec": 60,
    "env_key": "YIBAN_PROXY_FALLBACK",
    "role": "fallback",
    "label": "兜底常驻执行体",
    "alive": false,
    "enabled": false,
    "env_key_enable": "YIBAN_FALLBACK_ENABLE",
    "status": "off",
    "in_window": false
  },
  "window": {
    "effective_sec": 4680,
    "start": "06:30",
    "end": "07:50"
  },
  "activity": {
    "day": "2026-09-16",
    "in_window": true,
    "by_executor": [
      {"slot": 1, "role": "worker", "index": 0, "label": "并行执行体 #1",
       "done": 10, "failed": 0, "claimed": 0, "total": 10}
    ],
    "totals": {"done": 40, "failed": 0, "claimed": 0, "total": 40}
  },
  "measured": {
    "per_executor_capacity": 354,
    "source": "capacity_probe（部署者实测录入）",
    "env_key": "YIBAN_CAPACITY_MEASURED"
  },
  "recommendation": {
    "per_executor_accounts": 236,
    "executors_needed": 22,
    "note": "实测容量 × 2/3 的建议值；这是建议，不是程序上限"
  },
  "current_accounts": 5231
}
```

| 字段 | 类型 | 语义与页面用法 |
|------|------|----------------|
| `workers.configured` | int ≥ 1 | 当前配置的并行执行体数（对应 `--workers N` 的 N；未配置=1） |
| `workers.assignments[]` | list | 逐个执行体的出口描述 + `role`/`label`，`index` 与 `YIBAN_PROXY_LIST` 的下标一致；**空位显示为「直连（本机出口）」** |
| `workers.env_keys` | object | 键名由后端给出，前端**不要硬编码字符串** |
| `fallback.egress` | string | 兜底常驻执行体的出口描述 |
| `fallback.interval_sec` | int | 兜底执行体的扫描间隔（秒） |
| `fallback.role` / `label` | string | 固定 `fallback` / 「兜底常驻执行体」 |
| `fallback.alive` | bool | 兜底执行体**当前是否在跑**（按心跳新鲜度：超过 2 个扫描间隔即判"已停"，进程被强杀也能识别） |
| `fallback.enabled` | bool | `.env` 里**声明的**开关（`YIBAN_FALLBACK_ENABLE`，认 1/true/on/yes；未设=关）。"声明"与"在跑"是两件事，见 `status` |
| `fallback.env_key_enable` | string | 开关的键名（同 `env_keys` 的用意：前端不硬编码） |
| `fallback.status` | string | 后端算好的四态，见下表。**页面直接用它，不要自己用 enabled×alive 拼** |
| `fallback.in_window` | bool | 当前是否落在**有效**签到窗口内（已扣掐头去尾） |
| `window.*` | object | 有效窗口（已扣掐头去尾）：`effective_sec` 就是容量换算用的分母 |
| `activity.day` | string | 统计的业务日（北京时间） |
| `activity.in_window` | bool | 与 `fallback.in_window` 同值（放在这里便于前端一次取用） |
| `activity.by_executor[]` | list | 当日**按执行体归属**的计数；`slot` 是 1-based 槽位号（**替代 owner 原串**），`role`/`index`/`label` 来自 `yiban.egress.parse_owner` |
| `activity.totals` | object | 当日合计（各执行体之和）；库不存在/未初始化时 `by_executor: []`、`totals` 全 0 |
| `measured` | object \| **null** | 部署者实测值。**为 null 时页面必须显示"未实测"并隐藏建议**，不得编造数字 |
| `recommendation` | object \| **null** | `per_executor_accounts = 实测 × 2/3`（向下取整，至少 1）；`executors_needed = ⌈current_accounts / per_executor_accounts⌉` |
| `current_accounts` | int | 当前会计入容量的账号数（与设置页容量口径一致） |

### `fallback.status` 四态（页面按它决定提示文案）

| `enabled` | `alive` | `status` | 含义与页面该做什么 |
|-----------|---------|----------|--------------------|
| 0 | 0 | `off` | 没开兜底。**正常态**，不必提示（除非用户以为开了） |
| 1 | 1 | `running` | 开关开了、进程也真在跑。正常 |
| 1 | 0 | `declared_not_running` | **开了却没跑起来**：宿主那条 cron 多半漏加了（或刚改完还没到触发点）。仅当 `in_window=true` 时才值得报警——窗口外它本来就该退出 |
| 0 | 1 | `running_not_declared` | 没开开关却有进程在跑：多半是人工 `--fallback` 起的。提示"这条不是由配置拉起的"即可，不是故障 |

**报警纪律**：窗口外 `alive=false` 是**预期行为**（兜底进程只在窗口内运行），
页面只对 `in_window=true` 的 `declared_not_running` 报警，否则每天非签到时段都在误报。

### `activity` 的角色与"未标注"

`role` 取值 `worker` / `fallback` / `single` / `unknown`（口径唯一在 `yiban/egress.py`）：

- `worker` 带 `index`（0-based，与 `YIBAN_PROXY_LIST` 下标一致）与 `label`「并行执行体 #N」；
- **`unknown` 是存量数据的事实，不是错误**：2026-09-16 之前兜底执行体与单执行体同用
  `exec-` 前缀且带进程号，库里区分不出来。这批老记录照实回 `unknown` +「未标注（旧数据）」，
  **不要**在前端猜测归类。

**文案要求**：展示 `recommendation` 时必须带上"建议"字样（直接引用 `note` 即可），
不得让管理员理解成"超过就会出错"——不同部署者的机器与出口带宽差异很大，这个数字只作提醒。

## `PUT /api/scheduler/executors`

改执行体数量与出口（**仅主管理员**；字段可部分提交，未提交的字段保持原值）：

| 字段 | 类型 | 校验 | 落库键 |
|------|------|------|--------|
| `workers` | int | 1~64 | `YIBAN_WORKERS` |
| `proxy_list` | string | 逗号分隔；每段为空或 `http(s)://[user:pass@]host[:port]` | `YIBAN_PROXY_LIST` |
| `proxy_fallback` | string | 同上（单段，可空） | `YIBAN_PROXY_FALLBACK` |
| `capacity_measured` | int | 0~100000；`0` 表示清除实测值 | `YIBAN_CAPACITY_MEASURED` |
| `fallback_enable` | `0`/`1`/`true`/`false`（大小写不敏感，JSON 布尔也认） | 其它值一律 400；落盘统一归一成 `0`/`1` | `YIBAN_FALLBACK_ENABLE` |

响应：成功 `{"ok": true, "applied": ["..."], "note": "已写入配置；下一轮定时任务或容器重启后生效"}`；
校验失败 `400` + `{"error": "..."}`；权限不足 `403`。

**三条必须转达给用户的语义**：
1. **保存不会立即生效**（下一轮定时任务 / 容器重启后生效）——页面上要写明，不要让人以为点完就在跑；
2. **写完整代理串**（含凭据）由前端输入、后端落 `.env`；读回一律是描述串（见上文脱敏），
   所以"原样回显"是不可能的，编辑框应留空并提示"留空=不修改/直连"；
3. **`fallback_enable=1` 只落一个开关**，还必须在宿主加一条 cron 才会真有进程被拉起来
   （模板见 `scripts/yiban-fallback.sh` 头注释）。故**不能**把开关状态显示成"正在运行"——
   以 `GET` 回来的 `fallback.status` 为准。

## `PUT /api/scheduler/executors/workers/<index>` 与 `PUT /api/scheduler/executors/fallback`

**按序号只改一段出口**（仅主管理员）。存在的理由：上面那个整条 `proxy_list` 写入要求前端
提交**完整串**，而读接口只回**描述串**——前端拼不出别人的段，整条回写就会**把别人的代理凭据
清成空**（静默退回直连、不报错）。这两个端点只替换目标段，**其余段逐字保留**（不重新校验、
不规范化、不排序）。

请求体：

| 字段 | 含义 |
|------|------|
| `egress: "http://[user:pass@]host[:port]"` | 设置该段（`user:pass@` 可带，落 `.env`） |
| `egress: ""` 或 `null` | 该段**直连**（对 `fallback` 而言即"删掉这一键"；此时 `resolve` 的既有语义会退回 `YIBAN_PROXY`，若两者都空才是真直连） |
| 缺 `egress` 键 | `400`（不给"什么都不改"的歧义） |

校验与响应：
- `index`：`0..63` 且**必须小于当前 `YIBAN_WORKERS`**，否则 `400`（该槽位未被使用）；
  段数不足时按位补齐到 `index`；负数下标走不到路由（Werkzeug 的 int 转换器只吃非负）；
- 代理串校验复用整条写入的同一函数；**任何换行一律 400**（防 `.env` 注入）；
- 成功：`{"ok": true, "index": <int 或 "fallback">, "egress": "<脱敏描述串>"}`；
- 审计只落**目标键名**（如 `YIBAN_PROXY_LIST[2]`），**绝不记凭据**；
- 与整条写入同一把锁、同一写入函数（读-改-写原子）；**只写 `.env`，不重启**。

> 前端验收（对方明确会真测这条）：三个段都配好凭据时，改第 2 段后**第 1、3 段必须逐字未变**，
> 且响应与随后 `GET` 都只含描述串、不含 `userinfo`。

## 尚未提供（前端仍缺的后端能力，按需再排）

| 需求 | 现状 |
|------|------|
| 触发一轮并行签到（按钮） | 未提供。当前由宿主 cron / 容器调度器拉起；若要做"立即执行"，应新增一个受主管理员保护、带防抖与并发闸的端点 |
| 修改后自动重启执行体 | 未提供。改配置后由下一次定时/容器重启生效（与既有设置项一致的语义） |
| 逐账号的执行体分工明细 | **部分已定**：账号列表将加"**上一个业务日**是谁签的"一列（用户 2026-09-16 定口径）；"把账号转移到指定执行体"用户裁决**暂缓**。全量分工明细仍不提供（避免接口变成"整表导出"） |
| 每个并行执行体的在线状态 | **用户已定要做**（现在只有兜底有 `alive`）；实现为四态以区分"正常未运行"与"疑似被强杀" |
| 现场实测单账号耗时（按钮） | **用户已定要做**，但**必须限频 + 仅主管理员**，且它真的会用真实账号访问易班一次；窗口内拒绝执行 |
| 容器形态下起兜底常驻执行体 | 未做。本批只覆盖**宿主** cron（`scripts/yiban-fallback.sh`）；容器调度器未改动，容器里怎么起兜底留作独立事项 |
