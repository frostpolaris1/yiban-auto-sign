# deploy/docker/Dockerfile — Go 服务端＋React 前端的生产镜像。
#
# 形状：三阶段构建。前端段用 node:22 构建 dist；Go 段用
# golang:1.27-alpine 全量工具链；运行段用 distroless/static
# （非 root、无 shell、无包管理器），攻击面只有两个静态二进制。
# 页面由 Go 进程直接托管（YIBAN_UI_DIR），不再引入第二层代理：
# 生产只需一层反向代理终结 TLS，X-Forwarded-* 只有一跳。
# CGO_ENABLED=0 保证纯静态，迁移与 tzdata 都嵌在二进制里
# （internal/assembly/tzdata.go），不依赖镜像内的 zoneinfo。
#
# 构建与运行（在仓库根）：
#   docker build -f deploy/docker/Dockerfile -t yiban-server .
#   docker compose -f deploy/docker/docker-compose.yml up -d
#
# 旧 Python 镜像（docker/）与本镜像并存；Go 线发布时旧栈退役。

# ---- 前端构建段 ------------------------------------------------------------
FROM node:22-alpine AS ui

# npm registry 覆盖点：受限网络用 --build-arg NPM_CONFIG_REGISTRY=...
# 缺省不注入，沿用 npm 官方源。
ARG NPM_CONFIG_REGISTRY

WORKDIR /src/ui

# 先只拷清单：依赖不变时，源码改动不触发 npm ci。
# api-codegen 是 postinstall 的独立 npm 项目，清单必须同批就位。
COPY ui/package.json ui/package-lock.json ./
COPY ui/scripts/api-codegen/package.json ui/scripts/api-codegen/package-lock.json ./scripts/api-codegen/
RUN if [ -n "${NPM_CONFIG_REGISTRY:-}" ]; then npm config set registry "$NPM_CONFIG_REGISTRY"; fi \
 && npm ci --no-audit --no-fund

COPY ui/ ./
RUN npm run build

# ---- 构建段 ----------------------------------------------------------------
FROM golang:1.27-alpine AS build

# GOPROXY 覆盖点：受限网络或私有模块代理时用
#   --build-arg GOPROXY=https://your-proxy,direct
# 缺省不注入，沿用工具链默认（本仓不为任何镜像源背书）。
ARG GOPROXY

# 版本身份：注入 `yiban-server --version` 与 `yiban-cli version`。
# 缺省 dev/unknown 是显性占位符（不编造 sha 或时间）；发布构建必须传：
#   --build-arg YIBAN_VERSION=0.7.0 \
#   --build-arg YIBAN_COMMIT=$(git rev-parse --short HEAD) \
#   --build-arg YIBAN_BUILD_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)
ARG YIBAN_VERSION=dev
ARG YIBAN_COMMIT=unknown
ARG YIBAN_BUILD_TIME=unknown

WORKDIR /src/server

# 先只拷模块清单：依赖不变时，源码改动不触发 go mod download。
COPY server/go.mod server/go.sum ./
RUN if [ -n "${GOPROXY:-}" ]; then go env -w GOPROXY="${GOPROXY}"; fi \
 && go mod download

COPY server/ ./
RUN CGO_ENABLED=0 go build -trimpath \
      -ldflags="-s -w \
        -X github.com/frostpolaris1/yiban-auto-sign/server/internal/buildinfo.version=${YIBAN_VERSION} \
        -X github.com/frostpolaris1/yiban-auto-sign/server/internal/buildinfo.commit=${YIBAN_COMMIT} \
        -X github.com/frostpolaris1/yiban-auto-sign/server/internal/buildinfo.buildTime=${YIBAN_BUILD_TIME}" \
      -o /out/yiban-server ./cmd/server \
 && CGO_ENABLED=0 go build -trimpath \
      -ldflags="-s -w \
        -X github.com/frostpolaris1/yiban-auto-sign/server/internal/buildinfo.version=${YIBAN_VERSION} \
        -X github.com/frostpolaris1/yiban-auto-sign/server/internal/buildinfo.commit=${YIBAN_COMMIT} \
        -X github.com/frostpolaris1/yiban-auto-sign/server/internal/buildinfo.buildTime=${YIBAN_BUILD_TIME}" \
      -o /out/yiban-cli ./cmd/cli \
 && mkdir -p /out/photos

# ---- 运行段 ----------------------------------------------------------------
FROM gcr.io/distroless/static-debian12:nonroot

COPY --from=build /out/yiban-server /usr/local/bin/yiban-server
COPY --from=build /out/yiban-cli /usr/local/bin/yiban-cli

# 照片目录必须在镜像里预建并归 nonroot(65532)：
# 命名卷首次挂载继承镜像内目录的属主；不预建时 Docker 以 root
# 建空目录，distroless nonroot 写入即 permission denied。
COPY --chown=65532:65532 --from=build /out/photos /data/photos

# 前端构建产物只读：Go 进程直接托管页面与深链。
# 目录与文件是 root 属主、世界可读；distroless nonroot 只需要读。
COPY --from=ui /src/ui/dist /srv/ui

# 容器内必须监听全部接口；照片目录独立卷，便于备份与迁移。
ENV YIBAN_HTTP_ADDR=0.0.0.0:8080 \
    YIBAN_PHOTO_DIR=/data/photos \
    YIBAN_UI_DIR=/srv/ui

VOLUME /data/photos
EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/yiban-server"]
