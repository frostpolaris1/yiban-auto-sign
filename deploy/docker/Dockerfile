# deploy/docker/Dockerfile — Go 服务端的生产镜像。
#
# 形状：多阶段构建。构建段用 golang:1.27-alpine 全量工具链；
# 运行段用 distroless/static（非 root、无 shell、无包管理器），
# 攻击面只有两个静态二进制。CGO_ENABLED=0 保证纯静态，
# 迁移与 tzdata 都嵌在二进制里（internal/assembly/tzdata.go），
# 不依赖镜像内的 zoneinfo。
#
# 构建与运行（在仓库根）：
#   docker build -f deploy/docker/Dockerfile -t yiban-server .
#   docker compose -f deploy/docker/docker-compose.yml up -d
#
# 旧 Python 镜像（docker/）与本镜像并存；Go 线发布时旧栈退役。

# ---- 构建段 ----------------------------------------------------------------
FROM golang:1.27-alpine AS build

# GOPROXY 覆盖点：受限网络或私有模块代理时用
#   --build-arg GOPROXY=https://your-proxy,direct
# 缺省不注入，沿用工具链默认（本仓不为任何镜像源背书）。
ARG GOPROXY

WORKDIR /src/server

# 先只拷模块清单：依赖不变时，源码改动不触发 go mod download。
COPY server/go.mod server/go.sum ./
RUN if [ -n "${GOPROXY:-}" ]; then go env -w GOPROXY="${GOPROXY}"; fi \
 && go mod download

COPY server/ ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/yiban-server ./cmd/server \
 && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/yiban-cli ./cmd/cli

# ---- 运行段 ----------------------------------------------------------------
FROM gcr.io/distroless/static-debian12:nonroot

COPY --from=build /out/yiban-server /usr/local/bin/yiban-server
COPY --from=build /out/yiban-cli /usr/local/bin/yiban-cli

# 容器内必须监听全部接口；照片目录独立卷，便于备份与迁移。
ENV YIBAN_HTTP_ADDR=0.0.0.0:8080 \
    YIBAN_PHOTO_DIR=/data/photos

VOLUME /data/photos
EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/yiban-server"]
