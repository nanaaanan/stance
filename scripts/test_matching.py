import pathlib
import tempfile
import unittest
from unittest import mock

from scripts import matching


class KaptNormalizationTest(unittest.TestCase):
    def test_each_allowed_variant(self):
        cases = (
            (("  선릉로  1길  ", "  221  ", (1,)), "선릉로 1길 221"),
            (("선릉로", "00221-00001", (2,)), "선릉로 221-1"),
            (("선릉로", "221-00", (3,)), "선릉로 221"),
            (("선릉로\u3000", "221\uff0d1", (4,)), "선릉로 221-1"),
        )
        for args, expected in cases:
            with self.subTest(variant=args[2]):
                self.assertEqual(matching.kapt_key(*args), expected)

    def test_zero_padding_does_not_change_value(self):
        self.assertEqual(matching.kapt_key("선릉로", "1000", (2,)), "선릉로 1000")
        self.assertEqual(matching.kapt_key("선릉로", "221-10", (2,)), "선릉로 221-10")
        self.assertEqual(matching.kapt_key("선릉로", "221-01", (2,)), "선릉로 221-1")

    def test_subnumber_zero_only_removes_all_zero_suffix(self):
        self.assertEqual(matching.kapt_key("선릉로", "221-0", (3,)), "선릉로 221")
        self.assertEqual(matching.kapt_key("선릉로", "221-00", (3,)), "선릉로 221")
        self.assertEqual(matching.kapt_key("선릉로", "221-10", (3,)), "선릉로 221-10")
        self.assertEqual(matching.kapt_key("선릉로", "221-01", (3,)), "선릉로 221-01")

    def test_zero_padding_leaves_non_numeric_detail_unchanged(self):
        self.assertEqual(matching.kapt_key("선릉로", "221가", (2,)), "선릉로 221가")
        self.assertEqual(matching.kapt_key("선릉로", "221-가", (2,)), "선릉로 221-가")
        self.assertEqual(matching.kapt_key("선릉로", "22²", (2,)), "선릉로 22²")

    def test_active_path_uses_only_variant_three(self):
        self.assertEqual(matching.ACTIVE_NORMALIZATION_VARIANTS, (3,))
        self.assertEqual(
            matching.kapt_key("선릉로", "221-01", matching.ACTIVE_NORMALIZATION_VARIANTS),
            "선릉로 221-01",
        )


class MatchingSafetyTest(unittest.TestCase):
    def test_default_index_load_checks_snapshot(self):
        with mock.patch.object(matching, "_validate_active_normalization_snapshot") as validate:
            with mock.patch.object(matching.KaptIndex, "read_rows", return_value=[]):
                matching.KaptIndex.load()
        validate.assert_called_once_with()

    def test_explicit_measurement_index_skips_active_snapshot_gate(self):
        with mock.patch.object(matching, "_validate_active_normalization_snapshot") as validate:
            with mock.patch.object(matching.KaptIndex, "read_rows", return_value=[]):
                matching.KaptIndex.load(())
        validate.assert_not_called()

    def test_changed_snapshot_stops_active_load(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "kapt.csv"
            path.write_bytes(b"changed")
            with mock.patch.object(matching, "KAPT_CSV_PATH", path):
                with self.assertRaises(SystemExit):
                    matching._validate_active_normalization_snapshot()

    def test_coverage_module_is_loaded_from_repo(self):
        coverage = matching._load_local_coverage()
        expected = pathlib.Path(matching.__file__).with_name("coverage.py").resolve()
        self.assertEqual(pathlib.Path(coverage.__file__).resolve(), expected)

    def test_contribution_keeps_new_and_lost_separate(self):
        baseline = {
            "new": ("unmatched", 3),
            "lost": ("matched", 5),
            "same": ("matched", 7),
        }
        compared = {
            "new": ("matched", 3),
            "lost": ("ambiguous", 5),
            "same": ("matched", 7),
        }
        result = matching._contribution(baseline, compared)
        self.assertEqual(
            {name: result[name] for name in ("new", "lost", "ambiguous", "deals")},
            {"new": 1, "lost": 1, "ambiguous": 1, "deals": 3},
        )


if __name__ == "__main__":
    unittest.main()
