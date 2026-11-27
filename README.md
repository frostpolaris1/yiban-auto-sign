# 易班自动签到

> 自托管的易班校本化签到服务：配置一次，每天在签到窗口内自动完成。
> 后端 Go、前端 React、数据库 PostgreSQL 18；旧 Python 栈已于 2026-11-27 退役删除。

![Go](https://img.shields.io/badge/Go-1.27-00ADD8?logo=go&logoColor=white)
![React](https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=black)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-18-4169E1?logo=postgresql&logoColor=white)
![License](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)

> 🔐 安全政策：[`SECURITY.md`](SECURITY.md)（漏洞走私密上报）
> 🤝 贡献指南：[`CONTRIBUTING.md`](CONTRIBUTING.md)（分支、门禁与合并方式）

> ⚠️ **不要使用他人的公开实例。** 本项目保存易班账号凭据；公开实例可能记录、滥用或泄露你的密码与签到数据。请只部署在你自己控制的主机上。

## 项目介绍

易班（yiban）是部分高校使用的学生平台，一些学校要求每天完成早操或晚点名签到。
本项目在服务端保存账号与排期，到点在签到窗口内错峰执行，不需要个人电脑常开。

它是**自托管工具**，面向个人或小团体自建；一台 1 核 2G 的服务器即可起步。
能带多少账号取决于签到窗口、账号间隔与出口质量，启动时会打印容量估算与拐点提示，
不要按固定数字拍板。

## 功能

- **自动签到**：按学校窗口排期，窗口内分片错峰；支持时间偏好、周末门与每日计划预览。
- **多账号与多学校**：账号归属用户，管理员按学校绑定；批量动作与批量签到（单次最多 10 条）、失败重试带租约与幂等键。
- **签到结果可核对**：每次尝试记录结局、耗时与重试建议；未知结局不伪装成成功。
- **网页管理台**：账号、用户、学校、管理员、排期、照片、审计、事件诊断、公告与运行时设置。
- **定位策略**：按平台任务下发的签到围栏生成定位点，不伪造设备指纹。
- **照片**：签到照片留存、保留期、可选随机安全裁剪、学校默认图。
- **审计与诊断**：审计链可校验；事件与诊断保留策略可配置。
- **通知**：站内通知、邮件与 webhook（Server 酱 Turbo 或自定义 HTTPS 出口）。
- **安全边界**：密码与凭据密文存储；会话与角色分级；生产环境强制 HTTPS Cookie。

## 目录

| 路径 | 内容 |
|---|---|
| [`server/`](server/) | Go 服务、契约、迁移、签到 provider、门禁工具 |
| [`ui/`](ui/) | React 管理台与公开页面 |
| [`deploy/`](deploy/) | 容器与裸机部署、备份、升级脚本 |
| [`docs/`](docs/) | 开发、运维与发布文档 |
| [`scripts/`](scripts/) | 仓库级门禁与发布脚本 |
| [`tests/`](tests/) | 仓库级 Python 门（公开数据、结构、跳过登记等） |

分层与命名规则见 [`STRUCTURE.md`](STRUCTURE.md)；服务端能力与接口清册见
[`server/README.md`](server/README.md) 与 [`server/contracts/README.md`](server/contracts/README.md)。

## 部署

完整步骤、参数与排错见 [`deploy/README.md`](deploy/README.md)。两种形态：

- **容器（推荐）**：`deploy/container/docker-compose.yml`，一个 app 容器加一个 PostgreSQL 18。
- **裸机**：`deploy/baremetal/install.sh`，systemd 托管，页面目录由 `YIBAN_UI_DIR` 指定。

开始前需要三样东西：

1. PostgreSQL 18 与可连的 DSN（容器 compose 自带 PG 18；裸机需自备）。
2. 两个随机密钥：`YIBAN_HASH_PEPPER`、`YIBAN_CREDENTIAL_KEY`（生成命令见部署文档）。
3. 反向代理与证书。Go 进程不终结 TLS，也不监听公网端口。

没有网页初始化向导。第一个 master 用 CLI 创建：

```bash
# 裸机：先载入 root:root 0600 的运行配置
set -a; . /etc/yiban/server.env; set +a
yiban-cli user create-master --email you@example.com --password-stdin

# 容器：在已启动的 compose 项目里执行
docker compose --env-file deploy/container/compose.env \
  -f deploy/container/docker-compose.yml exec -T app \
  yiban-cli user create-master --email you@example.com --password-stdin
```

容器与裸机的完整命令见 [`deploy/README.md`](deploy/README.md) §3、§4。

## 配置

配置分两层：引导密钥（DSN、`YIBAN_HASH_PEPPER`、`YIBAN_CREDENTIAL_KEY`）只从进程环境读取；
键名册编译进二进制
（[`server/internal/configdata/registry.json`](server/internal/configdata/registry.json)）。
SMTP 六键按数据库 > 环境 > 编译默认取值，`mail.pass` 以密文落库；排期余量、配额、引擎暂停等
运行时设置也在管理台修改，落库并写同事务审计，无需重启。哪些键必填、哪些是秘密、改了
怎么生效，见 [`docs/ops/config-map.md`](docs/ops/config-map.md)。

## 本地开发与门禁

```bash
# Go：格式、vet、单测、契约、结构、许可证
bash server/scripts/verify.sh

# Python 仓库门（结构、公开数据、跳过登记、工作树卫生）
python3 -m pip install --require-hashes -r tests/requirements-gate-tools.txt
bash scripts/dev-verify.sh --ci

# 前端：类型、构建、token 与许可证
cd ui && npm ci && npm run verify
```

需要真实数据库的 Go 测试用 `YIBAN_TEST_DATABASE_URL` 指向一个 PostgreSQL 18。
CI 的 `verify`、`tooling-gate`、`contract-gate`、`stacks`、`ui-e2e` 与部署 e2e
分别覆盖这些面；发布门与 required checks 见
[`docs/dev/release-gate.md`](docs/dev/release-gate.md)。

## 备份与升级

- 备份三部分：数据库、照片卷、配置与密钥（`server.env`、`compose.env`、发布公钥）。
  容器用 compose 的 backup 任务（`deploy/container/backup.sh`），裸机用
  `yiban-cli backup` / `yiban-cli restore`；裸机命令不备份照片卷，照片需另行打包。
  `server.env`、`compose.env` 与发布公钥须手工离线另存，两条备份命令都不包含它们。
  配置或密钥丢失时，已加密的账号凭据不可恢复。步骤与恢复演练见
  [`docs/dev/backup-restore.md`](docs/dev/backup-restore.md)。
- 裸机升级走 `deploy/baremetal/update.sh`：先验签、再自检、再快照、再迁移；回退步骤见
  [`deploy/release.md`](deploy/release.md)。容器升级重建镜像后重新 `up -d`，同样先备份。

## 文档

- 部署与运维：[`deploy/README.md`](deploy/README.md)、[`docs/ops/`](docs/ops/)
- 开发与门禁：[`docs/dev/README.md`](docs/dev/README.md)
- 契约：[`server/contracts/README.md`](server/contracts/README.md)
- 部署 e2e 断言：[`docs/dev/deploy-e2e.md`](docs/dev/deploy-e2e.md)

## 已知边界

- 签到 provider 只在南京工程学院（NJIT）实测；其他学校的任务类型与接口可能不同，
  需要新增或调整 provider，不能假定开箱即用。
- 验证码自动识别明确禁止；遇到验证码由人工处理，本服务不做绕过。
- 使用前请确认符合所在学校的规章制度与平台条款；因使用本工具产生的后果由使用者承担。

## License

**GNU Affero General Public License v3.0（AGPL-3.0）**，见 [`LICENSE`](LICENSE)。
通过网络提供服务时，须向使用者提供完整源代码。

第三方组件与许可证清单：
[`server/THIRD_PARTY_LICENSES.md`](server/THIRD_PARTY_LICENSES.md)；
前端清单由 `ui` 的 `npm run gate:license` 校验。

## AI 生成说明

本仓库的代码、测试与文档在 AI 编码代理参与下完成，并由维护者审查：
先定验收、独立审查、门禁复跑、OCR 全审后合并。
