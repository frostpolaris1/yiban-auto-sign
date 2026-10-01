# -*- coding: utf-8 -*-
"""M35：注册/添加账号的在线校验必须走 `YIBAN_PROXY` 管控的出口。

标签：A · 引擎：探针/账号校验
覆盖：`probe._egress_env` 的两源合并口径、`probe._apply_egress_proxy` 的套用与
   "未配则不动"、`verify_account` 在登录前确实套上了出口。

对应实现：yiban/engine/probe.py（`_egress_env`、`_apply_egress_proxy`、
   `verify_account`）、yiban/egress.py（`resolve(ROLE_SINGLE)`）。

关键断言：这条路径是**服务器代用户向易班发起的真实登录**，风控暴露面与签到同一级，
   却是唯一跑在 web 进程里的一条——出口是给执行体子进程注入的，web 进程的环境里通常
   没有这个键，于是它直连出网、绕过了 `YIBAN_PROXY` 管控。判据是"session 上确实挂着
   解析出来的出口"，不是"配置读到了"。

依赖：临时 `.env` + 环境变量打桩 + 假客户端；不触网、不建库。整文件在本机执行，无 skip。
"""
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from yiban.engine import probe

PROXY = "http://user:pass@127.0.0.1:8888"
PROXY_PLAIN = "http://127.0.0.1:8888"


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-probe-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env_file = os.path.join(self.tmp, ".env")
        self._env_backup = {
            k: os.environ.get(k) for k in ("YIBAN_PROXY", "YIBAN_ENV_FILE")
        }

        def _restore():
            for k, v in self._env_backup.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        self.addCleanup(_restore)

    def _write_env(self, text):
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write(text)

    def _use_env(self):
        os.environ["YIBAN_ENV_FILE"] = self.env_file

    def _client(self):
        c = SimpleNamespace(account=SimpleNamespace(phone="13800000001"),
                            session=SimpleNamespace(proxies={}),
                            use_killyiban=True)
        return c


class EgressEnvTest(_Base):
    """`_egress_env`：进程环境打底，`.env` 只补缺。"""

    def test_env_file_fills_a_key_missing_from_process_env(self):
        """web 进程的典型处境：进程环境没有 `YIBAN_PROXY`，只有 `.env` 里有。

        只读 `os.environ` 就等于"这条出网不受管控"——这正是本项要堵的缺口。
        """
        self._write_env(f"YIBAN_PROXY={PROXY}\n")
        self._use_env()
        os.environ.pop("YIBAN_PROXY", None)
        self.assertEqual(probe._egress_env()["YIBAN_PROXY"], PROXY)

    def test_process_env_wins_so_per_slot_injected_egress_is_not_flattened(self):
        """执行体子进程被注入的出口必须压过 `.env`（否则每槽位出口被同一个值抹平）。

        `yiban/engine/workers.py` 按槽位把出口写进子进程环境；`YIBAN_PROXY_LIST` /
        清单形态下 `.env` 里那个 `YIBAN_PROXY` 只对 `single` 档有意义。
        """
        self._write_env(f"YIBAN_PROXY={PROXY}\n")
        self._use_env()
        os.environ["YIBAN_PROXY"] = "http://10.0.0.9:1080"
        self.assertEqual(probe._egress_env()["YIBAN_PROXY"], "http://10.0.0.9:1080")

    def test_missing_everywhere_yields_no_key(self):
        self._write_env("YIBAN_OTHER=1\n")
        self._use_env()
        os.environ.pop("YIBAN_PROXY", None)
        self.assertEqual(probe._egress_env().get("YIBAN_PROXY", ""), "")


class ApplyEgressProxyTest(_Base):
    """`_apply_egress_proxy`：套用 / 未配不动 / 日志不落凭据。"""

    def test_applies_resolved_proxy_to_session(self):
        self._write_env(f"YIBAN_PROXY={PROXY}\n")
        self._use_env()
        os.environ.pop("YIBAN_PROXY", None)
        c = self._client()
        self.assertEqual(probe._apply_egress_proxy(c), PROXY)
        self.assertEqual(c.session.proxies,
                         {"http": PROXY, "https": PROXY},
                         "出口必须同时覆盖 http/https（易班侧是 https）")

    def test_no_proxy_configured_leaves_session_untouched(self):
        """未配代理时**不动** session —— 不把"没配"变成"显式清空"。"""
        self._write_env("YIBAN_OTHER=1\n")
        self._use_env()
        os.environ.pop("YIBAN_PROXY", None)
        c = self._client()
        c.session.proxies = {"http": "http://kept", "https": "http://kept"}
        self.assertEqual(probe._apply_egress_proxy(c), "")
        self.assertEqual(c.session.proxies,
                         {"http": "http://kept", "https": "http://kept"},
                         "未配出口时不得清掉客户端原有的代理设置")

    def test_resolver_failure_does_not_break_the_call(self):
        """出口解析抛异常只降级为直连，绝不把注册/改密主流程带崩。"""
        self._use_env()
        os.environ.pop("YIBAN_PROXY", None)
        c = self._client()
        with mock.patch.object(probe.egress, "resolve",
                               side_effect=RuntimeError("boom")):
            self.assertEqual(probe._apply_egress_proxy(c), "")
        self.assertEqual(c.session.proxies, {})

    def test_log_line_never_contains_proxy_credentials(self):
        """日志走 `egress.describe()`：`user:pass@` 不得落进日志文件。"""
        self._write_env(f"YIBAN_PROXY={PROXY}\n")
        self._use_env()
        os.environ.pop("YIBAN_PROXY", None)
        c = self._client()
        with self.assertLogs("yiban", level="DEBUG") as cm:
            probe._apply_egress_proxy(c)
        text = "\n".join(cm.output)
        self.assertIn("127.0.0.1:8888", text, "日志应能看到出口主机（运维需要）")
        self.assertNotIn("user:pass", text, "代理凭据不得落日志")
        self.assertNotIn("user", text.split("127.0.0.1:8888")[0].split(": ")[-1],
                         "日志不得带出 userinfo")


class VerifyAccountUsesProxyTest(_Base):
    """`verify_account`：登录前确实套上了出口（端到端接线）。"""

    def test_proxy_is_applied_before_login(self):
        self._write_env(f"YIBAN_PROXY={PROXY}\n")
        self._use_env()
        os.environ.pop("YIBAN_PROXY", None)
        seen = {}

        def _login_killyiban():
            seen["proxies"] = dict(client.session.proxies)
            return True

        client = self._client()
        client.login_killyiban = _login_killyiban
        client.login = _login_killyiban
        client.verify = lambda: (True, "ok")
        client._wipe_credentials = lambda: None
        acc = SimpleNamespace(phone="13800000001", password="p", phone_model="",
                              phone_code="", account_id=0)
        with mock.patch.object(probe, "YibanClient", lambda a: client), \
             mock.patch.object(probe, "account_still_signable", lambda a: True):
            ok, _msg = probe.verify_account(acc)
        self.assertTrue(ok)
        self.assertEqual(seen.get("proxies"),
                         {"http": PROXY, "https": PROXY},
                         "登录那一刻 session 上必须已挂着配置的出口")

    def test_deleted_account_still_short_circuits_before_any_egress(self):
        """账号已删/停用时不得发起登录（既有短路不得因新增出口解析被挪到发请求之后）。"""
        self._write_env(f"YIBAN_PROXY={PROXY}\n")
        self._use_env()
        called = []
        with mock.patch.object(probe, "YibanClient",
                               lambda a: called.append(1)), \
             mock.patch.object(probe, "account_still_signable", lambda a: False):
            ok, msg = probe.verify_account(
                SimpleNamespace(phone="13800000001", password="p", phone_model="",
                                phone_code="", account_id=0))
        self.assertFalse(ok)
        self.assertEqual(msg, "账号已被删除或停用")
        self.assertEqual(called, [], "短路时不得构造客户端（更不得出网）")


if __name__ == "__main__":
    unittest.main()
