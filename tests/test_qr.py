"""本地二维码适配器测试：编码正确性、载荷约定、扫描容错。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from conftest_helper import fresh_db  # noqa: E402

import qradapter as q  # noqa: E402

import unittest


class QRCodeTests(unittest.TestCase):
    def test_data_codewords_known_vector(self):
        cw, total, blocks = q._bit_stream(b"HELLO WORLD", 1)
        self.assertEqual(cw.hex().upper(), "40B48454C4C4F20574F524C440EC11EC")
        self.assertEqual(total, 16)
        self.assertEqual(blocks, (1, 16, 0, 0))

    def test_rs_syndromes_all_versions(self):
        for payload in ["RES:A001", "MEAL:20260928-D-0007", "ROUTE:R2",
                        "x" * 60, "中文测试", "a" * 210]:
            raw = payload.encode()
            v = q._choose_version(len(raw))
            cw, _, (b1, d1, b2, d2) = q._bit_stream(raw, v)
            ec_len = q._EC_M[v][0]
            for data, ec in q._build_blocks(cw, ec_len, b1, d1, b2, d2):
                code = data + ec
                for i in range(ec_len):
                    s = 0
                    for b in code:
                        s = q._gf_mul(s, q._EXP[i]) ^ b
                    self.assertEqual(s, 0, f"{payload} v{v} RS 综合征不为 0")

    def test_matrix_complete_and_size(self):
        # v10-M 字节容量 213 字节；覆盖各版本取长度
        for payload in ["RES:A001", "a" * 100, "a" * 210]:
            mat = q.encode(payload)
            n = len(mat)
            self.assertTrue(all(x in (0, 1) for row in mat for x in row),
                            "存在未填充模块")
            self.assertEqual(len(mat), n)
            self.assertTrue(all(len(row) == n for row in mat))

    def test_too_long_rejected(self):
        with self.assertRaises(ValueError):
            q.encode("a" * 400)

    def test_svg_render(self):
        svg = q.to_svg(q.encode("MEAL:T1"))
        self.assertTrue(svg.startswith("<svg"))
        self.assertIn("<rect", svg)

    def test_payload_contract(self):
        self.assertEqual(q.build_payload("RES", "A001"), "RES:A001")
        self.assertEqual(q.build_payload("MEAL", "20260928-D-0001"),
                         "MEAL:20260928-D-0001")
        with self.assertRaises(ValueError):
            q.build_payload("BAD", "x")

    def test_scanner_accepts_and_rejects(self):
        s = q.LocalScanner()
        self.assertEqual(s.scan(" meal:a-1 "), ("MEAL", "A-1"))
        self.assertEqual(s.scan("route:r2"), ("ROUTE", "R2"))
        for bad in ["", "hello", "XYZ:1", "RES:"]:
            with self.assertRaises(q.ScanError):
                s.scan(bad)


if __name__ == "__main__":
    unittest.main()
