import unittest
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


if __name__ == "__main__":
    unittest.main()
