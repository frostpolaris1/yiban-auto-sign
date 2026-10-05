# 统一跑测入口 `scripts/dev-verify.sh`

> 2026-10-05 制定。**跑测只有这一个入口**：本地（Windows Git Bash 或 WSL 内）与 CI
> 共用同一个脚本，命令原文只存在脚本里一份。`AGENTS.md` 只保留一行目录引用。

## 1. 用法

```bash
# 本地全量门禁（Windows Git Bash 或 WSL 内，同一条命令；脚本自己进 WSL）
bash scripts/dev-verify.sh

# CI 关键子集（ubuntu runner 上就地跑，不建副本、不落日志）
bash scripts/dev-verify.sh --ci

# 选项
--repo DIR       源仓库目录（默认 = 脚本所在仓库根）
--target PATH    一个或多个 pytest 目标（默认 tests/）；只对默认全量模式有效
--log-dir DIR    日志目录（默认 <仓库父目录>/yiban-dev-verify-logs）；只对默认全量模式有效
--keep N         日志保留最近 N 份（默认 5）；只对默认全量模式有效
-h, --help       打印脚本头部说明（脚本头就是配方正文）
```

从 Windows Git Bash 跑时，脚本自己换算路径并 `wsl.exe -d Ubuntu -u root` 进 WSL；
不需要记两条命令，也不需要预先 `cd` 到某个目录。

`--target` / `--log-dir` / `--keep` 只对默认全量模式有效。`--ci` 就地跑固定关键子集、
不落日志，三个参数一律响亮拒绝（退出码 2）——静默忽略会让调用方以为自己的设置生效了。

## 2. 全量模式做了什么（每一步都有来历）

| 步骤 | 固定口径 | 为什么 |
|------|----------|--------|
| 副本落点 | `/root/.cache/yiban-dev-verify/worktree`，每次整体重建 | 必须落在 WSL 原生文件系统；`/mnt` 走 DrvFs，全量跑测慢且文件语义有差异。只排除 `.git` 与工具缓存目录（`__pycache__` / `.pytest_cache` / `.ruff_cache`）。副本路径固定，故用 `flock` 串行化并发运行（两个 dev-verify 不能同时重建同一份副本）；后到者等前者结束。机器上没有 `flock` 时脚本直接拒绝跑测（退出码 2）——共享副本路径不能在没有串行化的情况下降级运行 |
| `.git` | 由脚本播种：HEAD 指向源提交、对象经 `alternates` 复用源对象库、索引直接取源索引 | 入库类门禁（`tests/test_deploy_prod_artifacts.py`、`tests/test_web_vue_sources_tracked.py`）靠 `git ls-files` 判"哪些文件入库"；副本没有 `.git` 就是伪红。索引取源索引而不是 `git add -A`：源仓存在"已跟踪但被 `.gitignore` 命中"的文件（`scripts/git-hooks/commit-msg`），`git add -A` 会静默漏掉它 |
| LF 归一 | 副本内文本先转 LF 再跑 | `.gitattributes` 只约束入库形态；Windows 侧 `core.autocrlf=true` 会让工作树出现 CRLF。CRLF 会炸 shell 门禁脚本（`set -euo pipefail` 被 CR 破坏），是伪红大头 |
| 解释器 | 只认 `/root/.venv-yiban-wsl/bin/python`，跑测前打印绝对路径与版本 | 不许退化成 PATH 上的任意 python（历史上三轮才找到解释器） |
| 并发 | `-n auto --dist loadfile` | `--dist loadfile` 不得去掉：套内存在文件内先后依赖与进程级 DB 单例，按单条分发即误红 |
| 日志 | `<日志目录>/dev-verify-<时间戳>-<pid>.log`，整份 tee，保留最近 N 份 | **不截断**：只留 tail 会丢失败名单。文件名带 PID：同一秒的两次运行不互相覆盖。日志内含解释器绝对路径与版本、被跑提交 sha、ruff 退出码、pytest 汇总四数、脚本退出码 |
| ruff | `ruff check yiban/ tests/ scripts/ web/ --quiet` | 与 CI 同口径（含 `web/`） |

退出码：`0` 全绿；`1` 门禁红（ruff 或 pytest 非 0）；`2` 环境错误（守卫判红，
例如副本 `.git` 失活、副本残留 CRLF、源仓库不可解析为 git 仓库）。

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
```

两道门禁各带自己的元测试同批跑：`check-shared-facts.sh` 数名册里 awk 引擎的键，
`check-path-env-reads.py` 数名册里 `ast:` 路由的键（同一枚键只许一个引擎计数）。

解释器取 `PATH` 上的 `python`（CI 由 `actions/setup-python` + 钉版 `pip install` 保证），
可用 `DEV_VERIFY_PY` 覆盖。任一环节非 0，本步即非 0——与改前"步级失败即停"等价。

`--ci` **就地跑、不做 LF 归一化**（CI runner 的新检出按 `.gitattributes` 是 LF）。
若工作树的**跟踪文件**里真有 CRLF，脚本响亮拒绝（退出码 2）并点名文件，而不是
把 CRLF 造成的伪红当红交出去；本地要跑就地子集时用默认全量模式（它会先归一化）。

命令漂移由 `tests/test_dev_verify_entry.py` 冻结（逐字比对上述五条命令、含顺序）；
CI 接线由 `tests/test_shared_facts_gate.py::SharedFactsCiWiringTest` 两跳审
（verify job 调入口脚本 + 入口脚本的 `run_ci` 真调门禁脚本）。

`nightly.yml` 的全量轨**不在本入口内**：它是独立轨（含时区专项子集），本批不动它。

## 4. 守卫与"守卫承重"的可执行证明

两道守卫在副本建好后、跑测之前判：

1. **副本 git 守卫**：`.git` 可用、HEAD 可解析、**对象可达**（`git cat-file -e HEAD^{commit}`，
   副本对象走 `alternates`，指错时引用仍读得到而历史类用例会伪红）、跟踪集非空（≥100）、
   HEAD 等于源提交；
2. **LF 守卫**：归一化后副本内不得再有 CRLF 行尾。

它们由 `scripts/e2e/dev-verify-e2e.sh` 用**变异体**钉住：脚本内以
`# dev-verify-mutant-begin: <名>` / `# dev-verify-mutant-end: <名>` 夹住被钉住的段落，
e2e 用 `sed` 删掉该段造出变异体，再断言"伪红真的出现"。摘掉守卫后伪红出现，
证明守卫**必要**（AGENTS §14）。守卫的**在场性**由另一条钉住：变异体只证"没有守卫会伪红"，
证不了"守卫还在跑"——2026-10-05 实测，只删掉 `guard_copy_git "$DEST" "$SHA"` 与
`guard_lf "$DEST"` 两行调用（定义留着），e2e 仍全绿（10 次调用 0 FAIL）。故调用行由
`tests/test_dev_verify_entry.py::DevVerifyEntryTest::test_both_guards_are_invoked_in_the_full_mode_path`
正面钉住：调用行消失即红。

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
- `--target` 只是缩小 pytest 目标，不影响 ruff 与两道守卫；门禁口径永远是默认的 `tests/`。
