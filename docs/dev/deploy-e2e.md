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
```

## 2. 容器路径断言

脚本 `scripts/e2e/deploy-container-e2e.sh` 按 `deploy/docker/docker-compose.yml` 起栈。
独立 compose 项目、独立卷、独立网络、独立回环端口。

| 步骤 | 断言 |
|------|------|
| C1 | docker 可用；本项目无旧容器/卷/网络；端口空闲 |
| C2 | `docker compose build` 退出 0 |
| C3 | db 与 app 均 healthy |
| C4 | 容器内 `yiban-cli migrate` 打印嵌入集最新版本 |
| C4b | 签发→验签通过；错公钥必须被拒；篡改清单一字节后验签必须被拒（openssl 现场生成密钥，不入仓） |
| C5 | 宿主 `curl /healthz` 返回 200，body 为 `{"status":"ok"}` |
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
| B2 | 一次性 OS 镜像含真 systemd（PID1）与 openssl |
| B3 | 全新 PostgreSQL 18；public 表数为 0 |
| B4 | 用 `golang:1.27-alpine` 真编译 server 与 cli |
| B4b | 对真编译产物签发清单→验签通过 |
| B5 | OS 容器 systemd 就绪；`/etc/yiban`、`yiban` 用户、二进制均不存在 |
| B6 | `install.sh` 退出 0，输出 `install complete: healthz ok` |
| B7 | 单元 active + enabled；独立 `yiban-cli healthcheck` 通过 |
| B8 | `.env` 600 root:root；二进制 755；photos 属 yiban；名册逐字节一致；密钥形状正确 |
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
本仓 README 主体现描述旧 Python 栈。
本期在 README 顶部**新增一行指针**，指向本文件。
新栈 README 整体刷新已立 `t32`（接手已关闭的 `n4a-readme`），不在本期。
本期不改旧栈章节。

## 6. 等价性与已知边界（AGENTS §32）

裸机 e2e 的容器当 OS 用。它逐条覆盖被测性质：

- 真 systemd 作为 PID1；`systemctl` 真调用。
- 真 `useradd`、`chown`、`install` 与 POSIX 权限位。
- 真 PostgreSQL 18，经容器网络连接。
- 真二进制，由生产同款工具链编译。

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

## 8. 升级回滚演练（update-rollback-e2e，工单 9f6 的 E-2）

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
| H3 | PostgreSQL 18 就绪；应用角色 `yiban`（非超级用户、非库属主）有 public CREATE |
| H4 | 四组二进制（v1..v4）真编译产出，server sha256 两两不同 |
| H5 | 演练容器 systemd 就绪；标记已写；`pg_dump`/`psql`（PG18 客户端）容器内可用 |
| H6 | 容器内演练（R1..R4）退出码 0 |

容器内演练步骤（前缀 R）：

| 步骤 | 断言 |
|------|------|
| R1 | `install.sh` 装 v1 退出 0；unit active；独立 healthcheck 通过；schema 版本＝迁移目录最大编号 |
| R2（U3） | 对应用角色 `REVOKE CREATE ON SCHEMA public` 后跑 `update.sh`（新版含真迁移 00021）：退出码 1；日志含 `permission denied for schema public`（失败原因钉在 schema 权限）；安装位仍是 v1；无 `.bak`/`.new` 残留；unit active；healthz OK；schema 版本不前进 |
| R3（U4） | 权限归还后跑 `update.sh`（新版 `main()` 起手 panic，迁移集与 v1 相同）：退出码 1；安装位换回 v1；`.bak` 无残留；unit active；healthz OK |
| R4（U5） | 正常升级（新版含迁移 00021＋00022）：退出码 0；安装位＝v4；`.bak` 存在且＝v1（升级前旧件）；unit active；healthz OK；schema 版本＋2；`e2e_rollback_probe` 表存在 |

判据与 `update.sh` 头注一致：迁移失败回滚＝旧件继续跑（exit 1）；
健康检查失败回滚＝`.bak` 换回（exit 1）；正常升级＝`.bak` 留旧件（exit 0）。
schema 不随二进制回滚（无 down 迁移）由 U5 的表存在断言显式登记。

U3 的注入手法：迁移 SQL 嵌在二进制里改不了，演练在编译期给「新版」
源码副本加一条真迁移（00021，`CREATE TABLE`），升级前用 psql
`REVOKE CREATE ON SCHEMA public FROM yiban` 让真 goose 路径撞权限错误。
应用角色不是超级用户也不是库属主，REVOKE 才咬得住。

E-3 之后演练的三次 `update.sh` 全部显式带 `--allow-unsigned`
（默认拒未签名的守卫是 `server/cmd/cli/install_update_e2e_test.go`
的 `TestUpdateShDeniesUnsignedByDefault`）。

### 8.3 耗时与前置

- Docker 前置：`--privileged` 与 host cgroup（同裸机路径）；镜像
  `postgres:18`、`golang:1.27-alpine`、`ubuntu:24.04`（或已构建的
  `yiban-e2e-os:ubuntu24.04`）。
- 实测耗时（2026-10-31，本机 WSL2 Docker，24 核；口径＝脚本内
  `P0.log` 创建到 `exit-code` 写盘的墙钟）：
  OS 镜像与 Go 缓存卷全热时 88s（首测遍）与 77s（连跑第二遍）；
  冷首跑另含 OS 镜像构建（apt 装 systemd/openssl，实测 25s）与
  Go 模块冷缓存，约 2 分钟。数随环境变，引用请自带口径。
- 验收口径（工单 9f6）：同一脚本连跑两遍，rc 均 0。

### 8.4 等价性与已知边界（AGENTS §32）

逐条覆盖的被测性质：`update.sh` 的签名逃生口用法、文件替换与 `.bak`
回滚、真 `systemctl` 停起 unit、真 goose 迁移成败、真 `pg_dump` 迁移前
快照、`/healthz` 探活。

不等价的一维：容器没有真实内核启动与硬件；`network-online.target`
由 systemd 直接满足。该维不影响上述任一性质。

边界一：探活地址是容器回环 `127.0.0.1:47183`，探活由容器内已装
`yiban-cli` 执行（同裸机路径 §6）。

边界二：U4 故意让「新版」不带新迁移。schema 已前进时回滚件会撞
`ErrDatabaseAhead`（`pg.Migrate` 的防前瞻闸），那是另一条已登记边界，
不在本演练混测。

边界三：PG18 客户端（`pg_dump`/`psql`）从 `postgres:18` 镜像整包提取，
带私有库目录跑在 `/opt/pg18`，包装器落 `/usr/local/bin`。提取只换
客户端来源，不改变 `update.sh` 的任何步骤。
