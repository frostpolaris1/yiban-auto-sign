# 备份与恢复（Go 栈）

> 2026-11-27 旧 Python 栈退役后，备份面只剩 PostgreSQL、照片卷与配置/密钥三部分。
> 容器路径的执行细节见 `deploy/README.md` §3.1「容器备份（数据库＋照片）」；裸机路径的
> 一键更新备份见 `deploy/baremetal/update.sh` 头注。本文只写判据与命令入口。

## 1. 备份什么

| 件 | 载体 | 说明 |
|---|---|---|
| 数据库 | 容器：`pg_dump -Fc`；裸机：`yiban-cli backup`（默认纯文本转储 + 尾部 `yiban-backup-manifest`） | 含凭据密文、审计行、调度状态；恢复后必须跑迁移 |
| 照片 | 照片卷 / 目录 | 照片不进数据库备份，需单独打包 |
| 配置与密钥 | `server.env`、`compose.env`、发布公钥 | 与数据分开存放；密钥丢失＝已加密账号不可恢复 |

容器脚本 `deploy/container/backup.sh` 只做数据库与照片，产物固定四件：
`db.dump`、`db.list`、`photos.tar.gz`、`SHA256SUMS`。目录 0700、文件 0600；
任何一步失败即非 0，半成品目录保留排障，不伪装成完整备份。

## 2. 容器备份

在仓库根执行：

```bash
docker compose --env-file deploy/container/compose.env \
  -f deploy/container/docker-compose.yml --profile backup run --rm backup
```

产物落 `deploy/container/backups/<UTC 时间戳>/`，可用
`YIBAN_BACKUP_HOST_DIR=/srv/backups` 改宿主落点。定时执行用宿主 cron 或
systemd timer 拉起同一条命令；任务本身不常驻，也不复制 `compose.env` 的秘密。

脚本不加密。需要静态加密时在宿主对目录内文件跑
`gpg --symmetric --cipher-algo AES256 db.dump`，口令存独立位置，不进
`compose.env` 或容器环境。

## 3. 容器恢复

```bash
# 1) 先验备份完整；若设置了 YIBAN_BACKUP_HOST_DIR，把下面的默认路径替换成它
cd deploy/container/backups/<时间戳> && sha256sum -c SHA256SUMS

# 2) 停 app，避免恢复期间继续写入
docker compose --env-file deploy/container/compose.env \
  -f deploy/container/docker-compose.yml stop app

# 3) 数据库：从 stdin 导入 db.dump（执行用户须对目标库有属主/写权限）
docker compose --env-file deploy/container/compose.env \
  -f deploy/container/docker-compose.yml exec -T db \
  pg_restore -U yiban -d yiban --clean --if-exists --no-owner --no-privileges \
  < deploy/container/backups/<时间戳>/db.dump

# 4) 照片：用同一个一次性任务解包到 photos 卷
docker compose --env-file deploy/container/compose.env \
  -f deploy/container/docker-compose.yml --profile backup run --rm \
  --entrypoint sh backup -c 'tar -xzf /backups/<时间戳>/photos.tar.gz -C /photos'

# 5) 起 app 并验活
docker compose --env-file deploy/container/compose.env \
  -f deploy/container/docker-compose.yml start app
curl -fsS http://127.0.0.1:8080/healthz
```

## 4. 裸机备份与恢复

`yiban-cli` 的 `backup` 与 `restore` 是 Go 服务端形态（pg_dump + GPG +
行数对账）。数据库口令只经 0600 `PGPASSFILE` 传给子进程；GPG 口令优先用
0600 的 `--passphrase-file`，只在无文件时退回 `YIBAN_BACKUP_PASSPHRASE`。
环境变量形态会留在进程环境里，同用户可从 `/proc/<pid>/environ` 读到；
两种形态都不进 argv，但生产应优先用口令文件。

```bash
# 备份：数据库 + 行数清单，GPG 对称加密
yiban-cli backup --out /var/backups/yiban/db-$(date -u +%Y%m%dT%H%M%SZ).dump.gpg \
  --dsn "$YIBAN_DATABASE_URL" --passphrase-file /etc/yiban/backup-passphrase

# 恢复：目标须为空库/schema；非交互环境必须加 --force
yiban-cli restore --in /var/backups/yiban/db-<时间戳>.dump.gpg \
  --dsn "${YIBAN_RESTORE_DSN:?must point at an empty restore target, never YIBAN_DATABASE_URL}" \
  --passphrase-file /etc/yiban/backup-passphrase --force
```

`restore` 会先解密、导入、跑迁移，再按转储尾部的 `yiban-backup-manifest`
逐表对账；随后校验审计链（链非空时缺 `YIBAN_HASH_PEPPER` 即退出 1）。
任一业务表行数不一致或对账零交集即退出 1。照片 blob 目录不在该命令内，
需另行打包与恢复。

## 5. 演练与发布判据

- 容器 e2e 的 C4c/C4d 每次运行都做备份与恢复冒烟：
  `bash scripts/e2e/deploy-container-e2e.sh`。
- 一键更新在换件前做统一备份，迁移失败默认回库：
  `bash scripts/e2e/update-rollback-e2e.sh host --log-dir <日志目录>`。
- 发布前按 `deploy/release.md` 项 5/6 手跑对应入口；POINTER 不等于已执行。

## 6. 边界

- 容器脚本不加密，GPG 需在宿主另做；口令不要放进 `compose.env`。
- `yiban-cli` 不备份照片；照片卷必须单独打包并验证可解。
- `yiban-cli restore` 的目标必须为空库/schema（纯文本转储 + `psql` 导入，
  非空库会失败）。
- 容器路径用 `pg_restore --clean --if-exists` 覆盖同名对象；它不清理由备份
  之外的新表。
- 备份件含凭据密文，按敏感数据保管；保留策略由宿主负责，不要删除仍被
  更新回退引用的备份。
