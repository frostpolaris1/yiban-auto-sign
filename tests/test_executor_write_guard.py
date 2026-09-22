# -*- coding: utf-8 -*-
"""执行体写操作的**口令门**与行的**自定义名**（2026-09-17）。

背景（前端 88 号提示词）：系统开关那类高危动作有口令复核，执行体的写操作此前没有——
持一个被窃的主管理员会话（Cookie + CSRF 都能拿到）就能改出口、增删执行体、把兜底停掉，
而这恰恰是最容易造成**静默漏签**的一类配置。

口令门口径（**与 `POST /api/settings` 的系统开关逐字同构**，不另立一套）：
- 只在"**真的会改配置**"时要求 `confirm_password`：同值提交、只改自定义名、只读不要求；
- 校验走 `_verify_session_password`（只比对**不写**与登录共用的失败计数——P18 教训：
  持 Cookie 者若能写共享计数，就能反手把管理员锁出登录）；
- 缺口令 → **403**「需要输入当前口令，设置未生效」+ `reason=password_required`；错口令 →
  **403**「口令校验未通过，设置未生效」+ `reason=password_incorrect`（状态码相同、文案分开）
  + 审计 `executors_pw_fail`，**配置与清单都不动**；审计只落动作与槽位，绝不记口令、
  代理串、自定义名；
- 实测端点（`…/measure`）**不要求**口令：它不改配置，与手动签到同口径。

自定义名口径：随清单一起进 `.env`（`slot/type/proxy/name`）。未设 = `null`（页面显示后端
`label`）；空串 = 清掉。`label`（后端角色口径）与 `name`（用户输入）刻意分开。

全程 mock / 纯本地（Flask test client），无任何网络请求。
用法（项目根目录）：
    py -m pytest tests/test_executor_write_guard.py -v
"""
import contextlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_KEY = "a" * 64
ADMIN_PASS = "TestPass1234!"


def _load_webapp():
    """**独立名字**加载 web/app.py：它在导入期把 ENV_FILE 等读成模块级常量，
    与别的测试文件共用同一模块对象会读到另一个 `.env`（单跑绿、全量红的老坑）。"""
    spec = importlib.util.spec_from_file_location(
        "webapp_writeguard", os.path.join(BASE, "web", "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["webapp_writeguard"] = mod
    with contextlib.suppress(Exception):
        spec.loader.exec_module(mod)
    return mod


class _GuardBase(unittest.TestCase):
    """临时 .env/DB + 主管理员登录；每个用例自带前置配置（防"单跑绿、全量红"）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yiban-write-guard-")
        cls.env_file = os.path.join(cls.tmp, ".env")
        os.environ.update({
            "YIBAN_ENV_FILE": cls.env_file,
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_DB_FILE": os.path.join(cls.tmp, "yiban.db"),
            "YIBAN_STATE_DIR": cls.tmp,
            "YIBAN_LOG_FILE": os.path.join(cls.tmp, "sign.log"),
            "YIBAN_DISABLE_PURGE_LOOP": "1",
        })
        cls.webapp = _load_webapp()

    @classmethod
    def tearDownClass(cls):
        for k in ("YIBAN_ENV_FILE", "YIBAN_ACCOUNTS_KEY", "YIBAN_DB_FILE",
                  "YIBAN_STATE_DIR", "YIBAN_LOG_FILE", "YIBAN_DISABLE_PURGE_LOOP"):
            os.environ.pop(k, None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        import db
        if db._conn is not None:
            with contextlib.suppress(Exception):
                db._conn.close()
            db._conn = None
        for suffix in ("", "-wal", "-shm"):
            path = os.environ["YIBAN_DB_FILE"] + suffix
            if os.path.exists(path):
                os.remove(path)
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write(f"YIBAN_ACCOUNTS_KEY={TEST_KEY}\n"
                    "YIBAN_ADMIN_USER=admin@test.local\n"
                    f"YIBAN_ADMIN_PASSWORD={ADMIN_PASS}\n"
                    # 本文件钉的是"单次真变更当次就要口令"，而门禁的 TTL 豁免
                    # （刚复核过 + 同出口 IP → 免再输）恰好放松这条；豁免本身的口径
                    # 由 tests/test_web_auth_security.py 负责，这里把它关掉。
                    "YIBAN_PW_CONFIRM_TTL=0\n")
        # 重写 .env 会抹掉首启迁移生成的哈希，而 `verify_admin` 对"只有明文"是
        # fail-closed 拒绝 ⇒ 口令门会一律 403。补跑一次与启动同源的迁移，让夹具
        # 回到真实部署的样子（不是为了让测试变绿而放宽断言）。
        self.webapp.migrate_admin_password_to_hash(self.env_file)

    def _read_env(self):
        with open(self.env_file, encoding="utf-8") as f:
            return dict(ln.split("=", 1) for ln in f.read().splitlines()
                        if "=" in ln and not ln.startswith("#"))

    def _login(self):
        c = self.webapp.create_app().test_client()
        r = c.post("/api/login", json={"username": "admin@test.local", "password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        c.csrf = c.get("/api/me").get_json()["csrf_token"]
        return c

    def _rows(self, c):
        return c.get("/api/scheduler/executors").get_json()["executors"]

    def _add(self, c, payload=None):
        body = dict(payload or {})
        body.setdefault("confirm_password", ADMIN_PASS)
        return c.post("/api/scheduler/executors/rows", json=body,
                      headers={"X-CSRF-Token": c.csrf})

    def _put_row(self, c, slot, payload):
        return c.put(f"/api/scheduler/executors/rows/{slot}", json=payload,
                     headers={"X-CSRF-Token": c.csrf})


class WritePasswordGateTest(_GuardBase):
    """口令门：只在真变更时要求、被拒时不动配置、失败必审计且不记口令。"""

    def test_append_requires_password_and_does_not_write(self):
        c = self._login()
        before = self._read_env().get("YIBAN_EXECUTORS", "")
        with mock.patch.object(self.webapp.db, "audit", return_value=True) as m:
            r = c.post("/api/scheduler/executors/rows", json={"proxy": "http://n:1"},
                       headers={"X-CSRF-Token": c.csrf})
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["error"], "需要输入当前口令，设置未生效")
        self.assertEqual(r.get_json()["reason"], "password_required")
        self.assertEqual(self._read_env().get("YIBAN_EXECUTORS", ""), before,
                         "被拒时不得落盘")
        detail = " ".join(str(a) for a in m.call_args[0])
        self.assertIn("executors_pw_fail", detail, "失败必须审计留痕")
        self.assertIn("追加", detail, "审计要能看出是哪个动作")
        # 带正确口令 → 成功
        self.assertEqual(self._add(c, {"proxy": "http://n:1"}).status_code, 200)

    def test_wrong_password_is_403_and_audit_has_no_password(self):
        c = self._login()
        with mock.patch.object(self.webapp.db, "audit", return_value=True) as m:
            r = c.post("/api/scheduler/executors/rows",
                       json={"proxy": "http://n:1", "confirm_password": "wrong-pass"},
                       headers={"X-CSRF-Token": c.csrf})
        self.assertEqual(r.status_code, 403)
        raw = json.dumps([list(a) for a in m.call_args[0]], ensure_ascii=False)
        self.assertNotIn("wrong-pass", raw, "审计绝不许记口令")

    def test_delete_requires_password_and_keeps_the_row(self):
        c = self._login()
        slot = self._add(c, {"proxy": "http://n:1"}).get_json()["slot"]
        r = c.delete(f"/api/scheduler/executors/rows/{slot}",
                     headers={"X-CSRF-Token": c.csrf})
        self.assertEqual(r.status_code, 403, "删行必须带口令")
        self.assertIn(slot, [e["slot"] for e in self._rows(c)], "被拒时那一行必须还在")

    def test_update_requires_password_only_on_real_change(self):
        c = self._login()
        slot = self._add(c, {"proxy": "http://n:1", "name": "机房A"}).get_json()["slot"]
        # ① 同值提交：不算变更
        self.assertEqual(self._put_row(c, slot, {"proxy": "http://n:1"}).status_code, 200,
                         "同值提交不该被口令门拦下")
        # ② 只改名：不要求口令（改名不改行为）
        r = self._put_row(c, slot, {"name": "机房B"})
        self.assertEqual(r.status_code, 200, "只改名不该被口令门拦下")
        self.assertEqual(r.get_json()["name"], "机房B")
        # ③ 真的改出口：要口令
        r = self._put_row(c, slot, {"proxy": "http://other:2"})
        self.assertEqual(r.status_code, 403, "改出口必须带口令")
        self.assertNotIn('"proxy":"http://other:2"',
                         self._read_env().get("YIBAN_EXECUTORS", ""), "被拒时不落盘")
        # ④ 带口令 → 成功
        r = self._put_row(c, slot, {"proxy": "http://other:2",
                                    "confirm_password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_slot_egress_requires_password_only_on_real_change(self):
        c = self._login()
        # 同值：不要求口令（页面点"保存"不该弹口令）
        r = c.put("/api/scheduler/executors/workers/0", json={"egress": ""},
                  headers={"X-CSRF-Token": c.csrf})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        # 真的不一样：要口令
        r = c.put("/api/scheduler/executors/workers/0", json={"egress": "http://new:9"},
                  headers={"X-CSRF-Token": c.csrf})
        self.assertEqual(r.status_code, 403, "改出口必须带口令")
        self.assertNotIn("http://new:9", self._read_env().get("YIBAN_EXECUTORS", ""),
                         "被拒时不落盘")
        # 兜底段同理
        self.assertEqual(c.put("/api/scheduler/executors/fallback",
                               json={"egress": "http://fb2:9"},
                               headers={"X-CSRF-Token": c.csrf}).status_code, 403)

    def test_whole_write_requires_password_only_on_real_change(self):
        c = self._login()
        values = {"workers": 3, "proxy_list": "http://p1:1",
                  "proxy_fallback": "http://fb:9"}
        # 先带口令真写一次（这一步建立了"现值"）
        r = c.put("/api/scheduler/executors", headers={"X-CSRF-Token": c.csrf},
                  json={**values, "confirm_password": ADMIN_PASS})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        # 再用**完全相同的值**提交：不要求口令（前端"保存"点一下不该弹口令）
        r = c.put("/api/scheduler/executors", headers={"X-CSRF-Token": c.csrf},
                  json=dict(values))
        self.assertEqual(r.status_code, 200, "同值提交不该要求口令")
        # 真的改一个开关（兜底开关）：要口令
        r = c.put("/api/scheduler/executors", headers={"X-CSRF-Token": c.csrf},
                  json={"fallback_enable": "1"})
        self.assertEqual(r.status_code, 403, "改开关必须带口令")
        self.assertNotEqual(self._read_env().get("YIBAN_FALLBACK_ENABLE", ""), "1",
                            "被拒时不落盘")

    def test_same_value_submission_still_needs_no_password_after_a_real_change(self):
        """反向控制：先带口令真改一次，再把"改后的值"原样提交 → 不要求口令。"""
        c = self._login()
        r = c.put("/api/scheduler/executors/workers/0",
                  json={"egress": "http://set:1", "confirm_password": ADMIN_PASS},
                  headers={"X-CSRF-Token": c.csrf})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        r2 = c.put("/api/scheduler/executors/workers/0", json={"egress": "http://set:1"},
                   headers={"X-CSRF-Token": c.csrf})
        self.assertEqual(r2.status_code, 200, "同值提交不该要求口令")

    def test_measure_does_not_require_password(self):
        """实测端点不改配置（也不写签到态）：只判主管理员，与手动签到同口径。"""
        c = self._login()
        r = c.post("/api/scheduler/executors/measure", json={},
                   headers={"X-CSRF-Token": c.csrf})
        self.assertNotEqual(r.status_code, 403, "实测不该被口令门拦下")


class RowNameTest(_GuardBase):
    """行的自定义名：`name` 与后端 `label` 分开，未设=null，空串=清除。"""

    def test_default_rows_have_no_name(self):
        c = self._login()
        rows = self._rows(c)
        self.assertTrue(rows)
        for row in rows:
            self.assertIn("name", row, "字段必须照给（前端据此做能力探测）")
            self.assertIsNone(row["name"], "没设过就是 null")

    def test_set_then_clear_name(self):
        c = self._login()
        r = self._add(c, {"proxy": "http://n:1", "name": "机房A"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        slot = r.get_json()["slot"]
        self.assertEqual(r.get_json()["name"], "机房A")
        row = next(e for e in self._rows(c) if e["slot"] == slot)
        self.assertEqual(row["name"], "机房A")
        self.assertNotIn("机房A", row["label"], "自定义名不得混进后端 label")
        self.assertIn('"name":"机房A"', self._read_env().get("YIBAN_EXECUTORS", ""))
        r = self._put_row(c, slot, {"name": "", "confirm_password": ADMIN_PASS})
        self.assertIsNone(r.get_json()["name"])
        self.assertIsNone(next(e for e in self._rows(c) if e["slot"] == slot)["name"])

    def test_name_with_line_break_is_400(self):
        """名字要挤在 `.env` 的同一行里：换行是注入口，必须拦在写盘之前。"""
        c = self._login()
        for bad in ("a\nb", "a\rb", "a\u2028b"):
            with self.subTest(name=repr(bad)):
                r = self._add(c, {"name": bad})
                self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
                self.assertIn("换行", r.get_json()["error"])

    def test_overlong_name_is_400_not_silently_truncated(self):
        c = self._login()
        r = self._add(c, {"name": "x" * (self.webapp.yb_egress.NAME_MAX_LEN + 1)})
        self.assertEqual(r.status_code, 400)
        self.assertIn("最长", r.get_json()["error"])

    def test_label_is_single_source_for_both_pages(self):
        """兜底行的显示名只有一处（`egress.role_label`）：清单行与账号页角色列同串。"""
        c = self._login()
        fb = c.get("/api/scheduler/executors").get_json()["fallback"]
        self.assertEqual(fb["label"], self.webapp.yb_egress.role_label(
            self.webapp.yb_egress.ROLE_FALLBACK))
        self.assertEqual(fb["label"], "故障转移")


class AuditActorTest(_GuardBase):
    """执行体写的审计 actor = 当前会话用户名（批 3 §4.6：这里曾硬编码 "admin"）。

    执行体写端点只向主管理员开放，所以改前改后**放行判定**一样；问题在取证：审计表里
    一句写死的"admin"既不是任何真实账号，也与全表其他行的口径不一致——按 actor 追人时
    这一列直接失效。改后的口径与 `logout_ok`/`forbidden_path` 逐字同构。
    """

    def test_every_write_records_the_session_user(self):
        import db
        c = self._login()
        slot = self._add(c, {"proxy": "http://203.0.113.10:8080"}).get_json()["slot"]
        self.assertEqual(self._put_row(
            c, slot, {"proxy": "http://203.0.113.10:8081",
                      "confirm_password": ADMIN_PASS}).status_code, 200)
        self.assertEqual(c.delete(
            f"/api/scheduler/executors/rows/{slot}",
            json={"confirm_password": ADMIN_PASS},
            headers={"X-CSRF-Token": c.csrf}).status_code, 200)
        # 整表写端点（另一条落点，此前同样硬编码 actor）
        self.assertEqual(c.put("/api/scheduler/executors",
                               json={"workers": 2, "confirm_password": ADMIN_PASS},
                               headers={"X-CSRF-Token": c.csrf}).status_code, 200)
        rows = db.get_conn().execute(
            "SELECT username, detail FROM audit_logs WHERE target='executors'"
        ).fetchall()
        self.assertGreaterEqual(len(rows), 4, f"四条写路径都应留痕，实际 {len(rows)} 行")
        self.assertEqual({r["username"] for r in rows}, {"admin@test.local"},
                         "actor 必须是登录名，不是硬编码的 admin")


if __name__ == "__main__":
    unittest.main(verbosity=2)
