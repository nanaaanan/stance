import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import call, patch

import refresh_stats

TRADE_RESULT = {
    "window_start_ym": "202511",
    "window_end_ym": "202610",
    "refreshed_at": "2026-11-25T00:00:00+00:00",
    "group_rows": 12,
    "complex_rows": 10,
    "valid_count": 45,
    "ungrouped_valid_count": 2,
    "trades_without_complex": 3,
}
RENT_RESULT = {
    "window_start_ym": "202511",
    "window_end_ym": "202610",
    "refreshed_at": "2026-11-25T00:00:05+00:00",
    "group_rows": 9,
    "jeonse_count": 30,
    "renewal_count": 7,
    "rent_count": 60,
    "grouped_rent_count": 50,
    "rents_without_complex": 6,
    "ungrouped_rent_count": 4,
}
TRADE_CALL = call("POST", "rpc/refresh_trade_stats", "url", "key", body={})
RENT_CALL = call("POST", "rpc/refresh_rent_stats", "url", "key", body={})


class RefreshStatsTest(unittest.TestCase):
    def test_success_calls_trade_then_rent_and_prints_each_result(self):
        with (
            patch.object(refresh_stats, "_sb_config", return_value=("url", "key")),
            patch.object(refresh_stats, "_sb_request",
                         side_effect=[TRADE_RESULT, RENT_RESULT]) as request,
            patch.object(refresh_stats.time, "monotonic", side_effect=[10.0, 16.84, 20.0, 23.21]),
            redirect_stdout(io.StringIO()) as output,
            redirect_stderr(io.StringIO()) as errors,
        ):
            self.assertEqual(refresh_stats.main(), 0)
        self.assertEqual(request.call_args_list, [TRADE_CALL, RENT_CALL])
        self.assertEqual(output.getvalue().splitlines(), [
            "refresh_trade_stats",
            *(f"{key} {value}" for key, value in TRADE_RESULT.items()),
            "elapsed_sec 6.8",
            "refresh_rent_stats",
            *(f"{key} {value}" for key, value in RENT_RESULT.items()),
            "elapsed_sec 3.2",
        ])
        self.assertEqual(errors.getvalue(), "")

    def test_trade_failure_skips_rent_and_prints_first_300_characters(self):
        for status in (0, 401, 403, 500):
            with (
                self.subTest(status=status),
                patch.object(refresh_stats, "_sb_config", return_value=("url", "key")),
                patch.object(refresh_stats, "_sb_request", side_effect=
                    refresh_stats.SupabaseError(status, "오" * 300 + "잘림")) as request,
                patch.object(refresh_stats.time, "monotonic", side_effect=[10.0, 18.25]),
                redirect_stdout(io.StringIO()) as output,
                redirect_stderr(io.StringIO()) as errors,
            ):
                self.assertEqual(refresh_stats.main(), 1)
                self.assertEqual(request.call_args_list, [TRADE_CALL])
                self.assertEqual(output.getvalue(), "")
                self.assertEqual(
                    errors.getvalue(),
                    f"refresh_trade_stats status={status} {'오' * 300}\nelapsed_sec 8.2\n",
                )

    def test_rent_failure_keeps_trade_output_and_exits_with_one(self):
        with (
            patch.object(refresh_stats, "_sb_config", return_value=("url", "key")),
            patch.object(refresh_stats, "_sb_request", side_effect=[
                TRADE_RESULT, refresh_stats.SupabaseError(500, "제한 시간 초과")]) as request,
            patch.object(refresh_stats.time, "monotonic", side_effect=[10.0, 16.84, 20.0, 45.0]),
            redirect_stdout(io.StringIO()) as output,
            redirect_stderr(io.StringIO()) as errors,
        ):
            self.assertEqual(refresh_stats.main(), 1)
        self.assertEqual(request.call_args_list, [TRADE_CALL, RENT_CALL])
        self.assertEqual(output.getvalue().splitlines()[0], "refresh_trade_stats")
        self.assertEqual(output.getvalue().splitlines()[-1], "elapsed_sec 6.8")
        self.assertEqual(
            errors.getvalue(), "refresh_rent_stats status=500 제한 시간 초과\nelapsed_sec 25.0\n"
        )

    def test_missing_configuration_keeps_existing_exit_and_does_not_request(self):
        with (
            patch.object(refresh_stats, "_sb_config", side_effect=SystemExit("접속 정보 없음")),
            patch.object(refresh_stats, "_sb_request") as request,
            self.assertRaisesRegex(SystemExit, "접속 정보 없음"),
        ):
            refresh_stats.main()
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
