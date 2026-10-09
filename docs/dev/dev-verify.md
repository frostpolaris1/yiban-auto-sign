# 统一跑测入口 `scripts/dev-verify.sh`

> 2026-10-05 制定。**跑测只有这一个入口**：本地（Windows Git Bash 或 WSL 内）与 CI
> 共用同一个脚本，命令原文只存在脚本里一份。`AGENTS.md` 只保留一行目录引用。

## 1. 用法

```bash
# 本地全量门禁（Windows Git Bash 或 WSL 内，同一条命令；脚本自己进 WSL）
bash scripts/dev-verify.sh

# CI 关键子集（ubuntu runner 上就地跑，不建副本、不落日志）
bash scripts/dev-verify.sh --ci

# 提交前自查（两档，见 §1.1）
bash scripts/dev-verify.sh --fast              # 安全档：全量减已知长尾（实测约 1 分钟）
bash scripts/dev-verify.sh --fast-scoped       # 范围档：只跑改动相邻面（实测约 10 秒）
                                               # 空覆盖（无改动/无命中）退出码 3，不是 0

# 选项
--repo DIR       源仓库目录（默认 = 脚本所在仓库根）
--target PATH    一个或多个 pytest 目标（默认 tests/）；只对默认全量模式有效
--log-dir DIR    日志目录；默认按模式分开：全量 <仓库父目录>/yiban-dev-verify-logs，
                 fast 两档 <仓库父目录>/yiban-dev-verify-logs-fast（见 §1.1）
--keep N         日志保留最近 N 份（默认 5）；fast 两档与全量各自计数，互不影响
--fast           提交前自查·安全档：全量 − FAST_KNOWN_SLOW（5 条长尾）。无选择面损失
--fast-scoped    提交前自查·范围档：只跑改动相邻面。快，但未选中的用例没有跑
--base REF       上面两档的比较基准（默认 HEAD；传 origin/develop 则算整条分支的改动）
--fast-all       --fast 但不剔除长尾（只有改动确实落在那些文件上时才用）
-h, --help       打印脚本头部说明（脚本头就是配方正文）
```

`--ci` / `--fast` / `--fast-scoped` / `--fast-all` **互斥**：给两个**不同**模式即响亮拒绝（退出码 2）。
同一模式标志重复给（如 `--fast --fast`）按**幂等**接受——重复同一标志不改变语义。
互斥是必须的：静默让后者胜会让 `--fast --fast-scoped` 退成范围档，调用方以为跑了
"全量减长尾"，实际只跑了相邻面——这是危险方向。

从 Windows Git Bash 跑时，脚本自己换算路径并 `wsl.exe -d Ubuntu -u root` 进 WSL；
不需要记两条命令，也不需要预先 `cd` 到某个目录。

`--target` / `--log-dir` / `--keep` / `--base` 只对默认全量模式或 fast 两档有效。`--ci` 就地跑
固定关键子集、不落日志，这四个参数一律响亮拒绝（退出码 2）——静默忽略会让调用方以为
自己的设置生效了。
`--fast` / `--fast-scoped` 拒绝 `--target`（目标由该模式自己决定），`--base` 只对它们有效。

## 1.1 fast 模式（2026-10-07 立，两档）

**由来**：全量实测 **4331 用例（passed 4325 + skipped 6）/ 146s 墙钟**（pytest 自身 144.43s），
而瓶颈不在用例数——`--durations` 显示单个用例
（`test_locked_db_exit_two_not_tampered`，等待锁检测超时）独占 **135.92s＝全量墙钟的 93%**
（口径：单测点用时/全量墙钟）。剔除清单共 5 条，分属 3 个文件（shared_facts 3 条 +
audit_transaction 1 条 + path_env_read_gate 1 条）；另 4 条被 `--dist loadfile` 串行化约 **116s**。
删掉这 5 条，全量立刻落到 **57s**。
修一处跑一次全量再报错重修，时间全花在这些长尾上。

| 档 | 命令 | 实测墙钟（2026-10-07，本树空载） | 覆盖 | 什么时候用 |
|---|---|---|---|---|
| 安全档 | `--fast` | **57s**（covered=4326：passed 4320 + skipped 6） | 全量 − 5 条已知长尾，**无选择面损失** | 提交前默认用这个 |
| 范围档 | `--fast-scoped` | **9~11s**（目标为入口自检时 covered=18） | 只覆盖改动相邻面，**其余没跑**；空覆盖退出码 **3** | 改一处只想快速看一眼 |

**都不是门禁**：两档都会打印免责声明。推送前跑全量（默认模式）或确认 CI 绿；fast 的绿
不得当成门禁的绿。

**对账**：全量 4331 = `--fast` 的 covered 4326 + 剔除 5 条（2026-10-08 复测：
`--fast-all` 报 covered=4331，`--fast` 报 covered=4326，两个数都取自 pytest 汇总行）。
这条对账证明剔除清单只减"跑了的用例数"，没有隐藏红：`--fast-all` 那一轮 4325 passed /
0 failed / 0 errors。

**退出码 3 的语义**：范围档没测到任何真实用例——① 改动集为空，或 ② 改动存在但反查不到
任何相邻用例。这两种情况只跑 `tests/test_dev_verify_entry.py`（入口自检），退出码用 **3**。
3 的意思是**"没测到东西"**，不是"测了但失败"：**只有改动集反查零命中才是 3**。
文档类改动不一定零命中——改 `docs/dev/dev-verify.md` 本身走裸词回退（`dev-verify`），
命中入口自检与文档相关用例（本树实测 rc=0、覆盖非空：裸词回退命中 11 个用例文件）。
两种 fast 档都打印 `covered=<用例数>`（pytest 汇总四数之和：passed + failed + errors + skipped），
据此区分"只跑了入口自检"与"真的覆盖了"。

**退出码只有四种**，3 **由空覆盖独占**：

| 码 | 含义 |
|---|---|
| `0` | 通过：ruff 与 pytest 都 0，且覆盖非空 |
| `1` | 有红：ruff 非 0，或 pytest 非 0 |
| `2` | 环境错误：守卫判红或参数被拒（见 §2 表） |
| `3` | 空覆盖：范围档没测到任何真实用例，只跑了入口自检 |

ruff 与 pytest 的**原始退出码一律归一为 1**，不再原样透出。3 因此只有一个含义：
pytest 自身的 3（INTERNALERROR）也与空覆盖撞码不了。定位信息不丢——原始码仍逐行打印在
日志里（`DEV-VERIFY ruff_exit=` 与 `DEV-VERIFY pytest_exit=`）。红优先于空覆盖：
空覆盖同时有红时退出码是 1，不是 3；空覆盖提示会按最终退出码打印，不与它矛盾。

**范围档的选择法**（无依赖、无状态）：改到 `tests/` 下的用例就直接跑它；改到源码就**按导入
路径**反查（`from a.b.c import` / `import a.b.c` / `a.b.c.`）——**不能用裸词**：实测 `window`
裸词命中 70 个用例文件（占 29%），导入路径只命中 11 个；导入路径 0 命中时才回退裸词，并
如实打印回退与命中量。改到本脚本自身会补 `tests/test_dev_verify_entry.py`（它冻结 CI 命令表）。

**已知边界（必须知道）**：本脚本只跑 pytest。**前端改动**（`frontend/` 下的 vitest / playwright）
不在本脚本面内，范围档遇到 `frontend/` 改动会如实说"未反查到用例"——不要读成"有人覆盖"。
并发时墙钟会涨（§6）。

**fast 用独立副本与独立锁**（`…/fast-worktree`、`dev-verify-fast-runlock`）：共用一份副本会互相
`rm -rf`，而串行化会让"自查"去等一个跑满两分半的全量。fast 还复用上一份副本做增量同步
（`rsync -a -c --delete`），把固定开销压到几秒。**`-c` 不许去掉**：默认快检只看"尺寸 + 整秒
mtime"，同一秒内改完且尺寸不变的改动会被判成"没变"而跳过，副本因此陈旧，跑出来的是旧代码
（可能报绿）。加上 `-c` 后按内容校验，"副本 = 当前工作树"这条不变式由 rsync 自己承重
（rsync 非 0 退出即 die）。代价实测：本树 rsync 面内约 750 个文件（跟踪 731 个），副本已
同步时 `rsync -a` 约 0.65s、加 `-c` 约 1.4s，多付约 0.7s。

**日志目录也按模式分开**（`default_log_dir`）：全量用 `<仓库父目录>/yiban-dev-verify-logs`，
fast 两档用 `<仓库父目录>/yiban-dev-verify-logs-fast`。为什么必须分：日志目录按 mtime 轮转
（`--keep`，默认 5 份），而 fast 与全量走**不同的锁**、可以同时跑；共用一个目录时，一侧的
轮转会删掉另一侧的日志。实测（2026-10-07）：fast 档用默认目录跑几次，把同目录里他方的
5 份日志删掉了，且那份备份落在 WSL `/tmp` 后被清空，无法还原。不同锁的两个流程不共享
可被轮转的资源，这是根因修法。`--keep` 的语义在两档内不变，只是各自计数。
显式传 `--log-dir` 时两档都用调用方给的目录；若两档传同一目录，轮转互删的风险由调用方承担。


## 2. 全量模式做了什么（每一步都有来历）

| 步骤 | 固定口径 | 为什么 |
|------|----------|--------|
| 副本落点 | `/root/.cache/yiban-dev-verify/worktree`，每次整体重建 | 必须落在 WSL 原生文件系统；`/mnt` 走 DrvFs，全量跑测慢且文件语义有差异。只排除 `.git` 与工具缓存目录（`__pycache__` / `.pytest_cache` / `.ruff_cache`）。副本路径固定，故用 `flock` 串行化并发运行（两个 dev-verify 不能同时重建同一份副本）；后到者等前者结束。机器上没有 `flock` 时脚本直接拒绝跑测（退出码 2）——共享副本路径不能在没有串行化的情况下降级运行 |
| `.git` | 由脚本播种：HEAD 指向源提交、对象经 `alternates` 复用源对象库、索引直接取源索引 | 入库类门禁（`tests/test_deploy_prod_artifacts.py`、`tests/test_web_vue_sources_tracked.py`）靠 `git ls-files` 判"哪些文件入库"；副本没有 `.git` 就是伪红。索引取源索引而不是 `git add -A`：源仓存在"已跟踪但被 `.gitignore` 命中"的文件（`scripts/git-hooks/commit-msg`），`git add -A` 会静默漏掉它 |
| LF 归一 | 副本内文本先转 LF 再跑 | `.gitattributes` 只约束入库形态；Windows 侧 `core.autocrlf=true` 会让工作树出现 CRLF。CRLF 会炸 shell 门禁脚本（`set -euo pipefail` 被 CR 破坏），是伪红大头 |
| 解释器 | 只认 `/root/.venv-yiban-wsl/bin/python`，跑测前打印绝对路径与版本 | 不许退化成 PATH 上的任意 python（历史上三轮才找到解释器） |
| 并发 | `-n auto --dist loadfile` | `--dist loadfile` 不得去掉：套内存在文件内先后依赖与进程级 DB 单例，按单条分发即误红 |
| 日志 | `<日志目录>/dev-verify-<时间戳>-<pid>.log`，整份 tee，保留最近 N 份 | **不截断**：只留 tail 会丢失败名单。日志目录按模式分开（全量 `yiban-dev-verify-logs`，fast 两档 `yiban-dev-verify-logs-fast`，见 §1.1）：两档走不同的锁、可并发，共用目录会互相轮转删日志。文件名带 PID：同一秒的两次运行不互相覆盖。日志内含解释器绝对路径与版本、被跑提交 sha、ruff 退出码、pytest 汇总四数、脚本退出码 |
| ruff | `ruff check yiban/ tests/ scripts/ web/ --quiet` | 与 CI 同口径（含 `web/`） |

退出码（四种，全量模式与 fast 两档共用同一套裁决点）：

| 码 | 含义 | 什么时候出现 |
|---|---|---|
| `0` | 通过 | ruff 与 pytest 都 0，且覆盖非空 |
| `1` | 有红 | ruff 非 0，或 pytest 非 0（失败/错误都算）。**原始码不透出**，逐行打印在日志里（`ruff_exit=` / `pytest_exit=`） |
| `2` | 环境错误 | 守卫判红：副本 `.git` 失活、副本残留 CRLF、副本跟踪集为空、源仓库不可解析为 git 仓库、固定 venv 不可用、参数被响亮拒绝（互斥/失效参数）、`FAST_KNOWN_SLOW` 名单坏 |
| `3` | 空覆盖 | 只出现在 `--fast-scoped`：没测到任何真实用例，只跑了入口自检（见 §1.1）。**此码由空覆盖独占**：红优先，空覆盖同时有红时给 1 |

`3` 独占是硬约束。改前 ruff/pytest 的非 0 被原样透出，pytest 自己的 3（INTERNALERROR）
与空覆盖撞码，同一个码有两个意思，文档与实测对不上。

## 3. CI 侧（`--ci`）

`--ci` 在**当前检出上就地跑**关键子集，不建副本、不进 WSL、默认不落日志。
命令原文只住在入口脚本的 `run_ci` 一份，逐字冻结在 `tests/test_dev_verify_entry.py`：

```bash
python -m ruff check yiban/ tests/ scripts/ web/ --quiet
python -m pytest tests/ -q -n 4 --dist loadfile -k "security or mask or audit or login or private or csrf or ratelimit"
python -m pytest tests/test_login_e2e_mock.py -q -p no:randomly
bash scripts/check-shared-facts.sh
python -m pytest tests/test_shared_facts_gate.py -q -p no:randomly
python scripts/check-path-env-reads.py
python -m pytest tests/test_path_env_read_gate.py -q -p no:randomly
python scripts/check-config-registry.py
python -m pytest tests/test_config_registry_gate.py -q -p no:randomly
```

三道门禁各带自己的元测试同批跑：`check-shared-facts.sh` 数名册里 awk 引擎的键，
`check-path-env-reads.py` 数名册里 `ast:` 路由的键（同一枚键只许一个引擎计数），
`check-config-registry.py` 判配置名册三条（`config/registry.json` 自校验、已登记键的
缺省值字面量在代码内清零、未登记键不得出现在生产代码）。名册门禁的元测试是**突变
验证**：每条放行断言都配一条同形状的污染断言，门禁判据写反即红。

解释器取 `PATH` 上的 `python`（CI 由 `actions/setup-python` + 钉版 `pip install` 保证），
可用 `DEV_VERIFY_PY` 覆盖。任一环节非 0，本步即非 0——与改前"步级失败即停"等价。

`--ci` **就地跑、不做 LF 归一化**（CI runner 的新检出按 `.gitattributes` 是 LF）。
若工作树的**跟踪文件**里真有 CRLF，脚本响亮拒绝（退出码 2）并点名文件，而不是
把 CRLF 造成的伪红当红交出去；本地要跑就地子集时用默认全量模式（它会先归一化）。

命令漂移由 `tests/test_dev_verify_entry.py` 冻结（逐字比对上面的命令与顺序，条数以该测试文件的冻结清单 `EXPECTED_CI_COMMANDS` 为准——不在这里另写一个数字，免得两处漂移）；
CI 接线由 `tests/test_shared_facts_gate.py::SharedFactsCiWiringTest` 两跳审
（verify job 调入口脚本 + 入口脚本的 `run_ci` 真调门禁脚本）。

`nightly.yml` 的全量轨**不在本入口内**：它是独立轨（含时区专项子集），本批不动它。

## 4. 守卫与"守卫承重"的可执行证明

守卫共四道。第 1、2 道在副本建好后、跑测之前判；第 3 道更早（建副本之前）；
第 4 道就在复制那一步（rsync 自己承重）：

1. **副本 git 守卫**：`.git` 可用、HEAD 可解析、**对象可达**（`git cat-file -e HEAD^{commit}`，
   副本对象走 `alternates`，指错时引用仍读得到而历史类用例会伪红）、跟踪集非空（≥100）、
   HEAD 等于源提交；
2. **LF 守卫**：归一化后副本内不得再有 CRLF 行尾；
3. **长尾名单守卫**（`guard_fast_known_slow`，只在 fast 档且未用 `--fast-all` 时跑）：
   `FAST_KNOWN_SLOW` 每条 nodeid 的**文件、类、用例三层都必须存在**，且**不许有前缀嵌套条目**
   （同时有 `x.py::Cls` 与 `x.py::Cls::test_a` 时，前者已覆盖后者，后者是假条目）。
   违规即 `die`（退出码 2）并打印违规条目。它挡的是"文件/类/用例改名后 `--deselect` 指向空气、
   长尾静默溜回 `--fast`"——pytest 对不存在的 `--deselect` **静默忽略**（实测 rc=0、无告警），
   故只查文件存在挡不住类/方法改名。检查用文本匹配（在该文件里找 `class <类名>` 与
   `def <用例名>`），**不用** `pytest --collect-only`：后者要拉起 pytest 收集，会毁掉 `--fast`
   的墙钟。文本匹配挡的是"改名后静默变 no-op"，不是断言源码内容，够用。
   单段 `::` 有两种合法形状：模块级用例（形如 `x.py::test_a`，没有类名）与**类级** nodeid
   （形如 `x.py::Cls`，pytest `--deselect` 接受 file::Class 整类形状）。该分支对"用例名存在
   或类名存在"取或；假名（两者都不存在）仍判红。前缀嵌套检查不按形状分支，类级形状同样可达。
4. **副本内容校验**（F1 修复）：`rsync -a -c`——默认快检会跳过"同尺寸 + 同整秒 mtime"的
   改动（实测：`rsync -a` 后副本仍是旧内容，加 `-c` 才同步），副本陈旧等于跑旧代码。
   "副本 = 当前工作树"这条不变式由 rsync 自己承重：rsync 非 0 退出即 `die`。

前两道守卫由 `scripts/e2e/dev-verify-e2e.sh` 用**变异体**钉住：脚本内以
`# dev-verify-mutant-begin: <名>` / `# dev-verify-mutant-end: <名>` 夹住被钉住的段落，
e2e 用 `sed` 删掉该段造出变异体，再断言"伪红真的出现"。摘掉守卫后伪红出现，
证明守卫**必要**（AGENTS §14）。守卫的**在场性**由另一条钉住：变异体只证"没有守卫会伪红"，
证不了"守卫还在跑"——2026-10-05 实测，只删掉 `guard_copy_git "$DEST" "$SHA"` 与
`guard_lf "$DEST"` 两行调用（定义留着），e2e 仍全绿（10 次调用 0 FAIL）。故调用行由
`tests/test_dev_verify_entry.py::DevVerifyEntryTest::test_both_guards_are_invoked_in_the_full_mode_path`
正面钉住：调用行消失即红。

第 3、4 道守卫由 `tests/test_dev_verify_entry.py::DevVerifyFastModeTest` 钉住（契约用例
只在参数解析段与纯函数上跑 bash，不建副本）：
`FAST_KNOWN_SLOW` 名单的元测试（文件存在 + 无前缀嵌套）与守卫的改名拦截实测（临时目录上
真跑守卫：类改名、用例改名必须判红；模块级 nodeid 与类级 nodeid 必须放行，假类名必须判红）、
在场性（调用行必须出现在 rsync
之前）、rsync 行必须带 `-c`（防回退）、真跑 pytest 命令必须拼 `DESELECT` 数组（防拼接被删后
长尾静默溜回）、空覆盖必须写标记且最终退出码为 3、退出码归一
（ruff/pytest 非 0 一律 1，原始码留在日志）、空覆盖提示必须跟最终退出码、默认日志目录按模式
分开、`--ci --base` 与两个不同模式标志必须响亮拒绝、同一模式标志重复给必须幂等接受。

## 5. e2e（`scripts/e2e/dev-verify-e2e.sh`，必须在 WSL 内跑）

```bash
bash scripts/e2e/dev-verify-e2e.sh           # 快速用例（约 1 分钟）
bash scripts/e2e/dev-verify-e2e.sh --full    # 追加真仓全量绿（约 3 分钟）
```

覆盖：T1 绿（日志字段、副本 git 可用、汇总四数与副本内 `--collect-only` 一致）、
T2 红（注入必失败用例 ⇒ 非 0 且日志读得到）、T3 无 `.git`（真脚本响亮拒绝，
变异体必须复现伪红）、T4 CRLF（真脚本 0 伪红，变异体必须复现伪红）、
T5 全量绿（`--full`）、T6 日志保留份数、T7 `--ci` 遇 CRLF 响亮拒绝。

e2e 不写进 CI：CI 结果必须与改前等价，加一步即改变 CI 行为。因此它是**手跑**的
活体校验者；纯文本侧的漂移由 `tests/test_dev_verify_entry.py`（随全量/nightly 跑）
与 `SharedFactsCiWiringTest`（随 CI fast 轨的 shared-facts 元测试跑）自动钉住。

## 6. 已知边界

- 归一化与守卫都用 `grep -I` 判定文本；被 grep 判成二进制的文件（含 NUL）跳过——
  这类文件不参与行尾判定。
- 归一化按行尾 CR 处理，不看文件类型。这在本仓是等价的：跟踪对象里没有任何一枚含 CR
  的 blob（实测 `git grep -I -l -e $'\r' --cached` 输出为空，工作树上的 CRLF 全是
  Windows 侧 `core.autocrlf` 的产物）。将来若引入"故意含 CRLF 的跟踪 fixture"，
  副本归一化就会与 CI 检出不等价——那一步要重新评估本条。
- 副本 `.git` 的对象库经 `alternates` 指向源仓库对象库，故源仓库对象目录必须可读；
  历史因此与源一致（不是合成的单提交）。
- `--target` 只是缩小 pytest 目标，不影响 ruff 与副本相关守卫；门禁口径永远是默认的 `tests/`。
- **并发只免"等锁"，不免"抢 CPU"**：fast 与另一次跑测并发时共用 CPU 与磁盘。审查实测
  （2026-10-07）`--fast` 墙钟从约 58s 涨到约 104s。这是并发代价，不是缺陷；要准数字就
  先等对方跑完。锁只保证两次运行不同时清空同一份副本。
- **本脚本只跑 pytest**：`frontend/` 的 vitest 与 playwright 不在面内，范围档遇到
  `frontend/` 改动只能说"未反查到用例"，不能读成"有人覆盖"。
- **范围档的墙钟不是固定 5 秒**：本树实测 9~11s（目标集是 `tests/test_dev_verify_entry.py`，
  两次是同一目标集，差异属噪声）。目标集变大时墙钟随 pytest 执行时间增长；也别以为空覆盖
  那一档更快——它照样跑 ruff 与入口自检，不因为"没测到东西"而变快。
- `covered=` 的口径是 pytest 汇总行四数之和（含 skipped），它数的是"跑了多少用例"，
  不是"改动被覆盖了多少"。
- **worker 被 SIGKILL 时汇总行数字无意义**：pytest-xdist 的 worker 被杀后，汇总行仍是
  pytest 自己算的数，那个数与目标集无关（实测：目标集只有 1 个文件，汇总行报 `97 failed`）。
  `covered=` 跟着这个数走，于是也数出无意义的值。影响面有限：此时 `rc=1`（红），
  不会产生假绿；只是别拿那一轮的 `covered=` 与 `failed=` 当事实。要可信的数字就重跑。
- ruff 在任何档都扫 `yiban/ tests/ scripts/ web/` 全量，不随范围档缩小；范围档省的是
  pytest 时间，不是 ruff 时间。
