> [← 返回 README](../../README.md)

## 网页管理后台

```bash
# 1. 依赖（装进项目自带虚拟环境 .venv，与生产一致；上面已建过则跳过建环境那两行）
cd /opt/yiban-auto-sign
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock

# 2. 创建内置管理员（二选一）
#    ① 有浏览器：启动后首次打开站点，会自动进入 /setup 引导页；
#    ② 无浏览器（headless）：口令只走环境变量，不接受命令行参数。
YIBAN_ADMIN_PASSWORD='你的强口令' .venv/bin/python3 -m yiban.entry.cli init --username admin
#    ⚠️ 主管理员口令策略：至少 12 位，且包含大写字母 / 小写字母 / 数字 / 符号中的
#    至少三类。网页引导与 CLI init 使用同一套策略；写入的是 scrypt 哈希，明文不落盘。
#    首次登录后建议在「我的账号 → 修改主管理员密码」重设：修改会递增 PW_VERSION
#    （旧会话全部失效），并触发安全告警。

# 3. 启动（默认只监听回环；端口见 deploy/web.env 的 YIBAN_WEB_PORT，--host/--port 可改）
.venv/bin/python3 -m web
# 生产用 systemd + gunicorn 常驻（禁止 werkzeug dev server 公网直连）：
#   systemd 单元模板 web/deploy/yiban-web.service，nginx 示例 web/deploy/nginx.conf.example
#
# 🔑 **端口与线程数只有一处定义：`deploy/web.env`**。六个部署件（本单元的 ExecStart/
#   WorkingDirectory/ReadWritePaths、supervisord、nginx 两处 proxy_pass、Dockerfile、
#   compose 探活、`python -m web` 的 --port 缺省）都从这张表取；仓库里不再写死这两个值。
#   守卫：`tests/test_deploy_parameterization_gate.py`。
#
# 装单元前先备齐模板里引用的三样东西（缺任一样 systemd 会以 "Failed at step NAMESPACE"
# 或 EnvironmentFile 报错启动失败）：
#   useradd -r -s /usr/sbin/nologin yiban                     # 模板的 User=yiban（上面「服务器部署」第 1 步已建过则跳过）
#   install -d -m 0750 -o yiban -g yiban /etc/yiban           # EnvironmentFile 目录（必需；缺它 install 报错）
#   # 账号密钥在运行数据文件 data/.env（上面「服务器部署」第 4 步写入）。
#   # 取钥不猜格式：ensure_accounts_key 用 env_io.parse_env_file 读、decode_accounts_key
#   # 校验（BOM、大小写、值内空白、CRLF、NBSP 均与应用同口径）；缺键或坏值才生成，写回走
#   # env_io.write_env_key（折叠同名行，临时件 + os.replace，0600）。
#   test -f data/.env || { echo "缺 data/.env：先完成「服务器部署」第 4 步与 install.sh"; exit 1; }
#   chmod 600 data/.env                                      # 第 4 步 tee 建出的文件通常是 0644
#   .venv/bin/python3 -c 'import sys; from yiban.adapters.config import account_crypto; print("YIBAN_ACCOUNTS_KEY=" + account_crypto.ensure_accounts_key(sys.argv[1]).hex())' data/.env \
#     >/tmp/yiban-accounts-key \
#     || { rm -f /tmp/yiban-accounts-key; echo "data/.env 里没有合法的 YIBAN_ACCOUNTS_KEY"; exit 1; }
#   install -m 0640 -o root -g yiban /tmp/yiban-accounts-key /etc/yiban/accounts-key \
#     && rm -f /tmp/yiban-accounts-key                         # 模板的 EnvironmentFile：只放启动密钥这一行
#   ⚠️ 不要把整份 .env 拷成 EnvironmentFile：非路径键的读序是 .env 优先、
#   进程环境只补缺；四个路径键（DB_FILE/STATE_DIR/LOG_FILE）反过来
#   （进程环境优先）。另 YIBAN_ACCOUNTS_KEY 仍是环境变量档优先。整份拷贝
#   虽不再压住「系统设置」页的新值，但会把启动时刻的旧值留成第二份来源、随 .env
#   漂移。模板只放上面这一行；旧部署若已整份拷贝，收窄成只含这一行即可。
# ⚠️ 换目录部署：单元是**模板**，检出根由渲染命令注入（`YIBAN_APP_DIR="$(pwd)"`），
#   不再需要手工改 WorkingDirectory / ExecStart / ReadWritePaths。cron 表的
#   @YIBAN_APP_DIR@ 由 `deploy/prod/install.sh` 按检出根渲染。
#
# 安装与开机自启（照抄即可；先在检出根执行）：
#   set -a; . deploy/web.env; set +a        # 读端口/线程数（唯一值表）
#   export YIBAN_APP_DIR="$(pwd)"           # 检出根（标准部署即 /opt/yiban-auto-sign）
#   # 检出路径必须在白名单字符集内（绝对路径＋字母数字._/-）：空格/引号/;/$/` 等一律拒绝
#   [[ "$YIBAN_APP_DIR" =~ ^/[A-Za-z0-9._/-]+$ ]] || { echo "检出路径含不安全字符：$YIBAN_APP_DIR"; exit 1; }
#   # 白名单已排除 & 与 #；下面转义是第二层防护（sed 替换串里 & 表示匹配文本）
#   APP_DIR_SED=$(printf '%s' "$YIBAN_APP_DIR" | sed 's/[&#]/\\&/g')
#   sed -e "s#@YIBAN_APP_DIR@#$APP_DIR_SED#g" \
#       -e "s#@YIBAN_WEB_PORT@#$YIBAN_WEB_PORT#g" \
#       -e "s#@YIBAN_WEB_THREADS@#$YIBAN_WEB_THREADS#g" \
#       web/deploy/yiban-web.service | sudo tee /etc/systemd/system/yiban-web.service >/dev/null
#   systemctl daemon-reload && systemctl enable --now yiban-web
#   systemctl status yiban-web        # 启动失败看 journalctl -u yiban-web -n 50
```

> ⚠️ **Cookie Secure 必须与访问方式匹配**（systemd 模板默认 `YIBAN_COOKIE_SECURE=1`；开值 `1` / `true` / `yes` / `on` 均视为开，大小写与两侧空白不敏感）：
> 经 nginx HTTPS 反代访问时保持 `1`，让浏览器只经 HTTPS 回传会话 Cookie；**直连 HTTP**
> （`http://IP:17892`、`--host 0.0.0.0`）调试时必须删掉该行或设为 `0`，否则浏览器在明文
> HTTP 下不回传会话 Cookie，表现为登录后立刻显示未登录。不设该键时应用按请求自动判定：
> 满足以下任一条即给本次响应的 Cookie 加 Secure——① 请求本身走 HTTPS（应用自己终止 TLS）；
> ② 第一跳是回环可信代理、且带 `X-Forwarded-Proto: https`（详见 `.env.example` 的
> `YIBAN_COOKIE_SECURE` 说明）。

> ⚠️ **关掉系统的自动重启**：Debian/Ubuntu 的 `unattended-upgrades` 在自动升级后**默认会重启**
> （`Unattended-Upgrade::Automatic-Reboot` 上游缺省为 `true`）。本项目的签到由定时任务按固定时点
> 发起，重启落在窗口内会**静默漏签一轮**（不报错，只是少签）。请显式写死：新建
> `/etc/apt/apt.conf.d/51unattended-noreboot`，内容一行 `Unattended-Upgrade::Automatic-Reboot "false";`
> ——另起一个文件，别改 `50unattended-upgrades`（那是包提供的文件，改它升级时会被 `.dpkg-dist` 顶掉）。

浏览器访问 `https://你的域名`（经 nginx 反代）或 `http://127.0.0.1:17892`（本机调试）。**默认不监听全网卡**：确需直连局域网请显式 `.venv/bin/python3 -m web --host 0.0.0.0`（明文 HTTP 无防护，自担风险）。直连 HTTP 要能登录，`YIBAN_COOKIE_SECURE` 必须不为 1（`1` / `true` / `yes` / `on` 均视为开）——若你按模板或显式把它置了开值，请按上面的 Cookie 说明去掉或设为 0，否则浏览器不回传会话 Cookie、登录后拿不到会话。

管理员侧导航：**数据总览 / 签到日志 / 账号管理 / 用户管理 / 系统设置**（个人域为 **我的日历 / 我的账号**）。普通用户走邮箱注册，提交自己的易班账号（名称 + 手机号 + 密码 + 设备信息），管理员审核通过后参与每日自动签到；每人限一个账号，可自助注销（两次确认 + 密码验证，7 天内可登录撤销）。

安全设计：登录频率 10 次/60 秒、连续失败 10 次锁定 1 分钟（阈值与锁定时长见 `web/app.py` 的 `LOGIN_MAX_FAILS` 与 `web/security.py` 的 `LOGIN_LOCK_SECONDS`）+ 连续失败告警、CSRF 防护、密码 scrypt 哈希、会话 HttpOnly/SameSite、列表手机号/邮箱脱敏、密码明文永不下发前端。

> ⚠️ 无固定域名时建议在云服务商安全组仅放行常用 IP，并定期修改管理员密码。

<details>
<summary>🌐 部署形态：域名根 / 独立子域 / 主站子路径</summary>

### 部署形态

Web 应用**自动适配挂载前缀**，同一份代码可部署在三种位置，无需改代码：

| 部署形态 | 访问地址示例 |
|---|---|
| 域名根 | `https://yiban.example.com/` |
| 独立子域 | `https://sub.example.com/` |
| 主站子路径 | `https://example.com/tools/yiban-auto-sign/` |

**部署契约（务必遵守）**：反向代理把完整 URI【原样透传】给后端——nginx 的 `proxy_pass` 后**不要**加 `/`（加 `/` 会剥掉前缀，自动识别失效）。应用会从请求路径自动识别挂载前缀，登录后的跳转、静态资源、API 请求都会带上正确前缀；不依赖 nginx/Caddy/Apache 的特定配置，直连 17892 也能用。

> 顺带把 IP 口径说清（同一节的"直连也能用"经常被误读成"直连时 XFF 也算数"）：**只有当请求的 `remote_addr` 是回环地址、且转发头信任开关为开时，应用才采信 `X-Forwarded-For`**；非回环来源的 XFF 一律丢弃、退回 `remote_addr`。开关**缺省为"不信任"（fail-closed）**，因此回环直连与 `ssh -L` 隧道下客户端自报的 XFF 不会被采信。**"直连 `17892` 时 `remote_addr` 就是那台客户端自己的地址"这句只在显式 `.venv/bin/python3 -m web --host 0.0.0.0` 下成立**——默认监听是回环（`--host` 缺省 `127.0.0.1`），此时 `remote_addr` 恒为 `127.0.0.1`；只有显式置 `YIBAN_TRUST_FORWARDED_HEADERS=1` 才会采信客户端自带 XFF。反代场景下 nginx 到应用恒为回环且它会覆盖式重写 XFF，那一跳读到的就是真实客户端 IP——该形态必须显式置 1。

- 子路径首页请**带尾斜杠访问**；不带尾斜杠的裸路径按 404 处理（避免误伤根路径部署）。
- 若挂载前缀本身包含 `/api`、`/static` 或页面名等会与应用路由撞车的段（极少见），自动识别可能切错，请在 `.env` 显式设置 `YIBAN_BASE_PATH=/你的/前缀` 兜底。
- 静态资源由应用自带 `/static` 提供（含 30 天缓存 + `?v=` 版本号），**无需**为子路径单独配静态代理；追求性能可另配边缘直发（见 `web/deploy/nginx.conf.example`）。
- 代码位置：中间件 `BasePathMiddleware`（`web/app.py`）；前端 `BASE` 变量（三个外壳模板的 `<head>`，经 `partials/theme_boot.html` 注入）。

</details>

<details>
<summary>🎨 自定义 Web 图标（logo / favicon）</summary>

品牌图标**优先读取 `web/static/vendor/logo.png`**：文件存在即显示；不存在时自动回退为占位符（内联 SVG 蓝色圆角块 +「签」字，无版权资源）。**开源仓库不含 logo.png**（版权来源不明），自己部署时把图标文件放到该位置即可，无需改代码。

浏览器标签页图标：把 `favicon.png` 放入同一目录，机制相同（不存在则用浏览器默认图标）。建议 32×32 左右的 PNG；页面以约 1 小时短缓存引用，换图后无需改代码。

> 请确保你放入的图标有使用权（仓库不附带任何品牌图标资源）。

</details>

<details>
<summary>📜 合规文档接入（隐私政策 / 用户协议）</summary>

网页的《隐私政策》《用户协议》**读取仓库根目录的 `PRIVACY_POLICY.md` 与 `USER_AGREEMENT.md`**：文件存在即显示内容，缺失或留空时回退为"该文档尚未发布，请联系运营者"占位提示。

开源仓库内这两份文档是**只含说明注释的空模板**（涉及每个部署者真实运营者信息的内容不适合在公开仓库写死），自己部署时直接编辑这两个文件即可，无需改代码。站内展示位置：登录页注册勾选弹窗、`/terms`、`/privacy`；代码位置：`web/app.py` 的 `_DOC_FILES` / `_read_doc_html()` / `_render_md()`。

</details>
