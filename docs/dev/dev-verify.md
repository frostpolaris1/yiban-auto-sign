# 统一跑测入口 `scripts/dev-verify.sh`

> 2026-11-27 旧 Python 栈退役后重写。本脚本只承载 Python 面：`ruff` + `pytest tests/`。
> Go 与 UI 的门在 `server/scripts/verify.sh` 与 `ui npm run verify`，由
> `scripts/verify-stacks.sh` 汇总；CI 的 `verify` 与 `stacks` 作业分别调这两条入口。

## 1. 用法

```bash
bash scripts/dev-verify.sh          # 全量：ruff + pytest tests/
bash scripts/dev-verify.sh --ci     # CI 入口：同一集合
bash scripts/dev-verify.sh --fast   # 提交前自查：同上（参数保留给调用方习惯）
bash scripts/dev-verify.sh --help
```

所有模式跑**同一集合**，没有范围档、没有车道名册、没有快集/慢集/串行轨之分。
`--slow`、`--fast-all`、`--fast-scoped` 仍被接受，但不再改变行为。

`--log-dir DIR` 只作兼容参数：接受取值，不再建副本、不落日志。
`--repo` 已不支持（脚本只跑自身仓库），传了即退出码 2，避免误跑别的树。

解释器默认 `python3`，可用 `DEV_VERIFY_PY` 覆盖；必须能
`import pytest, pytest-xdist, ruff`，否则退出码 2 并点名。

## 2. 跑什么

```bash
python -m ruff check tests scripts --quiet
python -m pytest tests/test_public_exposure_gate.py tests/test_public_exposure_gate_wiring.py -q
python -m pytest tests/ -q -n <workers> --dist loadfile
```

- 公开数据门在 `run_ci` 内**显式点名**，且与接线守卫同一步执行；不放在任何条件分支里。
- pytest 并发默认 `max(1, nproc-1)`，可用 `PYTEST_WORKERS` 覆盖；`--dist loadfile`
  保证同一文件落在同一 worker。
- 当前 `tests/` 为 18 个文件、177 个用例，本机约 5 秒；数字只作观察，门以命令退出码为准。

## 3. 退出码

| 码 | 含义 |
|---|---|
| `0` | ruff 与两次 pytest 全 0 |
| `1` | ruff 或 pytest 非 0 |
| `2` | 环境错误：未知参数、解释器不存在、缺 pytest/pytest-xdist/ruff、`PYTEST_WORKERS` 非正整数 |

没有退出码 3。旧版的“空覆盖”语义随范围档一起删除。

## 4. CI 接线

- `.github/workflows/ci.yml` 的 `verify` 作业先装 `ruff`/`pytest`/`pytest-xdist`，
  再调 `bash scripts/dev-verify.sh --ci`。
- 命令漂移由 `tests/test_dev_verify_entry.py` 冻结：CI 必须调本入口，`run_ci` 必须含
  ruff、公开数据门、全量 pytest，且门与接线在同一逻辑命令。
- `tests/test_public_exposure_gate_wiring.py` 守同一件事的接线面。

## 5. Go 与 UI

```bash
bash scripts/verify-stacks.sh
```

它依次跑 `server/scripts/verify.sh` 与 `ui` 的 `npm run verify`。
Go 用 1.27.2+；缺 `go` 或 `npm` 时退出码 2，跳过不算绿。
CI 的 `stacks` 作业与本地入口同源。

## 6. 已知边界

- 本脚本只跑 Python 面；Go 与 UI 不在这里混跑。
- 旧目录（`yiban/`、`web/`、`frontend/`、`docker/`、`config/`）已删除，
  文中不再保留旧车道的墙钟与名册数字。
