# 设计契约 · Design Contract（单源，长期开发便利的核心）

> 目的：让任何实现者（主会话／外包模型／子代理）产出**默认合规**的前端，
> 把「事后审查抓违规」变成「事前契约＋门禁挡违规」。本文件与 `npm run accept` 配套。
> 规则正本仍为仓根 `AGENTS.md`；本文件只细化前端设计/验收口径。

## 1. 颜色单源（token-gate 咬死）

- 真值只在 `src/styles/tokens.css`，且必须与 vendor `adminator.css` 的 36×2 令牌逐字对齐。
- **禁止**在其它 src 文件写颜色字面量（#hex/rgb/hsl/oklch/lab/lch/color()）。
- **禁止**新增 vendor 之外的令牌名（token-gate 第 3 项会红）。
- 需要语义变体（如徽章可读文字色）时用**派生**：`color-mix(in srgb, var(--success) 55%, var(--t-base))`，
   DIFFERENCE 33→27：派生不引入新令牌、不写字面量，门自然绿。
- 暗色变体用主题选择器覆写为 vendor 令牌（暗底原色即够对比），不另设令牌。
- 注释里也不要写 #hex（token-gate 扫注释）；写裸 hex 或描述。

## 2. 框架与组件（大框架不动）

- 管理端框架＝Adminator：PageHeader / Card / data-table / data-foot / d-sidebar。只改细节，不换框架。
- 共用件优先复用 `components/ui/*`（AsyncState/BusyButton/FilterField/DataTable/Drawer/Tabs/ConfirmModal/…）。
- 新交互件先查共用件与素材库（只借结构，不整件引入）；确需新件放 `components/ui` 或隔离 `next/` 层。
- 功能目录不得互相 import（structure 门）；跨域复用走 pages 装配或 components/ui。
- 触达≥44px（只加命中区不改视觉尺寸）；双载体状态（点＋文字）；mono tabular 数字；reduced-motion 兜底。

## 3. 依赖纪律

- 默认**零新依赖**。引新依赖须先过 ROI 评估（见研究文档 dependency-roi-audit，不入库）＋用户拍板。
- Tailwind/shadcn 仅允许在全新隔离面（next/ 层），不得 retrofit 现有 vendor 页（preflight 冲突＋基线 churn）。

## 4. 验收门禁（提交前必跑 `npm run accept`）

`accept` = `verify`（types/eslint/check:api/unit/token+self-test/structure+self-test/doc-ref+self-test/license+self-test/build）
＋ behavior ＋ shots(win32)。另需：linux 基线（WSL ux1-linux）＋ a11y（tools/uapp-a11y --scope）。
- 任一红不得提交；继承红门须**另批清**并在交接注明，不得混入功能批。
- 视觉改动必须双平台重拍基线（win32＋linux），零差异复核。
- 行为改动必须补/改 e2e 不变式（含负向），不得只改实现。

## 5. 外包/子代理协议

- 派单给：ref＋文件面＋验收谓词＋禁止项；要求先操作/截图/工具、后读码（审查类）。
- 外包产物必须：对齐本契约 token/组件；跑 `npm run accept`；双平台重拍；写交接（含门禁证据 rc）。
- 主会话独立复跑收口门（不信任外包自报）；实现者≠审查者；推送前第三轮独立审查。
- 外包不得：自调新色／自引依赖／改门／混批／push。

## 6. 基线与债务

- 基线是动画落定口径；重拍用固定钟＋reduced-motion；先 build 再拍（防端口复用伪基线）。
- 债务（红门/缺口）登记交接＋另批清；功能批不背继承债。

## 7. 浏览器基线（2026-11-17 起）

- 用户端样式使用现代 CSS：`color-mix()`（Chrome 111+ / Safari 16.2+）、`:has()`（Chrome 105+ / Safari 15.4+）、`backdrop-filter`、`scroll-snap`、`mask-image`。
- 最低支持：Chrome/Edge 111+、Safari 16.2+、Firefox 113+；不为旧引擎写降级。
- 新增此类特性时在本节登记特性与最低版本；不得引入需要 polyfill 的语法。