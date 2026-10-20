# -*- coding: utf-8 -*-
"""脱敏重复实现收口（工单 g88j）：单源 `mask_tree_strings` 与两处副本真跑对拍。

标签：K · 脱敏/隐私
覆盖：
- `yiban/core/masking.py::mask_tree_strings`（单源），以及两处历史副本
  `yiban/engine/cli_support.py::_masked_tree`（stdout JSON）与
  `yiban/adapters/notifier/mail/layout.py::_masked_tree`（邮件内容树）在**共同形状**上
  逐枚等价；
- `yiban/adapters/persistence/accounts.py::_mask_phone_display` 必须**转发**
  `masking.mask_phone`（不得再长出一份切片公式）；
- `yiban/adapters/notifier/mail/config.py::_mask_addr` 与 `mask_email` 是**两张公式**
  （裁决：不合并），同输入必须给出不同结果——防止后人"顺手统一"改变邮件收件人的
  用户可见形态。
关键断言：手机号/邮箱不得出明文（红线）；副本与单源任一处改动都会红。
依赖：纯本地导入，不触网、不建库。
"""
import os
import sys
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from yiban.core import masking  # noqa: E402

PHONE = "13800138000"
MASKED = "138****8000"
EMAIL = "someone@example.com"


def _canon(node):
    """容器类型归一（tuple → list）：本对拍只比较**遮罩结果**，不比较容器种类。

    CLI 副本把 tuple 规整成 list（JSON 没有 tuple），邮件副本保留 tuple——两者都是
    合法实现，差异不在脱敏口径上。
    """
    if isinstance(node, dict):
        return {k: _canon(v) for k, v in node.items()}
    if isinstance(node, (list, tuple)):
        return [_canon(v) for v in node]
    return node


class TreeMaskParityTest(unittest.TestCase):
    """两处副本与单源在共同形状上必须逐枚等价。"""

    @classmethod
    def setUpClass(cls):
        from yiban.adapters.notifier.mail import layout as mail_layout
        from yiban.engine import cli_support
        cls.cli = cli_support
        cls.mail = mail_layout

    #: 共同形状（mail 树不含 dict，故 dict 只对 CLI 校验）
    SHAPES = (
        f"手机号 {PHONE} 请勿外传",
        ["a", f"tel:{PHONE}", 20261020],
        ("x", [f"y {PHONE}"], 7),
        None,
        42,
        3.5,
        True,
    )

    def test_cli_copy_matches_single_source(self):
        for shape in self.SHAPES:
            with self.subTest(shape=shape):
                self.assertEqual(_canon(self.cli._masked_tree(shape)),
                                 _canon(masking.mask_tree_strings(shape)))

    def test_mail_copy_matches_single_source(self):
        for shape in self.SHAPES:
            with self.subTest(shape=shape):
                self.assertEqual(_canon(self.mail._masked_tree(shape)),
                                 _canon(masking.mask_tree_strings(shape)))

    def test_cli_dict_shape_matches_single_source(self):
        shape = {"phone": PHONE, "count": 20261020, "nested": [f"{PHONE}"]}
        self.assertEqual(_canon(self.cli._masked_tree(shape)),
                         _canon(masking.mask_tree_strings(shape)))

    def test_no_plaintext_phone_survives_in_string_leaves(self):
        def _strings(node):
            if isinstance(node, str):
                yield node
            elif isinstance(node, dict):
                for v in node.values():
                    yield from _strings(v)
            elif isinstance(node, (list, tuple)):
                for v in node:
                    yield from _strings(v)

        for shape in self.SHAPES:
            with self.subTest(shape=shape):
                for s in _strings(masking.mask_tree_strings(shape)):
                    self.assertNotIn(PHONE, s)

    def test_integer_leaves_are_not_rewritten(self):
        """11 位整数不是手机号：改写会产出非法 JSON（单源必须只动 str 叶子）。"""
        self.assertEqual(masking.mask_tree_strings({"n": 13800138000}),
                         {"n": 13800138000})


class PhoneDisplayForwardingTest(unittest.TestCase):
    """`_mask_phone_display` 必须转发 `masking.mask_phone`，不是第二份公式。"""

    def test_forwards_canonical_masker(self):
        from yiban.adapters.persistence import accounts as accounts_store
        for raw in (PHONE, "12012345678", "not-a-phone", MASKED):
            with self.subTest(raw=raw):
                self.assertEqual(accounts_store._mask_phone_display(raw),
                                 masking.mask_phone(raw))


class MaskAddrVsMaskEmailTest(unittest.TestCase):
    """`_mask_addr`（邮件收件人）与 `mask_email`（展示/审计）刻意是两张公式。"""

    def test_rules_differ_and_both_hide_plaintext(self):
        from yiban.adapters.notifier.mail import config as mail_config
        a = mail_config._mask_addr(EMAIL)
        b = masking.mask_email(EMAIL)
        self.assertNotEqual(a, b, "两张公式被误合并：会改变其中一面的用户可见输出")
        self.assertNotIn("someone", a)
        self.assertNotIn("someone", b)
        for value in (a, b):
            self.assertIn("@example.com", value, "域名保留（收件人可辨识）")

    def test_mask_addr_masks_every_address_in_comma_list(self):
        """逗号列表逐项打码：第 2 个及之后的地址不得原样留在返回值里。"""
        from yiban.adapters.notifier.mail import config as mail_config
        out = mail_config._mask_addr("first@example.com, second@example.com")
        self.assertNotIn("first@", out)
        self.assertNotIn("second@", out)
        self.assertEqual(out.count("@example.com"), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
