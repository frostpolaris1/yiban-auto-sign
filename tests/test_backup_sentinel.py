# -*- coding: utf-8 -*-
"""`scripts/backup_sentinel.py` 与 `scripts/yiban-backup-sentinel.sh` 的契约用例。

钉三件事：
1. **缺包必须发声**：当日归档/清单缺失、或运行拷贝与仓库版漂移 → 一封管理员告警；
   正常路径（都在、且一致）零输出零外发——哨兵自己不许变成噪音源。
2. **节流真的接上了**：第二次运行在窗口内不再外发（跨进程磁盘表，cron 每次新进程）。
3. **发不出去要能看见**：收件人为空/组件失败 → 返回 1（cron 自己报错是最后一道声音）。

全程临时目录 + 打桩发送出口，不碰本机真实备份目录、不连 SMTP。
"""
import importlib.util
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

from yiban.notify import ledger as notify_ledger

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(BASE, "scripts", "backup_sentinel.py")
WRAPPER = os.path.join(BASE, "scripts", "yiban-backup-sentinel.sh")


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
        self.addCleanup(notify_ledger._throttle_ts.clear)
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
        })
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)
        self.mails = []

    # ---- 脚手架 ----
    def _write_archive(self, suffix=".tar.gz.gpg", sidecar=True, day=None):
        day = day or self.mod._now()
        path = os.path.join(self.backup_dir, f"yiban-{day}{suffix}")
        with io.open(path, "wb") as f:
            f.write(b"ciphertext")
        if sidecar:
            with io.open(path + ".sha256", "w", encoding="utf-8") as f:
                f.write("deadbeef  " + os.path.basename(path) + "\n")
        return path

    def _install_copy(self, content="#!/bin/bash\necho 仓库版\n"):
        with io.open(self.installed, "w", encoding="utf-8") as f:
            f.write(content)
        return self.installed

    def _run(self, *, due=None, send=None):
        """跑一次 main()：发送出口默认打桩为记录调用（不碰 SMTP）。"""
        sender = send or (lambda title, mail: self.mails.append((title, mail)) or True)
        with mock.patch.object(self.mod, "_send_admin_alert", side_effect=sender):
            if due is None:
                return self.mod.main([])
            with mock.patch.object(self.mod, "_alert_due", side_effect=due):
                return self.mod.main([])


class SentryVerdictTest(_Base):
    """缺什么喊什么；什么都不缺就安静。"""

    def test_archive_and_sidecar_present_is_silent(self):
        self._write_archive()
        self.assertEqual(self._run(), 0)
        self.assertEqual(self.mails, [], "正常路径不得外发任何告警")

    def test_plaintext_and_age_forms_also_accepted(self):
        """明文/age 形态也认：认不出会在合法部署上误报"没有备份"，那正是要消灭的噪音。"""
        for suffix in (".tar.gz", ".tar.gz.age"):
            with self.subTest(suffix=suffix):
                shutil.rmtree(self.backup_dir)
                os.makedirs(self.backup_dir)
                self.mails.clear()
                self._write_archive(suffix=suffix)
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
        self.assertIn("仓库版", body)
        self.assertIn("运行版", body)
        self.assertIn("/usr/local/sbin/yiban-backup.sh", body, "要给出以仓库版为准的重装指引")

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
    """发不出去必须能被看见：返回 1，让 cron 自己的报错邮件成为最后一道声音。"""

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


class WrapperSourceTest(unittest.TestCase):
    """静态核验薄包装：安装命令、08:05 排期与"防静默失败"的用途都写在头部。"""

    @classmethod
    def setUpClass(cls):
        with io.open(WRAPPER, encoding="utf-8") as f:
            cls.src = f.read()

    def test_header_documents_install_and_crontab(self):
        self.assertIn("/usr/local/sbin/yiban-backup-sentinel.sh", self.src, "安装命令不得缺")
        self.assertIn("5 8 * * *", self.src, "排期须是 08:05（02:00 备份之后）")
        self.assertIn("静默失败", self.src, "头部须写清它防的是什么")

    def test_wrapper_only_forwards(self):
        """薄包装：判据实现唯一在 backup_sentinel.py，包装里不出现任何检查逻辑。"""
        self.assertIn('exec "$PY" "$APP_DIR/scripts/backup_sentinel.py"', self.src)
        self.assertNotIn("sha256", self.src)
        self.assertNotIn("tar.gz", self.src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
