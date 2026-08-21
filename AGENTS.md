# AGENTS.md · 项目记忆

> 本文件是给 AI 助手/后续维护者的项目记忆。约定优先级：用户显式指令 > 本文件 > 默认行为。

## 项目概况

易班自动签到（yiban-auto-sign）：Python + Flask 的易班自动签到工具。
Web 控制台在 `web/`：Flask 应用（`web/app.py`）+ 3 个 Jinja 模板（`web/templates/{login,user,index}.html`），
前端使用**本地 vendored Tailwind Play CDN v3.4.17**（`web/static/vendor/tailwind.js`）+ MiSans 字体，无构建步骤，改模板即生效。

## 视觉体系现状（2026-08-21 用户裁决）

**配色 = Tailwind 默认原版**（zinc/blue/amber/red/green 默认值）；用户否决了「溯光星海鎏金」主题。
**保留的非配色改进**：SVG 图标系统、圆角体系、pill 徽章、柔和卡片阴影、emoji 清理、文档页卡片容器。

### 色板重映射机制（换色必读）

- 每页 `<head>` 的 `tailwind.config` 将 zinc/blue/amber/red/green 五个色阶重定向到
  `rgb(var(--c-*)) / <alpha-value>` CSS 变量；变量在紧随其后的 `<style>` 块中定义。
- **当前变量值 = Tailwind 官方默认色板**（`:root` 一份，深浅模式同值——与原生行为一致）。
  类名 `bg-blue-500` 实际读 `--c-blue-500`（59 130 246）。
- **将来换主题**：只改变量块即可整体换肤，深浅模式可在 `:root`/`.dark` 分别覆盖；
  不要去模板里逐个替换颜色类名。三页的 config + 变量块 + 图标脚本是复制粘贴关系，改动必须同步。
- 圆角体系经 `borderRadius` 重映射全局生效：`lg=10px`（控件）、`xl=14px`（卡片）、`2xl=18px`（模态）；徽章一律 `rounded-full` pill 化。
- 卡片阴影为双层柔影（黑基调），定义在各页 `.card/.card-sm` 组件类内。

### 字体栈（2026-08-21 起：仅标题用方圆体）

- **正文 = 原版字体栈**（MiSans 仅高分屏 web 加载 → 系统字体；低分屏媒体查询排除 web woff2 保 ClearType）。
- **标题 = 阿里妈妈方圆体 VF**（`"Alimama FangYuanTi VF"`，本机安装的可变字体，圆润亲和；
  可变字重轴自动匹配 500/600）。实现为一条选择器规则：
  `h1, h2, h3, h4, .font-semibold.tracking-tight { font-family: ... }`——
  本项目所有标题/品牌/模态题统一携带 `font-semibold tracking-tight` 类组合，
  **新增标题时保持该组合即可自动套用**；两处展示型大字（时钟、统计卡数字）也走该规则。
  未安装方圆体的设备自动回退 MiSans/系统字体。
- 三页 `<style>` 的 body+标题规则 + `app.py` 文档页 CSS 均已同步；改字体时四处一起改。

### 图标系统（emoji 禁令）

- **UI 一律不用 emoji**。2026-08-21 起全部界面图标替换为统一手绘线性 SVG：
  每页 head 有 `window.icon(name)` 工厂 + `[data-icon]` 占位经 `hydrateIcons()` 填充；
  图标用 `currentColor` 继承文字颜色、尺寸随字号（`.ic { width:1em; height:1em }`）。
- 图标名与功能：`sun/moon` 主题切换、`clock` 待签、`check` 成功、`close` 失败、`retry` 重试、
  `ban` 跳过、`minus` 无需、`pause` 暂停、`stop` 已取消、`trash` 删除、`warn` 警示、`cal` 日历、
  `play/dots/xmark/chevL/chevR/chevD` 操作符号。新增图标加进三页的 `P` 表（保持同步）。
- **数据层 emoji 是 API 契约，不许动**：日历接口返回 `'✅'/'❌'` 作为日期状态值、
  日志文件含 `✅/❌/➖` 符号（`app.py sym_map` 解析历史数据）、后端 `STATUS_ICON` 字典仍下发 emoji
  字段（前端已不渲染，仅为兼容）。前端只做 `stt === '✅'` 这类**比较**，不把 emoji 渲染进 DOM。
- 原生 `confirm()` 对话框无法内嵌图标，警示文案用「注意：」前缀替代 ⚠️。

### 其他视觉代码位置

- `app.py` 内嵌两处：协议/隐私文档页 CSS（搜「doc-card」，白卡+圆角结构保留）与
  签到状态色（`sign_status()`，已恢复原版东京夜蓝系四色 #565f89/#7aa2f7/#9ece6a/#e0af68；
  文案无 emoji）。index.html 的兜底色 `#7aa2f7` 与之对应。

## 维护约定

- **严禁未经用户明确要求就 git 提交**；本仓库的工作默认停留在工作区。
- 改动模板/app.py 前，先备份到 `backups/<yyyy-mm-dd>-<主题>/` 并用 SHA256 校验。
  现有备份：`backups/2026-08-21-starlight-theme/`（原始版 + applied-state/ 主题应用版快照）。
- UI emoji 回归扫描：`python scripts/scan_ui_emoji.py`（渲染三模板并检查残留）。
- Demo 凭据初始化脚本：`scripts/init_demo_credentials.py`（幂等，详见脚本头注释；
  凭据本身不写入本文件，见 .env 与数据库）。
- 设计预览存档：`docs/design/preview-starlight-theme.html` 为已否决的「溯光星海鎏金」主题预览，
  仅作历史参考，其中取值不再是现状。
