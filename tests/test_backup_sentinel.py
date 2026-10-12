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
    （cron 入口）；节流复用 `yiban.notify.ledger`。
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
        `AuditAnchorBroadcastTest`，那里直接打桩 `yiban.store.db` 的三个读函数。
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

        with (mock.patch.dict("sys.modules", {"yiban.store.db": _FakeDb}),
              mock.patch("yiban.store.db", _FakeDb)):
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

        with (mock.patch.dict("sys.modules", {"yiban.store.db": _FakeDb}),
              mock.patch("yiban.store.db", _FakeDb)):
            self.assertIsNone(self.mod._anchor_snapshot())


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
