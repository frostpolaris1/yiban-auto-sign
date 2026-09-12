# vendor/static 字体基线（Adminator 4.3.0 设计系统）

本目录为 **自托管 Web 字体**，页面不得引用外网 CDN（离线可用 + 供应链可控）。
Inter、JetBrains Mono 两个家族由 Google Fonts CSS API v2 取得 woff2、URL 改写为本地相对路径后入库。
**Noto Sans SC 为例外**：其 Google Fonts 网页子集覆盖不全（基本区约 12,258/20,992）、
分片粒度过粗（单页命中 23/101 片 ≈ 1.28 MiB/字重），已改为从上游**完整**可变字体
`NotoSansSC[wght].ttf` 实例化 400/700 后按字频重切片（见下文「Noto Sans SC」一节），
不再使用 Google Fonts CSS API。

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

- 子集化方式（**适用于 Inter / JetBrains Mono**）：**不做二次子集化**，直接采用 Google Fonts 的
  unicode-range 分片；下载器逐条 `@font-face` 拉取 woff2、重写 `src` 为本地相对文件名，
  **完整保留 `unicode-range` / `font-weight` / `font-style` / `font-display: swap`**。
  Noto Sans SC 不走此路径，改用 `scripts/build_cjk_font_slices.py` 重切片（见其小节）。
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
| 来源 | 上游**完整**可变字体 `google/fonts` 仓库 `ofl/notosanssc/NotoSansSC[wght].ttf`，commit `2894aab31764f10f29c421bdfd2340d3b382d384`（SIL OFL-1.1）；下载文件 SHA-256 `a3041811a78c361b1de50f953c805e0244951c21c5bd412f7232ef0d899af0da`（17,772,300 B） |
| 构建方式 | **可复现脚本** `scripts/build_cjk_font_slices.py`：`fontTools.varLib.instancer` 实例化 400/700 → 覆盖集合按「项目字频（模板实际用字）→ hanziDB 字频前 3,000 → 其余按码点」排序 → `pyftsubset` + brotli 产出 woff2。构建期依赖 `pip install fonttools brotli`（**不写入** `requirements.txt` / `requirements.lock`） |
| 分片策略 | 每字重 48 片：项目字频热区 300 字/片（893 字 ≈ 前 3 片）、高频区 300 字/片、其余按码点 500 字/片；`unicode-range` 由分片内容精确生成（逐片 cmap 与声明区段实测一致） |
| 覆盖 | 每字重 **21,341 码点**：CJK 基本区 20,976 / 20,992、CJK 标点 64 / 64、全角 224 / 240、扩展 A 77（《通用规范汉字表》收录的常用部分）；缺失项均为上游字体本身无字形（U+9FF0–9FFF 等）。动态中文不再回退到系统字体 |
| 文件 | `noto-sans-sc-{400,700}-{000..047}.woff2` 共 96 个；`notosanssc.css`（120,482 B） |
| 体积 | 400 字重 **3,622,396 字节**、700 字重 **3,710,524 字节**；woff2 合计 **7,332,920 字节**（含 CSS 目录合计 **7,453,402 字节**） |
| 单页载荷（实测） | 用本项目 28 个模板的 893 个不同 CJK/全角码点：400 命中 3 片 **132,220 B（0.126 MiB）**、700 命中 3 片 **134,808 B（0.129 MiB）**；旧基线为 23/101 片 ≈ 1.28 MiB/字重 |
| SHA-256 基线 | 聚合 `6bb360727d8daa7ecc2fd34436f19e825d81dfe4bed4d5e4ae3ed7150e79cec1`（命令见上） |

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
