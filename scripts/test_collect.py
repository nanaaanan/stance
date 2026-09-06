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


class TotalCountTest(unittest.TestCase):
    """totalCount 를 읽지 못한 것과 0 건을 구분해 기록하는지 본다."""

    def setUp(self):
        self.stdout = io.StringIO()
        self.enterContext(contextlib.redirect_stdout(self.stdout))
        # 실제 네트워크 진입 금지
        #   - 통과시키면 테스트가 조용히 외부 API 를 두드림
        self.enterContext(mock.patch.object(
            collect, "urlopen", side_effect=AssertionError("Unexpected network request")))
        self.enterContext(mock.patch.object(collect.time, "sleep"))
        self.count   = self.enterContext(mock.patch.object(collect, "_sb_count", return_value=0))
        self.max_log = self.enterContext(mock.patch.object(collect, "_max_log_id", return_value=0))
        self.upsert  = self.enterContext(mock.patch.object(collect, "upsert", return_value=0))
        self.mark    = self.enterContext(mock.patch.object(collect, "mark_stale", return_value=0))
        self.cfg = collect.CollectConfig(key="", dry_run=False)

    def root(self, total, item=""):
        """total 이 None 이면 totalCount 태그 자체가 없는 응답."""
        tag = "" if total is None else f"<totalCount>{total}</totalCount>"
        return ET.fromstring(f"<response><body>{tag}<items>{item}</items></body></response>")

    def item(self, kind, apt_seq="APT-TEST"):
        amount = "<dealAmount>110,000</dealAmount>" if kind == "trade" else "<deposit>50,000</deposit>"
        return (f"<item><aptSeq>{apt_seq}</aptSeq><aptNm>테스트아파트</aptNm>"
                "<dealYear>2026</dealYear><dealMonth>7</dealMonth><dealDay>1</dealDay>"
                "<excluUseAr>84.98000</excluUseAr><floor>10</floor>" + amount + "</item>")

    def collect_empty(self, total):
        """item 이 없는 1페이지 응답 하나로 collect_one 을 끝까지 돌린다."""
        with mock.patch.object(collect, "_fetch_page", return_value=self.root(total)) as fetch:
            result = collect.collect_one("trade", "11680", "202607", self.cfg)
        fetch.assert_called_once_with("trade", "11680", "202607", 1, "", False)
        self.mark.assert_not_called()
        self.assertEqual(result["page_count"], 0)
        return result

    def assert_unreadable(self, kind, total, item):
        """모순 응답이 ApiError 로 끝나고 그 앞에서 아무것도 건드리지 않았는지 본다."""
        with mock.patch.object(collect, "_fetch_page", return_value=self.root(total, item)) as fetch:
            with self.assertRaises(collect.ApiError) as raised:
                collect.collect_one(kind, "11680", "202607", self.cfg)
        self.assertEqual(raised.exception.code, "TOTALCOUNT_UNREADABLE")
        fetch.assert_called_once()
        # 예외 발생만 확인하면 "예외는 났지만 데이터는 이미 뒤집힌 뒤" 가 통과함
        #   - mark_stale 은 응답에서 사라진 행을 is_current=false 로 PATCH 함
        self.mark.assert_not_called()
        self.upsert.assert_not_called()
        self.count.assert_not_called()
        self.max_log.assert_not_called()

    # -------- totalCount 를 못 읽으면 None, 0 은 0 --------
    # 다섯 모두 is None 판정
    #   - == 0 이나 falsy 로 재면 None 과 0 이 같은 값이 되어 이 테스트의 목적이 사라짐

    def test_missing_tag_no_rows_becomes_none(self):
        self.assertIsNone(self.collect_empty(None)["total_count"])

    def test_empty_tag_no_rows_becomes_none(self):
        self.assertIsNone(self.collect_empty("")["total_count"])

    def test_whitespace_tag_no_rows_becomes_none(self):
        self.assertIsNone(self.collect_empty("   ")["total_count"])

    def test_non_numeric_tag_no_rows_becomes_none(self):
        self.assertIsNone(self.collect_empty("abc")["total_count"])

    def test_zero_stays_zero(self):
        self.assertEqual(self.collect_empty("0")["total_count"], 0)
        self.assertIsNotNone(self.collect_empty("0")["total_count"])

    def test_log_line_survives_none(self):
        self.collect_empty(None)
        self.assertIn("totalCount= NULL", self.stdout.getvalue())

    # -------- 모순 검사 --------

    def test_unreadable_with_rows_raises(self):
        for kind in ("trade", "rent"):
            for total in (None, "", "   ", "abc"):
                with self.subTest(kind=kind, total=total):
                    self.assert_unreadable(kind, total, self.item(kind))

    def test_unreadable_when_parser_drops_all_rows(self):
        """행 존재 판정을 all_rows 로 재는 구현을 걸러내는 음성 테스트."""
        for kind, parser in (("trade", collect.parse_trade), ("rent", collect.parse_rent)):
            with self.subTest(kind=kind):
                item = self.item(kind, apt_seq="")
                # 전제 확인: aptSeq 가 비어 자연키를 못 만들므로 파서가 이 행을 버림
                self.assertEqual(parser(self.root(None, item), "11680", "2026-07-01T00:00:00+00:00"), [])
                self.assert_unreadable(kind, None, item)

    # -------- main 경로: 기록까지 도달하는지 --------

    def run_main(self, roots, kind="trade"):
        """외부 IO 만 막고 main -> collect_one -> finish_run 경로를 실제로 돌린다."""
        argv = ["collect.py", "--kind", kind, "--months", "202607", "--districts", "11680", "--no-resume"]
        requests = []

        def request(method, path, *args, **kwargs):
            requests.append((method, path, kwargs["body"]))
            if method == "POST" and path == "collect_run":
                return [{"id": len(requests)}]
            self.assertEqual(method, "PATCH")
            self.assertTrue(path.startswith("collect_run?id=eq."))

        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(collect, "_get_key", return_value=""),
            mock.patch.object(collect, "_sb_config", return_value=("", "")),
            mock.patch.object(collect, "done_today", side_effect=AssertionError("Resume must be disabled")),
            mock.patch.object(collect, "_fetch_page", side_effect=roots) as fetch,
            mock.patch.object(collect, "_sb_request", side_effect=request),
        ):
            exit_code = 0
            try:
                for _ in range(len(roots) if kind == "trade" else 1):
                    collect.main()
            except SystemExit as error:
                exit_code = error.code
        return requests, fetch.call_count, exit_code

    def test_unreadable_without_items_is_ok(self):
        """진짜 빈 응답은 예외 없이 total_count=NULL 로 남는다. 이 로그의 부수 목적."""
        requests, calls, code = self.run_main([self.root(None)])
        self.assertEqual((len(requests), calls, code), (2, 1, 0))
        self.assertEqual(requests[1][2]["status"], "ok")
        # 키를 빼면 PATCH 가 그 칸을 안 건드려 이전 값이 남음. None 이 그대로 실려야 함
        self.assertIn("total_count", requests[1][2])
        self.assertIsNone(requests[1][2]["total_count"])

    def test_observations_reach_finish_without_extra_requests(self):
        totals = [148, 148, 150, None, None, 0, 154]
        requests, calls, code = self.run_main([self.root(total) for total in totals])
        self.assertEqual(code, 0)
        # 직전 값을 알아내려고 부르는 조회가 하나도 없음
        self.assertEqual(calls, len(totals))
        self.assertEqual(len(requests), 2 * len(totals))
        starts   = [body for method, _, body in requests if method == "POST"]
        finished = [body for method, _, body in requests if method == "PATCH"]
        self.assertEqual(len(starts), len(totals))
        self.assertEqual([body["total_count"] for body in finished], totals)
        self.assertTrue(all(body["status"] == "ok" for body in finished))
        self.assertTrue(all((body["kind"], body["lawd_cd"], body["deal_ym"]) == ("trade", "11680", "202607")
                            for body in finished))

    def test_unreadable_failure_records_error_and_continues(self):
        requests, calls, code = self.run_main(
            [self.root("abc", self.item("trade")), self.root(None)], kind="both")
        self.assertEqual((len(requests), calls, code), (4, 2, 1))
        failure, success = requests[1][2], requests[3][2]
        self.assertEqual((failure["status"], failure["error_code"], failure["kind"]),
                         ("error", "TOTALCOUNT_UNREADABLE", "trade"))
        self.assertEqual((failure["lawd_cd"], failure["deal_ym"]), ("11680", "202607"))
        self.assertEqual((success["status"], success["kind"]), ("ok", "rent"))
        self.assertIsNone(success["total_count"])
        self.upsert.assert_called_once()
        self.assertEqual(self.upsert.call_args.args[1], [])
        self.mark.assert_not_called()
        # 값이 새는 자리는 stdout 이 아니라 collect_run.error_msg
        #   - stdout 만 보면 예외 메시지에 total 원문이 애초에 안 실려 항상 통과
        self.assertNotIn("abc", failure["error_msg"])
        self.assertNotIn("<", failure["error_msg"])
        for token in ("trade", "11680", "202607"):
            self.assertIn(token, failure["error_msg"])
        self.assertNotIn("<response", self.stdout.getvalue())

    def test_page_fetches_unchanged(self):
        for kind in ("trade", "rent"):
            with self.subTest(kind=kind):
                root = self.root(collect.NUM_OF_ROWS + 1, self.item(kind))
                with mock.patch.object(collect, "_fetch_page", return_value=root) as fetch:
                    result = collect.collect_one(kind, "11680", "202607", self.cfg)
                self.assertEqual(result["total_count"], collect.NUM_OF_ROWS + 1)
                self.assertEqual(result["page_count"], 2)
                self.assertEqual([call.args[3] for call in fetch.call_args_list], [1, 2])


if __name__ == "__main__":
    unittest.main()
