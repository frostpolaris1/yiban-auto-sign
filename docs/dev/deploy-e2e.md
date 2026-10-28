# 部署 e2e（干净环境走通）

> 工单 6jk。判据来源：AGENTS.md §六 第 16/17 条。
> 可发布＝全新干净环境按部署件从零装成。
> 本文给出两条 e2e 的命令、断言与 CI 状态。

## 1. 两条命令

容器路径（自动轨）：

```bash
bash scripts/e2e/deploy-container-e2e.sh
```

裸机路径（发布前手跑项）：

```bash
bash scripts/e2e/deploy-baremetal-e2e.sh
```

两条都在仓库根目录执行。两条都在 WSL 或 Linux 上跑。
两条都只写 `work/` 与 docker 资源，不碰生产路径。

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

裸机不进自动轨的理由：它要求 docker 的 `--privileged` 与 host cgroup。
该形态尚未在 GitHub 托管 runner 上实测。
加入自动轨前，须先在托管 runner 上跑绿一次。
同一脚本可直接作为 CI 步骤使用，无需改动。

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
