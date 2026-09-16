# 分支与发布纪律：推送 `main` 的门槛

> 用户 2026-09-16 明确要求：**只有确认安全测试与压力测试通过、非常稳定且脱敏时，
> 才允许推到 `main`**。本文件是这条要求的可执行版本（命令 + 判据 + 证据要求）。

## 分支约定

| 分支 | 用途 | 推送门槛 |
|------|------|----------|
| `refactor/*`（如 `refactor/web-adminator`） | 开发主线，允许中间态 | 随时可推（**但不得含真实凭据/部署信息**） |
| `server-web` | 部署分支（生产按它升级） | 与开发分支同步；至少要能启动、能跑一轮签到 |
| **`main`** | **已发布/已验证分支**，对外默认看到的代码 | **必须同时通过下面四条，缺一不推** |

## 四条门槛（逐条给命令与判据）

### ① 安全测试通过 + 脱敏

```bash
# 安全与鉴权/脱敏相关用例（既有覆盖面；跳过的 node 类用例在 Windows 上补跑）
python -m pytest tests/ -q -n 8 -k "security or mask or audit or login or private or csrf or ratelimit"
# 全量（含上述）
python -m pytest tests/ -q -p no:randomly -n 8
# 脱敏自查：不得含真实域名/IP/邮箱/手机号/密钥、部署与站点注册信息
# 排除 tests/：测试桩里的示例号码（形如 13800138000）与回环地址不算泄漏
git diff <旧提交>..HEAD -- . ":(exclude)tests" | grep -E "^\+" \
  | grep -nE "<账号名前缀>|[0-9]{1,3}(\.[0-9]{1,3}){3}|1[3-9][0-9]{9}|\bssh \b"
```

判据：安全类用例 **0 失败**；脱敏自查 **0 命中**（命中即修，不得以"看起来是测试桩"放过）。

### ② 压力测试通过（有实测数据，不凭估测）

适用于任何触碰**规模、并发、调度、数据库写入、网络路径**的改动：

```bash
# 容量基准（测试机；自建假易班、零真实外联、跑完自动还原）
sudo python3 scripts/loadtest/capacity_probe.py --repo <repo> --users 5000
# 多执行体一致性（4 执行体 × N 账号：零重复登录、领取池均分）
python3 scripts/signin.py --workers 4
# 容器/裸机形态的真签到演练（登录路径改动必做）
```

判据：容量结论与既有实测**不矛盾**（或已在真实形态复跑）；并发一致性成立（每账号恰好一次登录）；
无锁错误/OOM/超时退化。**结论要写进当批记忆点文稿**（跑了什么、结果数字）。

### ③ 足够稳定

判据：全量测试 **0 失败**（跳过项必须写明原因，如"CI 无 node"）；已知缺陷无新增；
核心路径连续多轮运行无退化（例如连跑 3 轮签到，状态文件与日志无异常增长/无重试风暴）。

### ④ 改动已脱敏 + 非生产形态演练过

```bash
# 裸机形态（生产同版本 Python）
python3 scripts/signin.py --check-config        # 退出码 0，且手机号已脱敏
# 容器形态：构建 → 容器内访问回环 → 两个进程（web/sched）都 RUNNING
docker build -t <tag> -f docker/Dockerfile . && docker run ...
```

判据：部署路径（入口脚本、镜像 COPY、cron/systemd 模板）在**非生产环境**真跑过；
镜像内含新模块（有 `tests/test_docker_image_contents.py` 兜底）。

## 推送流程（照抄）

```bash
# 1) 在开发分支完成并自测
git push origin refactor/web-adminator server-web
# 2) 逐条跑上面四条，把证据写进 docs（记忆点文稿）
# 3) 四条都过后，才把 main 指到同一提交并推送
git branch -f main HEAD && git push origin main && git push gitee main
```

**未通过时的唯一正确动作**：留在开发分支继续修。
**禁止**为了让 `main` "看起来同步"而先推后补——`main` 是对外默认可见的代码，
它必须始终处于"可部署、已验证、已脱敏"的状态。

## 与记忆点的关系

每批推送 `main` 后，在当批文稿里留一段"门禁证据"（四条各自的命令与结果），
供下一次交接复核；没有这段证据，等同于没通过门禁。
