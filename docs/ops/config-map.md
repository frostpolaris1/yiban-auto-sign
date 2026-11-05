# 配置地图（运维一页）

> 适用：Go 栈（`server/` + `ui/`）。目的：运营者要知道「改什么去哪个文件、
> 改完怎么生效」，不用读源码。
> 键面唯一事实源是 `server/internal/configdata/registry.json`（编译期嵌进二进制）；
> 本页只讲文件位置与生效方式，不复制每个键的语义。

## 1. 运行文件（只有两份）

| 形态 | 运行文件 | 权限 | 谁读它 |
|---|---|---|---|
| 裸机（systemd） | `/etc/yiban/server.env` | `0600 root:root` | `yiban.service` 的 `EnvironmentFile` |
| 容器（compose） | `deploy/container/compose.env` | `0600`，已 gitignore | `docker compose --env-file`（插值）＋ `env_file`（注入 app） |
| 开发（本机） | `server/.env` | 本地文件 | `set -a; . server/.env; set +a` 后由进程环境读取 |

- Go 栈**不读**仓库根 `.env`。那是旧 Python 栈的配置；容器也不自动加载它
  （Compose 的隐式 `.env` 被显式 `--env-file` 取代）。
- 容器形态没有第二份运行配置：`POSTGRES_PASSWORD` 与所有 `YIBAN_*` 都在
  `compose.env` 这一份里；`YIBAN_DATABASE_URL` 由 compose 从
  `POSTGRES_PASSWORD` 派生，不手写第二遍。
- 裸机形态写入 `/etc/yiban/server.env` 的只有安装器与运维；模板落在
  `/etc/yiban/server.env.example`（0644，无密钥），供照键面补配置。

## 2. 模板与它们的去向

| 模板（入库） | 复制到 | 用途 |
|---|---|---|
| `deploy/baremetal/server.env.example` | `/etc/yiban/server.env`（安装器自动落） | 裸机运行配置；也装到 `/etc/yiban/server.env.example` |
| `deploy/container/compose.env.example` | `deploy/container/compose.env`（手工 `cp` + `chmod 600`） | 容器运行配置＋编排层口令 |
| `server/.env.example` | `server/.env`（开发用） | 本机开发 |
| `.env.example`（仓库根） | 不复制 | **旧 Python 栈专用**；Go 栈不读 |

三份新栈模板的键面由 `server/internal/adapters/config/registry_parity_test.go`
钉住：模板里的键必须与名册一致，秘密键必须为空。

## 3. 我要改什么，去哪改

| 目标 | 改哪里 | 生效方式 |
|---|---|---|
| 服务端行为（`YIBAN_*`） | 裸机 `/etc/yiban/server.env`；容器 `deploy/container/compose.env` | 裸机 `sudo systemctl restart yiban.service`；容器 `docker compose --env-file deploy/container/compose.env -f deploy/container/docker-compose.yml up -d`（重建容器才重读 `env_file`，只 `restart` 不会） |
| 数据库口令 | 容器 `compose.env` 的 `POSTGRES_PASSWORD` | 只影响首次初始化。已有库要改角色口令：先 `ALTER ROLE yiban PASSWORD '…'`，再改 `compose.env`，最后 `up -d` 重建 app（DSN 由它派生） |
| 不可信代理 / 反代地址 | `YIBAN_TRUSTED_PROXIES` | 裸机写 `server.env` 后重启；容器可写 `compose.env`，也可 `export` 后 `up -d`（shell 环境 > `--env-file` > compose 缺省） |
| 照片目录 | `YIBAN_PHOTO_DIR` | 裸机改 `server.env` 后**重跑 `install.sh`**（按新值重写单元的 `ReadWritePaths`）再重启；容器改 `compose.env` 后 `up -d`，并确认卷挂载点同步 |
| 备份落点 | 容器 `YIBAN_BACKUP_HOST_DIR`（宿主绝对路径，缺省 `./backups`） | 下一次 `--profile backup run` 生效；不在 Go 名册 |
| 前端产物 | 裸机 `YIBAN_UI_DIR`（安装器写；`--ui-dir` 升级会一并换页面） | 重跑 `install.sh` 或 `update.sh --ui-dir`；手改后重启 |
| systemd 隔离面 | 不手改单元；改 `install.sh` 的生成模板并重装 | `sudo bash deploy/baremetal/install.sh …` 重写单元 + `systemctl daemon-reload` |
| 版本 / 构建参数 | 仓库根 `VERSION`、`BUILD_VERSION/BUILD_COMMIT/BUILD_TIME` | **构建期**，不是运行配置；改后重新构建发布件 |

## 4. 秘密与非秘密

**秘密（只进运行文件，0600；不进仓库/argv/日志/镜像）**

- `YIBAN_DATABASE_URL`（含口令）
- `YIBAN_HASH_PEPPER`（摘要与审计链；轮换会让审计链断）
- `YIBAN_CREDENTIAL_KEY`（32 字节；轮换会让既有凭据密文无法解密）
- `YIBAN_MAIL_PASS`（开启重置邮件时）
- `POSTGRES_PASSWORD`（容器编排层；db 初始化与 DSN 派生）

**非秘密（可写模板/文档）**

- 监听地址、时区、并发/节流/窗口、`YIBAN_TRUSTED_PROXIES`、
  `YIBAN_UI_DIR`、`YIBAN_PHOTO_DIR`、SMTP 主机/端口/模式、`YIBAN_BACKUP_HOST_DIR`。

**操作约束**

- 生成：`openssl rand -hex 32`（pepper）、`openssl rand -base64 32`
  （credential key）、`openssl rand -hex 16`（数据库口令）。
- 模板里秘密键一律留空；真实值只出现在运行文件。
- 备份（裸机 `update.sh`、容器 `backup` profile）含凭据密文：目录 0700、
  文件 0600，按秘密级保管。容器备份需要静态加密时在宿主做 GPG，
  口令不放进 `compose.env`。
- production 缺 DSN 直接启动失败；`YIBAN_ALLOW_NO_DB=1` 会被拒绝。

## 5. 校验与查看

```bash
# 已装 CLI 自检（裸机；只读，不改任何文件）
sudo bash deploy/baremetal/update.sh --selfcheck-only   # 0 全 PASS/SKIP；2 有 FAIL

# 容器：确认渲染出的运行值（不打印口令）
docker compose --env-file deploy/container/compose.env \
  -f deploy/container/docker-compose.yml config

# 生效中的键面（管理接口，需 master）
curl -fsS https://<域名>/api/admin/settings
```

名册、默认值与每键说明在 `server/internal/configdata/registry.json`；
部署步骤与排障见 `deploy/README.md`。
