# 审查工单索引

正文在各文件的审查报告里，这里只登记一行。规则见 `docs/dev/python-script-contract.md` §十一。
严重度是"这段代码可不可信"，不是"线上是否在流血"——最后一条备注会改实际优先级。

| 编号 | 级别 | 一句话 | 报告 |
|---|---|---|---|
| T-ALGO-1 | P2 | 凹围栏下兜底全落空，返回未经校验的质心 → 签到以普通失败收场且看不出是定位问题（三角剖分升级方案已验证可行，见报告 §6，未实施） | `yiban-fyiban-algo.py-review.md` |
| T-ALGO-2 | P3 | `1e-12` 的"防水平边除零"说法在三处（源码注释 / `PROVENANCE.md:19` / 测试名）都是错的；除零不可达，它实际是退化边上的几何偏置 | 同上 |
| T-WAF-0 | P3 | 文件末尾孤立 `# ---- 签到 ----`（抽层残骸）—— **已办结** | `yiban-fyiban-waf.py-review.md` |
| T-WAF-1 | P1 | 缺真实 ydclearance 抓包 fixture：唯一样本是自造假页面且连合法 JS 都不是，模板换版无测试会红 | 同上 |
| T-WAF-2 | P1 | 变换 A 的运算顺序与本层所依据的 JS 文本原生读法不同（同参数 JS 得 255、实现得 30） | 同上 |
| T-WAF-3 | P1 | 变换 B 的循环下界与 A 差一位，且 B 的 `while` 条件根本没被正则提取，边界是硬编码的 | 同上 |
| T-WAF-4 | P1 | `do{...}while(--qo>=2)` 至少执行一次，`range(n,1,-1)` 在 `n<2` 时一次不跑；唯一样本正是 `n=1` | 同上 |
| T-WAF-5 | P2 | 基准主机常量内联在"策略靠注入"的第三方层 + 协议相对 target 被静默改写 + 白名单不看端口 | 同上 |
| T-WAF-6 | P2 | 四类异常（`IndexError`/`ZeroDivisionError`/`ValueError`/`OverflowError`）越过"必须抛明确错误"契约；解码结果无完整性校验 | 同上 |
| T-WAF-7 | P3 | `client.py` 的两个挑战方法已无生产调用方；`looks_like_challenge` 无任何直接用例 | 同上 |
| T-WAF-8 | P3 | 5 处 `re.compile(...).findall(x)[0]` 可换 `search`——**只以可读性为理由**，性能理由已被实测否掉 | 同上 |
| T-WAF-0…8 **（study 基线已重落，2026-09-19 晚）** | — | 逐条判：0 develop **已修掉** · 1 **仍成立且加重**（仓库 fixture 与真模板**系统性不同形**：A 段少外层括号、B 下界写 2、po 上界根本没写 ⇒ 测试全绿只是"和自己造的那份一致"）· 2 **已不成立**（真模板 3/3 带外层括号，与本层读法逐字节一致；4,194,304 组 0 差异 ⇒ **读法错的一直是 fixture**）· 3 算术一半**不成立**（真模板 B 下界就是 3），"条件没进正则"仍成立 · 4 仍成立但真模板结构下不可达 → **降 P3** · 5 仍成立（定性）· 6 仍成立，可达面收窄到 `arg` 无界 · 7 仍成立（零调用方 + `:31` True 分支 0 次）· 8 仍成立（10 万次 0 不一致） | `yiban-fyiban-waf.py-review.md` §study |
| **T-WAF-9**（新） | **P1**（若真实易班页面同形则升 P0） | **`po` 的上界取错了，本层对真模板族每次必少解最后一个字节**：`waf.py:119` 用变换 C 的 `n_c` 收尾，而真模板明写 `for (qo = 1; qo < oo.length - 1; qo++)`，实测关系 `n_c = len(oo) - 3`（三份逐份核：253/243/57 → 250/240/54）⇒ 真上界 = `n_c + 1`。丢掉的那个下标**恰在 C 变换范围之外**，是模板故意留的收尾字符（跳转路径的右引号）⇒ 引号丢失后 `path_m` 抠不到 → 抛"未提取到 cookie/跳转路径"，**登录硬失败而不是解错值**。实测：按真实形状造的 3 例页面 **3/3 抛**（node 侧全部正确）；只把上界改成 `len(oo)-2` 后 **2/2 与 node 逐字符一致**。仓库内无处可验这一条，正因为 fixture 的 po 上界恰好等于 `n_c`（同源自证）。候选方案取"边界全部来自提取 + 提取后一致性校验"（顺手收掉 T-WAF-4/6 的越界面） | 同上 §6 正文 + §8 控制器复核 |
| T-DOC-1 | ~~P3~~ **已裁定不做** | 注释契约不并入主仓（用户 2026-09-19）。审查代理一律读 `D:\code\yiban-wt-slim4\docs\dev\python-script-contract.md` 绝对路径 | `python-script-contract.md` |
| T-LOG-1 | **P1 → 用户裁定不改** | 日志泄漏精确坐标（经纬度未打码，48 天 693 条）。**修法与历史日志清理均已被明确否决，后续审查不得重复提议** | 取证记录见本页"生产实证" |
### `security.py` 已登记（2026-09-19，控制器已复核）

| 编号 | 级别 | 一句话 |
|---|---|---|
| T-SEC-1 | **高（代码）/ 生产未观测** | `masking.sanitize_url` 只打码 query，**不碰 userinfo 与 fragment**；`log_response_diagnostics` 把整条 `resp.url` 交给它 ⇒ 跳转链上若带 `user:pass@` 或 `#token`，明文进日志。控制器实测确认原样透传，且**同层 `security.location_desc` 会遮**（两套口径） |
| T-SEC-2 | 中高 | `location_desc` 的 `.port` 不在 try 内：`:99999`/`:abc` 抛 `ValueError`（控制器复现），会把协议层预期的 `RuntimeError` 换成裸异常 |
| T-SEC-3 | 中 | 两档白名单都不判端口（实测 `:22`、`:6379`、`:8443` 全放行，会话 cookie 不分端口）；宽松档还接受空标签主机。**推荐收紧为 `port in (None, 443)`**——fail-closed 一侧，但属行为变更，待你点头 |
| T-SEC-4 | 中 | `is_waf_blocked` 长度 >2000 直接判"不是拦截页"= fail-open（丢掉"别重试"保护）；`WAF` 只命中大写；短文案实测误判 |
| T-SEC-5 | 低 | 判据函数对非 `str` 入参抛 `AttributeError`/`TypeError`；bytes 版 `url_desc` 输出畸形串进日志 |
| T-SEC-6 | 低 | 同一事实三份：`WAF_KEYWORDS` ↔ `signin.RISK_FAIL_KEYWORDS`、`ydclearance` 文案 ↔ `waf.py` 字面量、可变 list 全局共享 |
| T-SEC-7 | 低 | `log_response_diagnostics` 里 400 串证明的冗余 `replace` + 同一句诊断话重复两行 |
| T-SEC-8 | 低 | 脱敏与诊断侧 0 用例（`grep -rn log_response_diagnostics tests/` 为空） |

控制器生产取证（2026-09-19，只读）：`/var/log/yiban/*.log` 48 天里 **`最终 URL` 出现 0 次**，
含 userinfo 或 fragment 的 0 条 ⇒ T-SEC-1 目前是**潜在漏点而非既存泄漏**，定级按"代码高危 +
未观测"记，修不修不影响今天，但 T-SEC-8（零用例）使这条永远不会自己浮出来。

### `client.py` 已登记（2026-09-19，控制器已复核）

| 编号 | 级别 | 一句话 |
|---|---|---|
| T-CLI-1 | P2 | 界外坐标无人复核 = T-ALGO-1 在本层的那一半；**收口只能选一处**，推荐 `algo.py` 兜底返回 `None` + client 跳过该任务，而不是 client 再判一次 |
| T-CLI-2 | P2 | 围栏顶点未校验数量/`NaN`（`float('nan')` 能一路通过判空并污染采样） |
| T-CLI-3 | P2 | `data["data"]` 硬索引，服务端少给一个键就是 `KeyError` 冒到顶层 |
| T-CLI-4 | P3 | 五个包装方法 0 生产调用方（并更正 `waf.py:19` 那句"给旧调用方留的薄转发"——旧调用方已不存在） |
| T-CLI-5 | P3 | `task_name` 缺名时用"任务{已处理数+1}"编号，隐含"results_tasks 与任务一一对应"的不变量 |
| T-CLI-6 | P3 | `verify` 与 `signin` 的健康口径分叉（`Position` 为空 verify 仍报 ok） |
| T-CLI-7 | P3 | `random.shuffle` 熵源与统计口径（非 `SystemRandom`，点位顺序可预测） |
| T-CLI-8 | P3 | 4 组带日期叙事搬进 `CHANGELOG.md`（裁决语义已保留在注释里） |

证据补充（控制器独立复现）：C 形围栏 200/200 返回**同一个**界外质心、去重坐标数 = 1；
矩形/L/U 形 0/200。所谓"端到端"是假 session 记录提交体，**未打生产接口**。

## ⚠ 审查基线警报（2026-09-19，控制器实测）

工作区检出的是 `main` = `1d24006`（09-16）；`develop` = `9f4ef62`（09-19）**领先 152 个提交**，
`develop..main` = 0（main 无任何 develop 缺的东西），且**生产实跑的正是 `9f4ef62`**。
main↔develop 差异面：**全仓 187 个文件、`yiban/` 下 48 个 py**。本轮已审 6 个文件在 develop 上的差异：

| 文件 | develop 侧差异 | 处置建议 |
|---|---|---|
| `fyiban/algo.py` +4 −4 / `fyiban/waf.py` +6 −9 | 注释级 | 可搬运，抽查即可 |
| `security.py` +42 −18 / `client.py` +22 −27 / `masking.py` +70 −23 / `window.py` +42 −32 | 实质改动 | **在 develop 基线上重审**（报告结论可能已过期） |

⇒ 审查基准应切到 `D:\code\yiban-wt-v045`（本地已是 `9f4ef62` = develop = 生产同提交）。
未确认前不再铺新批次。

### `protocol.py` / `masking.py` / `window.py` 已登记（2026-09-19，控制器已复核，**基线待重定**）

| 编号 | 级别 | 一句话 |
|---|---|---|
| T-PROTO-1 | P2 | 默认新流程**无反爬自愈**：撞上挑战页只会通用失败（旧流才有 `solve_ydclearance` 分支） |
| T-PROTO-2 | P2 | 密钥损坏绕过脱敏诊断（抛 `ValueError` 而非走 `(None,None)` 路径） |
| T-PROTO-3 | P2 | 挑战分支的 cookie 作用域被放大（整罐重建） |
| T-PROTO-4…9 | P3 | `cookies={}` 死参数 · `SignResponse` 两态不闭合 · flow 用字符串开关 · 旧 CSRF 复用 · `117` 与公钥位长写死无关 · **`PROVENANCE.md` 三处口径错（"五步"、pycryptodome、ydclearance 非上游）** |
| T-MASK-1 | **高** | `sanitize_url` 保留位过宽：控制器按自己的 12 放置位实测**泄 5**（userinfo、fragment、path 段、裸 token 作键、`sig`），代理报"泄 7"未给放置位集合定义——**数字随枚举集而变，核心结论一致**；同层 `url_desc` 泄 0，说明 security 那套口径才是对的。修法应进 `sanitize_url` 内部（netloc 收 hostname + 丢 fragment），一处覆盖所有调用点 |
| T-MASK-2…6 | 中/低 | 高熵兜底只认纯 `[A-Za-z0-9_-]`（108 位 JWT 整串绕过）· `sanitize_text` 对 dict 形态手机号/邮箱 7/7 残留、9 种控制符只转义 CR/LF · `mask_phone` 两条 fail-open + `db.py` 有第二份同公式 · 键名子串误伤 7/16 而 `phone`/`tel` 不在表内 · main 上 `sanitize_url` 用例数 0 |
| T-MASK-1…6 **（study 基线已重落，2026-09-19 晚）** | 高/中/低 | 逐条判：1 **仍成立**（`sanitize_url` 泄 **6/12**、`url_desc` 0/12，`security.py:211` 仍裸调）· 2 **仍成立且更严重**（9/10 绕过，24 位阈值翻转点实测恰在 24）· 3 **拆开**（引号截断 develop 已修；PII 5/7、控制符 2/9 仍成立）· 4 **仍成立**（第二份实现已迁 `store/db.py:706`）· 5 **一半已修**（`phone/mobile/tel` 已入表；误伤 6/16）· 6 **已不成立**（本基线有 3 处行为断言，main 的"0 用例"是分支落差） |
| T-MASK-7 | 中 | `sanitize_text` 的 `authorization` 专项把键名统一改写成小写字面量且只吞**一段**值 ⇒ `Bearer AAA BBB` 的尾段 `BBB` 落日志。复现：`sanitize_text('Authorization: Bearer AAA BBB')` → `authorization=*** BBB`。与 T-MASK-3 同源但独立可修（值类改 `(?:"[^"]*"\|[^\s,;]+)`） |
| T-MASK-8 | 低 | **控制器一手实测**：`sanitize_url` 不转义换行，值走"最终放行出口"时 `%0A` 原样出门（4 例中 3 例带裸换行），而 `security.py:211` 的 f-string 直接落日志 ⇒ **可伪造日志行**；同一混入还绕过按值的两条判据（`?u=13800138000%0AXID%3D1` 整号明文）。修法二选一：出口统一转义 `\r\n`，或 security 侧再过 `sanitize_text` |
| T-WIN-1 | 中高 | 上界 `eff_hi` 口径分裂：实测回退日窗口至 07:49 而 `_next_retry_at` 按 06:56 判"窗口不足"返 `None`，**差 53 分钟**。控制器核到独立算点是 3 处（`signin.py:1641`、`signin.py:1961`、`web/app.py:1469`），代理写"5 处"含测试引用，计数口径需注明 |
| T-WIN-2…7 | 中/低 | 格式非法静默回退不上报（`07:60`→`invalid=False`）· 三套 HH:MM 接受面分歧（`07:6`、全角数字 Python 认而 run.sh/容器不认）· 旧键上界 600>300 · 无"窗口未开"判据（develop 已补 `is_open`，即基线警报的一例）· `max(0.0,…)` 216 例不可达 · 回拨 90min 即重开窗口且 `signin` 侧 0 引用 `clock_guard` |

### `account_crypto.py` / `claims.py` 已登记（2026-09-19，控制器已复核）

| 编号 | 级别 | 一句话 |
|---|---|---|
| T-CRYPTO-1 | **高（不可逆）** | `_write_key_to_env_file` 的过滤用 `startswith("YIBAN_ACCOUNTS_KEY=")` 不折空格，而 `_parse_env_file` **取末次出现** ⇒ 真钥行在前、`KEY = `（带空格空值）在后时，解析判为"未配置"→ 生成新钥并**把真钥整行删掉**。控制器四形态实测：case1 真钥从文件消失、case3 同（纯空值行）、case2/`KEY = K1` 单行安全 |
| T-CRYPTO-2 | **高** | 同函数按 `str.splitlines()` 拆行再 `"\n".join()` ⇒ 值里的 U+2028 被实体化成真配置行（控制器复现：写后按 `\n` 拆出 5 行、注入行独立成立）。`env_io.has_line_break` 在文件里别处有调用，**写入路径没走它** |
| T-CRYPTO-3 | 中高 | `_KEY_CACHE` 与来源脱钩：`load_key(a)` 后再 `load_key(b)` 仍返回 a 的钥；同进程内 `has_key(b)=True` 与 `load_key(b)` 给出相反答案 |
| T-CRYPTO-4 | 中高 | 密文不带密钥指纹 ⇒ 换钥后无法区分"解不开"与"数据坏了" |
| T-CRYPTO-5 | 中 | 不校验 key 类型，`TypeError` 越过调用方 |
| T-CRYPTO-6…11 | 低 | 同 AAD 槽位可互换 · `has_key` 每次读盘 · `.object` 上挂明文字节 · 判据两处口径 · 实现四份重复 · `str()` 强转 12 例中 7 例静默变形 |
| T-CRYPTO-12（新） | 低-中 | `has_key()` 不读 `YIBAN_ENV_FILE`（`account_crypto.py:113` 只有 `env_file or DEFAULT_ENV_FILE`），而 `load_key()` 三层回退读它（`:66-67`）⇒ 两函数默认值口径分叉。`docker-compose.yml:43` 确实设 `YIBAN_ENV_FILE=/data/.env`，分叉在真实部署里是活的；**但两条生产调用点都显式传路径**（`engine/config_check.py:23`、`store/db.py:735`）⇒ 今天不流血，属"新调用方用默认值就静默查错文件"的潜伏坑。修法：`has_key` 复用 `load_key` 的路径解析（不建钥那条），或删掉默认值改成必填 |
| T-CLAIMS-1/2/3 | 高 | 收尾时机与逐账号 settle（详见报告 §5）· 多执行体方向未定 · 写锁 16.3s 后 `try_claim` 仍返回 true 而行仍属他人（双执行体窗口） |
| T-CLAIMS-4…7 | 中 | 时钟回拨 1h ⇒ 锁定 4500s · 坏状态行领不动也不计入、闸门答"无需补签"（静默漏签形状）· 拨快 15 天 ⇒ 当日 2/2 行被 `purge` · `purge` 无守卫 |
| T-CLAIMS-8…11 | 低 | `attempts` 基准、`touch` 续租义务、NOT NULL 才触发 `IntegrityError`、只有被领过的账号才有行（原为注释矛盾，已就地改正） |

**三处注释与代码矛盾（已由本轮改对，记录以免回退）**：头注释称"BEGIN IMMEDIATE 语义领取"（本文件 0 处走
`_begin_immediate`）· 状态表把 `failed` 标"已了结"而调用方实际只认 `(SUCCESS,ALREADY,NO_TASK)` ·
"多执行体由启动期自检拦住"（全仓不存在该自检）。

**生产侧现状（只读）**：`sign_claims` 110 行全 `done`、每日 owner 恒为 1、`MAX(attempts)=0` ⇒ 上述红线均**潜在**，
因为生产当前是单执行体。多执行体上线前必须先定 T-CLAIMS-1/2。

## 网安取证带来的改判（`docs/re/fyiban-official-client-findings.md`，2026-09-19 两轮）

控制器已就地核实三条最可执行的断言：README 的 e003 伪装拒绝确在原文（`README.md:1205` 起）；
`headers.py:16` 那句"与 Auto-Test 保持一致"与实际 UA 不符；`IsNeedPhoto`/`SignDay`/`RelatType`/
`RelatTimeType`/`https_waf_cookie`/`_YB_OPEN_V2_0` **全仓 py 侧 0 命中**。

| 编号 | 级别 | 一句话 |
|---|---|---|
| T-ATTR-1 | **高（合规）** | `waf.py` 四段正则的**真实血缘是 `sdk250/Auto-Test`（该仓库 `license=None`，默认全权保留）**，不是 FYIBAN；上游全史（unshallow 后 13 commit）零 `ydclearance`、无 JS 引擎、无拦截器，`PROVENANCE.md:23-24` 的"衍生自 `BaseReq.kt`/交给 JS 运行时"两处为假。本轮我在 `waf.py` 头部新写的"衍生自 onefeifan/fyiban（AGPL-3.0）"**随之为假，必须一起改** |
| T-PHOTO-1 | **高** | `IsNeedPhoto` 无人读：开了"签到需拍照"的学校，我们照交空附件，只能以服务端拒绝的形式伪装成普通签到失败（同 C 形围栏那条一样的"静默漏签"形状） |
| T-WIN-8 | 中 | `Range.SignDay`/`RelatType`/`RelatTimeType` 三个窗口语义字段完全没碰，`client.py` 只取 `StartTime`/`EndTime` ⇒ 存在"窗口开着但规则不允许签"的读法缺口 |
| T-OBS-1 | 中 | 挑战分支不可计数：`protocol.py:268` 那条 `if` 不打日志，所以"有没有遇到过挑战"这个问题**我们没有记录能力**（我此前两次拿"日志 0 命中"当证据，都不成立）。同项还要覆盖"是否真拿到过 `https_waf_cookie`" |
| T-HEAD-1 | 中 | `headers.py:16` 注释与实现不符（UA 已补全 `CPU iPhone OS…` 段、`Safari/537.36`、版本升过 5.2.3；`Connection` 大小写亦不同）⇒ 措辞应为"特征取自 Auto-Test，UA 已修正并升版" |
| T-CSRF-1 | ~~低~~ **撤回** | 原建议"`csrf_token` 大写 vs 小写可能被服务端当指纹"收益为零：前一轮实测三种写法（随机 32 位、同名 cookie、共同祖先库直接写死 `"00000"`）**一路等价**，门禁是会话不是 CSRF 形态。⚠ 该实测出自另一轮材料，**控制器未复核**，仅"上游大写/我们小写"这一形态差作为事实留存 |
| T-HEAD-2 | 中 | `AppVersion` 不是装饰：前一轮实测版本门禁可枚举，**换版本号等于换验证码厂商**（5.1.2→shumei spatial_select、5.2.0→shumei icon_select、5.2.3+→yidun 同一 captchaId）。后果：①"把头改得更像 App"这条念头的真实代价要先读这条；②我们与 FYIBAN 走的通道 B（`oauth.yiban.cn/code/*`）全程无验证码，唯一门槛是会话 ⇒ 别把端点往通道 A（`m.yiban.cn/api/v4`，需解验证码 + `sig`）靠 |
| T-PROTO-10 | 低 | `protocol.py:129-137` 把"RSA-1024 单次最多加密 117 字节"写死，而公钥是登录页**现取**的（`:151-159`，实测每次动态）⇒ 若服务端把 H5 登录页公钥换长（官方 native 流那把是 4096 位），这道显式报错会**误伤长密码用户**。当前无证据显示会变，留坐标 |
| T-RESP-1 | 低 | `signIn` 的 `data` 若为 `''`/字符串而非 `true`，`code==0 and data` 的判定会走偏（上游 `getBoolean` 同形，属同源风险） |

**优先级重排（事实驱动）**：`README.md:1205` 已记载旧 iOS 流程撞的是**登录接口层的 e003 伪装拒绝，
压根不发挑战页** ⇒ `waf.py` 的实用价值≈0（只剩特殊网络场景）。因此 **T-WAF-1…8 与 T-PROTO-1 全部降为
"除非复活旧流程，否则不排产"**；`T-WAF-3`（B 段循环下界）虽然大概率是真 bug，也排在同列。
官方 App 侧不可判定（ZxProtect 加固，真机启动 0 秒 SIGSEGV，脱壳期望收益≈0），
唯一剩路是真机 + 自己账号的被动抓包，需另行授权。

`yiban/client.py:125` `use_killyiban = os.environ.get("YIBAN_LEGACY_LOGIN", "") != "1"`
⇒ **默认登录流不走 ydclearance 分支**，只有显式设 `YIBAN_LEGACY_LOGIN=1` 的部署会跑到
`waf.py`。T-WAF-1…T-WAF-4 因此不是"线上在流血"，而是"这段代码不可信且没有样本能证伪"。
是否仍要补样本对拍，取决于还有多少人开着旧流程。

## 生产实证（2026-09-19 只读取证，输出已脱敏）

采样面：`/opt/yiban-auto-sign` 裸机部署，HEAD `9f4ef62` / v0.4.5，56 个在跑账号，
`/var/log/yiban/sign-*.log` 共 48 天 6,496 行，`sign_events` 覆盖 2026-08-29…09-18。

| 观察 | 数值 | 影响到哪张工单 |
|---|---|---|
| `.env` 未设 `YIBAN_LEGACY_LOGIN` | ⇒ 生产走默认 KillYiBan 流 | T-WAF-1…4 的实际暴露面为零 |
| ~~48 天日志 `ydclearance`/`挑战` 命中 0~~ | **判据作废**：新流程的日志文案本就不含这些字样，0 命中是必然 | 由下一条取代 |
| 48 天 urllib3 响应状态分布 | 112 条响应行：**200×79、302×33，0 条 4xx/5xx**；挑战页特征（`window.onload=setTimeout`/`qo=eval`）0 命中。⚠ 覆盖有限：同期"登录成功"878 次里 223 次是缓存复用，响应级日志只采到一小部分 ⇒ 只能说明"被采到的样本里没有 521"，**不支持"从未发生"** | T-WAF-1…4、T-PROTO-1 定级 |
| `生成定位` 次数 / 去重坐标 | **693 / 693（重复 0 种）** | T-ALGO-1 的"返回同一质心"分支 48 天内**未出现过两次**（单次触发无法与正常样本区分，不下"从未触发"的结论） |
| 日志中的服务端拒绝含位置/范围字样 | 0（失败原因只有"未登录或登录已经超时"18、"未找到签到位置数据"12、重试若干） | T-ALGO-1 严重度维持 P2，不升级 |
| 围栏顶点原文是否落日志 | **否**（`Points`/`polygon`/`Address` 均 0 命中） | 真实围栏样本只能现取，生产数据里没有 |

**结论**：`waf.py` 那三条 P1 是"备用登录路径不可信"，不是线上风险；`T-ALGO-1` 需要真实围栏
才能定级——用户已确认凹围栏**必定存在**，故该分支不是理论缺陷，只是尚未观测到命中。

## 未决的存量红（与本轮审查无关，HEAD 基线已确认）

`tests/test_no_position.py`（3）+ `tests/test_scheduler_gate.py::BackupDockerScriptTest`（4）
= 7 failed / 1592 passed / 4 skipped，在干净 HEAD `1d24006` 上同样红。

## 补裸行轮登记（2026-09-21，控制器）

- **T-STFW-1（已闭合）**：`tests/test_state_file_writes.py::test_signin_state_writers_use_replace`
  用 `src.split("def <func>(")[-1][:2500]` 截窗口找 `os.replace`；逐行注释把
  `_write_sign_state` 的 docstring/正文撑长后，`os.replace`（state_io.py:216）落在 2500 截断点之后，
  断言误判"未用原子替换"（全仓唯一一处由注释引起的测试红，基线 v045 通过、study 失败已复现）。
  修复：窗口改为"`def <func>(` 起到下一个顶层 `def` 止"（按函数边界而非魔法字符数），
  study 复验 7 passed / 8 subtests。
- **T-RNOT-ND（纪律工单）**：补裸行轮只允许"行尾追加注释"，禁止改动行首缩进/结构。
  实例：notify.py 代理把 `v = data[field]` 移进 `if field not in data:` 块；data.py 代理把两行
  合并成非法三元且吞掉 `if not sign_events:` 头——均由主控修复并回归 AST。
- 其余 47 文件补裸行轮：注释与代码矛盾 0 修正；无新增行为工单。

## 2026-09-21 修复批收口

范围：`feature/review-fix-20260921` 行为修复 + 注释 P0 + T-ATTR-1，基线 `develop=37d7334`，
本批 25 个 commit（`37d7334..6444f6a`）；注释达标批 `eec405d` 在本区间之外，见文末单独记录。
逐条一行（编号 · 已修 commit）。

- **T-CRYPTO-1 残留** · `5c202b3`：rekey 回写 `.env` 改走 `env_io` 窄行写入，堵住潜伏分隔符与影子行（account_crypto 主路径随 N-1 在 `ed51e8b` 收口，`_write_key_to_env_file` 的空格影子行折不掉问题一并消除）。
- **T-WIN-1** · **本批未修**：`round.py::_next_retry_at` 的内联 `eff_hi` 口径与 `window.bounds()` 剪辑仍分裂，25 个 commit 无一触及该区域（全批 `git show ... -- yiban/engine/round.py | grep eff_hi` 0 命中）。本批唯一与 Windows 状态文件相关的修复是 `a8cddc5`（按日状态写入侧改 `utf-8-sig`），属 E4，与 eff_hi 窗口口径无关。
- **E2** · `9eb86cd`：`round.py::_mark_window_skip` 对"已有结论"账号按原结论透传进 `results`，runner 汇总不再按 `PENDING` 归失败；`_daily_statuses` 缺 `status` 键归一空串，与 `_has_conclusion` 同口径。
- **E5** · `6180ad8`：子进程 argv 按序位剔除 `--workers` 及其后随值（`--workers=N` 一并），值≠槽位数时不再漏进执行体 argv。
- **N-1** · `ed51e8b`：`.env` 写钥/写盐下沉 `env_io.write_env_key` 窄行模型，`tracking` / `audit_chain` / `account_crypto` 三处共用。
- **N-2** · `1aa0ddf`：`audit_anchor_path` 改走 `env_io.resolve_path`，与写入侧同口径。
- **E1+E7** · `4821832`：8 处内联 `os.environ` 兜底统一改走 `env_io.resolve_path`（runner/alerts/probe/cli_support/cred_state/state_gc，含 `log_dir_from_env`）。
- **E6** · `6180ad8`（源改动）+ `df074d1`（兜底轮末 `_save_cred_state` 与回归测试）。
- **N1** · `68b9ff5`：默认登录流最终认证改 `allow_redirects=False` + 逐跳白名单校验后手动跟跳，堵住 SSRF 面。
- **B1** · `9eb86cd`：设置部分更新时容量硬门按 `.env` 现存账号间隔计算，不再被缺省 0 绕过。
- **B3** · `d254758`：`YIBAN_BLOCK_CAP=0` 按"不限容量"处理，自选片接口不再除零。
- **B6** · `3232054`：旧钥仅来自环境变量时拒绝回落到游离 `.env`，要求显式密钥文件（fail-closed）。
- **B12** · `3c99ba0`：loadtest 自检/iptables 兜底失败非零退出并中止 K 阶梯，失败信息带上子进程输出。
- **B10** · `bb8ad7b`：字体分片脚本限定 outdir 在仓库白名单内，拒绝整目录删除任意路径。
- **C1** · `aef2eb2`：拒信正文与审核拒绝理由统一过 `_nl_safe`，堵住伪造行。
- **N5** · `212ba9f`：旧登录流 `reUrl` 归一后再校验与使用，堵住 `null` 裸 `TypeError`。
- **N6** · `212ba9f`：同提交——校验对象与使用对象同源（不再验 A 用 B）。
- **E3** · `16e6346`：容量预检两分支口径统一，起跑即窗口已过也即时推送。
- **E4** · `a8cddc5`：按日状态文件写入侧改用 `utf-8-sig` 读，BOM 不再清空整日数据。
- **E8** · `668fe05`：配置加载入口一并捕 `ValueError`，账号字段缺失不再裸 traceback。
- **C6** · `1de8398`：手动签到子进程回收对已退出进程与卡死 `wait` 兜底。
- **B2** · `36f612a`：周六签到开关注释与真实默认对齐（未配置=关闭）。
- **B4** · `c38b7b3`：待删除账号冲突文案按 `DELETE_GRACE_DAYS` 动态生成。
- **B5** · `663c41b`：`ensure_secret_key` 的 `.env` 读取纳入降级兜底。
- **B7** · `b7b6892`：`.env` 写盘失败时清理含新密钥的 tmp 文件。
- **N7** · `a9f916d`：隔离层成员清单的定位算法口径改为剪耳三角剖分。
- **T-ATTR-1** · `6444f6a`：waf 正则血缘按取证事实修正为 `sdk250/Auto-Test`（无 LICENSE）。

另有 `6fedec6`（注释 P0：轴注释/断言/行号引用符号化）与 `eec405d`（批 4 头部四问补齐 +
批 3 遗留同步），非上表工单，按注释轮单独记录。
