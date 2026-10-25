# deploy —— 裸机部署执行件

## 放什么

裸机形态的执行件：`deploy/prod/manifest.tsv`（安装清单）、`cron.d/`、`tmpfiles.d/`、
安装脚本与备份包装器。清单一行一个生产执行件，给仓库源路径与生产目标路径。

## 不放什么

不放容器形态（在 `docker/`）、不放 Web 进程内模板（在 `web/deploy/`）、不放业务代码。
不放密钥与主机信息。

## 入口

安装：`deploy/prod/install.sh`。清单：`deploy/prod/manifest.tsv`。
cron 路径来源由 `scripts/check-cron-provenance.sh` 与 `tests/test_deploy_prod_artifacts.py` 钉住。

## 遗留与删除条件

三种部署形态（裸机、容器、Web 进程内模板）保持分开，见 `docs/dev/target-tree.md` §10。
删除条件：三种形态合并为一条部署链时，整目录重估。
