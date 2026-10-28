"""图片绘制框索引保持原容差，并避免复杂地图逐实例全表扫描。"""
import random
import unittest

from paperlocale.image_geometry import _matches_painted_image, _painted_image_index


class ImageGeometryIndexTests(unittest.TestCase):
    def test_adjacent_cells_negative_coordinates_and_strict_tolerance(self):
        # 四个坐标都跨过网格边界；不能只查同一格，也不能把负数截断为零。
        painted = [(-.010001, -.000001, 9.999999, 19.999999)]
        cells = _painted_image_index(painted)
        self.assertTrue(_matches_painted_image((-.009999, .000001, 10.000001, 20.000001), cells))
        self.assertFalse(_matches_painted_image((-.020002, -.000001, 9.999999, 19.999999), cells))
        zero = _painted_image_index([(0, 0, 1, 1)])
        self.assertTrue(_matches_painted_image((.009999, 0, 1, 1), zero))
        self.assertFalse(_matches_painted_image((.01, 0, 1, 1), zero))
        self.assertFalse(_matches_painted_image((.010001, 0, 1, 1), zero))

    def test_matches_original_predicate_for_all_four_coordinates(self):
        randomizer = random.Random(20261027)
        boxes = [tuple(randomizer.uniform(-20, 20) for _ in range(4)) for _ in range(60)]
        cells = _painted_image_index(boxes)
        queries = [tuple(value + randomizer.uniform(-.012, .012) for value in box)
                   for box in boxes for _ in range(3)]
        queries.extend([(-30, -20, -10, 0), (0, 0, 0, 0)])
        for outer in queries:
            expected = any(max(abs(a - b) for a, b in zip(outer, box)) < .01 for box in boxes)
            with self.subTest(outer=outer):
                self.assertEqual(_matches_painted_image(outer, cells), expected)

    def test_duplicate_instances_are_deduplicated_and_search_is_local(self):
        # 不使用耗时阈值。限制映射访问次数，并禁止遍历全部桶，直接证明查询
        # 只访问最多81个相邻格，而不是随图片绘制实例数增加做全表扫描。
        class LocalAccessOnly(dict):
            accesses = 0

            def get(self, key, default=None):
                self.accesses += 1
                return super().get(key, default)

            def __iter__(self):
                raise AssertionError('查询不得扫描所有图片框')

            def values(self):
                raise AssertionError('查询不得扫描所有图片框')

            def items(self):
                raise AssertionError('查询不得扫描所有图片框')

        boxes = [(i, i + 1, i + 2, i + 3) for i in range(5000)]
        cells = _painted_image_index(boxes * 25)
        self.assertEqual(sum(map(len, cells.values())), len(boxes))
        indexed = LocalAccessOnly(cells)
        self.assertTrue(_matches_painted_image(boxes[-1], indexed))
        self.assertEqual(indexed.accesses, 1)
        indexed.accesses = 0
        self.assertFalse(_matches_painted_image((-100, -99, -98, -97), indexed))
        self.assertLessEqual(indexed.accesses, 82)
        indexed.accesses = 0
        shifted = tuple(value + .009 for value in boxes[-1])
        self.assertTrue(_matches_painted_image(shifted, indexed))
        self.assertLessEqual(indexed.accesses, 82)
