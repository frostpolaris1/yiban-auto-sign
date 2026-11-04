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

### 4.3 升级

```bash
sudo bash deploy/linux/update.sh \
  --server-bin dist/yiban-server \
  --cli-bin dist/yiban-cli \
  --ui-dir ui/dist \
  --manifest release/manifest.json \
  --signature release/manifest.sig \
  --pubkey release-pubkey.pem
```

- 签名是默认要求。未签名升级必须显式给 `--allow-unsigned`，脚本会打印 WARN。
- 页面在二进制就位后激活，不会先于新 API 生效。
- 迁移或健康失败时二进制与页面一起回滚；旧页面留在 `/var/lib/yiban/ui.bak`。
- 漏给 `--ui-dir` 时页面停在旧版本，脚本打印 WARN。

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
