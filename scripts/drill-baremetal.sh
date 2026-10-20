#!/usr/bin/env bash
# ============================================================
# drill-baremetal.sh —— 测试机「裸机形态 e2e 演练」
# ============================================================
# 对齐 docs/dev/release-gate.md §2④（发布门槛 = 裸机 smoke + 备份可恢复演练）
# 与 §3「有效轮次」的三行判据。容器形态由 scripts/drill-container.sh 覆盖；
# 本脚本只走宿主/裸机路径，两者验证的链路不同，不重复。
#
# 与生产形态的对应关系（这是本演练的价值所在）：
#   生产 = systemd + .venv + cron 调 run.sh；测试机本地演练用同一套
#   Python（见 requirements.lock）、同一份 scripts/backup.sh。
#   差异只有进程管理器（cron/systemd vs 手工跑）——那是部署面，不是代码面。
#
# 判据（任一不过即 FAIL）：
#   B1 依赖：requirements.lock 装得进干净 venv（可复现装配）
#   B2 配置自检：scripts/signin.py --check-config 退出码 0，且手机号已脱敏
#   B3 假上游 e2e：tests/test_login_e2e_mock.py 全绿（零外联）
#   B4 备份：scripts/backup.sh 产出密文归档，回环自检通过
#   B5 恢复演练：scripts/backup.sh --restore 退出码 0
#   B6 整轮签到：run.sh 真跑一轮，日志三行齐（横幅带版本 / 汇总 0 失败 / 退出码 0）
#
# 用法（在演练树根目录、docker 不需要）：
#   ./scripts/drill-baremetal.sh
#
# 离线可解析性由 tests/test_release_gate_scripts.py 钉住（脚本必须入库且过 bash -n）。
#
# 退出码：0 全绿；1 某判据失败；2 前提不满足。
# ============================================================
set -Eeuo pipefail

ROOT="$(pwd -P)"
PY="$ROOT/.venv-bare/bin/python3"
PIP="$ROOT/.venv-bare/bin/pip"
EV=""
STEP="init"

die() { printf '\n[FAIL] %s：%s\n' "$STEP" "$*" >&2; exit 1; }
pre() { printf '\n[FAIL] 前提不满足：%s\n' "$*" >&2; exit 2; }
step() { STEP="$1"; printf '\n──── [%s] %s ────\n' "$(date +%H:%M:%S)" "$1"; }
note() { printf '       · %s\n' "$*"; }

STEP="P0"
[ -f "$ROOT/run.sh" ] || pre "不在演练树根目录（缺 run.sh）"
[ -x "$PY" ] || pre "缺裸机 venv：$PY"
# 本演练按**生产布局**摆盘：APP_DIR 就是演练树根，`.env` 与 `yiban.db` 放在
# `data/` 下（`backup.sh` 只认 `${APP_DIR}/data/.env`；库路径由该 .env 的
# `YIBAN_DB_FILE` 给出）。故这里不放"根下不得有 .env"的红线，改成断言这两个
# 文件是**本次自造**的合成件、不含任何真实凭据。
if [ -e "$ROOT/data/.env" ] && ! grep -q '裸机演练专用' "$ROOT/data/.env" 2>/dev/null; then
  pre "$ROOT/data/.env 已存在且不是本次合成件 —— 疑似携带真实凭据，拒绝在它上面跑演练"
fi
if [ -d "$ROOT/data/yiban.db" ]; then pre "data/yiban.db 是目录，树不对"; fi

EV="work/evidence-bare-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$EV"
# run.sh 与 backup.sh 都按 $APP_DIR/.venv/bin/python 找解释器（找不到才退回 PATH 上的
# python3，而系统 python3 没有 pycryptodome → 恢复核验会"无法定论"）。第一步就建好软链。
[ -e "$ROOT/.venv" ] || ln -s "$ROOT/.venv-bare" "$ROOT/.venv"
WORK="$ROOT/bare-data"          # 状态 / 日志 / 备份落点（`<APP_DIR>/data/.env` 与
                                # `<APP_DIR>/data/yiban.db` 是 backup.sh 的固定读取位置）

# 路径形状只认 bash 侧定义点 `scripts/lib/host_paths.sh`（census-P2-log-file /
# repo-root）：本演练按生产布局摆盘，故 APP_DIR 就是演练树根。导出三个环境键，
# 让下面的函数与 run.sh 看到同一份事实（此前这里手写了 `sign-<date>.log` 与
# `mock.jsonl`，与刚立的单一定义点相撞）。
APP_DIR="$ROOT"
export YIBAN_APP_DIR="$ROOT"
export YIBAN_LOG_FILE="$WORK/logs/sign.log"
export YIBAN_STATE_DIR="$WORK/state"
# shellcheck source=scripts/lib/host_paths.sh
source "$ROOT/scripts/lib/host_paths.sh"
if ! TRACE_BASENAME="$("$PY" -c 'import sys; sys.path.insert(0, sys.argv[1]); from yiban.adapters.protocol_fake import TRACE_BASENAME; print(TRACE_BASENAME)' "$ROOT")"; then
  die "无法从 protocol_fake 取得轨迹基名（TRACE_BASENAME）"
fi
[ -n "$TRACE_BASENAME" ] || die "protocol_fake.TRACE_BASENAME 为空"
rm -rf "$WORK"; mkdir -p "$WORK/state" "$WORK/logs" "$WORK/backups" "$ROOT/data"
rm -f "$ROOT/data/.env" "$ROOT/data/yiban.db"

printf 'tree=%s\nstarted=%s\n' "$ROOT" "$(date -Is)" > "$EV/run.meta"

# ---- B1 依赖装配 ----
step "B1 依赖：requirements.lock 装进干净 venv"
"$PY" -V > "$EV/b1-python.txt" 2>&1
"$PIP" freeze > "$EV/b1-frozen.txt" 2>&1
"$PIP" check > "$EV/b1-pipcheck.txt" 2>&1 || die "pip check 报依赖冲突（见 b1-pipcheck.txt）"
grep -qi '^flask==' "$EV/b1-frozen.txt" || die "venv 里没有 flask，装配不完整"
grep -qi '^gunicorn==' "$EV/b1-frozen.txt" || die "venv 里没有 gunicorn"
grep -qi '^portalocker==' "$EV/b1-frozen.txt" || die "venv 里没有 portalocker（跨进程锁真源）"
note "$(cat "$EV/b1-python.txt")；已装 $(wc -l < "$EV/b1-frozen.txt") 个包；pip check 通过"

# ---- 造数：自造账号 + 合成窗口（与容器演练同口径，密钥自造）----
step "B0 造数（自造账号 + 合成窗口，密钥自造）"
NOW_S=$(date +%s)
export DRILL_TREE="$ROOT" DRILL_N=3 DRILL_WS=$((NOW_S + 30)) DRILL_WE=$((NOW_S + 30 + 480))
"$PY" - > "$EV/b0-seed.log" 2>&1 <<'PY' || { cat "$EV/b0-seed.log" >&2; die "造数失败"; }
import contextlib, datetime, os, sys
TREE = os.environ["DRILL_TREE"]; N = int(os.environ["DRILL_N"])
DB, ENV = TREE + "/data/yiban.db", TREE + "/data/.env"
sys.path.insert(0, TREE + "/scripts")
os.environ["YIBAN_ENV_FILE"] = ENV
os.environ["YIBAN_DB_FILE"] = DB
os.umask(0o77)
start = datetime.datetime.fromtimestamp(int(os.environ["DRILL_WS"]))
end = datetime.datetime.fromtimestamp(int(os.environ["DRILL_WE"]))
assert start.date() == end.date(), "合成窗口不得跨日历日"
open(ENV, "w", encoding="utf-8").write("# 裸机演练专用 .env（自造，严禁入库）\n")
import db
from yiban.adapters.config import env_io
db.init_db(DB, env_file=ENV, cleanup=False, migrate=True)
_conn = db.get_conn(); _day = datetime.date.today().isoformat()
with db._conn_lock:
    _conn.execute("DELETE FROM sign_tasks WHERE day=?", (_day,)); _conn.commit()
for i in range(N):
    email = "drillbare%05d@mock.invalid" % i
    with contextlib.suppress(Exception):
        db.create_user(email, "mock-hash", role="user")
    with contextlib.suppress(Exception):
        db.add_account({"name": "drillbare%05d" % i, "phone": "1380013800%d" % i,
                        "password": "drill-pass-%d" % i, "phone_model": "MockPhone",
                        "phone_code": "", "owner": email, "status": "active",
                        "reject_reason": ""})
_audit_key = os.urandom(32).hex()
os.environ["YIBAN_AUDIT_KEY"] = _audit_key   # 写锚点的函数从进程环境取这把键
env_io.write_env_keys(ENV, {
    # 审计链密钥：恢复演练里 backup.sh --restore 会跑 audit_verify.py，
    # 缺这把键它只能报"无法定论"（退出码 2）。生产 .env 里本来就有，演练必须补上。
    "YIBAN_AUDIT_KEY": _audit_key,
    "YIBAN_SIGN_START": start.strftime("%H:%M"), "YIBAN_SIGN_END": end.strftime("%H:%M"),
    "YIBAN_START_DELAY_MAX": "0", "YIBAN_WINDOW_EDGE_FRONT_SEC": "0",
    "YIBAN_WINDOW_EDGE_BACK_SEC": "0", "YIBAN_GLOBAL_PAUSE": "0",
    "YIBAN_ACCOUNT_GAP_MAX": "2", "YIBAN_SIGN_ORDER": "sequence", "YIBAN_SIGN_DIST": "uniform",
    "YIBAN_SATURDAY_SIGN": "1", "YIBAN_SUNDAY_SIGN": "1",
    "YIBAN_STATE_DIR": TREE + "/bare-data/state",
    "YIBAN_LOG_FILE": TREE + "/bare-data/logs/sign.log",
    "YIBAN_DB_FILE": DB,
    "YIBAN_ACCOUNTS_FILE": TREE + "/bare-data/accounts.json",
})
print("WINDOW", start.strftime("%H:%M:%S"), "-", end.strftime("%H:%M:%S"))
print("ACCOUNTS", len(db.load_accounts()))
# 审计链外部锚点：生产由 web 每日线程写 <STATE_DIR>/audit-anchor.log。演练里没有那一步，
# 而 backup.sh --restore 会拿它与包内锚点比对、缺锚点即"无法定论"（退出码 2）。
# 这里直接调同一个写者补齐，让恢复演练覆盖真实判据而不是绕过它。
_anchor = os.path.join(TREE + "/bare-data/state", "audit-anchor.log")
# record_audit_anchor 对**空链**拒写（宁缺毋滥：少一个 token 的行会让后续解析整体错位）。
# 生产每日线程写锚点前总有审计行，演练也必须先落一条，否则锚点恒缺、恢复演练永远"无法定论"。
db.audit("drill", "drill_seed", "drill", "裸机演练造数：为锚点写一条合成审计行")
written = db.record_audit_anchor(_anchor)
print("ANCHOR_ROW", repr(written)[:80])
print("ANCHOR_WRITTEN", os.path.isfile(_anchor))
assert os.path.isfile(_anchor), "锚点未写出——record_audit_anchor 未生效"
PY
grep -qx "ACCOUNTS 3" "$EV/b0-seed.log" || { cat "$EV/b0-seed.log" >&2; die "造数账号数不是 3"; }
grep -qx "ANCHOR_WRITTEN True" "$EV/b0-seed.log" || { cat "$EV/b0-seed.log" >&2; die "审计锚点未写出"; }
note "$(grep ^WINDOW "$EV/b0-seed.log")；账号 3 个 @mock.invalid"

# ---- B2 配置自检（脱敏）----
step "B2 配置自检：signin.py --check-config"
set +e
YIBAN_ENV_FILE="$ROOT/data/.env" YIBAN_DB_FILE="$ROOT/data/yiban.db" \
  "$PY" scripts/signin.py --check-config > "$EV/b2-check-config.txt" 2>&1
RC=$?
set -e
cat "$EV/b2-check-config.txt" >> "$EV/b2-check-config.txt".full 2>/dev/null || true
[ "$RC" -eq 0 ] || { cat "$EV/b2-check-config.txt" >&2; die "--check-config 退出码 $RC"; }
if grep -qE '1[3-9][0-9]{9}' "$EV/b2-check-config.txt"; then
  die "--check-config 输出里出现完整手机号（脱敏失效），见 b2-check-config.txt"
fi
note "退出码 0；输出无完整手机号（脱敏有效）"

# ---- B3 假上游 e2e ----
step "B3 假上游 e2e：tests/test_login_e2e_mock.py（零外联）"
"$PY" -m pytest tests/test_login_e2e_mock.py -q -p no:randomly > "$EV/b3-e2e.log" 2>&1 \
  || { tail -30 "$EV/b3-e2e.log" >&2; die "e2e smoke 非全绿（见 b3-e2e.log）"; }
note "$(tail -1 "$EV/b3-e2e.log")"

# ---- B4 备份（密文 + 回环自检）----
step "B4 备份：scripts/backup.sh（密文 + 回环自检）"
set +e
APP_DIR="$ROOT" BACKUP_DIR="$WORK/backups" \
  YIBAN_STATE_DIR="$WORK/state" YIBAN_LOG_FILE="$WORK/logs/sign.log" \
  BACKUP_GPG_PASSPHRASE="drill-only-passphrase" \
  bash scripts/backup.sh > "$EV/b4-backup.log" 2>&1
RC=$?
set -e
[ "$RC" -eq 0 ] || { tail -30 "$EV/b4-backup.log" >&2; die "backup.sh 退出码 $RC（见 b4-backup.log）"; }
PKG="$(ls -1t "$WORK/backups"/yiban-*.tar.gz.gpg 2>/dev/null | head -1)"
[ -n "$PKG" ] || { ls -la "$WORK/backups" >&2; die "未见密文归档产物"; }
note "产物 $(basename "$PKG")；$(grep -c . "$EV/b4-backup.log") 行日志，退出码 0"

# ---- B5 恢复演练（release-gate §2④ 的固定门槛）----
step "B5 恢复演练：backup.sh --restore（退出码必须是 0）"
REST="$ROOT/bare-restore"
rm -rf "$REST"
set +e
APP_DIR="$ROOT" BACKUP_DIR="$WORK/backups" \
  YIBAN_STATE_DIR="$WORK/state" YIBAN_LOG_FILE="$WORK/logs/sign.log" \
  BACKUP_GPG_PASSPHRASE="drill-only-passphrase" \
  bash scripts/backup.sh --restore "$PKG" "$REST" > "$EV/b5-restore.log" 2>&1
RC=$?
set -e
[ "$RC" -eq 0 ] || { tail -35 "$EV/b5-restore.log" >&2; die "恢复演练退出码 $RC（6/7/8 各自含义见 backup.sh 头注释）"; }
find "$REST" -type f > "$EV/b5-restored-files.txt"
grep -q 'integrity_check=ok' "$EV/b5-restore.log" || die "恢复件未报告 integrity_check=ok"
grep -qE 'audit_verify|审计链' "$EV/b5-restore.log" || die "恢复件未跑审计核验"
note "退出码 0；恢复 $(wc -l < "$EV/b5-restored-files.txt") 个文件；integrity_check=ok"

# ---- B6 整轮签到（run.sh 真跑，假上游 + 自签 TLS）----
step "B6 整轮签到：run.sh + 假上游（真签到链路）"
DOMAINS="oauth.yiban.cn f.yiban.cn api.uyiban.com c.uyiban.com app.uyiban.com"
CADIR="$WORK/ca"; mkdir -p "$CADIR"
SAN="$CADIR/san.ext"
{ printf 'subjectAltName='; sep=""; for d in $DOMAINS; do printf '%sDNS:%s' "$sep" "$d"; sep=","; done; printf '\nextendedKeyUsage=serverAuth\n'; } > "$SAN"
openssl req -x509 -newkey rsa:2048 -sha256 -days 2 -nodes -keyout "$CADIR/ca.key" -out "$CADIR/ca.pem" -subj "/CN=yiban-bare-ca" > "$EV/b6-certs.log" 2>&1
openssl req -newkey rsa:2048 -sha256 -nodes -keyout "$CADIR/server.key" -out "$CADIR/server.csr" -subj "/CN=oauth.yiban.cn" >> "$EV/b6-certs.log" 2>&1
openssl x509 -req -in "$CADIR/server.csr" -CA "$CADIR/ca.pem" -CAkey "$CADIR/ca.key" -CAcreateserial -days 2 -sha256 -extfile "$SAN" -out "$CADIR/server.pem" >> "$EV/b6-certs.log" 2>&1
openssl verify -CAfile "$CADIR/ca.pem" "$CADIR/server.pem" >> "$EV/b6-certs.log" 2>&1 || die "自签证书自验失败"

"$PY" yiban/adapters/protocol_fake/__init__.py --host 127.0.0.1 --port 443 --no-ipv6 \
  --cert "$CADIR/server.pem" --key "$CADIR/server.key" \
  --log "$WORK/logs/$TRACE_BASENAME" --ready-file "$WORK/mock.ready" \
  > "$EV/b6-mock.log" 2>&1 &
MOCK_PID=$!
for _ in $(seq 1 20); do [ -f "$WORK/mock.ready" ] && break; sleep 1; done
[ -f "$WORK/mock.ready" ] || { cat "$EV/b6-mock.log" >&2; die "假上游未就绪"; }

# 五域名指向回环（本机演练；退出时恢复）
HOSTS_BAK="$EV/b6-hosts.bak"
cp /etc/hosts "$HOSTS_BAK"
for d in $DOMAINS; do
  grep -qE "^127\.0\.0\.1[[:space:]]+$d$" /etc/hosts || echo "127.0.0.1 $d" >> /etc/hosts
done
restore_hosts() { cp "$HOSTS_BAK" /etc/hosts; }
trap 'kill "$MOCK_PID" 2>/dev/null || true; restore_hosts' EXIT

set +e
REQUESTS_CA_BUNDLE="$CADIR/ca.pem" SSL_CERT_FILE="$CADIR/ca.pem" CURL_CA_BUNDLE="$CADIR/ca.pem" \
  YIBAN_APP_DIR="$ROOT" \
  YIBAN_ENV_FILE="$ROOT/data/.env" YIBAN_DB_FILE="$ROOT/data/yiban.db" \
  YIBAN_STATE_DIR="$WORK/state" YIBAN_LOG_FILE="$WORK/logs/sign.log" \
  YIBAN_GLOBAL_PAUSE=0 YIBAN_TRIGGER=drill \
  bash run.sh > "$EV/b6-run.log" 2>&1
RUN_RC=$?
set -e
LOG="$(yiban_day_log_file "$WORK/state")"
[ -f "$LOG" ] || { ls -la "$WORK/logs" >&2; cat "$EV/b6-run.log" >&2; die "未生成当日日志 $LOG"; }
cp -f "$LOG" "$EV/b6-sign.log"
grep -qE '==== 开始执行签到（v[0-9.]+）' "$LOG" || { tail -30 "$LOG" >&2; die "缺带版本号的轮次横幅"; }
grep -qF '==== 签到汇总' "$LOG" || { tail -30 "$LOG" >&2; die "缺签到汇总行"; }
grep -qF '❌ 0 失败' "$LOG" || { tail -30 "$LOG" >&2; die "汇总不是 ❌ 0 失败"; }
[ "$RUN_RC" -eq 0 ] || die "run.sh 退出码 $RUN_RC（有效轮次要求 0）"
cp -f "$WORK/logs/$TRACE_BASENAME" "$EV/b6-mock.jsonl" 2>/dev/null || true
note "横幅带版本 / 汇总 ❌ 0 失败 / run.sh 退出码 0"

restore_hosts
kill "$MOCK_PID" 2>/dev/null || true
trap - EXIT

step "结论"
printf 'finished=%s\nresult=PASS\n' "$(date -Is)" >> "$EV/run.meta"
cat <<EOF

  ✅ 裸机形态演练 PASS —— 证据见 $EV/

  B1 依赖      requirements.lock 装入干净 venv（$(cat "$EV/b1-python.txt")），pip check 通过
  B2 配置自检  --check-config 退出码 0，输出无完整手机号
  B3 e2e smoke tests/test_login_e2e_mock.py 全绿（零外联）
  B4 备份      backup.sh 产出密文归档，回环自检通过
  B5 恢复演练  backup.sh --restore 退出码 0，integrity_check=ok + 审计核验
  B6 整轮签到  run.sh 日志三行齐（版本横幅 / 签到汇总 / ❌ 0 失败），退出码 0

  本轮是**演练轮，不是有效轮次**：证据只在 $EV/，不进发布台账。
EOF
