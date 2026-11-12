# 生产切换检查单（RC → 生产）

> 适用：Go 栈（`server/` + `ui/`）在生产机的首次切换窗口。
> 把「发布件验收 → 安装演练 → 数据导入 → 切流 → 观察/回滚」收成一页可执行检查单。
> 导入、直连冒烟、回滚与数据销毁的**判据正本**在
> [`legacy-import.md`](legacy-import.md)，本文只引用、不复制；
> 运行文件位置与键面见 [`config-map.md`](config-map.md)；
> 从零安装见 [`deploy/README.md`](../../deploy/README.md)。
> 本文不含真实 DSN、密钥、邮箱、手机号、学校名与域名；命令一律占位符。

## 0. 基线与证据口径

- 版本基线：Go 线版本自 **v1.0.0** 起逐步加版，仓库根 `VERSION` 是唯一来源；
  发布件按切换当日最终 tip 现建，不沿用任何旧基线。当日 tip 以
  `git log --oneline -1 origin/develop` 为准；构建必须在干净 worktree 跑
  `server/scripts/build-release.sh`（主树会注入旧 hash；口径见交接手册
  §3.1，本地过程稿）。
- 生产现状：旧栈 v0.6.5，目录 `/opt/yiban-auto-sign`，SQLite 库 `data/yiban.db`，
  服务 `yiban-web`；无 docker、无 PostgreSQL。生产机只读巡检可用（`ssh yiban`）。
- 发布门证据只在 CI：`.github/workflows/deploy-gate.yml` 的三个作业
  `deploy-gate`、`container-deploy-e2e`、`update-rollback-e2e`。
- 本机无 docker 时，`server/scripts/verify.sh` 的 container e2e 打显性 SKIP。
  **SKIP 不是绿**：它只证明「没跑」，不构成发布证据。
- 导入流程已在一次性实例彩排通过（568 用户、520 账号、520 凭据、0 行错误、
  0 解密失败；真实数据已删除）。这只证明**方法**可用；这些数字是彩排值，
  不是本次切换的预期值。预期值以本次导出 `summary.json` 为准
  （对账判据见 [`legacy-import.md`](legacy-import.md) §4）。

## 1. 发布件验收

以下谓词全部通过才算「发布件可用」。任何一项不过：不安装、不切流。

### 1.1 版本身份

版本单一来源是仓库根 `VERSION`（单行 X.Y.Z）；commit 取
`git rev-parse --short HEAD`；两者由 `server/scripts/build-release.sh`
用 `-ldflags -X` 注入两个二进制。版本自 v1.0.0 起逐步加版；本页不写死
当日 tip 与版本号。

```bash
git log --oneline -1 origin/develop  # 切换当日最终 tip，抄进 §7
git rev-parse --short HEAD           # 构建点的 commit，V1/V2 以它为准
cat VERSION                          # 发布版本（≥ 1.0.0）
bash server/scripts/build-release.sh <outdir>
```

| # | 谓词 | 命令与判据 |
|---|---|---|
| V1 | server 自报版本 | `<outdir>/yiban-server --version` 打印 `version/commit/build_time` 三行；version == `cat VERSION`（发布时 ≥ 1.0.0 的当前值），commit == 构建时 `git rev-parse --short HEAD`；出现 `dev`/`unknown` 即失败 |
| V2 | cli 自报版本 | `<outdir>/yiban-cli version` 三行同上；与 V1 逐行一致（双侧核对） |
| V3 | 版本门 | `bash scripts/release-check.sh --only go-version` rc=0（内部含「发布 tag `v$VERSION` 指向 HEAD」断言；打 tag 前该项 FAIL 是预期，发布时才绿） |

- 构建成功打印 `build-release: version=… commit=… build_time=…`，抄进 §7 结论。
- Windows 宿主产物带 `.exe`（`yiban-server.exe` / `yiban-cli.exe`），仅本地冒烟。
- Linux 发布件、`manifest.json` 与签名由发布者在本机按
  [`../dev/release-signing.md`](../dev/release-signing.md) §2–§4 产出：
  `build-release.sh` → `make-manifest.sh` → `sign.sh`。CI `deploy-gate` 三作业
  只提供门证据，不产出发布件。

### 1.2 清单与签名

发布件三件：`manifest.json`、`manifest.json.sig`、发布公钥。生成与验签脚本在
`scripts/release/`；签名语义与信任锚见
[`../dev/release-signing.md`](../dev/release-signing.md)。

| # | 谓词 | 命令与判据 |
|---|---|---|
| S1 | 清单对账 | `manifest.json` 的 `version` == `cat VERSION`（与 V1 同值）；`files[].name` 覆盖 `yiban-server` 与 `yiban-cli`；每件 sha256 与发布件实算一致 |
| S2 | 验签 | `bash scripts/release/verify.sh --pubkey <发布公钥> --manifest <manifest.json> --signature <manifest.sig>` rc=0 |
| S3 | 信任锚 | 公钥指纹来自发布件目录**之外**（环境变量或安装根锚文件）；与发布者带外交付的指纹一致 |
| S4 | 验证确实跑了 | rc=2 表示「验证没跑」（openssl 缺失、文件缺失）；非 0 一律当失败，不得当通过 |

首次安装不走签名链（`deploy/baremetal/install.sh` 不验签），所以 S1–S4
必须在安装前完成。升级路径的验签与对账由 `deploy/baremetal/update.sh`
承担：先比指纹，再验签，再逐件 sha256 对账；任一失败不碰旧件、不停服务。

### 1.3 前端产物与健康端点

| # | 谓词 | 命令与判据 |
|---|---|---|
| U1 | 页面产物 | `ui/dist` 存在且 `ui/dist/index.html` 可读（`install.sh --ui-dir` 也会硬校验 index.html） |
| U2 | 健康端点 | `curl -fsS http://127.0.0.1:<端口>/healthz` 返回 200，body `{"status":"ok"}` |
| U3 | CLI 探活 | `<outdir>/yiban-cli healthcheck --addr <监听地址>` rc=0（对 `/healthz` 探活一次） |

## 2. 安装演练

发布前手跑项；三条命令见 [`../dev/deploy-e2e.md`](../dev/deploy-e2e.md) §1，
逐条断言见 §2（容器）、§3（裸机）、§8（update-rollback）。

- [ ] 裸机路径：`bash scripts/e2e/deploy-baremetal-e2e.sh` rc=0
      （`install.sh` 一次装成、`/healthz` 探活、systemd 加固、迁移到最新、
      `server.env` 0600 等断言）。
- [ ] 回滚演练：`bash scripts/e2e/update-rollback-e2e.sh host --log-dir <日志目录>`
      rc=0（R1..R10 全矩阵；CI 上 SKIP 时必须手跑，SKIP 不算绿）。
- [ ] 容器路径：CI `container-deploy-e2e` 绿（自动轨；本机无 docker 时看 CI，
      不看本地 SKIP）。

生产窗口内的正式安装（前提：PostgreSQL 18 已就绪且可连；建库不在安装器职责内）：

```bash
sudo bash deploy/baremetal/install.sh \
  --server-bin <outdir>/yiban-server \
  --cli-bin <outdir>/yiban-cli \
  --ui-dir <ui>/dist
# DSN 缺省读环境变量 YIBAN_DATABASE_URL。
# 成功标志：install complete: healthz ok
```

安装器生成 `/etc/yiban/server.env`（0600），缺键时随机生成 pepper 与
credential key，只打印写入路径与指纹，不打印值。
退出码：0 成功；1 健康检查超时或页面落位失败；2 用法或依赖缺失。
装完先跑只读自检：`sudo bash deploy/baremetal/update.sh --selfcheck-only`
（rc=0 全 PASS/SKIP，rc=2 有 FAIL；判读见 [`config-map.md`](config-map.md) §5）。

## 3. 数据导入

判据正本 [`legacy-import.md`](legacy-import.md)，本文不复制。窗口内只确认检查框：

- [ ] 前置条件齐备（新库隔离、新 master 与学校已建、密钥形状正确，
      含 `YIBAN_HASH_PEPPER`）：[`legacy-import.md`](legacy-import.md) §1。
- [ ] 旧库备份完成，sha256 已记录：§1.1。
- [ ] 冻结后导出完成，`summary.json` 已逐项核对：§2。
- [ ] `migrate-legacy ... --strict` 退出码 0：§3。
- [ ] 对账逐项通过（行数守恒、`decrypt_failed=0`、映射无悬空）：§4。
- [ ] 同 owner 的 live 账号数已复核；大于 1 的按 §3.1 处置。
- [ ] 自选时间补录完成：`backfill-time-prefs` 先 dry-run，核对
      `writes` / `conflicts` / `skips`，再 `--apply`，最后 dry-run 复核
      `rows_written=0`：§8 步 7 与
      [`../dev/time-prefs-backfill.md`](../dev/time-prefs-backfill.md)。
- [ ] 幂等重跑符合预期（可选）：§5。

## 4. 切换前检查单

T0 之前逐项打勾。任何一项不过：不切流。

配置与密钥：

- [ ] `/etc/yiban/server.env` 权限 0600 root:root；键面照
      `/etc/yiban/server.env.example` 补齐（地图见 [`config-map.md`](config-map.md)）。
- [ ] `YIBAN_DATABASE_URL` 指向目标新库（不是旧栈 SQLite，也不是彩排实例）。
- [ ] 密钥不进 argv、shell 历史、日志、导出物；`YIBAN_LEGACY_ACCOUNTS_KEY`
      只给导入 CLI，不写进 `server.env`（[`legacy-import.md`](legacy-import.md) §1）。
- [ ] 生成与指纹口径见 [`config-map.md`](config-map.md) §4；含密钥的输出
      不粘贴到聊天或工单附件。

数据与迁移：

- [ ] `yiban-cli migrate` 打印嵌入迁移集最新版本（新库 schema 就绪）。
- [ ] 旧库备份 sha256 已记录并抄进 §7 结论。

旧栈冻结与入口：

- [ ] `ssh yiban` 只读复核服务名、目录、库路径与版本：
      `systemctl status yiban-web`、`ls data/yiban.db`、版本输出；
      与 §0 的现状记录一致（服务名错时停写会立刻报错）。
- [ ] 旧栈已停写（停 `yiban-web` 或进维护模式），并确认不再有写。
- [ ] 正式反代仍指旧栈；新栈只起临时监听端口或临时入口；临时入口不占
      正式域名（[`legacy-import.md`](legacy-import.md) §6）。
- [ ] 不对旧库做 schema 改动、迁移或清理（§8 末段禁令）。

监控与记录：

- [ ] 新栈错误日志落点已确认（`journalctl -u yiban.service`）。
- [ ] 通知投递开关与 SMTP 键状态已确认（[`config-map.md`](config-map.md) §4）。
- [ ] §7 结论模板已备好，边执行边填。

## 5. 窗口执行顺序（切流）

按 [`legacy-import.md`](legacy-import.md) §8 的十一步执行；本文只列顺序与出口判据：

| 序 | 阶段 | 出口判据（正本在 §8 与所指节） |
|---|---|---|
| 1 | T-1 彩排与准备 | §8 步 1；方法已彩排验证（见 §0） |
| 2 | 停旧栈写入 | §8 步 2；确认不再有写 |
| 3 | 备份旧库 | §8 步 3 ＋ §1.1；sha256 已记录 |
| 4 | 导出 | §8 步 4 ＋ §2；`summary.json` 已核对 |
| 5 | 导入 | §8 步 5 ＋ §3；`--strict` 退出码 0 |
| 6 | 对账与一人一号复核 | §8 步 6 ＋ §4 ＋ §3.1；对账全绿；同 owner live>1 已处置 |
| 7 | 补录自选时间 | §8 步 7 ＋ [`../dev/time-prefs-backfill.md`](../dev/time-prefs-backfill.md)；dry-run → 核对 → `--apply` → 再 dry-run 复核 |
| 8 | 直连冒烟 | §8 步 8 ＋ §6；不切正式反代 |
| 9 | 切正式反代 | §8 步 9；`https://<域名>/healthz` 与登录复测通过 |
| 10 | 旧栈停机保留 | §8 步 10；旧库与备份保留，不再写 |
| 11 | 观察 | §8 步 11 → 本文 §6 |

第 9 步之前的失败按 [`legacy-import.md`](legacy-import.md) §7 的切换前回滚
口径处理（停新栈、保留旧栈、新库可丢弃重导）。第 9 步之后进入 §6 观察。

## 6. 切流后的观察与回滚

观察不设硬时长：**至少覆盖一个完整签到窗口**，窗口结束后再复查一轮
错误日志与对账计数。

### 6.1 观察项

| 观察项 | 看哪里 | 正常形状 |
|---|---|---|
| 健康检查 | `https://<域名>/healthz`；`yiban-cli healthcheck` | 持续 200 `{"status":"ok"}` |
| 登录失败 | master 登录；抽查普通用户旧口令登录 | 登录成功；旧口令首登当场重哈希为 argon2id，不要求重置（§6） |
| 凭据解密失败 | 抽查账号详情的机型字段；服务日志 | 机型字段非空（解密成功才可见，§6）；日志无解密失败 |
| 通知投递 | 通知账本与投递日志 | SMTP 已配置时无 `skipped(smtp_not_configured)` 类记录 |
| 引擎首轮签到 | 签到窗口内结果 | 无大面积失败；残余失败可逐条归因（平台侧或账号侧） |
| 自助注销与恢复 | `POST /api/me/delete` 回执 `recover_until`；冷静期内登录 401 带 `details.recoverable`；抢注后恢复 409 | 注销即得恢复窗终点；冷静期内口令正确的登录得到「7 天内可恢复」引导（不建会话）；号被抢注的恢复回 409 与「联系管理员」文案，事务零写入 |
| 错误日志 | `journalctl -u yiban.service` | 无新增 ERROR；无重启风暴（`systemctl show -p NRestarts` 不增长） |

冷静期保号口径：注销（登录账号）即时释放名下签到账号的手机号，宽限期内
不保号。这与旧线 D7 的释放语义一致——释放防止注销后仍占用号段资源，
接受的代价是手机号可被他人抢注；原用户 7 天内恢复时若撞活跃行唯一索引，
恢复报 409（文案不含占用者身份）并由界面引导联系管理员，
不再折叠成「邮箱或密码错误」的 401。注销响应与冷静期登录都携带同一个
`recover_until`，用户当下即可知恢复期限。

### 6.2 回滚触发条件（任一触发即切回旧栈）

- [ ] `/healthz` 连续失败，或 `yiban-cli healthcheck` 持续非 0。
- [ ] master 无法登录新栈（鉴权面失效）。
- [ ] 导入对账不符（§4 任一项在复查中翻红）。
- [ ] 账号/凭据解密失败（解密失败计数大于 0，或详情机型字段普遍为空）。
- [ ] 平台签到大面积异常，排除平台侧原因后仍无法归因。

回滚动作按 [`legacy-import.md`](legacy-import.md) §7：反向代理切回旧栈，
启动旧服务。切换后新栈产生的写入不会回旧库，回滚会丢这段数据；
触发判定宁可保守，不要带病观察。

## 7. 切换结论记录模板

只记占位符与计数，不记身份信息；summary 数字抄导入 stdout。

```
- 日期：<YYYY-MM-DD>
- 执行人：<角色>
- 发布件 version：<X.Y.Z>；commit：<短 sha>
- manifest 校验：verify.sh rc=<值>；sha256 对账=<一致|不一致>
- CI 发布门：<deploy-gate 作业链接占位>；<container-deploy-e2e 链接占位>；
  <update-rollback-e2e 链接占位或手跑记录落点占位>
- 导入 summary：users=<n>；accounts=<n>；row_errors=<n>；decrypt_failed=<n>
- 冒烟：直连冒烟=<过|不过>；切流后 https 复测=<过|不过>
- 切流时刻：<YYYY-MM-DD HH:MM（时区）>
- 观察结论：<覆盖的签到窗口数与结果>
- 回滚：<未发生 | 已回滚（触发条件=<§6.2 条目>；时刻=<…>）>
- 旧库保留起算日：<YYYY-MM-DD>（保留期与销毁见 legacy-import.md §9）
```

记录按秘密级保管：不进仓库，不进聊天记录；数据保管与销毁口径见
[`legacy-import.md`](legacy-import.md) §9。

## 8. 相关文档

- [`legacy-import.md`](legacy-import.md) — 导入、对账、冒烟、回滚、窗口步骤与数据销毁的判据正本。
- [`config-map.md`](config-map.md) — 运行文件位置、秘密与非秘密、生效方式与自检。
- [`../dev/release-signing.md`](../dev/release-signing.md) — 清单、签名、验签与信任锚。
- [`../dev/deploy-e2e.md`](../dev/deploy-e2e.md) — 三条部署 e2e 的命令、断言与 CI 登记。
- [`../dev/release-gate.md`](../dev/release-gate.md) — 分支分级与发布门（Go 线版本门在其 §2④）。
- [`../../deploy/README.md`](../../deploy/README.md) — 从零安装指南（容器与裸机）。
