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
  （2026-09-16 之前的旧记录形如 `{主机名}:workers:{进程号}:w{序号}` 或 `exec-…`，仍可解析；
  单执行体形态现由 `yiban.egress.runtime_owner` 写成 `single@{主机名}:{进程号}:{代次}`，
  即同一稳定名的两个进程是两个 owner——这是"同名进程不得互相重入"的前提）——
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
       "role": "worker", "label": "并行执行体 #1",
       "state": "finished", "last_seen_at": "2026-09-16 06:42:11"},
      {"index": 1, "egress": "直连（本机出口）",
       "role": "worker", "label": "并行执行体 #2",
       "state": "running", "last_seen_at": "2026-09-16 06:41:58"},
      {"index": 2, "egress": "http://proxy3.example:3128",
       "role": "worker", "label": "并行执行体 #3",
       "state": "idle", "last_seen_at": null}
    ],
    "env_keys": {"list": "YIBAN_PROXY_LIST", "single": "YIBAN_PROXY",
                 "manifest": "YIBAN_EXECUTORS"}
  },
  "executors": [
    {"slot": 0, "type": "worker", "egress": "http://proxy1.example:8080",
     "label": "并行执行体 #1", "state": "finished", "last_seen_at": "2026-09-16 06:42:11"},
    {"slot": 1, "type": "worker", "egress": "直连（本机出口）",
     "label": "并行执行体 #2", "state": "idle", "last_seen_at": null},
    {"slot": 2, "type": "worker", "egress": "http://proxy3.example:3128",
     "label": "并行执行体 #3", "state": "idle", "last_seen_at": null},
    {"slot": 3, "type": "fallback", "egress": "直连（本机出口）",
     "label": "故障转移", "name": null, "state": null, "last_seen_at": null},
    {"slot": 4, "type": "disabled", "egress": "http://proxy5.example:3129",
     "label": "已停用", "state": null, "last_seen_at": null}
  ],
  "fallback": {
    "egress": "直连（本机出口）",
    "interval_sec": 60,
    "env_key": "YIBAN_PROXY_FALLBACK",
    "role": "fallback",
    "label": "故障转移",
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
| `workers.configured` | int ≥ 1 | 当前配置的并行执行体数：**清单里 `type=worker` 的行数**（`disabled`/`fallback` 不计）；清单缺失时回退旧口径（`YIBAN_WORKERS`，未配置=1） |
| `workers.assignments[]` | list | 逐个执行体的出口描述 + `role`/`label`，`index` 是**清单槽位号**（清单缺失时即 `YIBAN_PROXY_LIST` 的下标）；**空位/空串显示为「直连（本机出口）」**。只列 `worker` 行——停用行只在 `executors[]` 里看得到 |
| `workers.assignments[].state` | string | 该执行体的存活四态，见下表。**页面直接用，不要自己拿文件/时间去拼** |
| `workers.assignments[].last_seen_at` | string \| **null** | 最后一次见到它活着的时刻（`YYYY-MM-DD HH:MM:SS`）；本业务日无记录时为 `null`。**不含 pid/主机名** |
| `workers.env_keys` | object | 键名由后端给出，前端**不要硬编码字符串**。`manifest` = 执行体清单键名（`YIBAN_EXECUTORS`） |
| `executors[]` | list | **执行体清单逐行**（`YIBAN_EXECUTORS` 的界面形态）：`{slot, type, egress, label[, state, last_seen_at]}`。见下节 |
| `fallback.egress` | string | 兜底常驻执行体的出口描述 |
| `fallback.interval_sec` | int | 兜底执行体的扫描间隔（秒） |
| `fallback.role` / `label` | string | 固定 `fallback` / 「**故障转移**」（2026-09-17 定：与 `executors[]` 里兜底行的标签、账号页「上次实领」的角色列**同一处实现**，改一处三处同时变） |
| `fallback.alive` | bool | 兜底执行体**当前是否在跑**（按心跳新鲜度：超过 2 个扫描间隔即判"已停"，进程被强杀也能识别） |
| `fallback.enabled` | bool | `.env` 里**声明的**开关（`YIBAN_FALLBACK_ENABLE`，认 1/true/on/yes；未设=关）。"声明"与"在跑"是两件事，见 `status` |
| `fallback.env_key_enable` | string | 开关的键名（同 `env_keys` 的用意：前端不硬编码） |
| `fallback.status` | string | 后端算好的四态，见下表。**页面直接用它，不要自己用 enabled×alive 拼** |
| `fallback.in_window` | bool | 当前是否落在**本应运行**的时段内 ＝ 有效窗口内（已扣掐头去尾）**且** 今天没被门挡下（周末签到未开 / 一键暂停）。见下方「`in_window` 的两段口径」 |
| `window.*` | object | 有效窗口（已扣掐头去尾）：`effective_sec` 就是容量换算用的分母 |
| `activity.day` | string | 统计的业务日（北京时间） |
| `activity.in_window` | bool | 与 `fallback.in_window` 同值（放在这里便于前端一次取用） |
| `activity.by_executor[]` | list | 当日**按执行体归属**的计数；`slot` 是 1-based 槽位号（**替代 owner 原串**），`role`/`index`/`label` 来自 `yiban.egress.parse_owner` |
| `activity.totals` | object | 当日合计（各执行体之和）；库不存在/未初始化时 `by_executor: []`、`totals` 全 0 |
| `measured` | object \| **null** | 部署者实测值。**为 null 时页面必须显示"未实测"并隐藏建议**，不得编造数字 |
| `recommendation` | object \| **null** | `per_executor_accounts = 实测 × 2/3`（向下取整，至少 1）；`executors_needed = ⌈current_accounts / per_executor_accounts⌉` |
| `current_accounts` | int | 当前会计入容量的账号数（与设置页容量口径一致） |

### `executors[]`：执行体清单（2026-09-17 新增）

清单是**单键 JSON 数组** `YIBAN_EXECUTORS`，每个执行体一行：

```json
[{"slot": 0, "type": "worker", "proxy": "http://u:p@h:1", "name": "机房A"},
 {"slot": 1, "type": "disabled", "proxy": ""},
 {"slot": 2, "type": "fallback", "proxy": "http://fb:8080"}]
```

| 概念 | 口径（页面按这个理解） |
|------|------------------------|
| `slot` | **稳定槽位号**（0~63）。**只增不复用**：新增行 = 当前最大 + 1；删中间行不重排其余槽位。删掉**当前最大**那一行后，若该号在**领取历史**（保留期内）里出现过，下一次追加会**跳过去**——否则重建的执行体会被显示成前任的归属；从没用过的号照旧复用。想长期占住位置就把行改成 `disabled` **而不是删除** |
| `type` | `worker`（并行执行体，可有 N 行）/ `fallback`（兜底，**最多 1 行**）/ `disabled`（停用） |
| `proxy` | 该行自己的出口；空串 = 直连。**写接口收完整串，读接口只回描述串**（脱敏硬要求不变） |
| 停用语义 | **保留出口**、不参与分配、不拉起、不计入建议值；接口回 `disabled`、**不报存活**；仍占 `slot`（不被复用）。重新启用（改回 `worker`）后出口照旧 |
| 建议值分母 | `workers.configured` 只数 `worker` 行（`disabled` 与 `fallback` 都不计） |
| `name` | 行的自定义名（**可选**）：设了就优先显示它，没设显示 `label`。空串 = 清除。**改它不触发口令门**（只改名不改行为） |
| 旧键 | 一个版本周期内保留：清单缺失时按旧三键读取（`resolve` 回退口径逐字不变）；**清单与旧键并存时以清单为准** |

`executors[]` 每行字段：

| 字段 | 类型 | 语义 |
|------|------|------|
| `slot` | int | 槽位号（同时就是并行执行体的下标，与 `workers.assignments[].index`、`activity.by_executor[].index` 同号） |
| `type` | string | `worker` / `fallback` / `disabled` |
| `egress` | string | 描述串（不含 userinfo） |
| `label` | string | **后端口径**的中文标签：`并行执行体 #N` / `故障转移` / `已停用`。行的自定义名在 `name` 里，两者刻意分开 |
| `state` | string \| null | **只有 `worker` 行有值**：该行的存活四态（口径同 `workers.assignments[].state`）；`fallback` 与 `disabled` 行**为 `null`** |
| `last_seen_at` | string \| null | 同上：只有 `worker` 行有值，`fallback` / `disabled` 行为 `null` |
| `name` | string \| null | **行的自定义名**（用户起的，2026-09-17 新增）。未设 = **`null`**（页面显示 `label`）；最长 32 字符、不许含换行。写接口收 `name`，空串/null = 清掉 |

**`fallback` 行与 `disabled` 行的 `state` / `last_seen_at` 是 `null`（字段照给、值为空）**：
兜底的存活在 `fallback.*` 里（心跳文件与判据不同，套 worker 四态会永远显示 `idle`），
停用行按要求不报存活。页面据此区分"停用"（`type == "disabled"`）。

**迁移**：首次读到 `.env` 里存在旧三键、而清单键缺失时，后端按旧口径（顺序、空位=直连、
兜底位置、worker 数量）**一次性生成清单并写回**，旧三键**不删**。所以页面第一次打开时
`executors[]` 的行数会比"配置的并行执行体数 + 1（兜底）"——这是迁移的正常结果。

### `fallback.status` 四态（页面按它决定提示文案）

| `enabled` | `alive` | `status` | 含义与页面该做什么 |
|-----------|---------|----------|--------------------|
| 0 | 0 | `off` | 没开兜底。**正常态**，不必提示（除非用户以为开了） |
| 1 | 1 | `running` | 开关开了、进程也真在跑。正常 |
| 1 | 0 | `declared_not_running` | **开了却没跑起来**：宿主形态多半是那条 cron 漏加了（或刚改完还没到触发点）；容器形态由调度器在窗口内自动拉起，拉起失败会在 sched 日志里留痕。仅当 `in_window=true` 时才值得报警——`in_window=false` 时它本来就该退出 |
| 0 | 1 | `running_not_declared` | 没开开关却有进程在跑：多半是人工 `--fallback` 起的。提示"这条不是由配置拉起的"即可，不是故障 |

### `in_window` 的两段口径（2026-09-17 定，前端**不需要**改判断）

`in_window` 从"落在有效窗口内"改成了"**本应运行**的时段内"，即在钟点口径上**再加一层
今天是否被挡下的判断**（周末签到未开 / 管理员一键暂停）。改动原因：兜底常驻现在会在这两
种日子直接退出（此前它会绕过这两道门照签，属缺陷），只按钟点算，页面会在每个周六周日、
以及每次一键暂停期间报"兜底开了却没跑起来"。

因此前端规矩不变：**只对 `in_window=true` 的 `declared_not_running` 报警**。
`in_window=false` 有两种情形（窗口外 / 今天被挡下），都不该报警——页面若要区分，看
`fallback.status` 即可，不必再要新字段。

> 另一个 **`in_window` 用法不同**的地方：`POST /api/scheduler/executors/measure` 的
> 409 拦截用的是**纯钟点口径**（"窗口内不做实测，避免与签到抢资源"）。所以周末在窗口
> 钟点内仍可能被 409 拒——这是刻意的：那道闸门只关心"会不会和本轮签到撞上"。

**报警纪律**：窗口外 `alive=false` 是**预期行为**（兜底进程只在窗口内运行），
页面只对 `in_window=true` 的 `declared_not_running` 报警，否则每天非签到时段都在误报。

**宿主形态与容器形态同契约**：两者的拉起方式不同（宿主是 `scripts/yiban-fallback.sh`
的 cron 条目；容器是 `docker/scheduler.py` 在窗口内自动拉起、窗口结束由进程自行退出），
但都用同一个开关 `YIBAN_FALLBACK_ENABLE`、同一份心跳文件与同一套存活判据，故
`fallback.*`（含 `status` / `alive` / `in_window`）在两种形态下语义相同、字段相同。

### `activity` 的角色与"未标注"

`role` 取值 `worker` / `fallback` / `single` / `unknown`（口径唯一在 `yiban/egress.py`）：

- `worker` 带 `index`（0-based，清单槽位号；清单缺失时与 `YIBAN_PROXY_LIST` 下标一致）与 `label`「并行执行体 #N」；
- **`unknown` 是存量数据的事实，不是错误**：2026-09-16 之前兜底执行体与单执行体同用
  `exec-` 前缀且带进程号，库里区分不出来。这批老记录照实回 `unknown` +「未标注（旧数据）」，
  **不要**在前端猜测归类。

**文案要求**：展示 `recommendation` 时必须带上"建议"字样（直接引用 `note` 即可），
不得让管理员理解成"超过就会出错"——不同部署者的机器与出口带宽差异很大，这个数字只作提醒。

## 行接口：`/api/scheduler/executors/rows…`（2026-09-17 新增）

**仅主管理员**；CSRF 与同族端点一致（全局 `before_request` 校验）。三个接口都只动清单键：
**只写 `.env`，不重启也不拉起进程**——下一轮定时任务或容器重启后生效。
审计只落 `YIBAN_EXECUTORS[<slot>]`（**绝不记凭据、也不记自定义名**）；响应只回描述串。

**口令门（2026-09-17 加）**：三个写接口都要求 `confirm_password`（**追加与删行一律要求**，
改行只在 `type`/`proxy` 真的会变时要求）；缺/错 → **`403` `{"error": "口令校验未通过，设置未生效"}`**，
此时**配置与清单都不动**，并写一条 `executors_pw_fail` 审计（只落动作与槽位）。
与 `POST /api/settings` 的系统开关**同一实现、同一文案**：只比对不计数（不写与登录共用的
失败计数——持 Cookie 者不得借门禁把管理员锁出登录）。原因：持被窃的主管理员会话此前
可以无口令改出口/增删执行体/关兜底，而这类配置错了会**静默漏签**，比"站内暂停"更难发现。

### `POST /api/scheduler/executors/rows` —— 追加一行

| body | 含义 |
|------|------|
| `type` | 可选，`worker`（默认）/ `fallback` / `disabled` |
| `proxy` | 可选，完整代理串；省略 / `null` / 空串 = 直连 |
| `name` | 可选，行自定义名；省略 / `null` / 空串 = 不设名（页面显示 `label`） |
| `confirm_password` | **必填**（追加一定会改配置）：当前管理员口令；缺/错 → `403` |

槽位 = **现有最大 + 1**，并**跳过保留期内真用过的号**（上限 63，满了 `400`）。
`fallback` 最多 1 行，已有则 `400`。

> 为什么要跳过"用过的号"：删掉当前最大那一行后，纯按"最大值 + 1"会把刚空出来的号再发一次，
> 而那个号在领取池（`sign_claims.owner`）里已有历史，新执行体会被显示成前任的归属。
> 领取历史读不到（库未初始化/抖动）时退回"最大值 + 1"，**追加本身永不因此失败**。

成功 `200`：`{"ok": true, "slot": <int>, "type": "...", "egress": "<描述串>", "note": "..."}`。

### `PUT /api/scheduler/executors/rows/<slot>` —— 改一行

| body | 含义 |
|------|------|
| `type` | 可选；**缺席 = 不改类型**。改成 `disabled` 即"停用" |
| `proxy` | 可选；**缺席 = 不改出口**，`null` / 空串 = 直连 |
| `name` | 可选；**缺席 = 不改名**，空串 = 清掉自定义名 |
| `confirm_password` | **`type` 或 `proxy` 真的会变时必填**（只改名或提交同值不要求） |

`type` / `proxy` / `name` 都不给 → `400`（不给"什么都不改"的歧义）；槽位不在清单里 → `400`；
改成 `fallback` 时若已有别的兜底行 → `400`。**其余行逐字保留**（与单段出口写接口同一纪律）。

成功 `200`：`{"ok": true, "slot": <int>, "type": "...", "egress": "<描述串>", "note": "..."}`。

### `DELETE /api/scheduler/executors/rows/<slot>` —— 删一行

`confirm_password` **必填**（删行一定会改配置）。body 里带 `{"confirm_password": "..."}`。
删行**不重排**其余槽位（删中间行后新建的行拿 `现有最大 + 1`；删掉当前最大那行后，
若该号在领取历史里出现过，新建的行会跳过它——见上面 POST 的说明）；槽位不存在 → `400`。
"以后可能还要用"请改成 `disabled`（保留出口、占住槽位），不要删。

成功 `200`：`{"ok": true, "slot": <int>, "type": "<被删行的类型>", "deleted": true, "note": "..."}`。

### 与旧写接口的关系（迁移期）

清单存在时，**旧写接口也会维护清单**（否则旧键写完被"以清单为准"的读接口盖过，
表现为"保存点了没生效"），页面可以放心继续调旧接口：

| 旧接口 | 清单存在时的行为 |
|--------|------------------|
| `PUT /api/scheduler/executors`（整条 `workers` / `proxy_list` / `proxy_fallback`） | 按旧口径同步清单：**保留已存在的 worker 槽位**（顺序吃新出口），执行体数变多用"最大槽位 + 1"追加、变少删除多余的 worker 行；兜底行更新出口；`disabled` 行不动 |
| `PUT …/executors/workers/<index>` | 只改清单里该槽位那一行的出口（槽位不在清单里 → `400`） |
| `PUT …/executors/fallback` | 改清单里兜底行的出口；清单里没有兜底行则**追加一行** |

**注意**：旧整条写入只能表达"数量 + 连续出口表"，故它会**按槽位升序重新对应**执行体，
并删除多出来的 worker 行——**停用行不会被它改动**，但槽位布局以它的口径重排。
要精细控制槽位/停用，请用上面的行接口。

## `PUT /api/scheduler/executors`

**口令门**：请求里带来的键**逐个与 `.env` 现值比对**，只要有**任何一个键的值真的会变**
（含 `workers` / `proxy_list` / `proxy_fallback` / `fallback_enable` / `capacity_measured`
以及顺带维护的清单键），就要求 `confirm_password`；提交与现值完全相同（或只带没变的键）
→ 不要求。缺/错 → `403`「口令校验未通过，设置未生效」且不落盘 + `executors_pw_fail` 审计。

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
3. **`fallback_enable=1` 只落一个开关**，进程由部署形态各自拉起：宿主形态要在宿主机加
   一条 cron（模板见 `scripts/yiban-fallback.sh` 头注释），容器形态由容器调度器在**签到
   窗口内**自动拉起（窗口结束由进程自行退出）。故**不能**把开关状态显示成"正在运行"——
   以 `GET` 回来的 `fallback.status` 为准。

## `PUT /api/scheduler/executors/workers/<index>` 与 `PUT /api/scheduler/executors/fallback`

**口令门**：提交的 `egress` 与**该槽位此刻生效的值**相同 → 不要求；不同 → 要求
`confirm_password`（缺/错 `403`，不落盘 + 审计）。

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

### `workers.assignments[].state` 四态（并行执行体）

并行执行体是**一轮就退出的短命进程**，不是常驻服务——所以"没在跑"在多数时间是
**正常**的（定时任务还没到 / 本轮已跑完）。接口因此回四态而不是 `alive` 布尔，
判定全在后端（心跳周期是部署细节，前端不必知道）：

| `state` | 判据（后端） | 含义与页面该做什么 |
|---------|--------------|--------------------|
| `running` | 有本轮心跳且新鲜（未超过 2 × 心跳周期） | 正在跑本轮。正常 |
| `finished` | 有本轮**收尾标记**（正常退出） | 已跑完。正常，灰显即可 |
| `idle` | 本业务日**没有**该槽位的记录 | 今天还没跑。正常，灰显即可 |
| `stale` | 有开始记录、**无收尾**，且心跳已过期 | **要提示的异常**：可能被强杀/超时杀掉（收尾没来得及写） |

**报警纪律**：只有 `stale` 值得提示（"这个执行体可能被中断了"）。把 `idle`/`finished`
报成异常会让页面在每天的绝大部分时间都在误报。跨业务日的旧记录按 `idle` 处理
（"昨天跑完了"不等于今天已跑）。

### `GET /api/accounts` 的 `last_executor`（"昨天是谁签的"）

账号列表每个账号多一个字段，口径是**上一个业务日**（不是当日，也不是"最近一次"）：

```json
"last_executor": {"role": "worker", "index": 0, "label": "并行执行体 #1"}
```

| 取值 | 语义与页面用法 |
|------|----------------|
| 对象 | 上一个业务日的归属：`role`/`index`/`label` 同 `activity.by_executor[]`（口径同样来自 `yiban.egress.parse_owner`） |
| **`null`** | 上一个业务日没有该账号的记录（含"库不存在/未初始化"与新账号）。**页面必须显示「—」**，不要当成 `unknown` |
| `role: "unknown"` + 「未标注（旧数据）」 | 老记录判不出角色的事实，照实回，不要在前端猜 |

**只回角色/槽位/标签**：`sign_claims.owner` 原串含主机名（旧格式还含进程号），属部署
信息，任何情况下都不进响应。列表接口的手机号本就打码，与该字段无关。

## `POST /api/scheduler/executors/measure`

**口令门：不需要**（2026-09-17 定）。它**不改配置**，只是"真访问易班一次并占全局冷却"——
与手动签到（`POST /api/signin`，同样会真实登录）同一口径：都只判主管理员 + 各自的防抖。
若页面认为实测也该算高危，请提出来改（后端改一行，前端加一个口令框）。

现场实测单账号耗时（**仅主管理员**，其他身份 `403`；CSRF 由全局 `before_request` 校验）。

> ⚠ **它真的会访问易班一次**：拿一个真实账号走**只读**路径（登录 + 拉取签到任务，
> 与探针同一条路，**不提交签到**）。它**不写**当日签到状态、**不动**领取池、**不写**
> 签到态事件。页面文案必须如实这么写，不要写成"本地测算"。

请求体：

| body | 含义 |
|------|------|
| `{}` | 服务端自动挑一个可签账号（不删、审核通过、未被用户暂停） |
| `{"phone": "<完整手机号>"}` | 指定账号（该号码不可用/不存在 → 404）。**响应里只回打码号码** |

成功 `200`：

```json
{"ok": true, "seconds": 1.83, "sample": "138****8000",
 "per_executor_capacity": 2395, "recommended_per_executor": 1596,
 "cooldown_sec": 600, "next_allowed_in": 0,
 "note": "实测单账号耗时 × 有效窗口的容量估算；建议值含余量（×2/3）"}
```

| 错误码 | body | 语义 |
|--------|------|------|
| `403` | `{"error": "仅主管理员可做耗时实测"}` | 非主管理员（含普通管理员）。**不消耗冷却、不联网** |
| `409` | `{"error": "签到窗口内不做实测（避免抢占本轮资源）"}` | 落在**有效**签到窗口内。**不消耗冷却、不联网**（窗口内抢当前轮次的资源与风控面） |
| `429` | `{"error": "实测冷却中", "next_allowed_in": 137}` | 冷却未到，`next_allowed_in` = 还需等几秒（整秒，向上取整） |
| `404` | `{"error": "没有可用于实测的账号（不存在、未过审、已删除或已被暂停）"}` | 没有可实测的账号。**不消耗冷却、不联网** |
| `502` | `{"error": "实测失败：<已脱敏原因>"}` | 登录/拉任务失败。**冷却已消耗**（真实登录已经发生） |
| `400` | `{"error": "..."}` | 请求体不是合法 JSON 对象 |

字段与三条硬约束：

| 字段 | 类型 | 语义 |
|------|------|------|
| `seconds` | float | 本次实测的单账号耗时（秒，两位小数） |
| `sample` | string | 被实测账号的**打码**手机号（完整号绝不出现在响应与审计里） |
| `per_executor_capacity` | int | 按 `seconds` 与**有效窗口**算出的单执行体可容纳账号数（与设置页容量口径同源） |
| `recommended_per_executor` | int | 建议值 = `per_executor_capacity × 2/3`（向下取整，至少 1）。**是建议不是上限**：换机器/换网络都要重新量 |
| `cooldown_sec` | int | 当前生效的冷却秒数（`YIBAN_MEASURE_COOLDOWN`，默认 600） |
| `next_allowed_in` | int | 下次可实测的剩余秒数；成功路径必然是 0 |
| `note` | string | 直接引用即可（文案须带"建议"与"余量"字样） |

1. **全局冷却**（跨进程有效，落状态目录的单条文件）：不按会话/IP 计——多管理员叠加
   点击就绕开了。冷却**先占位再联网**，故连点只会产生一次真实登录；失败也消耗冷却
   （真实登录已经发生，风控暴露已经产生）；
2. **窗口内拒绝**（409）：避免抢当前轮次的资源与风控面；
3. **不自动保存**：实测结果**不落 `.env`**。前端只拿数字填输入框，用户确认后再走
   `PUT /api/scheduler/executors` 的 `capacity_measured` 提交。审计留一条记录
   （动作名 + 打码账号 + 实测秒数），**不记完整手机号**。

### `note` 字段承载这两条说明（**本轮刻意不加机器可判字段**）

响应里的 `note` 会带上"只覆盖窗口外最小链路、数值偏乐观、正式定档请用测试机基准"这句话，
页面**直接显示 `note` 即可**。按前端 2026-09-17 的答复：**暂不加** `scope` 之类的枚举字段——
目前只有"窗口外粗量"这一种口径，加字段要动契约与测试、收益不足；
**将来出现第二种口径不同的实测**（例如容器形态、带尾延迟的档位）时再加，届时前端会主动来要。

### 实测端点的两条风险（**页面文案必须如实写**）

1. **默认拿的是列表里第一个可签账号**（`{}` 时）：通常是**某个真实用户**的账号（很可能不是
   管理员自己的），且账号列表顺序稳定 ⇒ **每次默认都是同一个账号**。等于"拿一位用户的账号
   替全站做一次真实登录"，那位用户并不知情。要避开就显式传 `phone`（例如用自己的账号）。
2. **窗口内外的链路不同构 ⇒ 数值偏乐观**：本端点走"登录 + 拉任务"（5 次请求），
   真实签到还要往下走**定位计算 + 提交签到**（6 次请求 + CPU 计算）；又因**窗口内被拒（409）**，
   它**永远只测得到窗口外的最小链路**（窗口外服务端本就没有可提交的任务）。
   故 `seconds` 天然偏小、`per_executor_capacity` / `recommended_per_executor` 偏大。
   **它与测试机基准（`scripts/loadtest/capacity_probe.py`，假易班跑完整链路、延迟可控）
   不可混用、不可比**：现场量的是"窗口外粗值"，正式容量定档请以测试机基准为准，
   别把两种数当同一回事填进 `YIBAN_CAPACITY_MEASURED`。

> 另：成功判据是"登录成功 + `signPosition` 返回 `code == 0`"。窗口外若服务端对某账号
> 返回非 0（未开启签到/无任务），实测会**直接失败**而不是给出偏小的数——两种情形
> （成功但偏乐观 / 直接报错）目前对使用者是同一个提示面。

## 尚未提供（前端仍缺的后端能力，按需再排）

| 需求 | 现状 |
|------|------|
| 触发一轮并行签到（按钮） | **不提供该端点**（用户 2026-09-17 裁决：与账号列表的多选批量手动签到重复，需求撤回）。需要立刻补签时用账号列表的**多选批量手动签到**（`POST /api/signin/batch`）；定时轮仍由宿主 cron / 容器调度器拉起 |
| 修改后自动重启执行体 | 未提供。改配置后由下一次定时/容器重启生效（与既有设置项一致的语义） |
| 逐账号的执行体分工明细 | **部分已定**：账号列表已加"**上一个业务日**是谁签的"一列（`GET /api/accounts` 的 `last_executor`）；"把账号转移到指定执行体"用户裁决**暂缓**。全量分工明细仍不提供（避免接口变成"整表导出"） |
| 每个并行执行体的在线状态 | 已提供：`workers.assignments[].state` 四态（`running`/`finished`/`idle`/`stale`），判定在后端 |
| 现场实测单账号耗时（按钮） | 已提供：`POST /api/scheduler/executors/measure`，仅主管理员 + 全局冷却 + 窗口内拒绝；**它真的会用真实账号访问易班一次** |
| 容器形态下起兜底常驻执行体 | 已提供：`docker/scheduler.py` 每 60 秒检查一次，开关开着且**在有效签到窗口内、今天没被周末门/暂停门挡下**时拉起 `sign --fallback`；窗口结束由进程自行退出。与宿主形态同一个开关、同一把独立锁（`signin-run.lock.fallback`）、同一份心跳，故 `fallback.*` 在容器下语义不变 |
