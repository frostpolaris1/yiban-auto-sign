# -*- coding: utf-8 -*-
"""基础设施层：与业务无关的通用能力（跨进程锁原语、状态 JSON 私有写、wire dump）。

**依赖方向**：本层可被任何层导入，自己**不**导入业务模块（`yiban.store` /
`yiban.client` / `scripts.*` 等一律不引）。这样"锁/env 解析"这类横切关注点
不会把业务逻辑卷进来，单独替换或测试都不需要拉起整个应用。

成员：`env_lock`（.env 写锁）、`locks`（跨进程锁原语，唯一实现）、`private_json`
（状态 JSON 私有写单通道，创建即 0600 的唯一实现）、`wire_dump`（线上报文样本）。

**已迁出（批 B）**：`env_io`（→ `yiban/adapters/config/env_io.py`）、
`account_crypto`（→ `yiban/adapters/config/account_crypto.py`）。旧坐标留转发壳。
"""
