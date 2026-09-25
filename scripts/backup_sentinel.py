# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""**功能**
备份失败哨兵：当日备份包缺失时发一封管理员告警，并核对"运行脚本 = 仓库脚本"。

备份是**唯一**没有自愈路径的环节：`scripts/backup.sh` 在加密配置失效时 fail-closed
（不产出任何归档），cron 侧仍是正常退出，于是"连续几天没有备份"这件事在任何页面上都
看不出来（生产上曾静默失败 4 天，最后靠手工翻备份目录才发现）。本脚本是这条静默失败
目前**唯一**的自动发现途径，覆盖面止于"当日包与清单在不在、运行脚本是否漂移"：包内容
坏掉、备份脚本自身逻辑错都不在它视野内；它自己没被 cron 调起时同样没人知道。

两项检查：
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

**归属**
运维侧脚本（`scripts/`）。判据所需的路径与命名口径取自 `scripts/backup.sh`（包名、
`.sha256` 旁挂件的唯一事实源是它），本脚本不另立一套命名。

**复用**
收件人算法复用 `web/services/notify_mail.py` 的 `_alert_mail_recipients`（与 A 线告警
邮件同一份：ADMIN_TO 按个人开关过滤 + 开启接收的管理员），发送出口复用
`yiban.mail.send_admin_alert`；节流复用 `yiban/notify/transport.py` 的 `_throttle_due`
（`$YIBAN_STATE_DIR/notify-throttle.json`，跨进程磁盘表——cron 每次新进程，进程内节流表
在这里等于没有节流，故不新造）。

**通信**
用法：`python3 scripts/backup_sentinel.py`（无参数）。
安装与排期见仓库 `scripts/yiban-backup-sentinel.sh` 的头部（安装到
`/usr/local/sbin/yiban-backup-sentinel.sh`，cron 每日 **08:05**，即 02:00 备份之后）。
输入：环境变量 `BACKUP_DIR` / `APP_DIR` / `YIBAN_BACKUP_INSTALLED` / `YIBAN_ENV_FILE` /
`YIBAN_STATE_DIR`（与 `backup.sh`、`run.sh` 同口径）。
输出：结论与原因到 stdout（cron 重定向到日志）；异常时一封管理员告警邮件。
退出码：`0`=检查完成（含"缺失但告警已发出"）；`1`=检查本身失败或告警发不出去。
⚠ 但别把退出码当成"最后一道声音"：仓库给出的三条排期行（`README.md`、`scripts/backup.sh`、
本脚本头部）都带 `>> /var/log/yiban/backup.log 2>&1`，stderr 被并进日志文件、cron 不发
报错邮件（全仓也没有任何 `MAILTO` 设置）⇒ 退出码 1 实际只在有人翻 backup.log 时才看得见。
真要让"发不出去"这件事自己响，得去掉 `2>&1` 并配 `MAILTO`，或把退出码接进监控。
调用谁：`yiban.mail`、`web.services.notify_mail`、`yiban.notify.transport`。
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
    """当日日期串（`YYYY-MM-DD`）。

    用**本机时区**而不是 `yiban.clock` 的北京时间：包名由 `backup.sh` 的
    `date +%Y-%m-%d` 生成，那是系统本地时间；两边取不同时钟会在时区非 +08 的服务器上
    按天错位，天天误报"备份缺失"。
    """
    return datetime.now().strftime("%Y-%m-%d")


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
