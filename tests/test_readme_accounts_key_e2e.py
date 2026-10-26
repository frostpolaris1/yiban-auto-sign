# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""README「网页管理后台」的账号密钥步骤必须产出 systemd 能读的 EnvironmentFile。

标签：J · 运维：部署/备份/发布
覆盖：发版 dry-run 的 F1（2026-10-25）与复审 H-1（2026-10-25 第二轮）。
    F1：README 曾从检出根 `.env` 取钥。运行数据迁入 `data/` 后读空，
    `/etc/yiban/accounts-key` 不生成，systemd 启动失败。
    H-1：grep 判据窄于应用解析器。BOM、值内空白、尾随 NBSP 都被应用接受，
    grep 判为坏值后静默换钥，旧密文不可解。修法：README 调应用自己的
    `account_crypto.ensure_accounts_key`，不再自写 grep/sed。
对应实现：`README.md`「网页管理后台」的装单元前准备块；
    `yiban/adapters/config/account_crypto.py` 的 `decode_accounts_key` /
    `ensure_accounts_key`；权威消费方是 `web/deploy/yiban-web.service`。
关键断言：
  ① 静态：README 的取钥来源是 `data/.env`；落点与单元 `EnvironmentFile=` 相同；
     安装参数是 `-m 0640 -o root -g yiban`；取钥走应用解析器，不用 grep/sed。
  ② 活体：该块原文在沙箱里执行，得到 `YIBAN_ACCOUNTS_KEY=<64hex>` 单行、
     mode 0640、属主 root:yiban（非 root 位格退为当前用户/组）；与 `data/.env`
     的生效值同钥；重复执行不追加第二行；`data/.env` 收口为 0600。
  ③ 合法写法逐字节保留 `.env`：大写、CRLF+两侧空白、BOM、值内空白、尾随 NBSP、
     同名键重复（后写覆盖先写）。只换 EnvironmentFile 的表示，不换密钥。
     公开示例占位钥必须被替换。多行且生效值非法时拒绝换钥（旧钥不删）。
依赖：POSIX + bash + GNU install（WSL/CI）；不触网、不写真实 /etc。
    沙箱改写 `/etc/yiban` 与 `/tmp/yiban-accounts-key` 两个落点——真实 `/etc` 可能
    存着生产密钥，测试不得读写它。
"""
import ast
import grp
import inspect
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from yiban.adapters.config import account_crypto

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = os.path.join(BASE, "README.md")
SERVICE_UNIT = os.path.join(BASE, "web", "deploy", "yiban-web.service")

#: 准备块的起止行：两者都在原文里独一无二。标题/段落被改写时抽不到 ⇒ 本文件判红
#: （抽取式自证，避免"正则静默匹配空串"的假绿）。
BLOCK_START = "useradd -r -s /usr/sbin/nologin yiban"
BLOCK_END = "&& rm -f /tmp/yiban-accounts-key"

#: 应用侧 `bytes.fromhex` 不分大小写：大写十六进制同样是合法密钥。
KEY_LINE = re.compile(r"^YIBAN_ACCOUNTS_KEY=([0-9a-fA-F]{64})$")
ENV_LINE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
ABSOLUTE_PATH = re.compile(
    r"(?<![A-Za-z0-9_./-])/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+")

#: README 块里允许出现的绝对路径前缀。e2e 只改写后两个；useradd 行在沙箱里跳过。
#: 新增前缀必须同时加进 `_run_block` 的改写集合，否则沙箱会写到真实系统路径。
KNOWN_ABSOLUTE_PREFIXES = (
    "/usr/sbin/nologin",
    "/etc/yiban",
    "/tmp/yiban-accounts-key",
)

#: 两把合法且非公开示例的 32 字节钥（hex）。PUBLISHED_EXAMPLE 是仓库历史模板钥。
KEY_A = "1f2e3d4c5b6a7988" * 4
KEY_B = "9e8d7c6b5a493827" * 4
PUBLISHED_EXAMPLE = "0123456789abcdef" * 4


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def _env_bytes(lines, newline="\n", bom=False):
    """按指定行尾/可选 BOM 生成 `.env` 原始字节。"""
    data = (newline.join(lines) + newline).encode("utf-8")
    return (b"\xef\xbb\xbf" + data) if bom else data


def _block_lines(text):
    """抽出 README 准备块的 shell 行（去掉每行一个 `#` 注释前缀）。

    抽不到起止行、或块内出现非注释行 ⇒ 返回空列表，调用方判红。
    """
    lines = text.splitlines()
    # 起止行必须落在**注释行**里：同一串命令在上面的「服务器部署」节也出现一次
    # （未注释），只按子串找会从那里开始、把整篇 README 都卷进块里。
    start = next((i for i, line in enumerate(lines)
                  if line.startswith("#") and BLOCK_START in line), None)
    if start is None:
        return []
    end = next((j for j in range(start, len(lines))
                if lines[j].startswith("#") and BLOCK_END in lines[j]), None)
    if end is None:
        return []
    out = []
    for line in lines[start:end + 1]:
        if not line.startswith("#"):
            return []
        out.append(line[1:])
    return out


def _install_target(block_text):
    m = re.search(r"install -m 0640 -o root -g yiban [^\s]+ (\S+)", block_text)
    return m.group(1) if m else None


def _unit_environment_file():
    m = re.search(r"^EnvironmentFile=(\S+)$", _read(SERVICE_UNIT), re.M)
    return m.group(1) if m else None


class ReadmeAccountsKeyBlockTest(unittest.TestCase):
    """静态判据：来源、机制、目标、安装参数都只有一个定义点。"""

    def setUp(self):
        self.text = _read(README)
        self.block = _block_lines(self.text)
        self.block_text = "\n".join(self.block)

    def test_block_is_extractable(self):
        self.assertTrue(self.block, "README 的准备块抽不到（起止行被改名？）")
        self.assertIn("YIBAN_ACCOUNTS_KEY", self.block_text,
                      "抽到的块里没有账号密钥步骤，抽取口径已失效")

    def test_key_source_is_runtime_data_env(self):
        """取钥来源必须是 data/.env；退回检出根 `.env` 即红（F1 的原始缺陷）。"""
        self.assertIn("account_crypto.ensure_accounts_key", self.block_text,
                      "README 没有调用应用自己的取钥入口（H-1）")
        self.assertIn("ensure_accounts_key(sys.argv[1])", self.block_text,
                      "取钥入口的入参不是 data/.env")
        self.assertIn("data/.env", self.block_text,
                      "README 没有从 data/.env 取账号密钥")
        self.assertNotRegex(
            self.block_text, r"grep\s+-[a-zA-Z]*q[a-zA-Z]*E?\s+'?\^?YIBAN_ACCOUNTS_KEY",
            "README 又用 grep 判密钥格式了——判据会再次窄于应用解析器（H-1）")
        self.assertNotIn("cat \"$_env_tmp\" > data/.env", self.block_text,
                         "README 又用非原子覆盖写 .env 了（中断会留空文件）")
        self.assertIn("chmod 600 data/.env", self.block_text,
                      "读钥块没有把 data/.env 收口为 0600（tee 默认建出 0644）")
        self.assertNotRegex(
            self.block_text, r"grep\s+'?\^YIBAN_ACCOUNTS_KEY='?\s+\.env\b",
            "README 又去读检出根 .env 了——该文件在现行布局下不存在，"
            "/etc/yiban/accounts-key 会写不出来",
        )

    def test_install_target_matches_systemd_environment_file(self):
        target = _install_target(self.block_text)
        self.assertEqual(target, "/etc/yiban/accounts-key",
                         "README 的落点与单元约定不一致")
        self.assertEqual(_unit_environment_file(), target,
                         "README 的落点与 yiban-web.service 的 EnvironmentFile 不是同一路径")

    def test_install_mode_and_owner(self):
        self.assertIn("install -m 0640 -o root -g yiban", self.block_text,
                      "EnvironmentFile 必须以 root:yiban 0640 落位（组可读、他人不可读）")

    def test_block_absolute_paths_are_in_known_rewrite_set(self):
        """块内绝对路径必须都在沙箱改写集合内，防止 e2e 写到真实系统路径。"""
        found = set(ABSOLUTE_PATH.findall(self.block_text))
        self.assertTrue(found, "抽取块里没有绝对路径，解析口径失效")
        unknown = [p for p in found
                   if not any(p == k or p.startswith(k + "/")
                              for k in KNOWN_ABSOLUTE_PREFIXES)]
        self.assertEqual(unknown, [],
                         "README 准备块新增绝对路径，e2e 沙箱未覆盖改写：%s"
                         % sorted(unknown))

    def test_rotation_snippet_points_at_runtime_data_env(self):
        """同族第二处：换钥脚本的 YIBAN_ENV_FILE 也必须指向 data/.env。"""
        self.assertIn("YIBAN_ENV_FILE=data/.env", self.text,
                      "换钥脚本没指向 data/.env（旧值 .env 在现行布局下不存在）")
        self.assertNotRegex(self.text, r"YIBAN_ENV_FILE=\.env(?:\s|\\|$)",
                            "README 仍有片段把 YIBAN_ENV_FILE 指向检出根 .env")


class AccountsKeyApiShapeTest(unittest.TestCase):
    """H-1 的形状守卫：部署取钥必须复用应用解析器与原子写入口。"""

    def test_ensure_helper_uses_app_parser_and_atomic_writer(self):
        src = inspect.getsource(account_crypto.ensure_accounts_key)
        self.assertIn("env_io.parse_env_file", src,
                      "部署取钥没有用应用解析器读 .env")
        self.assertIn("decode_accounts_key", src,
                      "部署取钥没有用公开校验函数")
        self.assertIn("env_io.write_env_key", src,
                      "部署取钥没有用原子写入口（临时件 + os.replace）")
        self.assertNotIn("fromhex", src,
                         "部署取钥自写 hex 解析——判据会再次窄于应用语义")

    def test_stat_and_owner_restore_are_inside_env_lock(self):
        """属主快照与恢复都必须在 env_lock 内（P2：锁外窗口可被并发换主）。"""
        tree = ast.parse(inspect.getsource(account_crypto.ensure_accounts_key))
        func = tree.body[0]
        withs = [n for n in ast.walk(func)
                 if isinstance(n, ast.With)
                 and "env_write_lock" in ast.unparse(n.items[0].context_expr)]
        self.assertEqual(len(withs), 1, "取钥没有唯一一处 env_write_lock 临界区")
        body = withs[0]
        inside = {id(n) for n in ast.walk(body)}
        calls = [ast.unparse(n.func) for n in ast.walk(body)
                 if isinstance(n, ast.Call)]
        self.assertIn("os.stat", calls, "属主快照在锁外（并发写者可换主）")
        self.assertIn("_preserve_env_owner", calls, "属主恢复在锁外（并发写者可换主）")
        outside = [ast.unparse(n.func) for n in ast.walk(func)
                   if isinstance(n, ast.Call) and id(n) not in inside
                   and ast.unparse(n.func) in ("os.stat", "_preserve_env_owner")]
        self.assertEqual(outside, [], "属主快照/恢复又出现锁外调用：%s" % outside)


class ReadmeAccountsKeyE2ETest(unittest.TestCase):
    """活体判据：照 README 的块执行后，EnvironmentFile 真的可读、值一致。"""

    def _require_posix_bash(self):
        if os.name != "posix" or not shutil.which("bash") or not shutil.which("install"):
            self.skipTest("需要 POSIX + bash + GNU install（Windows 开发机跳过）")

    def _make_sandbox(self, env_bytes):
        tmp = tempfile.mkdtemp(prefix="yiban-readme-key-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        project = os.path.join(tmp, "opt", "yiban-auto-sign")
        data = os.path.join(project, "data")
        etcdir = os.path.join(tmp, "etc", "yiban")
        tmpside = os.path.join(tmp, "tmp")
        os.makedirs(data)
        # `/etc/yiban` 刻意不预建：README 的 `install -d` 自己建（R-9）。
        # 删掉那行，下面的 `install -m 0640` 会失败 ⇒ e2e 判红。
        os.makedirs(tmpside)
        # README 的取钥命令直接写 `.venv/bin/python3`：沙箱把**整个 venv 目录**
        # 软链进来。只软链单个解释器会让 Python 丢掉 venv 前缀（找不到项目依赖）。
        os.symlink(os.path.dirname(os.path.dirname(sys.executable)),
                   os.path.join(project, ".venv"))
        # README 的取钥命令在检出根执行：应用代码按 cwd 导入。沙箱把仓库的
        # `yiban/` 软链进来，等价于真实部署的"代码就在检出根"。
        os.symlink(os.path.join(BASE, "yiban"), os.path.join(project, "yiban"))
        env_file = os.path.join(data, ".env")
        with open(env_file, "wb") as f:
            f.write(env_bytes)
        # README 第 4 步 `sudo -u yiban tee` 在默认 umask 下建出 0644。
        # 读钥块必须把 data/.env 收口到 0600（R-5）。
        os.chmod(env_file, 0o644)
        return tmp, project, etcdir, os.path.join(tmpside, "yiban-accounts-key")

    def _owner_spec(self):
        """期望属主：(uid, gid, verbatim)。

        root 且宿主有 yiban 组时按 README 原文执行（root:yiban）；
        其它位格退为当前用户/组——`install -o 自己的 uid -g 自己的组` 在非特权下合法，
        仍然校验 0640 与内容。位格降级只影响"属主名字"，不影响被测的读权限形状。
        """
        if os.geteuid() == 0:
            try:
                gid = grp.getgrnam("yiban").gr_gid
            except KeyError:
                gid = None
            if gid is not None:
                return 0, gid, True
        return os.getuid(), os.getgid(), False

    def _run_block(self, project, etcdir, tmp_key, uid, gid, verbatim):
        block = _block_lines(_read(README))
        self.assertTrue(block, "README 的准备块抽不到")
        out = []
        for line in block:
            if BLOCK_START in line:
                continue  # 沙箱不建系统账号：属主位格已由 _owner_spec 决定
            line = line.replace("/etc/yiban", etcdir)
            line = line.replace("/tmp/yiban-accounts-key", tmp_key)
            if not verbatim:
                line = line.replace("-o yiban -g yiban", "-o %d -g %d" % (uid, gid))
                line = line.replace("-o root -g yiban", "-o %d -g %d" % (uid, gid))
            out.append(line)
        script = "set -euo pipefail\ncd %s\n%s\n" % (
            shlex.quote(project), "\n".join(out))
        return subprocess.run(["bash", "-c", script], capture_output=True,
                              text=True, timeout=120)

    def _read_key_lines(self, env_file):
        """data/.env 的密钥行（文本、去两侧空白）。utf-8-sig 与应用解析器同口径。"""
        with open(env_file, encoding="utf-8-sig") as f:
            return [line.strip() for line in f.read().splitlines()
                    if line.strip().startswith("YIBAN_ACCOUNTS_KEY=")]

    def _assert_environment_file(self, keyfile, uid, gid, env_file):
        self.assertTrue(os.path.isfile(keyfile), "README 走完后没有生成 accounts-key")
        st = os.stat(keyfile)
        self.assertTrue(stat.S_ISREG(st.st_mode), "EnvironmentFile 不是常规文件")
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o640,
                         "EnvironmentFile 权限不是 0640（组不可读或他人可读）")
        self.assertEqual(st.st_uid, uid, "EnvironmentFile 属主不是 root")
        self.assertEqual(st.st_gid, gid, "EnvironmentFile 属组不是 yiban")
        text = _read(keyfile)
        lines = text.splitlines()
        self.assertEqual(len(lines), 1,
                         "EnvironmentFile 只许放 YIBAN_ACCOUNTS_KEY 一行：%r" % lines)
        self.assertIsNotNone(KEY_LINE.match(lines[0]),
                             "EnvironmentFile 内容不是 64 位 hex 的账号密钥：%r" % lines)
        self.assertIsNotNone(ENV_LINE.match(lines[0]),
                             "EnvironmentFile 不是 KEY=VALUE 形状：%r" % lines[0])
        self.assertNotIn(" ", lines[0],
                         "EnvironmentFile 行内含空格，systemd 解析会与预期不同")
        self.assertTrue(os.access(keyfile, os.R_OK), "当前进程读不到 EnvironmentFile")
        env_lines = self._read_key_lines(env_file)
        self.assertTrue(env_lines, "data/.env 里没有账号密钥行")
        # 解析口径与应用一致：同名键后写覆盖先写，取最后一行。
        self.assertEqual(bytes.fromhex(lines[0].split("=", 1)[1]),
                         bytes.fromhex(env_lines[-1].split("=", 1)[1]),
                         "EnvironmentFile 与 data/.env 生效密钥不一致（来源分叉）")
        self.assertEqual(stat.S_IMODE(os.stat(env_file).st_mode), 0o600,
                         "data/.env 没有收口为 0600（第 4 步 tee 默认建出 0644）")
        return lines[0]

    def _run_and_assert(self, env_bytes, expected_key_hex=None, unchanged=False):
        """执行 README 块并断言 EnvironmentFile 与 .env 形状。

        expected_key_hex=None 表示"生成新钥"路径，只校验形状。
        unchanged=True 时额外断言 `.env` 逐字节不变（合法写法不得被改写）。
        返回 (EnvironmentFile 行, project 根)。
        """
        _tmp, project, etcdir, tmp_key = self._make_sandbox(env_bytes)
        env_file = os.path.join(project, "data", ".env")
        uid, gid, verbatim = self._owner_spec()
        r = self._run_block(project, etcdir, tmp_key, uid, gid, verbatim)
        self.assertEqual(r.returncode, 0,
                         "README 准备块执行失败：\n%s\n%s" % (r.stdout, r.stderr))
        line = self._assert_environment_file(
            os.path.join(etcdir, "accounts-key"), uid, gid, env_file)
        if expected_key_hex is not None:
            self.assertEqual(bytes.fromhex(line.split("=", 1)[1]),
                             bytes.fromhex(expected_key_hex),
                             "EnvironmentFile 里的密钥不是预期值：%r" % line)
        if unchanged:
            self.assertEqual(_read_bytes(env_file), env_bytes,
                             "data/.env 被改写：合法写法必须逐字节保留（换钥=数据丢失）")
        return line, project

    def test_missing_key_is_generated_once(self):
        """首次部署：补一把随机钥；第二次执行不改写 .env、不换钥。"""
        _tmp, project, etcdir, tmp_key = self._make_sandbox(
            _env_bytes(["YIBAN_SECRET_KEY=readme-e2e-secret"]))
        env_file = os.path.join(project, "data", ".env")
        uid, gid, verbatim = self._owner_spec()
        r = self._run_block(project, etcdir, tmp_key, uid, gid, verbatim)
        self.assertEqual(r.returncode, 0,
                         "README 准备块执行失败：\n%s\n%s" % (r.stdout, r.stderr))
        first = self._assert_environment_file(
            os.path.join(etcdir, "accounts-key"), uid, gid, env_file)
        snapshot = _read_bytes(env_file)

        r = self._run_block(project, etcdir, tmp_key, uid, gid, verbatim)
        self.assertEqual(r.returncode, 0,
                         "README 准备块第二次执行失败：\n%s\n%s" % (r.stdout, r.stderr))
        second = self._assert_environment_file(
            os.path.join(etcdir, "accounts-key"), uid, gid, env_file)
        self.assertEqual(first, second, "重复执行改了密钥（幂等性被破坏）")
        self.assertEqual(_read_bytes(env_file), snapshot,
                         "第二次执行改写了 data/.env（合法密钥不应触发写回）")

    def test_uses_existing_key_from_runtime_data_env(self):
        """已有部署：data/.env 里那把钥必须原样沿用，不得另生成一把。"""
        env_bytes = _env_bytes(["YIBAN_SECRET_KEY=readme-e2e-secret",
                                "YIBAN_ACCOUNTS_KEY=" + KEY_A])
        line, _project = self._run_and_assert(env_bytes, KEY_A, unchanged=True)
        self.assertEqual(line, "YIBAN_ACCOUNTS_KEY=" + KEY_A)

    def test_uppercase_hex_key_is_kept(self):
        """大写十六进制合法（fromhex 不分大小写）；.env 必须逐字节保留。"""
        upper = KEY_A.upper()
        self._run_and_assert(_env_bytes(["YIBAN_ACCOUNTS_KEY=" + upper]),
                             upper, unchanged=True)

    def test_crlf_and_padded_key_is_kept(self):
        """CRLF 与两侧空白合法（解析器逐行 strip）；.env 必须逐字节保留。"""
        env_bytes = _env_bytes(["YIBAN_SECRET_KEY=s",
                                "  YIBAN_ACCOUNTS_KEY=" + KEY_A + "  "],
                               newline="\r\n")
        self._run_and_assert(env_bytes, KEY_A, unchanged=True)

    def test_bom_key_is_kept(self):
        """UTF-8 BOM 合法（utf-8-sig）；.env 必须逐字节保留。"""
        # 密钥行放在**第一行**：BOM 就在它前面，行首正则判据会读成坏键。
        env_bytes = _env_bytes(["YIBAN_ACCOUNTS_KEY=" + KEY_A,
                                "YIBAN_SECRET_KEY=s"], bom=True)
        self._run_and_assert(env_bytes, KEY_A, unchanged=True)

    def test_internal_whitespace_key_is_kept(self):
        """值内字节对之间的空白合法（fromhex 忽略）；.env 必须逐字节保留。"""
        spaced = " ".join(KEY_A[i:i + 2] for i in range(0, 64, 2))
        env_bytes = _env_bytes(["YIBAN_ACCOUNTS_KEY=" + spaced])
        self._run_and_assert(env_bytes, KEY_A, unchanged=True)

    def test_trailing_nbsp_key_is_kept(self):
        """尾随 NBSP 合法（str.strip 去 Unicode 空白）；.env 必须逐字节保留。"""
        env_bytes = _env_bytes(["YIBAN_ACCOUNTS_KEY=" + KEY_A + "\u00a0"])
        self._run_and_assert(env_bytes, KEY_A, unchanged=True)

    def test_duplicate_key_lines_use_last_value(self):
        """同名键重复：应用解析器后写覆盖先写，EnvironmentFile 用最后一行。"""
        env_bytes = _env_bytes(["YIBAN_ACCOUNTS_KEY=" + KEY_A,
                                "YIBAN_ACCOUNTS_KEY=" + KEY_B])
        self._run_and_assert(env_bytes, KEY_B, unchanged=True)

    def test_duplicate_with_invalid_last_refuses_rotation(self):
        """多行且生效值非法 ⇒ 拒绝换钥：旧钥不删，.env 逐字节不变。"""
        env_bytes = _env_bytes(["YIBAN_ACCOUNTS_KEY=" + KEY_A,
                                "YIBAN_ACCOUNTS_KEY=not-a-key"])
        _tmp, project, etcdir, tmp_key = self._make_sandbox(env_bytes)
        env_file = os.path.join(project, "data", ".env")
        uid, gid, verbatim = self._owner_spec()
        r = self._run_block(project, etcdir, tmp_key, uid, gid, verbatim)
        self.assertNotEqual(r.returncode, 0,
                            "多行且末行非法时块必须失败，不许自动换钥")
        self.assertEqual(_read_bytes(env_file), env_bytes,
                         "拒绝换钥时 .env 被改写（旧钥被删）")
        keyfile = os.path.join(etcdir, "accounts-key")
        self.assertFalse(os.path.exists(keyfile),
                         "拒绝换钥时不应生成 EnvironmentFile")

    def test_published_example_key_is_replaced(self):
        """仓库公开示例钥必须被替换（旧钥等同明文）。"""
        env_bytes = _env_bytes(["YIBAN_ACCOUNTS_KEY=" + PUBLISHED_EXAMPLE])
        line, project = self._run_and_assert(env_bytes, unchanged=False)
        new_key = bytes.fromhex(line.split("=", 1)[1])
        self.assertNotEqual(new_key, bytes.fromhex(PUBLISHED_EXAMPLE),
                            "公开示例钥没有被替换")
        env_file = os.path.join(project, "data", ".env")
        data = _read_bytes(env_file)
        self.assertNotIn(PUBLISHED_EXAMPLE.encode(), data,
                         "data/.env 里仍留着公开示例钥")
        self.assertEqual(data.count(b"YIBAN_ACCOUNTS_KEY="), 1,
                         "替换后 data/.env 只许有一条密钥行")

    def test_replaces_empty_key_line(self):
        """空值行按缺键处理：生成新钥，EnvironmentFile 不得混入空行。"""
        env_bytes = _env_bytes(["YIBAN_SECRET_KEY=readme-e2e-secret",
                                "YIBAN_ACCOUNTS_KEY="])
        line, _project = self._run_and_assert(env_bytes)
        self.assertIsNotNone(KEY_LINE.match(line), "空值行没有被替换成合法密钥")

    def test_generated_key_preserves_env_file_owner(self):
        """原子替换会把 .env 换成写入者属主；生成路径必须恢复原属主。

        服务账号必须能读写 .env。不恢复 ⇒ 服务读不到密钥，启动失败。
        root 下把 .env 先改成非当前属主，验证恢复路径真的执行；
        非 root 下只能验证"属主不变"（无法造别的属主），不跳过。
        """
        self._require_posix_bash()
        _tmp, project, etcdir, tmp_key = self._make_sandbox(
            _env_bytes(["YIBAN_ACCOUNTS_KEY="]))
        env_file = os.path.join(project, "data", ".env")
        if hasattr(os, "chown") and os.geteuid() == 0:
            os.chown(env_file, 12345, 12345)
        before = os.stat(env_file)
        uid, gid, verbatim = self._owner_spec()
        r = self._run_block(project, etcdir, tmp_key, uid, gid, verbatim)
        self.assertEqual(r.returncode, 0,
                         "README 准备块执行失败：\n%s\n%s" % (r.stdout, r.stderr))
        after = os.stat(env_file)
        self.assertEqual((after.st_uid, after.st_gid),
                         (before.st_uid, before.st_gid),
                         "生成密钥后 .env 属主变了（服务账号将读不到）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
