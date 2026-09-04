import contextlib
import io
import os
import sys
import unittest
import xml.etree.ElementTree as ET
from decimal import localcontext
from unittest import mock

from scripts import collect


class AreaPrecisionTest(unittest.TestCase):
    def _root(self, kind):
        amount = "<dealAmount>110,000</dealAmount>" if kind == "trade" else "<deposit>50,000</deposit>"
        items = []
        for apt_seq, area in (("APT-NORMAL", "84.98000"), ("APT-LOSS", "84.98765")):
            items.append(
                f"""
                <item>
                  <aptSeq>{apt_seq}</aptSeq>
                  <aptNm>테스트아파트</aptNm>
                  <dealYear>2026</dealYear>
                  <dealMonth>7</dealMonth>
                  <dealDay>1</dealDay>
                  <excluUseAr>{area}</excluUseAr>
                  <floor>10</floor>
                  {amount}
                </item>
                """
            )
        return ET.fromstring(f"<response><body><items>{''.join(items)}</items></body></response>")

    def test_area_accepts_value_preserving_inputs(self):
        self.assertEqual(collect._area("84.9800"), "84.9800")
        self.assertEqual(collect._area("84.98000"), "84.9800")
        self.assertEqual(collect._area("8.498E+1"), "84.9800")
        self.assertEqual(collect._area("+084.9800"), "84.9800")

    def test_area_rejects_precision_loss(self):
        for value in ("84.98765", "1E+30"):
            with self.subTest(value=value), self.assertRaises(collect.ApiError) as raised:
                collect._area(value, "APT-LOSS")
            self.assertEqual(raised.exception.code, "AREA_PRECISION")
            self.assertIn(value, str(raised.exception))
            self.assertIn("APT-LOSS", str(raised.exception))

        with localcontext() as decimal_context:
            decimal_context.prec = 100
            with self.assertRaises(collect.ApiError) as raised:
                collect._area("1E+30", "APT-LOSS")
        self.assertEqual(raised.exception.code, "AREA_PRECISION")

    def test_area_rejects_nonfinite_values(self):
        for value in ("NaN", "sNaN", "Infinity", "-Infinity"):
            with self.subTest(value=value), self.assertRaises(collect.ApiError) as raised:
                collect._area(value, "APT-NONFINITE")
            self.assertEqual(raised.exception.code, "AREA_NONFINITE")
            self.assertNotIsInstance(raised.exception, ValueError)

    def test_area_keeps_conversion_failure_as_value_error(self):
        with self.assertRaises(ValueError):
            collect._area("not-a-number")

    def test_parse_trade_propagates(self):
        with self.assertRaises(collect.ApiError) as raised:
            collect.parse_trade(self._root("trade"), "11680", "2026-07-01T00:00:00+00:00")
        self.assertEqual(raised.exception.code, "AREA_PRECISION")
        self.assertIn("APT-LOSS", str(raised.exception))

    def test_parse_rent_propagates(self):
        with self.assertRaises(collect.ApiError) as raised:
            collect.parse_rent(self._root("rent"), "11680", "2026-07-01T00:00:00+00:00")
        self.assertEqual(raised.exception.code, "AREA_PRECISION")
        self.assertIn("APT-LOSS", str(raised.exception))

    def test_main_records_failure_and_exits_one(self):
        success = {
            "total_count": 1,
            "fetched_rows": 1,
            "page_count": 1,
            "inserted_count": 0,
            "updated_count": 0,
            "unchanged_count": 0,
            "parsed_rows": 1,
            "merged_rows": 1,
            "rows": [],
        }
        argv = [
            "collect.py", "--kind", "both", "--months", "202607",
            "--districts", "11680", "--dry-run",
        ]
        failure = collect.ApiError("AREA_PRECISION", "면적 원문='84.98765' apt_seq='APT-LOSS'")
        stdout = io.StringIO()

        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.dict(os.environ, {"DATA_GO_KR_KEY": "test-key"}),
            mock.patch.object(collect, "collect_one", side_effect=[failure, success]) as collect_one,
            mock.patch.object(collect, "finish_run") as finish_run,
            mock.patch.object(collect.time, "sleep"),
            contextlib.redirect_stdout(stdout),
            self.assertRaises(SystemExit) as raised,
        ):
            collect.main()

        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(collect_one.call_count, 2)

        failure_calls = [call for call in finish_run.call_args_list if call.args[1] == "error"]
        self.assertEqual(len(failure_calls), 1)
        failure_call = failure_calls[0]
        self.assertEqual(failure_call.args[1], "error")
        self.assertEqual(failure_call.kwargs["error_code"], "AREA_PRECISION")
        self.assertEqual(
            (failure_call.kwargs["kind"], failure_call.kwargs["lawd_cd"], failure_call.kwargs["deal_ym"]),
            ("trade", "11680", "202607"),
        )

        ok_calls = [call for call in finish_run.call_args_list if call.args[1] == "ok"]
        self.assertEqual(len(ok_calls), 1)
        self.assertEqual(
            (ok_calls[0].kwargs["kind"], ok_calls[0].kwargs["lawd_cd"], ok_calls[0].kwargs["deal_ym"]),
            ("rent", "11680", "202607"),
        )

        output = stdout.getvalue()
        for expected in ("84.98765", "APT-LOSS", "trade", "11680", "202607"):
            self.assertIn(expected, output)
        for leaked in ("test-key", "serviceKey", "<response", "https://apis"):
            self.assertNotIn(leaked, output)


if __name__ == "__main__":
    unittest.main()
