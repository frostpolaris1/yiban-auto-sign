# 开发日志

> 面向开发者/维护者；用户可见的更新日志见 CHANGELOG.md（通俗脱敏版）。

## 2026-08-11 v1.7.0 — 体验优化包
- 平板/触屏适配：body 加 `overscroll-y-none` 消除橡皮筋回弹（管理端各 tab）
- 签到日志页：log-box 由 `max-h-[60vh]` 改为 `h-[calc(100vh-280px)] min-h-[300px]` 撑满视口
- 公告横幅：三端 `sticky top-0 z-40` 吸顶 + mb-3 间距（与下方标题对齐）
- 日历紧凑化：容器 `max-w-sm`，格子随容器缩小，整月一屏完整显示
- 随机延迟自动保存：`toggleDelay` 与 input `onchange` 触发 `saveSettings(true)`（静默模式 + "已自动保存"提示），移除保存按钮
- 操作列改省略号菜单：`openRowMenu(event, items)` fixed 定位 dropdown（点外部关闭），账号管理两表 + 用户管理三表统一
- 更新日志：`/api/changelog` 读取项目根 CHANGELOG.md（公开接口）；三端版本号点击弹出弹窗（遮罩/✕ 关闭）
- 教训：HTML 重排脚本必须用业务 JS 开头作锚点（`rindex('<script>')` 会命中页面第二个裸 script，导致业务 JS 被卷进被移动的 section、init 中断——v1.6.1 踩过）

## 2026-08-11 v1.6.x — 签到日历
- 状态文件方案（无数据库）：signin.py 汇总后写 `sign-daily-YYYY-MM-DD.json`（`YIBAN_STATE_DIR` 默认 /var/log/yiban）；web 读文件返回月状态
- `/api/my-calendar?month=`（月状态）+ `/api/my-logs?date=`（当日日志按手机号过滤，普通用户白名单）
- 自绘月历（零依赖）：周一起始 `(getDay()+6)%7`；四态 ✅绿/❌红/➖周日灰/空；今日蓝描边；点击日期加载日志
- v1.6.1：用户页 section 重排（签到情况置顶）；/api/me 补 email 字段

## 2026-08-11 v1.5.0 — 自助改密
- `/api/me/password`：内置管理员写 .env、注册用户更新 users.json 哈希；失败计数复用登录限速
- 移除 `/api/admin/credentials`（改用户名功能废除）；两前端改密表单

## 2026-08-11 v1.4.0 — 代码审查第 1 梯队
- requirements 补 flask/werkzeug（迁移服务器关键）；signin 日志默认 INFO；`from re import compile` 遮蔽修复；函数内 import requests 上移；tui 原子写（tmp+os.replace）

## 2026-08-11 v1.3.x — 审核体系与多端适配
- v1.3.0：账号两分组 + 拒绝带理由（rejected 状态机）+ 用户三分组
- v1.3.1：角色实时判定（`_effective_role()` 每次请求读 users.json/.env，替换全部 9 处 session['role']）——提权/降权立即生效
- v1.3.2：账号列表 10s 轮询（refreshAccountsSilent）
- v1.3.3~1.3.6：表格对齐（table-fixed + 列宽）、用户协议上线、表头缺「审核」列幽灵列修复（th=8=td）
- v1.3.7：手机适配（min-width 760/1000/880 + 横向滚动 + whitespace-nowrap 防中文竖排 + 随机延迟响应式）

## 2026-08-10 v1.0~1.2 — 网页管理系统从零上线
- Flask + 单 HTML + Tailwind CDN 零依赖架构；CSRF 自实现（session token）；登录/注册限速；日志脱敏（KillYiBan 降级 DEBUG）
- 邮箱注册登录 + 管理员审核（pending/active）；多管理员（users.json role）；排队机制（queue_ahead）
- 会话安全：HttpOnly/SameSite=Lax/防 session 固定；版本失效刷新兜底（WEB_VERSION + localStorage）
- 部署：systemd yiban-web（17892）、ufw 放行、双远端（GitHub/Gitee）同步
