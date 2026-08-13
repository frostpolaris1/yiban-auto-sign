# 对抗审查修复实施计划（3 组，0.9.5 → 0.9.7）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 2026-08-12 对抗性审查（docs/adversarial-review-20260812.md）发现的漏洞，在 `fix/web-security` 分支分 3 组完成，每组独立版本号、独立报告、独立 commit。

**Architecture:** 修复集中在 `web/app.py`（后端：手机号校验/CSRF 同源校验/操作级锁/登录锁定键/内存上限/日志倒读/版本吊销）与 `web/templates/index.html`、`user.html`（前端：动态值全部改为 data 属性 + 事件处理从 dataset 读取，杜绝 JS 字符串拼接注入）。组间互不依赖，每组一个 commit。

**Tech Stack:** Flask 3.1 / Python 3.12（本地 py launcher 3.14 亦可）/ 原生 JS + Tailwind

## Global Constraints

- 版本号：每组修复 +0.0.1（0.9.5 → 0.9.6 → 0.9.7），**大版本永不升 1.0**（用户明确许可 0.10.x 序列）；`web/app.py` 的 `APP_VERSION` 与根 `CHANGELOG.md` 同步更新
- 提交信息格式：`fix(0.9.x): <组名>——<要点>`
- 每组结束必须：代码 → 本地验证 → 更新 APP_VERSION + CHANGELOG → 写 `docs/fix-report-0.9.x.md` → 单 commit
- 手机号规则：中国 11 位 `1` 开头（`^1\d{10}$`），所有新增/修改账号统一校验
- ruff 通过：`py -m ruff check web/ && py -m ruff format web/`（配置见 pyproject.toml，忽略 E501 等）
- 前端验证：提取 `<script>` 内容跑 `node --check`；后端验证：本地起服务 + Python urllib（Git Bash curl 中文会损坏）
- 本地测试配置与 demo 数据隔离：用 `demo-log/test-*.json` 临时文件，测完删除
- `main` 与 `server-web` 双分支最终同步（0.9.3 教训：版本号须双分支一致）
- 未跟踪文件「服务器访问信息.md」与 `demo-log/` 不提交

---

### Task 1: 组1 —— Web 安全面（0.9.5：C1 存储型 XSS + I1 登录 CSRF + I7 esc JS 上下文）

**Files:**
- Modify: `web/app.py`（PHONE_RE 常量 + validate_account 手机号校验 + check_csrf 同源校验）
- Modify: `web/templates/index.html`（accountRow 菜单、renderCalendar 日历按钮/格子、renderMine 的 cal-wrap id）
- Modify: `web/templates/user.html`（renderList 的 cal-wrap id、renderCalendar 日历按钮/格子）
- Modify: `web/templates/index.html`（userRow 菜单改 data 属性）
- Create: `docs/fix-report-0.9.5.md`

**Interfaces:**
- Consumes: 审查报告发现 C1（phone 无格式校验 + `onclick="...doSignin('${a.phone}')..."` 未转义拼接）、I1（check_csrf 豁免 /api/login、/api/register）、I7（userRow 的 `'${esc(u.email)}'` 在 JS 字符串上下文，HTML 属性解码后 esc 失效）
- Produces: `PHONE_RE`、`_is_same_origin()`、前端 `accountRowMenu`/`userRowMenu`/`calShift(btn, delta)`/`calLoadLog(btn, date)`（后两个签名变化，Task 3 不依赖）

- [ ] **Step 1: 后端手机号格式校验（C1 根源）**

`web/app.py` 在 `EMAIL_RE`（第 71 行）后新增：

```python
# 手机号格式（易班登录账号为中国 11 位手机号；恶意字符可注入前端事件与日志）
PHONE_RE = re.compile(r"^1\d{10}$")
```

`validate_account`（约 256-270 行）在 `if not phone:` 后新增：

```python
if not PHONE_RE.match(phone):
    return "手机号格式不正确（应为 1 开头的 11 位数字）", None
```

- [ ] **Step 2: 登录/注册 CSRF 同源校验（I1）**

`web/app.py` 新增（放在 `check_csrf` 前）：

```python
def _is_same_origin():
    """登录/注册等未登录写接口的同源校验：跨站表单提交的 POST 必然携带 Origin 头。
    浏览器同源 fetch POST 也携带 Origin；无 Origin 的请求（同站导航、curl）放行。"""
    origin = request.headers.get("Origin")
    if not origin:
        return True
    from urllib.parse import urlparse

    try:
        o = urlparse(origin)
    except ValueError:
        return False
    return (o.scheme, o.netloc) == (request.scheme, request.host)
```

`check_csrf` 中把

```python
if request.path in ("/api/login", "/api/register"):
    return
```

替换为：

```python
if request.path in ("/api/login", "/api/register"):
    # 未登录态无 session token：用同源校验阻断跨站登录/注册 CSRF
    if not _is_same_origin():
        logger.warning(
            "跨站登录/注册被拒绝: ip=%s path=%s origin=%s",
            request.remote_addr,
            request.path,
            request.headers.get("Origin"),
        )
        return jsonify({"error": "请求来源校验失败"}), 403
    return
```

- [ ] **Step 3: 前端动态值改 data 属性（C1 注入点 + I7）**

`web/templates/index.html`：

(a) `accountRow`（约 701-725 行）的 ⋯ 按钮改为：

```html
<button title="更多操作" data-phone="${esc(a.phone)}" onclick="accountRowMenu(event, this, ${a.index})"
        class="px-3 py-1.5 rounded-lg text-xl leading-none text-zinc-500 dark:text-zinc-400 hover:bg-zinc-100 dark:hover:bg-zinc-700 hover:text-zinc-700 dark:hover:text-zinc-200 transition-colors duration-150">⋯</button>
```

新增函数（放在 `accountRow` 后）：

```javascript
function accountRowMenu(evt, btn, idx) {
  openRowMenu(evt, [
    {label: '上移', cls: 'text-zinc-600 dark:text-zinc-300', fn: () => moveAccount(idx, -1)},
    {label: '下移', cls: 'text-zinc-600 dark:text-zinc-300', fn: () => moveAccount(idx, 1)},
    {label: '手动签到', cls: 'text-blue-600 dark:text-blue-400', fn: () => doSignin(btn.dataset.phone)},
    {label: '编辑', cls: 'text-zinc-600 dark:text-zinc-300', fn: () => openForm(idx)},
    {label: '删除', cls: 'text-red-600 dark:text-red-400', fn: () => deleteAccount(idx)},
  ]);
}
```

(b) `userRow`（约 1117-1140 行）的 ⋯ 按钮改为：

```html
<button title="更多操作" data-email="${esc(u.email)}" data-role="${u.role}" onclick="userRowMenu(event, this)"
        class="px-3 py-1.5 rounded-lg text-xl leading-none text-zinc-500 dark:text-zinc-400 hover:bg-zinc-100 dark:hover:bg-zinc-700 hover:text-zinc-700 dark:hover:text-zinc-200 transition-colors duration-150">⋯</button>
```

新增函数（放在 `userRow` 后）：

```javascript
function userRowMenu(evt, btn) {
  const email = btn.dataset.email;
  const isAdmin = btn.dataset.role === 'admin';
  openRowMenu(evt, [
    {label: isAdmin ? '取消管理员' : '设为管理员', cls: isAdmin ? 'text-zinc-600 dark:text-zinc-300' : 'text-blue-600 dark:text-blue-400', fn: () => setUserRole(email, isAdmin ? 'user' : 'admin')},
    {label: '重置密码', cls: 'text-zinc-600 dark:text-zinc-300', fn: () => resetUserPassword(email)},
    {label: '清空账号', cls: 'text-amber-600 dark:text-amber-400', fn: () => deleteUserAccounts(email)},
    {label: '删除用户', cls: 'text-red-600 dark:text-red-400', fn: () => deleteUserFull(email)},
  ]);
}
```

(c) `renderCalendar`（index.html 约 1261-1309 行）与 `user.html`（约 264-312 行）——两个文件同样改动：

- `calShift` 按钮：`onclick="calShift('${phone}', -1)"` → `onclick="calShift(this, -1)" data-phone="${esc(phone)}"`（+1 同理）
- 日期格子：`onclick="calLoadLog('${phone}', '${date}')"` → `onclick="calLoadLog(this, '${date}')" data-phone="${esc(phone)}"`
- 函数签名与实现（两个文件）：

```javascript
function calShift(btn, delta) {
  const phone = btn.dataset.phone;
  const st = calState[phone] || (calState[phone] = (() => { const d = new Date(); return { year: d.getFullYear(), month: d.getMonth() + 1 }; })());
  st.month += delta;
  if (st.month < 1) { st.month = 12; st.year--; }
  if (st.month > 12) { st.month = 1; st.year++; }
  renderCalendar(phone);
}
```

```javascript
function calLoadLog(btn, date) {
  const phone = btn.dataset.phone;
  const box = $('cal-log-' + phone);
  ...（原逻辑不变）
}
```

(d) `cal-wrap` id 补 esc（id 属性中防 `"` 破坏）：`renderMine`（index.html 约 1218/1241 行）与 `renderList`（user.html 约 244 行）的 `id="cal-wrap-${a.phone}"` → `id="cal-wrap-${esc(a.phone)}"`。`renderCalendar` 内部 `$('cal-wrap-' + phone)` 不变（DOM 解析后的 id 与原始值一致）。

- [ ] **Step 4: 验证**

起服务（临时配置）：

```bash
cd "D:\code\WorkBuddy\橙星\yiban-auto-sign" && py -m web --port 17893 --config demo-log/test-accounts.json --log demo-log/test-sign.log --env demo-log/test.env --users demo-log/test-users.json
```

Python 验证脚本（urllib，放临时文件运行）：

```python
import json, urllib.request

BASE = "http://127.0.0.1:17893"

def post(path, body, headers=None):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

# C1: 恶意手机号必须被拒（400）
st, d = post("/api/register", {"email": "a1@test.com", "password": "pass123"})
assert st == 200, d
st, d = post("/api/login", {"username": "a1@test.com", "password": "pass123"})
assert st == 200, d
st, d = post("/api/my-accounts", {"name": "x", "phone": "13800138000');alert(1)//",
                                  "password": "pw123456", "phone_model": "", "phone_code": ""})
assert st == 400, (st, d)  # 手机号格式校验生效
st, d = post("/api/my-accounts", {"name": "x", "phone": "13800138000",
                                  "password": "pw123456", "phone_model": "", "phone_code": ""})
assert st == 200, d  # 正常手机号可提交

# I1: 跨站 Origin 登录被拒，同源/无 Origin 放行
st, d = post("/api/login", {"username": "a1@test.com", "password": "pass123"},
             headers={"Origin": "https://evil.example.com"})
assert st == 403, (st, d)
st, d = post("/api/login", {"username": "a1@test.com", "password": "pass123"})
assert st == 200, (st, d)
st, d = post("/api/login", {"username": "a1@test.com", "password": "pass123"},
             headers={"Origin": "http://127.0.0.1:17893"})
assert st == 200, (st, d)

# 已登录写接口仍需 CSRF token（回归）
st, d = post("/api/logout", {})
assert st == 403, (st, d)  # 无 token → 403

print("Task 1 后端验证全部通过")
```

模板验证：

```bash
grep -n "doSignin('" web/templates/index.html            # 必须无输出
grep -n "calShift('" web/templates/*.html               # 必须无输出
grep -n "calLoadLog('" web/templates/*.html             # 必须无输出
grep -n "setUserRole('" web/templates/index.html        # 必须无输出
```

提取每个 `<script>` 内容 `node --check` 验证 JS 语法（Python 提取脚本）：

```python
import re, subprocess, tempfile
for f in ["web/templates/index.html", "web/templates/user.html", "web/templates/login.html"]:
    html = open(f, encoding="utf-8").read()
    for i, m in enumerate(re.finditer(r"<script>(.*?)</script>", html, re.S)):
        if "src=" in m.group(0): continue
        p = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
        p.write(m.group(1)); p.close()
        r = subprocess.run(["node", "--check", p.name], capture_output=True, text=True)
        assert r.returncode == 0, (f, i, r.stderr)
print("JS 语法全部通过")
```

ruff：`py -m ruff check web/ && py -m ruff format web/`

- [ ] **Step 5: 版本号 + CHANGELOG + 报告**

`web/app.py` `APP_VERSION = "0.9.5"`。`CHANGELOG.md` 顶部插入：

```markdown
## v0.9.5（2026-08-12）
- [安全] 修复存储型 XSS：手机号未校验格式且未转义拼接进前端按钮事件（攻击者可提交恶意手机号，管理员打开账号管理页即被注入脚本、以管理员身份操作）
- [安全] 修复登录 CSRF：登录/注册接口增加同源校验，防止受害者被跨站登录进攻击者账号后泄露易班账号密码
- [安全] 前端动态数据统一改为 data 属性传递，消除事件属性中的字符串拼接注入隐患
- 手机号提交校验：必须为 1 开头的 11 位数字
```

创建 `docs/fix-report-0.9.5.md`：修复项（C1/I1/I7）逐条：漏洞描述、攻击路径、修复方式、验证结果（命令输出摘要）。

- [ ] **Step 6: 验证 + 提交**

```bash
cd "D:\code\WorkBuddy\橙星\yiban-auto-sign"
git add web/app.py web/templates/index.html web/templates/user.html CHANGELOG.md docs/fix-report-0.9.5.md
git commit -m "fix(0.9.5): Web安全组——存储型XSS/登录CSRF/事件拼接注入（对抗审查组1）"
```

---

### Task 2: 组2 —— 并发与数据安全（0.9.6：I2 竞态丢数据 + I4 NAT 锁定误伤）

**Files:**
- Modify: `web/app.py`（_file_lock → RLock、全部读改写 handler 加操作级锁、_my_account_indices 快照化、登录失败键改 (IP,用户名)）
- Create: `docs/fix-report-0.9.6.md`

**Interfaces:**
- Consumes: Task 1 的 `PHONE_RE`
- Produces: `_my_account_indices_of(accounts)`（Task 3 不依赖）；`_login_fails` 键类型从 `ip` 变为 `(ip, username_lower)` 元组

- [ ] **Step 1: 操作级锁（I2）**

`web/app.py` 第 183 行 `_file_lock = threading.Lock()` → `_file_lock = threading.RLock()`（允许 handler 层重入 load/save 内部锁）。

新增快照版索引函数（`_my_account_indices` 前）：

```python
def _my_account_indices_of(accounts):
    """按账号列表快照计算当前用户的账号下标（锁内调用，避免重复读文件）。"""
    email = session.get("username", "").lower()
    if _current_role() == "admin":
        return [i for i, a in enumerate(accounts) if a.get("owner") in ("admin", email)]
    return [i for i, a in enumerate(accounts) if a.get("owner") == email]
```

`_my_account_indices` 改为：

```python
def _my_account_indices():
    return _my_account_indices_of(load_accounts())
```

以下 handler 的"读 → 检查 → 改 → 写"序列用 `with _file_lock:` 包住（函数体整体缩进）：

- `api_register`（邮箱唯一 + append）
- `api_me_password`（注册用户分支的读改写）
- `api_account_add`（手机号唯一 + 每人限 1 + 自动注册）
- `api_account_update` / `api_account_delete` / `api_account_review` / `api_account_move`
- `api_my_account_add` / `api_my_account_update` / `api_my_account_delete`（用 `_my_account_indices_of(accounts)` 替换 `_my_account_indices()`，锁内同一快照）
- `api_user_role` / `api_user_password` / `api_user_delete`

- [ ] **Step 2: 登录锁定按 (IP, 用户名)（I4）**

`api_login`（约 490-544 行）与 `api_me_password`（约 587-626 行）中所有 `_login_fails.get(ip, (0, 0))` / `_login_fails[ip]` / `_login_fails.pop(ip, None)` 改为键 `(ip, username.strip().lower())`（api_me_password 用 `session.get("username", "")`）。例如：

```python
fail_key = (ip, username.strip().lower())
fails, lock_until = _login_fails.get(fail_key, (0, 0))
if now < lock_until:
    ...
...
if role:
    _login_fails.pop(fail_key, None)
...
fails += 1
if fails >= LOGIN_MAX_FAILS:
    _login_fails[fail_key] = (0, now + LOGIN_LOCK_SECONDS)
...
_login_fails[fail_key] = (fails, 0)
```

- [ ] **Step 3: 验证**

重启 17893 服务（沿用 demo-log/test-* 配置），Python 脚本：

```python
import json, threading, urllib.request, urllib.error

BASE = "http://127.0.0.1:17893"

def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

# I2: 同一用户并发提交账号 → 只成功 1 个
st, d = post("/api/register", {"email": "conc@test.com", "password": "pass123"})
assert st == 200, d
st, d = post("/api/login", {"username": "conc@test.com", "password": "pass123"})
assert st == 200, d

# 并发提交前先登录拿不到 CSRF token——从 /api/me 取
import urllib.request as u
me = json.loads(u.urlopen(BASE + "/api/me").read())
token = me["csrf_token"]

def submit(phone):
    req = urllib.request.Request(BASE + "/api/my-accounts",
        data=json.dumps({"name": "x", "phone": phone, "password": "pw123456",
                         "phone_model": "", "phone_code": ""}).encode(),
        headers={"Content-Type": "application/json", "X-CSRF-Token": token})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code

results = []
th = [threading.Thread(target=lambda p=13800138001+i: results.append((p, submit(str(p)))) for i in range(8)]
for t in th: t.start()
for t in th: t.join()
ok = [r for r in results if r[1] == 200]
assert len(ok) == 1, f"并发绕过单账号限制: {results}"  # 只允许 1 个成功

# I4: 同 IP 不同用户名各失败 5 次 → 互不锁定
for u in ["noise1@test.com", "noise2@test.com"]:
    for _ in range(5):
        post("/api/login", {"username": u, "password": "wrong"})
st, d = post("/api/login", {"username": "conc@test.com", "password": "pass123"})
assert st == 200, (st, d)  # 他人用户名刷失败不影响本用户
st, d = post("/api/login", {"username": "noise1@test.com", "password": "right1"})
assert st == 429, (st, d)  # 而 noise1 自己已被锁

print("Task 2 验证全部通过")
```

ruff：`py -m ruff check web/ && py -m ruff format web/`

- [ ] **Step 4: 版本号 + CHANGELOG + 报告 + 提交**

`APP_VERSION = "0.9.6"`。CHANGELOG 顶部插入：

```markdown
## v0.9.6（2026-08-12）
- [安全] 修复并发竞态：账号提交/注册/管理操作的「检查+写入」加操作级锁，杜绝绕过单账号限制与并发覆盖丢数据
- [安全] 登录失败锁定改为按「IP+用户名」组合计数：同一网络下的用户不再因他人爆破尝试被连带锁定
```

创建 `docs/fix-report-0.9.6.md`（同 Task 1 格式）。提交：

```bash
git add web/app.py CHANGELOG.md docs/fix-report-0.9.6.md
git commit -m "fix(0.9.6): 并发数据组——操作级锁防竞态丢数据/登录锁定按IP+用户名（对抗审查组2）"
```

---

### Task 3: 组3 —— 可用性与清理（0.9.7：I3 内存无界 + I5 int 异常 + I6 日志全读 + Minor）

**Files:**
- Modify: `web/app.py`（内存上限、settings int、日志 tail、pw_version 会话吊销、log_file 脱敏、内置管理员显示、fsync、长度上限、日期校验）
- Modify: `web/templates/index.html`（无改动；log_file 显示已为 basename 兼容）
- Create: `docs/fix-report-0.9.7.md`

**Interfaces:**
- Consumes: Task 1/2 的全部产出
- Produces: `_tail_lines(path)`、`_ip_store_trim(store, max_age)`、users.json 新字段 `pw_version`（注册/自动注册写入 1，改密递增）

- [ ] **Step 1: 内存上限（I3）**

新增（`_rate_limits` 等定义附近）：

```python
_IP_STORE_LIMIT = 10000  # 各 IP 计数 dict 的条目上限（防公网扫描器多 IP 打爆内存）
_IP_STORE_MAX_AGE = 3600  # 条目最长保留（秒）


def _ip_store_trim(store, max_age):
    """IP 计数 dict 超限时清理过期条目：仅当长度超上限才遍历，避免每请求开销。"""
    if len(store) <= _IP_STORE_LIMIT:
        return
    now = time.time()
    stale = [k for k, v in store.items() if now - v[-1] > max_age]
    for k in stale:
        store.pop(k, None)
```

调用点：
- `rate_limit`（before_request）：`_ip_store_trim(_rate_limits, RATE_WINDOW)`
- `api_login`：`_ip_store_trim(_login_fails, LOGIN_LOCK_SECONDS + 600)`——**先改 `_login_fails` 值为三元组** `(count, lock_until, last_ts)`（失败时 `last_ts = now`），读取处 `fails, lock_until, _ = _login_fails.get(fail_key, (0, 0, 0))`，写入处同步三元组
- `api_register`：`_ip_store_trim(_register_limits, REGISTER_WINDOW)`
- `login_page`：`_ip_store_trim(_login_loop, 60)`

- [ ] **Step 2: settings int 异常与上限（I5）**

`api_settings_save` 改为：

```python
data = request.get_json(silent=True) or {}
try:
    start = int(data.get("start_delay_max", 0))
    gap = int(data.get("gap_max", 0))
except (TypeError, ValueError):
    return jsonify({"error": "延迟秒数必须是整数"}), 400
start = min(max(start, 0), 3600)
gap = min(max(gap, 0), 3600)
write_env_int(ENV_FILE, "YIBAN_START_DELAY_MAX", start)
write_env_int(ENV_FILE, "YIBAN_ACCOUNT_GAP_MAX", gap)
logger.info("更新随机延迟: 启动=%s 间隔=%s", start, gap)
return jsonify({"ok": True, "msg": "设置已保存（cron 下次触发自动生效）"})
```

- [ ] **Step 3: 日志倒读（I6）**

新增（`parse_sign_log` 前）：

```python
_LOG_TAIL_BYTES = 2 * 1024 * 1024  # 日志倒读上限 2MB（约 2 万行）


def _tail_lines(path, max_bytes=_LOG_TAIL_BYTES):
    """从文件尾部读取最多 max_bytes 的完整文本行：大日志避免整读入内存。"""
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    try:
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()  # 丢弃首个不完整行
                raw = f.read()
            else:
                raw = f.read()
    except OSError:
        return []
    return raw.decode("utf-8", errors="replace").splitlines()
```

`parse_sign_log`（约 87-108 行）改为遍历 `_tail_lines(path)`，去掉外层 try/except OSError（_tail_lines 内部已处理）；`api_my_logs`（约 930-943 行）的 `reversed(f.readlines())` 改为 `reversed(_tail_lines(LOG_FILE))`，外层 try/except OSError 移除。

- [ ] **Step 4: Minor 清理**

(a) M1 日期校验（`api_my_logs` 约 923-924 行）：

```python
if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
    return jsonify({"error": "日期格式不正确，应为 YYYY-MM-DD"}), 400
```

(b) M2 重置密码吊销会话（pw_version）：
- `api_register` 与 `api_account_add` 自动注册的 users.append 字典加 `"pw_version": 1`
- `api_me_password`（注册用户分支）`u["password_hash"] = ...` 后加 `u["pw_version"] = u.get("pw_version", 1) + 1`
- `api_user_password`（管理员重置）`target["password_hash"] = ...` 后加 `target["pw_version"] = target.get("pw_version", 1) + 1`
- `api_login` 普通用户分支成功处加 `session["pw_version"] = u.get("pw_version", 1)`
- `_current_role` 传 `session.get("pw_version")` 给 `_effective_role`；`_effective_role` 签名加 `pw_version=None`，users.json 命中时：

```python
if "pw_version" in u and pw_version != u.get("pw_version", 1):
    return None  # 密码已重置/被重置 → 旧会话失效
```

（旧数据无 pw_version 字段不校验，兼容存量会话）

(c) M3 log_file 脱敏：`api_logs` 返回 `"log_file": os.path.basename(LOG_FILE)`（前端显示 `文件：<span id="log-file">` 默认值本为 basename 风格，兼容）。

(d) M4 内置管理员显示原样：新增

```python
def _builtin_admin_display():
    """内置管理员显示名（保留 .env 原始大小写，仅用于界面展示）。"""
    env = read_env(ENV_FILE)
    return env.get("YIBAN_ADMIN_USER", "").strip() or "admin"
```

`api_users` 返回 `"builtin_admin": _builtin_admin_display()`。

(e) M5 fsync（`_atomic_write`）：

```python
with open(tmp, "w", encoding="utf-8") as f:
    f.write(content)
    f.flush()
    os.fsync(f.fileno())
```

(f) M8 字段长度上限（`validate_account`）：

```python
name = str(data.get("name", "")).strip()
if len(name) > 50:
    return "名称过长（最多 50 字）", None
phone_model = str(data.get("phone_model", "")).strip()
if len(phone_model) > 50:
    return "设备型号过长（最多 50 字）", None
phone_code = str(data.get("phone_code", "")).strip()
if len(phone_code) > 128:
    return "设备识别码过长", None
```

（g）M6/M7/M9 不做，报告注明：M6 `--debug` 保留（开发调试用，生产严禁开启）；M7 手动签到子进程并发需 signin.py 侧单独评估（本轮范围外）；M9 邮箱验证需邮件基础设施（已知设计）。

- [ ] **Step 5: 验证**

重启服务后 Python 脚本：

```python
import json, urllib.request, urllib.error

BASE = "http://127.0.0.1:17893"

def req(path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data,
        headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(r) as x:
            return x.status, json.loads(x.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

# I5: 非法整数 → 400
st, d = req("/api/login", {"username": "a1@test.com", "password": "pass123"})
assert st == 200, d
me = req("/api/me")[1]
tok = me["csrf_token"]
st, d = req("/api/settings", {"start_delay_max": "abc", "gap_max": 5}, {"X-CSRF-Token": tok})
assert st == 400, (st, d)
st, d = req("/api/settings", {"start_delay_max": 99999999, "gap_max": 5}, {"X-CSRF-Token": tok})
assert st == 200 and d["ok"], d  # 超上限被截断不报错

# M2: 重置密码后旧会话失效
st, d = req("/api/users/a1@test.com/password", {"password": "newpass1"}, {"X-CSRF-Token": tok})
assert st == 200, d
st, d = req("/api/me")
assert st == 401, (st, d)  # 旧会话已被吊销

# I6/M1: 日期与日志接口正常
st, d = req("/api/my-logs?date=2026-1-1-1")
assert st == 400, (st, d)
st, d = req("/api/my-logs?date=2026-08-01")
assert st == 401, (st, d)  # 已登出

print("Task 3 验证全部通过")
```

ruff：`py -m ruff check web/ && py -m ruff format web/`；`node --check` 三模板（Task 1 脚本复用）。

- [ ] **Step 6: 版本号 + CHANGELOG + 报告 + 提交**

`APP_VERSION = "0.9.7"`。CHANGELOG 顶部插入：

```markdown
## v0.9.7（2026-08-12）
- [安全] 修复内存无界增长：IP 计数表（限速/登录失败/注册）增加条目上限，公网扫描器无法耗尽服务器内存
- [安全] 重置/修改密码后旧登录会话自动失效
- 修复设置接口对非法输入报 500 的问题，延迟秒数限制在 0~3600
- 大日志文件改为从尾部读取（最多 2MB），避免每次请求整文件载入内存
- 日志接口不再返回服务器上的完整日志路径
- 账号名称/设备型号/识别码增加长度上限；日期参数校验收紧
```

创建 `docs/fix-report-0.9.7.md`。提交：

```bash
git add web/app.py CHANGELOG.md docs/fix-report-0.9.7.md
git commit -m "fix(0.9.7): 可用性清理组——内存上限/设置容错/日志倒读/会话吊销等（对抗审查组3）"
```

---

### Task 4: 收尾合并（三组全部完成后）

**Files:**
- 无代码改动

- [ ] **Step 1: 全量回归**

三组验证脚本按顺序重跑一遍（0.9.5 脚本需适配：`/api/me` 拿 token 后操作）；本地浏览器冒烟（IAB 打开 17893 登录 admin 走一遍账号管理/用户管理/设置页）。

- [ ] **Step 2: 合并**

```bash
git switch server-web
git merge --no-ff fix/web-security -m "merge: 对抗审查修复（0.9.5~0.9.7，3 组）"
git switch main
git merge --no-ff server-web -m "merge: server-web 对抗审查修复同步到 main"
```

- [ ] **Step 3: 推送双远端**

```bash
git push origin server-web main
git push gitee server-web main
```

- [ ] **Step 4: 询问部署**

向用户确认是否按既定节奏部署生产（服务器 `git pull gitee server-web` + `systemctl restart yiban-web`），部署属于生产敏感操作，必须用户确认。
