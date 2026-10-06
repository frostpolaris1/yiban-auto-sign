# -*- coding: utf-8 -*-
"""yiban-auto-sign-6g92 守卫：`child_env` 告警频率上界口径 + 告警频次口径落在入库文件。

标签：B · 调度：配置与告警口径
覆盖：`scripts/child_env.py::parse_env_file` 的两类 `.env` 读失败告警，其**调用频率
   上界**必须以常量名表达（`PROBE_TRY_SECONDS` / `FALLBACK_TRY_SECONDS` /
   `YIBAN_FALLBACK_INTERVAL`）、不得抄常量值，且必须是一条同时管住
   `FileNotFoundError` 与 `OSError` 两个分支的共用口径；PM 2026-10-05 裁定的
   **告警频次口径**（状态突变类每次响亮 / 反复可容忍类允许静默）必须落在
   **会入库**的文件里（`run.sh`），不得只活在 `.gitignore` 命中的文稿里。
对应实现：`scripts/child_env.py::parse_env_file`（上界口径块）、
   `run.sh` 的「告警频次口径」段（口径正文）、`docker/scheduler.py`（两枚周期常量的
   定义点与到点判法、兜底闸门 `_fallback_gate_env` 的读盘）、
   `yiban/engine/workers.py`（常驻扫描间隔的键与缺省域）、
   `web/routes/signin_api.py`（web 手动签到每请求一次）。
关键断言：九条，缺一即红——
   ① 上界块按常量名引用三个周期来源，且每名在 `child_env.py` 内只出现一次；
   ② 上界块位于 `try:` 之上（同批管住两个 except 分支，不是只管 `OSError`）；
   ③ 注释里没有任何常量值或"数字 次/日"（本单要消灭的正是这种会过期的假话）；
   ④ 两枚周期常量仍是 `docker/scheduler.py` 的定义点并参与到点判法，值锚在本测试；
   ⑤ 常驻扫描间隔的键与缺省域仍是 `yiban/engine/workers.py` 的那一处；
   ⑥ 三个周期来源今日仍有出处（兜底闸门读盘 / web 每请求 / 兜底外壳在场）；
   ⑦ 口径正文住在 `run.sh`，且点名两类的承载者；
   ⑧ 口径正文与 `child_env` 的上界块逐字同名同锚；
   ⑨ 两枚口径承载者都在 git 跟踪集里（不入库 = 没写）。
   改常量值、改常量名、删掉兜底路径、把口径挪进不入库的文稿——任一项都会把上界式
   变成假话，本测试即红。数字只活在测试里（改值响亮），注释里一个数字都没有
   （改值不失真）：这样才不会再留一条假话。
依赖：只读源码文本 + `git ls-files`；不建临时件、不起服务、无网络、无 skip。
用法（项目根目录）：bash scripts/dev-verify.sh --target tests/test_alert_frequency_caliber.py
"""
import io
import os
import re
import subprocess
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CHILD_ENV = os.path.join(BASE, "scripts", "child_env.py")
SCHEDULER = os.path.join(BASE, "docker", "scheduler.py")
WORKERS = os.path.join(BASE, "yiban", "engine", "workers.py")
RUN_SH = os.path.join(BASE, "run.sh")
SIGNIN_API = os.path.join(BASE, "web", "routes", "signin_api.py")
FALLBACK_SH = os.path.join(BASE, "scripts", "yiban-fallback.sh")

# 口径正文的锚串（run.sh 与 child_env.py 必须逐字同一枚名字，否则指针落空）
CALIBER_ANCHOR = "告警频次口径"
CLASS_LOUD = "状态突变"
CLASS_TOLERABLE = "反复可容忍"

# 值锚：注释按**常量名**引用，值钉在这里。改值的人会被这一条拦住并要求他回去核对
# 上界式——而不是让值住进注释、在下次改值时变成一句没人碰过的假话。
PERIODIC_VALUES = {
    # 常量名: (定义点相对路径, 当日值)
    "PROBE_TRY_SECONDS": ("docker/scheduler.py", 600),
    "FALLBACK_TRY_SECONDS": ("docker/scheduler.py", 60),
}
# 常驻扫描间隔：键名 + 缺省值 + 合法域（唯一出处是 workers.run_fallback_worker）
RESIDENT_KEY = ("yiban/engine/workers.py", "YIBAN_FALLBACK_INTERVAL", 60, 5, 3600)


def _text(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def _comment_lines(text):
    """只取整行注释（`#` 起头），用于判"注释里写了什么"而不牵连代码与字符串。"""
    return [ln for ln in text.splitlines() if ln.lstrip().startswith("#")]


def _tracked_relpaths():
    """仓库跟踪集（相对路径）。WSL 里跑 Windows worktree 时 `.git` 里的 gitdir 是
    Windows 绝对路径，git 解析不了——换算成 /mnt/<盘>/… 重试（同
    tests/test_deploy_prod_artifacts.py 的既有做法）。"""
    run = subprocess.run(["git", "-C", BASE, "ls-files", "-z"],
                         capture_output=True, timeout=120)
    if run.returncode != 0:
        gitfile = os.path.join(BASE, ".git")
        if os.path.isfile(gitfile):
            gitdir = _text(gitfile).strip()
            if gitdir.startswith("gitdir:"):
                gitdir = gitdir[len("gitdir:"):].strip().replace("\\", "/")
            if len(gitdir) > 2 and gitdir[1] == ":":
                gitdir = "/mnt/" + gitdir[0].lower() + gitdir[2:]
            run = subprocess.run(["git", "--git-dir", gitdir,
                                  "--work-tree", BASE, "ls-files", "-z"],
                                 capture_output=True, timeout=120)
    assert run.returncode == 0, run.stderr.decode("utf-8", "replace")
    return {p.decode("utf-8") for p in run.stdout.split(b"\x00") if p}


def _cited_names():
    """本仓上界式必须点名的三个周期来源（两枚常量 + 一枚键）。"""
    return (*PERIODIC_VALUES, RESIDENT_KEY[1])


class ChildEnvBoundCommentTest(unittest.TestCase):
    """①②③：上界口径必须按常量名表达、只有一条、且同时管住两个 except 分支。"""

    def setUp(self):
        self.src = _text(CHILD_ENV)
        self.comments = "\n".join(_comment_lines(self.src))

    def test_bound_cites_every_periodic_source_by_name(self):
        for name in _cited_names():
            self.assertIn(name, self.comments,
                          f"child_env 的上界注释未点名 {name}：上界式必须可按常量名核对")
            self.assertEqual(
                self.src.count(name), 1,
                f"{name} 在 child_env.py 里出现 {self.src.count(name)} 次："
                "同一件事只准有一条上界口径，两处抄写迟早互相打架")

    def test_bound_block_covers_both_except_branches(self):
        """上界块必须在 `try:` 之上——两个 except 分支同族（同一函数、同一批调用点、
        同一上界），只管 `OSError` 那一支就是"同批只改了一半"。"""
        lines = self.src.splitlines()
        start = next(i for i, ln in enumerate(lines) if ln.startswith("def parse_env_file"))
        try_at = next((i for i, ln in enumerate(lines[start:], start)
                       if ln.strip() == "try:"), None)
        self.assertIsNotNone(try_at, "parse_env_file 里找不到 try 块")
        bound_lines = [i for i, ln in enumerate(lines)
                       if ln.lstrip().startswith("#")
                       and any(n in ln for n in _cited_names())]
        self.assertTrue(bound_lines, "注释里没有点名周期来源的上界块")
        self.assertLess(min(bound_lines), try_at,
                        "上界注释落在 except 分支内部：另一支不受它约束，两支的口径"
                        "会分叉——把上界块提到 try 之上")

    def test_bound_comment_carries_no_constant_value_or_day_rate(self):
        """③：不许把常量值抄进注释——`PROBE_TRY_SECONDS` 一改，抄值就变成假话。"""
        banned = ("600", "1584", "1440", "144")
        for num in banned:
            self.assertIsNone(
                re.search(r"(?<!\d)%s(?!\d)" % num, self.comments),
                f"注释里出现数字 {num}：上界必须写成引用常量名的公式，不得抄值")
        self.assertIsNone(
            re.search(r"\d+\s*次\s*/\s*[日天]", self.comments),
            "注释里出现「数字 次/日」式条数：改值即失真，只准留公式")


class CadenceSourceTest(unittest.TestCase):
    """④⑤⑥：上界式引用的来源必须仍然存在——任一来源消失或改名，注释就得跟着改。"""

    def test_periodic_constants_keep_value_and_gate_role(self):
        sched = _text(SCHEDULER)
        for name, (rel, pinned) in PERIODIC_VALUES.items():
            self.assertTrue(os.path.isfile(os.path.join(BASE, rel)),
                            f"{rel} 不在了：上界式的来源文件缺失")
            m = re.search(r"(?m)^%s = (\d+)\s*$" % name, sched)
            self.assertIsNotNone(
                m, f"{name} 不再是 {rel} 的模块级定义点："
                   "child_env 的上界式按它命名，来源没了注释就成了假话")
            self.assertEqual(
                int(m.group(1)), pinned,
                f"{name} 的值已从 {pinned} 改为 {m.group(1)}：请回去核对 "
                "scripts/child_env.py 的上界式与 run.sh 的「告警频次口径」是否仍然成立，"
                "再更新本值锚（数字只钉在这里，不钉在注释里）")
            self.assertIsNotNone(
                re.search(r">= %s\b" % name, sched),
                f"{name} 在 {rel} 里没有到点判法（>= 形式）："
                f"定义点之外还须参与判法，否则「每 N 一次」的上界不成立")

    def test_resident_interval_key_keeps_default_and_domain(self):
        """常驻扫描间隔的唯一出处：`run_fallback_worker` 的 `_env_int` 调用。"""
        rel, key, default, low, high = RESIDENT_KEY
        workers = _text(WORKERS)
        m = re.search(
            r'_env_int\(\s*"%s"\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)' % key, workers)
        self.assertIsNotNone(
            m, f"{key} 在 {rel} 不再是 `_env_int(…)` 的读点："
               "上界式的常驻项按它命名，读点没了注释就成了假话")
        got = tuple(int(g) for g in m.groups())
        self.assertEqual(
            got, (default, low, high),
            f"{key} 的缺省/域已从 {(default, low, high)} 改为 {got}："
            "请回去核对上界式与 run.sh 的「告警频次口径」，再更新本值锚")

    def test_three_periodic_sources_have_outlets(self):
        """上界式的三个来源逐个验：容器调度器两枚周期闸门 + 常驻引擎扫描 + web 每请求。"""
        sched = _text(SCHEDULER)
        self.assertRegex(
            sched, r'from child_env import .*parse_env_file',
            "容器调度器不再直接调 child_env.parse_env_file：兜底闸门那一项的落点变了")
        self.assertRegex(
            sched, r"(?s)def _fallback_gate_env\(.*?return parse_env_file\(ENV_FILE\)",
            "兜底闸门 `_fallback_gate_env` 不再读 .env：常驻路径的频率上界需要重算")
        lines = [ln for ln in sched.splitlines() if "FALLBACK_TRY_SECONDS" in ln]
        self.assertTrue(any("last_fallback_try" in ln for ln in lines),
                        "FALLBACK_TRY_SECONDS 不再守 last_fallback_try 那条闸门")
        lines = [ln for ln in sched.splitlines() if "PROBE_TRY_SECONDS" in ln]
        self.assertTrue(any("last_probe_try" in ln for ln in lines),
                        "PROBE_TRY_SECONDS 不再守 last_probe_try 那条闸门")
        self.assertIn("build_child_env", _text(SIGNIN_API),
                      "web 手动签到不再经 build_child_env：每请求一次的来源消失")
        self.assertTrue(os.path.isfile(FALLBACK_SH),
                        "反复可容忍类的承载者 yiban-fallback.sh 不在了")


class CaliberLocationTest(unittest.TestCase):
    """⑦⑧：口径正文必须住在会入库的文件里（本仓文档纪律：docs/ 一律不入库）。"""

    def test_run_sh_carries_the_caliber(self):
        src = _text(RUN_SH)
        for anchor in (CALIBER_ANCHOR, CLASS_LOUD, CLASS_TOLERABLE):
            self.assertIn(anchor, src, f"run.sh 缺少口径锚串「{anchor}」")
        # 两类的承载者都要点名，否则"同理由不同结论"的质疑还堵不住
        for carrier in ("scripts/child_env.py", "scripts/yiban-fallback.sh"):
            self.assertIn(carrier, src,
                          f"run.sh 的口径段未点名承载者 {carrier}")

    def test_child_env_points_at_the_caliber(self):
        comments = "\n".join(_comment_lines(_text(CHILD_ENV)))
        self.assertIn(CALIBER_ANCHOR, comments,
                      "child_env 未指向 run.sh 的口径正文：两处必须同名同锚")

    def test_caliber_files_are_tracked_by_git(self):
        tracked = _tracked_relpaths()
        for rel in ("run.sh", "scripts/child_env.py"):
            self.assertIn(rel, tracked,
                          f"{rel} 不在 git 跟踪集里：口径正文落在不入库的文件等于没写")


if __name__ == "__main__":
    unittest.main()
