# ui/tools · 走查工具链

高频、强依赖的本地走查工具。不被 `src` 引用、不进 bundle；凭据一律参数或环境变量传入，仓库不落默认口令。

## 推荐开发工具（强依赖）

| 工具 | 版本/来源 | 用途 |
|---|---|---|
| Node.js | ≥20（本仓实测 24） | 运行本目录脚本与 vite |
| Playwright | 随 ui devDependency（1.64.x） | 真页操作/截图/性能/a11y 探针 |
| Chromium | Playwright 自带 | 无头走查与基线 |
| WSL2 + PostgreSQL 18 | 系统包 | 真后端联调库（合成库，见 work 侧 smoke 脚本） |
| Go toolchain | server/go.mod 指定 | 真后端 server/cli 构建 |
| Python 3 | 系统包 | `python -m http.server` 静态 demo 服务（或 serve-demo.mjs） |
| OpenPencil | ZCode 插件 | .fig 设计稿读取/导出（PNG 坏时用 SVG 栅格化） |

## 工具清单

| 脚本 | 用途 | 关键参数 |
|---|---|---|
| `uapp-shot.mjs` | 真页截图矩阵 page×viewport×theme | `--pages app,app/settings --viewports 390,1440 --themes light,dark --out <dir>` |
| `uapp-perf.mjs` | 首屏 FCP/LCP/请求瀑布/chunk 体积 | `--url <url> [--out perf.md]` |
| `uapp-a11y.mjs` | 触达/名称/标题跳级/对比度/溢出/focus-visible/reduced-motion | `--url <url> [--viewport 390]` |
| `serve-demo.mjs` | demo 静态服务兜底（端口占用即退出） | `--root <demo-dir> [--port 8765]` |
| `lib.mjs` | 共享底座：launch/arg/list/login/creds/slug/unmsys | — |

## 口径与坑

- **无 logout 副作用**：dev:mock 会话是服务端全局标记；工具只登录不清会话，避免切换他人会话。真后端（5174 代理）会话按 cookie 隔离，可并发。
- **凭据**：`--email/--password` 或 `UAPP_EMAIL/UAPP_PASSWORD`；缺失即 exit 2。也可 `--no-login` 复用已有会话。
- **MSYS 参数转换**：Git Bash 会把 `/users` 转成 `C:/Program Files/Git/users`；`lib.unmsys` 在 arg/list 层还原。WSL 脚本路径要包在 `bash -c "..."` 引号串里。
- **e2e 密闭**：vite `preview.proxy={}`，活着真后端时 e2e 未 mock 请求不会漏进真库。
- **prod 循环依赖教训**：给 auth 域新增跨模块 import 边曾致 prod 整页卡 SessionLoading（dev 正常）。加导入边后必须跑 prod 构建冒烟。
- 对比度扫描对 `::after` 扩热区控件标「需实测」，不直接判缺陷。