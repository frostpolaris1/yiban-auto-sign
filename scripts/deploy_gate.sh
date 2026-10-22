#!/usr/bin/env bash
# ============================================================
# deploy_gate.sh —— 部署发布闸门（干净 docker 环境，一次装成）
# ============================================================
# 用途：
#   在**全新** docker 环境（独立 project / 独立命名卷 / 独立网络 / 独立数据目录，
#   不复用任何旧容器）按 README「Docker 部署」从零起全栈并逐条断言。三个阶段：
#     A 干净部署：起全栈 → /setup 200 → CLI init 建管理员 → /login 200 → 登录 → 灌合成账号并计数
#     B 迁移演练：v0.6.2 起库灌数 → 升级到本树顶端（v21→v22 自动）→ 校验数据完整
#                 → 回滚到 v0.6.2（不降 schema，验证完整性门容忍 v22）
#     C 备份恢复：容器形态加密备份（含库本体断言）→ 删库 → 恢复 → 自检
#   结尾打印 PASS/FAIL 摘要表；任一 FAIL 退出码 1。
#
# 用法（必须在仓库根目录执行）：
#   bash scripts/deploy_gate.sh                 # 跑 A+B+C
#   bash scripts/deploy_gate.sh --phase A       # 只跑某阶段（可多次/逗号分隔）
#   bash scripts/deploy_gate.sh --keep          # 结束不销毁容器（排障）
#   bash scripts/deploy_gate.sh --old-ref v0.6.2
#
# 可调环境变量（默认值见下）：
#   GATE_TMP        运行产物根目录（默认 <repo>/work/deploy-gate）
#   GATE_PROJECT    compose 项目名前缀（默认 yiban-gate）
#   GATE_HTTP_PORT / GATE_HTTPS_PORT   A 阶段宿主端口（默认 18080/18443；
#                   B/C 阶段各 +1/+2，避免与本机既有实例抢 80/443）
#   GATE_SEED_N     合成账号数（默认 5000；只计数，不做逐账号断言）
#   GATE_ADMIN_USER / GATE_ADMIN_PASSWORD   Phase A 的首配账号与口令（默认见下）。
#                   A4 在容器内走 README 的 headless 路径 `CLI init`；Phase B/C 继续用
#                   旧部署的明文两键兼容路径。
#   GATE_PIP_INDEX_URL  构建期 pip 索引。**默认不传**——与 README 的
#                   `docker compose up -d --build` 同形态（走默认索引）。国内主机直连
#                   pypi 不可达时由 caller 显式传镜像旁路，例如
#                   GATE_PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
#   GATE_OLD_REF    迁移演练的旧版本 ref（默认 tag v0.6.2）。用不可变 tag 而不是
#                   分支：分支会被删（实测 `release/0.6.2` 于 2026-10-17 被删，
#                   B0 随即无法解析），tag 的内容与 v0.6.2 发布树一致（schema v21）。
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
#   * 宿主侧权限模型跟随 README：交接口令文件用 `scripts/gate/handoff.sh`。root 直调
#     chown，非 root 走免密 sudo（GitHub 托管 runner 的形态）。两者都不行时 C 阶段
#     的备份项显性 SKIP，不当绿。
# ============================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$BASE"

# 交接口令文件的权限逻辑住在 gate/handoff.sh（判据跑同一份真函数）。
# 源不到就当场停：缺函数时 C 阶段会退化成 SKIP，那是一次"跳过冒充通过"。
# shellcheck source=gate/handoff.sh
. "$SCRIPT_DIR/gate/handoff.sh"
command -v handoff_to_uid10001 >/dev/null 2>&1 || {
    echo "deploy_gate: 缺少 scripts/gate/handoff.sh 或其中未定义 handoff_to_uid10001" >&2
    exit 2
}

GATE_TMP="${GATE_TMP:-$BASE/work/deploy-gate}"
GATE_PROJECT="${GATE_PROJECT:-yiban-gate}"
GATE_HTTP_PORT="${GATE_HTTP_PORT:-18080}"
GATE_HTTPS_PORT="${GATE_HTTPS_PORT:-18443}"
GATE_SEED_N="${GATE_SEED_N:-5000}"
GATE_ADMIN_USER="${GATE_ADMIN_USER:-gateadmin}"
GATE_ADMIN_PASSWORD="${GATE_ADMIN_PASSWORD:-XwxfGate2026Ab}"
GATE_PIP_INDEX_URL="${GATE_PIP_INDEX_URL:-}"
GATE_OLD_REF="${GATE_OLD_REF:-v0.6.2}"
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

# 先 build（up 不接受 --build-arg）再 up；日志由调用方重定向。
# 默认**不传** PIP_INDEX_URL：与 README 的 `docker compose up -d --build` 同形态
# （docker/Dockerfile 的 `${PIP_INDEX_URL:+-i "$PIP_INDEX_URL"}` 缺省即走默认索引）。
# 国内主机由 caller 显式传 GATE_PIP_INDEX_URL 走镜像旁路（见脚本头）。
dc_up_build() {
    if [ -n "$GATE_PIP_INDEX_URL" ]; then
        dc build --build-arg "PIP_INDEX_URL=$GATE_PIP_INDEX_URL" && dc up -d
    else
        dc build && dc up -d
    fi
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

# prepare_env <data_dir> <certs_dir> [write_admin] : 造数据目录 + .env + 证书
# README 第 2 步已改为「首次打开 /setup 引导创建」或 headless `CLI init`，模板不再带
# 管理员键。Phase A 传 0，由 A4 在容器内跑 `CLI init`；Phase B/C 传 1（缺省），
# 继续覆盖旧部署的明文管理员兼容路径。
prepare_env() {
    local data="$1" certs="$2" write_admin="${3:-1}"
    rm -rf "$data" "$certs"
    mkdir -p "$data/logs" "$data/state" "$certs"
    cp "$BASE/.env.docker.example" "$data/.env"
    if [ "$write_admin" = 1 ]; then
        printf 'YIBAN_ADMIN_USER=%s\nYIBAN_ADMIN_PASSWORD=%s\n' \
            "$GATE_ADMIN_USER" "$GATE_ADMIN_PASSWORD" >> "$data/.env"
    fi
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

# 存储模块的**镜像代次**映射：唯一事实源（工单 e0ro）。
# Phase A/C 跑本树构建的新镜像（`yiban.adapters.persistence`）；Phase B 的 B3 跑
# v0.6.2 旧镜像（只有旧 store 包）。这段 Python 在**别人的镜像**里执行，故
# import 不能只写一侧——迁移只看本树读者就会把另一侧改坏（T6 实测：B3/B6/B8/B11 连锁红）。
store_module() { # $1 = new|old；输出该代镜像的存储包路径
    case "${1:-}" in
        new) printf 'yiban.adapters.persistence\n' ;;
        old) printf 'yiban.store\n' ;;
        *) echo "store_module: 未知镜像代次 '${1:-}'（只认 new|old）" >&2; return 2 ;;
    esac
}

py_db_import() { # $1 = new|old；输出可拼进 `python -c` 的 import 语句
    local mod
    mod="$(store_module "$1")" || return 2
    printf 'from %s import db;' "$mod"
}

seed_sql_py() { # 灌合成数据：n 个账号 + 若干审计行 [+ 一条旧出口行]
    # $1 = plain|legacy；$2 = 镜像代次 new|old（**必填**：同一函数在两代镜像里执行）
    local mode="${1:-plain}" gen="${2:?seed_sql_py 缺少镜像代次（new|old）}" mod
    mod="$(store_module "$gen")" || return 2
    cat <<PY
import sys
from ${mod} import db, accounts
db.init_db()
print("STORE_MODULE", db.__name__)
n = $GATE_SEED_N
rows = [{"name": "seed%05d" % i, "phone": "13%09d" % i,
         "password": "seedpass", "status": "active", "owner": "admin"}
        for i in range(n)]
accounts.replace_accounts(rows)
for i in range(20):
    db.audit("gate-seed", "seed_audit", "t%d" % i, "row %d" % i)
$([ "$mode" = legacy ] && echo "db.get_conn().execute(\"INSERT OR REPLACE INTO egress_state (egress, rate, burst, tat, updated_at) VALUES ('worker-0@gatehost', 1.0, 0, 0, '2026-01-01 00:00:00')\"); db.get_conn().commit()")
print("ACCOUNTS=", db.get_conn().execute("SELECT COUNT(*) FROM accounts").fetchone()[0])
print("AUDIT=", db.get_conn().execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0])
PY
}

# 读灌数脚本自报的实际模块名，与所在镜像代次对账：单向改写会在这里当场红。
seed_module_of() { # $1 = 灌数输出；输出 STORE_MODULE 行里的模块名（缺失即空）
    printf '%s\n' "$1" | sed -n 's/^STORE_MODULE //p' | head -1
}

# ============================================================
# Phase A
# ============================================================
phase_A() {
    hdr "Phase A：干净部署（README Docker 路径）"
    local proj="${GATE_PROJECT}-a" data="$GATE_TMP/data-a" certs="$GATE_TMP/certs-a"
    local ov="$GATE_TMP/override-a.yml" log="$LOG_DIR/phase-a.log"
    CUR_PROJ="$proj"; CUR_OVERRIDE="$ov"
    prepare_env "$data" "$certs" 0
    # A0：模板不得自带管理员键或明文口令——首配只走 /setup 或 CLI init。
    if ! grep -qE '^YIBAN_ADMIN_(USER|PASSWORD|PASSWORD_HASH)=' "$data/.env"; then
        pass "A0 模板不含管理员键（首配走 /setup 或 CLI init）" "零管理员键"
    else
        fail "A0 模板不含管理员键" "模板仍带管理员键：$(grep -E '^YIBAN_ADMIN_' "$data/.env" | tr '\n' ' ')"
        cleanup_proj "$proj" "$ov"; return
    fi

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
    code="$(curl -sk -o /dev/null -w '%{http_code}' --max-time 15 "https://127.0.0.1:${GATE_HTTPS_PORT}/setup")"
    if [ "$code" = "200" ]; then pass "A3 未配置管理员时 /setup 200（首次引导）" "200"; else fail "A3 GET /setup" "HTTP $code"; fi
    code="$(curl -sk -o /dev/null -w '%{http_code}' --max-time 15 "https://127.0.0.1:${GATE_HTTPS_PORT}/login")"
    if [ "$code" = "302" ]; then pass "A3b 未配置管理员时 /login 302 到 /setup" "302"; else fail "A3b GET /login" "HTTP $code（期望 302）"; fi

    # A4：README 的 headless 首配路径——容器内跑 CLI init，口令只走环境变量。
    local iout irc
    iout="$(dc exec -T -u 10001 -e "YIBAN_ADMIN_PASSWORD=${GATE_ADMIN_PASSWORD}" \
        yiban python3 -m yiban.entry.cli init --username "${GATE_ADMIN_USER}" --json 2>>"$log")"
    irc=$?
    printf '%s\n' "$iout" >> "$log"
    if [ "$irc" = 0 ] && printf '%s' "$iout" | grep -q '"ok": true'; then
        pass "A4 容器内 CLI init 创建管理员" "rc=0"
    else
        fail "A4 CLI init" "rc=$irc 输出：$(printf '%s' "$iout" | tail -c 200)"
    fi
    # .env 已被 entrypoint chown 给容器内 uid 10001，宿主用户读不到；A5 在容器内读。
    if dc exec -T -u 10001 yiban python3 -c 'import pathlib,sys; t=pathlib.Path("/data/.env").read_text(); u=sys.argv[1]; sys.exit(0 if ("YIBAN_ADMIN_USER="+u+"\n") in t and "\nYIBAN_ADMIN_PASSWORD_HASH=scrypt:" in ("\n"+t) and "\nYIBAN_ADMIN_PASSWORD=" not in ("\n"+t) else 1)' "$GATE_ADMIN_USER"; then
        pass "A5 .env 写入账号与 scrypt 哈希、无明文口令" "哈希就位"
    else
        fail "A5 .env 管理员写入形状" "见 A4 日志"
    fi
    code="$(curl -sk -o /dev/null -w '%{http_code}' --max-time 15 "https://127.0.0.1:${GATE_HTTPS_PORT}/login")"
    if [ "$code" = "200" ]; then pass "A6 GET /login（已配置管理员）" "200"; else fail "A6 GET /login" "HTTP $code"; fi

    local lresp lcode
    lresp="$(curl -sk -o /dev/null -w '%{http_code}' --max-time 15 \
        -H 'Content-Type: application/json' \
        -d "{\"username\":\"${GATE_ADMIN_USER}\",\"password\":\"${GATE_ADMIN_PASSWORD}\"}" \
        "https://127.0.0.1:${GATE_HTTPS_PORT}/api/login")"
    lcode="$lresp"
    if [ "$lcode" = "200" ]; then pass "A7 管理员登录 /api/login" "200"; else fail "A7 管理员登录" "HTTP $lcode"; fi

    local out seed_mod want_mod got
    out="$(c_in_py "$proj" "$ov" "$(seed_sql_py plain new)" 2>>"$log")"
    echo "$out" >> "$log"
    got="$(printf '%s\n' "$out" | sed -n 's/^ACCOUNTS= //p' | head -1)"
    seed_mod="$(seed_module_of "$out")"
    want_mod="$(store_module new).db"
    # 守卫（工单 e0ro）：灌数脚本实际导入的模块必须属于**新镜像**。单向改写（把
    # import 改回只存在于旧镜像的模块）会在这里红，而不是等到 B3 连锁红。
    if [ "${got:-0}" = "$GATE_SEED_N" ] && [ "$seed_mod" = "$want_mod" ]; then
        pass "A8 灌合成账号并计数" "accounts=$got module=$seed_mod"
    else
        fail "A8 灌合成账号" "期望 $GATE_SEED_N 实得 ${got:-?}；STORE_MODULE=${seed_mod:-?}（期望 $want_mod）"
    fi

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
        # 解析旧版 ref：CI 检出（actions/checkout）通常只带 origin/* 远程跟踪引用，没有
        # 同名本地分支，故 `git archive <分支名>` 在 CI 上会失败。先试给定 ref，
        # 失败再试 origin/<ref>；两处都不在才报错（并提示可设 GATE_OLD_TREE 预导出）。
        local old_ref="$GATE_OLD_REF"
        if ! git rev-parse --verify --quiet "${old_ref}^{commit}" >/dev/null 2>&1; then
            if git rev-parse --verify --quiet "origin/${old_ref}^{commit}" >/dev/null 2>&1; then
                old_ref="origin/${old_ref}"
            fi
        fi
        rm -rf "$old_tree"; mkdir -p "$old_tree"
        if ! git archive "$old_ref" | tar -x -C "$old_tree"; then
            fail "B0 导出旧版代码树" "git archive $GATE_OLD_REF（试过 origin/$GATE_OLD_REF）失败（可设 GATE_OLD_TREE 预导出）"; return
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

    local out accounts_before head_before seed_mod want_mod
    out="$(c_in_py "$proj" "$ov" "$(seed_sql_py legacy old)" 2>>"$log")"; echo "$out" >>"$log"
    accounts_before="$(printf '%s\n' "$out" | sed -n 's/^ACCOUNTS= //p' | head -1)"
    seed_mod="$(seed_module_of "$out")"
    want_mod="$(store_module old).db"
    head_before="$(c_in_py "$proj" "$ov" "$(py_db_import old) db.init_db(cleanup=False, migrate=False); print('HEAD', db.audit_head_hash())" 2>>"$log")"
    echo "$head_before" >>"$log"
    # 守卫（工单 e0ro）：B3 在 **v0.6.2 旧镜像**里执行，实际导入必须是旧模块。
    if [ "${accounts_before:-0}" = "$GATE_SEED_N" ] && [ "$seed_mod" = "$want_mod" ]; then
        pass "B3 旧版灌数（账号+审计+旧出口行）" "accounts=$accounts_before module=$seed_mod"
    else
        fail "B3 旧版灌数" "accounts=${accounts_before:-?}；STORE_MODULE=${seed_mod:-?}（期望 $want_mod）"
    fi

    # --- B4 升级到 develop 顶端 ---
    _write_mig_override "${proj}-new" "$BASE"
    echo "-- 升级到 develop 顶端（同数据卷，镜像重建）"
    if dc_up_build >>"$log" 2>&1 && wait_healthy "$proj"; then
        pass "B4 升级后容器健康" "healthy"
    else
        fail "B4 升级后健康" "见 $log"; dc logs --tail 60 >>"$log" 2>&1; cleanup_proj "$proj" "$ov"; return
    fi

    local uv_after uv_expect
    uv_after="$(c_uv "$proj" "$ov" 2>>"$log")"
    # 期望值**从候选镜像自己的迁移登记表取**，不写死数字：新增迁移（v23）时本闸门
    # 自动跟上，不需要人记得改两处（旧写法写死 22，v23 一落地 CI 必红）。
    uv_expect="$(c_in_py "$proj" "$ov" "from yiban.adapters.persistence.migrations import _MIGRATIONS; print(max(m[0] for m in _MIGRATIONS))" 2>>"$log")"
    if [ -n "${uv_expect:-}" ] && [ "$uv_after" = "$uv_expect" ]; then
        pass "B5 迁移已自动执行（user_version=$uv_after）" "$uv_after"
    else
        fail "B5 迁移" "实得 $uv_after（期望 ${uv_expect:-取不到登记表}）"
    fi

    local accounts_after egress_left chain
    accounts_after="$(c_in_py "$proj" "$ov" "import sqlite3;print(sqlite3.connect('/data/yiban.db').execute('SELECT COUNT(*) FROM accounts').fetchone()[0])" 2>>"$log")"
    if [ "${accounts_after:-0}" = "$GATE_SEED_N" ]; then pass "B6 升级前后账号数不变" "$accounts_before=$accounts_after"; else fail "B6 账号数守恒" "$accounts_before→${accounts_after:-?}"; fi

    egress_left="$(c_in_py "$proj" "$ov" "import sqlite3;print(sqlite3.connect('/data/yiban.db').execute(\"SELECT COUNT(*) FROM egress_state WHERE egress='worker-0@gatehost'\").fetchone()[0])" 2>>"$log")"
    if [ "${egress_left:-1}" = "0" ]; then pass "B7 v22 清空旧出口行（egress_state 重建）" "旧键剩 0 行"; else fail "B7 v22 清空旧出口行" "旧键剩 ${egress_left:-?} 行"; fi

    chain="$(c_in_py "$proj" "$ov" "$(py_db_import new) db.init_db(cleanup=False, migrate=False); print('CHAIN', db.verify_audit_chain()[0])" 2>>"$log")"
    echo "$chain" >>"$log"
    if printf '%s' "$chain" | grep -q 'CHAIN True'; then pass "B8 升级后审计链自洽 chain_ok=True" "True"; else fail "B8 审计链" "$chain"; fi

    local egrc exits
    c_in_py "$proj" "$ov" "import sys; from yiban.entry import cli; sys.exit(cli.main(['egress','--status','--json']))" >>"$log" 2>&1
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
        pass "B10 回滚（旧版代码 on 迁移后库）容器健康" "healthy"
    else
        fail "B10 回滚后健康" "见 $log"; dc logs --tail 60 >>"$log" 2>&1; cleanup_proj "$proj" "$ov"; return
    fi
    local uv_rb accounts_rb
    uv_rb="$(c_uv "$proj" "$ov" 2>>"$log")"
    accounts_rb="$(c_in_py "$proj" "$ov" "import sqlite3;print(sqlite3.connect('/data/yiban.db').execute('SELECT COUNT(*) FROM accounts').fetchone()[0])" 2>>"$log")"
    # 回滚**不得降 schema**：与 B5 测到的版本比，而不是写死某个数字（旧版代码
    # 不认新列也不回退 user_version，故迁移后的版本必须原样保留）。
    if [ "$uv_rb" = "$uv_after" ] && [ "${accounts_rb:-0}" = "$GATE_SEED_N" ]; then
        pass "B11 回滚不降 schema 且数据完整" "uv=$uv_rb accounts=$accounts_rb"
    else
        fail "B11 回滚 schema/数据" "uv=$uv_rb（期望 $uv_after）accounts=${accounts_rb:-?}"
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
    # 先清日志：下面的交接若失败，错误详情要留下来（后面不再截断）。
    : > "$log"
    # 先删旧文件：上一轮的归属是 uid 10001（0600），本用户写不进去。
    # 目录属主是调用者，unlink 不需要提权。
    rm -f "$pass"
    printf 'gate-backup-pass\n' > "$pass"
    chmod 600 "$pass"
    # README 口径：口令文件必须对容器内 yiban（uid 10001）可读。调度器以该身份调备份脚本，
    # 这里按同一身份、同一权限模型演练（否则会在 root 身份下"假通过"）。
    # README 第 6 节的原命令带 sudo；交付走 handoff.sh，覆盖 root 与非 root 两种模型。
    local own_ok=1 own_why=""
    if ! handoff_to_uid10001 "$pass" 2>>"$log"; then
        own_ok=0
        own_why="宿主 uid=$(id -u) 既非 root 也无免密 sudo，无法按 README 第 6 节把口令文件交给 uid 10001"
        echo "  (skip) $own_why" >&2
    fi
    write_override "$ov" "$proj" "$data" "$certs" "$http" "$https" "$pass"

    if dc_up_build >>"$log" 2>&1 && wait_healthy "$proj"; then
        pass "C1 起全栈（启用容器备份口令挂载）" "healthy"
    else
        fail "C1 起全栈" "见 $log"; cleanup_proj "$proj" "$ov"; return
    fi
    # 口令文件以 uid 10001 身份可读（README 启用两步的验收面）
    if [ "$own_ok" != 1 ]; then
        # 跳过 ≠ 绿（AGENTS §十三 第 30 条）：SKIP 行进摘要表，理由写明。
        skip "C1b 口令文件对 uid 10001 可读" "$own_why（判据未执行）"
    elif dc exec -T -u 10001 yiban sh -c 'test -r /run/yiban-secrets/backup-passphrase' >>"$log" 2>&1; then
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
    if [ "$own_ok" != 1 ]; then
        # 口令读不到，容器内备份脚本必然 rc=2。后续每一项都测不到，整段显性 SKIP，
        # 不拿"环境不支持"去冒充产品缺陷。
        skip "C2–C8 容器备份/恢复演练" "口令文件权限未建立，容器内备份脚本读不到口令（同 C1b）"
        cleanup_proj "$proj" "$ov"
        return
    fi
    c_in_py "$proj" "$ov" "$(seed_sql_py plain new)" >>"$log" 2>&1
    # 落一次审计链外部锚点（README 承诺备份含 audit-anchor.log）：锚点由每日线程/事件写，
    # 全新库"首次备份前"可能尚未存在，故先显式落一次，再断言包内确实含它。
    c_in_py "$proj" "$ov" "$(py_db_import new) db.init_db(); db.record_audit_anchor()" >>"$log" 2>&1

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
    chain="$(c_in_py "$proj" "$ov" "$(py_db_import new) db.init_db(cleanup=False, migrate=False); print('CHAIN', db.verify_audit_chain()[0])" 2>>"$log")"
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
        # 未知阶段名必须判红，不得静默跳过：`--phase Q` 若只打一行提示、退出码仍为 0，
        # 就是一次"什么都没跑却报 PASS"的假绿（发布判据的守卫不许有这种口子）。
        *) fail "阶段名未知（--phase $p）" "允许值 A/B/C" ;;
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
