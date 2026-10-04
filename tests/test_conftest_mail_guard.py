# -*- coding: utf-8 -*-
"""B0（ba-p13-04）：测试进程的「禁发邮件」保险必须真的合上。

保险的旧形态只在进程环境里钉 `YIBAN_MAIL_ENABLE=0`（conftest）。但真值口径是
「`.env` 文件优先、进程环境只补缺」（`mail.config._get`，M27 全仓口径）。而
`env_io.env_path()` 在 `YIBAN_ENV_FILE` 未设时回落到 cwd 的 `.env`——裸机 cron /
开发机上就是工程 `.env`。若该文件写了 `YIBAN_MAIL_ENABLE=1` 且有可用 SMTP 条目，
`is_enabled()` 仍为真，signin/web 测试会向真实收件人发信：这道保险形同虚设。

本文件钉住修法：conftest 会话伊始把 `YIBAN_ENV_FILE` 指向测试专用 fixture，
使 `_read_env_file()` / `env_path()` 在测试期读不到工程 `.env`。
只断言「门」的状态：不连 SMTP、不发信。
删掉 conftest 的这行注入，本文件必红（变异已验证，见 out/b0/03-conftest-report.md）。
"""
import os
import tempfile

from yiban.infra import env_io
from yiban.mail import config as mail_config

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ENV = os.path.join(_REPO, ".env")


def test_env_locator_does_not_resolve_to_the_repo_env():
    """`env_path()` 不得解析到仓库根 `.env`。

    这是保险成立的前提。conftest 未设 `YIBAN_ENV_FILE` 时，`env_path()` 回落 cwd
    的 `.env`（即仓库根），本断言在**任何** cwd 下都变红（不缺 `.env` 也一样，因为
    `.env` 优先，进程环境的 `YIBAN_MAIL_ENABLE=0` 顶不住文件里的 `=1`）。
    """
    resolved = os.path.abspath(env_io.env_path())
    assert resolved != os.path.abspath(_REPO_ENV), (
        "测试进程会读仓库根 .env：conftest 必须显式设 YIBAN_ENV_FILE 指向 fixture")


def test_env_locator_points_into_a_temp_fixture():
    """保险的另一半：`YIBAN_ENV_FILE` 落在系统临时目录内，不是工程路径。"""
    resolved = os.path.abspath(env_io.env_path())
    tmp_root = os.path.abspath(tempfile.gettempdir())
    assert resolved.startswith(tmp_root + os.sep), (
        "YIBAN_ENV_FILE fixture 必须在系统临时目录内：%s" % resolved)


def test_mail_reader_sees_no_repo_env_key():
    """邮件读侧读到的键集合不得与仓库根 `.env` 的键相交。

    只报键名集合，绝不回带任何值（值可能含 SMTP 凭据）。
    """
    repo_keys = set(env_io.parse_env_file(_REPO_ENV))
    leaked = repo_keys & set(mail_config._read_env_file())
    assert not leaked, "测试进程从仓库根 .env 读到了键：%s" % sorted(leaked)


def test_mail_switch_is_off_in_tests():
    """在该环境下邮件开关被判为关闭：保险真的合上。"""
    assert mail_config.is_enabled() is False
    assert mail_config.smtp_channel_state()[0] == "off"
