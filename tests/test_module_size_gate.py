# -*- coding: utf-8 -*-
"""模块化门禁：按类型设目标行数 + 超限必须写明工程理由。

标签：J · 运维：部署/备份/发布
覆盖：未登记文件的类型上限、超限必须登记、登记项必须给得出工程理由、硬上限
    `HARD_CAP`、登记表不得指向已不存在的文件、扫描覆盖全部源码类型。
对应实现：本文件自身即门禁（`SCAN_DIRS = [scripts, web, docker, yiban]`、`LIMITS`、
    `OVERSIZED`）；被测对象是仓库源码文件，不含 `tests/`。
关键断言：**红线不是目的**——真正的判据是"拆了是否更好维护"；理由写不出 20 字就失败
    （要么拆，要么说清楚）。
依赖：纯本地扫盘计数（`sum(1 for _ in f)`，注释行照算），不起子进程、不需 bash/
    docker/网络。⚠ 本文件是别的批次的硬约束来源：改它等于改门禁。

**判断某处该不该拆时，按顺序问自己**：
① 两部分是否服务于同一件事（同一状态机/同一表/同一协议）？是→不拆；
② 拆分是否要求把共享状态、回调或异常类型在模块间来回传递？是→**慎重**；
③ 是否存在"独立变更轴"（一端改动不需要另一端改动）？是→拆；
④ 是否有第二个调用方（复用）？是→拆。
"""
import os
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 各类型目标上限：行数语义不同（py 源码、js/css 资源、html/sh 模板与脚本），
# 阈值按现值分布给足余量，又不放过真正的膨胀
LIMITS = {".py": 600, ".js": 800, ".css": 800, ".html": 400, ".sh": 400}
# 绝对上限：与逐行较劲无关，只拦"失控式堆砌"
HARD_CAP = 12000

# 超限但**经工程判断暂不拆/无法简单拆**的文件：(行数上限 or None, 理由)
# 理由需包含：它是什么、为什么现在这样、下一步。
OVERSIZED = {
    "web/app.py": (2800, (
        "Flask 应用工厂 + 跨域中间件 + 名字面。工厂 `create_app` 内含 23 个 per-app 闭包"
        "（限速、登录守卫、CSRF、安全响应头、错误页、高危门禁与口令复核、每日清理线程），"
        "其中 14 处登记进 `app.extensions` 供各域路由取回；另有中间件类 1 个、顶层函数 67 个"
        "（其中 61 个是转发包装：调用时刻现取本模块模块级状态后注入服务层）与 routes 经 `m.*` "
        "取用的 200 个名字的兼容面。之所以是终态：实现已按域全部拆入 web/routes/、"
        "web/services/、web/render.py、web/security.py，剩下的正是「工厂 + 它的 per-app 闭包 + "
        "名字面」这层不可再分的核——中间件与口令门族捕获 per-app 状态（同一把 `_file_lock` 下"
        "的读改写序列、按 app 实例的限速表与门禁闭包），外移就得把共享状态改成跨模块注入"
        "（门禁判据②通信成本高，收益低于风险）；名字面也必须有唯一宿主，否则 "
        "`web.app.<名字>` 的打桩与 `web.routes.appmod()` 的晚查找会静默失效。"
        "下一步：若超过 2800 行，按「请求期守卫（限速 / CSRF / 安全头 / 错误页）」与"
        "「启动编排与每日清理」切成两个模块，per-app 状态一律经 `app.extensions` 显式传递，"
        "转发包装随其实现一同迁出。"
    )),
    "web/routes/accounts_api.py": (1000, (
        "账号管理域路由：十条管理员视图（列表/详情/增删改/批量/审核/排序）与 register()。"
        "规模来自注释契约（四问头 + 每处安全语义的「为什么」，约占四分之一）与真被依赖的"
        "顺序约束（二次鉴权先于占额度、审计先于外发、容量闸门、idx 错位守卫）——十条视图"
        "共用同一份「_file_lock 下读-改-写」语义，按端点切两半只会把这份序列与守卫在模块间"
        "来回传（通信成本高于收益）。下一步：若超过 1000 行，按「读（列表/详情）」与"
        "「写（增删改/批量/审核/排序）」切成两个模块，共享的 idx 守卫与掩码助手留在读侧。"
    )),
    "web/routes/my.py": (1000, (
        "个人自助族路由：十三条普通用户视图（我的账号提交/列表/编辑/软删/撤销/自暂停、"
        "自选时间片读写与统计、在线校验任务查询/取消、我的月历/日志）＋ 七个本人视图助手"
        "（_my_account_indices_of/_my_account_view/_pref_slots 等）与 register()。规模来自"
        "注释契约（四问头 + 每处「信息分层/为什么」说明，约占四分之一）与真被依赖的顺序"
        "约束（_file_lock 下读-改-写、idx 错位守卫、cooldown 与配额先后）。整族服务于同一份"
        "「本人身份 → 本人数据」收窄语义，助手被多条视图共用（判据①同一件事、②拆开要把"
        "同一份收窄口径在模块间来回传、③无独立变更轴）。下一步：若超过 1000 行，可按"
        "「账号自助（my-accounts 族）」与「选片/历史（time-pref、calendar、logs、verify-jobs）」"
        "一分为二，共享的本人视图助手留在账号侧。"
    )),
    "web/routes/settings_api.py": (1600, (
        "设置 / 执行体 / 公告域路由：十五条管理员视图（系统开关读写、执行体清单读写与单段"
        "出口改写、清单行增删改、现场实测、公告草稿/读取/双人发布、注册暂停状态、更新日志）"
        "＋ 两条落点助手（_executor_write_guard / _reply_slot_egress）与 register()。规模来自"
        "注释契约（四问头 + 每处口径与口令门次序的「为什么」，约占四分之一）与真被依赖的"
        "顺序约束（口令复核先于占额度、执行体写回要与清单同步、公告发布必须当次口令）。"
        "下一步：若超过 1600 行，按「系统设置 + 公告」与「执行体清单（含现场实测）」切成两个"
        "模块——两组之间没有共享状态，口令门都经 web.routes 取回，切分不需跨模块传状态。"
    )),
    "yiban/store/db.py": (800, (
        "SQLite 数据访问层的门面：启动编排 `init_db`、密钥来源解析 `resolve_env_file` / "
        "`require_existing_env_file`、写事务入口 `_begin_immediate`、时钟跳变守卫 "
        "`_clock_jump_guard`，以及跨域粘合助手（清理留痕 `_record_purge_event` / "
        "`_table_min_max`、手机号连带清理 `_cascade_phone_owned` / `_clear_session_cache_by_phones`）。"
        "表级 CRUD 已全部按域拆入同包模块，之所以是终态：剩下的粘合函数被各域模块**经门面"
        "反向依赖**（events/cleanup/users 等都按属性取），拆走就得把共享事务与留痕语义改成"
        "跨模块传递（门禁判据①同一件事成立、②通信成本高）；再导出与 `_FORWARDED_STATE` 读写"
        "转发面（57 项）本身也必须有唯一宿主，否则 `db._conn = None` 一类打桩静默失效。"
        "下一步：粘合函数若再增，参照 web 侧服务层收口模式另立存储服务层，而不是继续往本文件堆。"
    )),
    "yiban/store/users.py": (800, (
        "用户与注销域：users / user_delete_requests 两表的读写、"
        "最后管理员守卫、软注销与反悔恢复、到期物理清除、注销请求冷却计数，行数含注释契约要求"
        "的四问头与函数级说明（占约四分之一）。整块服务同一条状态机（软注销→宽限→恢复/物理"
        "清除）与同一对表，拆开就得把'最后管理员守卫'与'连带清理'在模块间来回传（门禁判据①"
        "成立、②不成立）。上限 800：超过则按「用户读写 + 角色守卫」与「注销/恢复/到期清除」"
        "切成两个模块，守卫用局部导入共享。"
    )),
    "yiban/store/accounts.py": (800, (
        "账号域：accounts 表的 CRUD、行加解密与明文自愈、运行期有效性判定，"
        "行数含注释契约要求的四问头与函数级说明（占约四分之一）。整块服务同一张表与同一份"
        "凭据加解密口径（写入加密、读取解密、明文 CAS 自愈三条路径必须共用同一 AAD 规则），"
        "按读写拆开就得把加解密与自愈语义在模块间来回传（门禁判据①同一件事成立、②通信"
        "成本高）。上限 800：超过则按「读路径 + 加解密」与「写路径 + 状态机」切成两个模块，"
        "共享的加解密助手留在读侧。"
    )),
    "yiban/store/audit_chain.py": (None, (
        "审计链域：HMAC 哈希链写入/校验、全表重链留痕、"
        "库外锚点族、审计密钥来源与缓存。四块服务于同一条协议与同一份取证状态——锚点校验"
        "要读清理留痕、体检要汇总链/锚点/留痕全部信号，拆开就得把这份状态改成跨模块传递"
        "（门禁判据①「同一件事」成立、②不成立）。下一步：若继续膨胀需要再拆，按「密钥来源"
        "与缓存（_audit_key 族，只依赖 connection/env_io/env_lock）」与「锚点文件（record/"
        "verify/anchor 族）」切成两个模块；当前不拆，避免为搬家再动 db 门面与打桩面"
        "（db._audit_hash = 替身 / db._AUDIT_KEY_CACHE = None 必须落在真定义点）。"
    )),
    "yiban/store/migrations.py": (None, (
        "schema 版本迁移域：基线建表、migrate_v1..v17、"
        "版本编排 `_run_migrations` 与迁移助手 `_table_columns`/`_ensure_column`/`_ensure_index`，"
        "以及 JSON → SQLite 自动导入 `_maybe_migrate`/`_rename_backup`（同属 init_db 启动序列）。"
        "整块是**一份按版本号冻结的时间序列**——已发布的迁移函数不可再改，拆开就得把冻结的"
        "迁移登记表与「核心/可选、失败是否阻断启动」的编排判据在模块间来回传递（门禁判据①"
        "「同一件事」成立、②通信成本高）。下一步：版本只增不改，行数会持续增长（现 955 行）；"
        "若超过 1000 行，按「迁移项（v1..vN，纯 DDL/数据修复）」与「编排 + 助手」切成两个模块，"
        "迁移登记表留在编排侧作唯一登记点。当前不拆，避免为搬家再动 db 门面与 `db._MIGRATIONS` "
        "读写转发面。"
    )),
    "web/security.py": (800, (
        "安全域：内置管理员会话凭据与角色判定、口令存储/校验与启动弱口令检测、"
        "客户端出口与 IP 计数表（回收/窗口计数/失败计数）、敏感口令门禁旋钮、账号校验"
        "配额与冷却、原子落盘（临时文件 0600 + Windows 换名重试）。整块服务于**同一件事**"
        "——「谁能以管理员身份进来、进来后凭据何时失效」这一条判定链：会话判定要对拍"
        "凭据快照（口令版本 + sid），启动迁移与启动拒绝共用同一份口令策略词表，"
        "限速计数表全是同一把 `_rate_lock` 下的同一套读改写语义。拆开就得把这套"
        "「先判后增 + 同一把锁 + 同一份 .env 口径」在模块间来回传（门禁判据①同一件事"
        "成立、②通信成本高），而拆出的两半依旧要一起改。规模另来自注释契约要求的四问头"
        "与逐处安全语义说明（约占四分之一）。"
        "下一步：若超过 800 行，按「会话与身份判定」与「口令/限速计数/原子落盘」切成两个"
        "模块，`_rate_lock` 与 `.env` 读取器仍从 web/services/locks.py 与 web.app 注入。"
    )),
    # scripts/signin.py 已按"执行一轮"的边界切分为 yiban/engine/*（最大 round.py 507 行），
    # 旧路径只剩兼容壳（约 130 行），故不再登记。
    "yiban/egress.py": (None, (
        "出口分配 + 执行体清单模型（纳入 `YIBAN_EXECUTORS` 出口清单后规模上升）。"
        "两半**互相咬合**：清单的兜底/回退读取要用 `resolve`（清单优先、旧三键回退），"
        "而 `resolve` 又要读清单——按「拆开」办就得让两个模块来回传「当前清单」，"
        "通信成本高于收益（门禁判据②）。已定好的拆法：把清单模型（parse/dump/行增删改/"
        "迁移/name）整体搬到新模块 `yiban/executors.py`，`resolve` 侧用局部导入绕开循环"
        "（与 `engine/schedule.py` 里 `alerts` 的局部导入同法）；**清单模型下次变更时一起做**，"
        "不为了十几行做一次纯搬家（搬动会同时动 web/runner/测试几十处调用点）。"
    )),
    # yiban/engine/schedule.py：容量与排期（调度 v2 的 2×2 填充框架 + 窗口判定 +
    # 通道数口径 + 容量公式分派与 K 的唯一口径）。四种模式共用同一份块划分与容量口径，
    # 拆开要把 `_schedule_blocks` 的块列表与 `_slot_to_bi` 的片号基点跨模块来回传（门禁
    # 判据②），而"改一处必漏另一处"正是这个模块的历史问题（窗口退化的三处告警去重标记
    # 就是为此收口的）。行数账目按实测：把内联的通道数公式上提为 `channel_count`（M 的
    # 唯一口径，供容量公式、令牌桶突发额度与执行体通道数三处共用）后为 618 行；补上
    # `capacity_of`（按开关分派两套容量公式）与 `executor_count`（K 的唯一口径）后为
    # 675 行。下一步：若超过 760 行，按「配置解析（_schedule_config / _env_int /
    # planner_config）」与「排期算法 + 容量口径（build_schedule 及其填充助手 /
    # capacity_of / executor_count）」切分——前者是纯读取与回退告警，后者只吃配置快照。
    "yiban/engine/schedule.py": (760, (
        "容量与排期：调度 v2 的 2×2 填充框架 + 窗口判定 + 通道数/容量公式/K 的唯一口径。"
        "四种模式共用同一份块划分与容量口径，拆开要把块列表与片号基点跨模块传（判据②）；"
        "把内联的通道数公式上提为 `channel_count` 后为 618 行，补上 `capacity_of`（按开关"
        "分派两套公式）与 `executor_count`（K 的唯一口径）后为 675 行，再加窗口退化三标记"
        "的业务日复位点（`reset_daily_alerts`，与三处置位分支同属一个状态机）后为 706 行。"
        "若超过 760 行，按「配置解析」与「排期算法 + 容量口径」切分：前者是纯读取与回退告警，"
        "后者只吃配置快照，无共享可变状态。"
    )),
    # yiban/engine/runner.py：入口与轮次编排（分支顺序 + 三道门 + 计划写状态文件 +
    # 收尾与退出码汇总）。规模来自**分支顺序本身是契约**：兜底常驻 → 补签轮判定 →
    # 多执行体派发 → 探针 → 零账号守卫 → --only → --check-config → 周末/暂停门 →
    # 补签轮定向重跑 → 进程锁 → 一轮队列 → 汇总，每步的先后都被既有行为与测试钉住
    # （"补签轮判定必须在派发之前"就是实测出来的修复点），拆文件会让"顺序"这个唯一
    # 事实源分裂成两处。它此前已到 589/600，加调度 v3 的分流点（执行体实现二选一）
    # 与执行体导入后越线；批 1 Task10 再补监督 rc 归集（未了结取池∪状态文件并集）与
    # v2/v3 兜底分流后为 665 行。下一步：若超过 700 行，按「命令行装配 + 分支顺序（main 前半）」
    # 与「一轮执行 + 收尾汇总（main 后半）」切分，两半之间只传 accounts / notify_url /
    # 三道门判定结果这几个值。
    "yiban/engine/runner.py": (700, (
        "入口与轮次编排：分支顺序本身是契约（兜底常驻 → 补签轮判定 → 多执行体派发 → "
        "探针 → 零账号守卫 → --only → --check-config → 周末/暂停门 → 补签轮定向重跑 → "
        "进程锁 → 一轮队列 → 汇总），拆文件会把「顺序」这个唯一事实源分裂成两处。"
        "加调度 v3 的分流点、执行体导入与 `--only` 单进程收敛（派发判据）后为 655 行；"
        "批 1 补监督 rc 归集（池∪状态文件并集）与 v2/v3 兜底分流后为 665 行。"
        "若超过 700 行，按「命令行装配 + 分支顺序」与「一轮执行 + 收尾汇总」切分，"
        "两半之间只传少数几个值。"
    )),
    # yiban/store/claims.py：签到领取池与账号级租约（v17）。规模来自**四条纪律与两条
    # 显式处置路径**的逐条为什么（每条纪律都对应一次真实事故的防线），加上弃权原因
    # 分档的 result 前缀协议与收尸/接管的租约判据——这些判据都在同一条 upsert 的
    # WHERE 里，拆开会把"谁领到"的唯一事实源分裂成两处。
    "yiban/store/claims.py": (720, (
        "签到领取池与账号级租约（v17）：领取/重入/接管/收尾/弃权/续租/收尸/统计。规模来自"
        "注释契约（四条/data 纪律与两条显式处置路径各写清「为什么」，约占三成）与"
        "「判据必须与写入同处一条 upsert」这一硬约束：拆分会把 `try_claim` 的三条冲突分支"
        "（done / claimed / failed）与其租约、epoch、弃权档前缀判据分散到模块间，"
        "「谁领到」这个唯一事实源就此分裂（门禁判据①同一件事）。行数账目按实测："
        "加弃权原因分档（result 前缀协议）后为 643 行；批 1 补兜底唤醒源分档前缀过滤与"
        "同账号让位收窄后为 696 行。"
        "下一步：若超过 720 行，把只读统计族（`states_for_day` / `stats` / `activity` /"
        " `in_flight_phones` / `owners_*` / `latest_claims_day`）整体外移到"
        " `yiban/store/claims_read.py`——它们只读、与本模块无共享可变状态。"
    )),
    # yiban/engine/round.py：一轮队列（两种形态共用一份重试分级与领取池：时间驱动堆队列
    # 与手动队列），加在领账号的心跳与轮末收尸后从 560 行越线。
    "yiban/engine/round.py": (700, (
        "v2 轮次核心：把账号列表跑成一轮签到（分级重试、调度 v2 时间驱动队列、手动队列、"
        "领取池分工、窗口收尾）。两条分支共用同一份重试分级与领取池闭包"
        "（`_claim`/`_settle_claims`/`_emit_event` 与 `claimed_day`/`claimed_epoch` 两个映射），"
        "拆开就得把这份共享状态改成跨模块传递（门禁判据①同一件事、②通信成本高）。规模另来自"
        "注释契约（每处窗口/领取/心跳/收尸语义的「为什么」）。行数账目按实测：加在领账号周期"
        "续租（`_ClaimHeartbeat`）与轮末收尸（`reap_unreported`）后为 646 行。"
        "下一步：若超过 700 行，把 `_ClaimHeartbeat`（续租节流 + 分段睡眠）拆成同包模块"
        "`yiban/engine/heartbeat.py`——它与本模块只经 `claimed_day`/`claimed_epoch` 两个映射"
        "通信，是可整体外移的一块。"
    )),
    # yiban/engine/executor_v3.py：调度 v3 的执行体核心（M 条通道 + 补货 + 崩溃恢复 +
    # 出口限速 + 重试退避 + 终态收尾，外加计划/V/分片集的解析与执行体文件心跳）。
    "yiban/engine/executor_v3.py": (900, (
        "调度 v3 的执行体核心：M 条 asyncio 通道 + 补货 + 崩溃恢复（租约回收 / 死主分片"
        "接管）+ 出口限速 + 重试退避 + 终态收尾，以及计划/V/分片集的解析与执行体文件心跳。"
        "通道、补货、桶状态落库、恢复与心跳在同一事件循环里共享运行期上下文"
        "（inflight/busy/桶状态/槽位），拆开就得把这份共享状态改成跨模块传递"
        "（门禁判据②通信成本高）。规模另来自注释契约（四问头 + 每处并发/收干/脱敏/恢复的"
        "「为什么」，约占四分之一）。行数账目按实测：补上最终放弃通知、顶层异常兜底与"
        "「无真实计划行即视为无可用计划」判据后为 677 行；再补租约回收、死主接管与文件"
        "心跳后为 756 行；批 1 再补 v3 failed 当日回炉、v2/v3 兜底分流与持有者身份落 "
        "PID/代次后为 867 行。下一步：若超过 900 行，按「通道与尝试闭环（_lane/_attempt/收尾"
        "出口）」与「计划、分片解析与崩溃恢复（_ensure_plan/_plan_v/_prescan/"
        "_mark_window_skips/_widen_with_dead_peers）」切成两个模块，运行期上下文作为显式"
        "入参传递。"
    )),
    # yiban/cli.py：命令行统一入口（argparse 装配 + 七个子命令实现 + 单行 JSON 字段契约）。
    # 整块服务于同一件事——命令行面与它的退出码契约。拆开只增加跨文件对偶：新增子命令
    # 必须同时改"解析器"与"handler"，两个文件互为唯一调用方，没有独立变更轴（门禁判据②③）。
    # 当前 700 余行，其中约 45 行是给 agent 看的字段契约表。下一步：若超过 900 行，按
    # 「透传 sign/probe」与「只读运维子命令（config/capacity/state/db/version）」一分为二。
    "yiban/cli.py": (None, (
        "命令行统一入口：argparse 装配 + 七个子命令实现 + 单行 JSON 字段契约。整块服务于"
        "同一件事——命令行面与其退出码（每加一个子命令都要同时改解析与分发，拆成两个"
        "文件只增加跨文件对偶，没有独立变更轴）。当前 700 余行（含约 45 行字段契约表与"
        "逐条硬约定说明）；若增长到 900 行以上，按「透传 sign/probe」与「只读运维子命令"
        "（config/capacity/state/db/version）」切成两个模块。"
    )),
    "web/static/js/components/settings-executors.js": (800, (
        "执行体分区组件（规模 KPI + 清单表 + 行内设置弹窗 + 每个写操作的口令门 + 写明细口径的注释）。"
        "三块服务于同一个屏与**同一份接口响应**：lastData 被 KPI、清单渲染、行弹窗三处读，"
        "banner/focusAfterPaint/rowName/putRow 等助手三处共用——拆开等于把这份共享状态改成跨模块协议"
        "（门禁判据②），而任何接口字段变动仍要同时改多处（判据③不成立）。"
        "容量实测与建议已抽到 settings-quota.js；三种写操作（追加行/删行/改行）各带口令门，"
        "另有「只改名不打门」的分支。行数账目按实测：状态列的行内开关临时提到 800，"
        "该开关按用户要求收回、只留弹窗一个入口后为 778 行；补上多段保存取消后的收尾"
        "（重载视图 + 部分提交提示）后为 795 行；非取消失败复用同一收尾后为 799 行，"
        "上限保持 800——已贴到线上限，再涨就先切行内设置弹窗：openRow 及其独有助手"
        "（infoTip/linkBtn/ROW_HELP），届时要把它依赖的 lastData/putRow/banner 三样显式注入。"
    )),
    "web/static/js/core.js": (None, (
        "前端交互层核心（classic script，非 module）：全局 api/toast/modal/时钟/身份/导航行为，"
        "以及各页共用的 DOM 与脱敏助手，运行期只加载一次并挂在 window.YB。规模来自**共享本身**"
        "——所有页面模块都依赖这些全局符号，且加载顺序守卫钉住「core.js 先于组件与页面模块」，"
        "拆成多文件就得给同一份全局词法作用域加跨脚本加载协议（判据②通信成本高），并放大"
        "「拆文件撞名」的风险面。下一步：若继续增长，按「网络/身份」「UI 反馈」「助手函数」切分，"
        "切分前先重钉加载顺序守卫。"
    )),
    "web/static/css/app.css": (None, (
        "项目层唯一集中样式表：在 vendor 的 adminator.css 之后加载，只写设计系统没覆盖、"
        "或需按本项目语义扩展的部分。规模来自**单一样式上下文**——颜色/阴影/圆角一律取 "
        "Adminator 的主题 token，深浅两套主题才自动成立；字体栈是全站单一事实源，另有静态"
        "守卫对账收口。拆文件会破坏「加载顺序在 adminator.css 之后」与 token/字体收口的唯一"
        "来源性质（判据②）。下一步：若继续增长，把「token 与字体栈」（必须仍排最前）与"
        "「各节组件样式」切成两个文件，并同步收口守卫的对账清单。"
    )),
    "web/templates/pages/work_settings.html": (None, (
        "管理端系统设置页：全部系统级配置分区集中在一个模板里，含字段级权限矩阵（必须逐字段"
        "复刻后端判定）与模板素材复用说明。整页共用一套 tab 契约（[data-tab-group] + "
        "[data-tab-target] + [data-tab-id]）与一次「改动只标脏、点保存才提交」的语义，"
        "按分区拆文件会把权限矩阵与 tab 装配复制多份，并引入跨模板包含层（判据②）。"
        "下一步：若继续增长，把各分区移入 partials 由本页 include，tab 契约与权限说明"
        "留在本页作唯一登记点。"
    )),
    "scripts/backup.sh": (960, (
        "运维脚本：数据文件一致性快照 + 加密 + 保留策略 + --restore 恢复演练，"
        "头部使用/安装说明与各分支的防护校验占相当篇幅。行数账目按实测：M3 批次0"
        "（四项备份整改）补上密文回环自检 verify_encrypted_archive、tar 护栏判码、"
        "RETENTION 校验与最近 K 组轮转下界后为 864 行（原上限 800）。"
        "上限 960：参数解析由「只看 ${1}」改为全参数 case 循环（未知/移位旗标一律拒绝、"
        "`--require-encrypt` 位置无关），该门禁块为不可省的一小段；文件此前已贴 900。"
        "非运行时模块，给硬上限防继续膨胀；若超过 960 行，按「备份」与「恢复/校验」拆两个"
        "脚本（拆分须同步改 cron 安装行、README 与哨兵漂移比对——单文件是既有部署契约）。"
    )),
    # 工具脚本（非运行时模块，不参与模块化拆分），只设上限防继续膨胀
    "scripts/build_cjk_font_slices.py": (900, "构建期工具：字体分片生成脚本，一次性运行"),
    "scripts/loadtest/concurrency_probe.py": (800, "压测工具：并发探针，非运行时路径"),
    # mock_yiban.py 是压测假上游（易班协议 mock：端点 + 故障注入四旋钮 + 记账对平），
    # 非运行时路径。批 1 Task4 补假成功档与故障注入旋钮、记账原子化后到 674 行。
    # 若继续膨胀到 720 行以上，按「协议端点」与「故障注入旋钮 / 记账」拆两模块。
    "scripts/loadtest/mock_yiban.py": (720, "压测工具：假易班上游服务（端点+故障注入+记账），非运行时路径"),
}

# 扫描范围：运行时与共享代码（不含测试、构建产物、第三方）
SCAN_DIRS = ["scripts", "web", "docker", "yiban"]
# 各扫描目录纳入的扩展名：py 全量递归；前端资源限 web 内的 js/css/html（vendor 由剪枝排除）
SCAN_EXT = {
    "scripts": (".py", ".sh"),
    "web": (".py", ".js", ".css", ".html"),
    "docker": (".py",),
    "yiban": (".py",),
}


def _iter_scanned():
    for d in SCAN_DIRS:
        exts = SCAN_EXT[d]
        root_dir = os.path.join(BASE, d)
        if not os.path.isdir(root_dir):
            continue
        for dirpath, dirnames, filenames in os.walk(root_dir):
            dirnames[:] = [x for x in dirnames
                           if x not in ("__pycache__", "vendor", "node_modules", ".venv")]
            for name in sorted(filenames):
                ext = os.path.splitext(name)[1]
                if ext not in exts:
                    continue
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, BASE).replace(os.sep, "/")
                yield rel, full, LIMITS[ext]


def _count_lines(path):
    with open(path, encoding="utf-8") as f:
        return sum(1 for _ in f)


def _size_violations(entries):
    """未登记的超限文件清单；门禁与自检共用（entries 为 (rel, full, limit) 序列）。"""
    out = []
    for rel, full, limit in entries:
        n = _count_lines(full)
        if rel in OVERSIZED or n <= limit:
            continue
        out.append((rel, n, limit))
    return out


class ModuleSizeGateTest(unittest.TestCase):
    def test_unlisted_files_within_target(self):
        violations = [
            f"  {rel}: {n} 行 > {limit} 行"
            for rel, n, limit in _size_violations(_iter_scanned())
        ]
        if violations:
            self.fail(
                "文件超过目标规模且未登记（PROMPT.md §5.2 第 4/7 条）：\n"
                + "\n".join(violations)
                + "\n请二选一：按上文判据拆开，或在 OVERSIZED 里写明工程理由与下一步。"
            )

    def test_gate_rejects_unregistered_oversized(self):
        """自检：未登记的超限文件必须被拦下，已登记者放行（临时文件即用即删）。"""
        limit = LIMITS[".js"]
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "oversized_sample.js")
            with open(path, "w", encoding="utf-8") as f:
                f.write("// x\n" * (limit + 1))
            rel = "tmp/oversized_sample.js"
            self.assertEqual(
                len(_size_violations([(rel, path, limit)])), 1,
                "未登记的越线文件未被门禁拦下")
            self.assertEqual(_size_violations([(rel, path, limit * 1000)]), [])
        # 已登记者即便越线也放行
        core = os.path.join(BASE, "web", "static", "js", "core.js")
        self.assertEqual(
            _size_violations([("web/static/js/core.js", core, LIMITS[".js"])]), [])

    def test_scan_covers_all_types(self):
        """扫描面自检：各扩展名都必须扫到文件，防 SCAN_EXT 掉项后门禁静默失明。"""
        seen = {os.path.splitext(rel)[1] for rel, _, _ in _iter_scanned()}
        for ext in LIMITS:
            self.assertIn(ext, seen, f"扫描面漏掉 {ext} 类型文件")

    def test_oversized_entries_explain_themselves(self):
        """允许增长（limit=None）的登记项必须给出实质理由——"要么拆，要么说清楚"。

        工具脚本那类只给数字上限的条目是"防继续膨胀"，不参与拆分，无需长理由。
        """
        thin = []
        for rel, (limit, reason) in OVERSIZED.items():
            need = 20 if limit is None else 1
            if len(str(reason).strip()) < need:
                thin.append(f"  {rel}: 理由过短（{reason!r}）")
        if thin:
            self.fail("超限登记缺少工程理由：\n" + "\n".join(thin))

    def test_oversized_hard_limits_are_kept(self):
        """给了硬上限的登记项不得越线。"""
        violations = []
        for rel, (limit, _) in OVERSIZED.items():
            full = os.path.join(BASE, rel)
            if limit is None or not os.path.exists(full):
                continue
            n = _count_lines(full)
            if n > limit:
                violations.append(f"  {rel}: {n} 行 > 登记上限 {limit}")
        if violations:
            self.fail("工具脚本超出登记上限：\n" + "\n".join(violations))

    def test_no_file_runs_away(self):
        """绝对上限：拦"失控式堆砌"，与逐行红线无关。"""
        violations = [
            f"  {rel}: {_count_lines(full)} 行 > 绝对上限 {HARD_CAP}"
            for rel, full, _ in _iter_scanned()
            if _count_lines(full) > HARD_CAP
        ]
        if violations:
            self.fail("单文件失控膨胀（请按判据拆分）：\n" + "\n".join(violations))

    def test_entries_exist(self):
        """登记表不得指向已不存在的文件（拆走后请删除条目）。"""
        stale = [rel for rel in OVERSIZED
                 if not os.path.exists(os.path.join(BASE, rel))]
        if stale:
            self.fail("登记表指向已不存在的文件（拆分完成后请删除条目）：" + ", ".join(stale))


if __name__ == "__main__":
    unittest.main(verbosity=2)
