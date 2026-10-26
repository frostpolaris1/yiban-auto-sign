> [← 返回 README](../../README.md)

## 服务器部署（分步详解）

<details>
<summary>📋 展开：环境准备 / 上传代码 / 账号与 .env / run.sh / crontab / 手动测试</summary>

#### 1. 服务器环境准备（Ubuntu 22.04）

```bash
apt update
apt install -y python3 python3-venv python3-pip sqlite3 gnupg
# sqlite3：备份的一致性快照与完整性校验（缺它时 backup.sh 回退 cp 并跳过校验）。
# gnupg：备份加密（--require-encrypt 与故障转移都依赖它）。
useradd -r -s /usr/sbin/nologin yiban   # 服务账号：下一步的 install.sh 要 `-o yiban`
```

> 🔑 **服务账号 `yiban` 必须先存在**（本步是它的唯一建立点）。下一步的 `install.sh`
> 要把运行数据目录 `data/` 设成 `yiban:yiban`（`install -d -o yiban -g yiban`）；
> 账号不存在时它以 `install: invalid user 'yiban'` 退 1，部署中断。`useradd -r` 建的是
> 系统账号、无登录 shell。第 4 步写 `.env` 也用它（`sudo -u yiban tee`）。
> 该账号与 `web/deploy/yiban-web.service` 的 `User=`/`Group=` 同值（换名要一起改，
> 见「网页管理后台」的 systemd 单元说明）。

> **时区建议**：把服务器时区设为 `Asia/Shanghai`（`sudo timedatectl set-timezone Asia/Shanghai`）。
> 不设也**不影响签到功能**——签到窗口、"今天"的判定、状态/日志/备份的按天文件名都固定按北京时间
> （`yiban/core/clock.py` 钉死 +8，与服务器时区无关）；但不设时，**日志行的时间戳**会跟随服务器时区
> （如 UTC 服务器上比面板显示早 8 小时），排查问题时容易对不上时间。

#### 2. 上传项目代码

在本地打包（排除 `.git` 与缓存），再上传解压。**必须整仓更新**：代码已拆到 `yiban/` 多个模块，单文件覆盖会漏文件。

```bash
cd yiban-auto-sign
tar -czf yiban.tar.gz --exclude='.git' --exclude='__pycache__' .
scp yiban.tar.gz root@你的服务器IP:/opt/

# 服务器侧
mkdir -p /opt/yiban-auto-sign && cd /opt/yiban-auto-sign
tar -xzf /opt/yiban.tar.gz && rm /opt/yiban.tar.gz
pip3 config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
# 依赖装进项目自带虚拟环境 .venv（生产用 .venv，不用系统 pip）
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
```

> `mkdir -p /opt/yiban-auto-sign` 是生产默认目录。换目录部署不需要手改 systemd 单元与
> cron 表：单元是模板（`@YIBAN_APP_DIR@`），cron 表由 `install.sh` 按检出根渲染；检出
> 路径限白名单字符集（见「网页管理后台」的 systemd 单元说明）。
>
> 解压完后按第 6 节的顺序做两步：`sudo chown -R root:root . && sudo chmod -R go-w .`，
> 再 `sudo bash deploy/prod/install.sh`（后者建 `data/` 并归 `yiban:yiban`）。见下方
> 「运行数据在检出内的 `data/` 子目录」。

#### 3. 账号数据

推荐用网页后台添加（存 `yiban.db`，AES-GCM 加密）。无头环境 / CI 可用环境变量直接注入：

```bash
YIBAN_ACCOUNTS_JSON='[{"phone":"13800138000","password":"你的密码","phone_model":"Vivo-XXXX","phone_code":"64位识别码"}]'
```

单账号仍可用 `YIBAN_PHONE` / `YIBAN_PASSWORD`（向后兼容）。`accounts.json` 只是旧版本迁移来源（迁移后自动改名 `.bak`）。

#### 4. 配置环境变量（`.env`）

```bash
# 目录须先由 install.sh 建好（属 yiban:yiban、0700）：见「运行数据在检出内的 data/ 子目录」。
# 也可让服务首启自行生成（缺键时自动补齐密钥）。
sudo -u yiban tee /opt/yiban-auto-sign/data/.env >/dev/null << 'EOF'
YIBAN_PROXY=http://127.0.0.1:8888
# YIBAN_ACCOUNT_GAP_MAX=10   # 账号间隔：相邻请求最小间隔秒数（默认 10）
EOF
```

`.env` 只需放公共选项；账号已在数据库里。改完 `.env` 必须重启 Web 服务（配置在启动时读取），定时签到下次触发自动生效。完整键位见 [配置说明](#配置说明) 与仓库根 `.env.example`。

#### 5. run.sh（仓库自带，不要手写替代）

`run.sh` 已包含：单实例锁（防止两次 cron 并发触发风控）、当日已触发标记（第二次触发自动按补签轮运行）、进程内补签轮、总超时、按天日志（`<状态目录>/sign-YYYY-MM-DD.log`）。

```bash
chmod +x /opt/yiban-auto-sign/run.sh
mkdir -p /var/log/yiban
```

> 📁 `/var/log/yiban` 是全部 cron 表的**重定向目标**（签到/备份/审计/清理都写这里）。
> 目录不存在时 `sh` 在**执行命令之前**就因重定向失败退出，命令一行都不跑。
> `deploy/prod/install.sh` 会建立并修复它（属 `yiban:yiban`、`0700`，幂等）；
> 没走安装器的手工部署才需要上面那条 `mkdir`。
>
> 🔒 状态目录与锁目录（默认 `/var/log/yiban`、`/var/lock/yiban`）必须**属主为本用户且
> `chmod 700`**：目录**已存在**时 `run.sh` / `run_probe.sh` / 兜底执行体同样校验（M07），
> 非本用户属主或收紧失败即拒绝运行（不静默降级）。升级后若日志报
> 「状态目录/锁目录不安全」，执行
> `sudo chown yiban: /var/log/yiban /var/lock/yiban && sudo chmod 700 /var/log/yiban /var/lock/yiban`
> 后重跑。
>
> 锁目录的**权威件**是 `deploy/prod/tmpfiles.d/yiban.conf`（`install.sh` 装到
> `/etc/tmpfiles.d/`）：它声明 `/run/lock/yiban` 属主 `yiban:yiban`、模式 `0700`
> （`/var/lock` 是 `/run/lock` 的符号链接，故声明真路径）。按 `install.sh` 部署的主机重启后
> 由 systemd-tmpfiles 自动重建该目录，无需手工 `chown`/`chmod`；上面那条手工命令只在
> 未装 tmpfiles 件的旧主机上需要。

> ⚠️ 不要用"导出 .env 再跑 signin.py"的简化版覆盖它：那会丢掉锁、防重复与超时保护，两次 cron 并发登录同一批账号会触发易班风控。

#### 6. crontab

> ⚠️ **已跑过 `sudo bash deploy/prod/install.sh` 的主机跳过本节**：安装器已把
> `deploy/prod/cron.d/yiban-sign` 落到 `/etc/cron.d/`。这里再抄同样的两行，
> 首签与补签两个排期会各有两条触发器——同一作业并发跑两轮，正是 2026-10-25
> 备份事故（root crontab 与 cron.d 各一条同一 wrapper）的形状。

```cron
# 易班自动签到：06:31 首签 + 07:12 兜底（兜底仅覆盖"06:31 进程被宿主杀死"的场景）
31 6 * * * /opt/yiban-auto-sign/run.sh
12 7 * * * /opt/yiban-auto-sign/run.sh
```

补签时刻须与 `.env` 的 `YIBAN_SECOND_RUN_TIME`（默认 `07:12`）一致：`run.sh` 在首轮结束后、持锁的同一进程内等到该时刻再判定是否补跑；签到侧也按它判断"是否还有下一轮兜底"（告警抑制）。只改 cron 不改该键会出现提前告警或真异常漏报。

周六、周日默认都不签到：在「系统设置 → 签到调度」打开「周六签到 / 周日签到」后才会尝试（学校该日确实无任务时显示为无需签到）。

> 📦 **生产执行件已入库（M3 批次0，MF-42）**：上面的 crontab 行与清理/探针排期在
> `deploy/prod/cron.d/` 有原件（`yiban-sign`/`yiban-cleanup`/`yiban-probe`），配合
> `deploy/prod/manifest.tsv` + `deploy/prod/install.sh` 一键落位（支持 `DESTDIR` 暂存
> 安装；对已存在文件先做 sha256 对账，**校验和不符即拒装**——现网手工漂移必须先 diff
> 回填仓库，或确认以仓库为准后加 `--adopt-production` 归档覆写）。备份的 cron 入口改为
> wrapper：`deploy/prod/yiban-backup-wrapper.sh` 从 0600 口令文件读出口令后经 **stdin
> (fd 0) 单跳**注入 `yiban-backup.sh`，口令不再进入任何进程的环境变量（旧 `export`
> 形态会把口令带进整棵子进程树）。"cron 引用的路径必须能在仓库找到原件"由
> `scripts/check-cron-provenance.sh` 机器断言（安装时强制跑；`tests/test_deploy_prod_artifacts.py`
> 用活体反例钉死这道门）。
>
> 🔒 **以 root 安装的前置门（M01，2026-10-01；作用域修正 2026-10-06）**：`install.sh`
> 以 root 执行检出内的脚本，故**只要以 root 跑（带不带 `DESTDIR` 都一样）检出必须属
> root 且组/其他不可写**，否则拒装（`以 root 安装时检出必须属 root 且非组/其他可写`，
> exit 1）。`DESTDIR` 只改写入目标，不改 root 读取与执行的来源，所以它不豁免这道门；
> 想彻底绕开它只能用**非 root** 跑（无特权暂存安装）。
>
> 🔑 **运行数据在检出内的 `data/` 子目录（工单 `ybg8`）**：服务要写的 `.env` 与
> `yiban.db`（连同 `-wal`/`-shm`，以及通知账本/节流表）都落在 `data/` 下，属
> `yiban:yiban`、模式 `0700`；检出根只放代码。`data/` 由 **`install.sh` 建立**（唯一
> 建立点）——服务不自建：检出根变 root 只读后 `mkdir` 会失败。
> ⇒ 以 `install.sh` 部署时的**正确顺序**是：
> ```bash
> cd /opt/yiban-auto-sign
> sudo chown -R root:root . && sudo chmod -R go-w .   # ① 检出归 root、去组/其他写位
> sudo bash deploy/prod/install.sh                    # ② 落位执行件，并建/修复 data/（属 yiban、0700）
> ```
> ② 会把 `data/` **及其下运行数据**的属主改回 `yiban:yiban`（步骤①的
> `chown -R root:root` 会把它们一起带走），且**幂等**：重跑一次安装器即修复。
> 递归到内容是承重的：只修目录时服务写库报 `attempt to write a readonly database`，
> 而安装器照旧报 `complete`（静默假成功）。`data/` 已在持久盘上，重启不需要重建。
> 服务账号名与 `web/deploy/yiban-web.service` 的 `User=`/`Group=` 同值（本仓
> `yiban:yiban`；两处同值由用例钉住）。
> 状态/日志目录（`/var/log/yiban`）与锁目录（`/var/lock/yiban`）**不在**检出内。
> 前者同样由 `install.sh` 建立（属 `yiban:yiban`、`0700`；它是六张 cron 表的重定向目标），
> 后者由 `tmpfiles.d` 件声明。
> 另注：是否把产物设成 `root:root` 属主，看的仍是「root 且未设 `DESTDIR`」——那是写入
> 侧的不变量，与上面那条判据不同，两条门不许"统一"。
>
> 🔄 **升级既有安装：旧布局的运行数据由 `install.sh` 迁入 `data/`**。`data/` 落点是
> 后加的：在那之前的安装把 `.env` 与 `yiban.db` 放在检出根。升级时服务按新默认值去
> `data/` 找——找不到就自建一份**空库**、读不到 `.env` 的密钥与管理员口令（页面显示
> 零账号）。故 `install.sh` 在同一步把旧件**迁入**：`.env`、`yiban.db` 连同
> `yiban.db-wal`/`yiban.db-shm`（本项目统一 WAL 模式）。迁移的判据三条：
> ① **旧位置不留第二份权威**（是 `mv` 不是 `cp`）；迁入件改属服务账号，服务接着写；
> ② **幂等**：已是新布局时重跑没有任何可迁的件；
> ③ **同名件同时在检出根与 `data/` ⇒ 拒装**（权威未知，不猜、不覆盖）——按提示人工
> `diff` 两份、删掉旧的那份再重跑。
> ⇒ 升级的**正确顺序**（必须先停服：库被搬迁时若服务仍在跑，它会按旧路径另建一份空库，
> 随后重跑会因「两侧并存」拒装，按提示删旧件就丢掉服务刚写的新数据）：
> ```bash
> sudo systemctl stop yiban-web
> cd /opt/yiban-auto-sign && git pull --ff-only
> sudo chown -R root:root . && sudo chmod -R go-w .   # ①
> sudo bash deploy/prod/install.sh                    # ② 落位执行件、迁入运行数据、同步依赖
> sudo systemctl start yiban-web
> ```
> ② 自己也会**探测服务是否在跑**（`systemctl is-active yiban-web`，外加 /proc 里 argv
> 指向本检出的活进程兜底）：在跑即拒装，并打印可复制的停服命令
> （`sudo systemctl stop yiban-web`）。故"忘了停服"不会静默走完。
> 本门**只管裸机形态**：容器内的服务进程 argv 不含宿主检出路径，进程扫描判不出；
> 容器形态也不走本安装器（容器升级 = `git pull && docker compose up -d --build`，
> 见「Docker 部署」）。带 `DESTDIR` 的暂存安装不碰现网运行数据，豁免本门。
>
> 🚦 **部署可达门（MF-41）**：上线前断言目标提交真的在部署线上——
> `bash scripts/check-deploy-target.sh gitee server-web "$(git rev-parse HEAD)"`
> （只 fetch 比对，**永不 push**；红 = 按现流程 `git pull gitee server-web` 部署不到这份
> 代码，发布线统一需持有人执行）。

#### 7. 手动测试

```bash
bash /opt/yiban-auto-sign/run.sh
tail -20 /var/log/yiban/sign-$(date +%F).log
```

</details>
