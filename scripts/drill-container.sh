#!/usr/bin/env bash
# ============================================================
# drill-container.sh —— 47 号测试机「容器形态 e2e 演练」一键脚本
# ============================================================
# 用途：
#   把 docs/dev/production-isolation-rehearsal-plan-20260925.md §5 的容器形态
#   操作单（C0~C6）从 1.5 小时手工操作压成一条命令：造数 → 构建 → 起内部网
#   容器 → 起 mock 上游 → 整轮真签到 → 逐条自动断言 → 清理，全程证据落 evidence/。
#   验证的链路是**容器形态的真实签到路径**：
#     supervisord → scripts/container_scheduler.py → 子进程 scripts/signin.py
#   （镜像里没有 run.sh —— 根目录文件不进镜像，run.sh 是宿主形态的入口）
#
# 用法（在演练机上、于演练树根目录执行；不做跨 ssh 编排，同步由人做）：
#   cd /opt/yiban-test/yiban-051
#   ./scripts/drill-container.sh                       # 默认：3 账号、窗口 +1~+10 分钟
#   ./scripts/drill-container.sh --window-lead-sec 240 --window-span-sec 540
#                                                    # 复刻 2026-10-02 那一轮的窗口形状
#   ./scripts/drill-container.sh --skip-build          # 镜像已在，复跑不再构建
#   ./scripts/drill-container.sh --accounts 5
#
# 参数：
#   --tag NAME            镜像 tag（默认 yiban-rehearsal:<yiban 包版本号>）
#   --accounts N          造数账号数（默认 3）
#   --window-lead-sec S   签到窗口起点相对"写 .env 时刻"的秒数（默认 60）
#   --window-span-sec S   签到窗口长度秒数（默认 540 = 9 分钟，须 ≤10 分钟且不跨日）
#   --timeout-sec S       等整轮签到汇总的超时（默认 900）
#   --skip-build          跳过 docker build（镜像已存在时复跑用）
#   --host-python PATH    造数用的宿主解释器（默认 /opt/yiban-test/.venv/bin/python3）
#   -h/--help             本帮助
#
# 前提：
#   1. 在**测试机**的演练树根目录执行。脚本断言 docker/Dockerfile 与
#      yiban/__init__.py 在位，并断言树根**没有** .env / yiban.db —— 演练树必须
#      来自 `git archive HEAD | ssh <host> 'tar -x -C <tree>'`，严禁携带生产凭据。
#   2. 本机时钟必须已过 06:31（见下方"为什么窗口起点只留 60s 余量"）。
#   3. 宿主可用 docker；证书由 openssl 直产（CA 一张 + 服务器证书，SAN 含五域名）。
#   4. 宿主 python 需能 import db（脚本走仓库内 scripts/db.py 兼容壳，复用宿主 venv）。
#   5. mock = tests/fake_yiban_server.py。镜像按 M84 不含 tests/，用 docker cp 注入
#      后 docker exec -d 拉起（scripts/loadtest/ 工具族已裁撤，此为现状适配）。
#
# 为什么窗口起点只留 60s 余量（2026-10-02 实测修正了操作单的 +4min）：
#   容器调度器的首签闩锁是**无上界**判定（`hm >= FIRST(06:31)`），且命中即
#   `_run_signin_child()` **同步阻塞**整个主循环。所以子进程在容器起来约 10s 内
#   就被拉起，并在 spawn 那一刻经 build_child_env 把 .env **快照**进子进程环境
#   （运行期只有 --fallback 兜底每轮重读 .env，定时轮不重读）。
#   ⇒ "起容器后等 healthcheck 稳定再写窗口" 对定时轮**无效**（窗口已快照），
#     必须**在 docker run 之前**把窗口写进 .env；余量只需覆盖
#     "容器启动 → sched spawn → 子进程读 .env"（实测 ~10s），不必等 4 分钟。
#   窗口开闸后由引擎自己等到点再发请求（2026-10-02 实测：子进程 18:05:52 起跑、
#   窗口 18:09 开闸、18:10:02 首次出网），故压缩余量不损失任何一环的真实性。
#
# 红线（结构保证，不靠自觉）：
#   * 演练网络 docker network create --internal —— 容器**结构性**出不了公网
#     （脚本另有一次"探 egress 必须失败"的实证，见 c5-egress.log）；
#   * 五域名 --add-host ...:127.0.0.1，mock 监听容器内 443 回环；
#   * 不发布任何宿主端口（-p 一个都没有），全部检查走 docker exec；
#   * 全部凭据自造（@mock.invalid 账号、密钥运行期自动生成），不读不写生产凭据；
#   * 结束自动删容器与网络（镜像保留供复跑）并对账无残留；
#   * 演练轮**不是**有效轮次，证据只进 evidence/。
#
# 退出码：
#   0  演练轮 PASS（全部判据通过，已清理）
#   1  某一步判据失败（stderr 打印失败步与原因；证据在 evidence/drill-<时间戳>/）
#   2  前提不满足（缺 docker、树不对、时钟未过 06:31、凭据文件意外在场等）
# ============================================================
set -Eeuo pipefail

ROOT="$(pwd -P)"
SELF="scripts/drill-container.sh"

# ---- 参数默认值 ------------------------------------------------------------
TAG=""
ACCOUNTS=3
LEAD_SEC=60
SPAN_SEC=540
TIMEOUT_SEC=900
SKIP_BUILD=0
HOST_PYTHON="${DRILL_HOST_PYTHON:-/opt/yiban-test/.venv/bin/python3}"

CTR="yiban-rehearsal"
NET="yiban-rehearsal-net"
# 五域名与 mock_env.py:DEFAULT_DOMAINS 同源（mock 监听容器内 443 回环）
DOMAINS=(oauth.yiban.cn f.yiban.cn api.uyiban.com c.uyiban.com app.uyiban.com)
# 阿里内网源 http 被 pip 拒（非信任 http）、同域 https 证书不匹配，
# 清华源可用；不带 build-arg 时默认 PyPI 在该内网不可达。
PIP_INDEX_URL_DEFAULT="https://pypi.tuna.tsinghua.edu.cn/simple"
# 容器调度器首签闩锁的触发时刻（docker/scheduler.py:FIRST）
FIRST_LATCH="06:31"

STEP="init"
CLEANED=0
EV=""

usage() { sed -n '2,62p' "$SELF"; }

die()  { printf '\n[FAIL] 步骤「%s」失败：%s\n' "$STEP" "$*" >&2; exit 1; }
pre()  { printf '\n[FAIL] 前提不满足：%s\n' "$*" >&2; exit 2; }
step() { STEP="$1"; printf '\n──── [%s] %s ────\n' "$(date +%H:%M:%S)" "$1"; }
note() { printf '       · %s\n' "$*"; }

cleanup() {
  rc=$?
  if [ "$CLEANED" -eq 0 ]; then
    CLEANED=1
    docker rm -f "$CTR" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
    [ -n "$EV" ] && printf '[cleanup] 容器与网络已清理；证据：%s\n' "$EV" >&2
  fi
  exit "$rc"
}
trap cleanup EXIT
trap 'printf "\n[FAIL] 步骤「%s」失败（行 %s）\n" "$STEP" "$LINENO" >&2; exit 1' ERR

while [ $# -gt 0 ]; do
  case "$1" in
    --tag)              TAG="$2"; shift 2 ;;
    --accounts)         ACCOUNTS="$2"; shift 2 ;;
    --window-lead-sec)  LEAD_SEC="$2"; shift 2 ;;
    --window-span-sec)  SPAN_SEC="$2"; shift 2 ;;
    --timeout-sec)      TIMEOUT_SEC="$2"; shift 2 ;;
    --skip-build)       SKIP_BUILD=1; shift ;;
    --host-python)      HOST_PYTHON="$2"; shift 2 ;;
    -h|--help)          usage; exit 0 ;;
    *)                  pre "未知参数：$1（-h 看用法）" ;;
  esac
done

# ---- P0 前提 ---------------------------------------------------------------
step "P0 前提检查"
[ -f "$ROOT/docker/Dockerfile" ]  || pre "不在演练树根目录（缺 docker/Dockerfile）"
[ -f "$ROOT/yiban/__init__.py" ] || pre "缺 yiban/__init__.py"
[ -f "$ROOT/docker/scheduler.py" ] || pre "缺 docker/scheduler.py"
[ -f "$ROOT/tests/fake_yiban_server.py" ] || pre "缺 tests/fake_yiban_server.py"
command -v docker >/dev/null || pre "docker 不可用"
[ -x "$HOST_PYTHON" ] || pre "宿主解释器不可用：$HOST_PYTHON"
command -v openssl >/dev/null || pre "openssl 不可用"
# 红线：演练树里绝不允许出现生产凭据
for leak in .env yiban.db accounts.json; do
  if [ -e "$ROOT/$leak" ]; then
    pre "演练树根出现 $leak —— 疑似携带生产凭据，请用 git archive 重新同步"
  fi
done
# 调度器首签闩锁是无上界判定，本机时钟未过 06:31 则整轮永不触发
NOW_HM="$(date +%H:%M)"
if [ "$NOW_HM" \< "$FIRST_LATCH" ]; then
  pre "本机时钟 $NOW_HM 未过容器调度器首签闩锁 $FIRST_LATCH，本轮不会自动触发；请稍后再跑"
fi
if [ "$LEAD_SEC" -lt 45 ]; then
  pre "--window-lead-sec 至少 45s：需留出「起容器 → sched spawn → 子进程读 .env」的实测 ~10s"
fi
if [ "$SPAN_SEC" -gt 600 ]; then
  pre "--window-span-sec 不得超过 600s（预案 §5 E4：窗口 ≤10 分钟）"
fi
if [ -z "$TAG" ]; then
  VERSION="$("$HOST_PYTHON" -c "import sys;sys.path.insert(0,'.');from yiban import __version__;print(__version__)" \
             2>/dev/null || sed -n 's/^__version__ = "\(.*\)"/\1/p' yiban/__init__.py | head -1)"
  [ -n "$VERSION" ] || pre "读不到版本号"
  TAG="yiban-rehearsal:$VERSION"
fi
IMAGE="$TAG"
EV="evidence/drill-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$EV/baseline"
printf 'tree=%s\nimage=%s\naccounts=%s\nwindow_lead_sec=%s\nwindow_span_sec=%s\nstarted=%s\n' \
  "$ROOT" "$IMAGE" "$ACCOUNTS" "$LEAD_SEC" "$SPAN_SEC" "$(date -Is)" > "$EV/run.meta"
note "演练树 $ROOT"
note "镜像 $IMAGE"
note "证据目录 $EV"

# ---- C0 基线快照 -----------------------------------------------------------
step "C0 基线快照（宿主端口 / hosts / docker 现状）"
ss -ltnp          > "$EV/baseline/ss-ltnp.txt"  2>&1 || true
md5sum /etc/hosts > "$EV/baseline/hosts-md5.txt" 2>&1 || true
docker ps -a      > "$EV/baseline/docker-ps.txt" 2>&1 || true
docker network ls > "$EV/baseline/docker-net.txt" 2>&1 || true
note "宿主监听 $(wc -l < "$EV/baseline/ss-ltnp.txt") 条；hosts md5 $(cut -d' ' -f1 < "$EV/baseline/hosts-md5.txt")"

# ---- C1 构建 ---------------------------------------------------------------
step "C1 镜像构建（容器形态部署路径之一）"
if [ "$SKIP_BUILD" -eq 1 ]; then
  docker image inspect "$IMAGE" >/dev/null 2>&1 || pre "--skip-build 但镜像不存在：$IMAGE"
  note "--skip-build：复用已有镜像 $IMAGE"
else
  docker build -t "$IMAGE" -f docker/Dockerfile \
    --build-arg PIP_INDEX_URL="$PIP_INDEX_URL_DEFAULT" . > "$EV/c1-build.log" 2>&1 \
    || { tail -30 "$EV/c1-build.log" >&2; die "docker build 失败（完整日志 c1-build.log）"; }
fi
docker run --rm "$IMAGE" ls /app/yiban/__init__.py /app/scripts/container_scheduler.py \
  > "$EV/c1-image-contents.txt" 2>&1 \
  || die "镜像缺 yiban/__init__.py 或 scripts/container_scheduler.py（见 c1-image-contents.txt）"
note "镜像含 yiban/__init__.py 与 scripts/container_scheduler.py"

# ---- C2 造数（独立卷 + 演练配置） -------------------------------------------
step "C2 造数（自造账号 + 合成窗口，密钥全部自造）"
mkdir -p data/state data/logs
# 幂等：把上一轮的日志与状态文件归档进本轮证据目录，再从空目录开跑
# （不清 sched-slot-first-<日期>.json 就重跑，首签闩锁会认为当日已触发而整轮不发请求）
if [ -n "$(ls -A data/logs 2>/dev/null)" ] || [ -n "$(ls -A data/state 2>/dev/null)" ]; then
  mkdir -p "$EV/prev-round/logs" "$EV/prev-round/state"
  mv -f data/logs/*  "$EV/prev-round/logs/"  2>/dev/null || true
  mv -f data/state/* "$EV/prev-round/state/" 2>/dev/null || true
  note "上一轮日志/状态已归档到 $EV/prev-round/"
fi
rm -f data/mock.ready
NOW_S=$(date +%s)
WIN_S=$((NOW_S + LEAD_SEC))
WIN_E=$((NOW_S + LEAD_SEC + SPAN_SEC))
[ "$((WIN_S / 86400))" -eq "$((WIN_E / 86400))" ] || pre "合成窗口跨日历日，请调小 --window-span-sec"
export DRILL_TREE="$ROOT" DRILL_N="$ACCOUNTS" DRILL_WS="$WIN_S" DRILL_WE="$WIN_E"
"$HOST_PYTHON" - > "$EV/c2-seed.log" 2>&1 <<'PY' || { cat "$EV/c2-seed.log" >&2; die "造数失败（c2-seed.log）"; }
import contextlib, datetime, os, sys

TREE = os.environ["DRILL_TREE"]
N = int(os.environ["DRILL_N"])
DB, ENV = TREE + "/data/yiban.db", TREE + "/data/.env"

sys.path.insert(0, TREE + "/scripts")
os.environ["YIBAN_ENV_FILE"] = ENV
os.environ["YIBAN_DB_FILE"] = DB
os.umask(0o77)
os.makedirs(TREE + "/data/state", exist_ok=True)
os.makedirs(TREE + "/data/logs", exist_ok=True)

start = datetime.datetime.fromtimestamp(int(os.environ["DRILL_WS"]))
end = datetime.datetime.fromtimestamp(int(os.environ["DRILL_WE"]))
assert start.date() == end.date(), "合成窗口不得跨日历日"

if not os.path.exists(ENV):
    with open(ENV, "w", encoding="utf-8") as f:
        f.write("# 容器演练专用 .env（drill-container.sh 生成，密钥自造，严禁入库）\n")

import db  # noqa: E402  scripts/db.py 兼容壳
from yiban.infra import env_io  # noqa: E402

db.init_db(DB, env_file=ENV, cleanup=False, migrate=True)
for i in range(N):
    email = "drill%05d@mock.invalid" % i
    with contextlib.suppress(Exception):
        db.create_user(email, "mock-hash", role="user")
    with contextlib.suppress(Exception):
        db.add_account({
            "name": "drill%05d" % i,
            "phone": "1380013800%d" % i,
            "password": "drill-pass-%d" % i,
            "phone_model": "MockPhone",
            "phone_code": "",
            "owner": email,
            "status": "active",
            "reject_reason": "",
        })

# .env 值必须是**容器视角**路径（/data/...），与 docker-compose environment 同口径
env_io.write_env_keys(ENV, {
    "YIBAN_SIGN_START": start.strftime("%H:%M"),
    "YIBAN_SIGN_END": end.strftime("%H:%M"),
    "YIBAN_START_DELAY_MAX": "0",
    "YIBAN_WINDOW_EDGE_FRONT_SEC": "0",
    "YIBAN_WINDOW_EDGE_BACK_SEC": "0",
    "YIBAN_GLOBAL_PAUSE": "0",
    "YIBAN_ACCOUNT_GAP_MAX": "2",
    "YIBAN_SIGN_ORDER": "sequence",
    "YIBAN_SIGN_DIST": "uniform",
    # 演练轮可能落在周末；置位周末门，否则 day_off 直接退出一轮不发请求
    "YIBAN_SATURDAY_SIGN": "1",
    "YIBAN_SUNDAY_SIGN": "1",
    "YIBAN_STATE_DIR": "/data/state",
    "YIBAN_LOG_FILE": "/data/logs/sign.log",
    "YIBAN_DB_FILE": "/data/yiban.db",
    "YIBAN_ACCOUNTS_FILE": "/data/accounts.json",
})
print("WINDOW", start.strftime("%H:%M:%S"), "-", end.strftime("%H:%M:%S"))
print("ACCOUNTS", len(db.load_accounts()))
PY
grep -qx "ACCOUNTS $ACCOUNTS" "$EV/c2-seed.log" \
  || { cat "$EV/c2-seed.log" >&2; die "造数账号数不是 $ACCOUNTS（见 c2-seed.log）"; }
[ "$(stat -c '%a' data/.env)" = "600" ]     || die "data/.env 权限不是 0600"
[ "$(stat -c '%a' data/yiban.db)" = "600" ] || die "data/yiban.db 权限不是 0600"
note "账号 $ACCOUNTS 个 @mock.invalid；.env / yiban.db 均 0600"

# ---- C3 mock 证书（openssl 直产） ------------------------------------------
step "C3 自签证书（CA + 五域名 SAN 服务器证书）"
CADIR="data/loadtest/ca"
mkdir -p "$CADIR"
SAN="$CADIR/san.ext"
{
  printf 'subjectAltName='
  sep=""
  for d in "${DOMAINS[@]}"; do printf '%sDNS:%s' "$sep" "$d"; sep=","; done
  printf '\nextendedKeyUsage=serverAuth\n'
} > "$SAN"
openssl req -x509 -newkey rsa:2048 -sha256 -days 2 -nodes \
  -keyout "$CADIR/ca.key" -out "$CADIR/ca.pem" \
  -subj "/CN=yiban-drill-ca" > "$EV/c3-certs.log" 2>&1
openssl req -newkey rsa:2048 -sha256 -nodes \
  -keyout "$CADIR/server.key" -out "$CADIR/server.csr" \
  -subj "/CN=${DOMAINS[0]}" >> "$EV/c3-certs.log" 2>&1
openssl x509 -req -in "$CADIR/server.csr" -CA "$CADIR/ca.pem" -CAkey "$CADIR/ca.key" \
  -CAcreateserial -days 2 -sha256 -extfile "$SAN" \
  -out "$CADIR/server.pem" >> "$EV/c3-certs.log" 2>&1
openssl verify -CAfile "$CADIR/ca.pem" "$CADIR/server.pem" >> "$EV/c3-certs.log" 2>&1 \
  || { tail -20 "$EV/c3-certs.log" >&2; die "自签证书自验失败（见 c3-certs.log）"; }
note "CA + 服务器证书已生成并 openssl verify 通过；SAN = ${DOMAINS[*]}"

# ---- C4 内部网络 + 起容器 --------------------------------------------------
step "C4 内部网络（--internal，结构性无出口）+ 起容器"
docker rm -f "$CTR" >/dev/null 2>&1 || true
docker network rm "$NET" >/dev/null 2>&1 || true
docker network create --internal "$NET" > "$EV/c4-network.txt"
note "网络 $NET 为 --internal"

ADD_HOST=()
for d in "${DOMAINS[@]}"; do ADD_HOST+=(--add-host "$d:127.0.0.1"); done

# environment 照抄 docker-compose.yml 的 services.yiban.environment
# （release-gate §2④：缺键会得到"打不开数据库"的假失败）
docker run -d --name "$CTR" --network "$NET" \
  "${ADD_HOST[@]}" \
  --security-opt no-new-privileges:true --cap-drop ALL \
  --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER \
  --cap-add SETGID --cap-add SETUID --cap-add KILL \
  --memory 512m --cpus 0.5 \
  -e TZ=Asia/Shanghai \
  -e YIBAN_ENV_FILE=/data/.env -e YIBAN_DB_FILE=/data/yiban.db \
  -e YIBAN_ACCOUNTS_FILE=/data/accounts.json -e YIBAN_STATE_DIR=/data/state \
  -e YIBAN_LOG_FILE=/data/logs/sign.log -e YIBAN_COOKIE_SECURE=1 \
  -e YIBAN_BACKUP_DATA_DIR=/data -e YIBAN_BACKUP_DIR=/backups \
  -e YIBAN_BACKUP_RETAIN_DAYS=30 \
  -e YIBAN_BACKUP_PASSPHRASE_FILE=/run/yiban-secrets/backup-passphrase \
  -e YIBAN_GLOBAL_PAUSE=0 \
  -e REQUESTS_CA_BUNDLE=/data/loadtest/ca/ca.pem \
  -e SSL_CERT_FILE=/data/loadtest/ca/ca.pem \
  -e CURL_CA_BUNDLE=/data/loadtest/ca/ca.pem \
  -v "$ROOT/data:/data" \
  "$IMAGE" > "$EV/c4-container-id.txt"
# 不发布任何宿主端口（-p 一个都没有）：web 只在容器内 127.0.0.1:17892 可达
docker port "$CTR" > "$EV/c4-ports.txt"
[ ! -s "$EV/c4-ports.txt" ] || die "容器发布了宿主端口（红线），见 c4-ports.txt"

# ---- C5 mock 上游 + 判据（web / sched / RestartCount / 自签负向 / 出口） ---
step "C5 mock 上游 + 容器判据"
docker cp tests/fake_yiban_server.py "$CTR:/tmp/fake_yiban_server.py"
docker exec -d "$CTR" python3 /tmp/fake_yiban_server.py \
  --host 127.0.0.1 --port 443 --no-ipv6 \
  --cert /data/loadtest/ca/server.pem --key /data/loadtest/ca/server.key \
  --log /data/logs/mock.jsonl --ready-file /data/mock.ready
for _ in $(seq 1 30); do
  if [ -f data/mock.ready ]; then break; fi
  sleep 1
done
[ -f data/mock.ready ] || die "mock 未在 30s 内就绪"

cat > /tmp/drill_tls_pos.py <<'PY'
import sys, requests
try:
    r = requests.get("https://oauth.yiban.cn/__health", timeout=5)
    print("POS", r.status_code, r.headers.get("Server", ""))
    sys.exit(0 if r.status_code == 200 else 3)
except Exception as e:
    print("POS-FAIL", type(e).__name__, e)
    sys.exit(4)
PY
cat > /tmp/drill_tls_neg.py <<'PY'
import sys, requests
try:
    r = requests.get("https://oauth.yiban.cn/__health", timeout=5)
    print("NEG-UNEXPECTED-OK", r.status_code)
    sys.exit(5)
except requests.exceptions.SSLError as e:
    print("NEG-SSLError", e)
    sys.exit(0)
except Exception as e:
    print("NEG-OTHER", type(e).__name__, e)
    sys.exit(6)
PY
cat > /tmp/drill_egress.py <<'PY'
import socket, sys
# RFC 5737 文档地址；--internal 网络里必须连不通（结构性隔离的实证）
try:
    s = socket.create_connection(("203.0.113.7", 80), timeout=4)
    s.close()
    print("EGRESS-UNEXPECTED-OPEN")
    sys.exit(7)
except OSError as e:
    print("EGRESS-BLOCKED", type(e).__name__, e)
    sys.exit(0)
PY
docker cp /tmp/drill_tls_pos.py "$CTR:/tmp/pos.py"
docker cp /tmp/drill_tls_neg.py "$CTR:/tmp/neg.py"
docker cp /tmp/drill_egress.py "$CTR:/tmp/egress.py"

{
  echo "== 挂 CA 时 mock 必须可达 =="
  docker exec "$CTR" python3 /tmp/pos.py
  echo "== 摘掉三个 CA 环境变量后必须 SSLError（证明自签校验真实生效）=="
  docker exec "$CTR" sh -c 'unset REQUESTS_CA_BUNDLE SSL_CERT_FILE CURL_CA_BUNDLE; exec python3 /tmp/neg.py'
} > "$EV/c5-tls.log" 2>&1 || die "自签证书正/负向判据未过（见 c5-tls.log）"
grep -q "^POS 200 "      "$EV/c5-tls.log" || die "挂 CA 时 mock 不可达（见 c5-tls.log）"
grep -q "^NEG-SSLError " "$EV/c5-tls.log" || die "摘 CA 后未 SSLError —— 自签校验形同虚设（见 c5-tls.log）"

docker exec "$CTR" python3 /tmp/egress.py > "$EV/c5-egress.log" 2>&1 \
  || die "容器竟能连出公网 —— 隔离红线破了（见 c5-egress.log）"
grep -q "^EGRESS-BLOCKED " "$EV/c5-egress.log" || die "出口探测结果异常（见 c5-egress.log）"
note "自签证书挂 CA 可达 / 摘 CA SSLError；出口 203.0.113.7:80 连不通"

WEB=""
for _ in $(seq 1 60); do
  WEB="$(docker exec "$CTR" python3 -c \
    "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:17892/login',timeout=3).status)" \
    2>/dev/null || true)"
  if [ "$WEB" = "200" ]; then break; fi
  sleep 1
done
[ "$WEB" = "200" ] || die "容器内 web 200 判据未过（最后一次取值：'${WEB:-空}'）"
RC="$(docker inspect -f '{{.RestartCount}}' "$CTR")"
[ "$RC" = "0" ] || die "容器 RestartCount=$RC（应 0）"
docker top "$CTR" > "$EV/c5-top.txt" 2>&1 || die "docker top 失败"
grep -q container_scheduler "$EV/c5-top.txt" || die "sched 进程不在位（见 c5-top.txt）"
docker exec "$CTR" python3 scripts/container_scheduler.py --check-health \
  > "$EV/c5-sched-health.txt" 2>&1 || die "sched 心跳不新鲜"
note "web=200（容器内回环）、sched 进程在位、RestartCount=0、心跳新鲜"

# ---- C6 整轮签到 -----------------------------------------------------------
step "C6 整轮签到（等窗口开闸 → 真签到链 → 汇总）"
TODAY="$(date +%F)"
LOG=""
DEADLINE=$(( $(date +%s) + TIMEOUT_SEC ))
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  if [ -f "data/logs/sign-$TODAY.log" ] \
     && grep -qF '==== 签到汇总' "data/logs/sign-$TODAY.log" 2>/dev/null; then
    LOG="data/logs/sign-$TODAY.log"
    break
  fi
  sleep 5
done
[ -n "$LOG" ] || die "$TIMEOUT_SEC 内没出现当日（$TODAY）签到汇总；先看 $EV/../c5-top.txt 与 data/logs/"
cp -f "$LOG" "$EV/c6-sign.log"
docker exec "$CTR" tail -50 /data/logs/sched.log > "$EV/c6-sched.log" 2>&1 || true
grep -qF '==== 开始执行签到（v' "$LOG" || die "签到日志缺版本横幅（见 c6-sign.log）"
grep -qF "==== 签到汇总" "$LOG"   || die "签到日志缺汇总行（见 c6-sign.log）"
grep -qF "✅ $ACCOUNTS 成功，❌ 0 失败" "$LOG" \
  || die "汇总不是「✅ $ACCOUNTS 成功，❌ 0 失败」（见 c6-sign.log）"
note "签到整轮完成：✅ $ACCOUNTS 成功，❌ 0 失败"

# ---- C7 mock 轨迹 + 宿主零副作用 + 清理对账 --------------------------------
step "C7 mock 请求轨迹 / 宿主零副作用 / 清理对账"
cp -f data/logs/mock.jsonl "$EV/c7-mock.jsonl" 2>/dev/null || true
if ! "$HOST_PYTHON" - "$ACCOUNTS" "$EV/c7-mock.jsonl" > "$EV/c7-mock-trace.txt" 2>&1 <<'PY'
import json, sys
from collections import Counter

n, path = int(sys.argv[1]), sys.argv[2]
want = [
    "/code/html",
    "/code/usersure",
    "/iframe/index",
    "/base/c/auth/yiban",
    "/nightAttendance/student/index/signPosition",
    "/nightAttendance/student/index/signIn",
]
seen, bad = Counter(), []
with open(path, encoding="utf-8") as fh:
    for line in fh:
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        if r.get("path") in ("/__health", "/__stats"):
            continue          # 运维探测口，不算签到链路
        seen[r.get("path", "?")] += 1
        if r.get("status", 0) >= 400:
            bad.append((r.get("path"), r.get("status")))
print("== mock request paths ==")
for p, c in sorted(seen.items()):
    print("%4d  %s" % (c, p))
print("== 4xx/5xx ==", bad if bad else "无")
missing = [p for p in want if seen[p] != n]
if missing:
    print("MISSING_OR_WRONG_COUNT", missing)
    sys.exit(1)
print("== 每账号六步全链命中 ==")
PY
then
  cat "$EV/c7-mock-trace.txt" >&2
  die "mock 轨迹不完整或有 4xx/5xx（见 c7-mock-trace.txt）"
fi
note "mock 收齐 $ACCOUNTS 账号 × 6 步全链，无 ≥400"

ss -ltnp          > "$EV/c7-ss-after.txt"   2>&1 || true
md5sum /etc/hosts > "$EV/c7-hosts-after.txt" 2>&1 || true
diff "$EV/baseline/ss-ltnp.txt" "$EV/c7-ss-after.txt" > "$EV/c7-ss-diff.txt" 2>&1 \
  || die "宿主监听端口发生变化（红线），见 c7-ss-diff.txt"
diff "$EV/baseline/hosts-md5.txt" "$EV/c7-hosts-after.txt" > "$EV/c7-hosts-diff.txt" 2>&1 \
  || die "宿主 /etc/hosts 发生变化（红线），见 c7-hosts-diff.txt"
note "宿主 ss -ltnp 与 /etc/hosts md5 前后零变化"

docker rm -f "$CTR" >/dev/null
docker network rm "$NET" >/dev/null
CLEANED=1
docker ps -a --filter "name=^${CTR}$" --format '{{.Names}}' > "$EV/c7-leftover-ctr.txt"
docker network ls --filter "name=^${NET}$" --format '{{.Name}}' > "$EV/c7-leftover-net.txt"
[ ! -s "$EV/c7-leftover-ctr.txt" ] || die "容器残留（见 c7-leftover-ctr.txt）"
[ ! -s "$EV/c7-leftover-net.txt" ] || die "网络残留（见 c7-leftover-net.txt）"
docker ps -a      > "$EV/c7-docker-ps-after.txt" 2>&1 || true
docker network ls > "$EV/c7-docker-net-after.txt" 2>&1 || true
note "容器与网络已清理、无残留；镜像 $IMAGE 保留供复跑"

# ---- 汇总 ------------------------------------------------------------------
step "演练结论"
printf 'window=%s~%s\nfinished=%s\nresult=PASS\n' \
  "$(date -d "@$WIN_S" +%H:%M:%S)" "$(date -d "@$WIN_E" +%H:%M:%S)" "$(date -Is)" >> "$EV/run.meta"

cat <<EOF

  ✅ 演练轮 PASS —— 判据全绿，证据见 $EV/

  C1 构建          镜像含 yiban/__init__.py 与 scripts/container_scheduler.py
  C2 造数          $ACCOUNTS 个 @mock.invalid 账号；.env / yiban.db 均 0600；窗口已合成
  C3 证书          CA + 五域名 SAN 服务器证书，openssl verify 通过
  C4 网络/容器     --internal 网络；不发布任何宿主端口
  C5 判据          web=200 · sched 在位 · RestartCount=0 · 挂 CA 可达/摘 CA SSLError · 出口连不通
  C6 整轮签到      ==== 开始执行签到（v…）→ ✅ $ACCOUNTS 成功，❌ 0 失败
  C7 对账          mock 六步全链无 4xx/5xx · 宿主端口与 hosts 零变化 · 无容器/网络残留

  本轮是**演练轮，不是有效轮次**：证据只在 $EV/，不进发布台账。
EOF