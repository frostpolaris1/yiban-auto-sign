# docker —— 容器形态执行件

## 放什么

容器形态的构建与运行件：`Dockerfile`、`entrypoint.sh`、`supervisord.conf`、
`scheduler.py`、`nginx.conf`、`backup-docker.sh`。

## 不放什么

不放业务代码（在 `yiban/` 与 `web/`）、不放宿主 cron（在 `deploy/prod/`）、
不放 Web 进程内部署模板（在 `web/deploy/`）。开发工具 `tools/` 不进镜像。

## 入口

`docker-compose.yml` → `docker/Dockerfile` → `entrypoint.sh` → supervisord
拉起 web（gunicorn）与 sched。`scheduler.py` 在镜像里落成 `scripts/container_scheduler.py`。

## 遗留与删除条件

`scheduler.py` 有两种运行位置：仓库内 `docker/` 与镜像内 `scripts/`。
两处都比仓库根低一层，故保留 sys.path 引导。删除条件：M1③ 清掉 sys.path 注入
（模块改为纯包导入）后，同一批删除引导块。
