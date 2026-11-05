# -*- coding: utf-8 -*-
"""Go 线版本接线静态门（2026-11-05 版本批）。

标签：J · 运维：发布
覆盖：仓库根 VERSION 是 Go 线产品版本单一来源（单行 semver）；build-release.sh
    真把 VERSION 注入 buildinfo 三元组；release-check 的 go-version 项真调
    build-release.sh；Dockerfile 三处 -X 注入；compose build.args 传三个构建
    参数；deploy/container/build.sh 从 VERSION 取值。
对应实现：VERSION、server/scripts/build-release.sh、scripts/release-check.sh
    （check_go_version）、deploy/container/Dockerfile、deploy/container/docker-compose.yml、
    deploy/container/build.sh、server/internal/buildinfo/buildinfo.go。
关键断言：执行面由 Go 门（server/cmd/cli/build_release_e2e_test.go）真构建证明；
    本文件证静态接线不漂移——任何一处把注入改成字面量、删掉调用或换掉变量
    来源，这里立刻红。每条断言配突变（内存临时副本改坏即红，真文件零写入）。
依赖：全程静态（读仓库文本 + 内存突变），不联网、不编译、不起构建、不写文件。
用法（项目根目录）：python -m pytest tests/test_build_version_wiring.py -q
"""
import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
# -X <pkg>.<identifier>=：pkg 段必须指向 buildinfo 包（大小写不敏感，
# 覆盖脚本里的 ${BUILDINFO_PKG} 变量写法与 Dockerfile 的全路径写法）。
INJECT_RE = re.compile(r"-X\s+([^\s=]+)\.(version|commit|buildTime)=")


def read_repo_file(rel):
    with open(os.path.join(BASE, rel), "r", encoding="utf-8") as f:
        return f.read()


def assert_version_file(text):
    """单行、换行结尾、严格 X.Y.Z。"""
    assert text.endswith("\n"), "VERSION 必须以换行结尾"
    lines = text.splitlines()
    assert len(lines) == 1, f"VERSION 必须恰好一行，实际 {len(lines)} 行"
    assert SEMVER_RE.match(lines[0]), f"VERSION 不是 X.Y.Z semver: {lines[0]!r}"


def assert_build_release_script(script_text):
    """三个 -X buildinfo.* 注入齐全，且 version 值是变量引用（来自 VERSION 读入），不是字面量。"""
    found = set()
    for pkg, ident in INJECT_RE.findall(script_text):
        if "buildinfo" in pkg.lower():
            found.add(ident)
    missing = {"version", "commit", "buildTime"} - found
    assert not missing, f"build-release.sh 缺 -X buildinfo 注入符号: {sorted(missing)}"
    m = re.search(r"-X\s+[^\s=]+\.version=([^\s\\\"']+)", script_text)
    assert m, "build-release.sh 无法解析 buildinfo.version 的取值"
    value = m.group(1)
    assert value.startswith("$"), (
        f"build-release.sh buildinfo.version 注入的是字面量 {value!r}；必须来自仓库根 VERSION 读入的变量"
    )
    assert re.search(r"\$ROOT/VERSION\b", script_text), (
        "build-release.sh 没有读仓库根 VERSION（单一来源断了）：找不到 $ROOT/VERSION 引用"
    )


def assert_release_check_calls_builder(script_text):
    """go-version 项必须登记且真调 server/scripts/build-release.sh（不是只写在注释里）。"""
    assert re.search(r"^ITEMS=.*\bgo-version\b", script_text, re.M), (
        "release-check.sh 的 ITEMS 未登记 go-version"
    )
    assert re.search(r"^check_go_version\(\)", script_text, re.M), (
        "release-check.sh 缺 check_go_version 实现"
    )
    assert "server/scripts/build-release.sh" in script_text.replace("\\", "/"), (
        "release-check.sh 没有引用 server/scripts/build-release.sh"
    )
    assert re.search(r'bash\s+"\$builder"\s+"\$out"', script_text), (
        'release-check.sh 的 check_go_version 没有真执行 builder（bash "$builder" "$out"）'
    )


def assert_dockerfile_injections(dockerfile_text):
    """镜像构建必须注入同一组符号，值来自三个构建参数。"""
    for ident, arg in (("version", "BUILD_VERSION"),
                       ("commit", "BUILD_COMMIT"),
                       ("buildTime", "BUILD_TIME")):
        pattern = r"-X\s+\S*buildinfo\.%s=\$\{%s\}" % (ident, arg)
        assert re.search(pattern, dockerfile_text), (
            f"Dockerfile 缺 -X buildinfo.{ident}=${{{arg}}} 注入"
        )


def assert_compose_build_args(compose_text):
    """build.args 必须把三个构建参数透传给 Dockerfile（缺省值是 Dockerfile 的显性占位符）。"""
    m = re.search(r"^ {6}args:\n((?: {8}[^\n]*\n)+)", compose_text, re.M)
    assert m, "docker-compose.yml 找不到 build.args 段"
    args_block = m.group(1)
    for arg, fallback in (("BUILD_VERSION", "dev"),
                          ("BUILD_COMMIT", "unknown"),
                          ("BUILD_TIME", "unknown")):
        assert re.search(rf"{arg}: \$\{{{arg}:-{fallback}\}}", args_block), (
            f"docker-compose.yml build.args 缺 {arg}: ${{{arg}:-{fallback}}}"
        )


def assert_docker_build_sh(script_text):
    """容器构建入口必须从 VERSION 取版本并传三个 build-arg。"""
    assert re.search(r"\$ROOT/VERSION\b", script_text), (
        "deploy/container/build.sh 没有读仓库根 VERSION"
    )
    for arg in ("BUILD_VERSION", "BUILD_COMMIT", "BUILD_TIME"):
        # $ARG 与 ${ARG} 两种 shell 写法都认；字面量（无 $）一律红。
        assert re.search(rf'--build-arg {arg}="\$\{{?{arg}\}}?"', script_text), (
            f"deploy/container/build.sh 缺 --build-arg {arg} 透传"
        )
    assert re.search(r'^export BUILD_VERSION="\$ver"$', script_text, re.M), (
        "deploy/container/build.sh 的 BUILD_VERSION 没有来自 VERSION 读入的 ver 变量"
    )


class TestBuildVersionWiring(unittest.TestCase):
    """真文件全绿 + 每条断言的突变（内存副本）必红；真文件零写入。"""

    # ---------- 真文件断言 ----------

    def test_version_file_single_line_semver(self):
        assert_version_file(read_repo_file("VERSION"))

    def test_build_release_script_injects_version_from_version_file(self):
        assert_build_release_script(read_repo_file("server/scripts/build-release.sh"))

    def test_release_check_go_version_calls_builder(self):
        assert_release_check_calls_builder(read_repo_file("scripts/release-check.sh"))

    def test_dockerfile_injects_buildinfo(self):
        assert_dockerfile_injections(read_repo_file("deploy/container/Dockerfile"))

    def test_compose_build_args_pass_build_version(self):
        assert_compose_build_args(read_repo_file("deploy/container/docker-compose.yml"))

    def test_docker_build_sh_reads_version(self):
        assert_docker_build_sh(read_repo_file("deploy/container/build.sh"))

    # ---------- 突变（内存副本改坏即红；真文件零写入） ----------

    def test_mutation_version_file_red(self):
        for bad in ("0.7\n", "abc\n", "0.7.0\ndev\n", "0.7.0"):
            with self.assertRaises(AssertionError, msg=f"突变未红: {bad!r}"):
                assert_version_file(bad)

    def test_mutation_build_release_script_red(self):
        good = read_repo_file("server/scripts/build-release.sh")
        # 突变 A：version 注入改成字面量（版本值不再来自 VERSION）。
        mutant = good.replace("-X ${BUILDINFO_PKG}.version=${ver}", "-X ${BUILDINFO_PKG}.version=0.0.0")
        self.assertNotEqual(mutant, good, "突变未生效：源文本里找不到注入行")
        with self.assertRaises(AssertionError):
            assert_build_release_script(mutant)
        # 突变 B：整行删掉 version 注入。
        mutant_b = good.replace("-X ${BUILDINFO_PKG}.version=${ver} \\\n", "")
        self.assertNotEqual(mutant_b, good)
        with self.assertRaises(AssertionError):
            assert_build_release_script(mutant_b)

    def test_mutation_release_check_red(self):
        good = read_repo_file("scripts/release-check.sh")
        # 突变 A：ITEMS 里摘掉 go-version（门被下线）。
        mutant = good.replace('ITEMS="version go-version', 'ITEMS="version')
        self.assertNotEqual(mutant, good, "突变未生效：源文本里找不到 ITEMS 行")
        with self.assertRaises(AssertionError):
            assert_release_check_calls_builder(mutant)
        # 突变 B：调用点换成不存在的旗标（接线断了）。
        mutant_b = good.replace('bash "$builder" "$out"', 'bash "$builder" --dry-run')
        self.assertNotEqual(mutant_b, good)
        with self.assertRaises(AssertionError):
            assert_release_check_calls_builder(mutant_b)

    def test_mutation_dockerfile_red(self):
        good = read_repo_file("deploy/container/Dockerfile")
        mutant = good.replace(
            "-X github.com/frostpolaris1/yiban-auto-sign/server/internal/buildinfo.version=${BUILD_VERSION}", "")
        self.assertNotEqual(mutant, good, "突变未生效：源文本里找不到注入行")
        with self.assertRaises(AssertionError):
            assert_dockerfile_injections(mutant)

    def test_mutation_compose_red(self):
        good = read_repo_file("deploy/container/docker-compose.yml")
        mutant = good.replace("        BUILD_VERSION: ${BUILD_VERSION:-dev}\n", "")
        self.assertNotEqual(mutant, good, "突变未生效：源文本里找不到 build.args 行")
        with self.assertRaises(AssertionError):
            assert_compose_build_args(mutant)

    def test_mutation_docker_build_sh_red(self):
        good = read_repo_file("deploy/container/build.sh")
        # 突变 A：版本硬编码（不再来自 VERSION）。
        mutant = good.replace('export BUILD_VERSION="$ver"', 'export BUILD_VERSION="9.9.9"')
        self.assertNotEqual(mutant, good, "突变未生效：源文本里找不到 export 行")
        with self.assertRaises(AssertionError):
            assert_docker_build_sh(mutant)
        # 突变 B：build-arg 传字面量。
        mutant_b = good.replace('--build-arg BUILD_VERSION="$BUILD_VERSION"', '--build-arg BUILD_VERSION="dev"')
        self.assertNotEqual(mutant_b, good)
        with self.assertRaises(AssertionError):
            assert_docker_build_sh(mutant_b)

    def test_wiring_files_present(self):
        """接线文件存在性兜底：任何一份被删，本文件的真文件断言会红，这里给点名。"""
        for rel in ("VERSION", "server/scripts/build-release.sh", "scripts/release-check.sh",
                    "deploy/container/Dockerfile", "deploy/container/docker-compose.yml",
                    "deploy/container/build.sh"):
            self.assertTrue(os.path.isfile(os.path.join(BASE, rel)), f"接线文件缺失: {rel}")


if __name__ == "__main__":
    unittest.main()
