# 生产入口 umask 077 不还原：评估结论（工单 5f4t）

## 结论

**保留现状：生产入口把进程 umask 收到 `077` 后不还原。**

判据：这些入口都是**进程级短生命周期入口**，进程内后续创建的全部文件都是运行期
敏感件（状态、凭据、日志、备份产物）。收窄 umask 正是期望行为；"还原"会让后续
写入重新落到 `022`，把凭据/状态文件暴露给同主机其他用户。

## 现量（2026-10-20，`fix/gates-docs` 树）

| 入口 | 位置 | 形态 |
|------|------|------|
| 宿主签到 | `run.sh:2` | `umask 077`（脚本第一行） |
| 宿主探针 | `run_probe.sh:2` | 同上 |
| 容器入口 | `docker/entrypoint.sh:9` | 同上 |
| 容器 web | `docker/supervisord.conf:8` | `umask=077`（supervisord 对全部子进程生效） |
| 容器备份 | `docker/backup-docker.sh:44` | `umask 077` |
| 宿主备份 | `scripts/backup.sh:79`、`scripts/pull-prod-backup.sh:47` | 同上 |
| 状态清理 | `scripts/state_cleanup.py:94` | `os.umask(0o077)` |
| 引擎入口 | `yiban/app/runner.py:216` | `os.umask(0o077)`（兜底 web 子进程/裸调路径） |
| CLI 入口 | `yiban/entry/cli/__init__.py:673` | `os.umask(0o077)`（兜底备份副本等） |

## 为什么不在入口还原

1. **进程内没有"需要还原"的写者**。两个 Python 入口的 umask 都在参数解析之前设置；
   其后创建的文件只有运行期敏感件。实测 `grep -rn "open(.*['\"]w" yiban/app/runner.py`
   与 CLI 子命令的落盘点，全部是状态/日志/备份/凭据面。
2. **还原需要猜一个"原始值"**。`os.umask` 只能通过设置来读取；在入口处先读再还原，
   等于把"原始 umask 可能是 000"这一风险带回。生产入口的约定是"本进程及其子进程
   一律 077"，不还原才是单义口径。
3. **子进程必须继承 077**。`runner` 拉起的执行体、`cli` 拉起的备份/恢复动作都在同一
   进程树下创建敏感件；还原会让子进程回到宽松默认。
4. **测试层已隔离**。`tests/conftest.py` 的 `_restore_process_umask` 在逐用例前后
   各还原一次；`tests/test_conftest_umask_guard.py` 用子进程实测钉住它。生产入口
   在测试进程内被直调时不会污染下一条用例。

## 影响面（不改的理由有数据）

- 受影响的只有"本进程内新建文件的权限位"；已存在文件的权限位不受 umask 影响
  （收紧已存在文件由各写入方的显式 `os.chmod` / `os.open(..., 0o600)` 承担，
  见 `scripts/db_export.py` 与 `scripts/state_cleanup.py` 的 `_append_log`）。
- 不改 `run.sh` 之后的步骤：`run.sh` 自己就是 `umask 077`，后续步骤与改前一致。
- systemd 部署模板未设 `UMask=`，但它启动的是 `run.sh`/`gunicorn`，前者自带 077，
  后者由 `supervisord`/入口脚本覆盖；不依赖宿主默认。

## 重新评估的触发条件

出现下列任一情况时重开本单：

- 生产入口在同一进程内创建**需要组/其他可读**的文件（例如导出给非属主进程读取的
  状态快照）；
- 有入口在设置 umask **之后** `exec` 一个需要宽松默认的非本仓程序；
- systemd 模板改为直接 `ExecStart=python ...` 且不再经过 `run.sh`/入口脚本。
