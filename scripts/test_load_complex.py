"""단지 마스터 적재 계약과 실패 시 DB 쓰기 차단 확인"""

import copy
import csv
import hashlib
import io
import json
import pathlib
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from scripts import load_complex


class LoadComplexTest(unittest.TestCase):
    def setUp(self):
        registry = [{"reb_complex_id": "11110100000001", "households": 10, "approval_date": "2000-01-01"}]
        registry_patch = patch.object(
            load_complex, "load_reb_complex",
            return_value=(registry, {"households": 0, "approval_date": 0}),
        )
        registry_patch.start()
        self.addCleanup(registry_patch.stop)
        def failures():
            crosswalk, _ = load_complex.load_inputs()
            return [
                {"apt_seq": row["apt_seq"], "fail_layer": layer, "fail_reason": reason}
                for row in crosswalk
                for layer, reason, column in (
                    ("단지 명부", "후보 없음", "reb_complex_id"),
                    ("관리정보", "K-apt 마스터에 없음", "kapt_code"),
                )
                if row.get(column) is None
            ]
        failures_patch = patch.object(load_complex, "load_failures", side_effect=failures)
        failures_patch.start()
        self.addCleanup(failures_patch.stop)

    def _write_csv(self, path: pathlib.Path, header, rows) -> None:
        with path.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(header)
            writer.writerows(rows)

    def _crosswalk_row(self, **changes):
        row = {
            "sgg_cd": "11110",
            "apt_seq": "11110-1",
            "apt_nm": "테스트단지",
            "road_key": "테스트로 1",
            "kapt_code": "",
            "kapt_key": "",
            "match_source": "",
            "reb_complex_id": "", "reb_match_source": "",
        }
        row.update(changes)
        return [row[column] for column in load_complex.CROSSWALK_COLUMNS]

    def _area_row(self, **changes):
        row = {
            "sgg_cd": "11110",
            "apt_seq": "11110-1",
            "exclu_use_ar": "84.98",
            "area_status": "면적 매칭",
            "kapt_code": "A1",
            "kapt_area": "84.98",
            "households": "10",
            "group_id": "84.98",
            "group_label": "85",
            "group_areas": "84.98",
            "apt_nm": "테스트단지",
        }
        row.update(changes)
        return [row[column] for column in load_complex.AREA_COLUMNS]

    def test_reads_real_inputs_with_json_serializable_normalized_areas(self):
        crosswalk, areas = load_complex.load_inputs()

        self.assertEqual(len(crosswalk), 7_420)
        self.assertEqual(len(areas), 27_744)
        self.assertEqual(areas[0]["payload"]["exclu_use_ar"], "59.0400")
        self.assertIsNone(crosswalk[0]["kapt_code"])
        self.assertEqual(areas[0]["payload"]["kapt_area"], "59.0400")
        self.assertEqual(areas[0]["payload"]["households"], 12)
        json.dumps(areas[0], ensure_ascii=False)

    def test_separates_area_payload_from_validation_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "area.csv"
            self._write_csv(
                path,
                load_complex.AREA_COLUMNS,
                [self._area_row()],
            )
            row = load_complex.load_areas(path)[0]

        self.assertEqual(
            set(row["payload"]),
            {
                "apt_seq",
                "exclu_use_ar",
                "area_status",
                "kapt_area",
                "households",
                "group_id",
                "group_label",
                "group_areas",
            },
        )
        self.assertEqual(
            set(row["validation"]),
            {"sgg_cd", "apt_nm", "max_decimal_places"},
        )
        self.assertEqual(row["validation"]["max_decimal_places"], 2)

    def test_normalizes_four_decimal_places_without_float(self):
        self.assertEqual(load_complex._required_area("84.98", "area", 2), "84.9800")
        self.assertEqual(load_complex._required_area("84.1234", "area", 2), "84.1234")

    def test_rejects_area_beyond_four_decimal_places(self):
        with self.assertRaises(load_complex.InputError):
            load_complex._required_area("84.12345", "area", 2)

    def test_rejects_nonfinite_area(self):
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value), self.assertRaises(load_complex.InputError):
                load_complex._required_area(value, "area", 2)

    def test_empty_optional_values_become_none(self):
        self.assertIsNone(load_complex._optional_text("", "road_key", 2))
        self.assertIsNone(load_complex._optional_area("", "area", 2))
        self.assertIsNone(load_complex._optional_integer("", "households", 2))

    def test_whitespace_only_values_are_rejected(self):
        with self.assertRaises(load_complex.InputError):
            load_complex._required_text(" ", "apt_seq", 2)
        with self.assertRaises(load_complex.InputError):
            load_complex._optional_text(" ", "road_key", 2)

    def test_rejects_wrong_header(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "crosswalk.csv"
            self._write_csv(
                path,
                load_complex.CROSSWALK_COLUMNS[:-1],
                [["value"] * (len(load_complex.CROSSWALK_COLUMNS) - 1)],
            )
            with self.assertRaises(load_complex.InputError):
                load_complex.load_crosswalk(path)

    def test_rejects_rows_longer_or_shorter_than_header(self):
        for name, row in (
            ("long", self._crosswalk_row() + ["extra"]),
            ("short", self._crosswalk_row()[:-1]),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                path = pathlib.Path(directory) / "crosswalk.csv"
                self._write_csv(path, load_complex.CROSSWALK_COLUMNS, [row])
                with self.assertRaises(load_complex.InputError):
                    load_complex.load_crosswalk(path)

    def test_counts_duplicate_input_primary_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            crosswalk_path = root / "crosswalk.csv"
            area_path = root / "area.csv"
            self._write_csv(
                crosswalk_path,
                load_complex.CROSSWALK_COLUMNS,
                [self._crosswalk_row(), self._crosswalk_row()],
            )
            self._write_csv(
                area_path,
                load_complex.AREA_COLUMNS,
                [self._area_row(), self._area_row(exclu_use_ar="84.9800")],
            )
            with self.assertRaises(load_complex.InputError) as raised:
                load_complex.load_inputs(crosswalk_path, area_path)

        self.assertIn("crosswalk 1 / area 1", str(raised.exception))

    def test_real_input_preflight_counts(self):
        crosswalk, areas = load_complex.load_inputs()
        counts, max_places = load_complex.preflight_counts(crosswalk, areas)

        self.assertEqual(set(counts.values()), {0})
        self.assertEqual(max_places, 4)

    def test_preflight_counts_each_gate_without_db(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            crosswalk_path = root / "crosswalk.csv"
            area_path = root / "area.csv"
            self._write_csv(
                crosswalk_path,
                load_complex.CROSSWALK_COLUMNS,
                [
                    self._crosswalk_row(),
                    self._crosswalk_row(apt_seq="11110-2"),
                    self._crosswalk_row(apt_seq="11110-3"),
                ],
            )
            self._write_csv(
                area_path,
                load_complex.AREA_COLUMNS,
                [
                    self._area_row(group_id="a"),
                    self._area_row(exclu_use_ar="85", group_id="b"),
                    self._area_row(
                        apt_seq="11110-2", exclu_use_ar="40.1234",
                        sgg_cd="99999", apt_nm="다른단지",
                    ),
                    self._area_row(
                        apt_seq="11110-4", exclu_use_ar="0",
                        households="0", group_label="0",
                    ),
                ],
            )
            crosswalk, areas = load_complex.load_inputs(crosswalk_path, area_path)

        counts, max_places = load_complex.preflight_counts(crosswalk, areas)
        self.assertEqual(
            counts,
            {
                "label_collision": 2,
                "orphan_area": 1,
                "complex_without_area": 1,
                "shared_field_mismatch": 1,
                "nonpositive": 1,
            },
        )
        self.assertEqual(max_places, 4)

    def test_get_existing_apt_seqs_pages_by_apt_seq_without_float(self):
        first_page = [{"apt_seq": f"apt-{number:04d}"} for number in range(500)]
        responses = [
            io.BytesIO(json.dumps(first_page).encode()),
            io.BytesIO(b'[{"apt_seq":"apt-0500","unused_numeric":84.98}]'),
        ]
        original_loads = json.loads
        with (
            patch.object(load_complex, "count_rows", return_value=501),
            patch.object(load_complex, "urlopen", side_effect=responses) as open_mock,
            patch.object(load_complex.json, "loads", wraps=original_loads) as loads_mock,
        ):
            existing = load_complex.fetch_existing_apt_seqs(
                "https://example.invalid", "test-key"
            )

        self.assertEqual(len(existing), 501)
        self.assertEqual(open_mock.call_count, 2)
        paths = [call.args[0].full_url for call in open_mock.call_args_list]
        self.assertIn("order=apt_seq&limit=500&offset=0", paths[0])
        self.assertIn("order=apt_seq&limit=500&offset=500", paths[1])
        self.assertTrue(
            all(
                call.kwargs["parse_float"] is load_complex.Decimal
                for call in loads_mock.call_args_list
            )
        )

    def test_fetch_rows_keeps_numeric_as_decimal(self):
        response = io.BytesIO(b'[{"apt_seq":"a","exclu_use_ar":84.9800}]')
        with (
            patch.object(load_complex, "count_rows", return_value=1),
            patch.object(load_complex, "urlopen", return_value=response),
        ):
            rows = load_complex.fetch_rows(
                "https://example.invalid", "test-key", "complex_area",
                ("apt_seq", "exclu_use_ar"), "apt_seq,exclu_use_ar",
            )
        self.assertEqual(rows[0]["exclu_use_ar"], Decimal("84.9800"))

    def test_fetch_rows_rejects_shorter_read_than_exact_count(self):
        with (
            patch.object(load_complex, "count_rows", return_value=2),
            patch.object(
                load_complex, "urlopen",
                return_value=io.BytesIO(b'[{"apt_seq":"a"}]'),
            ),
        ):
            with self.assertRaises(load_complex.InputError) as raised:
                load_complex.fetch_rows(
                    "https://example.invalid", "key", "complex",
                    ("apt_seq",), "apt_seq",
                )
        self.assertIn("complex 조회가 잘렸습니다: DB 총수 2 / 읽은 수 1", str(raised.exception))

    def test_count_rows_uses_exact_head_total_not_page_length(self):
        response = MagicMock()
        response.__enter__.return_value.headers.get.return_value = "0-499/7362"
        with patch.object(load_complex, "urlopen", return_value=response) as open_mock:
            count = load_complex.count_rows("https://example.invalid", "test-key", "complex")

        self.assertEqual(count, 7362)
        request = open_mock.call_args.args[0]
        self.assertEqual(request.get_method(), "HEAD")
        self.assertEqual(request.get_header("Prefer"), "count=exact")
        self.assertIn("/complex?select=apt_seq", request.full_url)

        response.__enter__.return_value.headers.get.return_value = "*/0"
        with patch.object(load_complex, "urlopen", return_value=response):
            self.assertEqual(
                load_complex.count_rows("https://example.invalid", "test-key", "complex"),
                0,
            )

        response.__enter__.return_value.headers.get.return_value = ""
        with patch.object(load_complex, "urlopen", return_value=response):
            with self.assertRaises(load_complex.InputError):
                load_complex.count_rows("https://example.invalid", "test-key", "complex")

    def test_upsert_uses_pk_conflict_and_500_row_chunks(self):
        rows = [{"apt_seq": f"apt-{number}"} for number in range(501)]
        with patch.object(load_complex.collect, "_sb_request") as request_mock:
            load_complex.upsert_rows(
                "complex", rows, ("apt_seq",), "url", "key"
            )

        self.assertEqual(request_mock.call_count, 2)
        self.assertEqual(
            [len(call.kwargs["body"]) for call in request_mock.call_args_list],
            [500, 1],
        )
        for request_call in request_mock.call_args_list:
            self.assertEqual(request_call.args, ("POST", "complex?on_conflict=apt_seq", "url", "key"))
            self.assertEqual(
                request_call.kwargs["prefer"],
                "resolution=merge-duplicates,return=minimal",
            )

        with patch.object(load_complex.collect, "_sb_request") as area_request:
            load_complex.upsert_rows(
                "complex_area", [{"apt_seq": "a", "exclu_use_ar": "84.9800"}],
                ("apt_seq", "exclu_use_ar"), "url", "key",
            )
        self.assertEqual(
            area_request.call_args.args[1],
            "complex_area?on_conflict=apt_seq,exclu_use_ar",
        )

    def test_road_key_collisions_uses_distinct_apt_seq_and_ignores_null(self):
        rows = [
            {"apt_seq": "a", "road_key": "같은 길"},
            {"apt_seq": "a", "road_key": "같은 길"},
            {"apt_seq": "b", "road_key": "같은 길"},
            {"apt_seq": "c", "road_key": None},
        ]
        with patch.object(load_complex, "fetch_rows", return_value=rows) as fetch_mock:
            collisions = load_complex.road_key_collisions("url", "key")

        self.assertEqual(collisions, {"같은 길"})
        self.assertEqual(
            fetch_mock.call_args.args,
            ("url", "key", "complex", ("apt_seq", "road_key"), "apt_seq"),
        )

    def test_road_key_collisions_rejects_truncated_shared_read(self):
        with (
            patch.object(load_complex, "count_rows", return_value=2),
            patch.object(
                load_complex, "urlopen",
                return_value=io.BytesIO(
                    '[{"apt_seq":"a","road_key":"같은 길"}]'.encode()
                ),
            ),
        ):
            with self.assertRaises(load_complex.InputError) as raised:
                load_complex.road_key_collisions("https://example.invalid", "key")
        self.assertIn("complex 조회가 잘렸습니다: DB 총수 2 / 읽은 수 1", str(raised.exception))

    def test_last_seen_before_start_stops_and_after_start_passes(self):
        started_at = datetime(2026, 9, 24, 7, 26, 31, 686156, tzinfo=timezone.utc)
        with patch.object(
            load_complex.collect, "_sb_request",
            return_value=[{"last_seen_at": "2026-09-24T07:26:31.686155Z"}],
        ) as request_mock:
            with self.assertRaises(load_complex.InputError) as raised:
                load_complex.verify_last_seen_at("url", "key", "complex", started_at)
        self.assertIn("complex last_seen_at이 갱신되지 않았습니다", str(raised.exception))
        self.assertIn("2026-09-24T07:26:31.686155+00:00", str(raised.exception))
        self.assertIn("2026-09-24T07:26:31.686156+00:00", str(raised.exception))
        self.assertEqual(
            request_mock.call_args.args[:2],
            ("GET", "complex?select=last_seen_at&order=last_seen_at.asc&limit=1"),
        )

        with (
            patch.object(
                load_complex.collect, "_sb_request",
                return_value=[{"last_seen_at": "2026-09-24T07:26:31.686157Z"}],
            ),
            patch.object(load_complex, "fetch_rows") as fetch_mock,
        ):
            load_complex.verify_last_seen_at("url", "key", "complex_area", started_at)
        fetch_mock.assert_not_called()

        self.assertEqual(
            load_complex._digest_value(
                "first_seen_at", "2026-09-24T07:26:33.1429Z"
            ),
            "2026-09-24T07:26:33.142900",
        )

    def test_last_seen_surplus_still_rejects_stale_input_row(self):
        started_at = datetime(2026, 9, 24, 7, 26, 31, tzinfo=timezone.utc)
        with (
            patch.object(
                load_complex.collect, "_sb_request",
                return_value=[{"last_seen_at": "2000-01-01T00:00:00Z"}],
            ),
            patch.object(
                load_complex, "fetch_rows",
                return_value=[
                    {"apt_seq": "a", "last_seen_at": "2000-01-01T00:00:00Z"},
                    {"apt_seq": "old", "last_seen_at": "2000-01-01T00:00:00Z"},
                ],
            ),
        ):
            with self.assertRaises(load_complex.InputError) as raised:
                load_complex.verify_last_seen_at(
                    "url", "key", "complex", started_at, {"a"}
                )
        self.assertIn("complex last_seen_at이 갱신되지 않았습니다", str(raised.exception))

    def test_digest_uses_db_rows_and_canonical_serialization(self):
        row = {
            "apt_seq": "a", "exclu_use_ar": Decimal("84.98"),
            "area_status": "면적 매칭", "kapt_area": None, "households": 10,
            "group_id": None, "group_label": 85, "group_areas": None,
            "first_seen_at": "2026-09-24T09:01:02.1+09:00",
        }
        expected_fields = (
            "a", "84.9800", "면적 매칭", "", "10", "", "85", "",
            "2026-09-24T00:01:02.100000",
        )
        expected = hashlib.md5("\x1f".join(expected_fields).encode()).hexdigest()
        with patch.object(load_complex, "fetch_rows", return_value=[row]) as fetch_mock:
            actual = load_complex.digest_table("url", "key", "complex_area")

        self.assertEqual(actual, expected)
        self.assertNotIn("last_seen_at", load_complex.DIGEST_COLUMNS["complex_area"])
        self.assertEqual(
            fetch_mock.call_args.args[:5],
            (
                "url", "key", "complex_area",
                load_complex.DIGEST_COLUMNS["complex_area"],
                "apt_seq,exclu_use_ar",
            ),
        )
        self.assertEqual(fetch_mock.call_args.args[5], "apt_seq")
        with self.assertRaises(load_complex.InputError):
            load_complex._digest_value("exclu_use_ar", 84.98)

    def test_digest_separates_rows_and_normalizes_timestamp_to_utc(self):
        common = {
            "sgg_cd": "11110", "apt_nm": "단지", "road_key": None,
            "kapt_code": None, "kapt_key": None, "match_source": None, "reb_complex_id": None,
                "reb_fail_reason": None, "kapt_fail_reason": None,
        }
        rows = [
            {
                **common, "apt_seq": "a",
                "first_seen_at": "2026-09-24T09:00:00+09:00",
                "last_seen_at": "2026-09-25T00:00:00+00:00",
            },
            {
                **common, "apt_seq": "b",
                "first_seen_at": "2026-09-24T00:00:00Z",
                "last_seen_at": "2026-09-26T00:00:00+00:00",
            },
        ]
        first = "\x1f".join(("a", "11110", "단지", "", "", "", "", "", "", "", "2026-09-24T00:00:00.000000"))
        second = "\x1f".join(("b", "11110", "단지", "", "", "", "", "", "", "", "2026-09-24T00:00:00.000000"))
        expected = hashlib.md5(f"{first}\x1e{second}".encode()).hexdigest()
        with patch.object(load_complex, "fetch_rows", return_value=rows):
            actual = load_complex.digest_table("url", "key", "complex")

        self.assertEqual(actual, expected)

    def test_failed_preflight_stops_before_db_read(self):
        crosswalk = [
            {
                "sgg_cd": "11110", "apt_seq": "11110-1", "apt_nm": "단지",
                "road_key": None, "kapt_code": None, "kapt_key": None,
                "match_source": None, "reb_complex_id": None,
                "reb_fail_reason": None, "kapt_fail_reason": None,
            }
        ]
        with (
            patch.object(load_complex, "load_inputs", return_value=(crosswalk, [])),
            patch.object(load_complex.collect, "_sb_config") as config_mock,
            patch.object(load_complex, "fetch_existing_apt_seqs") as fetch_mock,
            patch.object(load_complex, "upsert_rows") as upsert_mock,
            redirect_stdout(io.StringIO()) as output,
        ):
            with self.assertRaises(SystemExit):
                load_complex.main()

        self.assertIn("단지0면적 1", output.getvalue())
        config_mock.assert_not_called()
        fetch_mock.assert_not_called()
        upsert_mock.assert_not_called()

    def test_main_reports_new_apt_seq_set_difference(self):
        crosswalk = [
            {
                "sgg_cd": "11110", "apt_seq": apt_seq, "apt_nm": "단지",
                "road_key": None, "kapt_code": None, "kapt_key": None,
                "match_source": None, "reb_complex_id": None,
                "reb_fail_reason": None, "kapt_fail_reason": None,
            }
            for apt_seq in ("11110-1", "11110-2")
        ]
        areas = [
            {
                "payload": {
                    "apt_seq": row["apt_seq"], "exclu_use_ar": "84.9800",
                    "area_status": "면적 매칭", "kapt_area": None,
                    "households": None, "group_id": None,
                    "group_label": None, "group_areas": None,
                },
                "validation": {
                    "sgg_cd": "11110", "apt_nm": "단지",
                    "max_decimal_places": 2,
                },
            }
            for row in crosswalk
        ]
        sent_tables = []

        def record_upsert(table, rows, key_fields, sb_url, sb_key):
            sent_tables.append((table, rows, key_fields, sb_url, sb_key))
            return len(rows)

        with (
            patch.object(load_complex, "load_inputs", return_value=(crosswalk, areas)),
            patch.object(load_complex.collect, "_sb_config", return_value=("url", "key")),
            patch.object(
                load_complex, "fetch_existing_apt_seqs", return_value={"11110-1"}
            ),
            patch.object(load_complex, "count_rows", side_effect=[2, 2, 1]),
            patch.object(
                load_complex, "road_key_collisions",
                side_effect=[set(), {"새 도로"}],
            ) as collision_mock,
            patch.object(load_complex, "upsert_rows", side_effect=record_upsert),
            patch.object(load_complex, "verify_last_seen_at") as last_seen_mock,
            patch.object(
                load_complex, "digest_table",
                side_effect=["a" * 32, "b" * 32, "c" * 32],
            ) as digest_mock,
            redirect_stdout(io.StringIO()) as output,
        ):
            load_complex.main()

        self.assertIn("신규 단지 수      1", output.getvalue())
        self.assertIn("적재 결과         complex 2 / complex_area 2", output.getvalue())
        self.assertIn("road_key 감사     C1 - C0 1\n  새 도로", output.getvalue())
        self.assertIn("멱등 다이제스트   complex 2행 ", output.getvalue())
        self.assertIn("멱등 다이제스트   complex_area 2행 ", output.getvalue())
        self.assertNotIn("DB 에만 있는 키", output.getvalue())
        self.assertEqual(collision_mock.call_count, 2)
        self.assertEqual(last_seen_mock.call_count, 3)
        self.assertEqual(digest_mock.call_count, 3)
        self.assertEqual([entry[0] for entry in sent_tables], ["reb_complex", "complex", "complex_area"])
        self.assertEqual(sent_tables[0][2], ("reb_complex_id",))
        self.assertEqual(sent_tables[2][2], ("apt_seq", "exclu_use_ar"))
        all_payloads = sent_tables[0][1] + sent_tables[1][1] + sent_tables[2][1]
        self.assertEqual(len({row["last_seen_at"] for row in all_payloads}), 1)
        self.assertTrue(all("first_seen_at" not in row for row in all_payloads))
        self.assertTrue(all("kapt_code" not in row for row in sent_tables[2][1]))
        self.assertTrue(all("sgg_cd" not in row for row in sent_tables[2][1]))
        self.assertTrue(all("apt_nm" not in row for row in sent_tables[2][1]))

    def test_db_count_mismatch_stops_before_digest(self):
        counts = {
            "label_collision": 0, "orphan_area": 0,
            "complex_without_area": 0, "shared_field_mismatch": 0,
            "nonpositive": 0,
        }
        with (
            patch.object(
                load_complex, "load_inputs",
                return_value=([{"apt_seq": "a", "reb_complex_id": None, "kapt_code": None}, {"apt_seq": "b", "reb_complex_id": None, "kapt_code": None}], []),
            ),
            patch.object(load_complex, "preflight_counts", return_value=(counts, 4)),
            patch.object(load_complex.collect, "_sb_config", return_value=("url", "key")),
            patch.object(load_complex, "fetch_existing_apt_seqs", return_value=set()),
            patch.object(load_complex, "count_rows", side_effect=[1, 0, 1]),
            patch.object(load_complex, "road_key_collisions", return_value=set()) as collision_mock,
            patch.object(load_complex, "upsert_rows") as upsert_mock,
            patch.object(load_complex, "digest_table") as digest_mock,
            redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(SystemExit) as raised:
                load_complex.main()

        self.assertIn("complex 적재된 행이 입력보다 적습니다", str(raised.exception))
        self.assertEqual(upsert_mock.call_count, 3)
        self.assertEqual(collision_mock.call_count, 1)
        digest_mock.assert_not_called()

    def test_db_rows_above_input_report_surplus_without_stopping(self):
        zero_counts = dict.fromkeys(
            (
                "label_collision", "orphan_area", "complex_without_area",
                "shared_field_mismatch", "nonpositive",
            ),
            0,
        )
        with (
            patch.object(
                load_complex, "load_inputs",
                return_value=(
                    [{"apt_seq": "a", "reb_complex_id": None, "kapt_code": None}],
                    [{"payload": {"apt_seq": "a", "exclu_use_ar": "84.9800"}}],
                ),
            ),
            patch.object(load_complex, "preflight_counts", return_value=(zero_counts, 4)),
            patch.object(load_complex.collect, "_sb_config", return_value=("url", "key")),
            patch.object(load_complex, "fetch_existing_apt_seqs", return_value={"a", "old"}),
            patch.object(load_complex, "road_key_collisions", side_effect=[set(), set()]),
            patch.object(load_complex, "upsert_rows"),
            patch.object(load_complex, "count_rows", side_effect=[2, 2, 2]),
            patch.object(
                load_complex.collect, "_sb_request",
                return_value=[{"last_seen_at": "2000-01-01T00:00:00Z"}],
            ),
            patch.object(
                load_complex, "fetch_rows",
                side_effect=lambda sb_url, sb_key, table, columns, order, *args: (
                    [
                        {"apt_seq": "a", "last_seen_at": "9999-01-01T00:00:00Z"},
                        {
                            "apt_seq": "old", "first_seen_at": "1999-01-01T00:00:00Z",
                            "last_seen_at": "2000-01-01T00:00:00Z",
                            "reb_complex_id": None,
                        },
                    ]
                    if table == "complex" else [{"reb_complex_id": "11110100000001", "last_seen_at": "9999-01-01T00:00:00Z"}]
                    if table == "reb_complex" else [
                        {
                            "apt_seq": "a", "exclu_use_ar": Decimal("84.9800"),
                            "last_seen_at": "9999-01-01T00:00:00Z",
                        },
                        {
                            "apt_seq": "old", "exclu_use_ar": Decimal("50.0000"),
                            "area_status": "K-apt 면적정보 없음",
                            "last_seen_at": "2000-01-01T00:00:00Z",
                        },
                    ]
                ),
            ),
            patch.object(load_complex, "digest_table", side_effect=["a" * 32, "b" * 32, "c" * 32]),
            redirect_stdout(io.StringIO()) as output,
        ):
            load_complex.main()

        self.assertIn("DB 에만 있는 행   complex 1", output.getvalue())
        self.assertIn("DB 에만 있는 행   complex_area 1", output.getvalue())
        self.assertIn("적재 결과         complex 2 / complex_area 2", output.getvalue())
        self.assertIn(
            "DB 에만 있는 키   complex old first_seen_at=1999-01-01T00:00:00Z "
            "last_seen_at=2000-01-01T00:00:00Z reb_complex_id=",
            output.getvalue(),
        )
        self.assertIn(
            "DB 에만 있는 키   complex_area old / 50.0000 K-apt 면적정보 없음 "
            "last_seen_at=2000-01-01T00:00:00Z",
            output.getvalue(),
        )
        self.assertNotIn("DB 에만 있는 키   reb_complex", output.getvalue())

    def test_db_only_keys_are_sorted_and_include_each_tables_details(self):
        started_at = datetime(2026, 11, 6, tzinfo=timezone.utc)
        old = "2000-01-01T00:00:00Z"
        cases = {
            "reb_complex": (
                [{"reb_complex_id": "11110100000002", "last_seen_at": old},
                 {"reb_complex_id": "11110100000001", "last_seen_at": old}],
                ["11110100000001", "11110100000002"],
            ),
            "complex": (
                [{"apt_seq": seq, "first_seen_at": old, "last_seen_at": old,
                  "reb_complex_id": "11110100000001" if seq == "b" else None}
                 for seq in ("b", "a")],
                [f"a first_seen_at={old} last_seen_at={old} reb_complex_id=",
                 f"b first_seen_at={old} last_seen_at={old} reb_complex_id=11110100000001"],
            ),
            "complex_area": (
                [{"apt_seq": "a", "exclu_use_ar": area,
                  "area_status": "K-apt 면적정보 없음", "last_seen_at": old}
                 for area in (Decimal("100"), Decimal("39.04"))],
                [f"a / 39.0400 K-apt 면적정보 없음 last_seen_at={old}",
                 f"a / 100.0000 K-apt 면적정보 없음 last_seen_at={old}"],
            ),
        }
        for table, (rows, expected) in cases.items():
            with (
                self.subTest(table=table),
                patch.object(load_complex, "fetch_rows", return_value=rows) as fetch,
                redirect_stdout(io.StringIO()) as output,
            ):
                load_complex.print_db_only_keys("url", "key", table, started_at)
            self.assertEqual(
                output.getvalue().splitlines(),
                [f"DB 에만 있는 키   {table} {detail}" for detail in expected],
            )
            self.assertEqual(fetch.call_args.args[-1],
                             "reb_complex_id" if table == "reb_complex" else "apt_seq")

    def test_db_only_keys_exclude_current_and_future_rows_for_every_table(self):
        started_at = datetime(2026, 11, 6, tzinfo=timezone.utc)
        for table in ("reb_complex", "complex", "complex_area"):
            with (
                self.subTest(table=table),
                patch.object(load_complex, "fetch_rows", return_value=[
                    {"last_seen_at": "2026-11-06T09:00:00+09:00"},
                    {"last_seen_at": "2026-11-07T00:00:00Z"},
                ]),
                redirect_stdout(io.StringIO()) as output,
            ):
                load_complex.print_db_only_keys("url", "key", table, started_at)
            self.assertEqual(output.getvalue(), "")

    def test_db_only_keys_have_no_output_limit(self):
        rows = [
            {"reb_complex_id": f"{number:014d}", "last_seen_at": "2000-01-01T00:00:00Z"}
            for number in range(501)
        ]
        with (
            patch.object(load_complex, "fetch_rows", return_value=rows),
            redirect_stdout(io.StringIO()) as output,
        ):
            load_complex.print_db_only_keys(
                "url", "key", "reb_complex", datetime(2026, 11, 6, tzinfo=timezone.utc)
            )
        self.assertEqual(len(output.getvalue().splitlines()), 501)

    def test_truncated_existing_read_stops_before_any_upsert(self):
        zero_counts = dict.fromkeys(
            (
                "label_collision", "orphan_area", "complex_without_area",
                "shared_field_mismatch", "nonpositive",
            ),
            0,
        )
        with (
            patch.object(load_complex, "load_inputs", return_value=([{"apt_seq": "a", "reb_complex_id": None, "kapt_code": None}], [])),
            patch.object(load_complex, "preflight_counts", return_value=(zero_counts, 4)),
            patch.object(load_complex.collect, "_sb_config", return_value=("https://example.invalid", "key")),
            patch.object(load_complex, "count_rows", return_value=2),
            patch.object(
                load_complex, "urlopen",
                return_value=io.BytesIO(b'[{"apt_seq":"a"}]'),
            ),
            patch.object(load_complex, "road_key_collisions") as collision_mock,
            patch.object(load_complex, "upsert_rows") as upsert_mock,
            redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(SystemExit) as raised:
                load_complex.main()

        self.assertIn("complex 조회가 잘렸습니다", str(raised.exception))
        collision_mock.assert_not_called()
        upsert_mock.assert_not_called()

    def test_truncated_digest_read_stops_before_digest_output(self):
        zero_counts = dict.fromkeys(
            (
                "label_collision", "orphan_area", "complex_without_area",
                "shared_field_mismatch", "nonpositive",
            ),
            0,
        )
        with (
            patch.object(
                load_complex, "load_inputs",
                return_value=([{"apt_seq": "a", "reb_complex_id": None, "kapt_code": None}, {"apt_seq": "b", "reb_complex_id": None, "kapt_code": None}], []),
            ),
            patch.object(load_complex, "preflight_counts", return_value=(zero_counts, 4)),
            patch.object(load_complex.collect, "_sb_config", return_value=("https://example.invalid", "key")),
            patch.object(load_complex, "count_rows", side_effect=[2, 0, 1, 2]),
            patch.object(load_complex, "fetch_existing_apt_seqs", return_value=set()),
            patch.object(load_complex, "road_key_collisions", return_value=set()),
            patch.object(load_complex, "upsert_rows") as upsert_mock,
            patch.object(load_complex, "verify_last_seen_at"),
            patch.object(
                load_complex, "urlopen",
                return_value=io.BytesIO(json.dumps([{
                    "apt_seq": "a", "sgg_cd": "11110", "apt_nm": "단지",
                    "road_key": None, "kapt_code": None, "kapt_key": None,
                    "match_source": None, "reb_complex_id": None,
                "reb_fail_reason": None, "kapt_fail_reason": None,
                    "first_seen_at": "2026-09-24T00:00:00Z",
                }]).encode()),
            ),
            redirect_stdout(io.StringIO()) as output,
        ):
            with self.assertRaises(SystemExit) as raised:
                load_complex.main()

        self.assertIn("complex 조회가 잘렸습니다", str(raised.exception))
        self.assertNotIn("멱등 다이제스트", output.getvalue())
        self.assertEqual(upsert_mock.call_count, 3)

    def test_two_mocked_runs_keep_row_counts_and_digests(self):
        crosswalk = [{
            "sgg_cd": "11110", "apt_seq": "a", "apt_nm": "단지",
            "road_key": None, "kapt_code": None, "kapt_key": None,
            "match_source": None, "reb_complex_id": None,
                "reb_fail_reason": None, "kapt_fail_reason": None,
        }]
        areas = [{
            "payload": {
                "apt_seq": "a", "exclu_use_ar": "84.9800",
                "area_status": "K-apt 면적정보 없음", "kapt_area": None,
                "households": None, "group_id": None,
                "group_label": None, "group_areas": None,
            },
            "validation": {
                "sgg_cd": "11110", "apt_nm": "단지",
                "max_decimal_places": 2,
            },
        }]
        db = {"reb_complex": {}, "complex": {}, "complex_area": {}}

        def fake_request(method, path, sb_url, sb_key, body=None, prefer=None):
            table = path.split("?", 1)[0]
            if method == "GET":
                return [{
                    "last_seen_at": min(
                        row["last_seen_at"] for row in db[table].values()
                    )
                }]
            self.assertEqual(method, "POST")
            for payload in body:
                key = (
                    (payload["apt_seq"], payload["exclu_use_ar"])
                    if table == "complex_area" else payload["reb_complex_id"] if table == "reb_complex" else payload["apt_seq"]
                )
                if key not in db[table]:
                    db[table][key] = {
                        **payload, "first_seen_at": "2026-09-24T00:00:00Z"
                    }
                else:
                    db[table][key].update(payload)

        def fake_fetch(sb_url, sb_key, table, columns, order, *args):
            return [
                {column: row[column] for column in columns}
                for _, row in sorted(db[table].items())
            ]

        outputs = []
        with (
            patch.object(load_complex, "load_inputs", return_value=(crosswalk, areas)),
            patch.object(load_complex.collect, "_sb_config", return_value=("url", "key")),
            patch.object(load_complex.collect, "_sb_request", side_effect=fake_request),
            patch.object(load_complex, "fetch_rows", side_effect=fake_fetch),
            patch.object(
                load_complex, "count_rows",
                side_effect=lambda sb_url, sb_key, table, *args: len(db[table]),
            ),
        ):
            for _ in range(2):
                with redirect_stdout(io.StringIO()) as output:
                    load_complex.main()
                outputs.append(output.getvalue())

        self.assertEqual((len(db["complex"]), len(db["complex_area"])), (1, 1))
        digests = [
            [line for line in output.splitlines() if line.startswith("멱등 다이제스트")]
            for output in outputs
        ]
        self.assertEqual(digests[0], digests[1])
        self.assertIn("신규 단지 수      1", outputs[0])
        self.assertIn("신규 단지 수      0", outputs[1])
        self.assertIsNone(db["complex"]["a"]["kapt_code"])
        self.assertIsNone(db["complex_area"][("a", "84.9800")]["kapt_area"])


class RegistryTest(unittest.TestCase):
    def write_csv(self, path, header, rows, encoding="utf-8-sig"):
        with path.open("w", encoding=encoding, newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(header)
            writer.writerows(rows)

    def registry_csv(self, rows):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = pathlib.Path(directory.name) / "registry.csv"
        self.write_csv(path, ("단지고유번호", "세대수", "사용승인일"), rows, "cp949")
        return path

    def test_registry_strips_id_and_preserves_valid_values(self):
        for approval in ("2000-02-29", "2000-02-29 ", " 2000-02-29 "):
            with self.subTest(approval=approval):
                rows, counts = load_complex.load_reb_complex(self.registry_csv([
                    ("11110100000001 ", "120", approval),
                ]))
                self.assertEqual(rows, [{"reb_complex_id": "11110100000001", "households": 120, "approval_date": "2000-02-29"}])
                self.assertEqual(counts, {"households": 0, "approval_date": 0})

    def test_invalid_households_and_dates_become_null_and_are_counted(self):
        for household, approval in [
            ("0", "2026-02-30"), ("-1", "20260101"),
            ("1.5", "2026-W01-1"), ("", ""), ("abc", "2026-1-01"),
            ("2147483648", "2026-01-01T00:00:00"),
        ]:
            with self.subTest(household=household, approval=approval):
                rows, counts = load_complex.load_reb_complex(self.registry_csv([
                    ("11110100000001", household, approval),
                ]))
                self.assertIsNone(rows[0]["households"])
                self.assertIsNone(rows[0]["approval_date"])
                self.assertEqual(counts, {"households": 1, "approval_date": 1})

    def test_duplicate_registry_id_after_strip_is_rejected(self):
        with self.assertRaisesRegex(load_complex.InputError, "중복"):
            load_complex.load_reb_complex(self.registry_csv([
                ("11110100000001 ", "1", "2000-01-01"),
                ("11110100000001", "1", "2000-01-01"),
            ]))

    def test_invalid_registry_id_is_rejected(self):
        for value in ("", "123", "1111010000000A", "１１１１０１０００００００１"):
            with self.subTest(value=value), self.assertRaises(load_complex.InputError):
                load_complex.load_reb_complex(self.registry_csv([(value, "1", "2000-01-01")]))

    def test_missing_registry_headers_and_wrong_encoding_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "registry.csv"
            self.write_csv(path, ("wrong",), [], "cp949")
            with self.assertRaises(load_complex.InputError):
                load_complex.load_reb_complex(path)
            self.write_csv(path, ("단지고유번호", "세대수", "사용승인일"), [("11110100000001", "1", "2000-01-01")])
            with self.assertRaises(load_complex.InputError):
                load_complex.load_reb_complex(path)

    def test_crosswalk_source_and_id_must_be_paired_and_known(self):
        for reb_id, source in [("11110100000001", ""), ("", "원문 일치"), ("11110100000001", "추정"), ("123", "원문 일치")]:
            with self.subTest(reb_id=reb_id, source=source), tempfile.TemporaryDirectory() as directory:
                path = pathlib.Path(directory) / "crosswalk.csv"
                self.write_csv(path, load_complex.CROSSWALK_COLUMNS, [("11110", "a", "단지", "", "", "", "", reb_id, source)])
                with self.assertRaises(load_complex.InputError):
                    load_complex.load_crosswalk(path)

    def test_failure_header_and_unknown_layer_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "failures.csv"
            self.write_csv(path, load_complex.FAILURE_COLUMNS[:-1], [])
            with self.assertRaises(load_complex.InputError):
                load_complex.load_failures(path)
            self.write_csv(path, load_complex.FAILURE_COLUMNS, [("11110", "a", "단지", "", "후보 없음", "다른 층")])
            with self.assertRaises(load_complex.InputError):
                load_complex.load_failures(path)

    def inputs(self):
        crosswalk = [
            {"apt_seq": "a", "reb_complex_id": "11110100000001", "kapt_code": "A1"},
            {"apt_seq": "b", "reb_complex_id": None, "kapt_code": None},
        ]
        registry = [{"reb_complex_id": "11110100000001"}]
        failures = [
            {"apt_seq": "b", "fail_layer": "단지 명부", "fail_reason": "후보 없음"},
            {"apt_seq": "b", "fail_layer": "관리정보", "fail_reason": "K-apt 마스터에 없음"},
        ]
        return crosswalk, registry, failures

    def test_preflight_assigns_reasons_and_clears_resolved_reasons(self):
        crosswalk, registry, failures = self.inputs()
        crosswalk[0].update(reb_fail_reason="후보 없음", kapt_fail_reason="K-apt 마스터에 없음")
        failures.append({"apt_seq": "other", "fail_layer": "면적", "fail_reason": "무시"})
        self.assertEqual(set(load_complex.registry_preflight(crosswalk, registry, failures).values()), {0})
        self.assertIsNone(crosswalk[0]["reb_fail_reason"])
        self.assertIsNone(crosswalk[0]["kapt_fail_reason"])
        self.assertEqual(crosswalk[1]["reb_fail_reason"], "후보 없음")
        self.assertEqual(crosswalk[1]["kapt_fail_reason"], "K-apt 마스터에 없음")

    def test_preflight_counts_each_new_gate(self):
        crosswalk, registry, failures = self.inputs()
        crosswalk[0]["reb_complex_id"] = "11110100000002"
        failures += [copy.copy(failures[0]), {"apt_seq": "a", "fail_layer": "관리정보", "fail_reason": "모름"}]
        failures = [row for row in failures if row["fail_layer"] != "단지 명부"]
        failures.append(copy.copy(failures[0]))
        counts = load_complex.registry_preflight(crosswalk, registry, failures)
        self.assertEqual(counts, {"명부번호없음": 1, "명부사유불일치": 1, "관리정보사유불일치": 1, "사유중복": 1, "모르는사유": 1})

    def test_each_registry_gate_stops_before_configuration_or_writes(self):
        for name in ("명부번호없음", "명부사유불일치", "관리정보사유불일치", "사유중복", "모르는사유"):
            with (
                self.subTest(name=name),
                patch.object(load_complex, "registry_preflight", return_value={name: 1}),
                patch.object(load_complex.collect, "_sb_config") as config,
                patch.object(load_complex, "upsert_rows") as upsert,
                redirect_stdout(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                load_complex.main()
            config.assert_not_called()
            upsert.assert_not_called()

    def test_real_inputs_dry_run_has_no_database_access(self):
        with patch.object(load_complex.collect, "_sb_config") as config, redirect_stdout(io.StringIO()) as output:
            load_complex.main(dry_run=True)
        config.assert_not_called()
        self.assertIn("명부 공유(N:1)   번호 3 / 단지 6", output.getvalue())

    def test_registry_count_shortfall_stops_before_digest(self):
        with (
            patch.object(load_complex.collect, "_sb_config", return_value=("url", "key")),
            patch.object(load_complex, "fetch_existing_apt_seqs", return_value=set()),
            patch.object(load_complex, "road_key_collisions", return_value=set()),
            patch.object(load_complex, "upsert_rows") as upsert,
            patch.object(load_complex, "count_rows", side_effect=[7420, 27744, 9527]),
            patch.object(load_complex, "digest_table") as digest,
            redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(SystemExit, "reb_complex 적재된 행이 입력보다 적습니다"),
        ):
            load_complex.main()
        self.assertEqual([call.args[0] for call in upsert.call_args_list], ["reb_complex", "complex", "complex_area"])
        digest.assert_not_called()

    def test_registry_database_error_stops_before_child_tables(self):
        with (
            patch.object(load_complex.collect, "_sb_config", return_value=("url", "key")),
            patch.object(load_complex, "fetch_existing_apt_seqs", return_value=set()),
            patch.object(load_complex, "road_key_collisions", return_value=set()),
            patch.object(load_complex, "upsert_rows", side_effect=load_complex.InputError("DB 오류")) as upsert,
            redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(SystemExit, "DB 오류"),
        ):
            load_complex.main()
        upsert.assert_called_once()
        self.assertEqual(upsert.call_args.args[0], "reb_complex")

    def test_registry_digest_includes_date_and_excludes_last_seen(self):
        row = {"reb_complex_id": "11110100000001", "households": 120, "approval_date": "2000-01-01", "first_seen_at": "2026-01-01T00:00:00Z"}
        with patch.object(load_complex, "fetch_rows", return_value=[row]) as fetch:
            actual = load_complex.digest_table("url", "key", "reb_complex")
        expected = hashlib.md5("\x1f".join(("11110100000001", "120", "2000-01-01", "2026-01-01T00:00:00.000000")).encode()).hexdigest()
        self.assertEqual(actual, expected)
        self.assertEqual(fetch.call_args.args[-1], "reb_complex_id")

    def test_registry_fetch_and_count_use_registry_primary_key(self):
        with patch.object(load_complex, "count_rows", return_value=1) as count, patch.object(load_complex, "urlopen", return_value=io.BytesIO(b'[{"reb_complex_id":"11110100000001"}]')):
            rows = load_complex.fetch_rows("https://example.invalid", "key", "reb_complex", ("reb_complex_id",), "reb_complex_id", "reb_complex_id")
        count.assert_called_once_with("https://example.invalid", "key", "reb_complex", "reb_complex_id")
        self.assertEqual(len(rows), 1)

    def test_registry_last_seen_surplus_checks_current_registry_ids(self):
        started = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for current_stamp, valid in [("2026-01-02T00:00:00Z", True), ("2000-01-01T00:00:00Z", False)]:
            with self.subTest(valid=valid), patch.object(load_complex.collect, "_sb_request", return_value=[{"last_seen_at": "2000-01-01T00:00:00Z"}]), patch.object(load_complex, "fetch_rows", return_value=[
                {"reb_complex_id": "11110100000001", "last_seen_at": current_stamp},
                {"reb_complex_id": "old", "last_seen_at": "2000-01-01T00:00:00Z"},
            ]):
                if valid:
                    load_complex.verify_last_seen_at("url", "key", "reb_complex", started, {"11110100000001"})
                else:
                    with self.assertRaises(load_complex.InputError):
                        load_complex.verify_last_seen_at("url", "key", "reb_complex", started, {"11110100000001"})


if __name__ == "__main__":
    unittest.main()
