# 对抗审查修复报告 · 组2（0.9.6）—— 并发与数据安全

日期：2026-08-12
分支：`fix/web-security`
审查依据：`docs/adversarial-review-20260812.md` 的 I2 / I4

## 修复项

### I2（Important）并发竞态：单账号限制/邮箱唯一/手机号唯一可绕过，且并发写互相覆盖丢数据

**漏洞描述**：所有数据操作均为「load → 检查 → save」三分离（`_file_lock` 只保护单次文件 IO），Flask `threaded=True` 多线程下并发请求可同时通过检查：
- 同一用户并发提交 2 个账号（单账号限制绕过）
- 并发注册同一邮箱互相覆盖（后写者覆盖先写者，**先注册者静默丢失**）
- 并发添加账号互相覆盖，账号静默丢失

**修复**：
1. `_file_lock` 从 `threading.Lock` 改为 `RLock`（handler 层锁内可重入 load/save 内部锁）
2. 新增 `_my_account_indices_of(accounts)` 快照版索引函数（锁内用同一份快照，避免两次读文件不一致）
3. 全部「读-检查-改-写」handler 用 `with _file_lock:` 包住完整序列：
   `api_register`、`api_me_password`（注册用户分支）、`api_account_add`、`api_account_update`、`api_account_delete`、`api_account_review`、`api_account_move`、`api_my_account_add`、`api_my_account_update`、`api_my_account_delete`、`api_user_role`、`api_user_password`、`api_user_delete`

### I4（Important）登录锁定按 IP：NAT 下一个用户可锁死全班

**漏洞描述**：登录失败计数只按 IP，校园网/宿舍 NAT 共享出口 IP 的真实用户会被他人爆破尝试连带锁定 300 秒（登录 DoS）。

**修复**：`api_login` 与 `api_me_password` 的失败计数键从 `ip` 改为 `(ip, username.lower())` 组合——同一 IP 下不同用户名互不影响，暴力尝试仍按用户名精确锁定并告警。

## 验证结果

| 验证项 | 方法 | 结果 |
|--------|------|------|
| 单账号限制防并发绕过 | 同一用户 8 线程 Barrier 同步并发 POST /api/my-accounts | 仅 1 个 200，其余 400 ✓ |
| 登录锁定互不牵连 | noise1/noise2 各失败 5 次后，conc 正常登录 | conc 200 ✓ |
| 锁定仍生效 | noise1 第 6 次登录（正确密码） | 429 ✓ |
| 静态检查 | `py -m ruff check web/` + format + py_compile | 全绿 ✓ |

## 影响面

- 锁粒度从单次 IO 提升到整个操作序列，写操作在锁内串行——单写者场景（单管理员 + 少量用户）吞吐无感
- 登录失败计数键变化只影响进程内内存字典，重启即重置，无持久化影响
