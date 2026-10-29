# 发布物签名（Ed25519）

> 读者：发布操作者、部署运维。上游裁决：master-plan P7「签名更新：ed25519 清单、
> sha256、健康检查、回滚」中的签名部分（健康检查与回滚已由 `update.sh` 承担）。
> 私钥红线见 §2；校验语义见 §4；门与测试见 §5。

## 1. 组成

| 件 | 作用 |
|---|---|
| `scripts/release/make-manifest.sh <version> <file>...` | 生成单行 `manifest.json`：`{"version":...,"files":[{"name":...,"sha256":...}]}`；键序固定、结尾换行（可复现字节） |
| `scripts/release/sign.sh --key PRIVATE.pem --in manifest.json --out manifest.json.sig` | openssl 3 Ed25519 纯签名（`-rawin`），签名字节＝清单文件字节 |
| `scripts/release/verify.sh --pubkey PUB.pem --manifest manifest.json --signature manifest.json.sig` | 验签；rc=0 通过／rc=1 不通过／rc=2 用法或环境错误（验证没跑） |
| `deploy/linux/update.sh --manifest/--signature/--pubkey` | **动任何已装文件之前**验签＋逐文件 sha256 对账；三参数必须同给或同不给 |
| `scripts/release-check.sh` 项 8（`signing`） | 发布件目录三件齐全时跑验签；缺件/缺 openssl 显性 SKIP（跳过≠绿） |

## 2. 密钥

- 生成：`openssl genpkey -algorithm ED25519 -out yiban-release-key.pem`；
  公钥：`openssl pkey -in yiban-release-key.pem -pubout -out release-pubkey.pem`。
- **红线**：私钥永不入库、不与部署机同机明文存放。仓库只收公钥（随发布件目录）。
- **门**：`tests/test_private_key_gate.py` 内容扫描全部跟踪文件（`*.pem` 与
  `release/` 已进 `.gitignore`；扫空不得当绿；改文件名不能绕过）。
- 轮换：换钥 = 新公钥随发布件分发；`update.sh` 不内置公钥（显式 `--pubkey`），无兼容矩阵。

## 3. 发布与升级流程

1. 构建 `yiban-server` 与 `yiban-cli`。
2. `bash scripts/release/make-manifest.sh 0.7.0 yiban-server yiban-cli > manifest.json`
3. `bash scripts/release/sign.sh --key yiban-release-key.pem --in manifest.json --out manifest.json.sig`
4. 发布件目录（缺省 `release/`，`YIBAN_RELEASE_DIR` 可覆盖）放三件：
   `manifest.json`＋`manifest.json.sig`＋`release-pubkey.pem`。
5. 升级：
   `sudo bash deploy/linux/update.sh --server-bin NEW [--cli-bin NEW] --manifest manifest.json --signature manifest.json.sig --pubkey release-pubkey.pem`
   未给三件 → 行为不变，打印显性 WARN（未签名升级，跳过≠绿）。

## 4. 校验语义

- 签名只证明清单字节出自私钥持有者；**sha256 对账**把清单绑定到本地文件。两者都过才可信。
- `verify.sh` rc=2 表示验证**没有跑**（openssl 缺失、文件缺失）——调用方必须按失败处理。
- `update.sh` 的次序保证：验签失败 → 未停服务、未换件、未跑迁移，退出码 1 并点名
  `(step: signing)`。

## 5. 门与测试

- Go 干跑测试：`go test ./cmd/cli -run TestUpdateSh`
- 私钥入库门：`pytest tests/test_private_key_gate.py`（跟踪文件零私钥块；有牙自证）。
  （验签通过执行／篡改清单拒绝且不碰旧件／三件缺一报用法错）。
- 发布门：`bash scripts/release-check.sh --only signing`；
  `--selftest` 含「篡改一字节必红」突变（openssl 在时执行，否则显性跳过）。
- e2e：容器路径 C4b（签发→验签→篡改必红）、裸机路径 B4b（真二进制签发→验签）。
  命令见 `docs/dev/deploy-e2e.md`。

## 6. 已知边界

- 首次安装（`install.sh`）从 git 检出安装，信任由 git 承担；签名覆盖的是**升级路径**的发布物。
- 清单覆盖二进制；schema 与配置随二进制发布，不单列。
