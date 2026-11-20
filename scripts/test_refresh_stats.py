import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import refresh_stats


class RefreshStatsTest(unittest.TestCase):
    def test_success_prints_all_eight_keys_and_posts_once(self):
        result = {
            "window_start_ym": "202512",
            "window_end_ym": "202611",
            "refreshed_at": "2026-11-17T00:00:00+00:00",
            "group_rows": 12,
            "complex_rows": 10,
            "valid_count": 45,
            "ungrouped_valid_count": 2,
            "trades_without_complex": 3,
        }
        with (
            patch.object(refresh_stats, "_sb_config", return_value=("url", "key")),
            patch.object(refresh_stats, "_sb_request", return_value=result) as request,
            redirect_stdout(io.StringIO()) as output,
            redirect_stderr(io.StringIO()) as errors,
        ):
            self.assertEqual(refresh_stats.main(), 0)
        request.assert_called_once_with(
            "POST", "rpc/refresh_trade_stats", "url", "key", body={}
        )
        self.assertEqual(output.getvalue().splitlines(), [
            f"{key} {value}" for key, value in result.items()
        ])
        self.assertEqual(errors.getvalue(), "")

    def test_failure_prints_status_and_first_300_characters_without_retry(self):
        for status in (0, 401, 403, 500):
            with (
                self.subTest(status=status),
                patch.object(refresh_stats, "_sb_config", return_value=("url", "key")),
                patch.object(refresh_stats, "_sb_request", side_effect=
                    refresh_stats.SupabaseError(status, "오" * 300 + "잘림")) as request,
                redirect_stdout(io.StringIO()) as output,
                redirect_stderr(io.StringIO()) as errors,
            ):
                self.assertEqual(refresh_stats.main(), 1)
                request.assert_called_once_with(
                    "POST", "rpc/refresh_trade_stats", "url", "key", body={}
                )
                self.assertEqual(output.getvalue(), "")
                self.assertEqual(errors.getvalue(), f"status={status} {'오' * 300}\n")

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
