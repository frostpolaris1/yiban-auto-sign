# -*- coding: utf-8 -*-
"""对外脱敏与出站白名单的残余缺口回归（本轮对抗性审查活体复现的三条）。

三条都是"看起来已收口、但构造一个输入就漏"的形态，故钉确定性断言：
1. `sanitize_text` 的凭据键名——`\b` 在 `_`/`-` 处不构成边界，`refresh_token` /
   `session_id` / `id_token` / `JSESSIONID` / `x-csrf` / `api_key` 整族原样返回；
2. `sanitize_text` 的引号配对——口令含单引号时 repr 用双引号包裹，旧值类 `[^'"]*`
   在第一个单引号处截断，残留首引号之后的明文尾巴；
3. `is_safe_url` 的段覆盖——`ipaddress.is_private` 不含 CGNAT 100.64.0.0/10
   （云厂商元数据服务落在此段），组播/保留段与 IPv4-mapped IPv6 写法同样放行。
另附 `_mask_addr` 逗号列表与 `sanitize_url` 手机号两处。

标签：G · 安全：脱敏/审计/配置注入
覆盖：脱敏三处残余绕过面（复合凭据键名、引号配对截断、`is_safe_url` 段覆盖：CGNAT/组播/
保留段与 IPv4-mapped IPv6 写法）、收件人逗号列表逐项遮罩、通知密钥定宽遮罩、
URL query 手机号按名/按值两条口径；末尾另有一组 signin 修复用例（状态文件自愈、
ydclearance 挑战解码与跳转白名单）。
对应实现：`yiban/masking.py` 的 `sanitize_text` / `sanitize_url`（`_CRED_KEY`、
`_QUOTED_OR_BARE`）、`yiban/notify/config.py` 的 `is_safe_url` 与 `_mask_secret`、
`yiban/mail/config.py` 的 `_mask_addr`；末尾一组走 `signin` 门面（仓库根的旧名 shim），
真实现在 `yiban/client.py::YibanClient._solve_ydclearance` 与
`yiban/engine/state_io.py::_write_sign_state`。
关键断言：段覆盖用例逐个网段列举（含 `100.64.0.0/10` 这段 `ipaddress.is_private`
**不含**的 CGNAT），配 `test_public_https_targets_still_allowed` 一条正向对照——
只有负例的话"一律拒绝"也算过。密钥遮罩断的是**星数不随长度变化**（否则等于把密钥
精确长度也发出去）。ydclearance 三条白名单拒绝分别钉"非白名单 / 形似主机 /
userinfo 绕过"，是三种不同构造，不要合并成一条。
本文件的 ydclearance 用例喂的是**自造假挑战页**（`_challenge_text` 按真模板形状造），
真模板换形状要靠 `legacy_bound` / `timeout_outside_script` 两个变体补，不等于对真站点
做过验证。
依赖：无网络（假页 + `__new__` 绕过构造，不发请求）、无 skip；
`YIBAN_STATE_DIR` 用临时目录覆盖后在 tearDown 还原。
"""
import json
import os
import shutil
import tempfile
import unittest
import unittest.mock as mock

import signin

from yiban.engine.accounts import Account
from yiban.mail import config as mail_config
from yiban.masking import sanitize_text, sanitize_url
from yiban.notify import config as notify_config

# 口令样本刻意覆盖：单引号、括号、双引号、空格
PASSWORDS = ["p@ss'w0rd)1", "abc'def'ghi", 'x"y"z', "a b c'd", "plain-123"]


class CredKeyNamesTest(unittest.TestCase):
    """复合凭据键名必须整族覆盖（旧实现 6/6 漏网）。"""

    def test_compound_credential_keys_are_masked(self):
        for key in ("refresh_token", "session_id", "id_token", "JSESSIONID",
                    "x-csrf", "api_key", "apikey", "auth-secret", "access_token"):
            value = f"SECRETVALUE_{key}"
            out = sanitize_text(f"{key}={value}")
            self.assertNotIn(value, out, f"{key} 的值不得出现在脱敏结果里: {out}")

    def test_credential_value_with_space_in_double_quotes_leaves_no_tail(self):
        """含空格/括号的值不得在引号处截断（旧实现残留 `w0rd)1`）。"""
        for pw in PASSWORDS:
            acc = Account(phone="13800000000", password=pw)
            for shape in (
                repr(acc),
                f"登录失败 Account(phone='13800000000', password={pw!r})",
                "cfg=" + repr({"phone": "13800000000", "password": pw}),
                '{"password": ' + repr(pw) + "}",
            ):
                out = sanitize_text(shape)
                tail = pw.split("'")[-1]
                self.assertNotIn(pw, out, f"整串泄漏: {out}")
                if len(tail) >= 3:
                    self.assertNotIn(tail, out, f"残留明文尾巴 {tail!r}: {out}")

    def test_benign_words_are_not_over_masked(self):
        """不含凭据词族的普通诊断文本保持原样（防误伤把日志改成不可读）。"""
        text = "sign failed: monkey=1 consider=2 task_id=88 range=10-20"
        self.assertEqual(sanitize_text(text), text)


class SafeUrlSegmentsTest(unittest.TestCase):
    def test_cgnat_multicast_reserved_are_blocked(self):
        for host in ("100.100.100.200", "100.64.0.1", "100.127.255.255",
                     "224.0.0.1", "239.255.255.255", "240.0.0.1"):
            self.assertFalse(notify_config.is_safe_url(f"https://{host}/hook"),
                             f"CGNAT/组播/保留段应被拒: {host}")
        self.assertFalse(notify_config.is_safe_url("https://[ff02::1]/hook"))
        self.assertFalse(notify_config.is_safe_url("https://[::2]/hook"))

    def test_ipv4_mapped_ipv6_cannot_bypass(self):
        for url in ("https://[::ffff:100.100.100.200]/hook",
                    "https://[::ffff:127.0.0.1]/hook",
                    "https://[::ffff:10.0.0.7]/hook"):
            self.assertFalse(notify_config.is_safe_url(url), f"映射写法绕过: {url}")

    def test_public_https_targets_still_allowed(self):
        for url in ("https://sctapi.ftqq.com/x.send", "https://example.com/hook",
                    "https://1.2.3.4/hook"):
            self.assertTrue(notify_config.is_safe_url(url), f"公网目标被误拒: {url}")


class MailAddrMaskingTest(unittest.TestCase):
    def test_comma_listed_recipients_masked_item_by_item(self):
        v = "alice@qq.com,bob@corp.cn,carolwang@secret.org"
        out = mail_config._mask_addr(v)
        for leaked in ("bob@", "carolwang@", "alice@"):
            self.assertNotIn(leaked, out, f"逗号列表泄漏完整地址: {out}")
        self.assertEqual(len(out.split(",")), 3)

    def test_single_and_empty_semantics_unchanged(self):
        self.assertEqual(mail_config._mask_addr("alice@qq.com"), "a****@qq.com")
        self.assertEqual(mail_config._mask_addr(""), "<未配置>")
        self.assertEqual(mail_config._mask_addr("not-an-email"), "not-an-email")


class NotifySecretMaskTest(unittest.TestCase):
    def test_secret_mask_is_fixed_width_and_keeps_both_ends(self):
        short_key = notify_config._mask_secret("SCT1234567890abcdef")
        long_key = notify_config._mask_secret("SCT1234567890abcdefghijklmnopqrstuv")
        self.assertEqual(short_key, "SCT***ef")
        self.assertEqual(long_key, "SCT***uv")
        self.assertEqual(short_key.count("*"), long_key.count("*"),
                         "星数不得随密钥长度变化（否则等于把精确长度也发出去）")

    def test_short_secret_gets_no_tail(self):
        # 6 位值若照"前 3 + 后 2"打码会露出 5/6，打码名不副实
        out = notify_config._mask_secret("abc123")
        self.assertEqual(out, "ab***")
        self.assertEqual(notify_config._mask_secret(""), "")


class UrlPhoneTest(unittest.TestCase):
    def test_phone_in_query_masked_by_name_and_by_value(self):
        by_name = sanitize_url("https://f.yiban.cn/cb?mobile=13800008000")
        self.assertNotIn("13800008000", by_name)
        by_value = sanitize_url("https://f.yiban.cn/cb?u=13800008000&keep=abc")
        self.assertNotIn("13800008000", by_value)
        self.assertIn("keep=abc", by_value, "非手机号参数不受影响")
        self.assertIn("138****8000", by_value, "按值打码保留前 3 后 4（与 mask_phone 同口径）")

    def test_high_entropy_value_rule_still_works(self):
        out = sanitize_url("https://f.yiban.cn/cb?x=" + "A" * 30)
        self.assertNotIn("A" * 30, out)


BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _challenge_text(target_url, cookie="abc123", arg=0, k=1000, legacy_bound=False):
    """构造可被 _solve_ydclearance 解析的本地假挑战页（不访问网络）。

    按真模板形状造：po 循环 `qo < oo.length - 1` 比 C 段上界多取一格，而真模板里
    n_c = len(oo) - 3，故 n_c+1 落在 C 变换范围之外——那一格直接存"收尾字符 ^ arg"
    （收尾字符是跳转路径的右引号）。两段都按 `chr(oo[i] ^ arg)` 的读法预先把 arg 异或
    编进去（arg 需 ≤ 0xFF，否则异或的高位会在解码侧带回来）。legacy_bound=True 还原
    改造前的假页形状（数组不设最后两格、po 上界恰好等于 n_c），用作"少解收尾字符"的负对照。
    """
    desired = f"https_ydclearance={cookie};window.document.location=\"{target_url}\""
    add1, add2, shift_l, shift_r = 1, 2, 3, 5
    head, tail = (desired, "") if legacy_bound else (desired[:-1], desired[-1])
    n_c = len(head)
    vals = [ord(c) ^ arg for c in head]  # 先异或 arg：解码侧读法是 chr(oo[i] ^ arg)
    arr = [0] + [
        ((((v >> shift_l) | ((v << shift_r) & 0xFF)) - add1 - add2) & 0xFF) for v in vals
    ]
    if not legacy_bound:
        arr += [ord(tail) ^ arg, 0]  # n_c+1 格存尾字符；len(oo)-1 格在模板里从不参与运算
    arr_str = ",".join(hex(x) for x in arr)
    return (
        "function ab(arg) { "
        'eval("qo=eval;qo(po);"); '
        "oo = [" + arr_str + "]; "
        '"qo=1; do{oo[qo]=(-oo[qo])&0xff;'
        "oo[qo]=((oo[qo]>>3)|((oo[qo]<<5)&0xff)-1)&0xff;} while(--qo>=2);\" "
        "qo = 1; do { oo[qo] = (oo[qo] - oo[qo - 1]) } while(--qo>=2); "
        f"if (qo > {n_c}) break; "
        "oo[qo] = ((((oo[qo] + 1) & 0xff) + 2) & 0xff) << 3) >> 5); qo++; "
        f"for (qo = 1; qo < oo.length - 1; qo++) "
        f"if (qo % {k}) po += String.fromCharCode(oo[qo] ^ arg); "
        f'window.document.location="{target_url}"; }} '
        f'window.onload=setTimeout("ab({arg})", 200) </script>'
    )


def _challenge_text_timeout_outside_script(target_url, cookie="abc123", arg=0):
    """真模板的另一种形状：`window.onload=setTimeout(...)` 落在 `</script>` 之后。

    这样它就不在 `re` 从 `function … </script>` 截出的函数体里——挑战参数只能回退整页
    找（`yiban/fyiban/waf.py` 的整页回退分支）。其余形状与 `_challenge_text` 逐字相同。
    """
    text = _challenge_text(target_url, cookie=cookie, arg=arg)
    body, sep, tail = text.rpartition("window.onload=setTimeout")
    assert sep and tail.endswith("</script>"), \
        "假页形状变了：setTimeout 段不在末尾的 </script> 之前"
    # 把 </script> 挪到函数体之后、setTimeout 之前：函数体与 setTimeout 分属两个脚本块
    return body.rstrip() + "</script> " + sep + tail[: -len("</script>")].rstrip()


class SigninFixes021Test(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="yiban-signin-fix-")
        self._old_state_dir = os.environ.get("YIBAN_STATE_DIR")
        os.environ["YIBAN_STATE_DIR"] = self._tmp

    def tearDown(self):
        if self._old_state_dir is None:
            os.environ.pop("YIBAN_STATE_DIR", None)
        else:
            os.environ["YIBAN_STATE_DIR"] = self._old_state_dir
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ---- H2 ----
    def test_write_sign_state_rebuilds_corrupt_json(self):
        today = signin.clock.today()
        state_path = os.path.join(self._tmp, f"sign-state-{today}.json")
        with open(state_path, "w", encoding="utf-8") as f:
            f.write("{ not json !!!")
        signin._write_sign_state("13800138000", "success", "ok")
        with open(state_path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["13800138000"]["status"], "success")

    def test_write_sign_state_rebuilds_non_dict_json(self):
        today = signin.clock.today()
        state_path = os.path.join(self._tmp, f"sign-state-{today}.json")
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(["not", "dict"], f)
        signin._write_sign_state("13800138000", "already", "今日已签到")
        with open(state_path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["13800138000"]["status"], "already")

    def test_write_sign_state_unwritable_dir_does_not_raise(self):
        # state_dir 是一个普通文件：makedirs 会失败，必须被吞掉
        blocked = os.path.join(self._tmp, "blocked")
        with open(blocked, "w", encoding="utf-8") as f:
            f.write("x")
        os.environ["YIBAN_STATE_DIR"] = blocked
        signin._write_sign_state("13800138000", "failed", "err")  # 不应抛异常

    # ---- H8 ----
    def test_solve_ydclearance_accepts_whitelist_absolute_url(self):
        client = signin.YibanClient.__new__(signin.YibanClient)
        cookie, target = client._solve_ydclearance(_challenge_text("https://f.yiban.cn/iapp7463"))
        self.assertEqual(cookie, "abc123")
        self.assertTrue(target.startswith("https://f.yiban.cn"), target)

    def test_solve_ydclearance_rejects_non_whitelist_url(self):
        client = signin.YibanClient.__new__(signin.YibanClient)
        with self.assertRaisesRegex(RuntimeError, "ydclearance 跳转目标不在白名单"):
            client._solve_ydclearance(_challenge_text("http://evil.example"))

    def test_solve_ydclearance_rejects_lookalike_host(self):
        client = signin.YibanClient.__new__(signin.YibanClient)
        with self.assertRaisesRegex(RuntimeError, "ydclearance 跳转目标不在白名单"):
            client._solve_ydclearance(_challenge_text("https://f.yiban.cn.evil.com/iapp7463"))

    def test_solve_ydclearance_rejects_userinfo_bypass(self):
        client = signin.YibanClient.__new__(signin.YibanClient)
        with self.assertRaisesRegex(RuntimeError, "ydclearance 跳转目标不在白名单"):
            client._solve_ydclearance(_challenge_text("https://f.yiban.cn@evil.com/iapp7463"))

    # ---- T-WAF-9：po 上界与真模板 `qo < oo.length - 1` 对齐 ----
    def test_solve_ydclearance_decodes_tail_char_outside_transform_c(self):
        """收尾字符（路径右引号）在下标 len(oo)-2，不在 C 变换范围内，必须逐字解出。

        arg 同时覆盖非 0 值：假页的 head 与 tail 两段都要按 `chr(oo[i] ^ arg)` 的
        读法把参数异或编进去，否则只有 arg=0 才自洽。158 是公开样本里的真实量级。
        """
        client = signin.YibanClient.__new__(signin.YibanClient)
        for arg in (0, 7, 158):
            with self.subTest(arg=arg):
                cookie, target = client._solve_ydclearance(
                    _challenge_text("https://f.yiban.cn/iapp7463", arg=arg)
                )
                self.assertEqual(cookie, "abc123")
                self.assertEqual(target, "https://f.yiban.cn/iapp7463")

    def test_solve_ydclearance_settimeout_outside_script_tag(self):
        """T-WAF-10：setTimeout 在 `</script>` 之后（真模板形状）时，参数须回退整页提取。

        此时 setTimeout 文本不在挑战函数体内，只看函数体会漏掉 arg；回退分支必须生效，
        且回退取到的 arg 要真正参与 po 解码（arg=158 为公开样本量级）。
        """
        client = signin.YibanClient.__new__(signin.YibanClient)
        cookie, target = client._solve_ydclearance(
            _challenge_text_timeout_outside_script("https://f.yiban.cn/iapp7463", arg=158)
        )
        self.assertEqual(cookie, "abc123")
        self.assertEqual(target, "https://f.yiban.cn/iapp7463")

    def test_solve_ydclearance_old_fixture_shape_fails_loudly(self):
        """改造前的假页形状（po 上界恰好等于 C 段上界）在新实现下必须响亮失败，不得静默截断。"""
        client = signin.YibanClient.__new__(signin.YibanClient)
        with self.assertRaisesRegex(RuntimeError, "ydclearance 挑战解析失败"):
            client._solve_ydclearance(
                _challenge_text("https://f.yiban.cn/iapp7463", legacy_bound=True)
            )

    # ---- T-WAF-6：异常契约（越界输入给明确 RuntimeError，不给裸内置异常）----
    def test_solve_ydclearance_rejects_arg_beyond_code_point_range(self):
        client = signin.YibanClient.__new__(signin.YibanClient)
        with self.assertRaisesRegex(RuntimeError, "ydclearance 挑战解析失败"):
            client._solve_ydclearance(
                _challenge_text("https://f.yiban.cn/iapp7463", arg=0x110000)
            )

    def test_solve_ydclearance_rejects_zero_k(self):
        client = signin.YibanClient.__new__(signin.YibanClient)
        with self.assertRaisesRegex(RuntimeError, "ydclearance 挑战解析失败"):
            client._solve_ydclearance(_challenge_text("https://f.yiban.cn/iapp7463", k=0))

    def test_is_fyiban_url_helper(self):
        self.assertTrue(signin._is_fyiban_url("https://f.yiban.cn/iapp7463"))
        self.assertFalse(signin._is_fyiban_url("https://f.yiban.cn.evil.com/iapp7463"))
        self.assertFalse(signin._is_fyiban_url("https://f.yiban.cn@evil.com/iapp7463"))
        self.assertFalse(signin._is_fyiban_url("http://f.yiban.cn/iapp7463"))

    # ---- H9 ----
    def test_attempt_signin_returns_safe_err_not_raw_exception(self):
        acc = signin.Account(phone="13800138000", password="secret")
        with mock.patch.object(signin.YibanClient, "login_killyiban",
                               side_effect=ValueError("boom password='raw'")), \
             mock.patch.object(signin, "_sanitize_text", return_value="SANITIZED"):
            success, message, skip, status = signin.attempt_signin(acc)
        self.assertFalse(success)
        self.assertEqual(message, "SANITIZED")
        self.assertFalse(skip)
        self.assertEqual(status, signin.STATUS_FAILED)

    # ---- M13 ----
    def test_retry_wait_never_below_retry_min_interval(self):
        acc = signin.Account(phone="13800138000", password="p")
        sleeps = []

        def fake_sleep(seconds):
            sleeps.append(seconds)

        with mock.patch.object(signin, "attempt_signin",
                               return_value=(False, "网络超时", False, signin.STATUS_FAILED)), \
             mock.patch.object(signin, "classify_failure", return_value=2), \
             mock.patch.object(signin, "random") as rnd, \
             mock.patch.object(signin.time, "sleep", side_effect=fake_sleep), \
             mock.patch.object(signin, "_write_sign_state"), \
             mock.patch.object(signin, "_update_cred_state"), \
             mock.patch.object(signin, "send_notification"):
            # 重试打散项 random.uniform(0, RETRY_GAP_MAX) 统一返回 0：排除随机抖动后
            # 精确断言下限
            rnd.uniform.return_value = 0.0
            signin.run_queue_retry([acc], "", 0, 30)
        self.assertTrue(sleeps, "应发生重试等待")
        self.assertGreaterEqual(sleeps[0], signin.RETRY_MIN_INTERVAL,
                                "重试总间隔不得小于 RETRY_MIN_INTERVAL")
        # 重试回队的间隔对齐只补残差（elapsed 已含原地等待），不得二次等待：
        # 总次数=原地等待+对齐残差，且合计仍 ≥ RETRY_MIN_INTERVAL
        self.assertEqual(len(sleeps), 2)
        self.assertLessEqual(sleeps[1], 30)
        self.assertGreaterEqual(sleeps[0] + sleeps[1], signin.RETRY_MIN_INTERVAL)

    def test_manual_queue_honors_account_gap_floor(self):
        """手动队列（schedule=None）相邻请求间隔必须 ≥ 账号间隔设置（下限语义）。

        手动路径与自动调度、容量预估 (avg+gap) 共用「最小间隔」口径；
        原实现此处为 U(0, gap) 随机打散：期望仅 gap/2、下界 0，违背
        「自动与手动签到均生效的最小间隔」承诺。"""
        accs = [signin.Account(phone=f"1380000000{i}", password="p") for i in (1, 2)]
        sleeps = []
        with mock.patch.object(signin, "attempt_signin",
                               return_value=(True, "已签到", False, "already")), \
             mock.patch.object(signin.time, "sleep", side_effect=sleeps.append), \
             mock.patch.object(signin.time, "monotonic", return_value=1000.0), \
             mock.patch.object(signin, "_write_sign_state"), \
             mock.patch.object(signin, "_update_cred_state"):
            signin.run_queue_retry(accs, "", 0, 10, schedule=None, cred_state={})
        # monotonic 冻结 → 账号 2 弹出时距上次尝试 elapsed=0，应补足整段间隔 10s；
        # 首个账号不等待
        self.assertEqual(sleeps, [10.0])

    def test_manual_queue_gap_zero_no_wait(self):
        """账号间隔 0=关闭：手动队列不得引入额外等待。"""
        accs = [signin.Account(phone=f"1380000000{i}", password="p") for i in (3, 4)]
        sleeps = []
        with mock.patch.object(signin, "attempt_signin",
                               return_value=(True, "已签到", False, "already")), \
             mock.patch.object(signin.time, "sleep", side_effect=sleeps.append), \
             mock.patch.object(signin.time, "monotonic", return_value=1000.0), \
             mock.patch.object(signin, "_write_sign_state"), \
             mock.patch.object(signin, "_update_cred_state"):
            signin.run_queue_retry(accs, "", 0, 0, schedule=None, cred_state={})
        self.assertEqual(sleeps, [])

    def test_gap_max_clamped_to_3600(self):
        """.env 直配超大值不得把队列睡死（与网页设置侧 3600 上限同口径）。"""
        accs = [signin.Account(phone=f"1380000000{i}", password="p") for i in (5, 6)]
        sleeps = []
        with mock.patch.object(signin, "attempt_signin",
                               return_value=(True, "已签到", False, "already")), \
             mock.patch.object(signin.time, "sleep", side_effect=sleeps.append), \
             mock.patch.object(signin.time, "monotonic", return_value=1000.0), \
             mock.patch.object(signin, "_write_sign_state"), \
             mock.patch.object(signin, "_update_cred_state"):
            signin.run_queue_retry(accs, "", 0, 86400, schedule=None, cred_state={})
        self.assertTrue(sleeps and max(sleeps) <= 3600)

    def test_retry_wait_schedule_branch_respects_sch_cfg_retry_min_interval(self):
        """P4（2026-08-27）：schedule 分支重试改为非阻塞重插——重试落点 ≥ now + retry_min_interval，
        不再原地 sleep ≥5s 阻塞整条队列。"""
        from random import Random as _R
        acc = signin.Account(phone="13800138000", password="p")
        sleeps = []
        scheduled_at = signin.clock.now()
        captured = {}
        orig_next = signin._next_retry_at

        def spy(now_dt, sch_cfg, rng=None):
            captured["nxt"] = orig_next(now_dt, sch_cfg, rng=_R(1))
            return captured["nxt"]

        with mock.patch.dict(
            os.environ,
            {
                "YIBAN_RETRY_MIN_INTERVAL": "5",
                "YIBAN_SIGN_START": "00:00",
                "YIBAN_SIGN_END": "23:59",
                "YIBAN_WINDOW_EDGE_SEC": "0",
            },
            clear=False,
        ), \
             mock.patch.object(signin, "attempt_signin",
                               return_value=(False, "网络超时", False, signin.STATUS_FAILED)), \
             mock.patch.object(signin, "classify_failure", return_value=2), \
             mock.patch.object(signin.time, "sleep", side_effect=lambda s: sleeps.append(s)), \
             mock.patch.object(signin, "_write_sign_state"), \
             mock.patch.object(signin, "_update_cred_state"), \
             mock.patch.object(signin, "send_notification"), \
             mock.patch.object(signin, "_next_retry_at", side_effect=spy):
            signin.run_queue_retry(
                [acc], "", 0, 0, schedule={acc.phone: scheduled_at}
            )
        self.assertIn("nxt", captured, "schedule 分支应计算重试落点")
        # 落点距失败时刻 ≥ retry_min_interval（5s，容差 2s），且不超出窗口末端（eff_hi=23:59）
        self.assertGreaterEqual(
            captured["nxt"] - signin.clock.now(),
            __import__("datetime").timedelta(seconds=3),
            "重试落点不得早于 now + retry_min_interval",
        )
        # 落点 ≤ eff_hi = sign_end - edge_back = 23:59（当日）
        eff_hi = signin.clock.now().replace(hour=23, minute=59, second=0, microsecond=0)
        self.assertLessEqual(captured["nxt"], eff_hi, "重试落点不得越过窗口末端")

    # ---- 通知组件化（2026-08-29）----
    def test_send_notification_delegates_to_notify_component(self):
        """signin 侧 webhook 已组件化：send_notification 委托 scripts/notify.py。

        webhook 的格式适配（Server酱 title+desp）、节流、响应检查与日志脱敏
        测试见 tests/test_notify_webhook.py（实现已从 signin 内联迁出）。
        """
        with mock.patch.object(signin.notify, "send", return_value=True) as m:
            signin.send_notification("标题", "内容", "https://legacy.example.com/hook")
        # send_notification 透传 urgent/force（默认 False）
        m.assert_called_once_with("标题", "内容", urgent=False, force=False)

    # ---- 低项：user_paused 显式布尔解析 ----
    def test_parse_account_dict_user_paused_explicit_truthy(self):
        for raw in ("1", "true", "on", "yes", " True ", 1):
            acc = signin._parse_account_dict(
                {"phone": "13800138000", "password": "p", "user_paused": raw}
            )
            self.assertTrue(acc.user_paused, raw)
        for raw in ("0", "false", "off", "no", "", 0):
            acc = signin._parse_account_dict(
                {"phone": "13800138000", "password": "p", "user_paused": raw}
            )
            self.assertFalse(acc.user_paused, raw)


if __name__ == "__main__":
    unittest.main()
