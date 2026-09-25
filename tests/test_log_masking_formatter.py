# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""日志输出面脱敏兜底：`MaskingFormatter` 与 `mask_phones_in_text` 的不变量。

脱敏此前靠各调用点手工调 `mask_phone`，漏一处就漏一处；本文件钉住输出面的兜底：
任何一条日志记录（含异常文本）经格式化后都不再含裸号，且替换幂等、不误伤坐标/
时间戳/普通数字。展示层 `_mask_log_phones` 与本兜底共用同一实现。

功能：日志格式化与文本脱敏的行为回归。
归属：`yiban.masking`（文本口径）+ `yiban.logging_ext`（输出面）。
复用：`mask_phones_in_text` / `MaskingFormatter` / `mask_phone`。
通信：直接构造 `logging.LogRecord` 格式化；另经临时文件 handler 落盘读回；
由 pytest 收集 `unittest.TestCase`。

标签：G · 安全：脱敏/审计/配置注入
覆盖：`mask_phones_in_text` 的文本口径（裸号/方括号/多号/幂等/不误伤）与
`MaskingFormatter` 的输出面兜底（成文行、异常文本、临时文件 handler 落盘结果），
再加两条"同一实现"的接线断言。
对应实现：`yiban/masking.py` 的 `mask_phones_in_text` / `mask_phone`、
`yiban/logging_ext.py` 的 `MaskingFormatter.format`，展示层 `_mask_log_phones` 复用同一实现。
关键断言：**遮罩发生在"最终成文之后"这一层**——`format()` 覆写的是
`Formatter.format` 的返回串，所以 `%s` 插值后的号和 traceback 里的号都在射程内
（`test_bare_phone_in_chinese_comma_text_masked` 与 `test_exception_text_masked`
钉的就是这个落点）；
它**不覆盖**：没挂 `MaskingFormatter` 的 handler（装配点只有 web 的日 handler 与 CLI 的
`_setup_cli_logging` 两处）、非手机号形态的标识（邮箱/身份证/IP 一概不动）、
以及非"11 位连续数字"的号码写法——实测 `+8613800138000`、`138-0013-8000`、
`138 0013 8000` 都原样穿过（号码规则要求两侧不是数字、且只认连续 11 位），
盘上按天日志仍可能留裸号（signin 写盘 + 状态解析依赖），那条出口靠 HTTP 层的
`_mask_log_phones` 再遮一遍，见 `tests/test_logs_export_masking.py`。
坐标/时间戳/非号码数字串"不得被误伤"与"不得漏"同权重：误伤会让日志失去诊断价值。
依赖：无网络、无 skip；用固定 `rec.created` 钉住时间戳，避免挂钟影响断言。
"""
import logging
import os
import tempfile
import time
import unittest

from yiban.logging_ext import MaskingFormatter
from yiban.masking import mask_phone, mask_phones_in_text

FORMAT = "[%(asctime)s] [%(levelname)s] %(name)s: %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"


def _format(msg, exc_info=None, name="yiban"):
    """把一条日志记录交给 MaskingFormatter 格式化，返回最终输出串。"""
    rec = logging.LogRecord(name, logging.INFO, __file__, 1, msg, None, exc_info)
    rec.created = 1758500000.0  # 固定时间戳，避免挂钟影响断言
    return MaskingFormatter(FORMAT, datefmt=DATEFMT).format(rec)


class MaskPhonesInTextTest(unittest.TestCase):
    def test_bare_phone_masked(self):
        self.assertEqual(
            mask_phones_in_text("会话缓存作废（跨业务日）: 17851095321，本次真实登录"),
            "会话缓存作废（跨业务日）: 178****5321，本次真实登录",
        )

    def test_bracketed_phone_masked(self):
        self.assertEqual(
            mask_phones_in_text("[18951982278] ⏹️ 用户已取消签到，跳过执行"),
            "[189****2278] ⏹️ 用户已取消签到，跳过执行",
        )

    def test_multiple_phones_masked(self):
        self.assertEqual(
            mask_phones_in_text("13800138000 与 13900139000"),
            "138****8000 与 139****9000",
        )

    def test_idempotent_on_already_masked(self):
        once = mask_phones_in_text("号码 13800138000 已遮")
        self.assertEqual(once, "号码 138****8000 已遮")
        self.assertEqual(mask_phones_in_text(once), once, "重复脱敏不得再变形")

    def test_coordinate_and_timestamp_untouched(self):
        # 真实日志文本里的坐标、时间戳、dur/attempt 等数字不得被误伤
        text = ("[2026-09-23 06:31:01] [INFO] yiban: 定位 118.88459277562808 "
                "dur=118.88459277562808 attempt=3 第 3 次")
        self.assertEqual(mask_phones_in_text(text), text)

    def test_non_phone_digit_runs_untouched(self):
        for s in ("1380013800", "138001380001", "20260923063", "1758500000000"):
            self.assertEqual(mask_phones_in_text(s), s, f"{s} 不是手机号，不得改动")

    def test_empty_and_non_str(self):
        self.assertEqual(mask_phones_in_text(""), "")
        self.assertEqual(mask_phones_in_text(None), "None")


class MaskingFormatterTest(unittest.TestCase):
    def test_formatted_record_has_no_bare_phone(self):
        out = _format("[18951982278] ⏹️ 用户已取消签到，跳过执行")
        self.assertIn("189****2278", out)
        self.assertNotIn("18951982278", out)

    def test_bare_phone_in_chinese_comma_text_masked(self):
        out = _format("会话缓存作废（跨业务日）: 17851095321，本次真实登录",
                      name="yiban.store.session_cache")
        self.assertIn("178****5321", out)
        self.assertNotIn("17851095321", out)

    def test_exception_text_masked(self):
        try:
            raise RuntimeError("上游回显账号 13800138000 失败")
        except RuntimeError:
            import sys
            out = _format("签到异常", exc_info=sys.exc_info())
        self.assertIn("138****8000", out)
        self.assertNotIn("13800138000", out)

    def test_output_idempotent(self):
        rec = logging.LogRecord("yiban", logging.INFO, __file__, 1,
                                "[138****8000] 已遮", None, None)
        rec.created = 1758500000.0
        fmt = MaskingFormatter(FORMAT, datefmt=DATEFMT)
        self.assertEqual(fmt.format(rec), fmt.format(rec))
        self.assertIn("[138****8000]", fmt.format(rec))

    def test_timestamp_and_coordinate_untouched_in_output(self):
        out = _format("定位 118.88459277562808 dur=118.88459277562808 attempt=3")
        self.assertIn("118.88459277562808", out)
        expected_day = time.strftime("%Y-%m-%d", time.localtime(1758500000.0))
        self.assertIn(expected_day, out)  # asctime 未被误伤

    def test_file_handler_output_is_masked(self):
        """经真实文件 handler（输出面）落盘后文件里不得有裸号。"""
        fd, path = tempfile.mkstemp(prefix="yiban-mask-", suffix=".log")
        os.close(fd)
        try:
            handler = logging.FileHandler(path, encoding="utf-8")
            handler.setFormatter(MaskingFormatter(FORMAT, datefmt=DATEFMT))
            lg = logging.getLogger("yiban.test.maskformatter")
            lg.addHandler(handler)
            lg.setLevel(logging.INFO)
            lg.propagate = False
            try:
                lg.info("[18951982278] ⏹️ 用户已取消签到，跳过执行")
            finally:
                lg.removeHandler(handler)
                handler.close()
            with open(path, encoding="utf-8") as f:
                body = f.read()
            self.assertIn("189****2278", body)
            self.assertNotIn("18951982278", body)
        finally:
            os.remove(path)


class MaskPhoneSingleSourceTest(unittest.TestCase):
    def test_text_helper_reuses_mask_phone_semantics(self):
        # 文本口径与单值口径必须同形（单一来源），不得各写一套
        self.assertEqual(mask_phones_in_text("13800138000"), mask_phone("13800138000"))
        self.assertEqual(mask_phones_in_text("138****8000"), mask_phone("138****8000"))


class DisplayMaskingReuseTest(unittest.TestCase):
    """展示/导出层与输出面兜底共用同一实现，且覆盖非方括号形态的裸号。"""

    def test_mask_log_phones_delegates_and_masks_bare_phone(self):
        from web.services.logs import _mask_log_phones
        self.assertEqual(
            _mask_log_phones("[13800138000] ✅ 签到成功"), "[138****8000] ✅ 签到成功")
        # 中文逗号分隔的裸号此前漏过，展示层必须一并遮住
        self.assertEqual(
            _mask_log_phones("会话缓存作废（跨业务日）: 17851095321，本次真实登录"),
            "会话缓存作废（跨业务日）: 178****5321，本次真实登录")
        self.assertEqual(
            _mask_log_phones("定位 118.88459277562808 dur=118.88459277562808"),
            "定位 118.88459277562808 dur=118.88459277562808")


if __name__ == "__main__":
    unittest.main()
