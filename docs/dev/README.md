# 开发文档（项目地图）

本目录是**可入库**的开发文档：模块地图、依赖方向、接口契约。与设计/调研类文稿分开——这里的每条内容都按脱敏红线自查过：
不含真实域名/IP/邮箱/手机号/密钥与站点注册信息，示例一律用占位值。

## 目录树与职责（一句话版）

```
yiban/                        签到引擎与共享基础（可被 web / scripts / docker 复用）
├── clock.py                  时间口径唯一来源（北京时间；禁止各写 datetime.now()）
├── window.py                 签到窗口口径（bounds / from_env / full_sec / retry_hm）
├── status.py                 状态码词汇表（唯一事实源，含两张刻意不合并的映射表）
├── masking.py                脱敏与清洗（手机号 / 日志文本 / URL query）
├── security.py               WAF 判定 + URL 白名单 + 脱敏描述（注入协议层，不内联）
├── egress.py                 **出口（代理）分配**：single / worker / fallback 三角色
├── client.py                 客户端外观：凭据托管 / 会话缓存 / 代理 / 设备绑定 / 签到分支
├── cred_state.py             账密熔断状态文件
├── state_gc.py               按日状态文件的保留期策略与清理
├── logging_ext.py            按天 + flock 的日志 handler
├── fyiban/                   ★ 第三方隔离层（AGPL-3.0 上游衍生，见 PROVENANCE.md）
│   ├── algo.py               多边形内随机定位点（缩放质心 + 射线法）
│   ├── headers.py            易班 App 请求指纹（版本号/请求头）
│   ├── waf.py                易盾 WAF 挑战纯 Python 解析 + 挑战特征识别
│   └── protocol.py           端点/参数/页面正则/握手顺序（平台事实，无安全判断）
├── infra/                    叶子工具：locks / env_io / env_lock / account_crypto
├── store/                    数据层：accounts / verify_jobs / claims（按表分文件）
├── notify/                   通知推送：config / ledger（额度与节流账本）/ transport
└── mail/                     邮件：config / transport

web/                          管理端（Flask 工厂 + 路由 + 模板 + 静态资源）
├── app.py                    create_app 工厂与全部路由（拆分进行中）
├── static/js/pages/*.js      每页一份脚本；共享组件在 core.js（YB.*）
├── static/css/app.css        设计令牌与组件样式
└── deploy/                   systemd / nginx / logrotate 部署模板

scripts/                      运维 CLI（含过渡期的兼容壳）
├── signin.py                 签到引擎 CLI（协议/客户端/策略已抽到 yiban/）
├── db.py                     连接 + 迁移（表级实现已迁到 yiban/store/）
├── state_cleanup.py          状态文件清理 CLI
└── loadtest/                 压测与容量基准（仅限隔离测试机，零真实外联）
    ├── mock_yiban.py         假易班（真 TLS，覆盖两条登录流程 + 签到）
    ├── mock_env.py           自签证书 + hosts 回环 + 出站兜底（一键搭建/还原）
    ├── capacity_probe.py     **容量基准**：一条命令给出建议执行体数与每执行体账号数
    ├── concurrency_probe.py  K 阶梯并发探针
    └── scale_driver.py       单进程规模驱动

docker/                       容器：Dockerfile / entrypoint / supervisord / scheduler
```

## 依赖方向（单向，有守卫测试钉住）

```
web / scripts / docker  →  yiban.*  →  infra, fyiban, store（store 延迟 import scripts/db）
```

- `yiban/infra/` 与 `yiban/fyiban/` **不得导入业务模块**（`tests/test_infra_layer.py`、
  `tests/test_fyiban_isolation.py`）；
- `yiban/fyiban/` 的安全策略必须由调用方**注入**（协议层不自算白名单与拦截判定）；
- `scripts/*.py` 直接运行时**必须先引导 `sys.path`**，且引导要早于任何 `yiban` 导入
  （`tests/test_deploy_entry_imports.py`）。

## 多执行体形态（当前实现状态）

| 能力 | 状态 |
|------|------|
| 领取池与账号级租约（一个账号一天只被一个执行体做） | 已实现（表 `sign_claims`，v17） |
| 并行执行体 | 已实现：`signin.py --workers N`（父进程监督 + 子进程领活） |
| 兜底常驻执行体 | 已实现：`signin.py --fallback`（窗口内反复扫"未了结"账号，时段结束退出） |
| 每个执行体独立出口 | 已实现：`yiban/egress.py` + 下列环境变量（留空=直连） |
| 前端页面（执行体与出口配置） | **未实现**（接口已就绪，见 `api-executors.md`） |

### 出口配置（部署者视角）

| 环境变量 | 作用 | 示例 |
|----------|------|------|
| `YIBAN_PROXY` | 单执行体 / 默认出口 | `http://host:3128` |
| `YIBAN_PROXY_LIST` | **每个并行执行体一个**，逗号分隔；空位=该执行体直连 | `http://a:1,,http://c:3` |
| `YIBAN_PROXY_FALLBACK` | 兜底常驻执行体的出口（未设则用 `YIBAN_PROXY`） | `http://fb:8080` |
| `YIBAN_WORKERS` | 并行执行体数（未设=1） | `4` |
| `YIBAN_FALLBACK_INTERVAL` | 兜底执行体扫描间隔秒（默认 60） | `60` |
| `YIBAN_CAPACITY_MEASURED` | 部署者实测的**单执行体容量**（账号/窗口），用于给出建议值 | `354` |

规则细节（按序取用、不足循环、空位语义）与脱敏口径见 `yiban/egress.py` 的模块文档；
容量基准由 `scripts/loadtest/capacity_probe.py` 实测得到，**建议值 = 实测 × 2/3，只是建议**。
