# vendor/static 字体基线（Adminator 4.3.0 设计系统）

本目录为 **自托管 Web 字体**，页面不得引用外网 CDN（离线可用 + 供应链可控）。
Inter、JetBrains Mono 两个家族由 Google Fonts CSS API v2 取得 woff2、URL 改写为本地相对路径后入库。
**Noto Sans SC 为例外**：其 Google Fonts 网页子集覆盖不全（基本区约 12,258/20,992）、
分片粒度过粗（单页命中 23/101 片 ≈ 1.28 MiB/字重）；`@fontsource(-variable)/noto-sans-sc`
只是同一套网页子集的再分发（实测 101 片并集仅覆盖基本区 12,242/20,992），以其为上游会降级。
现改为从 npm `md2note-fonts@1.0.0`（OFL-1.1）内嵌的**完整** Noto Sans SC Regular/Bold
静态字体按字频重切片（见下文「Noto Sans SC」一节），不再使用 Google Fonts CSS API。

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
| 文件 | `Inter-{400,500,600,700}-latin.woff2`（各 48,432 B）、`Inter-{400,500,600,700}-latin-ext.woff2`（各 85,272 B）；`inter.css`（3,276 B，含内容哈希版本查询串） |
| 体积 | woff2 合计 **534,816 字节**（含 CSS 目录合计 538,004 字节） |
| SHA-256 基线 | 聚合 `6bb4c04f96481569719088f3ee70e114b0149524223502a730d7e0d2dd32b3ff`（命令见上） |

## JetBrains Mono（`jetbrains-mono/`）

| 项 | 值 |
| --- | --- |
| 用途 | 等宽数值字体（手机号 / 日志行 / ID）；400，normal |
| 来源 | 官方 CSS API v2：`https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400&display=swap`（经镜像主机 `fonts.gstatic.font.im`） |
| 构建与子集化 | Google Fonts 原生 unicode-range 分片，**仅保留 `latin`**（丢弃 latin-ext / cyrillic / greek / vietnamese）；1 个 woff2 |
| 文件 | `JetBrainsMono-400-latin.woff2`（21,168 B）；`jetbrains-mono.css`（591 B，含内容哈希版本查询串） |
| 体积 | woff2 合计 **21,168 字节**（含 CSS 目录合计 21,748 字节） |
| SHA-256 基线 | 聚合 `0902c579fe6b6419140289302a7a3f8897c59bff8640dcd0e0d1320ce04bd162`（命令见上） |

## Noto Sans SC（`notosanssc/`）

| 项 | 值 |
| --- | --- |
| 用途 | 中文（含用户动态中文：账号名 / 邮箱 / 驳回原因 / 日志）正文字体；400 与 700，normal |
| 来源 | npm 包 `md2note-fonts@1.0.0`（`license: OFL-1.1`），tarball `https://registry.npmmirror.com/md2note-fonts/-/md2note-fonts-1.0.0.tgz`（SHA-256 `5bc75738d0bfac431a9bd0a202e97466211664e9235b5244e4095effb34d91c8`，15,939,052 B）；包内 `vfs_fonts.js`（SHA-256 `c1a9afc628ed138e1830032b0974a52dd8c0ebe3bf6196f89918830d631a53f3`）base64 内嵌完整 Noto Sans SC `NotoSansSC-Regular.otf`（400，8,331,336 B，SHA-256 `faa6c9df652116dde789d351359f3d7e5d2285a2b2a1f04a2d7244df706d5ea9`）与 `NotoSansSC-Bold.otf`（700，8,543,168 B，SHA-256 `c6cb5a93abaa9edc8ee7463b7ebb7f42d618d40e6ed2f7a5371c97b0b64767c0`）；备选 `mirrors.cloud.tencent.com/npm/...`、`cdn.jsdelivr.net/npm/...`、`unpkg.com/...`。两 OTF 家族名 `Noto Sans SC`，各 31,036 字形。**包许可 OFL-1.1；字体文件许可 OFL-1.1（同为 SIL OFL-1.1）** |
| 来源评估 | 评估并拒绝 `noto-sans-sc@37.0.0`（npm，14 版本、2019–2024 持续发布，看似更成熟；**包许可 MIT / 字体文件许可 OFL-1.1**）：其实为 Google Fonts CSS API v2 网页子集的再分发（每字重 101 个编号 woff2，`scripts/download.py` 直接抓 `fonts.googleapis.com/css2`），并集仅覆盖基本区 12,242/20,992、标点 32/64、全角 149/240、扩展 A 27，采用即降级，且包内无完整字体文件。`@betteroffice/fonts-cjk@0.1.0`（仅含 Regular、0.1.0 与 0.0.1 同日出包）、`@electron-fonts/noto-sans-sc@1.2.0`（electron 字体注入用途、仅 2 版本）均无更优成熟度。结论：**保留 `md2note-fonts@1.0.0`**，详见 `docs/refactor/16-font-source-mature.md` |
| 构建方式 | **可复现脚本** `scripts/build_cjk_font_slices.py`，下载与切片分离：`--fetch <dir>` 取回并按 SHA-256 校验上游字体；切片阶段 `--weight-font 400=… --weight-font 700=…` 离线执行。切片前 CFF/OTF 源经 cu2qu（`max_err=1.0`）转 glyf/TTF（同覆盖下 woff2 对 CFF 压缩差约 35%）→ 覆盖集合按「项目字频（模板实际用字）→ hanziDB 字频前 3,000 → 其余按码点」排序 → `pyftsubset` + brotli 产出 woff2。构建期依赖 `pip install fonttools brotli`（**不写入** `requirements.txt` / `requirements.lock`） |
| 分片策略 | 每字重 48 片：项目字频热区 300 字/片（893 字 ≈ 前 3 片）、高频区 300 字/片、其余按码点 500 字/片；`unicode-range` 由分片内容精确生成（逐片 cmap 与声明区段实测一致） |
| 覆盖 | 每字重 **21,341 码点**：CJK 基本区 20,976 / 20,992、CJK 标点 64 / 64、全角 224 / 240、扩展 A 77（《通用规范汉字表》收录的常用部分）；缺失项均为上游字体本身无字形（U+9FF0–9FFF 等）。动态中文不再回退到系统字体 |
| 文件 | `noto-sans-sc-{400,700}-{000..047}.woff2` 共 96 个；`notosanssc.css`（121,538 B，url 含内容哈希版本查询串） |
| 体积 | 400 字重 **3,538,580 字节**、700 字重 **3,606,984 字节**；woff2 合计 **7,145,564 字节**（含 CSS 目录合计 **7,266,046 字节**） |
| 单页载荷（实测） | 用本项目 28 个模板的 893 个不同 CJK/全角码点：400 命中 3 片 **126,772 B（0.121 MiB）**、700 命中 3 片 **128,436 B（0.122 MiB）**，合计 **255,208 B（0.243 MiB）**；旧基线为 23/101 片 ≈ 1.28 MiB/字重 |
| SHA-256 基线 | 聚合 `9bb613b6a6aebb0e7bc04e77c040632835eec26368969c2b428331ac226b9a7f`（命令见上） |

## 字体引入与版本化（2026-09-14 起：三个外壳直接并行 `<link>`）

| 项 | 值 |
| --- | --- |
| 引入方式 | 三个外壳（`layout_admin/user/auth`）直接并行引入三个子 CSS，不再经 `fonts.css` 的 `@import`（@import 必须等聚合文件下载并解析后才发起子请求，多 1–2 个 RTT）：<br>`<link rel="stylesheet" href="…/static/vendor/fonts/inter/inter.css?v={{ web_version }}">`<br>`<link rel="stylesheet" href="…/static/vendor/fonts/jetbrains-mono/jetbrains-mono.css?v={{ web_version }}">`<br>`<link rel="stylesheet" href="…/static/vendor/fonts/notosanssc/notosanssc.css?v={{ web_version }}">` |
| `fonts.css` 现状 | 保留为**纯注释说明文件**（不再被任何模板引用），兼作 `scripts/build_cjk_font_slices.py` 的 scope 哨兵（存在即对整个 fonts 目录打标）。 |
| URL 版本化（子 CSS） | 外壳以 `?v={{ web_version }}`（进程启动时间戳）版本化三个子 CSS —— 每次发版 URL 变化，子 CSS 自身总是新鲜。 |
| URL 版本化（分片） | 各家族子 CSS 内的 woff2 `url()` 均带**内容短哈希**查询串（`?v=<sha256 前 8 位>`），由 `scripts/stamp_font_versions.py` 打标（幂等，可重跑；`--check` 只查不写）。内容不变 URL 不变（30 天强缓存继续生效），内容/文件名一变 URL 即变。 |
| 缓存背景 / 为何不能退化 | `/static/` 为 30 天强缓存（`max-age=2592000`）。两级版本化共同打断失效链条：子 CSS URL 随发版变化 → 拿到新子 CSS → 其中分片 URL 因内容哈希变化也是新的。**不版本化时**重切片后旧访客会按缓存的旧子 CSS 引用已删除的分片 → 404 回退宋体，最长 30 天自愈。`build_cjk_font_slices.py` 生成完成后会自动调用 stamp 脚本刷新整个 fonts 目录。 |

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

> 说明：`fonts/misans/`（4.6 MB）已删除——MiSans 官方条款不允许再分发，随目录带出的 `OFL.txt` 亦不成立。
> 自托管中文字体现为 `fonts/notosanssc/`（子集化 + 字频重切片）。

登记日期：2026-09-12（前端模块化 / Adminator 设计系统）
