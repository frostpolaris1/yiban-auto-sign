# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""UI 发布物入签名清单的接线门（P1-6 ③，2026-11-26）。

标签：J · 运维：发布
覆盖：`ui/dist` 的每个普通文件都要进 `ui-SHA256SUMS`，清单本身进
    发布 manifest 作第三件，update.sh 在停服前按清单逐文件对账。
    本文件钉住四处接线：
      ① `scripts/release/make-ui-sums.sh` 真能生成可被 `sha256sum -c`
        直接吃的清单：路径相对 ui 目录、LC_ALL=C 排序、拒绝软链/非普通
        文件/路径逃逸/写进 ui 目录自身；
      ② `make-manifest.sh` 把 `ui-SHA256SUMS` 当普通第三件收进清单
        （名只认基名，哈希覆盖清单字节）；
      ③ `build-release.sh` 的旧单参调用仍可用，新增 `--ui-dir` 时在
        发布 staging（与二进制同目录、**不在** dist 内）生成清单，并把
        三件交给 make-manifest.sh 的命令打印出来；
      ④ `update.sh` 真解析 `--ui-checksums`、默认取 manifest 同目录、
        把 UI 与清单快照进 STAGE 后再校验，激活只用快照。
判据实现：行为断言跑真脚本（临时目录），静态断言只钉接线字符串；
    两者都失败即红，不依赖网络、不签名、不装服务。
依赖：bash、sha256sum、git（构建接线用例）；缺则显性 skipTest。
用法：python3 -m unittest discover -s tests -p 'test_ui_signing_wiring.py' -v
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELEASE_DIR = os.path.join(BASE, "scripts", "release")
MAKE_UI_SUMS = os.path.join(RELEASE_DIR, "make-ui-sums.sh")
MAKE_MANIFEST = os.path.join(RELEASE_DIR, "make-manifest.sh")
BUILD_RELEASE = os.path.join(BASE, "server", "scripts", "build-release.sh")
UPDATE_SH = os.path.join(BASE, "deploy", "baremetal", "update.sh")
SIGNING_DOC = os.path.join(BASE, "docs", "dev", "release-signing.md")
RELEASE_DOC = os.path.join(BASE, "deploy", "release.md")
TOOLING_GATE = os.path.join(BASE, "scripts", "tooling-gate.sh")
UI_SIGNING_E2E = os.path.join(BASE, "scripts", "e2e", "ui-signing-e2e.sh")
BARE_METAL_E2E = os.path.join(BASE, "scripts", "e2e", "deploy-baremetal-e2e.sh")
E2E_DOC = os.path.join(BASE, "docs", "dev", "deploy-e2e.md")
BASH = shutil.which("bash")
SHA256SUM = shutil.which("sha256sum")
GIT = shutil.which("git")


def read_repo(rel):
    with open(os.path.join(BASE, rel), "r", encoding="utf-8") as f:
        return f.read()


def run(args, cwd=None, env=None):
    full = dict(os.environ)
    full.update(env or {})
    return subprocess.run(args, cwd=cwd, env=full, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def run_bash(args, cwd=None, env=None):
    return run([BASH, *list(args)], cwd=cwd, env=env)


def write_file(path, body, mode=0o644):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    os.chmod(path, mode)


def make_ui_tree(root, marker="ui"):
    write_file(os.path.join(root, "index.html"), "<div>" + marker + "</div>\n")
    write_file(os.path.join(root, "assets", "app-1.js"), "// " + marker + "\n")
    write_file(os.path.join(root, "assets", "nested", "deep.css"), "body{}\n")


class MakeUISumsTest(unittest.TestCase):
    """行为：真跑 make-ui-sums.sh。"""

    def setUp(self):
        if not BASH:
            self.skipTest("SKIP: 没有 bash，UI 清单无法真跑")
        if not SHA256SUM:
            self.skipTest("SKIP: 没有 sha256sum，清单无法生成/校验")
        if not os.path.isfile(MAKE_UI_SUMS):
            self.fail("缺少 scripts/release/make-ui-sums.sh：UI 发布物没有任何清单生成器")
        self.tmp = tempfile.mkdtemp(prefix="ui-sums-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ui = os.path.join(self.tmp, "dist")
        make_ui_tree(self.ui)
        self.out = os.path.join(self.tmp, "ui-SHA256SUMS")

    def test_lists_every_regular_file_relative_and_sorted_lc_all(self):
        r = run_bash([MAKE_UI_SUMS, self.ui, self.out])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(self.out, "r", encoding="utf-8") as f:
            body = f.read()
        lines = [ln for ln in body.splitlines() if ln]
        self.assertTrue(lines, "清单不得为空")
        paths = []
        for line in lines:
            self.assertEqual(line[64:66], "  ", "每行必须是 <64hex><两空格><路径>：" + line)
            self.assertRegex(line[:64], r"^[0-9a-f]{64}$")
            paths.append(line[66:])
        self.assertEqual(
            paths,
            sorted(paths, key=lambda p: p.encode("utf-8")),
            "清单必须按 LC_ALL=C 字节序排序",
        )
        self.assertEqual(
            set(paths),
            {"index.html", "assets/app-1.js", "assets/nested/deep.css"},
        )
        for p in paths:
            self.assertFalse(p.startswith("/"), "清单路径必须是相对 ui 目录：" + p)
            self.assertNotIn("..", p.split("/"), "清单路径不得逃逸：" + p)
        self.assertNotIn("ui-SHA256SUMS", body, "清单不得把自己列进去")
        # sha256sum -c 直接吃：cd 进 ui 目录校验。
        check = run([SHA256SUM, "-c", "--", self.out], cwd=self.ui)
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)

    def test_refuses_output_inside_ui_dir(self):
        inside = os.path.join(self.ui, "ui-SHA256SUMS")
        r = run_bash([MAKE_UI_SUMS, self.ui, inside])
        self.assertNotEqual(r.returncode, 0, "清单写进 dist 会随发布被再发布：" + r.stdout + r.stderr)
        self.assertFalse(os.path.exists(inside))
        self.assertNotIn("index.html", r.stdout + r.stderr)

    def test_refuses_symlink_entry(self):
        try:
            os.symlink(os.path.join(self.ui, "index.html"),
                       os.path.join(self.ui, "link.html"))
        except OSError as exc:
            self.skipTest("SKIP: 无法创建软链: " + str(exc))
        r = run_bash([MAKE_UI_SUMS, self.ui, self.out])
        self.assertEqual(r.returncode, 1, "软链必须判 1：" + r.stdout + r.stderr)
        self.assertIn("symlink", (r.stdout + r.stderr).lower())
        self.assertFalse(os.path.exists(self.out), "拒绝时不得留下清单")

    def test_refuses_non_regular_entry(self):
        fifo = os.path.join(self.ui, "pipe")
        try:
            os.mkfifo(fifo)
        except (AttributeError, OSError) as exc:
            self.skipTest("SKIP: 无法创建 fifo: " + str(exc))
        r = run_bash([MAKE_UI_SUMS, self.ui, self.out])
        self.assertEqual(r.returncode, 1, "非普通文件必须判 1：" + r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.out), "拒绝时不得留下清单")

    def test_refuses_missing_index_and_symlinked_ui_dir(self):
        os.remove(os.path.join(self.ui, "index.html"))
        r = run_bash([MAKE_UI_SUMS, self.ui, self.out])
        self.assertEqual(r.returncode, 2, "缺 index.html 是用法错误：" + r.stdout + r.stderr)
        self.assertIn("index.html", r.stdout + r.stderr)

        other = os.path.join(self.tmp, "other")
        make_ui_tree(other)
        link = os.path.join(self.tmp, "dist-link")
        try:
            os.symlink(other, link)
        except OSError as exc:
            self.skipTest("SKIP: 无法创建软链: " + str(exc))
        out2 = os.path.join(self.tmp, "ui2-SHA256SUMS")
        r = run_bash([MAKE_UI_SUMS, link, out2])
        self.assertEqual(r.returncode, 2, "ui 目录本身是软链必须拒绝：" + r.stdout + r.stderr)
        self.assertFalse(os.path.exists(out2))

    def test_refuses_usage_errors(self):
        r = run_bash([MAKE_UI_SUMS])
        self.assertEqual(r.returncode, 2)
        r = run_bash([MAKE_UI_SUMS, self.ui, os.path.join(self.tmp, "no-such-dir", "sums")])
        self.assertEqual(r.returncode, 2, "输出目录不存在是用法错误：" + r.stdout + r.stderr)


class ManifestThirdItemTest(unittest.TestCase):
    """manifest 收 ui-SHA256SUMS 作第三件（不改 CLI 形态）。"""

    def setUp(self):
        if not BASH:
            self.skipTest("SKIP: 没有 bash")
        if not SHA256SUM:
            self.skipTest("SKIP: 没有 sha256sum")
        self.tmp = tempfile.mkdtemp(prefix="ui-manifest-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_three_items_including_ui_sums(self):
        names = ["yiban-server", "yiban-cli", "ui-SHA256SUMS"]
        paths = []
        for name in names:
            p = os.path.join(self.tmp, name)
            write_file(p, "payload-" + name + "\n")
            paths.append(p)
        r = run_bash([MAKE_MANIFEST, "1.2.3", *paths])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        doc = json.loads(r.stdout)
        self.assertEqual(doc["version"], "1.2.3")
        self.assertEqual([f["name"] for f in doc["files"]], names)
        for entry in doc["files"]:
            got = run([SHA256SUM, "--", os.path.join(self.tmp, entry["name"])])
            self.assertEqual(entry["sha256"], got.stdout.split(" ", 1)[0])

    def test_ui_sums_name_is_still_reduced_to_basename(self):
        # 清单只认基名：传嵌套路径也收成 basename，重名即拒绝。
        nested = os.path.join(self.tmp, "staging", "ui-SHA256SUMS")
        write_file(nested, "hash  index.html\n")
        other = os.path.join(self.tmp, "ui-SHA256SUMS")
        write_file(other, "other\n")
        r = run_bash([MAKE_MANIFEST, "1.2.3", nested])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(json.loads(r.stdout)["files"][0]["name"], "ui-SHA256SUMS")
        r = run_bash([MAKE_MANIFEST, "1.2.3", nested, other])
        self.assertEqual(r.returncode, 2, "同名第三件必须拒绝：" + r.stdout + r.stderr)


class BuildReleaseUIWiringTest(unittest.TestCase):
    """行为：假 go + 临时 git 检出，真跑 build-release.sh 的两种调用形态。"""

    def setUp(self):
        if not BASH:
            self.skipTest("SKIP: 没有 bash")
        if not GIT:
            self.skipTest("SKIP: 没有 git，无法构造发布检出")
        if not os.path.isfile(BUILD_RELEASE):
            self.fail("缺少 server/scripts/build-release.sh")
        self.tmp = tempfile.mkdtemp(prefix="ui-build-release-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # 复制成 <root>/server/scripts/build-release.sh：脚本按自身位置上两级取 ROOT。
        scripts = os.path.join(self.tmp, "server", "scripts")
        os.makedirs(scripts)
        self.script = os.path.join(scripts, "build-release.sh")
        shutil.copy2(BUILD_RELEASE, self.script)
        # 发布脚本同目录依赖：build-release.sh 按 ROOT 调 release/make-ui-sums.sh。
        release = os.path.join(self.tmp, "scripts", "release")
        os.makedirs(release)
        shutil.copy2(MAKE_UI_SUMS, os.path.join(release, "make-ui-sums.sh"))
        write_file(os.path.join(self.tmp, "VERSION"), "1.2.3\n")
        init = run([GIT, "-C", self.tmp, "init", "-q"])
        self.assertEqual(init.returncode, 0, init.stderr)
        commit = run([GIT, "-C", self.tmp, "-c", "user.name=test",
                      "-c", "user.email=test@example.invalid",
                      "commit", "--allow-empty", "-q", "-m", "init"])
        self.assertEqual(commit.returncode, 0, commit.stderr)
        # 假 go：解析 -o，落一个占位产物；真构建不在本门范围内。
        bindir = os.path.join(self.tmp, "bin")
        write_file(os.path.join(bindir, "go"),
                   "#!/usr/bin/env bash\n"
                   "out=\"\"\n"
                   "while [ \"$#\" -gt 0 ]; do\n"
                   "  case \"$1\" in -o) out=\"$2\"; shift 2 ;; *) shift ;; esac\n"
                   "done\n"
                   "[ -n \"$out\" ] || { echo 'fake go: missing -o' >&2; exit 1; }\n"
                   "printf 'fake-binary\\n' > \"$out\"\n",
                   0o755)
        self.env = {"PATH": bindir + os.pathsep + os.environ.get("PATH", "")}

    def test_old_single_outdir_call_still_works(self):
        out = os.path.join(self.tmp, "out-old")
        r = run_bash([self.script, out], env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.isfile(os.path.join(out, "yiban-server")))
        self.assertTrue(os.path.isfile(os.path.join(out, "yiban-cli")))
        self.assertFalse(os.path.exists(os.path.join(out, "ui-SHA256SUMS")),
                         "没给 --ui-dir 不得凭空造 UI 清单")

    def test_ui_dir_generates_checksums_beside_binaries_and_prints_three_item_handoff(self):
        ui = os.path.join(self.tmp, "ui-dist")
        make_ui_tree(ui, "signed-ui")
        out = os.path.join(self.tmp, "out-ui")
        r = run_bash([self.script, out, "--ui-dir", ui], env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        sums = os.path.join(out, "ui-SHA256SUMS")
        self.assertTrue(os.path.isfile(sums), "ui-SHA256SUMS 必须与二进制同处发布 staging")
        self.assertFalse(os.path.exists(os.path.join(ui, "ui-SHA256SUMS")),
                         "清单绝不能写进 ui/dist 本身")
        with open(sums, "r", encoding="utf-8") as f:
            body = f.read()
        self.assertIn("index.html", body)
        check = run([SHA256SUM, "-c", "--", sums], cwd=ui)
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        # 构建链把第三件交给 make-manifest.sh：命令里必须点名三件。
        self.assertIn("make-manifest.sh", r.stdout, "必须打印 manifest 交接命令")
        handoff = [ln for ln in r.stdout.splitlines() if "make-manifest.sh" in ln]
        self.assertTrue(handoff)
        self.assertIn("ui-SHA256SUMS", handoff[-1])
        self.assertIn("yiban-server", handoff[-1])
        self.assertIn("yiban-cli", handoff[-1])

    def test_ui_dir_env_fallback_and_usage_errors(self):
        ui = os.path.join(self.tmp, "ui-env")
        make_ui_tree(ui, "env-ui")
        out = os.path.join(self.tmp, "out-env")
        env = dict(self.env)
        env["YIBAN_UI_DIST"] = ui
        r = run_bash([self.script, out], env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.isfile(os.path.join(out, "ui-SHA256SUMS")))

        bad = os.path.join(self.tmp, "ui-empty")
        os.makedirs(bad)
        r = run_bash([self.script, os.path.join(self.tmp, "out-bad"), "--ui-dir", bad],
                     env=self.env)
        self.assertEqual(r.returncode, 2, "缺 index.html 必须判用法错误：" + r.stdout + r.stderr)
        self.assertIn("index.html", r.stdout + r.stderr)
        r = run_bash([self.script, os.path.join(self.tmp, "out-bad"), "--ui-dir"],
                     env=self.env)
        self.assertEqual(r.returncode, 2, "缺值必须判用法错误：" + r.stdout + r.stderr)

    def test_bad_ui_dir_fails_and_leaves_out_untouched(self):
        # 先成功构建一次，拿到可对照的旧产物。
        ui = os.path.join(self.tmp, "ui-good")
        make_ui_tree(ui, "good")
        out = os.path.join(self.tmp, "out-atomic")
        r = run_bash([self.script, out, "--ui-dir", ui], env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(os.path.join(out, "yiban-server"), "rb") as f:
            server_before = f.read()
        with open(os.path.join(out, "ui-SHA256SUMS"), "rb") as f:
            sums_before = f.read()

        # 坏 --ui-dir 必须零写入，旧产物原样保留。
        bad = os.path.join(self.tmp, "ui-bad")
        os.makedirs(bad)
        r = run_bash([self.script, out, "--ui-dir", bad], env=self.env)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        with open(os.path.join(out, "yiban-server"), "rb") as f:
            self.assertEqual(f.read(), server_before)
        with open(os.path.join(out, "ui-SHA256SUMS"), "rb") as f:
            self.assertEqual(f.read(), sums_before)
        self.assertEqual(
            [n for n in os.listdir(out) if n.startswith(".build-release.")], [],
            "失败必须清掉 staging，不得留半成品目录")

        # 全新 outdir 也不能先落二进制再失败。
        fresh = os.path.join(self.tmp, "out-fresh-bad")
        r = run_bash([self.script, fresh, "--ui-dir", bad], env=self.env)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(os.listdir(fresh) if os.path.isdir(fresh) else [], [])

        # 软链在 UI 校验阶段才被发现：staging 已经建立，trap 必须清干净。
        linked = os.path.join(self.tmp, "ui-linked")
        make_ui_tree(linked, "linked")
        os.symlink("index.html", os.path.join(linked, "alias.html"))
        fresh2 = os.path.join(self.tmp, "out-fresh-link")
        r = run_bash([self.script, fresh2, "--ui-dir", linked], env=self.env)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(os.listdir(fresh2) if os.path.isdir(fresh2) else [], [],
                         "清单失败也必须清掉 staging")

    def test_no_ui_build_removes_stale_ui_checksums(self):
        ui = os.path.join(self.tmp, "ui-stale")
        make_ui_tree(ui, "stale")
        out = os.path.join(self.tmp, "out-stale")
        r = run_bash([self.script, out, "--ui-dir", ui], env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.isfile(os.path.join(out, "ui-SHA256SUMS")))
        r = run_bash([self.script, out], env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.isfile(os.path.join(out, "ui-SHA256SUMS")),
                         "无 UI 构建必须清掉上次的清单，避免混进新 manifest")


class UpdateShUIWiringTest(unittest.TestCase):
    """静态：update.sh 的 UI 校验接线（行为由 Go 干跑 e2e 证）。"""

    def test_parses_ui_checksums_and_defaults_beside_manifest(self):
        text = read_repo("deploy/baremetal/update.sh")
        for needle in (
            "--ui-checksums",
            'UI_CHECKSUMS="$(dirname "$MANIFEST")/ui-SHA256SUMS"',
            'SNAP_UI_SUMS',
            'cp -R "$SNAP_UI/." "$UI_STAGE/"',
            "sha256sum -c",
            "manifest lists ui-SHA256SUMS but --ui-dir was not given",
            "manifest has no ui-SHA256SUMS entry",
            "ui verification ok",
        ):
            self.assertTrue(needle in text, "update.sh 缺 UI 签名链接线：" + needle)

    def test_sums_are_snapshot_before_verification_and_activation_uses_snapshot(self):
        text = read_repo("deploy/baremetal/update.sh")
        # 快照必须早于停服，激活必须只用快照（TOCTOU）。
        self.assertTrue('SNAP_UI="$STAGE/ui"' in text, "缺 UI 快照")
        snap = text.index('SNAP_UI="$STAGE/ui"')
        stop = text.index('PHASE="stop"')
        self.assertLess(snap, stop, "UI 快照必须发生在停服之前")
        self.assertTrue('cp -R "$SNAP_UI/." "$UI_STAGE/"' in text, "激活必须从快照复制")
        self.assertLess(text.index('cp -R "$SNAP_UI/." "$UI_STAGE/"'),
                        text.index('    UI_ACTIVATED=1'), "激活复制必须早于 UI_ACTIVATED")

    def test_tooling_gate_registers_new_shell(self):
        gate = read_repo("scripts/tooling-gate.sh")
        self.assertIn("scripts/release/make-ui-sums.sh", gate,
                      "新增 shell 必须进 tooling-gate 的 SHELL_FILES（否则 shellcheck 面缺失）")

    def test_docs_describe_ui_signing_chain(self):
        signing = read_repo("docs/dev/release-signing.md")
        for needle in ("ui-SHA256SUMS", "--ui-checksums", "make-ui-sums.sh", "index.html"):
            self.assertIn(needle, signing, "签名链文档缺 UI 说明：" + needle)
        release = read_repo("deploy/release.md")
        for needle in ("ui-SHA256SUMS", "make-ui-sums.sh"):
            self.assertIn(needle, release, "发布手册缺 UI 签名链：" + needle)


class UISigningE2EWiringTest(unittest.TestCase):
    """④ UI 签名链的本地 e2e 必须能单跑，且接进裸机演练与文档。"""

    def test_script_exists_and_is_executable(self):
        self.assertTrue(os.path.isfile(UI_SIGNING_E2E), "缺 scripts/e2e/ui-signing-e2e.sh")
        self.assertTrue(os.access(UI_SIGNING_E2E, os.X_OK), "e2e 脚本必须可执行")

    def test_each_case_runs_alone_without_docker(self):
        if not BASH:
            self.skipTest("no bash")
        for case in ("success", "tampered", "extra", "no-ui-entry"):
            with self.subTest(case=case):
                proc = run_bash([UI_SIGNING_E2E, case], cwd=BASE)
                self.assertEqual(
                    proc.returncode, 0,
                    "单跑用例 %s 必须绿：rc=%s\nstdout:%s\nstderr:%s"
                    % (case, proc.returncode, proc.stdout[-2000:], proc.stderr[-2000:]),
                )
                self.assertIn("passed=1 failed=0", proc.stdout)

    def test_baremetal_drill_signs_ui_list_and_runs_the_local_cases(self):
        text = read_repo("scripts/e2e/deploy-baremetal-e2e.sh")
        self.assertTrue(
            'make-ui-sums.sh" "$UI_OUT" "$SIGN_DIR/ui-SHA256SUMS"' in text,
            "B4b 必须为 UI 产物生成清单",
        )
        self.assertTrue(
            '"$BIN_OUT/yiban-server" "$BIN_OUT/yiban-cli" "$SIGN_DIR/ui-SHA256SUMS"' in text,
            "B4b 的 manifest 必须收 UI 清单作第三件",
        )
        self.assertTrue(
            '"$REPO/scripts/e2e/ui-signing-e2e.sh"' in text,
            "裸机演练必须跑 UI 签名链本地用例（失败即红）",
        )

    def test_docs_and_tooling_gate_cover_the_new_e2e(self):
        doc = read_repo("docs/dev/deploy-e2e.md")
        for needle in ("ui-signing-e2e.sh", "B4d"):
            self.assertIn(needle, doc, "e2e 文档缺 UI 签名链说明：" + needle)
        gate = read_repo("scripts/tooling-gate.sh")
        self.assertIn("scripts/e2e/ui-signing-e2e.sh", gate,
                      "新增 e2e 脚本必须进 tooling-gate 的 SHELL_FILES")


if __name__ == "__main__":
    unittest.main()
