"""구 단위 커버리지, K-apt 매칭률, 직거래/해제 비율 계산기

DB 에 적재된 실거래를 서울시 공동주택 정보 CSV 와 대조해 구 단위 지표를 낸다.

전제
    입력      Supabase trade 테이블 (raw XML 아님)
              data/seoul-apt-info.csv (cp949, 서울 전체)
              config/districts.json (25개 구)
    매칭      scripts/matching.py 가 도로명 매칭 규칙을 소유
    읽기 전용 DB 에 쓰지 않음. complex 테이블도 만들지 않음

    기존 면적 조합 키는 numeric(9,4) 원본을 그대로 쓴다.
    표시 그룹은 36개월 면적을 1 m2 인접 병합하고 최소 면적을 ID 로 쓴다.
      - ROUND() 단독 그룹핑은 폐기됨. 개포자이 13개 그룹 중 2개가 .5 경계에서 오분류

실행
    set -a; source .env; set +a
    python3 scripts/coverage.py
    python3 scripts/coverage.py --as-of 202607

산출물
    data/coverage-by-district.csv   한 행 = 한 개 구, 25행. utf-8-sig

    combos_recent12m_group 은 최근 전체 거래 합계가 양수인 표시 그룹 수다.
    combos_recent12m_group_valid 는 최근 valid 거래 합계가 양수인 표시 그룹 수다.
    combos_1or2_all 은 최근 전체 거래 합계가 1 또는 2인 그룹 수이며 대조 전용이다.
    combos_1or2_valid 는 최근 valid 거래 합계가 1 또는 2인 그룹 수다.
    이 값은 판정, 화면, README 용이다.

    valid 거래는 cdeal_type == "O" 인 해제 행 전체와 직거래를 제외한다.
    ambiguous_cancel 해제 행은 정상 거래를 포함해 행 전체가 빠질 수 있다.
"""

import argparse
import collections
import csv
import json
import os
import pathlib
import sys
from datetime import date
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import matching

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
DISTRICTS_PATH = ROOT / "config" / "districts.json"
OUT_PATH       = ROOT / "data" / "coverage-by-district.csv"


# ============================== 상수 ==============================
DEFAULT_AS_OF = "202607"     # 2026-08 은 신고 유입 중이라 제외 (recon.py 의 END_YM 과 동일)
MONTHS_LONG   = 36           # 커버리지 분모 창
MONTHS_RECENT = 12           # 커버리지 분자 창
PAGE_SIZE     = 1000         # PostgREST 기본 상한
TIMEOUT_SEC   = 60
RATE_DIGITS   = 4            # 비율 표기 자릿수. 임계값이 아니라 표기 형식
MERGE_GAP     = Decimal("1") # 표시 그룹의 인접 면적 병합 기준

COLUMNS = [
    "lawd_cd", "district_name", "deal_rows", "deal_count",
    "complexes", "complexes_kapt_matched", "complex_match_rate",
    "deals_kapt_matched", "deal_weighted_match_rate",
    "combos_36m", "combos_recent12m", "coverage_rate",
    "direct_deal_count", "direct_deal_rate",
    "cancel_count", "cancel_rate",
    "combos_36m_group", "combos_recent12m_group",
    "combos_recent12m_group_valid", "combos_1or2_all",
    "combos_1or2_valid",
]

# trade 에서 받을 컬럼. sgg_cd 와 is_current 는 필터로만 쓰므로 받지 않음
SELECT = ",".join([
    "apt_seq", "exclu_use_ar", "trade_count", "deal_ym",
    "road_nm", "road_nm_bonbun", "road_nm_bubun",
    "dealing_gbn", "cdeal_type", "ambiguous_cancel",
    "deal_date", "floor",
])


# ============================== 기간 ==============================
def month_range(as_of: str, n: int) -> tuple:
    """as_of 를 끝으로 하는 n 개월 구간의 (시작, 끝) 을 YYYYMM 으로 돌려줌.

    n=36, as_of=202607 -> ('202308', '202607')
    """
    y, m = int(as_of[:4]), int(as_of[4:])
    total = y * 12 + (m - 1) - (n - 1)
    return f"{total // 12:04d}{total % 12 + 1:02d}", as_of


# ============================== 입력 로딩 ==============================
def load_districts() -> list:
    """config/districts.json 이 25개 구의 유일한 출처. 코드를 코드에 박지 않음."""
    with open(DISTRICTS_PATH, encoding="utf-8") as f:
        items = json.load(f)["districts"]
    return sorted(((d["code"], d["name"]) for d in items), key=lambda x: x[0])


# ============================== Supabase ==============================
class SupabaseError(RuntimeError):
    pass


def sb_config() -> tuple:
    """scripts/collect.py 와 같은 환경변수를 읽음. 이름을 새로 만들지 않음."""
    url = (os.environ.get("SUPABASE_URL")
           or os.environ.get("NEXT_PUBLIC_SUPABASE_URL") or "").strip()
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url or not key:
        sys.exit("[중단] Supabase 접속 정보가 없습니다. SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY 를 확인하세요.\n"
                 "       set -a; source .env; set +a")
    return url.rstrip("/"), key


def fetch_district(sgg_cd: str, start: str, end: str, url: str, key: str) -> list:
    """한 구의 거래를 전부 받음. PostgREST 는 한 요청에 1000행이라 페이지네이션 필수.

    order=id 가 필수. 유일하지 않은 키로 정렬하면 offset 페이지네이션이 깨짐
      - order=apt_seq 로 강남구를 받으면 중복 40행이 섞이고 실행마다 결과가 달라짐
      - id 는 유일해서 페이지 경계가 흔들리지 않음

    parse_float=Decimal 을 쓰는 이유
      - PostgREST 는 numeric 을 JSON 숫자로 주고 기본 파싱은 float
      - float 는 근사값. 조합 키가 흔들리면 같은 조합이 둘로 갈라짐
    """
    out, offset = [], 0
    path = (f"trade?select={SELECT}&sgg_cd=eq.{sgg_cd}&is_current=is.true"
            f"&deal_ym=gte.{start}&deal_ym=lte.{end}&order=id")
    while True:
        req = Request(f"{url}/rest/v1/{path}&offset={offset}&limit={PAGE_SIZE}",
                      headers={"apikey": key, "Authorization": f"Bearer {key}"})
        try:
            with urlopen(req, timeout=TIMEOUT_SEC) as resp:
                body = resp.read().decode("utf-8")
        except HTTPError as e:
            raise SupabaseError(f"HTTP {e.code}") from None
        except (URLError, TimeoutError, OSError) as e:
            raise SupabaseError(str(e)) from None
        chunk = json.loads(body, parse_float=Decimal)
        out += chunk
        if len(chunk) < PAGE_SIZE:
            return out
        offset += PAGE_SIZE


# ============================== 집계 ==============================
def display_groups(areas) -> list[list[Decimal]]:
    """같은 단지의 면적을 1 m2 인접 병합한 표시 그룹으로 돌려줌.

    중복을 없애고 정렬하므로 입력 순서와 표기 자릿수에 영향받지 않음.
    직전 면적과의 차가 MERGE_GAP 미만이면 같은 그룹으로 이어 붙임.
    """
    unique = set()
    for area in areas:
        value = Decimal(str(area)).normalize()
        unique.add(Decimal(format(value, "f")))
    ordered = sorted(unique)
    groups = []
    for area in ordered:
        if not groups or area - groups[-1][-1] >= MERGE_GAP:
            groups.append([area])
        else:
            groups[-1].append(area)
    return groups


def rate(num, den):
    """분모가 0 이면 빈 칸. 0 으로 메우지 않는다."""
    if not den:
        return ""
    return f"{num / den:.{RATE_DIGITS}f}"


def aggregate(rows: list, gu_name: str, index: matching.KaptIndex, recent_start: str) -> tuple:
    """한 구의 지표를 계산. (결과 dict, 진단 dict) 를 돌려줌."""
    deal_rows  = len(rows)
    deal_count = sum(r["trade_count"] for r in rows)

    direct = sum(r["trade_count"] for r in rows if r.get("dealing_gbn") == "직거래")
    # ambiguous_cancel 인 행은 정상 n 건 중 어느 것이 해제됐는지 모름
    #   - trade_count 전부를 해제로 세므로 과대계상. 보정하지 않음(보정 자체가 추측)
    cancel = sum(r["trade_count"] for r in rows if r.get("cdeal_type") == "O")
    amb_rows = sum(1 for r in rows if r.get("ambiguous_cancel"))
    amb_tc   = sum(r["trade_count"] for r in rows if r.get("ambiguous_cancel"))

    combos_36 = {(r["apt_seq"], matching.area_key(r["exclu_use_ar"])) for r in rows}
    combos_12 = {(r["apt_seq"], matching.area_key(r["exclu_use_ar"]))
                 for r in rows if r["deal_ym"] >= recent_start}

    # 단지 단위로 묶어 대표 도로명 결정
    per_apt = collections.defaultdict(list)
    for r in rows:
        per_apt[r["apt_seq"]].append(r)

    group_ids = {}
    group_keys = set()
    wide_groups = 0
    for apt, rs in per_apt.items():
        groups = display_groups(r["exclu_use_ar"] for r in rs)
        for group in groups:
            group_id = matching.area_key(group[0])
            group_keys.add((apt, group_id))
            if group[-1] - group[0] > MERGE_GAP:
                wide_groups += 1
            for area in group:
                group_ids[(apt, area)] = group_id

    recent_all = collections.defaultdict(int)
    recent_valid = collections.defaultdict(int)
    ambiguous_groups = set()
    for r in rows:
        if r["deal_ym"] < recent_start:
            continue
        area = Decimal(str(r["exclu_use_ar"]))
        combo = (r["apt_seq"], group_ids[(r["apt_seq"], area)])
        recent_all[combo] += r["trade_count"]
        if r.get("ambiguous_cancel") and r["trade_count"] > 0:
            ambiguous_groups.add(combo)
        if r.get("cdeal_type") != "O" and r.get("dealing_gbn") != "직거래":
            recent_valid[combo] += r["trade_count"]

    positive_recent_all = {combo: count for combo, count in recent_all.items() if count > 0}
    positive_recent_valid = {combo: count for combo, count in recent_valid.items() if count > 0}

    matched_apts, multi_cand = 0, 0
    matched_deals = 0
    miss_no_key, miss_with_key = 0, 0
    for apt, rs in per_apt.items():
        _, cands, status = matching.match_complex(index, gu_name, rs)
        if status == "matched":
            matched_apts += 1
            matched_deals += sum(x["trade_count"] for x in rs)
            if len(cands) > 1:
                multi_cand += 1
        elif status == "no_key":
            miss_no_key += 1
        else:
            miss_with_key += 1

    result = {
        "deal_rows": deal_rows,
        "deal_count": deal_count,
        "complexes": len(per_apt),
        "complexes_kapt_matched": matched_apts,
        "complex_match_rate": rate(matched_apts, len(per_apt)),
        "deals_kapt_matched": matched_deals,
        "deal_weighted_match_rate": rate(matched_deals, deal_count),
        "combos_36m": len(combos_36),
        "combos_recent12m": len(combos_12),
        "coverage_rate": rate(len(combos_12), len(combos_36)),
        "direct_deal_count": direct,
        "direct_deal_rate": rate(direct, deal_count),
        "cancel_count": cancel,
        "cancel_rate": rate(cancel, deal_count),
        "combos_36m_group": len(group_keys),
        "combos_recent12m_group": len(positive_recent_all),
        "combos_recent12m_group_valid": len(positive_recent_valid),
        "combos_1or2_all": sum(1 for count in positive_recent_all.values() if count in (1, 2)),
        "combos_1or2_valid": sum(1 for count in positive_recent_valid.values() if count in (1, 2)),
    }
    diag = {"multi_cand": multi_cand, "miss_no_key": miss_no_key,
            "miss_with_key": miss_with_key, "amb_rows": amb_rows, "amb_tc": amb_tc,
            "wide_groups": wide_groups, "ambiguous_groups": len(ambiguous_groups)}
    return result, diag


def write_csv(rows: list, path=OUT_PATH):
    """구별 집계 행을 고정된 COLUMNS 순서로 CSV 에 씀."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig: 이 파일은 Excel 로 열어 볼 대상. BOM 이 없으면 구 이름이 깨짐
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)


# ============================== main ==============================
def main():
    ap = argparse.ArgumentParser(description="구 단위 커버리지/매칭률 계산기")
    ap.add_argument("--as-of", default=DEFAULT_AS_OF,
                    help=f"기준월 YYYYMM. 기본 {DEFAULT_AS_OF} (이번 달은 신고 유입 중이라 제외)")
    args = ap.parse_args()

    as_of = args.as_of.strip()
    if len(as_of) != 6 or not as_of.isdigit() or not 1 <= int(as_of[4:]) <= 12:
        sys.exit(f"[중단] --as-of 는 YYYYMM 형식이어야 합니다: {as_of!r}")

    long_start, long_end = month_range(as_of, MONTHS_LONG)
    recent_start, _      = month_range(as_of, MONTHS_RECENT)

    districts = load_districts()
    index = matching.KaptIndex.load()
    url, key = sb_config()

    print(f"기준월 {as_of}  |  분모 {long_start}~{long_end} ({MONTHS_LONG}개월)"
          f"  |  분자 {recent_start}~{long_end} ({MONTHS_RECENT}개월)")
    print(f"K-apt 마스터: {index.source_rows:,}행 중 도로명 키 {index.key_rows:,}개, {index.gu_count}개 구로 파티션")
    print(f"대상 {len(districts)}개 구\n")

    out_rows, failed = [], []
    tot_rows = tot_count = 0
    for i, (code, name) in enumerate(districts, 1):
        try:
            rows = fetch_district(code, long_start, long_end, url, key)
        except SupabaseError as e:
            print(f"[{i:2d}/{len(districts)}] {code} {name:6s} 조회 실패: {e}")
            failed.append(code)
            continue
        res, diag = aggregate(rows, name, index, recent_start)
        res["lawd_cd"], res["district_name"] = code, name
        out_rows.append(res)
        tot_rows  += res["deal_rows"]
        tot_count += res["deal_count"]
        print(f"[{i:2d}/{len(districts)}] {code} {name:6s} "
              f"행={res['deal_rows']:>6,} 건수={res['deal_count']:>6,} "
              f"단지={res['complexes']:>4,}({res['complexes_kapt_matched']:>4,} 매칭) "
              f"조합={res['combos_36m']:>5,}/{res['combos_recent12m']:>5,} "
              f"cov={res['coverage_rate'] or '-':>6s} "
              f"그룹={res['combos_36m_group']:>5,}/{res['combos_recent12m_group']:>5,} "
              f"cov_g={rate(res['combos_recent12m_group'], res['combos_36m_group']) or '-':>6s} "
              f"| 후보2+={diag['multi_cand']:>2} "
              f"미매칭(키없음/키있음)={diag['miss_no_key']}/{diag['miss_with_key']} "
              f"ambiguous={diag['amb_rows']}({diag['amb_tc']}) "
              f"폭1㎡초과그룹={diag['wide_groups']} "
              f"ambiguous영향그룹={diag['ambiguous_groups']}")

    if failed:
        print(f"\n[경고] 조회 실패 {len(failed)}개 구: {failed}")

    write_csv(out_rows)

    print(f"\n{OUT_PATH.relative_to(ROOT)}  {len(out_rows)}행")
    print(f"합계  행={tot_rows:,}  건수={tot_count:,}")
    print(f"생성일 {date.today().isoformat()}  (data/recon-summary.md 에 기준월을 함께 적을 것)")
    if len(out_rows) != len(districts):
        sys.exit(1)


if __name__ == "__main__":
    main()
