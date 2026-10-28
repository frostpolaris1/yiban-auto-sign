> [← 返回 README](../../README.md)

## 测试范围与适配说明

<details>
<summary>🧪 已测试环境 / 运行测试 / 未覆盖场景</summary>

**已测试环境**：Ubuntu 22.04+／24.04+ 服务器部署、Python 3.13+。

### 运行测试

本地门禁与全量跑测统一走仓内入口（脚本自己建带 `.git` 的 WSL 副本、归一化 LF、用固定 venv、
整份落盘日志；配方见 [`docs/dev/dev-verify.md`](docs/dev/dev-verify.md)）：

```bash
bash scripts/dev-verify.sh          # 本地全量（Windows Git Bash 或 WSL 内同一条命令）
bash scripts/dev-verify.sh --ci     # CI 的关键子集（就地跑，不建副本）
```

下面的是**裸 pytest**，只适合单文件/分组调试，不是门禁口径（缺固定解释器与 LF 归一化，
Windows 工作树上会踩 CRLF 伪红）。

```bash
python -m pytest tests/ -q                 # 全量（串行）
python -m pytest tests/ -q -n auto         # 并发（需 pytest-xdist）

# 按功能域运行（文件前缀 = 功能域）
python -m pytest tests/test_notify_*.py    # 消息推送与告警
python -m pytest tests/test_web_*.py       # 网页管理后台
python -m pytest tests/test_scheduler*.py  # 调度器
python -m pytest tests/test_signin*.py     # 签到核心
python -m pytest tests/test_db_*.py        # 数据库与迁移

python -m pytest tests/test_smoke.py -v    # 单个文件
```

测试文件按**功能域**命名（`test_<功能>.py`），便于按需运行与定位；完整清单见 [`tests/README.md`](tests/README.md)。`testpaths = ["tests"]` 由 `pyproject.toml` 配置，`scripts/` 下的独立压力测试不参与默认收集。

**未覆盖场景**（请自行验证）：

- **Docker / Windows 部署**：未测试（仅 Ubuntu 验证）
- **消息通知**（Server 酱 / Bark / 企业微信）：代码已实现并测试，但未在生产环境实测通知链路，建议配置后自行验证
- **多学校适配**：易班校本化签到因学校而异（接口与任务类型可能不同）。本项目**仅在南京工程学院（NJIT）实测**，其他学校可能需要适配
- **多校混合账号**：未测试同一实例下多校账号混合签到

如你在其他学校使用成功或有适配需求，欢迎提交 Issue 或 PR。

</details>
