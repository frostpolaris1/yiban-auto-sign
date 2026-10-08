# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""Vue 构建产物可复现门禁的元测试（工单 yiban-auto-sign-spn6）。

标签：F · 前端与界面守卫
覆盖：`scripts/check-vue-build-reproducible.sh` 的三件事——
    ① 守卫一（换行符口径）在真仓判绿，且在带 CR 的合成树上判红并点名文件；
       二进制文件（含 NUL 字节）不参与判定；
    ② 守卫二（可复现）的比对段在真产物上判绿，在内容改/多件/缺件/CR 四种漂移上判红；
       待比对目录不存在时退出码 2（环境错误，不是判红）；
    ③ 门在 CI：`.github/workflows/ci.yml` 的 `frontend` job 真跑门禁脚本；
       门禁脚本真**调用**两道守卫的承重段（定义留着、调用摘掉也必须红）。

本文件只跑不需要 node 的两个模式（`--lf-only` 与 `--compare`）。需要 node 的重建
与突变证明在 `scripts/e2e/vue-build-reproducible-e2e.sh`（WSL 内手跑，7 例）。

为什么本文件存在：CRLF 会让 Vite 引出不同的 Vue scopeId，产物文件名随之不同。
2026-10-06 的 develop 曾因此出现混合来源产物（工单 spn6）。
本门禁钉住「单一 LF 来源 + 单次构建可完整复现」。

依赖：bash（`shutil.which` 判缺则整文件跳过）；真实临时目录；不需要 node、不联网。
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASH = shutil.which("bash")
GUARD = os.path.join(BASE, "scripts", "check-vue-build-reproducible.sh")
CI_YML = os.path.join(BASE, ".github", "workflows", "ci.yml")
COMMITTED = os.path.join(BASE, "web", "static", "vue")
TIMEOUT = 120


def _guard(*args):
    return subprocess.run(
        [BASH, GUARD, *args], capture_output=True, text=True, cwd=BASE, timeout=TIMEOUT
    )


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _logs_js_name():
    """取 logs 入口的 JS 产物名。产物名含内容哈希，故由目录反查，不写死。"""
    assets = os.path.join(COMMITTED, "assets")
    names = sorted(n for n in os.listdir(assets)
                   if n.startswith("logs-") and n.endswith(".js"))
    if len(names) != 1:
        raise AssertionError(f"logs 入口的 JS 产物应恰好一个，实得：{names}")
    return names[0]


def _job_body(ci_text, job):
    """抽 <.github/workflows/ci.yml> 里 2 空格缩进的某个 job 的正文。

    只取该 job 段落，避免用整份 ci.yml 做断言——别处的同名串会让断言变成废断言。
    """
    m = re.search(rf"^  {re.escape(job)}:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
                  ci_text, re.S | re.M)
    return m.group(1) if m else ""


@unittest.skipUnless(BASH, "需要 bash（Git Bash / WSL）")
class VueBuildReproducibleLfCensusTest(unittest.TestCase):
    """守卫一：换行符口径。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-vue-eol-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fixture_repo(self, src_name="a.vue", src_bytes=b"<template/>\n",
                      art_name="x.js", art_bytes=b"export default 1\n"):
        """建一棵最小仓库树：frontend/src/<src_name> + web/static/vue/assets/<art_name>。"""
        src_dir = os.path.join(self.tmp, "frontend", "src")
        art_dir = os.path.join(self.tmp, "web", "static", "vue", "assets")
        os.makedirs(src_dir, exist_ok=True)
        os.makedirs(art_dir, exist_ok=True)
        with open(os.path.join(src_dir, src_name), "wb") as fh:
            fh.write(src_bytes)
        with open(os.path.join(art_dir, art_name), "wb") as fh:
            fh.write(art_bytes)
        return self.tmp

    def test_lf_census_is_green_on_the_current_repo(self):
        """真仓当前全 LF ⇒ 守卫一必须绿（门禁不许出生即红）。"""
        r = _guard("--repo", BASE, "--lf-only")
        self.assertEqual(r.returncode, 0, f"真仓应全 LF：\nstdout={r.stdout}\nstderr={r.stderr}")
        self.assertIn("全绿", r.stdout)

    def test_lf_census_is_red_on_crlf_source_and_names_the_file(self):
        """源码带 CR ⇒ 红，且点名的路径指向该源文件。"""
        repo = self._fixture_repo(src_bytes=b"<template/>\r\n<script/>\r\n")
        r = _guard("--repo", repo, "--lf-only")
        self.assertEqual(r.returncode, 1, f"CRLF 源码必须判红：\n{r.stdout}{r.stderr}")
        self.assertIn("frontend/src/a.vue", r.stderr, "必须点名带 CR 的源文件")

    def test_lf_census_is_red_on_crlf_artifact(self):
        """产物带 CR ⇒ 红（产物与源码同口径判定）。"""
        repo = self._fixture_repo(art_bytes=b"export default 1\r\n")
        r = _guard("--repo", repo, "--lf-only")
        self.assertEqual(r.returncode, 1, f"CRLF 产物必须判红：\n{r.stdout}{r.stderr}")
        self.assertIn("web/static/vue/assets/x.js", r.stderr)

    def test_lf_census_skips_binary_files_even_with_cr_byte(self):
        """含 NUL 的二进制文件即使有 0x0D 也不判红（资源不可按行尾论）。"""
        repo = self._fixture_repo(art_name="font.woff2", art_bytes=b"\x00\x0d\x00\x0a\xff")
        r = _guard("--repo", repo, "--lf-only")
        self.assertEqual(r.returncode, 0, f"二进制不应参与行尾判定：\n{r.stdout}{r.stderr}")

    def test_lf_census_is_red_on_crlf_in_the_committed_vite_manifest(self):
        """D2：`web/static/vue/.vite/manifest.json` 是跟踪产物，含 CR 必须判红。

        剪枝曾把整个 `.vite` 剪掉，只给它留 CR 时 `--lf-only` 会漏判。
        """
        repo = self._fixture_repo()
        manifest = os.path.join(repo, "web", "static", "vue", ".vite", "manifest.json")
        os.makedirs(os.path.dirname(manifest), exist_ok=True)
        with open(manifest, "wb") as fh:
            fh.write(b'{"x.js": {}}\r\n')
        r = _guard("--repo", repo, "--lf-only")
        self.assertEqual(r.returncode, 1,
                         f".vite/manifest.json 带 CR 必须判红：\n{r.stdout}{r.stderr}")
        self.assertIn(".vite/manifest.json", r.stderr, "必须点名 manifest.json")

    def test_lf_census_is_an_environment_error_when_a_scan_dir_is_missing(self):
        """D1：扫描目录缺失是环境错误（退 2），不是全绿。

        曾对不存在的目录直接 `return 0`：删掉 frontend/ 后 `--lf-only` 打印全绿退 0。
        """
        repo = self._fixture_repo()
        shutil.rmtree(os.path.join(repo, "frontend"))
        r = _guard("--repo", repo, "--lf-only")
        self.assertEqual(r.returncode, 2,
                         f"扫描目录缺失必须退 2（环境错误）：\n{r.stdout}{r.stderr}")
        self.assertIn("环境错误", r.stderr)

    def test_lf_census_is_an_environment_error_when_a_scan_dir_is_empty(self):
        """D1：扫描目录在但没有可扫文件，同样是环境错误（零命中不许判绿）。"""
        repo = self._fixture_repo()
        src = os.path.join(repo, "frontend", "src")
        for name in os.listdir(src):
            os.remove(os.path.join(src, name))
        r = _guard("--repo", repo, "--lf-only")
        self.assertEqual(r.returncode, 2,
                         f"零命中必须退 2（环境错误）：\n{r.stdout}{r.stderr}")
        self.assertIn("环境错误", r.stderr)

    def test_unknown_flag_is_an_environment_error(self):
        """未知参数是环境错误（退出码 2），不是门禁红（1）。"""
        r = _guard("--no-such-flag")
        self.assertEqual(r.returncode, 2, f"参数错必须退 2：\n{r.stdout}{r.stderr}")


@unittest.skipUnless(BASH, "需要 bash（Git Bash / WSL）")
class VueBuildReproducibleCompareTest(unittest.TestCase):
    """守卫二的比对段：不需要 node，直接喂比对目录。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-vue-cmp-")
        self.copy = os.path.join(self.tmp, "built")
        shutil.copytree(COMMITTED, self.copy)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cmp(self, path):
        return _guard("--repo", BASE, "--compare", path)

    def test_compare_is_green_on_an_identical_tree(self):
        r = self._cmp(self.copy)
        self.assertEqual(r.returncode, 0, f"同一棵树应判绿：\n{r.stdout}{r.stderr}")
        self.assertIn("逐字节相同", r.stdout)

    def test_compare_is_red_on_a_changed_byte(self):
        target = os.path.join(self.copy, "assets", _logs_js_name())
        with open(target, "ab") as fh:
            fh.write(b"\n/* drift */\n")
        r = self._cmp(self.copy)
        self.assertEqual(r.returncode, 1, f"内容漂移必须判红：\n{r.stdout}{r.stderr}")
        self.assertIn(os.path.basename(target), r.stderr, "diff 必须点名漂移的文件")

    def test_compare_is_red_on_an_extra_file(self):
        with open(os.path.join(self.copy, "assets", "extra-Chunk0.js"), "w",
                  encoding="utf-8", newline="\n") as fh:
            fh.write("export default 0\n")
        r = self._cmp(self.copy)
        self.assertEqual(r.returncode, 1, f"多余条目必须判红：\n{r.stdout}{r.stderr}")
        self.assertIn("extra-Chunk0.js", r.stderr)

    def test_compare_is_red_on_a_missing_file(self):
        name = _logs_js_name()
        os.remove(os.path.join(self.copy, "assets", name))
        r = self._cmp(self.copy)
        self.assertEqual(r.returncode, 1, f"缺失条目必须判红：\n{r.stdout}{r.stderr}")
        self.assertIn(name, r.stderr)

    def test_compare_is_red_on_a_crlf_conversion(self):
        """产物由 LF 改 CRLF ⇒ 红（逐字节比对对行尾同样敏感）。"""
        target = os.path.join(self.copy, ".vite", "manifest.json")
        with open(target, "rb") as fh:
            data = fh.read()
        with open(target, "wb") as fh:
            fh.write(data.replace(b"\n", b"\r\n"))
        r = self._cmp(self.copy)
        self.assertEqual(r.returncode, 1, f"CRLF 化必须判红：\n{r.stdout}{r.stderr}")
        self.assertIn("manifest.json", r.stderr)

    def test_compare_reports_an_environment_error_when_the_dir_is_missing(self):
        r = self._cmp(os.path.join(self.tmp, "no-such-dir"))
        self.assertEqual(r.returncode, 2, f"缺目录是环境错误：\n{r.stdout}{r.stderr}")
        self.assertIn("环境错误", r.stderr)


class VueBuildReproducibleWiringTest(unittest.TestCase):
    """③ 门在 CI，且门禁脚本真调用两道守卫的承重段（在场性）。"""

    def setUp(self):
        self.guard = _read(GUARD)
        self.ci = _read(CI_YML)
        self.frontend_job = _job_body(self.ci, "frontend")
        self.verify_job = _job_body(self.ci, "verify")

    def test_guard_script_exists_and_is_readable(self):
        self.assertGreater(len(self.guard), 500, "门禁脚本读不出来——路径变了？")

    def test_frontend_job_runs_the_guard(self):
        """D6：断言必须钉住完整 `run:` 行，不能只钉文件名。

        只钉 `scripts/check-vue-build-reproducible.sh` 是废断言：该串在 frontend job 的
        注释里也有（`ci.yml` 步骤说明段）。删掉承重的 `run:` 行后断言仍绿，
        CI 守卫可被静默摘除。
        """
        self.assertGreater(len(self.frontend_job), 100,
                           "抽不出 frontend job 正文——ci.yml 结构改了，本条会变废断言")
        self.assertIn("run: bash ../scripts/check-vue-build-reproducible.sh", self.frontend_job,
                      "CI 的 frontend job 必须真跑可复现门禁（job 有 node 与 node_modules）")
        self.assertIn("run: npm ci", self.frontend_job, "frontend job 必须先装依赖，门禁才可重建")

    def test_guard_invokes_both_load_bearing_blocks(self):
        """两道守卫的调用行必须在（定义留着、调用摘掉 = 守卫不承重）。"""
        for call in ('census_scan "$FRONTEND"', 'census_scan "$COMMITTED"',
                     'compare_artifacts "$tmp/out"'):
            self.assertIn(call, self.guard, f"守卫调用行不见了：{call}")

    def test_verify_job_selects_this_meta_test(self):
        """D5：元测试必须被 CI 选择面选中，否则它在 CI 里不跑（仓里的摆设）。

        CI 的 `-k` 词集住在 scripts/dev-verify.sh 的 run_ci（本单不改：另一分支在飞改
        该文件），故本单在 verify job 里按文件路径直接选测。本元测试不需要 node，
        放进有 Python 的 verify job。
        """
        self.assertGreater(len(self.verify_job), 100,
                           "抽不出 verify job 正文——ci.yml 结构改了，本条会变废断言")
        self.assertIn("tests/test_vue_build_reproducible.py", self.verify_job,
                      "CI 必须在 verify job 直接跑本元测试（dev-verify.sh 的 -k 不收它）")

    def test_guard_reproducible_cleans_its_temp_dir_on_any_exit(self):
        """D3：重建段的临时目录必须挂 EXIT trap 清理——环境错误路径（die → exit 2）也不许残留。"""
        m = re.search(r"guard_reproducible\(\)\s*\{(.*?)\n\}", self.guard, re.S)
        self.assertIsNotNone(m, "抽不出 guard_reproducible 函数体——结构改了，本条会变废断言")
        body = m.group(1)
        self.assertIn("trap", body, "重建段必须挂 EXIT trap 清临时目录")
        self.assertIn('rm -rf "$tmp"', body, "EXIT trap 必须删 $tmp")

    def test_guard_keeps_the_mutant_markers_the_e2e_relies_on(self):
        """e2e 用 sed 删段造变异体；标记消失 = 突变证明静默失效。"""
        for name in ("lf-census", "compare-artifacts"):
            self.assertIn(f"vue-eol-mutant-begin: {name}", self.guard, f"缺变异段起点：{name}")
            self.assertIn(f"vue-eol-mutant-end: {name}", self.guard, f"缺变异段终点：{name}")


if __name__ == "__main__":
    unittest.main()
