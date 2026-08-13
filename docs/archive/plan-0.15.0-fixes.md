# 0.15.0 综合修复计划（2026-08-13）

> 汇总全部未处理审查问题（功能 29 条 / 安全第三轮 13 项 / 协议层 A 组 / 历史依赖 B 组），
> 按文件域分为 4 个并行修复流，各流独立分支 + worktree 隔离（superpowers 多分支并行规则）。
> 基准分支：server-web（0.14.0）。完成后合并回 server-web 统一验证，升 0.15.0。

## 流分配（文件域不重叠，可安全并行）

| 流 | 分支 | worktree | 文件域 | 任务数 |
|---|---|---|---|---|
| 1 签到脚本 | fix/0.15-signin | yiban-auto-sign-wt-signin | scripts/signin.py、run.sh | 16 |
| 2 Web 后端 | fix/0.15-web | yiban-auto-sign-wt-web | web/app.py | 14 |
| 3 前端+合规 | fix/0.15-frontend | yiban-auto-sign-wt-frontend | 三个模板、.gitignore、docs | 10 |
| 4 CI/依赖 | fix/0.15-ci | yiban-auto-sign-wt-ci | requirements.txt、.github/workflows、.gitee-ci.yml | 6 |

## 流 1：scripts/signin.py + run.sh（16 项）

- [ ] F1 高危：`_load_accounts_from_file`（signin.py:268-272）排除 `rejected`（旧数据无 status 字段须通过：`status != pending and status != rejected and not deleted`）
- [ ] A1：代理日志脱敏（`urlsplit` 只记 scheme/host/port，去 userinfo，signin.py:388）
- [ ] A3：配置错误异常消息不带完整 dict（只带 phone 或脱敏 password，signin.py:245）
- [ ] A2：全账号窗口外 skip 时退出码区分（全 skip → exit 2；run.sh 状态文件仅在"有实际执行/失败"时写 SUCCESS）
- [ ] M1/A-M2：`first_round` 在循环开头（pop 后）无条件置 False（signin.py:816/821/852）
- [ ] M2/A-M3：重试间隔固定基线 `wait = remaining + uniform(0, RETRY_GAP_MAX)`（signin.py:848-849）
- [ ] A-M1：通知去重——仅最终放弃时通知（去掉 attempt_signin 内逐次通知）
- [ ] A-M4：`resp.headers.get("Location", "")` + 可诊断错误（signin.py:503）
- [ ] A-M5：WAF 关键词入 `RISK_FAIL_KEYWORDS`；所有 `resp.json()` 前先 `is_waf_blocked(resp.text)`（signin.py:422/468/674/738）
- [ ] A-M6 + js2py 替换：`_solve_ydclearance` 改纯正则/字符串解析（cookie 值 + location 提取，不做 eval_js），删除 `from js2py import eval_js`；触发判定改特征（Set-Cookie/JS 特征）而非 `len>10`
- [ ] A-L1：verify_request 的 Location 令牌进异常消息 → 只留 host/path
- [ ] A-L3：run.sh `.env` 加载改 `set -a; . .env; set +a`（替换 xargs）
- [ ] A-L4：`"已签到"/"今日无需签到"` 精确匹配改包含匹配
- [ ] A-L5：窗口 Range 缺失时视为 skip
- [ ] A-L8：删除 `self.session.keep_alive` 死代码；密码 >117 字节校验；响应 dump 截断 1500→摘要；错误消息换行转义
- [ ] 功能 L3：状态文件以尝试开始时刻日期命名（跨午夜边界）；功能 L13：非签到日不记 ✅（skip 语义）

## 流 2：web/app.py（14 项）

- [ ] F3 高危：批量 unset_admin 循环内动态重算 admins；批量 delete 加"至少保留 1 管理员"检查（app.py:1486-1498）
- [ ] 功能 M3：编辑接口保留 deleted 字段（update 不清除；用户端 PUT 拒绝已 deleted 账号）
- [ ] 功能 M4：restore（单条+批量）检查 owner 无其他未删除账号
- [ ] 功能 M5 后端：api_signin 校验 phone 为可签到账号（active 且非 deleted）
- [ ] 功能 M8：改密接口失败计数与登录一致（设置 lock_until + 达阈值锁定）
- [ ] 功能 M9：软删除占用规则统一——admin add 的"已有一个账号"检查排除 deleted（与用户端一致）；手机号唯一仍含 deleted（文档化 7 天占用）
- [ ] 功能 L1：软删除超期按秒比较（`>= 7*86400`）；deleted_at 缺失按文件 mtime 兜底
- [ ] 功能 L2：排队 active 排除 deleted
- [ ] 功能 L4：日历异常文件名 `result.setdefault`（防 500）
- [ ] 功能 L5：上移/下移 `int(dir)` 捕获异常
- [ ] 功能 L6 后端：识别码清空语义（收到 `__clear__` 时删除 phone_code）
- [ ] 功能 L7：accounts.json 解析失败时拒绝写操作（load 失败标记 + save 前检查并告警）
- [ ] 功能 L10：注册管理员 mine 只显示 owner==email 的账号（内置管理员显示全部 admin 名下）
- [ ] 功能 L12：pending_count 排除 deleted；批量 approve 排除 deleted
- [ ] 功能 L14：`has_active`（正式用户判定）仅 active 不算 rejected
- [ ] 安全 M3：密码策略升级——下限 10 位且含两类字符（注册/改密/添加/重置全路径）；内置管理员口令改哈希存储（.env 存 YIBAN_ADMIN_PASSWORD_HASH，登录走 check_password_hash，旧明文兼容过渡）；scrypt 参数 `scrypt:65536:8:1`
- [ ] 安全 M1：after_request 加安全头（X-Content-Type-Options: nosniff / X-Frame-Options: DENY / CSP default-src 'self' + script-src/style-src 'self' 'unsafe-inline' + connect-src 'self' + frame-ancestors 'none' / Referrer-Policy: no-referrer）
- [ ] 安全 M6：改密成功时轮换 SECRET_KEY（secrets.token_hex(32) 重写 .env）

## 流 3：前端 + 合规 + 文档（10 项）

- [ ] F2 高危：user.html 软删除死路——deleted 卡片提供"移除记录"入口（调 DELETE /api/my-accounts/<i>）或仅剩 deleted 时仍显示提交表单
- [ ] 功能 M6：user.html:103 / index.html:508 编辑表单密码 `required` 动态移除（编辑模式）
- [ ] 功能 L6 前端：识别码输入框"清除已配置识别码"交互（发送 `__clear__`）
- [ ] 功能 L16：日历 calState 键从槽位 `u-<i>`/`mine-<i>` 改 phone（防排序变化后翻页错位）——注意与 0.14.0 脱敏键并存，用 phone 即可（DOM id 仍用索引键）
- [ ] 功能 M5 前端：手动签到下拉只列可签到账号（active 且非 deleted）
- [ ] 安全 M7：login.html 用户协议文案修正（第 4 条密码存储改为事实描述："加密形式存储于服务器本地，仅用于自动签到执行，任何人无法在界面查看明文；网站登录密码为不可逆哈希"）+ 补充数据保留期限、删除权/导出权、未成年条款
- [ ] B1：`服务器访问信息.md` 加入 .gitignore（并建议内容脱敏）
- [ ] B7：新建 docs/web-console/VENDOR.md 记录 tailwind.js 3.4.17 来源 URL + sha256sum、MiSans 字体来源
- [ ] 安全 M4 文档：部署文档补充 umask 077 / 数据文件权限 / 备份策略（每日 tar + 异机加密副本 + 保留 30 天）
- [ ] 功能 L9/L8/L17：并发编辑与索引漂移——文档化风险与缓解（乐观锁留后续）

## 流 4：CI/依赖（6 项）

- [ ] B4：requirements.txt 下限修正——`werkzeug>=3.1.3`、`requests>=2.32.0`、`urllib3>=2.2.2`、`pycryptodome>=3.19.1`（js2py 由流 1 移除代码后同步删除依赖项）
- [ ] B6：生成锁定文件（pip-compile 或精确 pin `==`）；CI 加 `pip-audit -r requirements.txt` 步骤；新建 `.github/dependabot.yml`（pip + github-actions）
- [ ] A-M7：keepalive action 固定 commit SHA；移除或最小化 `actions: write` 权限
- [ ] A-M8：signin job 加 `permissions: contents: read`
- [ ] A-L8 延伸：`.gitee-ci.yml` 死配置处理（删除或补齐 env）
- [ ] 协调：与流 1 的 js2py 移除同步确认 requirements 中 js2py 行删除

## 明确不做（已确认决策）
- 易班密码 AES-GCM 加密存储（用户 2026-08-12 明确"本轮不做，留后续专项"）——但协议文案按事实修正（流 3）
- TLS/反代部署改造（S3——部署环境变更，需用户另行安排；流 3 只补文档）
- 历史泄露 filter-repo 重写双远端（B3——高风险操作，需用户单独确认后执行；本轮仅 gitignore 防护 B1）

## 合并与发布流程
1. 4 流完成 → 各自静态检查（ruff / python ast / node --check）
2. 依次 merge 回 server-web（按 1→4 顺序，解决跨流小冲突）
3. 全量浏览器回归（管理员/普通用户双视角 + 签到脚本 --check 模式）
4. 版本 0.15.0 + CHANGELOG（用户可见版按小版本风格）+ docs/CHANGELOG-full.md
5. 合 main → 推双远端 → 生产部署 → 验证
