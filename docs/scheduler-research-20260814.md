# 调度升级技术调研（2026-08-14）

> 背景：为「易班自动签到系统」未来的调度升级做技术调研，目标功能：
> 1. **混合签到时间队列**——不同账号可配置不同签到时间（如 A 账号 06:35、B 账号 07:00）
> 2. **普通用户自选签到时间**——用户在网页上为自己的账号选择签到时间（需解决公平/防拥挤）
> 3. 可能的一天多时段任务（早/晚）、不同星期几不同任务
>
> 调研执行：longcat-2.0 模型子智能体（web 搜索开源库与算法）；补充分析：主代理结合本项目架构。

## 一、结论摘要

1. **APScheduler 是 Python 生态中最适合本场景的调度库**：cron/date/interval 三种触发器、SQLite 持久化、动态增删任务、`misfire_grace_time` 错过窗口补偿、`replace_existing=True` 热更新，可与 Flask 深度集成。
2. **"数据库驱动调度表 + Worker"是长期演进方向**：天然支持用户动态改时间、水平扩展、任务错过补偿，适合未来多时段任务。
3. **时间轮/堆调度**：本项目账号量级（百~千级）用优先级队列足够，时间轮是过度设计。
4. **公平/防拥挤推荐"服务端时间片 + 随机错峰"双层策略**：前端选粗粒度时间片（如 5 分钟一档），后端片内随机偏移（±60s）+ 每分钟并发上限（如 20 账号/分钟）。
5. **重试推荐"指数退避 + 死信"**：首次失败 2 分钟后重试、之后 5/10 分钟退避，超过窗口（07:50）或连续 4 次失败则终止标记 failed。

## 二、调度库对比（Python 生态）

| 方案 | 原理 | 优点 | 缺点 | 适用性 |
|------|------|------|------|--------|
| **APScheduler 3.x** | 内置触发器 + 优先级队列 + 可插拔 jobstore/executor | 动态增删任务、SQLite 持久化、错过窗口处理、并发控制、时区原生、Flask 集成成熟 | 单进程模型、不天然分布式 | ⭐⭐⭐⭐⭐ 推荐 |
| croniter | 纯 cron 表达式计算库 | 极轻量、无依赖 | 只算时间不含调度执行 | ⭐⭐ 前端"下次执行时间预览"可用 |
| schedule | 内存轻量调度 | API 极简 | 无持久化/无 cron 表达式/无错过处理 | ⭐ 不推荐 |
| Celery Beat | 分布式任务队列+调度器 | 分布式高可用、生态丰富 | 重依赖（RabbitMQ/Redis）、Beat 静态配置、运维复杂 | ⭐⭐ 初期不推荐 |
| Dramatiq/RQ/Arq | 消息队列任务执行框架 | 比 Celery 轻量、支持重试 | 本身非调度器，需配合 cron 或自研 | ⭐⭐ 可作 Worker 层 |
| stdlib sched | heapq 简单事件调度 | 标准库零依赖 | 无 cron/持久化/并发控制 | ⭐ 学习用 |

其他生态：gocron（Go，时间轮+分布式，需引入 Go 栈）、node-cron/node-schedule（Node，无持久化）、BullMQ（Node+Redis，延迟队列/退避/死信优秀但引入双依赖）、Agenda/Pulse（Node+MongoDB，偏重）。

## 三、底层算法

| 算法 | 原理 | 适用 | 本项目 |
|------|------|------|--------|
| 最小堆/优先级队列 | 按执行时间排序，堆顶即最近任务 | 通用调度（APScheduler 采用） | ⭐⭐⭐⭐⭐ 完全胜任 |
| 时间轮 | 哈希环+槽位，O(1) 插入/取任务 | 高并发（Kafka/Netty） | ⭐⭐ 过度设计 |
| 红黑树/跳表 | 有序结构支持范围查询 | 需范围扫描场景 | ⭐⭐ 堆+轮询已够 |

## 四、开源项目参考

| 项目 | 仓库 | 适合点 |
|------|------|--------|
| APScheduler | github.com/agronholm/apscheduler | 核心调度引擎 |
| flask-apscheduler | github.com/viniciuschiele/flask-apscheduler | Flask 集成 |
| croniter | github.com/kiyoto/croniter | cron 预览 |
| 青龙面板 | github.com/whyour/qinglong | 多账号定时签到管理参考（Python/Node 混合） |
| BullMQ | github.com/Taskforcesh/bullmq | 延迟队列/重试/死信参考 |
| gocron | github.com/go-co-op/gocron | Go 生态参考 |

## 五、演进路线

### 路线 A：保留 cron + 脚本内按账号时间排序执行（过渡方案）

```
cron 06:31 → signin.py → 读取账号时间列表 → 按时间排序 → 循环:
   now >= task_time? 执行签到 : sleep(task_time - now)
```

| 维度 | 评估 |
|------|------|
| 复杂度 | 低——仅改造现有脚本（改动集中在 signin.py 一个文件） |
| 部署变化 | 无——仍是 cron + 单脚本 |
| 可靠性 | 中——脚本崩溃则全部账号失败 |
| 动态生效 | 改时间后需等下次 cron 触发（最多一天） |
| 错过窗口 | 需自研（脚本内判断是否已超 07:50） |

### 路线 B：APScheduler 常驻进程（⭐ 推荐近期目标）

```
Flask App（常驻，现有 systemd 服务）
  ├── APScheduler（BackgroundScheduler）
  │     ├── SQLAlchemyJobStore（sqlite:///jobs.db，持久化）
  │     ├── ThreadPoolExecutor（max_workers=10）
  │     └── Jobs: {account_1: cron 06:35, account_2: cron 07:00, ...}
  ├── Web API: /api/account/<id>/schedule（用户/管理员改时间）
  │     → scheduler.reschedule_job(job_id, trigger=CronTrigger(...))
  └── Web UI: 时间选择器
```

| 维度 | 评估 |
|------|------|
| 复杂度 | 中——引入 APScheduler + Flask 集成 |
| 部署变化 | 小——web 服务本已常驻（systemd），APScheduler 跑在 web 进程内，cron 可退役 |
| 可靠性 | 高——jobstore 持久化，服务重启后任务自动恢复 |
| 动态生效 | ✅ 实时——reschedule_job 毫秒级生效 |
| 错过窗口 | ✅ misfire_grace_time + 自定义 07:50 截止 |
| 并发控制 | ✅ max_instances + 线程池 |

关键代码骨架（longcat 提供）：

```python
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.triggers.cron import CronTrigger
from apscheduler.executors.pool import ThreadPoolExecutor

jobstores = {'default': SQLAlchemyJobStore(url='sqlite:///jobs.db')}
executors = {'default': ThreadPoolExecutor(max_workers=10)}
job_defaults = {'coalesce': True, 'misfire_grace_time': 300, 'max_instances': 1}

scheduler = BackgroundScheduler(
    jobstores=jobstores, executors=executors,
    job_defaults=job_defaults, timezone='Asia/Shanghai')

def add_signin_job(account_id, hour, minute):
    trigger = CronTrigger(hour=hour, minute=minute, day_of_week='mon-sat')
    scheduler.add_job(func=do_signin, trigger=trigger, id=f'signin_{account_id}',
                      args=[account_id], replace_existing=True)

def reschedule_signin(account_id, new_hour, new_minute):
    trigger = CronTrigger(hour=new_hour, minute=new_minute, day_of_week='mon-sat')
    scheduler.reschedule_job(f'signin_{account_id}', trigger=trigger)
```

### 路线 C：数据库驱动调度表 + Worker（长期方案）

```
Flask Web（管理界面）→ schedules 数据表 ← Worker（签到执行，可多实例）
```

表结构示意：

```sql
CREATE TABLE schedules (
    id INTEGER PRIMARY KEY,
    account_id TEXT NOT NULL,
    sign_time TIME NOT NULL,            -- 签到时间 HH:MM
    days_of_week TEXT DEFAULT 'mon-sun',-- 星期几
    status TEXT DEFAULT 'active',
    created_at TIMESTAMP, updated_at TIMESTAMP
);
CREATE TABLE signin_jobs (
    id INTEGER PRIMARY KEY,
    schedule_id INTEGER REFERENCES schedules(id),
    job_date DATE NOT NULL,
    scheduled_at TIMESTAMP NOT NULL,
    status TEXT DEFAULT 'pending',      -- pending/running/success/failed/skipped
    attempts INTEGER DEFAULT 0, last_error TEXT, completed_at TIMESTAMP
);
```

| 维度 | 评估 |
|------|------|
| 复杂度 | 高——自研 Worker + 状态机 |
| 部署变化 | 独立 Worker 进程（可多实例） |
| 可靠性 | 高——数据库持久化、水平扩展 |
| 动态生效 | ✅ 实时 |
| 适合场景 | 未来多时段任务、大规模账号 |

## 六、用户自选时间的公平/防拥挤方案

| 策略 | 描述 | 适配 |
|------|------|------|
| 服务端时间片 + 前端粗粒度选择 | 06:30–07:50 划 5min 时间片（16 片），用户选片不选秒 | ✅ 推荐 |
| 时间片内随机偏移 | 片内均匀随机一个执行时刻（现有 0-60s 随机延迟的扩展） | ✅ 推荐 |
| 每分钟并发上限 | 令牌桶/滑动窗口限 20 账号/分钟，超限顺延 | ✅ 推荐（防风控核心） |
| 按注册顺序分配秒级偏移 | FIFO 排序分配精确偏移 | ✅ 简单公平 |
| 排队预估等待 | 前端显示"预计等待 X 分钟"引导选冷门时段 | ✅ 体验优化 |

## 七、补充分析（结合本项目架构）

1. **web 服务本已常驻**（systemd yiban-web）→ 路线 B 的"部署变化"比通常估计小得多：APScheduler 直接跑在 web 进程内，**无需新增部署组件**，cron 可逐步退役。
2. **风险点**：APScheduler 常驻 web 进程后，web 重启 = 调度中断；依赖 SQLAlchemyJobStore 持久化兜底（重启后任务自动恢复）。
3. **过渡策略**：用户自选时间的 UI/数据层可先做，调度先用路线 A（signin.py 按时间排序）顶着；账号规模上来或需要多时段任务时再切路线 B。
4. **待定设计点**（实施前需决策）：
   - 账号时间存哪：`accounts` 表加字段 vs 独立 `schedules` 表（倾向独立，为多时段/多星期预留，与 sign-state 的 task 键对应）
   - 用户自选粒度：5 分钟时间片 vs 精确到分；片内偏移用户提交时固定 vs 每天重随机
   - 与现有随机延迟（启动 0-60s/间隔 0-10s）的关系：按时间点执行后，间隔随机延迟是否保留（时间点本身已有错峰效果）
5. **与 0.17.17 状态体系的衔接**：sign-state 文件已带 `task` 键，未来按"账号 × 任务"写多条状态；聚合显示规则（FAILED > RETRYING > PENDING > SKIPPED > 已了结）已预留。

## 八、引用来源

- APScheduler: https://github.com/agronholm/apscheduler · https://apscheduler.readthedocs.io/en/stable/modules/triggers/cron.html
- flask-apscheduler: https://github.com/viniciuschiele/flask-apscheduler
- croniter: https://github.com/kiyoto/croniter
- 青龙面板: https://github.com/whyour/qinglong
- gocron: https://github.com/go-co-op/gocron · node-cron: https://github.com/kelektiv/node-cron
- BullMQ: https://docs.bullmq.io/
- 时间轮 topic: https://github.com/topics/timewheel · delayq: https://github.com/spy16/delayq
- Python 调度对比: https://dev.to/st3m/options-for-scheduling-in-django-celery-cron-apscheduler-and-the-lightweight-alternatives-3hgi
- 分布式调度: https://dev.to/roni_das_b1b76c5ee6583027/designing-a-distributed-job-scheduler-that-runs-jobs-exactly-once-2cc3

---

## 九、新手导读（2026-08-14 补充）

### 9.1 三个评估维度是什么

| 词 | 通俗含义 | 在项目里指 |
|---|---|---|
| 易用性 | 使用者（学生/管理员）用起来爽不爽 | 改签到时间后多久生效？界面清楚吗？ |
| 易维护性 | 出问题好不好查、升级麻烦不麻烦 | 服务重启会不会丢任务？日志好不好懂？ |
| 易开发性 | 写代码费不费劲、改动大不大 | 新功能要写多少代码？会不会搞坏现有功能？ |

### 9.2 三条路线的类比

- **路线 A（闹钟 + 名单）**：一个定时闹钟（cron），响了之后按名单顺序逐个打电话。改动最小，但用户改时间要等明天闹钟响才生效；闹钟坏了当天全挂。
- **路线 B（常驻助理）**：办公室里一个助理（APScheduler 跑在常驻的 web 服务里），人手一本日程本（数据库）。到点自动执行、改时间立刻生效、停电重启后翻日程本继续。错过窗口/并发/恢复这些麻烦事助理自带，不用自己写。
- **路线 C（排班表 + 员工）**：大白板（数据库表）+ 多名员工（Worker）轮流看板。最灵活（多时段/多任务/多机器），但全部要自己设计自己写。

### 9.3 规模结论（2026-08-14 用户确认的典型部署画像）

> 单站点：使用人数 ≤ 100、管理员 ≤ 10、大概率所有账号使用相同签到窗口（06:30–07:50）。

在这个规模下：

- **路线 B 合适且推荐**：100 个任务对 APScheduler 毫无压力（量级支持到千级）；"相同窗口"不冲突——自选时间的意义正是窗口内错峰；规模越小，B 的复杂度越可控
- **路线 A 也能撑住，但体验差**：改时间隔天生效，与"用户自选时间"的即时性诉求矛盾
- **路线 C 完全没必要**：等出现"一天多个签到时段"或账号上千再考虑

### 9.4 给新手的一句话建议

把专业的事交给专业的库：直接选 **B**（APScheduler），不要自己造调度器；C 暂时忘掉。

