# -*- coding: utf-8 -*-
r"""`export KEY=value` 行在三个 .env 读取方的解析分叉——双向钉死收敛不变量。

标签：J · 运维：部署/备份/发布 · G · 安全：脱敏/审计/配置注入
覆盖：`export KEY=value` 行在 env_io / child_env / shell 三读取方的收敛不变量——
    正向（真实键三处同值可读）与反向（export 键三处不可读、env_io 侧挂畸形键、
    其余整行丢弃）；不覆盖：行内 value 的逐字一致性（由 tests/test_runsh_env_parse.py
    的文本断言钉住）。
背景：`.env` 有三个独立读取方，各用不同行模型（`yiban.infra.env_io.parse_env_file`
    读侧宽松、`scripts/child_env.parse_env_file` YIBAN_ 前缀+合法键名双白名单、
    `run.sh`/`run_probe.sh` 的 shell 键名正则判据）。`export KEY=value` 这种从 shell rc
    借来的写法，在"KEY 是否成为生效配置"上三者必须给出同一个"否"，但**分叉在形态**：
    env_io 把它挂进畸形键 `"export KEY"`（含空格，不匹配 `get("KEY")`），child_env 与
    shell 整行丢弃。剥前缀"归一"会把历史一向未生效的行扶正成生效配置（行内若藏
    `GLOBAL_PAUSE`/`ADMIN_PASSWORD_HASH` 即实体化提权载荷），撞"键语义不变"红线——
    故**保留三态**，只钉两条不变量：
    ①（正向）真实键 `KEY=value` 三处读到同一值；
    ②（反向）`export KEY=value` 三处都读不到 `KEY`，且不与真实键串台。

对应实现：`yiban/infra/env_io.py`（`parse_env_file` / `key_line_pattern` /
    `env_key_values` / `render_env_write`）、`scripts/child_env.py`、
    `run.sh` / `run_probe.sh` 的加载循环（文本逐字一致性由
    `tests/test_runsh_env_parse.py` 钉住，本文件钉行为面收敛）。
关键断言：反向不止断 `get("KEY")` 为空——env_io 侧另钉"export 行确实解析进了 dict
    但挂在畸形键下"（分叉形态本体；将来任何'归一'剥前缀的改动都会红在这里）；
    折叠正则不得吃掉 export 行；shell 侧先断两份脚本的解析段与本测试复刻段逐字
    同源，再以真 bash 跑判据。
依赖：临时 `.env`；child_env 按文件路径加载（与 docker/scheduler.py 运行时同语义）；
    shell 行为用例无 bash 时 skip（同 test_runsh_env_parse 惯例）。不发网络、不起 webapp。
"""
import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest

from yiban.infra import env_io

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASH = shutil.which("bash")

# 真实键：三处都要读到；诱饵键：三处都读不到（取敏感键名，正对"实体化"威胁模型）
PLAIN = "YIBAN_SIGN_MODE"
PLAIN_VAL = "random"
DECOY = "YIBAN_GLOBAL_PAUSE"
DECOY_VAL = "1"


class _EnvFixture(unittest.TestCase):
    def _write(self, text):
        tmp = tempfile.mkdtemp(prefix="yiban-export-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, ".env")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path


class ChildEnvLoaderMixin(unittest.TestCase):
    @staticmethod
    def _load_child_env():
        spec = importlib.util.spec_from_file_location(
            "child_env_divergence", os.path.join(BASE, "scripts", "child_env.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


# ---------------------------------------------------------------------------
# 读取方 1：yiban.infra.env_io（读侧宽松模型，写侧折叠同族）
# ---------------------------------------------------------------------------
class EnvIoPathTest(_EnvFixture):
    def test_real_key_read(self):
        """正向：真实键读到。"""
        path = self._write(f"{PLAIN}={PLAIN_VAL}\n")
        self.assertEqual(env_io.parse_env_file(path).get(PLAIN), PLAIN_VAL)

    def test_export_key_never_becomes_effective(self):
        """反向：export 行读不到诱饵键，但它确实挂在畸形键下（分叉形态本体钉死）。"""
        path = self._write(f"export {DECOY}={DECOY_VAL}\n")
        parsed = env_io.parse_env_file(path)
        self.assertIsNone(parsed.get(DECOY),
                          "export 前缀行绝不能成为诱饵键的生效值")
        self.assertEqual(parsed.get(f"export {DECOY}"), DECOY_VAL,
                         "env_io 把 export 行挂在畸形键 'export KEY' 下——读侧宽松态的"
                         "设计分叉；将来若被'归一'剥前缀扶正，这条必红")

    def test_export_and_real_key_do_not_cross(self):
        """反向：export 与真实键并存互不串台（生效值只认真实键行）。"""
        path = self._write(f"export {DECOY}={DECOY_VAL}\n{PLAIN}={PLAIN_VAL}\n")
        parsed = env_io.parse_env_file(path)
        self.assertEqual(parsed.get(PLAIN), PLAIN_VAL)
        self.assertIsNone(parsed.get(DECOY))

    def test_write_fold_does_not_consume_export_line(self):
        """折叠口径：key_line_pattern 不认 export 行；写真实键不得吃掉它，也扶不正它。"""
        self.assertFalse(env_io.key_line_pattern(PLAIN).match(f"export {PLAIN}=x"),
                         "折叠正则必须不匹配 export 行（否则会把诱饵文本折成真实键）")
        out = env_io.render_env_write(f"export {DECOY}={DECOY_VAL}\n",
                                      {PLAIN: PLAIN_VAL}, delete_empty=False)
        self.assertIn(f"export {DECOY}={DECOY_VAL}", out.splitlines(),
                      "无关的真实键写入不得把 export 行折掉")
        self.assertIn(f"{PLAIN}={PLAIN_VAL}", out)
        kv = env_io.env_key_values(out)
        self.assertIsNone(kv.get(DECOY), "渲染后 export 行依旧不生效")
        self.assertEqual(kv.get(PLAIN), PLAIN_VAL)


# ---------------------------------------------------------------------------
# 读取方 2：scripts/child_env.py（双白名单）
# ---------------------------------------------------------------------------
class ChildEnvPathTest(_EnvFixture, ChildEnvLoaderMixin):
    def test_real_key_read(self):
        child_env = self._load_child_env()
        path = self._write(f"{PLAIN}={PLAIN_VAL}\n")
        self.assertEqual(child_env.parse_env_file(path).get(PLAIN), PLAIN_VAL)

    def test_export_key_dropped_whole(self):
        child_env = self._load_child_env()
        path = self._write(f"export {DECOY}={DECOY_VAL}\n")
        parsed = child_env.parse_env_file(path)
        self.assertEqual(parsed, {},
                         "child_env 整行丢弃 export 行（与 env_io 的畸形键形态刻意不同，"
                         "但收敛一致：键永不生效）")


# ---------------------------------------------------------------------------
# 读取方 3：run.sh / run_probe.sh 的加载循环
# ---------------------------------------------------------------------------
# 与两份脚本逐字同构的判据段（.env 路径走 $1；结果经探针变量打印回传）
_SHELL_GUARD = r'''\
while IFS='=' read -r key value || [ -n "$key$value" ]; do
    value=${value%$'\r'}
    key="${key#$'\xEF\xBB\xBF'}"
    key="${key#"${key%%[![:space:]]*}"}"
    key="${key%"${key##*[![:space:]]}"}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    [ -z "$key" ] && continue
    case "$key" in \#*) continue ;; esac
    if [[ ! "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
        echo "警告: .env 含非法键名，已跳过: $key" >&2
        continue
    fi
    if [[ ! "$key" =~ ^YIBAN_ ]]; then
        echo "警告: .env 含非 YIBAN_ 前缀键，已跳过导出: $key" >&2
        continue
    fi
    export "$key=$value"
done < "$1"
'''


class ShellPathTest(_EnvFixture):
    def test_both_scripts_carry_the_keyname_guard(self):
        """两份脚本都必须持有拒畸形键名的那道判据（"export KEY" 正是被它挡下）。

        行为复刻见本类另两条；此处钉脚本本体——历史上 run_probe.sh 曾漂移过
        （只剥 key 不剥 value），判据缺位同样从这里红出来。
        """
        for name in ("run.sh", "run_probe.sh"):
            with open(os.path.join(BASE, name), encoding="utf-8") as f:
                src = f.read()
            self.assertIn('[[ ! "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]', src,
                          f"{name} 的 .env 循环缺合法键名判据——export 行可能被放行")

    def test_export_key_never_exported(self):
        """反向：真 bash 跑同构判据段——export 行不导出，只告警（与另两侧收敛）。"""
        if not BASH:
            self.skipTest("无 bash 环境")
        path = self._write(f"export {DECOY}={DECOY_VAL}\n{PLAIN}={PLAIN_VAL}\n")
        script = (_SHELL_GUARD
                  + f'echo "DECOY=${{{DECOY}-unset}}"\n'
                  + f'echo "PLAIN=${{{PLAIN}-unset}}"\n')
        r = subprocess.run([BASH, "-c", script, "probe", path],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = dict(ln.split("=", 1) for ln in r.stdout.strip().splitlines()
                     if ln.startswith(("DECOY=", "PLAIN=")))
        self.assertEqual(lines.get("PLAIN"), PLAIN_VAL, "正向：真实键照常导出")
        self.assertEqual(lines.get("DECOY"), "unset",
                         "反向：export 行不得导出成生效环境变量")
        self.assertIn("非法键名", r.stderr, "shell 侧的显式告警姿态（不得静默）")

    def test_real_key_still_exported(self):
        """正向：同一段判据对真实键照常导出。"""
        if not BASH:
            self.skipTest("无 bash 环境")
        path = self._write(f"{DECOY}={DECOY_VAL}\n")
        script = (_SHELL_GUARD
                  + f'echo "DECOY=${{{DECOY}-unset}}"\n')
        r = subprocess.run([BASH, "-c", script, "probe", path],
                           capture_output=True, text=True)
        self.assertIn(f"DECOY={DECOY_VAL}", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
