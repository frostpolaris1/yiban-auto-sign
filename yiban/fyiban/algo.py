# -*- coding: utf-8 -*-
"""多边形内随机定位点：**衍生自 `onefeifan/fyiban`（AGPL-3.0）**。

来源与逐块差异见同目录 `PROVENANCE.md`。差异摘要（**勿当笔误改回**）：
- 上游用正态分布偏移（`normalRandom * 0.2 × 跨度`）采样、最多 100 次，采样圆心是
  顶点算术平均；本实现用**剪耳三角剖分 + 三角形内均匀采样**——凹形围栏（L 形、
  门形）的算术平均落在围栏外时，"绕质心撒点"永远取不到界内点。
- 剖分不成立（自交、退化）时退回"缩放质心 + 均匀分布 ±0.1 跨度"的旧路径，与上游
  的缩放质心骨架一致；抖动兜底也是本地补充，避免多账号多次签到聚在质心附近形成
  行为指纹。
- 剖分很贵（O(n²)）而围栏按学校固定，故按顶点元组缓存剖分与面积前缀和（LRU 8 条）：
  同一围栏重复取点直接跳过剪耳，并在建表时抽检一次保证取点不出界。

随机源用 `secrets.SystemRandom()`（不可预测）：坐标是要提交给服务端的"人在现场"
证据，不能用可预测的伪随机序列。
"""
import bisect
import collections
import math
import secrets

_SECURE_RANDOM = secrets.SystemRandom()

# 旧路径（缩放质心采样）的缩放系数，与上游一致（createScaledPolygon(coords, centroid, 0.7)）
SCALE_FACTOR = 0.7
# 采样半径 = 外接矩形跨度的该比例（半宽即为 ±0.1 跨度）
SAMPLE_SPAN_RATIO = 0.2
MAX_ATTEMPTS = 5000
# 三角形内向内心收缩的比例。不取 1.0（顶点即围栏顶点）是因为 GPS 有几十米漂移，
# 贴边的采样点容易被服务端判在围栏外；取 0.7 与旧路径的 SCALE_FACTOR 同量级，
# 三条边各留约三成边距，同时保住三角形约一半的面积（0.7²）可用。
TRI_SHRINK_RATIO = 0.7

# 剪耳剖分很贵（O(n²)，48 顶点数百 µs、400 顶点数十 ms），而围栏按学校固定、同一轮
# 多账号共用同一多边形——把剖分连同内缩三角形、面积前缀和缓存下来，命中就跳过剪耳，
# 每点只剩取样本身。容量 8 条是为同进程内服务多校区留的余量，LRU 淘汰。
_TRIANGULATION_CACHE_CAPACITY = 8
# 建表时的自检抽样数：剖分刚建好时抽这么多点，全部落在围栏内才认可这份剖分（见
# `_build_sampling_table`）。抽检替代旧实现的"每点复核"，语义不弱：自交/退化围栏的
# 剖分在建表时就被否定。
_SELF_CHECK_SAMPLES = 200
_CACHE_MISS = object()
# 键是顶点浮点元组，值是 (内缩三角形, 面积前缀和, 总面积)，不可用的多边形值为 None。
# 引擎进程内取点是严格串行的，普通 dict 就够；即便被多线程并发访问，最坏也只是同一
# 围栏被重复建造一次，不会取到别人的表（dict 的读写本身是原子操作）。
_TRIANGULATION_CACHE = collections.OrderedDict()


def point_in_polygon(x, y, polygon):
    """射线法判断点是否在多边形内（上游 `isPointInPolygon`）。

    1e-12 加在分母上防"水平边除零"（本地加固，上游无此保护）。
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


def _signed_area(polygon):
    """鞋带公式：多边形有向面积，正数表示逆时针（CCW），负数表示顺时针。

    原理：把每条边与原点围成的梯形面积带符号累加，落在多边形外的部分正负相消，
    剩下的就是多边形自身面积；符号记录顶点的绕行方向。
    """
    total = 0.0
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        total += x1 * y2 - x2 * y1
    return total / 2.0


def _cross(o, a, b):
    """叉积 (a-o)×(b-o)：正数=左转（凸角），负数=右转（凹角），0=三点共线。"""
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _point_in_triangle(p, a, b, c):
    """点是否在三角形内（含边界）：对三条边分别判方向，三点同侧即在内部。"""
    d1 = _cross(a, b, p)
    d2 = _cross(b, c, p)
    d3 = _cross(c, a, p)
    return not ((d1 < 0 or d2 < 0 or d3 < 0) and (d1 > 0 or d2 > 0 or d3 > 0))


def _triangle_area(triangle):
    """三角形面积：两条边的叉积取绝对值再折半。"""
    (ax, ay), (bx, by), (cx, cy) = triangle
    return abs((bx - ax) * (cy - ay) - (by - ay) * (cx - ax)) / 2.0


def _ear_clip_triangles(polygon):
    """剪耳法把简单多边形剖分成三角形；剖不动（自交、退化）时返回 None。

    原理：任何简单多边形至少有两个"耳朵"——连续三个顶点组成的三角形，它是凸角
    （叉积为正）、且内部不含其它顶点。剪掉一个耳朵，顶点少一个但仍是简单多边形，
    重复到只剩一个三角形，就得到覆盖整块围栏的三角形集合。找不到耳朵即说明输入
    不是简单多边形，此时返回 None 交给调用方回退，不抛异常。

    凹角和共线（零面积）的候选耳直接跳过，一轮扫不出耳朵就收工，不会死循环。
    """
    pts = [(float(p[0]), float(p[1])) for p in polygon]
    # 丢掉相邻重复顶点，包括"首尾写同一点"的闭合环写法：重复点不贡献形状，却会
    # 挡住剪耳，让一个本来正常的围栏被误判成退化。
    merged = []
    for point in pts:
        if not merged or point != merged[-1]:
            merged.append(point)
    if len(merged) > 1 and merged[0] == merged[-1]:
        merged.pop()
    if len(merged) < 3:
        return None
    pts = merged
    # 整体平移到首个顶点为原点的局部坐标：围栏坐标是经纬度（百量级的大数），
    # 而面积只有 1e-5 度² 量级，鞋带公式直接用大数相减会因抵消丢掉有效位、
    # 连绕行方向都判错。剪耳只用到顶点之差，平移不改变形状。
    origin_x, origin_y = pts[0]
    pts = [(x - origin_x, y - origin_y) for x, y in pts]
    if _signed_area(pts) < 0:
        pts.reverse()  # 统一成逆时针：只有逆时针时"叉积为正=凸角"才成立
    index = list(range(len(pts)))
    triangles = []
    while len(index) > 3:
        m = len(index)
        ear_found = False
        for k in range(m):
            i0, i1, i2 = index[k - 1], index[k], index[(k + 1) % m]
            a, b, c = pts[i0], pts[i1], pts[i2]
            if _cross(a, b, c) <= 0:
                continue  # 凹角或共线的挡板，不是耳朵
            if any(_point_in_triangle(pts[j], a, b, c)
                   for j in index if j not in (i0, i1, i2)):
                continue  # 三角形里还夹着别的顶点，剪了会切掉不该切的地方
            triangles.append((a, b, c))
            del index[k]
            ear_found = True
            break
        if not ear_found:
            return None
    last = (pts[index[0]], pts[index[1]], pts[index[2]])
    if _cross(last[0], last[1], last[2]) > 0:
        triangles.append(last)  # 共线的收尾三角面积为 0，不追加也不影响面积守恒
    if not triangles:
        return None
    covered = sum(_triangle_area(t) for t in triangles)
    whole = _signed_area(pts)
    if abs(whole - covered) > 1e-9 * max(whole, covered):
        return None  # 三角形拼不回原面积：多半是自交多边形，结果不可信
    return [tuple((x + origin_x, y + origin_y) for x, y in triangle)
            for triangle in triangles]


def _shrink_triangle(triangle, ratio):
    """把三角形三个顶点按比例拉向内心，得到原三角形内部的一个小三角形。

    内心到三条边的距离相等，一定在三角形内部；顶点按 `ratio` 向它靠拢后，
    新顶点都是"内心 + 原顶点"两点的凸组合，所以新三角形整个落在原三角形里——
    这是"采出来的点不可能放到围栏外"的依据。取内心而不是重心，是为了对三条边
    留出尽量均匀的边距，窄三角形不会有一侧贴着围栏边界。代价是原三角形的三个角
    不再被采到（内缩后面积只剩 `ratio²`），越贴角的区域采样越稀。
    """
    (ax, ay), (bx, by), (cx, cy) = triangle
    la = math.hypot(bx - cx, by - cy)  # 顶点 A 对面的边长
    lb = math.hypot(cx - ax, cy - ay)
    lc = math.hypot(ax - bx, ay - by)
    perimeter = la + lb + lc
    if perimeter <= 0:
        return triangle  # 三点重合：内心无定义，原样返回（采样仍是同一个点）
    ix = (la * ax + lb * bx + lc * cx) / perimeter
    iy = (la * ay + lb * by + lc * cy) / perimeter
    return tuple((ix + (px - ix) * ratio, iy + (py - iy) * ratio)
                 for px, py in triangle)


def _sample_in_triangle(triangle):
    """在三角形内均匀取一个点。

    原理：用两个独立均匀数当重心坐标，但要先对其中一个开方。直接线性组合会让点
    向重心聚集；开方把抽签空间从"边长"换成"面积"，点在三角形内才是均匀的。
    """
    (ax, ay), (bx, by), (cx, cy) = triangle
    root = math.sqrt(_SECURE_RANDOM.random())
    rest = _SECURE_RANDOM.random()
    w_a = 1.0 - root
    w_b = root * (1.0 - rest)
    w_c = root * rest
    return (w_a * ax + w_b * bx + w_c * cx, w_a * ay + w_b * by + w_c * cy)


def _sample_from_table(shrunk, cumulative, total):
    """从采样表取一个点：按面积加权选一块内缩三角形，再在三角形内均匀取点。

    `bisect_left` 在前缀和里定位第一块"上界 ≥ 抽签值"的三角形，即面积加权抽签；
    抽出的是三角形（凸集）且已向心内缩，取点必然落在该三角形内部，从而落在围栏内。
    """
    target = _SECURE_RANDOM.uniform(0.0, total)
    index = bisect.bisect_left(cumulative, target)
    if index >= len(shrunk):  # uniform 取到上界（理论上的边界情形）时归到最后一块
        index = len(shrunk) - 1
    return _sample_in_triangle(shrunk[index])


def _self_check(table, polygon):
    """建表自检：从刚建好的表里抽 `_SELF_CHECK_SAMPLES` 个点，全在围栏内才认可。

    旧实现在每次取点后都用 `point_in_polygon` 核一遍，只为拦住"自交/退化围栏拼出
    偏出围栏的三角形"这一种输入；改成建表时一次性抽检，正常围栏命中缓存后不再付
    这份成本。抽检点出界即否定整份剖分——这类围栏本身说不清内外，宁可走旧行为。
    """
    shrunk, cumulative, total = table
    for _ in range(_SELF_CHECK_SAMPLES):
        point = _sample_from_table(shrunk, cumulative, total)
        if not point_in_polygon(point[0], point[1], polygon):
            return False
    return True


def _build_sampling_table(polygon):
    """建造采样表并自检，得到 `_TRIANGULATION_CACHE` 的值；不可用返回 None。

    流程：剪耳剖分 → 每块三角形向内心内缩 `TRI_SHRINK_RATIO` → 累出面积前缀和。
    剖分失败、总面积为 0、或建时自检不通过都返回 None，调用方据此回退旧路径。
    """
    triangles = _ear_clip_triangles(polygon)
    if not triangles:
        return None
    shrunk = [_shrink_triangle(triangle, TRI_SHRINK_RATIO) for triangle in triangles]
    cumulative = []
    total = 0.0
    for triangle in shrunk:
        total += _triangle_area(triangle)
        cumulative.append(total)
    if total <= 0:
        return None
    table = (shrunk, cumulative, total)
    if not _self_check(table, polygon):
        return None
    return table


def _sampling_table(polygon):
    """取（缺则建造并缓存）多边形的采样表；该围栏不可用时返回 None。

    键是顶点浮点元组：同一份围栏重复调用是同一批 float，浮点相等即命中（不引入
    容差——形状相近但不同的围栏不该互相污染）。不可用的多边形也记一份（值为 None），
    免得每次取样都重跑一遍注定失败的剪耳。
    """
    key = tuple((float(point[0]), float(point[1])) for point in polygon)
    table = _TRIANGULATION_CACHE.get(key, _CACHE_MISS)
    if table is not _CACHE_MISS:
        _TRIANGULATION_CACHE.move_to_end(key)
        return table
    table = _build_sampling_table(polygon)
    _TRIANGULATION_CACHE[key] = table
    if len(_TRIANGULATION_CACHE) > _TRIANGULATION_CACHE_CAPACITY:
        _TRIANGULATION_CACHE.popitem(last=False)
    return table


def _sample_by_triangulation(polygon):
    """剖分采样：按三角形面积加权挑一块，再在它内缩后的范围里均匀取点。

    面积加权让点在整块围栏里大致按面积均匀（大三角形被选中的机会按比例更大）；
    单块三角形是凸集，向内缩固定比例后仍整体在原三角形内，所以取点不可能出界。
    代价是内缩把每块三角形的三个角让了出去，贴着围栏外角的那一小片密度偏低——
    这正是"不可能贴边"要换的东西。
    采样表（剖分 + 内缩 + 面积前缀和）由 `_sampling_table` 缓存，命中即跳过剪耳；
    "取点不出界"由建表自检保证，这里不再逐点复核。表不可用（自交、退化）返回 None，
    交调用方走旧路径。
    """
    table = _sampling_table(polygon)
    if table is None:
        return None
    return _sample_from_table(*table)


def _generate_by_scaled_centroid(polygon_points):
    """旧路径：绕顶点算术平均撒点，取同时落在"缩放多边形 + 原多边形"内的点。

    只在剖分采样不可用（自交、退化多边形）时兜底。算术平均可能落在凹围栏外，
    此路径因此可能取不到界内点——局限见 `PROVENANCE.md`。
    """
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


def generate_position_in_polygon(polygon_points):
    """在多边形内生成随机点；空多边形返回 None。

    凹形围栏（L 形、门形）的顶点算术平均可能落在围栏外，绕它撒点会一次都取不到
    界内点；所以先把围栏剖分成三角形，再按面积加权在三角形内取点（凹性被剖分
    消化掉，与轮廓凸不凸无关）。剖分不成立时退回旧路径 `_generate_by_scaled_centroid`。
    """
    if not polygon_points:
        return None
    point = _sample_by_triangulation(polygon_points)
    if point is not None:
        return point
    return _generate_by_scaled_centroid(polygon_points)
