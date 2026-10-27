"""공시가격 면적 명부와 매매, 전월세 면적을 매칭해 표시 그룹 CSV 산출.

kapt_code는 빈 칸, kapt_area는 명부 면적, households는 unit_count.

실행
    set -a; source .env; set +a
    python3 scripts/area.py
"""

import collections
import csv
import importlib.util
import io
import json
import pathlib
import random
import subprocess
import sys
from decimal import (
    Decimal, DefaultContext, InvalidOperation, ROUND_HALF_EVEN, ROUND_HALF_UP, localcontext,
)
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
PRICE_AREA_PATH = ROOT / "data" / "price-area-seoul-apt.csv"
CROSSWALK_PATH = ROOT / "data" / "complex-crosswalk.csv"
COMPLEX_AREA_PATH = ROOT / "data" / "complex-area.csv"
MATCHING_FAILURES_PATH = ROOT / "data" / "matching-failures.csv"

REGISTRY_COLUMNS = ["reb_complex_id", "exclu_use_ar", "unit_count"]
AREA_SCALE = Decimal("0.0001")

OUTPUT_COLUMNS = [
    "sgg_cd",
    "apt_seq",
    "exclu_use_ar",
    "area_status",
    "kapt_code",
    "kapt_area",
    "households",
    "group_id",
    "group_label",
    "group_areas",
    "apt_nm",
]
FAILURE_COLUMNS = ["sgg_cd", "apt_seq", "apt_nm", "road_key", "fail_reason", "fail_layer"]
FAILURE_LAYER_ORDER = {"단지 명부": 0, "관리정보": 1, "면적": 2}

STATUS_MATCHED = "면적 매칭"
STATUS_OUTSIDE = "면적 ±0.5㎡ 밖"
STATUS_TIE = "면적 동점(모호)"
STATUS_NO_REGISTRY = "면적 명부 없음"
AREA_STATUSES = {STATUS_MATCHED, STATUS_OUTSIDE, STATUS_TIE, STATUS_NO_REGISTRY}
AREA_FAILURE_REASONS = {STATUS_OUTSIDE, STATUS_TIE}
ROAD_FAILURE_REASONS = {
    "K-apt 마스터에 없음",
    "후보 2개 이상(모호)",
    "도로명 키 생성 불가",
}

FAILURE_REASONS = {
    "관리정보": ROAD_FAILURE_REASONS,
    "단지 명부": {"후보 없음", "후보 2개 이상(모호)", "도로명 키 생성 불가"},
    "면적": AREA_FAILURE_REASONS,
}

MATCH_LIMIT = Decimal("0.5")
BOUNDARIES = (Decimal("60"), Decimal("85"), Decimal("102"))


def _load_local_coverage():
    """레포의 coverage.py를 동명 외부 패키지와 구분해 로드."""
    try:
        from scripts import matching as local_matching
    except ImportError:
        import matching as local_matching

    path = HERE / "coverage.py"
    name = "_stance_area_coverage"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"{path.name} 을 로드하지 못했습니다.")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get("matching")
    sys.modules["matching"] = local_matching
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop("matching", None)
        else:
            sys.modules["matching"] = previous
    return module


coverage = _load_local_coverage()
display_groups = coverage.display_groups


def normalize_area(value) -> Decimal:
    """coverage.display_groups 와 같은 규칙으로 면적 표기를 정규화."""
    if value is None or value == "":
        raise ValueError("면적 값이 비어 있습니다.")
    try:
        return display_groups([value])[0][0]
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"면적 값이 숫자가 아닙니다: {value!r}") from exc


def area_text(value) -> str:
    """지수 표기 없이 정규화한 면적 문자열."""
    return format(normalize_area(value), "f")


def half_up_label(value) -> str:
    """표시용 면적을 ROUND_HALF_UP 정수 문자열로 반환."""
    return format(
        normalize_area(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP),
        "f",
    )


def required_area(value, source) -> Decimal:
    """소수 4자리로 값이 바뀌는 입력 거부."""
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            raise InvalidOperation
        with localcontext(DefaultContext):
            normalized = number.quantize(AREA_SCALE, rounding=ROUND_HALF_UP)
        if number != normalized:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        raise ValueError(f"{source}: 유한한 소수 4자리 면적이 아닙니다: {value!r}") from None
    return normalize_area(normalized)


def load_registry_index(path=PRICE_AREA_PATH) -> dict[str, dict[Decimal, int]]:
    """명부를 단지 번호 -> 면적 -> 호 수로 색인."""
    index = collections.defaultdict(dict)
    with open(path, encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames != REGISTRY_COLUMNS:
            raise ValueError(f"면적 명부 헤더가 계약과 다릅니다: {reader.fieldnames!r}")
        for line, row in enumerate(reader, start=2):
            code = row["reb_complex_id"]
            if not code or not code.strip():
                raise ValueError(f"면적 명부 {line}행 reb_complex_id 가 비어 있습니다.")
            area = required_area(row["exclu_use_ar"], f"면적 명부 {line}행")
            count = _integer_households(row["unit_count"], row_number=line)
            if count <= 0:
                raise ValueError(f"면적 명부 {line}행 unit_count 는 양의 정수여야 합니다.")
            if area in index[code]:
                raise ValueError(f"면적 명부 번호/면적 중복: {code}/{area}")
            index[code][area] = count
    if not index:
        raise ValueError("면적 명부에 데이터 행이 없습니다.")
    return dict(index)


def _integer_households(value, *, row_number=None) -> int:
    if value is None or value == "":
        where = f" {row_number}행" if row_number is not None else ""
        raise ValueError(f"'unit_count' 값이 비어 있습니다:{where}")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"'unit_count' 값이 정수가 아닙니다: {value!r}") from exc
    if not number.is_finite() or number != number.to_integral_value():
        raise ValueError(f"'unit_count' 값이 정수가 아닙니다: {value!r}")
    return int(number)


def load_crosswalk(path=CROSSWALK_PATH) -> dict[str, dict]:
    with open(path, encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        required = {"sgg_cd", "apt_seq", "apt_nm", "road_key", "kapt_code", "reb_complex_id"}
        missing = sorted(required - set(reader.fieldnames or ()))
        if missing:
            raise ValueError(f"crosswalk 필수 열이 없습니다: {', '.join(missing)}")
        rows = {}
        for row in reader:
            apt_seq = row["apt_seq"]
            if apt_seq in rows:
                raise ValueError(f"crosswalk apt_seq 가 중복입니다: {apt_seq}")
            rows[apt_seq] = row
    if not rows:
        raise ValueError("crosswalk 에 데이터 행이 없습니다.")
    return rows


def load_failure_rows(path=MATCHING_FAILURES_PATH) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames != FAILURE_COLUMNS:
            raise ValueError(
                "matching-failures.csv 열이 계약과 다릅니다: "
                f"{reader.fieldnames!r}"
            )
        return list(reader)


def load_committed_area_rows() -> list[dict]:
    """G4 승인 기준인 HEAD 면적표 읽기."""
    result = subprocess.run(
        ["git", "show", "HEAD:data/complex-area.csv"], cwd=ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode != 0:
        message = result.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"G4 커밋 승인 기준을 읽지 못했습니다: {message}")
    reader = csv.DictReader(io.StringIO(result.stdout.decode("utf-8-sig"), newline=""))
    if reader.fieldnames != OUTPUT_COLUMNS:
        raise ValueError(f"커밋된 complex-area.csv 열이 계약과 다릅니다: {reader.fieldnames!r}")
    return list(reader)


def unapproved_tie_apts(current_rows, committed_rows) -> list[tuple[str, Decimal]]:
    """현재 동점 중 HEAD에 없는 (단지, 면적) 반환."""
    def ties(rows):
        return {
            (row["apt_seq"], required_area(row["exclu_use_ar"], "G4 면적표"))
            for row in rows if row["area_status"] == STATUS_TIE
        }
    return sorted(ties(current_rows) - ties(committed_rows))


def fetch_trade_rows(sgg_cd: str, url: str, key: str) -> list[dict]:
    return _fetch_area_rows("trade", sgg_cd, url, key)


def fetch_rent_rows(sgg_cd: str, url: str, key: str) -> list[dict]:
    return _fetch_area_rows("rent", sgg_cd, url, key)


def _fetch_area_rows(table: str, sgg_cd: str, url: str, key: str) -> list[dict]:
    """한 구의 전 기간 current 거래에서 면적 조합에 필요한 열만 전부 조회."""
    output = []
    offset = 0
    previous_id = None
    path = (
        f"{table}?select=id,apt_seq,exclu_use_ar"
        f"&sgg_cd=eq.{sgg_cd}&is_current=is.true&order=id"
    )
    while True:
        request = Request(
            f"{url}/rest/v1/{path}&offset={offset}&limit={coverage.PAGE_SIZE}",
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
        )
        try:
            with urlopen(request, timeout=coverage.TIMEOUT_SEC) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            raise RuntimeError(f"Supabase HTTP {exc.code}") from None
        except (URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"Supabase 조회 실패: {exc}") from None
        chunk = json.loads(body, parse_float=Decimal)
        for row in chunk:
            if previous_id is not None and row["id"] <= previous_id:
                raise RuntimeError(f"{table} {sgg_cd}: id 엄격 증가 위반: {previous_id} -> {row['id']}")
            previous_id = row["id"]
            required_area(row.get("exclu_use_ar"), f"{table} {sgg_cd} id={row['id']}")
        output.extend(chunk)
        if len(chunk) < coverage.PAGE_SIZE:
            return output
        offset += coverage.PAGE_SIZE


def fetch_all_trade_rows() -> list[dict]:
    return _fetch_all_area_rows("trade", fetch_trade_rows)


def fetch_all_rent_rows() -> list[dict]:
    return _fetch_all_area_rows("rent", fetch_rent_rows)


def _fetch_all_area_rows(table, fetch_rows) -> list[dict]:
    url, key = coverage.sb_config()
    output = []
    for sgg_cd, district_name in coverage.load_districts():
        rows = fetch_rows(sgg_cd, url, key)
        output.extend(rows)
        print(f"조회 {table} {sgg_cd} {district_name}: {len(rows):,}행 (누적 {len(output):,})")
        sys.stdout.flush()
    return output


def nearest_kapt_area(exclu_use_ar, candidates) -> tuple[str, Decimal | None]:
    """축은 면적 명부. 단지 안에서 ±0.5㎡ 최근접 하나만 할당하고 동점은 고르지 않음."""
    exclu = normalize_area(exclu_use_ar)
    areas = sorted({normalize_area(candidate) for candidate in candidates})
    if not areas:
        return STATUS_NO_REGISTRY, None
    distances = [(abs(exclu - area), area) for area in areas]
    minimum = min(distance for distance, _ in distances)
    if minimum > MATCH_LIMIT:
        return STATUS_OUTSIDE, None
    winners = [area for distance, area in distances if distance == minimum]
    if len(winners) > 1:
        return STATUS_TIE, None
    return STATUS_MATCHED, winners[0]


def display_group_metadata(area_households) -> dict[Decimal, dict[str, str]]:
    """축은 면적 명부. 각 축 면적을 순서 무관한 표시 그룹 메타데이터에 대응."""
    normalized = {
        normalize_area(area): _integer_households(households)
        for area, households in area_households.items()
    }
    metadata = {}
    for group in display_groups(normalized):
        # 세대수는 정수 내림차순, 동점이면 면적 오름차순으로 라벨 원본을 고른다.
        label_source = min(group, key=lambda area: (-normalized[area], area))
        group_record = {
            "group_id": area_text(group[0]),
            "group_label": half_up_label(label_source),
            "group_areas": "|".join(area_text(area) for area in group),
        }
        for area in group:
            metadata[area] = group_record
    return metadata


def boundary_conflict_count(left, right, boundaries=BOUNDARIES) -> int:
    """두 원천 면적이 법정 경계의 서로 다른 쪽을 가리키는 개수."""
    left_area = normalize_area(left)
    right_area = normalize_area(right)
    return sum((left_area > boundary) != (right_area > boundary) for boundary in boundaries)


def _trade_combinations(trade_rows: list[dict]):
    raw = set()
    normalized = collections.defaultdict(set)
    for row in trade_rows:
        apt_seq = row.get("apt_seq")
        if not apt_seq:
            raise ValueError("trade apt_seq 가 비어 있습니다.")
        value = row.get("exclu_use_ar")
        raw.add((apt_seq, str(value)))
        normalized[apt_seq].add(required_area(value, f"trade apt_seq={apt_seq}"))
    return raw, dict(normalized)


def build_area_rows(
    trade_rows: list[dict],
    crosswalk: dict[str, dict],
    registry_index: dict[str, dict[Decimal, int]],
):
    """CSV 행과 진단값을 메모리에서 결정적으로 계산."""
    raw_combos, combos_by_apt = _trade_combinations(trade_rows)
    referenced_codes = sorted(
        {
            row["reb_complex_id"]
            for row in crosswalk.values()
            if row["reb_complex_id"] and row["reb_complex_id"] in registry_index
        }
    )
    group_cache = {
        code: display_group_metadata(registry_index[code]) for code in referenced_codes
    }

    output = []
    failure_reasons_by_apt = collections.defaultdict(set)
    failed_combos_by_apt = collections.Counter()
    status_counts = collections.Counter()

    for apt_seq, trade_areas in combos_by_apt.items():
        source = crosswalk[apt_seq]
        code = source["reb_complex_id"]
        has_registry_axis = bool(code and code in registry_index)

        for exclu in trade_areas:
            kapt_area = None
            if has_registry_axis:
                status, kapt_area = nearest_kapt_area(exclu, registry_index[code])
            else:
                status = STATUS_NO_REGISTRY

            row = {
                "sgg_cd": source["sgg_cd"],
                "apt_seq": apt_seq,
                "exclu_use_ar": area_text(exclu),
                "area_status": status,
                "kapt_code": "",
                "kapt_area": "",
                "households": "",
                "group_id": "",
                "group_label": "",
                "group_areas": "",
                "apt_nm": source["apt_nm"],
            }
            if status == STATUS_MATCHED:
                row["kapt_area"] = area_text(kapt_area)
                # 같은 명부 번호가 여러 apt_seq에 대응해도 호 수는 합산 없이 복제
                row["households"] = str(registry_index[code][kapt_area])
                row.update(group_cache[code][kapt_area])
            elif status in AREA_FAILURE_REASONS:
                failure_reasons_by_apt[apt_seq].add(status)
                failed_combos_by_apt[apt_seq] += 1

            status_counts[status] += 1
            output.append(row)

    output.sort(
        key=lambda row: (
            row["sgg_cd"],
            row["apt_seq"],
            normalize_area(row["exclu_use_ar"]),
        )
    )
    diagnostics = {
        "db_rows": len(trade_rows),
        "raw_combos": len(raw_combos),
        "normalized_combos": sum(len(areas) for areas in combos_by_apt.values()),
        "apt_count": len(combos_by_apt),
        "status_counts": status_counts,
        "combos_by_apt": combos_by_apt,
        "group_cache": group_cache,
        "referenced_codes": referenced_codes,
        "failed_combos_by_apt": failed_combos_by_apt,
    }
    return output, dict(failure_reasons_by_apt), diagnostics


def build_combined_area_rows(trade_rows, rent_rows, crosswalk, registry_index):
    """대응표 안의 매매, 전월세 면적 합집합 산출."""
    _, trade_combos = _trade_combinations(trade_rows)
    trade_keys = {(apt, value) for apt, values in trade_combos.items() for value in values}
    rent_keys = set()
    included_rent_rows = []
    outside_apts = set()
    for row in rent_rows:
        apt = row.get("apt_seq")
        if not apt:
            raise ValueError("rent apt_seq 가 비어 있습니다.")
        value = required_area(row.get("exclu_use_ar"), f"rent apt_seq={apt}")
        if apt not in crosswalk:
            outside_apts.add(apt)
            continue
        rent_keys.add((apt, value))
        included_rent_rows.append(row)
    combined = trade_rows + included_rent_rows
    rows, failures, diagnostics = build_area_rows(combined, crosswalk, registry_index)
    added_keys = rent_keys - trade_keys
    diagnostics.update({
        "trade_db_rows": len(trade_rows),
        "rent_db_rows": len(rent_rows),
        "trade_combos": len(trade_keys),
        "rent_combos": len(rent_keys),
        "rent_outside_apts": len(outside_apts),
        "added_combos": len(added_keys),
        "added_status_counts": collections.Counter(
            row["area_status"] for row in rows
            if (row["apt_seq"], normalize_area(row["exclu_use_ar"])) in added_keys
        ),
    })
    return rows, failures, diagnostics


def _validate_nonarea_failures(rows, crosswalk) -> None:
    seen = set()
    for row in rows:
        layer, apt = row["fail_layer"], row["apt_seq"]
        if layer not in FAILURE_LAYER_ORDER or layer == "면적":
            raise ValueError(f"실패표 층 오류: {layer!r}")
        if row["fail_reason"] not in FAILURE_REASONS[layer]:
            raise ValueError(f"실패표 계약 밖 사유: {row['fail_reason']!r}")
        if (apt, layer) in seen:
            raise ValueError(f"실패표 (단지, 층) 중복: {apt}/{layer}")
        seen.add((apt, layer))
        if apt not in crosswalk or any(
            row[name] != crosswalk[apt][name] for name in ("sgg_cd", "apt_nm", "road_key")
        ):
            raise ValueError(f"실패표 표시값 불일치: {apt}; crosswalk.py 를 먼저 다시 실행")
    for layer, column in (("단지 명부", "reb_complex_id"), ("관리정보", "kapt_code")):
        actual = {row["apt_seq"] for row in rows if row["fail_layer"] == layer}
        expected = {apt for apt, row in crosswalk.items() if not row[column]}
        if actual != expected:
            raise ValueError(f"실패표 {layer} 집합 불일치; crosswalk.py 를 먼저 다시 실행")


def build_failure_rows(existing_rows, crosswalk, failure_reasons_by_apt) -> list[dict]:
    """비면적 층 보존 후 이번 산출의 면적 층으로 교체."""
    output = [row for row in existing_rows if row["fail_layer"] != "면적"]
    _validate_nonarea_failures(output, crosswalk)
    for apt, reasons in failure_reasons_by_apt.items():
        if not reasons or not reasons <= AREA_FAILURE_REASONS:
            raise ValueError(f"면적 층 계약 밖 사유: {apt}/{reasons!r}")
        if STATUS_TIE in reasons:
            reason = STATUS_TIE
        else:
            reason = STATUS_OUTSIDE
        source = crosswalk[apt]
        output.append({
            "sgg_cd": source["sgg_cd"],
            "apt_seq": apt,
            "apt_nm": source["apt_nm"],
            "road_key": source["road_key"],
            "fail_reason": reason,
            "fail_layer": "면적",
        })
    return sorted(output, key=lambda row: (
        row["sgg_cd"], row["apt_seq"], FAILURE_LAYER_ORDER[row["fail_layer"]]))


def write_csv(path, columns, rows) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _read_csv(path) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as source:
        return list(csv.DictReader(source))


def _validate_prewrite(crosswalk, db_apt_sequences) -> None:
    crosswalk_apt_sequences = set(crosswalk)
    if db_apt_sequences != crosswalk_apt_sequences:
        only_db = sorted(db_apt_sequences - crosswalk_apt_sequences)
        only_crosswalk = sorted(crosswalk_apt_sequences - db_apt_sequences)
        raise ValueError(
            "G2a DB/crosswalk apt_seq 집합 불일치: "
            f"DB만 {len(only_db):,}, crosswalk만 {len(only_crosswalk):,}; "
            f"표본 DB만={only_db[:5]!r}, crosswalk만={only_crosswalk[:5]!r}"
        )


def _validate_output_rows(rows, combos_by_apt, crosswalk, registry_index) -> None:
    """G5a/G5b/G6: 쓴 CSV를 다시 읽어 원천과 전수 대조."""
    statuses = {row["area_status"] for row in rows}
    unexpected = statuses - AREA_STATUSES
    if unexpected:
        raise ValueError(f"G5a 계약 밖 area_status: {sorted(unexpected)!r}")

    expected_keys = {
        (apt_seq, area)
        for apt_seq, areas in combos_by_apt.items()
        for area in areas
    }
    actual_keys = [(row["apt_seq"], normalize_area(row["exclu_use_ar"])) for row in rows]
    if len(actual_keys) != len(set(actual_keys)) or set(actual_keys) != expected_keys:
        raise ValueError("G5b 출력 (apt_seq, exclu_use_ar) 조합이 DB 조합과 다릅니다.")

    violations = []
    groups_by_label = collections.defaultdict(set)
    referenced_codes = {
        crosswalk[apt_seq]["reb_complex_id"]
        for apt_seq in combos_by_apt
        if (
            crosswalk[apt_seq]["reb_complex_id"]
            and crosswalk[apt_seq]["reb_complex_id"] in registry_index
        )
    }
    group_cache = {
        code: display_group_metadata(registry_index[code]) for code in referenced_codes
    }
    for row in rows:
        apt_seq = row["apt_seq"]
        source = crosswalk[apt_seq]
        exclu = normalize_area(row["exclu_use_ar"])
        code = source["reb_complex_id"]
        has_axis = bool(code and code in registry_index)
        status = row["area_status"]

        expected_status = STATUS_NO_REGISTRY
        expected_area = None
        if has_axis:
            # 산출 함수(nearest_kapt_area)를 다시 부르지 않는다. G5b는 원천 축에서
            # 거리, 상한, 최소값의 유일성을 직접 계산해야 같은 결함을 공유하지 않는다.
            distances = [
                (abs(exclu - candidate), candidate)
                for candidate in registry_index[code]
            ]
            minimum = min(distance for distance, _ in distances)
            winners = [
                candidate for distance, candidate in distances
                if distance == minimum
            ]
            if minimum > MATCH_LIMIT:
                expected_status = STATUS_OUTSIDE
            elif len(winners) > 1:
                expected_status = STATUS_TIE
            else:
                expected_status = STATUS_MATCHED
                expected_area = winners[0]
        if status != expected_status:
            violations.append(f"{apt_seq}/{area_text(exclu)} 상태 {status!r} != {expected_status!r}")
            continue

        if status == STATUS_MATCHED:
            if (
                row["kapt_code"] != ""
                or not row["kapt_area"]
                or not row["households"]
            ):
                violations.append(f"{apt_seq}/{area_text(exclu)} 매칭값 또는 세대수 비어 있음")
                continue
            actual_area = normalize_area(row["kapt_area"])
            if actual_area != expected_area:
                violations.append(f"{apt_seq}/{area_text(exclu)} 최근접 면적 불일치")
                continue
            if row["households"] != str(registry_index[code][expected_area]):
                violations.append(f"{apt_seq}/{area_text(exclu)} 세대수 불일치")
            expected_group = group_cache[code][expected_area]
            if any(row[name] != expected_group[name] for name in expected_group):
                violations.append(f"{apt_seq}/{area_text(exclu)} 명부 그룹 메타데이터 불일치")
        elif status in {STATUS_OUTSIDE, STATUS_TIE}:
            if (
                not has_axis
                or row["kapt_code"] != ""
                or row["kapt_area"]
                or row["households"]
                or any(row[name] for name in ("group_id", "group_label", "group_areas"))
            ):
                violations.append(f"{apt_seq}/{area_text(exclu)} 면적 실패 칸 불변식 위반")
        else:
            if has_axis or any(row[name] for name in (
                "kapt_code", "kapt_area", "households", "group_id", "group_label", "group_areas"
            )):
                violations.append(f"{apt_seq}/{area_text(exclu)} 명부 없음 칸 불변식 위반")

        if row["sgg_cd"] != source["sgg_cd"] or row["apt_nm"] != source["apt_nm"]:
            violations.append(f"{apt_seq}/{area_text(exclu)} crosswalk 표시값 불일치")

        group_id = row["group_id"]
        group_label = row["group_label"]
        if group_id or group_label or row["group_areas"]:
            if not (group_id and group_label and row["group_areas"]):
                violations.append(f"{apt_seq}/{area_text(exclu)} 그룹 메타데이터 일부만 존재")
            else:
                groups_by_label[(apt_seq, group_label)].add(group_id)

    if violations:
        raise ValueError(
            f"G5b 상태별 불변식 위반 {len(violations):,}건: " + "; ".join(violations[:10])
        )
    collisions = {
        key: group_ids
        for key, group_ids in groups_by_label.items()
        if len(group_ids) > 1
    }
    if collisions:
        sample = list(collisions.items())[:10]
        raise ValueError(f"G6 같은 단지의 group_label 충돌 {len(collisions):,}건: {sample!r}")

    full_axis_collisions = {}
    for code, metadata in group_cache.items():
        group_ids_by_label = collections.defaultdict(set)
        for record in metadata.values():
            group_ids_by_label[record["group_label"]].add(record["group_id"])
        collisions = {
            label: group_ids
            for label, group_ids in group_ids_by_label.items()
            if len(group_ids) > 1
        }
        if collisions:
            full_axis_collisions[code] = collisions
    if full_axis_collisions:
        sample = list(full_axis_collisions.items())[:10]
        raise ValueError(
            f"G6 명부 전체 축 group_label 충돌 {len(full_axis_collisions):,}코드: {sample!r}"
        )


def _validate_failure_rows(rows, crosswalk, area_rows) -> None:
    _validate_nonarea_failures([row for row in rows if row["fail_layer"] != "면적"], crosswalk)
    reasons = collections.defaultdict(set)
    for row in area_rows:
        if row["area_status"] in AREA_FAILURE_REASONS:
            reasons[row["apt_seq"]].add(row["area_status"])
    expected = build_failure_rows(
        [row for row in rows if row["fail_layer"] != "면적"], crosswalk, reasons)
    if rows != expected:
        raise ValueError("matching-failures 층별 사유 또는 실제 산출 상태 불일치")


def _group_diagnostics(registry_index, crosswalk, diagnostics):
    referenced_codes = diagnostics["referenced_codes"]
    group_cache = diagnostics["group_cache"]
    code_group_count = 0
    max_group_size = 0
    household_tie_count = 0
    tie_label_difference_count = 0
    half_up_difference_count = 0
    order_mismatches = 0
    rng = random.Random(0)

    for code in referenced_codes:
        areas = list(registry_index[code])
        groups = display_groups(areas)
        code_group_count += len(groups)
        max_group_size = max(max_group_size, *(len(group) for group in groups))
        for group in groups:
            maximum = max(registry_index[code][area] for area in group)
            tied = [area for area in group if registry_index[code][area] == maximum]
            if len(tied) > 1:
                household_tie_count += 1
                if len({half_up_label(area) for area in tied}) > 1:
                    tie_label_difference_count += 1
            source = min(group, key=lambda area: (-registry_index[code][area], area))
            half_even = format(
                source.quantize(Decimal("1"), rounding=ROUND_HALF_EVEN), "f"
            )
            if half_up_label(source) != half_even:
                half_up_difference_count += 1

        baseline = group_cache[code]
        reversed_items = list(reversed(list(registry_index[code].items())))
        shuffled_items = list(registry_index[code].items())
        rng.shuffle(shuffled_items)
        if (
            display_group_metadata(dict(reversed_items)) != baseline
            or display_group_metadata(dict(shuffled_items)) != baseline
        ):
            order_mismatches += 1

    apt_with_axis = [
        row for row in crosswalk.values()
        if row["reb_complex_id"] and row["reb_complex_id"] in registry_index
    ]
    apt_group_count = sum(
        len({record["group_id"] for record in group_cache[row["reb_complex_id"]].values()})
        for row in apt_with_axis
    )
    apt_singleton_count = sum(
        sum(len(group) == 1 for group in display_groups(registry_index[row["reb_complex_id"]]))
        for row in apt_with_axis
    )
    return {
        "kapt_code_count": len(referenced_codes),
        "kapt_code_groups": code_group_count,
        "apt_with_axis_count": len(apt_with_axis),
        "apt_groups": apt_group_count,
        "apt_singleton_groups": apt_singleton_count,
        "max_group_size": max_group_size,
        "household_tie_groups": household_tie_count,
        "tie_label_difference_groups": tie_label_difference_count,
        "half_up_difference_groups": half_up_difference_count,
        "order_mismatches": order_mismatches,
    }


def _print_report(rows, failures, crosswalk, registry_index, diagnostics) -> None:
    statuses = diagnostics["status_counts"]
    missing_registry_apts = sum(
        bool(row["reb_complex_id"] and row["reb_complex_id"] not in registry_index)
        for row in crosswalk.values()
    )
    group_stats = _group_diagnostics(registry_index, crosswalk, diagnostics)
    print(f"\n매매 / 전월세 조회 행: {diagnostics['trade_db_rows']:,} / {diagnostics['rent_db_rows']:,}")
    print(f"  대응표 밖 전월세 단지: {diagnostics['rent_outside_apts']:,}")
    print(
        "  매매 / 전월세(대응표 안) / 합집합 / 전월세만 면적 조합: "
        f"{diagnostics['trade_combos']:,} / {diagnostics['rent_combos']:,} / "
        f"{len(rows):,} / {diagnostics['added_combos']:,}"
    )
    for status in sorted(AREA_STATUSES):
        print(f"  {status}: {statuses[status]:,}")
        print(f"    전월세만: {diagnostics['added_status_counts'][status]:,}")
    print(f"  실패표 전체 행: {len(failures):,}")
    print(
        "  원본 표기 조합 / 정규화 조합: "
        f"{diagnostics['raw_combos']:,} / {diagnostics['normalized_combos']:,}"
    )
    print(
        "  명부 그룹 grain: "
        f"{group_stats['kapt_code_count']:,}코드 / {group_stats['kapt_code_groups']:,}그룹"
    )
    print(
        "  출력 그룹 grain: "
        f"{group_stats['apt_with_axis_count']:,} apt_seq / {group_stats['apt_groups']:,}그룹"
    )
    print(
        "  그룹 구성 1개 / 최대 구성 수 / 세대수 최다 동점 / 라벨 영향 동점 / "
        "HALF_UP≠HALF_EVEN: "
        f"{group_stats['apt_singleton_groups']:,} / {group_stats['max_group_size']:,} / "
        f"{group_stats['household_tie_groups']:,} / "
        f"{group_stats['tie_label_difference_groups']:,} / "
        f"{group_stats['half_up_difference_groups']:,}"
    )
    print(f"  명부 그룹 정방향/역방향/셔플 불일치: {group_stats['order_mismatches']:,}")
    print(f"  명부 번호는 있으나 면적 명부 행이 없는 apt_seq: {missing_registry_apts:,}")
    print(f"  명부 번호가 빈 apt_seq: {sum(not row['reb_complex_id'] for row in crosswalk.values()):,}")

    matched_rows = [row for row in rows if row["area_status"] == STATUS_MATCHED]
    source_conflicts = sum(
        boundary_conflict_count(row["exclu_use_ar"], row["kapt_area"])
        for row in matched_rows
    )
    print(f"  exclu_use_ar / kapt_area 경계 충돌 합계: {source_conflicts:,}")
    for boundary in BOUNDARIES:
        count = sum(
            (normalize_area(row["exclu_use_ar"]) > boundary)
            != (Decimal(row["group_label"]) > boundary)
            for row in matched_rows
        )
        print(f"  group_label / exclu_use_ar {boundary}㎡ 경계 충돌: {count:,}")

    print(f"  면적 동점 조합: {statuses[STATUS_TIE]:,}")
    print("  ±0.5㎡ 밖 단지 (실패 조합/전체 조합):")
    failed = diagnostics["failed_combos_by_apt"]
    outside_apts = sorted(
        {
            row["apt_seq"]
            for row in rows
            if row["area_status"] == STATUS_OUTSIDE
        },
        key=lambda apt: (crosswalk[apt]["sgg_cd"], apt),
    )
    for apt_seq in outside_apts:
        print(
            f"    {apt_seq} {crosswalk[apt_seq]['apt_nm']}: "
            f"{failed[apt_seq]:,}/{len(diagnostics['combos_by_apt'][apt_seq]):,}"
        )


def main(
    path=PRICE_AREA_PATH,
    *,
    out_area=COMPLEX_AREA_PATH,
    out_failures=MATCHING_FAILURES_PATH,
) -> None:
    """매매, 전월세 면적 조합 산출 후 게이트 검사."""
    try:
        registry_index = load_registry_index(path)
        crosswalk = load_crosswalk()
        existing_failures = load_failure_rows()
        _validate_nonarea_failures(
            [row for row in existing_failures if row["fail_layer"] != "면적"], crosswalk)
        committed_area_rows = load_committed_area_rows()

        trade_rows = fetch_all_trade_rows()
        db_apt_sequences = {row["apt_seq"] for row in trade_rows if row.get("apt_seq")}
        _validate_prewrite(crosswalk, db_apt_sequences)
        rent_rows = fetch_all_rent_rows()

        area_rows, failure_reasons_by_apt, diagnostics = build_combined_area_rows(
            trade_rows, rent_rows, crosswalk, registry_index
        )
        failure_rows = build_failure_rows(
            existing_failures, crosswalk, failure_reasons_by_apt
        )

        write_csv(out_area, OUTPUT_COLUMNS, area_rows)
        write_csv(out_failures, FAILURE_COLUMNS, failure_rows)

        written_area_rows = _read_csv(out_area)
        written_failure_rows = _read_csv(out_failures)
        _validate_output_rows(
            written_area_rows,
            diagnostics["combos_by_apt"],
            crosswalk,
            registry_index,
        )
        _validate_failure_rows(written_failure_rows, crosswalk, written_area_rows)

        new_tie_apts = unapproved_tie_apts(
            written_area_rows, committed_area_rows
        )
        _print_report(
            written_area_rows, written_failure_rows, crosswalk, registry_index, diagnostics,
        )
        if new_tie_apts:
            print("G4 새 면적 동점 조합:")
            for apt_seq, area in new_tie_apts:
                print(f"  {apt_seq} {crosswalk[apt_seq]['apt_nm']} {area_text(area)}")
            raise ValueError(f"G4 승인되지 않은 면적 동점 조합 {len(new_tie_apts):,}개")

        print(f"\n저장: {out_area} ({len(written_area_rows):,}행)")
        print(f"저장: {out_failures} ({len(written_failure_rows):,}행)")
        print("게이트 G2a / G4 / G5a / G5b / G6: 통과")
    except (ValueError, RuntimeError) as exc:
        sys.exit(f"[중단] {exc}")


if __name__ == "__main__":
    main()
