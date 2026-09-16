# API 契约：执行体与出口（`/api/executors`）

给前端做「执行体配置」页用。**后端已就绪，页面未实现**——本文档是唯一契约来源，
字段与语义变了要同步改这里（并与 `tests/test_egress_and_executors_api.py` 的断言对齐）。

## 权限与脱敏（先读这两条）

- **仅主管理员**（`.env` 里的内置管理员）可访问；其他身份一律 `401`/`403`。
- **出口串只以"描述"形式返回**：`scheme://host[:port]`，**不含 userinfo**。
  代理地址里可能带 `user:pass@`，任何页面都**不得**期望看到完整串；写接口接受完整串，
  读接口只回描述。示例：写入 `http://svc:secret@proxy.example:8080` → 读回 `http://proxy.example:8080`。

## `GET /api/executors`

```json
{
  "ok": true,
  "workers": {
    "configured": 3,
    "assignments": [
      {"index": 0, "egress": "http://proxy1.example:8080"},
      {"index": 1, "egress": "直连（本机出口）"},
      {"index": 2, "egress": "http://proxy3.example:3128"}
    ],
    "env_keys": {"list": "YIBAN_PROXY_LIST", "single": "YIBAN_PROXY"}
  },
  "fallback": {
    "egress": "直连（本机出口）",
    "interval_sec": 60,
    "env_key": "YIBAN_PROXY_FALLBACK"
  },
  "window": {
    "effective_sec": 4680,
    "start": "06:30",
    "end": "07:50"
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
| `workers.assignments[]` | list | 逐个执行体的出口描述，`index` 与 `YIBAN_PROXY_LIST` 的下标一致；**空位显示为「直连（本机出口）」** |
| `workers.env_keys` | object | 键名由后端给出，前端**不要硬编码字符串** |
| `fallback.egress` | string | 兜底常驻执行体的出口描述 |
| `fallback.interval_sec` | int | 兜底执行体的扫描间隔（秒） |
| `window.*` | object | 有效窗口（已扣掐头去尾）：`effective_sec` 就是容量换算用的分母 |
| `measured` | object \| **null** | 部署者实测值。**为 null 时页面必须显示"未实测"并隐藏建议**，不得编造数字 |
| `recommendation` | object \| **null** | `per_executor_accounts = 实测 × 2/3`（向下取整，至少 1）；`executors_needed = ⌈current_accounts / per_executor_accounts⌉` |
| `current_accounts` | int | 当前会计入容量的账号数（与设置页容量口径一致） |

**文案要求**：展示 `recommendation` 时必须带上"建议"字样（直接引用 `note` 即可），
不得让管理员理解成"超过就会出错"——不同部署者的机器与出口带宽差异很大，这个数字只作提醒。

## 写路径：`POST /api/settings`

新字段（与既有设置同接口、同字段级权限；**仅主管理员**可写）：

| 字段 | 类型 | 校验 | 落库键 |
|------|------|------|--------|
| `proxy_list` | string | 逗号分隔；每段为空或 `http(s)://host[:port]` | `YIBAN_PROXY_LIST` |
| `proxy_fallback` | string | 同上（单段） | `YIBAN_PROXY_FALLBACK` |
| `workers` | int | 1~64 | `YIBAN_WORKERS` |
| `capacity_measured` | int | 0~100000；0 表示清除 | `YIBAN_CAPACITY_MEASURED` |

返回与错误：成功 `{"ok": true}`；校验失败 `400` + `{"error": "..."}`；
权限不足 `403`。**写完整代理串**（含凭据）由前端输入、后端落 `.env`；
读回一律是描述串（见上文脱敏）。

## 尚未提供（前端仍缺的后端能力，按需再排）

| 需求 | 现状 |
|------|------|
| 触发一轮并行签到（按钮） | 未提供。当前由宿主 cron / 容器调度器拉起；若要做"立即执行"，应新增一个受主管理员保护、带防抖与并发闸的端点 |
| 实时查看各执行体进度（谁在签哪个账号） | 领取池已能回答（`sign_claims` 的 `state/owner`），但未暴露接口；做的话建议 `GET /api/executors/progress` 回聚合计数（避免逐账号列表） |
| 修改后自动重启执行体 | 未提供。改配置后由下一次定时/容器重启生效（与既有设置项一致的语义） |
