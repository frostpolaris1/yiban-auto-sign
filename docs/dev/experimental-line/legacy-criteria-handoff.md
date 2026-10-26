# 旧系统判据交接（New Stack Handoff）

> 读者：实验线（`server/` ＋ `ui/`）的实现与审查者。
> 来源：旧系统（`yiban/` `web/` `frontend/`）在 2026-10-25 之前沉淀的判据与协议资产。
> 用户裁决（2026-10-25）：旧系统把工单做完；**新功能在实验线先做**；两线判据必须对账。
> 本文件只写**可核对的事实与约定**。凡标「待裁决」的，需要一次明确决定，不要默认。

---

## 1. 六请求链与 Provider 映射

### 1.1 端点顺序

网页 OAuth ＋ nightAttendance 链共六步。顺序有依赖，不可重排：

| # | 方法与端点 | 作用 | 产物 |
|---|---|---|---|
| 1 | `GET https://c.uyiban.com/code/html` | 授权页 | `page_use`（一次性令牌）、`public_key_pem`（RSA-1024） |
| 2 | `POST https://c.uyiban.com/code/usersure` | 提交账号口令 | 登录回执；`s200`＝成功 |
| 3 | `GET https://api.uyiban.com/base/c/auth/yiban` | 身份 | 姓名/学号/班级/学院（`data`） |
| 4 | `GET https://api.uyiban.com/nightAttendance/student/index/signPosition` | 签到点与窗口 | `Position[]`、`Range`、`State` |
| 5 | `GET https://api.uyiban.com/device/student/index/getState` | 设备登记状态 | 是否需登记/是否已登记 |
| 6 | `POST https://api.uyiban.com/nightAttendance/student/index/signIn` | 签到 | `data: true` 或失败信封 |

### 1.2 映射到 `signing/contract` 的四个方法

| Provider 方法 | 承担链步 | 必须产出 |
|---|---|---|
| `DiscoverWindow` | 第 4 步的 `Range` | `Window{Start,End,Source,Raw}`；`Source` 取 `platform`／`cache`／`fallback` |
| `DiscoverTargets` | 第 4 步的 `Position[]` | `[]Target`（**不得只取 `[0]`**，实拍常见 2 个） |
| `Verify` | 第 1–3 步（登录＋身份） | `Profile`；失败必须给出 `OutcomeCredentialBad`／`OutcomeChallenge`／`OutcomeDeviceUnbound` |
| `Sign` | 第 5–6 步（设备＋提交） | `Result{Outcome, Evidence}` |

**第 5 步的归属要一次决定（待裁决）**：设备登记属于「签到前的前置检查」，可放
`Sign` 内部，也可作为独立能力声明。若放 `Sign` 内部，`Sign` 就成为两请求链，幂等边界随之变化。

### 1.3 协议容错规则（七条，全部来自实拍差异）

1. **数值字段双态**：`Range/MapType/State/CreateTime` 可能是数字也可能是字符串
   （`"Range": 70` 与 `"Range": "110"` 同库并存）。收敛：数字直取；字符串去空白后转换；
   失败或缺失 → 空值（必填除外）。
2. **字符串 `"None"`**：`BuildingId` 实拍出现字面量 `"None"`。空串／`"None"` → 空值。
3. **JSON 藏在 HTML 头下**：信封解析**不看 `Content-Type`**。
4. **query 末位令牌**：`verify_request` 后没有 `&`；正则不得要求后随分隔符。
5. **PEM 藏在属性里**：`value="-----BEGIN…\n…"` 属性值含真实换行，提取需 DOTALL。
6. **多候选签到点**：`Position` 是数组，不得只取第一个。
7. **顶层 `Range` 是对象不是数值**：`{StartTime,EndTime,SignDay,RelatType,RelatTimeType}`，
   epoch 秒（实拍为当日 06:30–07:50+08）。数值收敛规则**不适用于它**。

### 1.4 错误分类法

- `ParseError`：输入不符合已知形态（结构性失败），带字段名与 ≤120 字符片段摘要。
- `SessionExpired`：信封 `code == 999`。**只有 999 抛异常**，因其语义是「调用方必须重登」。
- 业务失败（`code != 0` 但非 999）**返回数据、不抛异常**。
- 解析不 speculate：缺字段/类型错且无默认 → `ParseError`。

坐标：`yiban/adapters/protocol_yiban/SPEC.md` §1、§3（唯一规格来源）。

---

## 2. 七族单一定义点清单

旧系统 2026-10-25 建了七族契约等值门（PR #202）。逐族说明新栈如何对账。

| 族 | 旧系统载体 | 门 | 新栈对账口径 |
|---|---|---|---|
| 分页 | `web/routes/__init__.py` 的 `PAGE_LIMIT_{MIN,MAX,DEFAULT}`（1/200/100）；`data.py` 形态门禁内联回填 | `tests/test_pagination_contract_gate.py` | 新栈用 OpenAPI 定义分页参数，须**生成代码与契约同源**；若手写处理器，按同名门做源码形态断言 |
| cron 时刻 | `deploy/prod/cron.d/yiban-sign`（06:31/07:12）↔ `yiban/core/window.py` 的 `DEFAULT_FIRST_HM`/`DEFAULT_RETRY_HM` | `tests/test_cron_time_equivalence.py` | **显式豁免**：新栈无 cron 面（单进程调度）。在豁免表登记「无此载体」 |
| 退出码 | `yiban/engine/cli_support.py` 的 `_EXIT_KINDS` ↔ `docs/dev/cli.md` 表 ↔ workers 别名 | `tests/test_exit_code_table_gate.py` | 见 §6：需要一次裁决哪些码沿用 |
| 截断 | `RESULT_MAX`／`AUDIT_DETAIL_LIMIT`（均 200），九面引用 | `tests/test_truncation_literal_gate.py` | 新栈须有同等常量；审计 detail 与结果摘要各一，禁止裸 200 |
| SSE | `web/routes/stream_api.py` 读 cfg 缺省（15s/500） | `tests/test_sse_default_parity.py` | **显式豁免**：新栈无 SSE 面。若将来加实时推送，此处重新立项 |
| 脱敏 | `PHONE_RE`／`EMAIL_RE`／`EMAIL_USER_MAX`，测试样本由常量派生 | `tests/test_masking_phone_contract.py`、`test_web_mask_email_parity.py` | 见 §4 双口径对账 |
| 周末判定 | `yiban/engine/schedule.py::day_off` ↔ `frontend/src/calendar/model.js::offDayOf` | `tests/test_weekend_parity.py`（node 真跑 7 天×4 组） | 新栈若把周末判定放在 `core/plan`，单侧实现即可；若 UI 也要判（置灰），保留跨语言对拍 |

**要求**：新栈逐族给出「已覆盖／需新增／显式豁免」三列结论。豁免必须写明理由，
不接受「没有就跳过」。

---

## 3. 隐私暴露语义（三条）

来源：PR #201（后端）＋ #208（前端），qmz4 单。

1. **默认全不暴露**：`profile`（姓名/学号/学院/班级/邮箱）默认全部不暴露。
   默认态下所有既有端点回包**逐字节不变**——这是最小暴露面判据，新栈须落成
   API 响应断言（golden 级，不是字段抽样）。
2. **管理列表永不出现**：暴露字段只出现在「本人视图」与「管理详情」两个面；
   管理列表是逐行广播面（全量枚举、默认脱敏），**任何开关组合下都不含**这些字段。
3. **只提交改动键**：写接口只提交本次改动的键；未提交键保持原值，不得被空串冲掉。
   前端做脏检查，后端做子集合并。

补充口径（旧系统实现事实）：
- `profile` 整体 AES-GCM 加密，AAD＝手机号，与 `password`/`phone_code` 同通道。
- `profile_expose` 是明文布尔集（非 PII）：损坏方向＝少暴露。
- 失败方向：坏密文 → 响亮错误，不静默吞成空。

新栈对账要求：三条各写一条断言；「管理列表永不出」须有**全开关组合**用例，不能只测单开。

---

## 4. 脱敏双口径

旧系统在 2026-10-25 已拆成两套口径，新栈需对账差异：

| 口径 | 目的 | 方向 | 载体 |
|---|---|---|---|
| **写侧（入库/入日志）** | 宁多勿漏 | 只要可能是 PII 就掩 | `yiban/core/masking.py`：`mask_phone`、`mask_device_code`、`MaskingFormatter`（日志全量装配） |
| **检测侧（门/告警匹配）** | 宁缺勿冤 | 只认确定形态，减少误报 | `PHONE_RE=^1\d{10}$`、`EMAIL_RE`、`EMAIL_USER_MAX=32` |

**关键差异**：写侧可以过掩（多掩无害），检测侧不能过严（会把正常文本判成 PII 拒判）。

新栈对账要点：
- 掩码格式是否与旧系统一致（手机号 `139****5678` 形态；设备码 `devc…3456` 截断形态）。
- 日志层是否有等价的全量 Formatter（旧系统是入口装配，不靠各处手写）。
- 检测侧样本是否**由常量派生**，而不是手抄例子（手抄会在常量变更时静默失配）。

---

## 5. 审计锚点边界

旧系统 2026-10-25 定稿（PR #197）：

- 审计链有哈希链＋锚点文件；锚点与 web 进程**同 uid 可写**。
- 取得该 uid（含应用级代码执行）者可改锚点并重签自洽——**链本身不能证明未被篡改**。
- **只有离机副本是兜底**；同机副本与锚点不构成防篡改证据。

新栈落地要求：
- 若锚点属 P7，先在豁免表登记「审计锚点边界待 P7」，并写明「当前不声称防篡改」。
- 一旦落地，须复述上述边界（同 uid 可写、仅离机副本兜底），不得表述为「不可篡改」。

---

## 6. 退出码语义与「跳过≠绿」

### 6.1 旧系统码表（只增不改）

| 码 | 含义 |
|---|---|
| 0 | 全部成功（或无需执行） |
| 1 | 存在真实失败（账号/网络/配置） |
| 2 | 全部跳过／窗口外未了结 |
| 3 | 队列忙（进程级锁被持有） |
| 4 | schema 迁移完整性拒启（`user_version` 与完成记录不符） |
| 10 | `--second-run-check` 专用：需要补跑第二轮 |

并行汇总取最严重者：`4 > 10 > 1 > 3 > 2 > 0`。

### 6.2 新栈裁决（待拍板）

新栈是**单进程无执行体**，码 3（队列忙）与码 10（补跑第二轮）的语义可能不存在。
需要一次明确决定：

- 沿用：0/1/2（语义普适）。
- 待定：3（单进程仍有 DB 锁竞争？）、4（迁移拒启仍然要区分吗？）、10（是否还有「第二轮」概念）。
- **不存在的码不要占号**：码表只增不改的意思是不复用旧含义，不是必须保留空位。

### 6.3「跳过≠绿」（跨栈通用的门纪律）

- 因环境不满足而跳过的检查必须**显性可见**（打印 SKIPPED＋原因），不得静默。
- 整套没跑却报绿＝门不存在。
- 未登记的跳过即判红；豁免须登记理由。

旧系统载体：`scripts/dev-verify.sh` 的 `SKIP_EXEMPT_PATHS`／`SKIP_EXEMPT_REASONS`，
`tests/test_skip_visibility_gate.py`，`scripts/deploy_gate.sh` 的 SKIP→PARTIAL。

---

## 7. fixtures 形状与映射（最关键一节）

### 7.1 事实

- 位置：旧系统 `tests/protocol/fixtures/`，共 18 个文件。
- `replay_manifest.json` 登记 12 条：**6 captured ＋ 6 synthetic**。
- 未登记的 5 个文件是**非响应体文本件**（解析输入，不入回放面）：
  `iframe_location.txt`、`sign_in_body.txt`、`sign_in_body_urlencoded.txt`、
  `usersure_form.txt`、`usersure_url.txt`。
- 旧系统回放机制：`yiban/adapters/protocol_fake/replay.py`（opt-in，`--replay-dir`）。
  `covers()` 只看端点是否有条目；**仅 synthetic 覆盖的端点回 501 不回退**（保守方向）。

### 7.2 抓包是 HTTP 层，Provider 契约在其上——缺一层映射约定

以下三条是实验线必须遵守的约定：

**(a) 传输内部字段不进契约。**
这些属于 HTTP/会话层，Provider 的 `Result`/`Profile` 不得携带：

- Cookie、CSRF token、`page_use`、PEM 公钥原文；
- `verify_request` 的查询串；
- 请求体里的设备参数原值（`phone_code`/`phone_model` 只在 `Credential` 内一次性使用）。

**必须进契约的**（业务语义）：
- 窗口起止（epoch→`time.Time`，时区 +8）；
- 签到点列表（含 `Range`/`Points`/`LngLat`，坐标 WGS84）；
- 结果 `Outcome`（见 7.4）；
- `Evidence`（脱敏后的上游 msg 片段、http 状态、envelope code）。

**(b) 结果如何从抓包与信封推导。**
映射表（旧系统 `classify_failure` ＋ R3 批判据的等价物）：

| 抓包/信封形态 | Outcome | 是否可重试 |
|---|---|---|
| `code:0` 且 `data:true` | `success` | — |
| `code:0` 但 `data` 非 true（如空对象、`{"Id":""}`） | `rejected`（**不是 success**） | 否 |
| `code:999` | 会话失效 → 重登；`Outcome` 由上层决定（旧系统为 `credential_invalid` 的上位） | 是（重登一次） |
| 文案含「已签到」「重复」 | `already_done` | 否 |
| `Position` 为空数组 | `no_target` | 否 |
| `Position` 存在但坐标为 0/空 | `invalid_target` | 否 |
| 文案含「授权设备」 | `device_unbound` | **否**（确定性失败，旧系统仍重试 3 次＝待改） |
| `IsNeedPhoto` 触发且未提供 | `photo_missing` | 否 |
| 照片被拒文案 | `photo_rejected` | 否 |
| 页面含 `ydclearance`/`acw_sc`/风控页特征 | `challenge` | 否（**红线：不做自动识别**） |
| 网络错误/超时/5xx | `transient` | 是 |
| 以上都不匹配 | `unknown` | 需人工；**不得等于 success** |

**(c) synthetic 的 provenance 必须随样本传下去。**
旧系统要求证据可追溯：每份夹具在 manifest 里标 `provenance: captured|synthetic`，
`serve()` 对未实拍状态回 501，**不把 synthetic 当已知下发**。

新栈要求：
- 录制回放适配器读到的每个场景，必须携带 `Provenance` 字段；
- 一致性套件（`signing/conformance`）对 `synthetic` 场景只能断言「解析不 panic、结果落 `unknown`」，
  **不能断言业务成功**；
- 未实拍的状态显式标「未知」，不许编造。

### 7.3 一份 captured 样本原文与它的契约映射

文件：`tests/protocol/fixtures/sign_position.json`（provenance=captured）。
顶层键：`code`/`msg`/`data`；`data` 下：`AcsState`/`State`/`Msg`/`OutState`/`Remark`/
`FileUrl`/`Type`/`Position[]`/`Range`/`IsNeedPhoto`/`AttachmentFileName`。

原文（脱敏后，缩进为阅读所加，实际为单行）：

```json
{"code":0,"msg":"","data":{
  "AcsState":"off","State":0,"Msg":"","OutState":"on",
  "Remark":"因受不可抗因素影响导致定位有误，允许在范围之外签到，但需上传签到地点的照片",
  "FileUrl":"","Type":"campus",
  "Position":[
    {"Id":"27464ed31464763a6827816a0ac9c8ab","UniversityId":"cb49cdd74db8bb035e927bd8b8ab4ab9",
     "Campus":"","BuildingId":null,"Type":"campus",
     "Title":"示例省示例市示例区示例大学示例校区-示例苑·公共教学组团",
     "Address":"示例省示例市示例区示例大学示例校区-示例苑·公共教学组团",
     "LngLat":"118.101837,32.003954","Range":70,"MapType":2,
     "Points":["118.101177,32.005118","…共 13 个点…"],
     "AddressName":"自定义多边形","CreateTime":1668235132},
    {"Id":"519c864f4cb663758c864724dd7b178c","UniversityId":"b88a00a549d364b9eb061a9acef9c6ad",
     "Campus":"","BuildingId":null,"Type":"campus","Title":"示例省示例市示例区示例大学示例校区",
     "Address":"示例省示例市示例区日晖路示例大学示例校区",
     "LngLat":"118.103894,32.007838","Range":110,"MapType":2,
     "Points":["118.103377,32.006228","…共 5 个点…"],
     "AddressName":"示例校区","CreateTime":1668235199}
  ],
  "Range":{"StartTime":1790980200,"EndTime":1790985000,"SignDay":1,"RelatType":1,"RelatTimeType":1},
  "IsNeedPhoto":2,"AttachmentFileName":""
}}
```

**这份对应 `DiscoverTargets` ＋ `DiscoverWindow`，不是 `Sign`。**
- `Position[]` → `[]Target`（本例 2 个，`Range` 分别为 70 与 110 米）。
- `Range.StartTime/EndTime`（epoch 秒）→ `Window{Start,End,Source:platform}`；
  本例即 2026-10-03 06:30–07:50+08。
- `data.State` 是账号在当日链上的状态位（0＝可签），**不是签到结果**；
  不要把它当 `Outcome`。
- `IsNeedPhoto: 2` 是照片要求档位；`2` 的语义需与上游文档或实拍确认（旧系统未定论）。

另一份：`sign_in_response.json`（captured）＝ `{"code":0,"msg":"","data":true}`
→ 对应 `Sign` 的 `OutcomeSuccess`。**注意 `data` 必须是布尔 `true`**；
旧系统 R3-002 修的正是「只判真值」导致 `{"Id":""}` 被误判成功。

### 7.4 Outcome 词表（新栈已定义 13 个）

`success`、`already_done`、`pending_upstream`、`no_target`、`invalid_target`、
`credential_invalid`、`device_unbound`、`challenge`、`transient`、`rejected`、
`photo_missing`、`photo_rejected`、`unknown`。

口径：`unknown` 与 `success` **必须保持区分**（新栈 contract 包自述已写明这条）。

---

## 8. 明确不搬清单

以下内容在旧系统被**裁决删除或明确不做**，实验线不得因「旧系统有」而搬回去：

| 项 | 旧系统处置 | 原因 |
|---|---|---|
| 任务级租约族（按时间戳判活＋宽限期） | 已删除（工单 l1rx） | 单进程＋进程句柄即可；心跳只服务 UI |
| HRW 粘性分片 | 保留但冻结；多执行体已移除 | 单进程不需要一致性哈希分配 |
| 多执行体（workers 多进程） | 保留接口、生产单执行体 | 单进程是明文设计不变量 |
| 旧调度器（`executor_v3` 的窗口/块容量/elbow） | 生产在用，但复杂度高 | 新栈用 `core/plan`＋`core/capacity` 重写，目标是更简单而非等价 |
| SQLite schema（`user_version` 1→25） | 旧栈事实 | 新栈是 PostgreSQL＋goose，schema 不搬；**数据迁移另立项** |
| 旧 UI（Vue＋Element Plus ＋ core.js 壳） | 旧栈事实 | 新栈是 React＋Adminator；`ui/` 已成型 |
| 旧部署件（`deploy/prod`、`docker/`、cron、supervisord） | 旧栈事实 | 新栈单二进制；部署件需重新设计 |
| 验证码自动识别 | **红线，不做** | 只做 `looks_like_challenge` 检测，人工接管 |
| 设备指纹伪造 | **红线，不做** | 设备标识按上游真实接口读取，不生成 |
| 「复用前一晚会话」的优化 | **禁止** | cdff 实测：会话跨自然日 00:00 失效（envelope 999） |

---

## 9. 2026-10-26 增量（v0.6.5 前后发生了什么）

本节只写**会改变新栈判据**的事实，不写过程。来源是当天合并的六张 PR（#216–#221）。

### 9.1 账号密钥：合法性的唯一判据在解析器，不在正则

旧系统原先用 `grep` 判「`.env` 里的 `YIBAN_ACCOUNTS_KEY` 是否合法」，该判据**窄于**应用自己的解析器。实测会静默换钥、旧密文不可解、且无任何告警的三种写法：

| 写法 | 应用是否接受 | grep 是否接受 |
|---|---|---|
| 大写十六进制 | 是 | 否 |
| 值内有空白/制表 | 是（`bytes.fromhex` 忽略） | 否 |
| 文件开头 UTF-8 BOM、行尾 NBSP、CRLF | 是（`utf-8-sig` ＋ `strip()`） | 否 |

**新栈必须照这个语义**：合法钥＝能解出 32 字节，且**只看解析结果**。旧系统现在的实现是 `account_crypto.decode_accounts_key`（唯一判据）与 `ensure_accounts_key`（部署取钥入口）。`ensure_accounts_key` 的另外三条语义也要照搬：

1. 写回是**原子替换**（临时件＋`os.replace`，0600），失败清临时件；
2. 替换后**恢复原属主**（root 生成、`yiban` 读）；
3. **同名键多行且生效值非法时显式拒绝**，不自动换钥——「静默换钥」等价于数据丢失。

### 9.2 备份：同一作业恰好一个活跃触发器

旧系统在 2026-10-25 因**两条 cron 触发同一备份**而丢了当天的密文（两跑互删）。现在有三条不变量：

1. 同一作业的活跃触发器**计数必须为 1**；哨兵每天统计并在 ≠1 时告警；
2. `/etc/cron.d` 的文件名规则按 cron 实现：**只允许 `[A-Za-z0-9_-]`**，其余（含带点号的）一律被 cron 忽略。新栈若也走目录式排期，必须实现同一条白名单，否则「以为没跑、其实跑了」或反之；
3. 校验清单**原子写**（临时件＋mv）；孤儿临时件按 mtime 60 分钟清理。`kill -9` 不可捕获，孤儿上界＝下一轮轮转，这条要写进运维预期。

### 9.3 配置缺省值：只住名册，代码不留第二份

- 缺省值只允许出现在配置名册（旧栈是 `config/registry.json`）；代码里的读取点一律取「名册缺省」，不得内联字面量。
- **承载常量必须显式声明**：若某常量承载名册缺省，它要登记在 `default_symbols` 里，门才能钉住「改常量 → 红」。未声明的载体＝门看不见的第二定义点。
- `default_required` 是**导入期**绑定；`.env` 的**热改**仍由读取器每次现读文件。两者语义不同，不要合并成一处缓存。
- 新栈建议不要照抄「逐键上限」的做法（见 9.5）。

### 9.4 时间：单一时钟源，业务日固定 +8

- 一切「现在几点」取 `yiban.core.clock`（固定 `+08:00`，naive 北京时间）；业务日切换、日志文件名、备份名都按北京日。
- 门禁止的字面量：`timedelta(hours=8)`、`8 * 3600`、`28800`、`hours = 08`。新栈若用 Go 的 `time.FixedZone`，请同样只留一处定义。

### 9.5 仓库与 CI：本仓门层的已知弱点（供参考，不要照抄）

- 测试规模已到 **142,104 行 / 5,323 条**，是产品代码（46,320 行）的 **3.07 倍**；其中 47 个文件、19,561 行是「元门」（测门本身的）。
- 2026-10-26 当天 CI 红 6 次，**产品缺陷 0 次**：共享事实家族上限漂移 ×2、README 行号豁免漂移、job 撞 10 分钟上限、deploy-gate 旧镜像 schema 偶发 ×2。
- 结论（已立案 `47w`）：**用结构规则代替计数**。例如「缺省只能来自名册」「路径只能经唯一入口」是 AST 规则，一次成立永久成立；而「某键在代码里出现 N 次」每次改动都要人工校准。
- `verify` 作业上限已从 10 提到 15 分钟；`tests/test_verify_jobs.py` 已移出快集走串行轨（异步校验任务用真实墙钟等终态，并行时会互相干扰）。新栈自建 CI 时，别把「需要墙钟收敛」的用例放进并行快集。

### 9.6 新栈当前状态（2026-10-26 只读核对）

- 分支 `test/go-p1-20261025`：140 个 `.go` / 25,497 行 / 48 个测试文件，领先 `develop` 110 个提交。
- 最近提交集中在 `signing`：`verify` 端点、传输失败的错误信封、掩码上限、超时判据——即本文件长期标注的**唯一硬缺口**（没有真实 Provider）正在被补。
- 仍缺：**实拍对齐**。六请求链目前的形状来自旧实现与既有夹具，不是新的真实抓包。旧系统一停，抓包窗口就没有了，这一步要在切换前做完。

---

## 附：证据与坐标

| 内容 | 旧系统坐标 |
|---|---|
| 协议唯一规格 | `yiban/adapters/protocol_yiban/SPEC.md` |
| 六请求链编排 | `yiban/platform.py`（`login_legacy`/`login_killyiban`/`fetch_sign_position`/`fetch_device_state`/`submit_sign_in`） |
| 解析与构造 | `yiban/adapters/protocol_yiban/`（page/location/envelopes/identity/position/forms/device/crypto） |
| 夹具与清单 | `tests/protocol/fixtures/`（`replay_manifest.json`） |
| 回放机制 | `yiban/adapters/protocol_fake/replay.py` |
| 脱敏 | `yiban/core/masking.py` |
| 退出码 | `yiban/engine/cli_support.py`（`_EXIT_KINDS`）、`docs/dev/cli.md` §3 |
| 七族门 | `tests/test_{pagination_contract,cron_time_equivalence,exit_code_table,truncation_literal,sse_default_parity,masking_phone_contract,weekend_parity}_*.py` |
| 隐私暴露 | PR #201／#208；`web/routes/my_api.py`、`web/routes/accounts_api.py` |
| 审计锚点边界 | `docs/guides/deploy-docker.md`（「安全运维」审计节；2026-10-26 从 README 搬入）、`yiban/adapters/persistence/audit_chain.py` |
| 会话跨日失效实测 | 结论已写入 §8「复用前一晚会话」行；原始探针数据为 56 次跨 9 天记录（过程稿，不入库） |
