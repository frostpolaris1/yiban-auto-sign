# SPDX-License-Identifier: AGPL-3.0-only
"""yiban.web：管理后台的双路径命名空间（128-P2a）。

本包是旧 `web/` 的再导出命名空间。真实代码仍在 `web/` 下。
本批只建骨架。物理迁移由 P2b 分片执行。拆墙由 P2c 执行。

实现机制一：显式再导出。`from web import app as app` 建立静态依赖边。
import-linter 契约 1（`yiban` 不得 import `web`）看得见这条边。
这条边是 P2a 的已知过渡态。基线文件 `scripts/gate/import-linter-baseline.txt`
登记它。P2c 拆墙时该基线行即退场清单。

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

    def create_module(self, spec):
        # 返回已存在的真模块对象。import 机制随后把它放进 sys.modules。
        # importlib 只在属性缺失时才补 __name__ / __spec__，故 web.* 侧身份不变。
        return importlib.import_module(self._real_name)

    def exec_module(self, module):
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
