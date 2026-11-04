# 部署（Go + React 线）

> 适用对象：本仓的**新架构**（`server/` + `ui/`）。
> 旧 Python 栈的部署说明在根 `README.md`，两者不要混用。
> 部署 e2e 的命令与断言见 [`docs/dev/deploy-e2e.md`](../docs/dev/deploy-e2e.md)。

## 1. 产物形态

一个 Go 进程同时提供两件事：

- `/api/*`：JSON 接口。
- 其余路径：前端页面。未命中的无扩展名路径回 `index.html`，由前端路由接管深链。

外部依赖只有 PostgreSQL 18。页面文件由 `YIBAN_UI_DIR` 指定；不设该键时进程只提供 API。

TLS 由进程外的反向代理终结。Go 进程不监听公网端口。
Go 进程不做 gzip/brotli 压缩；压缩交给外层反向代理（它已在链路上）。

## 2. 准备

三件必须准备好：

1. PostgreSQL 18，以及一个可连的 DSN。
2. 两个随机的服务端密钥：
   ```bash
   openssl rand -hex 32      # YIBAN_HASH_PEPPER
   openssl rand -base64 32   # YIBAN_CREDENTIAL_KEY
   ```
3. 反向代理与证书。对外必须是 HTTPS：`production` 环境拒绝不安全 cookie。

## 3. 容器部署（推荐）

在仓库根执行：

```bash
export YIBAN_HASH_PEPPER=$(openssl rand -hex 32)
export YIBAN_CREDENTIAL_KEY=$(openssl rand -base64 32)
export YIBAN_PG_PASSWORD=$(openssl rand -hex 16)

docker compose -f deploy/docker/docker-compose.yml up -d --build
```

镜像包含页面：前端在构建段执行 `npm ci && npm run build`，产物落在 `/srv/ui`。

验证：

```bash
curl -fsS http://127.0.0.1:8080/healthz     # {"status":"ok"}
curl -fsS http://127.0.0.1:8080/ | head     # 前端首页 HTML
```

创建第一个 master：

```bash
docker compose -f deploy/docker/docker-compose.yml exec -T app \
  /usr/local/bin/yiban-cli user create-master \
  --email you@example.com --password-stdin <<< "$PASSWORD"
```

数据卷有两个：`pgdata`（数据库）与 `photos`（签到照片）。备份要同时覆盖两者。

## 4. 裸机部署（systemd）

前提：宿主机已装 PostgreSQL 18，并能用 DSN 连上。

### 4.1 构建

```bash
# 后端。版本身份由 ldflags 注入，发布构建必须给三个值；
# 不注入时 --version 显性打印 dev/unknown（不编造 sha 或时间）。
cd server
BUILD_VERSION=0.7.0
BUILD_COMMIT=$(git rev-parse --short HEAD)
BUILD_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)
LDFLAGS="-s -w \
  -X github.com/frostpolaris1/yiban-auto-sign/server/internal/buildinfo.version=$BUILD_VERSION \
  -X github.com/frostpolaris1/yiban-auto-sign/server/internal/buildinfo.commit=$BUILD_COMMIT \
  -X github.com/frostpolaris1/yiban-auto-sign/server/internal/buildinfo.buildTime=$BUILD_TIME"
CGO_ENABLED=0 go build -trimpath -ldflags="$LDFLAGS" -o ../dist/yiban-server ./cmd/server
CGO_ENABLED=0 go build -trimpath -ldflags="$LDFLAGS" -o ../dist/yiban-cli ./cmd/cli
cd ..

# 前端
cd ui
npm ci
npm run build      # 产物：ui/dist
cd ..
```

### 4.2 安装

```bash
sudo bash deploy/linux/install.sh \
  --server-bin dist/yiban-server \
  --cli-bin dist/yiban-cli \
  --ui-dir ui/dist \
  --database-url 'postgres://yiban:<password>@127.0.0.1:5432/yiban?sslmode=disable'
```

口令含 `@`、`:`、`/` 等字符时必须百分号编码，否则 DSN 解析失败。

安装器完成这些事：

- 建 `yiban` 服务用户与 `/var/lib/yiban`。
- 二进制落 `/usr/local/bin`。
- 页面落 `/var/lib/yiban/ui`，并在 `/etc/yiban/.env` 写 `YIBAN_UI_DIR`。
- 生成密钥并写 `/etc/yiban/.env`（0600 root:root），只打印一次。
- 装 systemd 单元，`enable --now`，再探测 `/healthz`。

不给 `--ui-dir` 时只装 API。安装器会打印显性提示。

创建第一个 master：

```bash
set -a; . /etc/yiban/.env; set +a
printf '%s\n' "$PASSWORD" | yiban-cli user create-master \
  --email you@example.com --password-stdin
```

### 4.3 升级（一键更新 U2）

```bash
sudo bash deploy/linux/update.sh \
  --server-bin dist/yiban-server \
  --cli-bin dist/yiban-cli \
  --ui-dir ui/dist \
  --manifest release/manifest.json \
  --signature release/manifest.sig \
  --pubkey release-pubkey.pem \
  --expect-version 0.7.0
```

一条命令走完：**自检 → 备份（数据库＋照片）→ 停服 → 迁移 → 换件 → 健康检查 →
失败回滚**。脚本不做静默自动应用：它是命令，不是守护进程。

- 签名是默认要求。未签名升级必须显式给 `--allow-unsigned`，脚本会打印 WARN。
- 页面在二进制就位后激活，不会先于新 API 生效；回滚时页面一起换回，
  旧页面留在 `/var/lib/yiban/ui.bak`。
- 漏给 `--ui-dir` 时页面停在旧版本，脚本打印 WARN。

**自检门（停服前，fail-closed）**

用**已安装**的 `yiban-cli selfcheck --json` 跑 U1 判据（配置、数据库 schema、
工具、目录权限、密钥指纹、服务状态）。任何 FAIL 即 `exit 2`，不停服、不写安装位。
已安装 CLI 太旧（不含 `selfcheck`）同样拒绝：自检不可用不等于绿。

只跑自检、不做任何修改（不取锁、不写文件）：

```bash
sudo bash deploy/linux/update.sh --selfcheck-only    # 0 全 PASS/SKIP；2 有 FAIL
```

**统一备份（停服前，失败即中止）**

落在 `/var/lib/yiban/updates/backups/<UTC时间戳>/`（目录 0700、文件 0600）：

| 文件 | 内容 |
|---|---|
| `db.dump` | `pg_dump -Fc`（含凭据密文与审计行） |
| `db.list` | `pg_restore --list` 的清单（证明转储可读） |
| `db-schema.txt` | 迁移前 goose 版本与 public 基表数（恢复后的对账基线） |
| `photos.tar` | 照片目录快照（`tar -h`；照片不进数据库备份） |
| `bin/yiban-server`、`bin/yiban-cli` | 更新前的二进制（含既有 `.bak` 的归档） |
| `SHA256SUMS` | 以上文件的 sha256，写入后立即校验 |

给了口令（`--backup-passphrase-file` 或环境 `YIBAN_BACKUP_PASSPHRASE`）时，
另外跑 `yiban-cli backup` 产出 `db.sql.gpg`（GPG 对称加密，含行数清单）。
没有口令时只留未加密的 `-Fc` 转储，脚本打印 WARN（跳过 ≠ 绿）。

保留最近 `--keep-backups N` 份（缺省 5；`0` = 不清理）。只删本目录下形如
`20261104T101500Z` 的备份目录；备份失败不删任何东西，半成品目录直接清掉。

**状态与日志**

| 路径 | 内容 |
|---|---|
| `/var/lib/yiban/updates/update-state.json` | 最近一次更新：`result`（running/success/failed/aborted/interrupted）、`exit_code`、`phase`、版本、备份路径、`db_schema_before/after`、`db_restore` |
| `/var/lib/yiban/updates/update.log` | 追加日志（时间／阶段／结果），一行一事件 |
| `/var/lib/yiban/updates/selfcheck-latest.json` | 停服前自检的完整 JSON 报告 |

`/var/lib/yiban/update-state.json`（注意不是 `updates/` 下那份）是 U1 的**密钥指纹**
状态，只由 `yiban-cli selfcheck --record-keys` 写；更新成功时脚本会自动调用它。
两份文件都不含 DSN 口令、密钥原文与 GPG 口令。

**失败回滚**

| 失败点 | 行为 | 退出码 |
|---|---|---|
| 签名/对账/版本校验 | 不碰旧件、不停服 | 1 |
| 自检 FAIL / 无 CREATEDB / 备份失败 | 不开始（不停服） | 2 |
| 迁移失败 | 旧二进制继续服务；库没变就不动库 | 1 |
| 迁移后（换件/健康）失败 | 二进制与 UI 换回旧件；**数据库默认恢复到升级前** | 1 |
| 数据库恢复失败 | 停住（不启动旧件），打印 manual action | 2 |
| 回滚后仍不健康 | 打印 manual action | 2 |

数据库回滚（默认开启，`--no-restore-db` 关闭）：

1. 只在**停服后**、且本次 `db.dump` 通过 `pg_restore --list` 校验时执行；
2. 迁移前后 goose 版本相同（库没变）就跳过——不为了回滚而回滚；
3. 迁移改变了库时：`CREATE DATABASE yiban_restore_<UTC时间戳>` →
   `pg_restore` 到新库 → 校验新 DSN 的 `current_database()` 与新库同名（绝不写回原库）
   → 校验 goose 版本与基表数和备份基线一致 → 原子改写 `/etc/yiban/.env` 的
   `YIBAN_DATABASE_URL` → 起旧二进制；
4. **原库原样保留**（迁移过的失败库）供取证；不删、不 `--clean`、不原地覆盖。

前提：连接角色必须有 `CREATEDB`。没有且未给 `--no-restore-db` 时预检直接
`exit 2`，不允许在「失败后回不了库」的状态下开始。
已知代价：健康探测窗口内新二进制写入的行不会保留（恢复到备份时刻）；旧库仍在。

**回滚点与并发**

- 二进制回滚点始终是 `yiban-server.bak`（与旧版相同）；本次升级开始前若已有
  `.bak`，先原样归档进备份目录 `bin/yiban-server.rollback-point`，不覆盖。
- 升级成功后 `.bak` 保留旧件，供人工回退；`--no-restore-db` 时尤其重要。
- `flock` 保证同一时刻只有一个更新；第二个进程 `exit 2`。

**退出码**

| 码 | 含义 |
|---|---|
| 0 | 成功（含 `--selfcheck-only` 全 PASS/SKIP） |
| 1 | 升级失败但已回滚到旧件（旧件在跑且健康）；验签/对账失败 |
| 2 | 用法错误、并发锁、自检 FAIL、预检/备份失败、数据库恢复失败、回滚后仍不健康 |

**runbook：看状态与人工恢复**

```bash
# 1) 看最近一次更新做了什么
sudo cat /var/lib/yiban/updates/update-state.json
sudo tail -n 50 /var/lib/yiban/updates/update.log

# 2) 看停服前自检的完整报告（FAIL 项在 checks[] 里，status=FAIL）
sudo python3 -m json.tool /var/lib/yiban/updates/selfcheck-latest.json | less

# 3) 看本次备份与校验
sudo ls -l /var/lib/yiban/updates/backups/
sudo sh -c 'cd /var/lib/yiban/updates/backups/<ts> && sha256sum -c SHA256SUMS'

# 4) 数据库恢复失败（exit 2）时，按打印的 manual action 操作，等价命令：
sudo -u postgres createdb yiban_restore_manual
sudo -u postgres pg_restore --no-owner --no-privileges \
  -d yiban_restore_manual /var/lib/yiban/updates/backups/<ts>/db.dump
sudo sed -i 's|^YIBAN_DATABASE_URL=.*|YIBAN_DATABASE_URL=postgres://…/yiban_restore_manual|' /etc/yiban/.env
sudo systemctl start yiban.service

# 5) 确认旧件与新 schema 兼容、选择不回库时：
sudo systemctl start yiban.service
```

人工恢复后重跑 `sudo bash deploy/linux/update.sh --selfcheck-only` 确认全 PASS，
再重新执行升级。

## 5. 反向代理

浏览器只访问代理，代理转给 Go 进程。代理必须做三件事：

1. 终结 TLS。
2. 传 `X-Forwarded-Proto: https`。
3. 传 `X-Forwarded-For`。

服务端只采信 `YIBAN_TRUSTED_PROXIES` 里来源的转发头。不配时：

- 所有请求按代理地址计，登录等受限端点共用同一个 IP 桶（默认 60 次/分钟）。
- 外部 TLS 的登录来源判定看到 `http`：带同源 Fetch Metadata 的现代浏览器走 401，旧浏览器与非浏览器客户端回 403。

容器部署的 compose 默认值已覆盖 Docker 网桥与回环：

```text
YIBAN_TRUSTED_PROXIES=172.16.0.0/12,127.0.0.1
```

原因：发布端口经网桥网关转发，容器看到的来源是网关，不是 `127.0.0.1`（2026-11-01 实测：`127.0.0.1:8080` 发布 → 容器内 `RemoteAddr=172.18.0.1`）。
外层代理在别的地址时覆盖它：

```bash
export YIBAN_TRUSTED_PROXIES=192.0.2.10
docker compose -f deploy/docker/docker-compose.yml up -d
```

裸机部署的代理在宿主回环。`install.sh` 新装的 `.env` 已写入：

```text
YIBAN_TRUSTED_PROXIES=127.0.0.1
```

已有 `.env` 手工补一行后重启：

```bash
sudo sh -c "printf 'YIBAN_TRUSTED_PROXIES=127.0.0.1\n' >> /etc/yiban/.env"
sudo systemctl restart yiban.service
```

判据：带 `Origin: https://<你的域名>` 与 `X-Forwarded-Proto: https` 的登录请求不得回 403。

## 6. 关键配置

| 键 | 作用 | 缺省 |
|---|---|---|
| `YIBAN_DATABASE_URL` | PostgreSQL DSN | 空：业务端点回 501 |
| `YIBAN_HASH_PEPPER` | 摘要与审计链密钥 | 必填 |
| `YIBAN_CREDENTIAL_KEY` | 凭据加密钥（32 字节） | 必填 |
| `YIBAN_HTTP_ADDR` | 监听地址 | `127.0.0.1:47183` |
| `YIBAN_UI_DIR` | 前端产物目录（含 `index.html`） | 空：只提供 API |
| `YIBAN_TRUSTED_PROXIES` | 可信代理 IP/CIDR，逗号分隔 | 空：不信任转发头（compose 默认 `172.16.0.0/12,127.0.0.1`） |
| `YIBAN_PHOTO_DIR` | 照片目录 | `data/photos` |

完整名册与每键说明见 `config/registry.json`。

## 7. 排障

| 现象 | 先查 |
|---|---|
| 首页 404，`/healthz` 正常 | `YIBAN_UI_DIR` 是否设置；目录里有没有 `index.html`；进程启动日志有没有 `router: YIBAN_UI_DIR` 报错 |
| 登录回 403 | 代理是否传 `X-Forwarded-Proto`；`YIBAN_TRUSTED_PROXIES` 是否包含代理地址 |
| 登录成功但立刻掉线 | 是否用 HTTP 访问；`production` 的 cookie 带 `Secure` |
| 进程起不来 | `YIBAN_UI_DIR` 指向的目录缺 `index.html` 时服务端**拒绝启动**。这是有意行为 |

## 8. 部署判据

可发布＝全新干净环境按本文从零装成，一次完成。
自动检查：

```bash
bash scripts/e2e/deploy-container-e2e.sh   # 容器路径，CI 自动轨
bash scripts/e2e/deploy-baremetal-e2e.sh   # 裸机路径，发布前手跑
```
