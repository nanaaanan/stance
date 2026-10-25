"""작은 가짜 원본으로 명부 생성 계약을 검사한다. 실제 data 파일은 쓰지 않는다."""

import csv
import io
import pathlib
import tempfile
import unittest
from unittest import mock

from scripts import registry


REB_HEADER = ["단지고유번호", "단지종류", "단지명_공시가격", "도로명주소"]
PRICE_HEADER = [*registry.PRICE_REQUIRED, "단지명"]
APT = ["11110100000003 ", "1", "서울,아파트", "길 1"]
OTHER = ["11110200000004 ", "2", "연립", "길 2"]
MULTI = ["11110300000005 ", "3", "다세대", "길 3"]
OUTSIDE = ["26110100000003 ", "1", "부산", "길 4"]


def price(area="84.98", code="3", building="1", unit="1", district="1111010100", name="서울,아파트"):
    return [district, code, building, unit, area, name]


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = pathlib.Path(self.temp.name)
        self.reb = self.directory / "reb.csv"
        self.price = self.directory / "price.csv"
        self.snapshot = self.directory / "snapshot.csv"
        self.area = self.directory / "area.csv"

    def write(self, path, header, rows, encoding):
        stream = io.StringIO(newline="")
        csv.writer(stream, lineterminator="\r\n").writerows([header, *rows])
        path.write_bytes(stream.getvalue().encode(encoding))

    def sources(self, prices=None, reb_rows=None, reb_header=None, price_header=None):
        self.write(self.reb, REB_HEADER if reb_header is None else reb_header,
                   [APT, OTHER, MULTI, OUTSIDE] if reb_rows is None else reb_rows, "cp949")
        self.write(self.price, PRICE_HEADER if price_header is None else price_header,
                   [price()] if prices is None else prices, "utf-8")

    def build(self):
        return registry.build(self.reb, self.price, self.snapshot, self.area)

    def stopped(self, message):
        self.snapshot.write_bytes(b"old snapshot")
        self.area.write_bytes(b"old area")
        with self.assertRaisesRegex(registry.RegistryError, message):
            self.build()
        self.assertEqual(self.snapshot.read_bytes(), b"old snapshot")
        self.assertEqual(self.area.read_bytes(), b"old area")

    def test_filter_original_bytes_and_aggregate(self):
        self.sources([price(), price("84.98000"), price("59", unit="2"),
                      price("invalid", code="4"), price("233.252106", code="5"),
                      price("invalid", district="2611010100")])
        summary = self.build()
        raw = self.reb.read_bytes().splitlines(keepends=True)
        self.assertEqual(self.snapshot.read_bytes(), raw[0] + raw[1])
        self.assertEqual(self.area.read_text(),
                         "reb_complex_id,exclu_use_ar,unit_count\n"
                         "11110100000003,59.0000,1\n11110100000003,84.9800,1\n")
        self.assertEqual(summary["duplicate_unit_rows"], 1)
        self.assertEqual(summary["price_snapshot_rows"], 3)
        self.assertEqual(summary["reb_seoul_rows"], 3)
        self.assertEqual(summary["unit_count_sum"], 2)

    def test_missing_and_duplicate_headers_report_all_names(self):
        for source, header in (("reb", REB_HEADER), ("price", PRICE_HEADER)):
            for changed, names in ((header[2:], header[:2]),
                                   (header + header[:2], header[:2])):
                with self.subTest(source=source, changed=changed):
                    self.sources(**{f"{source}_header": changed})
                    self.stopped("헤더")
                    with self.assertRaises(registry.RegistryError) as result:
                        self.build()
                    for name in names:
                        self.assertIn(name, str(result.exception))

    def test_area_errors_preserve_both_outputs(self):
        for value in ("84.98765", "", "abc", "NaN", "Infinity"):
            with self.subTest(value=value):
                self.sources([price(value)])
                self.stopped("전용면적")

    def test_area_validator_is_reused(self):
        self.sources()
        with mock.patch.object(registry.collect, "_area", wraps=registry.collect._area) as validate:
            self.build()
        validate.assert_called_once_with("84.98")

    def test_registry_key_collision_across_kinds(self):
        self.sources(reb_rows=[APT, ["11110200000003", "2", "연립", "길"]])
        self.stopped("키 중복")

    def test_invalid_registry_ids_and_kinds_even_outside_seoul(self):
        for complex_id, kind in (("", "1"), ("2611010000000", "1"),
                                 ("26110100000003", ""), ("26110100000003", "2"),
                                 ("１1110100000003", "1")):
            with self.subTest(complex_id=complex_id, kind=kind):
                self.sources(reb_rows=[[complex_id, kind, "이름", "길"]])
                self.stopped("단지고유번호|단지종류")

    def test_district_code_checked_before_filtering(self):
        for value in ("", "261101010", "abcdefghij", "١111010100"):
            with self.subTest(value=value):
                self.sources([price(district=value)])
                self.stopped("법정동코드")

    def test_invalid_complex_code_and_empty_unit(self):
        for column, value in (("code", ""), ("code", "123456789"), ("code", "x"),
                              ("building", ""), ("unit", " ")):
            with self.subTest(column=column, value=value):
                self.sources([price(**{column: value})])
                self.stopped("단지코드|동코드|호코드")

    def test_conflicting_unit_area_in_both_orders(self):
        rows = [price("59"), price("84")]
        for ordered in (rows, rows[::-1]):
            self.sources(ordered)
            self.stopped("기존 면적")

    def test_input_order_and_signed_zero_do_not_change_output(self):
        rows = [price("0"), price("-0", unit="2"), price("84.98000", unit="3")]
        self.sources(rows)
        self.build()
        expected = self.area.read_bytes()
        for ordered in (rows[::-1], [rows[1], rows[2], rows[0]]):
            self.sources(ordered)
            self.build()
            self.assertEqual(self.area.read_bytes(), expected)
        self.assertLess(expected.index(b"-0.0000"), expected.index(b",0.0000"))

    def test_missing_links_and_name_mismatch_are_observations(self):
        self.sources([price(name="다른 이름"), price(code="999")],
                     reb_rows=[APT, ["11110100000006", "1", "다른 단지", "길"]])
        summary = self.build()
        for key in ("price_missing_complexes", "snapshot_without_price", "name_mismatch_complexes"):
            self.assertEqual(summary[key], 1)

    def test_multiline_price_record_is_supported(self):
        self.sources([price(name="다른\n이름")])
        self.assertEqual(self.build()["name_mismatch_complexes"], 1)

    def test_unclosed_registry_record_with_correct_field_count_stops(self):
        self.sources()
        self.write(self.reb, REB_HEADER + [f"추가{i}" for i in range(7)], [], "cp949")
        # 두 줄을 따로 느슨하게 읽으면 각각 11칸이다. 전체로는 한 레코드다.
        first = b'11110100000003,1,name,a,b,c,d,e,f,g,"road\r\n'
        second = b'a,b,c,d,e,f,g,h,i,j,k"\r\n'
        for line in (first, second):
            self.assertEqual(len(next(csv.reader([line.decode("ascii")]))), 11)
        with self.reb.open("ab") as target:
            target.write(first + second)
        self.stopped("레코드")

    def test_record_width_and_decode_errors(self):
        for source in ("reb", "price"):
            for suffix in (b'extra\n', b'\xff\n'):
                with self.subTest(source=source, suffix=suffix):
                    self.sources()
                    with getattr(self, source).open("ab") as target:
                        target.write(suffix)
                    self.stopped("칸 수|해독")

    def test_cli_failure_has_nonzero_status(self):
        self.sources([price("84.98765")])
        with mock.patch("sys.stderr", new=io.StringIO()) as stderr:
            self.assertEqual(registry.main(["--reb", str(self.reb), "--price", str(self.price)]), 1)
        self.assertIn("전용면적", stderr.getvalue())

    def test_output_cannot_overwrite_source(self):
        self.sources()
        before = self.reb.read_bytes()
        with self.assertRaises(registry.RegistryError):
            registry.build(self.reb, self.price, self.reb, self.area)
        self.assertEqual(self.reb.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
