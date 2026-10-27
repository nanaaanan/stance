import contextlib
import io
import pathlib
import random
import subprocess
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import patch

from scripts import area, crosswalk, load_complex


class AreaMatchingTest(unittest.TestCase):
    def test_display_groups_are_order_independent_and_keep_exact_gap_boundary(self):
        values = [59.9751, 59.9818, 78.7747, 84.5978, 84.9800, 109.8841]

        self.assertEqual(area.display_groups(values), area.display_groups(reversed(values)))
        self.assertEqual(len(area.display_groups([60, 60.999])), 1)
        self.assertEqual(len(area.display_groups([60, 61])), 2)
        self.assertEqual(len(area.display_groups([60, 61.001])), 2)

    def test_nearest_match_includes_half_and_rejects_outside_or_tie(self):
        self.assertEqual(
            area.nearest_kapt_area("60.5", ["60.0"]),
            (area.STATUS_MATCHED, Decimal("60")),
        )
        self.assertEqual(
            area.nearest_kapt_area("60.5001", ["60.0"]),
            (area.STATUS_OUTSIDE, None),
        )
        self.assertEqual(
            area.nearest_kapt_area("60.0", ["59.0", "61.0"]),
            (area.STATUS_OUTSIDE, None),
        )
        self.assertEqual(
            area.nearest_kapt_area("60.5", ["60.0", "61.0"]),
            (area.STATUS_TIE, None),
        )
        self.assertEqual(
            area.nearest_kapt_area("60.5", ["61.0", "60.0"]),
            (area.STATUS_TIE, None),
        )
        self.assertEqual(
            area.nearest_kapt_area("84.1", ["84.0", "84.2"]),
            (area.STATUS_TIE, None),
        )

    def test_label_uses_integer_households_then_area_tiebreak(self):
        larger_household_count = area.display_group_metadata(
            {Decimal("84.4"): "9", Decimal("85.3"): "10"}
        )
        tied_reversed = area.display_group_metadata(
            {Decimal("85.3"): "10", Decimal("84.4"): "10"}
        )

        self.assertEqual(larger_household_count[Decimal("84.4")]["group_label"], "85")
        self.assertEqual(tied_reversed[Decimal("85.3")]["group_label"], "84")

    def test_labels_use_round_half_up_at_adjacent_half_values(self):
        metadata = area.display_group_metadata({Decimal("84.5"): 1, Decimal("85.5"): 1})
        collision_guard = area.display_group_metadata(
            {Decimal("83.5"): 1, Decimal("84.5"): 1}
        )

        self.assertEqual(metadata[Decimal("84.5")]["group_label"], "85")
        self.assertEqual(metadata[Decimal("85.5")]["group_label"], "86")
        self.assertNotEqual(
            metadata[Decimal("84.5")]["group_label"],
            metadata[Decimal("85.5")]["group_label"],
        )
        self.assertEqual(collision_guard[Decimal("83.5")]["group_label"], "84")
        self.assertEqual(collision_guard[Decimal("84.5")]["group_label"], "85")

    def test_boundary_conflict_counter_is_symmetric(self):
        boundary = (Decimal("85"),)

        self.assertEqual(area.boundary_conflict_count("84.9", "85.1", boundary), 1)
        self.assertEqual(area.boundary_conflict_count("85.1", "84.9", boundary), 1)
        self.assertEqual(area.boundary_conflict_count("84.9", "84.95", boundary), 0)

    def test_build_rows_preserves_unmatched_nulls_and_failure_reasons(self):
        trade_rows = [
            {"apt_seq": "M", "exclu_use_ar": Decimal("60.5")},
            {"apt_seq": "O", "exclu_use_ar": Decimal("62")},
            {"apt_seq": "T", "exclu_use_ar": Decimal("60.5")},
            {"apt_seq": "N", "exclu_use_ar": Decimal("84.5")},
            {"apt_seq": "N", "exclu_use_ar": Decimal("85.5")},
        ]
        crosswalk = {
            apt: {
                "sgg_cd": "1",
                "apt_seq": apt,
                "apt_nm": apt,
                "road_key": "길 1",
                "kapt_code": code,
                "reb_complex_id": code,
            }
            for apt, code in (("M", "KM"), ("O", "KO"), ("T", "KT"), ("N", ""))
        }
        kapt_index = {
            "KM": {Decimal("60"): 10},
            "KO": {Decimal("60"): 20},
            "KT": {Decimal("60"): 30, Decimal("61"): 40},
        }

        rows, failures, _ = area.build_area_rows(trade_rows, crosswalk, kapt_index)
        by_apt = {row["apt_seq"]: row for row in rows if row["apt_seq"] != "N"}
        no_kapt = [row for row in rows if row["apt_seq"] == "N"]

        self.assertEqual(by_apt["M"]["area_status"], area.STATUS_MATCHED)
        self.assertEqual(by_apt["M"]["kapt_area"], "60")
        self.assertEqual(by_apt["M"]["households"], "10")
        for apt in ("O", "T"):
            self.assertEqual(by_apt[apt]["kapt_area"], "")
            self.assertEqual(by_apt[apt]["households"], "")
        self.assertEqual(by_apt["O"]["area_status"], area.STATUS_OUTSIDE)
        self.assertEqual(by_apt["T"]["area_status"], area.STATUS_TIE)
        self.assertEqual(failures, {"O": {area.STATUS_OUTSIDE}, "T": {area.STATUS_TIE}})
        self.assertEqual([row["group_label"] for row in no_kapt], ["", ""])
        self.assertTrue(all(row["area_status"] == area.STATUS_NO_REGISTRY for row in no_kapt))
        self.assertTrue(all(row["kapt_code"] == "" for row in rows))
        self.assertTrue(all(row["households"] == "" for row in no_kapt))

    def test_failure_append_is_idempotent_and_keeps_road_block(self):
        road = {
            "sgg_cd": "1",
            "apt_seq": "R",
            "apt_nm": "도로 실패",
            "road_key": "",
            "fail_reason": "K-apt 마스터에 없음",
            "fail_layer": "관리정보",
        }
        old_area = {
            "sgg_cd": "1",
            "apt_seq": "A",
            "apt_nm": "면적 실패",
            "road_key": "길 1",
            "fail_reason": area.STATUS_OUTSIDE,
            "fail_layer": "면적",
        }
        crosswalk = {
            "A": {
                "sgg_cd": "1",
                "apt_seq": "A",
                "apt_nm": "면적 실패",
                "road_key": "길 1",
                "kapt_code": "K",
                "reb_complex_id": "K",
            }
        }

        crosswalk["R"] = {**road, "kapt_code": "", "reb_complex_id": "R"}

        first = area.build_failure_rows(
            [road, old_area], crosswalk, {"A": {area.STATUS_OUTSIDE}}
        )
        second = area.build_failure_rows(
            first, crosswalk, {"A": {area.STATUS_OUTSIDE}}
        )

        self.assertEqual(first, second)
        self.assertIn(road, first)
        self.assertEqual(len(first), 2)

    def test_g4_requires_committed_tie_approval(self):
        tie = {"apt_seq": "T", "exclu_use_ar": "59.88", "area_status": area.STATUS_TIE}
        self.assertEqual(area.unapproved_tie_apts([tie], []), [("T", Decimal("59.88"))])
        self.assertEqual(area.unapproved_tie_apts([tie], [tie]), [])

    def test_g5b_recomputes_nearest_invariant_independently(self):
        trade_rows = [{"apt_seq": "A", "exclu_use_ar": Decimal("60.4")}]
        crosswalk = {
            "A": {
                "sgg_cd": "1",
                "apt_seq": "A",
                "apt_nm": "검증",
                "road_key": "길 1",
                "kapt_code": "K",
                "reb_complex_id": "K",
            }
        }
        kapt_index = {"K": {Decimal("60"): 10, Decimal("70"): 20}}
        original = area.nearest_kapt_area
        area.nearest_kapt_area = lambda exclu, candidates: (
            area.STATUS_MATCHED,
            max(candidates),
        )
        try:
            rows, _, diagnostics = area.build_area_rows(
                trade_rows, crosswalk, kapt_index
            )
            with self.assertRaisesRegex(ValueError, "G5b"):
                area._validate_output_rows(
                    rows,
                    diagnostics["combos_by_apt"],
                    crosswalk,
                    kapt_index,
                )
        finally:
            area.nearest_kapt_area = original

    def test_source_group_metadata_is_order_independent_for_every_registry_id(self):
        index = area.load_registry_index()
        rng = random.Random(0)
        for code, values in index.items():
            baseline = area.display_group_metadata(values)
            self.assertEqual(area.display_group_metadata(dict(reversed(list(values.items())))), baseline, code)
            for _ in range(3):
                shuffled = list(values.items())
                rng.shuffle(shuffled)
                self.assertEqual(area.display_group_metadata(dict(shuffled)), baseline, code)

    def test_source_half_up_regressions_and_full_registry_axis(self):
        for source_area, expected in (("44.5", "45"), ("148.5", "149"), ("58.5", "59")):
            self.assertEqual(area.display_group_metadata({source_area: 1})[Decimal(source_area)]["group_label"], expected)
        rows, _, _ = area.build_area_rows(
            [{"apt_seq": "A", "exclu_use_ar": "60"}],
            {"A": fixture_complex("A")},
            {"A": {Decimal("60"): 1, Decimal("60.9"): 2}})
        self.assertEqual(rows[0]["group_areas"], "60|60.9")
        self.assertEqual(rows[0]["group_label"], "61")


def fixture_complex(apt, *, registry=None, kapt="K"):
    return {"sgg_cd": "1", "apt_seq": apt, "apt_nm": apt, "road_key": "길 1",
            "kapt_code": kapt, "reb_complex_id": apt if registry is None else registry}


def fixture_failure(source, layer, reason):
    return {**{key: source[key] for key in ("sgg_cd", "apt_seq", "apt_nm", "road_key")},
            "fail_layer": layer, "fail_reason": reason}


class RegistryInputTest(unittest.TestCase):
    def test_registry_input_contract(self):
        cases = [
            (area.REGISTRY_COLUMNS, [["A", "84.9800", "2"]], True),
            (list(reversed(area.REGISTRY_COLUMNS)), [], False),
            (area.REGISTRY_COLUMNS, [["A", "84.98001", "2"]], False),
            (area.REGISTRY_COLUMNS, [["A", "NaN", "2"]], False),
            (area.REGISTRY_COLUMNS, [["A", "84.98", "2"], ["A", "84.9800", "3"]], False),
        ]
        cases += [(area.REGISTRY_COLUMNS, [["A", "84.98", count]], False)
                  for count in ("0", "-1", "1.5", "NaN", "Infinity", "")]
        for header, rows, valid in cases:
            with self.subTest(rows=rows, header=header), tempfile.TemporaryDirectory() as directory:
                path = pathlib.Path(directory) / "input.csv"
                area.write_csv(path, header, [dict(zip(header, row)) for row in rows])
                if valid:
                    self.assertEqual(area.load_registry_index(path), {"A": {Decimal("84.98"): 2}})
                else:
                    with self.assertRaises(ValueError):
                        area.load_registry_index(path)

    def test_four_decimal_validation_matches_loader(self):
        for value in (None, "", " ", "abc", "NaN", "Infinity", "-Infinity", "84.98001",
                      "84.98000", "84.98", "0", "-1.0000", "1e2", "1e100"):
            with self.subTest(value=value):
                accepted = []
                for function in (lambda: area.required_area(value, "fixture"),
                                 lambda: load_complex._required_area(value, "exclu_use_ar", 2)):
                    try:
                        function()
                        accepted.append(True)
                    except ValueError:
                        accepted.append(False)
                self.assertEqual(accepted[0], accepted[1])
        with self.assertRaisesRegex(ValueError, "trade.*84.98001"):
            area.build_area_rows([{"apt_seq": "A", "exclu_use_ar": "84.98001"}],
                                 {"A": fixture_complex("A")}, {})

    def test_missing_registry_has_no_labels_or_axis_values(self):
        cross = {"A": fixture_complex("A", registry=""), "B": fixture_complex("B", registry="absent")}
        rows, failures, diagnostics = area.build_area_rows(
            [{"apt_seq": apt, "exclu_use_ar": "84.5"} for apt in cross], cross, {})
        for row in rows:
            self.assertEqual(row["area_status"], area.STATUS_NO_REGISTRY)
            self.assertTrue(all(row[key] == "" for key in
                ("kapt_code", "kapt_area", "households", "group_id", "group_label", "group_areas")))
        area._validate_output_rows(rows, diagnostics["combos_by_apt"], cross, {})
        self.assertEqual(failures, {})
        existing = [fixture_failure(cross["A"], "단지 명부", "후보 없음")]
        failure_rows = area.build_failure_rows(existing, cross, failures)
        self.assertEqual(failure_rows, existing)
        area._validate_failure_rows(failure_rows, cross, rows)
        for apt in cross:
            for reason in (area.STATUS_NO_REGISTRY, area.STATUS_OUTSIDE, area.STATUS_TIE):
                with self.subTest(apt=apt, reason=reason), self.assertRaises(ValueError):
                    area._validate_failure_rows(
                        failure_rows + [fixture_failure(cross[apt], "면적", reason)], cross, rows)
        self.assertEqual(area.nearest_kapt_area("60", []), (area.STATUS_NO_REGISTRY, None))

    def test_page_ids_and_trade_query(self):
        def response(rows):
            import json
            result = unittest.mock.MagicMock()
            result.__enter__.return_value.read.return_value = json.dumps(rows).encode()
            return result
        for ids in ((1, 2, 3), (1, 1, 3), (2, 1, 3), (1, 2, 2)):
            rows = [{"id": id, "apt_seq": "A", "exclu_use_ar": "60"} for id in ids]
            with self.subTest(ids=ids), patch.object(area.coverage, "PAGE_SIZE", 2), patch.object(
                area, "urlopen", side_effect=[response(rows[:2]), response(rows[2:])]) as fetch:
                if ids == (1, 2, 3):
                    self.assertEqual(area.fetch_trade_rows("1", "https://fixture", "key"), rows)
                else:
                    with self.assertRaises(RuntimeError):
                        area.fetch_trade_rows("1", "https://fixture", "key")
                url = fetch.call_args_list[0].args[0].full_url
                self.assertIn("trade?select=id,apt_seq,exclu_use_ar", url)
                self.assertIn("is_current=is.true&order=id", url)
                self.assertNotIn("/rent?", url)

    def test_output_bytes_are_order_independent_and_chain_is_preserved(self):
        index = {"A": {Decimal("60"): 1, Decimal("60.9"): 2, Decimal("61.8"): 1}}
        cross = {"A": fixture_complex("A")}
        trades = [{"apt_seq": "A", "exclu_use_ar": value} for value in ("61.8", "60", "60.9", "60")]
        with tempfile.TemporaryDirectory() as directory:
            outputs = []
            variants = [trades, list(reversed(trades))]
            shuffled = list(trades)
            random.Random(0).shuffle(shuffled)
            variants.append(shuffled)
            for i, variant in enumerate(variants):
                rows, _, _ = area.build_area_rows(variant, cross, index)
                self.assertTrue(all(row["group_areas"] == "60|60.9|61.8" for row in rows))
                path = pathlib.Path(directory) / str(i)
                area.write_csv(path, area.OUTPUT_COLUMNS, rows)
                outputs.append(path.read_bytes())
            self.assertEqual(outputs, [outputs[0]] * len(outputs))


class LayerFailureTest(unittest.TestCase):
    def setUp(self):
        self.cross = {"A": fixture_complex("A", kapt=""),
                      "B": fixture_complex("B", registry="")}
        self.existing = [fixture_failure(self.cross["A"], "관리정보", "K-apt 마스터에 없음"),
                         fixture_failure(self.cross["B"], "단지 명부", "후보 없음")]

    def test_layer_contract_matches_crosswalk(self):
        self.assertEqual(area.FAILURE_COLUMNS, crosswalk.FAILURE_COLUMNS)
        self.assertEqual(area.FAILURE_LAYER_ORDER, crosswalk.FAILURE_LAYER_ORDER)

    def test_old_area_garbage_is_ignored_tie_wins_and_layers_can_coexist(self):
        reasons = {"A": {area.STATUS_OUTSIDE, area.STATUS_TIE}}
        expected = area.build_failure_rows(self.existing, self.cross, reasons)
        garbage = {"fail_layer": "면적", "fail_reason": "garbage"}
        self.assertEqual(area.build_failure_rows(self.existing + [garbage], self.cross, reasons), expected)
        self.assertEqual(area.build_failure_rows(expected, self.cross, reasons), expected)
        self.assertEqual([(row["apt_seq"], row["fail_layer"]) for row in expected],
                         [("A", "관리정보"), ("A", "면적"), ("B", "단지 명부")])
        self.assertEqual(expected[1]["fail_reason"], area.STATUS_TIE)
        area._validate_failure_rows(expected, self.cross,
            [{"apt_seq": apt, "area_status": status} for apt, statuses in reasons.items() for status in statuses])
        outside = area.build_failure_rows(self.existing, self.cross, {"A": {area.STATUS_OUTSIDE}})
        area._validate_failure_rows(outside, self.cross, [{"apt_seq": "A", "area_status": area.STATUS_OUTSIDE}])

    def test_missing_extra_duplicate_bad_layer_reason_and_metadata_are_rejected(self):
        variants = [self.existing[:1], self.existing[1:], self.existing + [self.existing[0]]]
        for field, value in (("fail_layer", "unknown"), ("fail_reason", "unknown"),
                             ("sgg_cd", "2"), ("apt_nm", "wrong"), ("road_key", "wrong")):
            variants.append([{**self.existing[0], field: value}, self.existing[1]])
        variants.append(self.existing + [fixture_failure(self.cross["A"], "단지 명부", "후보 없음")])
        for variant in variants:
            with self.subTest(rows=variant), self.assertRaises(ValueError):
                area.build_failure_rows(variant, self.cross, {})
        for layer, reason in (("관리정보", "후보 없음"), ("단지 명부", "K-apt 마스터에 없음"), ("면적", "unknown")):
            rows = area.build_failure_rows(self.existing, self.cross, {"A": {area.STATUS_OUTSIDE}})
            rows.append(fixture_failure(self.cross["A"], layer, reason))
            with self.assertRaises(ValueError):
                area._validate_failure_rows(rows, self.cross, [{"apt_seq": "A", "area_status": area.STATUS_OUTSIDE}])

    def test_no_registry_reason_is_rejected_for_area_layer(self):
        area_rows = [{"apt_seq": "A", "area_status": area.STATUS_OUTSIDE}]
        rows = area.build_failure_rows(self.existing, self.cross, {"A": {area.STATUS_OUTSIDE}})
        rows = [{**row, "fail_reason": area.STATUS_NO_REGISTRY}
                if row["fail_layer"] == "면적" else row for row in rows]
        with self.assertRaises(ValueError):
            area._validate_failure_rows(rows, self.cross, area_rows)
        with self.assertRaisesRegex(ValueError, "면적 층 계약 밖 사유"):
            area.build_failure_rows(self.existing, self.cross, {"B": {area.STATUS_NO_REGISTRY}})

    def test_six_column_failure_header_and_committed_area_header(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "failures.csv"
            for columns, valid in ((area.FAILURE_COLUMNS, True), (area.FAILURE_COLUMNS[:-1], False)):
                area.write_csv(path, columns, [])
                if valid:
                    self.assertEqual(area.load_failure_rows(path), [])
                else:
                    with self.assertRaises(ValueError):
                        area.load_failure_rows(path)
        for columns, valid in ((area.OUTPUT_COLUMNS, True), (area.FAILURE_COLUMNS, False)):
            body = (",".join(columns) + "\n").encode()
            with patch.object(area.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, body, b"")) as run:
                if valid:
                    self.assertEqual(area.load_committed_area_rows(), [])
                else:
                    with self.assertRaises(ValueError):
                        area.load_committed_area_rows()
                self.assertEqual(run.call_args.args[0], ["git", "show", "HEAD:data/complex-area.csv"])

    def test_new_tie_area_in_approved_complex_is_not_approved(self):
        approved = {"apt_seq": "A", "exclu_use_ar": "59.88", "area_status": area.STATUS_TIE}
        new = {**approved, "exclu_use_ar": "84.1"}
        self.assertEqual(area.unapproved_tie_apts([approved, new], [approved]), [("A", Decimal("84.1"))])

    def test_read_failure_keeps_output_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [pathlib.Path(directory) / name for name in ("area.csv", "fail.csv")]
            for path in paths:
                path.write_bytes(b"original")
            with (
                patch.object(area, "load_registry_index", return_value={}),
                patch.object(area, "load_crosswalk", return_value=self.cross),
                patch.object(area, "load_failure_rows", return_value=self.existing),
                patch.object(area, "load_committed_area_rows", return_value=[]),
                patch.object(area, "fetch_all_trade_rows", side_effect=RuntimeError("fixture DB failure")),
                self.assertRaisesRegex(SystemExit, "fixture DB failure"),
            ):
                area.main(out_area=paths[0], out_failures=paths[1])
            self.assertEqual([path.read_bytes() for path in paths], [b"original", b"original"])

    def test_main_writes_tie_then_stops_until_exact_area_is_approved(self):
        cross = {"A": fixture_complex("A", kapt="")}
        failures = [fixture_failure(cross["A"], "관리정보", "K-apt 마스터에 없음")]
        trades = [{"apt_seq": "A", "exclu_use_ar": "60.5"}]
        index = {"A": {Decimal("60"): 10, Decimal("61"): 20}}
        with tempfile.TemporaryDirectory() as directory:
            paths = [pathlib.Path(directory) / name for name in ("area.csv", "fail.csv")]
            with (
                patch.object(area, "load_registry_index", return_value=index),
                patch.object(area, "load_crosswalk", return_value=cross),
                patch.object(area, "load_failure_rows", return_value=failures),
                patch.object(area, "load_committed_area_rows", return_value=[]) as committed,
                patch.object(area, "fetch_all_trade_rows", return_value=trades),
                patch.object(area, "fetch_all_rent_rows", return_value=[]),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                with self.assertRaisesRegex(SystemExit, "G4"):
                    area.main(out_area=paths[0], out_failures=paths[1])
                written = area._read_csv(paths[0])
                self.assertEqual(written[0]["area_status"], area.STATUS_TIE)
                self.assertEqual(written[0]["group_label"], "")
                before = [path.read_bytes() for path in paths]
                committed.return_value = written
                area.main(out_area=paths[0], out_failures=paths[1])
                self.assertEqual([path.read_bytes() for path in paths], before)

    def test_g2a_requires_exact_trade_complex_set(self):
        area._validate_prewrite(self.cross, set(self.cross))
        for actual in (set(), {"A"}, {"A", "B", "C"}):
            with self.subTest(actual=actual), self.assertRaisesRegex(ValueError, "G2a"):
                area._validate_prewrite(self.cross, actual)


class RentAreaTest(unittest.TestCase):
    def setUp(self):
        self.cross = {"A": fixture_complex("A"), "B": fixture_complex("B")}
        self.index = {"A": {Decimal("60"): 10, Decimal("61"): 20},
                      "B": {Decimal("84.5"): 1, Decimal("85.4"): 2}}
        self.trades = [{"apt_seq": "A", "exclu_use_ar": "60"},
                       {"apt_seq": "B", "exclu_use_ar": "84.5"}]

    def build(self, rents):
        return area.build_combined_area_rows(self.trades, rents, self.cross, self.index)

    def test_union_deduplicates_and_preserves_every_trade_column(self):
        baseline, _, _ = area.build_area_rows(self.trades, self.cross, self.index)
        rents = [{"apt_seq": "A", "exclu_use_ar": "60.0000"},
                 {"apt_seq": "A", "exclu_use_ar": "61"},
                 {"apt_seq": "B", "exclu_use_ar": "85.4"}]
        rows, _, diagnostics = self.build(rents)
        by_key = {(row["apt_seq"], row["exclu_use_ar"]): row for row in rows}
        self.assertEqual(len(rows), 4)
        for row in baseline:
            self.assertEqual(by_key[(row["apt_seq"], row["exclu_use_ar"])], row)
        self.assertEqual(diagnostics["trade_combos"], 2)
        self.assertEqual(diagnostics["rent_combos"], 3)
        self.assertEqual(diagnostics["added_combos"], 2)
        self.assertEqual(diagnostics["added_status_counts"], {area.STATUS_MATCHED: 2})
        area._validate_output_rows(rows, diagnostics["combos_by_apt"], self.cross, self.index)

    def test_outside_crosswalk_counts_distinct_apts_without_rows(self):
        rents = [{"apt_seq": apt, "exclu_use_ar": value}
                 for apt, value in (("X", "60"), ("X", "61"), ("Y", "84.5"))]
        rows, failures, diagnostics = self.build(rents)
        self.assertEqual(rows, area.build_area_rows(self.trades, self.cross, self.index)[0])
        self.assertEqual(failures, {})
        self.assertEqual(diagnostics["rent_outside_apts"], 2)
        self.assertEqual(diagnostics["rent_db_rows"], 3)
        self.assertEqual(diagnostics["rent_combos"], 0)

    def test_rent_only_failures_are_one_per_complex_and_tie_wins(self):
        rents = [{"apt_seq": apt, "exclu_use_ar": value}
                 for apt, value in (("A", "62"), ("A", "60.5"), ("B", "90"), ("B", "91"))]
        rows, reasons, diagnostics = self.build(rents)
        failures = area.build_failure_rows([], self.cross, reasons)
        self.assertEqual([(row["apt_seq"], row["fail_layer"], row["fail_reason"])
                          for row in failures],
                         [("A", "면적", area.STATUS_TIE), ("B", "면적", area.STATUS_OUTSIDE)])
        self.assertEqual(diagnostics["added_status_counts"],
                         {area.STATUS_TIE: 1, area.STATUS_OUTSIDE: 3})
        area._validate_failure_rows(failures, self.cross, rows)
        tie = next(row for row in rows if row["area_status"] == area.STATUS_TIE)
        self.assertTrue(all(tie[key] == "" for key in
                            ("kapt_area", "households", "group_id", "group_label", "group_areas")))

    def test_rent_precision_is_checked_even_outside_crosswalk(self):
        for apt in ("A", "X"):
            with self.subTest(apt=apt), self.assertRaisesRegex(ValueError, "rent.*84.98001"):
                self.build([{"apt_seq": apt, "exclu_use_ar": "84.98001"}])

    def test_rent_url_and_strict_ids_across_pages(self):
        import json
        from urllib.parse import parse_qs, urlsplit
        for ids in ((1, 2, 3), (1, 1, 3), (2, 1, 3), (1, 2, 2), (1, 2, 1)):
            responses = []
            rows = [{"id": id, "apt_seq": "A", "exclu_use_ar": "60"} for id in ids]
            for chunk in (rows[:2], rows[2:]):
                response = unittest.mock.MagicMock()
                response.__enter__.return_value.read.return_value = json.dumps(chunk).encode()
                responses.append(response)
            with self.subTest(ids=ids), patch.object(area.coverage, "PAGE_SIZE", 2), patch.object(
                    area, "urlopen", side_effect=responses) as fetch:
                if ids == (1, 2, 3):
                    self.assertEqual(area.fetch_rent_rows("1", "https://fixture", "key"), rows)
                else:
                    with self.assertRaisesRegex(RuntimeError, "rent 1: id 엄격 증가 위반"):
                        area.fetch_rent_rows("1", "https://fixture", "key")
                for call in fetch.call_args_list:
                    url = urlsplit(call.args[0].full_url)
                    self.assertEqual(url.path, "/rest/v1/rent")
                    query = parse_qs(url.query)
                    self.assertEqual(query["select"], ["id,apt_seq,exclu_use_ar"])
                    self.assertEqual(query["is_current"], ["is.true"])
                    self.assertEqual(query["order"], ["id"])
                    self.assertEqual(query["sgg_cd"], ["eq.1"])
                    self.assertNotIn("monthly_rent", query)
                if ids == (1, 2, 3):
                    self.assertEqual(parse_qs(urlsplit(fetch.call_args_list[1].args[0].full_url).query)
                                     ["offset"], ["2"])

    def test_rent_fetch_precision_failure_and_query_failure_propagate(self):
        import json
        from urllib.error import URLError
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            [{"id": 1, "apt_seq": "A", "exclu_use_ar": "84.98001"}]).encode()
        with patch.object(area, "urlopen", return_value=response), self.assertRaisesRegex(
                ValueError, "rent 1 id=1.*84.98001"):
            area.fetch_rent_rows("1", "https://fixture", "key")
        with patch.object(area, "urlopen", side_effect=URLError("fixture DNS failure")), self.assertRaisesRegex(
                RuntimeError, "Supabase 조회 실패.*fixture DNS failure"):
            area.fetch_rent_rows("1", "https://fixture", "key")

    def test_main_g2a_uses_trade_only_and_rent_failure_keeps_files(self):
        rents = [{"apt_seq": "X", "exclu_use_ar": "60"},
                 {"apt_seq": "A", "exclu_use_ar": "61"}]
        with tempfile.TemporaryDirectory() as directory:
            paths = [pathlib.Path(directory) / name for name in ("area.csv", "fail.csv")]
            with (
                patch.object(area, "load_registry_index", return_value=self.index),
                patch.object(area, "load_crosswalk", return_value=self.cross),
                patch.object(area, "load_failure_rows", return_value=[]),
                patch.object(area, "load_committed_area_rows", return_value=[]),
                patch.object(area, "fetch_all_trade_rows", return_value=self.trades) as trades,
                patch.object(area, "fetch_all_rent_rows", return_value=rents) as rent,
                patch.object(area, "_validate_prewrite", wraps=area._validate_prewrite) as gate,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                area.main(out_area=paths[0], out_failures=paths[1])
                gate.assert_called_once_with(self.cross, {"A", "B"})
                before = [path.read_bytes() for path in paths]
                self.assertEqual({row["apt_seq"] for row in area._read_csv(paths[0])}, {"A", "B"})
                area.main(out_area=paths[0], out_failures=paths[1])
                self.assertEqual([path.read_bytes() for path in paths], before)
                rent.side_effect = RuntimeError("fixture rent failure")
                with self.assertRaisesRegex(SystemExit, "fixture rent failure"):
                    area.main(out_area=paths[0], out_failures=paths[1])
                self.assertEqual([path.read_bytes() for path in paths], before)
                rent.side_effect = None
                trades.return_value = self.trades[:1]
                with self.assertRaisesRegex(SystemExit, "G2a"):
                    area.main(out_area=paths[0], out_failures=paths[1])
                self.assertEqual([path.read_bytes() for path in paths], before)


if __name__ == "__main__":
    unittest.main()
