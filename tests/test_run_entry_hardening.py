# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""宿主入口脚本加固批（P12-11/13/18, census-P2-settled-marker, P12-24/25）。

标签：J · 运维：部署/备份/发布
覆盖：
  ① run_probe.sh 的按天日志名取 `business_day`（北京业务日），不取宿主 date（P12-11）。
  ② yiban-fallback.sh 的 `.env` 指针固定为 `<APP_DIR>/data/.env`，不受
     YIBAN_ENV_FILE 影响（P12-13）。
  ③ run.sh 补签轮 else 分支按 SEAL_CHECK_RC 分措辞；收尾标记走 mktemp+mv 且 0600
     （P12-18 / census-P2-settled-marker）。
  ④ drill-container.sh 的时钟判据全部按 Asia/Shanghai；探测脚本落 mktemp -d 目录
     并纳入清理对账（P12-24/P12-25）。

依赖：bash；Python 解释器用桩（只回放业务日/版本/退出码），不触网、不碰生产路径。
"""
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBE = os.path.join(BASE, "run_probe.sh")
FALLBACK = os.path.join(BASE, "scripts", "yiban-fallback.sh")
RUN_SH = os.path.join(BASE, "run.sh")
DRILL = os.path.join(BASE, "scripts", "drill-container.sh")
DRILL_BAREMETAL = os.path.join(BASE, "scripts", "drill-baremetal.sh")
BASH = shutil.which("bash")

#: +8 时区字面量的**性质判据**（R-7，2026-10-25 第三轮收紧）。
#: 三种写法算第二定义点：hours=8 / +8 / 08、8*3600、28800。
#: `timedelta(hours=18)` 等其它小时值放行；小数 `hours=8.5` 不是 +8 偏移，放行。
#: 数字边界防禁宽：hours=18、hours=80、128800、16*3600 不算。
TZ_PLUS8_LITERAL = re.compile(
    r"hours\s*=\s*\+?0?8(?![0-9.])"
    r"|(?<![0-9])8\s*\*\s*3600(?![0-9])"
    r"|(?<![0-9])28800(?![0-9])"
)

PY_STUB = r"""#!/usr/bin/env bash
case "$1" in
  -c)
    case "$*" in
      *yiban.core.clock*) echo 2099-01-02 ;;
      *yiban.egress*) echo "1 1" ;;
      *) echo "" ;;
    esac
    exit 0 ;;
  --version) echo "Python 3.11.0"; exit 0 ;;
  -) exit 1 ;;                       # 兜底心跳查询：无心跳
esac
if [ -n "${STUB_MARKER:-}" ]; then : > "$STUB_MARKER"; fi
exit 0
"""


def _write(path, text, mode=0o644):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.chmod(path, mode)


def _tree(tmp, script_src, script_dst):
    dst = os.path.join(tmp, script_dst)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(script_src, dst)
    os.chmod(dst, 0o755)
    _write(os.path.join(tmp, ".venv", "bin", "python3"), PY_STUB, 0o755)
    _write(os.path.join(tmp, "scripts", "signin.py"), "# 测试桩\n", 0o644)
    # 宿主脚本共用的路径事实（business_day / 按天日志名）：入口脚本 source 它。
    _write(os.path.join(tmp, "scripts", "lib", "host_paths.sh"),
           open(os.path.join(BASE, "scripts", "lib", "host_paths.sh"),
                encoding="utf-8").read(), 0o644)
    os.makedirs(os.path.join(tmp, "data"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "state"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "lock"), exist_ok=True)


def _clean_env():
    """去掉 pytest conftest 注入的 YIBAN_*，避免读到跑测进程的全局配置。"""
    return {k: v for k, v in os.environ.items() if not k.startswith("YIBAN_")}


@unittest.skipIf(BASH is None, "需要 bash（Git Bash/WSL）")
class RunProbeBusinessDayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="probe-day-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        _tree(self.tmp, PROBE, "run_probe.sh")
        _write(os.path.join(self.tmp, "data", ".env"),
               "YIBAN_PROBE_ENABLE=1\nYIBAN_STATE_DIR=%s\nYIBAN_LOCK_DIR=%s\n"
               % (os.path.join(self.tmp, "state"), os.path.join(self.tmp, "lock")))
        self.env = _clean_env()
        self.env.update({
            "HOME": self.tmp,
            "YIBAN_APP_DIR": self.tmp,
            "YIBAN_STATE_DIR": os.path.join(self.tmp, "state"),
            "YIBAN_LOCK_DIR": os.path.join(self.tmp, "lock"),
            "YIBAN_PROBE_ENABLE": "1",
        })

    def test_log_name_uses_business_day(self):
        r = subprocess.run([BASH, os.path.join(self.tmp, "run_probe.sh")],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=self.env, cwd=self.tmp, timeout=120)
        out = (r.stdout or "") + (r.stderr or "")
        self.assertEqual(0, r.returncode, out)
        logs = sorted(os.listdir(os.path.join(self.tmp, "state")))
        self.assertIn("sign-2099-01-02.log", logs,
                      f"探针日志名没取 business_day（宿主 date）：{logs}\n{out}")


@unittest.skipIf(BASH is None, "需要 bash（Git Bash/WSL）")
class FallbackEnvPointerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fallback-env-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        _tree(self.tmp, FALLBACK, "scripts/yiban-fallback.sh")
        self.marker = os.path.join(self.tmp, "started")
        self.env = _clean_env()
        self.env.update({
            "HOME": self.tmp,
            "STUB_MARKER": self.marker,
            "YIBAN_STATE_DIR": os.path.join(self.tmp, "state"),
        })

    def test_env_pointer_is_fixed_to_app_dir(self):
        _write(os.path.join(self.tmp, "data", ".env"),
               "YIBAN_FALLBACK_ENABLE=0\n")
        alt = os.path.join(self.tmp, "alt.env")
        _write(alt, "YIBAN_FALLBACK_ENABLE=1\n")
        self.env["YIBAN_ENV_FILE"] = alt
        r = subprocess.run([BASH, os.path.join(self.tmp, "scripts", "yiban-fallback.sh")],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=self.env, cwd=self.tmp, timeout=120)
        self.assertEqual(0, r.returncode, (r.stdout or "") + (r.stderr or ""))
        self.assertFalse(os.path.exists(self.marker),
                         "YIBAN_ENV_FILE 覆盖了 .env 指针（页面关、cron 却拉起）")

    def test_enabled_starts_worker(self):
        _write(os.path.join(self.tmp, "data", ".env"),
               "YIBAN_FALLBACK_ENABLE=1\n")
        r = subprocess.run([BASH, os.path.join(self.tmp, "scripts", "yiban-fallback.sh")],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=self.env, cwd=self.tmp, timeout=120)
        self.assertEqual(0, r.returncode, (r.stdout or "") + (r.stderr or ""))
        self.assertTrue(os.path.exists(self.marker), "开关打开时兜底进程没有启动")


@unittest.skipIf(BASH is None, "需要 bash（Git Bash/WSL）")
class RunShSecondRoundWordingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="runsh-second-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        _tree(self.tmp, RUN_SH, "run.sh")
        _write(os.path.join(self.tmp, "data", ".env"),
               "YIBAN_HOST_SECOND_ROUND=0\n"
               "YIBAN_STATE_DIR=%s\nYIBAN_LOCK_DIR=%s\n"
               % (os.path.join(self.tmp, "state"), os.path.join(self.tmp, "lock")))
        self.env = _clean_env()
        self.env.update({
            "HOME": self.tmp,
            "YIBAN_APP_DIR": self.tmp,
            "YIBAN_STATE_DIR": os.path.join(self.tmp, "state"),
            "YIBAN_LOCK_DIR": os.path.join(self.tmp, "lock"),
        })

    def test_else_branch_does_not_claim_judgement_was_made(self):
        r = subprocess.run([BASH, os.path.join(self.tmp, "run.sh")],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=self.env, cwd=self.tmp, timeout=180)
        out = (r.stdout or "") + (r.stderr or "")
        self.assertEqual(0, r.returncode, out)
        log = os.path.join(self.tmp, "state", "sign-2099-01-02.log")
        self.assertTrue(os.path.isfile(log), f"没有当日日志：{os.listdir(os.path.join(self.tmp, 'state'))}\n{out}")
        with open(log, encoding="utf-8") as f:
            body = f.read()
        self.assertNotIn("当日已收尾且无未了结账号", body,
                         f"未做库内判定却声称已收尾：{body}")
        self.assertIn("未做库内事实判定", body,
                      f"else 分支没有按 SEAL_CHECK_RC 分措辞：{body}")

    def test_settled_marker_is_atomic_0600(self):
        r = subprocess.run([BASH, os.path.join(self.tmp, "run.sh")],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=self.env, cwd=self.tmp, timeout=180)
        self.assertEqual(0, r.returncode, (r.stdout or "") + (r.stderr or ""))
        state = os.path.join(self.tmp, "state")
        marker = os.path.join(state, "yiban-settled-2099-01-02.marker")
        self.assertTrue(os.path.exists(marker), f"收尾标记未写入：{os.listdir(state)}")
        mode = stat.S_IMODE(os.stat(marker).st_mode)
        self.assertEqual(0o600, mode, f"收尾标记权限 {mode:o} ≠ 0600")
        leftovers = [n for n in os.listdir(state) if n.startswith(".yiban-settled.")]
        self.assertEqual([], leftovers, f"mktemp 中间件残留：{leftovers}")


class DrillClockAndTempGateTest(unittest.TestCase):
    """drill-container.sh 的结构不变量（该脚本需要 docker，CI 只查源码面）。"""

    @classmethod
    def setUpClass(cls):
        with open(DRILL, encoding="utf-8") as f:
            cls.src = f.read()
        with open(DRILL_BAREMETAL, encoding="utf-8") as f:
            cls.src_baremetal = f.read()

    def test_clock_judgements_use_asia_shanghai(self):
        self.assertIn('TZ=Asia/Shanghai date +%H:%M', self.src,
                      "首签闩锁预检没有按北京钟取当前时刻")
        # 当日日志名改走 `business_day`（唯一副本在 scripts/lib/host_paths.sh）：
        # 这里只钉"没有退回宿主钟"，北京业务日由 CrossLanguageShapeTest 与 lib 自身钉住。
        self.assertIn("business_day", self.src,
                      "当日日志名没有按北京业务日取")
        self.assertNotIn('NOW_HM="$(date +%H:%M)"', self.src,
                         "首签闩锁预检仍用宿主钟")
        self.assertNotIn('TODAY="$(date +%F)"', self.src,
                         "签到日志名仍用宿主 date")
        # 窗口与跨日判定只认 yiban.core.clock.TZ（R-7，2026-10-25 复审）：
        # 再写一份 +8 字面量即第二定义点，门要红。
        self.assertIn("from yiban.core import clock", self.src,
                      "合成窗口没有引用业务钟 clock.TZ")
        self.assertIn('int(os.environ["DRILL_WS"]), clock.TZ', self.src,
                      "合成窗口没有按 clock.TZ 构造")
        self.assertIn("from yiban.core.clock import TZ", self.src,
                      "跨日判定没有引用业务钟 TZ")
        # 性质判据：任何 +8 字面量写法都算第二定义点（R-7，第二轮）。
        self.assertNotRegex(self.src, TZ_PLUS8_LITERAL,
                            "drill-container.sh 写死了 +8 时区字面量")
        self.assertNotRegex(self.src_baremetal, TZ_PLUS8_LITERAL,
                            "drill-baremetal.sh 写死了 +8 时区字面量")

    def test_probe_scripts_use_mktemp_dir(self):
        self.assertIn("mktemp -d", self.src, "探测脚本没有用 mktemp -d 建私有目录")
        self.assertNotIn("cat > /tmp/drill_", self.src,
                         "探测脚本仍写可预测的 /tmp/drill_*.py")
        self.assertIn('"$DRILL_TMP"', self.src,
                      "探测脚本没有落进 mktemp 目录")
        self.assertRegex(self.src, r"rm -rf \"\$DRILL_TMP\"",
                         "mktemp 目录没有纳入清理")


class TimezoneLiteralPropertyTest(unittest.TestCase):
    """R-7 性质判据的边界：三类写法必中，正常写法不得误红（防禁宽）。"""

    FORBIDDEN = (
        "hours=8",
        "hours = 8",
        "hours=+8",
        "hours=08",
        "hours = +08",
        "timedelta(hours=8)",
        "x = datetime.timedelta(hours=+08)",
        "8*3600",
        "8 * 3600",
        "28800",
        "WIN_S + 28800",
    )
    BENIGN = (
        "hours=18",
        "hours=80",
        "hours=8.5",
        "timedelta(hours=18)",
        "timedelta(hours=80)",
        "timedelta(minutes=480)",
        "16*3600",
        "128800",
        "clock.TZ",
    )

    def test_forbidden_forms_match(self):
        for sample in self.FORBIDDEN:
            self.assertRegex(sample, TZ_PLUS8_LITERAL,
                             "性质判据漏掉写法：%r" % sample)

    def test_benign_forms_do_not_match(self):
        for sample in self.BENIGN:
            self.assertNotRegex(sample, TZ_PLUS8_LITERAL,
                                "性质判据过宽，误伤正常写法：%r" % sample)


if __name__ == "__main__":
    unittest.main(verbosity=2)
