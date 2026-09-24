# -*- coding: utf-8 -*-
"""URL userinfo 脱敏：唯一实现 + 两处调用点（回显与日志）的接线守卫。

为什么单独一个口径：出口串按契约**允许带 `user:pass@`**，而
- web 的"地址格式不正确: <你填的值>"会把用户输入回显进响应/DOM；
- 引擎的异常消息（requests 会把完整 URL 嵌进去）会落日志。
`sanitize_url` 只处理 **query 参数**，不碰 userinfo（实测无 query 时原样返回），
故这两处必须各自过一遍 `mask_url_userinfo`。

标签：G · 安全：脱敏/审计/配置注入
覆盖：`mask_url_userinfo` 的四种写法（带 scheme / 裸 `user:pass@host` / 混在异常整句里 /
已遮形态幂等），加两条引擎接线断言。
对应实现：`yiban/masking.py` 的 `mask_url_userinfo`（`_URL_USERINFO_RE`）、
`yiban/engine/attempts.py` 与 `yiban/engine/probe.py` 的异常消息组装处。
关键断言：脱敏函数本身按"整串等值"断（`assertEqual(..., "http://***@host:8080")`），
接线两条是**源码级** `assertIn` 字面量匹配——它只证明那条调用链的文本还在，
不证明运行时真的走到；把这三处调用换名或改走 helper 会误红，把脱敏挪到永远不执行
的分支里则不会红。另外 `mask_url_userinfo` 的调用点不止本文件钉的两处——web 侧
`web/routes/settings_api.py` 与 `web/services/executor_env.py` 的回显也过它，
那几处不在本文件的断言范围内。
依赖：无网络、无 skip；接线用例读仓库源文件，故依赖 BASE 路径与那两处字面量共存。
"""
import os
import unittest

from yiban.masking import mask_url_userinfo

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


class EngineSanitizeWiringTest(unittest.TestCase):
    """接线守卫：引擎两处异常消息必须真的过 userinfo 脱敏。

    背景：这两处注释此前**宣称**已打码 userinfo，实际没有（`sanitize_url` 不管它）——
    注释与实现不符是最容易骗过 review 的形态，故用源码级断言钉住调用顺序。
    """

    def _src(self, rel):
        with open(os.path.join(BASE, rel), encoding="utf-8") as f:
            return f.read()

    def test_attempts_pipeline_masks_userinfo(self):
        src = self._src("yiban/engine/attempts.py")
        self.assertIn("_sanitize_text(_mask_url_userinfo(_sanitize_url(str(e))))", src)
        self.assertIn("from yiban.masking import mask_url_userinfo as _mask_url_userinfo", src)

    def test_probe_pipeline_masks_userinfo(self):
        src = self._src("yiban/engine/probe.py")
        self.assertIn("_sanitize_text(_mask_url_userinfo(_sanitize_url(str(e))))", src)
        self.assertIn("from yiban.masking import mask_url_userinfo as _mask_url_userinfo", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
