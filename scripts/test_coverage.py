import csv
import random
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from scripts import matching

# coverage.py 의 직접 실행과 같은 인접 모듈 이름으로 테스트에 연결
sys.modules["matching"] = matching
from scripts import coverage


RECENT_START = "202508"
EXISTING_COLUMNS = (
    "lawd_cd",
    "district_name",
    "deal_rows",
    "deal_count",
    "complexes",
    "complexes_kapt_matched",
    "complex_match_rate",
    "deals_kapt_matched",
    "deal_weighted_match_rate",
    "combos_36m",
    "combos_recent12m",
    "coverage_rate",
    "direct_deal_count",
    "direct_deal_rate",
    "cancel_count",
    "cancel_rate",
)
NEW_COLUMNS = (
    "combos_36m_group",
    "combos_recent12m_group",
    "combos_recent12m_group_valid",
    "combos_1or2_all",
    "combos_1or2_valid",
)


def make_row(
    apt,
    area,
    count=1,
    ym="202601",
    road="테스트로",
    bon="00001",
    dealing="중개거래",
    cancel=None,
    ambiguous=False,
    day="2026-01-01",
):
    return {
        "apt_seq": apt,
        "exclu_use_ar": Decimal(area),
        "trade_count": count,
        "deal_ym": ym,
        "road_nm": road,
        "road_nm_bonbun": bon,
        "road_nm_bubun": "00000",
        "dealing_gbn": dealing,
        "cdeal_type": cancel,
        "ambiguous_cancel": ambiguous,
        "deal_date": day,
        "floor": 1,
    }


def aggregate(rows):
    by_gu = {
        "테스트구": {
            "테스트로 1": [("K-A", "A")],
            "테스트로 2": [("K-B", "B")],
        }
    }
    index = matching.KaptIndex(by_gu, source_rows=2, key_rows=2)
    return coverage.aggregate(rows, "테스트구", index, RECENT_START)


def regression_rows():
    return [
        make_row("APT-A", "59.0000", 2, "202401", day="2024-01-01"),
        make_row("APT-A", "59.9000", 1, "202601"),
        make_row("APT-A", "60.8000", 2, "202602", dealing="직거래", day="2026-02-01"),
        make_row(
            "APT-A", "61.7000", 3, "202603", cancel="O",
            ambiguous=True, day="2026-03-01",
        ),
        make_row("APT-B", "70.0000", 1, "202402", bon="00002", day="2024-02-01"),
        make_row("APT-B", "71.0000", 1, "202604", bon="00002", cancel="O", day="2026-04-01"),
        make_row("APT-C", "84.98", 4, "202403", road="", bon="00000", day="2024-03-01"),
        make_row("APT-C", "84.9800", 2, "202605", road="", bon="00000", day="2026-05-01"),
    ]


class CoverageTest(unittest.TestCase):
    def test_boundary(self):
        cases = (
            (("59.0000", "60.0000"), 2),
            (("59.0000", "59.9999"), 1),
            (("59.0000", "60.0001"), 2),
        )
        for values, expected in cases:
            with self.subTest(values=values):
                groups = coverage.display_groups(Decimal(value) for value in values)
                self.assertEqual(len(groups), expected)

        rows = [make_row("APT-A", "59.5"), make_row("APT-A", "60.5")]
        result, _ = aggregate(rows)
        self.assertEqual(tuple(result[name] for name in NEW_COLUMNS), (2, 2, 2, 2, 2))

    def test_chaining(self):
        groups = coverage.display_groups(map(Decimal, ("59.0", "59.9", "60.8", "61.7")))
        self.assertEqual(groups, [[Decimal("59.0"), Decimal("59.9"), Decimal("60.8"), Decimal("61.7")]])
        self.assertEqual(groups[0][-1] - groups[0][0], Decimal("2.7"))

        groups = coverage.display_groups(map(Decimal, ("59.0", "60.5", "61.0")))
        self.assertEqual(groups, [[Decimal("59.0")], [Decimal("60.5"), Decimal("61.0")]])

    def test_order_invariance(self):
        values = [Decimal("84.98"), Decimal("59.0"), Decimal("84.9800"), Decimal("60.5")]
        expected = [[Decimal("59.0")], [Decimal("60.5")], [Decimal("84.98")]]
        variants = [values, list(reversed(values))]
        rng = random.Random(20260905)
        for _ in range(3):
            shuffled = list(values)
            rng.shuffle(shuffled)
            variants.append(shuffled)
        for variant in variants:
            self.assertEqual(coverage.display_groups(variant), expected)

        forward = coverage.display_groups((Decimal("84.98"), Decimal("84.9800")))
        reverse = coverage.display_groups((Decimal("84.9800"), Decimal("84.98")))
        self.assertEqual(matching.area_key(forward[0][0]), "84.98")
        self.assertEqual(matching.area_key(reverse[0][0]), "84.98")

    def test_aggregate_order_invariance(self):
        rows = regression_rows()
        expected_result, expected_diag = aggregate(rows)
        variants = [rows, list(reversed(rows))]
        rng = random.Random(20260905)
        for _ in range(3):
            shuffled = list(rows)
            rng.shuffle(shuffled)
            variants.append(shuffled)
        for variant in variants:
            result, diag = aggregate(variant)
            self.assertEqual(tuple(result[name] for name in NEW_COLUMNS), (4, 3, 2, 2, 2))
            self.assertEqual(result, expected_result)
            self.assertEqual(diag, expected_diag)

        tied_rows = [
            make_row("APT-TIE", "84.0", road="테스트로", bon="00001"),
            make_row("APT-TIE", "84.0", road="없는로", bon="00002"),
        ]
        forward_result, forward_diag = aggregate(tied_rows)
        reverse_result, reverse_diag = aggregate(list(reversed(tied_rows)))
        self.assertEqual(forward_result, reverse_result)
        self.assertEqual(forward_diag, reverse_diag)

    def test_valid_definition(self):
        rows = [
            make_row("APT-A", "59.0"),
            make_row("APT-A", "59.0", dealing="직거래"),
            make_row("APT-A", "59.0", cancel="O"),
            make_row("APT-A", "59.0", count=2, cancel="O"),
            make_row("APT-A", "59.0", count=3, cancel="O", ambiguous=True),
        ]
        result, diag = aggregate(rows)
        self.assertEqual(result["deal_count"], 8)
        self.assertEqual(tuple(result[name] for name in NEW_COLUMNS), (1, 1, 1, 0, 1))
        self.assertEqual(diag["ambiguous_groups"], 1)

    def test_existing_columns_unchanged(self):
        result, _ = aggregate(regression_rows())
        expected = {
            "deal_rows": 8,
            "deal_count": 16,
            "complexes": 3,
            "complexes_kapt_matched": 2,
            "complex_match_rate": "0.6667",
            "deals_kapt_matched": 10,
            "deal_weighted_match_rate": "0.6250",
            "combos_36m": 8,
            "combos_recent12m": 5,
            "coverage_rate": "0.6250",
            "direct_deal_count": 2,
            "direct_deal_rate": "0.1250",
            "cancel_count": 4,
            "cancel_rate": "0.2500",
        }
        self.assertEqual({name: result[name] for name in expected}, expected)
        self.assertEqual(tuple(coverage.COLUMNS[:16]), EXISTING_COLUMNS)
        self.assertEqual(tuple(coverage.COLUMNS[16:]), NEW_COLUMNS)

        result["lawd_cd"] = "11110"
        result["district_name"] = "테스트구"
        expected_row = (
            "11110", "테스트구", "8", "16", "3", "2", "0.6667", "10",
            "0.6250", "8", "5", "0.6250", "2", "0.1250", "4", "0.2500",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "coverage.csv"
            coverage.write_csv([result], path)
            with path.open(encoding="utf-8-sig", newline="") as f:
                written = list(csv.reader(f))
        self.assertEqual(tuple(written[0]), tuple(coverage.COLUMNS))
        self.assertEqual(tuple(written[1][:16]), expected_row)

    def test_zero_trade_count(self):
        result, _ = aggregate([make_row("APT-ZERO", "59.0", count=0)])
        self.assertEqual(result["deal_count"], 0)
        self.assertEqual(tuple(result[name] for name in NEW_COLUMNS), (1, 0, 0, 0, 0))

    def test_complex_isolation(self):
        rows = [make_row("APT-A", "59.0"), make_row("APT-B", "59.5")]
        result, _ = aggregate(rows)
        self.assertEqual(result["combos_36m_group"], 2)

    def test_window_reuse(self):
        rows = [
            make_row("APT-A", "59.0"),
            make_row("APT-A", "59.8", ym="202401", day="2024-01-01"),
            make_row("APT-A", "60.7", day="2026-01-02"),
        ]
        result, _ = aggregate(rows)
        self.assertEqual(result["combos_36m_group"], 1)
        self.assertEqual(result["combos_recent12m_group"], 1)


if __name__ == "__main__":
    unittest.main()
