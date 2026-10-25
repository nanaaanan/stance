"""두 원본에서 서울 아파트 스냅샷과 면적별 호 수를 만든다.

실행: python3 scripts/registry.py --reb 원본.csv --price 원본.csv
원본 경로는 필수다. 검사 실패 시 기존 출력 두 개를 바꾸지 않는다.
"""

import argparse
import collections
import csv
import io
import pathlib
import sys
from decimal import Decimal

if __package__:
    from . import collect
else:
    import collect


DATA = pathlib.Path(__file__).resolve().parents[1] / "data"
SNAPSHOT = DATA / "reb-complex-seoul-apt.csv"
AREA = DATA / "price-area-seoul-apt.csv"
SEOUL = "11"
APARTMENT_KIND = "1"
REB_REQUIRED = ("단지고유번호", "단지종류")
PRICE_REQUIRED = ("법정동코드", "단지코드", "동코드", "호코드", "전용면적")
AREA_COLUMNS = ("reb_complex_id", "exclu_use_ar", "unit_count")


class RegistryError(ValueError):
    """원본 계약을 만족하지 못했을 때 발생한다."""


def fail(source, line, column, value, reason):
    raise RegistryError(f"{source} {line}행 {column}={value!r}: {reason}")


def header_indices(header, required, source):
    counts = collections.Counter(header)
    missing = [name for name in required if not counts[name]]
    duplicates = [name for name, count in counts.items() if count > 1]
    if missing or duplicates:
        fail(source, 1, "헤더", {"빠짐": missing, "겹침": duplicates},
             "필수 칸 누락 또는 같은 이름 칸 중복")
    return {name: index for index, name in enumerate(header)}


def ascii_digits(value, length):
    return len(value) == length and value.isascii() and value.isdigit()


def reb_record(raw, line):
    # cp949 원본은 쉼표가 든 값만 따옴표로 감싼다. split(',')은 쓸 수 없다.
    try:
        text = raw.decode("cp949")
    except UnicodeDecodeError:
        fail("부동산원", line, "인코딩", "cp949", "엄격 해독 실패")
    try:
        records = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except csv.Error:
        fail("부동산원", line, "CSV", "레코드 경계", "완결된 한 레코드가 아님")
    # 칸 수만 맞아도 따옴표 안의 줄바꿈으로 복사 단위가 깨질 수 있다.
    if len(records) != 1 or any("\r" in v or "\n" in v for v in records[0]):
        fail("부동산원", line, "CSV", "레코드 경계", "물리 줄 하나가 레코드 하나여야 함")
    return records[0]


def read_reb(path, summary):
    seoul = {}
    snapshot = []
    with pathlib.Path(path).open("rb") as source:
        raw_header = source.readline()
        header = reb_record(raw_header, 1) if raw_header else []
        # 이름 대조를 하려면 두 원본의 이름 칸도 있어야 한다.
        indices = header_indices(header, (*REB_REQUIRED, "단지명_공시가격"), "부동산원")
        snapshot.append(raw_header)
        for line, raw in enumerate(source, start=2):
            row = reb_record(raw, line)
            if len(row) != len(header):
                fail("부동산원", line, "칸 수", len(row), f"기대 {len(header)}")
            summary["reb_rows"] += 1
            # 원본 ID 뒤 공백은 조회할 때만 뗀다. 복사 바이트는 건드리지 않는다.
            complex_id = row[indices["단지고유번호"]].strip()
            kind = row[indices["단지종류"]]
            if not ascii_digits(complex_id, 14):
                fail("부동산원", line, "단지고유번호", complex_id, "ASCII 숫자 14자리 필요")
            if kind != complex_id[5]:
                fail("부동산원", line, "단지종류", kind, f"단지고유번호 {complex_id}의 6번째 자리와 다름")
            if not complex_id.startswith(SEOUL):
                continue
            summary["reb_seoul_rows"] += 1
            key = (complex_id[:5], complex_id[6:])
            if key in seoul:
                fail("부동산원", line, "단지 키", key, "서울 조회 키 중복")
            seoul[key] = (complex_id, kind, row[indices["단지명_공시가격"]])
            if kind == APARTMENT_KIND:
                snapshot.append(raw)
                summary["snapshot_rows"] += 1
    return seoul, snapshot


def read_price(path, seoul, summary):
    units = {}
    counts = collections.Counter()
    missing = set()
    linked = set()
    mismatched = set()
    with pathlib.Path(path).open(encoding="utf-8", newline="") as source:
        reader = csv.reader(source, strict=True)
        try:
            header = next(reader, [])
            indices = header_indices(header, (*PRICE_REQUIRED, "단지명"), "공시가격")
            for row in reader:
                line = reader.line_num
                if len(row) != len(header):
                    fail("공시가격", line, "칸 수", len(row), f"기대 {len(header)}")
                summary["price_rows"] += 1
                district = row[indices["법정동코드"]]
                if not ascii_digits(district, 10):
                    fail("공시가격", line, "법정동코드", district, "ASCII 숫자 10자리 필요")
                if not district.startswith(SEOUL):
                    continue
                summary["price_seoul_rows"] += 1
                code = row[indices["단지코드"]]
                if not (code.isascii() and code.isdigit() and 1 <= len(code) <= 8):
                    fail("공시가격", line, "단지코드", code, "ASCII 숫자 1자리부터 8자리까지 필요")
                # 공시가격에는 단지종류가 없다. 0 채움 없는 코드를 맞춰 원본 사전에서 찾는다.
                key = (district[:5], code.zfill(8))
                if key not in seoul:
                    missing.add(key)
                    continue
                complex_id, kind, name = seoul[key]
                if kind != APARTMENT_KIND:
                    continue
                summary["price_snapshot_rows"] += 1
                linked.add(complex_id)
                if row[indices["단지명"]] != name:
                    mismatched.add(complex_id)
                building, unit = row[indices["동코드"]], row[indices["호코드"]]
                for column, value in (("동코드", building), ("호코드", unit)):
                    if not value.strip():
                        fail("공시가격", line, column, value, f"빈 값, 단지고유번호 {complex_id}")
                raw_area = row[indices["전용면적"]]
                # 다세대에는 4자리보다 정밀한 값이 있다. 출력 대상 아파트만 검사한다.
                try:
                    area = collect._area(raw_area)
                except (ValueError, collect.ApiError) as exc:
                    fail("공시가격", line, "전용면적", raw_area,
                         f"단지고유번호 {complex_id}, {exc}")
                unit_key = (complex_id, building, unit)
                if unit_key in units:
                    if units[unit_key] != area:
                        fail("공시가격", line, "전용면적", area,
                             f"호 키 {unit_key}, 기존 면적 {units[unit_key]}와 다름")
                    summary["duplicate_unit_rows"] += 1
                else:
                    units[unit_key] = area
                    counts[complex_id, area] += 1
        except UnicodeDecodeError:
            fail("공시가격", reader.line_num + 1, "인코딩", "utf-8", "엄격 해독 실패")
        except csv.Error as exc:
            fail("공시가격", reader.line_num, "CSV", "레코드", str(exc))
    summary["price_missing_complexes"] = len(missing)
    summary["snapshot_without_price"] = summary["snapshot_rows"] - len(linked)
    summary["name_mismatch_complexes"] = len(mismatched)
    summary["area_rows"] = len(counts)
    summary["area_complexes"] = len(linked)
    summary["unit_count_sum"] = sum(counts.values())
    return counts


def build(reb_path, price_path, snapshot_path=SNAPSHOT, area_path=AREA):
    """모든 검사를 끝낸 뒤 출력한다. 쓰기 중 디스크 오류의 복구는 재실행이다."""
    inputs = {pathlib.Path(p).resolve() for p in (reb_path, price_path)}
    outputs = [pathlib.Path(p) for p in (snapshot_path, area_path)]
    if outputs[0].resolve() == outputs[1].resolve() or any(p.resolve() in inputs for p in outputs):
        raise RegistryError("출력 경로는 서로 달라야 하며 원본 경로와 같을 수 없습니다.")
    summary = collections.Counter()
    seoul, snapshot = read_reb(reb_path, summary)
    counts = read_price(price_path, seoul, summary)
    # Decimal 값이 같은 0과 -0도 입력 순서와 무관하게 정렬한다.
    keys = sorted(counts, key=lambda key: (key[0], Decimal(key[1]), key[1]))
    with outputs[0].open("wb") as target:
        target.writelines(snapshot)
    with outputs[1].open("w", encoding="utf-8", newline="") as target:
        writer = csv.writer(target, lineterminator="\n")
        writer.writerow(AREA_COLUMNS)
        writer.writerows((complex_id, area, counts[complex_id, area]) for complex_id, area in keys)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reb", required=True, type=pathlib.Path)
    parser.add_argument("--price", required=True, type=pathlib.Path)
    args = parser.parse_args(argv)
    try:
        summary = build(args.reb, args.price)
    except (RegistryError, OSError) as exc:
        print(f"명부 생성 실패: {exc}", file=sys.stderr)
        return 1
    for name in ("reb_rows", "reb_seoul_rows", "snapshot_rows", "price_rows",
                 "price_seoul_rows", "price_snapshot_rows", "price_missing_complexes",
                 "snapshot_without_price", "name_mismatch_complexes", "duplicate_unit_rows",
                 "area_rows", "area_complexes", "unit_count_sum"):
        print(f"{name}: {summary[name]}")
    print(f"snapshot: {SNAPSHOT}")
    print(f"area: {AREA}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
