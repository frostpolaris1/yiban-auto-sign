# -*- coding: utf-8 -*-
"""多边形内的随机定位点：**洁净室自研实现**（内外判定 + 拒绝采样 + 显式兜底）。

为什么改：旧实现绕顶点算术平均撒点，凹围栏（L 形、门形）的算术平均落在围栏外，
主采样一次都过不了验收；改用"剪耳剖分 + 三角形内缩 0.7"后虽不再出界，但**分布**
被内缩毁掉——每块三角形的三个角被整块让出，贴角与贴边的格永远空置（真实围栏实测
覆盖率只有 57%–66%）。本实现去掉剖分与内缩，改为**外接矩形内的拒绝采样**：点在
围栏内精确均匀，实现更短，也不再需要剪耳。

**三件算法**（全部自研或取自公有领域教科书定义）：

1. **内外判定 = 教科书偶奇射线法（even-odd / crossing number）**：自待判点向 +x
   方向发一条水平射线，数它与多边形边的**有效交点**个数，奇数即在内部。边界条件
   清单见 `point_in_polygon` 的文档串——每条都有确定性表现，逐条列在那里。
2. **取点 = 拒绝采样**：在围栏外接矩形内均匀取点，落在围栏内即采用，否则重取；
   因此落点在围栏内**精确均匀**，角与边的采样概率不再为零。
3. **兜底 = 顶点质心 + 小抖动 + 响亮告警**：围栏没有可判定的内部（顶点不足 3 个、
   面积为零、自交）或拒绝采样超过 `MAX_DRAWS` 次时走这一条。

**兜底点的语义（调用方必读）**：兜底点**不保证**落在围栏内——退化围栏本身没有
可判定的内部，自交围栏的"内/外"也没有公认定义（偶奇与非零填充结果不同）。仍要
给坐标的理由是**调用方契约**：`yiban/client.py` 对非空围栏直接解包生成的点（无
None 保护），"非空围栏必返回点"是现行契约。每次兜底都落一条 WARNING，把原因
（顶点不足 / 面积为零 / 自交 / 拒绝采样超限）写进日志，绝不静默。**空围栏返回
None**（调用方据此报"签到范围点解析失败"）。

随机源用 `secrets.SystemRandom()`（不可预测）：坐标是要提交给服务端的"人在现场"
证据，不能用可预测的伪随机序列。
"""
import logging
import secrets

_SECURE_RANDOM = secrets.SystemRandom()

logger = logging.getLogger("yiban.geo")

#: 拒绝采样的抽点上限。极凹/极狭长的围栏接受率低，上限防死循环；超限走兜底并告警。
#: 真实围栏（12 / 4 顶点）的接受率 = 面积 / 外接矩形面积，典型 0.4–0.7，用不到零头。
MAX_DRAWS = 10000

#: 兜底点相对围栏外接矩形跨度的随机抖动比例。取小值的理由：多账号/多次触发若收到
#: **完全相同**的兜底坐标，固定点聚集会成为风控行为指纹；抖动让每次兜底各不相同。
_FALLBACK_JITTER_RATIO = 0.01
#: 跨度为 0（单点围栏）时的抖动下限，防抖动归零。
_FALLBACK_MIN_JITTER = 1e-6
#: "面积为零"的相对判据：面积小于外接矩形面积的该比例即视为没有内部（浮点残差量级）。
_DEGENERATE_AREA_RATIO = 1e-12


def point_in_polygon(x, y, polygon):
    """点 `(x, y)` 是否在多边形内部——**教科书偶奇射线法**。

    做法：自 `(x, y)` 向 +x 方向发一条水平射线，逐边判断该射线是否**穿过**它；
    穿过次数为奇数即在内部。顶点与边的写法差异（闭合重复点、相邻重复点、顺时针
    或逆时针）都不影响结果。

    **边界条件清单**（每条都是本实现的确定性表现；调用方不得把"恰在边界上"当成
    语义保证，采样用连续随机点，落在边界上的概率为零）：

    1. **水平边**：先按"两端点分居待判点 y 的两侧"筛掉，再做除法——水平边（两端
       y 相同）必然被筛掉，因此**不存在除零**，也不需要给分母加保护量。
    2. **射线恰过顶点**：两侧判据取半开区间（`y_i > y` 与 `y_j > y` 一真一假才算
       穿过），故与射线同高的顶点**只计一次**，不会让奇偶翻转两次。
    3. **恰在边上 / 恰在顶点上**：本函数**不作语义保证**（结果由边的走向与上面的
       半开判据共同决定，上下两条边的表现还不对称）；这是偶奇射线法的定义后果，
       不是缺陷。取点从不依赖它。
    4. **顶点不足 3 个 / 空输入**：一律 `False`——没有边就没有内部。
    5. **重复顶点**：相邻重复点与闭合环的重复首点先在内部折掉，判定不受影响。
    6. **绕序无关**：奇偶计数对顺时针/逆时针对称，故顶点整体反向不改结果。
    7. **自交多边形**：按**偶奇填充规则**定义"内部"（与非零填充规则不同）；本模块
       对自交围栏另有显式处置，见 `generate_position_in_polygon`。
    """
    return _point_in_ring(x, y, _normalize_ring(polygon))


def generate_position_in_polygon(polygon_points):
    """在多边形内生成随机点；**空围栏返回 None，非空围栏必返回一个点**。

    取点办法：在围栏外接矩形内均匀取点，落在围栏内即采用（拒绝采样）。因此点分布
    在围栏内**精确均匀**——贴角与贴边区域与围栏中心一样可采到（旧实现按三角形内缩
    0.7，角与边的采样概率为零）。

    兜底（**响亮、不静默**）：围栏没有可判定的内部（顶点不足 3 个、面积为零、自交），
    或拒绝采样超过 `MAX_DRAWS` 次仍未命中时，返回**顶点质心附近**的坐标并落一条
    WARNING。**该兜底点不保证落在围栏内**——退化围栏没有内部可判，自交围栏的"内/外"
    也没有公认定义。仍返回坐标的理由是调用方契约（`yiban/client.py` 直接解包）。
    """
    if not polygon_points:
        return None
    ring = _normalize_ring(polygon_points)
    reason = _degenerate_reason(ring)
    if reason is None:
        lo_x, hi_x, lo_y, hi_y = _bbox(ring)
        for _ in range(MAX_DRAWS):
            x = _SECURE_RANDOM.uniform(lo_x, hi_x)
            y = _SECURE_RANDOM.uniform(lo_y, hi_y)
            if _point_in_ring(x, y, ring):
                return (x, y)
        reason = f"拒绝采样超过 {MAX_DRAWS} 次仍未命中"
    return _fallback_point(ring, reason)


# ---------------------------------------------------------------------------
# 内外判定内核（调用方只经 point_in_polygon 进入；这里的 ring 已规范化）
# ---------------------------------------------------------------------------
def _point_in_ring(x, y, ring):
    """偶奇射线法内核：`ring` 必须是已规范化的顶点序列（无相邻/闭合重复点）。"""
    count = len(ring)
    if count < 3:
        return False
    inside = False
    x_prev, y_prev = ring[-1]
    for x_cur, y_cur in ring:
        # 半开判据：与射线同高的顶点只在一侧被算作"穿过"，故只计一次
        if (y_cur > y) != (y_prev > y):
            # 射线与该边交点的横坐标；y_prev != y_cur 由上一条判据保证，无除零
            crossing_x = (x_prev - x_cur) * (y - y_cur) / (y_prev - y_cur) + x_cur
            if x < crossing_x:
                inside = not inside
        x_prev, y_prev = x_cur, y_cur
    return inside


def _normalize_ring(polygon):
    """规范化顶点：转成浮点，丢掉相邻重复点与闭合环的重复首点。

    重复点不贡献形状，却会让"边是否相邻"的判断失准；闭合环写法（首尾写同一点）
    在这里被折掉。返回序列可能不足 3 个顶点（调用方自行判定退化）。
    """
    merged = []
    for point in polygon:
        vertex = (float(point[0]), float(point[1]))
        if not merged or vertex != merged[-1]:
            merged.append(vertex)
    if len(merged) > 1 and merged[0] == merged[-1]:
        merged.pop()
    return merged


def _bbox(ring):
    """外接矩形 `(min_x, max_x, min_y, max_y)`。"""
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return min(xs), max(xs), min(ys), max(ys)


def _signed_area(ring):
    """鞋带公式：有向面积，正数表示逆时针（CCW），负数表示顺时针。"""
    total = 0.0
    count = len(ring)
    for i in range(count):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % count]
        total += x1 * y2 - x2 * y1
    return total / 2.0


def _degenerate_reason(ring):
    """围栏不可用于取点的原因；可用时返回 None。

    判据顺序有意如此：**自交先判**——对称蝴蝶结的自交与"面积为零"同时成立，报
    "自交"才是根因（面积为零是自交的后果），日志里给管理员的信息才准确。
    """
    if len(ring) < 3:
        return f"顶点不足 3 个（{len(ring)} 个）"
    lo_x, hi_x, lo_y, hi_y = _bbox(ring)
    bbox_area = (hi_x - lo_x) * (hi_y - lo_y)
    if bbox_area <= 0.0:
        return "外接矩形面积为零"
    if _has_self_intersection(ring):
        return "自交围栏"
    if abs(_signed_area(ring)) <= _DEGENERATE_AREA_RATIO * bbox_area:
        return "围栏面积为零"
    return None


def _cross(o, a, b):
    """叉积 (a-o)×(b-o)：正数=左转（凸角），负数=右转（凹角），0=三点共线。"""
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _bbox_disjoint(a, b, c, d):
    """线段 ab 与 cd 的外接矩形是否不相交：不相交则两线段必不相交，可跳过精判。"""
    if max(a[0], b[0]) < min(c[0], d[0]) or max(c[0], d[0]) < min(a[0], b[0]):
        return True
    return max(a[1], b[1]) < min(c[1], d[1]) or max(c[1], d[1]) < min(a[1], b[1])


def _has_self_intersection(ring):
    """简单性判定：存在一对**不相邻**的边真交叉即为自交（非简单多边形）。

    真交叉的判据：每条边把另一条边的两个端点**严格分居两侧**（两侧叉积之积为负）。
    相邻边共享顶点是合法的，跳过；"端点在另一条边上"与"共线重叠"不在此判据内。

    先做外接矩形预筛，实践上接近线性；最坏 O(n²)，`n` 是围栏顶点数（真实值 ≤ 20）。
    """
    count = len(ring)
    if count < 4:  # 三角形一定是简单多边形
        return False
    for i in range(count):
        a, b = ring[i], ring[(i + 1) % count]
        for j in range(i + 1, count):
            if j == i + 1 or (i == 0 and j == count - 1):
                continue  # 相邻边共享端点，合法
            c, d = ring[j], ring[(j + 1) % count]
            if _bbox_disjoint(a, b, c, d):
                continue
            if _cross(a, b, c) * _cross(a, b, d) < 0 \
                    and _cross(c, d, a) * _cross(c, d, b) < 0:
                return True
    return False


def _fallback_point(ring, reason):
    """兜底点：顶点质心 + 小抖动 + WARNING。**不保证落在围栏内**（见模块文档串）。"""
    cx = sum(p[0] for p in ring) / len(ring)
    cy = sum(p[1] for p in ring) / len(ring)
    if len(ring) > 1:
        lo_x, hi_x, lo_y, hi_y = _bbox(ring)
        span = max(hi_x - lo_x, hi_y - lo_y)
    else:
        span = 0.0
    jitter = max(span * _FALLBACK_JITTER_RATIO, _FALLBACK_MIN_JITTER)
    point = (cx + _SECURE_RANDOM.uniform(-jitter, jitter),
             cy + _SECURE_RANDOM.uniform(-jitter, jitter))
    logger.warning(
        "围栏采样兜底（%s）：围栏没有可判定的内部，返回顶点质心附近的坐标 %s——"
        "该点不保证落在围栏内，请在入参围栏数据上排查", reason, point,
    )
    return point
