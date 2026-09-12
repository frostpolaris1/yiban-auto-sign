# vendor/static 字体基线（Adminator 4.3.0 设计系统）

本目录为 **自托管 Web 字体**，页面不得引用外网 CDN（离线可用 + 供应链可控）。
三个家族均由 Google Fonts CSS API v2 取得 woff2，URL 改写为本地相对路径后入库。

## 许可（OFL-1.1）

**Inter、Noto Sans SC、JetBrains Mono 三者均以 SIL Open Font License 1.1（OFL-1.1）授权。**
OFL-1.1 明确允许自托管（self-hosting）与再分发——不论是否修改、商用与非商用皆可——
条件是保留版权声明与本许可证；若对字体软件本身作出修改并以 Reserved Font Name 命名，
须遵守相应的改名限制。**完整许可证正文见本目录 `OFL.txt`**（含三家族版权声明）。

## 共同构建与校验

- 取字命令（须带浏览器 UA 与 `Accept: text/css`，否则服务端返回 TTF 回退版而非 woff2 分片）：

```bash
UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
curl -sS -H "User-Agent: $UA" -H "Accept: text/css,*/*;q=0.1" \
  "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" -o inter.css
```

- 子集化方式：**不做二次子集化**，直接采用 Google Fonts 的 unicode-range 分片；下载器逐条
  `@font-face` 拉取 woff2、重写 `src` 为本地相对文件名，**完整保留 `unicode-range` /
  `font-weight` / `font-style` / `font-display: swap`**。
- 网络出口实测将 `fonts.gstatic.com` 重写为镜像主机 `fonts.gstatic.font.im`；文件内容为 Google
  Fonts 原始 woff2（`wOF2` 魔数校验通过），仅 URL 主机不同，不影响自托管产物。

### 聚合 SHA-256 命令（多文件家族）

对按文件名排序后的逐文件哈希列表再取一次 SHA-256（**精确命令，需在家族子目录内执行**）：

```bash
cd web/static/vendor/fonts/<family> && sha256sum *.woff2 | sort -k2 | sha256sum
```

---

## Inter（`inter/`）

| 项 | 值 |
| --- | --- |
| 用途 | 拉丁 UI 正文字体；400/500/600/700 四档字重，normal 样式 |
| 来源 | 官方 CSS API v2：`https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap`（经镜像主机 `fonts.gstatic.font.im` 取回实际 woff2） |
| 构建与子集化 | Google Fonts 原生 unicode-range 分片，仅保留 `latin` + `latin-ext` 两个子集（丢弃 cyrillic / greek / vietnamese，减小体积）；8 个 woff2 |
| 文件 | `Inter-{400,500,600,700}-latin.woff2`（各 48,432 B）、`Inter-{400,500,600,700}-latin-ext.woff2`（各 85,272 B）；`inter.css`（3,188 B） |
| 体积 | woff2 合计 **534,816 字节**（含 CSS 目录合计 538,004 字节） |
| SHA-256 基线 | 聚合 `6bb4c04f96481569719088f3ee70e114b0149524223502a730d7e0d2dd32b3ff`（命令见上） |

## JetBrains Mono（`jetbrains-mono/`）

| 项 | 值 |
| --- | --- |
| 用途 | 等宽数值字体（手机号 / 日志行 / ID）；400，normal |
| 来源 | 官方 CSS API v2：`https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400&display=swap`（经镜像主机 `fonts.gstatic.font.im`） |
| 构建与子集化 | Google Fonts 原生 unicode-range 分片，**仅保留 `latin`**（丢弃 latin-ext / cyrillic / greek / vietnamese）；1 个 woff2 |
| 文件 | `JetBrainsMono-400-latin.woff2`（21,168 B）；`jetbrains-mono.css`（580 B） |
| 体积 | woff2 合计 **21,168 字节**（含 CSS 目录合计 21,748 字节） |
| SHA-256 基线 | 聚合 `0902c579fe6b6419140289302a7a3f8897c59bff8640dcd0e0d1320ce04bd162`（命令见上） |

## Noto Sans SC（`notosanssc/`）

| 项 | 值 |
| --- | --- |
| 用途 | 中文（含用户动态中文：账号名 / 邮箱 / 驳回原因 / 日志）正文字体；400 与 700，normal |
| 来源 | 官方 CSS API v2：`https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@400%3B700&display=swap`（`;` 须编码为 `%3B`；经镜像主机 `fonts.gstatic.font.im`） |
| 构建与子集化 | **不得按已知 UI 字符串做字符子集**（动态中文会缺字变豆腐块）；采用 Google Fonts 标准 **unicode-range 切片**：每字重 101 个 woff2 连续 CJK 区段，每片带 `unicode-range`，浏览器按页面实际码点按需加载 |
| 文件 | `noto-sans-sc-{400,700}-{0..100}.woff2` 共 202 个；`notosanssc.css`（206,541 B） |
| 覆盖 | 每字重 101 片 / 16,279 码点；单片 2,080 – 76,800 B，均值 44,717 B |
| 体积 | woff2 合计 **9,033,016 字节**（含 CSS 目录合计 9,239,557 字节） |
| SHA-256 基线 | 聚合 `04b95381a395614745c9588fa35683127e1c32be4aa4f614c2ce55db455567b7`（命令见上） |

## fonts.css 聚合入口

| 项 | 值 |
| --- | --- |
| 用途 | 单一 `@import` 入口，相对路径聚合三家族 |
| 体积 | **672 字节** |
| 引入方式 | `<link rel="stylesheet" href="{{ request.script_root }}/static/vendor/fonts/fonts.css?v={{ web_version }}">` |

## 许可文件

| 项 | 值 |
| --- | --- |
| 文件 | `OFL.txt`（SIL OFL-1.1 全文 + 三家族版权声明） |
| 体积 | 5,040 字节 |
| SHA-256 | `0665276146258814c147723911786fabe5f06cb7ce74c9b81682fd03bf6df948` |

## 反例与验证

```bash
# 任何 CSS 中不得残留外网字体 URL
grep -rniE "https?://|fonts\.googleapis|fonts\.gstatic" web/static/vendor/fonts --include=*.css
# 期望：无输出
```

> 备注：`fonts/misans/`（4.6 MB，许可存疑）为历史遗留，**本次未触碰**，后续步骤单独处理移除。

登记日期：2026-09-12（前端模块化 / Adminator 设计系统）
