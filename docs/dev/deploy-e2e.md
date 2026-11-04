# 部署 e2e（干净环境走通）

> 工单 6jk。判据来源：AGENTS.md §六 第 16/17 条。
> 可发布＝全新干净环境按部署件从零装成。
> 本文给出三条 e2e 的命令、断言与 CI 状态。

## 1. 三条命令

容器路径（自动轨）：

```bash
bash scripts/e2e/deploy-container-e2e.sh
```

裸机路径（发布前手跑项）：

```bash
bash scripts/e2e/deploy-baremetal-e2e.sh
```

升级回滚演练（发布前手跑项，见 §8）：

```bash
bash scripts/e2e/update-rollback-e2e.sh host --log-dir <日志目录>
```

三条都在仓库根目录执行。三条都在 WSL 或 Linux 上跑。
三条都写 `--log-dir` 指定目录与 docker 资源，不碰生产路径。

常用选项：

```bash
# 保留容器排障
bash scripts/e2e/deploy-container-e2e.sh --keep
# 日志落到指定目录（WSL 勿落 /mnt/d，见 §7）
bash scripts/e2e/deploy-baremetal-e2e.sh --log-dir /root/dvlogs/6jk-baremetal
# 受限网络：Go 模块走镜像代理
DEPLOY_E2E_GOPROXY=https://goproxy.cn,direct bash scripts/e2e/deploy-container-e2e.sh
# 受限网络：npm 走镜像源
DEPLOY_E2E_NPM_REGISTRY=https://registry.npmmirror.com bash scripts/e2e/deploy-container-e2e.sh
```

## 2. 容器路径断言

脚本 `scripts/e2e/deploy-container-e2e.sh` 按 `deploy/docker/docker-compose.yml` 起栈。
独立 compose 项目、独立卷、独立网络、独立回环端口。
镜像同时构建前端（Dockerfile 的 `ui` 段），页面由 Go 进程托管。

| 步骤 | 断言 |
|------|------|
| C1 | docker 可用；本项目无旧容器/卷/网络；端口空闲 |
| C2 | `docker compose build` 退出 0（含前端 `npm ci && npm run build`） |
| C3 | db 与 app 均 healthy |
| C4 | 容器内 `yiban-cli migrate` 打印嵌入集最新版本 |
| C4b | 签发→验签通过；错公钥必须被拒；篡改清单一字节后验签必须被拒（openssl 现场生成密钥，不入仓） |
| C5 | 宿主 `curl /healthz` 返回 200，body 为 `{"status":"ok"}` |
| C5b | 宿主 `curl /` 与深链 `/accounts` 都返回 200 且含 `<div id="root">`；未知 `/api/*` 仍是 JSON 404（`code=not_found`）且不含页面；`/assets/*.js` 带 `immutable` 长缓存 |
| C5c | 带 `Origin: https://<host>` 与 `X-Forwarded-Proto: https` 的登录请求回 401（无效凭据），不得回 403；403 表示 compose 默认可信代理未覆盖网桥网关 |
| C6 | 干净库 `users=0`；goose 最高已应用版本等于迁移目录最大编号 |
| C7 | `compose stop` 发 SIGTERM：退出码 0；耗时 < 30s；日志含 `worker: in-flight drained` |
| C8 | `compose down -v` 后容器/卷/网络均为 0 |

迁移版本不写死。脚本从 `server/migrations/postgres` 取最大编号。
新增迁移后，断言自动跟上。

C7 的日志断言有独立守卫：`TestRunClosesPoolAfterWorkerDrain`
（`server/internal/assembly/assembly_test.go`）。
它钉住「连接池不得在 worker 结算完成前关闭」。
2026-10-28 实测：旧形状 `srv.RegisterOnShutdown(stack.Close)`
与 `loop.Stop` 并行关池，`CountRunning` 报 `closed pool`。

## 3. 裸机路径断言

脚本 `scripts/e2e/deploy-baremetal-e2e.sh` 真跑 `deploy/linux/install.sh`。
它不用假 systemctl，也不用 `YIBAN_INSTALL_ROOT` 干跑前缀。

| 步骤 | 断言 |
|------|------|
| B1 | docker 可用；本前缀资源未占用 |
| B2 | 一次性 OS 镜像含真 systemd（PID1）、openssl 与 curl；旧缓存缺工具即重建 |
| B3 | 全新 PostgreSQL 18；public 表数为 0 |
| B4 | 用 `golang:1.27-alpine` 真编译 server 与 cli |
| B4c | 用 `node:22-alpine` 真跑 `npm ci && npm run build`；产物含 `index.html` 与 `assets/*.js`（可复用外部产物排障） |
| B4b | 对真编译产物签发清单→验签通过 |
| B5 | OS 容器 systemd 就绪；`/etc/yiban`、`yiban` 用户、二进制均不存在 |
| B6 | `install.sh --ui-dir` 退出 0，输出 `install complete: healthz ok` |
| B7 | 单元 active + enabled；独立 `yiban-cli healthcheck` 通过 |
| B7b | 容器内 `curl /` 与深链 `/accounts` 返回 200 且含 `<div id="root">`；未知 `/api/*` 是 JSON 404；`/assets/*.js` 带 `immutable` 长缓存 |
| B7c | 带 `Origin: https://<loopback>` 与 `X-Forwarded-Proto: https` 的登录请求回 401，不得回 403（`.env` 的信任链生效） |
| B8 | `.env` 600 root:root 且含 `YIBAN_UI_DIR=/var/lib/yiban/ui` 与 `YIBAN_TRUSTED_PROXIES=127.0.0.1`；二进制 755；photos 与 ui 属 yiban；名册逐字节一致；密钥形状正确 |
| B9 | 已装 `yiban-cli migrate` 打印最新版本；`users=0` |
| B10 | `systemctl stop` 后 `ExecMainStatus=0`、`Result=success`；healthcheck 已失败 |
| B11 | 容器/网络/卷清零 |

## 4. 失败口径

任何一步失败，脚本以退出码 1 结束。
脚本打印 `failed step: <ID> (<标题>)`，并回显该步日志尾部。
缺 docker 等环境错误以退出码 2 结束。
跳过不是绿：脚本不提供跳过开关。没有 docker 即红。

## 5. CI 与手跑登记（AGENTS §30）

| 路径 | 状态 | 位置 |
|------|------|------|
| 容器 | 自动轨 | `.github/workflows/deploy-gate.yml` 的 `container-deploy-e2e` 作业 |
| 裸机 | **发布前手跑项** | 本文件 §1；脚本每次运行打印 MANUAL 行 |
| 回滚演练 | **发布前手跑项** | 本文件 §1 与 §8；脚本每次运行打印 MANUAL 行 |

裸机与回滚演练不进自动轨的理由：它们要求 docker 的 `--privileged`
与 host cgroup。该形态尚未在 GitHub 托管 runner 上实测。
加入自动轨前，须先在托管 runner 上跑绿一次。
同一脚本可直接作为 CI 步骤使用，无需改动。

## 5.1 验收①的口径登记

验收①要求「按 README 一次装成」。
根 README 的主体描述旧 Python 栈。
新栈的从零安装指南是 `deploy/README.md`；根 README 顶部指向它。
本期已补该指南与页面断言（CLEAN-0）。
根 README 整体刷新仍归 `t32`。

## 6. 等价性与已知边界（AGENTS §32）

裸机 e2e 的容器当 OS 用。它逐条覆盖被测性质：

- 真 systemd 作为 PID1；`systemctl` 真调用。
- 真 `useradd`、`chown`、`install` 与 POSIX 权限位。
- 真 PostgreSQL 18，经容器网络连接。
- 真二进制，由生产同款工具链编译。
- 真前端产物，由 `node:22-alpine` 跑 `npm ci && npm run build` 得到。

不等价的一维：没有真实内核启动与硬件。
`network-online.target` 由 systemd 直接满足。
该维不影响上述任一性质。

边界一：`install.sh` 生成的 `YIBAN_HTTP_ADDR` 是 `127.0.0.1:47183`。
容器端口映射到不了容器回环。
故裸机路径的探活由已装 `yiban-cli` 在容器内执行。
容器路径的探活仍在宿主执行，覆盖外部可达性。

边界二：受限网络需要预置基础镜像。
预置只换镜像来源，不改变安装步骤。
示例（test47 实测可用）：

```bash
docker pull docker.m.daocloud.io/library/postgres:18 \
  && docker tag docker.m.daocloud.io/library/postgres:18 postgres:18
docker pull docker.m.daocloud.io/library/golang:1.27-alpine \
  && docker tag docker.m.daocloud.io/library/golang:1.27-alpine golang:1.27-alpine
docker pull docker.m.daocloud.io/library/node:22-alpine \
  && docker tag docker.m.daocloud.io/library/node:22-alpine node:22-alpine
docker pull gcr.m.daocloud.io/distroless/static-debian12:nonroot \
  && docker tag gcr.m.daocloud.io/distroless/static-debian12:nonroot \
       gcr.io/distroless/static-debian12:nonroot
docker pull docker.m.daocloud.io/library/ubuntu:24.04 \
  && docker tag docker.m.daocloud.io/library/ubuntu:24.04 ubuntu:24.04
```

## 7. 证据落点

默认日志目录：

- 容器：`work/deploy-e2e-container/`
- 裸机：`work/deploy-e2e-baremetal/`

每次运行写 `run.log` 与每步 `<ID>.log`。
`run.log` 末尾有 `result=PASS` 或 `result=FAIL`。
WSL 里不要把日志落 `/mnt/d`。跨文件系统追加会给 rc=141。
用 `--log-dir /root/...`。

## 8. 一键更新与回滚演练（update-rollback-e2e，U2 失败矩阵）

脚本 `scripts/e2e/update-rollback-e2e.sh` 真跑 `deploy/linux/update.sh`。
壳与裸机路径同款：一次性 Ubuntu 容器（真 systemd PID1、`--privileged`、
host cgroup）＋ 真 PostgreSQL 18 ＋ 独立前缀/网络/卷。

### 8.1 用法与安全守卫

```bash
# 宿主驱动（唯一宿主合法形态）：建容器、写标记、容器内跑演练
bash scripts/e2e/update-rollback-e2e.sh host --log-dir /root/dvlogs/9f6-rollback
# 保留容器排障；受限网络走 Go 模块代理
ROLLBACK_E2E_GOPROXY=https://goproxy.cn,direct \
  bash scripts/e2e/update-rollback-e2e.sh host --log-dir /root/dvlogs/9f6-rollback --keep
```

安全守卫（对症上一次「宿主误跑 update.sh 写穿真实安装位」的事故）：

- 演练步骤只认 `run` 子命令；`run` 必须在演练容器内执行。
- 判据＝标记文件 `/etc/yiban-e2e-container`（内容由驱动创建容器时写入，
  为演练主机名配置值）＋ 容器主机名与标记同值。两件同时成立。
- 容器外跑 `run`：退出码 2，**零写入**（守卫先于日志目录创建）。
- 驱动永不执行 `update.sh`；它只做 docker 编排。
- 驱动 H1 步每次现演负向用例：在宿主跑 `run` → 必须退 2、
  安装位元数据快照一致（`/usr/local/bin/yiban-*`、`/etc/yiban`、
  `/var/lib/yiban`、`/usr/lib/postgresql-common/pg_wrapper` 的
  名称/类型/大小/mtime）。
- 无参数调用：打印用法，退出码 2，零写入。

### 8.2 阶段与判据

宿主驱动步骤（前缀 H）：

| 步骤 | 断言 |
|------|------|
| H1 | docker 可用；宿主跑 `run` 被守卫拒绝（rc=2、零写入、安装位元数据快照一致） |
| H2 | OS 镜像含真 systemd 与 openssl（缺则按裸机同款 Dockerfile 构建） |
| H3 | PostgreSQL 18 就绪；应用角色 `yiban`（非超级用户、非库属主）有 public CREATE 与 CREATEDB |
| H4 | 六组二进制（v1..v6）真编译产出，server sha256 两两不同 |
| H5 | 演练容器 systemd 就绪；标记已写；`pg_dump`/`pg_restore`/`psql`（PG18 客户端）容器内可用；`scripts/release` 已复制（R2 真验签） |
| H6 | 容器内演练（R1..R10）退出码 0 |

容器内演练步骤（前缀 R）：

| 步骤 | 断言 |
|------|------|
| R1 | `install.sh` 装 v1 退出 0；unit active；独立 healthcheck 通过；schema 版本＝迁移目录最大编号；`update.sh --selfcheck-only` rc=0 且不创建状态目录；再放一个老机的 `/var/lib/yiban/pre-migrate`（root 0700）后，明文 `yiban-cli selfcheck` 仍 rc=0 且 `fs.backup_dir` 不是 FAIL（备份目录默认值跟上了 U2） |
| R2（签名错） | 现场生成 Ed25519 密钥、签发清单后篡改一字节：rc=1；日志含 `signing verification FAILED`；安装位不变；无 `.bak`/`.new`/状态目录；unit active |
| R3（自检 FAIL） | 把照片目录改成 root 专属（U1 的 `config.photo_dir`/`fs.photo_dir` 按服务用户判权）：rc=2；日志点名 `selfcheck FAILED` 与失败项；无状态目录；安装位不变；unit active；healthz OK |
| R4（备份失败） | 把 `pg_dump` 换成「找得到但一跑就失败」的桩（tools 检查只做 LookPath）：rc=2；日志含 `backup FAILED`；状态 `phase=backup`/`result=aborted`；`update.log` 无 `service stopped`；无备份目录（半成品被清掉）；unit active |
| R5（无 CREATEDB） | `ALTER ROLE yiban NOCREATEDB` 后跑 `update.sh`：rc=2；日志点名 `CREATEDB` 与 `--no-restore-db`；状态 `phase=preflight`；无备份；unit active；随后还原 CREATEDB |
| R6（迁移失败） | `REVOKE CREATE ON SCHEMA public` 后跑 `update.sh --no-restore-db`：rc=1；日志含 `migrate FAILED` 与 `permission denied for schema public`；安装位仍是 v1；无 `.bak`/`.new`；unit active；healthz OK；schema 不前进；DSN 不变；状态 `db_restore=disabled` |
| R7（健康失败+回库） | 跑含真迁移＋`main()` panic 的 v3：rc=1；日志含 `healthz failed after swap`、`db restore`、`rolled back`；DSN 切到 `yiban_restore_<ts>`；新库 schema＝升级前；原库保留且 schema 前进一档；恢复件 `users` 可读；安装位与 CLI 回到 v1；`.bak` 无残留；备份 `db.dump` 0600、目录 0700；unit active；healthz OK；状态 `db_restore=restored` |
| R8（正常升级） | 在恢复后的新库上升级到 v4，并首次带 `--ui-dir` 装页面：rc=0；安装位＝v4；`.bak`＝v1；schema＋2；`e2e_rollback_probe` 表存在；状态 `success`/`exit_code=0`/`db_restore=not-needed`；新增一份备份；备份根 0700 属 root、本份 0700 root、`env.backup` 0600；页面与 `.env` 的 `YIBAN_UI_DIR` 各就位且只有一行；状态文件 0600；证据文件不含 DSN 口令；pg 客户端 argv 不含口令且都拿到 PGPASSFILE |
| R9（健康失败 + UI 回滚） | 跑 v6（迁移集与 v4 相同 → migrate 空转）并带 `--ui-dir` 换页面：rc=1；日志含 `healthz failed after swap`/`rolled back`；安装位回到 v4；`ui.bak` 无残留、`index.html` 是旧页 `ui-one`；`YIBAN_UI_DIR` 只有一行且指回 `/var/lib/yiban/ui`；DSN 不变；状态 `db_restore=not-needed`/`exit_code=1`；unit active；healthz OK |
| R9b（备份件被篡改） | 在健康探活窗口里把本轮备份目录内的 `bin/yiban-cli` 改成组/其他可写（属主仍 root）：rc=2；日志含 `refusing to install the backup cli` 与 `writable by group or others`；安装位 CLI 未被篡改件替换（仍是本轮换上的 v6）；安装位 server 已换回 v4；状态 `exit_code=2`；unit inactive |
| R10（恢复失败） | 装一个只放行 `pg_restore --list` 的包装器（备份校验过、真恢复必失败），跑 v5（再迁移一档＋panic）：rc=2；日志含 `db restore FAILED` 与 `manual action required`；状态 `db_restore=failed`/`exit_code=2`，`db_restore_target` 是真实库名且出现在 manual action 里；DSN 不变；二进制已换回 v4；unit **inactive**（库状态未知，不自动起旧件） |

判据与 `update.sh` 头注一致：迁移失败回滚＝旧件继续跑（exit 1）；
健康检查失败回滚＝`.bak` 换回（exit 1）；迁移后失败且库已改变＝数据库恢复到
新库并切 DSN（exit 1）；恢复失败＝exit 2 + manual action，不启动旧件；
正常升级＝`.bak` 留旧件（exit 0）。

注入手法：迁移 SQL 嵌在二进制里改不了，演练在编译期给「新版」源码副本加
真迁移（号段从仓库实测最大编号 +1 起，不写死）；R6 用 psql
`REVOKE CREATE ON SCHEMA public FROM yiban` 让真 goose 路径撞权限错误
（应用角色不是超级用户也不是库属主，REVOKE 才咬得住）；R4 用一跑就失败的
`pg_dump` 桩打备份步；R10 用只放行 `--list` 的 `pg_restore` 包装器制造
「备份可读、恢复失败」。v6 是「同迁移集 + panic」，用来只测 UI/二进制回滚。

R3..R10 走 `--allow-unsigned` 显式逃生口；R2 走真签名链
（默认拒未签名的守卫是 `server/cmd/cli/install_update_e2e_test.go`
的 `TestUpdateShDeniesUnsignedByDefault`）。

### 8.3 耗时与前置

- Docker 前置：`--privileged` 与 host cgroup（同裸机路径）；镜像
  `postgres:18`、`golang:1.27-alpine`、`ubuntu:24.04`（或已构建的
  `yiban-e2e-os:ubuntu24.04`）。
- 实测耗时（2026-11-04，本机 WSL2 Docker，U2 矩阵 R1..R10；口径＝脚本内
  `P0.log` 创建到 `exit-code` 写盘的墙钟）：
  OS 镜像、Go 镜像与 Go 缓存卷全热时单遍约 2 分 40 秒；连跑两遍均 rc=0。
  冷首跑另含 OS 镜像构建（apt 装 systemd/openssl）与 Go 模块冷缓存。
  数随环境变，引用请自带口径。
- 验收口径：同一脚本连跑两遍，rc 均 0。

### 8.4 等价性与已知边界（AGENTS §32）

逐条覆盖的被测性质：`update.sh` 的签名验收与逃生口、停服前自检门
（U1 判据）、统一备份（真 `pg_dump -Fc` + `pg_restore --list` + 照片 tar +
sha256）、真 `systemctl` 停起 unit、真 goose 迁移成败、真
`CREATE DATABASE`/`pg_restore` 的数据库回滚与 DSN 切换、状态文件与退出码、
`/healthz` 探活、备份件所有权复核（被篡改的备份 CLI 不得被执行）。

不等价的一维：容器没有真实内核启动与硬件；`network-online.target`
由 systemd 直接满足。该维不影响上述任一性质。

边界一：探活地址是容器回环 `127.0.0.1:47183`，探活由容器内已装
`yiban-cli` 执行（同裸机路径 §6）。

边界二：R6 用 `--no-restore-db` 只测「回二进制」；「迁移后健康失败」
（R7）走默认数据库回滚，所以回滚件不会撞 `ErrDatabaseAhead`
（`pg.Migrate` 的防前瞻闸）。「库已前进 + 只回二进制」这条组合仍由
`--no-restore-db` 显式选择，不在演练里混测。

边界三：PG18 客户端（`pg_dump`/`pg_restore`/`psql`）从 `postgres:18`
镜像整包提取，带私有库目录跑在 `/opt/pg18`，包装器落 `/usr/local/bin`。
提取只换客户端来源，不改变 `update.sh` 的任何步骤。

边界四：R10 的恢复失败是注入的（只放行 `--list` 的 `pg_restore` 包装器），
不是真的磁盘满/转储损坏；被测性质是「恢复失败时 exit 2、打印 manual action、
不自动启动旧件」。

边界五：容器内 `deps.Containerized` 为真，U1 自检的 `tools.*` 与
`fs.backup_dir` 按设计 SKIP；R1 只要求它不是 FAIL。备份目录判权口径
（按调用者 root，而非服务用户）由 Go 单测
`TestBackupDirIsJudgedByCaller` 与 R1 的明文自检共同钉住。

边界六：v6 与 v4 迁移集相同、只多一个起手 panic：它不推进 schema，
所以 R9 只测 UI/二进制回滚，不混测数据库回滚。

### 8.5 CI 接线

`.github/workflows/deploy-gate.yml` 的 `update-rollback-e2e` job 两步：

1. `go test ./cmd/cli/ -run 'TestUpdateSh|TestInstallSh'`：update.sh 干跑契约
   （自检门、统一备份、start 失败回滚、首次装页面回滚、argv 无口令、缺值 exit 2）。
   不需要 docker，托管 runner 上必定真跑。
2. `bash scripts/e2e/update-rollback-e2e.sh host ...`：R1..R10 全矩阵。
   托管 runner 上 `--privileged` systemd 容器未实测：环境起不来时脚本 exit 2，
   job 打印 `SKIP` 并写进 step summary 后退出 0 —— **SKIP 不算绿**，
   release-check 项 7 仍记 POINTER，发布前按 §1 手跑一遍。

`tests/test_deploy_e2e_wiring.py` 钉住这条接线：job 存在、有超时门、
真的调用脚本、干跑契约在场、SKIP 路径写进 step summary。
