> [← 返回 README](../../README.md)

## Docker 部署（可选）

> 适合已有 Docker 环境、想"一键起 Web + HTTPS + 定时签到"的服务器。**与服务器部署二选一**（都占用 80/443，不要同时跑）。

### 1. 前置条件

- 已安装 Docker 与 Compose（验证：`docker --version`、`docker compose version`；Ubuntu 可参考 `curl -fsSL https://get.docker.com | sh`）
  > **Ubuntu 的 `docker.io` 包不含 Compose**：走 `sudo apt install docker.io` 这条路的人，`docker compose version` 会报 `unknown command: docker compose`，需再装 `sudo apt install docker-compose-v2`（2026-10-09 全新环境演练实测）。用 `get.docker.com` 脚本装的版本自带 compose，不受影响。
- **x86_64** 架构（镜像暂仅构建 x86_64）
- 资源建议 1 核 1G，端口 **80/443** 空闲

### 2. 一键部署

```bash
# 1. 拉取代码（Gitee 为国内拉取源；GitHub 为开发源，两者同一仓库）
git clone https://gitee.com/frostpolaris/yiban-auto-sign.git && cd yiban-auto-sign

# 2. 数据目录与配置（数据全部落在 ./data，证书放 ./certs）
mkdir -p data certs
cp .env.docker.example data/.env
# 管理员账号不用手填：首次打开站点会进入 /setup 引导创建（无浏览器部署用 CLI init）

# 3. 生成 HTTPS 证书（自签，用于快速体验；生产请替换为受信证书）
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout certs/key.pem -out certs/fullchain.pem \
  -subj "/CN=你的域名或IP"

# 4. 启动（首次会构建镜像）
docker compose up -d --build

# 5. 验证
docker compose ps && docker compose logs -f yiban
```

浏览器打开 `https://你的域名或IP`（自签证书首访需点「继续访问」信任），按页面引导创建内置管理员，进入后台后再添加易班账号。

> **无浏览器（headless）首配**：容器健康后，用容器内的 CLI 创建管理员。口令只走环境变量，不写命令行历史。
> ```bash
> docker compose exec -u 10001 -e YIBAN_ADMIN_PASSWORD='你的强口令' yiban \
>   python3 -m yiban.entry.cli init --username admin
> ```
> 成功后打开 `/login` 登录；`/setup` 会自动关闭。

> **国内主机构建镜像**：容器内直连 `pypi.org` 通常不可达，构建会在安装依赖时以 `No matching distribution found` 失败。指定国内索引即可（仅构建期生效，不写进镜像配置）：
> ```bash
> docker compose build --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
> ```
> 或 `docker build --build-arg PIP_INDEX_URL=... -f docker/Dockerfile .`。

> **生产 HTTPS**：用受信证书替换 `certs/fullchain.pem` 与 `certs/key.pem`，然后 `docker compose restart yiban-nginx`；申请免费证书可参考 `web/deploy/nginx.conf.example` 的提示（acme.sh / Let's Encrypt）。

### 3. 日常运维

```bash
docker compose down                  # 停止
docker compose up -d                 # 启动
docker compose logs -f yiban         # 应用/签到日志
docker compose logs -f yiban-nginx   # 反代日志

git pull && docker compose up -d --build   # 更新代码后重建
```

### 4. 与宿主部署的差异（容器内已自动处理）

- **定时签到**：不依赖宿主 cron，由容器内 `supervisor` 常驻的 `docker/scheduler.py` 承担（首签 + 补签 + 每日清理）。
- **时区**：容器固定 `Asia/Shanghai`；同时窗口与日期判定本身按北京时间计算（`yiban/core/clock.py`），宿主是 UTC 也不会算错。
- **安全模型**：nginx 通过 `network_mode: service` 与应用共享网络栈，应用只见回环流量。**信任模型（M12）**：应用只在**请求来源是回环地址**（`127.0.0.0/8` 整段、`::1`、双栈下的 `::ffff:127.0.0.1`）**且转发头信任开关为开**时才采信 `X-Forwarded-For`；非回环来源的 XFF 一律丢弃、退回 `remote_addr`。开关是 `YIBAN_TRUST_FORWARDED_HEADERS`（见 `.env.example`）：**缺省为"不信任"（fail-closed）**；`docker-compose.yml` 已为容器内 nginx 显式置 `1`，非 compose 的反代部署请自行置 1。要伪造 XFF 得先能在回环上发连接——**`ssh -L` 隧道与"回环直连"两种形态下这条不成立**：隧道客户端没有本机权限，却拿到回环身份。这两种形态保持缺省（不信任）即可；显式设 0 会同时停用 `X-Forwarded-Proto` 的 Cookie Secure 自动判定（这两种形态不经反代，本来也不该采信该头）。
- **定时备份（M44，2026-10-01 补）**：容器形态现在**自带每日 02:00 的定时备份**，不再需要宿主 cron——容器部署的用户本来就没有宿主 cron，容器调度器此前又漏了这个挂点，等于"看着在跑、其实从没备份过"。挂点由 `docker/scheduler.py` 的 `BACKUP_AT=(2, 0)` 承担，**复用 `docker/backup-docker.sh`**（加密落盘、自检、保留期轮转）。落点是 compose 声明的命名卷 `yiban-backups` → 容器 `/backups`，**刻意不在 `/data` 里**（否则每轮 tar 会把上一轮备份再打进去，体积逐轮翻倍）。启用只需两步，验证另有三步，见下方「容器形态现在自带定时备份」。
- **自定义 Web 图标**：取消 `docker-compose.yml` 中 `yiban` 服务里那行被注释的挂载（宿主 `./logo.png` → 容器 `web/static/vendor/logo.png`），把图标放到仓库根 `logo.png`。

#### 容器形态现在自带定时备份（启用两步 + 验证三步）

备份脚本拒绝产出明文包（明文包内含全部密钥与管理员工令哈希），所以要启用得先给一份口令文件。

```bash
# ① 建口令文件（内容只有一行备份口令）。必须对容器内的 yiban 用户（uid 10001）可读：
printf '你的备份口令\n' > backup-passphrase
sudo chown 10001:10001 backup-passphrase && sudo chmod 600 backup-passphrase
# ② 取消 docker-compose.yml 里那行注释：
#    - ./backup-passphrase:/run/yiban-secrets/backup-passphrase:ro
docker compose up -d
```

之后：备份包落在命名卷 `/backups`（`docker volume inspect yiban-auto-sign_yiban-backups` 可查其宿主落点），文件名 `yiban-data-<日期>.tar.gz.gpg`，保留 30 天（`YIBAN_BACKUP_RETAIN_DAYS`）。**口令丢失 = 备份不可解密**，请另行离机存一份。

备份卷的**属主不用你手动准备**。容器入口 `docker/entrypoint.sh` 在 root 阶段对 `/backups` 做 `mkdir` + `chown yiban:yiban` + `chmod 0700`，与它准备 `/data` 同一处、同一形态。Docker 首次创建命名卷时挂点是 `root:root 0755`，而写备份的是 supervisord 按 `user=yiban` 降级后的 uid 10001；漏了这一步，每天 02:00 的备份就是 EACCES，日志里只留三行 WARNING。`docker compose up -d` 之后按下面三步自查：

```bash
# 验证①：挂点属主与权限位应是 yiban:yiban 且组/其他无权限（drwx------）
docker compose exec yiban ls -ld /backups
# 验证②：业务账号（uid 10001）真的写得进去（写完即删，不污染备份目录）
docker compose exec -u 10001 yiban sh -c 'touch /backups/.write-probe && echo 可写 && rm -f /backups/.write-probe'
# 验证③：当天 02:00 过后确认产物真的落在卷里（那一轮的结果看 docker compose logs yiban | grep 备份）
docker compose exec yiban ls -l /backups
```

两条必须知道的边界：

- **不启用就没有备份。** 默认部署（没做上面两步）时调度器每天只在日志里留一行「容器备份未启用」后跳过——它判的是**口令文件真的可读**，不是那个环境变量非空，所以不会天天重试报错。
- **口令文件挂载默认是注释掉的**，这是有意的：compose 对缺失的 bind 源会**自动在宿主建一个同名目录**，文件没准备好就挂上去会得到一个读不了的目录、并让容器起不来。

> 容器内的备份脚本是 `docker/backup-docker.sh` 直接 tar `/data`（WAL 模式下的活库快照，不是 `sqlite3 .backup` 的一致性快照）。想要与宿主形态同级的快照一致性，走宿主形态的 `scripts/backup.sh`；这是两种形态的既有差异，本次不合并。

### 5. 目录/文件说明

| 文件 | 作用 |
|---|---|
| `docker/Dockerfile` | 应用镜像（x86_64） |
| `docker-compose.yml` | 编排 `yiban`（应用）+ `yiban-nginx`（反代）双容器，声明备份命名卷 |
| `docker/nginx.conf` | 容器内 HTTPS 反代配置**模板**（端口经官方镜像的 envsubst 渲染；覆盖式 XFF，见上文信任模型） |
| `deploy/web.env` | **部署面唯一值表**：Web 端口与 gunicorn 线程数（systemd/容器/nginx/compose/Dockerfile 共用） |
| `docker/supervisord.conf` | 容器内进程管理（Web + 调度） |
| `docker/scheduler.py` | 容器内签到调度 + 每日 02:00 备份挂点 |
| `docker/backup-docker.sh` | 容器形态的加密备份/恢复脚本（调度器每日调它；也可手工调） |
| `docker/entrypoint.sh` | 容器入口：准备数据卷后拉起 supervisor |
| `.env.docker.example` | Docker 部署配置模板 |
| `.dockerignore` | 阻止账号/密钥/数据库/日志进镜像 |

> 💾 **Docker 备份与恢复**（加密备份 / 手动 tar / 恢复校验）见 [`docs/dev/backup-restore.md`](docs/dev/backup-restore.md)。

<details>
<summary>🔐 安全运维：主管理员权限追回 / 账号凭据密钥泄露处置 / 时钟守卫</summary>

**核心机制**：主管理员会话有效性绑定 `.env` 的 `YIBAN_ADMIN_PW_VERSION`（整数）；递增即全部旧主管理员会话立即失效（无需重启，下一次请求生效）。v0.26.0 起，通过 SSH 重写 `YIBAN_ADMIN_PASSWORD` 后重启，系统检测到"明文与现存哈希不一致"会**自动递增** PW_VERSION。

**场景 A：密码被攻击者改掉/泄露**

1. SSH 登录服务器，编辑 `.env`（Docker：`/data/.env`；systemd：`/opt/yiban-auto-sign/data/.env`）：
   - **删除/清空 `YIBAN_ADMIN_PASSWORD_HASH` 行**（不删则旧哈希仍优先生效，等于没改；**删 HASH 行与保留旧 HASH 行现在等价，都会吊销会话**——重启迁移一改哈希就递增 PW_VERSION 并换发内置会话凭据）；
   - 写入新密码（至少 12 位，且包含大写字母、小写字母、数字、符号中的至少三类，弱口令会拒绝启动）：`YIBAN_ADMIN_PASSWORD=新强密码`；
   - `YIBAN_ADMIN_PW_VERSION` 若已存在则 +1（不存在则忽略，重启迁移会自动处理）；
2. 重启服务：`systemctl restart yiban-web` 或 `docker compose restart yiban`（启动迁移会把新明文转 scrypt 哈希并清空明文，同时递增 PW_VERSION）；
3. 用新密码登录，清理失控的注册管理员（改回普通用户/删除）；
4. （可选，全端强制下线）更换 `YIBAN_SECRET_KEY` 为新随机串——所有用户需重新登录。账号密文密钥（`YIBAN_ACCOUNTS_KEY`）与审计链密钥（`YIBAN_AUDIT_KEY`）相互独立，不受影响。

**场景 B：仅会话 cookie 被盗（密码未失守）**：只做第 1 步的 PW_VERSION+1（实时生效）；如需全端下线再做第 4 步。

> **`YIBAN_ACCOUNTS_KEY` 疑似泄露时怎么办（手工轮换全序）**：该键用于静态加密账号凭据，SSH 失陷时攻击者可读 `.env` 后离线解密。
>
> **先认清两个"面"，漏一个就是静默事故**：
> 1. **同钥密文面不止库内两列**——① `accounts.password` / `accounts.phone_code`（AAD=手机号）、
>    ② `.env` 的 `YIBAN_MAIL_SMTPS_ENC`（告警邮件凭据）、③ `.env` 的 `YIBAN_NOTIFY_SECRET_ENC`
>    （Server酱 SendKey / webhook URL）都是这把钥加密的；换钥只重加密①，②③立刻解不开——
>    **告警通道静默死亡**。（`session_cache` 的 cookie 密文是此钥 HKDF 派生后加密的**可再生数据**：
>    轮换后旧缓存读侧解不开自动清行、用户重登重建，无需手工重写。）
> 2. **钥来源有两档**——环境变量档（现网 web 由 systemd `EnvironmentFile=/etc/yiban/accounts-key`
>    注入）与 `.env` 文件档，且 **env 档优先于 `.env`**；只改其中一侧 ⇒ "web 一把钥、引擎另一把钥"，
>    一侧新写的密文另一侧永远解不开。
>
> **自证手段（动手前后都可用）**：v0.5.0 起新写密文为 v2 格式、携带 `kid`（密钥单向指纹，16 位
> hex；既有 v1 无 kid 密文永久可读、按需重写）。程序启动时对两档钥做**一致性断言**：两档都能读到
> 且不是同一把 ⇒ **拒绝启动**（fail-closed，web 与引擎日志各报两侧 kid）——换对了能不能起、
> 换错了卡在哪侧，由启动日志自证，不靠碰运气；正常启动时日志打印 `账号密钥自证：… kid=…`。
>
> 现版本**仍不提供自动轮换工具**（十年一遇场景，"自动重加密全库"的停服窗口与崩溃恢复维护成本
> 高于收益），真要轮换按以下顺序手工做（每一步都可中断重来）：
>
> 1. 停服全部进程（Docker `docker compose stop yiban`；裸机 `systemctl stop yiban-web`，并停掉
>    cron / 兜底常驻 / 容器调度器）；
> 2. 生成新钥：`python3 -c "import secrets;print(secrets.token_hex(32))"`；
> 3. 用旧钥解密、新钥重加密上面①②③三个面（库内两列与 `.env` 两键一并做；解不开即抛错停下，
>    绝不跳过坏行）：
>
> ```bash
> cd /opt/yiban-auto-sign   # Docker 部署在容器内相应目录
> PY=.venv/bin/python3        # 裸机用部署的 .venv；容器内依赖装在系统解释器，改成 PY=python3
> OLD_KEY='<旧钥64hex>' NEW_KEY='<新钥64hex>' YIBAN_ENV_FILE=data/.env \
> "$PY" - <<'PY'
> import json, os, sqlite3
> from yiban.adapters.config import account_crypto as ac, env_io
> old, new = bytes.fromhex(os.environ["OLD_KEY"]), bytes.fromhex(os.environ["NEW_KEY"])
> conn = sqlite3.connect(os.environ.get("YIBAN_DB_FILE", "data/yiban.db"))
> for pid, pwd, code in conn.execute("SELECT phone, password, phone_code FROM accounts"):
>     for col, v in (("password", pwd), ("phone_code", code)):
>         if v:
>             plain = ac.decrypt_password(json.loads(v), old, pid)
>             ct = json.dumps(ac.encrypt_password(plain, new, pid))  # 新写即 v2+kid
>             conn.execute(f"UPDATE accounts SET {col}=? WHERE phone=?", (ct, pid))
> conn.commit(); conn.close()
> envf = env_io.env_path()
> env = env_io.parse_env_file(envf, strict=True)
> for k in ("YIBAN_MAIL_SMTPS_ENC", "YIBAN_NOTIFY_SECRET_ENC"):
>     raw = env.get(k, "").strip()
>     if raw:
>         plain = ac.decrypt_text(json.loads(raw), old)
>         env_io.write_env_key(envf, k, json.dumps(ac.encrypt_text(plain, new)))
> print("三面重加密完成，新钥 kid =", ac.key_fingerprint(new))
> PY
> ```
>
> 4. **两侧同时换成新钥**：`.env`（或 `YIBAN_ENV_FILE` 指向的文件）的 `YIBAN_ACCOUNTS_KEY` 与环境
>    变量档 `/etc/yiban/accounts-key`（存在即必须同步——它是装单元时由 `.env` 生成的启动快照：
>    旧装法整份 `.env` 拷贝、新装法只拷 `YIBAN_ACCOUNTS_KEY` 一行。按现行装法重做一次并
>    `systemctl daemon-reload`；整份拷贝的旧部署建议顺手收窄成只含密钥行，否则其余被冻结的
>    键会持续压住设置页写入 `.env` 的新值，见「邮箱通知」节注记）；
> 5. 重启全部进程，确认启动日志出现 `账号密钥自证：… kid=<第 2 步打印的 kid>`；若报
>    "两侧不一致"拒启，就是还有一侧没改（断言替你兜住了），按日志里两个 kid 核对补齐；
> 6. 抽样解密自校验（任一账号试手动签到或读回解密）→ 用 `scripts/audit_verify.py` 校验审计链。
>
> **过渡态处置**：升级后若部署本就处于"两侧不一致"而被拒启——先按第 5 步的日志比对存量密文的 kid，
> 确认存量密文实际属于哪一档的钥，把两侧统一成那把并重启（先恢复可启动），再走上面的轮换全序。
> **不要**删断言或绕过它——它是这套流程唯一的自证面。
> 旧密钥一律视为已泄露：若攻击者拷走过数据库文件，历史密文仍需按泄露处理（通知受影响用户改易班密码）。

**事后取证**——裸机部署在检出根执行：`python3 scripts/audit_verify.py --db data/yiban.db --env data/.env --anchor /var/log/yiban/audit-anchor.log`
**事后取证**——容器形态在容器内执行（`docker compose exec yiban sh`）：`python3 scripts/audit_verify.py --db /data/yiban.db --env /data/.env --anchor /data/state/audit-anchor.log`
一次跑完三件校验——哈希链自洽（防改行）、库外锚点比对（防**锚点覆盖区内**的删尾/删前缀/整表清空/截断或改写锚点文件）、审计写入欠账。
退出码 0=健康、1=检出异常、2=无法定论（缺密钥/库不存在/锚点不可读）。批量操作审计含脱敏目标清单，登录成功留有匿名化 IP
审计（登录失败阈值/越权 403/数据导出同样留痕）。

> 诚实边界：以上判据都在**同一台机器**上，且都落在**同一个 uid** 的可写面内。审计锚点文件与 web 进程同 uid
> 可写；拿到该 uid（含取得应用级代码执行者）即可改 `.env` 里的审计密钥、改锚点并在新密钥下重签自洽——
> 链、锚点与库内指纹同属可写面，本机自洽时抹痕可以不被发现。仅**离机副本**是兜底。要真正排除，靠的是**离开本机的留痕**：
>
> 1. **每日随备份外发的审计链头哈希**（`scripts/backup_sentinel.py` 的锚点外发，cron 08:05 在 02:00 备份之后跑，**每天一封**）——
>    正文给出完整 64 位链头哈希 + 记录数 + 当天该用的备份包名。这是 M28 的落点：备份把锚点与库**同包同盘**恢复，
>    两者一起回到旧批次时链自洽、锚点自洽，本机什么都查不出来；外发邮件天然在别的机器上、天然带可信时间戳，
>    是唯一能跨出本机权限域的参照。**恢复任意备份包后，比对该包日期当天那一封里的链头哈希**：不一致即说明
>    那份包/那个库不是它自称的批次（被回滚或被换过）。**为什么整体重写审计链必然暴露（M74）**：能在本机重写链与锚点的人
>    改不掉**已经发出去的那封邮件**——重写后的链头哈希与离机留痕必然对不上，这就是"锚点离机"化解 M74 的那一层。
> 2. **告警通道健康邮件里的链头哈希与记录数**（常规每周一发，通道降级当天就发）——频率低，只作旁证。
> 3. **异机备份副本**（`REMOTE_BACKUP`，其中已含审计锚点文件）。
>
> 怀疑失陷时先取这三处比对，再决定是否按密钥泄露处理。锚点外发依赖邮件通道可达：通道全断的那天不会有基线，
> `backup.log` 里会留一行「审计链锚点未能外发」，看到就别把那天当有离机基线。
>
> **独立见证纵深已移除**：随之失去的是"锚点之后新增的行被删除"这一窗口的本机检出能力（锚点看不见它之后写入的行，删掉它们无需双写即可不被本机发现）。要覆盖它，只能比对上面那几份离机留痕里的链头哈希与记录数。

**时钟守卫**：系统时间前进超 72h / 回拨超 1h（合法长停机、时钟维修后都会触发）时，守卫会**记 ERROR 日志并跳过本轮物理清理**（不删任何数据），同时把参照点推进到当前时间 ⇒ **只跳一轮**，下一轮自动恢复。之所以"只跳一轮"而不是一直冻结：冻结需要人工重置，而重置工具本身就是运维负担。诚实边界：正向拨快被拦后参照点落在被拨后的时间，若此后被 NTP 校正回真实时间，会再触发一次回拨跳变 ⇒ 最多连跳两轮。核实系统时间后无需任何操作，等下一轮即可。

> 若 `.env` 不可写：启动迁移失败后主管理员登录会被 fail-closed 拒绝（明文比对已停用），修复文件属主/权限后重启即自动补齐哈希。

> 注：Windows 无 `fcntl`，签到运行锁/状态文件锁/env 锁退化为进程内互斥——Windows 仅建议单进程开发调试，生产请用 Linux/容器。

</details>

<details>
<summary>👥 管理员权限矩阵（内置主管理员 / 注册管理员 / 普通用户）</summary>

| 能力 | 内置主管理员（.env） | 注册管理员 | 普通用户 |
|---|---|---|---|
| 账号管理（审核/编辑/删除/彻底删除/批量） | ✓ | ✓ | 仅本人（my-*） |
| 手动签到 / 批量签到 | ✓ | ✓ | ✗ |
| 重置/删除普通用户 | ✓ | ✓ | ✗ |
| 设为/取消管理员（单个操作 + 二次确认）、重置/删除**其他管理员** | ✓ | ✗ | ✗ |
| 物理清除已注销用户（剥夺 7 天反悔权，不可逆） | ✓ | ✗ | ✗ |
| 调度设置（排序/分布/掐头去尾/账号间隔/窗口/自选开关/全局暂停） | ✓ | ✗ | ✗ |
| 周六/周日开关、公告、注册开关、探针开关 | ✓ | ✓ | ✗ |
| SMTP 邮箱配置 | ✓ | ✗ | ✗ |
| `.env` 的 PW_VERSION / SECRET_KEY / 容量上限 / 审计密钥 | 仅 SSH | 不可达 | 不可达 |

</details>
