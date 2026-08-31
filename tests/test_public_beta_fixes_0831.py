"""2026-08-31 公测反馈修复的回归测试（批次14 追加）。

三处行为改动，各自钉住"改前会红、改后绿"：

R1（scripts/signin.py，公测问题 2/6/8）：缓存会话被服务端作废（"未登录或登录已经超时"）
  时，原实现既不清缓存也不收敛次数——该消息不在风控关键词里，于是拿同一份死缓存
  空跑满 4 次。生产 2026-08-31 06:31 那轮：3 个账号各空跑 4 次，把首轮拖到 07:39，
  07:10 的兜底补签被 run.sh 判"已有进程在运行"跳过，当天再没有第二次机会。
  现由 _retry_budget 统一给预算：会话陈旧类 = 2 次 + 清缓存（下次尝试真重登）。
  刻意不改 RISK_FAIL_KEYWORDS：会话陈旧不是凭据问题，误并入会让账密熔断累计失败天数。

R2（web/app.py，公测问题 13）：登录连续失败 3 次一律 urgent=True，而紧急账默认只有
  3 条/天——公测首日 07:59 就有一次学生忘密码触发锁定，常见误操作会挤掉"告警通道
  被人拆了""审计链断裂"这类真紧急信号。现按"同一 IP 失败过的不同用户名数"判喷洒：
  达到 LOGIN_SPRAY_USERS 才升级紧急，单账号反复输错走非紧急账（邮件不受影响，照旧全量）。

R3（web/app.py，公测问题 5）：用户提交账号申请入库后，管理员侧零通知，只能主动打开
  后台发现，于是出现"用户说交了申请、管理员说没收到"（生产 2026-08-31 07:55:55 另有一次
  提交被 CSRF 拒绝：session_token_len=0，即会话已丢，用户端以为成功）。现补一条非紧急
  "新账号申请待审核"告警，且整段兜异常——账号已入库，通知失败不得把提交带崩成 500。

用法（项目根目录，勿设 PYTHONIOENCODING）：
    py -m pytest tests/test_public_beta_fixes_0831.py -v
"""
import os
import sys
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))
sys.path.insert(0, os.path.join(BASE, "tests"))

import signin  # noqa: E402
from test_batch14_fixes_0829 import _B14AlertGateBase  # noqa: E402

# 生产 2026-08-31 的原始失败消息（signin.py 拼出的完整串，非构造）
PROD_STALE_MSG = "获取签到任务失败: 未登录或登录已经超时"


class SessionStaleBudgetTest(unittest.TestCase):
    """R1：会话陈旧类失败必须收敛到 2 次并清除会话缓存。"""

    def test_prod_stale_message_gets_two_attempts_and_cache_clear(self):
        self.assertEqual(signin._retry_budget(PROD_STALE_MSG), (2, True))

    def test_stale_is_not_classified_as_risk(self):
        # 钉住"收敛不来自风控表"：若有人图省事把关键词塞进 RISK_FAIL_KEYWORDS，
        # 该消息会被 _is_credential_failure 的邻居逻辑误累计成账密熔断天数。
        self.assertEqual(signin.classify_failure(PROD_STALE_MSG), signin.MAX_ATTEMPTS)
        self.assertFalse(signin._is_credential_failure(PROD_STALE_MSG),
                         "会话陈旧不是凭据问题，不得参与账密熔断累计")

    def test_risk_and_device_binding_still_clear_cache(self):
        self.assertEqual(signin._retry_budget("登录失败: 账号或密码错误"), (2, True))
        self.assertEqual(signin._retry_budget("请求被 WAF 风控拦截"), (2, True))
        self.assertEqual(signin._retry_budget("签到失败: 请使用授权设备进行签到"),
                         (signin.MAX_ATTEMPTS, True))

    def test_network_failure_keeps_full_budget_without_clearing(self):
        # 真瞬时故障保持 3 次上限且不动缓存（缓存本身没问题，清了反而多打一次登录）
        self.assertEqual(signin._retry_budget("HTTPSConnectionPool 读超时"),
                         (signin.MAX_ATTEMPTS, False))

    def test_network_budget_is_three_not_four(self):
        # 2026-08-31：窗口为全体账号共享、每次重试间隔≥60s，重试越多越拖长队列
        # （实证：把首轮拖过 07:10 兜底）。网络类上限由 4 收敛为 3。
        self.assertEqual(signin.MAX_ATTEMPTS, 3)

    def test_no_position_fails_fast_without_clearing_cache(self):
        # 易班侧无点位是数据问题：1 次即止；会话本身没问题，不得清缓存
        msg = "未找到签到位置数据（易班未返回该账号的签到点位，非账号密码问题）"
        self.assertEqual(signin._retry_budget(msg),
                         (signin.NO_POSITION_MAX_ATTEMPTS, False))
        self.assertEqual(signin.NO_POSITION_MAX_ATTEMPTS, 1)

    def test_success_message_is_not_stale(self):
        self.assertFalse(signin._is_session_stale_failure("今日已签到（无需重复签到）"))


class LoginAlertUrgencyTest(_B14AlertGateBase):
    """R2：只有喷洒特征才升级紧急。"""

    def _alerts(self):
        return [a for a in self.alerts if a[0] == "登录失败告警"]

    def test_below_threshold_sends_nothing(self):
        c = self._client()
        for _ in range(self.webapp.LOGIN_FAIL_NOTIFY - 1):
            c.post("/api/login", json={"username": "admin", "password": "WrongPass#111"})
        self.assertEqual(self._alerts(), [])

    def test_same_user_repeated_mistake_is_not_urgent(self):
        """本人忘密码：连续 3 次输错 → 仍告警（可追溯），但走非紧急账不占手机额度。"""
        c = self._client()
        for _ in range(self.webapp.LOGIN_FAIL_NOTIFY):
            c.post("/api/login", json={"username": "admin", "password": "WrongPass#111"})
        got = self._alerts()
        self.assertEqual(len(got), 1, f"每轮应只告警一次：{got}")
        self.assertFalse(got[0][2], "单账号反复输错不得占用紧急额度")

    def test_spray_across_users_is_urgent(self):
        """同一 IP 打多个用户名且某账号已到阈值 → 撞库特征，升级紧急。"""
        c = self._client()
        users = ["a1@beta.local", "a2@beta.local", "a3@beta.local"]
        for u in users:
            for _ in range(self.webapp.LOGIN_FAIL_NOTIFY - 1):
                c.post("/api/login", json={"username": u, "password": "WrongPass#111"})
        # 第 3 个账号的第 3 次失败触发告警：此时该 IP 已试过 3 个不同用户名
        c.post("/api/login", json={"username": users[-1], "password": "WrongPass#111"})
        got = self._alerts()
        self.assertEqual(len(got), 1, f"仅命中阈值那一次告警：{got}")
        self.assertTrue(got[0][2], "跨账号喷洒必须升级紧急")
        self.assertIn(f"{self.webapp.LOGIN_SPRAY_USERS} 个不同用户名", got[0][1],
                      "正文须交代升级依据，否则管理员无从判断是不是误报")


class LoginAlertRealChannelTest(_B14AlertGateBase):
    """不替换 send_notification：钉住"降级"改的是账本归属，不是把通知整条跳过。"""

    PATCH_NOTIFY = False

    def test_login_failure_still_reaches_the_notification_layer(self):
        """降级只降"推不推手机"的账本归属，不得在应用层就把通知整条跳过。"""
        c = self._client()
        with mock.patch.object(self.webapp.notify, "send") as send_mock:
            for _ in range(self.webapp.LOGIN_FAIL_NOTIFY):
                c.post("/api/login", json={"username": "admin", "password": "WrongPass#111"})
            self.assertEqual(send_mock.call_count, 1,
                             "非紧急仍须调用 notify.send（是否推手机由通道侧决定）")
            self.assertIs(send_mock.call_args.kwargs.get("urgent"), False,
                          "传入的 urgent 必须与判据一致")


class NewApplicationAlertTest(_B14AlertGateBase):
    """R3：申请入库后管理员必须被通知到。"""

    EMAIL = "beta.tester@qq.com"
    PASSWORD = "BetaUser#2026x"

    def _submit_account(self):
        c = self._client()
        r = c.post("/api/register",
                   json={"email": self.EMAIL, "password": self.PASSWORD, "agree": True})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        token = self._login(c, self.EMAIL, self.PASSWORD)
        return c.post(
            "/api/my-accounts",
            json={"name": "小李的手机", "phone": "13800001234", "password": "Yiban#pw123",
                  "phone_model": "", "phone_code": ""},
            headers=self._csrf(token),
        )

    def test_admin_gets_non_urgent_notice_on_new_application(self):
        r = self._submit_account()
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        got = [a for a in self.alerts if a[0] == "新账号申请待审核"]
        self.assertEqual(len(got), 1, f"新申请须且只须一条告警：{self.alerts}")
        self.assertFalse(got[0][2], "新申请属日常事务，不得占用紧急额度")
        self.assertIn("1234", got[0][1], "正文须含脱敏手机号尾号供管理员定位")
        self.assertNotIn("13800001234", got[0][1], "告警正文不得外泄完整手机号")

    def test_notice_failure_does_not_break_the_submission(self):
        """通知通道炸掉时，已入库的申请仍须返回成功（不得退化成 500 让用户重交）。"""
        c = self._client()
        r = c.post("/api/register",
                   json={"email": self.EMAIL, "password": self.PASSWORD, "agree": True})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        token = self._login(c, self.EMAIL, self.PASSWORD)
        with mock.patch.object(self.webapp, "send_notification",
                               side_effect=RuntimeError("通道炸了")):
            r2 = c.post(
                "/api/my-accounts",
                json={"name": "", "phone": "13800005678", "password": "Yiban#pw123",
                      "phone_model": "", "phone_code": ""},
                headers=self._csrf(token),
            )
        self.assertEqual(r2.status_code, 200, r2.get_data(as_text=True))
        accs = [a for a in self.webapp.load_accounts() if a.get("phone") == "13800005678"]
        self.assertEqual(len(accs), 1, "申请须已入库且状态待审核")
        self.assertEqual(accs[0].get("status"), self.webapp.ACCOUNT_STATUS_PENDING)


if __name__ == "__main__":
    unittest.main()
