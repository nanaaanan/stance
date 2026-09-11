"""실거래 단지와 K-apt 관리정보의 도로명 대응표와 실패표 산출기.

파일명에는 구 접미사를 붙이지 않는다. 서울 전체를 한 파일에 담아 M4 입력으로 쓴다.

실행
    set -a; source .env; set +a
    python3 scripts/crosswalk.py
"""

import collections
import csv
import importlib.util
import json
import pathlib
import sys
from decimal import Decimal
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
    "deal_date", "exclu_use_ar", "floor", "trade_count",
])
PAGE_SIZE = 1000
TIMEOUT_SEC = 60

CROSSWALK_COLUMNS = [
    "sgg_cd", "apt_seq", "apt_nm", "road_key",
    "kapt_code", "kapt_key", "match_source",
]
FAILURE_COLUMNS = ["sgg_cd", "apt_seq", "apt_nm", "road_key", "fail_reason"]


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


def main():
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
    write_csv(OUT_CROSSWALK, CROSSWALK_COLUMNS, crosswalk)
    write_csv(OUT_FAILURES, FAILURE_COLUMNS, failures)

    matched = sum(1 for row in crosswalk if row["kapt_code"])
    print(f"{OUT_CROSSWALK.relative_to(ROOT)}  {len(crosswalk)}행")
    print(f"{OUT_FAILURES.relative_to(ROOT)}  {len(failures)}행")
    print(f"단지={len(crosswalk):,}  매칭={matched:,}  실패={len(failures):,}")
    print("두 구에 걸친 단지:")
    grouped_sgg = collections.defaultdict(set)
    for row in trade_rows:
        grouped_sgg[row["apt_seq"]].add(str(row.get("sgg_cd") or ""))
    for apt_seq in sorted(apt for apt, codes in grouped_sgg.items() if len(codes) > 1):
        print(f"  {apt_seq}: {','.join(sorted(grouped_sgg[apt_seq]))}")


if __name__ == "__main__":
    main()
