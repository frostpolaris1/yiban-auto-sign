# 配置名册（旧 Python 栈专用）

本目录的 `registry.json` **只服务旧 Python 栈**（`yiban/`、`web/`、`frontend/`、
旧 `tests/`）。它是旧线配置的唯一定义点：类型、域、缺省、分组、敏感级别与
网页可编辑面。

## 不要往这里加 Go 键

Go 服务端（`server/`）的配置名册是
`server/internal/configdata/registry.json`，编译期嵌入二进制（`go:embed`）：

- 只登记 `server/internal/adapters/config` 实际读取的键；
- `GET /api/admin/settings` 与 `yiban-cli doctor` 读同一份嵌入名册；
- 安装位、工作目录与镜像层里都不再有 `registry.json`。

给旧名册补 Go 键不会生效：Go 侧从不读取本文件。双向键面门
（`server/internal/adapters/config/registry_parity_test.go`）会同时拦住
“代码读了名册没登记”和“名册登记了代码不读”。

## 冻结口径

新栈未部署、旧栈冻结：本文件保持原样，不做清理、重排或删键。
旧线测试与 `scripts/dev-verify.sh` 仍按本文件断言。
