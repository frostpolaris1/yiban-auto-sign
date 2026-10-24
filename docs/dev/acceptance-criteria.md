# 验收口径：D1 与对外交付（工单 fodo 第 2、3 条）

## 1. D1 验收（主判据：把守卫改坏必须报警）

D1 的验收**不**看"删掉多少行""测试源码比"。主判据只有一条：

> 把下列五条关键守卫**逐条故意改坏**，必须**条条报警**。

每条守卫的"改坏即红"由它自己的突变用例钉住；这些用例在 CI 快集里常跑，不是
一次性手工记录。验收时逐条跑对应用例即可复现：

| # | 守卫 | 钉的性质 | 改坏即红的用例 |
|---|------|----------|----------------|
| 1 | 车道名册 `tests/test_gate_lane_roster.py` | 每个测试文件恰好归一条道；名册＝文件系统＝pytest 收集 | `LaneRosterCheckerMutationTest`（新增文件不归类 ⇒ 红） |
| 2 | 跳过显性化 `tests/test_skip_visibility_gate.py` + `scripts/dev-verify.sh` 的 skip policy | 未登记的跳过在严格档判红；关键族"永不运行"有名有姓 | `RuntimeSkipPolicyTest`、`NeverRunsRegisteredTest` |
| 3 | 并发保真 `tests/test_concurrency_fidelity_gate.py` | 线程不得冒充跨进程并发；两把进程内锁的行为可证 | `ConcurrencyFidelityTeethTest`（假锁 ⇒ 红）、`NAMED_SMOKE` 摘条目 ⇒ 红 |
| 4 | 覆盖面对账 `scripts/check-coverage-ledger.py` ＋ `scripts/gate/coverage-ledger.tsv` | 改动文件 ⊆ 覆盖并集或显式登记；空改动集退 2 | **无运行期门**（2026-10-21 瘦身批删除 `test_coverage_ledger_gate.py`；该工具全仓零调用点，登记表保留作人工对账） |
| 5 | 入库引用 `tests/test_doc_reference_gate.py` | 入库文本里的路径必须 `git ls-files` 可解 | 门内突变夹具：塞入未入库的 `docs/` 路径 ⇒ 红；扫空不得当绿 |

辅判据：下列三处**负数条款**为零（见第 2 节），每处都有常驻门。

**明确不写**：不设"测试源码比降到 1:2"这类目标。本仓实测删测试只省 1–3 秒，成本
在信任不在时间；用行数比例当验收等于拿删测试换好看。

## 2. 负数条款（可机械检查的"明确不做"）

正面要求（先写 e2e、要审查、要盘查影响面）可机械检查性弱。下列三条是**禁**，
每条都接上了常驻门：

| 负数条款 | 载体 | 门 |
|----------|------|----|
| 不得用线程冒充跨进程并发 | `tests/**` 里宣称"跨进程/跨执行体"的用例 | `tests/test_concurrency_fidelity_gate.py`（未登记即红；`NAMED_SMOKE` 只许点名冒烟） |
| 不得把"因环境不满足而跳过"读成"通过" | `tests/**` 的 skip 点、`scripts/dev-verify.sh` 的 skip policy | `tests/test_skip_visibility_gate.py` + `dev-verify.sh` 的 `SKIP_EXEMPT_*` |
| 不得在入库文件里引用库外路径 | 入库文本件 | `tests/test_doc_reference_gate.py` |

新增负数条款的纪律：**写下它的那一刻**同时写门，或在本表登记"无门 + 理由"；不许
只活在注释里（AGENTS §十三 第 28 条）。

## 3. 对外交付验收

包内必须自带"**缺什么**"的声明，且声明的技术栈与依赖清单**逐条可核**。

### 3.1 必带声明

1. **能力边界**：README / 交付说明必须写明"本包**没有**什么"（例如前端构建产物是否
   随包、是否含演示数据、是否含生产配置）。
2. **依赖清单**：`requirements.txt`（下限）与 `requirements.lock`（精确）必须同时
   在包内；README 的安装命令必须指向其中之一，不得出现"裸 `pip3` 与自带 `.venv`
   两套说法并存"。
3. **运行前提**：Python 版本、systemd/cron 二选一、必需的环境变量、可选的
   `pycryptodome` 附加件。

**哈希生成（2026-10-24 起，R3-007）**：`requirements.lock` 的每个 pin 带
`--hash=sha256:`。生成方式是查 `https://pypi.org/pypi/<pkg>/<ver>/json` 的
`urls[].digests.sha256`，取该版本当前全部 sdist/wheel 的哈希。安装侧
`deploy/prod/install.sh` 与 `docker/Dockerfile` 用 `--require-hashes`。
`scripts/check-deps-lock.sh` 对缺哈希的条目 fail-closed；
`tests/test_deploy_deps_sync.py` 的 `DepsLockHashTest` 逐条核对。

### 3.2 逐条可核（交付前清单）

| 声明 | 核法 |
|------|------|
| README 提到的每个文件/目录存在 | `git ls-files` + 路径存在性检查 |
| README 声明的依赖 = `requirements.lock` | `docs/dev/constraints.tsv` 的 `requirements-audited-equals-deployed` 检查器 |
| README 声明的协议/风控能力 | 指向 `yiban/adapters/protocol_yiban/SPEC.md` 与实拍夹具；没有实拍背书的分支必须标"未实拍" |
| README 声明的部署形态 | `run.sh` / `docker/` / `web/deploy/` 的实文件；不得声明不存在的 `config/registry.json`、队列服务等 |
| "已验证"的结论 | 必须给命令 + 退出码 + 汇总行；不接受"跑过、绿了" |

反面样例（来自外部资料评估，2026-10-09）：README 把前端列为已有、并声称有
`config/registry.json` 与 Procrastinate，包里三项皆无。验收时按上表逐条核，
不以"文档写了"当证据。
