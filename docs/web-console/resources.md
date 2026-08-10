# 网页管理系统 · 开发资源库

> 目标：用网页（HTML）替代 TUI 管理面板，管理员账号登录后可在任意设备访问管理。
> 分支：`server-web`（基于 `server-tui` 43c58ce）

## 一、已确认的环境事实

| 环境 | 版本 / 说明 |
|------|------------|
| 本地开发机（Windows） | Python 3.12.11，Node v24.18.0 / npm 11.16.0 |
| 生产服务器 | 阿里云 ECS 120.26.23.83，Ubuntu 22.04.5，部署目录 `/opt/yiban-auto-sign` |
| 服务器依赖 | `pip3` 可用（清华源已配置），已有 requests/pycryptodome/js2py/PySocks/textual |
| 复用资产 | `scripts/signin.py`（签到引擎，不动）、`run.sh`、`accounts.json`、`.env`、`/var/log/yiban/sign.log` |

## 二、可用的本地 skills（设计 & 开发）

| skill | 用途 |
|-------|------|
| `sidebar-fixed`（已复制到 `design/`） | **仪表盘布局基准**：固定侧边栏 + 可滚动主内容区，后台管理经典布局。Tailwind 风格，含配色/组件/禁用模式 |
| `ui-ux-pro-max`（`~/.agents/skills/`） | 设计智能库：84 种风格、192 色板、74 字体配对、98 UX 指南、25 图表类型、16 动画预设（sidebar-fixed 即其 StyleKit 产物） |
| `frontend-design`（`~/.agents/skills/`） | 新 UI 的视觉方向与设计决策指导 |
| `web-design-guidelines`（`~/.agents/skills/`） | 审查 UI 是否符合 Web 界面规范（可访问性、UX） |
| `vue-best-practices`（`~/.agents/skills/`） | 若前端选 Vue：Composition API + `<script setup>` + TS |
| `vibe-coding-guide`（`~/.zcode/skills/`，本次新建） | 非专业用户协作规范：模糊即提问、小步交付、解释原因 |
| `skill-search`（`~/.zcode/skills/`，本次新建） | 搜索可用 skill/插件：`python ~/.zcode/skills/skill-search/scripts/search.py 关键词` |

## 三、技术栈（已确认 2026-08-10）

| 层面 | 方案 | 说明 |
|------|------|------|
| 后端 | **Flask** | 服务器 `pip3 install flask`；路由/登录会话开箱即用 |
| 前端 | **单页 HTML + Tailwind CDN + 原生 JS** | 一个 HTML 文件，手机自适应，贴合 sidebar-fixed 风格 |
| 认证 | .env 管理员账号 + Flask 签名 session | 新增 `YIBAN_ADMIN_USER` / `YIBAN_ADMIN_PASSWORD` / `SECRET_KEY`；登录失败限速 |
| 访问 | **公网 IP + 端口**（HTTP 起步） | 阿里云安全组需放行端口；强密码 + 登录限速；后续可加 Nginx+HTTPS |
| 部署 | systemd 服务（待定）或 nohup | 端口建议 8000（待实现时定） |

### 进度（2026-08-10）

**v1 已完成并通过测试：**
- [x] 后端 Flask API（认证/账号 CRUD/日志/设置/手动签到/连通性/时钟）
- [x] 前端单页 HTML + Tailwind 本地化（登录页 + 账号管理/日志/设置三模块）
- [x] 测试：20 项 API 冒烟 + 手动签到端到端 + 浏览器界面验证（发现并修复：编辑清空设备识别码、设置页 gap 控件 id 不匹配、切页时钟不刷新）
- [x] 本地演示数据：项目根 `.env`（admin/admin123456 仅演示）、`accounts.json`、`demo-log/`（均被 .gitignore 排除）

**v2 用户系统已完成（2026-08-10）：**
- [x] 邮箱注册/登录（格式校验、邮箱唯一、大小写不敏感、密码哈希存储；不做验证码）
- [x] 普通用户：登录后修改昵称（`PUT /api/profile`，默认昵称=邮箱前缀）
- [x] 用户提交自己的易班账号（名称+手机号+密码+设备信息一起填），**每用户限 1 套**（可编辑/删除后重新提交）
- [x] 重复审查：手机号全局唯一（"已被使用"）、邮箱唯一（"已注册"）
- [x] 管理员审核：待审核提示 + 通过/拒绝（通过后参与定时签到；signin.py 跳过 status=pending）
- [x] 后台归属列显示提交者昵称（owner 存邮箱，映射显示昵称）
- [x] 登录页精简：去掉品牌区/介绍，仅登录/注册卡片
- [x] 时间平滑走秒（1s 本地走格 + 60s 校准）；emoji 清理（导航改 SVG、按钮文字化）
- [x] 测试：19+5+5 项 API 全流程（含修复 compare_digest 中文 500、/api/me 用户白名单）+ 浏览器全链路（注册→提交→审核→生效）

**v3 安全加固 + 精简（2026-08-10）：**
- [x] **端口改为 17892**（非常见端口，避开 8000/5000/3000 防冲突；`--port` 可覆盖）
- [x] **删除昵称体系**：无 /api/profile；注册/登录仅邮箱+密码；账号备注名（name 字段）在账号表单中填写，文案精简为"名称 / 备注（可选）"
- [x] **管理员账号后台修改**：设置页新增"管理员账号"卡片（旧密码验证 + 新用户名/密码，写入 .env，下次登录生效）
- [x] session 安全：HttpOnly + SameSite=Lax（防 XSS 窃取/CSRF）+ 登录成功重建会话（防 session 固定）
- [x] 登录大小写策略：管理员用户名大小写敏感；用户邮箱大小写不敏感
- [x] 修复：普通用户登出 403 导致无法退出（/api/logout 加入用户白名单）
- [x] 归属列显示邮箱前缀（无昵称体系后）；密码明文仅存 accounts.json（signin.py 登录必需，已 gitignore，建议服务器 chmod 600）

### 待办（生产部署）
- [ ] 服务器：`pip3 install flask`
- [ ] 服务器 `.env` 配置管理员账号（`YIBAN_ADMIN_USER` / `YIBAN_ADMIN_PASSWORD`）与 SECRET_KEY（自动生成）
- [ ] 阿里云安全组放行端口
- [ ] systemd 服务单元（开机自启、崩溃重启）

### 待办（后续功能）
- [ ] 用户账号管理系统（用户表单提交自己易班账号 + 查看自己签到情况）——后端 API 已预留扩展空间

## 四、功能映射（TUI → 网页版）

见项目根 README 与 `tui/app.py`；核心功能：账号 CRUD+排序、手动签到、日志面板、状态图标（⏳✅❌🔄➖）、随机延迟设置、连通性检测、服务器时间/签到窗口状态；新增：管理员登录、任意设备访问。

## 五、外部资源链接（备用）

- **StyleKit 设计库**：https://github.com/AnxForever/stylekit （146 种设计风格 + tokens + 组件 + AI Rules 导出；在线展示 https://stylekit.top ，AI 可读文档 /llms.txt；sidebar-fixed 即其布局风格之一）→ 需要换风格时先来这里挑
- Tailwind CSS：https://tailwindcss.com/docs （本地副本已放 `web/static/vendor/tailwind.js`，无需外网）
- Flask 文档：https://flask.palletsprojects.com/
- Alpine.js（可选轻交互）：https://alpinejs.dev/
- Lucide 图标：https://lucide.dev/（CDN: `https://unpkg.com/lucide@latest`）

## 六、同类开源方案调研（2026-08-10）

### 同类签到项目（参考风控/部署/通知）
| 项目 | 说明 | 可参考点 |
|------|------|---------|
| zimo0o0omiz/auto-sign（341★） | 今日校园自动签到，通用，云函数部署 | 表单配置化、多学校配置共享、云函数免服务器 |
| Avenshy/YibanCheckin | 易班签到（Python），同平台 | 同平台实现对比 |
| aowubulao/auto-cpdaily 等 | 今日校园系列（多个） | 通知机制、异常处理 |
| Crazynob/helpcat | 今日校园（Java），多账号查寝/信息收集 | 多账号管理 |

### 网页后台参考（Flask 生态）
| 方案 | 用途 | 本项目现状 |
|------|------|-----------|
| flask-login | 会话管理（成熟） | 自写 session，可替代 |
| flask-wtf | 表单 + **CSRF 防护**（比 SameSite 更完整） | 当前 SameSite=Lax，可加固 |
| flask-admin | 现成管理后台 | 定制性差，仅参考 |
| flask-security / flask-user | 用户认证 + 角色 | 参考角色设计 |
| AdminLTE / Tabler / CoreUI | 开源后台模板（Bootstrap/Tailwind） | 组件细节参考 |
| APScheduler | 可视化定时任务（替代 cron） | 未来功能可选 |

### 本项目已吸收的实践
- KillYiBan 真实 App 登录特征（e003 风控）、队列重试、随机延迟、代理出口、多账号、webhook 通知
- 邮箱+密码注册、scrypt 哈希、登录限速、HttpOnly/SameSite、字段级即时校验
