# M2 派发计划与工作流清单（2026-09-24）

> **是什么**：M2（两条流：**注释整备流** + **逐文件审查流**）的**派发方案**——基线、分组、每个子代理的输入与产出、门禁、验收、回灌路径、风险与已知面。**本文件只规划，不实施**（用户 2026-09-24：具体实行由用户交接）。
> **状态**：**注释流已执行完毕并合入 develop（2026-09-25）**——见 §11 执行结果；本节从"待放行"转为"已执行记录"。
> 两条流的任务书已就绪且均已标「状态：可执行」、基线 SHA 已填 `9d3f491`：
> `docs/dev/handoff-comment-annotator-prompt.md`（本地，`handoff-*.md` 有意不入库）与 `docs/dev/handoff-review-stream-prompt.md`。
> **范围**：只覆盖 M2。M3（修复流）、M4（前端流）、v3 灰度批、S1 的 SL-* 登记项的处置**不在本文件**，只在 §8 列出接口。
> **读者**：接收派单的协调者（用户）与两条流的执行方。
>
> **2026-09-24 校订**：对冻结提交 `9d3f491` 逐条实测后修订。原稿有 **3 处失效事实**（§9 步 0 的核对命令、
> 「不许碰」worktree 清单、R13/R14 行数）、**1 处覆盖缺口**（15 个生产文件 / 2,563 行不属任何 R 组，
> 其中含 MF-1 必查的脱敏面）、**3 个会让注释流必红的机械门**（物理行尺寸门、2 500 字符窗、
> 三个不剥注释的前端门）。全部落进 §3.5、§4.1、§9、§10，派发前必须读。

**关键词**：M2、注释整备、测试标签、逐文件审查、上下游链路、E2E、性能折算、CLI 黑箱、基线冻结、回灌

## 速览

- **基线**：冻结提交 `9d3f491`（M1 调度 v3 与 S1 安全瘦身都已合入）。⚠ 它**已不是 develop 的 HEAD**——本计划自己被提交成了 `b26736f`（docs-only 子提交），核对方式见 §1。门禁：`ruff` clean；`pytest -q -n auto --dist loadfile` → **2928 passed / 4 skipped / 0 failed / 57.43s**（本次校订未重跑全量，沿用原稿实测值）。
- **两条流并行、同基线、互为只读**：审查流**只读**（只出报告，一行代码都不改），注释流**只动注释/docstring**（AST 等价）。因此**无合并冲突**；代价是注释流提交后**行号会漂**，所以审查流的报告一律用 `文件:符号` 引用（任务书已立此规矩）。⚠ 但"AST 等价 ⇒ 不影响测试"在本仓库**不成立**，有三个按源码文本判的门，见 §3.5。
- **规模**（本次校订逐条实测复核，全部与原稿一致）：生产代码 114 个 Python 文件 / 38,680 行（`yiban` 64/20,193、`web` 29/12,784、`scripts` 20/5,306、`docker` 1/397）+ 34 个 JS 文件 / 10,862 行 + 137 个测试文件。
- **分组**：注释流 **6 组（A1~A6）+ 4 批（B1~B4）**、审查流 L1 **15 组（R1~R15，R15 为本次校订新增）**（见 §3/§4），L2/L3/S 各 1~2 组收口。
  ⚠ 原稿在 §速览 写"注释流 12 组"、§9 写"A1~A11"与"A6/A7"，都是早期 12 组切法的残留；**§3 的表是 A1~A6，以表为准**。
- **用户对本轮的硬要求**：审查流聚焦**单文件逐行逐函数**（业务逻辑 / 代码简化 / 安全）+ **两个跨文件检查**（上下游调用链聚合、全功能 E2E）+ **性能折算**（500/1 000/5 000/10 000，允许仅折算）+ **CLI 黑箱**（硬协议：**只告诉测试项目，遇 bug 或缺少功能前绝不允许读代码**）；允许**只读**拉取生产真实数据、脱敏后检查异常。

## 目录

1. 基线冻结与工作树
2. 两条流的关系（为什么能并行、哪里必须串行）
3. 注释流：分组与派发清单
   - **3.5 ⚠ 三个会让注释流必红的机械门（实测余量）**
   - **3.6 ⚠ `+20%` 注释闸的计数器必须先定义**
4. 审查流：分组与派发清单
   - **4.1b ⚠ 原稿覆盖缺口：15 个生产文件不属任何 R 组**
5. 每个提示词**必带**的纪律段落（可直接粘贴）
6. 门禁与验收
7. 回灌：发现去哪、谁裁决
8. 风险与已知面（含 S1 带来的新事实）
9. 排期建议与并行度（含 worktree 方案与放行前六件事）
10. **子代理模型与档位**

---

## 1. 基线冻结与工作树

| 项目 | 取值 |
|------|------|
| 冻结提交 | `9d3f491`（两条流都以它为准；审查流**只读这个提交**，不看工作区、不看别的分支） |
| ⚠ 冻结 SHA **已不是 develop 的 HEAD** | 本计划自己被提交成了 `develop @ b26736f`（docs-only，`9d3f491` 的唯一子提交）。核对命令**必须**用祖先判定，不能用 `git log -1 develop`：<br>`git merge-base --is-ancestor 9d3f491 develop && echo OK`（实测 OK）<br>注释流分支合回时的目标是 `b26736f`（或更新的 develop HEAD），**不是** `9d3f491`。 |
| 注释流工作树/分支 | 新建 `D:/code/yiban-wt-annotate`，分支 `feature/comments-annotate`（自 `9d3f491` 切出） |
| 审查流工作树/分支 | 新建 `D:/code/yiban-wt-review`，分支 `review/m2-readonly`（自 `9d3f491` 切出；**只为跑 E2E / 性能 / CLI 用，不许提交任何改动**） |
| CLI 黑箱 | 在 `D:/code/yiban-wt-review` 里跑（同一环境，避免"测试环境差异"被当成缺陷） |
| 生产 | 只读，见任务书【二.2】白名单；**只允许 `ssh yiban`**，直连 IP 会被拒 |
| 不许碰（**2026-09-24 按 `git worktree list` 与 `ls /d/code` 实测重列**） | 主工作区 `D:/code/yiban-auto-sign`（`server-web`）；已存在的 worktree `D:/code/yiban-wt-study`（`study/manual-review`）、`D:/code/yiban-wt-v045`（**持有 develop，别在这里切分支**）；非 worktree 的独立副本 `D:/code/yiban-auto-sign-next`、`D:/code/yiban-fyiban`、`D:/code/yiban-preview-feprev`、`D:/code/yiban-preview-mob`（后两个是前端预览，feprev 是前端最新线） |
| 原稿已失效的路径 | `D:/code/yiban-wt-schedv3`、`D:/code/yiban-wt-secslim*` **已不存在**，从纪律段落里删掉，别拿不存在的路径当约束（会让执行方以为清单可信） |

**冻结纪律**：一旦放行，`9d3f491` 不再接受任何改动；若必须修（例如修好一条阻断 E2E 的缺陷），做法是**新建提交并在报告里记账**，不得"就地改完不说"。

## 2. 两条流的关系

```
9d3f491（冻结）
  ├── 审查流（只读）──→ 报告①逐文件 ②链路 ③E2E+性能 ④CLI  ──→ 登记进 must-fix-list
  └── 注释流（只改注释，AST 等价）──→ 分支 feature/comments-annotate ──→ 合并
```

- **可并行**：审查流不写文件，注释流不改语义 ⇒ 两边不会互相踩。审查流**必须**按提交对象审（`git show <sha>:<path>`），不要审工作区。
- **必须串行的两处**：① 审查流的 **L2/L3/S 排在该流 L1 全部完成之后**（契约如此）；② 注释流合入 develop 时，若审查流已提出"注释与代码矛盾"清单，**优先把那些矛盾一并改掉**再合并（否则注释流会把错的注释"整理"得更像对的）。
- **回灌方向**：审查流的产出**不直接改码**，一律进 `docs/dev/must-fix-list.md`（MF-n）或本计划的 §7 裁决表，由后续修复流处置。

## 3. 注释流：分组与派发清单

**范围**（任务书 §三）：**A 生产代码**注释规范化——界定方式是**自上次注释整备以来的改动清单**（权威命令见任务书 §三，基线 `b6457e6..9d3f491`，实测 **73 个文件 / +5,039 −2,041 行**，含 M1 调度 v3 与 S1 安全瘦身两批）；**B 全部测试文件**（137 个 / 2,922 个用例方法）加标签注释 + 生成式索引。

**硬门（违反即整批作废）**：只动注释与 docstring、**AST 等价**（任务书自带自检脚本）、测试断言不得改、分批提交、无任务痕迹（批次号/日期/工单号/"修了什么"）。

| 组 | 文件域（73 个的切分） | 文件数 | 要点 |
|----|---------------------|-------|------|
| A1 | `yiban/store/*`（`db` / `migrations` / `claims` / `queue_store` / `events` / `verify_jobs`…） | 12 | 台账与领取池口径最易写错，四问的"通信"必须写真实调用点 |
| A2 | `yiban/engine/*`（`executor_v3` / `planner` / `hrw` / `token_bucket` / `runner` / `round` / `state_io` / `schedule`…） | 12 | M1 新代码为主，重点核"过时注释"与 V 不变量这类前提 |
| A3 | `yiban/*`（`egress` / `status` / `window` / `logging_ext` / `masking`…）+ `yiban/notify/*` + `yiban/infra/*` | 11 | 出口解析、通知账本、凭据加密 |
| A4 | `web/routes/*`（`settings_api` / `notify` / `users_api` / `accounts_api` / `my` / `me`…）+ `web/{app,security}.py` | 10 | 门禁与鉴权核心，S1 改动集中区 |
| A5 | `web/services/*`（含备份哨兵、周报闸门、验证队列）+ `scripts/*`（含 `backup_sentinel.py`、`ledger_check.py`、`yiban-*.sh`） | 11 | 含 `.sh` 的头部说明与退出码契约 |
| A6 | `web/static/js/*`（core + components + pages）+ `web/templates/pages/*` | 17 | S1 新加的统一提交 helper 与倒计时框；模板只碰逻辑片段 |
| B1~B4 | `tests/test_*.py` 137 个，按 13 个组名切 4 批 | 34/34/34/35 | 文件头标签块（固定字段 `覆盖`/`对应实现`/`关键断言`/`依赖`）+ 只补命名不自解释的函数级标签；产出**生成式索引**带 `--check` |

**范围 A 的取舍说明**：不做全仓 148 个文件的清扫，只做**改动清单**——与本项目既有的"每批改动后随批整备注释"的做法一致（09-19 那批就是 9 个文件的注释修订），也避免把已在历轮审过的注释再动一遍。

**每组的产出**：① 该组文件的改动（分批提交，提交信息只写范围与"注释规范化"级别的技术要点）；② 任务书【六】对应格式的报告（逐文件明细 / 调用点核实 / 注释↔代码矛盾清单 / 只登记不修的代码缺陷 / 待裁决事项）；③ 范围 B 另交索引与依赖分布统计。

### 3.5 ⚠ 三个会让注释流**必红**的机械门（2026-09-24 在 `9d3f491` 上实测，派发前必须写进提示词）

> ⚠ **执行后订正（2026-09-25）：实际是四类门，本节漏了第四类**——措辞门禁连注释一起扫，
> 且是唯一真的把套件跑红的那一类；本节这三个反倒在改落点标准后都没再构成压力。详见 §11.2 与 §11.3。

注释流的前提是"纯注释变更 + AST 等价 ⇒ 行为不变 ⇒ 不必跑测试"（任务书【四.3】明写不跑测试套件）。
**这个前提在本仓库不成立**：有三个门是按**源码文本**判的，加注释就能把它们改红。09-21 那轮已经栽过一次
（注释把某函数撑长约 891 字符，顶红了一条按字符窗口切片的钉测试）。

| # | 门 | 判据（实测） | 暴露面 |
|---|----|-------------|--------|
| 1 | `tests/test_module_size_gate.py` | `_count_lines` = `sum(1 for _ in f)`，即**物理行**；`LIMITS = {.py:600, .js:800, .css:800, .html:400, .sh:400}`；只有登记进 `OVERSIZED`（当前 **22 个**文件）才豁免 | **所有加注释的文件都在消耗额度** |
| 2 | `tests/test_state_file_writes.py::test_signin_state_writers_use_replace` | `src.split(f"def {func}(")[-1][:2500]` 里必须出现 `os.replace`——**按字符窗口切片** | 4 个 WRITERS，实测见下表 |
| 3 | `tests/test_web_design_tokens.py` / `test_web_text_contrast.py` / `test_web_class_hygiene.py` | 扫 `web/templates` + `web/static/js` 的 `.html`/`.js` **原文**；三者 `_strip_comments`/`_COMMENT_RE` 命中数均为 **0**（只有 `test_web_component_adoption.py` 有，命中 6） | **注释里出现色值字面量、`class="…"` 字符串就能顶红**；A6 的 15 个 JS + 2 个模板全暴露 |

**门 1 的实测余量**（A 清单 73 个文件里、**未**登记 OVERSIZED 的，按剩余行数升序）：

| 剩余行 | 现行数/上限 | 文件 | 组 |
|-------|-----------|------|----|
| **4** | 596/600 | `yiban/notify/ledger.py` | A5 |
| **10** | 590/600 | `yiban/engine/round.py` | A2 |
| **29** | 571/600 | `yiban/engine/state_io.py` | A2 |
| **46** | 554/600 | `web/routes/me.py` | A4 |
| **76** | 524/600 | `web/routes/users_api.py` | A4 |
| **89** | 711/800 | `web/static/js/pages/data_dashboard.js` | A6 |
| 107 | 493/600 | `yiban/engine/planner.py` | A2 |
| 145 | 255/400 | `web/templates/pages/data_dashboard.html` | A6 |

> 上一轮 50 个文件共新增 **7,295** 行注释，**平均 ≈146 行/文件** ⇒ `ledger.py`（余 4）与 `round.py`（余 10）
> **不是风险，是必然**。`state_io.py`（余 29）、`me.py`（余 46）大概率同样。

**门 1 的实证（2026-09-25 跑过，不是推断）**：用户提出"注释行不计入门禁"，与实测相反，故留证。
取 `yiban/notify/ledger.py`（596 行，未登记 OVERSIZED）在末尾**只追加 5 行纯注释**，
调用仓库自己的 `tests/test_module_size_gate.py::_size_violations`：

```
ORIGINAL (596)          counted=596  limit=600  violations=NONE
+5 COMMENT LINES (601)  counted=601  limit=600  violations=[('yiban/notify/ledger.py', 601, 600)]
```

三条独立证据都指向同一结论：
1. `_count_lines(path)` 的实现就是 `sum(1 for _ in f)`——**逐行计数，无任何剥注释/剥 docstring 的步骤**；
2. 门禁**自己的自检用例**（`test_gate_rejects_unregistered_oversized`）写入的是
   `f.write("// x\n" * (limit + 1))`，即**一个通篇只有注释行的文件**，并断言它**必须被判越线**；
3. `OVERSIZED` 登记表里的理由文本反复写着"行数含注释契约要求的四问头与函数级说明（**约占四分之一**）"
   ——仓库自己就承认注释体量是超限的成因之一。
   另外全仓 `tests/` 里**不存在**任何"按代码行计量"的尺寸门（搜过 `ast.get_docstring`/`tokenize`/
   `code_lines` 等，命中的都是别的用途，例如 `test_executors_kpi_scope.py` 剥注释是为了
   **防止注释顶替代码**满足断言，方向正好相反）。

**"只看代码行"这条口径的来源与现状**：它是 2026-09-19 用户为"算不算大文件 / 要不要分片 /
注释是否过量"定的**人工判断口径**，写在当时的注释契约 §五。而 `9d3f491` 上的
`docs/dev/python-script-contract.md` **只剩 31 行 / §一~§四**，§五 已不存在 ⇒
这条口径现在**只活在记忆里，没有任何仓库文档或可执行门禁承载它**。
可执行的门只有物理行这一个口径，两者冲突时**门会赢**（它会红）。

**因此有两条路，放行前必须选一条**：

- **路 A（不改代码，本计划默认）**：承认物理行是硬约束，按 §3.5 处置规则 2 对紧余量文件限制注释增量。
  代价：注释整备在最需要注释的几个大文件上反而最束手束脚。
- **路 B（改门禁，需用户明确授权）**：把 `_count_lines` 改成**剥掉注释行与 docstring 后计数**，
  让门禁口径与"只看代码行"一致。这能一次性消解门 1 的全部问题，但是**改动一个可执行门禁**，
  与 M2"执行方绝不许改门禁"的纪律相冲 ⇒ 只能由协调者在**注释流开工之前**作为独立提交做完，
  且要连带处理副作用：现有 22 条 `OVERSIZED` 登记的理由文本大量引用"注释占四分之一"，
  口径一改这些理由就失真，需要重新核；部分已登记文件按新口径可能根本不超限，登记会变成僵尸条目。
  **本计划不替用户选**，默认走路 A（更窄的一侧）。

**门 2 的实测余量**（`os.replace` 在窗口内的字符位置 / 2 500）：

| WRITERS 函数 | 所在文件 | `os.replace` 位置 | 余量（字符） |
|-------------|---------|------------------|-------------|
| `_write_sign_state` | `yiban/engine/state_io.py` | **2 468** | **32** ⚠ |
| `_write_sched_done` | `yiban/engine/state_io.py` | 816 | 1 684 |
| `_write_probe_state` | `yiban/engine/probe.py` | 269 | 2 231 |
| `_user_fail_mail_reserve` | `yiban/engine/alerts.py` | 1 282 | 1 218 |

> `_write_sign_state` 只剩 **32 个字符**：在 `def _write_sign_state(` 与其 `os.replace` 之间写任何一条
> 超过约 32 字符的注释，这条测试立刻变红。`state_io.py` 属 A2，且它同时也是门 1 的余 29 文件。

**处置规则（派发前定死，不留给执行方临场判断）**：

1. **执行方绝不许碰这三个测试文件**，也不许为了容纳自己的注释增量去改 `LIMITS` / 加 `OVERSIZED` 登记 /
   放宽窗口。09-21 那轮就有一个代理自行给 5 个文件加了 900 行的 Oversized 登记、并把日期与"用户裁定"
   写进源码理由——已被判定为**越界改代码 + 任务痕迹**，本轮重犯即整批作废。
2. **协调者在放行时按上表逐个预先裁决**（不要下放）：余量 ≤ 50 行的 4 个文件（`ledger.py`、`round.py`、
   `state_io.py`、`me.py`）由协调者先决定是"限制该文件注释增量到余量内"还是"本轮跳过该文件的行内注释、
   只改既有注释"。**默认取更窄的一侧**（限制增量），放宽要用户点头。
3. **执行方撞上任何一个门变红，一律停下写进报告，不自行修**——写清是哪个门、当前行数/字符位、
   自己加了多少。
4. **每批提交前必须跑这三条**（不是全量，秒级）：
   ```
   python -m pytest tests/test_module_size_gate.py tests/test_state_file_writes.py \
     tests/test_web_design_tokens.py tests/test_web_text_contrast.py \
     tests/test_web_class_hygiene.py tests/test_web_component_adoption.py -q
   ```
   A6（JS/模板）另加 `tests/test_dashboard_stats_caliber_js.py`。任务书【四.3】的"不跑测试套件"
   要改成"不跑全量，但必须跑上面这几条结构钉测试"。

### 3.6 ⚠ `+20%` 注释闸的计数器**必须先定义**（否则 A1~A6 六组各按各的读法）

任务书【二.6】写"范围 A：每文件**行内注释**净增 ≤ +20%（相对该文件**原有注释行数**）"。两个问题：

1. **分子分母口径不一致**：分母是"注释行数"（全部注释行），分子是"行内注释"（字面读=行尾注释）。
   09-21 实测过这个歧义的后果——只数**独立注释行**时 50 个文件里 46 个合规；把**行尾注释 token 一并计入**
   时 `client.py` 达代码行 73%、`window.py` 85%，全线爆表。**两种读法给出相反结论**，不能留给执行方选。
2. **这条规则已经不在契约里了**：`docs/dev/python-script-contract.md` 在 `9d3f491` 上只有 **31 行 / §一~§四**
   （实测），**没有任何 +20% 条款**，也没有"细节优先挂行尾注释"那句。任务书§四.1 却写"摘自契约，
   冲突时以契约为准"——冲突时契约无话可说。所以 +20% 是**只活在任务书里**的孤立规则。

**放行前二选一并写进任务书**（本计划建议 A）：
- **A（推荐）**：删掉 +20% 这条数值闸，改用 §3.5 门 1 的**物理行余量**做硬约束（它可测、且本来就是红的判据），
  另加一条定性要求"新增注释必须能过 ponytail 梯子第一问：这句话需要存在吗"。
- **B**：保留 +20%，但把计数器逐字定义为"**独立成行的注释行**（不含行尾注释 token、不含 docstring）"，
  并写明分母同样只数独立注释行。

## 4. 审查流：分组与派发清单

### 4.1 L1 单文件逐行逐函数（**15 组**：R1~R14 为原稿，R15 为本次校订补缺口）

| 组 | 文件域 | 行数（约） | 备注 |
|----|--------|-----------|------|
| R1 | `web/app.py` + `web/security.py` + `web/render.py` + 入口两文件 | 3.5k | **最高优先**：门禁、CSRF、会话、脱敏都在此 |
| R2 | `web/routes/settings_api.py` + `notify.py` + `auth.py` | 2.5k | S1 改动集中区（门禁三档、告警删除） |
| R3 | `web/routes/{my,accounts_api,users_api,me}.py` + 其余 routes | 3.0k | 越权与 IDOR 重点 |
| R4 | `web/services/*`（13 个） | 2.9k | 备份哨兵、周报闸门、验证队列 |
| R5 | `yiban/store/{db,migrations,claims,queue_store}.py` | 2.6k | 迁移与领取池（服务器版本兼容面） |
| R6 | `yiban/store/{audit_chain,accounts,users,events,cleanup,clock_meta,verify_jobs,…}.py` | 3.4k | 审计链红线、清理链、时钟守卫新语义 |
| R7 | `yiban/engine/{runner,round,state_io,workers,schedule}.py` | 2.6k | 执行链与门 |
| R8 | `yiban/engine/{executor_v3,planner,hrw,token_bucket,alerts,probe}.py` | 2.5k | M1 新代码（含默认关闭的 v3） |
| R9 | `yiban/egress.py` + `yiban/{fyiban,infra}/*` | 2.5k | 出口解析、隔离层、凭据加密 |
| R10 | `yiban/{notify,mail,attempt}/*` + `yiban/cli.py` | 2.9k | 通知账本、邮件、CLI 契约 |
| R11 | `scripts/*`（**逐文件点名，见下**）+ `run.sh` + `docker/*` | 3.0k | 备份、哨兵、审计校验、容器排程 |
| R12 | `scripts/loadtest/*`（6 个）+ `scripts/{build_cjk_font_slices,build_lucide_sprite,stamp_font_versions}.py` | 4.0k | 压测与构建工具（低优先，但 L3 要性能折算，是它的输入） |
| R13 | `web/static/js/core.js` + `components/*`（22 个） | **7.7k**（实测 7 664，原稿 ~6k 偏低） | 前端逻辑：凭据与 CSRF 处理、统一提交 helper、倒计时框 |
| R14 | `web/static/js/pages/*`（10 个，实测 2 791 行）+ `web/templates/**/*.html`（实测 3 033 行）里的**逻辑片段**（Jinja 条件/`\|safe`/URL 拼装/数据注入） | **~5.8k** | 模板只审逻辑，样式与布局归前端线 |
| **R15（新增，补缺口）** | `yiban/` 根 10 个 + `yiban/engine/` 5 个 = **15 文件 / 2 563 行**，逐文件见 §4.1b | 2.6k | **含 MF-1 必查的脱敏面**，原稿无人认领 |

**⚠ R14 必须显式排除 `web/static/vendor/**`**：原稿写"其余 JS"会把它扫进来，而那是 **31 331 行第三方**
（`chartjs/chart.min.js` 压缩产物 + `adminator.css` + 字体 css/svg + `md-render.js`）。仓库自己的尺寸门
在 `os.walk` 里就把 `vendor` 剪掉了 ⇒ 第三方是仓库既有的口径。提示词里要写死："不审 `web/static/vendor/`"，
否则执行方会把整个预算烧在读压缩过的 chart.js 上。

**R11 / R12 的边界要逐文件点名**（原稿"生产脚本"vs"构建工具"是判断题，会重复或漏）：
- 归 **R12**：`scripts/loadtest/*`（6 个）、`build_cjk_font_slices.py`、`build_lucide_sprite.py`、`stamp_font_versions.py`；
- 归 **R11**：`audit_verify.py`、`backup_sentinel.py`、`child_env.py`、`db.py`、`db_export.py`、`email_policy.py`、
  `generate_demo_data.py`、`ledger_check.py`、`list_duplicate_owners.py`、`signin.py`、`state_cleanup.py`；
- **`generate_demo_data.py` 要标红**：已知 P0「demo 可清生产库」在这里，R11 必须显式核这条是否仍在。

### 4.1b ⚠ 原稿覆盖缺口：15 个生产文件不属任何 R 组（实测 2 563 行）

原稿的验收标准是"L1 覆盖 R1~R14 的**全部文件**"，但 R1~R14 **并没有划分整个文件树**。
按 `git ls-tree -r 9d3f491` 逐目录对账（生产 py 共 114 个：`yiban/store` 16、`yiban/engine` 16、
`scripts` 14、`web/services` 13、`yiban` 根 12、`web/routes` 11、`scripts/loadtest` 6、`yiban/infra` 5、
`yiban/fyiban` 5、`web` 根 5、`yiban/notify` 4、`yiban/mail` 4、`yiban/attempt` 2、`docker` 1）：

| 未认领文件 | 行数 | 为什么必须在 R15 |
|-----------|-----|----------------|
| `yiban/client.py` | 395 | 上游 HTTP 客户端 |
| `yiban/state_gc.py` | 258 | 状态回收 |
| **`yiban/security.py`** | 248 | ⚠ 与 R1 的 `web/security.py`（760 行）**同名不同文件**，极易被当成已覆盖 |
| `yiban/window.py` | 246 | 签到窗口几何（"同一份几何被多处消费"的分叉面） |
| **`yiban/masking.py`** | 149 | ⚠ **MF-1「生产版本没有隐私遮罩」的实现就在这里**，任务书§七把它列为**本轮必查项**，却没有组认领 |
| `yiban/cred_state.py` | 139 | 凭据状态落盘 |
| `yiban/status.py` | 115 | ⚠ 与 masking/logging 同属隐私输出面 |
| `yiban/logging_ext.py` | 91 | ⚠ 日志脱敏挂载点，MF-1 取证要顺着它走 |
| `yiban/clock.py` | 35 | 时钟守卫新语义（S1 改过） |
| `yiban/__init__.py` | 14 | — |
| `yiban/engine/attempts.py` | 279 | 重试计数（幂等面） |
| `yiban/engine/accounts.py` | 253 | 账号装载（签到主链第一跳） |
| `yiban/engine/cli_support.py` | 244 | ⚠ S 专项（CLI 黑箱）的实现侧，没人认领就没法定性 |
| `yiban/engine/config_check.py` | 64 | ⚠ 最近三个提交（F1/F2/F3）全在改它 |
| `yiban/engine/__init__.py` | 33 | — |

> **结论**：原稿的验收标准①"L1 覆盖 R1~R14 的全部文件"**不可达**，而且缺的正好是 MF-1 必查项与
> CLI 定性所需的实现侧。必须加 R15（或把这 15 个并进 R9/R7 并重算行数），否则必查项无主。

**每组产出一份 L1 报告**（任务书【八】模板），逐文件独立产出、不要攒到最后。**L1 必须按提交对象审**。

### 4.2 L2 上下游链路聚合（L1 完成后）

1 组（可拆 2 组并跑，但结论要合并）。按任务书【四】点名的 5 条链逐链给结论：**签到主链 / 补签链 / 数量与状态一致性 / 配置链 / 写入与恢复链**。判据：找"每个文件单独看没问题、连起来跑才出问题"的。

### 4.3 L3 全功能 E2E + 性能折算

1 组。**E2E** 覆盖任务书【五.1】列的全部功能与全部情况（~18 格矩阵），逐格给"通过/失败/未覆盖+原因"。**性能**按 500 / 1 000 / 5 000 / 10 000 四档折算，**必须展示算式**，并与 `docs/refactor/40-capacity-estimate.md` 与设计 §11 对账，差异要解释。

### 4.4 S CLI 黑箱专项

1 组，**协议最硬**：只给"测试项目"清单（任务书【六.3】）→ 只用 CLI 自己的输出判断 → **遇 bug 或找不到功能之前绝不允许读代码** → 触发后只读最小范围 → 记录卡住点。对人（人类按 `README.md` 能不能用）与对 agent（非交互 / `--json` / 退出码 / 错误自解释）两条线分开评。

### 4.5 生产只读取证（贯穿 L1/L3）

不是单独一组：**隐私类问题必须用生产数据取证**（例如"某日志行/某接口出现未脱敏值"）。授权与红线见任务书【二.2】【二.2b】，其中**四条统计纪律**（必须按 `stage` 过滤、不许用 `LIMIT` 截断下"不存在"的结论、同天多行不必然是重复、`(phone, day)` 领取池是权威互斥面）**已写进任务书，必须原样带上**。

## 5. 每个提示词必带的纪律段落（可直接粘贴）

```
【纪律】
1. 只在指定工作树/分支工作；绝不改主工作区 D:/code/yiban-auto-sign 与其它 worktree；
   绝不 git stash pop/apply；不 push；不用 git add -A（未跟踪的 nul 必须保持未跟踪），提交时显式列路径。
2. git 走 Git Bash（Windows）；pytest/ruff 走 WSL。worktree 的 .git 指向 Windows 路径，
   在 WSL 里跑 git 会 fatal: not a git repository。
3. 注释遵守 docs/dev/python-script-contract.md：四问头部（功能/归属/复用/通信，通信须写真实调用点）、
   函数注释只写「为什么」、无任务痕迹（批次号/日期/工单号/"修了什么"）、跨文件引用用符号名不用行号。
4. 脱敏：日志、文档、报告里的手机号一律 138****0000，邮箱 a***@qq.com；贴日志前先脱敏，
   只保留能支撑结论的最短片段。
5. 审查流一行代码都不许改；注释流只动注释/docstring（AST 等价，自带自检脚本）。
6. 门禁：ruff check yiban/ tests/ scripts/ --quiet && python -m pytest tests/ -q -n auto --dist loadfile
   ——不要用裸 -n auto（会把同一个类拆到不同 worker，叠加进程级 DB 单例 ⇒ 确定性 401）。
7. 已知负载/时序敏感的间歇用例（单文件串行复跑全绿即算间歇，不算门禁失败）：
   tests/test_sign_round_guards.py::BatchSignCooldownTest::{test_cooldown_blocks_immediate_retry,
   test_cooldown_zero_disables,test_single_signin_shares_global_cooldown}、
   tests/test_login_e2e_mock.py::KillYiBanE2ETest::test_full_chain_rehearsal、
   tests/test_login_e2e_mock.py::LegacyLoginE2ETest::test_legacy_chain_rehearsal、
   tests/test_verify_jobs.py::VerifyJobIntegrationTest::test_admin_submit_creates_active_account_rejected_on_failure。
8. 有疑问先按「这条在守什么」判断，不要为了让东西通过而放宽断言或跳过检查。
9. AST 等价的 before-image 一律取**冻结提交**：`git show 9d3f491:<path>`，**不要用 `HEAD:`**。
   理由：任务书【四.2.e】写的是 `git show HEAD:{path}`，但本流分批提交，第一批提交之后 HEAD 就变成了
   自己的上一批产物——同一个文件改第二遍时会拿"自己刚写的版本"当基线，越界改动会被判成 AST-EQUIVALENT。
   冻结提交不可变，才是唯一可靠的基线（09-21 也踩过同类坑：before-image 不一定是 HEAD）。
10. **不许写共享汇总文件**：`docs/dev/must-fix-list.md`、任何 `TICKETS.md`、`docs/dev/test-inventory.md`
    一律**只读**。发现写进**你自己那份报告**，编号由协调者统一分配。
    理由：并行代理同写一个文件会互相覆盖，09-21 那轮因此产生 18 个跨文件撞名的工单号、142 条有正文无索引。
11. **不许写记忆目录**：`~/.qoder-cn/memory/`、`~/.qoder-cn/projects/*/memory/` 与任何 `MEMORY.md` 一律不碰。
    理由：上一轮的"只读"审计代理偷偷创建了记忆文件并改了索引。
12. **限流与调用预算**（上一轮有代理在 52 次调用 / 600 万 token / 19 分钟处平台报错死亡，且磁盘零写入）：
    单代理 ≤45 次工具调用；不整读 >400 行的文件（分片读并写明未覆盖区间）；`grep` 一律接 `head`；
    `git diff` 用 `--stat`；`pytest` 用 `-q` 且只跑相关文件。**交付物按价值排序**：先把该落盘的产物落盘自证，
    再做复核与报告。
13. **并行同 worktree 会互相打断测试**：另一代理正在写某文件时，你的 `pytest` 会撞出大量 collection error
    （09-21 实测 51 个）。跑不了就在报告里写"因并行写未能执行"，**不要当缺陷报**。
```

## 6. 门禁与验收

| 流 | 每批门禁 | 整批验收 |
|----|---------|---------|
| 注释流 | **§3.5 的六条结构钉测试**（秒级，每批必跑）+ `ruff` + **AST 等价自检**（before-image 用 `9d3f491`，见 §5 第 9 条）+ 索引 `--check`；全量 `pytest` 由**协调者**在每批合并点跑，不下放给执行方 | ① 137 个测试文件都有文件头标签块；② 索引可生成且 `--check` 绿；③ 代码缺陷只登记未修；④ 无一处断言改动；⑤ **`test_module_size_gate.py` 未被改动、`OVERSIZED` 登记数仍是 22**；⑥ 全量 pytest 与基线 `2928 passed / 4 skipped / 0 failed` 逐字一致 |
| 审查流 | 不适用（只读；跑 E2E/CLI 时不许写仓库，临时文件放 `/tmp`） | ① L1 覆盖 **R1~R15** 的全部文件（逐文件一份报告），且**显式声明未审 `web/static/vendor/`**；② L2 五条链逐链结论；③ L3 矩阵逐格 + 性能四档含算式；④ CLI 黑箱记录含卡住点；⑤ 隐私类发现必须有生产取证，且 **MF-1 的结论必须落到 `yiban/masking.py` / `logging_ext.py` / `status.py` 的具体符号**（这三个文件在 R15，原稿无主） |

**放行前要顺手改掉的两处任务书数字**：
- 注释任务书【五.1】写"全部测试文件都要，**126 处**"，而【三】范围 B 写的是 **137 个文件**——
  `126` 是旧快照，改成 137（实测 `git ls-tree -r 9d3f491 | grep -cE '^tests/test_[^/]*\.py$'` = 137；
  另有 4 个非 `test_*` 的 `tests/*.py`：`_frontend_src.py`、`_mail_body.py`、`_wcag.py`、`conftest.py`，**不在**范围 B）。
- 审查任务书【一】的表格与【八】都按 L1/L2/L3/S 写，没有 R15；加组后要在派发提示词里点名，
  任务书本身不必改（它只定义层次，不定义分组）。

## 7. 回灌：发现去哪、谁裁决

| 产出 | 去向 |
|------|------|
| 代码缺陷（审查流 L1/L2/L3/S） | `docs/dev/must-fix-list.md` 新编号 **MF-4 起**（实测当前最大编号是 MF-3，MF-4 起编号正确），每条含现象 / 证据 / 处置时点。**⚠ 只有协调者能写这个文件**：并行代理一律把发现留在自己报告里，由协调者统一编号后登记（见 §5 第 10 条） |
| 注释↔代码矛盾（注释流） | 注释流**就地改**注释；若矛盾根因在代码，则登记进 must-fix-list |
| 测试质量问题（顺序/时序依赖、同义反复断言、脆断言） | 登记进 must-fix-list（用户在 2026-09-23 已点名要审查流覆盖"测试质量"） |
| 需要用户拍板的设计级问题 | 报告【八】的"待协调者裁决事项"逐条列出；本计划 §8 的 SL-1 / SL-3 等已在此列 |
| S1 的 SL-* 登记项 | 由**修复流**消费，不属 M2；但审查流若在代码里看到它们的现场，按发现登记 |

## 8. 风险与已知面（含 S1 带来的新事实）

1. **S1 改了默认安全姿态**：`YIBAN_PW_GATE` 缺省 `risk`（危险操作默认不再要口令，仅换环境时要求一次）。审查流**不要**把"默认不要口令"当成缺陷——那是用户拍板的；要审的是**三档语义是否自洽、失败路径是否仍有告警、`off`/`risk` 下是否留下了本该有的补偿信号**。
2. **S1 删了 15 处告警与两簇功能**（rekey 工具链、clock_guard 持久化冻结）。审查流不要报"缺少 XX 告警/工具"，除非能指出**具体后果**。
3. **待裁决项（M2 报告里不必重复提，除非找到新证据）**：`SL-1`（急停的即时告警随之下线，是否保留一条）、`SL-3`（跨用户名喷洒判据的可达性）、`SL-5`（执行体保存两段请求无法原子）、`SL-13`（周报闸门每天只评估一次，"评估后才耗尽"的告知仍会丢）、`SL-16`（`test-inventory.md` 无生成脚本）。
4. **注释流的行号漂移**：审查流与注释流并行时，审查报告一律用 `文件:符号`。若两者交叉阅读，以**冻结提交**为准。
5. **CLI 黑箱的诱因**：CLI 里有一条"遇 bug 前不许读码"的硬协议，执行方**很可能在中途想读代码**——任务书要求把"想读的那一刻"记下来（那是引导质量的直接证据），协调者不要提前放行。
6. **性能折算不是实测**：用户明确"可以仅折算"。要求是**算式可复核 + 与既有测算对账 + 标注哪些是实测、哪些是假设**。
7. **生产只读的越界风险**：授权很硬（白名单 + 禁读口令/密钥/`.env` 敏感值）。协调者放行时**再贴一次**任务书【二.2】【二.2b】，并要求执行方在报告里声明"只用了白名单命令"。
8. **规模提醒**：L1 是 **15 组** × 每份报告，注释流是 **6（A）+4（B）组**。建议**分组放行、逐组验收**，不要一次把 25 个提示词全发出去——单组失败时要能只重跑那一组。
9. **⚠ 注释流会把非行为测试改红（本次校订新发现，最高优先）**：详见 §3.5。三个门按源码文本判：
   物理行尺寸门（`ledger.py` 只剩 **4 行**、`round.py` **10 行**）、2 500 字符窗（`_write_sign_state` 只剩
   **32 字符**）、三个不剥注释的前端门。而任务书【四.3】明写"不跑测试套件"⇒ 执行方不会发现，
   协调者会在合并点一次性收下一堆红且无法归因。**放行前必须先按 §3.5 处置规则 2 逐个裁决余量 ≤50 行的 4 个文件。**
10. **⚠ 审查流有 15 个文件无主（本次校订新发现）**：详见 §4.1b。缺的正好是 MF-1 必查的脱敏面
    （`yiban/masking.py`、`logging_ext.py`、`status.py`）与 CLI 定性所需的 `engine/cli_support.py`。
    另有同名陷阱：R1 审的是 `web/security.py`（760 行），而 `yiban/security.py`（248 行）**无人审**。
11. **`+20%` 注释闸不可测**：详见 §3.6。计数器未定义，两种读法给出相反结论；而且这条规则**已不在契约里**
    （`9d3f491` 上的契约只有 31 行 / §一~§四，无任何数值闸）。放行前必须删掉或把计数器逐字定义。
12. **MF-1 的口径要对齐**：本计划 §7 原稿写"MF-1 已修"，而审查任务书§七把 MF-1 列为"生产版本没有隐私遮罩，
    本轮**必查项**"。两者不一定矛盾（代码已修 / 生产未部署），但**派发前要确认是哪一种**，
    否则 R15 的执行方不知道该验代码还是该验生产。
13. **⚠ 文档入库口径：用户新规与仓库既有 `.gitignore` 相冲，放行前必须裁定**
    - **用户 2026-09-25 指令**：文档一律不入库，除非特别指定（如 API 文档与 README）。
    - **仓库现状（实测 `9d3f491:.gitignore`）**：第 83-86 行 `docs/*` 确实整体不跟踪
      （注释写"内部运维/设计/调研文档仅本地留存，2026-08-22 起整体移出版本控制"），
      **但第 88 与 152-153 行有负向规则 `!docs/dev/` + `!docs/dev/**` 把 `docs/dev/` 整个放行**，
      注释写"开发文档（模块地图 / API 契约）例外放行"。
      ⇒ 实际被跟踪的 `docs/dev/` 文件有 **21 个**，其中约 16 个是带日期的内部计划/设计稿
      （`m1-task-rearrangement-20260923.md`、`security-slim-plan-20260923.md`、`reviewfix-*-20260922.md`、
      `ledger-dual-version-design-20260923.md`、`upgrade-data-migration-design-20260923.md`、
      `review-t5/t6/t7-independent-20260923.md`…），**超出"只放 API 文档与 README"的范围**。
    - **已经撞上的实例**：本计划自己被提交成了 `develop @ b26736f`，正是新规要排除的那类文稿。
      主工作区（`server-web`）里另有一份**未跟踪**的同名文件，本次校订只改了这份 ⇒ 两份已分叉（差 295 行）。
      撤销 `b26736f` 属改写 develop 历史／影响共享状态，**必须用户点头才做**，本计划不擅自处理。
    - **⚠ 一刀切会弄坏工作流**：两份任务书都要求执行方读 `docs/dev/python-script-contract.md`（注释流的"唯一尺子"）
      与 `docs/dev/test-inventory.md`（范围 B 的分组清单）。它们**之所以在新切出的 worktree 里存在，
      正是因为被跟踪**。若把 `docs/dev/` 整体移出版本控制，从冻结 SHA 新切的 worktree 里这两份会消失，
      任务书的必读项直接悬空。同理 `must-fix-list.md`（§7 的回灌目标）与 `release-gate.md`。
    - **建议的裁定（默认取更窄的一侧，等用户确认）**：把"特别指定"明确成一份**白名单**——
      `README.md`、`docs/dev/README.md`、`docs/dev/api-*.md`、`docs/dev/cli.md`、
      外加工作流依赖的 `python-script-contract.md` / `doc-writing-contract.md` / `test-inventory.md` /
      `must-fix-list.md` / `release-gate.md`；**其余带日期的计划/设计/审查稿一律不入库**
      （本文件、两份任务书、`reviewfix-*`、`*-design-*`、`security-slim-plan-*`、`m1-task-rearrangement-*`、
      `reviews/**` 都归此类）。落地方式是**收窄 `.gitignore` 的负向规则**（改成逐文件放行），
      而不是靠纪律——现状已经证明纪律挡不住（`b26736f` 就是漏网的）。
    - **对 M2 的直接影响**：注释流的 `docs/dev/test-index.json`（任务书【五.3】的产出）与
      `scripts/test_index.py`（`--check` 自检脚本）要先定归属——索引是文档还是产物？
      脚本在 `scripts/` 下不受 docs 规则约束，但索引若属"不入库文档"，`--check` 在全新 checkout 上就无基准可比。
      **放行前一并裁定。**

## 9. 排期建议与并行度

| 序 | 动作 | 并行度 | 说明 |
|----|------|-------|------|
| 0 | 建 worktree（见下"并行度与 worktree"）；核对冻结 SHA；按 §3.5 处置规则 2 裁决 4 个紧余量文件；按 §3.6 定 +20% 计数器；改注释任务书【五.1】的 `126`→`137` | — | **任务书已标「状态：可执行」且基线 SHA 已填 `9d3f491`，原稿这一步已做完，别再当待办** |
| 1 | **审查流 L1**（R1~**R15**）与**注释流 A 批**（A1~**A6**）同时开 | 审查流 3~4 组并跑；注释流**≤2 组**并跑 | 两流域不重叠（审查只读）；先放 **R1/R2/R3 + R15**（最高风险区，R15 含 MF-1 必查面）与 **A4/A5**（门禁与备份核心）。原稿写的"A6/A7"不存在（只有 A1~A6），且 A6 是前端 JS/模板、受 §3.5 门 3 约束，**不适合当首批** |
| 2 | **审查流 L2** 与**注释流 B 批**（测试标签）同时开 | L2 1 组；B 批 2 组并跑 | L2 依赖 L1 全完；B 批量大但机械 |
| 3 | **审查流 L3 + S** 与注释流收尾（索引 + 合并准备） | L3 1 组、S 1 组可并跑 | S 的黑箱协议独立 |
| 4 | 注释流合入 develop（先把审查流报出的"注释↔代码矛盾"改掉） | — | 门禁全绿后合并；**合并目标是 develop 当前 HEAD（`b26736f` 或更新），不是 `9d3f491`** |
| 5 | 审查流报告汇总 → must-fix-list（MF-4 起，**协调者统一编号**）+ 裁决清单交用户 | — | 此后进 M3 修复流 |

**⚠ 并行度与 worktree（本次校订新增，直接影响能不能并跑）**：
原稿让注释流"各 3~4 组并跑"却只给**一个** worktree（`yiban-wt-annotate`）。09-21 实测过后果：
一个代理正在写 `claims.py`（一度不可编译）时，另一个代理的 `pytest -k` 撞出 **51 个 collection error**。
三选一，**建议第 1 种**：

1. **每组一个 worktree**（`yiban-wt-annotate-a1` … `-a6`），同分支不同 worktree 会冲突 ⇒ 每组一条子分支
   （`feature/comments-annotate-a1` …），最后由协调者按 A1→A6 顺序串行合并。代价是 6 个目录，收益是可真并跑。
2. **单 worktree 但串行**：一次只跑 1 组。最稳，最慢。
3. **单 worktree 并跑 + 禁跑测试**：并跑但任何代理都不许跑 `pytest`（只跑 §3.5 那六条结构钉测试时也会读到
   别人的半成品 ⇒ 仍不安全，**不推荐**）。

审查流是只读的，**多个 R 组可以共用一个 `yiban-wt-review`**，不受此限制；但 L3/S 要跑 E2E 与 CLI，
会产生临时库与状态文件，**L3 与 S 各自单开 worktree**，别和只读的 L1 组混在一起。

**放行前协调者要做的六件事**：
① 冻结 SHA 核实——用 `git merge-base --is-ancestor 9d3f491 develop && echo OK`，
**不要**用 `git log -1 --format='%H' develop`（那现在返回 `b26736f`，是本计划自己的 docs 提交）；
② worktree 按上面的方案建好且干净；
③ 按 §3.5 处置规则 2 逐个裁决余量 ≤50 行的 4 个文件（`ledger.py` 4 行 / `round.py` 10 行 /
`state_io.py` 29 行 / `me.py` 46 行）——**默认取更窄的一侧**，放宽要用户点头；
④ 按 §3.6 二选一定下 `+20%` 计数器（建议 A：删掉数值闸，改用物理行余量）；
⑤ 改注释任务书【五.1】的 `126 处`→`137 处`，并把【四.3】的"不跑测试套件"改成"不跑全量，
但必须跑 §3.5 列的结构钉测试"；
⑥ 确认 §8 第 12 条的 MF-1 口径（验代码还是验生产），再决定 R15 提示词怎么写。

## 10. 子代理模型与档位（本次校订新增）

**结论先给**：子代理用哪个模型由**代理定义文件的 frontmatter `model:` 字段**决定，可选值只有六档；
唯一能压过它的是环境变量。

| 机制 | 取值 / 位置 | 证据 |
|------|-----------|------|
| **代理 frontmatter `model:`** | **`inherit`（默认）、`lite`、`efficient`、`auto`、`performance`、`ultimate`** | 〔实测〕Qoder CN 0.4.2 内置 agent-creator 技能文档第 90 行；运行时校验集 `new Set(["auto","ultimate","performance","efficient","lite"])`，`inherit` 另行处理。文档全路径：<br>`D:\Program Files\Qoder CN\.qoder-versions\0.4.2\resources\app.asar.unpacked\node_modules\@qoder-ai\qoder-cn-agent-sdk\dist\_worker\builtin\agent-creator\SKILL.md` |
| 其它可用 frontmatter 字段 | `effort: low\|medium\|high\|max`、`maxTurns`、`timeoutMins`、`tools`、`disallowedTools`、`skills`、`color` | 〔实测〕同上 `SKILL.md:89-94` |
| **环境变量（优先级最高）** | `QODERCN_SUBAGENT_MODEL` | 〔**推断**〕运行时 `resolveAgentModel` 读 `xr("SUBAGENT_MODEL")`，前缀常量 `Os ? "QODERCN_" : "QODER_"`，而 bundle 里确有 `QODERCN_CONFIG_DIR` 等字面量 ⇒ CN 构建前缀为 `QODERCN_`。**变量名是运行时拼的，未在 bundle 里以完整字面量出现**，用之前先 `QODERCN_SUBAGENT_MODEL=ultimate` 起一个探针代理验证 |
| CLI 参数 | `-m/--model` 设的是**会话**模型；`model: inherit` 的子代理跟随它 | 〔实测〕参数表含 `["--model","model"]`；**没有** `--subagent-model`（搜索 0 命中） |
| settings.json | **无**任何按代理配模型的键 | 〔实测〕`agents`/`agentModels`/`subagentModel`/`modelAliases` 四个字符串在 bundle 里 0 命中；`~/.qoder-cn/settings.json` 只有 `enabledPlugins`/`mcpServers`/`providers`；本仓库 `.qoder/` 下只有 `agents/`、`worktrees/`，**无 `settings.json`** |

**优先级（高→低）**〔实测自 `resolveAgentModel`〕：`pinToParentModel` ＞ 环境变量 `QODERCN_SUBAGENT_MODEL` ＞
单次调用的内部 `modelOverride`（**未**作为 Agent/Task 工具的参数暴露，派发时改不了）＞
**代理 frontmatter `model:`** ＞ `inherit` 回退链（调用方快照模型 → 活动模型 → 默认模型）。

**本仓库现状**：`.qoder/agents/function-reviewer.md` 已存在，frontmatter 是
`tools: [Read, Write, Edit, Grep, Glob, Bash, Skill, WebSearch, WebFetch]` / `model: inherit` / `maxTurns: 500`。
⚠ 它的正文第 2、3 步要求读 `docs/dev/review-rubric.md` 与 `PROMPT.md`——**这两个文件在 `9d3f491` 上都不存在**
（实测 `git cat-file -e` 均 ABSENT；rubric 已被删、契约并成一份）。直接复用这个代理会让它去读两个死路径。
⇒ **M2 若走 Qoder 子代理，必须先改这个文件**（或新建 M2 专用代理），把第 2、3 步指向
`docs/dev/python-script-contract.md`（31 行）与两份任务书。

**六档的官方语义**〔实测，同上 `SKILL.md:126-140`，档位线性递增、越高越耗额度〕：

| 档 | 官方定位 | 
|----|---------|
| `inherit` | 用调用方的模型。**默认且安全的选择** |
| `lite` | 简单问答、轻量任务 |
| `efficient` | 日常编码、代码补全 |
| `auto` | 复杂任务、多步推理 |
| `performance` | 难的工程问题、大型代码库 |
| `ultimate` | 最强能力，质量最重要时 |

官方同页还给了一条选型原则，**正好就是 M2 的形状**（原文）：
> Prefer `inherit` unless the agent has a clear reason to differ from the caller
> (e.g. a focused security auditor that benefits from a stronger tier,
> or a high-volume helper that should stay on a lighter tier).

即：**聚焦的安全审计代理该升档，高频的辅助代理该降档**。M2 的审查流就是前者、注释流就是后者，
所以下表偏离 `inherit` 是有官方依据的，不是随意拍的。

**按流的档位建议**（本计划的推荐，不是既定事实；用户可直接改）：

| 流 / 组 | 建议 `model:` | 建议 `effort:` | `maxTurns` | 理由 |
|--------|-------------|--------------|-----------|------|
| 审查流 R1/R2/R3/**R15**（门禁、越权、脱敏） | `ultimate` | `high` | 500 | 需要跨文件推理与攻击面想象，且承载 MF-1 必查项 |
| 审查流 R4~R12 | `performance` | `high` | 300 | 常规业务逻辑与简化审查 |
| 审查流 R13/R14（前端） | `performance` | `medium` | 300 | 体量大（7.7k + 5.8k），要留预算给分片读 |
| 审查流 L2（链路聚合） | `ultimate` | `max` | 500 | "单看每段都对、连起来才出问题"最吃推理 |
| 审查流 L3（E2E + 性能折算） | `performance` | `high` | 400 | 要跑命令、算算式 |
| 审查流 S（CLI 黑箱） | `efficient` | `medium` | 200 | **故意降档**：黑箱协议要模拟"只会读 --help 的人/agent"，档位太高会自行脑补出引导里没有的知识，污染结论 |
| 注释流 A1~A6 | `efficient` | `low` | 150 | 机械整备；上一轮实测 54~55 次调用即可收工，高档位是浪费 |
| 注释流 B1~B4 | `lite` 或 `efficient` | `low` | 120 | 最机械（打标签 + 生成索引） |

**两条落地提醒**：
- 新代理类型要用户执行 `/agents reload` 才会注册；注册前只能派 `general-purpose`（内置 150 轮上限，
  一个 123 行的 Tier A 文件实测会撞顶被截断）。
- `maxTurns` **不在 `settings.json`**，只能按代理类型写在定义文件里。上一轮的崩溃发生在
  **52 次调用 / 600 万 token / 19 分钟**且磁盘零写入 ⇒ 档位越高单次消耗越大，
  §5 第 12 条的限流纪律比 `maxTurns` 更能防崩，两者都要带上。

## 11. 执行结果（2026-09-25 回填；本节是事实，不是计划）

**状态变更**：注释流**已执行完毕并合入 develop**，本文件从"待放行的方案"变成"已执行方案的记录"。
审查流不在本节范围（由另一会话指挥）。

### 11.1 落地数字

| 项 | 值 |
|---|---|
| 改动文件 | **195 个**（范围 A 71 个存活生产文件 + 范围 B 137 个测试文件 + 交付物 2 个） |
| 行差 | **+2,836 / −1,761**（净 +1,075） |
| 提交 | A1~A6、B1~B4 共 10 组 + 索引批 + 门禁修正，develop 上 `9d3f491..HEAD` 共 **57** 个 |
| develop 走过的点 | `9d3f491` → `b26736f`（本计划入库）→ `c0a74d8`（注释流合并）→ `e6cd488`（MF-27 注释订正）→ `72ab867`（索引合并） |
| 跨组文件重叠 | **0**（10 组两两不相交，A 未碰 `tests/`、B 未碰非 `tests/`） |
| 行为中性 | 195/195 判定为纯注释改动（AST 等价 + 前端剥注释归一化），检查器做过双向反向对照 |

### 11.2 ⚠ §3.5 只列了三类门，实际是四类——第四类是唯一真改红的

`tests/test_rekey_key_source.py::PasswordPolicyParityB14Test::test_ambiguous_wording_is_gone`
按**裸字符串**扫 `web/services/accounts_data.py`、`web/security.py`、`web/app.py`
**加 `PW_TEMPLATES` 的聚合前端源码（模板 + 递归 include + 外链 JS）**，且判据明写"**含注释**"。
注释流两处踩中并真的把套件跑红：A6 在 `core.js` 写"改签名要同时看**两类**调用点"（与口令策略无关的普通用词），
以及本计划协调者自己写的注释——**为解释为何禁用某写法而把被禁写法复述了一遍**。
已在 `5bc77cc` 改写修掉，之后 `test_rekey_key_source.py` 全组 94 passed。

**方法论修正（比这条缺陷本身更重要）**：§3.5 和 §5 依赖的两种证明——AST 等价、剥注释后逐字比较——
**都把注释当噪声丢掉**，而第四类门恰恰只看注释。所以
**"195/195 纯注释改动"不等于"不会改红测试"，它只保证可执行语义不变**；
凡按源码文本判定的门必须靠跑测试兜，等价证明兜不住。§3.5 的"三类门"应按四类理解。

### 11.3 新落点标准回收了行额度（推翻 §3.5 的紧张前提）

用户把落点标准改为"拆开、就近、行尾优先、不写词汇表式散文块"后，同一批文件**反而变短**：

| 文件 | 旧落点（头部散文） | 新落点（拆散+行尾） | 变化 |
|---|---|---|---|
| `yiban/engine/round.py` | 590/600（余 10） | **547**（余 53） | −43 |
| `yiban/engine/state_io.py` | 571/600（余 29） | **563**（余 37） | −8 |
| `_write_sign_state` 字符窗位 | 2448/2500（余 52） | **2257**（余 243） | 余量 ×4.7 |
| `yiban/notify/ledger.py` | 600/600（余 0） | **599**（余 1） | −1 |
| `web/app.py` | 2579 | **2521** | 撤下 45 行版本史注释块 |

⇒ A2 首批"round.py 余量不足、无法按要求整备"的结论**被否证**：余量紧张是旧落点风格的产物。
派发注释类工作的正确第一问不是"还剩多少行"，而是"**现有注释有多少被错放在头部**"。
`+20%` 数值闸按 §3.6 方案 A 取消，改以物理行余量为硬约束，执行中未发生冲突。

### 11.4 门禁基线需按环境重写（§速览 那组数字不可复现）

本文件与两份任务书都写死 `2928 passed / 4 skipped / 0 failed / 57.43s`。
本机（Windows `.venv`，非 WSL）实测本底是 **6 failed / 2900 passed / 7 skipped / 19 errors / 84~87s**：
4 只 `BackupDockerScriptTest`（无 docker）、`test_delay_ack_frontend.py` 1 fail + 该文件 19 errors
（= **MF-12**，node harness 缺 `encoding="utf-8"`）。WSL 侧 `~/.venv-yiban-wsl` 无 pytest 且无 docker，
⇒ 那组数字取自第三套环境且未记录来源。已登记 **MF-37**；以"0 failed"作放行条件在本机永远不成立。
另有 `ManualSignExitTest` 两只**只在全量套件下红、单独跑串行与 `-n 8` 各 3 次全绿**，
基线树红的是另一只 ⇒ 跨文件干扰，登记 **MF-38** 且不计在注释流账上。

### 11.5 交付物

- **索引**：`scripts/test_index.py`（312 行，尺寸门内）+ `docs/dev/test-index.tsv`（2,928 条）。
  类别列 `??` **0** 条；"守什么"列 `??` 1,525 条（命名已自解释者，刻意不猜）；
  node/bash 列：无需 123 / 仅 node 5 / 仅 bash 9。
  `--check` 经协调者破坏性验证：改标签组字母、删索引行均退出码 1 并给 unified diff，还原后 0。
  ⚠ **`test-index.tsv` 必须入库**——`--check` 要跟它比对，不入库则新 checkout 无基准。
- **回灌**：`must-fix-list.md` 新增 **MF-4 … MF-39**（高 2 / 中 8 / 低-中 5 / 低 16 + 索引与门禁类 4），
  下一空号 **MF-40**。两条高危均经协调者本人复跑确认。
  ⚠ 附带查出**门禁命令本身有盲区**：`ruff check yiban/ tests/ scripts/` 不含 `web/`，
  而 `web/` 是 29 个 py / 12,784 行的最大面，那里 2 处 RUF100 自基线就存在、从没人看见（MF-39）。
- 各份报告与派发动作书、落点范本、验收工具在 `C:/Users/Frostpolaris/qoder-scratch/m2-annotate/`
  （**个人 scratch，不在仓库内**）——`REFERENCE.md` / `DISPATCH-COMMON.md` / `spec-*.md` /
  `report-*.md` / `CONSOLIDATED-findings.md` / `astcheck.py` / `verify-all.py`。
  ⚠ 属 [[feedback-scratch-tool-reproducibility]] 说的断链风险：别人 checkout 后拿不到这些，
  要长期留存需在"清无关文档"那一步一并决定去向。

### 11.6 派发过程的教训（协调者侧，写下来免得再犯）

- 两个代理（B1、B3）**中途死掉但文件已全部改完并提交** ⇒ 死代理先查落盘再决定重派范围；
  本次是接管（我核为其未提交部分为纯注释后代提交），没重做已完成的活。
- **代理自报数字两次被推翻**：B4"279 只文件含明文号"（`tests/` 总共 137 只，不可能）实为 71 只/714 处；
  B1 的源文本断言计数三口径互斥 ⇒ 只能记"方向成立、计数待取证"。
- 上一批代理**往注释里加了 6 处工单号**、并有一条 `依赖：` 谎称"无 skip"（实有 2 处 `self.skipTest`），
  收尾批查出并修掉 ⇒ `依赖：` 这类如实描述字段要抽查，不能只看填没填。
- 我自己制造的全部故障：三个全量套件同时跑、`-n 4` 砍掉并行度、`/tmp` 里数日攒下的 5,270 个孤儿临时目录、
  管道末端 `| tail` 把输出全缓冲导致误判"卡死"、一个 powershell 探针假报 0 进程、
  以及两次**空操作破坏性探针**（`grep -v` 删的是不存在的行；`$?` 取到的是 `tail` 的退出码）
  ——探针必须验"它真的改了东西/真的取对了码"，否则等于没测。


### 11.7 收尾轮（09-25 下午）：从 `DISPATCH-REMAINING.md` 里挑属于注释流的部分

那份派发清单是**审查流**写的（四条派发 + 两份人工清单）。逐项判归属：

| 单元 | 内容 | 归属判据 | 处置 |
|---|---|---|---|
| 派发 1 | 给 `scripts/loadtest/mock_yiban.py` 补故障注入旋钮、重跑 L3 剩余 16 格 | 审查流 L3 证据欠账；要改实现与其测试 | **不接**（不是注释工作） |
| 派发 2 | semgrep 265 命中四类归类 + 规则收紧 | 审查流自己的扫描产物 | **不接** |
| 派发 3 | 把 13:10 后三份精修并进登记表 | 登记表写入方 = 审查流指挥 | 只接其中与注释流相关的一半（见下） |
| 核查清单 | 旧服务器只读登机 | 需用户给地址与只读账号 | **阻塞**，未执行 |
| 操作清单 | 备份口令轮换 | 涉及生产写，清单自己写明"我不代做" | **不代做** |

注释流真正欠的两笔（派发清单上没写，是它自己的收尾）：

1. **MF-62..66 的处置**（待裁决 #5）：跨流对账判出注释流把 4 条"缺陷自认"洗平、新写 1 条更硬的
   假担保、1 处漏改。按建议执行"不 revert 语义、把话说回事实"：八文件注释级改动，与 `12c54a6`
   剥 docstring 后 `ast.dump` 逐文件等价；登记表补"处置"小节。
   ⇒ 顺着 MF-62 反查代码，查出两流都没登记的一件事：**MF-67**（领取层对"当日重复领取"没有跨轮
   上限——`give_up` 主动置过期租约、`attempts` 列无人当判据、预算住在单轮进程内）。
2. **§2.A "改注释即可"30 条只被抽查过 14 条**：余 18 条分三组并行改准（A 引擎/存储/邮件 5、
   B 前端 JS 与 `web/render.py` 6、C 模板与压测文档 7），三组文件集互不相交、各自 worktree，
   全量套件由协调者在合入后统一跑一次（代理侧禁跑全量：两次并发全量实测会互相打脏）。

本轮协调者自己犯并当场改掉的一个错，记下来当反面样本：改准 MF-63 时把 occurrence 计数写成
"24 处直接向用户承诺天数"——那正是被判出回归的同一类过强陈述（里面混着注释行和一处不相干的
「每 7 天」排期选项）。改法是只报命令可复点的两个数，并把两个例外写进句子。

### 11.8 FA-2A 收尾轮执行结果（09-25 下午，三组并行 + 协调者残留批）

18 条判定：**已改准 16 / 已改到位 1（FA-64，真落点在 `alerts.py` 而非派发表的
`workers.py`——FA 表那行标错了文件）/ 前置未满足不动 1**；另有 FA-69 因"句子在 `round.py`
不在 `schedule.py`"被组 A 按越界规则移交、由协调者改准。**代理报的数不采信，逐条独立复核**：

| 组 | 提交 | 文件 | 独立复核结果 |
|---|---|---|---|
| B | `90bf781` | render.py + 4 只 JS | 剥注释后逐字节相同；`edge_front_sec` 确无生产调用点（唯一读者 `app.py:875` 转发 + `test_web_boundary.py:3081/3179`）；`_mask_addr` 确为逗号逐项打码（`config.py:141`）且 `MailAddrMaskingTest` 在守；`data-view` 全仓仅 `date-field.js:90` 一处写入 |
| C | `3b1c989` | 5 模板 + 1 JS + loadtest README | `ui.html` 的"越界"是**注释例字自触发**：原版那句讲"注释不嵌套"的例字里就带着注释结束标记，块被提前闭合、后面的散文当正文输出。另 6 文件剥注释相同；`{% include %}` 引用 ui.html 实测 0 处（前置解除）、from-import 真实引用点 16 处（代理报 16，含自身示例行 1 处已剔除口径核对） |
| A | `5c02ed7` | state_cleanup / audit_chain / mail/__init__ | 3 文件 AST 等价；`state_cleanup.py` 项目内 import 只有 `state_gc` 一条、取时两处走 stdlib（宿主时区），生效截止在 `state_gc._cutoff`（北京钟）——UTC 主机上日志文案与实际删除口径差一天，代理把这条难看的写进去了，没软化 |
| 残留批 | 协调者自做 | round / state_io / mail + 5 模板 2 JS | 9 文件剥注释相同；`_sched_marker_exists` 的真实读者是 `_is_second_run()`→`runner.py:342`（补签轮剔除已了结账号），告警侧只是取来不读 |

**门禁**：合并后全量 `5 failed, 2901 passed, 7 skipped, 19 errors`——红集是基线 7 条的**子集**
（docker 4 + `MailClearAdminToTipTest` 1；两条 `ManualSignExitTest` 这轮绿）。基线本身在三次
近乎相同的树上跑出过 7 / 8 / 5 三种红数 ⇒ **单轮全量不可作判据**，这条要写进 MF-38 的证据里。

**未进登记表、已移交的三件事**（避免双写编号，留给审查流收口或用户拍板）：
1. `loadtest` 三条代码缺口（`delay_ms/fail_rate` 不回显、探测侧无 `requests_per_account`、
   每账号耗时缺 p50/p95）——只在 R12b 单元报告里，登记表 grep 无命中。
2. FA-56 原文括注"`app.py` 转发器同样零读者"已过期（`test_web_boundary.py` 在读）。
3. `/mine` 别称在 10+ 文件里仍与在册端点混写（本轮只清了 FA-113 点名的 8 处叙述）。
