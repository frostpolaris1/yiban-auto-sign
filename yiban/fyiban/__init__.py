# -*- coding: utf-8 -*-
"""**第三方隔离层**：沿用 `onefeifan/fyiban`（AGPL-3.0）的算法与协议特征。

本层存在的理由：把**非原创**部分圈在一处，使它可以独立核对、替换或升级——上层
（`yiban.client` / 签到引擎）只依赖本层定义的**接口**，不复制其中的算法细节。

成员与来源（逐块见 `PROVENANCE.md`）：
- `algo.py`     多边形内随机定位点（缩放质心 + 射线法）
- `headers.py`  易班 App 请求头与版本特征（`HEADERS` / `KILLYIBAN_HEADERS`）
- `waf.py`      易盾 WAF（`https_ydclearance`）挑战的纯 Python 解析
- `protocol.py` 登录握手与签到接口的端点、请求形状与响应解析

**两条纪律（改本层前先读）**：
1. **安全策略靠注入，不内联**：URL 白名单、脱敏、日志口径等本项目自有的安全校验
   一律由调用方以参数传入（如 `waf.solve_ydclearance(text, allow_url=...)`）。
   第三方层不得反向依赖 `yiban.store` / `yiban.security` / web 层。
2. **许可与来源随代码走**：`LICENSE`（AGPL-3.0 全文副本）与 `PROVENANCE.md`
   必须留在本目录——它声明了衍生来源，也是 AGPL §5 要求的"修改声明"。
"""
