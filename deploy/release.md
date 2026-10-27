# 发布门清单（release checklist）

> 载体：`scripts/release-check.sh`。用法：`bash scripts/release-check.sh`；
> 单项 `--only <item>`；项名 `--list`；自测 `--selftest`。
> 状态口径：PASS／FAIL／SKIP／POINTER／MANUAL。SKIP 与 MANUAL 显性打印，不当绿。
> 汇总退出码：0 无 FAIL；1 有 FAIL；2 用法错误。
> 考古基准：ref `ad5b3639`，考古日 2026-10-27。命令在 WSL 跑。

## 项 1　版本三处一致（item 名 `version`）

命令：`bash scripts/release-check.sh --only version`

考古结论（三处定义点）：

1. **版本唯一来源**：`yiban/__init__.py` 的 `__version__`（基底为 `0.6.4`）。
   守卫：`tests/test_release_version_source.py` 钉「版本字面量只在一处定义」。
2. **CHANGELOG**：`CHANGELOG.md` 顶部 `[vX.Y.Z]` 条目与 `__version__` 一致
   （同一守卫用例钉住）。
3. **git tag**：发布时打 `vX.Y.Z`。`scripts/deploy_gate.sh` 的迁移演练按
   `GATE_OLD_REF`（默认 `v0.6.2`）取不可变 tag。基底 `ad5b3639` 上
   `v0.6.4` 的 tag 尚未打——本项在打完发布 tag 后跑。
4. **二进制 `--version`：缺机制，待立项。** `server/cmd/server/main.go` 不读
   命令行参数；`server/cmd/cli/main.go` 的子命令表没有 version。发布件无法
   自报版本。
5. **openapi `info.version`：不随发布走，待立项。**
   `server/contracts/openapi.yaml` 的 `version: "1.0.0"` 是 API 契约版本，
   与发布版本无同步机制。

本项可执行部分钉存在的两处：tag `v$__version__` 存在且指向 HEAD；
`__version__` 与 CHANGELOG 顶部一致。期望输出
`version: __version__=…, CHANGELOG top=…, tag vX.Y.Z -> HEAD ok`，退出码 0。
红：tag 不存在／不指向 HEAD／两处版本不一致，退出码 1。

## 项 2　许可证清单重生成（item 名 `license`）

命令：`bash scripts/release-check.sh --only license`（实跑
`server/scripts/license.sh`）。

期望：重生成清单与 `server/THIRD_PARTY_LICENSES.md` diff 为空，退出码 0。
红：清单漂移或违禁许可证，`license.sh` 退出码 1 → 本项 FAIL。
SKIP：`license.sh` 退出码 2 ＝ 门没跑（首跑需联网把 go-licenses 装进用户
缓存；或扫描不可用）。脚本打印 SKIP 与原因，不计 FAIL，发布前必须补跑。

## 项 3　生成物不过期（item 名 `generate`）

命令：`bash scripts/release-check.sh --only generate`（实跑
`server/scripts/generate.sh --check`）。

期望：`generate: internal/entry/http/oapi/oapi.gen.go is up to date`，退出码 0。
红：生成物与规格不一致，或生成器产不出字节（该门自带 fail-closed，
「没跑」退 2 不当绿）→ 本项 FAIL。

## 项 4　全量测试＋真库（item 名 `tests`，POINTER）

本项是对既有门的调用引用；脚本只验指向存在，不代跑全量（全量由发布
收口跑一次）。指向：

```bash
YIBAN_TEST_DATABASE_URL=<测试库 DSN> bash server/scripts/verify.sh
bash scripts/dev-verify.sh --ci      # 提交前；合并前跑全量
```

期望：两条退出码 0；`verify.sh` 里 pg／auth e2e 打印实测 PASS 数（未设
DSN 时它显性打 SKIP，不算绿）。红：任一退出码非 0。
脚本侧红：被引文件缺失。

## 项 5　容器烟测（item 名 `container`，POINTER）

指向：

```bash
bash scripts/deploy_gate.sh --phase A   # 全新 docker 环境按 README 一次装成 + 健康检查
bash scripts/drill-container.sh         # 容器形态整轮真签到（演练机）
```

期望：`deploy_gate.sh --phase A` 摘要全 PASS、退出码 0（发布判据：
干净环境从零安装一次完成）。红：摘要含 FAIL，退出码 1。
脚本侧红：被引文件缺失。

## 项 6　备份演练（item 名 `backup`，POINTER）

指向：

```bash
bash scripts/backup.sh --restore <最新备份包> <临时目标目录>
yiban-cli backup && yiban-cli restore   # server/cmd/cli 的 backup/restore 子命令
bash scripts/deploy_gate.sh --phase C   # 容器形态备份恢复判据
```

期望：`backup.sh --restore` 解包后自动跑 `PRAGMA integrity_check` 与
`scripts/audit_verify.py` 并通过，退出码 0（其自检失败码 6／7／8 任一
出现即红）。`yiban-cli restore` 行数对账通过。红：任一退出码非 0。
脚本侧红：`scripts/backup.sh` 缺失，或 `server/cmd/cli/main.go` 未登记
backup/restore 子命令。

## 项 7　回滚演练（item 名 `rollback`，POINTER）

考古结论：**回滚机制存在。** `deploy/linux/update.sh`：

- 迁移失败：保留旧二进制并重启服务（退出码 1）；
- 健康检查失败：stop → 换回 `.bak` 旧二进制 → start（退出码 1）；
- 回滚后仍不健康：退出码 2，报「manual action required」。

已知边界（该脚本头注登记）：schema 不随二进制回滚（无 down 迁移），
旧二进制须向前兼容新 schema。

既有守卫与本项指向：

```bash
bash scripts/deploy_gate.sh --phase B   # 升到本树顶端→回滚到 v0.6.2→数据完整性
go test ./cmd/cli/ -run TestUpdateShRollsBackOnFailedMigration -count=1
```

红：演练退出码非 0。脚本侧红：`update.sh` 无 `.bak` 回滚路径，或守卫
用例名消失。

## 项 8　ed25519/SPA 发布件检查（item 名 `signing`，MANUAL）

考古结论：**未发现既有门。** `scripts/`、`server/scripts/`、`deploy/`
无 ed25519/SPA 发布件检查；`SPA` 仅作为协议注释里的 H5 页面名出现
（`server/internal/signing/protocol/forms.go`）。

无门即不装门，本项登记为人工步骤。发布操作者人工核对：发布件与构建
物料清单一致、产物 sha256 对账、无未登记的二进制面变更。本项打 MANUAL，
不计入退出码。

## 已登记缺口（待立项，本清单不现场补造）

1. 二进制无 `--version`（项 1 考古第 4 条）。
2. openapi `info.version` 与发布版本无同步（项 1 考古第 5 条）。
3. 迁移无 down 迁移，schema 不随二进制回滚（项 7 边界，
   `deploy/linux/update.sh` 头注登记）。
