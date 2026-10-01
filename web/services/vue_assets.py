# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""Vue 构建资产清单读取（前端翻新 P0；计划：docs/refactor/29-frontend-vue-refactor-plan.md §3.2）。

**为什么存在**：Vue（Vite）产物是内容哈希文件名，每次构建都变，模板无法写死。
`vite build` 的 `manifest: true` 产出 `web/static/vue/.vite/manifest.json`；本模块把它
换算成模板可直接输出的资产清单。

**契约**
- `vue_assets(entry)` 返回 `{"js": [...], "preloads": [...], "css": [...]}`，元素一律是
  `/static/vue/...` 形态的**站点路径，不含 script_root 前缀**——模板侧自行拼
  `request.script_root`（与既有 `?v=` 链接同一约定，子路径部署在模板层统一收口）；
- `js` 只含**入口模块**：`<script type="module">` 自会解析其相对 chunk 引用
  （构建用 `base: './'`，chunk 引用是相对路径，子路径下天然成立）；
- `preloads` 是入口传递依赖的其它 chunk（`<link rel="modulepreload">`，提高并行度，
  非正确性必需）；`css` 对入口 + 依赖闭包透传收集（manualChunks 拆出的 vendor chunk
  自带 CSS）；
- manifest 缺失 / 入口不存在时返回空表，**由视图层决定**（试点页 404）。dist 提交入库
  （裁决 J4），故常规运行与测试（无需 Node）时清单恒在。

**归属**：前端翻新线（与 web/templates、web/routes/pages.py 同侧）；后端修复批不触碰本文件。
"""
import json
import logging
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger(__name__)

_MANIFEST_PATH = Path(__file__).resolve().parents[1] / "static" / "vue" / ".vite" / "manifest.json"

# 页面视图传的 entry 名 = Vite 输入 HTML 相对其根目录的路径（当前唯一入口）
PILOT_ENTRY = "index.html"


def _load_manifest() -> dict:
    try:
        with open(_MANIFEST_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as exc:
        logger.warning("vue manifest 不可读：%s（%s）——需在 frontend/ 执行 npm run build", _MANIFEST_PATH, exc)
        return {}


_MANIFEST: Dict[str, dict] = _load_manifest()


def vue_assets(entry: str = PILOT_ENTRY) -> Dict[str, List[str]]:
    """manifest 条目 → 模板可用资产清单（见模块 docstring 的契约）。"""
    chunk = _MANIFEST.get(entry)
    if not isinstance(chunk, dict) or not chunk.get("file"):
        return {"js": [], "preloads": [], "css": []}

    base = "/static/vue/"
    js = [base + str(chunk["file"])]
    preloads: List[str] = []
    css: List[str] = []
    seen = {entry}

    def walk(key: str) -> None:
        dep = _MANIFEST.get(key)
        if not isinstance(dep, dict):
            return
        for css_file in dep.get("css") or []:
            rel = base + str(css_file)
            if rel not in css:
                css.append(rel)
        for dep_key in dep.get("imports") or []:
            if dep_key in seen:
                continue
            seen.add(dep_key)
            dep_chunk = _MANIFEST.get(dep_key)
            if isinstance(dep_chunk, dict) and dep_chunk.get("file"):
                preloads.append(base + str(dep_chunk["file"]))
            walk(dep_key)

    walk(entry)
    return {"js": js, "preloads": preloads, "css": css}
