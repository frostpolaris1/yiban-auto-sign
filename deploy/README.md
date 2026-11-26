# 部署（Go + React 线）

> 适用对象：本仓的**新架构**（`server/` + `ui/`）。
> 旧 Python 栈的部署说明在根 `README.md`，两者不要混用。
> 部署 e2e 的命令与断言见 [`docs/dev/deploy-e2e.md`](../docs/dev/deploy-e2e.md)。
> 「改什么去哪、秘密与非秘密、怎么生效」的一页地图见
> [`docs/ops/config-map.md`](../docs/ops/config-map.md)。

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
cp deploy/container/compose.env.example deploy/container/compose.env
chmod 600 deploy/container/compose.env
# 编辑 compose.env：填 POSTGRES_PASSWORD / YIBAN_HASH_PEPPER / YIBAN_CREDENTIAL_KEY
openssl rand -hex 32      # YIBAN_HASH_PEPPER
openssl rand -base64 32   # YIBAN_CREDENTIAL_KEY
openssl rand -hex 16      # POSTGRES_PASSWORD

docker compose --env-file deploy/container/compose.env \
  -f deploy/container/docker-compose.yml up -d --build
```

镜像里的二进制版本身份来自三个构建参数（`BUILD_VERSION/BUILD_COMMIT/BUILD_TIME`）。
正式构建入口是 `deploy/container/build.sh`：它从仓库根 `VERSION` 读版本，三个值
一次给齐后调 `docker build`：

```bash
bash deploy/container/build.sh "yiban-server:v$(cat VERSION)"
```

不走该入口时（例如上面 `compose up -d --build`），compose 的 `build.args`
从环境取这三个变量，不导出即落 Dockerfile 的显性缺省 `dev/unknown`。
三个值是构建期参数，不进运行时配置名册。构建后验自报版本见 §4.1。

运行配置只来自 `deploy/container/compose.env`（0600 宿主）：
compose 用 `env_file` 把它注入 app，`--env-file` 供 db 的服务口令插值。
**不读取仓库根 `.env`**——旧 Python 栈的配置因此不可能混进 Go 容器。
`YIBAN_DATABASE_URL` 由 compose 从 `POSTGRES_PASSWORD` 派生（口令只有一处来源）。
这三个键只放一份：模板 `deploy/container/compose.env.example` → 运行文件
`deploy/container/compose.env`（0600，已 gitignore）；没有第二份容器配置。

compose 文件顶部固定了项目名 `name: yiban`：首次部署的卷名是
`yiban_pgdata`、`yiban_photos`，与仓库目录名无关；e2e 的 `-p` 仍然优先。
**没有存量迁移**：新线从未部署。只有更早的实验配置（未固定项目名）起过栈时，
旧卷前缀才不同；那种情况按旧项目名手工迁移卷，正常安装不需要。

配置名册随二进制嵌入（`server/internal/configdata/registry.json`），
镜像与安装位里都没有 `registry.json` 文件；`GET /api/admin/settings`
读的就是编译进二进制的那一份。

镜像包含页面：前端在构建段执行 `npm ci && npm run build`，产物落在 `/srv/ui`。

验证：

```bash
curl -fsS http://127.0.0.1:8080/healthz     # {"status":"ok"}
curl -fsS http://127.0.0.1:8080/ | head     # 前端首页 HTML
```

创建第一个 master（同样要显式 `--env-file`：compose 文件里有
`${POSTGRES_PASSWORD:?}` 插值，缺它连 `exec` 都解析不了）：

```bash
docker compose --env-file deploy/container/compose.env \
  -f deploy/container/docker-compose.yml exec -T app \
  /usr/local/bin/yiban-cli user create-master \
  --email you@example.com --password-stdin <<< "$PASSWORD"
```

### 3.1 容器备份（数据库＋照片）

数据卷有两个：`pgdata`（数据库）与 `photos`（签到照片）。
备份是同一条一次性任务，两个都覆盖——只备数据库会丢照片，只备卷会在
数据库写入中途拿到不一致的副本。

```bash
# 在仓库根执行；产物落 deploy/container/backups/<UTC 时间戳>/
docker compose --env-file deploy/container/compose.env \
  -f deploy/container/docker-compose.yml --profile backup run --rm backup
```

任务用 `postgres:18` 镜像（客户端与 db 同版本），产物：

| 文件 | 内容 |
|---|---|
| `db.dump` | `pg_dump -Fc`（含凭据密文与审计行） |
| `db.list` | `pg_restore --list` 清单（证明转储可读；读不了等于没备份） |
| `photos.tar.gz` | 照片卷快照（照片不进数据库备份） |
| `SHA256SUMS` | 以上文件的 sha256；任务内写入后立即 `-c` 校验 |

目录与文件按 `umask 077` 落盘（目录 0700、文件 0600）：备份含凭据密文。
默认落点 `deploy/container/backups`（已 gitignore）；换路径用
`YIBAN_BACKUP_HOST_DIR=/srv/backups`（宿主绝对路径），命令里其余部分不变。

**调度。** 任务不常驻，用宿主 cron 或 systemd timer 定时跑。cron 例：

```cron
15 3 * * * cd /path/to/yiban-auto-sign && /usr/bin/docker compose \
  --env-file deploy/container/compose.env \
  -f deploy/container/docker-compose.yml --profile backup run --rm backup \
  >> /var/log/yiban-backup.log 2>&1
```

systemd timer 等价：`OnCalendar=*-*-* 03:15:00`，service 的 `ExecStart` 就是上面
那条 `docker compose … run --rm backup`。两种方式都只管“拉起任务”，不复制
compose.env 里的秘密（任务自己从同一个 `--env-file` 取）。

**恢复。**

```bash
# 1) 停 app，避免恢复期间还有写入
docker compose --env-file deploy/container/compose.env \
  -f deploy/container/docker-compose.yml stop app

# 2) 数据库：pg_restore 从 stdin 读宿主上的 db.dump（--clean 覆盖同名对象）
docker compose --env-file deploy/container/compose.env \
  -f deploy/container/docker-compose.yml exec -T db \
  pg_restore -U yiban -d yiban --clean --if-exists --no-owner --no-privileges \
  < deploy/container/backups/<时间戳>/db.dump

# 3) 照片：用同一个一次性任务解包到 photos 卷
docker compose --env-file deploy/container/compose.env \
  -f deploy/container/docker-compose.yml --profile backup run --rm \
  --entrypoint sh backup -c 'tar -xzf /backups/<时间戳>/photos.tar.gz -C /photos'

# 4) 起 app 并验活
docker compose --env-file deploy/container/compose.env \
  -f deploy/container/docker-compose.yml start app
curl -fsS http://127.0.0.1:8080/healthz
```

恢复前先把 `<时间戳>` 换成备份目录名，并确认 `sha256sum -c SHA256SUMS` 通过。
需要静态加密时，在宿主对备份目录执行
`gpg --symmetric --cipher-algo AES256 db.dump`，把口令存到独立位置；
容器任务本身不加密（GPG 口令不应进 compose.env 或容器环境）。

保留策略由宿主负责：按需删除旧时间戳目录。不要删除仍被
`update.sh`/人工回退引用的备份。

## 4. 裸机部署（systemd）

前提：宿主机已装 PostgreSQL 18，并能用 DSN 连上。

### 4.1 构建

```bash
# 后端。版本身份由 server/scripts/build-release.sh 注入：它从仓库根
# VERSION（Go 线产品版本单一来源）读版本，连同 commit/build_time 用
# ldflags -X 写进两个二进制，并打印 version/commit/build_time 一行汇总。
# 缺 VERSION / 非 X.Y.Z / 非 git 检出时 exit 2，不产发布件。
bash server/scripts/build-release.sh dist
# Linux 产物：dist/yiban-server 与 dist/yiban-cli（Windows 宿主加 .exe 后缀）。

# 前端
cd ui
npm ci
npm run build      # 产物：ui/dist
cd ..
```

装好后验一下自报版本与 `VERSION` 一致：`dist/yiban-server --version`
（CLI 是 `dist/yiban-cli version`）。发布 tag 用 `v$VERSION`；openapi
`info.version` 是契约版本，不随产品版本走（`server/contracts/README.md`）。

### 4.2 安装

```bash
sudo bash deploy/baremetal/install.sh \
  --server-bin dist/yiban-server \
  --cli-bin dist/yiban-cli \
  --ui-dir ui/dist \
  --database-url 'postgres://yiban:<password>@127.0.0.1:5432/yiban?sslmode=disable'
```

口令含 `@`、`:`、`/` 等字符时必须百分号编码，否则 DSN 解析失败。

安装器完成这些事：

- 建 `yiban` 服务用户与 `/var/lib/yiban`。
- 二进制落 `/usr/local/bin`。
- 页面落 `/var/lib/yiban/ui`（重装时旧页面留 `ui.bak`），并在 `/etc/yiban/server.env` 写 `YIBAN_UI_DIR`。
- 生成密钥并写 `/etc/yiban/server.env`（0600 root:root）；只打印写入位置与
  指纹（sha256 前 16 位），不打印值。
- 同时落一份无密钥模板 `/etc/yiban/server.env.example`（0644）。
- 装 systemd 单元，`enable`＋`restart`（重装也换进程），再探测 `/healthz`。

不给 `--ui-dir` 时只装 API。安装器会打印显性提示。

**systemd 加固。** 安装器生成的 `yiban.service` 在 `User=`/`Group=` 之外带：

| 指令 | 作用 |
|---|---|
| `NoNewPrivileges=true` | 进程及其子进程不得提权 |
| `PrivateTmp=true` | 私有 `/tmp`、`/var/tmp`，不与其他服务共享 |
| `UMask=0077` | 新建文件默认 0600（照片仍由同一进程读取） |
| `ProtectSystem=strict` | 除 `/dev`、`/proc`、`/sys` 外整个文件系统只读 |
| `ProtectHome=true` | `/home`、`/root`、`/run/user` 不可见 |
| `ReadWritePaths=-/var/lib/yiban/photos` | 只开照片目录可写 |

`ReadWritePaths` 取 `server.env` 的 `YIBAN_PHOTO_DIR`（缺省
`/var/lib/yiban/photos`）。安装器会先建好该目录并把它交给服务用户
（`ProtectSystem=strict` 下进程自己建不了目录）。判据是**白名单**：
路径先用 `realpath -m` 归一化（`..` 与软链一起解析），归一化结果必须是
`/var/lib/yiban/` 下的真实子目录；`/var/lib/yiban` 本身、
`/var/lib/yiban-updater` 与其它裸目录一律拒绝。路径本身是软链也拒绝，
`chown` 前再用 `realpath -e` 复核，且只做非递归 `chown -h`。
拒绝发生在任何写入或 `chown` 之前（exit 2）。改照片目录要同时改
`server.env` 并重跑 `install.sh`（它会按新值重写 `ReadWritePaths` 并
`restart` 服务），不需要手写 `systemctl restart`。

升级、回滚、自检与备份都在服务进程之外运行（`update.sh` 以 root 停服、
`pg_dump`、tar 照片、换件、再起服），加固不改变这些路径；`update.sh` 不改写
systemd 单元——已由旧安装器装过的机器要获得加固，重跑一次 `install.sh`。
真 systemd 容器里的 `systemd-analyze verify` 与 `systemctl show` 断言见
`scripts/e2e/deploy-baremetal-e2e.sh` 的 B6b。

创建第一个 master：

```bash
set -a; . /etc/yiban/server.env; set +a
printf '%s\n' "$PASSWORD" | yiban-cli user create-master \
  --email you@example.com --password-stdin
```

### 4.3 升级（一键更新 U2）

```bash
sudo bash deploy/baremetal/update.sh \
  --server-bin dist/yiban-server \
  --cli-bin dist/yiban-cli \
  --ui-dir ui/dist \
  --manifest release/manifest.json \
  --signature release/manifest.sig \
  --pubkey release-pubkey.pem \
  --expect-version 0.7.5
```

一条命令走完：**自检 → 备份（数据库＋照片）→ 停服 → 迁移 → 换件 → 健康检查 →
失败回滚**。脚本不做静默自动应用：它是命令，不是守护进程。

- 大表迁移（如 `00032` 的并发建索引）优先在停服窗口跑 `yiban-cli migrate`
  （5 分钟预算）或本脚本，不要靠重启服务兜底：启动迁移预算 10 分钟，
  被 systemd 取消的 CIC 可能留下 INVALID 索引，处理见 §7.1。
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
sudo bash deploy/baremetal/update.sh --selfcheck-only    # 0 全 PASS/SKIP；2 有 FAIL
```

**统一备份（停服前，失败即中止）**

落在 `/var/lib/yiban-updater/backups/<UTC时间戳>/`（目录 0700、文件 0600；
`/var/lib/yiban-updater/state/` 只放状态与日志）。**备份根与状态目录都是
root:root 0700**：备份含凭据密文，回滚还会执行备份内的 CLI，因此服务用户
不得能写（U1 自检的 `fs.backup_dir` 也按调用者 root 判权）；每份子目录
0700、文件 0600。

| 文件 | 内容 |
|---|---|
| `db.dump` | `pg_dump -Fc`（含凭据密文与审计行） |
| `db.list` | `pg_restore --list` 的清单（证明转储可读） |
| `db-schema.txt` | 迁移前库名、goose 版本与 public 基表数（恢复后的对账基线） |
| `env.backup` | `/etc/yiban/server.env` 快照（0600；库回滚会改写 DSN，人工回退要用） |
| `photos.tar` | 照片目录快照（照片不进数据库备份）。目录路径先 `readlink -f` 解析（含尾斜杠/软链挂载）；解析结果若是 `/`、系统目录、更新器根或状态/备份根则拒绝备份；**目录内部的软链只存链接本身，不跟随**——避免把软链指向的 root-only 文件读进备份 |
| `bin/yiban-server`、`bin/yiban-cli` | 更新前的二进制（含既有 `.bak` 的归档） |
| `SHA256SUMS` | 以上文件的 sha256，写入后立即校验 |

给了口令（`--backup-passphrase-file` 或环境 `YIBAN_BACKUP_PASSPHRASE`）时，
另外跑 `yiban-cli backup` 产出 `db.sql.gpg`（GPG 对称加密，含行数清单）。
没有口令时只留未加密的 `-Fc` 转储，脚本打印 WARN（跳过 ≠ 绿）。

保留最近 `--keep-backups N` 份（缺省 5；`0` = 不清理）。只删本目录下形如
`20261104T101500Z` 的备份目录；备份失败不删任何东西，半成品目录直接清掉。

更新器的状态/备份根放在 `/var/lib/yiban-updater/{state,backups}`：它与服务用户的
`/var/lib/yiban` **同级**，install.sh 只把 `photos`/`ui` 交给服务用户（不再对整棵
树递归 chown），因此重装不会把更新器根改成 yiban 属主。

**从旧布局升级**：旧版本写在 `/var/lib/yiban/{pre-migrate,backups,updates}` 下的
备份与状态**不会删**。新备份落新根；旧备份仍可按上面「看本次备份」的方式手工
`pg_restore`（文件是 root 0600，需 root 读）。自检的 `fs.backup_dir` 判的是新根，
旧目录（典型是 root:root 0700 的 `pre-migrate`）在场也不会误红。

**状态与日志**

| 路径 | 内容 |
|---|---|
| `/var/lib/yiban-updater/state/update-state.json` | 最近一次更新：`result`（running/success/failed/aborted/interrupted）、`exit_code`、`phase`、版本、备份路径、`db_name_before/after`、`db_schema_before/after`、`db_restore` |
| `/var/lib/yiban-updater/state/update.log` | 追加日志（时间／阶段／结果），一行一事件 |
| `/var/lib/yiban-updater/state/selfcheck-latest.json` | 停服前自检的完整 JSON 报告 |

`/var/lib/yiban/update-state.json`（与更新器的 `/var/lib/yiban-updater/state/`
不是一份）是 U1 的**密钥指纹**状态，只由 `yiban-cli selfcheck --record-keys` 写；
更新成功时脚本会自动调用它。
两份文件都不含 DSN 口令、密钥原文与 GPG 口令。

**失败回滚**

| 失败点 | 行为 | 退出码 |
|---|---|---|
| 签名/对账/版本校验 | 不碰旧件、不停服 | 1 |
| 自检 FAIL / 无 CREATEDB / 备份失败 | 不开始（不停服） | 2 |
| 停服失败 | 什么都没换，服务保持原样 | 2 |
| 迁移失败 | 旧二进制继续服务；库没变就不动库 | 1 |
| 换件后 `systemctl start` 失败 | 与健康失败同一条回滚（换回 server/CLI/UI） | 1（回滚健康）/ 2（仍不健康） |
| 迁移后（换件/健康）失败 | 二进制与 UI 换回旧件；**数据库默认恢复到升级前** | 1 |
| 旧件健康，但备份里的 CLI 没换回（缺件/被篡改拒绝执行） | 旧件继续服务但 CLI 版本不配对；打印 manual action | 2 |
| 数据库恢复失败 | 停住（不启动旧件），打印 manual action | 2 |
| 回滚后仍不健康 | 打印 manual action | 2 |

口令不进 argv：`update.sh` 把 DSN 里的口令摘出来写进 0600 的
`PGPASSFILE`（暂存目录内，进程退出即删），交给 libpq；`psql`/`pg_dump`/
`pg_restore` 的 argv 只留去掉口令的 DSN。URI 的百分号编码会解码，
key=value 形态按 libpq 词法解析；解析不了就 `exit 2`（绝不再把口令塞回 argv）。

页面回滚：换过旧目录就删新目录、把 `ui.bak` 换回；首次装页面就删新目录、
撤掉本轮写的 `YIBAN_UI_DIR`（旧二进制恢复「无页面」形态）。

数据库回滚（默认开启，`--no-restore-db` 关闭）：

1. 只在**停服后**、且本次 `db.dump` 通过 `pg_restore --list` 校验时执行；
2. 迁移前后 goose 版本相同（库没变）就跳过——不为了回滚而回滚；
3. 迁移改变了库时：`CREATE DATABASE yiban_restore_<UTC时间戳>` →
   `pg_restore` 到新库 → 校验新 DSN 的 `current_database()` 与新库同名（绝不写回原库）
   → 校验 goose 版本与基表数和备份基线一致 → 原子改写 `/etc/yiban/server.env` 的
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
sudo cat /var/lib/yiban-updater/state/update-state.json
sudo tail -n 50 /var/lib/yiban-updater/state/update.log

# 2) 看停服前自检的完整报告（FAIL 项在 checks[] 里，status=FAIL）
sudo python3 -m json.tool /var/lib/yiban-updater/state/selfcheck-latest.json | less

# 3) 看本次备份与校验
sudo ls -l /var/lib/yiban-updater/backups/
sudo sh -c 'cd /var/lib/yiban-updater/backups/<ts> && sha256sum -c SHA256SUMS'

# 4) 数据库恢复失败（exit 2）时，按打印的 manual action 操作，等价命令：
#    注意：db.dump 是 root 0600（含凭据密文），postgres 用户读不到原文，
#    先复制一份给它（用完即删），不要改 dump 权限。
sudo -u postgres createdb yiban_restore_manual
sudo install -m 0640 -o postgres -g postgres \
  /var/lib/yiban-updater/backups/<ts>/db.dump /tmp/yiban-restore.dump
sudo -u postgres pg_restore --no-owner --no-privileges \
  -d yiban_restore_manual /tmp/yiban-restore.dump
sudo rm -f /tmp/yiban-restore.dump
sudo sed -i 's|^YIBAN_DATABASE_URL=.*|YIBAN_DATABASE_URL=postgres://…/yiban_restore_manual|' /etc/yiban/server.env
sudo systemctl start yiban.service

# 5) 确认旧件与新 schema 兼容、选择不回库时：
sudo systemctl start yiban.service
```

人工恢复后重跑 `sudo bash deploy/baremetal/update.sh --selfcheck-only` 确认全 PASS，
再重新执行升级。

**自动验证**

`.github/workflows/deploy-gate.yml` 的 `update-rollback-e2e` job 是 required
检查，真跑 update.sh 的干跑契约（`go test ./cmd/cli/ -run 'TestUpdateSh|TestInstallSh'`），
任何非 0 退出判红。需要特权 systemd 容器的完整失败矩阵 R1..R10 不在托管 runner
上跑：它由 `.github/workflows/update-rollback-privileged.yml` 承载（手动触发、
自托管 runner），同样 fail-closed。发布前仍按 `docs/dev/deploy-e2e.md` §8 手跑一遍。

## 5. 反向代理

浏览器只访问代理，代理转给 Go 进程。代理必须做三件事：

1. 终结 TLS。
2. 传 `X-Forwarded-Proto: https`。
3. 传 `X-Forwarded-For`。

建议代理同时下发 HSTS（`Strict-Transport-Security: max-age=31536000; includeSubDomains`）。
Go 进程**不**设 HSTS：TLS 在代理终结，HSTS 必须由同一层统一下发，避免双头取值不一致；
只在确认全站与子域都可长期 HTTPS 后再加 `includeSubDomains`。

服务端只采信 `YIBAN_TRUSTED_PROXIES` 里来源的转发头。不配时：

- 所有请求按代理地址计，登录等受限端点共用同一个 IP 桶（默认 60 次/分钟）。
- 外部 TLS 的登录来源判定看到 `http`：带同源 Fetch Metadata 的现代浏览器走 401，旧浏览器与非浏览器客户端回 403。

容器部署的 compose 默认值已覆盖 Docker 网桥与回环（compose 文件里
`${YIBAN_TRUSTED_PROXIES:-172.16.0.0/12,127.0.0.1}` 插值透传）：

```text
YIBAN_TRUSTED_PROXIES=172.16.0.0/12,127.0.0.1
```

原因：发布端口经网桥网关转发，容器看到的来源是网关，不是 `127.0.0.1`（2026-11-01 实测：`127.0.0.1:8080` 发布 → 容器内 `RemoteAddr=172.18.0.1`）。
外层代理在别的地址时有两种覆盖方式，取值优先级为 shell 环境 > compose.env > compose 缺省：

```bash
# 方式一：shell 环境覆盖（插值透传，优先级最高）
export YIBAN_TRUSTED_PROXIES=192.0.2.10
docker compose --env-file deploy/container/compose.env \
  -f deploy/container/docker-compose.yml up -d
```

```bash
# 方式二：改 deploy/container/compose.env 里的 YIBAN_TRUSTED_PROXIES 行，
# 再用 --env-file 启动（运行配置以 compose.env 为正本）。
```

裸机部署的代理在宿主回环。`install.sh` 新装的 `server.env` 已写入：

```text
YIBAN_TRUSTED_PROXIES=127.0.0.1
```

已有 `server.env` 手工补一行后重启：

```bash
sudo sh -c "printf 'YIBAN_TRUSTED_PROXIES=127.0.0.1\n' >> /etc/yiban/server.env"
sudo systemctl restart yiban.service
```

判据：带 `Origin: https://<你的域名>` 与 `X-Forwarded-Proto: https` 的登录请求不得回 403。

## 6. 关键配置

| 键 | 作用 | 缺省 |
|---|---|---|
| `YIBAN_DATABASE_URL` | PostgreSQL DSN | production 必填；其他环境留空须显式 `YIBAN_ALLOW_NO_DB=1` |
| `YIBAN_HASH_PEPPER` | 摘要与审计链密钥 | 必填 |
| `YIBAN_CREDENTIAL_KEY` | 凭据加密钥（32 字节） | 必填 |
| `YIBAN_HTTP_ADDR` | 监听地址 | `127.0.0.1:47183` |
| `YIBAN_UI_DIR` | 前端产物目录（含 `index.html`） | 空：只提供 API |
| `YIBAN_DOCS_DIR` | 合规文档目录（`CHANGELOG.md`、`USER_AGREEMENT.md`、`PRIVACY_POLICY.md`） | 空或文件缺失：`/api/changelog` 回「暂无更新日志」，`/terms` 与 `/privacy` 回占位页 |
| `YIBAN_TRUSTED_PROXIES` | 可信代理 IP/CIDR，逗号分隔 | 空：不信任转发头（compose 默认 `172.16.0.0/12,127.0.0.1`） |
| `YIBAN_PHOTO_DIR` | 照片目录 | `data/photos` |
| `YIBAN_AUDIT_ANCHOR_FILE` | 库外审计锚点文件（append-only，0600） | 空：巡检端点不做锚点比对 |
| `YIBAN_TURNSTILE_MODE` / `_SITE_KEY` / `_SECRET` | 公开注册的人机验证（auto/off/log/on）；两把密钥成对设置，开启时 CSP 放开 Cloudflare 源 | `auto`：缺密钥即关闭 |
| `YIBAN_TURNSTILE_EXPECTED_HOSTNAME` / `_EXPECTED_ACTION` | 可选的令牌绑定：回执 hostname（忽略大小写）或 action（区分大小写）不匹配即判令牌不合格；留空不校验。开启 action 时必须设为 `register`——注册界面控件已固定携带该动作，设成别的值会让开档注册必 400。任一期望非空而私钥为空即启动错误 | 两项都空 |

完整名册与每键说明见 `server/internal/configdata/registry.json`（编译期嵌入二进制）。

### 6.1 审计锚点（建议开启）

`GET /api/admin/audit-status` 只复算链内自洽；DB owner 禁用触发器后删首/尾行，
链内仍然自洽。库外锚点把链头（seq/count/hash）写到数据库之外，巡检时比对：
删前缀、截尾、整表清空与链尾重签都会 `ok=false`。库内 `anchor_meta` 单行高水位
（seq＋head_hash）再兜一层：锚点文件末行被删或整文件消失时，巡检报
`anchor_truncated`，不会因为剩余行仍自洽而放行。
锚点只覆盖到写入时刻，其后的新增行被删需要离机副本（备份/外发）才能发现。
锚点文件本身必须放在数据库权限之外（0600，只有服务/运维用户可写）；
同一 uid 仍可同时改写文件和库内高水位，离机副本是最后一道防线。
备份时把锚点文件一并归档；恢复旧备份后按下面的第 3 步重新播种。

1. 在运行配置里设 `YIBAN_AUDIT_ANCHOR_FILE`（空值＝不启用）：
   - 裸机建议 `/var/lib/yiban/audit-anchor.log`（服务用户可写；服务进程只读）。
   - 容器建议 `/data/photos/audit-anchor.log`（落在 photos 卷，随卷保留；
     blob 回收只认两层摘要目录，不会碰根下这个文件）。
2. 裸机：用 systemd timer 每日追加一次链头。`/etc/yiban/server.env` 是
   0600 root:root，`yiban` 用户直跑 cron 读不到它；systemd 以 root 读
   `EnvironmentFile` 后再降权到 `yiban`，这是唯一能同时满足权限与身份的写法。
   把下面两个单元装进 `/etc/systemd/system/`：
   ```ini
   # yiban-audit-anchor.service
   [Unit]
   Description=Yiban audit anchor
   After=network-online.target postgresql.service
   Wants=network-online.target

   [Service]
   Type=oneshot
   User=yiban
   Group=yiban
   EnvironmentFile=/etc/yiban/server.env
   ExecStart=/usr/local/bin/yiban-cli audit anchor
   ProtectSystem=strict
   ProtectHome=true
   ReadWritePaths=/var/lib/yiban
   NoNewPrivileges=true
   PrivateTmp=true
   UMask=0077
   ```
   ```ini
   # yiban-audit-anchor.timer
   [Unit]
   Description=Daily Yiban audit anchor

   [Timer]
   OnCalendar=*-*-* 03:10:00
   Persistent=true
   RandomizedDelaySec=300

   [Install]
   WantedBy=timers.target
   ```
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now yiban-audit-anchor.timer
   sudo systemctl list-timers yiban-audit-anchor.timer
   journalctl -u yiban-audit-anchor.service   # 链头前进一行 seq/count/hash，安静日一行 already anchored
   ```
   容器：在宿主用 cron 进容器执行（compose 已把运行配置注入 app 容器）：
   ```cron
   10 3 * * * root cd /opt/yiban && docker compose --env-file deploy/container/compose.env -f deploy/container/docker-compose.yml exec -T app /usr/local/bin/yiban-cli audit anchor >> /var/log/yiban/audit-anchor-cron.log 2>&1
   ```
   命令输出是一行 `audit anchor: seq=… count=… hash=…`；锚点文件本身由命令追加，
   不要把 stdout 重定向到同一个锚点文件（日志的 `>>` 目标不是锚点文件）。
   链头未前进时重跑是幂等的：输出 `already anchored`，不追加、不写 meta，
   安静日的每日 timer 不再往文件里堆"同 seq 新时间戳"行。
3. 恢复旧备份或手工整表清理后，锚点会指向已不存在的链头并持续判红
   （`anchor_status=mismatch`、`anchor_reason=anchor_truncated`）。这是预期。
   确认是合法回滚后走闭环：**停服 → 移走旧锚点文件（留取证）→
   `--reseed` 显式重播种 → 巡检回 `ok`**。重播种必须走单元同款身份：
   `/etc/yiban/server.env` 是 0600 root:root，`yiban` 直跑读不到，systemd
   以 root 读 `EnvironmentFile` 后降权到 `yiban`——锚点文件属主保持 `yiban`，
   后续 timer 才接得上。用一次性 transient 单元，**不要**给常驻 service 的
   `ExecStart` 加 `--reseed`（那等于天天放弃检测）：
   ```bash
   sudo systemctl stop yiban.service
   sudo mv /var/lib/yiban/audit-anchor.log /var/lib/yiban/audit-anchor.log.forensic-$(date +%F)
   sudo systemd-run --unit=yiban-anchor-reseed --wait \
     -p EnvironmentFile=/etc/yiban/server.env \
     -p User=yiban -p Group=yiban -p ReadWritePaths=/var/lib/yiban \
     /usr/local/bin/yiban-cli audit anchor --reseed
   # stderr 一行 "--reseed 显式重播种（运维动作）…"，stdout 一行 reseed seq=…
   sudo systemctl start yiban.service
   ```
   巡检回到 `anchor_status=ok` 即闭环完成（`GET /api/admin/audit-status`；
   或再跑一次**不带 flag** 的 `audit anchor`——输出 `already anchored`
   同样说明文件与 meta 已一致）。信任假设同时写进 flag 说明与代码注释：
   `--reseed` 走 `OverwriteAnchorMeta` 无条件覆盖 `anchor_meta`（绕开棘轮），
   只允许人工确认过的恢复动作使用；日常路径的同 seq 异哈希直写会被新棘轮
   （`seq >` 或 `seq =` 且 `line_hash` 相同）拒绝。
   容器形态同理（compose 已注入运行配置），恢复窗口里手动进容器跑：
   ```bash
   docker compose --env-file deploy/container/compose.env -f deploy/container/docker-compose.yml \
     exec -T app /usr/local/bin/yiban-cli audit anchor --reseed
   ```

容器形态另有两个编排层变量，不进 Go 名册，也不在镜像里：`POSTGRES_PASSWORD`
（db 初始口令，同时是派生 `YIBAN_DATABASE_URL` 的唯一来源）与
`YIBAN_BACKUP_HOST_DIR`（备份宿主目录，缺省 `./backups`）。两者都只在
`deploy/container/compose.env` 或 shell 环境里给。

### 6.2 人机验证（Cloudflare Turnstile）

公开注册可接人机验证，只在配好密钥时生效；密钥属于运行配置，**不进仓库**。

1. 在 Cloudflare 控制台 → Turnstile → Add widget，填站点域名，拿到
   site key（公开）与 secret（秘密）。Cloudflare 提供恒过/恒败测试密钥，
   本地联调可先用测试值。
2. 只把两把密钥写进运行配置，二者要么都给、要么都不给：
   - 裸机：`sudo sh -c "printf 'YIBAN_TURNSTILE_SITE_KEY=...\nYIBAN_TURNSTILE_SECRET=...\n' >> /etc/yiban/server.env"`
     （`install.sh` 生成，0600 root:root；systemd 以 root 读后降权，普通用户读不到）。
   - 容器：改 `deploy/container/compose.env`（`.gitignore` 已忽略，经
     `--env-file` 注入；不要写进 `docker-compose.yml`）。
   仓库里三份 `*.env.example` 只留空占位；`TestTemplatesMatchRegistry`
   会钉住键面与「sensitive 键必须为空」，改动模板即红。
   还可选配 `YIBAN_TURNSTILE_EXPECTED_HOSTNAME` 与 `YIBAN_TURNSTILE_EXPECTED_ACTION`
   把令牌绑到站点与动作：留空不校验；填了就必须与回执一致，否则算令牌不合格
   （`on` 档 400，不是 502）。**开 `_EXPECTED_ACTION` 时必须填 `register`**：
   注册界面控件固定带 `action: 'register'`，填别的值会让开档注册全部失败。
   任一期望非空而私钥为空是启动错误。
3. `YIBAN_TURNSTILE_MODE`：缺省 `auto`（两把齐备即强制校验）；先设 `log`
   观察几天（只记 outcome/codes，不拦注册），确认无误再设 `on`；
   `off` 等于关闭，CSP 回到不含 Cloudflare 源的旧基线。
4. 重启服务后验：`GET /api/auth/registration` 的 `turnstile_site_key` 非空；
   注册页出现控件；响应头 CSP 含 `https://challenges.cloudflare.com`；
   日志只出现校验结果与错误码，不出现令牌或私钥。
5. 轮换：在 Cloudflare 侧 roll secret，改文件、重启即可，无需重新构建——
   密钥不编译进二进制，也不进镜像、CI 或前端产物。

## 7. 排障

| 现象 | 先查 |
|---|---|
| 首页 404，`/healthz` 正常 | `YIBAN_UI_DIR` 是否设置；目录里有没有 `index.html`；进程启动日志有没有 `router: YIBAN_UI_DIR` 报错 |
| 登录回 403 | 代理是否传 `X-Forwarded-Proto`；`YIBAN_TRUSTED_PROXIES` 是否包含代理地址 |
| 登录成功但立刻掉线 | 是否用 HTTP 访问；`production` 的 cookie 带 `Secure` |
| 注册页没有验证控件 | 是否两把密钥成对配置；`GET /api/auth/registration` 的 `turnstile_site_key` 是否为空（空＝按设计整块不渲染） |
| 注册回 `human verification failed` | 令牌是否 300 秒内、未重复使用；secret 与站点是否配对；`log` 档可先在日志看 outcome/codes |
| 进程起不来 | `YIBAN_UI_DIR` 指向的目录缺 `index.html` 时服务端**拒绝启动**。这是有意行为 |
| 上传/读取照片失败（裸机） | `server.env` 的 `YIBAN_PHOTO_DIR` 是否等于单元里 `ReadWritePaths` 的路径；改目录后重跑 `install.sh` 再 `systemctl restart` |

### 7.1 00032 索引失败处理

`00032` 用 `CREATE INDEX CONCURRENTLY` 给 `sign_tasks` 建两个按日索引。
并发建索引不在事务里：连接中断或进程被杀会留下 `INVALID` 索引——
索引定义还在，查询不选它。

时机要分清：`00032` 是 NO TRANSACTION 分支，CIC 当场失败时 goose 不会
写入版本行，重跑会再次执行 `00032`；危险在重跑时 `IF NOT EXISTS` 看到
同名 INVALID 直接跳过，随后版本落库为 32。版本一旦记录，`Migrate` 只会
看到 32，不再执行本迁移，重跑也修不好这个索引。

迁移窗口（大表尤其）不要同时跑 `pg_dump`、长事务或另一份备份：旧快照
会让 CIC 的等待阶段卡住。`update.sh` 的备份在停服前做完，迁移阶段不要
再手工备份。

**第一步：逐条查有效性**（连部署库执行）

```sql
SELECT c.relname, i.indisvalid
  FROM pg_index i
  JOIN pg_class c ON c.oid = i.indexrelid
 WHERE c.relname IN ('idx_sign_tasks_day_run_at_id', 'idx_sign_tasks_day_state');
```

只处理 `indisvalid = false` 的那一条。**禁止无条件同时 DROP 两条**：
健康索引被误删后不会自动补，只能按恢复 A 重建。

**恢复 A（推荐，手工重建）**

只删 INVALID 那条，按 `00032` 原样重建对应索引，再 `ANALYZE sign_tasks;`：

```sql
-- 以 idx_sign_tasks_day_state 为例；若是 run_at 那条，换成
-- ON sign_tasks (day, run_at DESC, id DESC)
DROP INDEX CONCURRENTLY IF EXISTS idx_sign_tasks_day_state;
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sign_tasks_day_state
    ON sign_tasks (day, state);
ANALYZE sign_tasks;
```

**恢复 B（让 00032 重跑）**

先只删 INVALID 那条，再把版本行退回到 31，然后跑 CLI 迁移（5 分钟预算）。
下面的 SQL 以 `idx_sign_tasks_day_state` 为示意，按实际 INVALID 的那条替换
索引名：

```sql
DROP INDEX CONCURRENTLY IF EXISTS idx_sign_tasks_day_state;
DELETE FROM goose_db_version WHERE version_id = 32;
```

```bash
set -a; . /etc/yiban/server.env; set +a
yiban-cli migrate
```

`00032` 会重跑：`ALTER TABLE` 幂等，两索引 `IF NOT EXISTS` 只补缺的那条，
`ANALYZE` 与两个 autovacuum 设置也会重新执行。若 CIC 又失败，重复本节。
恢复 A 有 e2e 覆盖（模拟 INVALID 后手工重建）；恢复 B 仅文档核对，未做自动化。

**升级大表时**

优先在停服窗口跑 `yiban-cli migrate`（5 分钟预算）或 `update.sh`，不要靠
重启服务触发启动迁移：启动预算 10 分钟，被 systemd 取消的 CIC 仍可能留
INVALID，失败日志会指向本节。

- `ALTER TABLE sign_tasks SET (...)` 已成功时无需回滚：两个值只影响统计刷新
  频率，不损坏数据，保留即可。
- 若整体回滚该迁移：先删两条索引（无论健康与否），再
  `ALTER TABLE sign_tasks RESET (autovacuum_analyze_scale_factor, autovacuum_analyze_threshold);`。
- 清理 INVALID 索引必须用 `DROP INDEX CONCURRENTLY`：普通 DROP 会长时间持写锁。
- 迁移里两条 CIC 处在 `lock_timeout=0` 窗口：旧快照长期存在时 CIC 会
  等待锁而不是失败，等待结束即完成。手工重建若直接在 psql 里跑，默认也是
  `lock_timeout=0`，同样等待；会话若设了更短的超时，失败后按本节重试。

## 8. 部署判据

可发布＝全新干净环境按本文从零装成，一次完成。
自动检查：

```bash
bash scripts/e2e/deploy-container-e2e.sh   # 容器路径，CI 自动轨
bash scripts/e2e/deploy-baremetal-e2e.sh   # 裸机路径，发布前手跑
```
