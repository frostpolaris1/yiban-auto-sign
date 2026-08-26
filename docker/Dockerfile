# ============================================================
# 易班自动签到 - Docker 镜像（x86_64）
# 构建上下文 = 仓库根目录：
#   docker build -t yiban-auto-sign . -f docker/Dockerfile
# 或直接 `docker compose up -d --build`（已配好）。
# ------------------------------------------------------------
# 设计约束：
#   1. 时区必须为上海——签到窗口按本地时间计算，容器默认 UTC 会错 8 小时。
#   2. 数据（SQLite / .env / 日志 / 加密密钥）全部落 /data 外部卷，
#      保证重启不丢、便于备份与平滑升级，绝不写入镜像层。
#   3. 合规文档 *.md 必须拷入镜像（web 端渲染《用户协议/隐私政策》用）。
# ============================================================
FROM python:3.11-slim

# 时区：签到窗口按本地时间计算，必须设为上海，否则签到错 8 小时
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1

# supervisor：容器内常驻拉起 gunicorn(web) 与调度(sched)；tzdata：时区数据
RUN apt-get update \
 && apt-get install -y --no-install-recommends supervisor tzdata \
 && ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
 && rm -rf /var/lib/apt/lists/*

# 非 root 专用用户（2026-08-27 审查加固 P2-11）：业务子进程以此身份运行，
# 入口脚本先做一次 /data 卷属主准备再交由 supervisord 按该用户拉起。
RUN groupadd -r -g 10001 yiban \
 && useradd -r -u 10001 -g yiban -d /nonexistent -s /usr/sbin/nologin yiban

WORKDIR /app

# 依赖（flask/gunicorn/requests/pycryptodome 等；textual 供容器内 python3 -m tui 可选使用）
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 应用源码（web 管理端 + 签到脚本 + TUI + 合规文档）
COPY web/ ./web/
COPY scripts/ ./scripts/
COPY tui/ ./tui/
COPY ./*.md ./

# 容器内进程管理 / 调度 / 入口
COPY docker/supervisord.conf /etc/supervisord.conf
COPY docker/entrypoint.sh /entrypoint.sh
COPY docker/scheduler.py ./scripts/container_scheduler.py

RUN chmod +x /entrypoint.sh
# 数据目录（SQLite / .env / 日志 / 加密密钥 / 状态文件），由 compose 挂载
VOLUME ["/data"]

# gunicorn 监听端口（仅回环，由共享网络栈的 nginx 反代）
EXPOSE 17892

CMD ["/entrypoint.sh"]