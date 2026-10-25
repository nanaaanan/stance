import csv
import hashlib
import io
from contextlib import redirect_stdout
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


class RebMatchingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.crosswalk = matching._load_local_crosswalk()

    def index(self, entries):
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=[
            "단지고유번호", "도로명주소", "단지명_공시가격", "사용승인일",
            "주소", "단지명_도로명주소",
        ])
        writer.writeheader()
        for complex_id, address in entries:
            writer.writerow({"단지고유번호": complex_id, "도로명주소": address,
                             "단지명_공시가격": "같은 이름", "사용승인일": "20000101",
                             "주소": "테스트로 1", "단지명_도로명주소": "테스트로 1"})
        content = output.getvalue().encode("cp949")
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = pathlib.Path(directory.name) / "reb.csv"
        path.write_bytes(content)
        return matching.RebIndex(path, hashlib.sha256(content).hexdigest())

    def trade(self, apt="apt", road="테스트로", number="1", gu="종로구", count=7):
        return {"apt_seq": apt, "road_nm": road, "road_nm_bonbun": number,
                "road_nm_bubun": "0", "_district_name": gu, "trade_count": count,
                "apt_nm": "테스트", "deal_date": "2026-01-01", "floor": 1,
                "exclu_use_ar": "84"}

    def measure(self, entries, trades, active=(), order=(1, 2, 3, 4)):
        return matching.measure_reb_contribution(
            self.index(entries), trades, self.crosswalk.representative, active, order)

    def test_raw_key_and_seoul_road_names(self):
        for address in ("서울숲2길 32-14", "서울시립대로 111", "테스트로  1", "분리자없음"):
            self.assertEqual(matching.reb_key(" " + address + " "), address)
        for number in matching.NORMALIZATION_NAMES:
            self.assertEqual(matching.reb_key("분리자없음", (number,)), "분리자없음")

    def test_each_variant_has_three_negative_inputs(self):
        addresses = ((1, "테스트로  1"), (1, "테스트로 \t1"), (2, "테스트로 001"),
                     (3, "테스트로 1-0"), (4, "테스트로\u30001"))
        for intended, address in addresses:
            index = self.index([("11110000000001 ", address)])
            self.assertEqual(index.source_rows, 1)
            self.assertEqual(index.key_rows, 1)
            self.assertFalse(matching.match_reb(index, {"종로구"}, "테스트로 1", ())["reb_complex_id"])
            self.assertEqual(matching.reb_key(address), address)
            for number in matching.NORMALIZATION_NAMES:
                with self.subTest(address=address, intended=intended, number=number):
                    result = matching.match_reb(index, {"종로구"}, "테스트로 1", (number,))
                    self.assertEqual(bool(result["reb_complex_id"]), intended == number)
                    if intended == number:
                        self.assertEqual(result["reb_complex_id"], "11110000000001")
                        self.assertEqual(result["reb_match_source"], f"변형 {number} 적용 후 일치")

    def test_adapter_preserves_space_structure_without_variant_one(self):
        address = "선릉로  1길  \u3000001"
        self.assertEqual(matching.reb_key(address, (2,)), "선릉로  1길  \u30001")
        self.assertEqual(matching.reb_key(address, (4,)), "선릉로  1길   001")
        self.assertEqual(matching.reb_key(address, (1, 2, 4)), "선릉로 1길 1")

    def test_id_validation(self):
        for complex_id in ("1111000000000", "111100000000001", "\uff11\uff11\uff11\uff11\uff10\uff10\uff10\uff10\uff10\uff10\uff10\uff10\uff10\uff11",
                           "11110abcdefghi", "99999000000001"):
            with self.subTest(complex_id=complex_id), self.assertRaises(SystemExit):
                self.index([(complex_id, "테스트로 1")])

    def test_gu_partitions_and_distinct_ids(self):
        index = self.index([("11110000000001", "테스트로 1"),
                            ("11110000000001", "테스트로 1"),
                            ("11140000000001", "테스트로 1")])
        single = matching.match_reb(index, {"종로구"}, "테스트로 1")
        self.assertEqual(single["reb_complex_id"], "11110000000001")
        self.assertEqual(single["reb_match_source"], "원문 일치")
        both = matching.match_reb(index, {"종로구", "중구"}, "테스트로 1")
        self.assertEqual(both["reb_complex_id"], "")
        self.assertEqual(both["fail_reason"], "후보 2개 이상(모호)")
        self.assertEqual(index.duplicate_keys, 1)
        self.assertEqual(index.duplicate_rows, 2)

    def test_raw_ambiguity_stops_before_variants(self):
        index = self.index([("11110000000001", "테스트로 1"),
                            ("11110000000002", "테스트로 1")])
        with mock.patch.object(index, "lookup", wraps=index.lookup) as lookup:
            result = matching.match_reb(index, {"종로구"}, "테스트로 1", (1, 2))
        self.assertEqual(lookup.call_count, 1)
        self.assertEqual(result["reb_complex_id"], "")
        self.assertEqual(result["reb_match_source"], "")

    def test_all_variant_candidates_are_merged(self):
        entries = [("11110000000001", "선릉로  1길 1"),
                   ("11110000000002", "선릉로 1길 001")]
        for extra in ([], [("11110000000003", "선릉로  1길 1")]):
            index = self.index(entries + extra)
            result = matching.match_reb(index, {"종로구"}, "선릉로 1길 1", (2, 1))
            self.assertEqual(result["reb_complex_id"], "")
            self.assertEqual(result["fail_reason"], "후보 2개 이상(모호)")
            self.assertEqual(len(result["candidates"]), 2 + len(extra))

    def test_combination_source_uses_sorted_numbers(self):
        index = self.index([("11110000000001", "테스트로  001")])
        result = matching.match_reb(index, {"종로구"}, "테스트로 1", (2, 1))
        self.assertEqual(result["reb_match_source"], "변형 1+2 적용 후 일치")

    def test_empty_keys_and_nonmatching_names(self):
        index = self.index([("11110000000001", ""), ("11110000000002", "다른로 1")])
        self.assertEqual(index.key_rows, 1)
        self.assertEqual(matching.match_reb(index, {"종로구"}, "")["fail_reason"], "도로명 키 생성 불가")
        self.assertEqual(matching.match_reb(index, {"종로구"}, "테스트로 1")["fail_reason"], "후보 없음")

    def test_measurement_is_independent_and_order_invariant(self):
        entries = [("11110000000001", "공백로  1"),
                   ("11110000000002", "전각로\u30001"),
                   ("11110000000003", "조합로  001")]
        trades = [self.trade("space", "공백로", count=3),
                  self.trade("width", "전각로", count=5),
                  self.trade("both", "조합로", count=11)]
        report = self.measure(entries, trades, (1, 2))
        reverse = self.measure(list(reversed(entries)), list(reversed(trades)), (1, 2), (4, 3, 2, 1))
        self.assertEqual(report, reverse)
        self.assertEqual(report["baseline"], 0)
        self.assertEqual(report["total"], 3)
        self.assertEqual(report["eligible"], (1, 4))
        expected = {1: (1, 3), 2: (0, 0), 3: (0, 0), 4: (1, 5), "final": (2, 14)}
        for label, (new, deals) in expected.items():
            result = report["results"][label]
            self.assertEqual((result["new"], result["deals"]), (new, deals))
            self.assertEqual((result["lost"], result["ambiguous"], result["conflicts"]), (0, 0, 0))

    def test_collision_does_not_appear_as_operational_loss(self):
        entries = [("11110000000001", "테스트로 1"),
                   ("11110000000002", "테스트로 001"),
                   ("11110000000003", "신규로 001")]
        report = self.measure(entries, [self.trade("collision"), self.trade("new", "신규로")], (2,))
        result = report["results"][2]
        self.assertEqual((result["new"], result["lost"], result["ambiguous"], result["conflicts"]),
                         (1, 0, 0, 1))
        self.assertEqual(result["conflict_apts"], ["collision"])
        self.assertNotIn(2, report["eligible"])
        self.assertEqual(report["results"]["final"]["conflicts"], 1)

    def test_representative_is_crosswalk_function(self):
        index = self.index([("11110000000001", "테스트로 1")])
        rows = [dict(self.trade(), apt_nm="나"), dict(self.trade(), apt_nm="가")]
        with mock.patch.object(self.crosswalk, "representative", wraps=self.crosswalk.representative) as choose:
            result = matching.match_reb_complex(index, {"종로구"}, rows, choose)
        choose.assert_called_once_with(rows)
        self.assertEqual(self.crosswalk.representative(rows)["apt_nm"], "가")
        self.assertTrue(result["reb_complex_id"])

    def test_report_rejects_active_list_that_differs_from_measurement(self):
        index = self.index([("11110000000001", "테스트로  1")])
        rows = [self.trade()]
        with mock.patch.object(matching, "RebIndex", return_value=index), \
                mock.patch.object(matching, "_load_local_crosswalk", return_value=self.crosswalk), \
                mock.patch.object(matching, "_load_local_coverage") as coverage, \
                mock.patch.object(self.crosswalk, "load_districts", return_value=[("11110", "종로구")]), \
                mock.patch.object(self.crosswalk, "fetch_district", return_value=rows), \
                redirect_stdout(io.StringIO()):
            coverage.return_value.sb_config.return_value = ("unused", "unused")
            with self.assertRaisesRegex(SystemExit, "운영 변형이 다릅니다"):
                matching.report_reb_normalization_contribution()

    def test_default_match_has_no_zero_contribution_variant(self):
        self.assertEqual(matching.ACTIVE_REB_NORMALIZATION_VARIANTS, ())
        index = self.index([("11110000000001", "테스트로  1")])
        with mock.patch.object(index, "lookup", wraps=index.lookup) as lookup:
            result = matching.match_reb(index, {"종로구"}, "테스트로 1")
        self.assertEqual(result["reb_complex_id"], "")
        lookup.assert_called_once_with({"종로구"}, "테스트로 1", ())

    def test_report_rejects_conflict_only_in_final_combination(self):
        index = self.index([("11110000000001", "테스트로 1"),
                            ("11110000000002", "테스트로  001"),
                            ("11110000000003", "공백로  1"),
                            ("11110000000004", "제로로 001")])
        rows = [self.trade("collision"), self.trade("space", "공백로"),
                self.trade("zero", "제로로")]
        report = matching.measure_reb_contribution(index, rows, self.crosswalk.representative, (1, 2))
        self.assertEqual(report["eligible"], (1, 2))
        for number in (1, 2):
            result = report["results"][number]
            self.assertEqual((result["new"], result["lost"], result["conflicts"]), (1, 0, 0))
        final = report["results"]["final"]
        self.assertEqual(final["lost"], 0)
        self.assertEqual(final["conflicts"], 1)
        self.assertEqual(final["conflict_apts"], ["collision"])
        with mock.patch.object(matching, "ACTIVE_REB_NORMALIZATION_VARIANTS", (1, 2)), \
                mock.patch.object(matching, "RebIndex", return_value=index), \
                mock.patch.object(matching, "_load_local_crosswalk", return_value=self.crosswalk), \
                mock.patch.object(matching, "_load_local_coverage") as coverage, \
                mock.patch.object(self.crosswalk, "load_districts", return_value=[("11110", "종로구")]), \
                mock.patch.object(self.crosswalk, "fetch_district", return_value=rows), \
                redirect_stdout(io.StringIO()):
            coverage.return_value.sb_config.return_value = ("unused", "unused")
            with self.assertRaisesRegex(SystemExit, "운영 변형이 다릅니다"):
                matching.report_reb_normalization_contribution()

    def test_parses_checked_bytes_when_file_changes_after_read(self):
        read_bytes = pathlib.Path.read_bytes

        def read_then_replace(path):
            content = read_bytes(path)
            path.write_bytes(content.replace("기존로 1".encode("cp949"),
                                             "바뀐로 1".encode("cp949")))
            return content

        with mock.patch.object(pathlib.Path, "read_bytes", read_then_replace):
            index = self.index([("11110000000001", "기존로 1")])
        self.assertEqual(index.lookup({"종로구"}, "기존로 1"), {"11110000000001"})
        self.assertEqual(index.lookup({"종로구"}, "바뀐로 1"), set())

    def test_sha_rejects_changed_content_and_short_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "reb.csv"
            path.write_bytes(b"original")
            expected = hashlib.sha256(path.read_bytes()).hexdigest()
            path.write_bytes(b"changed")
            with self.assertRaisesRegex(SystemExit, "변형 기여를 다시 측정하라"):
                matching.RebIndex(path, expected)
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            for invalid in (actual[:8], actual[:-1] + ("0" if actual[-1] != "0" else "1")):
                with self.assertRaises(SystemExit):
                    matching.RebIndex(path, invalid)

    def test_default_path_always_checks_sha(self):
        with mock.patch.object(pathlib.Path, "read_bytes", return_value=b"changed"):
            with self.assertRaisesRegex(SystemExit, "SHA-256"):
                matching.RebIndex()

    def test_checked_bytes_require_cp949_and_headers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "reb.csv"
            for content in (b"wrong,header\n", b"\xff"):
                path.write_bytes(content)
                with self.assertRaises(SystemExit):
                    matching.RebIndex(path, hashlib.sha256(content).hexdigest())


if __name__ == "__main__":
    unittest.main()
