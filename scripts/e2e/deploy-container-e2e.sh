#!/usr/bin/env bash
# ============================================================
# scripts/e2e/deploy-container-e2e.sh — 容器部署路径的干净环境 e2e（工单 6jk）。
#
# 模拟的真实用户动作（AGENTS §8g）：
#   一台新主机按 deploy/docker/docker-compose.yml 头部的命令，
#   从零 build 镜像、起 db+app、访问首页与 /healthz、再停机。
# 断言用户可见结果：
#   C1 前置成立，且本项目无旧容器/卷/网络、端口空闲（干净环境）
#   C2 docker compose build 退出 0（Dockerfile 真构建，含前端构建段）
#   C3 db 与 app 均 healthy
#   C4 迁移到嵌入集的最新版本（版本号从 server/migrations 现取）
#   C5 宿主 curl /healthz 返回 200 且 body 为 {"status":"ok"}
#   C5b 宿主 curl / 与前端深链 /accounts 返回首页；未知 /api 仍是 JSON 404；
#       哈希资产带 immutable 长缓存
#   C5c 带 X-Forwarded-Proto=https 的登录请求回 401 而非 403
#       （compose 默认的可信代理覆盖网桥网关）
#   C6 干净库无旧数据（users=0）
#   C7 compose stop 发 SIGTERM：退出码 0、耗时 < stop_grace_period、
#      日志含 worker: in-flight drained
#   C8 down -v 后本项目的容器/卷/网络清零
# 失败口径：退出码 1，并打印 failed step: <ID> <标题>；每步日志在 --log-dir。
# 退出码 2 = 用法或环境错误（缺 docker 等）。
#
# 用法（仓库根目录，WSL/Linux）：
#   bash scripts/e2e/deploy-container-e2e.sh
#   bash scripts/e2e/deploy-container-e2e.sh --keep --timeout 420
#   bash scripts/e2e/deploy-container-e2e.sh --log-dir /root/dvlogs/6jk-container
#
# 环境变量：
#   DEPLOY_E2E_PROJECT     compose 项目名（默认 yiban-e2e-container）
#   DEPLOY_E2E_HTTP_PORT   宿主回环端口（默认 18081；只绑 127.0.0.1）
#   DEPLOY_E2E_WAIT_SEC    app 健康等待上限秒数（默认 300）
#   DEPLOY_E2E_GOPROXY     可选，作为 GOPROXY 构建参数传给 Dockerfile
#                         （受限网络用，如 https://goproxy.cn,direct）
#   DEPLOY_E2E_NPM_REGISTRY 可选，作为 NPM_CONFIG_REGISTRY 构建参数
#                         （受限网络用，如 https://registry.npmmirror.com）
#   DEPLOY_E2E_IMAGE       镜像 tag（默认 <project>-server:e2e，不覆盖共享的
#                         yiban-server:local）
#   DEPLOY_E2E_SKIP_BUILD=1 复用已有镜像，只用于排障复跑；不作发布判据
#
# 发布门状态：自动轨。.github/workflows/deploy-gate.yml 的
#   container-deploy-e2e 作业调用本脚本（超时门 30 分钟）。
#
# 隔离：独立 compose 项目名 + 独立卷/网络 + 独立回环端口。
#   不碰本机既有容器（如 test47 上的 yiban-go-test）。
# 依赖：docker + compose v2、bash、curl、openssl（缺则 /dev/urandom 兜底）。
# ============================================================
set -Eeuo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SELF_DIR/../.." && pwd)"
# shellcheck source=lib/deploy-e2e.sh
. "$SELF_DIR/lib/deploy-e2e.sh"

PROJECT="${DEPLOY_E2E_PROJECT:-yiban-e2e-container}"
HTTP_PORT="${DEPLOY_E2E_HTTP_PORT:-18081}"
WAIT_SEC="${DEPLOY_E2E_WAIT_SEC:-300}"
GOPROXY_ARG="${DEPLOY_E2E_GOPROXY:-}"
NPM_REGISTRY_ARG="${DEPLOY_E2E_NPM_REGISTRY:-}"
IMAGE="${DEPLOY_E2E_IMAGE:-${PROJECT}-server:e2e}"
LOG_DIR="${DEPLOY_E2E_LOG_DIR:-$REPO/work/deploy-e2e-container}"
KEEP=0
COMPOSE_FILE="$REPO/deploy/docker/docker-compose.yml"
OVERRIDE=""
E2E_STARTED=0

while [ $# -gt 0 ]; do
    case "$1" in
        --keep) KEEP=1; shift ;;
        --timeout) WAIT_SEC="${2:?--timeout 需要秒数}"; shift 2 ;;
        --timeout=*) WAIT_SEC="${1#*=}"; shift ;;
        --log-dir) LOG_DIR="${2:?--log-dir 需要目录}"; shift 2 ;;
        --log-dir=*) LOG_DIR="${1#*=}"; shift ;;
        -h|--help) sed -n '2,48p' "$0"; exit 0 ;;
        *) echo "未知参数：$1" >&2; exit 2 ;;
    esac
done

e2e_init "container" "$LOG_DIR"

# ---- compose 包装：固定项目名、隔离的 compose.env（不读仓库根 .env）、标准输入关闭 ----
# COMPOSE_ENV 由模板现场生成（0600，只在本机 LOG_DIR）：键面与运行值同生产，
# 但不把密钥写进仓库可提交路径。YIBAN_COMPOSE_ENV_FILE 让 compose 的
# env_file 指到它；--env-file 供 db 的 ${POSTGRES_PASSWORD} 插值。
dc() {
    docker compose --env-file "$COMPOSE_ENV" -p "$PROJECT" \
        -f "$COMPOSE_FILE" -f "$OVERRIDE" "$@"
}

cleanup() {
    local rc=$?
    # 先摘掉 ERR/EXIT，防清理命令的失败再次触发 trap（会造成重复汇总）。
    trap - ERR EXIT
    e2e_exit_note "$rc"
    if [ "$E2E_STARTED" -eq 1 ]; then
        if [ "$KEEP" -eq 1 ]; then
            e2e_say "(--keep) 保留容器与卷。手动清理："
            e2e_say "  docker compose --env-file $COMPOSE_ENV -p $PROJECT -f $COMPOSE_FILE -f $OVERRIDE down -v"
        else
            dc down -v --remove-orphans >>"$LOG_DIR/cleanup.log" 2>&1 || true
        fi
    fi
    exit "$rc"
}
trap cleanup EXIT

write_override() {
    {
        printf 'services:\n'
        printf '  app:\n'
        printf '    image: "%s"\n' "$IMAGE"
        printf '    build:\n'
        printf '      args:\n'
        printf '        GOPROXY: "%s"\n' "$GOPROXY_ARG"
        printf '        NPM_CONFIG_REGISTRY: "%s"\n' "$NPM_REGISTRY_ARG"
        printf '    ports: !override\n'
        printf '      - "127.0.0.1:%s:8080"\n' "$HTTP_PORT"
    } > "$OVERRIDE"
}

wait_health() {
    local svc="$1"
    local limit="$2"
    local end=$((SECONDS + limit))
    local cid st
    while [ "$SECONDS" -lt "$end" ]; do
        cid="$(dc ps -q "$svc" 2>/dev/null | head -n1 || true)"
        if [ -n "$cid" ]; then
            st="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
                "$cid" 2>/dev/null || true)"
            case "$st" in
                healthy) return 0 ;;
                unhealthy) return 1 ;;
            esac
        fi
        sleep 2
    done
    return 1
}

# compose_version_at_least <版本> <主> <次> <补>：比较语义化版本。
# `ports: !override` 需要 Compose ≥ 2.24.4。低于该版本时 C1 直接失败。
compose_version_at_least() {
    local v="$1" want_major="$2" want_minor="$3" want_patch="$4"
    local major minor patch
    v="${v#v}"
    major="${v%%.*}"
    v="${v#*.}"
    minor="${v%%.*}"
    v="${v#*.}"
    patch="${v%%[!0-9]*}"
    major="${major:-0}"
    minor="${minor:-0}"
    patch="${patch:-0}"
    case "$major$minor$patch" in
        *[!0-9]*) return 1 ;;
    esac
    if [ "$major" -gt "$want_major" ]; then return 0; fi
    if [ "$major" -lt "$want_major" ]; then return 1; fi
    if [ "$minor" -gt "$want_minor" ]; then return 0; fi
    if [ "$minor" -lt "$want_minor" ]; then return 1; fi
    if [ "$patch" -ge "$want_patch" ]; then return 0; fi
    return 1
}

# ---- C1 前置与干净环境 -----------------------------------------------------
e2e_step "C1" "前置与干净环境"
e2e_require docker
e2e_require curl
if ! docker info >"$E2E_STEP_LOG" 2>&1; then
    e2e_die "docker daemon 不可达（docker info 失败）"
fi
if ! docker compose version >>"$E2E_STEP_LOG" 2>&1; then
    e2e_die "docker compose v2 不可用"
fi
COMPOSE_VERSION="$(docker compose version --short 2>>"$E2E_STEP_LOG" | head -n1 || true)"
if [ -z "$COMPOSE_VERSION" ]; then
    COMPOSE_VERSION="$(docker compose version 2>>"$E2E_STEP_LOG" \
        | sed -n 's/.*[Vv]ersion v\{0,1\}\([0-9][0-9.]*\).*/\1/p' | head -n1 || true)"
fi
printf 'compose_version=%s\n' "$COMPOSE_VERSION" >>"$E2E_STEP_LOG"
if ! compose_version_at_least "$COMPOSE_VERSION" 2 24 4; then
    e2e_die "docker compose 版本 ${COMPOSE_VERSION:-unknown} 低于 2.24.4（ports: !override 需要）"
fi
if ! e2e_port_free "$HTTP_PORT"; then
    e2e_die "127.0.0.1:$HTTP_PORT 已被占用（换 DEPLOY_E2E_HTTP_PORT）"
fi
OVERRIDE="$LOG_DIR/compose-override.yml"
write_override

# 运行配置：模板 + 现场生成的三个秘密，落在 LOG_DIR（0600）。
# 必须在任何 dc 调用之前就位：compose 用 ${VAR:?} 插值与环境文件，
# 缺它连 dc down 都失败，残留清理会静默失效。
# POSTGRES_PASSWORD 是编排层变量（非 YIBAN_ 前缀）：它只给 db 与 DSN 派生用，
# 不会进 Go 名册，也不会让容器里的 doctor 误报未登记键。
COMPOSE_ENV="$LOG_DIR/compose.env"
POSTGRES_PASSWORD="$(e2e_secret_hex 16)"
YIBAN_HASH_PEPPER="$(e2e_secret_hex 32)"
YIBAN_CREDENTIAL_KEY="$(e2e_secret_b64 32)"
(
    umask 077
    grep -Ev '^(YIBAN_HASH_PEPPER|YIBAN_CREDENTIAL_KEY|POSTGRES_PASSWORD)=' \
        "$REPO/deploy/container/compose.env.example" > "$COMPOSE_ENV"
    printf 'YIBAN_HASH_PEPPER=%s\n' "$YIBAN_HASH_PEPPER" >> "$COMPOSE_ENV"
    printf 'YIBAN_CREDENTIAL_KEY=%s\n' "$YIBAN_CREDENTIAL_KEY" >> "$COMPOSE_ENV"
    printf 'POSTGRES_PASSWORD=%s\n' "$POSTGRES_PASSWORD" >> "$COMPOSE_ENV"
)
chmod 600 "$COMPOSE_ENV"
export YIBAN_COMPOSE_ENV_FILE="$COMPOSE_ENV"
if [ "$(stat -c '%a' "$COMPOSE_ENV" 2>/dev/null || echo unknown)" != "600" ]; then
    e2e_die "$COMPOSE_ENV 必须是 0600"
fi

# 上次失败可能留下本项目名的资源。先清，再断言零残留。
# 只动本项目名的资源；其他项目（含 test47 上的 yiban-go-test）一律不碰。
STALE=0
if [ -n "$(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT")" ]; then STALE=1; fi
if [ -n "$(docker volume ls -q --filter "label=com.docker.compose.project=$PROJECT")" ]; then STALE=1; fi
if [ -n "$(docker network ls -q --filter "label=com.docker.compose.project=$PROJECT")" ]; then STALE=1; fi
if [ "$STALE" -eq 1 ]; then
    e2e_say "  [WARN] 发现上次残留（project=$PROJECT），先清理"
    dc down -v --remove-orphans >>"$E2E_STEP_LOG" 2>&1 || true
    # compose down 只清它认识的容器（需要 service 标签）。
    # 带项目标签的游离件按标签兜底删除；否则 C1 的零残留断言自相矛盾。
    for cid in $(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT"); do
        docker rm -f "$cid" >>"$E2E_STEP_LOG" 2>&1 || true
    done
    for vid in $(docker volume ls -q --filter "label=com.docker.compose.project=$PROJECT"); do
        docker volume rm -f "$vid" >>"$E2E_STEP_LOG" 2>&1 || true
    done
    for nid in $(docker network ls -q --filter "label=com.docker.compose.project=$PROJECT"); do
        docker network rm "$nid" >>"$E2E_STEP_LOG" 2>&1 || true
    done
fi
LEFT_C="$(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT" | wc -l | tr -d ' ')"
LEFT_V="$(docker volume ls -q --filter "label=com.docker.compose.project=$PROJECT" | wc -l | tr -d ' ')"
LEFT_N="$(docker network ls -q --filter "label=com.docker.compose.project=$PROJECT" | wc -l | tr -d ' ')"
if [ "$LEFT_C" != "0" ] || [ "$LEFT_V" != "0" ] || [ "$LEFT_N" != "0" ]; then
    e2e_die "清理后仍有本项目残留（容器=$LEFT_C 卷=$LEFT_V 网络=$LEFT_N）"
fi
docker version --format '{{.Server.Version}}' >>"$E2E_STEP_LOG" 2>&1 || true
git -C "$REPO" rev-parse HEAD >>"$E2E_STEP_LOG" 2>&1 || true
e2e_ok "项目 $PROJECT，端口 $HTTP_PORT，compose $COMPOSE_VERSION，GOPROXY=${GOPROXY_ARG:-<default>}"

# ---- C1b compose config：显式 env-file 是硬前提 ----------------------------
# compose 文件里有 ${POSTGRES_PASSWORD:?} 插值：不带 --env-file 必须解析失败
# （fail-closed），带 --env-file 必须渲染出派生 DSN 与可信代理缺省。
# 这条断言把 deploy/README 的命令形状钉住：少写 --env-file 的文档会在这里红。
e2e_step "C1b" "compose config 显式 env-file"
if docker compose -p "$PROJECT" -f "$COMPOSE_FILE" config >"$LOG_DIR/config-no-envfile.yml" 2>&1; then
    e2e_die "不带 --env-file 的 compose config 竟然成功：口令插值必须显式给值（fail-closed）"
fi
if ! docker compose --env-file "$COMPOSE_ENV" -p "$PROJECT" \
        -f "$COMPOSE_FILE" -f "$OVERRIDE" config >"$LOG_DIR/compose-config.yml" 2>>"$E2E_STEP_LOG"; then
    e2e_die "带 --env-file 的 compose config 失败"
fi
grep -q 'YIBAN_DATABASE_URL: postgres://yiban:' "$LOG_DIR/compose-config.yml" \
    || e2e_die "渲染缺派生 DSN（POSTGRES_PASSWORD 未进插值）"
grep -q 'YIBAN_TRUSTED_PROXIES: 172.16.0.0/12,127.0.0.1' "$LOG_DIR/compose-config.yml" \
    || e2e_die "渲染缺可信代理缺省（透传行失效）"
grep -q 'YIBAN_ENV: production' "$LOG_DIR/compose-config.yml" \
    || e2e_die "渲染缺 env_file 注入的 YIBAN_ENV"
# export 覆盖必须赢过 --env-file：deploy/README §5 的方式一靠它。
if ! YIBAN_TRUSTED_PROXIES=192.0.2.10 docker compose --env-file "$COMPOSE_ENV" -p "$PROJECT" \
        -f "$COMPOSE_FILE" -f "$OVERRIDE" config >"$LOG_DIR/compose-config-override.yml" 2>>"$E2E_STEP_LOG"; then
    e2e_die "带覆盖变量的 compose config 失败"
fi
grep -q 'YIBAN_TRUSTED_PROXIES: 192.0.2.10' "$LOG_DIR/compose-config-override.yml" \
    || e2e_die "shell 覆盖未透传 YIBAN_TRUSTED_PROXIES"
e2e_ok "无 --env-file 拒绝解析；有 --env-file 渲染出派生 DSN/代理缺省/YIBAN_ENV；export 覆盖生效"

# ---- C2 构建镜像 -----------------------------------------------------------
e2e_step "C2" "构建镜像"
E2E_STARTED=1
if [ "${DEPLOY_E2E_SKIP_BUILD:-0}" = "1" ] && docker image inspect "$IMAGE" >/dev/null 2>&1; then
    e2e_ok "复用已有镜像 $IMAGE（DEPLOY_E2E_SKIP_BUILD=1，仅排障复跑）"
else
    if ! dc build >>"$E2E_STEP_LOG" 2>&1; then
        e2e_die "docker compose build 失败"
    fi
    e2e_ok "镜像 $IMAGE 构建完成"
fi

# ---- C3 起栈并等待健康 -----------------------------------------------------
e2e_step "C3" "起栈并等待健康"
if ! dc up -d --no-build >>"$E2E_STEP_LOG" 2>&1; then
    e2e_die "docker compose up -d 失败"
fi
if ! wait_health db 90; then
    dc ps >>"$E2E_STEP_LOG" 2>&1 || true
    dc logs --tail 60 db >>"$E2E_STEP_LOG" 2>&1 || true
    e2e_die "db 未在 90s 内 healthy"
fi
if ! wait_health app "$WAIT_SEC"; then
    dc ps >>"$E2E_STEP_LOG" 2>&1 || true
    dc logs --tail 80 app >>"$E2E_STEP_LOG" 2>&1 || true
    e2e_die "app 未在 ${WAIT_SEC}s 内 healthy"
fi
e2e_ok "db 与 app 均 healthy"

# ---- C4 迁移版本 -----------------------------------------------------------
e2e_step "C4" "迁移版本"
EXPECTED_SCHEMA="$(e2e_expected_schema_version "$REPO")"
if [ "$EXPECTED_SCHEMA" -le 0 ]; then
    e2e_die "server/migrations/postgres 里没有迁移文件"
fi
if ! dc exec -T app /usr/local/bin/yiban-cli migrate >"$E2E_STEP_LOG" 2>&1; then
    e2e_die "容器内 yiban-cli migrate 失败"
fi
if ! grep -q "schema version $EXPECTED_SCHEMA" "$E2E_STEP_LOG"; then
    e2e_die "迁移版本不是 $EXPECTED_SCHEMA（输出：$(tr '\n' ' ' < "$E2E_STEP_LOG")）"
fi
e2e_ok "schema version $EXPECTED_SCHEMA"

# ---- C4b 签名发布物（签发 → 验签 → 错公钥必拒 → 篡改必红） -----------------
e2e_step "C4b" "签名发布物"
SIGN_DIR="$E2E_LOG_DIR/c4b.signing"
mkdir -p "$SIGN_DIR"
printf 'release-body-1
' > "$SIGN_DIR/fixture.bin"
bash "$REPO/scripts/release/make-manifest.sh" "e2e-C4b" "$SIGN_DIR/fixture.bin" > "$SIGN_DIR/manifest.json"
openssl genpkey -algorithm ED25519 -out "$SIGN_DIR/key.pem" >>"$E2E_STEP_LOG" 2>&1     || e2e_die "openssl 生成 Ed25519 私钥失败"
openssl pkey -in "$SIGN_DIR/key.pem" -pubout -out "$SIGN_DIR/pub.pem" >>"$E2E_STEP_LOG" 2>&1     || e2e_die "openssl 导出公钥失败"
bash "$REPO/scripts/release/sign.sh" --key "$SIGN_DIR/key.pem"     --in "$SIGN_DIR/manifest.json" --out "$SIGN_DIR/manifest.sig" >>"$E2E_STEP_LOG" 2>&1     || e2e_die "sign.sh 签名失败"
if ! bash "$REPO/scripts/release/verify.sh" --pubkey "$SIGN_DIR/pub.pem" \
    --manifest "$SIGN_DIR/manifest.json" --signature "$SIGN_DIR/manifest.sig" \
    >>"$E2E_STEP_LOG" 2>&1; then
    e2e_die "正路径验签失败"
fi
# 错公钥必须被拒（2026-10-29 审查 P2）：另起一把钥，同一清单/签名必须红。
openssl genpkey -algorithm ED25519 -out "$SIGN_DIR/other-key.pem" >>"$E2E_STEP_LOG" 2>&1 \
    || e2e_die "openssl 生成第二把 Ed25519 私钥失败"
openssl pkey -in "$SIGN_DIR/other-key.pem" -pubout -out "$SIGN_DIR/other-pub.pem" \
    >>"$E2E_STEP_LOG" 2>&1 || e2e_die "openssl 导出第二把公钥失败"
if bash "$REPO/scripts/release/verify.sh" --pubkey "$SIGN_DIR/other-pub.pem" \
    --manifest "$SIGN_DIR/manifest.json" --signature "$SIGN_DIR/manifest.sig" \
    >>"$E2E_STEP_LOG" 2>&1; then
    e2e_die "错公钥验签竟然通过（门无牙）"
fi
printf ' ' >> "$SIGN_DIR/manifest.json"
if bash "$REPO/scripts/release/verify.sh" --pubkey "$SIGN_DIR/pub.pem" \
    --manifest "$SIGN_DIR/manifest.json" --signature "$SIGN_DIR/manifest.sig" \
    >>"$E2E_STEP_LOG" 2>&1; then
    e2e_die "篡改清单后验签竟然通过（门无牙）"
fi
e2e_ok "签发/验签通过；错公钥被拒；篡改清单被拒"

# ---- C5 /healthz -----------------------------------------------------------
e2e_step "C5" "宿主 /healthz"
BODY="$E2E_STEP_LOG.body"
CODE="$(curl -sS -o "$BODY" -w '%{http_code}' --max-time 5 \
    "http://127.0.0.1:$HTTP_PORT/healthz" 2>>"$E2E_STEP_LOG" || true)"
if [ "$CODE" != "200" ]; then
    e2e_die "/healthz HTTP $CODE（期望 200）"
fi
if ! grep -q '"status":"ok"' "$BODY"; then
    e2e_die "/healthz 响应体不是 status=ok：$(tr -d '\n' < "$BODY")"
fi
e2e_ok "HTTP 200，body=$(tr -d '\n' < "$BODY")"

# ---- C5b 前端页面（CLEAN-0） ----------------------------------------------
# 断言用户可见结果：首页能打开、深链回首页、缺失 API 仍是 JSON 404、
# 哈希资产长缓存。四条都不依赖数据库与账号。
e2e_step "C5b" "首页与前端深链"
UI_HTML="$E2E_STEP_LOG.ui.html"
CODE="$(curl -sS -o "$UI_HTML" -w '%{http_code}' --max-time 5 \
    "http://127.0.0.1:$HTTP_PORT/" 2>>"$E2E_STEP_LOG" || true)"
if [ "$CODE" != "200" ]; then
    e2e_die "GET / HTTP $CODE（期望 200）"
fi
if ! grep -q '<div id="root">' "$UI_HTML"; then
    e2e_die "GET / 不是前端首页：$(head -c 200 "$UI_HTML")"
fi

CODE="$(curl -sS -o "$UI_HTML" -w '%{http_code}' --max-time 5 \
    "http://127.0.0.1:$HTTP_PORT/accounts" 2>>"$E2E_STEP_LOG" || true)"
if [ "$CODE" != "200" ]; then
    e2e_die "GET /accounts（前端深链）HTTP $CODE（期望 200）"
fi
if ! grep -q '<div id="root">' "$UI_HTML"; then
    e2e_die "GET /accounts 未回 index.html：$(head -c 200 "$UI_HTML")"
fi

API_404="$E2E_STEP_LOG.api404.json"
CODE="$(curl -sS -o "$API_404" -w '%{http_code}' --max-time 5 \
    "http://127.0.0.1:$HTTP_PORT/api/definitely-not-a-route" 2>>"$E2E_STEP_LOG" || true)"
if [ "$CODE" != "404" ]; then
    e2e_die "GET /api/definitely-not-a-route HTTP $CODE（期望 404）"
fi
if ! grep -q '"code":"not_found"' "$API_404"; then
    e2e_die "未知 API 未回 JSON 信封：$(head -c 200 "$API_404")"
fi
if grep -q '<div id="root">' "$API_404"; then
    e2e_die "未知 API 落到了页面路由（前端吞掉了 API 404）"
fi

ASSET="$(grep -o '/assets/[^"]*\.js' "$UI_HTML" | head -n1 || true)"
if [ -z "$ASSET" ]; then
    e2e_die "index.html 里找不到 /assets/*.js"
fi
ASSET_HEADERS="$E2E_STEP_LOG.asset.headers"
CODE="$(curl -sS -o /dev/null -D "$ASSET_HEADERS" -w '%{http_code}' --max-time 5 \
    "http://127.0.0.1:$HTTP_PORT$ASSET" 2>>"$E2E_STEP_LOG" || true)"
if [ "$CODE" != "200" ]; then
    e2e_die "GET $ASSET HTTP $CODE（期望 200）"
fi
if ! grep -qi '^cache-control: public, max-age=31536000, immutable' "$ASSET_HEADERS"; then
    e2e_die "$ASSET 缺 immutable 长缓存：$(grep -i '^cache-control:' "$ASSET_HEADERS" || echo none)"
fi
e2e_ok "首页 200；/accounts 回首页；未知 API 为 JSON 404；资产长缓存"

# ---- C5c 外部 TLS 终结的信任链 ---------------------------------------------
# 模拟真实用户动作：宿主反代终结 TLS，带 X-Forwarded-Proto=https 转给容器。
# 判据：登录来源判定必须看到 https（回 401），不得回 403。
# 403 表示 compose 默认 YIBAN_TRUSTED_PROXIES 没覆盖网桥网关。
e2e_step "C5c" "TLS 反代信任链"
LOGIN_CODE="$(curl -sS -o "$E2E_STEP_LOG.login.json" -w '%{http_code}' --max-time 5 \
    -X POST "http://127.0.0.1:$HTTP_PORT/api/auth/login" \
    -H 'Content-Type: application/json' \
    -H "Origin: https://127.0.0.1:$HTTP_PORT" \
    -H 'X-Forwarded-Proto: https' \
    -d '{"email":"nobody@example.com","password":"wrong-password"}' \
    2>>"$E2E_STEP_LOG" || true)"
if [ "$LOGIN_CODE" = "403" ]; then
    e2e_die "登录回 403：compose 默认 YIBAN_TRUSTED_PROXIES 未覆盖网桥网关"
fi
if [ "$LOGIN_CODE" != "401" ]; then
    e2e_die "登录 HTTP $LOGIN_CODE（期望 401 无效凭据；403 表示信任链断了）"
fi
e2e_ok "X-Forwarded-Proto=https 被采信，登录回 401 而非 403"

# ---- C6 干净库（无旧数据） -------------------------------------------------
e2e_step "C6" "干净库无旧数据"
if ! USERS="$(dc exec -T db psql -U yiban -d yiban -tAc 'select count(*) from users' 2>>"$E2E_STEP_LOG" | tr -d '[:space:]')"; then
    e2e_die "users 计数查询失败（psql）"
fi
if [ "$USERS" != "0" ]; then
    e2e_die "干净库 users=$USERS（期望 0）"
fi
if ! DB_SCHEMA="$(dc exec -T db psql -U yiban -d yiban -tAc \
    'select coalesce(max(version_id),0) from goose_db_version where is_applied' \
    2>>"$E2E_STEP_LOG" | tr -d '[:space:]')"; then
    e2e_die "goose 版本查询失败（psql）"
fi
if [ "$DB_SCHEMA" != "$EXPECTED_SCHEMA" ]; then
    e2e_die "goose 最高已应用版本=$DB_SCHEMA（期望 $EXPECTED_SCHEMA）"
fi
e2e_ok "users=0，goose 最高已应用版本=$DB_SCHEMA"

# ---- C7 SIGTERM 优雅停机 ---------------------------------------------------
e2e_step "C7" "SIGTERM 优雅停机"
APP_CID="$(dc ps -aq app | head -n1 || true)"
if [ -z "$APP_CID" ]; then
    e2e_die "找不到 app 容器"
fi
START_MS="$(date +%s%3N)"
if ! dc stop app >>"$E2E_STEP_LOG" 2>&1; then
    e2e_die "docker compose stop app 失败"
fi
END_MS="$(date +%s%3N)"
ELAPSED_MS=$((END_MS - START_MS))
EXIT_CODE="$(docker inspect -f '{{.State.ExitCode}}' "$APP_CID" 2>/dev/null || echo missing)"
printf 'elapsed_ms=%s\nexit_code=%s\n' "$ELAPSED_MS" "$EXIT_CODE" >>"$E2E_STEP_LOG"
if [ "$EXIT_CODE" != "0" ]; then
    e2e_die "SIGTERM 后退出码 $EXIT_CODE（期望 0；137 表示被 SIGKILL）"
fi
if [ "$ELAPSED_MS" -ge 30000 ]; then
    e2e_die "停机耗时 ${ELAPSED_MS}ms，达到 stop_grace_period 30s 的上限"
fi
dc logs app >>"$E2E_STEP_LOG" 2>&1 || true
if ! grep -q 'worker: in-flight drained' "$E2E_STEP_LOG"; then
    e2e_die "日志缺 worker: in-flight drained（有序收尾未发生）"
fi
e2e_ok "退出码 0，耗时 ${ELAPSED_MS}ms，日志含 worker: in-flight drained"

# ---- C8 资源清零 -----------------------------------------------------------
e2e_step "C8" "资源清零"
if [ "$KEEP" -eq 1 ]; then
    e2e_ok "(--keep) 保留容器与卷"
else
    if ! dc down -v --remove-orphans >>"$E2E_STEP_LOG" 2>&1; then
        e2e_die "compose down -v 失败"
    fi
    LEFT_C="$(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT" | wc -l | tr -d ' ')"
    LEFT_V="$(docker volume ls -q --filter "label=com.docker.compose.project=$PROJECT" | wc -l | tr -d ' ')"
    LEFT_N="$(docker network ls -q --filter "label=com.docker.compose.project=$PROJECT" | wc -l | tr -d ' ')"
    if [ "$LEFT_C" != "0" ] || [ "$LEFT_V" != "0" ] || [ "$LEFT_N" != "0" ]; then
        e2e_die "清零后残留：容器=$LEFT_C 卷=$LEFT_V 网络=$LEFT_N"
    fi
    e2e_ok "容器/卷/网络均为 0"
fi

e2e_say ""
e2e_say "日志目录：$LOG_DIR"
if e2e_summary; then
    exit 0
fi
exit 1
