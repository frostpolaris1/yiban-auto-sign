# 备份与恢复

> 本文由仓库根 `README.md` 的「运维 → 备份与恢复」一节与「Docker 备份与恢复」折叠块拆出，正文逐字保留。
> 文内命令均在仓库根执行；容器内路径以 `docker-compose.yml` 的挂载与环境变量为准。

## 宿主部署（systemd / cron）

`scripts/backup.sh`（建议 cron 每日 02:00）做四件事：`sqlite3 .backup` 一致性快照 → **快照必须过 `PRAGMA integrity_check` 才算这次备份成功**（源库本身损坏时归档照留但非 0 退出；`.backup` 失败回退 `cp` 且校验不过时不落该归档——"看着有备份"比没备份更危险）→ 本地归档**默认加密**（明文落盘需显式 `BACKUP_PLAINTEXT=1`）→ 本地与异机各保留 30 天（`.sha256` 侧车一起轮转）→ 可选异机加密副本（`REMOTE_BACKUP`）。本地归档默认落 `/var/backups/yiban`（专用子目录；与 `pull-prod-backup.sh` 的 `REMOTE_BACKUP_DIR` 默认同值，改一处必须同批改三处）。

包内内容除 `yiban.db`、`.env`、密钥文件外，还含**当日闸门标记与账本**（`sched-run-*`/`sched-slot-*`/`sched-snapshot-*`/`notify-ledger.json`/`notify-throttle.json`/`cred-state.json`）与**审计链外部锚点** `audit-anchor.log`：缺前者恢复当天会重签或漏签、告警日额度被重置；缺后者恢复出来的库就再也验不了"删尾/删前缀/整表清空"。`--restore` 解包后会自动跑 `integrity_check` 与 `audit_verify.py`（链 + 锚点 + 欠账）并带回结论，同时提示"先停服再覆盖"与"必须删除残留 `-wal`/`-shm`"。

```bash
# 备份（生产执行件收编在 deploy/prod/：backup.sh→/usr/local/sbin/yiban-backup.sh、
# wrapper、各 cron 表（含 02:00 备份 deploy/prod/cron.d/yiban-backup）一起按
# manifest.tsv 落位；sha256 对账不过即拒装，见其头注释。
# 单独手工安装亦可：sudo install -m 0700 -o root -g root scripts/backup.sh /usr/local/sbin/yiban-backup.sh）
sudo bash deploy/prod/install.sh
# 触发器只有一条：/etc/cron.d/yiban-backup（权威件；表内排期 02:00、身份 root、
# 命令 /usr/local/sbin/yiban-backup-wrapper.sh；--require-encrypt 由 wrapper 钉死，
# 不带时加密配置失效会静默产出含全部密钥与口令哈希的明文归档）。
# **不要**再往 root crontab 加同一条命令：两条触发器会在 02:00 并发跑同一份备份
# （2026-10-25 事故：gpg 互相覆写，当天备份全失）。已有该行的主机必须删除。
# 口令从 0600 口令文件经 **stdin fd 0 单跳**
# 注入，不进 crontab 行 / 命令历史 / 任何子进程的 env（M3 批次0 起，MF-42）：
#   printf '你的备份口令\n' | sudo tee /etc/yiban/backup-passphrase && sudo chmod 600 /etc/yiban/backup-passphrase
# 手工/恢复场景仍可环境变量注入（会给整棵子进程树带上口令，用完即散）：
sudo BACKUP_GPG_PASSPHRASE='你的备份口令' /usr/local/sbin/yiban-backup.sh --require-encrypt
# 每日取证校验（锚点判据不能只挂在 web 的每日线程上——web 没起来就永远没人查）。
# 权威件 deploy/prod/cron.d/yiban-audit（身份 yiban）里的 `cd` 目标带**尾斜杠**
# `/opt/yiban-auto-sign/`——承重：check-cron-provenance.sh 只认 `$APP_ROOT/*` 形态可回指
# 仓库，去掉尾斜杠会让来源断言判红。下面手工执行这条不带尾斜杠，与那份等价，但别照抄进 cron.d：
30 2 * * * yiban cd /opt/yiban-auto-sign/ && .venv/bin/python3 scripts/audit_verify.py --db data/yiban.db --env data/.env >> /var/log/yiban/audit-verify.log 2>&1
# 备份哨兵（08:05，02:00 备份之后）：当日包/清单缺失、运行脚本与检出**漂移**、
# 或同一作业的活跃触发器不为 1 条时发一封
# 管理员告警；正常路径零输出。**以 root 运行**（须读 0700 root:root 的已装 yiban-backup.sh
# 做防漂移比对；yiban 读不到会每日误报）；状态目录另给 root 私有目录，避免 root 在 yiban
# 属主的共享状态目录里留下属主文件。权威件 deploy/prod/cron.d/yiban-backup-sentinel：
5 8 * * * root YIBAN_STATE_DIR=/var/lib/yiban-backup-sentinel APP_DIR=/opt/yiban-auto-sign /usr/local/sbin/yiban-backup-sentinel.sh >> /var/log/yiban/backup.log 2>&1
# 恢复演练 / 真实恢复（支持 .tar.gz / .gpg / .age）
sudo APP_DIR=/opt/yiban-auto-sign BACKUP_GPG_PASSPHRASE='你的备份口令' \
  bash scripts/backup.sh --restore <备份包> <目标目录>
```

> ℹ️ **升级提示（曾装过"审计独立见证"的旧部署请清理）**：审计的独立见证纵深已移除，`deploy/prod` 不再提供 `yiban-audit-witness.sh` 与 `/etc/cron.d/yiban-audit-witness`。此前按旧文档装过见证的主机请手工删掉这两个文件——`sudo rm -f /usr/local/sbin/yiban-audit-witness.sh /etc/cron.d/yiban-audit-witness`——否则残留的 root cron 会每 10 分钟调用已删除的脚本并持续报错（仅噪声，不影响体检结论）。移除后"锚点之后的追加行被删除"这一窗口不再由本机检出（见 README「安全运维」的「诚实边界」）。

> ⚠️ **异机副本默认未启用**：`REMOTE_BACKUP` 不配置时备份仅存本机——root 失陷时攻击者可一并清掉 `/var/backups/yiban` 下的备份（备份随主机同灭）。**`backup.sh` 本体只从环境变量或 stdin（fd 0 单跳，由 wrapper 注入）取口令**（`BACKUP_GPG_PASSPHRASE`，旧名 `BACKUP_AGE_PASSPHRASE` 兼容；另有 `BACKUP_GPG_RECIPIENT` 走公钥加密）；它自己不读口令文件——读 0600 口令文件的是 `yiban-backup-wrapper.sh`，且只经管道单跳给 backup.sh，口令不进任何子进程的 env。所以口令**必须另行离机保存一份**（密码管理器/离线介质；`/etc/yiban/backup-passphrase` 不算离机副本），否则主机损毁 = 备份与口令同灭、密文不可恢复。需要异地容灾时配置 `REMOTE_BACKUP`（见脚本头部说明）。
>
> ⚠️ **公钥模式（`BACKUP_GPG_RECIPIENT`）没有回环自检，这是它的代价**：服务器只存公钥、没有对应私钥，所以每轮备份的「解密 → 解包 → `integrity_check`」这一步**在本机根本做不到**，脚本只能降级成「密文 ≥200 字节」这一句尺寸检查。后果要说清：**一把 >200 字节的损坏密文会被判为合格**——脚本照常删明文、写 `.sha256` 清单、跑异机同步与保留期轮转，本地只剩一把解不开的密文；而哨兵（只要求归档与 `.sha256` 旁挂件同在）会判它健康。这个边界**在本机无法消解**（本机没有私钥），只有两条路能盖住：① **按计划跑 `--restore` 演练**（唯一的真判据）；② 异机侧真解一次。两条都做不了就用口令模式（能回环自检），别用公钥模式。
>
> ℹ️ 恢复路径的审计核验按结论分档：**通过**（退出码 0）/ **检出异常**（1，链被改写或库与锚点不同批次）/ **无法定论**（2，缺解释器、缺 `YIBAN_AUDIT_KEY` 或包内 `.env`）。给到 2 时别按"备份完好"处理，也别按"被篡改"处理——先补齐解释器与密钥来源再重跑。恢复件用的解释器优先取部署自己的 `.venv/bin/python`（系统 `python3` 往往没有 `pycryptodome` 等依赖）。


> ⚠️ 加密密钥（`.env` 的 `YIBAN_ACCOUNTS_KEY`）与数据分开备份——密钥丢失 = 已加密账号密码不可恢复。
>
> ℹ️ 数据库结构由启动时自动迁移（老数据平滑升级）；升级前仍建议先跑一次备份。

**取证与排障工具**：

```bash
.venv/bin/python3 scripts/audit_verify.py --db /opt/yiban-auto-sign/data/yiban.db --env data/.env --anchor /var/log/yiban/audit-anchor.log
    # 链自洽 + 库外锚点比对 + 审计写入欠账三件一起跑；0=健康 1=检出删除/篡改 2=无法定论
.venv/bin/python3 scripts/list_duplicate_owners.py                   # 列出"同一用户多个未删除账号"（人工清理后自动恢复一人一号约束）
.venv/bin/python3 scripts/db_export.py --out /tmp/export             # 导出回 JSON（降级/迁移用）
```


## Docker 部署

<details>
<summary>💾 Docker 备份与恢复（加密备份 / 手动 tar / 恢复校验）</summary>

数据（SQLite / 账号密文 / 加密密钥 / 日志）全部位于宿主 `./data`，**备份该目录即可**：

```bash
# 推荐：加密备份（口令经 0600 文件读入，不进子进程环境；RETAIN_DAYS 自动轮转，默认 30 天）
printf '%s\n' '你的备份口令' > /etc/yiban/backup-passphrase && chmod 600 /etc/yiban/backup-passphrase
YIBAN_BACKUP_PASSPHRASE_FILE=/etc/yiban/backup-passphrase bash docker/backup-docker.sh

# 也可手动裸 tar（明文落盘，请自行妥善保管）
# 注意：此命令打包的是活库快照，已提交但尚未 checkpoint 的行在 data/yiban.db-wal 里；
# 恢复时必须把整棵 data/ 解回，且不能留下目标侧旧 yiban.db-wal/yiban.db-shm（见下方恢复说明）。
tar czf yiban-backup-$(date +%F).tar.gz data/
```

> 兼容写法：`YIBAN_BACKUP_PASSPHRASE='你的备份口令' bash docker/backup-docker.sh` 仍可用，
> 但口令会进入该 shell 与 tar/gpg/find 整棵子进程的环境变量（`/proc/<pid>/environ` 可读），
> 脚本会打印一行提示；建议改用上面的 `_FILE` 形式。

恢复（校验与解包一体；口令经 `_FILE`（或环境变量）注入，不出现在命令行/ps/shell history；解包前做路径穿越、符号链接、设备节点三重校验）：

```bash
YIBAN_BACKUP_PASSPHRASE_FILE=/etc/yiban/backup-passphrase \
    bash docker/backup-docker.sh --restore backups/yiban-data-2026-08-29.tar.gz.gpg ./restore-test
```

> **恢复后启用（任意 Docker 备份路线：`backup-docker.sh` 或手动裸 tar）**：两种路线产出的都是 `data/` 的**活库快照**（含 `yiban.db` 与其 `yiban.db-wal`/`yiban.db-shm`；非 `sqlite3 .backup` 一致性快照）。生效步骤：① `docker compose stop yiban`；② **整棵 `data/` 覆盖**回宿主 `./data`；若采用"保留原目录、只覆盖库文件"的做法，必须先删掉目标侧残留的 `yiban.db-wal` 与 `yiban.db-shm`，再把 `yiban.db`、`yiban.db-wal`、`yiban.db-shm` 三件一起覆盖；③ `docker compose start yiban`。**不要只替换 `yiban.db`**：已提交但尚未 checkpoint 的行在 `yiban.db-wal` 里，只搬主文件会静默丢掉这一批行。
>
> ⚠️ 与 systemd 部署一致：加密密钥（`data/.env`）与备份口令要与数据**分开存放备份**——密钥丢失 = 已加密账号不可恢复。
>
> ⚠️ 威胁边界：口令与数据**同机**存放（root crontab/.env）时，加密只能防「备份介质单独失窃」——SSH/root 失陷即口令与全部备份（含异机副本）同时易手。更高强度口径：用 systemd 部署 `scripts/backup.sh` 的 `BACKUP_GPG_RECIPIENT` 公钥模式（服务器只存公钥），或把口令/私钥保存在异机、仅在备份时注入。

</details>
