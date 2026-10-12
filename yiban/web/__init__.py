# SPDX-License-Identifier: AGPL-3.0-only
"""yiban.web：管理后台的双路径命名空间（128-P2a）。

本包是旧 `web/` 的再导出命名空间。真实代码仍在 `web/` 下。
本批只建骨架。物理迁移由 P2b 分片执行。拆墙由 P2c 执行。

实现机制一：显式再导出。`from web import app as app` 建立静态依赖边。
import-linter 契约 1（`yiban` 不得 import `web`）看得见这条边。
这条边是 P2a 的已知过渡态。`.importlinter` 的契约 1 用 `ignore_imports`
逐边豁免它（5 条 `yiban.web -> web.*`）。P2c 拆墙时该段即退场清单。

实现机制二：`sys.modules` 别名。`_AliasFinder` 把 `yiban.web.<子模块>`
映射到 `web.<子模块>` 本体。两侧拿到**同一个模块对象**。
只有同一对象，`mock.patch` 才在两侧互通。名字再导出（`from x import y`）
会生成另一个模块对象，那样两侧打桩不互通，"两套并存"就成立了。

嵌套子模块必须同法处理。若只做顶层映射，`import yiban.web.routes.pages`
会新建一个副本模块。import 机制随后把副本写回 `web.routes.pages` 属性。
那会污染旧路径。故本包接管整个 `yiban.web.` 前缀。

边界：本包不搬任何真实代码。旧 `web/` 转发墙原样保留。
生产入口仍是 `web.app:create_app`。gunicorn 与 systemd 与 docker 零改动。
"""

import importlib
import importlib.abc
import importlib.util
import sys

# 显式再导出：绑定本包属性，同时建立 import-linter 可见的静态依赖边。
from web import app as app
from web import render as render
from web import routes as routes
from web import security as security
from web import services as services

_PREFIX = __name__  # "yiban.web"
_REAL = "web"


class _AliasLoader(importlib.abc.Loader):
    """把 `yiban.web.*` 的加载交给 `web.*` 本体。"""

    def __init__(self, real_name):
        self._real_name = real_name
        self._real_spec = None

    def create_module(self, spec):
        # 返回已存在的真模块对象。import 机制随后把它放进 sys.modules。
        # 但 import 机制会**无条件**把别名 spec 写进 module.__spec__（不是"缺失才补"），
        # web.* 的 __spec__.name 因此会被改成 yiban.web.*。故这里先把真 spec 存下来，
        # 交给 exec_module 还原（否则 find_spec / reload 失准）。
        module = importlib.import_module(self._real_name)
        self._real_spec = module.__spec__
        return module

    def exec_module(self, module):
        # 在 import 机制补写 __spec__ 之后还原真 spec。
        # 未还原时：web.X.__spec__.name == "yiban.web.X"，find_spec 返回别名 spec；
        # importlib.reload(web.X) 走别名 loader 的 no-op，静默不重载（审查 M-1）。
        if self._real_spec is not None:
            module.__spec__ = self._real_spec
        return None


class _AliasFinder(importlib.abc.MetaPathFinder):
    """拦截 `yiban.web.` 前缀，把每个名字别名到 `web.` 的同名模块。"""

    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(_PREFIX + "."):
            return None
        real_name = _REAL + fullname[len(_PREFIX):]
        try:
            importlib.import_module(real_name)
        except ImportError:
            return None
        return importlib.util.spec_from_loader(fullname, _AliasLoader(real_name))


# 幂等：同一进程重复导入本包不重复挂载 finder。
if not any(isinstance(_f, _AliasFinder) for _f in sys.meta_path):
    sys.meta_path.insert(0, _AliasFinder())
