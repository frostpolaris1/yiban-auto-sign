# -*- coding: utf-8 -*-
"""**功能**
**第三方隔离层**：收录沿用自上游的算法与协议特征（逐块归属见 `PROVENANCE.md`）——
`algo.py` / `protocol.py` 沿用 `onefeifan/fyiban`（AGPL-3.0）；`waf.py` 的挑战形状正则
沿用 `sdk250/Auto-Test`（无 LICENSE）；其"三段字节变换"为本项目重写的纯 Python 运算。

成员与来源：
- `algo.py`     多边形内随机定位点（剪耳三角剖分 + 三角形内均匀采样，退化时回退缩放质心）
- `headers.py`  易班 App 请求头与版本特征（`HEADERS` / `KILLYIBAN_HEADERS`）
- `waf.py`      易盾 WAF（`https_ydclearance`）挑战的纯 Python 解析
- `protocol.py` 登录握手与签到接口的端点、请求形状与响应解析

**归属**
本项目唯一的**非原创代码圈**：把第三方部分圈在一处，使它可以独立核对、替换或升级。
上层（`yiban.client` / 签到引擎）只依赖本层定义的**接口**，不复制其中的算法细节。

**复用**
`algo` / `headers` / `waf` / `protocol` 四个子模块的公开函数与常量是隔离层的对外接口，
由 `yiban.client` 组装使用；本层**不得**被其它模块穿透复制。

**通信**
输入/输出：见各子模块；本层不做 I/O 与安全裁决，一切平台交互经注入的 `requests.Session`、
`policy` 与 `session_store`。
谁调用：`yiban/client.py`（生产唯一入口）。
前端调用点：无直接调用点（隔离层）。
本层不得反向依赖 `yiban.store` / `yiban.security` / web 层。

**两条纪律（改本层前先读）**：
1. **安全策略靠注入，不内联**：URL 白名单、脱敏、日志口径等本项目自有的安全校验
   一律由调用方以参数传入（如 `waf.solve_ydclearance(text, allow_url=...)`）。
   第三方层不得反向依赖 `yiban.store` / `yiban.security` / web 层。
2. **许可与来源随代码走**：`LICENSE`（AGPL-3.0 全文副本）与 `PROVENANCE.md`
   必须留在本目录——它声明了衍生来源，也是 AGPL §5 要求的"修改声明"。
"""
