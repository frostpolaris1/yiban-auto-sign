#!/usr/bin/env bash
# ============================================================
# deploy_gate.sh —— 部署发布闸门（干净 docker 环境，一次装成）
# ============================================================
# 用途：
#   在**全新** docker 环境（独立 project / 独立命名卷 / 独立网络 / 独立数据目录，
#   不复用任何旧容器）按 README「Docker 部署」从零起全栈并逐条断言。三个阶段：
#     A 干净部署：起全栈 → /login 200 → .env 管理员登录 → 灌合成账号并计数
#     B 迁移演练：v0.6.2 起库灌数 → 升级到本树顶端（v21→v22 自动）→ 校验数据完整
#                 → 回滚到 v0.6.2（不降 schema，验证完整性门容忍 v22）
#     C 备份恢复：容器形态加密备份（含库本体断言）→ 删库 → 恢复 → 自检
#   结尾打印 PASS/FAIL 摘要表；任一 FAIL 退出码 1。
#
# 用法（必须在仓库根目录执行）：
#   bash scripts/deploy_gate.sh                 # 跑 A+B+C
#   bash scripts/deploy_gate.sh --phase A       # 只跑某阶段（可多次/逗号分隔）
#   bash scripts/deploy_gate.sh --keep          # 结束不销毁容器（排障）
#   bash scripts/deploy_gate.sh --old-ref release/0.6.2
#
# 可调环境变量（默认值见下）：
#   GATE_TMP        运行产物根目录（默认 <repo>/work/deploy-gate）
#   GATE_PROJECT    compose 项目名前缀（默认 yiban-gate）
#   GATE_HTTP_PORT / GATE_HTTPS_PORT   A 阶段宿主端口（默认 18080/18443；
#                   B/C 阶段各 +1/+2，避免与本机既有实例抢 80/443）
#   GATE_SEED_N     合成账号数（默认 5000；只计数，不做逐账号断言）
#   GATE_ADMIN_USER / GATE_ADMIN_PASSWORD   .env 显式管理员（默认见下）
#   GATE_PIP_INDEX_URL  构建期 pip 索引（默认 aliyun 镜像；国内主机直连 pypi 不可达）
#   GATE_OLD_REF    迁移演练的旧版本 ref（默认 release/0.6.2）
#   GATE_TIMEOUT    健康等待上限秒数（默认 600）
#   GATE_OLD_TREE   B 阶段旧版代码树路径（预导出）。默认走 `git archive $GATE_OLD_REF`；
#                   若当前检出无法跑 git archive（典型：WSL 下 worktree 的 `.git` 是
#                   Windows 路径指针 `D:/…/.git/worktrees/…`，WSL 侧 git 认不出），
#                   先在 Git Bash 侧 `git archive $GATE_OLD_REF | tar -x -C <dir>`，
#                   再把 <dir> 传给本变量。
#
# 维护提醒：B5 断言把 `user_version` 钉死为 **22**（本单顶端的 schema 版本）。下一次
#   schema 迁移（v23+）落地后，必须同步改 B5 的期望值，并复核 B11 回滚判据。
#
# 红线：
#   * 只在本机 docker 内造数；不连任何真实平台（合成数据不上网，账号为 13xxxx 占位）。
#   * 独立 compose 项目名 + 独立端口，绝不接管本机既有容器（如 /root/yiban-drill）。
#   * 全部凭据自造，写进 $GATE_TMP，不入库、不进日志明文。
#   * 造数/检查一律以容器内业务用户 uid 10001 执行（与 supervisord 同身份）；以 root
#     执行会把 /data/.env 写成 root:0600，业务用户随即读不到（备份脚本据此正当拒绝）。
# ============================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$BASE"

GATE_TMP="${GATE_TMP:-$BASE/work/deploy-gate}"
GATE_PROJECT="${GATE_PROJECT:-yiban-gate}"
GATE_HTTP_PORT="${GATE_HTTP_PORT:-18080}"
GATE_HTTPS_PORT="${GATE_HTTPS_PORT:-18443}"
GATE_SEED_N="${GATE_SEED_N:-5000}"
GATE_ADMIN_USER="${GATE_ADMIN_USER:-gateadmin}"
GATE_ADMIN_PASSWORD="${GATE_ADMIN_PASSWORD:-XwxfGate2026Ab}"
GATE_PIP_INDEX_URL="${GATE_PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple/}"
GATE_OLD_REF="${GATE_OLD_REF:-release/0.6.2}"
GATE_TIMEOUT="${GATE_TIMEOUT:-600}"
KEEP=0
PHASES="A,B,C"

while [ $# -gt 0 ]; do
    case "$1" in
        --phase) PHASES="$2"; shift 2 ;;
        --phase=*) PHASES="${1#*=}"; shift ;;
        --keep) KEEP=1; shift ;;
        --old-ref) GATE_OLD_REF="$2"; shift 2 ;;
        --old-ref=*) GATE_OLD_REF="${1#*=}"; shift ;;
        -h|--help) sed -n '2,50p' "$0"; exit 0 ;;
        *) echo "未知参数: $1" >&2; exit 2 ;;
    esac
done
IFS=',' read -ra PHASE_LIST <<< "$PHASES"

LOG_DIR="$GATE_TMP/logs"
mkdir -p "$LOG_DIR"

# ---- 结果收集 ----
RESULT_KEYS=()
RESULT_STATE=()
RESULT_NOTE=()
_record() { RESULT_KEYS+=("$1"); RESULT_STATE+=("$2"); RESULT_NOTE+=("$3"); }
pass() { echo "  [PASS] $1${2:+ — $2}"; _record "$1" PASS "$2"; }
fail() { echo "  [FAIL] $1${2:+ — $2}"; _record "$1" FAIL "$2"; }
skip() { echo "  [SKIP] $1${2:+ — $2}"; _record "$1" SKIP "$2"; }
hdr()  { echo; echo "==== $* ===="; }

CUR_PROJ=""
CUR_OVERRIDE=""
dc() { docker compose -p "$CUR_PROJ" -f "$BASE/docker-compose.yml" -f "$CUR_OVERRIDE" \
        --project-directory "$BASE" "$@"; }

# 先 build（up 不接受 --build-arg）再 up；日志由调用方重定向
dc_up_build() {
    dc build --build-arg "PIP_INDEX_URL=$GATE_PIP_INDEX_URL" && dc up -d
}

cleanup_proj() {
    local proj="$1" ov="$2"
    [ "$KEEP" = 1 ] && { echo "  (--keep) 保留 $proj 容器"; return; }
    docker compose -p "$proj" -f "$BASE/docker-compose.yml" -f "$ov" \
        --project-directory "$BASE" down -v --remove-orphans >/dev/null 2>&1 || true
}

# write_override <file> <proj> <data_dir> <certs_dir> <http> <https> [pass_file]
write_override() {
    local file="$1" proj="$2" data="$3" certs="$4" http="$5" https="$6" pass_file="${7:-}"
    {
        echo "services:"
        echo "  yiban:"
        echo "    container_name: ${proj}-yiban"
        echo "    ports: !override"
        echo "      - \"${http}:80\""
        echo "      - \"${https}:443\""
        echo "    volumes: !override"
        echo "      - ${data}:/data"
        echo "      - yiban-backups:/backups"
        if [ -n "$pass_file" ]; then
            echo "      - ${pass_file}:/run/yiban-secrets/backup-passphrase:ro"
        fi
        echo "  yiban-nginx:"
        echo "    container_name: ${proj}-nginx"
        echo "    volumes: !override"
        echo "      - ${BASE}/docker/nginx.conf:/etc/nginx/conf.d/default.conf:ro"
        echo "      - ${certs}:/etc/yiban/certs:ro"
    } > "$file"
}

wait_healthy() {
    local proj="$1" end=$((SECONDS + GATE_TIMEOUT)) st
    while [ $SECONDS -lt $end ]; do
        st="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${proj}-yiban" 2>/dev/null || echo missing)"
        case "$st" in
            healthy) return 0 ;;
            unhealthy) return 1 ;;
        esac
        sleep 5
    done
    return 1
}

# prepare_env <data_dir> : 造数据目录 + .env + 证书
prepare_env() {
    local data="$1" certs="$2"
    rm -rf "$data" "$certs"
    mkdir -p "$data/logs" "$data/state" "$certs"
    cp "$BASE/.env.docker.example" "$data/.env"
    sed -i "s|^YIBAN_ADMIN_USER=.*|YIBAN_ADMIN_USER=${GATE_ADMIN_USER}|" "$data/.env"
    sed -i "s|^YIBAN_ADMIN_PASSWORD=.*|YIBAN_ADMIN_PASSWORD=${GATE_ADMIN_PASSWORD}|" "$data/.env"
    openssl req -x509 -nodes -days 2 -newkey rsa:2048 \
        -keyout "$certs/key.pem" -out "$certs/fullchain.pem" \
        -subj "/CN=deploy-gate" >/dev/null 2>&1
}

c_in_py() { # c_in_py <proj> <ov> <python code>
    # 一律以业务用户 uid 10001（容器内 yiban）执行：与 supervisord 下的 web/调度同身份。
    # 用 root 执行会让 init_db 把 /data/.env 写成 root:0600，业务用户随即读不到——
    # 备份脚本「打前先查」会据此正当地拒绝，属于演练身份错误而非产品缺陷。
    local proj="$1" ov="$2" code="$3"
    docker compose -p "$proj" -f "$BASE/docker-compose.yml" -f "$ov" \
        --project-directory "$BASE" exec -T -u 10001 yiban python3 -c "$code"
}

c_uv() { # user_version（不过 init_db，只读）
    c_in_py "$1" "$2" "import sqlite3;print(sqlite3.connect('/data/yiban.db').execute('PRAGMA user_version').fetchone()[0])"
}

seed_sql_py() { # 灌合成数据：n 个账号 + 若干审计行 [+ 一条旧出口行]
    cat <<PY
import sys
from yiban.store import db, accounts
db.init_db()
n = $GATE_SEED_N
rows = [{"name": "seed%05d" % i, "phone": "13%09d" % i,
         "password": "seedpass", "status": "active", "owner": "admin"}
        for i in range(n)]
accounts.replace_accounts(rows)
for i in range(20):
    db.audit("gate-seed", "seed_audit", "t%d" % i, "row %d" % i)
$([ "$1" = legacy ] && echo "db.get_conn().execute(\"INSERT OR REPLACE INTO egress_state (egress, rate, burst, tat, updated_at) VALUES ('worker-0@gatehost', 1.0, 0, 0, '2026-01-01 00:00:00')\"); db.get_conn().commit()")
print("ACCOUNTS=", db.get_conn().execute("SELECT COUNT(*) FROM accounts").fetchone()[0])
print("AUDIT=", db.get_conn().execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0])
PY
}

# ============================================================
# Phase A
# ============================================================
phase_A() {
    hdr "Phase A：干净部署（README Docker 路径）"
    local proj="${GATE_PROJECT}-a" data="$GATE_TMP/data-a" certs="$GATE_TMP/certs-a"
    local ov="$GATE_TMP/override-a.yml" log="$LOG_DIR/phase-a.log"
    CUR_PROJ="$proj"; CUR_OVERRIDE="$ov"
    prepare_env "$data" "$certs"
    write_override "$ov" "$proj" "$data" "$certs" "$GATE_HTTP_PORT" "$GATE_HTTPS_PORT"
    : > "$log"

    echo "-- docker compose up -d --build"
    if dc_up_build >>"$log" 2>&1; then
        pass "A1 docker compose 构建并启动" "rc=0"
    else
        fail "A1 docker compose 构建并启动" "见 $log"; cleanup_proj "$proj" "$ov"; return
    fi

    if wait_healthy "$proj"; then
        pass "A2 容器健康（healthcheck: /login + sched 心跳）" "healthy"
    else
        fail "A2 容器健康" "超时 $GATE_TIMEOUT s 未 healthy"; dc logs --tail 40 >>"$log" 2>&1
        cleanup_proj "$proj" "$ov"; return
    fi

    local code
    code="$(curl -sk -o /dev/null -w '%{http_code}' --max-time 15 "https://127.0.0.1:${GATE_HTTPS_PORT}/login")"
    if [ "$code" = "200" ]; then pass "A3 GET /login（经 nginx HTTPS）" "200"; else fail "A3 GET /login" "HTTP $code"; fi

    local lresp lcode
    lresp="$(curl -sk -o /dev/null -w '%{http_code}' --max-time 15 \
        -H 'Content-Type: application/json' \
        -d "{\"username\":\"${GATE_ADMIN_USER}\",\"password\":\"${GATE_ADMIN_PASSWORD}\"}" \
        "https://127.0.0.1:${GATE_HTTPS_PORT}/api/login")"
    lcode="$lresp"
    if [ "$lcode" = "200" ]; then pass "A4 .env 管理员登录 /api/login" "200"; else fail "A4 管理员登录" "HTTP $lcode"; fi

    local out
    out="$(c_in_py "$proj" "$ov" "$(seed_sql_py plain)" 2>>"$log")"
    echo "$out" >> "$log"
    local n got
    got="$(printf '%s\n' "$out" | sed -n 's/^ACCOUNTS= //p' | head -1)"
    if [ "${got:-0}" = "$GATE_SEED_N" ]; then pass "A5 灌合成账号并计数" "accounts=$got"; else fail "A5 灌合成账号" "期望 $GATE_SEED_N 实得 ${got:-?}"; fi

    cleanup_proj "$proj" "$ov"
}

# ============================================================
# Phase B：迁移演练
# ============================================================
phase_B() {
    hdr "Phase B：迁移演练（$GATE_OLD_REF → develop 顶端，v21→v22 + 回滚）"
    local proj="${GATE_PROJECT}-mig" data="$GATE_TMP/data-mig" certs="$GATE_TMP/certs-mig"
    local ov="$GATE_TMP/override-mig.yml" log="$LOG_DIR/phase-b.log"
    local old_tree="$GATE_TMP/tree-old"
    local http=$((GATE_HTTP_PORT + 1)) https=$((GATE_HTTPS_PORT + 1))
    CUR_PROJ="$proj"; CUR_OVERRIDE="$ov"
    prepare_env "$data" "$certs"
    : > "$log"

    # 旧版代码树：默认用 git archive 现场导出（不改动本工作树、不注册 worktree）。
    # 若调用方已导出（如 WSL 下工作树 .git 指针为 Windows 路径、git 不可用），
    # 用 GATE_OLD_TREE 直接指过去。
    if [ -n "${GATE_OLD_TREE:-}" ]; then
        old_tree="$GATE_OLD_TREE"
        if [ ! -f "$old_tree/docker/Dockerfile" ]; then
            fail "B0 旧版代码树" "GATE_OLD_TREE 无 docker/Dockerfile: $old_tree"; return
        fi
    else
        rm -rf "$old_tree"; mkdir -p "$old_tree"
        if ! git archive "$GATE_OLD_REF" | tar -x -C "$old_tree"; then
            fail "B0 导出旧版代码树" "git archive $GATE_OLD_REF 失败（可设 GATE_OLD_TREE 预导出）"; return
        fi
    fi

    # 覆盖文件：镜像与构建上下文可变（旧/新代码切换）
    _write_mig_override() {
        local img="$1" ctx="$2"
        {
            echo "services:"
            echo "  yiban:"
            echo "    container_name: ${proj}-yiban"
            echo "    image: ${img}"
            echo "    build:"
            echo "      context: ${ctx}"
            echo "      dockerfile: docker/Dockerfile"
            echo "    ports: !override"
            echo "      - \"${http}:80\""
            echo "      - \"${https}:443\""
            echo "    volumes: !override"
            echo "      - ${data}:/data"
            echo "      - yiban-backups:/backups"
            echo "  yiban-nginx:"
            echo "    container_name: ${proj}-nginx"
            echo "    volumes: !override"
            echo "      - ${BASE}/docker/nginx.conf:/etc/nginx/conf.d/default.conf:ro"
            echo "      - ${certs}:/etc/yiban/certs:ro"
        } > "$ov"
    }

    # --- B1 旧版起库 ---
    _write_mig_override "${proj}-old" "$old_tree"
    echo "-- 旧版构建并启动（v0.6.2 形态起库）"
    if dc_up_build >>"$log" 2>&1 && wait_healthy "$proj"; then
        pass "B1 旧版（$GATE_OLD_REF）起库并健康" "healthy"
    else
        fail "B1 旧版起库" "见 $log"; cleanup_proj "$proj" "$ov"; return
    fi

    local uv_before
    uv_before="$(c_uv "$proj" "$ov" 2>>"$log")"
    if [ "$uv_before" = "21" ]; then pass "B2 旧版 user_version=21" "21"; else fail "B2 旧版 user_version" "实得 $uv_before（期望 21）"; fi

    local out accounts_before head_before
    out="$(c_in_py "$proj" "$ov" "$(seed_sql_py legacy)" 2>>"$log")"; echo "$out" >>"$log"
    accounts_before="$(printf '%s\n' "$out" | sed -n 's/^ACCOUNTS= //p' | head -1)"
    head_before="$(c_in_py "$proj" "$ov" "from yiban.store import db; db.init_db(cleanup=False, migrate=False); print('HEAD', db.audit_head_hash())" 2>>"$log")"
    echo "$head_before" >>"$log"
    if [ "${accounts_before:-0}" = "$GATE_SEED_N" ]; then pass "B3 旧版灌数（账号+审计+旧出口行）" "accounts=$accounts_before"; else fail "B3 旧版灌数" "accounts=${accounts_before:-?}"; fi

    # --- B4 升级到 develop 顶端 ---
    _write_mig_override "${proj}-new" "$BASE"
    echo "-- 升级到 develop 顶端（同数据卷，镜像重建）"
    if dc_up_build >>"$log" 2>&1 && wait_healthy "$proj"; then
        pass "B4 升级后容器健康" "healthy"
    else
        fail "B4 升级后健康" "见 $log"; dc logs --tail 60 >>"$log" 2>&1; cleanup_proj "$proj" "$ov"; return
    fi

    local uv_after
    uv_after="$(c_uv "$proj" "$ov" 2>>"$log")"
    if [ "$uv_after" = "22" ]; then pass "B5 v22 迁移已自动执行（user_version=22）" "22"; else fail "B5 v22 迁移" "实得 $uv_after（期望 22）"; fi

    local accounts_after egress_left chain
    accounts_after="$(c_in_py "$proj" "$ov" "import sqlite3;print(sqlite3.connect('/data/yiban.db').execute('SELECT COUNT(*) FROM accounts').fetchone()[0])" 2>>"$log")"
    if [ "${accounts_after:-0}" = "$GATE_SEED_N" ]; then pass "B6 升级前后账号数不变" "$accounts_before=$accounts_after"; else fail "B6 账号数守恒" "$accounts_before→${accounts_after:-?}"; fi

    egress_left="$(c_in_py "$proj" "$ov" "import sqlite3;print(sqlite3.connect('/data/yiban.db').execute(\"SELECT COUNT(*) FROM egress_state WHERE egress='worker-0@gatehost'\").fetchone()[0])" 2>>"$log")"
    if [ "${egress_left:-1}" = "0" ]; then pass "B7 v22 清空旧出口行（egress_state 重建）" "旧键剩 0 行"; else fail "B7 v22 清空旧出口行" "旧键剩 ${egress_left:-?} 行"; fi

    chain="$(c_in_py "$proj" "$ov" "from yiban.store import db; db.init_db(cleanup=False, migrate=False); print('CHAIN', db.verify_audit_chain()[0])" 2>>"$log")"
    echo "$chain" >>"$log"
    if printf '%s' "$chain" | grep -q 'CHAIN True'; then pass "B8 升级后审计链自洽 chain_ok=True" "True"; else fail "B8 审计链" "$chain"; fi

    local egrc exits
    c_in_py "$proj" "$ov" "import sys; from yiban import cli; sys.exit(cli.main(['egress','--status','--json']))" >>"$log" 2>&1
    egrc=$?
    # 引擎侧出口身份仍解析得出（"升级后引擎配置仍读得到出口"）：exit 标识来自 .env/配置，
    # 与 egress_state（已被 v22 清空）无关，故两者都要成立。
    exits="$(c_in_py "$proj" "$ov" "from yiban import egress; print('EXITS', egress.egress_identity(egress.resolve('worker', 0)), egress.egress_identity(egress.resolve('fallback')))" 2>>"$log")"
    echo "$exits" >>"$log"
    if [ "$egrc" = "0" ] && printf '%s' "$exits" | grep -qE 'EXITS [^ ]'; then
        pass "B9 升级后引擎出口可读（cli egress + 身份解析）" "rc=0 ${exits#EXITS }"
    else
        fail "B9 引擎出口读取" "rc=$egrc exits=$exits"
    fi

    # --- B10 回滚 ---
    _write_mig_override "${proj}-old" "$old_tree"
    echo "-- 回滚到旧版代码（库不动）"
    if dc_up_build >>"$log" 2>&1 && wait_healthy "$proj"; then
        pass "B10 回滚（旧版代码 on v22 库）容器健康" "healthy"
    else
        fail "B10 回滚后健康" "见 $log"; dc logs --tail 60 >>"$log" 2>&1; cleanup_proj "$proj" "$ov"; return
    fi
    local uv_rb accounts_rb
    uv_rb="$(c_uv "$proj" "$ov" 2>>"$log")"
    accounts_rb="$(c_in_py "$proj" "$ov" "import sqlite3;print(sqlite3.connect('/data/yiban.db').execute('SELECT COUNT(*) FROM accounts').fetchone()[0])" 2>>"$log")"
    if [ "$uv_rb" = "22" ] && [ "${accounts_rb:-0}" = "$GATE_SEED_N" ]; then
        pass "B11 回滚不降 schema 且数据完整" "uv=$uv_rb accounts=$accounts_rb"
    else
        fail "B11 回滚 schema/数据" "uv=$uv_rb accounts=${accounts_rb:-?}"
    fi

    cleanup_proj "$proj" "$ov"
}

# ============================================================
# Phase C：备份 / 恢复
# ============================================================
phase_C() {
    hdr "Phase C：容器形态备份 → 删库 → 恢复 → 自检"
    local proj="${GATE_PROJECT}-bak" data="$GATE_TMP/data-bak" certs="$GATE_TMP/certs-bak"
    local ov="$GATE_TMP/override-bak.yml" log="$LOG_DIR/phase-c.log"
    local pass="$GATE_TMP/backup-passphrase"
    local http=$((GATE_HTTP_PORT + 2)) https=$((GATE_HTTPS_PORT + 2))
    CUR_PROJ="$proj"; CUR_OVERRIDE="$ov"
    prepare_env "$data" "$certs"
    printf 'gate-backup-pass\n' > "$pass"
    chmod 600 "$pass"
    # README 口径：口令文件必须对容器内 yiban（uid 10001）可读。调度器以该身份调备份脚本，
    # 这里按同一身份、同一权限模型演练（否则会在 root 身份下"假通过"）。
    chown 10001:10001 "$pass" 2>>"$log" || echo "  (warn) 口令文件 chown 10001 失败（宿主文件系统不支持？）" >&2
    write_override "$ov" "$proj" "$data" "$certs" "$http" "$https" "$pass"
    : > "$log"

    if dc_up_build >>"$log" 2>&1 && wait_healthy "$proj"; then
        pass "C1 起全栈（启用容器备份口令挂载）" "healthy"
    else
        fail "C1 起全栈" "见 $log"; cleanup_proj "$proj" "$ov"; return
    fi
    # 口令文件以 uid 10001 身份可读（README 启用两步的验收面）
    if dc exec -T -u 10001 yiban sh -c 'test -r /run/yiban-secrets/backup-passphrase' >>"$log" 2>&1; then
        pass "C1b 口令文件对 uid 10001 可读" "可读"
    else
        fail "C1b 口令文件对 uid 10001 可读" "不可读（README 启用步骤未生效）"
    fi
    # 数据卷对业务用户可读（备份脚本「打前先查」的前提；root 误写会在此暴露）
    if dc exec -T -u 10001 yiban sh -c 'test -r /data/.env && test -w /data' >>"$log" 2>&1; then
        pass "C1c 数据卷对 uid 10001 可读写" "/data/.env 可读"
    else
        fail "C1c 数据卷对 uid 10001 可读写" "/data/.env 不可读或 /data 不可写"
    fi
    c_in_py "$proj" "$ov" "$(seed_sql_py plain)" >>"$log" 2>&1
    # 落一次审计链外部锚点（README 承诺备份含 audit-anchor.log）：锚点由每日线程/事件写，
    # 全新库"首次备份前"可能尚未存在，故先显式落一次，再断言包内确实含它。
    c_in_py "$proj" "$ov" "from yiban.store import db; db.init_db(); db.record_audit_anchor()" >>"$log" 2>&1

    # 复刻调度器 02:00 的备份调用环境（docker/scheduler.py 的 _backup_env）：
    #   * 身份 = uid 10001（yiban）；
    #   * DATA_DIR/BACKUP_DIR 由 YIBAN_BACKUP_DATA_DIR/YIBAN_BACKUP_DIR 翻译而来；
    #   * GNUPGHOME 指向状态目录下的私有子目录——yiban 的 HOME 是 /nonexistent，
    #     不设它 gpg 会 `can't create directory '/nonexistent/.gnupg'` 直接失败。
    dc exec -T -u 10001 yiban mkdir -p /data/state/gnupg >>"$log" 2>&1
    local out
    out="$(dc exec -T -u 10001 -e DATA_DIR=/data -e BACKUP_DIR=/backups \
        -e GNUPGHOME=/data/state/gnupg \
        yiban bash scripts/backup-docker.sh 2>>"$log")"; rc=$?
    echo "$out" >>"$log"
    if [ "$rc" = "0" ] && printf '%s' "$out" | grep -q '自检   : 解密+解包验证通过'; then
        pass "C2 容器形态加密备份成功" "自检通过"
    else
        fail "C2 容器备份" "rc=$rc"; cleanup_proj "$proj" "$ov"; return
    fi
    if printf '%s' "$out" | grep -q '库本体 : 已包含'; then
        pass "C3 备份含库本体（4o35 修复断言）" "已包含 yiban.db"
    else
        fail "C3 备份库本体断言" "未见 '库本体 : 已包含'"
    fi
    if printf '%s' "$out" | grep -q '锚点   : 已包含'; then
        pass "C3b 备份含审计链外部锚点" "已包含 audit-anchor.log"
    else
        fail "C3b 备份含审计链外部锚点" "未见 '锚点   : 已包含'（包内无 audit-anchor.log）"
    fi

    # 记录可供恢复的包名
    local pkg
    pkg="$(printf '%s\n' "$out" | sed -n 's/^备份完成: //p' | head -1)"
    if [ -z "$pkg" ]; then
        fail "C4 定位备份产物" "未解析到包路径"; cleanup_proj "$proj" "$ov"; return
    fi

    # 删库（模拟灾难）
    dc exec -T yiban sh -c 'rm -f /data/yiban.db /data/yiban.db-wal /data/yiban.db-shm' >>"$log" 2>&1
    if ! dc exec -T yiban test -e /data/yiban.db; then
        pass "C5 删除库文件（模拟灾难）" "库已不在"
    else
        fail "C5 删库" "库仍在"
    fi

    # 恢复（校验式解包）到临时目录，再把整份库（主文件 + WAL + SHM）搬回 /data。
    # 容器形态备份是 tar /data 的**活库快照**（README 明示：非 sqlite3 .backup 一致性快照）：
    # 已提交但未 checkpoint 的行在 -wal 里，只搬主文件会丢这部分数据。
    if dc exec -T yiban bash scripts/backup-docker.sh --restore "$pkg" /tmp/restore-test >>"$log" 2>&1; then
        pass "C6 恢复解包（三重安全校验通过）" "rc=0"
    else
        fail "C6 恢复解包" "rc!=0"
    fi
    if ! dc exec -T yiban test -e /tmp/restore-test/data/yiban.db; then
        fail "C6b 解包产物含库本体" "无 /tmp/restore-test/data/yiban.db"
    fi
    dc exec -T yiban sh -c 'cp /tmp/restore-test/data/yiban.db* /data/; chown yiban:yiban /data/yiban.db* 2>/dev/null; rm -rf /tmp/restore-test; ls -l /data/yiban.db*' >>"$log" 2>&1

    # 重启 web（重新打开恢复出来的库）
    dc restart yiban >>"$log" 2>&1
    if wait_healthy "$proj"; then pass "C7 恢复后容器健康（/login 200）" "healthy"; else fail "C7 恢复后健康" "见 $log"; fi

    local accounts chain
    accounts="$(c_in_py "$proj" "$ov" "import sqlite3;print(sqlite3.connect('/data/yiban.db').execute('SELECT COUNT(*) FROM accounts').fetchone()[0])" 2>>"$log")"
    chain="$(c_in_py "$proj" "$ov" "from yiban.store import db; db.init_db(cleanup=False, migrate=False); print('CHAIN', db.verify_audit_chain()[0])" 2>>"$log")"
    echo "accounts=$accounts $chain" >>"$log"
    if [ "${accounts:-0}" = "$GATE_SEED_N" ] && printf '%s' "$chain" | grep -q 'CHAIN True'; then
        pass "C8 恢复后数据自检（账号数+审计链）" "accounts=$accounts chain_ok=True"
    else
        fail "C8 恢复后数据自检" "accounts=${accounts:-?} $chain"
    fi

    cleanup_proj "$proj" "$ov"
}

# ============================================================
# 说明项：部署清单里依赖"未合入批次"的项
# ============================================================
phase_info() {
    hdr "说明项：SSE 批 fdjl（gunicorn 线程升档 + nginx /api/stream/ location）"
    local th
    th="$(grep -oE '\-\-threads [0-9]+' "$BASE/docker/supervisord.conf" | head -1)"
    if grep -rqF "/api/stream/" "$BASE/web" 2>/dev/null; then
        if grep -qF "/api/stream/" "$BASE/docker/nginx.conf"; then
            pass "nginx /api/stream/ location（fdjl 已合入）" "存在"
        else
            fail "nginx /api/stream/ location（fdjl 已合入）" "缺失"
        fi
        skip "gunicorn 线程升档" "fdjl 已合入，请按该批判据另行校验（现状 ${th:-unknown}）"
    else
        skip "SSE 批 fdjl 全部项" "fdjl 未合入 develop，按任务书跳过（现状 supervisord: ${th:-unknown}）"
    fi
}

# ============================================================
# main
# ============================================================
echo "deploy_gate: base=$BASE tmp=$GATE_TMP project=$GATE_PROJECT phases=$PHASES"
for p in "${PHASE_LIST[@]}"; do
    case "$(echo "$p" | tr 'a-z' 'A-Z')" in
        A) phase_A ;;
        B) phase_B ;;
        C) phase_C ;;
        *) echo "未知阶段: $p" >&2 ;;
    esac
done
phase_info

echo
echo "================ PASS/FAIL 摘要 ================"
n_fail=0
for i in "${!RESULT_KEYS[@]}"; do
    printf '%-6s | %-46s | %s\n' "${RESULT_STATE[$i]}" "${RESULT_KEYS[$i]}" "${RESULT_NOTE[$i]}"
    [ "${RESULT_STATE[$i]}" = FAIL ] && n_fail=$((n_fail + 1))
done
echo "==============================================="
echo "合计: ${#RESULT_KEYS[@]} 项, FAIL=$n_fail"
[ "$n_fail" -eq 0 ] && echo "闸门结论: PASS" || echo "闸门结论: FAIL"
exit "$([ "$n_fail" -eq 0 ] && echo 0 || echo 1)"
