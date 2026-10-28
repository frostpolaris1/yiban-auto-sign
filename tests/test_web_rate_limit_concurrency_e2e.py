# -*- coding: utf-8 -*-
"""限速的**真 HTTP 并发维**载体：与生产同形的 `gunicorn -w 1 --threads <值表>`。

标签：E · Web：认证/权限/API

**本文件补哪一维**：全仓 87 个测试文件用 `test_client`（进程内直调、**不经过 HTTP
服务器**）验 Web 面；真起 HTTP 服务的测试文件 **0 个**。而生产形状是
`docker/supervisord.conf` 的 `gunicorn -w 1 --threads %(ENV_YIBAN_WEB_THREADS)s`，其注释明写"必须保持
`-w 1`（单 worker 多线程）：**限速/爆破锁定/签到防抖状态存于进程内存**"。
⇒ "生产线程数共享同一份进程内限速状态"这一维**零载体**：每个用例都新建 app 实例、
拿到一个全新的空额度桶，且没有任何用例对一个活服务发过并发请求。
线程数从部署面值表 `deploy/web.env` 读取（`PROD_THREADS`）：模拟器必须与生产同值，
否则本维在值表变更后静默失真。

**本文件测什么（一条不变式，不是编舞）**：在**同一个 app 实例**（一个 gunicorn worker）
上并发打 `/api/*`，**窗口内放行数恰为 `RATE_MAX`**——不多放（超发＝限速失效），
不少放（丢更新＝共享计数没在并发下正确递增）。请求由客户端线程池并发发出；被测的
共享状态在**服务端进程内**，与客户端线程无关。

**有牙**：起一个**同代码、只把 `RATE_MAX` 放到极大**的对照服务（真 `create_app()`），
并发打同一端点数 ⇒ 放行数远超 `RATE_MAX`。这证明"放行数 ≤ RATE_MAX"这条判据能识别
"限速没生效"，不是恒绿摆设。对照服务不是替身：它就是同一份 `web.app`，只差一个常量。

对应实现：web/app.py（`create_app` 内的 `rate_limit` before_request 与 `_rate_limits`
   每实例字典、`RATE_MAX`）、web/security.py（`_bump_window_count` 的锁内读改写）、
    docker/supervisord.conf（生产进程形状）。
关键断言：钉"同一 app 实例上，窗口内 `/api/*` 放行数不得超过 `RATE_MAX`"这一不变式；
    不钉任何请求顺序。
依赖：`gunicorn`（本机 WSL 门禁 venv 已装）＋ 真 HTTP 服务 ＋ 回环网络；无外联。
    非 POSIX 或未装 gunicorn 时整组 skip（**可见**，不静默当绿）。
"""
import ast
import concurrent.futures
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    import gunicorn  # noqa: F401
    _HAS_GUNICORN = True
except ImportError:
    _HAS_GUNICORN = False

_POSIX = os.name == "posix"
TEST_KEY = "c" * 64
_ADMIN_EMAIL = "admin@test.local"
_ADMIN_PASS = "TestPass1234!"

#: 目标端点：`/api/*` 且匿名可读（`require_login` 对它放行），故 429 只可能来自限速。
_ENDPOINT = "/api/announcement"


def _app_constant(name):
    """从 `web/app.py` 源码读模块级常量（不 import，避免副作用与双份模块）。"""
    with open(os.path.join(BASE, "web", "app.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("web/app.py 里找不到常量 %s" % name)


RATE_MAX = _app_constant("RATE_MAX")
RATE_WINDOW = _app_constant("RATE_WINDOW")


def _deploy_value(name):
    """部署面值表（`deploy/web.env`）里的一个键——不在测试里再抄一份数。"""
    with open(os.path.join(BASE, "deploy", "web.env"), encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == name:
                return value.strip()
    raise AssertionError("deploy/web.env 缺键 %s" % name)


#: 与生产同形的 gunicorn 线程数（模拟器必须等价，见模块 docstring）。
PROD_THREADS = int(_deploy_value("YIBAN_WEB_THREADS"))

#: 对照服务的 WSGI 入口：同一份 `web.app`，只把限速上限放大到极大。
_CONTROL_WSGI = '''\
import web.app as w
w.RATE_MAX = 10 ** 9
w.RATE_MAX_AUTH_GET = 10 ** 9
app = w.create_app()
'''


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _get(port, path=_ENDPOINT, timeout=10):
    """发一个 GET，返回状态码（429/401 等都算正常返回）。"""
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, OSError) as e:  # 连接被拒/超时：当失败码，交给上层断言
        return "ERR:%r" % (e,)


def _ready(port):
    """就绪探测走**非 `/api/`** 路径：`rate_limit` 只对 `/api/*` 计数，
    故就绪探测不消耗被测的窗口额度（否则放行数会少 1，判据错位）。"""
    return not str(_get(port, path="/", timeout=5)).startswith("ERR")


@unittest.skipUnless(_POSIX and _HAS_GUNICORN,
                     "真 HTTP 服务 + gunicorn 仅 POSIX 且装了 gunicorn 时可用")
class RateLimitRealHttpConcurrencyTest(unittest.TestCase):
    """同一个 app 实例上并发打：窗口内放行数恰为 RATE_MAX（有牙）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yiban-rl-http-")
        self.env_file = os.path.join(self.tmp, ".env")
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write(
                "YIBAN_ACCOUNTS_KEY=%s\n"
                "YIBAN_ADMIN_USER=%s\n"
                "YIBAN_ADMIN_PASSWORD=%s\n" % (TEST_KEY, _ADMIN_EMAIL, _ADMIN_PASS))
        self._procs = []

    def tearDown(self):
        for p in self._procs:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait(timeout=10)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _start_server(self, spec, extra_pythonpath=None):
        port = _free_port()
        env = dict(os.environ)
        env.update({
            "YIBAN_ACCOUNTS_KEY": TEST_KEY,
            "YIBAN_ENV_FILE": self.env_file,
            "YIBAN_DB_FILE": os.path.join(self.tmp, "yiban.db"),
            "YIBAN_STATE_DIR": self.tmp,
        })
        if extra_pythonpath:
            env["PYTHONPATH"] = extra_pythonpath + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.Popen(
            [sys.executable, "-m", "gunicorn", "-w", "1", "--threads", str(PROD_THREADS),
             "--timeout", "60", "--no-control-socket",
             "-b", "127.0.0.1:%d" % port, spec],
            cwd=BASE, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self._procs.append(proc)
        # 就绪轮询：第一个 200 出现即算服务起来了。
        end = time.time() + 30
        while time.time() < end:
            if proc.poll() is not None:
                out = (proc.stdout.read() or b"").decode("utf-8", "replace")
                raise AssertionError("gunicorn 启动即退出：\n%s" % out)
            if _ready(port):
                return port
            time.sleep(0.2)
        raise AssertionError("gunicorn 未在 30s 内就绪")

    def _hammer(self, port, n):
        """并发发 n 个请求，返回 {200: a, 429: b, ...} 计数。"""
        counts = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=64) as ex:
            for code in ex.map(lambda _i: _get(port), range(n)):
                counts[code] = counts.get(code, 0) + 1
        return counts

    def test_same_app_instance_caps_at_rate_max_under_concurrency(self):
        """一个 app 实例、生产线程数：窗口内放行数恰为 RATE_MAX。"""
        port = self._start_server("web.app:create_app()")
        n = RATE_MAX + 20
        counts = self._hammer(port, n)
        allowed = counts.get(200, 0)
        rejected = counts.get(429, 0)
        self.assertEqual(
            counts.get(200, 0) + counts.get(429, 0), n,
            "所有请求都必须拿到 200 或 429（其余码=%s）——"
            "出现别的码说明端点本身坏了，本次并发结论不作数" % counts)
        self.assertLessEqual(
            allowed, RATE_MAX,
            "窗口内放行数 %d > RATE_MAX %d：限速在并发下超发（进程内共享计数被并发绕过）"
            % (allowed, RATE_MAX))
        self.assertEqual(
            allowed, RATE_MAX,
            "窗口内放行数 %d ≠ RATE_MAX %d：共享计数在并发下少放（丢更新）或额度未共享。"
            "计数=%s" % (allowed, RATE_MAX, counts))
        self.assertEqual(rejected, n - RATE_MAX, "其余必须全部 429；计数=%s" % counts)

    def test_teeth_control_without_cap_admits_far_more(self):
        """牙：同代码、只放大 RATE_MAX 的对照服务 ⇒ 放行数远超 RATE_MAX。

        证明"放行数 ≤ RATE_MAX"这条判据能识别"限速没生效"，不是恒绿摆设。
        """
        ctl_dir = os.path.join(self.tmp, "ctl")
        os.makedirs(ctl_dir, exist_ok=True)
        with open(os.path.join(ctl_dir, "_rl_control.py"), "w", encoding="utf-8") as f:
            f.write(_CONTROL_WSGI)
        port = self._start_server("_rl_control:app", extra_pythonpath=ctl_dir)
        n = RATE_MAX + 20
        counts = self._hammer(port, n)
        self.assertGreater(
            counts.get(200, 0), RATE_MAX,
            "把上限放大到极大后放行数仍 ≤ RATE_MAX（%s）——"
            "本判据测不到'限速没生效'，是恒绿摆设" % counts)


if __name__ == "__main__":
    unittest.main(verbosity=2)
