# -*- coding: utf-8 -*-
"""凹围栏采样：剪耳剖分路径的出界保证、绕序变化与退化输入边界。

背景：以顶点算术平均为圆心撒点的旧算法，在凹形围栏（L 形、门形）上圆心落在围栏
外，主采样一次都过不了验收。本文件把"凹形也必须采出界内点"钉住，并覆盖剖分路径
会遇到的退化写法（重合顶点、首尾闭合、共线、自交）与绕序变化。

验收口径统一用本层自己的 `point_in_polygon`，即调用方用的同一裁判；每个用例都
拿采样点回判，不另立标准。
"""
import unittest

from yiban.fyiban import algo as fyiban_algo

POINT_IN = fyiban_algo.point_in_polygon
SAMPLE = fyiban_algo.generate_position_in_polygon

# L 形：竖直臂 3×1 + 水平臂 2×1。顶点算术平均 (4/3, 4/3) 落在缺口里（围栏外）
L_SHAPE = [(0.0, 0.0), (3.0, 0.0), (3.0, 1.0), (1.0, 1.0), (1.0, 3.0), (0.0, 3.0)]
# 门形：顶边开矩形凹口，算术平均 (1.5, 1.25) 同样落在凹口里
GATE_SHAPE = [(0.0, 0.0), (0.0, 2.0), (1.0, 2.0), (1.0, 1.0),
              (2.0, 1.0), (2.0, 2.0), (3.0, 2.0), (3.0, 0.0)]
SQUARE = [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)]
TRIANGLE = [(0.0, 0.0), (2.0, 0.0), (0.0, 2.0)]


def _vertices_are_inside(polygon, count):
    """采 count 个点，返回其中被判在围栏内的个数（None 记作出界）。"""
    inside = 0
    for _ in range(count):
        point = SAMPLE(polygon)
        if point is not None and POINT_IN(point[0], point[1], polygon):
            inside += 1
    return inside


class ConcaveFenceSamplingTest(unittest.TestCase):
    def test_arithmetic_mean_of_concave_fence_is_outside(self):
        """先确认缺陷前提成立：凹形围栏的顶点算术平均确实在围栏外。

        这是旧算法在 L 形/门形上必失败的根因，也是下面各用例的意义所在。
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
                inside = _vertices_are_inside(polygon, 2000)
                self.assertEqual(inside, 2000, f"{name}有 {2000 - inside} 个采样点落在围栏外")

    def test_convex_fences_are_unaffected(self):
        """凸形（正方形、三角形）仍须全部落在围栏内：换算法不能只讨好凹形。"""
        for name, polygon in (("正方形", SQUARE), ("三角形", TRIANGLE)):
            with self.subTest(shape=name):
                inside = _vertices_are_inside(polygon, 1000)
                self.assertEqual(inside, 1000, f"{name}有 {1000 - inside} 个采样点落在围栏外")

    def test_vertex_order_variants_sample_inside(self):
        """同一块围栏换个起点或整体反向，采出的点仍必须在围栏内。

        这两种写法只是同一个环的等价描述（顶点顺序未变、只换了读法），绕序归一化
        就是为了让它们走同一条剖分路径。
        """
        for name, polygon in (("L 形", L_SHAPE), ("门形", GATE_SHAPE)):
            variants = [polygon, list(reversed(polygon))]
            variants += [polygon[i:] + polygon[:i] for i in range(1, len(polygon))]
            for index, variant in enumerate(variants):
                with self.subTest(shape=name, variant=index):
                    inside = _vertices_are_inside(variant, 200)
                    self.assertEqual(inside, 200,
                                     f"{name}第 {index} 种绕序有采样点落在围栏外")

    def test_shuffled_vertices_do_not_crash(self):
        """顶点被随意打乱后不崩、不死循环：这种输入已不是原来的环，不保证界内。

        打乱后的顶点序列把一个围栏描述成自交路径，围栏的"内/外"本身失去了意义
        （围栏数据该由服务端给出有序顶点）。这里只钉"不抛异常、返回值形状合法"。
        """
        shuffled = [L_SHAPE[0], L_SHAPE[3], L_SHAPE[1], L_SHAPE[5], L_SHAPE[2], L_SHAPE[4]]
        for _ in range(50):
            point = SAMPLE(shuffled)
            self.assertTrue(point is None or len(point) == 2)

    def test_closed_ring_and_repeated_vertices_still_sample_inside(self):
        """首尾写同一点（闭合环）与相邻重复顶点是常见写法，不能被当成退化输入。

        重复点对形状没有贡献，却会挡住剪耳，让正常的凹围栏退回旧路径；这里同时
        确认剖分没被挡住、采样全部在界内。
        """
        closed_l = [*L_SHAPE, L_SHAPE[0]]
        repeated_l = [L_SHAPE[0], L_SHAPE[0], L_SHAPE[1], L_SHAPE[1],
                      L_SHAPE[2], L_SHAPE[3], L_SHAPE[4], L_SHAPE[5], L_SHAPE[5]]
        for name, polygon in (("闭合环", closed_l), ("重复顶点", repeated_l)):
            with self.subTest(shape=name):
                self.assertIsNotNone(fyiban_algo._ear_clip_triangles(polygon),
                                     f"{name}写法没能进入剖分路径")
                inside = _vertices_are_inside(polygon, 500)
                self.assertEqual(inside, 500, f"{name}有采样点落在围栏外")

    def test_degenerate_polygons_do_not_crash(self):
        """退化输入（点数不足、全共线、面积为零）不能抛异常，也不能返回坏结构。

        这些形状没有真正的"内部"，退回旧的质心路径交差即可；关键是别崩、别死循环。
        """
        degenerate = [
            ("单点", [(1.0, 1.0)]),
            ("两点", [(0.0, 0.0), (1.0, 1.0)]),
            ("全共线", [(0.0, 0.0), (1.0, 1.0), (2.0, 2.0), (3.0, 3.0)]),
            ("重复同一点", [(1.0, 1.0)] * 5),
            ("零面积反复", [(0.0, 0.0), (1.0, 0.0), (0.0, 0.0), (1.0, 0.0)]),
        ]
        for name, polygon in degenerate:
            with self.subTest(shape=name):
                self.assertIsNone(fyiban_algo._ear_clip_triangles(polygon))
                point = SAMPLE(polygon)
                self.assertTrue(point is None or len(point) == 2)

    def test_self_intersecting_polygon_never_yields_unverified_point(self):
        """自交多边形（蝴蝶结、自交五边形）不支持：新路径要么拒绝、要么交出过裁判的点。

        自交路径的"内/外"本身没有公认定义，剖分可能拼出偏出围栏的三角形；这里钉住
        新路径的后置条件——它返回的任何坐标都必须过 `point_in_polygon`。核不上时
        交回旧路径（旧行为，可能出界），但绝不能由新路径直接交出可疑坐标。
        """
        bowtie = [(0.0, 0.0), (2.0, 2.0), (2.0, 0.0), (0.0, 2.0)]
        crossed = [(0.0, 1.0), (2.0, -1.0), (-2.0, 1.0), (2.0, 1.0), (-2.0, -1.0)]
        for name, polygon in (("蝴蝶结", bowtie), ("自交五边形", crossed)):
            with self.subTest(shape=name):
                for _ in range(500):
                    point = fyiban_algo._sample_by_triangulation(polygon)
                    if point is not None:
                        self.assertTrue(POINT_IN(point[0], point[1], polygon),
                                        f"{name}的新路径交出了围栏外的点")
                point = SAMPLE(polygon)
                self.assertTrue(point is None or len(point) == 2, "公开入口应返回点或 None")

    def test_empty_polygon_returns_none(self):
        """空输入仍返回 None（调用方据此报"签到范围点解析失败"）。"""
        self.assertIsNone(SAMPLE([]))
        self.assertIsNone(SAMPLE(()))

    def test_shrink_ratio_leaves_edge_margin(self):
        """内缩比例必须严格小于 1：等于 1 就失去"不可能贴边"的边距。"""
        self.assertGreater(fyiban_algo.TRI_SHRINK_RATIO, 0.0)
        self.assertLess(fyiban_algo.TRI_SHRINK_RATIO, 1.0)


class TriangulationTest(unittest.TestCase):
    def test_triangles_tile_the_fence_without_area_loss(self):
        """剖分正确性：n 个顶点切成 n-2 个三角形，面积之和等于围栏面积。

        面积守恒是"点不可能出界"的前提——三角形拼不回原面积就说明剖分不可信，
        实现里会据此退回旧路径。
        """
        dense_rect = []
        for step in range(6):  # 直边上插入共线顶点（地图描边的常见写法）
            dense_rect.append((0.0, 0.5 * step))
        for step in range(6):
            dense_rect.append((0.5 * step, 2.5))
        for step in range(6):
            dense_rect.append((2.5, 2.5 - 0.5 * step))
        for step in range(6):
            dense_rect.append((2.5 - 0.5 * step, 0.0))

        for name, polygon in (("L 形", L_SHAPE), ("门形", GATE_SHAPE), ("密共线矩形", dense_rect)):
            with self.subTest(shape=name):
                triangles = fyiban_algo._ear_clip_triangles(polygon)
                self.assertIsNotNone(triangles)
                # 首尾重复的顶点会被合并（闭合环写法），故按去重后的点数算 n-2
                unique = []
                for point in polygon:
                    if not unique or point != unique[-1]:
                        unique.append(point)
                if unique[0] == unique[-1]:
                    unique.pop()
                self.assertEqual(len(triangles), len(unique) - 2)
                covered = sum(fyiban_algo._triangle_area(t) for t in triangles)
                self.assertAlmostEqual(covered, _polygon_area(polygon), places=9)

    def test_samples_spread_by_area_not_clustered(self):
        """面积均匀性冒烟：网格分桶后，点数与桶内围栏面积相当，不出现量级失衡。

        桶内面积用更细的子网格数点估出（子格中心落在围栏内的比例 × 桶面积），再与
        实际采样数对比。只拦"密度堆在某一角、别处几乎采不到"这类失衡，不做严格
        统计检验。宽度放到 3 倍，是因为内缩 0.7 后每块三角形都把三个角让了出去，
        贴围栏外角的桶天然稀一些（实测 3×3 网格最稀 0.5 倍、最密 1.5 倍）；子网格
        自身也有误差，故只统计占围栏 3% 以上的桶。
        """
        samples = 20000
        cols = rows = 3
        subdivisions = 12
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


def _polygon_area(polygon):
    """鞋带公式（取绝对值），只给用例做面积对照。"""
    total = 0.0
    for i, (x1, y1) in enumerate(polygon):
        x2, y2 = polygon[(i + 1) % len(polygon)]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


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
