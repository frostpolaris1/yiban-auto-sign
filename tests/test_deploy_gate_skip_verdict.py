# -*- coding: utf-8 -*-
"""发布闸门把 SKIP 计入结论（R3-018）。

标签：J · 运维：部署/备份/发布

缺陷形状：摘要只累加 FAIL，`n_fail=0` 就打印 `闸门结论: PASS` 并 `exit 0`。
"全部项 SKIP"（一项断言都没跑）同样报 PASS——跳过不是绿（AGENTS §30）。

判据（不动 docker，跑夹具副本）：
  ① 全 SKIP ⇒ rc≠0、结论为 PARTIAL、不得打印 PASS；
  ② 登记为"允许跳过"的项（fdjl 批未合入本发布线）在正常轮里仍算 PASS；
  ③ 未登记的 SKIP ⇒ rc≠0。
全 SKIP 夹具用 `--phase ""`（不跑 A/B/C 阶段，只剩说明项）构造。
"""
import io
import os
import shutil
import subprocess
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE = os.path.join(BASE, "scripts", "deploy_gate.sh")
HANDOFF = os.path.join(BASE, "scripts", "gate", "handoff.sh")
NGINX = os.path.join(BASE, "docker", "nginx.conf")
SUPERVISORD = os.path.join(BASE, "docker", "supervisord.conf")
BASH = shutil.which("bash")


def _write(path, text):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


@unittest.skipIf(BASH is None, "需要 bash（Git Bash/WSL）")
class DeployGateSkipVerdictTest(unittest.TestCase):
    """R3-018：SKIP 必须进入结论。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="deploy-gate-skip-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _fixture(self, stream_api_present):
        """最小闸门树：只要 phase_info 能跑（它 grep web/ 与 supervisord）。"""
        root = os.path.join(self.tmp, "gate-tree")
        _write(os.path.join(root, "scripts", "deploy_gate.sh"), io.open(GATE, encoding="utf-8").read())
        os.makedirs(os.path.join(root, "scripts", "gate"))
        shutil.copyfile(HANDOFF, os.path.join(root, "scripts", "gate", "handoff.sh"))
        os.makedirs(os.path.join(root, "docker"))
        shutil.copyfile(SUPERVISORD, os.path.join(root, "docker", "supervisord.conf"))
        shutil.copyfile(NGINX, os.path.join(root, "docker", "nginx.conf"))
        web = os.path.join(root, "web")
        os.makedirs(web)
        if stream_api_present:
            _write(os.path.join(web, "stream_api_marker.py"), 'ROUTE = "/api/stream/x"\n')
        return root

    def _run(self, root, phases, gate_tmp):
        env = dict(os.environ)
        env["GATE_TMP"] = gate_tmp
        return subprocess.run([BASH, os.path.join(root, "scripts", "deploy_gate.sh"),
                               "--phase", phases],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env, cwd=root, timeout=120)

    def test_all_skip_is_not_pass(self):
        """一项都没有 PASS（全 SKIP）⇒ 非 0，且不得打印 PASS。"""
        root = self._fixture(stream_api_present=False)
        r = self._run(root, "", os.path.join(self.tmp, "gate-tmp"))
        out = (r.stdout or "") + (r.stderr or "")
        self.assertIn("SKIP", out, "夹具没造出全 SKIP 场景：\n" + out)
        self.assertNotIn("闸门结论: PASS", out,
                         "全部项 SKIP 仍报 PASS（一项断言都没跑）：\n" + out)
        self.assertIn("闸门结论: PARTIAL", out, "结论必须显性标 PARTIAL：\n" + out)
        self.assertNotEqual(r.returncode, 0, "全 SKIP 却 exit 0：\n" + out)

    def test_registered_skip_alone_does_not_fail_the_run(self):
        """fdjl 批未合入本发布线 ⇒ 那两项是登记过的允许跳过，正常轮仍 PASS。"""
        root = self._fixture(stream_api_present=True)
        r = self._run(root, "", os.path.join(self.tmp, "gate-tmp2"))
        out = (r.stdout or "") + (r.stderr or "")
        self.assertIn("SKIP", out, "夹具没造出 SKIP：\n" + out)
        self.assertIn("闸门结论: PASS", out,
                      "登记为允许跳过的项不该把整轮判红：\n" + out)
        self.assertEqual(r.returncode, 0, out)

    def test_unregistered_skip_is_partial(self):
        """未登记的跳过项 ⇒ PARTIAL 且非 0（跳过不是绿）。"""
        root = self._fixture(stream_api_present=True)
        gate = os.path.join(root, "scripts", "deploy_gate.sh")
        src = io.open(gate, encoding="utf-8").read()
        # 把登记表里的一项改名 ⇒ 该项成为"未登记的跳过"（等价于下一个人新加了一条 skip）。
        src = src.replace('"gunicorn 线程升档"', '"gunicorn 线程升档（改名后未登记）"', 1)
        _write(gate, src)
        r = self._run(root, "", os.path.join(self.tmp, "gate-tmp3"))
        out = (r.stdout or "") + (r.stderr or "")
        self.assertIn("闸门结论: PARTIAL", out, "未登记的 SKIP 必须判 PARTIAL：\n" + out)
        self.assertNotEqual(r.returncode, 0, "未登记的 SKIP 不得 exit 0：\n" + out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
