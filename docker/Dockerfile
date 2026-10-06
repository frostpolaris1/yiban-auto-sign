# ============================================================
# 易班自动签到 - Docker 镜像（x86_64）
# 构建上下文 = 仓库根目录：
#   docker build -t yiban-auto-sign . -f docker/Dockerfile
# 或直接 `docker compose up -d --build`（已配好）。
# ------------------------------------------------------------
# 设计约束：
#   1. 时区设为上海：窗口与日期判定在程序内已统一按北京时间算（yiban/clock.py），
#      这里是双保险，也让日志/依赖工具链的本地时间与业务时间同口径。
#   2. 数据（SQLite / .env / 日志 / 加密密钥）全部落 /data 外部卷，
#      保证重启不丢、便于备份与平滑升级，绝不写入镜像层。
#   3. 合规文档 *.md 必须拷入镜像（web 端渲染《用户协议/隐私政策》用）。
# ============================================================
FROM python:3.11-slim

# 时区：签到窗口按本地时间计算，必须设为上海，否则签到错 8 小时
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1

# supervisor：容器内常驻拉起 gunicorn(web) 与调度(sched)；tzdata：时区数据；
# gnupg + tar + coreutils：M44 的容器内定时备份（调度器 02:00 调 backup-docker.sh，
# 它按 tar|gpg 流式加解密）。这三样过去容器里没有，于是"容器形态自带备份"根本没装。
RUN apt-get update \
 && apt-get install -y --no-install-recommends supervisor tzdata gnupg tar coreutils \
 && ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
 && rm -rf /var/lib/apt/lists/*

# 非 root 专用用户（2026-08-27 审查加固 P2-11）：业务子进程以此身份运行，
# 入口脚本先做一次 /data 卷属主准备再交由 supervisord 按该用户拉起。
RUN groupadd -r -g 10001 yiban \
 && useradd -r -u 10001 -g yiban -d /nonexistent -s /usr/sbin/nologin yiban

WORKDIR /app

# 依赖（flask/gunicorn/requests/pycryptodome 等）
# 镜像安装改用精确锁定的 requirements.lock——CI 的 `dependency-audit` job
# （.github/workflows/ci.yml，跑 `pip-audit -r requirements.lock`）审计的就是它，
# 浮动下限会让镜像依赖偏离审计结论且构建不可复现。
# requirements.lock 是手工维护的精确锁定、全程不含 `--hash=`，没有哈希锁可复核，
# 因此 **pip-audit 是这里唯一的自动依赖审计**——改 lock 必须同时看那个 job 的结论；
# 它能否**阻塞合并**取决于分支保护是否把 `dependency-audit` 列为必需检查（所有者动作）。
# 国内主机直连 pypi.org 常不可达（构建会以 "No matching distribution" 失败），
# 故支持构建期指定索引：docker build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple ...
ARG PIP_INDEX_URL
COPY requirements.lock ./
RUN pip install --no-cache-dir ${PIP_INDEX_URL:+-i "$PIP_INDEX_URL"} -r requirements.lock

# 应用源码（web 管理端 + 签到脚本 + 共享包 + 合规文档）
# `yiban/` 是共享包（yiban/status.py 起被 signin/web 导入），漏拷会让容器在
# import 阶段直接崩——tests/test_docker_image_contents.py 自动比对防复发。
COPY web/ ./web/
COPY scripts/ ./scripts/
COPY yiban/ ./yiban/
# 配置名册（104 §5.3）：`config/registry.json` 是每个配置键类型/域/缺省的唯一定义点，
# `yiban/config_loader.py` 在 import 阶段读它取默认常量——漏拷会让容器在 import 阶段
# 直接崩（本地跑测与裸机部署发现不了，它们的 sys.path 里本来就有仓库根）。
# 守卫：tests/test_config_registry_gate.py 的
# GateRealRegistryContentTest::test_real_registry_is_tracked_and_shipped 钉这行 COPY 与
# 该文件入库（config/ 不是 Python 包，test_docker_image_contents.py 的包判据盖不到它）。
COPY config/ ./config/
# 合规文档 / 更新日志 / 自述显式白名单（M84）：原 `COPY ./*.md ./` 是根级通配，
# 哪些 .md 进镜像取决于 .dockerignore 与 .gitignore 两份清单的差集——一旦本地存在
# AGENTS.md / PROMPT*.md / HANDOFF*.md / MEMORY.md / MAINTENANCE.md 这类含服务器 IP
# 与部署细节的运维文档，就会被一并烤进镜像。改为只拷运行时真正读取的这几份：
#   USER_AGREEMENT.md / PRIVACY_POLICY.md（web/render.py 渲染合规页）
#   CHANGELOG.md（web/routes/settings_api.py 的更新日志接口）
#   README.md（公开自述）
COPY USER_AGREEMENT.md PRIVACY_POLICY.md CHANGELOG.md README.md ./

# 容器内进程管理 / 调度 / 入口
COPY docker/supervisord.conf /etc/supervisord.conf
COPY docker/entrypoint.sh /entrypoint.sh
COPY docker/scheduler.py ./scripts/container_scheduler.py
# M44：容器内定时备份脚本。它在仓库里位于 docker/，镜像里与调度器同处 scripts/
# （调度器的 _backup_script_path 按候选列表解析，两处位置都成立）。
COPY docker/backup-docker.sh ./scripts/backup-docker.sh

RUN chmod +x /entrypoint.sh ./scripts/backup-docker.sh
# 数据目录（SQLite / .env / 日志 / 加密密钥 / 状态文件），由 compose 挂载
VOLUME ["/data"]

# gunicorn 监听端口（仅回环，由共享网络栈的 nginx 反代）
EXPOSE 17892

CMD ["/entrypoint.sh"]