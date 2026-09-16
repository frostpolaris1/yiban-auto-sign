# -*- coding: utf-8 -*-
"""多边形内随机定位点：**衍生自 `onefeifan/fyiban`（AGPL-3.0）**。

来源与差异见同目录 `PROVENANCE.md`。差异摘要（**勿当笔误改回**）：
- 上游用正态分布偏移（`normalRandom * 0.2 × 跨度`）采样、最多 100 次；本实现用
  **均匀分布 ±0.1×跨度**、5000 次尝试，并对极小多边形做了兜底抖动——目的是
  避免多账号多次签到聚在质心附近形成行为指纹（固定聚集是风控特征）。
- 数学骨架（缩放质心 0.7 + 射线法双多边形验收）与上游一致。

随机源用 `secrets.SystemRandom()`（不可预测）：坐标是要提交给服务端的"人在现场"
证据，不能用可预测的伪随机序列。
"""
import secrets

_SECURE_RANDOM = secrets.SystemRandom()

# 缩放质心的缩放系数与上游一致（createScaledPolygon(coords, centroid, 0.7)）
SCALE_FACTOR = 0.7
# 采样半径 = 外接矩形跨度的该比例（半宽即为 ±0.1 跨度）
SAMPLE_SPAN_RATIO = 0.2
MAX_ATTEMPTS = 5000


def point_in_polygon(x, y, polygon):
    """射线法判断点是否在多边形内（上游 `isPointInPolygon`）。

    1e-12 加在分母上防"水平边除零"（上游 Kotlin 版无此保护，是本地加固）。
    """
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def generate_position_in_polygon(polygon_points):
    """在多边形内生成随机点（缩放质心算法）；空多边形返回 None。"""
    if not polygon_points:
        return None

    min_lng = min(p[0] for p in polygon_points)
    max_lng = max(p[0] for p in polygon_points)
    min_lat = min(p[1] for p in polygon_points)
    max_lat = max(p[1] for p in polygon_points)

    center_lng = sum(p[0] for p in polygon_points) / len(polygon_points)
    center_lat = sum(p[1] for p in polygon_points) / len(polygon_points)

    scaled_points = [
        ((p[0] - center_lng) * SCALE_FACTOR + center_lng,
         (p[1] - center_lat) * SCALE_FACTOR + center_lat)
        for p in polygon_points
    ]

    for _ in range(MAX_ATTEMPTS):
        lng = center_lng + (max_lng - min_lng) * SAMPLE_SPAN_RATIO * (_SECURE_RANDOM.random() - 0.5)
        lat = center_lat + (max_lat - min_lat) * SAMPLE_SPAN_RATIO * (_SECURE_RANDOM.random() - 0.5)
        if point_in_polygon(lng, lat, scaled_points) and point_in_polygon(lng, lat, polygon_points):
            return (lng, lat)

    # 兜底：质心 + 小范围随机抖动。避免多账号/多次触发共用同一质心坐标
    # （固定坐标聚集会成为风控行为指纹），同时保持仍在签到范围内。
    jitter = min(max_lng - min_lng, max_lat - min_lat) * 0.01  # 范围边长的 1%，约几十米量级
    jitter = max(jitter, 1e-6)  # 极小多边形时防止抖动归零
    for _ in range(50):
        fallback = (center_lng + _SECURE_RANDOM.uniform(-jitter, jitter),
                    center_lat + _SECURE_RANDOM.uniform(-jitter, jitter))
        if point_in_polygon(fallback[0], fallback[1], polygon_points):
            return fallback
    return (center_lng, center_lat)
