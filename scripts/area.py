"""K-apt 면적 로딩, 실거래 면적 매칭, 표시 그룹 CSV 산출.

파일명에는 구 접미사를 붙이지 않는다. 서울 전체를 한 파일에 담아 M4 입력으로 쓴다.

실행
    set -a; source .env; set +a
    python3 scripts/area.py

필요 패키지: openpyxl
"""

import collections
import csv
import hashlib
import importlib.util
import io
import json
import pathlib
import random
import subprocess
import sys
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, ROUND_HALF_UP
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import openpyxl


HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
KAPT_AREA_XLSX_PATH = ROOT / "data" / "kapt-area-info.xlsx"
CROSSWALK_PATH = ROOT / "data" / "complex-crosswalk.csv"
COMPLEX_AREA_PATH = ROOT / "data" / "complex-area.csv"
MATCHING_FAILURES_PATH = ROOT / "data" / "matching-failures.csv"

HEADER_NAME = "단지코드"
AREA_NAME = "주거전용면적(세부)"
HOUSEHOLDS_NAME = "세대수"
REQUIRED_COLUMNS = (HEADER_NAME, AREA_NAME, HOUSEHOLDS_NAME)
TEXT_VALUE_COLUMNS = REQUIRED_COLUMNS

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
FAILURE_COLUMNS = ["sgg_cd", "apt_seq", "apt_nm", "road_key", "fail_reason"]

STATUS_MATCHED = "면적 매칭"
STATUS_OUTSIDE = "면적 ±0.5㎡ 밖"
STATUS_TIE = "면적 동점(모호)"
STATUS_NO_KAPT = "K-apt 면적정보 없음"
AREA_STATUSES = {STATUS_MATCHED, STATUS_OUTSIDE, STATUS_TIE, STATUS_NO_KAPT}
AREA_FAILURE_REASONS = {STATUS_OUTSIDE, STATUS_TIE}
ROAD_FAILURE_REASONS = {
    "K-apt 마스터에 없음",
    "후보 2개 이상(모호)",
    "도로명 키 생성 불가",
}

MATCH_LIMIT = Decimal("0.5")
BOUNDARIES = (Decimal("60"), Decimal("85"), Decimal("102"))
EXPECTED_XLSX_SHA_PREFIX = "24825264"
EXPECTED_XLSX_ROWS = 18_062
EXPECTED_KAPT_CODES = 3_137
EXPECTED_CROSSWALK_MAPPING_SHA_PREFIX = "9a938e87"


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


def load_kapt_area_rows(path=KAPT_AREA_XLSX_PATH) -> list[dict]:
    """헤더 행을 찾아 다음 행부터 원본 값의 dict 목록으로 반환."""
    workbook = openpyxl.load_workbook(path, read_only=True)
    rows = None
    try:
        if len(workbook.sheetnames) != 1:
            raise ValueError(
                "K-apt 면적정보 xlsx의 시트가 1개여야 합니다: "
                f"{workbook.sheetnames!r}"
            )

        rows = workbook.active.iter_rows(values_only=True)
        for header_row_number, values in enumerate(rows, start=1):
            if HEADER_NAME in values:
                headers = values
                break
        else:
            raise ValueError(f"필수 헤더 {HEADER_NAME!r}를 찾지 못했습니다.")

        missing = [name for name in REQUIRED_COLUMNS if name not in headers]
        if missing:
            raise ValueError(f"필수 열이 없습니다: {', '.join(missing)}")

        data = []
        for row_number, values in enumerate(rows, start=header_row_number + 1):
            record = dict(zip(headers, values))
            for name in TEXT_VALUE_COLUMNS:
                value = record[name]
                if value is not None and not isinstance(value, str):
                    raise ValueError(
                        f"{name!r} 값은 문자열이어야 합니다: {row_number}행 {value!r}"
                    )
            data.append(record)

        if not data:
            raise ValueError("K-apt 면적정보에 데이터 행이 없습니다.")
        return data
    finally:
        if rows is not None:
            rows.close()
        workbook.close()


def _integer_households(value, *, row_number=None) -> int:
    if value is None or value == "":
        where = f" {row_number}행" if row_number is not None else ""
        raise ValueError(f"{HOUSEHOLDS_NAME!r} 값이 비어 있습니다:{where}")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{HOUSEHOLDS_NAME!r} 값이 정수가 아닙니다: {value!r}") from exc
    if number != number.to_integral_value():
        raise ValueError(f"{HOUSEHOLDS_NAME!r} 값이 정수가 아닙니다: {value!r}")
    return int(number)


def build_kapt_index(rows: list[dict]) -> dict[str, dict[Decimal, int]]:
    """K-apt 행을 단지코드 -> 면적 -> 세대수로 색인."""
    index = collections.defaultdict(dict)
    for row_number, row in enumerate(rows, start=1):
        code = row[HEADER_NAME]
        if not code:
            raise ValueError(f"{HEADER_NAME!r} 값이 비어 있습니다: 데이터 {row_number}행")
        area = normalize_area(row[AREA_NAME])
        households = _integer_households(row[HOUSEHOLDS_NAME], row_number=row_number)
        if area in index[code]:
            raise ValueError(f"K-apt 단지코드/면적이 중복입니다: {code} / {area_text(area)}")
        index[code][area] = households
    return dict(index)


def load_crosswalk(path=CROSSWALK_PATH) -> dict[str, dict]:
    with open(path, encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        required = {"sgg_cd", "apt_seq", "apt_nm", "road_key", "kapt_code"}
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


def load_committed_failure_rows() -> list[dict]:
    """G4 승인 기준인 Git HEAD의 실패표를 읽음."""
    result = subprocess.run(
        ["git", "show", "HEAD:data/matching-failures.csv"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"G4 커밋 승인 기준을 읽지 못했습니다: {message}")
    source = io.StringIO(result.stdout.decode("utf-8-sig"), newline="")
    reader = csv.DictReader(source)
    if reader.fieldnames != FAILURE_COLUMNS:
        raise ValueError(
            "커밋된 matching-failures.csv 열이 계약과 다릅니다: "
            f"{reader.fieldnames!r}"
        )
    return list(reader)


def unapproved_tie_apts(current_rows, committed_rows) -> list[str]:
    """현재 산출의 동점 중 커밋된 실패표로 승인되지 않은 apt_seq."""
    current = {
        row["apt_seq"] for row in current_rows if row["fail_reason"] == STATUS_TIE
    }
    committed = {
        row["apt_seq"] for row in committed_rows if row["fail_reason"] == STATUS_TIE
    }
    return sorted(current - committed)


def crosswalk_mapping_hash(crosswalk: dict[str, dict]) -> str:
    pairs = sorted((apt_seq, row["kapt_code"]) for apt_seq, row in crosswalk.items())
    payload = "\n".join(f"{apt_seq},{kapt_code}" for apt_seq, kapt_code in pairs)
    return hashlib.sha256(payload.encode()).hexdigest()


def fetch_trade_rows(sgg_cd: str, url: str, key: str) -> list[dict]:
    """한 구의 전 기간 current 거래에서 면적 조합에 필요한 열만 전부 조회."""
    output = []
    offset = 0
    path = (
        "trade?select=apt_seq,exclu_use_ar"
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
        output.extend(chunk)
        if len(chunk) < coverage.PAGE_SIZE:
            return output
        offset += coverage.PAGE_SIZE


def fetch_all_trade_rows() -> list[dict]:
    url, key = coverage.sb_config()
    output = []
    for sgg_cd, district_name in coverage.load_districts():
        rows = fetch_trade_rows(sgg_cd, url, key)
        output.extend(rows)
        print(f"조회 {sgg_cd} {district_name}: {len(rows):,}행 (누적 {len(output):,})")
        sys.stdout.flush()
    return output


def nearest_kapt_area(exclu_use_ar, candidates) -> tuple[str, Decimal | None]:
    """단지 안에서 ±0.5㎡ 최근접 하나만 할당하고 동점은 고르지 않음."""
    exclu = normalize_area(exclu_use_ar)
    areas = sorted({normalize_area(candidate) for candidate in candidates})
    if not areas:
        return STATUS_NO_KAPT, None
    distances = [(abs(exclu - area), area) for area in areas]
    minimum = min(distance for distance, _ in distances)
    if minimum > MATCH_LIMIT:
        return STATUS_OUTSIDE, None
    winners = [area for distance, area in distances if distance == minimum]
    if len(winners) > 1:
        return STATUS_TIE, None
    return STATUS_MATCHED, winners[0]


def display_group_metadata(area_households) -> dict[Decimal, dict[str, str]]:
    """각 축 면적을 순서 무관한 표시 그룹 메타데이터에 대응."""
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


def fallback_group_metadata(areas) -> dict[Decimal, dict[str, str]]:
    """K-apt 축이 없을 때 거래 면적을 병합하고 최소 면적으로 라벨링."""
    metadata = {}
    for group in display_groups(areas):
        group_record = {
            "group_id": area_text(group[0]),
            "group_label": half_up_label(group[0]),
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
        normalized[apt_seq].add(normalize_area(value))
    return raw, dict(normalized)


def build_area_rows(
    trade_rows: list[dict],
    crosswalk: dict[str, dict],
    kapt_index: dict[str, dict[Decimal, int]],
):
    """CSV 행과 진단값을 메모리에서 결정적으로 계산."""
    raw_combos, combos_by_apt = _trade_combinations(trade_rows)
    referenced_codes = sorted(
        {
            row["kapt_code"]
            for row in crosswalk.values()
            if row["kapt_code"] and row["kapt_code"] in kapt_index
        }
    )
    group_cache = {
        code: display_group_metadata(kapt_index[code]) for code in referenced_codes
    }

    output = []
    failure_reasons_by_apt = collections.defaultdict(set)
    failed_combos_by_apt = collections.Counter()
    status_counts = collections.Counter()

    for apt_seq, trade_areas in combos_by_apt.items():
        source = crosswalk[apt_seq]
        code = source["kapt_code"]
        has_kapt_axis = bool(code and code in kapt_index)
        fallback = fallback_group_metadata(trade_areas) if not has_kapt_axis else None

        for exclu in trade_areas:
            kapt_area = None
            if has_kapt_axis:
                status, kapt_area = nearest_kapt_area(exclu, kapt_index[code])
            else:
                status = STATUS_NO_KAPT

            row = {
                "sgg_cd": source["sgg_cd"],
                "apt_seq": apt_seq,
                "exclu_use_ar": area_text(exclu),
                "area_status": status,
                "kapt_code": code if has_kapt_axis else "",
                "kapt_area": "",
                "households": "",
                "group_id": "",
                "group_label": "",
                "group_areas": "",
                "apt_nm": source["apt_nm"],
            }
            if status == STATUS_MATCHED:
                row["kapt_area"] = area_text(kapt_area)
                # 한 K-apt 코드가 여러 apt_seq 에 대응해도 세대수는 합산하지 않고 복제한다.
                row["households"] = str(kapt_index[code][kapt_area])
                row.update(group_cache[code][kapt_area])
            elif status in AREA_FAILURE_REASONS:
                failure_reasons_by_apt[apt_seq].add(status)
                failed_combos_by_apt[apt_seq] += 1
            else:
                row.update(fallback[exclu])

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


def build_failure_rows(
    existing_rows: list[dict],
    crosswalk: dict[str, dict],
    failure_reasons_by_apt: dict[str, set[str]],
) -> list[dict]:
    """도로명 실패 블록은 그대로 두고 면적 실패 블록만 멱등 교체."""
    output = [row for row in existing_rows if row["fail_reason"] not in AREA_FAILURE_REASONS]
    reason_order = {STATUS_OUTSIDE: 0, STATUS_TIE: 1}
    additions = []
    for apt_seq, reasons in failure_reasons_by_apt.items():
        source = crosswalk[apt_seq]
        for reason in reasons:
            additions.append(
                {
                    "sgg_cd": source["sgg_cd"],
                    "apt_seq": apt_seq,
                    "apt_nm": source["apt_nm"],
                    "road_key": source["road_key"],
                    "fail_reason": reason,
                }
            )
    additions.sort(
        key=lambda row: (
            row["sgg_cd"],
            row["apt_seq"],
            reason_order[row["fail_reason"]],
        )
    )
    return output + additions


def write_csv(path, columns, rows) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _read_csv(path) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as source:
        return list(csv.DictReader(source))


def _validate_prewrite(xlsx_path, kapt_rows, crosswalk, db_apt_sequences) -> str:
    xlsx_sha = hashlib.sha256(pathlib.Path(xlsx_path).read_bytes()).hexdigest()
    if not xlsx_sha.startswith(EXPECTED_XLSX_SHA_PREFIX):
        raise ValueError(
            f"G1 xlsx SHA-256 불일치: {xlsx_sha[:8]} != {EXPECTED_XLSX_SHA_PREFIX}"
        )
    distinct_codes = {row[HEADER_NAME] for row in kapt_rows if row[HEADER_NAME]}
    if len(kapt_rows) != EXPECTED_XLSX_ROWS or len(distinct_codes) != EXPECTED_KAPT_CODES:
        raise ValueError(
            "G3 xlsx 행/단지코드 수 불일치: "
            f"{len(kapt_rows):,}/{len(distinct_codes):,} != "
            f"{EXPECTED_XLSX_ROWS:,}/{EXPECTED_KAPT_CODES:,}"
        )
    crosswalk_apt_sequences = set(crosswalk)
    if db_apt_sequences != crosswalk_apt_sequences:
        only_db = sorted(db_apt_sequences - crosswalk_apt_sequences)
        only_crosswalk = sorted(crosswalk_apt_sequences - db_apt_sequences)
        raise ValueError(
            "G2a DB/crosswalk apt_seq 집합 불일치: "
            f"DB만 {len(only_db):,}, crosswalk만 {len(only_crosswalk):,}; "
            f"표본 DB만={only_db[:5]!r}, crosswalk만={only_crosswalk[:5]!r}"
        )
    return xlsx_sha


def _validate_output_rows(rows, combos_by_apt, crosswalk, kapt_index) -> None:
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
        crosswalk[apt_seq]["kapt_code"]
        for apt_seq in combos_by_apt
        if (
            crosswalk[apt_seq]["kapt_code"]
            and crosswalk[apt_seq]["kapt_code"] in kapt_index
        )
    }
    group_cache = {
        code: display_group_metadata(kapt_index[code]) for code in referenced_codes
    }
    fallback_cache = {
        apt_seq: fallback_group_metadata(areas)
        for apt_seq, areas in combos_by_apt.items()
        if not (
            crosswalk[apt_seq]["kapt_code"]
            and crosswalk[apt_seq]["kapt_code"] in kapt_index
        )
    }
    for row in rows:
        apt_seq = row["apt_seq"]
        source = crosswalk[apt_seq]
        exclu = normalize_area(row["exclu_use_ar"])
        code = source["kapt_code"]
        has_axis = bool(code and code in kapt_index)
        status = row["area_status"]

        expected_status = STATUS_NO_KAPT
        expected_area = None
        if has_axis:
            # 산출 함수(nearest_kapt_area)를 다시 부르지 않는다. G5b는 원천 축에서
            # 거리, 상한, 최소값의 유일성을 직접 계산해야 같은 결함을 공유하지 않는다.
            distances = [
                (abs(exclu - candidate), candidate)
                for candidate in kapt_index[code]
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
                row["kapt_code"] != code
                or not row["kapt_area"]
                or not row["households"]
            ):
                violations.append(f"{apt_seq}/{area_text(exclu)} 매칭값 또는 세대수 비어 있음")
                continue
            actual_area = normalize_area(row["kapt_area"])
            if actual_area != expected_area:
                violations.append(f"{apt_seq}/{area_text(exclu)} 최근접 면적 불일치")
                continue
            if row["households"] != str(kapt_index[code][expected_area]):
                violations.append(f"{apt_seq}/{area_text(exclu)} 세대수 불일치")
            expected_group = group_cache[code][expected_area]
            if any(row[name] != expected_group[name] for name in expected_group):
                violations.append(f"{apt_seq}/{area_text(exclu)} K-apt 그룹 메타데이터 불일치")
        elif status in AREA_FAILURE_REASONS:
            if (
                not has_axis
                or row["kapt_code"] != code
                or row["kapt_area"]
                or row["households"]
                or any(row[name] for name in ("group_id", "group_label", "group_areas"))
            ):
                violations.append(f"{apt_seq}/{area_text(exclu)} 면적 실패 칸 불변식 위반")
        else:
            if has_axis or any(row[name] for name in ("kapt_code", "kapt_area", "households")):
                violations.append(f"{apt_seq}/{area_text(exclu)} K-apt 없음 칸 불변식 위반")
            expected_group = fallback_cache[apt_seq][exclu]
            if any(row[name] != expected_group[name] for name in expected_group):
                violations.append(f"{apt_seq}/{area_text(exclu)} fallback 그룹 메타데이터 불일치")

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
            f"G6 K-apt 전체 축 group_label 충돌 {len(full_axis_collisions):,}코드: {sample!r}"
        )


def _validate_failure_rows(rows, crosswalk) -> None:
    reasons = {row["fail_reason"] for row in rows}
    unexpected = reasons - ROAD_FAILURE_REASONS - AREA_FAILURE_REASONS
    if unexpected:
        raise ValueError(f"matching-failures 계약 밖 사유: {sorted(unexpected)!r}")
    road_rows = [row for row in rows if row["fail_reason"] in ROAD_FAILURE_REASONS]
    area_rows = [row for row in rows if row["fail_reason"] in AREA_FAILURE_REASONS]
    blank_crosswalk = {apt for apt, row in crosswalk.items() if not row["kapt_code"]}
    road_apts = {row["apt_seq"] for row in road_rows}
    area_apts = {row["apt_seq"] for row in area_rows}
    if len(road_rows) != len(blank_crosswalk) or road_apts != blank_crosswalk:
        raise ValueError("matching-failures 도로명 블록이 crosswalk 빈 kapt_code 와 다릅니다.")
    if road_apts & area_apts:
        raise ValueError("matching-failures 도로명/면적 실패 apt_seq 가 겹칩니다.")
    if any(not crosswalk[apt]["kapt_code"] for apt in area_apts):
        raise ValueError("matching-failures 면적 실패에 kapt_code 없는 단지가 있습니다.")


def _group_diagnostics(kapt_index, crosswalk, diagnostics):
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
        areas = list(kapt_index[code])
        groups = display_groups(areas)
        code_group_count += len(groups)
        max_group_size = max(max_group_size, *(len(group) for group in groups))
        for group in groups:
            maximum = max(kapt_index[code][area] for area in group)
            tied = [area for area in group if kapt_index[code][area] == maximum]
            if len(tied) > 1:
                household_tie_count += 1
                if len({half_up_label(area) for area in tied}) > 1:
                    tie_label_difference_count += 1
            source = min(group, key=lambda area: (-kapt_index[code][area], area))
            half_even = format(
                source.quantize(Decimal("1"), rounding=ROUND_HALF_EVEN), "f"
            )
            if half_up_label(source) != half_even:
                half_up_difference_count += 1

        baseline = group_cache[code]
        reversed_items = list(reversed(list(kapt_index[code].items())))
        shuffled_items = list(kapt_index[code].items())
        rng.shuffle(shuffled_items)
        if (
            display_group_metadata(dict(reversed_items)) != baseline
            or display_group_metadata(dict(shuffled_items)) != baseline
        ):
            order_mismatches += 1

    apt_with_axis = [
        row for row in crosswalk.values()
        if row["kapt_code"] and row["kapt_code"] in kapt_index
    ]
    apt_group_count = sum(
        len({record["group_id"] for record in group_cache[row["kapt_code"]].values()})
        for row in apt_with_axis
    )
    apt_singleton_count = sum(
        sum(len(group) == 1 for group in display_groups(kapt_index[row["kapt_code"]]))
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


def _print_report(rows, failures, crosswalk, kapt_index, diagnostics) -> None:
    statuses = diagnostics["status_counts"]
    missing_xlsx_apts = sum(
        bool(row["kapt_code"] and row["kapt_code"] not in kapt_index)
        for row in crosswalk.values()
    )
    group_stats = _group_diagnostics(kapt_index, crosswalk, diagnostics)
    expected = {
        "DB 원본 행": 194_976,
        "정규화 조합": 21_044,
        STATUS_MATCHED: 10_218,
        STATUS_OUTSIDE: 75,
        STATUS_TIE: 0,
        STATUS_NO_KAPT: 10_751,
        "실패표 전체 행": 4_999,
        "kapt_code 그룹": 8_769,
        "apt_seq 그룹": 9_134,
    }
    actual = {
        "DB 원본 행": diagnostics["db_rows"],
        "정규화 조합": diagnostics["normalized_combos"],
        STATUS_MATCHED: statuses[STATUS_MATCHED],
        STATUS_OUTSIDE: statuses[STATUS_OUTSIDE],
        STATUS_TIE: statuses[STATUS_TIE],
        STATUS_NO_KAPT: statuses[STATUS_NO_KAPT],
        "실패표 전체 행": len(failures),
        "kapt_code 그룹": group_stats["kapt_code_groups"],
        "apt_seq 그룹": group_stats["apt_groups"],
    }

    print("\n예상값 / 실제값 (관측 전용)")
    for name, expected_value in expected.items():
        print(f"  {name}: {expected_value:,} / {actual[name]:,}")
    print(
        "  원본 표기 조합 / 정규화 조합: "
        f"{diagnostics['raw_combos']:,} / {diagnostics['normalized_combos']:,}"
    )
    print(
        "  K-apt 그룹 grain: "
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
    print(f"  K-apt 그룹 정방향/역방향/셔플 불일치: {group_stats['order_mismatches']:,}")
    print(f"  xlsx 에 kapt_code 없는 apt_seq: {missing_xlsx_apts:,}")

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
    path=KAPT_AREA_XLSX_PATH,
    *,
    out_area=COMPLEX_AREA_PATH,
    out_failures=MATCHING_FAILURES_PATH,
) -> None:
    """전수 면적 조합을 매칭해 두 CSV를 쓰고 G1~G6을 검사."""
    try:
        kapt_rows = load_kapt_area_rows(path)
        kapt_index = build_kapt_index(kapt_rows)
        crosswalk = load_crosswalk()
        existing_failures = load_failure_rows()
        committed_failures = load_committed_failure_rows()

        mapping_sha = crosswalk_mapping_hash(crosswalk)
        print(f"crosswalk (apt_seq,kapt_code) SHA-256: {mapping_sha[:8]}")
        if not mapping_sha.startswith(EXPECTED_CROSSWALK_MAPPING_SHA_PREFIX):
            print(
                "[경고] crosswalk 대응이 0-9 예상값 산출 시점과 다릅니다. "
                "차이표를 드리프트로 읽으세요."
            )

        trade_rows = fetch_all_trade_rows()
        db_apt_sequences = {row["apt_seq"] for row in trade_rows if row.get("apt_seq")}
        _validate_prewrite(path, kapt_rows, crosswalk, db_apt_sequences)

        area_rows, failure_reasons_by_apt, diagnostics = build_area_rows(
            trade_rows, crosswalk, kapt_index
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
            kapt_index,
        )
        _validate_failure_rows(written_failure_rows, crosswalk)

        new_tie_apts = unapproved_tie_apts(
            written_failure_rows, committed_failures
        )
        if new_tie_apts:
            print("G4 새 면적 동점 단지:")
            for apt_seq in new_tie_apts:
                print(f"  {apt_seq} {crosswalk[apt_seq]['apt_nm']}")
            raise ValueError(f"G4 승인되지 않은 면적 동점 단지 {len(new_tie_apts):,}개")

        _print_report(
            written_area_rows,
            written_failure_rows,
            crosswalk,
            kapt_index,
            diagnostics,
        )
        print(f"\n저장: {out_area} ({len(written_area_rows):,}행)")
        print(f"저장: {out_failures} ({len(written_failure_rows):,}행)")
        print("게이트 G1 / G2a / G3 / G4 / G5a / G5b / G6: 통과")
    except (ValueError, RuntimeError) as exc:
        sys.exit(f"[중단] {exc}")


if __name__ == "__main__":
    main()
