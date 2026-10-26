# -*- coding: utf-8 -*-
"""`scripts/backup_sentinel.py` 与 `scripts/yiban-backup-sentinel.sh` 的契约用例。

标签：J · 运维：部署/备份/发布
覆盖：当日归档与 .sha256 清单齐不齐（只有密文形态 gpg/age 计入健康——M3 批次0
    明文模式护栏：明文包不再算数，且单独触发告警）、昨日包不算今日备份、
    运行拷贝与仓库版的漂移比对、跨进程节流真的接上、发不出去要能看见、
    wrapper 切工作目录与导出 .env、wrapper 头部文档与"只转发"契约、
    **审计链头哈希随备份离机外发**（M28：每天必发、与备份成败无关、分标题分节流、
    链头完整且独占一行、只读不写库、读失败与发不出都不吞结论）。
对应实现：`scripts/backup_sentinel.py`（判定与外发）、`scripts/yiban-backup-sentinel.sh`
    （cron 入口）；节流复用 `yiban.adapters.notifier.notify.ledger`。
关键断言：① 缺包/缺清单/漂移 → 恰好一封管理员告警且正文带排查路径；② 正常路径
    （都在且一致）**零失败告警**——哨兵自己不许变成噪音源（M28 起这条只约束"失败
    告警"；当天仍会**另外**外发一封锚点外发，断言在 `self.anchor_mails` 分账里）；
③ 未安装
    `YIBAN_BACKUP_INSTALLED` → 不报漂移，由缺包那一项兜底；④ 收件人为空/发送失败/
    发送抛异常 → 返回 1（这条"看不见"的兜底能走多远，见 `scripts/backup_sentinel.py`
    头部对 `>> backup.log 2>&1` 的提醒）；⑤ 第二次运行落在窗口内不再外发。
依赖：Python 判定部分进程内跑（临时目录 + 打桩 `_send_admin_alert`），不连 SMTP、
    不碰本机真实备份目录；wrapper 相关两条要 bash 与 python3（Git Bash/WSL），
    缺任一按 SkipTest 跳过整类；不需 docker。

⚠ 本文件不等于"备份会自动成功"：它只验**缺备份能不能被发现**。backup.sh 自身的
归档与加密路径由 tests/test_backup_require_encrypt.py 与
tests/test_deploy_paths_and_restore_verdict.py 分别钉。
"""
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from yiban.adapters.notifier.notify import ledger as notify_ledger
from yiban.core import clock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(BASE, "scripts", "backup_sentinel.py")
WRAPPER = os.path.join(BASE, "scripts", "yiban-backup-sentinel.sh")
REPO_CRON_D = os.path.join(BASE, "deploy", "prod", "cron.d")
BASH = shutil.which("bash")

#: 生产事故原文的命令行（2026-10-25/26）：root crontab 里与 /etc/cron.d/yiban-backup
#: 同一条 wrapper 调用。crontab 格式没有身份列；/etc/cron.d 格式有。
WRAPPER_CRON_LINE = ("0 2 * * * /usr/local/sbin/yiban-backup-wrapper.sh"
                     " >> /var/log/yiban/backup.log 2>&1")
#: /etc/cron.d 形态：五个时间字段之后是身份列。文件里的行必须能被真解析出来，
#: 否则"含点号文件不计入"的用例没有判别力（丢弃身份列后命令首段变成重定向）。
WRAPPER_CRON_D_LINE = ("0 2 * * * root /usr/local/sbin/yiban-backup-wrapper.sh"
                       " >> /var/log/yiban/backup.log 2>&1")

#: crontab 测试桩：只实现 `-l` 与 `-l -u root`。内容从环境变量指向的文件读；
#: 文件不存在时按真 crontab 的口径 rc=1 + "no crontab for"。FAKE_CRONTAB_ROOT_ERROR
#: 非空时模拟"权限不足"（例如非 root 身份查询 root 的 crontab）。
FAKE_CRONTAB = """#!/usr/bin/env bash
if [ "${1:-}" != "-l" ]; then
  echo "fake crontab: unsupported args: $*" >&2
  exit 2
fi
if [ "${2:-}" = "-u" ]; then
  if [ "${3:-}" = "root" ]; then
    if [ -n "${FAKE_CRONTAB_ROOT_ERROR:-}" ]; then
      echo "$FAKE_CRONTAB_ROOT_ERROR" >&2
      exit 1
    fi
    if [ -f "${FAKE_CRONTAB_ROOT:-}" ]; then cat "$FAKE_CRONTAB_ROOT"; exit 0; fi
    echo "no crontab for root" >&2
    exit 1
  fi
  # 其它身份：一律返回"当前身份"的内容文件（供"两种身份都读"的用例使用）
  if [ -f "${FAKE_CRONTAB_CURRENT:-}" ]; then cat "$FAKE_CRONTAB_CURRENT"; exit 0; fi
  echo "no crontab for ${3:-unknown}" >&2
  exit 1
fi
if [ -f "${FAKE_CRONTAB_CURRENT:-}" ]; then cat "$FAKE_CRONTAB_CURRENT"; exit 0; fi
echo "no crontab for current" >&2
exit 1
"""


def _write_bytes(path, payload, mode=None):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with io.open(path, "wb") as f:
        f.write(payload)
    if mode is not None:
        os.chmod(path, mode)
    return path


def _install_repo_tables(dest_dir, render=True):
    """把仓库 cron 表按 install.sh 的口径落位（渲染 @YIBAN_APP_DIR@）。

    只收 `deploy/prod/cron.d` 里的**普通文件**：作业键是命令里可执行件的文件名，
    占位符渲染与否不影响归类；渲染是为了贴近生产落位形态。
    """
    os.makedirs(dest_dir, exist_ok=True)
    for name in sorted(os.listdir(REPO_CRON_D)):
        src = os.path.join(REPO_CRON_D, name)
        if not os.path.isfile(src):
            continue
        with io.open(src, encoding="utf-8") as f:
            text = f.read()
        if render:
            text = text.replace("@YIBAN_APP_DIR@", "/opt/yiban-auto-sign")
        with io.open(os.path.join(dest_dir, name), "w", encoding="utf-8",
                     newline="\n") as f:
            f.write(text)
    return dest_dir


def _fake_crontab_bin(bin_dir):
    return _write_bytes(os.path.join(bin_dir, "crontab"),
                        FAKE_CRONTAB.encode("utf-8"), 0o755)


def _load_script():
    """按模块载入脚本：要打桩发送出口与节流，只能对同一 `main()` 注入。"""
    spec = importlib.util.spec_from_file_location("_backup_sentinel_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_script()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-bksentinel-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # 节流表是进程级的（内存快速路径 + 磁盘权威）：用例之间必须复位，否则前一个
        # 用例放行过的标题会把后面所有用例挡在窗口内（磁盘表按 YIBAN_STATE_DIR 隔离，
        # 内存表不会）。
        notify_ledger._throttle_ts.clear()
        self.addCleanup(notify_ledger._throttle_ts.clear)  #内存表不清则后一个用例被前一个的窗口挡住：磁盘表按临时 STATE 隔离，内存表不会
        self.backup_dir = os.path.join(self.tmp, "backups")
        self.app_dir = os.path.join(self.tmp, "app")
        self.state_dir = os.path.join(self.tmp, "state")
        self.installed = os.path.join(self.tmp, "sbin", "yiban-backup.sh")
        for path in (self.backup_dir, os.path.join(self.app_dir, "scripts"),
                     self.state_dir, os.path.dirname(self.installed)):
            os.makedirs(path, exist_ok=True)
        # 仓库版备份脚本（比对对象）
        with io.open(os.path.join(self.app_dir, "scripts", "backup.sh"), "w",
                     encoding="utf-8") as f:
            f.write("#!/bin/bash\necho 仓库版\n")
        with io.open(os.path.join(self.tmp, ".env"), "w", encoding="utf-8") as f:
            f.write("")
        self._env_patch = mock.patch.dict(os.environ, {
            "BACKUP_DIR": self.backup_dir,
            "APP_DIR": self.app_dir,
            "YIBAN_BACKUP_INSTALLED": self.installed,
            "YIBAN_STATE_DIR": self.state_dir,
            "YIBAN_ENV_FILE": os.path.join(self.tmp, ".env"),
            "YIBAN_NOTIFY_COOLDOWN": "60",
            # 锚点外发要读审计链：把库路径钉到临时区，绝不让哨兵在进程里隐式
            # init_db 到 cwd（= 仓库根）建一份野 yiban.db。
            "YIBAN_DB_FILE": os.path.join(self.tmp, "yiban.db"),
        })
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)
        self.mails = []
        # 锚点外发（M28）与失败告警**分账**：它每天必发，若混进 self.mails 会让
        # 「失败时恰好一封」的既有用例全部读成两封。标题不同、节流键也不同。
        self.anchor_mails = []

    # ---- 脚手架 ----
    def _write_archive(self, suffix=".tar.gz.gpg", sidecar=True, day=None):
        day = day or self.mod._now()
        path = os.path.join(self.backup_dir, f"yiban-{day}{suffix}")
        with io.open(path, "wb") as f:
            f.write(b"ciphertext")
        if sidecar:
            with io.open(path + ".sha256", "w", encoding="utf-8") as f:
                f.write("deadbeef  " + os.path.basename(path) + "\n")  #清单内容不参与判定，齐不齐才是判据——这里连哈希都不用真
        return path

    def _install_copy(self, content="#!/bin/bash\necho 仓库版\n"):
        with io.open(self.installed, "w", encoding="utf-8") as f:
            f.write(content)
        return self.installed

    def _default_snapshot(self):
        """锚点快照的桩（不进真库）：`_anchor_snapshot` 的读库路径另有专测。"""
        return {"state": "ok", "head": "a" * 64, "count": 42}

    def _send(self, title, mail):
        bucket = (self.anchor_mails if title == self.mod.ANCHOR_TITLE else self.mails)
        bucket.append((title, mail))
        return True

    def _run(self, *, due=None, send=None, snapshot=None):
        """跑一次 main()：发送出口默认打桩为记录调用（不碰 SMTP）。

        `snapshot` 默认给一份桩快照：`_anchor_snapshot()` 会走 `db.init_db()`，
        那是对**进程级单例连接**的真实初始化——在测试里做会把同一 worker 后续所有
        用例的库指到临时区（-n 8 并发下污染面更大）。锚点读库路径的断言放在
        `AuditAnchorBroadcastTest`，那里直接打桩 `yiban.adapters.persistence.db` 的三个读函数。
        """
        sender = send or self._send
        with mock.patch.object(self.mod, "_send_admin_alert", side_effect=sender), \
                mock.patch.object(
                    self.mod, "_anchor_snapshot",
                    side_effect=snapshot if snapshot is not None else self._default_snapshot):
            if due is None:
                return self.mod.main([])
            with mock.patch.object(self.mod, "_alert_due", side_effect=due):  #只打桩"该不该发"，发送出口仍是记录器：节流路径不许被桩绕过
                return self.mod.main([])


class SentryVerdictTest(_Base):
    """缺什么喊什么；什么都不缺就安静（安静指的是**不喊失败**，见 M28 的锚点外发）。"""

    def test_archive_and_sidecar_present_is_silent(self):
        self._write_archive()
        self.assertEqual(self._run(), 0)
        self.assertEqual(self.mails, [], "正常路径不得外发任何告警")

    def test_age_form_still_accepted(self):
        """age 形态是密文，认：认不出会在合法部署上误报"没有备份"，那正是要消灭的噪音。

        M3 批次0（明文模式护栏）：`.tar.gz`（明文）从"也认"名单里移除——"明文包直接满足
        当日包存在=健康"曾让告警链整体静默；明文判定见 test_plaintext_only_is_unhealthy。
        """
        self._write_archive(suffix=".tar.gz.age")
        self.assertEqual(self._run(), 0)
        self.assertEqual(self.mails, [])

    def test_plaintext_only_is_unhealthy(self):
        """验收不变量反例：目录里只放明文 .tar.gz（哪怕带清单）⇒ 哨兵必须告警。"""
        self._write_archive(suffix=".tar.gz")
        rc = self._run()
        self.assertEqual(rc, 0, "检查完成（告警已发），退出码语义不变")
        self.assertEqual(len(self.mails), 1, f"明文包不得计入健康，实际 {self.mails}")
        body = self.mails[0][1].to_plain()
        self.assertIn("明文", body, "告警要说清为什么不健康：只有明文包 = 加密链路失效")
        self.assertIn(self.backup_dir, body, "正文要给到运维排查的路径")

    def test_plaintext_not_matched_by_health_probe(self):
        """判定原语本身：_find_archive 不得把明文包认作当日归档。"""
        path = self._write_archive(suffix=".tar.gz")
        found, _ = self.mod._find_archive(self.backup_dir, self.mod._now())
        self.assertIsNone(found, f"明文包被计为健康归档：{found}")
        self.assertNotIn(path, self.mod._archive_candidates(self.backup_dir, self.mod._now()),
                         "健康候选名单里不应再有裸 .tar.gz")

    def test_plaintext_alongside_encrypted_is_silent(self):
        """同日既有密文又有明文残留（加密切换的过渡日）：密文已满足健康，不双告警。"""
        self._write_archive(suffix=".tar.gz.gpg")
        self._write_archive(suffix=".tar.gz")
        self.assertEqual(self._run(), 0)
        self.assertEqual(self.mails, [])

    def test_missing_archive_alerts_with_expected_path(self):
        self.assertEqual(self._run(), 0)
        self.assertEqual(len(self.mails), 1, f"缺包必须发声，实际 {self.mails}")
        title, mail = self.mails[0]
        self.assertEqual(title, "备份失败哨兵告警")
        body = mail.to_plain()
        self.assertIn(self.mod._now(), body)
        self.assertIn("不存在", body)
        self.assertIn(self.backup_dir, body, "正文要给到运维排查的路径")

    def test_missing_sidecar_alerts_even_when_archive_present(self):
        self._write_archive(sidecar=False)
        self.assertEqual(self._run(), 0)
        self.assertEqual(len(self.mails), 1, f"缺清单同样是不可核验的备份，实际 {self.mails}")
        body = self.mails[0][1].to_plain()
        self.assertIn(".sha256", body)
        self.assertIn("核验", body)

    def test_yesterdays_archive_does_not_satisfy_today(self):
        """昨天的包不等于今天的备份——按包名精确比对，不做"目录非空"式的宽松判定。"""
        self._write_archive(day="2000-01-01")
        self.assertEqual(self._run(), 0)
        self.assertEqual(len(self.mails), 1, f"实际 {self.mails}")

    def test_zero_byte_plaintext_is_missing_not_plaintext(self):
        """0 字节明文是残留，归因必须是「包缺失」（第 5 项）。"""
        path = self._write_archive(suffix=".tar.gz")
        with io.open(path, "wb"):
            pass
        self.assertEqual(self._run(), 0)
        self.assertEqual(len(self.mails), 1, f"实际 {self.mails}")
        body = self.mails[0][1].to_plain()
        self.assertIn("不存在", body, "0 字节明文被归到缺包分支")
        self.assertNotIn("只有【明文】", body, "0 字节明文不该说成只有明文包")


class ArchiveProbeTest(_Base):
    """0 字节归档/清单不得判健康（P3-2）：只判 `isfile` 会被空文件骗过。"""

    def test_zero_byte_archive_is_not_found_and_alerts(self):
        path = self._write_archive()
        with io.open(path, "wb"):
            pass  # 0 字节：加密切换/半写入的残留形状
        found, sidecar_ok = self.mod._find_archive(self.backup_dir, self.mod._now())
        self.assertIsNone(found, f"0 字节归档被当成健康备份：{found}")
        self.assertFalse(sidecar_ok)
        self.assertEqual(self._run(), 0)
        self.assertEqual(len(self.mails), 1, f"0 字节归档必须走缺失告警：{self.mails}")
        self.assertIn("不存在", self.mails[0][1].to_plain())

    def test_zero_byte_sidecar_is_not_ok(self):
        path = self._write_archive()
        with io.open(path + ".sha256", "wb"):
            pass  # 0 字节清单：2026-10-25 事故的残留形状
        found, sidecar_ok = self.mod._find_archive(self.backup_dir, self.mod._now())
        self.assertEqual(path, found, "归档本身非空，仍应被识别")
        self.assertFalse(sidecar_ok, "0 字节清单被当成'清单在'（不可核验的备份）")
        self.assertEqual(self._run(), 0)
        self.assertEqual(len(self.mails), 1, f"0 字节清单必须告警：{self.mails}")
        self.assertIn(".sha256", self.mails[0][1].to_plain())


class DriftTest(_Base):
    """"运行脚本 = 仓库脚本"的比对：漂移要喊，一致/未安装要安静。"""

    def test_identical_copy_is_silent(self):
        self._write_archive()
        self._install_copy()
        self.assertEqual(self._run(), 0)
        self.assertEqual(self.mails, [], "一致时任何输出都是噪音")

    def test_installed_copy_absent_is_silent(self):
        """未按约定安装（自定义路径/单机试用）：无从比对，不报——缺包那一项兜底。"""
        self._write_archive()
        self.assertEqual(self._run(), 0)
        self.assertEqual(self.mails, [])

    def test_drifted_copy_alerts_with_fingerprints(self):
        self._write_archive()
        self._install_copy("#!/bin/bash\necho 旧拷贝\n")
        self.assertEqual(self._run(), 0)
        self.assertEqual(len(self.mails), 1, f"漂移必须发声，实际 {self.mails}")
        body = self.mails[0][1].to_plain()
        self.assertIn("不一致", body)
        # 措辞口径：比对的两侧是「检出」与「运行版」。写成"仓库版"比实际证明的窄
        # ——仓库远端并不参与比对，比的是 $APP_DIR 下的检出。
        self.assertIn("检出", body)
        self.assertIn("运行版", body)
        self.assertIn("/usr/local/sbin/yiban-backup.sh", body, "要给出以检出为准的重装指引")

    def test_drift_alerts_even_when_backup_is_fine(self):
        """两个判据互不掩盖：备份正常但脚本漂移，仍须单独喊一声。"""
        self._write_archive()
        self._install_copy("#!/bin/bash\n# 漂移\n")
        self.assertEqual(self._run(), 0)
        self.assertEqual(len(self.mails), 1)
        self.assertIn("不一致", self.mails[0][1].to_plain())


class ThrottleTest(_Base):
    """节流接的是既有磁盘表：第二次运行不再外发（cron 每次新进程，进程内表在这里失效）。"""

    def test_second_run_in_window_is_throttled(self):
        first = self._run()
        self.assertEqual(first, 0)
        self.assertEqual(len(self.mails), 1)
        second = self._run()
        self.assertEqual(second, 0, "被节流不算失败")
        self.assertEqual(len(self.mails), 1, f"窗口内不得重复外发，实际 {self.mails}")

    def test_zero_cooldown_always_sends(self):
        with mock.patch.dict(os.environ, {"YIBAN_NOTIFY_COOLDOWN": "0"}):
            self.assertEqual(self._run(), 0)
            self.assertEqual(self._run(), 0)
        self.assertEqual(len(self.mails), 2, "0=关闭节流")


class DeliveryFailureTest(_Base):
    """发不出去必须与"检查通过"可区分：返回 1（不承诺有人因此收到信——退出码能否
    变成一次通知，取决于 cron 排期行有没有把 stderr 吞进日志）。"""

    def test_send_returning_false_exits_nonzero(self):
        self.assertEqual(self._run(send=lambda title, mail: False), 1)

    def test_send_raising_exits_nonzero_without_traceback(self):
        def _boom(title, mail):
            raise RuntimeError("smtp down")

        self.assertEqual(self._run(send=_boom), 1, "组件异常不得外泄成栈，但必须非 0")

    def test_empty_recipients_is_visible(self):
        """收件人为空 → _send_admin_alert 真实实现返回 False → 退出码 1。"""
        with mock.patch("web.services.notify_mail._alert_mail_recipients", return_value=[]):
            self.assertEqual(self.mod.main([]), 1)


class NeverCreatesDbTest(_Base):
    """r4 复审 MEDIUM-1：哨兵以 root 跑（排期件 /etc/cron.d/yiban-backup-sentinel 的身份列
    = root）。库缺失时**绝不建库**——否则 root 属主的空库会顶掉 yiban 属主的真库，
    yiban 侧 web 随后打不开它。"""

    def test_missing_db_is_not_created_and_env_recipients_are_used(self):
        db_path = os.environ["YIBAN_DB_FILE"]
        self.assertFalse(os.path.exists(db_path), "夹具的库路径本应不存在")
        captured = {}

        def _capture(title, mail, to=None, **_kw):
            captured["to"] = to
            return True

        with mock.patch.dict(os.environ, {"YIBAN_MAIL_ADMIN_TO": "ops@example.com"}), \
                mock.patch.object(self.mod.mailer, "send_admin_alert", side_effect=_capture):
            ok = self.mod._send_admin_alert(self.mod.ALERT_TITLE, mock.Mock())
        self.assertTrue(ok, "库缺失时仍应能用 .env 收件人把告警发出去")
        self.assertFalse(os.path.exists(db_path),
                         "库缺失时 _send_admin_alert 竟就地建库（root 属主空库会顶掉真库）")
        self.assertEqual("ops@example.com", captured.get("to"),
                         "库缺失时收件人必须退到 .env 的 ADMIN_TO（与库查询失败同一降级结论）")


class AuditAnchorBroadcastTest(_Base):
    """M28：当日审计链头哈希**随备份外发**——备份与锚点同包同盘时无法证明没被回滚。

    验收不变量（逐条都能判错）：
    1. **每天必发**，与备份成败无关（只在该喊的时候发 = 把"安静那天正好被回滚"
       继续留在盲区里）；
    2. 走**既有**告警出口（`_send_admin_alert`），不新建通道/凭据；
    3. 与失败告警**分标题分节流**：一天既喊"备份没成"又发"链头离机"是常态，
       两封都要发，互相不得挡；
    4. 正文带**完整**链头哈希（通道健康日报只带前 12 位，那是给人看的，这里是
       比对用的）+ 记录数 + 锚点末行 + 对应的备份包名；
    5. 读库/读锚点失败或发不出去时**不得抛**、不得把"没发成"说成成功。
    """

    def test_anchor_broadcast_happens_on_a_healthy_day(self):
        """活体反例：备份齐全（不喊失败）的那天，锚点**照样**外发一封。"""
        self._write_archive()
        self.assertEqual(self._run(), 0)
        self.assertEqual(self.mails, [], "健康日不得发失败告警")
        self.assertEqual(len(self.anchor_mails), 1,
                         f"健康日也必须外发当日链头（离机基线），实际 {self.anchor_mails}")
        title, mail = self.anchor_mails[0]
        self.assertEqual(title, self.mod.ANCHOR_TITLE)
        self.assertNotEqual(title, self.mod.ALERT_TITLE, "两件事必须分标题分节流键")
        body = mail.to_plain()
        self.assertIn("a" * 64, body, "正文必须带**完整**链头哈希（恢复后靠它比对）")
        self.assertNotIn("a" * 12 + "…", body, "不得截断——截断后无法用于比对")
        # 独占一整行：渲染器 72 列硬切，折行后的哈希抄下来对不上，正好毁掉这封的用途
        lines = [ln.strip() for ln in body.splitlines()]
        self.assertIn("a" * 64, lines,
                      "链头哈希必须独占一整行（被折行就等于没有）")
        self.assertIn("42", body, "记录数要一起出箱")
        self.assertIn(self.mod._now(), body,
                      "业务日是「这份基线属于哪一天」的唯一线索")

    def test_anchor_broadcast_also_happens_when_backup_is_missing(self):
        """备份没成的那天**同样**要外发：链头是库的现状，与包在不在无关。"""
        self.assertEqual(self._run(), 0)
        self.assertEqual(len(self.mails), 1, "缺包仍发失败告警")
        self.assertEqual(len(self.anchor_mails), 1,
                         "失败告警不得把当天的离机基线挤掉（分标题分节流）")

    def test_anchor_mail_names_the_backup_package_it_belongs_to(self):
        """锚点必须与「那天该用哪个备份包」绑定：恢复现场才知道拿哪份哈希比对。"""
        archive = self._write_archive()
        self._run()
        body = self.anchor_mails[0][1].to_plain()
        self.assertIn(os.path.basename(archive), body)

    def test_empty_chain_is_reported_as_empty_not_as_a_blank_hash(self):
        """空链是**查清了的结论**，不能印成一个空字段让人误以为"值丢了"。"""
        self._write_archive()
        snap = {"state": "empty", "head": "", "count": 0}
        self._run(snapshot=lambda: snap)
        body = self.anchor_mails[0][1].to_plain()
        self.assertIn("空链", body)

    def test_anchor_read_failure_is_swallowed_and_logged_not_raised(self):
        """读不到链头：不得把整个哨兵带崩（cron 会天天红），也不得假装成功。"""
        self._write_archive()

        def _boom():
            raise RuntimeError("库读不了")

        rc = self._run(snapshot=_boom)
        self.assertEqual(rc, 0, "读锚点失败是旁路观测件的事，不该改哨兵退出码")
        self.assertEqual(self.anchor_mails, [], "读不到就没有基线可发，不得编一个")

    def test_anchor_send_failure_does_not_break_the_health_verdict(self):
        """外发失败时哨兵的**备份判定**结论不变（退出码仍是 0，检查确实做完了）。"""
        self._write_archive()

        def _send(title, mail):
            if title == self.mod.ANCHOR_TITLE:
                return False
            self.mails.append((title, mail))
            return True

        self.assertEqual(self._run(send=_send), 0)
        self.assertEqual(self.mails, [], "锚点发不出去不该反过来造出一条失败告警")

    def test_anchor_snapshot_reads_chain_head_without_initializing_with_defaults(self):
        """读链头必须显式 `init_db(cleanup=False, migrate=False)`。

        `connection.get_conn()` 的隐式 `init_db()` 是**全套缺省**
        （cleanup=True / migrate=True）：让这个 cron 进程顺手做一次启动清理/迁移，
        等于把只读取证变成一次计划外的写库动作。这里直接断言那两个参数。
        """
        seen = {}

        def _fake_init(*args, **kwargs):
            seen["args"] = args
            seen["kwargs"] = kwargs

        class _FakeDb:
            init_db = staticmethod(_fake_init)

            @staticmethod
            def audit_head_hash_ex():
                return "ok", "d" * 64

            @staticmethod
            def audit_row_count():
                return 7

        with (mock.patch.dict("sys.modules", {"yiban.adapters.persistence.db": _FakeDb}),
              mock.patch("yiban.adapters.persistence.db", _FakeDb)):
            snap = self.mod._anchor_snapshot()
        self.assertEqual(seen["kwargs"].get("cleanup"), False,
                         "只读取证不得触发启动清理")
        self.assertEqual(seen["kwargs"].get("migrate"), False,
                         "只读取证不得触发迁移（迁移会回填审计链）")
        self.assertEqual(snap["head"], "d" * 64)
        self.assertEqual(snap["count"], 7)

    def test_anchor_snapshot_returns_none_when_chain_head_unreadable(self):
        """`audit_head_hash_ex` 的 error 态（读失败）必须与 empty（空链）分开。

        把读失败当空链，会把"没查成"印成"没有"——而那正是最该看见的时刻。
        """
        class _FakeDb:
            init_db = staticmethod(lambda *a, **k: None)

            @staticmethod
            def audit_head_hash_ex():
                return "error", None

        with (mock.patch.dict("sys.modules", {"yiban.adapters.persistence.db": _FakeDb}),
              mock.patch("yiban.adapters.persistence.db", _FakeDb)):
            self.assertIsNone(self.mod._anchor_snapshot())


class TriggerCountTest(_Base):
    """同一作业的活跃触发器必须恰好 1 条（2026-10-25 生产事故的运行期门）。

    缺陷形状：root crontab 与 /etc/cron.d 各有一条同一 wrapper 命令，cron 每天
    02:00 并发跑两轮同一备份。哨兵此前只比"已装脚本 vs 检出脚本"，看不到触发器。
    """

    def setUp(self):
        super().setUp()
        # 检出侧的 cron 表（期望面）：按 install.sh 口径落位到临时 APP_DIR
        _install_repo_tables(os.path.join(self.app_dir, "deploy", "prod", "cron.d"))
        # 生产侧 /etc/cron.d 的夹具（已按 install.sh 落位，六张表各一条）
        self.cron_d = _install_repo_tables(os.path.join(self.tmp, "etc-cron.d"))
        self.bin = os.path.join(self.tmp, "bin")
        _fake_crontab_bin(self.bin)
        self.root_crontab = os.path.join(self.tmp, "root-crontab")
        self.current_crontab = os.path.join(self.tmp, "current-crontab")
        _write_bytes(self.root_crontab, b"")
        _write_bytes(self.current_crontab, b"")
        patch = mock.patch.dict(os.environ, {
            "PATH": self.bin + os.pathsep + os.environ.get("PATH", ""),
            "YIBAN_CRON_D_DIR": self.cron_d,
            "YIBAN_SYSTEM_CRONTAB": os.path.join(self.tmp, "absent-etc-crontab"),
            "FAKE_CRONTAB_ROOT": self.root_crontab,
            "FAKE_CRONTAB_CURRENT": self.current_crontab,
        })
        patch.start()
        self.addCleanup(patch.stop)

    def _write_root(self, text):
        _write_bytes(self.root_crontab, text.encode("utf-8"))

    def test_duplicate_trigger_alerts_on_the_existing_channel_and_names_it(self):
        self._write_archive()
        self._write_root(WRAPPER_CRON_LINE + "\n")
        self.assertEqual(self._run(), 0)
        self.assertEqual(len(self.mails), 1, f"重复触发器必须发声，实际 {self.mails}")
        title, mail = self.mails[0]
        self.assertEqual(title, self.mod.ALERT_TITLE,
                         "必须复用既有告警通道（同标题=同节流键），不新造通道")
        body = mail.to_plain()
        self.assertIn("yiban-backup-wrapper.sh", body, "正文必须点名作业")
        self.assertIn("0 2 * * *", body, "正文必须点名排期")
        self.assertIn("2 条", body, "正文必须给出条数")
        self.assertIn("root 的 crontab", body, "正文必须给出每条触发器的来源")
        self.assertIn(os.path.join(self.cron_d, "yiban-backup"), body)
        # 第 6 项：重复场景的摘要必须说「多条」，且不得混入未声明排期措辞
        self.assertIn("多条", mail.summary)
        self.assertNotIn("未在仓库表声明", mail.summary)

    def test_single_trigger_per_job_is_quiet(self):
        self._write_archive()
        self.assertEqual(self._run(), 0)
        self.assertEqual(self.mails, [], f"每作业一条触发器时不得告警：{self.mails}")

    def _empty_cron_d(self):
        path = os.path.join(self.tmp, "empty-cron.d")
        os.makedirs(path, exist_ok=True)
        return path

    def test_both_crontab_identities_are_read(self):
        """`crontab -l -u root` 与 `crontab -l` 都必须读：重复行可能只在其一。"""
        self._write_root(WRAPPER_CRON_LINE + "\n")
        _write_bytes(self.current_crontab, (WRAPPER_CRON_LINE + "\n").encode("utf-8"))
        with mock.patch.object(self.mod, "_current_user", return_value="yiban"):
            entries, unverified = self.mod._active_triggers(
                self._empty_cron_d(), os.path.join(self.tmp, "absent-etc-crontab"))
        self.assertEqual([], unverified)
        sources = [src for src, _schedule, command in entries
                   if self.mod._job_key(command) == "yiban-backup-wrapper.sh"]
        self.assertIn("root 的 crontab", sources, f"没有读 root 的 crontab：{sources}")
        self.assertIn("yiban 的 crontab", sources, f"没有读当前身份的 crontab：{sources}")

    def test_root_identity_is_not_counted_twice(self):
        """哨兵以 root 跑：`-l` 与 `-l -u root` 是同一条 crontab，不得算成两条。"""
        self._write_root(WRAPPER_CRON_LINE + "\n")
        with mock.patch.object(self.mod, "_current_user", return_value="root"):
            entries, unverified = self.mod._active_triggers(
                self._empty_cron_d(), os.path.join(self.tmp, "absent-etc-crontab"))
        self.assertEqual([], unverified)
        matches = [e for e in entries
                   if self.mod._job_key(e[2]) == "yiban-backup-wrapper.sh"]
        self.assertEqual(1, len(matches),
                         f"root 身份下同一条 crontab 被读了两次（会假报重复）：{matches}")

    def test_other_job_duplicate_is_caught_by_the_same_gate(self):
        """作业识别必须通用：sign 的 run.sh 出现第二条触发器同样判漂移。"""
        self._write_archive()
        _write_bytes(os.path.join(self.cron_d, "yiban-sign-dup"),
                     ("31 6 * * * yiban /opt/yiban-auto-sign/run.sh"
                      " >> /var/log/yiban/sign-dup.log 2>&1\n").encode("utf-8"))
        self.assertEqual(self._run(), 0)
        self.assertEqual(len(self.mails), 1, f"同一形状的重复必须被同一条门抓到：{self.mails}")
        body = self.mails[0][1].to_plain()
        self.assertIn("run.sh", body)
        self.assertIn("2 条", body)

    def test_extra_schedule_for_same_job_is_drift(self):
        """同一作业多排一个时刻也是重复触发（一天跑两遍），必须点名。"""
        self._write_archive()
        self._write_root("0 3 * * * /usr/local/sbin/yiban-backup-wrapper.sh"
                         " >> /var/log/yiban/backup.log 2>&1\n")
        self.assertEqual(self._run(), 0)
        self.assertEqual(len(self.mails), 1, f"未声明的排期必须判漂移：{self.mails}")
        body = self.mails[0][1].to_plain()
        self.assertIn("0 3 * * *", body)

    def test_findings_survive_an_unreadable_source(self):
        """未核不得把已发现的重复吞掉：两者并存返回（P3-3，跳过≠绿）。"""
        self._write_root(WRAPPER_CRON_LINE + "\n" + WRAPPER_CRON_LINE + "\n")
        report, unverified = self.mod._trigger_drift_report(
            os.path.join(self.app_dir, "deploy", "prod", "cron.d"),
            os.path.join(self.tmp, "absent-cron.d"),
            os.path.join(self.tmp, "absent-etc-crontab"))
        self.assertTrue(unverified, "缺一面来源时必须报未核")
        self.assertIsNotNone(report, "未核把已发现的重复吞掉了（report=None）")
        self.assertEqual(1, len(report["findings"]), report)
        self.assertEqual("yiban-backup-wrapper.sh", report["findings"][0]["job"])
        self.assertEqual(2, report["findings"][0]["count"])

    def test_drift_summary_uses_structured_flag_not_note_text(self):
        """第 P2-1 项：摘要只认 undeclared 布尔字段，note 文案改了也不退化。"""
        findings = [
            {"job": "yiban-backup-wrapper.sh", "schedule": "0 2 * * *",
             "count": 2, "sources": ["root 的 crontab"],
             "note": "生产者改过的文案甲", "undeclared": False},
            {"job": "yiban-backup-wrapper.sh", "schedule": "0 3 * * *",
             "count": 1, "sources": ["root 的 crontab"],
             "note": "生产者改过的文案乙", "undeclared": True},
        ]
        summary = self.mod._trigger_drift_summary(findings)
        self.assertEqual(
            "同一作业的触发器与仓库表声明不一致（含重复触发器与未声明排期）。",
            summary,
            "两形状并存时 note 文案变化不得让摘要退化成只剩「多条」")

    def test_findings_carry_structured_undeclared_flag(self):
        """第 P4-3 项：未声明排期用布尔字段判定，不靠 note 文案。"""
        repo = os.path.join(self.app_dir, "deploy", "prod", "cron.d")
        absent = os.path.join(self.tmp, "absent-etc-crontab")
        self._write_root("0 3 * * * /usr/local/sbin/yiban-backup-wrapper.sh\n")
        report, _ = self.mod._trigger_drift_report(repo, self.cron_d, absent)
        self.assertIsNotNone(report)
        self.assertTrue(report["findings"][0]["undeclared"])
        self._write_root(WRAPPER_CRON_LINE + "\n")
        report, _ = self.mod._trigger_drift_report(repo, self.cron_d, absent)
        self.assertIsNotNone(report)
        self.assertFalse(report["findings"][0]["undeclared"])

    def test_trigger_finding_is_in_the_mail_when_archive_is_missing(self):
        """归档缺失时触发器漂移必须进邮件（P3-4）：本批的意义就是告警。"""
        self._write_root(WRAPPER_CRON_LINE + "\n")
        self._install_copy("#!/bin/bash\n# 漂移\n")
        self.assertEqual(self._run(), 0)
        self.assertEqual(len(self.mails), 1,
                         f"缺包 + 触发器/脚本漂移必须恰好一封告警：{self.mails}")
        body = self.mails[0][1].to_plain()
        self.assertIn("不存在", body, "缺包仍是首要结论")
        self.assertIn("yiban-backup-wrapper.sh", body,
                      "归档缺失时触发器漂移只进 stdout，没进邮件")
        self.assertIn("2 条", body, "邮件必须给出触发器条数")
        self.assertIn("不一致", body, "归档缺失时脚本漂移同样不得被吞")
        # 第 P3-1 项：2 条重复才可以说并发
        self.assertIn("并发", self.mails[0][1].summary,
                      "2 条重复应说明并发，摘要不得把并发删掉")

    def test_missing_archive_summary_does_not_claim_concurrency_for_one_trigger(self):
        """第 P3-1 项：缺包 + 未声明排期 count=1，摘要不得说并发。"""
        self._write_root("0 3 * * * /usr/local/sbin/yiban-backup-wrapper.sh"
                         " >> /var/log/yiban/backup.log 2>&1\n")
        self.assertEqual(self._run(), 0)
        self.assertEqual(len(self.mails), 1, f"实际 {self.mails}")
        summary = self.mails[0][1].summary
        self.assertIn("不存在", summary)
        self.assertIn("未在仓库表声明", summary)
        self.assertNotIn("并发", summary,
                         f"1 条触发器不存在并发，摘要不得断言：{summary}")

    def test_wrapped_job_forms_are_recognized(self):
        """包装器把脚本放在位置参数里：任一 token 命中作业键即归入该作业（P3-5）。"""
        forms = [
            "flock -n /run/lock/yiban-backup.lock"
            " /usr/local/sbin/yiban-backup-wrapper.sh",
            "timeout 600 /usr/local/sbin/yiban-backup-wrapper.sh",
            "nice -n 10 /usr/local/sbin/yiban-backup-wrapper.sh",
            "bash /usr/local/sbin/yiban-backup-wrapper.sh",
        ]
        for form in forms:
            with self.subTest(form=form):
                self._write_root(f"0 2 * * * {form} >> /var/log/yiban/backup.log 2>&1\n")
                report, unverified = self.mod._trigger_drift_report(
                    os.path.join(self.app_dir, "deploy", "prod", "cron.d"),
                    self.cron_d, os.path.join(self.tmp, "absent-etc-crontab"))
                self.assertEqual([], unverified)
                self.assertIsNotNone(report, f"包装器形态漏检：{form}")
                self.assertTrue(
                    any(f["job"] == "yiban-backup-wrapper.sh"
                        for f in report["findings"]),
                    f"没有归到被包装的作业：{report}")

    def test_attached_option_values_do_not_hide_the_job(self):
        """第 P3-2 项：`--opt=value` 紧贴形态仍要认出被包装的作业。"""
        empty = self._empty_cron_d()
        repo = os.path.join(self.app_dir, "deploy", "prod", "cron.d")
        absent = os.path.join(self.tmp, "absent-etc-crontab")
        for command in (
            "sudo --user=root /usr/local/sbin/yiban-backup-wrapper.sh",
            "nice --adjustment=10 /usr/local/sbin/yiban-backup-wrapper.sh",
        ):
            with self.subTest(command=command):
                self._write_root(f"0 3 * * * {command}\n")
                report, unverified = self.mod._trigger_drift_report(
                    repo, empty, absent)
                self.assertEqual([], unverified)
                self.assertIsNotNone(report, f"紧贴选项值漏检：{command}")
                self.assertEqual("0 3 * * *", report["findings"][0]["schedule"])
        # 分离值 `--warn-if wrapper.sh` 仍不命中
        self._write_root(
            "0 3 * * * --warn-if /usr/local/sbin/yiban-backup-wrapper.sh\n")
        report, unverified = self.mod._trigger_drift_report(repo, empty, absent)
        self.assertEqual([], unverified)
        self.assertIsNone(report, f"分离值 --warn-if 不应命中：{report}")

    def test_installed_backup_sh_is_the_same_job_as_wrapper(self):
        """安装版 yiban-backup.sh 与 wrapper 是同一作业（第 1 项：别名漏检）。"""
        self._write_archive()
        table = os.path.join(self.cron_d, "yiban-backup")
        with io.open(table, encoding="utf-8") as f:
            text = f.read()
        with io.open(table, "w", encoding="utf-8", newline="\n") as f:
            f.write(text.replace("yiban-backup-wrapper.sh", "yiban-backup.sh"))
        self._write_root(WRAPPER_CRON_LINE + "\n")
        report, unverified = self.mod._trigger_drift_report(
            os.path.join(self.app_dir, "deploy", "prod", "cron.d"),
            self.cron_d, os.path.join(self.tmp, "absent-etc-crontab"))
        self.assertEqual([], unverified)
        self.assertIsNotNone(report, "yiban-backup.sh 别名未被归到同一作业")
        findings = [f for f in report["findings"]
                    if f["job"] == "yiban-backup-wrapper.sh"]
        self.assertEqual(1, len(findings), report)
        self.assertEqual(2, findings[0]["count"])
        self.assertEqual("0 2 * * *", findings[0]["schedule"])

    def test_repo_table_alias_is_canonicalized(self):
        """仓库表直接写 yiban-backup.sh 时，也归到 wrapper 这个同一作业键。"""
        d = os.path.join(self.tmp, "repo-alias-cron.d")
        os.makedirs(d, exist_ok=True)
        _write_bytes(os.path.join(d, "yiban-backup"),
                     b"0 2 * * * root /usr/local/sbin/yiban-backup.sh\n")
        jobs = self.mod._repo_jobs(d)
        self.assertIsNotNone(jobs)
        self.assertIn("yiban-backup-wrapper.sh", jobs)
        self.assertNotIn("yiban-backup.sh", jobs)

    def test_cron_table_filename_charset(self):
        """cron 只读文件名 [A-Za-z0-9_-]+；其余字符一律忽略（第 2 项）。"""
        d = os.path.join(self.tmp, "charset-cron.d")
        os.makedirs(d, exist_ok=True)
        valid = ["yiban-backup", "yiban_backup-1", "yiban-backup_2", "yiban2"]
        invalid = ["yiban-backup~", "yiban backup", "yiban-backup.rc",
                   "yiban-backup.reconcile-20260101"]
        line = b"0 2 * * * root /usr/local/sbin/yiban-backup-wrapper.sh\n"
        for name in valid + invalid:
            _write_bytes(os.path.join(d, name), line)
        entries, unverified = self.mod._cron_d_entries(d)
        self.assertEqual([], unverified)
        self.assertEqual(set(valid),
                         {os.path.basename(path) for path, _s, _c in entries})
        self.assertEqual(4, len(entries), f"非法文件名被计入：{entries}")

    def test_repo_table_ignores_invalid_filename(self):
        """非白名单文件名 cron 不读，不能据此声明作业（第 2 项）。"""
        d = os.path.join(self.tmp, "repo-charset-cron.d")
        os.makedirs(d, exist_ok=True)
        _write_bytes(os.path.join(d, "yiban-sign"),
                     b"31 6 * * * yiban /opt/yiban-auto-sign/run.sh\n")
        _write_bytes(os.path.join(d, "yiban-backup~"),
                     b"0 4 * * * root /opt/other-job.sh\n")
        jobs = self.mod._repo_jobs(d)
        self.assertIsNotNone(jobs)
        self.assertIn("run.sh", jobs)
        self.assertNotIn("other-job.sh", jobs, "非法文件名被当成仓库表解析")

    def test_non_command_mentions_are_not_triggers(self):
        """echo/选项值/行尾注释里的作业名不是触发器（第 3 项）。"""
        empty = self._empty_cron_d()
        repo = os.path.join(self.app_dir, "deploy", "prod", "cron.d")
        for command in ("/bin/echo yiban-backup-wrapper.sh",
                        "--warn-if yiban-backup-wrapper.sh"):
            with self.subTest(command=command):
                self._write_root(f"0 3 * * * {command}\n")
                report, unverified = self.mod._trigger_drift_report(
                    repo, empty, os.path.join(self.tmp, "absent-etc-crontab"))
                self.assertEqual([], unverified)
                self.assertIsNone(report, f"非命令提及被算成触发器：{report}")
        # 行尾注释里的 run.sh 不得让 run.sh 多出一条未声明排期。
        # 用解释器形态（`/bin/bash wrapper.sh`）才能让注释截断成为承重件：
        # 直接以 wrapper 开头时，"只认命令位置"已经不吃后面的参数。
        self._write_root("0 2 * * * /bin/bash /usr/local/sbin/yiban-backup-wrapper.sh"
                         " # 同族见 run.sh\n")
        report, unverified = self.mod._trigger_drift_report(
            repo, empty, os.path.join(self.tmp, "absent-etc-crontab"))
        self.assertEqual([], unverified)
        self.assertIsNone(report, f"行尾注释被算成触发器：{report}")

    def test_undeclared_schedule_summary_does_not_claim_multiple_triggers(self):
        """仅未声明排期且 count=1 时，摘要不得说「多条」（第 6 项）。"""
        self._write_archive()
        self._write_root("0 3 * * * /usr/local/sbin/yiban-backup-wrapper.sh"
                         " >> /var/log/yiban/backup.log 2>&1\n")
        self.assertEqual(self._run(), 0)
        self.assertEqual(len(self.mails), 1, f"实际 {self.mails}")
        summary = self.mails[0][1].summary
        self.assertIn("未在仓库表声明", summary)
        self.assertNotIn("多条", summary,
                         f"未声明排期且 count=1，摘要不得说多条：{summary}")

    def test_shell_c_with_single_script_path_is_recognized(self):
        """`sh -c '<单个脚本路径>'` 会被识别（第 8 项：docstring 实测口径）。"""
        found = self.mod._job_keys_in_command(
            "sh -c '/usr/local/sbin/yiban-backup-wrapper.sh'",
            {"yiban-backup-wrapper.sh"})
        self.assertEqual({"yiban-backup-wrapper.sh"}, found)

    def test_empty_repo_table_dir_is_unverified_not_green(self):
        """扫空不得当绿：检出里解析不出任何作业时，门必须报"未核"。"""
        empty_repo = os.path.join(self.tmp, "empty-repo-cron.d")
        os.makedirs(empty_repo, exist_ok=True)
        report, unverified = self.mod._trigger_drift_report(
            empty_repo, self.cron_d, os.path.join(self.tmp, "absent-etc-crontab"))
        self.assertIsNone(report)
        self.assertTrue(unverified, "空仓库表目录被当成绿：门被架空却没人知道")
        self.assertIn("解析不出作业", unverified[0])


@unittest.skipIf(BASH is None, "需要 bash（Git Bash/WSL）")
class TriggerCountE2ETest(unittest.TestCase):
    """真跑 `scripts/backup_sentinel.py`：cron 文件名规则与"读不到就显式未核"。

    模拟的真实用户动作：主机同时装了 /etc/cron.d/yiban-backup 与 root crontab
    里同一条 wrapper 命令（2026-10-25 生产形态）。
    用户可见结果：08:05 哨兵日志点名该作业与条数；删掉重复后日志回到"都在"。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-bksentinel-trigger-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.backup_dir = os.path.join(self.tmp, "backups")
        self.cron_d = os.path.join(self.tmp, "etc-cron.d")
        self.fakebin = os.path.join(self.tmp, "bin")
        self.state_dir = os.path.join(self.tmp, "state")
        sbin = os.path.join(self.tmp, "sbin")
        for path in (self.backup_dir, self.cron_d, self.fakebin, self.state_dir, sbin):
            os.makedirs(path, exist_ok=True)
        # 当日归档 + 清单：让"缺备份"那一项不发声，判据只剩触发器条数
        day = clock.today()
        archive = os.path.join(self.backup_dir, f"yiban-{day}.tar.gz.gpg")
        _write_bytes(archive, b"ciphertext")
        _write_bytes(archive + ".sha256",
                     b"deadbeef  " + os.path.basename(archive).encode("utf-8") + b"\n")
        # 运行拷贝与检出一致：排除"脚本内容漂移"这个干扰项
        self.installed = os.path.join(sbin, "yiban-backup.sh")
        shutil.copyfile(os.path.join(BASE, "scripts", "backup.sh"), self.installed)
        _install_repo_tables(self.cron_d)
        self.root_crontab = os.path.join(self.tmp, "root-crontab")
        _write_bytes(self.root_crontab, b"")
        _write_bytes(os.path.join(self.tmp, "current-crontab"), b"")
        _fake_crontab_bin(self.fakebin)
        _write_bytes(os.path.join(self.tmp, ".env"), b"")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("YIBAN_")}
        self.env.update({
            "PATH": self.fakebin + os.pathsep + os.environ.get("PATH", ""),
            "HOME": self.tmp,
            "TMPDIR": self.tmp,
            "BACKUP_DIR": self.backup_dir,
            "APP_DIR": BASE,
            "YIBAN_APP_DIR": BASE,
            "YIBAN_BACKUP_INSTALLED": self.installed,
            "YIBAN_STATE_DIR": self.state_dir,
            "YIBAN_ENV_FILE": os.path.join(self.tmp, ".env"),
            "YIBAN_DB_FILE": os.path.join(self.tmp, "absent.db"),
            "YIBAN_CRON_D_DIR": self.cron_d,
            "YIBAN_SYSTEM_CRONTAB": os.path.join(self.tmp, "absent-etc-crontab"),
            "YIBAN_NOTIFY_COOLDOWN": "0",
            "FAKE_CRONTAB_ROOT": self.root_crontab,
            "FAKE_CRONTAB_CURRENT": os.path.join(self.tmp, "current-crontab"),
        })

    def _run(self):
        return subprocess.run([sys.executable, SCRIPT], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=self.env,
                              cwd=self.tmp, timeout=120)

    def _write_root(self, text):
        _write_bytes(self.root_crontab, text.encode("utf-8"))

    def test_duplicate_trigger_reported_with_job_count_and_sources(self):
        self._write_root(WRAPPER_CRON_LINE + "\n")
        r = self._run()
        self.assertIn("触发器条数漂移", r.stdout,
                      f"重复触发器没有被判漂移：stdout={r.stdout!r} stderr={r.stderr!r}")
        self.assertIn("yiban-backup-wrapper.sh", r.stdout, "必须点名作业")
        self.assertIn("0 2 * * *", r.stdout, "必须点名排期")
        self.assertIn("2 条", r.stdout, "必须给出条数")
        self.assertIn("root 的 crontab", r.stdout, "必须给出触发器来源")
        self.assertIn(os.path.join(self.cron_d, "yiban-backup"), r.stdout)

    def test_single_trigger_is_green_and_quiet(self):
        r = self._run()
        self.assertEqual(r.returncode, 0, f"每作业一条触发器时应安静通过：{r.stdout}{r.stderr}")
        self.assertIn("的归档与清单都在", r.stdout)
        self.assertNotIn("触发器条数漂移", r.stdout)

    def test_cron_d_file_name_with_dot_is_ignored_by_cron(self):
        """cron 自己的文件名规则：/etc/cron.d 下含点号的文件不被读取。

        生产上实测 4 份 `yiban-*.reconcile-*` 残留（2026-10-26 生产：cleanup
        两份 + probe 一份 + sign 一份），按 cron 规则本来就不执行。不实现这条
        规则，门会把它们误报成重复触发器。
        """
        # cron.d 形态（带身份列），与生产残留件的形态一致：若门漏了文件名规则，
        # 这一行会被算成第二条备份触发器。
        _write_bytes(os.path.join(self.cron_d, "yiban-backup.reconcile-20260101"),
                     (WRAPPER_CRON_D_LINE + "\n").encode("utf-8"))
        r = self._run()
        self.assertEqual(r.returncode, 0,
                         f"含点号的文件被当成活跃触发器误报：{r.stdout}{r.stderr}")
        self.assertNotIn("触发器条数漂移", r.stdout)
        self.assertIn("的归档与清单都在", r.stdout)

    def test_missing_crontab_command_is_explicitly_unverified(self):
        """无 crontab 命令：跳过不等于绿，必须显式写出"触发器条数未核"。"""
        empty_bin = os.path.join(self.tmp, "empty-bin")
        os.makedirs(empty_bin, exist_ok=True)
        self.env["PATH"] = empty_bin
        r = self._run()
        self.assertIn("触发器条数未核", r.stderr,
                      f"读不到 crontab 却静默当通过：stdout={r.stdout!r} stderr={r.stderr!r}")
        self.assertNotIn("触发器条数漂移", r.stdout)
        self.assertEqual(r.returncode, 0, f"读不到不是检查失败（但必须可见）：{r.stderr}")

    def test_permission_denied_on_root_crontab_is_explicitly_unverified(self):
        self.env["FAKE_CRONTAB_ROOT_ERROR"] = "must be privileged to use -u"
        r = self._run()
        self.assertIn("触发器条数未核", r.stderr,
                      f"权限不足却静默当通过：{r.stdout}{r.stderr}")
        self.assertIn("must be privileged", r.stderr, "未核原因必须带真实错误")
        self.assertNotIn("触发器条数漂移", r.stdout)

    def test_unverified_source_does_not_hide_a_duplicate(self):
        """未核与漂移并存时两者都要打印（P3-3）：隐藏重复比"未核"更危险。"""
        _write_bytes(os.path.join(self.cron_d, "yiban-backup-dup"),
                     (WRAPPER_CRON_D_LINE + "\n").encode("utf-8"))
        self.env["FAKE_CRONTAB_ROOT_ERROR"] = "must be privileged to use -u"
        r = self._run()
        self.assertIn("触发器条数未核", r.stderr, f"未核必须可见：{r.stderr}")
        self.assertIn("触发器条数漂移", r.stdout,
                      f"未核把已发现的重复吞掉了：stdout={r.stdout!r} stderr={r.stderr!r}")
        self.assertIn("2 条", r.stdout)


class WrapperWorkingDirTest(unittest.TestCase):
    """薄包装必须自己把 cwd 与 .env 路径摆正——cron 的 cwd 是 $HOME。

    这条只能按**行为**钉，不能只看源码里有没有 `cd`：cwd 是哨兵侧所有 cwd 相对回落的
    基准（`yiban/adapters/config/env_io.py` 的 env_path() 默认 "data/.env"、`yiban/adapters/persistence/connection.py`
    的 DB_DEFAULT "data/yiban.db"）。落在 $HOME 就读不到 .env ⇒ 收件人解析为空 ⇒ 告警发不
    出去（退出码 1，恰是哨兵要消灭的那种静默），并在 $HOME 就地新建一份野 yiban.db。

    假哨兵只回报"进程落在哪、拿到哪个 .env 路径"：本用例钉的是包装脚本的职责，判据
    本身由 `_Base` 各组用例钉。
    """

    @classmethod
    def setUpClass(cls):
        if shutil.which("bash") is None or shutil.which("python3") is None:
            raise unittest.SkipTest("需要 bash 与 python3（Git Bash/WSL）")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-bksentinel-wd-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        self.app_dir = os.path.join(self.tmp, "app")
        os.makedirs(os.path.join(self.app_dir, "scripts"))
        os.makedirs(self.home)
        with io.open(os.path.join(self.app_dir, "scripts", "backup_sentinel.py"), "w",
                     encoding="utf-8") as f:
            # 报告"落在哪个目录"用落一个标记文件（而非打印路径）：宿主可能是 Git Bash
            # （Windows 路径）也可能是 WSL，路径文本形式不可比，标记文件在哪一目了然。
            f.write("import os\n"
                    "open('sentinel-cwd', 'w').close()\n"
                    "print(os.environ.get('YIBAN_ENV_FILE', '<unset>'))\n")

    def _run(self, extra_env=None):
        """跑一次包装脚本：cwd 取 home（模拟 cron 的 $HOME），返回 CompletedProcess。"""
        env = {k: v for k, v in os.environ.items() if not k.startswith("YIBAN_")}
        env["APP_DIR"] = self.app_dir
        env.update(extra_env or {})
        return subprocess.run([shutil.which("bash"), WRAPPER], capture_output=True,
                              text=True, cwd=self.home, env=env, timeout=60)

    @staticmethod
    def _slashed(path):
        """统一分隔符后再比：Git Bash 下 shell 拼出的 <APP_DIR>/.env 是混合分隔符。"""
        return os.path.normcase(str(path).replace("\\", "/"))

    def test_wrapper_moves_to_app_dir_and_exports_env_file(self):
        """cwd 必须是 APP_DIR；YIBAN_ENV_FILE 默认导出为 <APP_DIR>/data/.env。"""
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.app_dir, "sentinel-cwd")),
                        "哨兵进程的 cwd 必须是 APP_DIR（cron 的 cwd 是 $HOME）："
                        f"标记文件不在 {self.app_dir}，stderr={r.stderr!r}")
        self.assertFalse(os.path.exists(os.path.join(self.home, "sentinel-cwd")),
                         "$HOME 下不得落下任何哨兵进程的痕迹")
        self.assertEqual(self._slashed(r.stdout.strip()),
                         self._slashed(os.path.join(self.app_dir, "data", ".env")),
                         "YIBAN_ENV_FILE 必须默认导出为 <APP_DIR>/data/.env")

    def test_explicit_env_file_is_kept(self):
        """部署方显式给出 YIBAN_ENV_FILE 时沿用其值，不覆盖成默认路径。"""
        custom = os.path.join(self.tmp, "custom.env")
        r = self._run({"YIBAN_ENV_FILE": custom})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._slashed(r.stdout.strip()), self._slashed(custom))


class WrapperSourceTest(unittest.TestCase):
    """静态核验薄包装：安装命令、08:05 排期与"防静默失败"的用途都写在头部。"""

    @classmethod
    def setUpClass(cls):
        with io.open(WRAPPER, encoding="utf-8") as f:
            cls.src = f.read()


    def test_wrapper_only_forwards(self):
        """薄包装：判据实现唯一在 backup_sentinel.py，包装里不出现任何检查逻辑。"""
        self.assertIn('exec "$PY" "$APP_DIR/scripts/backup_sentinel.py"', self.src)
        self.assertNotIn("sha256", self.src)
        self.assertNotIn("tar.gz", self.src)

    def test_header_lists_the_trigger_gate_env_vars(self):
        """环境变量清单必须与 backup_sentinel.py 的触发器门输入对齐（P3-6）。"""
        for name in ("YIBAN_CRON_D_DIR", "YIBAN_SYSTEM_CRONTAB"):
            self.assertIn(name, self.src,
                          f"包装头部环境变量清单缺 {name}（与哨兵输入面不对齐）")


class SentinelDocContractTest(unittest.TestCase):
    """哨兵 docstring 的承重口径：扫面边界、未核 rc、sh -c 与别名边界。"""

    @classmethod
    def setUpClass(cls):
        with io.open(SCRIPT, encoding="utf-8") as f:
            cls.src = f.read()
        cls.mod = _load_script()

    def test_docstring_states_the_trigger_scan_scope(self):
        self.assertIn("只扫 root 与当前身份", self.src,
                      "登记边界必须写进 docstring：其它身份的 crontab 不在扫描面")

    def test_docstring_states_that_unverified_still_exits_zero(self):
        self.assertIn("未核也算检查完成", self.src,
                      "rc 口径必须写明：未核不改退出码")

    def test_job_matcher_docstring_describes_shell_c_truthfully(self):
        """第 8 项：实测 `sh -c '<单个脚本路径>'` 会识别，旧 docstring 写反了。"""
        doc = self.mod._job_keys_in_command.__doc__ or ""
        self.assertIn("单个脚本路径", doc,
                      "docstring 必须写明 sh -c 的单脚本路径会被识别")
        self.assertNotIn("本门不认", doc,
                         "旧 docstring 说 sh -c 不认，与实测不符")
        self.assertIn("不做 inode 归并", doc,
                      "软链/改名边界必须登记")

    def test_cron_filename_rule_cites_man_8_cron(self):
        """第 P4-1 项：规则出处是 man 8 cron，不是 man 5 crontab。"""
        self.assertIn("man 8 cron", self.src)
        self.assertNotIn("man 5 crontab", self.src)

    def test_cron_d_docstring_registers_lsb_name_assumption(self):
        """第 P4-2 项：`-l`（LSB 名字规则）假设与漏报后果必须写进 docstring。"""
        doc = self.mod._cron_d_entries.__doc__ or ""
        self.assertIn("-l", doc)
        self.assertIn("LSB", doc)
        # P2-2：承重的是「开了 -l 会漏报」这半句，不能只断言关键词
        self.assertIn("会变成活跃触发器，本门不会报", doc,
                      "漏报声明被删掉了：本门不会报")

    def test_cron_d_docstring_reports_the_measured_reconcile_count(self):
        """第 P2-3 项：残留份数与口径写对（2026-10-26 生产实测 4 份）。"""
        self.assertIn("实测 4 份", self.src)
        self.assertIn("2026-10-26", self.src)
        self.assertNotIn("生产上三份", self.src)


class BackupDocsContractTest(unittest.TestCase):
    """部署文档的三条承重口径（P2-2/P3-7）：crontab 步骤有条件、正常路径有输出。"""

    @classmethod
    def setUpClass(cls):
        with io.open(os.path.join(BASE, "README.md"), encoding="utf-8") as f:
            cls.readme = f.read()
        with io.open(os.path.join(BASE, "docs", "dev", "backup-restore.md"),
                     encoding="utf-8") as f:
            cls.backup_doc = f.read()

    def test_crontab_sections_warn_to_skip_after_installer(self):
        """两处手工 crontab 步骤都必须标注「跑过 install.sh 就跳过」（P2-2）。"""
        for marker in ("# 5. 定时任务", "#### 6. crontab"):
            idx = self.readme.find(marker)
            self.assertNotEqual(-1, idx, f"README 缺段落标记：{marker}")
            window = self.readme[idx:idx + 700]
            self.assertIn("跳过本节", window,
                          f"{marker} 没写「跑过 install.sh 就跳过」——两处都写会重复触发")

    def test_backup_doc_does_not_claim_a_silent_normal_path(self):
        """哨兵正常路径每天打印一行；文档不得再写「零输出」（P3-7）。"""
        self.assertNotIn("正常路径零输出", self.backup_doc,
                         "正常路径每天打印一行「归档与清单都在」，零输出为假")
        self.assertNotIn("不为 1 条", self.backup_doc,
                         "实现对 0 条不报；应写「多于 1 条或未声明排期」")
        self.assertIn("归档与清单都在", self.backup_doc)

    def test_quickstart_says_delete_root_crontab_after_installer(self):
        """第 7 项：快速开始先写 root crontab，必须写明跑过安装器后删掉。"""
        idx = self.readme.find("# 5. 定时任务")
        self.assertNotEqual(-1, idx, "README 缺快速开始的定时任务段")
        window = self.readme[idx:idx + 900]
        self.assertIn("跑过 install.sh 后删掉上面两行", window,
                      "快速开始没写「跑过安装器后删掉那两行」")


if __name__ == "__main__":
    unittest.main(verbosity=2)
