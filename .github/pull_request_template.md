<!-- 每节只写事实与数字。空着的小节等于没写。 -->

## 做了什么 / 为什么

<!-- 一两句。关联 bd 工单号与缺陷编号。 -->

## 影响面盘查（AGENTS.md §12）

- 端点 / 入口数：
- 跨语言载体数（bash / JS / SQL / env 行 / 文档 / cron / compose）：
- 读者与调用点数：
- 盘查手段（grep 写法 / census 名册 / bd 工单）：

## 改动边界（AGENTS.md §13）

**本批改动文件**
-

**明确不改的文件（含理由）**
-

## 自检清单

- [ ] **e2e 先行**：先写会红的 e2e 并看到它红，再写实现看到它绿；没有事后补测试凑数
- [ ] **门禁**：`bash scripts/dev-verify.sh`（WSL，全量并发）全绿；CI 子集 `bash scripts/dev-verify.sh --ci` 同绿
- [ ] **changelog**：用户可见变更已写进 `CHANGELOG.md`；无用户可见变更则写明理由
- [ ] **open-code-review**：提交前过 `ocr review` 无缺陷；修完重新 `git add` 再复审
- [ ] **校验者**：为本次边界留下会因越界而变红的测试或门禁
- [ ] 基线红如实登记并点名节点（无则写"无"）

## 门禁证据（命令 + 数字）

```
$ bash scripts/dev-verify.sh
passed=  failed=  skipped=  errors=

$ bash scripts/dev-verify.sh --ci
DEV-VERIFY(ci) exit_code=
```
