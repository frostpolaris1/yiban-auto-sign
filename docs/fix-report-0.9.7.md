# 对抗审查修复报告 · 组3（0.9.7）—— 可用性与清理

日期：2026-08-12
分支：`fix/web-security`
审查依据：`docs/adversarial-review-20260812.md` 的 I3 / I5 / I6 与 M1-M5、M8

## 修复项

### I3（Important）内存无界增长

**漏洞描述**：`_rate_limits` / `_login_fails` / `_register_limits` / `_login_loop` 四个 IP 计数 dict 只增不清，公网扫描器用海量不同 IP 请求即可让内存线性增长直至 OOM。

**修复**：新增 `_IP_STORE_LIMIT = 10000` 上限与 `_ip_store_trim(store, max_age)` 清理助手——仅在 dict 超限时才遍历清理过期条目（无每请求开销）；`_login_fails` 值升级为三元组 `(count, lock_until, last_ts)` 以支持按最后活动时间清理。四个调用点全部接入。

### I5（Important）设置接口 `int()` 异常 + 超大值

**漏洞描述**：`api_settings_save` 对 `"abc"` 直接 ValueError → 500；超大整数写入 .env 会破坏生产签到随机延迟。

**修复**：try/except 返回 400「延迟秒数必须是整数」；值域钳制 0~3600。

### I6（Important）日志全文件读入内存

**漏洞描述**：`api_my_logs` 的 `reversed(f.readlines())` 与 `parse_sign_log` 都整读 sign.log（36MB/年），每 10 秒轮询叠加消耗内存与 IO。

**修复**：新增 `_tail_lines(path)`——从文件尾部倒读最多 2MB（约 2 万行）的完整行；`parse_sign_log` 与 `api_my_logs` 均改用它（保留倒序扫描与更早日期截断逻辑）。

### Minor 清理

| 编号 | 修复 |
|------|------|
| M1 | `api_my_logs` 日期校验改 `\d{4}-\d{2}-\d{2}` 全匹配（原 `2026-1-1-1` 可通过） |
| M2 | 密码版本会话吊销：users.json 新增 `pw_version`（注册=1，改密/被重置 +1），登录写入 session，`_effective_role` 校验不一致即失效；旧数据无该字段不校验（兼容存量会话） |
| M3 | `/api/logs` 的 `log_file` 只返回文件名（`os.path.basename`） |
| M4 | `api_users` 新增 `_builtin_admin_display()` 显示 .env 原始大小写（`_builtin_admin_email` 保持小写仅供比较） |
| M5 | `_atomic_write` 增加 `flush + fsync` 后再 `os.replace` |
| M8 | `validate_account` 增加名称 ≤50 / 设备型号 ≤50 / 识别码 ≤128 长度上限 |
| M6 | `--debug` 保留（开发调试用，生产严禁开启）——不改代码 |
| M7 | 手动签到子进程并发需 signin.py 侧单独评估——本轮范围外 |
| M9 | 注册邮箱验证需邮件基础设施——已知设计 |

## 验证结果

| 验证项 | 方法 | 结果 |
|--------|------|------|
| I5 非法整数 | 管理员 POST `start_delay_max:"abc"` | 400「必须是整数」✓ |
| I5 超大值截断 | POST `99999999` | 200 且读回 3600 ✓ |
| M2 会话吊销 | 管理员重置 a1 密码 → a1 旧会话 GET /api/me | 401 ✓；新密码可登录 ✓ |
| M1 日期校验 | `?date=2026-1-1-1` / `?date=2026-08-01` | 400 / 200 ✓ |
| M3 路径脱敏 | `/api/logs` 返回 `test-sign.log`（无路径） | ✓ |
| M4 显示名 | `builtin_admin == "admin"`（.env 原样） | ✓ |
| 静态检查 | `py -m ruff check web/` + format + py_compile | 全绿 ✓ |

## 影响面

- `pw_version` 只影响**注册用户**会话：存量会话（登录于本次部署前）若用户记录无该字段则不受影响；内置管理员（.env）无版本机制，改密后旧会话仍有效（已知限制，30 天会话上限兜底）
- 日志查询最多读尾部 2MB：极端情况下更早日期的日志行不在倒读窗口内（正常运营场景 2MB ≈ 2 万行，覆盖数周，可接受）
