# M3 修复流 · 批次 0 执行计划（2026-09-25）

> 目的：按 `docs/dev/must-fix-list.md`（develop@178cc7a，MF-1..103）的修复顺序，本批关掉三组 P0——
> 脚本判码与隔离护栏（收尾轮顺序 0：MF-68/78/79/80/81/82）、备份可恢复性（MF-76/77/10）、部署日阻断（MF-40/41仓内半条/42）。
> 执行方式：子代理驱动（SDD）——每任务一个实现子代理 + 任务评审（规格+质量）+ 修复循环（≤5 轮）+ 全分支终审。
> 修复分支：`repair/m3-batch0`；每任务独立提交，任务间串行。

## Global Constraints（每个任务的实现者与评审者都受其绑定）

1. **工作区**：只在 `D:/code/yiban-wt-m3`（分支 `repair/m3-batch0`）内改文件；不碰主工作区 `D:/code/yiban-auto-sign`、不碰其他 worktree、不碰 `D:/code/yiban-wt-study`。
2. **git 纪律**：git 在 Git Bash 跑；**禁止 push**；禁止 `git stash pop/apply`；禁止 `git add -A`（仓库有 0 字节 `nul` 文件须保持未跟踪）；逐文件 add。
3. **门禁环境**：pytest 与 ruff 只在 WSL 跑（Windows venv 全量不可复现，登记表 MF-37）：
   - `wsl -e bash -c "cd /mnt/d/code/yiban-wt-m3 && ~/.venv-yiban-wsl/bin/python -m pytest <目标> -q"`
   - `wsl -e bash -c "cd /mnt/d/code/yiban-wt-m3 && ~/.venv-yiban-wsl/bin/python -m ruff check yiban/ tests/ scripts/ --quiet"`
   - 全量基线 **2928 passed / 4 skipped / 0 failed**（约 60s）；全量红时先按 MF-38 单文件串行复跑再下结论（ManualSignExitTest 有跨文件干扰史）。每个任务提交前跑一次全量。
4. **行为契约不得破坏**：CLI/脚本退出码族语义保持（特别是 `run.sh` 的 rc 契约与 `yiban/cli` 0/1/2/3/10）；`.env` 既有键语义不变；引擎代理出口（`yiban/engine/egress.py` 的 `YIBAN_PROXY*`）行为不变。
5. **MF-91 元纪律（先行生效）**：凡验收不变量是"判据/闸门"，修复必须附带至少一条**活体反例**测试——把输入改坏 ⇒ 判据必须红。没有反例的闸门测试视为未完成。
6. **测试卫生**：禁止"断言源码含某段字"的假绿测试（MF-10 教训）；改行为必须同批更新钉旧行为的测试；测试输出必须干净（无新告警/噪音）。
7. **脱敏**：报告、日志摘录、测试里手机号一律 `138****0000` 形态；不引入任何真实凭据值。
8. **改动保守**：登记表行号相对快照 9d3f491，可能偏移——按符号（函数名/变量名）定位，不按行号盲改；不顺手重构任务范围外的代码。

## Task 1 — run.sh 写失败判码 + sign-status 采信（MF-81 高、MF-82 中）

**现象（摘自登记表）**：
- MF-81：`scripts/run.sh:331` 的 `: > "$SECOND_DONE_MARKER" 2>/dev/null || true` 零留痕 ⇒ 标记缺失时 `:286` 不跳、`:292-297` 状态非 SUCCESS、`:306` **再跑一整轮真实登录**（重复真实登录）。②`:110-114` 把 **noclobber 写失败**当成"今日已触发"⇒ 导出 `YIBAN_SECOND_RUN=1` ⇒ `:201` **静默关闭进程内补签轮**（漏签；脚本自己在 `:50-52` 承认这条混淆），而现网补签只剩这一条通道。③`:128-131` 对锁目录做属主硬校验，对承载判定的 `STATE_DIR` **零校验**。④`:17` `.env` 不可读时无 else 分支、静默回落默认值。⑤`:204` 用 `_is_truthy`、`:276` 用 `= "1"` ⇒ 同一键两套取值域。
- MF-82：`:292-297` 与 `:316` 精确等串采信 `sign-status` 文本，无属主/来源/与库内事实的交叉核对 ⇒ 伪造或搬走 `STATE_DIR` 即可让当天全部账号一次不签。注入通道经裁决降格（写入方只有 run.sh + 3 个测试），环的另一半走 MF-46 的 `.env` 注入臂（另一批修）。

**修法方向**：
1. 凡决定"跑/不跑"的写入（RUN_MARKER、SECOND_DONE_MARKER、状态文件、`_status_write` 的 mktemp/echo/mv 三步、`mkdir -p "$STATE_DIR"`）全部判码；写失败走 **fail-closed = 不跑并告警**；不得存在"写失败==已完成"的等价类。告警必须有声音：stderr + 脚本现有告警通道（沿用 `run.sh` 已有的通知路径；若没有则 stderr + 非零 rc）。
2. STATE_DIR 补属主/可写校验，对齐锁目录的既有做法。
3. `sign-status` 采信前加**与库内事实的交叉核对**（只读 sqlite 查当日 sign_claims/sign_tasks 完成数，或核对状态文件写入者标记 + mtime 合理性；选定一种并写进测试）：不一致 ⇒ 拒绝采信、告警、按未完成处理。
4. `.env` 不可读 ⇒ 显式 stderr 告警并记入状态，不再静默回落默认值（是否退出非 0 由实现者按现网影响选定并在报告说明理由；现网 root-only 读取，不能因此让 cron 每天红——倾向：告警 + 继续用默认值 + 在状态里可见）。
5. `YIBAN_SECOND_RUN` 取值域统一为一处解析（`_is_truthy`）。

**验收不变量（各配测试）**：
- 决定跑/不跑的写入失败 ⇒ 不跑 + 有告警。活体反例：把状态目录置不可写 ⇒ 脚本不得写 SUCCESS、不得再跑整轮。
- sign-status 与库内事实矛盾 ⇒ 拒绝采信 + 告警。活体反例：手写"SUCCESS"状态文件但临时库当日无完成 ⇒ 脚本不得退 0 报成功。
- 既有 rc 契约保持：`tests/test_run_sh_workers.py` 等既有用例绿；被改行为钉住的旧断言同批更新。

**涉及**：`scripts/run.sh`、`tests/test_run_sh_workers.py`（扩展）或新测试文件。

## Task 2 — backup.sh 可恢复性 + 明文包 + tar fail-open + 轮转（MF-76 中、MF-79 中-高、MF-80 中、MF-77 中、MF-10 假绿同批）

**现象（摘自登记表）**：
- MF-76：`scripts/backup.sh:559-561` 在 `try_encrypt` 返回 0（判据只有 gpg 退出码 `:146-153`）后**立刻 `rm -f "${ARCHIVE}"`**；唯一能证明密文可读的 `--restore`（`:169-214` 解密 + 三重包校验 → `:260-303` integrity/audit 双验）**没有任何 cron 或代码调用点**；`:585` 的 sha256sum 是对密文自指纹，证不了可解。同仓 `docker/backup-docker.sh:103-118` 已做"尺寸下限 + 流式解密解包自检 + 失败删件非 0 退出"⇒ 弱契约正是 cron 装的那套。口径修正：删明文那一刻 `TMPDIR_BAK` 里还有明文组件，且删除发生在异机同步之前。
- MF-79：`BACKUP_PLAINTEXT=1` 一个开关即产出含 `.env`(`:76/:361-368`)+整库(`:390-427`)+`keys/accounts-key`(`:440-443`)+30 天日志(`:509-517`) 的明文包（`:65/:107/:533`，文件 0600 但 `:355` **从不 chmod 目录**）；零确认、零落盘护栏、享 30 天保留。最硬一条：`scripts/backup_sentinel.py:70 ARCHIVE_SUFFIXES` 含 `.tar.gz` ⇒ **明文归档直接满足"当日包存在 = 健康"**，告警只进 MF-42 实测 0644 的 backup.log。登记表 424 行"明文凭据进备份包记为误报"依据的是 state 白名单（`:482-489`），不可用来否掉本条。
- MF-80：`:58 set -euo pipefail` 使 `:201/:205/:211` 三条路径护栏在 tar 非 0 时**全部不执行**（活体复现 "GUARD_SKIPPED"），叠加两处 `2>/dev/null` 吞诊断；`--anchored` 是死选项（无 pattern 抽取上首选恒失败，加固从未存在）。
- MF-77：`:646-649` 四条 `find -mtime +N -delete` 按 mtime 删、`RETENTION_DAYS`（`:66`）零校验（0 是合法值=删掉除当天外全部）、删后不数不验不写日志；哨兵只查当日包 ⇒ 历史被清而当天件在 ⇒ 完全静默。反证：同仓 `pull-prod-backup.sh:250-258` 已是"按文件名日期保留最近 N 份"。
- MF-10（同批）：`tests/test_db_integrity.py::BackupPlaintextP3Test` 只断 `backup.sh` 源码含 "BACKUP_PLAINTEXT=1"/"明文" 字串（纯注释行即满足），删掉告警块仍全绿。

**修法方向**：
1. **可解才删明文**：加密成功后先用 `--restore` 的解密+校验逻辑对当日归档做一次真实回环（解到临时目录、integrity_check=ok），通过后才删明文与临时组件；失败 ⇒ 不删明文、不出清单、**非 0 退出**。参考 `docker/backup-docker.sh:103-118` 的强契约。
2. **明文模式护栏**：BACKUP_PLAINTEXT=1 时 stderr 大字告警保留 + 退出码区分（新增专用 rc，如 6=本次明文包，不与既有 rc=4/5 冲突）；备份目录 `:355` 补 `chmod 0700`；明文包保留期从紧（如 2 天，脚本内常量并在注释说明理由）。
3. **哨兵不得把明文包当健康**：`backup_sentinel.py` 判定健康只认加密后缀（`.gpg`）；发现当日只有明文包 ⇒ 判 unhealthy 并走告警路径。选定语义（"明文不计入健康"）并钉测试。
4. **tar 护栏判码**：`:201/:205/:211` 三条护栏改成显式判码执行（`if ! tar …; then` 或等价），tar 失败时护栏**必须执行**且脚本失败；去掉吞诊断的 `2>/dev/null`；处理 `--anchored` 死选项（移除或改对，报告里说明选择）。
5. **轮转加下界**：RETENTION_DAYS 校验（<1 拒绝执行）；删除前保留最少份数下界（当日包 + 最近 K 份，K 为脚本常量）；删除动作逐个写日志行（文件名+时间）；删除后自检"当日包仍在"。
6. **MF-10 行为断言**：`BackupPlaintextP3Test` 改为行为测试——构造 BACKUP_PLAINTEXT=1 实跑（临时目录夹具）⇒ 断言 stderr 告警、专用 rc、明文包不被哨兵计为健康；删除/改坏告警块 ⇒ 测试必须红。

**验收不变量**：当日归档必须能经 `--restore` 解到临时目录且 integrity_check=ok，否则**不得删明文、不得出清单、非 0 退出**（活体反例：gpg stub 必败 ⇒ 明文仍在 + rc 非 0）；明文包存在 ⇒ 哨兵 unhealthy（反例：目录里只放明文 .tar.gz ⇒ 哨兵告警）；tar 失败 ⇒ 护栏执行 + 脚本失败（反例：tar stub 必败）；RETENTION_DAYS=0 ⇒ 拒绝执行。

**涉及**：`scripts/backup.sh`、`scripts/backup_sentinel.py`、`tests/test_db_integrity.py`、`tests/test_backup_sentinel.py`、新测试文件（按需）。

## Task 3 — pull-prod-backup.sh 注入护栏（MF-78 高）

**现象（摘自登记表）**：`:50 REMOTE_DIR="${REMOTE_BACKUP_DIR:-…}"` 与 `:52 MAX_FETCH` 被**未加引号地插进** `:85`/`:112` 的远端命令串 ⇒ `REMOTE_BACKUP_DIR='x; id #'` 一步成立，零文件名配合，`MAX_FETCH` 全脚本无整数校验；远端 `ls` 文件名回流（`:93-96` 单引号拼接）成立但更窄——实测必须含 `'` 才能闭合；`:49 SSH_HOST` 可被 env 覆盖（本机 RCE via `-oProxyCommand`，中）。"现网未启用"不算豁免——`:28-32` 正在劝运维排调度。既有契约测试 10 条全是 `assertIn` 读源码文本（假绿族）。

**修法方向**：所有进远端命令串的变量白名单校验——目录匹配 `^/[\w/.-]+$`、`MAX_FETCH` 必须是纯整数、`SSH_HOST` 拒绝 shell 元字符/空白/换行；校验失败 ⇒ 拒绝执行、非 0 退出、stderr 点名哪个变量；远端命令串一律安全引用；远端回流文件名走白名单字符校验后才进本地变量。

**验收不变量**：注入值 ⇒ 拒绝执行且退非 0（活体反例：`REMOTE_BACKUP_DIR='x; id #'` 与 `MAX_FETCH='1; id'` 各一条测试，用假 ssh stub 记录 argv、断言远端串不含注入片段）；合法值 ⇒ 行为不变（既有合法路径用例改造为行为断言后保持绿）。

**涉及**：`scripts/pull-prod-backup.sh`、其既有测试文件（改造为行为断言）。

## Task 4 — loadtest 隔离链（MF-68 高）

**现象（摘自登记表）**：`scripts/loadtest/scale_driver.py:221`、`concurrency_probe.py:306` 用 `dict(os.environ)` 继承全部代理键且从不摘除；引擎 `yiban/client.py:135` 裸 `requests.Session()`、`trust_env` 在生产代码 0 处设置（默认 True）⇒ 经代理时解析发生在代理端，本机 hosts 与 `--dport 443` REJECT 双双旁落；现网确有 Squid `127.0.0.1:3128`。"测试机"红线只有 Linux+root 两条（`mock_env.py:393-399`），生产机全中。唯一主动探测在缺省 `--egress-probe-ip=""` 下**恒走 [SKIP] ⇒ 代理路径下必报绿**（假担保本体）。`mock_env.py:64` 的 REJECT 用 `-A` 追加链尾 vs ACCEPT `-I 1`（顺序错）。全树 `atexit` 0 命中、搭建段无 try/finally；`reset_state_dir` 前缀白名单漏 `sched-slot-*`/`mail-user-fail-*`/`.lock`/`.tmp<pid>`；`read_meminfo` `except OSError: pass` ⇒ 假"内存饱和 K=1"。

**修法方向**：
1. **启动即断言（fail-closed）**：loadtest 入口统一断言——进程自身与传给子进程的 env 无任何 `*PROXY*` 键；`--egress-probe-ip` 必填（缺省值取消或改为内置安全探测目标并强制校验）；mock 侧记账条数 == 发出条数。任一不满足 ⇒ 拒绝启动（非 0 + 原因）。
2. **代理主动摘除**：子进程 env 构造处删除全部 `*PROXY*` 键；loadtest 自建会话显式 `session.trust_env = False`（这满足登记表"`grep -rn trust_env yiban/ scripts/` 必须有正向命中"）。**只改 scripts/ 侧；不动 `yiban/` 引擎**（引擎代理出口受全局约束 4 保护）。
3. 修 iptables 顺序：REJECT 改 `-I` 前插；`--no-iptables` 不再静默退 0（要么真支持并明示"隔离已降级"且要求显式 `--i-understand-no-isolation`，要么拒绝执行；选定一种）。
4. 搭建段 try/finally（或 atexit）保证清理；`reset_state_dir` 前缀白名单补齐漏项。
5. `read_meminfo` 读取失败 ⇒ 显式报错退出，不得假报内存饱和。

**验收不变量**：带 `HTTPS_PROXY=x` 启动 ⇒ 拒绝启动（反例测试）；`--egress-probe-ip` 缺省 ⇒ 拒绝启动；记账对平（发出 n ⇒ mock 记账 n，不等 ⇒ 非 0）；`grep -rn "trust_env" scripts/` 有正向命中。

**涉及**：`scripts/loadtest/{mock_env.py,scale_driver.py,concurrency_probe.py,capacity_probe.py}`（按需）、新测试文件。**不动 `yiban/`。**

## Task 5 — 迁移 fail-closed（MF-40 高）

**现象（摘自登记表，A 簇全文 + 表六补句）**：v18/v19/v20 标"可选"实为新路径硬前置——任一失败只发 warning、`user_version` 永不提升，而 `try_claim` 把 `epoch` 当硬编列名；现网 `user_version=17`、`sign_claims` 缺 `epoch`。连带：`_ensure_column`/`_ensure_index`/v5 内部的 `conn.commit()` **提前结束 `BEGIN IMMEDIATE`**（"整段迁移原子"的注释不成立，实测第二连接可 BEGIN）；`migrate_v5` 缺 `DROP TABLE IF EXISTS users_new` ⇒ 半程崩溃后重跑必 `table already exists` = **启动永久阻断**；`migrate_v20` 状态目录读不到时静默返回 0 **却照样提升版本**；v18/v20 以 `vshard=-1`+`INSERT OR IGNORE` 占 `(phone,day)` ⇒ 被补成 failed/skipped 的账号**永久失去当日计划**，`pending_count` 只数 pending、闸门显示"已了结"；`_rename_backup` 先写 tmp（umask 0644，含明文凭据）**才** chmod ⇒ 失败即永久残留；`:1195-1199` 同族第二处；SQL 标识符未转义（`:352` 单引号、`:576-587` 双引号）⇒ 最坏 OperationalError 阻断启动；重建型迁移缺列/行守恒断言；v20 回填明文驻留无上界（`_BACKFILL_DAYS=14` 是扫描窗口不是保留期，唯一 `DELETE FROM sign_tasks` 是 `db.py:685` 按 phone，源文件默认 365 天）。

**修法方向**：
1. 迁移分级显式声明：可选/硬前置明确标注；硬前置迁移失败 ⇒ **拒绝启动**（非 0 + 点名哪条失败），不得只 warning 继续。
2. 迁移加记录表（或等价机制）：每条迁移的完成状态持久化，"未完成 ⇒ 拒绝启动"fail-closed；`user_version` 提升与迁移成功绑定。
3. `migrate_v5` 补 `DROP TABLE IF EXISTS users_new`；v20 状态目录读不到 ⇒ 不提升版本 + 显式告警。
4. v18/v20 的 `INSERT OR IGNORE` 占位改显式标记行：不与真实计划同键（`vshard=-1` 行不进 planner 领取面、`pending_count` 排除），被补账账号的当日计划由显式路径恢复。
5. tmp 文件权限先于内容：`os.open(..., 0o600)` 或等效（`_rename_backup` 与 `:1195-1199` 两处）。
6. 重建型迁移加列/行守恒断言（列清单由 schema 驱动，不硬编码）；修 `:352`/`:576-587` 标识符转义。
7. 修 `BEGIN IMMEDIATE` 被 `conn.commit()` 提前结束的三处（`_ensure_column`/`_ensure_index`/v5 内部）——保证"整段迁移原子"承诺成立或改掉承诺。

**验收不变量（各配测试）**：①迁移链跑完后 `epoch` 列仍缺失 ⇒ 非零退出并点名缺哪条迁移；②每条迁移跑完断言"行数守恒 + 版本已提升"；③"迁移中途 kill"用例必须能重跑成功（v5 重跑不再 table already exists）；④tmp 文件权限 0600 断言；⑤v20 状态目录不可读 ⇒ 版本不提升 + 告警（反例）；⑥从 v17 库到全量迁移的成功路径测试保持绿。

**红线**：现网 user_version=17 的升线路径必须保持可用，不得要求人工干预；不得破坏既有迁移测试语义。

**涉及**：`yiban/store/migrations.py`（主）、`yiban/store/db.py`、启动链路（按代码现状定位判定点）、迁移相关既有测试 + 新测试。

## Task 6 — 生产执行件入库与部署断言（MF-42 高 + MF-41 仓内半条 + 真名示例）

**现象（摘自登记表）**：`/usr/local/sbin/yiban-backup.sh` 是手工拷贝（基线那份差 3 行）、`.bak-20260923` 旧件残留、**`yiban-backup-wrapper.sh`（口令注入点）与 `/etc/cron.d/yiban-{cleanup,sign,probe}` 三张 cron 表全仓无原件** ⇒ "审仓库 ≠ 审生产"。`backup.log` 实测 0644 全局可读（同目录 `cleanup.log` 0600），与 `backup.sh:643-645` 自述冲突（backup.log 权限在 Task 2 的 backup.sh 里顺手收，若属运行时行为则归本任务记录）；wrapper 用 `export` 把口令带进整棵子进程树环境。MF-41：`gitee/develop` 停在 `b6457e6`，现网部署命令是 `git pull gitee server-web` ⇒ 按现流程部署不到本线代码；生产有 2 个基线没有的提交（合计 1 行：备注 placeholder 示例改"电力123庄**"，待裁决 #6② 已定换合成名）；GitHub 仓 PUBLIC，该示例已外推。

**修法方向**：
1. 新建 `deploy/prod/`（或沿用仓内既有部署目录约定，先查 `web/deploy/` 与 README）：收编 `yiban-backup.sh`（以生产实测版为准，差 3 行以生产版回填并 diff 说明）、`yiban-backup-wrapper.sh`、三张 cron 表原件；提供 `install.sh`（或 Makefile `install` 目标）：安装到对应绝对路径 + 安装后校验和输出。 `.bak-20260923` 类残留的处理写进安装脚本（安装时清理并记录）。
2. 口令传递改 `--passphrase-fd 0` 单跳（stdin/fd），wrapper 不再 `export` 口令；与 Task 2 的 backup.sh 改动对齐（若 backup.sh 尚无 `--passphrase-fd` 形态，wrapper 侧先按 fd 方案落，冲突交协调者）。
3. **cron 路径来源断言**：一条测试解析 `deploy/prod/` 的 cron 表，断言其引用的每个可执行/脚本路径都能在仓库内找到来源文件。
4. **部署可达断言**：`scripts/check-deploy-target.sh`——断言部署远端（`gitee/server-web`）包含目标提交（`git fetch` + `git merge-base --is-ancestor` 或等价）；输出人类可读结论。**不做任何 push**；报告里写明"统一发布线需用户执行 push"交接事项。
5. placeholder 示例换合成名：全仓 grep `电力123` 定位，改成中性占位（如「电力123示例站」样式或不带姓名的文案，实现者选定并报告）；报告记录"公开历史已有旧值，按待裁决 #6② 接受不 rewrite"。

**验收不变量**：cron 路径来源测试绿（活体反例：往 cron 表加一个仓内不存在的路径 ⇒ 测试红）；wrapper 启动的子进程环境无口令（测试：stub gpg 记录 env ⇒ 断言无明文口令键）；`电力123庄**` 全仓零命中（测试或 grep 证据）；check-deploy-target 对"目标提交不在远端"给出非 0（用本地 fixture 远端测，不碰真远端）。

**涉及**：`deploy/prod/**`（新）、`scripts/check-deploy-target.sh`（新）、web 模板/文案 1 行、新测试。

## 批次边界（本批不做）

MF-41 的 push 与发布线统一（用户执行）；MF-43/45/46/47/48 红线批；MF-71/72/75（分类与假成功批）；容量与口径批（MF-56 族）；CLI/前端批（MF-60/61）；semgrep 6 硬门入库（待裁决 #10）；SL-\* 项。以上排批次 1+。

## 2026-09-25 增补约定（用户指令，优先级高于本计划其他测试条款）：测试降本增效

1. **TDD**：新行为先写测试再写代码，用测试约束后续行为（实现者报告须带 RED→GREEN 证据）。
2. **e2e > 单元/集成套件**：e2e 结果大于一切——每个任务的验收以"真实入口的端到端实跑"为准
   （脚本真跑 + 故障注入、真实迁移链、真实 HTTP 流），哪怕不为行为补齐单元测试也要先保证 e2e。
3. **既有套件不再是唯一约束标准**：改代码/改行为导致既有测试报错时**直接无视**（在报告/台账登记一行即可，
   不要求修绿）；e2e 为准。全量套件仍照跑作回归参考，但红灯不阻塞验收。
