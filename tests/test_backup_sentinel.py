# -*- coding: utf-8 -*-
"""`scripts/backup_sentinel.py` 与 `scripts/yiban-backup-sentinel.sh` 的契约用例。

标签：J · 运维：部署/备份/发布
覆盖：当日归档与 .sha256 清单齐不齐（只有密文形态 gpg/age 计入健康——M3 批次0
    明文模式护栏：明文包不再算数，且单独触发告警）、昨日包不算今日备份、
    运行拷贝与仓库版的漂移比对、跨进程节流真的接上、发不出去要能看见、
    wrapper 切工作目录与导出 .env、wrapper 头部文档与"只转发"契约。
对应实现：`scripts/backup_sentinel.py`（判定与外发）、`scripts/yiban-backup-sentinel.sh`
    （cron 入口）；节流复用 `yiban.notify.ledger`。
关键断言：① 缺包/缺清单/漂移 → 恰好一封管理员告警且正文带排查路径；② 正常路径
    （都在且一致）**零输出零外发**——哨兵自己不许变成噪音源；③ 未安装
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
                f.write("deadbeef  " + os.path.basename(path) + "\n")  #清单内容不参与判定，齐不齐才是判据——这里连哈希都不用真
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
            with mock.patch.object(self.mod, "_alert_due", side_effect=due):  #只打桩"该不该发"，发送出口仍是记录器：节流路径不许被桩绕过
                return self.mod.main([])


class SentryVerdictTest(_Base):
    """缺什么喊什么；什么都不缺就安静。"""

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


class WrapperWorkingDirTest(unittest.TestCase):
    """薄包装必须自己把 cwd 与 .env 路径摆正——cron 的 cwd 是 $HOME。

    这条只能按**行为**钉，不能只看源码里有没有 `cd`：cwd 是哨兵侧所有 cwd 相对回落的
    基准（`yiban/infra/env_io.py` 的 env_path() 默认 ".env"、`yiban/store/connection.py`
    的 DB_DEFAULT "yiban.db"）。落在 $HOME 就读不到 .env ⇒ 收件人解析为空 ⇒ 告警发不
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
        """cwd 必须是 APP_DIR；YIBAN_ENV_FILE 默认导出为 <APP_DIR>/.env。"""
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.app_dir, "sentinel-cwd")),
                        "哨兵进程的 cwd 必须是 APP_DIR（cron 的 cwd 是 $HOME）："
                        f"标记文件不在 {self.app_dir}，stderr={r.stderr!r}")
        self.assertFalse(os.path.exists(os.path.join(self.home, "sentinel-cwd")),
                         "$HOME 下不得落下任何哨兵进程的痕迹")
        self.assertEqual(self._slashed(r.stdout.strip()),
                         self._slashed(os.path.join(self.app_dir, ".env")),
                         "YIBAN_ENV_FILE 必须默认导出为 <APP_DIR>/.env")

    def test_explicit_env_file_is_kept(self):
        """部署方显式给出 YIBAN_ENV_FILE 时沿用其值，不覆盖成 <APP_DIR>/.env。"""
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
