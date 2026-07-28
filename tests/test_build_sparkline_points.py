import unittest

import app


class BuildSparklinePointsTests(unittest.TestCase):
    def test_empty_values_returns_empty_string(self):
        self.assertEqual(app.build_sparkline_points([], 100, 30), "")

    def test_single_value_returns_empty_string(self):
        self.assertEqual(app.build_sparkline_points([40], 100, 30), "")

    def test_flat_values_draw_a_horizontal_midline(self):
        points = app.build_sparkline_points([40, 40, 40], 100, 30)
        pairs = [p.split(",") for p in points.split(" ")]
        self.assertEqual(len(pairs), 3)
        ys = {y for _, y in pairs}
        self.assertEqual(ys, {"15.0"})
        xs = [x for x, _ in pairs]
        self.assertEqual(xs, ["0.0", "50.0", "100.0"])

    def test_values_span_full_height_inverted(self):
        points = app.build_sparkline_points([0, 10], 100, 30)
        self.assertEqual(points, "0.0,30.0 100.0,0.0")


if __name__ == "__main__":
    unittest.main()
