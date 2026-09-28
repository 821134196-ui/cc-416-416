"""本地二维码适配器测试:结构不变量、容量、模式头、扫描解析。

注:完整解码器端到端验证在开发期通过 jsQR(Nayuki 参考矩阵逐位一致)完成;
此处做不依赖第三方解码器的结构与协议断言。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import qr


class TestQrAdapter(unittest.TestCase):
    def test_parse_resident_and_box(self):
        self.assertEqual(qr.parse_scan("RES:R-101"), ("resident", "R-101"))
        self.assertEqual(qr.parse_scan("box:l-abc123"), ("box", "L-ABC123"))
        self.assertEqual(qr.parse_scan("  RES:R2 "), ("resident", "R2"))

    def test_parse_rejects_unknown(self):
        for bad in ("", "HTTP://x", "XYZ:1", "RES:", "BOX:"):
            with self.assertRaises(ValueError):
                qr.parse_scan(bad)

    def test_code_helpers(self):
        self.assertTrue(qr.resident_code("r1").startswith("RES:"))
        self.assertTrue(qr.box_code("tok").startswith("BOX:"))

    def test_version_selection(self):
        # 字节模式容量:v1=14, v2=26, v3=42
        self.assertEqual(qr.encode("A").version, 1)
        self.assertEqual(qr.encode("A" * 14).version, 1)
        self.assertEqual(qr.encode("A" * 15).version, 2)
        self.assertEqual(qr.encode("A" * 26).version, 2)
        self.assertEqual(qr.encode("A" * 27).version, 3)

    def test_square_matrix_and_finders(self):
        r = qr.encode("RES:R001")
        n = r.size
        self.assertEqual(len(r.matrix), n)
        self.assertTrue(all(len(row) == n for row in r.matrix))
        # 三个寻像图形:左上 7x7 边框
        for (br, bc) in ((0, 0), (0, n - 7), (n - 7, 0)):
            self.assertEqual(r.matrix[br][bc], 1)
            self.assertEqual(r.matrix[br + 6][bc + 6], 1)
            self.assertEqual(r.matrix[br][bc + 6], 1)
            # 内部 3x3 实心
            self.assertEqual(r.matrix[br + 3][bc + 3], 1)

    def test_timing_pattern(self):
        r = qr.encode("x")
        n = r.size
        for i in range(8, n - 8):
            self.assertEqual(r.matrix[6][i], (i + 1) % 2)
            self.assertEqual(r.matrix[i][6], (i + 1) % 2)

    def test_quiet_zone_in_svg(self):
        svg = qr.to_svg("RES:R1", module=4, quiet=4)
        self.assertIn("<svg", svg)
        r = qr.encode("RES:R1")
        expected = (r.size + 8) * 4
        self.assertIn(f'width="{expected}"', svg)

    def test_format_bits_present_and_valid(self):
        # v1: 固定暗模块 (8,13) 为 1
        r = qr.encode("RES:R001")
        self.assertEqual(r.matrix[13][8], 1)

    def test_chinese_multibyte(self):
        r = qr.encode("餐盒标签 BX-001 温度65℃")
        self.assertGreaterEqual(r.version, 1)
        self.assertEqual(len(r.matrix), r.size)

    def test_too_long_raises(self):
        with self.assertRaises(ValueError):
            qr.encode("x" * 1000, max_version=10)


if __name__ == "__main__":
    unittest.main()
