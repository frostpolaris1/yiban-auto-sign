# 验收口径：D1 与对外交付（2026-11-27 版）

旧 Python 栈退役后，本篇只保留当前在役的 Python 门与 Go 栈交付口径。
旧版的车道名册、并发保真、覆盖面对账、入库引用门已随载体删除，不再作为判据。

## 1. D1 验收（主判据：把守卫改坏必须报警）

D1 的验收**不**看“删掉多少行”“测试源码比”。主判据只有一条：

> 把下列五条关键守卫**逐条故意改坏**，必须**条条报警**。

每条守卫的“改坏即红”由它自己的突变/负向用例钉住；这些用例在 CI 的 verify 里常跑：

| # | 守卫 | 钉的性质 | 改坏即红的用例 |
|---|------|----------|----------------|
| 1 | 顶层结构 `tests/test_repo_structure_gate.py` | 顶层条目必须与 `ALLOWED_TOP` 双向一致；新增未登记目录即红 | `ScannerHasTeethTest`（合成未登记条目 ⇒ 红） |
| 2 | 跳过显性化 `tests/test_skip_visibility_gate.py` | 每个 skip 点必须登记；登记不得陈旧；扫描面不得空 | `SkipSurfaceRegisteredTest` 三条 |
| 3 | 公开数据 `tests/test_public_exposure_gate.py` | 扫描面不得为空；真实形状必须被拒；合成值必须放行 | `test_empty_scan_is_red`、`test_gate_rejects_every_shape` |
| 4 | 私钥 `tests/test_private_key_gate.py` | 入库文件不得含私钥；扫描器对合成仓中的私钥必须报警 | `test_scanner_has_teeth`、`TempRepoGateTest` |
| 5 | 工作树卫生 `tests/test_suite_cwd_isolation_gate.py` | 测试不得在仓库留下运行期产物；xdist controller 下同样必须捕获 | `ArtifactDiffLogicTest`、`SessionGateE2ETest` |

辅判据：每条守卫都自带“空扫描不得当绿”，删测试或缩小扫描面会让对应用例红。

**明确不写**：不设“测试源码比降到 1:2”这类目标。本仓实测删测试只省 1–3 秒，成本
在信任不在时间；用行数比例当验收等于拿删测试换好看。

## 2. 负数条款（可机械检查的“明确不做”）

| 负数条款 | 载体 | 门 |
|----------|------|----|
| 不得把“因环境不满足而跳过”读成“通过” | `tests/**` 的 skip 点 | `tests/test_skip_visibility_gate.py` |
| 不得把空扫描、空名册当成绿 | 各扫描型门自身的合成用例 | 结构、公开数据、私钥、工作树卫生四门 |
| 不得在入库文件里放真实数据 | `tests/synthetic-fixtures.json` 之外的登记值 | `tests/test_public_exposure_gate.py` |
| 不得把私钥、keystore 一类敏感件入库 | 全部跟踪文件 | `tests/test_private_key_gate.py` |

“不得在入库文件里引用库外路径”的旧门已随 Python 文档门删除；当前无自动门，靠审查。
新增负数条款的纪律不变：**写下它的那一刻**同时写门，或在本表登记“无门 + 理由”。

## 3. 对外交付验收

包内必须自带“**缺什么**”的声明，且声明的技术栈与依赖清单**逐条可核**。

### 3.1 必带声明

1. **能力边界**：README / 交付说明必须写明“本包**没有**什么”（例如是否含前端构建产物、
   是否含演示数据、是否含生产配置）。
2. **依赖清单**：Go 侧 `server/go.mod` + `server/go.sum`；前端侧 `ui/package.json` +
   `ui/package-lock.json`；部署侧 `deploy/container/` 与 `deploy/baremetal/` 的脚本。
   不再有 `requirements.txt` / `requirements.lock`。
3. **运行前提**：Go 版本、Node 版本、PostgreSQL、必需的环境变量、容器或裸机二选一。

### 3.2 逐条可核（交付前清单）

| 声明 | 核法 |
|------|------|
| README 提到的每个文件/目录存在 | `git ls-files` + 路径存在性检查 |
| README 声明的依赖 = 锁文件 | `go.mod`/`go.sum` 与 `package-lock.json` 实文件；`npm ci` 可复现 |
| README 声明的协议/风控能力 | 指向 `server/` 内的实现与测试；没有实拍背书的分支必须标“未实拍” |
| README 声明的部署形态 | `deploy/container/`、`deploy/baremetal/` 的实文件；不得声明不存在的配置或服务 |
| “已验证”的结论 | 必须给命令 + 退出码 + 汇总行；不接受“跑过、绿了” |

反面样例（来自外部资料评估，2026-10-09）：README 把前端列为已有、并声称有
`config/registry.json` 与 Procrastinate，包里三项皆无。验收时按上表逐条核，
不以“文档写了”当证据。
