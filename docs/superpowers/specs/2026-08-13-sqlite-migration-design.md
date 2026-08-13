# SQLite 数据层迁移设计（0.17.0）

> 更新日期：2026-08-13。版本：0.17.0（进行中）。
> 相关记忆：[[sqlite-migration-progress]]；回滚工具 `scripts/db_export.py`。
> 本文档为阶段 B/C/D 的详细改造清单；阶段 A（scripts/db.py）已完成并提交（53dc089）。

---

## 一、背景与目标

部署按年、用户上百、管理员 10+、每年可能迁移服务器、用 AI 高频开发。历史问题：

| 问题 | 根因 | SQLite 方案 |
|---|---|---|
| 并发覆盖 | web 多 worker 同时 JSON 整文件读写 | 单行事务 + WAL + busy_timeout |
| 索引漂移 | 前端以列表位置 idx 寻址，增删后错位 | 稳定 id PK；业务层 idx→id 转换 |
| 进程外覆盖 | TUI / cron 整表写回覆盖 web 修改 | 单行操作 + 整表替换函数（TUI 专用，注明风险） |
| 无审计 | 多管理员操作无法追溯 | audit_logs 表（180 天清理，sqlite3 查询） |

**目标**：accounts/users 数据从 JSON 整文件读写迁移到 SQLite（`yiban.db`，WAL）；
业务层（web/signin/tui）共享 `scripts/db.py` 访问层；JSON 迁移后改名 `.bak` 保留逃生门；
回滚走 `db_export.py`（db→JSON）。

**明确不做**（已确认决策）：
- sign-daily 状态 JSON 不入库（日历/日志保持文件读取）
- 审计不做 UI（管理员用 `sqlite3` 命令行查询）
- 登录限流持久化（内存计数保持现状）
- 密码哈希算法不变（werkzeug scrypt）

## 二、设计原则

1. **前端契约不变**：所有 API 仍以列表索引 idx 与现有字段交互，服务端负责 idx→稳定 id 转换。
2. **数据库原子性兜底**：业务层的"检查→写入"序列由 SQLite UNIQUE 约束 + 事务兜底
   （并发下约束冲突 → 捕获返回业务错误，而非依赖进程锁）。
3. **_file_lock 保留**（进程内互斥）：web 内"读→检查→写"序列（如"每人限 1 账号"判定）
   在单进程内仍需要互斥；跨进程一致性由 SQLite 事务保证。
4. **TTL 缓存移除**：SQLite 本地读为毫秒级（100 账号规模），每次请求直读；
   仅保留与文件系统相关的缓存（CHANGELOG 启动缓存、公告内存缓存）。
5. **密文只在库内**：password/phone_code 在库内仍为 AES-GCM 密文对象（复用 account_crypto），
   `_row_to_account` 解密在 load 时统一完成，业务层始终看到明文。
6. **迁移幂等**：`init_db(migrate_from=...)` 在库空表且 JSON 存在时导入；
   `INSERT OR IGNORE`（phone/email UNIQUE）天然幂等；多 worker 并发迁移安全。

## 三、阶段 A 回顾（db.py 已实现 API）

```
init_db(db_file=None, migrate_from=None)   # 建表/WAL/自动迁移/审计清理
get_conn()
load_accounts() -> [已解密明文 dict]        # 按 sort_order 升序（含 deleted 行）
add_account(fields) -> id                   # 手机号 UNIQUE 冲突抛 sqlite3.IntegrityError
update_account(id, fields, expect_snapshot) # 乐观锁指纹不匹配返回 False；行不存在返回 None
set_account_deleted(id, deleted, deleted_at="")
purge_account(id)
update_account_status(id, status, reject_reason=None)
move_account(id, direction)                 # 仅 deleted=0 行之间交换 sort_order
load_users() / find_user(email)
create_user(email, password_hash, role, created_at, pw_version)
update_user(email, fields)                  # password_hash/role/pw_version
delete_user(email)
audit(username, action, target="", detail="")  # detail 已脱敏，截断 200 字
```

表结构：accounts（id PK/sort_order/name/phone UNIQUE/password/phone_model/phone_code/owner/status/reject_reason/deleted/deleted_at）、
users（id PK/email UNIQUE/password_hash/role/created_at/pw_version）、audit_logs（id/ts/username/action/target/detail，ts 索引）。

## 四、阶段 B：web/app.py 改造（worktree wt-webdb / fix/0.17-webdb）

### B1. 初始化与数据函数替换

| 现状（JSON） | 改造后 |
|---|---|
| `load_accounts()`：TTL 缓存 + 加解密 + 惰性清理 + 解析失败保护 | `db.load_accounts()`（惰性清理移入 db 层，见 §七 db.py 增强） |
| `save_accounts(list)` | 删除；各写操作改 db 单行函数 |
| `load_users()` / `save_users(list)` | `db.load_users()` / db 单行函数 |
| `_accounts_cache/_users_cache/_cache_get/_invalidate_caches` | 删除（TTL 缓存移除） |
| `_load_failed_paths` / `_load_json_list` | 删除（数据库无整文件损坏问题） |
| `_encrypt_account_fields/_decrypt_account_fields/_account_has_*` | 删除（db 层负责） |
| `_file_lock` | 保留 |
| `create_app()` | 开头调用 `db.init_db(os.environ.get("YIBAN_DB_FILE","yiban.db"), migrate_from=ACCOUNTS_FILE)` |
| `config_file` 响应字段 | 显示 db 文件名（basename(YIBAN_DB_FILE)） |
| 手动签到子进程 env | 追加 `YIBAN_DB_FILE`（signin.py 阶段 C 后读 db） |

`ACCOUNTS_FILE` 变量仍保留（用于迁移路径与兼容），但不再有读写语义。
模块顶层导入 `scripts/db`（复用 `_SCRIPTS_DIR` sys.path 机制，与 account_crypto 一致）。

### B2. 账号 API 映射表

| API | 现状 | 改造后 |
|---|---|---|
| GET /api/accounts | load 全量 | load 全量（来源 db）+ mask，不变 |
| GET /api/accounts/<idx>/detail | 列表按 idx | load → idx → mask，不变 |
| POST /api/accounts | 检查唯一/每人限1/自动注册 + append + save | 业务检查 + `db.add_account`（IntegrityError→"手机号已存在"）；自动注册用 `db.create_user`；audit |
| PUT /api/accounts/<idx> | 乐观锁快照 + 整表替换 | `db.update_account(id, fields, expect_snapshot=snapshot)`（False→409，None→404）；audit |
| POST /api/accounts/batch | 整表循环 | approve/reject→`db.update_account_status`；purge→`db.purge_account`；restore→`db.set_account_deleted(0)`；delete→`db.set_account_deleted(1, now)`；audit 一次（done 计数） |
| DELETE /api/accounts/<idx> | 软删 | `db.set_account_deleted(id, 1, now)`；audit |
| POST /api/accounts/<idx>/restore | 软删撤销 | 检查 owner 无其他 live（load 全量）→ `db.set_account_deleted(id, 0)`；audit |
| POST /api/accounts/<idx>/purge | 物理删除 | `db.purge_account(id)`；audit |
| POST /api/accounts/<idx>/review | 审核 | `db.update_account_status(id, status, reject_reason)`；audit |
| POST /api/accounts/<idx>/move | 整表交换 | `db.move_account(id, direction)`；audit |
| GET /api/my-accounts | load + 过滤 | load（来源 db）+ 过滤，不变 |
| POST /api/my-accounts | 提交 | 检查单账号/手机号唯一 → `db.add_account`；audit |
| PUT /api/my-accounts/<idx> | 编辑 | `db.update_account(id, fields)`（rejected→pending 语义保留）；audit |
| DELETE /api/my-accounts/<idx> | 用户自删（物理） | `db.purge_account(id)`；audit |
| POST /api/signin | 手动签到 | load 找 phone → idx 校验逻辑不变（只读） |

### B3. 用户 API 映射表

| API | 现状 | 改造后 |
|---|---|---|
| POST /api/register | 检查唯一 + append + save | 业务检查 + `db.create_user`（IntegrityError→"该邮箱已注册"）；audit |
| POST /api/me/password | 整表改哈希 | `db.update_user(email, {password_hash, pw_version})` |
| GET /api/users | 全量 | `db.load_users()` + 账号计数（load 全量），不变 |
| POST /api/users/batch | 整表循环 | set/unset_admin/reset_password→`db.update_user`；delete→`db.delete_user` + `db.delete_accounts_by_owner`（§七）；audit |
| POST /api/users/<email>/role | 整表 | `db.update_user`；audit |
| POST /api/users/<email>/password | 整表 | `db.update_user`；audit |
| POST /api/users/<email>/delete | 整表 | `db.delete_accounts_by_owner` +（full）`db.delete_user`；audit |
| `_effective_role` | 循环 load_users | `db.find_user(email)` |

### B4. 审计动作清单（db.audit，username=session 用户名）

account_add / account_update / account_delete(软删) / account_restore / account_purge /
account_review(approve|reject) / account_move / account_batch(action, done) /
my_account_add / my_account_update / my_account_delete / user_register /
user_password(自助) / user_role / user_password_reset / user_delete(mode) /
users_batch(action, done) / tui_save（阶段 C）。detail 一律脱敏（手机号 _mask_phone、邮箱 _mask_email）。

### B5. 测试适配

`tests/test_smoke.py`：setUpClass 增加 `YIBAN_DB_FILE`（临时目录）；`_write_accounts` 改为
`db.add_account` 或直写临时 JSON 走迁移路径；删除 `_accounts_cache` 重置逻辑；
`test_plain_migration_to_cipher` 改为验证 db 迁移（明文 JSON → 库内密文 → load 解密）；
`test_batch_purge_requires_deleted` / `test_expired_soft_delete_cleaned` 改调 db 层函数。

## 五、阶段 C：signin.py / tui/app.py 改造（worktree wt-signin / fix/0.17-signin）

### C1. signin.py

- `_load_accounts_from_file()` → 改调 `db.load_accounts()`（已解密明文列表）；
  过滤逻辑保留在 signin 层：`status not in (pending, rejected) and not deleted`。
- `_parse_account_dict` 保留（JSON 环境变量 / 旧格式加载仍用）。
- `_apply_global_device_info` 保留。
- 注意：`account_crypto.load_key()` 默认读 `.env`（cwd=项目根）；db 层解密同密钥，无冲突。
- `YIBAN_DB_FILE` 由 run.sh/.env 注入；cron 场景 cwd 为 /opt/yiban-auto-sign，默认相对路径一致。

### C2. tui/app.py

- `_load()` → `db.load_accounts()`；删除 `_decrypt_accounts`（db 层已解密）；
  文件解析失败提示逻辑删除（db 错误自然抛出）。
- `action_save()` → `db.replace_accounts(self.accounts)`（§七 整表替换）；
  删除加密落盘代码（password/phone_code 加密由 add/update 内 json.dumps 密文处理——
  注意：replace_accounts 内部对敏感字段做与 add_account 相同的密文化，见 §七）；
  随机延迟写 .env 部分保留。
- `_refresh_table` 等展示逻辑不变（dict 字段与 db 行字段一致）。

## 六、阶段 D：备份 / 测试 / 部署 / 发布

### D1. backup.sh
- `DATA_FILES` 由 `(accounts.json users.json .env)` 改为 `(yiban.db .env)`；
  yiban.db 用 `sqlite3 "$f" ".backup '$TMPDIR_BAK/data/yiban.db'"` 一致性备份（WAL 安全，cp 会漏未合并 WAL）。
- `--restore` 说明同步更新（核对项改为 sqlite3 查询账号数）。

### D2. run.sh / 部署模板
- run.sh：`set -a; . .env` 已导出全部变量，.env 配 `YIBAN_DB_FILE` 后自动生效；
  文档标注默认值 `yiban.db`（cwd=/opt/yiban-auto-sign）。
- systemd 模板 `web/deploy/yiban-web.service`：WorkingDirectory 已是 /opt/yiban-auto-sign，
  默认相对路径可用；注释补充 YIBAN_DB_FILE 说明。

### D3. 冒烟测试扩展
新增 db 层用例（临时 db 文件）：CRUD 往返 / 手机号唯一冲突 / 乐观锁 409 语义 /
move 交换 / 审计写入 / JSON→DB 自动迁移（含 .bak 改名）/ 软删除过期清理。

### D4. 版本与文档
- `APP_VERSION` → 0.17.0；CHANGELOG.md 用户可见版（按 [[changelog-writing-principles]]）；
  docs/CHANGELOG-full.md 记技术细节。
- AI-HANDOFF.md / OPERATIONS-GUIDE.md / DEPLOY-CHECKLIST.md 补充 YIBAN_DB_FILE、
  数据文件变化、sqlite3 审计查询、备份新方式。

### D5. 生产演练部署（执行前单独确认）
备份 → git pull → 环境变量 → 重启 → 验证自动迁移（sqlite3 计数 + curl 页面）→
cron 检查 → 观察次日签到日志。

## 七、db.py 增强（阶段 B/C 前置，先在 server-web 完成）

1. **惰性清理**：`load_accounts()` 内 DELETE 超期软删除行
   （`deleted=1 AND deleted_at 距今 >= 7*86400 秒`；deleted_at 空串按不过期处理，
   与 web 现状 mtime 兜底不同——库内数据必有 deleted_at，迁移逻辑保证，无需 mtime 兜底）。
2. **`delete_accounts_by_owner(email)`**：事务内 `DELETE FROM accounts WHERE owner=?`。
3. **`replace_accounts(accounts)`**（TUI 整表保存）：事务内 DELETE 全部 + 逐条 INSERT
   （保持 sort_order = 列表顺序 1..N；敏感字段密文化同 add_account）；
   返回新行 id 列表。⚠️ 语义 = 整表替换：与 web 并发使用时以最后一次保存为准
   （与现状 JSON 整写一致，文档注明"TUI 与 web 勿同时编辑"）。
4. **IntegrityError 语义**：add_account/create_user 的 UNIQUE 冲突（phone/email）向上抛出，
   业务层捕获后返回 400 业务错误（"手机号已存在"/"该邮箱已注册"），
   update_account 改 phone 冲突同样捕获。

## 八、风险与回滚

| 风险 | 缓解 |
|---|---|
| 迁移中数据丢失 | 幂等导入 + JSON 改名 .bak；迁移前先备份 |
| 迁移后功能异常 | 冒烟测试 + 本地演示环境双视角回归 + 生产演练 |
| 回滚 | 停服 → `db_export.py --out /tmp/export` → 恢复 accounts.json/users.json → 旧代码启动 |
| 多 worker 并发迁移 | INSERT OR IGNORE 幂等 + .bak 改名幂等 |
| TUI 整表覆盖 | replace_accounts 文档注明；生产上 TUI 与 web 不同时编辑 |
| 密钥丢失 | 不变：备份必须含密钥（backup.sh keys/ 子目录） |

## 九、测试策略

1. `py -m ruff check web/ scripts/ tui/` 静态清零
2. `py tests/test_smoke.py`（适配后）+ 新增 db 层用例
3. 本地演示环境起服务（YIBAN_DATA_DIR 临时目录 + 迁移验证）：管理员/普通用户双视角 API 冒烟
4. signin.py：`--only` 加载路径本地试跑（不真实签到，验证 db 读取与过滤）
5. 浏览器回归（browser-use）：账号 CRUD/审核/移动/批量/用户管理/我的账号
6. 生产演练部署后：curl 页面 + sqlite3 计数 + 次日 cron 日志观察
