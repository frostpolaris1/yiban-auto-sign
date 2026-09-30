# 前端修复计划与待办总账（2026-09-30 立）

> **分工纪律（用户 2026-09-30 明确）**：**后端代码不由 AI 直接修改**。任何 `yiban/**`、
> `web/routes/**`、`web/services/**`、`web/app.py` 的改动一律先列进 §3 待协调清单，
> 交由用户协调专有角色处理；AI 只在 `web/static/**`（前端）与 `web/templates/**`（模板）范围内动手。
> 需要后端配合才能完成的前端功能，**停在「已备好、等后端」状态，不擅自降级实现**。

> **临时文件纪律（用户 2026-09-30 补充）**：会话临时产物**禁止写 C 盘**（%TEMP%、~/.cache 等），
> 统一放仓库 `.tmp/`（已 gitignore，用法见 `.tmp/README.md`）。跑测试用
> `TMPDIR="D:/code/yiban-auto-sign/.tmp/pytest" .venv/Scripts/python.exe -m pytest …`。
> 本日已清理 C 盘历史残留 **14,262 个** `yiban-*` / 审查类临时目录。冻结截图快照
> `D:/code/yiban-ui-baseline` 留在原处（D 盘、树外，不占 C 盘且避免被仓库内 grep 误命中）。

## 0. 基线与证据

- 基线：`develop` @ `66cebb8`（工作区干净），冻结快照 `D:/code/yiban-ui-baseline`（110 文件带 SHA-256，含 `vendor/adminator.css`）
- 审查产出：两轮共 826 条
  - 第一轮（22 组 / 51 图 / 66 次独立审查 + 22 次交叉验证）：786 条落盘（32 high / 425 medium / 329 low），分册在 `out/ui-review-part-1..4.md`
  - 第二轮（`mobile-native` / `better-ui` / `better-accessibility` 三把新尺子）：40 条（8 high）
- **失真已知**（勿当独立发现用）：第一轮 prompt 明写「文案不该这么写就汇报」，导致文案类占 34%；
  但其中改法明确指向「改为/改作/措辞」的只有 **20 条**。对照实验：第二、三轮用不提文案的
  判据重审，`mobile-native` 12 条零文案发现、`better-ui` 14 条仅 2 条沾边 ⇒ 真实文案规模约 20 条。

## 1. 本轮目标（只做两件，其余全部挂起）

### 目标 A：周末开关合并为一个多选组件 ✅ **已落地（工作树，未提交）**

- 改造前现状：`work_settings.html` 两个独立 `.set-row` + 两个 `.switch`（`ss-sat` / `ss-sun`）
- 语义：**两者独立可开**（只周六 / 只周日 / 都开 / 都不开）⇒ **复选组**，不是单选 segmented
- 后端已支持：`saturday_sign` / `sunday_sign` 在 `web/services/env_io.py:83` 标签表与 `:102` 布尔键表内，保存路径现成
- Adminator 模板库**无多选组件**（只有 `.btn-group` 与 `.radio`）⇒ 按项目自研范式新建，
  以 `web/static/js/components/select-field.js` + `app.css` 的 `.select-field` 套为原型加
  `aria-multiselectable` 与多选勾选态，**不另起一套皮肤**
- 硬性要求：选中态不能只靠颜色（`app.css` 页脚规矩：色相之外须有文字或记号）；命中区 ≥44px；
  键盘可用（方向键 / 空格切换 / Esc 收起 / 焦点环沿用 `outline: 2px solid var(--primary); outline-offset: 2px`）；
  暗色两套主题正确；**权限粒度不变**（这两项是全体管理员可改，非主管理员也能动）
- 验收：四种状态（只周六 / 只周日 / 都开 / 都不开）界面上能一眼分辨
- **落地记录（2026-09-30）**：
  - 新组件 `web/static/js/components/multiselect-field.js`：combobox + `aria-multiselectable`
    listbox；触发器文案直书四态（"周六 · 周日" / "周六" / "周日" / "都不选"）；空态 = 非空文案
    + 弱化字色（色相之外另有文字通道）；选项行无条件 44px；↑↓/Home/End、空格/回车切换**不收起**
    （可连选）、Esc 收起并归还焦点、焦点移出整组自动收起；勾选记号 `.ms-tick` 与全站 `.check`
    同尺寸同令牌（选项是 `<button>`，不能嵌真 checkbox）
  - 模板改单行 `.set-row` 多选组：**隐藏 input 值契约不变**（id 仍 `ss-sat`/`ss-sun`，值
    `"1"/"0"`）⇒ `collect()/submit()` 零分叉；`aria-labelledby` 显式关联行标签
  - `settings-schedule.js`：`setDisabled` 增多选根分支（同组共用一个触发器，按整组置灰，
    **权限粒度不变**——这两项仍属任意管理员可改）；`apply()` 走组件 `set()` 回填（不派发
    change，不误标脏）；`bind()` 照旧监听隐藏 input 的 change（组件勾选时派发）
  - `app.css` 新增 30.5 节（皮肤沿用 30.1 的 `.select-field` 套，仅多选特有三处）+
    `.set-row-text--static`（复选组行无单一 `for` 目标，压掉谎报热区的 pointer 光标）
  - `work_settings.html` 增加 `multiselect-field.js` 脚本标签；`work_settings.js` 挂载
  - 测试：相关 4 文件 27 例 **26 过 1 挂**；挂例 `test_saturday_sign.py::test_saturday_on_proceeds`
    为 Windows 无 `fcntl` 单实例锁的既有环境性失败（早前已在干净树上证实，与本次无关，CI/Linux 过）

### 目标 B：正态分布参数前端调参 ⛔ **阻塞，需后端**

- 后端 `yiban/engine/schedule.py:345-350` 已读 4 个 env 键（`YIBAN_SCHEDULE_MU_MIN_PCT` /
  `_MU_MAX_PCT` / `_SIGMA_MIN_PCT` / `_SIGMA_MAX_PCT`，默认 μ 40~60、σ 15~25，**定义的是区间**，
  实际值每天在区间内取一次）
- **但这 4 个键不在 `POST /api/settings` 的写入映射里**（该路径只映射了 `edge_front_sec` 等，
  见 `web/routes/settings_api.py:565`）⇒ 前端无处可存
- ⇒ 见 §3 后端项 B1、B2。后端就绪后前端形态：区间成对（min/max）、步进 5、单位 %、范围 0-100，
  help 说明「每天在区间内取一次值」，复用 `settings-schedule.js` 现有 `collect()` / `clearDirty()` /
  `applyPerm()` / `submit()` 路径

## 2. 挂起清单（之前要求过、至今未做，本轮明确不做）

| 编号 | 项 | 状态 | 备注 |
|---|---|---|---|
| H1 | A8 执行体分区排版重写 | **挂起** | `settings-executors.js` 仍 800 行；来源 `docs/refactor/67-frontend-backlog.md:29` |
| H2 | 5a 网页通知看发给"我"的信息 | **挂起** | `topbar.html:31` 与 `topbar_user.html:31` 仍写「将在后续版本接入」；缺按用户过滤的读取端点（需后端） |
| H3 | 5c 网页直改 env 可暴露配置 | **挂起** | `PUT /api/settings` 已是写 `.env` 通路，缺「哪些键可暴露」清单（需用户定策略） |
| H4 | 批次 A（WCAG 硬标准） | 挂起 | 10 项，判据与改法已列全，等本轮两项落地后再动 |
| H5 | 批次 B（触屏） | 挂起 | 12 项（`mobile-native`），含 20 条裸 `:hover` 需包 `@media (hover:hover)` 等 |
| H6 | 批次 C（焦点与键盘） | 挂起 | 7 项，含 toast 双 live region、行菜单不归还焦点 |
| H7 | 批次 D（视觉一致性） | 挂起 | 8 项，含同心圆角 4 链、图标线宽令牌化。**注意**：图标尺寸重排会进一步偏离 Adminator 体系，建议只改描边令牌、不动尺寸档位 |
| H8 | 批次 E（AI 自身引入的缺陷） | 挂起 | `.btn.is-busy` 标签右跳 9px（`::before` 在 `justify-content:center` flex 里把整组重新居中，与 `app.css:1770-1772` 注释不符，注释与实现都要改）；`M-calendar` 补拍 |
| H9 | 批次 F（文案与语义） | 挂起 | 20 条真实文案项；**「缺失用户协议与隐私政策」一项已裁决驳回**（见 §5.3），修复时跳过 |
| H10 | 批次 G（证据补齐） | 挂起 | `M-calendar` 渲染的是 500 错误页；360 截图的桌面滚动条需标注 |
| H11 | `break` 状态铺开页 | 挂起 | `/static/preview-states`，把 switch/button/form 全部状态铺开 |
| H12 | 批次 H（动效） | 挂起 | 8 类控件零按压反馈（四个触发器 `transition` 缺 `transform`）；`.select-menu`/`.date-pop` 零退场；主题切换整页交叉淡入三种时长不同步 |

## 3. 后端待协调清单（AI 不得自行修改）

| 编号 | 位置 | 内容 | 阻塞了什么 |
|---|---|---|---|
| **B1** | `yiban/engine/schedule.py:723-726` | **μ/σ 推导分歧**：`rng.uniform(...)` 随机、每次调用重采样；而 `planner.py:184-185` 用 `_u(day,"mu")` 确定性推导，其 docstring 明写「同一 (phone, day) 的落点在任何进程、任何时刻都可重放」。⇒ 06:31 生成的计划与实际签到时刻对不上，planner 的峰值速率整形失去意义。`schedule.py:722` 注释「μ/σ 每天采样一次」正是被这个 `rng` 破坏的 | **目标 B**（不修则用户调的值不生效）；也是一条独立正确性缺陷 |
| **B2** | `web/routes/settings_api.py` 写入映射（对照 `:565` 现有 `YIBAN_WINDOW_EDGE_FRONT_SEC` 写法）+ 读路径（对照 `:235`）+ `web/services/env_io.py:83` 标签表 | 把 `YIBAN_SCHEDULE_MU_MIN_PCT` / `_MU_MAX_PCT` / `_SIGMA_MIN_PCT` / `_SIGMA_MAX_PCT` 四键接入设置读写；校验须沿用既有约束（`mu_lo >= mu_hi` 告警回退 40~60；`sigma_lo >= sigma_hi` 回退 15~25） | **目标 B** |
| B3 | `yiban/store/verify_jobs.py` 与 `web/routes/*` | 网页通知按用户过滤的读取端点（数据侧 `sign_events` 与 `yiban/notify/ledger.py` 均已具备） | H2 |
| B4 | `yiban/engine/round.py`、`yiban/store/*` | 待确认是否有可移除的遗留路径 | —— |

> **B1+B2 已有一份被叫停代理写下的实现**（2026-09-30）：已按分工纪律从工作树回退，完整 diff
> 存档于 `out/backend-5b-diff-20260930.patch`（108 行：`hrw.py` 增 `u01()` 均匀量、`planner.py`
> 改走 `schedule.day_mu_sigma_pct` 与执行层同源、`schedule.py` 增当日 μ/σ 推导）。交由专有角色
> 审阅后采纳或重写，AI 不再触碰。

## 4. 已知需真机复验的项（桌面工具无法覆盖）

`mobile-native` 判据下第 1/2/7/8 条属「代码可判定但观感需真机」：hover 粘滞、长按选中说明文字、
命中区误触率。最低成本组合：iPhone SE 2（触控板小，32px 命中区误触最明显）+ 一台 Android Chrome
（验 `overscroll-behavior` 与 `input[type=range]` 拖拽）。

## 5. 审查项裁决记录（2026-09-30，用户裁定）

### 5.1 M-calendar 500 —— 修复已上生产，develop 待回流

- 生产机实测（ssh 只读）：`/opt/yiban-auto-sign` HEAD = `acd2e76`（二轮修复提交），工作区无未提交
  代码改动；两处修复标记在位（`web/routes/my.py:708` 双纪元 `needles`、`executor_v3.py:361` attempt
  归 0）；`yiban-web.service` 进程启动 2026-09-30 14:08:50，晚于修复提交（14:02:02）⇒ **两个热修
  （d4bfd6d 一轮 + acd2e76 二轮）均已在生产运行**。
- develop（本地 66cebb8 / 远端 aa012c4）不含这两笔 ⇒ 审查基线上「M-calendar 500」为真实缺陷。
- 待办：merge `hotfix/calendar-and-events`（75187f7/ea76e9a，与生产已部署内容等价的 cherry-pick）
  → develop，无文件冲突；等用户点头执行。

### 5.2 审计链锚点文件首行校验异常 —— 升级窗口竞态，不立项

用户观察：每次新升级时该告警都会出现，时间点恰为拉取新版本期间 ⇒ 校验器读到**半写入状态**的
`audit-anchor.log`，属竞态误报而非数据损坏。不立项专项核查；如将来治理，方向是校验器对读取
失败做短暂重试/容忍窗口（后端范畴，届时另行协调）。

### 5.3 用户协议与隐私政策「缺失」—— 设计如此，驳回

仓库里**有意**只放占位模板：部署到服务器时由运营者用自己的正式文档替换占位符。审查发现中
「缺失用户协议/隐私政策」及相关的「空协议页强制勾选同意」按设计驳回，任何批次不得改动协议
页面的占位性质。

## 6. 批次执行台账（滚动更新）

### 批 A+C（WCAG 对比度 + 焦点键盘）✅ 完成

- **已修 20 项**（批 A 12 + 批 C 8），每项带守卫测试或算术断言；`.set-ok` 零规则补齐。
  代表项：`--t-light` 低位文字全面上移 `--t-sub`、placeholder 全局规则、弹窗初始焦点不再恒落 ✕
  （confirmDialog 初始焦点改取消侧）、下拉/行菜单焦点归还、toast 收敛单一 polite live region、
  焦点环白名单补 7 个选择器、窄屏吸底操作条改 static。
- **大改动面停下 4+1 项**（理由见代理报告）：G21#2 控制件边框 1.4.11（需新增 `--control-border`
  令牌+改 4 类触发器，归入整体视觉轮）、G04#10-low 输入框描边、G14#8 日历图例色块重排
  （模板+JS 大改）、G05#8 余下已读态/数字徽标/aria-label 三小项、G03#2 未保存修改守卫
  （openModal 需异步 beforeClose 钩子，行为变更）。
- **测试**：必跑 43 passed；扩大回归（金标准/JS 模块面/页面一致性等 12 文件）122 passed +
  9 subtests；`node --check` 过；金标准 4 例按提示 `UPDATE_GOLDEN=1` 各更 1 行（toast-host 属性）。
- 过期项：G22#22 `.dd-menu` 硬边（vendor 本就有）不修。
