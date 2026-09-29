# -*- coding: utf-8 -*-
"""URL userinfo 脱敏：唯一实现 + 两处调用点（回显与日志）的接线守卫。

为什么单独一个口径：出口串按契约**允许带 `user:pass@`**，而
- web 的"地址格式不正确: <你填的值>"会把用户输入回显进响应/DOM；
- 引擎的异常消息（requests 会把完整 URL 嵌进去）会落日志。
`sanitize_url` 只处理 **query 与 fragment 参数**，不碰 userinfo（实测无参数时原样返回），
故这两处必须各自过一遍 `mask_url_userinfo`。

标签：G · 安全：脱敏/审计/配置注入
覆盖：`mask_url_userinfo` 的四种写法（带 scheme / 裸 `user:pass@host` / 混在异常整句里 /
已遮形态幂等）。
对应实现：`yiban/masking.py` 的 `mask_url_userinfo`（`_URL_USERINFO_RE`）。
关键断言：脱敏函数本身按"整串等值"断（`assertEqual(..., "http://***@host:8080")`）。
另注：`mask_url_userinfo` 的调用点不止一处——引擎 `yiban/engine/attempts.py` /
`yiban/engine/probe.py` 的异常消息组装、web 侧 `web/routes/settings_api.py` 与
`web/services/executor_env.py` 的回显也过它；这些**接线**的行为面由
`test_users_exit_surface` 与 `test_masking_ssrf_gaps` 在真实出口上钉住脱敏值。
依赖：无网络、无 skip。

> 批 6c3-A 说明：旧 `EngineSanitizeWiringTest` 两条**源码级** `assertIn` 字面量匹配
> 只证明调用链文本还在、不证明运行时真的走到（文件原文自述），按对表裁撤；
> 脱敏值本身的行为覆盖在真实出口测试上。
"""
import unittest

from yiban.masking import mask_url_userinfo


class MaskUrlUserinfoTest(unittest.TestCase):
    def test_masks_credentials_keeps_rest(self):
        for raw, want in (
            ("http://u1:secret@proxy.example:8080", "http://***@proxy.example:8080"),
            ("socks5://user:pwd@1.2.3.4:1080", "socks5://***@1.2.3.4:1080"),
            ("http://u:p@h/path?token=abc", "http://***@h/path?token=abc"),
            # 无 scheme 的裸写法也抹掉
            ("user:pass@host:8080", "***@host:8080"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(mask_url_userinfo(raw), want)

    def test_without_userinfo_is_untouched(self):
        for raw in ("http://proxy.example:8080", "notaurl", "", "连接超时"):
            with self.subTest(raw=raw):
                self.assertEqual(mask_url_userinfo(raw), raw)

    def test_embedded_in_exception_text(self):
        """异常消息里嵌了带凭据的 URL——整句过一遍即可，其余文字不动。"""
        msg = "ProxyError: cannot connect to http://u:p@proxy.example:8080 (refused)"
        out = mask_url_userinfo(msg)
        self.assertNotIn("u:p@", out)
        self.assertIn("***@proxy.example:8080", out)
        self.assertIn("(refused)", out)

    def test_idempotent(self):
        once = mask_url_userinfo("http://u:p@h:1")
        self.assertEqual(mask_url_userinfo(once), once)


if __name__ == "__main__":
    unittest.main(verbosity=2)
