# 对抗审查修复报告 · 组1（0.9.5）—— Web 安全面

日期：2026-08-12
分支：`fix/web-security`（基于 server-web 8ccd700）
审查依据：`docs/adversarial-review-20260812.md` 的 C1 / I1 / I7

## 修复项

### C1（Critical）存储型 XSS：手机号注入管理端

**漏洞描述**：`validate_account` 对手机号只做 strip 不做格式校验，普通用户可提交含引号的恶意手机号；管理端 `index.html` 的 `doSignin('${a.phone}')` 将其未转义拼进 onclick 的 JS 字符串，管理员打开账号管理页即触发注入，攻击者可读取全局 `csrfToken` 并以管理员身份调用任意 API。

**攻击路径**：注册 → 提交恶意手机号 → 管理员查看账号列表 → JS 注入执行 → 接管管理员会话。

**修复**：
1. `web/app.py`：新增 `PHONE_RE = re.compile(r"^1\d{10}$")`，`validate_account` 拒绝非 11 位手机号（同时杜绝换行符等日志伪造向量）
2. `index.html`：账号行按钮改 `data-phone="${esc(a.phone)}"` + 新增 `accountRowMenu(event, this, idx)` 从 `btn.dataset.phone` 取值；日历翻页/日期格子同样改为 `calShift(this, ±1)` / `calLoadLog(this, date)` + data 属性（index.html 与 user.html 两处）
3. `cal-wrap` 容器 id 补 `esc()`（防 `"` 破坏 id 属性）

### I1（Important）登录 CSRF

**漏洞描述**：`check_csrf` 豁免 `/api/login`、`/api/register`，而 SameSite=Lax 挡不住 top-level 表单提交；攻击者网站可跨站把受害者登录进攻击者账号，受害者随后提交的易班账号（明文密码）将归属攻击者。

**修复**：`web/app.py` 新增 `_is_same_origin()`：登录/注册接口校验 `Origin` 头（跨站 POST 必带 Origin），非本机 scheme+host 一律 403；无 Origin（同站导航、curl）放行。日志记录被拒请求。

### I7（Important）`esc()` 用于 JS 字符串上下文

**漏洞描述**：`userRow` 把 `esc(u.email)` 拼进 onclick JS 字符串——HTML 属性解析会把 `&#39;` 解码回 `'`，当前仅靠 EMAIL_RE 字符集限制幸免，是定时炸弹。

**修复**：用户行按钮改 `data-email="${esc(u.email)}" data-role="${u.role}"` + 新增 `userRowMenu(event, this)` 从 dataset 读取。

## 附带清理

- `pyproject.toml`：ignore 增加 RUF001/RUF002/RUF003（中文全角标点为项目惯例，误报率高）；**基线验证**：改动前 ruff check 即有 434 个历史报错（0.9.3 引入配置时未真正跑通），本次一并清理 E741（变量名 `l`→`ln`）×2、SIM105（try-pass → contextlib.suppress），当前 `ruff check` 全绿

## 验证结果

| 验证项 | 方法 | 结果 |
|--------|------|------|
| 恶意手机号被拒 | urllib POST `13800138000');alert(1)//` | 400「手机号格式不正确」✓ |
| 正常手机号提交 | urllib POST `13800138000` | 200 ✓ |
| 跨站 Origin 登录 | POST /api/login + `Origin: https://evil.example.com` | 403 ✓ |
| 同源登录 | `Origin: http://127.0.0.1:17893` | 200 ✓ |
| 无 Origin 登录 | 裸 POST | 200 ✓ |
| 注册跨站 | POST /api/register + 恶意 Origin | 403 ✓ |
| CSRF 回归 | 无 token POST /api/logout | 403 ✓ |
| 无残留拼接 | grep `doSignin('` / `calShift('` / `calLoadLog('` / `setUserRole('` | 无输出 ✓ |
| JS 语法 | 三模板 `<script>` 提取 node --check | 通过 ✓ |
| 静态检查 | `py -m ruff check web/` + format | 全绿 ✓ |

## 影响面

- 账号提交/编辑（管理员 + 普通用户）手机号必须为 11 位数字——**存量非法手机号账号不受影响**（校验只作用于新增/修改请求）
- 登录/注册接口行为不变（浏览器场景均携带同源 Origin 或无 Origin）
