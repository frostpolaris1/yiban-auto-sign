# -*- coding: utf-8 -*-
"""多边形内定位点采样（`yiban/geo.py`）：内外判定边界、分布均匀性与兜底语义。

标签：K · 登录协议与自研链路
覆盖：偶奇射线法的边界条件（水平边不除零、射线过顶点只计一次、恰在边上的确定性
    表现、退化输入不抛异常）、凹/凸围栏的界内保证、绕序与重复顶点写法、**角与边
    可达性**（旧实现按三角形内缩 0.7，角与边采样概率为零）、覆盖率（≥90% 的内部
    格必须落到点）、**非空围栏必返回点**（调用方契约）、退化/自交围栏的自研兜底
    语义（响亮告警 + 文档写明"不保证界内"）、与旧实现的黑盒对拍冻结位串。
对应实现：yiban/geo.py（point_in_polygon、generate_position_in_polygon）。
关键断言：验收口径统一用本模块自己的 `point_in_polygon`（调用方用的同一裁判），
    不另立标准；凹形的缺陷前提（顶点算术平均落在围栏外）本身也要断言成立，否则
    围栏一换、用例静默失去意义也无人知。**对拍位串**是旧实现（已退役）在确定性
    点位上的输出冻结：改写射线法后必须与它逐点一致。
依赖：纯几何计算（每用例最多 4000 点采样，无 IO、无网络）。整文件在本机执行，无 skip。

背景：旧实现（上游缩放质心骨架 + 剪耳剖分 + 三角形内缩 0.7）在真实围栏上只覆盖
57%–66% 的内部格——三角形的三个角被整块让出，贴角与贴边的格永远空置。本实现改为
外接矩形内的拒绝采样：点在围栏内**精确均匀**，且不需要剪耳剖分。兜底（围栏退化或
采样超限）返回顶点质心并落 WARNING，**不保证落在围栏内**（退化围栏没有可判定的
内部），文档与用例同时钉住这一点。
"""
import math
import random
import unittest

from yiban import geo

POINT_IN = geo.point_in_polygon
SAMPLE = geo.generate_position_in_polygon

# L 形：竖直臂 3×1 + 水平臂 2×1。顶点算术平均 (4/3, 4/3) 落在缺口里（围栏外）
L_SHAPE = [(0.0, 0.0), (3.0, 0.0), (3.0, 1.0), (1.0, 1.0), (1.0, 3.0), (0.0, 3.0)]
# 门形：顶边开矩形凹口，算术平均 (1.5, 1.25) 同样落在凹口里
GATE_SHAPE = [(0.0, 0.0), (0.0, 2.0), (1.0, 2.0), (1.0, 1.0),
              (2.0, 1.0), (2.0, 2.0), (3.0, 2.0), (3.0, 0.0)]
SQUARE = [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)]
TRIANGLE = [(0.0, 0.0), (2.0, 0.0), (0.0, 2.0)]
BOWTIE = [(0.0, 0.0), (2.0, 2.0), (2.0, 0.0), (0.0, 2.0)]
CROSSED = [(0.0, 1.0), (2.0, -1.0), (-2.0, 1.0), (2.0, 1.0), (-2.0, -1.0)]


def _star_polygon(points, outer, inner, center):
    """外向/内向顶点交替的简单五角星（凹形，不交叉），用于覆盖更多凹分支。"""
    cx, cy = center
    ring = []
    for k in range(points):
        outer_angle = math.radians(90 + 360.0 * k / points)
        inner_angle = outer_angle + math.radians(180.0 / points)
        ring.append((cx + outer * math.cos(outer_angle), cy + outer * math.sin(outer_angle)))
        ring.append((cx + inner * math.cos(inner_angle), cy + inner * math.sin(inner_angle)))
    return ring


STAR_SHAPE = _star_polygon(5, 1.5, 0.62, (1.5, 1.5))  # 比 L 形多十个凹口：只测两种形状就还是「只讨好这两种」

#: 一点真实量级的围栏（南京，约 1km）：退化判定按度算，随手写 0~1 会绕过它
REAL_FENCE = [(118.889486, 31.919508), (118.891321, 31.920421), (118.893002, 31.918772),
              (118.890911, 31.917266), (118.888024, 31.916913), (118.886201, 31.918004),
              (118.886842, 31.920115), (118.888411, 31.921027)]


def _samples_inside(polygon, count):
    """采 count 个点，返回其中被判在围栏内的个数（None 记作出界）。"""
    inside = 0
    for _ in range(count):
        point = SAMPLE(polygon)
        if point is not None and POINT_IN(point[0], point[1], polygon):
            inside += 1
    return inside


def _polygon_area(polygon):
    """鞋带公式（取绝对值），只给用例做面积对照。"""
    total = 0.0
    for i, (x1, y1) in enumerate(polygon):
        x2, y2 = polygon[(i + 1) % len(polygon)]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


class PointInPolygonBoundaryTest(unittest.TestCase):
    """偶奇射线法的边界条件清单：每条都有确定性表现，逐条钉住。"""

    def test_basic_inside_and_outside(self):
        inside = (118.8895, 31.9190)  # 围栏形心附近；界外点取在围栏纬度下界之外
        outside = (118.8893, 31.9160)
        self.assertTrue(POINT_IN(*inside, REAL_FENCE))
        self.assertFalse(POINT_IN(*outside, REAL_FENCE))

    def test_horizontal_edge_does_not_divide_by_zero(self):
        """相邻两点纬度相同的边（水平边）不得除零：实现在判交叉前先排除水平边。"""
        poly = [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)]
        self.assertTrue(POINT_IN(1.0, 1.0, poly))
        self.assertFalse(POINT_IN(3.0, 1.0, poly))
        # 恰在下边上的点：水平边被跳过，其余边判出奇数交点 ⇒ 界内
        self.assertTrue(POINT_IN(1.0, 0.0, poly))

    def test_ray_through_vertex_counts_once(self):
        """射线正好穿过一个顶点时，该顶点只能计一次（否则奇偶翻转、内外判反）。"""
        diamond = [(0.0, 1.0), (1.0, 0.0), (2.0, 1.0), (1.0, 2.0)]
        self.assertTrue(POINT_IN(1.0, 1.0, diamond), "射线穿过右侧顶点 (2,1) 只应计一次")
        self.assertTrue(POINT_IN(1.9, 1.0, diamond))
        self.assertFalse(POINT_IN(3.0, 1.0, diamond))

    def test_on_edge_result_is_deterministic_not_symmetric(self):
        """恰落在边上：语义上不作保证，但实现是确定性的——这里冻结其确定性表现。

        上下两条边的结果不对称是偶奇射线法（半开判定 `> y` / `<= y`）的定义后果，
        不是缺陷；采样用连续随机点，落在边上的概率为零，所以取点从不依赖这条。
        """
        poly = [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)]
        self.assertTrue(POINT_IN(1.0, 0.0, poly))    # 下边
        self.assertFalse(POINT_IN(1.0, 2.0, poly))   # 上边

    def test_degenerate_inputs_return_false_without_raising(self):
        """顶点不足 3 个的输入没有内部：一律 False，且不抛异常。"""
        for polygon in ([], [(1.0, 1.0)], [(0.0, 0.0), (1.0, 1.0)],
                        [(0.0, 0.0), (1.0, 1.0), (2.0, 2.0), (3.0, 3.0)]):
            with self.subTest(polygon=polygon):
                self.assertFalse(POINT_IN(1.0, 1.0, polygon))

    def test_closed_ring_and_repeated_vertices_are_absorbed(self):
        """闭合环（首尾同点）与相邻重复顶点是常见写法，判定结果不受它们影响。"""
        closed = [*L_SHAPE, L_SHAPE[0]]
        repeated = [L_SHAPE[0], L_SHAPE[0], L_SHAPE[1], L_SHAPE[1],
                    L_SHAPE[2], L_SHAPE[3], L_SHAPE[4], L_SHAPE[5], L_SHAPE[5]]
        for name, polygon in (("闭合环", closed), ("重复顶点", repeated)):
            with self.subTest(shape=name):
                self.assertFalse(POINT_IN(2.5, 2.5, polygon), "缺口里的点不是界内")
                self.assertTrue(POINT_IN(0.5, 0.5, polygon))
                self.assertTrue(POINT_IN(2.5, 0.5, polygon))


class UniformSamplingTest(unittest.TestCase):
    def test_arithmetic_mean_of_concave_fence_is_outside(self):
        """先确认缺陷前提成立：凹形围栏的顶点算术平均确实在围栏外。

        这是旧算法（绕算术平均撒点）在 L 形/门形上必失败的根因，也是下面各用例
        的意义所在。
        """
        for polygon in (L_SHAPE, GATE_SHAPE):
            mean_x = sum(p[0] for p in polygon) / len(polygon)
            mean_y = sum(p[1] for p in polygon) / len(polygon)
            with self.subTest(polygon=polygon):
                self.assertFalse(POINT_IN(mean_x, mean_y, polygon))

    def test_concave_fences_always_sample_inside(self):
        """L 形与门形各采 2000 点，必须全部落在围栏内（凹口不是采样区）。"""
        for name, polygon in (("L 形", L_SHAPE), ("门形", GATE_SHAPE)):
            with self.subTest(shape=name):
                inside = _samples_inside(polygon, 2000)
                self.assertEqual(inside, 2000, f"{name}有 {2000 - inside} 个采样点落在围栏外")

    def test_convex_and_star_fences_always_sample_inside(self):
        """凸形与凹多角（五角星）同样必须全部落在围栏内：换算法不能只讨好一种形状。"""
        for name, polygon in (("正方形", SQUARE), ("三角形", TRIANGLE), ("五角星", STAR_SHAPE),
                              ("真实量级围栏", REAL_FENCE)):
            with self.subTest(shape=name):
                inside = _samples_inside(polygon, 1000)
                self.assertEqual(inside, 1000, f"{name}有 {1000 - inside} 个采样点落在围栏外")

    def test_vertex_order_variants_sample_inside(self):
        """同一块围栏换个起点或整体反向，采出的点仍必须在围栏内。

        两种写法只是同一个环的等价描述（顶点顺序未变、只换了读法）。
        """
        for name, polygon in (("L 形", L_SHAPE), ("门形", GATE_SHAPE)):
            variants = [polygon, list(reversed(polygon))]
            variants += [polygon[i:] + polygon[:i] for i in range(1, len(polygon))]
            for index, variant in enumerate(variants):
                with self.subTest(shape=name, variant=index):
                    inside = _samples_inside(variant, 200)
                    self.assertEqual(inside, 200, f"{name}第 {index} 种绕序有采样点落在围栏外")

    def test_shuffled_vertices_do_not_crash(self):
        """顶点被随意打乱后不崩、不死循环：这种输入已不是原来的环，不保证界内。

        打乱后的顶点序列把一个围栏描述成自交路径，围栏的"内/外"本身失去了意义
        （围栏数据该由服务端给出有序顶点）。这里只钉"不抛异常、返回值形状合法"。
        """
        shuffled = [L_SHAPE[0], L_SHAPE[3], L_SHAPE[1], L_SHAPE[5], L_SHAPE[2], L_SHAPE[4]]
        for _ in range(50):
            point = SAMPLE(shuffled)
            self.assertTrue(point is None or len(point) == 2)

    def test_non_empty_polygon_always_returns_a_point(self):
        """调用方契约：非空围栏**必**返回一个点（`client.py` 直接解包，无 None 保护）。

        含退化与自交围栏——它们走兜底，但仍必须交出坐标。
        """
        polygons = {
            "L 形": L_SHAPE, "正方形": SQUARE, "三角形": TRIANGLE,
            "真实围栏": REAL_FENCE,
            "极小": [(118.88, 31.92), (118.8800001, 31.92),
                     (118.8800001, 31.9200001), (118.88, 31.9200001)],
            "单点": [(1.0, 1.0)],
            "两点": [(0.0, 0.0), (1.0, 1.0)],
            "全共线": [(0.0, 0.0), (1.0, 1.0), (2.0, 2.0), (3.0, 3.0)],
            "重复同一点": [(1.0, 1.0)] * 5,
            "蝴蝶结": BOWTIE,
            "自交五边形": CROSSED,
        }
        for name, polygon in polygons.items():
            with self.subTest(shape=name):
                point = SAMPLE(polygon)
                self.assertIsNotNone(point, f"{name}：非空围栏必须返回坐标（契约）")
                self.assertEqual(len(point), 2)

    def test_empty_polygon_returns_none(self):
        """空输入仍返回 None（调用方据此报"签到范围点解析失败"）。"""
        self.assertIsNone(SAMPLE([]))
        self.assertIsNone(SAMPLE(()))

    def test_corners_and_edges_are_reachable(self):
        """角与边必须可采样：旧实现按三角形内缩 0.7，角与边的采样概率为零。

        这是本批换算法的直接动因；用"极值坐标"钉住它——均匀采样下 4000 点必然
        覆盖到贴近边的区域，而内缩方案永远做不到。容差按形状取：三角形的贴顶点
        区域面积过小（0.05 见方只占全形的 1/800），给它留统计余量不划算，改用
        正方形与 L 形——这两块在旧实现下同样被内缩让出（贴边约 0.3 跨度），一样
        能判红。
        """
        for name, polygon, low, high, tol in (("正方形", SQUARE, 0.0, 2.0, 0.05),
                                              ("L 形", L_SHAPE, 0.0, 3.0, 0.1)):
            xs, ys = [], []
            for _ in range(4000):
                x, y = SAMPLE(polygon)
                xs.append(x)
                ys.append(y)
            with self.subTest(shape=name):
                self.assertLess(min(xs), low + tol, "左侧贴边区域采不到点（内缩残留？）")
                self.assertGreater(max(xs), high - tol, "右侧贴边区域采不到点（内缩残留？）")
                self.assertLess(min(ys), low + tol, "下侧贴边区域采不到点（内缩残留？）")
                self.assertGreater(max(ys), high - tol, "上侧贴边区域采不到点（内缩残留？）")


class SamplingUniformityTest(unittest.TestCase):
    """分布均匀性：覆盖率（有落点的内部格 / 内部格总数）。"""

    #: 外接矩形上的网格边长（格）。格越细，贴边格越难命中，是"覆盖率"的噪声源。
    GRID = 16
    SAMPLES = 4000

    def _coverage(self, polygon):
        xs = [p[0] for p in polygon]
        ys = [p[1] for p in polygon]
        lo_x, hi_x, lo_y, hi_y = min(xs), max(xs), min(ys), max(ys)
        width = (hi_x - lo_x) / self.GRID
        height = (hi_y - lo_y) / self.GRID

        interior = set()
        for row in range(self.GRID):
            for col in range(self.GRID):
                cx = lo_x + (col + 0.5) * width
                cy = lo_y + (row + 0.5) * height
                if POINT_IN(cx, cy, polygon):
                    interior.add((row, col))
        hit = set()
        for _ in range(self.SAMPLES):
            x, y = SAMPLE(polygon)
            col = min(self.GRID - 1, max(0, int((x - lo_x) / (hi_x - lo_x) * self.GRID)))
            row = min(self.GRID - 1, max(0, int((y - lo_y) / (hi_y - lo_y) * self.GRID)))
            hit.add((row, col))
        return len(interior & hit) / len(interior), len(interior)

    def test_interior_cells_are_covered(self):
        """至少 90% 的内部格必须落到点（旧实现 57%–66%）。

        阈值取 90%：腔形围栏贴边格的"内部占比"很小，靠有限样本必然漏掉少数格，
        90% 给足统计余量，同时与旧实现的 57%–66% 拉开距离——内缩一复活即红。
        """
        for name, polygon in (("L 形", L_SHAPE), ("门形", GATE_SHAPE),
                              ("正方形", SQUARE), ("五角星", STAR_SHAPE)):
            with self.subTest(shape=name):
                coverage, cells = self._coverage(polygon)
                self.assertGreater(cells, 0)
                self.assertGreaterEqual(
                    coverage, 0.90,
                    f"{name}覆盖率 {coverage:.3f}（{cells} 个内部格）低于 90%：分布不均匀")

    def test_density_matches_area_within_smoke_limits(self):
        """密度冒烟：3×3 网格里各桶的落点数与桶内围栏面积相当（宽度放到 3 倍）。

        均匀采样的严格分布在数学上成立（拒绝采样），这里只拦"密度堆在某一角、
        别处几乎采不到"这类实现级失衡；因为只统计占围栏 3% 以上的桶，子网格估面积
        的误差也一并留出余量。
        """
        cols = rows = 3
        subdivisions = 12
        samples = 20000
        for name, polygon in (("L 形", L_SHAPE), ("门形", GATE_SHAPE)):
            with self.subTest(shape=name):
                counts = _bucket_counts(polygon, SAMPLE, samples, cols, rows)
                areas = _bucket_areas(polygon, cols, rows, subdivisions)
                total = sum(sum(row) for row in areas)
                for row in range(rows):
                    for col in range(cols):
                        share = areas[row][col] / total
                        if share < 0.03:
                            continue
                        expected = samples * share
                        observed = counts[row][col]
                        self.assertLess(observed, expected * 3,
                                        f"{name}第({row},{col})桶采样过密")
                        self.assertGreater(observed, expected / 3,
                                           f"{name}第({row},{col})桶采样过疏")


class FallbackSemanticsTest(unittest.TestCase):
    """兜底语义：围栏没有可判定的内部时，交给明确定义的坐标 + 响亮告警。"""

    def test_degenerate_fences_fall_back_loudly(self):
        """退化围栏（顶点不足、面积为零）→ 兜底点 + WARNING，绝不静默。"""
        degenerate = {
            "单点": [(1.0, 1.0)],
            "两点": [(0.0, 0.0), (1.0, 1.0)],
            "全共线": [(0.0, 0.0), (1.0, 1.0), (2.0, 2.0), (3.0, 3.0)],
            "零面积反复": [(0.0, 0.0), (1.0, 0.0), (0.0, 0.0), (1.0, 0.0)],
        }
        for name, polygon in degenerate.items():
            with self.subTest(shape=name):
                with self.assertLogs("yiban.geo", level="WARNING") as captured:
                    point = SAMPLE(polygon)
                self.assertEqual(len(point), 2)
                self.assertIn("兜底", "".join(captured.output))

    def test_self_intersecting_fences_fall_back_loudly(self):
        """自交围栏的"内/外"无公认定义（偶奇与非零填充结果不同）→ 显式兜底 + 告警。"""
        for name, polygon in (("蝴蝶结", BOWTIE), ("自交五边形", CROSSED)):
            with self.subTest(shape=name):
                with self.assertLogs("yiban.geo", level="WARNING") as captured:
                    point = SAMPLE(polygon)
                self.assertEqual(len(point), 2)
                self.assertIn("自交", "".join(captured.output))

    def test_fallback_stays_near_the_fence(self):
        """兜底点取顶点质心（加小抖动）：必须落在围栏外接矩形的近邻，不能乱飞。"""
        for polygon in ([(0.0, 0.0), (1.0, 1.0), (2.0, 2.0), (3.0, 3.0)], BOWTIE):
            xs = [p[0] for p in polygon]
            ys = [p[1] for p in polygon]
            span = max(max(xs) - min(xs), max(ys) - min(ys))
            with self.assertLogs("yiban.geo", level="WARNING"):
                x, y = SAMPLE(polygon)
            self.assertLessEqual(abs(x - sum(xs) / len(xs)), span * 0.01 + 1e-9)
            self.assertLessEqual(abs(y - sum(ys) / len(ys)), span * 0.01 + 1e-9)

    def test_fallback_semantics_are_documented(self):
        """兜底点**不保证界内**——这一点必须写进文档（不许静默产出无法验证的点）。"""
        for doc in (geo.__doc__, SAMPLE.__doc__):
            self.assertIn("不保证", doc, "文档必须写明兜底点不保证落在围栏内")
            self.assertIn("兜底", doc)


POINTS = 200
SEED = 20261007
FROZEN = {
    "square":
        "1111111111111111111111111111111111111111111111111111111111111111111111111111111111111111111111111111"
        "1111111111111111111111111111111111111111111111111111111111111111111111111111111111111111111111111111",
    "triangle":
        "1111011101000001010100000011101000101010001010100100100010010010010110010100110000110101110000010100"
        "1110001001100101101000110011001000010010111111100010010101101110001011111001000000101000100101010101",
    "L":
        "1110111010110011000110111001101110111011000111111100001000111001110101010000011101110101001001001001"
        "1100001110101101100100000110111101110010101010111010010001111101011010001000111011000111011101101010",
    "gate":
        "1111111110111110111111111011101111111111111111111111101111101110111111111111111101111111001111111011"
        "1111111101101111101101110111111010111111111011111111111111110101011111101011010111010101110101111111",
    "dense_rect":
        "1111111111111111111111111111111111111111111111111111111111111111111111111111111111111111111111111111"
        "1111111111111111111111111111111111111111111111111111111111111111111111111111111111111111111111111111",
    "closed_L":
        "0001111111010110100010100101111001110110110001111100111101101001101001100111000110111011111100110010"
        "0110000110101101110110110101010111101001101000101011101010100010011110011011001100011010011011000001",
    "real_fence":
        "0101011100101000011110001111111110011101111010001010111101011111110100110101000011111010000111110010"
        "1111011111101000111010110011001000011001010010111111110101111011110001100110111101100110010101001001",
    "bowtie":
        "1101000001100111001111111000000111001011000100001011010100001010000101001101011010101111000100101000"
        "0011001111000110011001101101000110100001000010110100000111110111001000101101001110001101001011011101",
    "collinear":
        "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
        "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000",
}


class DifferentialSnapshotTest(unittest.TestCase):
    """黑盒对拍冻结位串：与已退役的旧实现在确定性点位上的输出逐点一致。

    位串由 `work/gen_diff_fixture.py` 用**旧实现**（`yiban.fyiban.algo`，已删除）
    生成：同输入比输出。射线法洁净室重写只允许在"生成方式"上等同于教科书偶奇
    定义，故这次改写不得改动任何一个判定结果。
    """

    def test_differential_matches_frozen_snapshot(self):
        rng = random.Random(SEED)
        mismatches = []
        for name, polygon in _diff_polygons().items():
            expected = FROZEN[name]
            for index, (x, y) in enumerate(_diff_points(polygon, POINTS, rng)):
                if POINT_IN(x, y, polygon) != (expected[index] == "1"):
                    mismatches.append((name, index, x, y))
        self.assertEqual(mismatches, [], f"与旧实现的对拍不一致：{mismatches[:5]}")


def _diff_polygons():
    """对拍用的围栏集合（与 `work/gen_diff_fixture.py` 的 DIFF_POLYGONS 逐字一致）。"""
    dense_rect = ([(0.0, 0.5 * s) for s in range(6)] + [(0.5 * s, 2.5) for s in range(6)]
                  + [(2.5, 2.5 - 0.5 * s) for s in range(6)]
                  + [(2.5 - 0.5 * s, 0.0) for s in range(6)])
    return {
        "square": SQUARE,
        "triangle": TRIANGLE,
        "L": L_SHAPE,
        "gate": GATE_SHAPE,
        "dense_rect": dense_rect,
        "closed_L": [*L_SHAPE, L_SHAPE[0]],
        "real_fence": REAL_FENCE,
        "bowtie": BOWTIE,
        "collinear": [(0.0, 0.0), (1.0, 1.0), (2.0, 2.0), (3.0, 3.0)],
    }


def _diff_points(ring, count, rng):
    """确定性点位：外接矩形内均匀取点（与生成位串的脚本用同一段代码）。"""
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    lo_x, hi_x = min(xs), max(xs)
    lo_y, hi_y = min(ys), max(ys)
    return [(rng.uniform(lo_x, hi_x), rng.uniform(lo_y, hi_y)) for _ in range(count)]


def _bucket_areas(polygon, cols, rows, subdivisions):
    """用细子网格估计每个网格桶内的围栏面积。"""
    min_x = min(p[0] for p in polygon)
    max_x = max(p[0] for p in polygon)
    min_y = min(p[1] for p in polygon)
    max_y = max(p[1] for p in polygon)
    width = (max_x - min_x) / cols
    height = (max_y - min_y) / rows
    cell = 1.0 / subdivisions
    areas = [[0.0] * cols for _ in range(rows)]
    for row in range(rows):
        for col in range(cols):
            hits = 0
            for i in range(subdivisions):
                for j in range(subdivisions):
                    x = min_x + (col + (i + 0.5) * cell) * width
                    y = min_y + (row + (j + 0.5) * cell) * height
                    if POINT_IN(x, y, polygon):
                        hits += 1
            areas[row][col] = hits * cell * cell * width * height
    return areas


def _bucket_counts(polygon, sampler, samples, cols, rows):
    """按同一网格把采样点分桶计数。"""
    min_x = min(p[0] for p in polygon)
    max_x = max(p[0] for p in polygon)
    min_y = min(p[1] for p in polygon)
    max_y = max(p[1] for p in polygon)
    counts = [[0] * cols for _ in range(rows)]
    for _ in range(samples):
        x, y = sampler(polygon)
        col = min(cols - 1, int((x - min_x) / (max_x - min_x) * cols))
        row = min(rows - 1, int((y - min_y) / (max_y - min_y) * rows))
        counts[row][col] += 1
    return counts


if __name__ == "__main__":
    unittest.main(verbosity=2)
