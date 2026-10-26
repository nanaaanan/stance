import unittest
import copy
import csv
import hashlib
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
import tempfile

from scripts import crosswalk, matching


def trade_row(apt, road, bon, bubun="00000", district="테스트구", code="1", name="거래단지"):
    return {
        "apt_seq": apt,
        "apt_nm": name,
        "sgg_cd": code,
        "_district_name": district,
        "road_nm": road,
        "road_nm_bonbun": bon,
        "road_nm_bubun": bubun,
        "deal_date": "2026-01-01",
        "exclu_use_ar": Decimal("59"),
        "floor": 1,
        "trade_count": 1,
    }


class CrosswalkBuildTest(unittest.TestCase):
    def setUp(self):
        source = [
            {
                "주소(도로명)": "테스트로",
                "주소(도로상세주소)": "1",
                "주소(시군구)": "테스트구",
                "k-아파트코드": "K-1",
                "k-아파트명": "첫단지",
            },
            {
                "주소(도로명)": "테스트로",
                "주소(도로상세주소)": "2-0",
                "주소(시군구)": "테스트구",
                "k-아파트코드": "K-2",
                "k-아파트명": "둘단지",
            },
            {
                "주소(도로명)": "테스트로",
                "주소(도로상세주소)": "3",
                "주소(시군구)": "테스트구",
                "k-아파트코드": "K-3A",
                "k-아파트명": "셋A",
            },
            {
                "주소(도로명)": "테스트로",
                "주소(도로상세주소)": "3",
                "주소(시군구)": "테스트구",
                "k-아파트코드": "K-3B",
                "k-아파트명": "셋B",
            },
        ]
        self.indexes = {
            variants: matching.KaptIndex.from_rows(source, variants)
            for variants in ((), (3,))
        }

    def test_original_and_variant_matches_are_labeled(self):
        rows = [
            trade_row("A1", "테스트로", "00001"),
            trade_row("A2", "테스트로", "00002"),
        ]
        walk, failures = crosswalk.build_rows(rows, self.indexes, (3,))
        self.assertEqual([row["match_source"] for row in walk], [
            "원문 일치", "변형 3 적용 후 일치",
        ])
        self.assertEqual(failures, [])

    def test_ambiguous_and_no_key_are_failures(self):
        rows = [
            trade_row("A3", "테스트로", "00003"),
            trade_row("A4", "", "00000"),
        ]
        walk, failures = crosswalk.build_rows(rows, self.indexes, (3,))
        self.assertEqual([row["kapt_code"] for row in walk], ["", ""])
        self.assertEqual(
            {row["apt_seq"]: row["fail_reason"] for row in failures},
            {"A3": "후보 2개 이상(모호)", "A4": "도로명 키 생성 불가"},
        )

    def test_rows_are_sorted_and_sgg_uses_mode(self):
        rows = [
            trade_row("A2", "테스트로", "00001", code="2", district="구2"),
            trade_row("A1", "테스트로", "00001", code="1", district="구1"),
            trade_row("A1", "테스트로", "00001", code="1", district="구1"),
        ]
        indexes = {(): matching.KaptIndex.from_rows([
            {
                "주소(도로명)": "테스트로", "주소(도로상세주소)": "1",
                "주소(시군구)": "구1", "k-아파트코드": "K-1", "k-아파트명": "첫단지",
            },
        ], ())}
        walk, failures = crosswalk.build_rows(rows, indexes, ())
        self.assertEqual([row["apt_seq"] for row in walk], ["A1", "A2"])
        self.assertEqual(walk[0]["sgg_cd"], "1")
        self.assertEqual(walk[1]["match_source"], "")
        self.assertEqual(failures[0]["fail_reason"], "K-apt 마스터에 없음")
        self.assertEqual([row["apt_seq"] for row in failures], ["A2"])

    def test_csv_uses_utf8_bom_and_lf(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "crosswalk.csv"
            crosswalk.write_csv(path, ["value"], [{"value": "가"}])
            data = path.read_bytes()
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\r\n", data)

    def test_same_code_two_rows_in_one_gu_stays_ambiguous(self):
        index = matching.KaptIndex({
            "테스트구": {"테스트로 1": [("K-1", "하나"), ("K-1", "하나")]}},
            source_rows=2,
            key_rows=2,
        )
        candidates = crosswalk.candidate_map(index, {"테스트구"}, "테스트로 1")
        self.assertEqual(len(candidates), 2)

    def test_empty_kapt_code_cannot_be_a_success(self):
        index = matching.KaptIndex({
            "테스트구": {"테스트로 1": [("", "코드없음")]}},
            source_rows=1,
            key_rows=1,
        )
        result = crosswalk.match_one({(): index}, (), {"테스트구"}, "테스트로 1")
        self.assertEqual(result["kapt_code"], "")
        self.assertEqual(result["fail_reason"], "K-apt 마스터에 없음")


class RebCrosswalkTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.entries = [
            ("11110100000001", "테스트로 1", "거래단지", "2000-01-01"),
            ("11110100000002", "테스트로 2", "거래단지", "2000-01-01"),
            ("11110100000003", "테스트로 2", "다른이름", "2000-01-01"),
            ("11110100000004", "테스트로 4", "거래단지", "2000-02-30"),
        ]
        source = self.path / "snapshot.csv"
        self.write_registry(source, self.entries)
        self.index = matching.RebIndex(source, hashlib.sha256(source.read_bytes()).hexdigest())
        kapt_rows = [
            {
                "주소(도로명)": "테스트로", "주소(도로상세주소)": detail,
                "주소(시군구)": district, "k-아파트코드": code, "k-아파트명": name,
            }
            for detail, district, code, name in [
                ("1-0", "종로구", "K-1", "성공단지"),
                ("2", "종로구", "K-2A", "모호A"),
                ("2", "종로구", "K-2B", "모호B"),
                ("3", "종로구", "K-3", "중복단지"),
                ("3", "종로구", "K-3", "중복단지"),
                ("4", "종로구", "K-4", "공유단지"),
                ("4", "중구", "K-4", "공유단지"),
                ("5", "종로구", "K-5", "동률단지"),
            ]
        ]
        self.kapt_variants = matching.ACTIVE_NORMALIZATION_VARIANTS
        variant_sets = {(), tuple(self.kapt_variants)}
        variant_sets.update((variant,) for variant in self.kapt_variants)
        self.kapt_indexes = {
            variants: matching.KaptIndex.from_rows(kapt_rows, variants)
            for variants in variant_sets
        }

    def write_registry(self, path, entries, others=()):
        with path.open("w", encoding="cp949", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["단지고유번호", "도로명주소", "단지명_공시가격", "사용승인일", "단지종류"])
            writer.writerows([(*entry, "1") for entry in entries])
            writer.writerows([(*entry, "2") for entry in others])

    def trades(self):
        return [
            {**trade_row(apt, road, number, district="종로구", code="11110"),
             "build_year": year}
            for apt, road, number, year in [
                ("A1", "테스트로", "1", 1990),
                ("A2", "테스트로", "2", 2000),
                ("A3", "테스트로", "3", 2000),
                ("A4", "", "0", None),
                ("A5", "테스트로", "1", None),
                ("A6", "테스트로", "4", 2000),
            ]
        ]

    def build(self, trades):
        walk, failures = crosswalk.build_rows(trades, self.kapt_indexes, self.kapt_variants)
        return crosswalk.add_reb_layer(walk, failures, trades, self.index)

    def test_headers_and_layer_failures(self):
        self.assertEqual(crosswalk.CROSSWALK_COLUMNS, [
            "sgg_cd", "apt_seq", "apt_nm", "road_key", "kapt_code", "kapt_key",
            "match_source", "reb_complex_id", "reb_match_source"])
        self.assertEqual(crosswalk.FAILURE_COLUMNS, [
            "sgg_cd", "apt_seq", "apt_nm", "road_key", "fail_reason", "fail_layer"])
        walk, failures, results = self.build(self.trades())
        by_apt = {row["apt_seq"]: row for row in walk}
        self.assertEqual(len(by_apt), 6)
        self.assertEqual(by_apt["A2"]["reb_complex_id"], "")
        reb_failures = [row for row in failures if row["fail_layer"] == "단지 명부"]
        self.assertEqual([(row["apt_seq"], row["fail_reason"]) for row in reb_failures], [
            ("A2", "후보 2개 이상(모호)"), ("A3", "후보 없음"), ("A4", "도로명 키 생성 불가")])
        self.assertEqual([row["fail_layer"] for row in failures if row["apt_seq"] == "A4"],
                         ["단지 명부", "관리정보"])
        self.assertFalse(any(row["fail_layer"] == "면적" for row in failures))
        self.assertEqual({apt for apt, result in results.items() if len(result["candidates"]) > 1},
                         {row["apt_seq"] for row in reb_failures
                          if row["fail_reason"] == "후보 2개 이상(모호)"})

    def test_observations_keep_pairs_and_rows_unchanged(self):
        trades = self.trades()
        walk, failures, results = self.build(trades)
        before = copy.deepcopy((walk, failures, results))
        stats = crosswalk.observe_reb(walk, results, trades, self.index)
        self.assertEqual((walk, failures, results), before)
        self.assertEqual(stats["matched"], 3)
        self.assertEqual((stats["shared_ids"], stats["shared_apts"]), (1, 2))
        self.assertEqual(stats["year_differences"], {-10: 1})
        self.assertEqual(stats["trade_year_missing"], 1)
        self.assertEqual(stats["approval_date_missing"], 1)
        self.assertEqual(stats["same_name"], 3)
        self.assertEqual(stats["ambiguous_names"], {1: 1})
        self.assertEqual(stats["year_pairs"], [(-10, "A1", "거래단지", "거래단지", 1990, 2000)])
        self.assertIn("미측정 (원본 경로 미지정)", "\n".join(crosswalk.reb_report_lines(stats, self.index)))
        self.assertEqual(self.build(list(reversed(trades))), (walk, failures, results))

    def test_other_type_candidates_are_observed_without_resolving_ambiguity(self):
        raw = self.path / "raw.csv"
        self.write_registry(raw, self.entries, [
            ("11110200000001", "테스트로 3", "다른종류", "2000-01-01"),
            ("11110200000002", "테스트로 2", "거래단지", "2000-01-01"),
        ])
        other = crosswalk.OtherRebIndex(raw, self.index)
        trades = self.trades()
        walk, failures, results = self.build(trades)
        stats = crosswalk.observe_reb(walk, results, trades, self.index, other)
        self.assertEqual(stats["other_only"], 1)
        self.assertEqual(walk[1]["reb_complex_id"], "")

    def test_raw_missing_and_snapshot_mismatch_stop(self):
        with self.assertRaisesRegex(SystemExit, "원본 읽기 실패"):
            crosswalk.OtherRebIndex(self.path / "absent.csv", self.index)
        raw = self.path / "raw.csv"
        self.write_registry(raw, self.entries[:-1])
        with self.assertRaisesRegex(SystemExit, "집합 불일치"):
            crosswalk.OtherRebIndex(raw, self.index)

    def test_raw_errors_exit_before_outputs_change(self):
        raw = self.path / "raw.csv"
        self.write_registry(raw, [])
        for path, message in ((self.path / "absent.csv", "원본 읽기 실패"), (raw, "집합 불일치")):
            before = [p.read_bytes() for p in (crosswalk.OUT_CROSSWALK, crosswalk.OUT_FAILURES)]
            run = subprocess.run([sys.executable, str(Path(crosswalk.__file__)),
                                  "--reb-raw", str(path)], capture_output=True, text=True)
            self.assertNotEqual(run.returncode, 0)
            self.assertIn(message, run.stderr)
            self.assertEqual([p.read_bytes() for p in (crosswalk.OUT_CROSSWALK, crosswalk.OUT_FAILURES)], before)

    def test_kapt_columns_and_failures_survive_layer_addition(self):
        trades = [
            trade_row("K1", "테스트로", "1", district="종로구", code="11110"),
            trade_row("K2", "테스트로", "2", district="종로구", code="11110"),
            trade_row("K3", "테스트로", "3", district="종로구", code="11110"),
            trade_row("K4", "테스트로", "4", district="중구", code="11140"),
            trade_row("K4", "테스트로", "4", district="종로구", code="11110"),
            trade_row("K5", "테스트로", "5", district="종로구", code="11110", name="나단지"),
            trade_row("K5", "테스트로", "5", district="종로구", code="11110", name="가단지"),
        ]
        expected_walk = [
            {"sgg_cd": "11110", "apt_seq": "K1", "apt_nm": "거래단지",
             "road_key": "테스트로 1", "kapt_code": "K-1", "kapt_key": "테스트로 1",
             "match_source": "변형 3 적용 후 일치"},
            {"sgg_cd": "11110", "apt_seq": "K2", "apt_nm": "거래단지",
             "road_key": "테스트로 2", "kapt_code": "", "kapt_key": "", "match_source": ""},
            {"sgg_cd": "11110", "apt_seq": "K3", "apt_nm": "거래단지",
             "road_key": "테스트로 3", "kapt_code": "", "kapt_key": "", "match_source": ""},
            {"sgg_cd": "11110", "apt_seq": "K4", "apt_nm": "거래단지",
             "road_key": "테스트로 4", "kapt_code": "K-4", "kapt_key": "테스트로 4",
             "match_source": "원문 일치"},
            {"sgg_cd": "11110", "apt_seq": "K5", "apt_nm": "가단지",
             "road_key": "테스트로 5", "kapt_code": "K-5", "kapt_key": "테스트로 5",
             "match_source": "원문 일치"},
        ]
        expected_failures = [
            {"sgg_cd": "11110", "apt_seq": "K2", "apt_nm": "거래단지",
             "road_key": "테스트로 2", "fail_reason": "후보 2개 이상(모호)",
             "fail_layer": "관리정보"},
            {"sgg_cd": "11110", "apt_seq": "K3", "apt_nm": "거래단지",
             "road_key": "테스트로 3", "fail_reason": "후보 2개 이상(모호)",
             "fail_layer": "관리정보"},
        ]
        for trade_rows in (trades, list(reversed(trades))):
            with self.subTest(reversed=trade_rows is not trades):
                kapt_walk, kapt_failures = crosswalk.build_rows(
                    trade_rows, self.kapt_indexes, self.kapt_variants)
                self.assertEqual(kapt_walk, expected_walk)
                self.assertEqual([{**row, "fail_layer": "관리정보"} for row in kapt_failures],
                                 expected_failures)
                walk, failures, _ = crosswalk.add_reb_layer(
                    kapt_walk, kapt_failures, trade_rows, self.index)
                self.assertEqual([
                    {k: row[k] for k in (
                        "sgg_cd", "apt_seq", "apt_nm", "road_key",
                        "kapt_code", "kapt_key", "match_source")}
                    for row in walk
                ], expected_walk)
                self.assertEqual([row for row in failures if row["fail_layer"] == "관리정보"],
                                 expected_failures)


if __name__ == "__main__":
    unittest.main()
