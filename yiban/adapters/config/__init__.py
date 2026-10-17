# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""配置适配器（adapters/config）：env 读写 + 配置名册加载器 + 密钥（契约第 3、6 节）。

成员来自批 B 搬迁：
- `env_io`：`.env` / `secrets.env` 解析与写回的单一实现（原 `yiban/infra/env_io.py`）；
- `loader`：配置名册加载器（原 `yiban/config_loader.py`）；
- `account_crypto`：账号字段 AES-GCM 加解密（原 `yiban/infra/account_crypto.py`）。

旧坐标留转发壳（`yiban/infra/env_io.py`、`yiban/infra/account_crypto.py`、
`yiban/config_loader.py`），读者无需改动。

依赖方向：本层实现 ports。本层不得导入 `yiban.app` / `yiban.entry` / `yiban.workers`
（契约 `adapters-no-upward`）。
"""
