"""두 CSV를 단지 마스터 테이블에 적재한다.

실행
    요구 Python 버전: 3.11 이상
    python3 scripts/load_complex.py
"""

import csv
import hashlib
import json
import pathlib
from datetime import datetime, timezone
from decimal import (
    Decimal,
    DefaultContext,
    InvalidOperation,
    ROUND_HALF_UP,
    localcontext,
)
from typing import TypedDict
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

if __package__:
    from . import collect
else:
    import collect


ROOT = pathlib.Path(__file__).resolve().parents[1]
CROSSWALK_PATH = ROOT / "data" / "complex-crosswalk.csv"
AREA_PATH = ROOT / "data" / "complex-area.csv"
AREA_SCALE = Decimal("0.0001")  # complex_area 면적 칸은 numeric(9,4)
AREA_TEXT_FORMAT = f".{-AREA_SCALE.as_tuple().exponent}f"  # numeric(9,4) 고정 스케일로 DB ::text와 같은 형식
DIGEST_COLUMNS = {
    "complex": (
        "apt_seq", "sgg_cd", "apt_nm", "road_key", "kapt_code", "kapt_key",
        "match_source", "first_seen_at",
    ),
    "complex_area": (
        "apt_seq", "exclu_use_ar", "area_status", "kapt_area", "households",
        "group_id", "group_label", "group_areas", "first_seen_at",
    ),
}
DIGEST_ORDER = {
    "complex": "apt_seq",
    "complex_area": "apt_seq,exclu_use_ar",
}

CROSSWALK_COLUMNS = (
    "sgg_cd",
    "apt_seq",
    "apt_nm",
    "road_key",
    "kapt_code",
    "kapt_key",
    "match_source",
)

AREA_COLUMNS = (
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
)


class InputError(ValueError):
    """입력 CSV가 명세의 헤더나 값 계약과 다름."""


class ComplexRow(TypedDict):
    sgg_cd: str
    apt_seq: str
    apt_nm: str
    road_key: str | None
    kapt_code: str | None
    kapt_key: str | None
    match_source: str | None


class ComplexAreaPayload(TypedDict):
    apt_seq: str
    exclu_use_ar: str
    area_status: str
    kapt_area: str | None
    households: int | None
    group_id: str | None
    group_label: int | None
    group_areas: str | None


class ComplexAreaValidation(TypedDict):
    sgg_cd: str
    apt_nm: str
    max_decimal_places: int


class ComplexAreaRow(TypedDict):
    payload: ComplexAreaPayload
    validation: ComplexAreaValidation


def _required_text(value: str | None, column: str, line: int) -> str:
    if value is None or value.strip() == "":
        raise InputError(f"{line}행 {column}: 빈 값이 될 수 없습니다")
    return value


def _optional_text(value: str | None, column: str, line: int) -> str | None:
    if value is None or value == "":
        return None
    if value.strip() == "":
        raise InputError(f"{line}행 {column}: 공백만 있는 선택 값은 사용할 수 없습니다")
    return value


def _required_area(value: str | None, column: str, line: int) -> str:
    """collect.py::_area와 같은 면적 정규화 및 정밀도 검사."""
    text = _required_text(value, column, line)
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise InputError(f"{line}행 {column}: 소수가 아닙니다: {text!r}") from None
    if not number.is_finite():
        raise InputError(f"{line}행 {column}: 유한한 소수가 아닙니다: {text!r}")
    try:
        with localcontext(DefaultContext):
            normalized = number.quantize(AREA_SCALE, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        raise InputError(f"{line}행 {column}: numeric(9,4) 정밀도를 벗어납니다: {text!r}") from None
    if number != normalized:
        raise InputError(f"{line}행 {column}: 소수 넷째 자리를 초과합니다: {text!r}")
    return str(normalized)


def _optional_area(value: str | None, column: str, line: int) -> str | None:
    if value is None or value == "":
        return None
    return _required_area(value, column, line)


def _optional_integer(value: str | None, column: str, line: int) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        raise InputError(f"{line}행 {column}: 정수가 아닙니다: {value!r}") from None


def _source_decimal_places(value: str | None) -> int:
    """정규화 전 CSV 수의 소수 자릿수. 빈 선택 값은 0자리."""
    if value is None or value == "":
        return 0
    return max(0, -Decimal(value).as_tuple().exponent)


def _read_csv(path: pathlib.Path, expected_columns: tuple[str, ...]):
    try:
        source = path.open(encoding="utf-8-sig", newline="")
    except OSError as error:
        raise InputError(f"입력 파일을 열 수 없습니다: {path}: {error}") from error

    with source:
        reader = csv.DictReader(source)
        actual_columns = tuple(reader.fieldnames or ())
        if actual_columns != expected_columns:
            raise InputError(
                f"{path.name}: CSV 헤더가 다릅니다\n"
                f"기대: {expected_columns}\n"
                f"실제: {actual_columns}"
            )

        for line, row in enumerate(reader, start=2):
            if None in row:
                raise InputError(f"{path.name} {line}행: 헤더보다 값이 많습니다")
            if any(value is None for value in row.values()):
                raise InputError(f"{path.name} {line}행: 헤더보다 값이 적습니다")
            yield line, row


def load_crosswalk(path: pathlib.Path = CROSSWALK_PATH) -> list[ComplexRow]:
    """단지 대응표 로드. 선택 값의 빈 문자열은 None으로 변환."""
    rows: list[ComplexRow] = []
    for line, source in _read_csv(path, CROSSWALK_COLUMNS):
        rows.append(
            {
                "sgg_cd": _required_text(source["sgg_cd"], "sgg_cd", line),
                "apt_seq": _required_text(source["apt_seq"], "apt_seq", line),
                "apt_nm": _required_text(source["apt_nm"], "apt_nm", line),
                "road_key": _optional_text(source["road_key"], "road_key", line),
                "kapt_code": _optional_text(source["kapt_code"], "kapt_code", line),
                "kapt_key": _optional_text(source["kapt_key"], "kapt_key", line),
                "match_source": _optional_text(
                    source["match_source"], "match_source", line
                ),
            }
        )
    return rows


def load_areas(path: pathlib.Path = AREA_PATH) -> list[ComplexAreaRow]:
    """단지별 면적 로드. float를 거치지 않고 numeric(9,4) 문자열로 정규화."""
    rows: list[ComplexAreaRow] = []
    for line, source in _read_csv(path, AREA_COLUMNS):
        rows.append(
            {
                "payload": {
                    "apt_seq": _required_text(source["apt_seq"], "apt_seq", line),
                    "exclu_use_ar": _required_area(
                        source["exclu_use_ar"], "exclu_use_ar", line
                    ),
                    "area_status": _required_text(
                        source["area_status"], "area_status", line
                    ),
                    "kapt_area": _optional_area(
                        source["kapt_area"], "kapt_area", line
                    ),
                    "households": _optional_integer(
                        source["households"], "households", line
                    ),
                    "group_id": _optional_text(source["group_id"], "group_id", line),
                    "group_label": _optional_integer(
                        source["group_label"], "group_label", line
                    ),
                    "group_areas": _optional_text(
                        source["group_areas"], "group_areas", line
                    ),
                },
                # 사전 검사 전용. complex_area payload에 넣지 않음
                "validation": {
                    "sgg_cd": _required_text(source["sgg_cd"], "sgg_cd", line),
                    "apt_nm": _required_text(source["apt_nm"], "apt_nm", line),
                    "max_decimal_places": max(
                        _source_decimal_places(source["exclu_use_ar"]),
                        _source_decimal_places(source["kapt_area"]),
                    ),
                },
            }
        )
    return rows


def _duplicate_row_count(rows, key) -> int:
    seen = set()
    duplicates = 0
    for row in rows:
        value = key(row)
        if value in seen:
            duplicates += 1
        else:
            seen.add(value)
    return duplicates


def input_pk_duplicates(
    crosswalk: list[ComplexRow], areas: list[ComplexAreaRow]
) -> tuple[int, int]:
    """두 입력의 PK 기준 중복 행 수 계산."""
    crosswalk_duplicates = _duplicate_row_count(
        crosswalk, lambda row: row["apt_seq"]
    )
    area_duplicates = _duplicate_row_count(
        areas,
        lambda row: (
            row["payload"]["apt_seq"],
            row["payload"]["exclu_use_ar"],
        ),
    )
    return crosswalk_duplicates, area_duplicates


def load_inputs(
    crosswalk_path: pathlib.Path = CROSSWALK_PATH,
    area_path: pathlib.Path = AREA_PATH,
) -> tuple[list[ComplexRow], list[ComplexAreaRow]]:
    """적재의 유일한 두 입력 파일 로드 및 PK 유일성 확인."""
    crosswalk = load_crosswalk(crosswalk_path)
    areas = load_areas(area_path)
    crosswalk_duplicates, area_duplicates = input_pk_duplicates(crosswalk, areas)
    if crosswalk_duplicates or area_duplicates:
        raise InputError(
            "입력 PK 중복 행이 있습니다: "
            f"crosswalk {crosswalk_duplicates:,} / area {area_duplicates:,}"
        )
    return crosswalk, areas


def preflight_counts(
    crosswalk: list[ComplexRow], areas: list[ComplexAreaRow]
) -> tuple[dict[str, int], int]:
    """입력 두 파일만 교차 검사한다. DB에 쓰기 전에 모두 계산한다."""
    by_seq = {row["apt_seq"]: row for row in crosswalk}
    area_seqs = {row["payload"]["apt_seq"] for row in areas}
    groups: dict[tuple[str, int | None], set[str | None]] = {}
    for row in areas:
        payload = row["payload"]
        key = (payload["apt_seq"], payload["group_label"])
        groups.setdefault(key, set()).add(payload["group_id"])
    conflicting = {key for key, group_ids in groups.items() if len(group_ids) > 1}

    counts = {
        "label_collision": sum(
            (row["payload"]["apt_seq"], row["payload"]["group_label"])
            in conflicting
            for row in areas
        ),
        "orphan_area": sum(row["payload"]["apt_seq"] not in by_seq for row in areas),
        "complex_without_area": sum(row["apt_seq"] not in area_seqs for row in crosswalk),
        "shared_field_mismatch": sum(
            (
                row["validation"]["sgg_cd"]
                != by_seq[row["payload"]["apt_seq"]]["sgg_cd"]
                or row["validation"]["apt_nm"]
                != by_seq[row["payload"]["apt_seq"]]["apt_nm"]
            )
            for row in areas
            if row["payload"]["apt_seq"] in by_seq
        ),
        "nonpositive": sum(
            Decimal(row["payload"]["exclu_use_ar"]) <= 0
            or (
                row["payload"]["kapt_area"] is not None
                and Decimal(row["payload"]["kapt_area"]) <= 0
            )
            or (
                row["payload"]["households"] is not None
                and row["payload"]["households"] <= 0
            )
            or (
                row["payload"]["group_label"] is not None
                and row["payload"]["group_label"] <= 0
            )
            for row in areas
        ),
    }
    max_decimal_places = max(
        (row["validation"]["max_decimal_places"] for row in areas), default=0
    )
    return counts, max_decimal_places


def fetch_rows(
    sb_url: str, sb_key: str, table: str, columns: tuple[str, ...], order: str
) -> list[dict]:
    """지정한 DB 칸을 PK 순서로 전량 읽는다. JSON 소수는 Decimal로 받는다."""
    db_count = count_rows(sb_url, sb_key, table)
    rows: list[dict] = []
    offset = 0
    while True:
        path = (
            f"{table}?select={','.join(columns)}&order={order}"
            f"&limit={collect.UPSERT_CHUNK}&offset={offset}"
        )
        request = Request(
            f"{sb_url}/rest/v1/{path}",
            headers={"apikey": sb_key, "Authorization": f"Bearer {sb_key}"},
        )
        try:
            with urlopen(request, timeout=collect.TIMEOUT_SEC) as response:
                raw = response.read()
        except HTTPError as error:
            raise InputError(f"{table} 조회 HTTP {error.code}") from error
        except (URLError, TimeoutError, OSError) as error:
            raise InputError(f"{table} 조회 연결 실패: {error}") from error
        try:
            page = json.loads(raw, parse_float=Decimal)
        except (ValueError, UnicodeDecodeError) as error:
            raise InputError(f"{table} 조회 응답이 올바른 JSON이 아닙니다") from error
        if not isinstance(page, list) or len(page) > collect.UPSERT_CHUNK:
            raise InputError(f"{table} 조회 응답의 페이지 형태가 잘못됐습니다")
        if any(
            not isinstance(row, dict) or any(column not in row for column in columns)
            for row in page
        ):
            raise InputError(f"{table} 조회 응답에 필요한 칸이 없습니다")
        rows.extend(page)
        if len(page) < collect.UPSERT_CHUNK:
            break
        offset += len(page)
    if len(rows) != db_count:
        raise InputError(
            f"{table} 조회가 잘렸습니다: DB 총수 {db_count:,} / 읽은 수 {len(rows):,}"
        )
    return rows


def fetch_existing_apt_seqs(sb_url: str, sb_key: str) -> set[str]:
    """complex의 PK만 읽고 신규 단지 집합 차의 기준으로 쓴다."""
    rows = fetch_rows(sb_url, sb_key, "complex", ("apt_seq",), "apt_seq")
    if any(not isinstance(row["apt_seq"], str) for row in rows):
        raise InputError("complex PK 조회 응답의 apt_seq가 문자열이 아닙니다")
    return {row["apt_seq"] for row in rows}


def count_rows(sb_url: str, sb_key: str, table: str) -> int:
    """collect.py::_sb_count와 같이 HEAD + count=exact로 DB 실제 행 수를 읽는다."""
    request = Request(
        f"{sb_url}/rest/v1/{table}?select=apt_seq",
        headers={
            "apikey": sb_key,
            "Authorization": f"Bearer {sb_key}",
            "Prefer": "count=exact",
        },
        method="HEAD",
    )
    try:
        with urlopen(request, timeout=collect.TIMEOUT_SEC) as response:
            header = response.headers.get("Content-Range", "")
    except HTTPError as error:
        raise InputError(f"{table} 행 수 조회 HTTP {error.code}") from error
    except (URLError, TimeoutError, OSError) as error:
        raise InputError(f"{table} 행 수 조회 연결 실패: {error}") from error
    total = header.rsplit("/", 1)[-1] if "/" in header else ""
    if not total.isdigit():
        raise InputError(f"{table} Content-Range를 읽지 못했습니다: {header!r}")
    return int(total)


def road_key_collisions(sb_url: str, sb_key: str) -> set[str]:
    """DB에서 한 road_key를 여러 apt_seq가 공유하는 충돌군을 구한다."""
    rows = fetch_rows(
        sb_url, sb_key, "complex", ("apt_seq", "road_key"), "apt_seq"
    )
    by_road: dict[str, set[str]] = {}
    for row in rows:
        road_key = row["road_key"]
        if road_key is not None:
            by_road.setdefault(road_key, set()).add(row["apt_seq"])
    return {road_key for road_key, apt_seqs in by_road.items() if len(apt_seqs) > 1}


def upsert_rows(
    table: str,
    rows: list[dict],
    key_fields: tuple[str, ...],
    sb_url: str,
    sb_key: str,
) -> None:
    """collect.py::upsert와 같은 PostgREST 요청을 500행씩 보낸다."""
    path = f"{table}?on_conflict={','.join(key_fields)}"
    for start in range(0, len(rows), collect.UPSERT_CHUNK):
        chunk = rows[start:start + collect.UPSERT_CHUNK]
        collect._sb_request(
            "POST", path, sb_url, sb_key, body=chunk,
            prefer="resolution=merge-duplicates,return=minimal",
        )


def _parse_timestamptz(value, column: str) -> datetime:
    """PostgREST timestamptz의 시간대 포함 datetime 파싱."""
    if not isinstance(value, str):
        raise InputError(f"{column}이 문자열이 아닙니다")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise InputError(f"{column} 형식 오류: {value!r}") from None
    if stamp.tzinfo is None:
        raise InputError(f"{column}에 시간대가 없습니다")
    return stamp


def verify_last_seen_at(
    sb_url: str,
    sb_key: str,
    table: str,
    started_at: datetime,
    current_keys: set | None = None,
) -> None:
    """가장 이른 last_seen_at의 실행 시작 시각 이상 여부 확인."""
    rows = collect._sb_request(
        "GET", f"{table}?select=last_seen_at&order=last_seen_at.asc&limit=1",
        sb_url, sb_key,
    )
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise InputError(f"{table} last_seen_at 조회 결과가 한 행이 아닙니다")
    last_seen = _parse_timestamptz(rows[0].get("last_seen_at"), "last_seen_at")
    if last_seen < started_at:
        if current_keys is not None:
            # DB 에만 있는 과거 행은 이번 upsert 대상이 아니므로 현재 입력 키만 확인
            if not current_keys:
                return
            columns = (
                ("apt_seq", "last_seen_at") if table == "complex"
                else ("apt_seq", "exclu_use_ar", "last_seen_at")
            )
            order = "apt_seq" if table == "complex" else "apt_seq,exclu_use_ar"
            all_rows = fetch_rows(sb_url, sb_key, table, columns, order)
            current_rows = [
                row for row in all_rows
                if (
                    row["apt_seq"] if table == "complex"
                    else (row["apt_seq"], _digest_value("exclu_use_ar", row["exclu_use_ar"]))
                ) in current_keys
            ]
            if len(current_rows) != len(current_keys):
                raise InputError(
                    f"{table} 이번 입력 행의 last_seen_at 조회가 누락됐습니다: "
                    f"입력 {len(current_keys):,} / 조회 {len(current_rows):,}"
                )
            last_seen = min(
                _parse_timestamptz(row["last_seen_at"], "last_seen_at")
                for row in current_rows
            )
            if last_seen >= started_at:
                return
        raise InputError(
            f"{table} last_seen_at이 갱신되지 않았습니다: "
            f"DB {last_seen.isoformat()} / 시작 {started_at.isoformat()}"
        )


def _digest_value(column: str, value) -> str:
    if value is None:
        return ""
    if column == "first_seen_at":
        stamp = _parse_timestamptz(value, "first_seen_at")
        return stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")
    if column in ("exclu_use_ar", "kapt_area"):
        if isinstance(value, float):
            raise InputError(f"다이제스트 {column}이 float를 경유했습니다")
        try:
            return format(Decimal(value), AREA_TEXT_FORMAT)
        except (InvalidOperation, TypeError):
            raise InputError(f"다이제스트 {column} 값이 numeric이 아닙니다") from None
    if column in ("households", "group_label"):
        if not isinstance(value, int) or isinstance(value, bool):
            raise InputError(f"다이제스트 {column} 값이 integer가 아닙니다")
        return str(value)
    if not isinstance(value, str):
        raise InputError(f"다이제스트 {column} 값이 text가 아닙니다")
    return value


def digest_table(sb_url: str, sb_key: str, table: str) -> str:
    """적재 후 DB 행을 PK 순서로 읽어 명세 4-3-1의 md5를 계산한다."""
    columns = DIGEST_COLUMNS[table]
    rows = fetch_rows(sb_url, sb_key, table, columns, DIGEST_ORDER[table])
    digest = hashlib.md5()
    for index, row in enumerate(rows):
        if index:
            digest.update(b"\x1e")
        serialized = "\x1f".join(
            _digest_value(column, row[column]) for column in columns
        )
        digest.update(serialized.encode("utf-8"))
    return digest.hexdigest()


def main() -> None:
    # 전 행이 실행 시작 시각 하나를 공유 (§4-3-2, collect.py:838 선례)
    # 첫 적재에서는 first_seen_at(DB now())보다 앞설 수 있음 (실측 07:26:31 < 07:26:33)
    started_at = datetime.now(timezone.utc)
    started_at_text = started_at.isoformat()
    try:
        crosswalk, areas = load_inputs()
        counts, max_decimal_places = preflight_counts(crosswalk, areas)
    except InputError as error:
        raise SystemExit(f"[중단] {error}") from None

    print(f"입력 행 수        crosswalk {len(crosswalk):,} / area {len(areas):,}")
    print(
        "사전 검사 게이트  "
        f"라벨충돌 {counts['label_collision']:,} / "
        f"고아면적행 {counts['orphan_area']:,} / "
        f"단지0면적 {counts['complex_without_area']:,} / "
        f"공통칸불일치 {counts['shared_field_mismatch']:,} / "
        f"양성위반 {counts['nonpositive']:,}"
    )
    print(f"사전 검사 정보    관측 최대 소수 자릿수 {max_decimal_places}")
    if any(counts.values()):
        raise SystemExit("[중단] 사전 검사 게이트가 0이 아닙니다")

    sb_url, sb_key = collect._sb_config()
    try:
        existing = fetch_existing_apt_seqs(sb_url, sb_key)
        before_collisions = road_key_collisions(sb_url, sb_key)
    except InputError as error:
        raise SystemExit(f"[중단] {error}") from None
    new_apt_seqs = {row["apt_seq"] for row in crosswalk} - existing
    print(f"신규 단지 수      {len(new_apt_seqs):,}")

    complex_rows = [{**row, "last_seen_at": started_at_text} for row in crosswalk]
    area_rows = [
        {**row["payload"], "last_seen_at": started_at_text} for row in areas
    ]
    try:
        upsert_rows(
            "complex", complex_rows, ("apt_seq",), sb_url, sb_key
        )
        upsert_rows(
            "complex_area", area_rows, ("apt_seq", "exclu_use_ar"), sb_url, sb_key
        )
        complex_count = count_rows(sb_url, sb_key, "complex")
        area_count = count_rows(sb_url, sb_key, "complex_area")
        for table, db_count, input_count in (
            ("complex", complex_count, len(crosswalk)),
            ("complex_area", area_count, len(areas)),
        ):
            if db_count < input_count:
                raise InputError(
                    f"{table} 적재된 행이 입력보다 적습니다: "
                    f"DB {db_count:,} / 입력 {input_count:,}"
                )
            if db_count > input_count:
                print(f"DB 에만 있는 행   {table} {db_count - input_count:,}")
        print(f"적재 결과         complex {complex_count:,} / complex_area {area_count:,}")
        complex_keys = (
            {row["apt_seq"] for row in crosswalk}
            if complex_count > len(crosswalk) else None
        )
        area_keys = (
            {
                (row["payload"]["apt_seq"], row["payload"]["exclu_use_ar"])
                for row in areas
            }
            if area_count > len(areas) else None
        )
        verify_last_seen_at(sb_url, sb_key, "complex", started_at, complex_keys)
        verify_last_seen_at(sb_url, sb_key, "complex_area", started_at, area_keys)
        after_collisions = road_key_collisions(sb_url, sb_key)
        complex_digest = digest_table(sb_url, sb_key, "complex")
        area_digest = digest_table(sb_url, sb_key, "complex_area")
    except (InputError, collect.SupabaseError) as error:
        raise SystemExit(f"[중단] {error}") from None

    new_collisions = after_collisions - before_collisions
    baseline = " (첫 적재 기준선)" if not existing else ""
    print(f"road_key 감사     C1 - C0 {len(new_collisions):,}{baseline}")
    for road_key in sorted(new_collisions):
        print(f"  {road_key}")
    print(f"멱등 다이제스트   complex {complex_count:,}행 {complex_digest}")
    print(f"멱등 다이제스트   complex_area {area_count:,}행 {area_digest}")


if __name__ == "__main__":
    main()
