# 发布物签名（Ed25519）

> 读者：发布操作者、部署运维。上游裁决：master-plan P7「签名更新：ed25519 清单、
> sha256、健康检查、回滚」中的签名部分（健康检查与回滚已由 `update.sh` 承担）。
> 私钥红线见 §2；信任锚见 §3；流程见 §4；校验语义见 §5；门见 §6；边界见 §7。

## 1. 组成

| 件 | 作用 |
|---|---|
| `scripts/release/make-manifest.sh <version> <file>...` | 生成单行 `manifest.json`：`{"version":...,"files":[{"name":...,"sha256":...}]}`；键序固定、结尾换行（可复现字节） |
| `scripts/release/sign.sh --key PRIVATE.pem --in manifest.json --out manifest.json.sig` | openssl 3 Ed25519 纯签名（`-rawin`），签名字节＝清单文件字节 |
| `scripts/release/verify.sh --pubkey PUB.pem --manifest manifest.json --signature manifest.sig` | 只验签，不判断公钥是否可信；rc=0 通过／rc=1 不通过／rc=2 验证没跑 |
| `deploy/linux/update.sh --manifest/--signature/--pubkey [--expect-version V]` | 先比指纹，再验签，再对账 sha256；任何失败不碰旧件、不停服务。未给三件默认拒绝（exit 2）；`--allow-unsigned` 是显式逃生口，与三件互斥 |
| `<安装根>/release-pubkey.sha256` | 信任锚：管理员在安装时写入的发布公钥 SHA-256 |
| `scripts/release-check.sh` 项 8（`signing`） | 三件齐全且**外部锚**匹配时验签；缺件或验证没跑显性 SKIP（跳过≠绿） |

## 2. 密钥

- 生成：`openssl genpkey -algorithm ED25519 -out yiban-release-key.pem`；
  公钥：`openssl pkey -in yiban-release-key.pem -pubout -out release-pubkey.pem`。
- **私钥红线**：私钥永不入库、不与部署机同机明文存放。私钥只留在发布者机器。
- **公钥不入库**：仓库只收指纹示例 `deploy/linux/release-pubkey.sha256.example`
  与本文档。真实公钥随发布件分发，但不提交。
- **指纹入受管配置**：指纹不是秘密，要防的是篡改。
  部署机写 `/release-pubkey.sha256`（root 持有）；一次性调用可用环境变量
  `YIBAN_RELEASE_PUBKEY_SHA256`。
- **门**：`tests/test_private_key_gate.py` 内容扫描全部跟踪文件。`*.pem` 与
  仓库根 `release/` 已进 `.gitignore`；标记常量拼接生成（门不自匹配）；
  临时 git 仓库自证扫描真跑（WSL/CI 不依赖当前工作树的 `.git`）。
- 轮换：新公钥随发布件；管理员更新 `/release-pubkey.sha256`；旧钥失效。
  `update.sh` 不内置公钥，无兼容矩阵。

## 3. 信任锚从哪来

1. 发布者在自己的机器生成密钥对（§2）。私钥不出该机器。
2. 发布者把公钥的 SHA-256 用**带外渠道**交给运维：当面、电话核对、
   或已建立 TLS 的工单系统。不要经发布件目录回传。
3. 运维在安装时写入 `/release-pubkey.sha256`（一行 64 位十六进制）。
   文件内容不是秘密；属主必须是 root，权限 0644。
   组/其他可写的锚会被拒绝（`update.sh` exit 2）。
4. 升级时 `update.sh` 先算 `--pubkey` 的 sha256，再与钉住值比对。
   缺锚 exit 2；不符 exit 2。两者都发生在停服务之前。
5. 没有锚就拒绝升级（fail-closed）。无锚时无法区分发布方密钥与攻击者密钥。
6. **发布门只信外部锚**：`release-check.sh` 只认环境变量
   `YIBAN_RELEASE_PUBKEY_SHA256`，或 `--pubkey-sha256-file` 指定的文件。
   该文件必须在发布件目录之外。发布件目录内的 `release-pubkey.sha256`
   一律不认：整包不可信时，随包的锚也不可信，等于没锚。
   缺锚在 `--require-signing` 下是 FAIL，报
   `anchored fingerprint must come from outside the release directory`。

## 4. 发布与升级流程

1. 构建 `yiban-server` 与 `yiban-cli`。
2. `bash scripts/release/make-manifest.sh 0.7.0 yiban-server yiban-cli > manifest.json`
3. `bash scripts/release/sign.sh --key yiban-release-key.pem --in manifest.json --out manifest.json.sig`
4. 发布件目录（缺省 `release/`，`YIBAN_RELEASE_DIR` 可覆盖）放三件：
   `manifest.json`＋`manifest.json.sig`＋`release-pubkey.pem`。
5. 发布门：`bash scripts/release-check.sh --only signing --require-signing`。
   锚用环境变量 `YIBAN_RELEASE_PUBKEY_SHA256` 给，或用
   `--pubkey-sha256-file <发布件目录之外的路径>`。
   `--require-signing` 把缺件、缺锚与缺 openssl 从 SKIP 升为 FAIL。
   发布操作必须用它。
6. 升级：
   `sudo bash deploy/linux/update.sh --server-bin NEW [--cli-bin NEW] --manifest manifest.json --signature manifest.sig --pubkey release-pubkey.pem --expect-version 0.7.0`
   未给三件 → 默认拒绝（exit 2，发生在停服务之前）。
   钥匙仪式未完成的部署显式给 `--allow-unsigned` 放行（显性 WARN，跳过≠绿）；
   逃生口与三件互斥，同给即 exit 2。

## 5. 校验语义

- 三层：指纹（这把钥是不是发布方的）→ 签名（清单是不是这把钥签的）
  → sha256（文件是不是清单列的）。三层都过才可信。
- 版本：`--expect-version` 钉住期望版本；不符 exit 1。防已签名旧包重放。
- `verify.sh` rc=2 表示验证**没有跑**（openssl 缺失、文件缺失）——
  调用方必须按失败处理。
- `update.sh` 次序保证：指纹 → 验签 → 版本 → sha256 → 停服务 → 迁移 → 换件。
  前四步任一失败：未停服务、未换件、未跑迁移，退出码 1 或 2 并点名步骤。
- **TOCTOU 防护**：`update.sh` 先把清单、签名、公钥与新二进制复制进 root
  私有暂存目录（`mktemp -d`，0700）。之后的哈希、验签与安装只读副本。
  失败路径删除暂存目录与 `.new` 暂存件。

## 6. 门与测试

- Go 干跑：`go test ./cmd/cli -run "TestUpdateSh|TestInstallSh" -count=1`。
  覆盖：验签通过、篡改清单拒绝、缺锚拒绝、错指纹拒绝、清单缺条目拒绝、
  二进制与清单不符拒绝、版本不符拒绝、未给三件默认拒绝（rc=2、未停服务、
  旧件未动）、`--allow-unsigned` 有痕放行、逃生口与三件同给拒绝。
- 私钥入库门：`py -3 -m pytest tests/test_private_key_gate.py -q`
  （WSL 用 `/root/.venv-yiban-wsl/bin/python -m pytest`）。
- 发布门：`bash scripts/release-check.sh --selftest`（含「篡改必红」「错指纹必红」
  「缺锚在 `--require-signing` 下必红」「私钥不得被 `--out` 覆盖」突变）。
- e2e：容器路径 C4b（签发→验签→错公钥必拒→篡改必拒）、裸机路径 B4b。
  命令见 `docs/dev/deploy-e2e.md`。

## 7. 已知边界

- 首次安装（`install.sh`）从 git 检出安装，信任由 git 承担；签名覆盖的是
  **升级路径**的发布物。
- 未签名升级必须显式给 `--allow-unsigned`（默认强制验签）。
  逃生口只影响验签分支；停服务、迁移、健康检查与回滚不受影响。
  WARN 行留在 stderr 供审计。
- 清单覆盖二进制；schema 与配置随二进制发布，不单列。
- **已签名旧包可重放（降级）**：不传 `--expect-version` 时，持有旧发布件的人
  可把版本退回去。传 `--expect-version` 可挡。发布操作必须传该参数。
- 信任锚文件放在安装根（`<安装根>/release-pubkey.sha256`）。干跑时同前缀：
  `$YIBAN_INSTALL_ROOT/release-pubkey.sha256`。
