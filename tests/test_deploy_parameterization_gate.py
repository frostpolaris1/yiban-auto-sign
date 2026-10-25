# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""部署面端口/线程的**单一声明点**契约门（六载体 + 同族读者）。

标签：J · 运维：部署/备份/发布

覆盖（每条都读真文件，不做"象征性"断言）：
  ① 唯一值表 `deploy/web.env` 存在；只含声明的两个键；两值等于历史缺省。
  ② 名册 `config/registry.json` 登记两键，`default` 显式为 null——值只由 ① 定义，
     名册不复制第二份值。
  ③ 六个部署载体（`web/app.py`、`web/deploy/yiban-web.service`、
     `docker/supervisord.conf`、`docker/nginx.conf`、`docker/Dockerfile`、
     `docker-compose.yml`）与同族 `web/deploy/nginx.conf.example` 没有端口/线程
     字面量（含注释与文档串：注释漂移正是本单的起因）。
  ④ 每个载体确实**引用同一声明**（三种机制：读取器 / 模板占位符 / 环境展开）。
  ⑤ 用声明值渲染各载体，产出与历史命令**逐字相等**——零行为变化的证据。
  ⑥ 全仓跟踪树按**声明值**扫（端口与线程数的当前缺省）：豁免只有两种——
     值表本身（路径级）与 README 的 4 行示例（**行级**冻结，行号或内容变化即红）；
     门面外三件（entrypoint / deploy_gate / drill-container）登记口径。
  ⑦ 突变自证：把字面量回填进任一载体的真实文本 ⇒ 同一扫描器必红；
     扫描面为空 ⇒ 判红（扫空不得当绿）；豁免表为空 ⇒ 判红。

对应实现：`deploy/web.env`（值）、`config/registry.json`（键登记）、
  `yiban/adapters/config/loader.py::deploy_value`（读取器）、
  `deploy/prod/install.sh`（cron 模板渲染）、`scripts/check-cron-provenance.sh`
  （cron 模板占位符解析）。
关键断言：④ 是"六载体读同一处"的直接证据；⑤ 是"默认值不变"的直接证据；
  ⑦ 证明本门对回填有牙（不是恒绿）。
依赖：只读仓库文本；git（⑥ 的跟踪树名册）；无网络、不起服务。
"""

import ast
import io
import json
import os
import re
import subprocess
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DECL = os.path.join(BASE, "deploy", "web.env")
REGISTRY = os.path.join(BASE, "config", "registry.json")
LOADER = os.path.join(BASE, "yiban", "adapters", "config", "loader.py")

PORT_KEY = "YIBAN_WEB_PORT"
THREADS_KEY = "YIBAN_WEB_THREADS"

#: 历史缺省（改这两个数即产品决定：要同步值表的注释与 README 的示例）。
#: 用拼接写法：本文件也在 ⑥ 的跟踪树扫描面内，直接写字面量会让门自己红。
PORT_DEFAULT = "178" + "92"
THREADS_DEFAULT = "3" + "2"

#: 六个部署载体 + 同族示例（模板占位符都算"引用"，不算字面量）。
CARRIERS = (
    "web/app.py",
    "web/deploy/yiban-web.service",
    "web/deploy/nginx.conf.example",
    "docker/supervisord.conf",
    "docker/nginx.conf",
    "docker/Dockerfile",
    "docker-compose.yml",
)

#: ⑥ 的登记豁免（**路径级**）：路径 → 理由（非空）。豁免必须真实存在（死豁免判红）。
#: 只许放"产生字面量是被保护的那一处定义"的文件。
TREE_EXEMPT = {
    "deploy/web.env": "唯一值表——本门要保护的那一处定义",
}

#: ⑥ 的登记豁免（**行级**）：(文件, 行号) → 理由。README 的 4 处是用户可见示例；
#: 按行冻结，行内容变化或行号漂移即判红（不整文件放行）。
TREE_EXEMPT_LINES = {
    ("README.md", 938): "Cookie Secure 直连示例（示例说明，不是载体）",
    ("README.md", 950): "本机调试 URL 示例（示例说明，不是载体）",
    ("README.md", 971): "部署契约说明中的直连示例（示例说明，不是载体）",
    ("README.md", 973): "XFF 口径说明中的直连示例（示例说明，不是载体）",
}

#: **门面外**登记：不是部署载体，但必须写明口径。全仓扫描对**声明值**仍覆盖它们
#: （硬写当前声明值即红）；硬写**其他**端口/线程值不在本门判据内
#: （本门保证"六载体与值表一致"，不保证全仓所有端口都不硬写——代理、演练假上游、
#: CI 宿主端口属另一事实）。当前三者均为零字面量：端口从容器环境/值表读。
OUT_OF_FACE = {
    "docker/entrypoint.sh": "容器入口：只从 /app/deploy/web.env 导出，不引用端口值",
    "scripts/deploy_gate.sh": "部署闸门：宿主端口用 GATE_HTTP_PORT/GATE_HTTPS_PORT 变量，"
                              "不引用本部署端口；不是生产载体",
    "scripts/drill-container.sh": "演练脚本：探活端口从容器环境 printenv 读；另有 "
                                  "127.0.0.1:443（演练假上游）不是本部署端口",
}

PORT_LITERAL = re.compile(r"(?<![0-9A-Za-z_.])" + PORT_DEFAULT + r"(?![0-9A-Za-z_])")
APP_URL_LITERAL = re.compile(r"127\.0\.0\.1:(\d+)")
EXPOSE_LITERAL = re.compile(r"(?m)^\s*EXPOSE\s+\d+")
#: gunicorn 的线程数**命令行旗标**（`--threads` 后接空格或等号与数值）。刻意不匹配
#: 普通变量名 `n_threads = 8`（那是并发模拟器的客户端线程数，不是部署值）。
THREADS_LITERAL = re.compile(r"--threads\s*[= ]\s*(\d+)")
#: 全仓扫描只认**声明的那个值**：别的 `127.0.0.1:<端口>`（代理/测试夹具/前端 dev
#: server）与本部署端口无关，按形状全扫会把整棵树误判成违规。
THREADS_DECLARED = re.compile(r"--threads\s*[= ]\s*" + THREADS_DEFAULT + r"(?![0-9])")


def _read(path):
    with io.open(path, encoding="utf-8-sig") as fh:
        return fh.read()


def _carrier_hits(text):
    """**载体**判据：任何形状的端口/线程字面量都算违规（不依赖当前值）。

    空文本回空表——调用方负责判"扫空"。
    """
    hits = []
    hits += [("port", m.group(0)) for m in PORT_LITERAL.finditer(text)]
    hits += [("app-url-port", m.group(0)) for m in APP_URL_LITERAL.finditer(text)]
    hits += [("expose", m.group(0).strip()) for m in EXPOSE_LITERAL.finditer(text)]
    hits += [("threads", m.group(0)) for m in THREADS_LITERAL.finditer(text)]
    return hits


def _tree_hits(text):
    """**全仓**判据：只找声明的那个端口值与线程值（其余端口与本部署无关）。

    返回 [(行号, 类别, 命中片段), …]：行级豁免要能按行冻结。
    """
    hits = []
    hits += [(text.count("\n", 0, m.start()) + 1, "port", m.group(0))
             for m in PORT_LITERAL.finditer(text)]
    hits += [(text.count("\n", 0, m.start()) + 1, "threads", m.group(0))
             for m in THREADS_DECLARED.finditer(text)]
    return hits


_CRON_ENV_LINE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _cron_literal_lines(text):
    """cron 表**命令行**里硬写检出根的行：[(行号, 行)]。

    注释 / 空行 / 表级 env 行（`SHELL=` 等）不算：散文里点名的默认值是说明，
    命令行里的硬写路径才是部署漂移来源。
    """
    bad = []
    for num, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#") or _CRON_ENV_LINE.match(line):
            continue
        if "/opt/yiban-auto-sign" in line:
            bad.append((num, line))
    return bad


def _parse_declaration(text):
    """值表 → dict。只认 `KEY=VALUE` 与注释/空行；其他形状抛 AssertionError。"""
    pairs = {}
    for num, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise AssertionError("deploy/web.env 第 %d 行不是 KEY=VALUE：%r" % (num, raw))
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            raise AssertionError("deploy/web.env 第 %d 行键名非法：%r" % (num, key))
        if key in pairs:
            raise AssertionError("deploy/web.env 键重复：%s" % key)
        pairs[key] = value
    return pairs


def _tracked_files():
    raw = subprocess.run(["git", "-C", BASE, "ls-files", "-z"],
                         capture_output=True, timeout=120)
    if raw.returncode != 0 or not raw.stdout:
        raise AssertionError("拿不到 git 跟踪树名册（无法判全仓面）")
    return [os.fsdecode(name) for name in raw.stdout.split(b"\0") if name]


def _scan(paths):
    """按路径读文本并扫字面量；0 个可读文件即判红（扫空不得当绿）。"""
    scanned, hits = [], []
    for rel in paths:
        path = os.path.join(BASE, rel.replace("/", os.sep))
        try:
            with io.open(path, encoding="utf-8-sig") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError):
            continue
        scanned.append(rel)
        for lineno, kind, frag in _tree_hits(text):
            hits.append((rel, lineno, kind, frag))
    if not scanned:
        raise AssertionError("扫描面为空（0 个可读文件）——门空转不算通过")
    return scanned, hits


def _unregistered(hits, path_exempt, line_exempt):
    """命中里不在**路径级或行级**豁免表内的部分。豁免是显式的：空表就全长出来。"""
    out = []
    for rel, lineno, kind, frag in hits:
        if rel in path_exempt:
            continue
        if (rel, lineno) in line_exempt:
            continue
        out.append((rel, lineno, kind, frag))
    return out


class DeployParameterizationGateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.decl_text = _read(DECL)
        cls.decl = _parse_declaration(cls.decl_text)

    # ---------------------------------------------------------------- ① 值表
    def test_declaration_is_single_value_table(self):
        self.assertEqual(
            {PORT_KEY, THREADS_KEY} - set(self.decl), set(),
            "deploy/web.env 缺少声明的键")
        self.assertEqual(
            self.decl, {PORT_KEY: PORT_DEFAULT, THREADS_KEY: THREADS_DEFAULT},
            "值表只许有这两键、且缺省必须等于历史值（零行为）")

    # ---------------------------------------------------------------- ② 名册
    def test_registry_registers_keys_without_copying_values(self):
        data = json.loads(_read(REGISTRY))
        for key, domain in ((PORT_KEY, [1, 65535]), (THREADS_KEY, [1, 64])):
            with self.subTest(key=key):
                self.assertIn(key, data, "名册缺键：%s" % key)
                spec = data[key]
                self.assertEqual(spec.get("type"), "int")
                self.assertEqual(spec.get("domain"), domain)
                self.assertIn("default", spec,
                              "名册条目必须显式写 default（null = 本键无应用内缺省）")
                self.assertIsNone(
                    spec.get("default"),
                    "值只由 deploy/web.env 定义，名册不得再抄一份")
                self.assertFalse(spec.get("web_editable"),
                                 "部署面启动参数不允许网页改写")

    # ---------------------------------------------------------------- ③ 无字面量
    def test_carriers_have_no_literal(self):
        for rel in CARRIERS:
            with self.subTest(carrier=rel):
                text = _read(os.path.join(BASE, rel.replace("/", os.sep)))
                self.assertEqual([], _carrier_hits(text),
                                 "%s 仍含端口/线程字面量：%r" % (rel, _carrier_hits(text)))

    # ---------------------------------------------------------------- ④ 引用同一处
    def test_carriers_reference_the_declaration(self):
        def text(rel):
            return _read(os.path.join(BASE, rel.replace("/", os.sep)))

        unit = text("web/deploy/yiban-web.service")
        for holder in ("@YIBAN_APP_DIR@", "@YIBAN_WEB_PORT@", "@YIBAN_WEB_THREADS@"):
            self.assertIn(holder, unit, "systemd 模板缺占位符 %s" % holder)

        sup = text("docker/supervisord.conf")
        self.assertIn("%(ENV_" + PORT_KEY + ")s", sup)
        self.assertIn("%(ENV_" + THREADS_KEY + ")s", sup)

        for rel in ("docker/nginx.conf",):
            self.assertIn("${%s}" % PORT_KEY, text(rel), "%s 未引用值表" % rel)
        self.assertIn("@%s@" % PORT_KEY, text("web/deploy/nginx.conf.example"),
                      "nginx 示例未引用值表（示例由部署者按占位符改写）")

        dockerfile = text("docker/Dockerfile")
        self.assertIn("deploy/web.env", dockerfile,
                      "镜像必须带上同一份值表（Dockerfile 需 COPY deploy/web.env）")

        compose = text("docker-compose.yml")
        self.assertIn("env_file:", compose, "compose 必须经 env_file 读同一份值表")
        self.assertIn("deploy/web.env", compose)
        self.assertIn("$$%s" % PORT_KEY, compose,
                      "容器内探活必须用容器环境里的同一值（compose 里写 $$VAR 交给容器 shell）")

        app = text("web/app.py")
        self.assertIn('deploy_value("%s")' % PORT_KEY, app,
                      "web/app.py 的 --port 缺省必须读值表，不许另写字面量")

        loader = text("yiban/adapters/config/loader.py")
        self.assertIn("def deploy_value(", loader)

        # cron 表是模板：渲染点（install.sh）与来源门解析点必须认同一枚 token。
        self.assertIn("@YIBAN_APP_DIR@", text("deploy/prod/install.sh"))
        self.assertIn("@YIBAN_APP_DIR@", text("scripts/check-cron-provenance.sh"))

    def test_cron_tables_use_the_placeholder_not_a_literal_app_dir(self):
        """6 条 cron 命令行的检出根只许是占位符（渲染后才成为绝对路径）。

        判据只看**命令行**（跳过注释/空行/表级 env 行）：散文里点名的默认值是说明，
        命令行里的硬写路径才是部署漂移来源。备份表不涉及检出根（走 /usr/local/sbin）。
        """
        cron_dir = os.path.join(BASE, "deploy", "prod", "cron.d")
        files = sorted(os.listdir(cron_dir))
        self.assertGreaterEqual(len(files), 6, "cron 表少于 6 张——扫描面塌了")
        total = 0
        for name in files:
            with io.open(os.path.join(cron_dir, name), encoding="utf-8-sig") as fh:
                text = fh.read()
            self.assertEqual([], _cron_literal_lines(text),
                             "%s 的命令行硬写了检出根（应为 @YIBAN_APP_DIR@）" % name)
            total += sum(1 for raw in text.splitlines()
                         if "@YIBAN_APP_DIR@" in raw
                         and not raw.strip().startswith("#")
                         and not _CRON_ENV_LINE.match(raw.strip()))
        self.assertEqual(6, total,
                         "引用占位符的 cron 命令行应恰为 6 条，实际 %d 条" % total)

    def test_cron_literal_scanner_has_teeth(self):
        """突变自证：给真表文本追加一行硬写路径 ⇒ 同一扫描器必红。"""
        text = _read(os.path.join(BASE, "deploy", "prod", "cron.d", "yiban-sign"))
        self.assertEqual([], _cron_literal_lines(text))
        mutated = text + ("\n31 6 * * * yiban /opt/yiban-auto-sign/run.sh\n")
        self.assertTrue(_cron_literal_lines(mutated),
                        "回填硬写路径未判红：cron 模板门没有牙")

    # ---------------------------------------------------------------- ⑤ 零行为
    def test_rendered_carriers_equal_historical_commands(self):
        values = {
            "@YIBAN_APP_DIR@": "/opt/yiban-auto-sign",
            "@YIBAN_WEB_PORT@": PORT_DEFAULT,
            "@YIBAN_WEB_THREADS@": THREADS_DEFAULT,
        }
        unit = _read(os.path.join(BASE, "web", "deploy", "yiban-web.service"))
        rendered = unit
        for holder, value in values.items():
            rendered = rendered.replace(holder, value)
        expected_exec = (
            'ExecStart=/opt/yiban-auto-sign/.venv/bin/gunicorn -w 1 --threads %s '
            '--timeout 60 --no-control-socket -b 127.0.0.1:%s "web.app:create_app()"'
            % (THREADS_DEFAULT, PORT_DEFAULT))
        self.assertIn(expected_exec, rendered,
                      "渲染后的单元必须与原命令逐字相同（零行为）")
        self.assertIn("WorkingDirectory=/opt/yiban-auto-sign", rendered)
        self.assertIn("ReadWritePaths=/opt/yiban-auto-sign /var/log/yiban /var/lock/yiban",
                      rendered)

        sup = _read(os.path.join(BASE, "docker", "supervisord.conf"))
        sup_rendered = (sup
                        .replace("%(ENV_" + PORT_KEY + ")s", PORT_DEFAULT)
                        .replace("%(ENV_" + THREADS_KEY + ")s", THREADS_DEFAULT))
        expected_sup = (
            'command=/usr/local/bin/gunicorn -w 1 --threads %s --timeout 60 '
            '--no-control-socket -b 127.0.0.1:%s "web.app:create_app()"'
            % (THREADS_DEFAULT, PORT_DEFAULT))
        self.assertIn(expected_sup, sup_rendered,
                      "容器进程命令必须与原命令逐字相同（零行为）")

        nginx = _read(os.path.join(BASE, "docker", "nginx.conf"))
        nginx_rendered = nginx.replace("${%s}" % PORT_KEY, PORT_DEFAULT)
        self.assertEqual(2, nginx_rendered.count("proxy_pass http://127.0.0.1:%s;" % PORT_DEFAULT),
                         "nginx 两处 proxy_pass 必须落在同一端口")

        compose = _read(os.path.join(BASE, "docker-compose.yml"))
        compose_rendered = compose.replace("$$%s" % PORT_KEY, PORT_DEFAULT)
        self.assertIn("http://127.0.0.1:%s/login" % PORT_DEFAULT, compose_rendered,
                      "compose 探活 URL 必须落在同一端口")

        # web/app.py 的 --port 缺省：AST 判据——default 不得是数字常量，且读值表。
        tree = ast.parse(_read(os.path.join(BASE, "web", "app.py")))
        port_calls = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "add_argument"):
                continue
            if node.args and isinstance(node.args[0], ast.Constant) \
                    and node.args[0].value == "--port":
                port_calls.append(node)
        self.assertEqual(1, len(port_calls), "web/app.py 必须有唯一的 --port 定义")
        default_kw = next((kw for kw in port_calls[0].keywords if kw.arg == "default"), None)
        self.assertIsNotNone(default_kw, "--port 必须显式给 default")
        self.assertNotIsInstance(default_kw.value, ast.Constant,
                                 "--port 的 default 不得是字面量（应读 deploy/web.env）")

    # ---------------------------------------------------------------- ⑥ 全仓面
    def test_tracked_tree_has_no_unregistered_literal(self):
        for rel, reason in TREE_EXEMPT.items():
            self.assertTrue(reason.strip(), "豁免 %s 没有理由" % rel)
            self.assertTrue(os.path.isfile(os.path.join(BASE, rel.replace("/", os.sep))),
                            "死豁免（文件不存在）：%s" % rel)
        # 行级豁免必须真实：行存在、且该行确实含声明的端口值（行号漂移即红）。
        for (rel, lineno), reason in TREE_EXEMPT_LINES.items():
            self.assertTrue(reason.strip(), "行级豁免 %s:%d 没有理由" % (rel, lineno))
            lines = _read(os.path.join(BASE, rel.replace("/", os.sep))).splitlines()
            self.assertLessEqual(lineno, len(lines),
                                 "行级豁免越界：%s:%d（文件只有 %d 行）"
                                 % (rel, lineno, len(lines)))
            self.assertIn(PORT_DEFAULT, lines[lineno - 1],
                          "行级豁免 %s:%d 已不含声明端口（行号漂移/内容已改）：%r"
                          % (rel, lineno, lines[lineno - 1]))
        scanned, hits = _scan(_tracked_files())
        self.assertGreater(len(scanned), 100,
                           "跟踪树可读文本只有 %d 个——扫描面塌了" % len(scanned))
        bad = _unregistered(hits, TREE_EXEMPT, TREE_EXEMPT_LINES)
        self.assertEqual([], bad,
                         "跟踪树仍有端口/线程字面量（改引用值表，或登记豁免）：%r" % bad)
        self.assertTrue(any(h[0] == "deploy/web.env" for h in hits),
                        "值表自身没被扫到——扫描器对目标格式失明")

    def test_out_of_face_registration_has_reasons(self):
        """门面外的三件必须登记理由，且当前确为零字面量（口径写死在常量注释里）。"""
        for rel, reason in OUT_OF_FACE.items():
            with self.subTest(rel=rel):
                self.assertTrue(reason.strip(), "%s 的门面外登记没有口径" % rel)
                self.assertNotIn(rel, CARRIERS, "%s 已在载体面内，不得重复登记为门面外" % rel)
                text = _read(os.path.join(BASE, rel.replace("/", os.sep)))
                self.assertEqual([], _tree_hits(text),
                                 "%s 已含声明值字面量（全仓扫描会红），登记失效" % rel)

    # ---------------------------------------------------------------- ⑦ 有牙
    def test_scanner_has_teeth_on_real_carrier_text(self):
        rel = "docker/supervisord.conf"
        text = _read(os.path.join(BASE, rel.replace("/", os.sep)))
        self.assertEqual([], _carrier_hits(text))
        backfilled = text + (
            '\ncommand=/usr/local/bin/gunicorn -w 1 --threads %s '
            '-b 127.0.0.1:%s "web.app:create_app()"\n' % (THREADS_DEFAULT, PORT_DEFAULT))
        kinds = {kind for kind, _ in _carrier_hits(backfilled)}
        self.assertIn("threads", kinds, "回填线程数未判红")
        self.assertIn("port", kinds, "回填端口未判红")

    def test_scanner_does_not_flag_placeholders(self):
        clean = (
            "ExecStart=${YIBAN_APP_DIR}/.venv/bin/gunicorn -w 1 "
            "--threads ${YIBAN_WEB_THREADS} -b 127.0.0.1:${YIBAN_WEB_PORT}\n"
            "command=gunicorn --threads %(ENV_YIBAN_WEB_THREADS)s "
            "-b 127.0.0.1:%(ENV_YIBAN_WEB_PORT)s\n"
            "proxy_pass http://127.0.0.1:${YIBAN_WEB_PORT};\n"
            "EXPOSE ${YIBAN_WEB_PORT}\n")
        self.assertEqual([], _carrier_hits(clean), "占位符被误判成字面量")

    def test_empty_scan_is_red(self):
        with self.assertRaises(AssertionError):
            _scan([])

    def test_empty_exemptions_are_red(self):
        _scanned, hits = _scan(_tracked_files())
        self.assertTrue(any(h[0] == "deploy/web.env" for h in hits),
                        "前提：值表必须在命中里")
        # 没有豁免时，值表与 README 示例都会长出来——这正是"豁免表为空 ⇒ 红"的形态。
        self.assertTrue(_unregistered(hits, {}, {}),
                        "豁免表为空却无违规：判据没有牙")


if __name__ == "__main__":
    unittest.main(verbosity=2)
