# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""夹具回放：按夹具文件驱动假上游响应（ziqz 代码面）。

职责两条：
  1. 读夹具：响应体取夹具文件的原始字节，不二次序列化。
  2. 记来源：回放面的响应夹具逐份登记。

登记口径：
  manifest 共 12 条：6 captured ＋ 6 synthetic。
  `tests/protocol/fixtures/` 另有 5 份非响应体文本件。
  它们不是响应夹具，不属回放面，不登记。
  5 份是 iframe_location（302 Location 串）、sign_in_body 两形、usersure 两件。

来源两档：
  · `captured`  实拍。`serve()` 原样回放。
  · `synthetic` 未实拍（已知未知）。
                `load()` 供解析分支用例取用；`serve()` 拒答。

回放模式口径：
  `covers()` 把 synthetic 端点也算在回放面内。
  故回放模式下，仅 synthetic 覆盖的端点一律 501。
  它不回退手写响应。这是保守方向。
  目的：未实拍状态不得被当已知下发。
  代价：旧 iOS 链在纯 synthetic 回放下走不完。

未实拍清单一目可见：合成夹具名一律以 `synthetic_` 开头。
本模块只读夹具，不写盘、不联网。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

MANIFEST_NAME = "replay_manifest.json"
PROVENANCE_CAPTURED = "captured"
PROVENANCE_SYNTHETIC = "synthetic"
PROVENANCE_VALUES = (PROVENANCE_CAPTURED, PROVENANCE_SYNTHETIC)
SYNTHETIC_PREFIX = "synthetic_"

_CONTENT_TYPES = {
    ".json": "application/json; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
}


class ReplayError(LookupError):
    """回放清单缺项、格式不符或夹具读不到。"""


class ReplayNotCovered(ReplayError):
    """该端点整条不在回放面内；调用方可回退到假上游的既有响应。"""


class ReplayUnavailable(ReplayError):
    """端点在回放面内，但该状态未实拍；假上游必须拒答。"""


@dataclass(frozen=True)
class ReplayResponse:
    """一份夹具的回放结果。`body` 与夹具文件逐字节相同。"""

    name: str
    endpoint: str
    state: str
    provenance: str
    note: str
    file: str
    body: bytes
    content_type: str

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")

    def json(self) -> Any:
        return json.loads(self.text)


def _content_type(path: Path) -> str:
    return _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


class ReplayCatalog:
    """一份 `replay_manifest.json` 加它引用的夹具文件。"""

    def __init__(self, fixtures_dir: Path, entries: Mapping[str, Mapping[str, Any]]):
        self.fixtures_dir = fixtures_dir
        self._entries = {name: dict(meta) for name, meta in entries.items()}

    @classmethod
    def from_dir(cls, fixtures_dir: str | Path) -> "ReplayCatalog":
        root = Path(fixtures_dir)
        manifest = root / MANIFEST_NAME
        try:
            raw = json.loads(manifest.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ReplayError(f"读不到回放清单: {manifest}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ReplayError(f"回放清单不是合法 JSON: {manifest}: {exc}") from exc
        entries = raw.get("fixtures")
        if not isinstance(entries, dict) or not entries:
            raise ReplayError(f"回放清单没有 fixtures 条目: {manifest}")
        for name, meta in entries.items():
            if not isinstance(meta, dict):
                raise ReplayError(f"回放条目不是对象: {name}")
            for key in ("file", "endpoint", "state", "provenance"):
                if not meta.get(key):
                    raise ReplayError(f"回放条目缺 {key}: {name}")
            provenance = meta["provenance"]
            if provenance not in PROVENANCE_VALUES:
                raise ReplayError(f"回放条目 provenance 不合法: {name}={provenance}")
            filename = str(meta["file"])
            if provenance == PROVENANCE_SYNTHETIC and not Path(filename).name.startswith(
                    SYNTHETIC_PREFIX):
                raise ReplayError(
                    f"未实拍夹具的文件名必须以 {SYNTHETIC_PREFIX} 开头: {name}={filename}")
            if not (root / filename).is_file():
                raise ReplayError(f"回放夹具不存在: {name}={filename}")
        return cls(root, entries)

    def names(self) -> list[str]:
        return sorted(self._entries)

    def covers(self, endpoint: str) -> bool:
        """端点是否在回放面内（有一个或多个状态条目）。"""
        return any(meta["endpoint"] == endpoint for meta in self._entries.values())

    def load(self, name: str) -> ReplayResponse:
        """按条目名取夹具；合成夹具也照常返回（供解析分支用例）。"""
        try:
            meta = self._entries[name]
        except KeyError:
            raise ReplayError(f"回放清单没有条目: {name}") from None
        path = self.fixtures_dir / str(meta["file"])
        try:
            body = path.read_bytes()
        except OSError as exc:
            raise ReplayError(f"读不到回放夹具: {path}: {exc}") from exc
        return ReplayResponse(
            name=name,
            endpoint=str(meta["endpoint"]),
            state=str(meta["state"]),
            provenance=str(meta["provenance"]),
            note=str(meta.get("note") or ""),
            file=str(meta["file"]),
            body=body,
            content_type=_content_type(path),
        )

    def find(self, endpoint: str, state: str) -> ReplayResponse | None:
        for name in self.names():
            meta = self._entries[name]
            if meta["endpoint"] == endpoint and meta["state"] == state:
                return self.load(name)
        return None

    def serve(self, endpoint: str, state: str) -> ReplayResponse:
        """回放一个端点状态。

        端点不在回放面内 ⇒ ReplayNotCovered，调用方回退既有响应。
        端点在面内但该状态缺条目 ⇒ ReplayUnavailable（未实拍，拒答）。
        条目为 synthetic ⇒ ReplayUnavailable（未实拍，拒答）。
        """
        if not self.covers(endpoint):
            raise ReplayNotCovered(f"端点不在回放面内: {endpoint}")
        resp = self.find(endpoint, state)
        if resp is None:
            raise ReplayUnavailable(
                f"未实拍（已知未知）: {endpoint} state={state}；回放清单无此状态，假上游拒答")
        if resp.provenance != PROVENANCE_CAPTURED:
            raise ReplayUnavailable(
                f"未实拍（已知未知）: {endpoint} state={state}；"
                f"夹具 {resp.name} 标为 {resp.provenance}，假上游拒答")
        return resp

    def unrecorded(self) -> list[str]:
        """未实拍条目名（一眼可见的清单）。"""
        return [name for name in self.names()
                if self._entries[name]["provenance"] != PROVENANCE_CAPTURED]


@lru_cache(maxsize=8)
def catalog_for(fixtures_dir: str) -> ReplayCatalog:
    """按目录缓存目录表；假上游每请求取用，避免重复读盘。"""
    return ReplayCatalog.from_dir(fixtures_dir)
