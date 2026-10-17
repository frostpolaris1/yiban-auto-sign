# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""README「服务器部署（分步详解）」必须自足：该节先建服务账号，再让人跑 install.sh。

现象（本批 F3）：按 README **全文**读，发布判据（全新干净环境从零装成、一次完成、
零手动调整）成立；但**只按「服务器部署（分步详解）」那一节**读就不成立——该节 1~7 步
都不建 `yiban` 账号，而 `install.sh` 会以 `install: invalid user 'yiban'` 退 1
（它要 `install -d -o yiban` 建 `data/`）。账号创建原先只写在**后面**的
「网页管理后台」小节的 systemd 单元块里。用户判据原文：「全新干净环境按 README
从零安装部署，一次完成，零手动调整」。

钉住的不变式（任何时刻都不该被违反）：
  ① 该节里第一次出现"跑 install.sh"之前，必须先给出创建服务账号的命令；
  ② 该节里第一次用 `sudo -u <服务账号>`（第 4 步写 `.env`）也不得早于账号创建；
  ③ 该节建的账号名 = `web/deploy/yiban-web.service` 的 `User=`（同一事实不两处定义）。

对应实现：`README.md` 的「服务器部署（分步详解）」节；账号权威件是
`web/deploy/yiban-web.service` 的 `User=`/`Group=`。
关键断言：段落抽取自证非空（标题改名即红）＋ 三处位置先后关系。
依赖：只读 README 与单元文件；不起子进程、不触网。
"""
import io
import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = os.path.join(BASE, "README.md")
SERVICE_UNIT = os.path.join(BASE, "web", "deploy", "yiban-web.service")

SECTION_HEADING = "## 服务器部署（分步详解）"


def _read(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def _service_account():
    """服务账号 (User, Group)：唯一事实源 = web/deploy/yiban-web.service。"""
    user = group = None
    for line in _read(SERVICE_UNIT).splitlines():
        if line.startswith("User="):
            user = line.split("=", 1)[1].strip()
        elif line.startswith("Group="):
            group = line.split("=", 1)[1].strip()
    return user, group


def _deploy_section(text):
    """README 的「服务器部署（分步详解）」节（到下一个二级标题为止）。"""
    m = re.search(re.escape(SECTION_HEADING) + r".*?(?=^## )", text, re.M | re.S)
    return m.group(0) if m else ""


class DeploySectionSelfSufficiencyTest(unittest.TestCase):
    """分步详解节必须自带"先建服务账号"这一步。"""

    def setUp(self):
        self.text = _read(README)
        self.section = _deploy_section(self.text)
        self.user, _group = _service_account()

    def test_section_is_extractable_and_mentions_the_installer(self):
        """解析式先自证：段抽取非空且真的提 install.sh，否则下面的"先后"没有意义。"""
        self.assertTrue(self.section,
                        "README 的「服务器部署（分步详解）」节抽不到（标题被改名？）")
        self.assertRegex(self.section, r"install\.sh",
                         "该节不再提 install.sh，本门失去意义")

    def test_section_creates_service_account_before_running_installer(self):
        """该节必须先给创建服务账号的命令，再让人跑 install.sh。"""
        self.assertTrue(self.user, "yiban-web.service 缺 User=")
        create = re.search(r"useradd[^\n]*\b%s\b" % re.escape(self.user), self.section)
        self.assertIsNotNone(
            create,
            "「服务器部署（分步详解）」节没给出创建服务账号 %s 的命令：只按该节部署时，"
            "install.sh 会以 `install: invalid user '%s'` 退 1（该节因此不自足）"
            % (self.user, self.user))
        first_install = re.search(r"install\.sh", self.section)
        self.assertLess(
            create.start(), first_install.start(),
            "该节在提 install.sh 之后才建账号——顺序反了，只按该节走必然中断")

    def test_sudo_u_usage_is_not_before_account_creation(self):
        """第 4 步的 `sudo -u <服务账号>` 同样不得早于账号创建。"""
        create = re.search(r"useradd[^\n]*\b%s\b" % re.escape(self.user), self.section)
        self.assertIsNotNone(create, "该节没给出创建服务账号的命令")
        as_user = re.search(r"sudo -u %s\b" % re.escape(self.user), self.section)
        if as_user is not None:
            self.assertLess(create.start(), as_user.start(),
                            "该节先用 `sudo -u %s` 再建账号——只按该节走会报 user not found"
                            % self.user)


if __name__ == "__main__":
    unittest.main(verbosity=2)
