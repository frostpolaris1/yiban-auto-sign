# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""**功能**
备份失败哨兵：当日备份包缺失时发一封管理员告警，核对"运行脚本 = 检出脚本"、
"同一作业的活跃触发器恰好 1 条"，并把**当日审计链头哈希随备份外发**（离机锚点，见下）。

备份是**唯一**没有自愈路径的环节：`scripts/backup.sh` 在加密配置失效时 fail-closed
（不产出任何归档），cron 侧仍是正常退出，于是"连续几天没有备份"这件事在任何页面上都
看不出来（生产上曾静默失败 4 天，最后靠手工翻备份目录才发现）。本脚本是这条静默失败
目前**唯一**的自动发现途径，覆盖面止于"当日包与清单在不在、运行脚本是否漂移"：包内容
坏掉、备份脚本自身逻辑错都不在它视野内；它自己没被 cron 调起时同样没人知道。

四项检查：
1. **当日包存在且为密文**：`${BACKUP_DIR:-/var/backups/yiban}/yiban-<今天>.tar.gz.gpg` 与它的
   `.sha256` 旁挂件都在**且非空**（age 密文形态也认，见 `_archive_candidates`）。
   0 字节归档/清单是半写入或事故残留，判健康等于把"备份没成"藏起来（P3-2）。
   **明文包（裸 `.tar.gz`）不计入健康**（M3 批次0 · MF-79）：backup.sh 只在加密配置
   失效或显式 BACKUP_PLAINTEXT=1 时才产出明文包，把它当"当日备份完成"正是曾经
   4 天静默失败的同型盲区；当日只有明文包 ⇒ 判不健康并走告警路径；
   同日既有密文又有明文残留（加密切换过渡日）⇒ 以密文为准，不双告警。
2. **同一作业的活跃触发器恰好 1 条**（2026-10-25 生产事故）：对 `deploy/prod/cron.d/*`
   每张仓库表里的每个作业，统计生产上 `crontab -l -u root`、`crontab -l`、
   `/etc/cron.d/*` 与 `/etc/crontab` 的活跃命令；**只扫 root 与当前身份**，其它
   身份的 crontab 不在扫描面（登记边界，见 `_active_triggers`）。同一作业同一排期
   出现多于一条，或多出仓库表未声明的排期，都判漂移。作业按命令里 token 的
   文件名归类（`flock`/`timeout`/`nice` 等包装器把脚本放在位置参数里也不会漏），
   因此 sign/probe/cleanup 同形状的重复也会被抓到。
   `/etc/cron.d` 下**文件名只允许 `[A-Za-z0-9_-]`**，其余字符（点号、波浪号、
   空格等）让整份文件被忽略（生产上实测 4 份 `yiban-*.reconcile-*` 残留因此不是
   活跃触发器；2026-10-26 生产口径：cleanup 两份 + probe 一份 + sign 一份），
   统计先按 cron 自己的文件名规则过滤。读不到 crontab（无命令/权限
   不足）时**显式**打印"触发器条数未核"，
   **且不丢掉已发现的重复**——两者并存返回、两者都打印；跳过不等于绿。
   判据与实现见 `_trigger_drift_report`。
3. **防漂移**：`$APP_DIR/scripts/backup.sh`（检出侧）与 `$YIBAN_BACKUP_INSTALLED`
   （默认 `/usr/local/sbin/yiban-backup.sh`，cron 实际调的那个）内容一致——"运行脚本
   是检出脚本的拷贝"是部署约定，判据与它挡的是什么见 `_drift_report`。安静路径上
   两者一致（或安装版不存在）时不输出任何东西。
4. **审计链头哈希随备份外发**（M28，每轮都发，与前几项的成败无关）：见 `_broadcast_anchor`。

**为什么第 4 项与备份"同包同盘"的问题必须另解**：备份把 `audit-anchor.log` 一起打进去，
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
`yiban.adapters.notifier.mail.send_admin_alert`；节流复用 `yiban/adapters/notifier/notify/transport.py` 的 `_throttle_due`
（`$YIBAN_STATE_DIR/notify-throttle.json`，跨进程磁盘表——cron 每次新进程，进程内节流表
在这里等于没有节流，故不新造）；链头与记录数读 `yiban.adapters.persistence.db` 的
`audit_head_hash_ex` / `audit_row_count`（只读；不读、不改锚点文件）。

**通信**
用法：`python3 scripts/backup_sentinel.py`（无参数）。
安装与排期见仓库 `scripts/yiban-backup-sentinel.sh` 的头部（安装到
`/usr/local/sbin/yiban-backup-sentinel.sh`，cron 每日 **08:05**，即 02:00 备份之后）。
输入：环境变量 `BACKUP_DIR` / `APP_DIR` / `YIBAN_BACKUP_INSTALLED` / `YIBAN_ENV_FILE` /
`YIBAN_STATE_DIR` / `YIBAN_DB_FILE`（与 `backup.sh`、`run.sh` 同口径）；
`YIBAN_CRON_D_DIR` / `YIBAN_SYSTEM_CRONTAB`（触发器条数门读的生产 cron 面，默认
`/etc/cron.d` 与 `/etc/crontab`）。
输出：结论与原因到 stdout（cron 重定向到日志）；审计链头哈希外发一封邮件（每日一封）；
异常时一封管理员告警邮件。
退出码：`0`=检查完成（含"缺失但告警已发出"、**锚点外发失败**——见下；
**触发器条数未核也算检查完成**，未核原因打印到 stderr，不改退出码——理由见
`main()` 的 docstring）；`1`=检查本身失败或告警发不出去。
⚠ 外发失败**不**改退出码：哨兵的退出码语义是"检查是否做完"，把"邮件通道当时抖动"
升格成 cron 非 0 只会让每日备份链在日志里天天红，反而淹没真故障。外发失败照样
`print` 到 stderr 并记 `logger.warning`，与本模块头部"没人看得见 cron 退出码"那段
提醒是同一个诚实边界。
⚠ 但别把退出码当成"最后一道声音"：仓库给出的三条排期行（`README.md`、`scripts/backup.sh`、
本脚本头部）都带 `>> /var/log/yiban/backup.log 2>&1`，stderr 被并进日志文件、cron 不发
报错邮件（全仓也没有任何 `MAILTO` 设置）⇒ 退出码 1 实际只在有人翻 backup.log 时才看得见。
真要让"发不出去"这件事自己响，得去掉 `2>&1` 并配 `MAILTO`，或把退出码接进监控。
调用谁：`yiban.adapters.notifier.mail`、`web.services.notify_mail`、
`yiban.adapters.notifier.notify.transport`、`yiban.adapters.persistence.db`。
谁调用：cron（每日一次）；无其它调用点。
"""
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from hashlib import sha256

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from yiban.adapters.notifier import mail as mailer  # noqa: E402
from yiban.adapters.notifier.mail import layout as mail_layout  # noqa: E402

logger = logging.getLogger("yiban")

#: 备份目录默认值（与 `scripts/backup.sh` 的 `BACKUP_DIR` 默认一致）
DEFAULT_BACKUP_DIR = "/var/backups/yiban"
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
#: `/etc/cron.d` 默认目录：cron 从这里读系统级 cron 表
DEFAULT_CRON_D_DIR = "/etc/cron.d"
#: `/etc/crontab` 默认路径：系统级 cron 表，格式同 `/etc/cron.d`（带身份列）
DEFAULT_SYSTEM_CRONTAB = "/etc/crontab"
#: cron 环境变量行（`SHELL=` / `PATH=` / `MAILTO=`）不是调度行
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
#: 解释器：作业标识取它执行的脚本名（`bash x.sh` 与直接调用 `/opt/x.sh` 同键）
_INTERPRETER_NAMES = frozenset({
    "sh", "bash", "dash", "zsh", "ksh", "python", "python2", "python3",
    "perl", "ruby", "env", "nice", "ionice", "timeout", "flock", "sudo", "runuser",
})
#: 独立成词的重定向：下一个 token 是文件名，不是命令
_BARE_REDIRECT_TOKENS = frozenset({
    ">", ">>", "<", "<<", "1>", "1>>", "2>", "2>>", "&>", "&>>",
})
#: 贴紧式重定向（`2>&1`、`>/dev/null`、`2>/dev/null`、`>>/var/log/x`）
_ATTACHED_REDIRECT_RE = re.compile(r"^\d*(?:>>?|<<?)\S")
#: cron 只读文件名匹配该字符集（man 8 cron）；其余字符让整份文件被忽略
_CRON_TABLE_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")


def _env(name, default):
    """环境变量优先、空串按未设置处理（与 backup.sh 的 `VAR="${VAR:-默认}"` 同口径）。"""
    value = os.environ.get(name, "").strip()
    return value or default


def _now():
    """当日日期串（`YYYY-MM-DD`）——业务日（`yiban.core.clock` 北京钟）。

    与 backup.sh 的包名时钟**同源**（MF-109 统一后 backup.sh 的包名也走
    `yiban.core.clock.today()`）：两边取不同时钟才会在时区非 +08 的服务器上按天错位、
    天天误报"备份缺失"。宿主时区本就是北京时它与 `datetime.now()` 逐日相等。
    """
    from yiban.core.clock import today
    return today()


def _archive_candidates(backup_dir, day):
    """当日归档的候选路径（按 ARCHIVE_SUFFIXES 顺序），与 `.sha256` 旁挂件同目录。"""
    return [os.path.join(backup_dir, f"yiban-{day}{suffix}") for suffix in ARCHIVE_SUFFIXES]


def _nonempty_file(path):
    """存在且字节数 > 0；stat 失败按"不存在"处置。

    只判 `isfile` 会让 0 字节残留（加密切换/半写入/事故残留）被判健康——"文件在"
    不等于"备份在"，与 `.sha256` 清单那条同理（P3-2）。
    """
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def _find_archive(backup_dir, day):
    """返回 (归档路径, 旁挂件已就位) 或 (None, False)。

    `.sha256` 与归档同时要求：归档在而清单不在，同样是"不能核验的备份"——恢复流程
    靠清单比对完整性，缺了它等于备份不可信。两侧都要求**非空**：0 字节归档 /
    0 字节清单都是不可恢复的残留，判健康会把"备份没成"藏起来。
    """
    for path in _archive_candidates(backup_dir, day):
        if _nonempty_file(path):
            # 清单缺失单独带出来：不可核验的包等于没有备份，但它是"包在、清单没了"，
            # 与"整个包没生成"的排查方向不同，故不合并成一个布尔
            return path, _nonempty_file(path + ".sha256")
    return None, False


def _find_plaintext(backup_dir, day):
    """当日明文归档（裸 `.tar.gz`）路径；不存在返回 None。

    仅用于把"当日只有明文包"与"什么都没生成"区分开——两种都不健康，但排查方向
    完全不同（前者查加密配置为何失效，后者查 cron/脚本本身）。
    与 `_find_archive` 同口径：0 字节明文是残留，按"包缺失"归因，不算"只有明文包"。
    """
    path = os.path.join(backup_dir, f"yiban-{day}{PLAINTEXT_SUFFIX}")
    return path if _nonempty_file(path) else None


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

    这一项删不掉：cron 调的是安装拷贝。检出修好了而拷贝没跟上时，备份照旧按旧脚本
    跑（旧拷贝曾连续几天静默失败），"当日包缺失"那一项只能在事后才发现这件事。
    """
    if not os.path.isfile(installed):
        # 未按约定安装（自定义路径/单机试用）：无从比对，不报——真正的备份失败由
        # "当日包缺失"那一项兜住，这里多喊只会制造噪音
        return None
    if not os.path.isfile(repo_copy):
        return {"reason": f"检出不存在（{repo_copy}）"}
    try:
        repo_fp = _file_fingerprint(repo_copy)
        inst_fp = _file_fingerprint(installed)
    except OSError as e:
        return {"reason": f"读取失败（{e}）"}
    if repo_fp["sha256"] == inst_fp["sha256"]:
        return None  # 只比内容哈希：重装/chmod 会改 size 与 mtime，那不算漂移
    return {"reason": "内容不一致", "repo": repo_fp, "installed": inst_fp}


# ---------------------------------------------------------------------------
# 触发器条数门：同一作业的活跃 cron 触发器必须恰好 1 条
# （2026-10-25 生产事故：root crontab 与 /etc/cron.d/yiban-backup 各一条同一 wrapper，
#   每天 02:00 并发跑两轮，gpg 互相覆写，当天备份全失。防漂移只比脚本内容，
#   看不到"同一作业被排了两次"这件事。）
# ---------------------------------------------------------------------------
def _strip_redirections(command):
    """去掉重定向（含贴紧形态）后的 token 序列：作业标识只看命令本身。"""
    out = []
    skip_next = False
    for tok in command.split():
        if skip_next:
            skip_next = False
            continue
        if tok in _BARE_REDIRECT_TOKENS:
            skip_next = True          # 下一个 token 是重定向目标
            continue
        if tok.startswith("&>") or _ATTACHED_REDIRECT_RE.match(tok):
            continue
        out.append(tok)
    return out


def _command_segments(command):
    """按 shell 控制符（`&&` / `||` / `;` / `|`）切段；行尾注释截断。

    `cd <检出> && python3 x.py` 的作业是第二段——`cd` 只是定位，不是作业。
    以 `#` 开头的 token 起是 shell 注释：本行其余 token 不是命令
    （`wrapper.sh # 同族见 run.sh` 里的 run.sh 不得被算成触发器）。
    """
    segment = []
    for tok in _strip_redirections(command):
        if tok.startswith("#"):
            if segment:
                yield segment
            return
        if tok in ("&&", "||", ";", "|"):
            if segment:
                yield segment
            segment = []
            continue
        segment.append(tok)
    if segment:
        yield segment


def _job_key(command):
    """命令的作业标识；无法识别返回 None。

    同一可执行件归同一作业：`/usr/local/sbin/x.sh`、`bash /opt/x.sh` 与
    `env A=1 /usr/local/sbin/x.sh` 同键（解释器取其后第一个非选项参数）。
    排期不参与归类：同一作业可以有多条排期（`yiban-sign` 的首签与补签
    两个排期是有意设计），重复判据按"作业 + 排期"逐对计。
    边界：本函数只用于**仓库表**的作业声明（部署契约是直接写脚本路径或
    `bash/python3 <脚本>`）。生产侧命令的归类走 `_job_keys_in_command` 的逐 token
    匹配——带选项值的包装器（`flock`/`timeout`/`nice`/`sudo -u user`）在这里会
    取到选项值，逐 token 匹配则不受影响。
    """
    for segment in _command_segments(command):
        i = 0
        while i < len(segment) and _ENV_ASSIGN_RE.match(segment[i]):
            i += 1
        if i >= len(segment):
            continue
        name = os.path.basename(segment[i])
        if name == "cd":
            continue
        if name in _INTERPRETER_NAMES:
            j = i + 1
            while j < len(segment) and (segment[j].startswith("-")
                                        or _ENV_ASSIGN_RE.match(segment[j])):
                j += 1
            if j < len(segment):
                return os.path.basename(segment[j])
            continue
        return name
    return None


#: 同一作业的入口别名组（部署面已知）。
#: 安装版 `yiban-backup.sh` 与 cron wrapper 调的是同一个备份作业。
#: 其它改名入口/软链名不归并（登记边界，不做 inode 归并）。
_JOB_ALIAS_GROUPS = (
    frozenset({"yiban-backup-wrapper.sh", "yiban-backup.sh"}),
)


def _canonical_job_name(name):
    """别名归一到组内代表（部署契约优先 wrapper）；无别名原样返回。"""
    for group in _JOB_ALIAS_GROUPS:
        if name in group:
            for member in ("yiban-backup-wrapper.sh", "yiban-backup.sh"):
                if member in group:
                    return member
    return name


def _known_job_for(name, known_jobs):
    """token 的 basename 对应的已知作业键；无对应返回 None。"""
    if name in known_jobs:
        return name
    for group in _JOB_ALIAS_GROUPS:
        if name in group:
            for member in group:
                if member in known_jobs:
                    return member
    return None


def _job_keys_in_command(command, known_jobs):
    """命令里**处于命令位置**的已知作业键。

    只有下列 token 是作业：
      - 每段的首个命令 token（跳过前置 `A=1` 环境赋值）；
      - 段首是解释器/包装器（`_INTERPRETER_NAMES`）时，其后跳过选项值与
        选项参数的 token。`flock`/`timeout`/`nice`/`sudo` 因此都不会漏检。
    普通命令的参数不是作业：`/bin/echo wrapper.sh` 与 `--warn-if wrapper.sh`
    不计。行尾注释由 `_command_segments` 截断。
    别名：`yiban-backup.sh` 与 `yiban-backup-wrapper.sh` 同作业。
    其它改名入口/软链名不归并（登记边界，不做 inode 归并）。
    `sh -c '<单个脚本路径>'` 会识别（引号剥除后 basename 命中）；
    `sh -c '<含多个词的整段脚本>'` 不保证。
    边界（登记）：无值短选项（`sudo -n`、`env -i`、`bash -x`、`sh -eu`）后的
    命令会被当成选项值跳过；部署契约没有这种写法，出现时本门不保证认出。
    已知漏检形态（登记，不在本批范围）：`sudo -uroot`、`nice -n10`、
    `nice -10`、`ionice -c2`、`sudo --`、`sudo --login`、`env --unset=PATH --`；
    这些形态会让被包装的作业不被计入。
    「跳过选项值」是降噪守卫：去掉它只会多报（安全方向），不会漏报（P4-4）。
    """
    found = set()
    for segment in _command_segments(command):
        i = 0
        while i < len(segment) and _ENV_ASSIGN_RE.match(segment[i]):
            i += 1
        if i >= len(segment):
            continue
        head = os.path.basename(segment[i].strip("'\""))
        job = _known_job_for(head, known_jobs)
        if job is not None:
            found.add(job)
            continue
        if head not in _INTERPRETER_NAMES:
            continue                  # 普通命令的参数不是触发器
        prev_is_option = False
        for token in segment[i + 1:]:
            name = os.path.basename(token.strip("'\""))
            if not prev_is_option:
                job = _known_job_for(name, known_jobs)
                if job is not None:
                    found.add(job)
            # -c/--command 的值是命令；`--opt=value` 是紧贴值，下一 token 仍是命令
            prev_is_option = (False if token in ("-c", "--command") or "=" in token
                              else token.startswith("-"))
    return found


def _iter_cron_lines(text, has_user_field):
    """逐行产出 `(schedule, command)`；跳过注释/空行/环境变量行。

    `has_user_field=True` 用于 `/etc/cron.d` 与 `/etc/crontab`（五个时间字段之后
    是身份列）；`False` 用于 `crontab -l` 的输出。`@daily` 这类关键字占一个字段。
    """
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if _ENV_ASSIGN_RE.match(fields[0]):
            continue
        if fields[0].startswith("@"):
            schedule, rest = fields[0], fields[1:]
        else:
            if len(fields) < 5:
                continue
            schedule, rest = " ".join(fields[:5]), fields[5:]
        if has_user_field:
            rest = rest[1:]
        if not rest:
            continue
        yield schedule, " ".join(rest)


def _current_user():
    """当前进程的身份名；取不到返回空串。"""
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, AttributeError, KeyError, OSError):
        pass
    try:
        import getpass
        return getpass.getuser()
    except (ImportError, KeyError, OSError):
        return ""


def _read_crontab(crontab_bin, user=None):
    """读一份 crontab 清单：返回 `(text, reason)`。

    `reason` 非空 = 读不到（无命令 / 权限不足 / 其它失败）。空 crontab 返回
    `("", None)`——"该身份没有 crontab"是正常空态，不是未核。
    """
    cmd = [crontab_bin, "-l"] + (["-u", user] if user else [])
    try:
        # errors="replace"：crontab 里混入非 UTF-8 注释时，解码异常不得让
        # 每日哨兵整体崩掉（它崩了，"备份没成"就没人喊）。
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        return "", f"无法执行 {' '.join(cmd)}：{e}"
    out = proc.stdout or ""
    err = (proc.stderr or "").strip()
    if proc.returncode == 0:
        return out, None
    if "no crontab" in (out + err).lower():
        return "", None
    detail = err or out.strip()
    return "", f"{' '.join(cmd)} 失败（rc={proc.returncode}）：{detail}"


def _cron_d_entries(cron_d_dir):
    """读 `cron.d` 目录的活跃条目：返回 `([(path, schedule, command)], [未核原因])`。

    **cron 自己的文件名规则**：`/etc/cron.d` 下文件名只允许 `[A-Za-z0-9_-]`；
    其余字符（点号、波浪号、空格、`+` 等）让整份文件被忽略。生产上实测 4 份
    `yiban-*.reconcile-20261017125303` 残留（2026-10-26 生产：cleanup 两份 +
    probe 一份 + sign 一份）因此不是活跃触发器；不实现这条规则，门会把它们
    误报成重复。
    假设（生产实测）：cron **未开 `-l`（LSB 名字规则）**。开 `-l` 时 cron 只认
    `^_?([a-z0-9_.]+-)+[a-z0-9]+$` 形态的 LSB 文件名，这 4 份
    `yiban-*.reconcile-*` 会变成活跃触发器，本门不会报（生产机
    `ps -o args= -C cron` 无 `-l`）。
    边界（如实登记）：文件的属主/权限条件（root 且组与其他不可写）**不判**。
    权限不合的文件 cron 不执行，本门仍把它算成触发器——方向是多报，不是漏报。
    """
    entries, unverified = [], []
    try:
        names = sorted(os.listdir(cron_d_dir))
    except OSError as e:
        return [], [f"无法读取 cron.d 目录 {cron_d_dir}：{e}"]
    for name in names:
        if not _CRON_TABLE_NAME_RE.fullmatch(name):
            continue                  # cron 的规则：非白名单文件名的表不读
        path = os.path.join(cron_d_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as e:
            unverified.append(f"无法读取 {path}：{e}")
            continue
        for schedule, command in _iter_cron_lines(text, has_user_field=True):
            entries.append((path, schedule, command))
    return entries, unverified


def _active_triggers(cron_d_dir=DEFAULT_CRON_D_DIR,
                     system_crontab=DEFAULT_SYSTEM_CRONTAB, crontab_bin="crontab"):
    """生产上所有活跃 cron 触发器：返回 `([(source, schedule, command)], [未核原因])`。

    来源：root 的 crontab、当前身份的 crontab、`/etc/cron.d/*`、`/etc/crontab`。
    哨兵以 root 跑时 `crontab -l` 与 `crontab -l -u root` 是同一条 crontab，
    只读一次——否则每条 root 行都被算两条，门会自己假报重复。
    **只扫 root 与当前身份**；其它身份的 crontab 不在扫描面（登记边界：部署契约
    把排期放在 `/etc/cron.d`，身份由 install.sh 决定；生产当前只有 root crontab，
    现状无影响。要扩到全部身份得枚举 /var/spool/cron，属另一批改动）。
    """
    entries, unverified = [], []
    current = _current_user()
    users = ["root"]
    if current and current != "root":
        users.append(current)
    for user in users:
        text, reason = _read_crontab(crontab_bin, user)
        if reason:
            unverified.append(reason)
            continue
        label = f"{user} 的 crontab"
        for schedule, command in _iter_cron_lines(text, has_user_field=False):
            entries.append((label, schedule, command))
    cron_d_entries, reasons = _cron_d_entries(cron_d_dir)
    entries.extend(cron_d_entries)
    unverified.extend(reasons)
    if os.path.isfile(system_crontab):
        try:
            with open(system_crontab, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as e:
            unverified.append(f"无法读取 {system_crontab}：{e}")
        else:
            for schedule, command in _iter_cron_lines(text, has_user_field=True):
                entries.append((system_crontab, schedule, command))
    return entries, unverified


def _repo_jobs(repo_cron_dir):
    """仓库 cron 表里的作业清单：`{作业键: {"schedules": set(), "tables": set()}}`。

    目录不可读、或解析不出任何作业时返回 None——调用方按"未核"处置，
    不当成"没有作业"（扫空不得当绿）。
    """
    try:
        names = sorted(os.listdir(repo_cron_dir))
    except OSError:
        return None
    jobs = {}
    for name in names:
        if not _CRON_TABLE_NAME_RE.fullmatch(name):
            continue                  # 与生产同一套规则：非白名单文件名的表 cron 不读
        path = os.path.join(repo_cron_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        for schedule, command in _iter_cron_lines(text, has_user_field=True):
            key = _job_key(command)
            if key is None:
                continue
            key = _canonical_job_name(key)
            info = jobs.setdefault(key, {"schedules": set(), "tables": set()})
            info["schedules"].add(schedule)
            info["tables"].add(name)
    if not jobs:
        # 扫空不得当绿：目录在却没有解析出任何作业 ⇒ 门被架空，按"未核"报。
        return None
    return jobs


def _trigger_drift_report(repo_cron_dir, cron_d_dir=DEFAULT_CRON_D_DIR,
                          system_crontab=DEFAULT_SYSTEM_CRONTAB):
    """同一作业的活跃触发器条数检查：返回 `(report, unverified)`。

    `report=None` = 没发现漂移；`unverified` 非空 = 条数未核（读不到 crontab）。
    **两者并存返回**：少读一面只说明"可能还有没看见的重复"，不构成把已发现的
    重复丢掉的理由（P3-3：实测 root crontab 有两条重复 + cron.d 不可读时旧实现
    返回 report=None，重复被隐藏）。调用方必须同时打印两者。
    发现两类漂移：
      - 同一作业同一排期出现多条（生产事故的形态：root crontab + cron.d 各一条）；
      - 同一作业多出仓库表未声明的排期（一天跑两遍，同样要人删）。
    生产侧归类走 `_job_keys_in_command`（逐 token 匹配），`flock`/`timeout`/`nice`
    等包装形态不会漏检（P3-5）。
    """
    jobs = _repo_jobs(repo_cron_dir)
    if jobs is None:
        return None, [f"仓库 cron 表目录不可读或解析不出作业（{repo_cron_dir}）"]
    entries, unverified = _active_triggers(cron_d_dir, system_crontab)
    production = {}
    for source, schedule, command in entries:
        for key in _job_keys_in_command(command, jobs):
            production.setdefault((key, schedule), []).append(source)
    findings = []
    for job in sorted(jobs):
        declared = jobs[job]["schedules"]
        observed = {schedule for (key, schedule) in production if key == job}
        if not observed:
            # 本机没有该作业的任何触发器（表未安装/自定义部署）：条数无从判。
            # "该有的排期缺失"是另一形状，由安装器对账与缺包检查负责。
            continue
        for schedule in sorted(observed - declared):
            findings.append({
                "job": job, "schedule": schedule,
                "count": len(production[(job, schedule)]),
                "sources": production[(job, schedule)],
                "note": "排期未在仓库表声明",
                # 类型判据用布尔字段，不靠 note 文案（文案一改分流会静默退化）
                "undeclared": True,
            })
        for schedule in sorted(observed & declared):
            count = len(production[(job, schedule)])
            if count != 1:
                findings.append({
                    "job": job, "schedule": schedule, "count": count,
                    "sources": production[(job, schedule)],
                    "note": "同一作业同一排期有多条",
                    "undeclared": False,
                })
    report = None
    if findings:
        # 措辞覆盖两种形状：同一排期多条，**以及**未在仓库表声明的排期（count 可为 1）
        report = {"reason": f"{len(findings)} 条作业排期的活跃触发器与仓库表声明不一致",
                  "findings": findings}
    return report, unverified


def _alert_due(title):
    """同类型告警节流（复用推送组件的磁盘节流表，跨进程有效）。

    窗口是 `YIBAN_NOTIFY_COOLDOWN`（默认 60 秒）：它吸收的是"手工连跑几次 / 两个实例
    同时跑"这类重复；跨天的重复由 cron 每日一次的频次天然限制。
    """
    from yiban.adapters.notifier.notify import transport
    return transport._throttle_due(title)  # 刻意复用推送组件那份磁盘表：cron 每次新进程


def _send_admin_alert(title, mail):
    """发一封管理员告警邮件（收件人算法与 A 线告警同一份）。

    **绝不建库**（2026-10-13 r4 批）：哨兵以 root 跑（排期件
    `deploy/prod/cron.d/yiban-backup-sentinel` 的身份列 = root）。而
    `_alert_mail_recipients()` 经 `connection.get_conn()` 的隐式 `init_db(create=True)`
    会在库缺失时就地建一份 **root 属主** 的库，顶掉 yiban 属主的真库——yiban 侧的 web
    随后打不开它。故库文件不存在时**不走数据库收件人读法**，改取 .env 的 ADMIN_TO；
    这与 `db.admin_mail_recipients` 在库查询失败时的降级**同结论**（`filter_mail_notify`
    对 `find_user` 失败原样保留收件人，拿掉库读不改变结果）。库存在时走同一份共享算法，
    行为逐字不变。
    """
    from yiban.adapters.config import env_io
    from yiban.adapters.persistence import db
    if os.path.isfile(env_io.resolve_path("YIBAN_DB_FILE", db.DB_DEFAULT)):
        from web.services.notify_mail import _alert_mail_recipients
        recipients = _alert_mail_recipients()
    else:
        # 库缺失：只取 .env 的 ADMIN_TO（与 db 查询失败同一降级结论），绝不触发建库
        recipients = (sorted(set(mailer.admin_recipients()))
                      if mailer.admin_notify_enabled() else [])
    if not recipients:
        # 空收件人不静默当成功：本脚本的意义就是"不允许静默"，"没人收得到"必须让上层
        # 看见（退出码 1；它能否真的被看见，见模块头部对 `2>&1` 的那段提醒）
        logger.warning("备份哨兵告警无可用收件人（ADMIN_TO 与开启接收的管理员均为空）")
        return False
    return bool(mailer.send_admin_alert(title, mail, to=",".join(recipients)))


def _script_drift_parts(repo_copy, installed, drift):
    """脚本内容漂移的邮件字段与处置建议（缺失告警与漂移告警共用同一份）。"""
    fields = [("检出", repo_copy), ("运行版", installed),
              ("判定", drift.get("reason", "不一致"))]
    for label, key in (("检出", "repo"), ("运行版", "installed")):
        fp = drift.get(key)
        if fp:
            fields.append((f"{label}指纹",
                           f"{fp['size']} 字节 / {fp['sha256'][:16]}… / {fp['mtime']}"))
    advice = ["以检出为准重装：install -m 0700 -o root -g root scripts/backup.sh "
              "/usr/local/sbin/yiban-backup.sh",
              "运行拷贝漂移曾导致备份连续静默失败，改完检出务必同步安装"]
    return fields, advice


def _trigger_drift_parts(trigger_drift):
    """触发器条数漂移的邮件字段与处置建议（缺失告警与漂移告警共用同一份）。"""
    fields, advice = [], []
    for finding in trigger_drift["findings"]:
        # 条数放在值的最前面：纯文本渲染器按 72 列折行，放后面会被折开
        fields.append((
            f"作业 {finding['job']}",
            f"{finding['count']} 条；排期 {finding['schedule']}；"
            f"{finding['note']}；来源：" + "、".join(finding["sources"])))
    advice += ["同一作业只保留一条触发器：删掉 root crontab 里的重复行，"
               "或删掉多余的 cron.d 表",
               "权威触发器是检出里的 deploy/prod/cron.d/<表名>，由 install.sh 落位"]
    return fields, advice


def _missing_mail(day, backup_dir, archive, sidecar_ok, plaintext=None, *,
                  drift=None, trigger_drift=None, repo_copy="", installed=""):
    """当日包缺失/清单缺失/只有明文包的告警正文。

    归档没成时脚本漂移与触发器漂移**一并写进这封**（P3-4）：本批的意义就是让
    "备份链路不可信"这件事有声音；只打到 stdout 的漂移在 cron 日志里没人看。
    共用同一标题与节流键，不另发一封（分标题会让两封互相挤掉）。
    """
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
    elif archive is None:
        summary = f"当日备份包不存在：{backup_dir} 下没有 yiban-{day} 的归档。"
        fields = [
            ("期望位置", "、".join(_archive_candidates(backup_dir, day))),
            ("影响", "从这台主机损毁算起，最近一次可用备份已退到更早的日期"),
        ]
        advice = ["先看 backup.log 与 cron 的报错邮件：backup.sh 加密失败会 fail-closed 不落包",
                  "缺加密口令时归档不会生成（BACKUP_GPG_PASSPHRASE 只从环境变量取）",
                  "修好后手工补跑一次并做恢复演练"]
    else:
        summary = (f"当日备份包缺 .sha256 清单：{os.path.basename(archive)} "
                   "的旁挂件缺失或为 0 字节。")
        fields = [
            ("归档", archive),
            ("影响", "包无法核验完整性，恢复流程不能确认它没被改过"),
        ]
        advice = ["先看 backup.log 与 cron 的报错邮件：backup.sh 加密失败会 fail-closed 不落包",
                  "缺加密口令时归档不会生成（BACKUP_GPG_PASSPHRASE 只从环境变量取）",
                  "修好后手工补跑一次并做恢复演练"]
    if drift is not None:
        parts, tips = _script_drift_parts(repo_copy, installed, drift)
        fields += parts
        advice += tips
        summary += " 运行脚本与检出内容不一致。"
    if trigger_drift is not None:
        parts, tips = _trigger_drift_parts(trigger_drift)
        fields += parts
        advice += tips
        summary += (f" 另有 {len(trigger_drift['findings'])} 条作业排期漂移："
                    + _trigger_drift_summary(trigger_drift["findings"]))
    return mail_layout.Mail(summary=summary, fields=fields, advice=advice, level="urgent")


def _trigger_drift_summary(findings):
    """触发器漂移摘要：按「多条重复」与「仅未声明排期」分流措辞。

    `count=1` 只可能来自「排期未在仓库表声明」。此时说「存在多条」会与正文的
    「1 条」自相矛盾（第 6 项）。两种形状并存时用中性措辞，不单说哪一种。
    """
    has_multi = any(f["count"] > 1 for f in findings)
    has_undeclared = any(f["undeclared"] for f in findings)
    if has_multi and has_undeclared:
        return "同一作业的触发器与仓库表声明不一致（含重复触发器与未声明排期）。"
    if has_multi:
        return "同一作业同一排期存在多条活跃触发器（cron 会并发跑同一份作业）。"
    return "作业排期未在仓库表声明（可能一天多跑或排期漂移）。"


def _drift_mail(repo_copy, installed, drift, trigger_drift=None):
    """"运行脚本 ≠ 检出脚本"与"同一作业多条触发器"的告警正文。

    两件事共用一封：同一标题与节流键（"今天的备份链路不可信"）。分标题会让节流
    窗口各算一份、同一天发两封，还会互相挤掉。
    """
    details, advice, summaries = [], [], []
    if drift is not None:
        parts, tips = _script_drift_parts(repo_copy, installed, drift)
        details += parts
        advice += tips
        summaries.append("备份脚本的运行拷贝与检出内容不一致（cron 跑的可能不是检出里的那份）。")
    if trigger_drift is not None:
        parts, tips = _trigger_drift_parts(trigger_drift)
        details += parts
        advice += tips
        summaries.append(_trigger_drift_summary(trigger_drift["findings"]))
    return mail_layout.Mail(summary="".join(summaries), fields=details,
                            advice=advice, level="urgent")


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
    from yiban.adapters.persistence import db
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
    """跑一次哨兵；返回进程退出码（0=检查完成，1=检查或告警失败）。

    触发器条数**未核也算检查完成（rc=0）**：读不到 crontab 是"这一面没看见"，
    不是"哨兵自己坏了"；未核原因打印到 stderr、缺包/漂移照常告警。把未核升格成
    rc=1 会让没有 crontab 命令或权限受限的主机天天在日志里红，淹没真故障。
    """
    backup_dir = _env("BACKUP_DIR", DEFAULT_BACKUP_DIR)
    # 部署根：唯一环境键 YIBAN_APP_DIR；APP_DIR 只作旧部署别名（census-P2-repo-root）。
    app_dir = _env("YIBAN_APP_DIR", "") or _env("APP_DIR", DEFAULT_APP_DIR)
    installed = _env("YIBAN_BACKUP_INSTALLED", DEFAULT_INSTALLED)
    day = _now()

    archive, sidecar_ok = _find_archive(backup_dir, day)
    # 明文包只在"密文不存在"时才需要单独识别——密文在则当日健康已满足，
    # 明文残留（加密切换过渡日）不双告警
    plaintext = None if archive is not None else _find_plaintext(backup_dir, day)
    repo_copy = os.path.join(app_dir, "scripts", "backup.sh")
    drift = _drift_report(repo_copy, installed)
    # 触发器条数门（2026-10-25 事故）：root crontab 与 /etc/cron.d 各一条同一 wrapper，
    # cron 02:00 并发跑两轮，gpg 互相覆写，当天备份全失。防漂移只看脚本内容，
    # 看不到"同一作业被排了两次"这件事。
    try:
        trigger_drift, trigger_unverified = _trigger_drift_report(
            os.path.join(app_dir, "deploy", "prod", "cron.d"),
            _env("YIBAN_CRON_D_DIR", DEFAULT_CRON_D_DIR),
            _env("YIBAN_SYSTEM_CRONTAB", DEFAULT_SYSTEM_CRONTAB))
    except Exception as e:
        # 本项异常不得带崩整个哨兵（它崩了，"备份没成"就没人喊）：
        # 降级为"未核"并保留可见性，其余判定照旧。
        trigger_drift, trigger_unverified = None, [f"触发器条数检查异常：{e!r}"]
    for reason in trigger_unverified:
        # 跳过 ≠ 绿（AGENTS §30）：读不到 crontab 时条数结论不完整，必须显性可见
        print(f"备份哨兵：触发器条数未核（{reason}）", file=sys.stderr)
    for finding in (trigger_drift or {}).get("findings", []):
        print(f"备份哨兵：触发器条数漂移——作业 {finding['job']} 在排期 "
              f"{finding['schedule']} 上有 {finding['count']} 条活跃触发器"
              f"（{finding['note']}）：" + "、".join(finding["sources"]))

    if archive is not None and sidecar_ok and drift is None and trigger_drift is None:
        print(f"备份哨兵：{day} 的归档与清单都在（{archive}），运行脚本与检出一致")
        try:
            _broadcast_anchor(day, archive, sidecar_ok)
        except Exception as e:  # 锚点外发绝不改变本函数的结论与退出码
            logger.warning("审计链锚点外发流程异常（已忽略）: %r", e)
        return 0

    if archive is not None and sidecar_ok:
        # 归档健康也要喊漂移：两个判据互不掩盖（脚本内容 / 触发器条数）。
        problems = []
        if drift is not None:
            problems.append(f"运行脚本与检出不一致（{drift['reason']}）")
        if trigger_drift is not None:
            problems.append(f"触发器条数漂移（{trigger_drift['reason']}）")
        print(f"备份哨兵：{day} 的归档在（{archive}），但" + "；".join(problems))
        mail = _drift_mail(repo_copy, installed, drift, trigger_drift)
    elif archive is None and plaintext is not None:
        # MF-79：明文包不算健康——当日只有裸 .tar.gz 时加密链路已失效（或被显式
        # 关闭），这正是"看着有备份、其实全库凭据裸奔"的那一格，必须发声
        print(f"备份哨兵：{day} 只有【明文】归档（{plaintext}），不计入健康，已发告警")
        mail = _missing_mail(day, backup_dir, archive, sidecar_ok, plaintext=plaintext,
                             drift=drift, trigger_drift=trigger_drift,
                             repo_copy=repo_copy, installed=installed)
    else:
        # 排在漂移之前：归档没成比脚本漂移严重，两病同发时先喊缺备份（同类型只发一封）。
        # 但漂移**必须同时写进这封**（P3-4）：只打 stdout 的漂移在 cron 日志里没人看，
        # 缺包那天正是最需要知道"链路还有没有别的病"的时候。
        print(f"备份哨兵：{day} 的备份不完整（归档 {archive or '缺失'}，"
              f"清单 {'在' if sidecar_ok else '缺失'}）")
        mail = _missing_mail(day, backup_dir, archive, sidecar_ok,
                             drift=drift, trigger_drift=trigger_drift,
                             repo_copy=repo_copy, installed=installed)

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
