import contextlib
import hashlib
import io
import pathlib
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

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


if __name__ == "__main__":
    unittest.main()
