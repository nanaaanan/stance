import contextlib
import hashlib
import io
import pathlib
import random
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from decimal import Decimal

import openpyxl

from scripts import area


SOURCE = pathlib.Path(__file__).resolve().parents[1] / "data" / "kapt-area-info.xlsx"
DATA_DIR = SOURCE.parent
SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


class KaptAreaLoaderTest(unittest.TestCase):
    def _file_state(self, path: pathlib.Path):
        if not path.exists():
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _real_output_state(self):
        return (
            self._file_state(DATA_DIR / "complex-area.csv"),
            self._file_state(DATA_DIR / "matching-failures.csv"),
        )

    def _copy_source(self, directory: str, name: str) -> pathlib.Path:
        target = pathlib.Path(directory) / name
        shutil.copy(SOURCE, target)
        return target

    def _replace_header(self, path: pathlib.Path, header: str) -> None:
        workbook = openpyxl.load_workbook(path)
        worksheet = workbook.active
        for cell in worksheet[2]:
            if cell.value == header:
                cell.value = f"{header} 없음"
                break
        else:
            self.fail(f"fixture 원본에서 헤더를 찾지 못함: {header}")
        workbook.save(path)
        workbook.close()

    def _stored_column_text(self, path: pathlib.Path, column: str) -> list[str]:
        with zipfile.ZipFile(path) as archive:
            sheet_paths = [
                name
                for name in archive.namelist()
                if name.startswith("xl/worksheets/") and name.endswith(".xml")
            ]
            self.assertEqual(len(sheet_paths), 1)
            sheet_root = ET.fromstring(archive.read(sheet_paths[0]))

            shared = []
            if "xl/sharedStrings.xml" in archive.namelist():
                shared_root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                shared = [
                    "".join(item.itertext())
                    for item in shared_root.findall(f"{SHEET_NS}si")
                ]

        stored = []
        for row in sheet_root.iter(f"{SHEET_NS}row"):
            for cell in row.findall(f"{SHEET_NS}c"):
                if not cell.attrib.get("r", "").startswith(column):
                    continue
                if cell.attrib.get("t") == "inlineStr":
                    inline = cell.find(f"{SHEET_NS}is")
                    if inline is not None:
                        stored.append("".join(inline.itertext()))
                    continue
                value = cell.find(f"{SHEET_NS}v")
                if value is not None:
                    stored.append(
                        shared[int(value.text)] if cell.attrib.get("t") == "s" else value.text
                    )
        return stored

    def test_reads_source_by_header_name(self):
        rows = area.load_kapt_area_rows(SOURCE)

        self.assertEqual(len(rows), 18_062)
        self.assertEqual(rows[0]["단지코드"], "A10023348")
        self.assertEqual(len({row["단지코드"] for row in rows}), 3_137)
        self.assertEqual(sum(row["단지코드"] is None for row in rows), 0)

    def test_finds_header_after_another_notice_row(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._copy_source(directory, "kapt-plus1.xlsx")
            workbook = openpyxl.load_workbook(path)
            worksheet = workbook.active
            worksheet.insert_rows(1)
            worksheet.cell(1, 1, "안내문 한 줄 추가")
            workbook.save(path)
            workbook.close()

            rows = area.load_kapt_area_rows(path)

        self.assertEqual(len(rows), 18_062)
        self.assertEqual(rows[0]["단지코드"], "A10023348")

    def test_missing_required_headers_raise_value_error_without_outputs(self):
        for header in area.REQUIRED_COLUMNS:
            with self.subTest(header=header), tempfile.TemporaryDirectory() as directory:
                path = self._copy_source(directory, "bad-header.xlsx")
                self._replace_header(path, header)
                out_area = pathlib.Path(directory) / "complex-area.csv"
                out_failures = pathlib.Path(directory) / "matching-failures.csv"

                with self.assertRaises(ValueError) as raised:
                    area.load_kapt_area_rows(path)

                self.assertIn(header, str(raised.exception))
                if header == area.HEADER_NAME:
                    self.assertIn("필수 헤더", str(raised.exception))
                self.assertFalse(out_area.exists())
                self.assertFalse(out_failures.exists())

                real_outputs_before = self._real_output_state()
                with (
                    contextlib.redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit) as main_exit,
                ):
                    area.main(path, out_area=out_area, out_failures=out_failures)
                self.assertIn(header, str(main_exit.exception))
                self.assertFalse(out_area.exists())
                self.assertFalse(out_failures.exists())
                self.assertEqual(self._real_output_state(), real_outputs_before)

    def test_header_without_data_raises_value_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "empty.xlsx"
            out_area = pathlib.Path(directory) / "complex-area.csv"
            out_failures = pathlib.Path(directory) / "matching-failures.csv"
            workbook = openpyxl.Workbook()
            workbook.active.append(list(area.REQUIRED_COLUMNS))
            workbook.save(path)
            workbook.close()

            with self.assertRaises(ValueError) as raised:
                area.load_kapt_area_rows(path)
            real_outputs_before = self._real_output_state()
            with self.assertRaises(SystemExit) as main_exit:
                area.main(path, out_area=out_area, out_failures=out_failures)
            self.assertIn("데이터 행", str(raised.exception))
            self.assertIn("데이터 행", str(main_exit.exception))
            self.assertFalse(out_area.exists())
            self.assertFalse(out_failures.exists())
            self.assertEqual(self._real_output_state(), real_outputs_before)

    def test_numeric_required_values_are_rejected(self):
        cases = (
            (area.HEADER_NAME, 1002334),
            ("주거전용면적(세부)", 84.9),
            ("세대수", 168),
        )
        for header, numeric_value in cases:
            with self.subTest(header=header), tempfile.TemporaryDirectory() as directory:
                path = pathlib.Path(directory) / "numeric.xlsx"
                workbook = openpyxl.Workbook()
                worksheet = workbook.active
                worksheet.append(list(area.REQUIRED_COLUMNS))
                row = {
                    "단지코드": "A-NUMERIC",
                    "주거전용면적(세부)": "84.9",
                    "세대수": "168",
                }
                row[header] = numeric_value
                worksheet.append([row[name] for name in area.REQUIRED_COLUMNS])
                workbook.save(path)
                workbook.close()

                with self.assertRaises(ValueError) as raised:
                    area.load_kapt_area_rows(path)

                self.assertIn(header, str(raised.exception))
                self.assertIn("문자열", str(raised.exception))

    def test_blank_row_after_header_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "blank-row.xlsx"
            workbook = openpyxl.Workbook()
            worksheet = workbook.active
            worksheet.append(list(area.REQUIRED_COLUMNS))
            worksheet.append(["A-FIRST", "59.9", "10"])
            worksheet.append([])
            worksheet.append(["A-SECOND", "84.9", "20"])
            workbook.save(path)
            workbook.close()

            rows = area.load_kapt_area_rows(path)

        self.assertEqual(len(rows), 3)
        self.assertTrue(all(value is None for value in rows[1].values()))

    def test_column_order_does_not_choose_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "reordered.xlsx"
            workbook = openpyxl.Workbook()
            worksheet = workbook.active
            worksheet.append(["안내"])
            worksheet.append(["세대수", "단지코드", "주거전용면적(세부)"])
            worksheet.append(["7", "A-ORDER", "84.9"])
            workbook.save(path)
            workbook.close()

            rows = area.load_kapt_area_rows(path)

        self.assertEqual(
            rows,
            [{"세대수": "7", "단지코드": "A-ORDER", "주거전용면적(세부)": "84.9"}],
        )

    def test_source_preserves_blank_and_zero_values(self):
        rows = area.load_kapt_area_rows(SOURCE)

        self.assertEqual(sum(row["읍면"] is None for row in rows), 18_062)
        self.assertEqual(sum(row["동수"] is None for row in rows), 3)
        self.assertEqual(sum(row["관리비부과면적"] is None for row in rows), 7)
        self.assertEqual(sum(row["주거전용면적(세부)"] is None for row in rows), 0)
        self.assertEqual(sum(row["세대수"] is None for row in rows), 0)
        self.assertEqual(sum(row["주거전용면적(세부)"] == "0" for row in rows), 12)
        self.assertEqual(sum(row["세대수"] == "0" for row in rows), 0)

    def test_area_text_matches_xlsx_stored_text(self):
        rows = area.load_kapt_area_rows(SOURCE)
        stored_areas = self._stored_column_text(SOURCE, "J")

        self.assertEqual(stored_areas[0], "주거전용면적(세부)")
        self.assertEqual(
            [row["주거전용면적(세부)"] for row in rows],
            stored_areas[1:],
        )

    def test_stored_text_reader_handles_inline_strings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "inline.xlsx"
            workbook = openpyxl.Workbook()
            worksheet = workbook.active
            worksheet.append(["주거전용면적(세부)"])
            worksheet.append(["84.9"])
            workbook.save(path)
            workbook.close()

            stored = self._stored_column_text(path, "A")

        self.assertEqual(stored, ["주거전용면적(세부)", "84.9"])


class AreaMatchingTest(unittest.TestCase):
    def test_display_groups_are_order_independent_and_keep_exact_gap_boundary(self):
        values = [59.9751, 59.9818, 78.7747, 84.5978, 84.9800, 109.8841]

        self.assertEqual(area.display_groups(values), area.display_groups(reversed(values)))
        self.assertEqual(len(area.display_groups([60, 60.999])), 1)
        self.assertEqual(len(area.display_groups([60, 61])), 2)
        self.assertEqual(len(area.display_groups([60, 61.001])), 2)

    def test_nearest_match_includes_half_and_rejects_outside_or_tie(self):
        self.assertEqual(
            area.nearest_kapt_area("60.5", ["60.0"]),
            (area.STATUS_MATCHED, Decimal("60")),
        )
        self.assertEqual(
            area.nearest_kapt_area("60.5001", ["60.0"]),
            (area.STATUS_OUTSIDE, None),
        )
        self.assertEqual(
            area.nearest_kapt_area("60.0", ["59.0", "61.0"]),
            (area.STATUS_OUTSIDE, None),
        )
        self.assertEqual(
            area.nearest_kapt_area("60.5", ["60.0", "61.0"]),
            (area.STATUS_TIE, None),
        )
        self.assertEqual(
            area.nearest_kapt_area("60.5", ["61.0", "60.0"]),
            (area.STATUS_TIE, None),
        )
        self.assertEqual(
            area.nearest_kapt_area("84.1", ["84.0", "84.2"]),
            (area.STATUS_TIE, None),
        )

    def test_label_uses_integer_households_then_area_tiebreak(self):
        larger_household_count = area.display_group_metadata(
            {Decimal("84.4"): "9", Decimal("85.3"): "10"}
        )
        tied_reversed = area.display_group_metadata(
            {Decimal("85.3"): "10", Decimal("84.4"): "10"}
        )

        self.assertEqual(larger_household_count[Decimal("84.4")]["group_label"], "85")
        self.assertEqual(tied_reversed[Decimal("85.3")]["group_label"], "84")

    def test_labels_use_round_half_up_at_adjacent_half_values(self):
        metadata = area.fallback_group_metadata([Decimal("84.5"), Decimal("85.5")])
        collision_guard = area.fallback_group_metadata(
            [Decimal("83.5"), Decimal("84.5")]
        )

        self.assertEqual(metadata[Decimal("84.5")]["group_label"], "85")
        self.assertEqual(metadata[Decimal("85.5")]["group_label"], "86")
        self.assertNotEqual(
            metadata[Decimal("84.5")]["group_label"],
            metadata[Decimal("85.5")]["group_label"],
        )
        self.assertEqual(collision_guard[Decimal("83.5")]["group_label"], "84")
        self.assertEqual(collision_guard[Decimal("84.5")]["group_label"], "85")

    def test_boundary_conflict_counter_is_symmetric(self):
        boundary = (Decimal("85"),)

        self.assertEqual(area.boundary_conflict_count("84.9", "85.1", boundary), 1)
        self.assertEqual(area.boundary_conflict_count("85.1", "84.9", boundary), 1)
        self.assertEqual(area.boundary_conflict_count("84.9", "84.95", boundary), 0)

    def test_build_rows_preserves_unmatched_nulls_and_failure_reasons(self):
        trade_rows = [
            {"apt_seq": "M", "exclu_use_ar": Decimal("60.5")},
            {"apt_seq": "O", "exclu_use_ar": Decimal("62")},
            {"apt_seq": "T", "exclu_use_ar": Decimal("60.5")},
            {"apt_seq": "N", "exclu_use_ar": Decimal("84.5")},
            {"apt_seq": "N", "exclu_use_ar": Decimal("85.5")},
        ]
        crosswalk = {
            apt: {
                "sgg_cd": "1",
                "apt_seq": apt,
                "apt_nm": apt,
                "road_key": "길 1",
                "kapt_code": code,
            }
            for apt, code in (("M", "KM"), ("O", "KO"), ("T", "KT"), ("N", ""))
        }
        kapt_index = {
            "KM": {Decimal("60"): 10},
            "KO": {Decimal("60"): 20},
            "KT": {Decimal("60"): 30, Decimal("61"): 40},
        }

        rows, failures, _ = area.build_area_rows(trade_rows, crosswalk, kapt_index)
        by_apt = {row["apt_seq"]: row for row in rows if row["apt_seq"] != "N"}
        no_kapt = [row for row in rows if row["apt_seq"] == "N"]

        self.assertEqual(by_apt["M"]["area_status"], area.STATUS_MATCHED)
        self.assertEqual(by_apt["M"]["kapt_area"], "60")
        self.assertEqual(by_apt["M"]["households"], "10")
        for apt in ("O", "T"):
            self.assertEqual(by_apt[apt]["kapt_area"], "")
            self.assertEqual(by_apt[apt]["households"], "")
        self.assertEqual(by_apt["O"]["area_status"], area.STATUS_OUTSIDE)
        self.assertEqual(by_apt["T"]["area_status"], area.STATUS_TIE)
        self.assertEqual(failures, {"O": {area.STATUS_OUTSIDE}, "T": {area.STATUS_TIE}})
        self.assertEqual([row["group_label"] for row in no_kapt], ["85", "86"])
        self.assertTrue(all(row["kapt_code"] == "" for row in no_kapt))
        self.assertTrue(all(row["households"] == "" for row in no_kapt))

    def test_failure_append_is_idempotent_and_keeps_road_block(self):
        road = {
            "sgg_cd": "1",
            "apt_seq": "R",
            "apt_nm": "도로 실패",
            "road_key": "",
            "fail_reason": "K-apt 마스터에 없음",
        }
        old_area = {
            "sgg_cd": "1",
            "apt_seq": "A",
            "apt_nm": "면적 실패",
            "road_key": "길 1",
            "fail_reason": area.STATUS_OUTSIDE,
        }
        crosswalk = {
            "A": {
                "sgg_cd": "1",
                "apt_seq": "A",
                "apt_nm": "면적 실패",
                "road_key": "길 1",
                "kapt_code": "K",
            }
        }

        first = area.build_failure_rows(
            [road, old_area], crosswalk, {"A": {area.STATUS_OUTSIDE}}
        )
        second = area.build_failure_rows(
            first, crosswalk, {"A": {area.STATUS_OUTSIDE}}
        )

        self.assertEqual(first, second)
        self.assertEqual(first[0], road)
        self.assertEqual(len(first), 2)

    def test_g4_requires_committed_tie_approval(self):
        tie = {
            "sgg_cd": "1",
            "apt_seq": "T",
            "apt_nm": "동점",
            "road_key": "길 1",
            "fail_reason": area.STATUS_TIE,
        }

        self.assertEqual(area.unapproved_tie_apts([tie], []), ["T"])
        self.assertEqual(area.unapproved_tie_apts([tie], [tie]), [])

    def test_g5b_recomputes_nearest_invariant_independently(self):
        trade_rows = [{"apt_seq": "A", "exclu_use_ar": Decimal("60.4")}]
        crosswalk = {
            "A": {
                "sgg_cd": "1",
                "apt_seq": "A",
                "apt_nm": "검증",
                "road_key": "길 1",
                "kapt_code": "K",
            }
        }
        kapt_index = {"K": {Decimal("60"): 10, Decimal("70"): 20}}
        original = area.nearest_kapt_area
        area.nearest_kapt_area = lambda exclu, candidates: (
            area.STATUS_MATCHED,
            max(candidates),
        )
        try:
            rows, _, diagnostics = area.build_area_rows(
                trade_rows, crosswalk, kapt_index
            )
            with self.assertRaisesRegex(ValueError, "G5b"):
                area._validate_output_rows(
                    rows,
                    diagnostics["combos_by_apt"],
                    crosswalk,
                    kapt_index,
                )
        finally:
            area.nearest_kapt_area = original

    def test_source_group_metadata_is_order_independent_for_every_kapt_code(self):
        index = area.build_kapt_index(area.load_kapt_area_rows(SOURCE))
        crosswalk = area.load_crosswalk()
        referenced_codes = sorted(
            {
                row["kapt_code"]
                for row in crosswalk.values()
                if row["kapt_code"] in index
            }
        )
        rng = random.Random(0)

        mismatches = []
        for code in referenced_codes:
            values = index[code]
            baseline = area.display_group_metadata(values)
            reversed_values = dict(reversed(list(values.items())))
            if area.display_group_metadata(reversed_values) != baseline:
                mismatches.append(code)
                continue
            for _ in range(3):
                shuffled_values = list(values.items())
                rng.shuffle(shuffled_values)
                if area.display_group_metadata(dict(shuffled_values)) != baseline:
                    mismatches.append(code)
                    break

        self.assertEqual(len(referenced_codes), 2_274)
        self.assertEqual(mismatches, [])

    def test_source_half_up_regressions_and_full_kapt_axis(self):
        index = area.build_kapt_index(area.load_kapt_area_rows(SOURCE))
        cases = (
            ("A10022623", Decimal("44.5"), "45"),
            ("A10024240", Decimal("148.5"), "149"),
            ("A12010103", Decimal("58.5"), "59"),
        )

        for code, source_area, expected_label in cases:
            with self.subTest(code=code, source_area=source_area):
                metadata = area.display_group_metadata(index[code])
                self.assertEqual(metadata[source_area]["group_label"], expected_label)

        gaepo = area.display_group_metadata(index["A10023348"])
        self.assertEqual(len({record["group_id"] for record in gaepo.values()}), 11)


if __name__ == "__main__":
    unittest.main()
