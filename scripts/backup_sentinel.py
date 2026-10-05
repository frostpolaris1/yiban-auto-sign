# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""**功能**
备份失败哨兵：当日备份包缺失时发一封管理员告警，核对"运行脚本 = 仓库脚本"，
并把**当日审计链头哈希随备份外发**（离机锚点，见下）。

备份是**唯一**没有自愈路径的环节：`scripts/backup.sh` 在加密配置失效时 fail-closed
（不产出任何归档），cron 侧仍是正常退出，于是"连续几天没有备份"这件事在任何页面上都
看不出来（生产上曾静默失败 4 天，最后靠手工翻备份目录才发现）。本脚本是这条静默失败
目前**唯一**的自动发现途径，覆盖面止于"当日包与清单在不在、运行脚本是否漂移"：包内容
坏掉、备份脚本自身逻辑错都不在它视野内；它自己没被 cron 调起时同样没人知道。

三项检查：
1. **当日包存在且为密文**：`${BACKUP_DIR:-/var/backups}/yiban-<今天>.tar.gz.gpg` 与它的
   `.sha256` 旁挂件都在（age 密文形态也认，见 `_archive_candidates`）。
   **明文包（裸 `.tar.gz`）不计入健康**（M3 批次0 · MF-79）：backup.sh 只在加密配置
   失效或显式 BACKUP_PLAINTEXT=1 时才产出明文包，把它当"当日备份完成"正是曾经
   4 天静默失败的同型盲区；当日只有明文包 ⇒ 判不健康并走告警路径；
   同日既有密文又有明文残留（加密切换过渡日）⇒ 以密文为准，不双告警。
2. **防漂移**：`$APP_DIR/scripts/backup.sh`（仓库版）与 `$YIBAN_BACKUP_INSTALLED`
   （默认 `/usr/local/sbin/yiban-backup.sh`，cron 实际调的那个）内容一致——"运行脚本
   是仓库脚本的拷贝"是部署约定，判据与它挡的是什么见 `_drift_report`。安静路径上
   两者一致（或安装版不存在）时不输出任何东西。
3. **审计链头哈希随备份外发**（M28，每轮都发，与前两项的成败无关）：见 `_broadcast_anchor`。

**为什么第 3 项与备份"同包同盘"的问题必须另解**：备份把 `audit-anchor.log` 一起打进去，
恢复时锚点与库**同时**回到那个批次——于是"这份库/这个锚点有没有被整体回滚到更早的批次"
在本机是**不可证**的：两者一起旧，链自洽、锚点自洽，什么都查不出来。本机任何自检都
活在同一个权限域里，攻下 `.env` 的 `YIBAN_AUDIT_KEY` 就能在新密钥下把链与锚点一起重写
自洽（M74）。唯一能跨出这个域的既有通道是**告警邮件本身**：外发消息天然在别的机器上、
天然带可信时间戳（收件方的），拿它当"这一天库应该长什么样"的离机基线，恢复后比对即知
是否回滚。因此本项**每天必发**——只在该喊的时候发，等于把"安静的那天正好被回滚"这一格
继续留在盲区里。发信走既有的 `send_admin_alert` 出口（不新造通道、不新建凭据）。

**归属**
运维侧脚本（`scripts/`）。判据所需的路径与命名口径取自 `scripts/backup.sh`（包名、
`.sha256` 旁挂件的唯一事实源是它），本脚本不另立一套命名。

**复用**
收件人算法复用 `web/services/notify_mail.py` 的 `_alert_mail_recipients`（与 A 线告警
邮件同一份：ADMIN_TO 按个人开关过滤 + 开启接收的管理员），发送出口复用
`yiban.mail.send_admin_alert`；节流复用 `yiban/notify/transport.py` 的 `_throttle_due`
（`$YIBAN_STATE_DIR/notify-throttle.json`，跨进程磁盘表——cron 每次新进程，进程内节流表
在这里等于没有节流，故不新造）；链头与记录数读 `yiban.store.db` 的
`audit_head_hash_ex` / `audit_row_count`（只读；不读、不改锚点文件）。

**通信**
用法：`python3 scripts/backup_sentinel.py`（无参数）。
安装与排期见仓库 `scripts/yiban-backup-sentinel.sh` 的头部（安装到
`/usr/local/sbin/yiban-backup-sentinel.sh`，cron 每日 **08:05**，即 02:00 备份之后）。
输入：环境变量 `BACKUP_DIR` / `APP_DIR` / `YIBAN_BACKUP_INSTALLED` / `YIBAN_ENV_FILE` /
`YIBAN_STATE_DIR` / `YIBAN_DB_FILE`（与 `backup.sh`、`run.sh` 同口径）。
输出：结论与原因到 stdout（cron 重定向到日志）；审计链头哈希外发一封邮件（每日一封）；
异常时一封管理员告警邮件。
退出码：`0`=检查完成（含"缺失但告警已发出"，以及**锚点外发失败**——见下）；
`1`=检查本身失败或告警发不出去。
⚠ 外发失败**不**改退出码：哨兵的退出码语义是"检查是否做完"，把"邮件通道当时抖动"
升格成 cron 非 0 只会让每日备份链在日志里天天红，反而淹没真故障。外发失败照样
`print` 到 stderr 并记 `logger.warning`，与本模块头部"没人看得见 cron 退出码"那段
提醒是同一个诚实边界。
⚠ 但别把退出码当成"最后一道声音"：仓库给出的三条排期行（`README.md`、`scripts/backup.sh`、
本脚本头部）都带 `>> /var/log/yiban/backup.log 2>&1`，stderr 被并进日志文件、cron 不发
报错邮件（全仓也没有任何 `MAILTO` 设置）⇒ 退出码 1 实际只在有人翻 backup.log 时才看得见。
真要让"发不出去"这件事自己响，得去掉 `2>&1` 并配 `MAILTO`，或把退出码接进监控。
调用谁：`yiban.mail`、`web.services.notify_mail`、`yiban.notify.transport`、`yiban.store.db`。
谁调用：cron（每日一次）；无其它调用点。
"""
import logging
import os
import sys
from datetime import datetime, timezone
from hashlib import sha256

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from yiban import mail as mailer  # noqa: E402
from yiban.mail import layout as mail_layout  # noqa: E402

logger = logging.getLogger("yiban")

#: 备份目录默认值（与 `scripts/backup.sh` 的 `BACKUP_DIR` 默认一致）
DEFAULT_BACKUP_DIR = "/var/backups"
#: 项目部署目录默认值（与 `scripts/backup.sh` 的 `APP_DIR` 默认一致）
DEFAULT_APP_DIR = "/opt/yiban-auto-sign"
#: 安装拷贝的默认落点（`backup.sh` 头部的安装命令写的就是这个路径）
DEFAULT_INSTALLED = "/usr/local/sbin/yiban-backup.sh"
#: 告警标题：单类型节流的键，也是邮件主题；两处告警共用一条标题——它们是同一件事
#: （"今天的备份没成"）的两个原因，分标题会让节流窗口各算一份。
ALERT_TITLE = "备份失败哨兵告警"
#: 审计锚点外发的标题：**刻意与失败告警分开记账**。共用一条会让"今天备份没成"那封
#: 把当天的离机基线挤掉（节流键即标题），反向亦然——两件事每天都要送达，不能互相挡。
ANCHOR_TITLE = "审计链锚点随备份外发"
#: **健康**归档后缀（只认密文形态），按"部署契约优先"排序：`--require-encrypt` 下的
#: 产物是 `.tar.gz.gpg`（README 的部署命令就带这个标志）；age 同为密文也认——认不出
#: 会在合法部署上误报"没有备份"，那正是本脚本要消灭的那种噪音。
#: 裸 `.tar.gz`（明文）刻意**不在**名单里（M3 批次0 · MF-79）：明文包只可能来自
#: 加密配置失效或显式 BACKUP_PLAINTEXT=1，两种都是需要人知道的异常态。
ARCHIVE_SUFFIXES = (".tar.gz.gpg", ".tar.gz.age")
#: 明文归档后缀：不算健康，但必须被识别并单独告警（否则与"整包缺失"混成同一条
#: 误报，运维会白找加密配置——包其实躺在目录里，只是裸奔）
PLAINTEXT_SUFFIX = ".tar.gz"


def _env(name, default):
    """环境变量优先、空串按未设置处理（与 backup.sh 的 `VAR="${VAR:-默认}"` 同口径）。"""
    value = os.environ.get(name, "").strip()
    return value or default


def _now():
    """当日日期串（`YYYY-MM-DD`）——业务日（`yiban.clock` 北京钟）。

    与 backup.sh 的包名时钟**同源**（MF-109 统一后 backup.sh 的包名也走
    `yiban.clock.today()`）：两边取不同时钟才会在时区非 +08 的服务器上按天错位、
    天天误报"备份缺失"。宿主时区本就是北京时它与 `datetime.now()` 逐日相等。
    """
    from yiban.clock import today
    return today()


def _archive_candidates(backup_dir, day):
    """当日归档的候选路径（按 ARCHIVE_SUFFIXES 顺序），与 `.sha256` 旁挂件同目录。"""
    return [os.path.join(backup_dir, f"yiban-{day}{suffix}") for suffix in ARCHIVE_SUFFIXES]


def _find_archive(backup_dir, day):
    """返回 (归档路径, 旁挂件已就位) 或 (None, False)。

    `.sha256` 与归档同时要求：归档在而清单不在，同样是"不能核验的备份"——恢复流程
    靠清单比对完整性，缺了它等于备份不可信。
    """
    for path in _archive_candidates(backup_dir, day):
        if os.path.isfile(path):
            # 清单缺失单独带出来：不可核验的包等于没有备份，但它是"包在、清单没了"，
            # 与"整个包没生成"的排查方向不同，故不合并成一个布尔
            return path, os.path.isfile(path + ".sha256")
    return None, False


def _find_plaintext(backup_dir, day):
    """当日明文归档（裸 `.tar.gz`）路径；不存在返回 None。

    仅用于把"当日只有明文包"与"什么都没生成"区分开——两种都不健康，但排查方向
    完全不同（前者查加密配置为何失效，后者查 cron/脚本本身）。
    """
    path = os.path.join(backup_dir, f"yiban-{day}{PLAINTEXT_SUFFIX}")
    return path if os.path.isfile(path) else None


def _file_fingerprint(path):
    """文件指纹：内容 sha256 + 字节数 + mtime。

    内容哈希是判据（任何一处改动都能看出来），大小与时间只是给运维的定位线索
    （哪边是旧拷贝、差了多少字节）。
    """
    with open(path, "rb") as f:
        digest = sha256(f.read()).hexdigest()
    stat = os.stat(path)
    return {
        "sha256": digest,
        "size": stat.st_size,
        "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%SZ"),
    }


def _drift_report(repo_copy, installed):
    """两份备份脚本的内容差异；不存在/一致/无法读取时返回 None（安静路径）。

    这一项删不掉：cron 调的是安装拷贝。仓库版修好了而拷贝没跟上时，备份照旧按旧脚本
    跑（旧拷贝曾连续几天静默失败），"当日包缺失"那一项只能在事后才发现这件事。
    """
    if not os.path.isfile(installed):
        # 未按约定安装（自定义路径/单机试用）：无从比对，不报——真正的备份失败由
        # "当日包缺失"那一项兜住，这里多喊只会制造噪音
        return None
    if not os.path.isfile(repo_copy):
        return {"reason": f"仓库版不存在（{repo_copy}）"}
    try:
        repo_fp = _file_fingerprint(repo_copy)
        inst_fp = _file_fingerprint(installed)
    except OSError as e:
        return {"reason": f"读取失败（{e}）"}
    if repo_fp["sha256"] == inst_fp["sha256"]:
        return None  # 只比内容哈希：重装/chmod 会改 size 与 mtime，那不算漂移
    return {"reason": "内容不一致", "repo": repo_fp, "installed": inst_fp}


def _alert_due(title):
    """同类型告警节流（复用推送组件的磁盘节流表，跨进程有效）。

    窗口是 `YIBAN_NOTIFY_COOLDOWN`（默认 60 秒）：它吸收的是"手工连跑几次 / 两个实例
    同时跑"这类重复；跨天的重复由 cron 每日一次的频次天然限制。
    """
    from yiban.notify import transport
    return transport._throttle_due(title)  # 刻意复用推送组件那份磁盘表：cron 每次新进程


def _send_admin_alert(title, mail):
    """发一封管理员告警邮件（收件人算法与 A 线告警同一份）。"""
    from web.services.notify_mail import _alert_mail_recipients
    recipients = _alert_mail_recipients()
    if not recipients:
        # 空收件人不静默当成功：本脚本的意义就是"不允许静默"，"没人收得到"必须让上层
        # 看见（退出码 1；它能否真的被看见，见模块头部对 `2>&1` 的那段提醒）
        logger.warning("备份哨兵告警无可用收件人（ADMIN_TO 与开启接收的管理员均为空）")
        return False
    return bool(mailer.send_admin_alert(title, mail, to=",".join(recipients)))


def _missing_mail(day, backup_dir, archive, sidecar_ok, plaintext=None):
    """当日包缺失/清单缺失/只有明文包的告警正文。"""
    if archive is None and plaintext is not None:
        summary = (f"当日只有【明文】备份包：{os.path.basename(plaintext)}。"
                   "明文包不计入健康——它内含 .env 全部密钥、管理员口令哈希与全量数据库，"
                   "备份目录被读 = 全库凭据泄露。")
        fields = [
            ("明文包", plaintext),
            ("期望形态", "、".join(_archive_candidates(backup_dir, day))),
            ("影响", "本轮加密链路失效或被显式关闭（BACKUP_PLAINTEXT=1），归档在裸奔"),
        ]
        advice = ["确认是否有人显式设了 BACKUP_PLAINTEXT：那是刻意为之，改回默认并补做加密副本",
                  "否则检查 BACKUP_GPG_PASSPHRASE / BACKUP_GPG_RECIPIENT 与 gpg 是否可用"
                  "（backup.sh 加密失败且未带 --require-encrypt 时会静默回退明文）",
                  "处置后手工补跑一次 backup.sh --require-encrypt 并做恢复演练"]
        return mail_layout.Mail(summary=summary, fields=fields, advice=advice, level="urgent")
    if archive is None:
        summary = f"当日备份包不存在：{backup_dir} 下没有 yiban-{day} 的归档。"
        fields = [
            ("期望位置", "、".join(_archive_candidates(backup_dir, day))),
            ("影响", "从这台主机损毁算起，最近一次可用备份已退到更早的日期"),
        ]
    else:
        summary = f"当日备份包缺 .sha256 清单：{os.path.basename(archive)} 没有旁挂件。"
        fields = [
            ("归档", archive),
            ("影响", "包无法核验完整性，恢复流程不能确认它没被改过"),
        ]
    return mail_layout.Mail(
        summary=summary,
        fields=fields,
        advice=["先看 backup.log 与 cron 的报错邮件：backup.sh 加密失败会 fail-closed 不落包",
                "缺加密口令时归档不会生成（BACKUP_GPG_PASSPHRASE 只从环境变量取）",
                "修好后手工补跑一次并做恢复演练"],
        level="urgent",
    )


def _drift_mail(repo_copy, installed, drift):
    """"运行脚本 ≠ 仓库脚本"的告警正文。"""
    details = [("仓库版", repo_copy), ("运行版", installed), ("判定", drift.get("reason", "不一致"))]
    for label, key in (("仓库版", "repo"), ("运行版", "installed")):
        fp = drift.get(key)
        if fp:
            details.append((f"{label}指纹", f"{fp['size']} 字节 / {fp['sha256'][:16]}… / {fp['mtime']}"))
    return mail_layout.Mail(
        summary="备份脚本的运行拷贝与仓库版内容不一致（cron 跑的可能不是仓库里的那份）。",
        fields=details,
        advice=["以仓库版为准重装：install -m 0700 -o root -g root scripts/backup.sh "
                "/usr/local/sbin/yiban-backup.sh",
                "运行拷贝漂移曾导致备份连续静默失败，改完仓库版务必同步安装"],
        level="urgent",
    )


# ---------------------------------------------------------------------------
# M28：审计链头哈希随备份离机（M74 的化解前提是这一层锚点真的在别处）
# ---------------------------------------------------------------------------
def _anchor_snapshot():
    """当日审计链的离机基线快照 dict；读不到链头时返回 None。

    只带两个读数：`head`（链头哈希，**完整 64 hex**——比对就比它，截断了就没法比）
    与 `count`（记录数）。刻意**不**带锚点文件末行：那行有 160+ 字符，纯文本渲染器
    会在 72 列硬切，折行后的哈希/行文给人对不上；而逐行比对锚点文件是
    `scripts/audit_verify.py --anchor` 的职责（它读文件，不走邮件宽度）。
    链头走 `audit_head_hash_ex` 的**三态**读取：空链（`state="empty"`）与读失败
    （`state="error"`）处置相反，绝不压成同一个"无锚点"。
    库文件不存在或打不开时同样返回 None——那是"读不到"，不是"空链"。
    """
    from yiban.store import db
    # 只读取证类调用一律 `create=False`（与 `scripts/audit_verify.py` 同一口径）。
    # `get_conn()` 的隐式 `init_db()` 是**全套缺省**（cleanup/migrate/create 全 True）；
    # 本文件此前显式关了 cleanup 与 migrate，却漏了 create——`init_db` 于是
    # `sqlite3.connect` 就地建库、`PRAGMA journal_mode=WAL`、`_create_tables`。
    # 库路径落空时（回落档没命中、或库被挪走，正是 ba-p02-02 那条链）后果不是报错
    # 而是"读出一份空链"：`audit_head_hash_ex` 得到 state="empty"，下面那条
    # "读不到就别发"的自保不触发，于是一份**当场建出来的空伪库**被当成当日离机基线
    # 外发出去——审计链有没有被整段删掉，唯一的外部参照就此失效。
    try:
        db.init_db(cleanup=False, migrate=False, create=False)
    except OSError as e:
        # 库不存在 / 打不开：只读取证绝不建库，按"读不到"处置（不外发）
        logger.warning("审计库不可只读打开，本日无链头可锚（不建库）: %s", e)
        return None
    state, head = db.audit_head_hash_ex()
    if state == "error":
        logger.warning("审计链头读取失败（读不到就别发锚点外发：宁缺毋滥）")
        return None
    return {"state": state, "head": head, "count": db.audit_row_count()}


def _anchor_mail(day, snap, archive, sidecar_ok):
    """当日审计链头的离机外发正文（每日一封，非告警，level=info）。

    链头哈希**独占一行**（64 字符 < 渲染器的 72 列）：挂在字段值里会被硬折成两行，
    折行后的哈希抄下来对不上——那正好毁掉这封邮件唯一的用途。
    """
    notes = ["当日审计链头哈希（完整 64 位；恢复该批备份后与恢复件的链头逐字符比对）："]
    if snap["state"] == "empty":
        notes.append("（空链——当日尚无审计记录，无链头可比）")
    else:
        notes.append(snap["head"] or "")
    fields = [
        ("业务日", day),
        ("审计记录数", str(snap["count"])),
    ]
    if archive is not None and sidecar_ok:
        fields.append(("对应备份包", os.path.basename(archive)))
    else:
        fields.append(("对应备份包", "当日备份包缺失或清单不全——本封只锚链头，不锚备份"))
    return mail_layout.Mail(
        summary=f"{day} 的审计链头哈希已离机留存。本机自检与备份包同在一个权限域内，"
                "无法证明「没被回滚到更早的旧批次」——这封邮件才是那个外部参照。",
        fields=fields,
        notes=notes,
        advice=["恢复任意备份包后，比对该包日期当天的这一条链头哈希："
                "不一致即说明那份包/那个库不是它自称的批次（回滚或被换过）",
                "不必每天人工看——留档即可；真出事时这串哈希就是离线基线",
                "本机锚点 audit-anchor.log 与备份包同盘，单独存在不构成离机证据；"
                "以本封（及同形态的通道健康日报）为准"],
        level="info",
    )


def _broadcast_anchor(day, archive, sidecar_ok):
    """把当日审计链头哈希发出去；返回是否送达（失败不抛，只记日志）。"""
    try:
        snap = _anchor_snapshot()
    except Exception as e:  # 读库/读锚点失败不该让整个哨兵挂掉
        logger.warning("读取审计链头失败，跳过本日锚点外发: %r", e)
        snap = None
    if snap is None:
        print("备份哨兵：读不到审计链头，今日锚点未离机（邮件通道正常也会缺这一条基线）",
              file=sys.stderr)
        return False
    try:
        # 节流判定也在 try 里：`_alert_due` → ledger 的状态文件锁 `open()` 在
        # **状态目录不可写**时会抛 OSError（makedirs 被吞、锁的 open 没兜底）。
        # 那正是哨兵该出声的场景，绝不能让它把下游的失败告警一起带走。
        if not _alert_due(ANCHOR_TITLE):
            print(f"备份哨兵：锚点外发在节流窗口内已发过，本次不外发（{ANCHOR_TITLE}）")
            return False
        sent = _send_admin_alert(ANCHOR_TITLE, _anchor_mail(day, snap, archive, sidecar_ok))
    except Exception as e:
        logger.warning("审计链锚点外发异常: %r", e)
        sent = False
    if not sent:
        print("备份哨兵：审计链锚点未能外发（无可用收件人或邮件组件失败）——"
              "今天的离机基线缺失，回滚检测这一天起是盲区", file=sys.stderr)
    return bool(sent)


def main(argv=None):
    """跑一次哨兵；返回进程退出码（0=检查完成，1=检查或告警失败）。"""
    backup_dir = _env("BACKUP_DIR", DEFAULT_BACKUP_DIR)
    app_dir = _env("APP_DIR", DEFAULT_APP_DIR)
    installed = _env("YIBAN_BACKUP_INSTALLED", DEFAULT_INSTALLED)
    day = _now()

    archive, sidecar_ok = _find_archive(backup_dir, day)
    # 明文包只在"密文不存在"时才需要单独识别——密文在则当日健康已满足，
    # 明文残留（加密切换过渡日）不双告警
    plaintext = None if archive is not None else _find_plaintext(backup_dir, day)
    drift = _drift_report(os.path.join(app_dir, "scripts", "backup.sh"), installed)

    if archive is not None and sidecar_ok and drift is None:
        print(f"备份哨兵：{day} 的归档与清单都在（{archive}），运行脚本与仓库版一致")
        try:
            _broadcast_anchor(day, archive, sidecar_ok)
        except Exception as e:  # 锚点外发绝不改变本函数的结论与退出码
            logger.warning("审计链锚点外发流程异常（已忽略）: %r", e)
        return 0

    if archive is not None and sidecar_ok:
        print(f"备份哨兵：{day} 的归档在（{archive}），但运行脚本已漂移：{drift['reason']}")
        mail = _drift_mail(os.path.join(app_dir, "scripts", "backup.sh"), installed, drift)
    elif archive is None and plaintext is not None:
        # MF-79：明文包不算健康——当日只有裸 .tar.gz 时加密链路已失效（或被显式
        # 关闭），这正是"看着有备份、其实全库凭据裸奔"的那一格，必须发声
        print(f"备份哨兵：{day} 只有【明文】归档（{plaintext}），不计入健康，已发告警")
        mail = _missing_mail(day, backup_dir, archive, sidecar_ok, plaintext=plaintext)
    elif archive is None or not sidecar_ok:
        # 排在漂移之前：归档没成比脚本漂移严重，两病同发时先喊缺备份（同类型只发一封）
        print(f"备份哨兵：{day} 的备份不完整（归档 {archive or '缺失'}，"
              f"清单 {'在' if sidecar_ok else '缺失'}）")
        mail = _missing_mail(day, backup_dir, archive, sidecar_ok)
    else:
        print(f"备份哨兵：运行脚本已漂移：{drift['reason']}")
        mail = _drift_mail(os.path.join(app_dir, "scripts", "backup.sh"), installed, drift)

    # M28：链头外发与上面这次告警**互相独立**——当天既喊了"备份没成"又喊了
    # "链头离机"是常态（前者说数据没存下来，后者说基线存下来了），两封都要发。
    # 放在告警发送之前：即便告警那封发不出去，基线那封也已经在路上。
    # 整段兜底：锚点链路里任何一步（含节流锁的 OSError）都不得让下面的
    # 失败告警发不出去、也不得改变退出码——状态目录坏掉时它恰好最容易炸。
    try:
        _broadcast_anchor(day, archive, sidecar_ok)
    except Exception as e:
        logger.warning("审计链锚点外发流程异常（已忽略，不影响告警与退出码）: %r", e)

    if not _alert_due(ALERT_TITLE):
        # stdout 结论在上面已经打过了：节流只挡邮件，cron 日志里每次都留一行
        print(f"备份哨兵：同类告警在节流窗口内已发过，本次不外发（{ALERT_TITLE}）")
        return 0
    try:
        sent = _send_admin_alert(ALERT_TITLE, mail)
    except Exception as e:  # 邮件组件承诺内部静默，这里兜底防新增异常外泄
        logger.warning("备份哨兵告警发送异常: %s", type(e).__name__)
        sent = False
    if not sent:
        print("备份哨兵：告警未能送达（邮件组件失败或收件人为空），请手工排查", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
