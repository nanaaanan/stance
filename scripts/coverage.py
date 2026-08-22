"""구 단위 커버리지, K-apt 매칭률, 직거래/해제 비율 계산기

DB 에 적재된 실거래를 서울시 공동주택 정보 CSV 와 대조해 구 단위 지표를 낸다.

전제
    입력      Supabase trade 테이블 (raw XML 아님)
              data/seoul-apt-info.csv (cp949, 서울 전체)
              config/districts.json (25개 구)
    매칭      도로명 단독. 이름/지번 매칭은 폐기됨
    정규화    도로명 키 규칙은 scripts/recon.py 의 road_key() 와 같아야 함
              두 파일의 규칙이 달라지면 recon.py 로 낸 실측과 대조가 성립하지 않음
    읽기 전용 DB 에 쓰지 않음. complex 테이블도 만들지 않음

    면적은 numeric(9,4) 원본 그대로 조합 키에 넣는다
      - ROUND() 단독 그룹핑은 폐기됨. 개포자이 13개 그룹 중 2개가 .5 경계에서 오분류

실행
    set -a; source .env; set +a
    python3 scripts/coverage.py
    python3 scripts/coverage.py --as-of 202607

산출물
    data/coverage-by-district.csv   한 행 = 한 개 구, 25행. utf-8-sig
"""

import argparse
import collections
import csv
import json
import os
import pathlib
import sys
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
DISTRICTS_PATH = ROOT / "config" / "districts.json"
KAPT_CSV_PATH  = ROOT / "data" / "seoul-apt-info.csv"
OUT_PATH       = ROOT / "data" / "coverage-by-district.csv"


# ============================== 상수 ==============================
DEFAULT_AS_OF = "202607"     # 2026-08 은 신고 유입 중이라 제외 (recon.py 의 END_YM 과 동일)
MONTHS_LONG   = 36           # 커버리지 분모 창
MONTHS_RECENT = 12           # 커버리지 분자 창
PAGE_SIZE     = 1000         # PostgREST 기본 상한
TIMEOUT_SEC   = 60
RATE_DIGITS   = 4            # 비율 표기 자릿수. 임계값이 아니라 표기 형식

COLUMNS = [
    "lawd_cd", "district_name", "deal_rows", "deal_count",
    "complexes", "complexes_kapt_matched", "complex_match_rate",
    "deals_kapt_matched", "deal_weighted_match_rate",
    "combos_36m", "combos_recent12m", "coverage_rate",
    "direct_deal_count", "direct_deal_rate",
    "cancel_count", "cancel_rate",
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


# ============================== 도로명 키 ==============================
def _int(v) -> int:
    """'00221' -> 221. 빈 값이나 숫자가 아니면 0."""
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return 0


def road_key(rn, bonbun, bubun) -> str:
    """실거래: '선릉로' + '00221' + '00000' -> '선릉로 221'

    scripts/recon.py 의 road_key() 와 문자 단위로 같은 결과를 내야 함
      - 규칙이 갈라지면 recon.py 로 낸 강남구 실측(39.3%/84.3%)과 대조 불가
    실거래는 본번이 zero-pad 되어 오고, CSV 는 원문 그대로라 여기서 맞춤
    부번이 0 이면 붙이지 않음
    """
    b, s = _int(bonbun), _int(bubun)
    if not str(rn).strip() or not b:
        return ""
    return f"{str(rn).strip()} {b}" + (f"-{s}" if s else "")


def kapt_key(road_nm, detail) -> str:
    """K-apt: '주소(도로명)' + '주소(도로상세주소)' -> '선릉로 221'."""
    rn, de = str(road_nm or "").strip(), str(detail or "").strip()
    return f"{rn} {de}" if rn and de else ""


# ============================== 입력 로딩 ==============================
def load_districts() -> list:
    """config/districts.json 이 25개 구의 유일한 출처. 코드를 코드에 박지 않음."""
    with open(DISTRICTS_PATH, encoding="utf-8") as f:
        items = json.load(f)["districts"]
    return sorted(((d["code"], d["name"]) for d in items), key=lambda x: x[0])


def load_kapt() -> tuple:
    """도로명 조회 사전 생성. { 시군구 -> { road_key -> [(코드, 이름), ...] } }

    서울 전체를 로드하고 구로 파티션하는 이유
      - 구로 걸러 로드하면 다른 구가 전부 미매칭이 됨 (과거 폐기 사례)
      - 그렇다고 한 사전에 뭉치면 다른 구의 같은 도로명에 잘못 붙을 수 있음
      - 현 스냅샷에서 구를 넘는 중복 키는 0개지만 계약으로 보장된 값이 아님
    후보를 리스트로 보존하는 이유
      - 같은 구 안에 같은 키가 2행 이상인 경우가 82개 있음
      - 판정에는 영향이 없지만 버렸다는 사실을 세기 위함
    """
    rows, decoded = [], False
    # 디코딩이 조용히 성공할 수 있어, 예외 여부가 아니라 컬럼 존재로 확인
    for enc in ("cp949", "utf-8-sig", "euc-kr", "utf-8"):
        try:
            with open(KAPT_CSV_PATH, encoding=enc, newline="") as f:
                rows = list(csv.DictReader(f))
        except (UnicodeDecodeError, LookupError):
            continue
        if rows and "주소(도로명)" in rows[0]:
            decoded = True
            break
    if not decoded:
        sys.exit(f"[중단] {KAPT_CSV_PATH.name} 을 읽지 못했습니다. 인코딩과 컬럼명을 확인하세요.")

    by_gu, n = collections.defaultdict(dict), 0
    for r in rows:
        k = kapt_key(r.get("주소(도로명)"), r.get("주소(도로상세주소)"))
        if not k:
            continue
        gu = (r.get("주소(시군구)") or "").strip()
        by_gu[gu].setdefault(k, []).append(
            ((r.get("k-아파트코드") or "").strip(), (r.get("k-아파트명") or "").strip()))
        n += 1
    return by_gu, len(rows), n


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
def area_key(v) -> str:
    """면적을 조합 키에 넣을 문자열로. 원본 자릿수를 유지.

    반올림하지 않는 이유
      - ROUND() 단독 그룹핑은 폐기됨 (.5 경계 오분류 실측)
      - 표시용 그룹(인접 차이 1㎡ 미만 병합)은 D4 작업
    """
    try:
        return str(Decimal(str(v)))
    except (InvalidOperation, TypeError):
        return ""


def sort_key(r) -> tuple:
    """대표 도로명을 고를 때 쓰는 결정적 정렬 순서.

    첫 행을 고정하는 것은 recon.py 의 setdefault 와 같은 성격
    정렬을 명시하는 이유는 recon.py 와의 일치가 아니라
    이 스크립트가 실행할 때마다 같은 값을 낸다는 것 자체
      - 도로명이 갈리는 단지는 강남구 512개 중 4개(0.78%)
      - 다만 건수 가중으로는 205/10,237 = 2.00%p 까지 움직일 수 있음
    """
    return (str(r.get("deal_date") or ""), area_key(r.get("exclu_use_ar")),
            _int(r.get("floor")))


def rate(num, den):
    """분모가 0 이면 빈 칸. 0 으로 메우지 않는다."""
    if not den:
        return ""
    return f"{num / den:.{RATE_DIGITS}f}"


def aggregate(rows: list, gu_name: str, by_gu: dict, recent_start: str) -> tuple:
    """한 구의 지표를 계산. (결과 dict, 진단 dict) 를 돌려줌."""
    deal_rows  = len(rows)
    deal_count = sum(r["trade_count"] for r in rows)

    direct = sum(r["trade_count"] for r in rows if r.get("dealing_gbn") == "직거래")
    # ambiguous_cancel 인 행은 정상 n 건 중 어느 것이 해제됐는지 모름
    #   - trade_count 전부를 해제로 세므로 과대계상. 보정하지 않음(보정 자체가 추측)
    cancel = sum(r["trade_count"] for r in rows if r.get("cdeal_type") == "O")
    amb_rows = sum(1 for r in rows if r.get("ambiguous_cancel"))
    amb_tc   = sum(r["trade_count"] for r in rows if r.get("ambiguous_cancel"))

    combos_36 = {(r["apt_seq"], area_key(r["exclu_use_ar"])) for r in rows}
    combos_12 = {(r["apt_seq"], area_key(r["exclu_use_ar"]))
                 for r in rows if r["deal_ym"] >= recent_start}

    # 단지 단위로 묶어 대표 도로명 결정
    per_apt = collections.defaultdict(list)
    for r in rows:
        per_apt[r["apt_seq"]].append(r)

    lookup = by_gu.get(gu_name, {})
    matched_apts, multi_cand = 0, 0
    matched_deals = 0
    miss_no_key, miss_with_key = 0, 0
    for apt, rs in per_apt.items():
        head = min(rs, key=sort_key)
        k = road_key(head.get("road_nm"), head.get("road_nm_bonbun"), head.get("road_nm_bubun"))
        cands = lookup.get(k) if k else None
        if cands:
            matched_apts += 1
            matched_deals += sum(x["trade_count"] for x in rs)
            if len(cands) > 1:
                multi_cand += 1
        elif not k:
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
    }
    diag = {"multi_cand": multi_cand, "miss_no_key": miss_no_key,
            "miss_with_key": miss_with_key, "amb_rows": amb_rows, "amb_tc": amb_tc}
    return result, diag


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
    by_gu, csv_rows, csv_keys = load_kapt()
    url, key = sb_config()

    print(f"기준월 {as_of}  |  분모 {long_start}~{long_end} ({MONTHS_LONG}개월)"
          f"  |  분자 {recent_start}~{long_end} ({MONTHS_RECENT}개월)")
    print(f"K-apt 마스터: {csv_rows:,}행 중 도로명 키 {csv_keys:,}개, {len(by_gu)}개 구로 파티션")
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
        res, diag = aggregate(rows, name, by_gu, recent_start)
        res["lawd_cd"], res["district_name"] = code, name
        out_rows.append(res)
        tot_rows  += res["deal_rows"]
        tot_count += res["deal_count"]
        print(f"[{i:2d}/{len(districts)}] {code} {name:6s} "
              f"행={res['deal_rows']:>6,} 건수={res['deal_count']:>6,} "
              f"단지={res['complexes']:>4,}({res['complexes_kapt_matched']:>4,} 매칭) "
              f"조합={res['combos_36m']:>5,}/{res['combos_recent12m']:>5,} "
              f"cov={res['coverage_rate'] or '-':>6s} "
              f"| 후보2+={diag['multi_cand']:>2} "
              f"미매칭(키없음/키있음)={diag['miss_no_key']}/{diag['miss_with_key']} "
              f"ambiguous={diag['amb_rows']}({diag['amb_tc']})")

    if failed:
        print(f"\n[경고] 조회 실패 {len(failed)}개 구: {failed}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig: 이 파일은 Excel 로 열어 볼 대상. BOM 이 없으면 구 이름이 깨짐
    with open(OUT_PATH, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(out_rows)

    print(f"\n{OUT_PATH.relative_to(ROOT)}  {len(out_rows)}행")
    print(f"합계  행={tot_rows:,}  건수={tot_count:,}")
    print(f"생성일 {date.today().isoformat()}  (data/recon-summary.md 에 기준월을 함께 적을 것)")
    if len(out_rows) != len(districts):
        sys.exit(1)


if __name__ == "__main__":
    main()
