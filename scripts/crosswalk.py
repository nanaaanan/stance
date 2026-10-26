"""실거래 단지와 K-apt 관리정보의 도로명 대응표와 실패표 산출기.

파일명에는 구 접미사를 붙이지 않는다. 서울 전체를 한 파일에 담아 M4 입력으로 쓴다.

실행
    set -a; source .env; set +a
    python3 scripts/crosswalk.py
"""

import argparse
import collections
import csv
import importlib.util
import json
import pathlib
import sys
from decimal import Decimal
from datetime import date
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
OUT_CROSSWALK = ROOT / "data" / "complex-crosswalk.csv"
OUT_FAILURES = ROOT / "data" / "matching-failures.csv"
DISTRICTS_PATH = ROOT / "config" / "districts.json"

SELECT = ",".join([
    "id", "apt_seq", "apt_nm", "sgg_cd", "deal_ym",
    "road_nm", "road_nm_bonbun", "road_nm_bubun",
    "deal_date", "exclu_use_ar", "floor", "trade_count", "build_year",
])
PAGE_SIZE = 1000
TIMEOUT_SEC = 60

CROSSWALK_COLUMNS = [
    "sgg_cd", "apt_seq", "apt_nm", "road_key",
    "kapt_code", "kapt_key", "match_source",
    "reb_complex_id", "reb_match_source",
]
FAILURE_COLUMNS = ["sgg_cd", "apt_seq", "apt_nm", "road_key", "fail_reason", "fail_layer"]
FAILURE_LAYER_ORDER = {"단지 명부": 0, "관리정보": 1, "면적": 2}


def _load_local_matching():
    """실행 방식과 무관하게 레포의 matching.py를 로드."""
    path = HERE / "matching.py"
    name = "_stance_crosswalk_matching"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"{path.name} 을 로드하지 못했습니다.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


matching = _load_local_matching()


def load_districts():
    with open(DISTRICTS_PATH, encoding="utf-8") as f:
        return sorted(
            ((item["code"], item["name"]) for item in json.load(f)["districts"]),
            key=lambda item: item[0],
        )


def fetch_district(sgg_cd, url, api_key):
    """한 구의 전 기간 current 거래를 id 순으로 전부 조회."""
    # id는 유일한 정렬 키다. apt_seq 같은 비유일 키면 offset 페이지 경계가 흔들린다.
    # parse_float=Decimal은 numeric 면적을 float 근사값으로 바꾸지 않기 위해 필요하다.
    rows, offset = [], 0
    path = (
        f"trade?select={SELECT}&sgg_cd=eq.{sgg_cd}&is_current=is.true"
        f"&order=id&offset={offset}&limit={PAGE_SIZE}"
    )
    while True:
        request = Request(
            f"{url}/rest/v1/{path}",
            headers={"apikey": api_key, "Authorization": f"Bearer {api_key}"},
        )
        try:
            with urlopen(request, timeout=TIMEOUT_SEC) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            raise RuntimeError(f"Supabase HTTP {exc.code}") from None
        except (URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"Supabase 조회 실패: {exc}") from None
        # PostgREST numeric 값을 float로 파싱하면 정밀도가 바뀌어 후속 키가 갈라질 수 있다.
        chunk = json.loads(body, parse_float=Decimal)
        rows.extend(chunk)
        if len(chunk) < PAGE_SIZE:
            return rows
        offset += PAGE_SIZE
        path = (
            f"trade?select={SELECT}&sgg_cd=eq.{sgg_cd}&is_current=is.true"
            f"&order=id&offset={offset}&limit={PAGE_SIZE}"
        )


def representative(rows):
    """matching.sort_key 뒤 apt_nm 사전순을 타이브레이크로 쓰는 대표행."""
    return min(rows, key=lambda row: (matching.sort_key(row), str(row.get("apt_nm") or "")))


def candidate_map(index, gu_names, key):
    """구 파티션 후보를 kapt_code 기준으로 합친다."""
    candidates = {}
    if not key:
        return candidates
    for gu_name in gu_names:
        local = index.lookup(gu_name, key) or ()
        if len(local) > 1:
            # 같은 구 안의 두 행은 같은 코드라도 모호 후보로 남긴다.
            for number, candidate in enumerate(local):
                candidates[(gu_name, number)] = candidate
        else:
            for kapt_code, kapt_name in local:
                if not kapt_code:
                    continue
                # 같은 코드가 여러 구 파티션에 나오면 하나의 후보로 센다.
                candidates.setdefault(kapt_code, (kapt_code, kapt_name))
    return candidates


def match_one(indexes, active_variants, gu_names, key):
    """원문, 단독 변형, 최종 조합 순서로 후보를 시도."""
    attempts = [((), "원문 일치")]
    attempts.extend(((variant,), f"변형 {variant} 적용 후 일치")
                     for variant in active_variants)
    if len(active_variants) > 1:
        label = "+".join(str(variant) for variant in active_variants)
        attempts.append((tuple(active_variants), f"변형 {label} 적용 후 일치"))

    saw_ambiguous = False
    for variants, source in attempts:
        candidates = candidate_map(indexes[variants], gu_names, key)
        if len(candidates) == 1:
            kapt_code, kapt_name = next(iter(candidates.values()))
            return {
                "kapt_code": kapt_code,
                "kapt_key": key,
                "match_source": source,
                "fail_reason": "",
            }
        if len(candidates) > 1:
            saw_ambiguous = True

    return {
        "kapt_code": "",
        "kapt_key": "",
        "match_source": "",
        "fail_reason": "후보 2개 이상(모호)" if saw_ambiguous else "K-apt 마스터에 없음",
    }


def build_rows(trade_rows, indexes, active_variants):
    """거래 전 기간을 apt_seq 단위로 묶어 두 CSV의 행을 만든다."""
    grouped = collections.defaultdict(list)
    for row in trade_rows:
        grouped[row["apt_seq"]].append(row)

    crosswalk, failures = [], []
    for apt_seq, rows in grouped.items():
        head = representative(rows)
        road_key = matching.road_key(
            head.get("road_nm"), head.get("road_nm_bonbun"), head.get("road_nm_bubun")
        )
        counts = collections.Counter(str(row.get("sgg_cd") or "") for row in rows)
        sgg_cd = min(counts, key=lambda code: (-counts[code], code))
        result = match_one(indexes, active_variants,
                           {str(row.get("_district_name") or "") for row in rows}, road_key)
        base = {
            "sgg_cd": sgg_cd,
            "apt_seq": apt_seq,
            "apt_nm": str(head.get("apt_nm") or ""),
            "road_key": road_key,
        }
        if result["kapt_code"]:
            crosswalk.append({**base, **{name: result[name] for name in (
                "kapt_code", "kapt_key", "match_source")}})
        else:
            crosswalk.append({**base, "kapt_code": "", "kapt_key": "", "match_source": ""})
            failures.append({**base, "fail_reason": (
                "도로명 키 생성 불가" if not road_key else result["fail_reason"]
            )})

    crosswalk.sort(key=lambda row: (row["sgg_cd"], row["apt_seq"]))
    failures.sort(key=lambda row: (row["sgg_cd"], row["apt_seq"]))
    return crosswalk, failures


def write_csv(path, columns, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def add_reb_layer(crosswalk, failures, trade_rows, reb_index):
    """기존 대응표에 명부 판정과 층별 실패 행 추가."""
    grouped = collections.defaultdict(list)
    for row in trade_rows:
        grouped[row["apt_seq"]].append(row)
    results = {}
    output = []
    layered_failures = [{**row, "fail_layer": "관리정보"} for row in failures]
    for row in crosswalk:
        rows = grouped[row["apt_seq"]]
        gu_names = {str(item.get("_district_name") or "") for item in rows}
        result = matching.match_reb_complex(reb_index, gu_names, rows, representative)
        results[row["apt_seq"]] = result
        output.append({**row, **{name: result[name] for name in (
            "reb_complex_id", "reb_match_source")}})
        if result["fail_reason"]:
            base = {name: row[name] for name in ("sgg_cd", "apt_seq", "apt_nm", "road_key")}
            layered_failures.append({**base, "fail_reason": result["fail_reason"],
                                     "fail_layer": "단지 명부"})
    layered_failures.sort(key=lambda row: (
        row["sgg_cd"], row["apt_seq"], FAILURE_LAYER_ORDER[row["fail_layer"]]))
    return output, layered_failures, results


class OtherRebIndex(matching.RebIndex):
    """스냅샷 번호 집합과 대조한 원본의 아파트 외 관측 사전."""

    def __init__(self, path, snapshot):
        districts = dict(load_districts())
        apartment_ids = set()
        self._rows = []
        self._indexes = {}
        try:
            with open(path, encoding="cp949", newline="") as f:
                reader = csv.DictReader(f)
                required = {"단지고유번호", "단지종류", "도로명주소"}
                if not required.issubset(reader.fieldnames or ()):
                    raise ValueError("원본 필수 헤더 없음")
                for row in reader:
                    complex_id = str(row.get("단지고유번호") or "").strip()
                    if not complex_id.startswith("11"):
                        continue
                    if (len(complex_id) != 14 or not complex_id.isascii()
                            or not complex_id.isdigit() or complex_id[:5] not in districts):
                        raise ValueError(f"원본 서울 단지고유번호 오류: {complex_id!r}")
                    if str(row.get("단지종류") or "").strip() == "1":
                        apartment_ids.add(complex_id)
                    else:
                        self._rows.append((districts[complex_id[:5]], complex_id,
                                           matching.reb_key(row.get("도로명주소"))))
        except (OSError, UnicodeDecodeError, csv.Error, ValueError) as exc:
            sys.exit(f"[중단] 명부 원본 읽기 실패: {exc}")
        snapshot_ids = set(snapshot.metadata)
        if apartment_ids != snapshot_ids:
            sys.exit("[중단] 원본-스냅샷 서울 아파트 번호 집합 불일치: "
                     f"원본만 {len(apartment_ids - snapshot_ids)}, "
                     f"스냅샷만 {len(snapshot_ids - apartment_ids)}")


def observe_reb(crosswalk, results, trade_rows, reb_index, other_index=None):
    """완성된 대응표와 후보에서 이름, 연도, 번호 공유 관측 산출."""
    grouped = collections.defaultdict(list)
    for row in trade_rows:
        grouped[row["apt_seq"]].append(row)
    stats = {
        "total": len(crosswalk), "matched": 0, "failures": collections.Counter(),
        "same_name": 0, "ambiguous_names": collections.Counter(),
        "year_differences": collections.Counter(), "year_pairs": [],
        "trade_year_missing": 0, "approval_date_missing": 0,
        "shared_ids": 0, "shared_apts": 0,
        "other_only": None if other_index is None else 0,
    }
    id_counts = collections.Counter()
    for row in crosswalk:
        apt_seq = row["apt_seq"]
        result = results[apt_seq]
        complex_id = row["reb_complex_id"]
        name = row["apt_nm"].strip()
        if not complex_id:
            reason = result["fail_reason"]
            stats["failures"][reason] += 1
            if len(result["candidates"]) > 1:
                same = sum(name == reb_index.metadata[candidate]["name"]
                           for candidate in result["candidates"])
                stats["ambiguous_names"][min(same, 2)] += 1
            if reason == "후보 없음" and other_index is not None:
                rows = grouped[apt_seq]
                gu_names = {str(item.get("_district_name") or "") for item in rows}
                other = matching.match_reb(other_index, gu_names, row["road_key"])
                stats["other_only"] += bool(other["candidates"])
            continue
        stats["matched"] += 1
        id_counts[complex_id] += 1
        metadata = reb_index.metadata[complex_id]
        stats["same_name"] += name == metadata["name"]
        head = representative(grouped[apt_seq])
        trade_year = head.get("build_year")
        if not trade_year:
            stats["trade_year_missing"] += 1
        try:
            approval_year = date.fromisoformat(metadata["approval_date"]).year
        except ValueError:
            approval_year = None
            stats["approval_date_missing"] += 1
        if not trade_year or approval_year is None:
            continue
        difference = int(trade_year) - approval_year
        stats["year_differences"][difference] += 1
        if difference:
            stats["year_pairs"].append((difference, apt_seq, row["apt_nm"],
                                        metadata["name"], int(trade_year), approval_year))
    stats["year_pairs"].sort(key=lambda item: (-abs(item[0]), item[1]))
    stats["shared_ids"] = sum(count > 1 for count in id_counts.values())
    stats["shared_apts"] = sum(count for count in id_counts.values() if count > 1)
    return stats


def reb_report_lines(stats, reb_index):
    """관측 수와 분모를 담은 표준 출력 줄 생성."""
    total, matched = stats["total"], stats["matched"]
    failures = stats["failures"]
    ambiguous = failures["후보 2개 이상(모호)"]
    year_total = sum(stats["year_differences"].values())
    lines = [
        f"단지 명부 층 (분모={total}): 붙음={matched}, 후보 2개 이상={ambiguous}, "
        f"후보 없음={failures['후보 없음']}, 키 없음={failures['도로명 키 생성 불가']}",
        f"명부 사전 (분모={reb_index.source_rows}): 적재={reb_index.source_rows}, "
        f"키 생성={reb_index.key_rows}, 중복 키={reb_index.duplicate_keys}, "
        f"중복 키 행={reb_index.duplicate_rows}",
        f"이름 글자 일치 (분모={matched}): 같음={stats['same_name']}, "
        f"다름={matched - stats['same_name']}",
        f"모호 단지의 같은 이름 후보 수 (분모={ambiguous}): "
        + ", ".join(f"{label}={stats['ambiguous_names'][number]}"
                    for number, label in ((0, "0개"), (1, "1개"), (2, "2개 이상"))),
        f"연도 관측 (분모={matched}): 양쪽 연도 있음={year_total}, "
        f"실거래 준공연도 없음={stats['trade_year_missing']}, "
        f"명부 사용승인일 없음={stats['approval_date_missing']}",
        f"연도 차이 분포 (실거래-명부, 분모={year_total}): "
        + ", ".join(f"{difference:+d}={count}"
                    for difference, count in sorted(stats["year_differences"].items())),
        f"연도 차이 0 아닌 쌍 전체 (분모={year_total}, {len(stats['year_pairs'])}쌍):",
    ]
    lines.extend(f"  {apt_seq} | {name} | {reb_name} | {trade_year} | {approval_year} | {diff:+d}"
                 for diff, apt_seq, name, reb_name, trade_year, approval_year in stats["year_pairs"])
    lines.append(f"N:1 (붙은 단지 분모={matched}): 공유 명부 번호={stats['shared_ids']}, "
                 f"그 안의 apt_seq={stats['shared_apts']}")
    other = ("미측정 (원본 경로 미지정)" if stats["other_only"] is None
             else str(stats["other_only"]))
    lines.append(f"아파트 외 종류에만 후보가 있는 단지 수: {other} "
                 f"(명부 후보 없음 분모={failures['후보 없음']}, 전체 분모={total})")
    return lines


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reb-raw", type=pathlib.Path)
    args = parser.parse_args()
    reb_index = matching.RebIndex()
    other_index = OtherRebIndex(args.reb_raw, reb_index) if args.reb_raw else None
    # matching이 규칙과 K-apt 사전의 유일한 소유자다. CSV는 이 호출에서 한 번만 읽는다.
    coverage = matching._load_local_coverage()

    url, api_key = coverage.sb_config()
    districts = load_districts()
    trade_rows = []
    district_names = dict(districts)
    for sgg_cd, district_name in districts:
        rows = fetch_district(sgg_cd, url, api_key)
        for row in rows:
            row["_district_name"] = district_names.get(str(row.get("sgg_cd") or sgg_cd), district_name)
        trade_rows.extend(rows)

    matching._validate_active_normalization_snapshot()
    source_rows = matching.KaptIndex.read_rows()
    active_variants = matching.ACTIVE_NORMALIZATION_VARIANTS
    variant_sets = {(): (), **{(variant,): (variant,) for variant in active_variants}}
    if len(active_variants) > 1:
        variant_sets[tuple(active_variants)] = tuple(active_variants)
    indexes = {variants: matching.KaptIndex.from_rows(source_rows, variants)
               for variants in variant_sets.values()}
    crosswalk, failures = build_rows(trade_rows, indexes, active_variants)
    crosswalk, failures, reb_results = add_reb_layer(crosswalk, failures, trade_rows, reb_index)
    stats = observe_reb(crosswalk, reb_results, trade_rows, reb_index, other_index)
    report_lines = reb_report_lines(stats, reb_index)
    write_csv(OUT_CROSSWALK, CROSSWALK_COLUMNS, crosswalk)
    write_csv(OUT_FAILURES, FAILURE_COLUMNS, failures)

    matched = sum(1 for row in crosswalk if row["kapt_code"])
    print(f"{OUT_CROSSWALK.relative_to(ROOT)}  {len(crosswalk)}행")
    print(f"{OUT_FAILURES.relative_to(ROOT)}  {len(failures)}행")
    print(f"단지={len(crosswalk):,}  매칭={matched:,}  실패={len(crosswalk) - matched:,}")
    print("\n".join(report_lines))
    print("두 구에 걸친 단지:")
    grouped_sgg = collections.defaultdict(set)
    for row in trade_rows:
        grouped_sgg[row["apt_seq"]].add(str(row.get("sgg_cd") or ""))
    for apt_seq in sorted(apt for apt, codes in grouped_sgg.items() if len(codes) > 1):
        print(f"  {apt_seq}: {','.join(sorted(grouped_sgg[apt_seq]))}")


if __name__ == "__main__":
    main()
