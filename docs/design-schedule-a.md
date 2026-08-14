# 方案 A 实施设计：窗口内自动错峰（2026-08-14）

> 目标：每个账号每天在签到窗口（06:30–07:50）内被自动分配一个均匀分布的时间点，系统到点逐个执行；用户可看到"今日计划签到时间"。100 人规模、相同窗口下，取代"启动随机延迟 + 账号间随机间隔"。
> 配套调研背景见 [scheduler-research-20260814.md](scheduler-research-20260814.md)。

## 一、执行模型改造

```
cron 06:31（主力）──┐
                    ├─→ run.sh（flock 单实例锁 + STATUS_FILE 防重复）
cron 07:10（备份）──┘         │
                             ▼
                    signin.py
                      1. build_schedule()：为每个账号计算今日时间点
                      2. 写 sign-state：全部账号 scheduled + message="计划 HH:MM"
                      3. 按时间点排序逐个执行：
                           now < 时间点 → sleep 到点
                           now ≥ 时间点 → 立即执行（含 07:10 备份进程的"补跑"）
                      4. 失败重试走现有队列逻辑（不等待计划，窗口内尽快重试）
```

**关键设计**：

1. **run.sh flock 单实例锁**（防止时间点模式下 06:31 进程 sleep 期间，07:10 cron 并发启动）：
   ```bash
   exec 9>/tmp/yiban-sign.lock
   flock -n 9 || { echo "...已有签到进程在运行，本次跳过" >> /var/log/yiban/sign.log; exit 0; }
   ```
   - 06:31 进程活着 → 07:10 拿不到锁退出；进程崩溃 → 锁自动释放 → 07:10 补跑；正常完成 → STATUS_FILE=SUCCESS → 07:10 被现有机制跳过（双重保险）

2. **时间点优先，随机延迟退役**：时间点模式下跳过启动/账号间随机延迟（会打乱时间点）；.env 配置保留兼容（默认关）。`--only` 手动签到不走计划（用户主动触发，立即执行）。

## 二、分配算法（含"随机顺序"支持）

```
窗口：06:30:00 ~ 07:50:00（4800 秒）
N = 参与账号数（load_accounts 已过滤 pending/rejected/deleted）
槽宽 = 4800/N 秒

账号槽位：
  列表顺序（sequence）→ 按 sort_order 固定排列（"谁在我前面"每天不变，可预期）
  列表随机（random）  → 当天 random.shuffle 重排（时间点每天全变，防风控最强）

时间点 = 槽起点 + 随机(0, 0.8 × 槽宽)   ← 两种模式共用，留 20% 余量防最晚超窗
```

| 模式 | 槽位 | 槽内偏移 | 可预期性 | 防风控 |
|---|---|---|---|---|
| 列表顺序 | 按列表顺序固定 | 每天随机 | 高 | 中 |
| 列表随机 | 每天随机重排 | 每天随机 | 低 | 强 |

- 设置页「签到模式」开关原样保留，语义延续："每次签到随机打乱顺序"
- 账号变动（增删）：每天重新计算，自动纳入
- 周日：走现有开关逻辑，跳过时不分配

## 三、状态与展示（复用 0.17.17 体系）

```
sign-state-{date}.json 每条新增 scheduled 字段：
  { "status": "pending", "scheduled": "06:42:00", "message": "计划 06:42", "task": "default" }
  执行后 message 被结果覆盖（签到成功/无需签到…），scheduled 保留当日计划

展示：
  用户端卡片（pending）："待签到 · 今日计划 06:42 · 前方排队 N 人"
  管理端表格：状态列 title "待签 · 计划 06:42"（/api/accounts 新增 state_msgs）
  日历：不变
```

## 四、代码改动清单

| 文件 | 改动 |
|---|---|
| `scripts/signin.py` | `build_schedule()`；`_write_sign_state` 加 scheduled 参数；main() 执行前写计划、按计划排序；run_queue_retry 加 schedule 参数（到点执行、跳过随机延迟） |
| `run.sh` | flock 单实例锁（3 行） |
| `web/app.py` | /api/accounts 加 state_msgs（脱敏）；my-accounts 已带 state_message 无需改 |
| `web/templates/user.html` | 卡片 pending 文案加"今日计划" |
| `web/templates/index.html` | 状态列 title 加计划时间 |
| 测试 | 分配算法断言 + 时间点执行顺序 + 前端展示 |

**不动**：数据库、部署（cron 不变）、状态码体系、API 结构。

## 五、验证方案

1. 算法断言：N=1/2/100 均匀性、窗口边界（最晚 ≤ 07:50）、时间点不重叠、random 模式两次分配不同
2. mock 集成：按时间点顺序执行、已过点立即执行、重试不受计划约束、--only 立即执行
3. run.sh flock 静态检查
4. playwright：用户端/管理端"计划时间"展示
5. 冒烟测试 13/13

## 六、通往方案 B 的路径（A 完成后）

```
方案 A（现在）→ 第 1 步：db.py 加 _ensure_column 列迁移机制（B 的地基）
              → 第 2 步：accounts 加 sign_time 字段 + web 校验 + 编辑链路
              → 第 3 步：build_schedule 升级（有 sign_time 用自选，无则自动兜底）
              → 第 4 步：用户端时间片 UI（16 片 + 拥挤度）
              → 第 5 步（可选）：APScheduler 常驻（秒级生效、多时段）
              → 方案 B 完成
```

关键转化点：build_schedule 从"唯一的分配者"变成"兜底的默认分配者"。
