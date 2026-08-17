"""
[매매 실거래와 공동주택 정보의 매칭검증용 정찰 스크립트 - 1개 구 기준]

문제 인식
    정찰 결과(매칭률)에 따라 만들 수 있는 화면 설계가 달라지는데, 수집 파이프라인을 다 짠 뒤 "안 붙네"를 알면 늦는다.
    정찰 후 25개 구 전량 적재 -> 정제 파이프라인 작성 -> 마스터 조인 테이블 구축 -> DB 적재 순으로 진행한다.

목표 설정
    아래 1,2번 데이터 모두에 있는 '도로명 주소' 컬럼을 기준으로, 두 데이터를 붙여보고 리포트 산출. -> "된다 / 안 된다 판정"
    - 리포트 : '단지별 거래건수 + 두 데이터 매칭률(complex-coverage)', '붙지 않은 목록(matching-failures)'

    1. '국토교통부_아파트 매매 실거래가 상세 API 호출 데이터'
    2. '서울특별시 공동주택 아파트 정보.csv'

원인 및 대안
    두 데이터를 붙이는 이유:
    한 쪽 정보만으로는 거래 회전율과 같은 지표를 만들 수 없어, 두 데이터가 안붙으면 만들 수 있는 지표 및 화면이 달라진다.
    '국토교통부_아파트 매매 실거래가 상세 자료 OPEN API': 거래 데이터 위주 - 언제, 얼마에, 몇 층, 몇 제곱미터가 팔렸는지 등.
    '서울특별시 공동주택 아파트 정보.csv': 관리 데이터 - 세대수, 좌표, 주차대수, 복도유형, 난방방식 등. (대안: K-apt 오픈API를 호출하지 않음 - 약 6,300콜 절감)

    실측 후 - 도로명 주소를 기준으로 매칭 결정:
    실측에서 단지명/지번 부분일치가 오매칭을 만든다. 미매칭은 '관리정보 미확인' 처리가능, 오매칭은 틀린세대수를 근거로 거래회전율까지 조용히 왜곡해 사실처럼 전달하므로 폐기.

    실측 후 - 문제 원인 발견:
    1. csv에 없는 단지가 실거래에 있다. (원인: 100세대 미만은 공동주택 의무 등록대상이 아니다. - 대안: 지번 지오코딩 좌표만)
    2. 실거래 도로명 주소가 다르다. (원인: 실거래 쪽만 숫자 정규화돼 있고 csv 쪽은 원문이다. - 대안: csv 쪽 정규화를 맞춘다)
    3. 실거래 도로명 주소가 비어 있다. (원인: 도로명으로는 불가. 지번은 있다. - 대안: 지번 지오코딩 좌표만)
    
    문제 원인별 개수 파악 목적 - docs/matching-failures/
    몇번 문제를 해결하면 매칭률이 얼마나 오르는지 계산할 수 있고, 현재 설계대로 진행해도 괜찮은지 판단할 수 있다.
    road_key() 열이 빈 행은 원인 3, 값이 있는데 안 붙은 행은 원인 1 또는 2다.

레포 루트에서 실행
    export DATA_GO_KR_KEY='디코딩_서비스키'
    python3 scripts/recon.py                수집 + 리포트  (약 40콜)
    python3 scripts/recon.py --report-only  재파싱만       (API 호출 0)
    python3 scripts/recon.py --audit-key    자연키 감사    (API 호출 0. --rent 로 전월세)

    --key 인자도 되지만 셸 히스토리에 평문으로 남으므로 환경변수를 쓴다.
    서비스키는 '디코딩' 키여야 한다. urlencode() 로 조립하므로 인코딩 키를 넣으면 이중 인코딩되어 에러 30 이 난다.

입력
    data/seoul-apt-info.csv   서울특별시 공동주택 아파트 정보 (출처: data/recon-summary.md)
    csv파일은 레포에 함께 커밋한다. (원본이 수시로 갱신되므로 재현성 확보 필요, 용량 작음)

    seoul-apt-info.csv 중 5개 컬럼만 읽어, 도로명 -> (단지코드, 단지명) 조회 사전(by_road)을 만든다.
            주소(시군구)        해당 구만 남기는 필터
            주소(도로명)        조회 키
            주소(도로상세주소)    조회 키
            k-아파트코드        붙었을 때 얻는 값
            k-아파트명          붙었을 때 얻는 값
    나머지 42열(좌표, 세대수, 복도유형, 난방방식, 주차대수, 사용승인일)은
    complex 테이블 구축 단계에서 사용.  # TODO: 추후 작업 대상

산출물
    scripts/raw/trade/{LAWD_CD}_{YYYYMM}_{page}.xml   원본 응답. gitignore
    data/complex-coverage-{LAWD_CD}.csv               단지별 거래건수 + 매칭 결과. 커밋
    data/matching-failures-{LAWD_CD}.csv              붙지 않은 단지. 커밋

    파일명에 구 코드 넣는 이유: 파일 안에 시군구 칼럼이 없다. 한 파일이 한개의 구만 담는다. -> 구 코드를 파일명에 포함시켜 구분한다.

설계 원칙
    - 원본 응답을 먼저 파일로 떨군다. 재파싱에 API 를 다시 부르지 않는다
    - 30tps 제한 -> 요청 간 0.2초. 한도 초과(코드 22) 시 중단하고 다음 날 이어받는다
    - DEAL_YMD 에 범위 파라미터가 없다. 1콜 = 1개월. 36개월 = 36콜
    - 매칭은 도로명 단독. 단지명 / 지번 폴백은 오매칭을 만들어 폐기했다
    - 매칭률은 단지 기준과 거래건수 가중 둘 다 본다. 하나만 쓰면 유리한 쪽만 고른 것이 된다
"""

import os
import sys
import csv
import time
import math
import pathlib
import collections
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import urlencode
from urllib.request import urlopen

# ====================== 필요하면 여기만 수정 ======================
LAWD_CD   = "11680"        # 강남구. 강남 11680 / 노원 11350 / 마포 11440 / 관악 11620 / 성동 11200 / 동작 11590 / 강동 11740
SIGUNGU   = "강남구"        # LAWD_CD와 반드시 일치시킬 것
START_YM  = "202308"       # 36개월 전
END_YM    = "202607"       # 2026-08은 신고 유입 중이라 제외
CSV_NAME  = "seoul-apt-info.csv"
# ===================================================================

NUM_OF_ROWS = 1000
SLEEP_SEC   = 0.2          # 명세상 30tps -> 5req/s로 여유
OK_CODES    = ("000", "00")
BASE = "https://apis.data.go.kr/1613000/RTMSDataSvcAptTradeDev/getRTMSDataSvcAptTradeDev"

HERE     = pathlib.Path(__file__).resolve().parent   # scripts/
ROOT     = HERE.parent                               # 레포 루트
DATA     = ROOT / "data"                             # 프로젝트 데이터 (의존성, 근거)
CSV_PATH = DATA / CSV_NAME
RAW      = HERE / "raw" / "trade"                    # 원본 응답. gitignore
RAW_RENT = HERE / "raw" / "rent"                     # 전월세 원본 응답. gitignore


# -------------------------- 준비 --------------------------
def get_key() -> str:
    if "--key" in sys.argv:
        i = sys.argv.index("--key")
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1].strip()
    return os.environ.get("DATA_GO_KR_KEY", "").strip()


def preflight(need_key: bool = True) -> str:
    problems = []
    key = get_key()
    if need_key:
        if not key:
            problems.append("서비스키가 없습니다.   export DATA_GO_KR_KEY='디코딩키'")
        elif "%" in key:
            problems.append("서비스키에 '%'가 있습니다 -> 인코딩 키로 보입니다. 디코딩 키를 쓰세요.")
    if not CSV_PATH.exists():
        problems.append(f"파일 없음: {CSV_PATH}")
    if problems:
        print("\n[실행 전 확인 필요]")
        for p in problems:
            print("  - " + p)
        print(f"\n기대 경로: {CSV_PATH}")
        print("서울열린데이터광장에서 받아 data/ 에 두거나 CSV_NAME을 수정하세요.\n")
        sys.exit(1)
    RAW.mkdir(parents=True, exist_ok=True)
    DATA.mkdir(parents=True, exist_ok=True)
    return key


def months(start: str, end: str):
    """DEAL_YMD는 단일 계약년월만 받음. 기간범위/콤마목록 파라미터 없음. 1콜=1개월.
    """
    y, m = int(start[:4]), int(start[4:])
    ey, em = int(end[:4]), int(end[4:])
    while (y, m) <= (ey, em):
        yield f"{y}{m:02d}"
        m += 1
        if m == 13:
            y, m = y + 1, 1


# -------------------------- 수집 --------------------------
def fetch(key: str, ym: str, page: int) -> str:
    """API 한 번 호출해 XML 본문 돌려줌
    
    서비스키는 urlencode로 조립 - 디코딩 키 써야함.
    """
    q = urlencode({"serviceKey": key, "LAWD_CD": LAWD_CD, "DEAL_YMD": ym,
                   "pageNo": page, "numOfRows": NUM_OF_ROWS})
    with urlopen(f"{BASE}?{q}", timeout=30) as r:
        return r.read().decode("utf-8")


def check_error(body: str, ym: str, page: int):
    """resultCode, returnReasonCode 둘 다 볼 것 - 한쪽만 보면 "에러는 없는데 데이터 0건"으로 잘못 읽힘.

    인증 실패하면 루트 태그, 코드 필드가 달라질 수 있음
    """
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        sys.exit(f"[중단] {ym} p{page}: XML이 아닌 응답. 서비스키/URL 확인.\n{body[:300]}")
    code = (root.findtext(".//resultCode") or root.findtext(".//returnReasonCode") or "").strip()
    if code in ("30", "31"):
        sys.exit(f"[중단] 서비스키 오류(코드 {code}). 디코딩 키인지, 활용신청이 승인됐는지 확인하세요.")
    if code == "20":
        sys.exit("[중단] 활용승인 대기(코드 20). 포털 마이페이지에서 승인 상태를 확인하세요.")
    if code == "22":
        sys.exit(f"[중단] 일일 요청 한도 초과. {ym} p{page} 직전까지 완료.\n"
                 f"       내일 같은 명령을 다시 실행하면 raw/ 에 있는 건 건너뛰고 이어받습니다.")
    return root, code


def collect(key: str):
    calls = 0
    for ym in months(START_YM, END_YM):
        page = 1
        while True:
            path = RAW / f"{LAWD_CD}_{ym}_{page}.xml"

            # 정상응답에는 totalCount 있음. 
            # 이를 포함하지 않은 기존 파일은 오류 응답으로 보고 지운 뒤 다시 받을 것
            if path.exists() and "<totalCount>" not in path.read_text(encoding="utf-8"):
                path.unlink()

            if path.exists():                      # 이미 받은 건 건너뜀 (쿼터 절약)
                body, fresh = path.read_text(encoding="utf-8"), False
            else:
                body, fresh = fetch(key, ym, page), True
                calls += 1
                time.sleep(SLEEP_SEC)

            root, code = check_error(body, ym, page)   # 치명 코드면 여기서 중단

            if code not in OK_CODES:
                print(f"[경고] {ym} p{page} code={code} {root.findtext('.//resultMsg')}")
                break

            # 저장은 검사를 통과한 뒤에.
            # 에러 본문 남기면 다음 실행이 그 파일을 다시읽어 같은 에러로 멈춤 - 이어받기 영구히 막힘
            if fresh:
                path.write_text(body, encoding="utf-8")

            total = int(root.findtext(".//totalCount") or 0)
            pages = max(1, math.ceil(total / NUM_OF_ROWS))
            if page == 1:
                print(f"  {ym}: totalCount={total:>5}  (페이지 {pages})")
            if page >= pages or total == 0:
                break
            page += 1
    print(f"\n신규 API 호출: {calls}건  (개발계정 한도 10,000건/일)")


# -------------------------- 파싱 --------------------------
def parse_raw():
    """재파싱에 API 호출이 들지 않고, 저장된 원본에서만 읽음.

    RAW에 여러 구의 응답이 있을 수 있으므로 현재 LAWD_CD만 읽음.
    """
    rows = []
    for f in sorted(RAW.glob(f"{LAWD_CD}_*.xml")):
        try:
            root = ET.fromstring(f.read_text(encoding="utf-8"))
        except ET.ParseError:
            print(f"[건너뜀] 파싱 실패: {f.name}")
            continue
        for it in root.iter("item"):
            g = lambda t: (it.findtext(t) or "").strip()
            rows.append({
                "aptSeq": g("aptSeq"), "aptNm": g("aptNm"), "umdNm": g("umdNm"),
                "roadNm": g("roadNm"), "roadNmBonbun": g("roadNmBonbun"),
                "roadNmBubun": g("roadNmBubun"),
                "bonbun": g("bonbun"), "bubun": g("bubun"),
                "excluUseAr": g("excluUseAr"),
                "dealAmount": g("dealAmount").replace(",", ""),
                "dealYear": g("dealYear"), "dealMonth": g("dealMonth"),
                # 정제 대상 플래그. 이 리포트의 거래건수는 두 값을 걸러내지 않은 원시 건수
                # 해제 중복 제거와 직거래 제외는 적재/판정 단계의 일.
                "cdealType": g("cdealType"),
                "dealingGbn": g("dealingGbn"),
            })
    return rows


# -------------------------- 매칭 키 --------------------------
def _int(v) -> int:
    try:
        return int(str(v).strip() or 0)
    except ValueError:
        return 0


def road_key(rn, bonbun, bubun) -> str:
    """ 실거래: '선릉로' + '00221' + '00000' -> '선릉로 221'.
    
    실거래는 도로명/본번/부번을 세 필드로 나눠 주고 본번은 zero-pad 되어 있음.
    공동주택 정보 csv 는 [주소(도로명) + 주소(도로상세주소)] 한 문자열이라 여기서 맞춤.
    csv쪽은 원문 그대로라 표기가 다르면 안 붙음(문제 원인 2). 부번이 0이면 붙이지 않음.  # TODO: 추후 작업 대상
    """
    b, s = _int(bonbun), _int(bubun)
    if not str(rn).strip() or not b:
        return ""
    return f"{str(rn).strip()} {b}" + (f"-{s}" if s else "")


def jibun_key(umd, bonbun, bubun) -> str:
    """'역삼동' + '0826' + '0029' -> '역삼동 826-29'.

    매칭에는 쓰지 않음 - 미매칭 단지를 나중에 지오코딩할 때의 입력이 지번이라 실패 목록에 진단 컬럼으로 함께 남김
    """
    b, s = _int(bonbun), _int(bubun)
    if not b:
        return ""
    return f"{str(umd).strip()} {b}" + (f"-{s}" if s else "")


# -------------------------- 공동주택 정보 로딩 --------------------------
def load_kapt():
    """도로명을 key로 사용하는 조회 사전 생성 (value: k-아파트코드, k-아파트명)

    구 먼저 거르는 이유: 도로명은 구 단위로 유일하지 않음. 25개 구를 한 사전에 넣으면 다른 구의 같은 도로명에 잘못 붙을 수 있음.
    """
    rows, decoded = [], False
    # 잘못된 인코딩으로 읽어도 예외가 나지 않을 수 있어,
    # 디코딩 성공 여부보다 실제 필터 결과를 함께 확인 할 것.
    for enc in ("cp949", "utf-8-sig", "euc-kr", "utf-8"):
        try:
            with open(CSV_PATH, encoding=enc, newline="") as f:
                all_rows = list(csv.DictReader(f))
        except (UnicodeDecodeError, LookupError):
            continue
        decoded = True
        rows = [r for r in all_rows if (r.get("주소(시군구)") or "").strip() == SIGUNGU]
        if rows:
            break
    if not decoded:
        sys.exit(f"[중단] CSV 인코딩을 판별하지 못했습니다: {CSV_PATH}")
    if not rows:
        sys.exit(f"[중단] CSV에서 '{SIGUNGU}' 행을 찾지 못했습니다.\n"
                 f"       SIGUNGU 값 또는 CSV 인코딩(헤더 깨짐)을 확인하세요.")

    by_road = {}
    for r in rows:
        code, name = r.get("k-아파트코드", ""), r.get("k-아파트명", "")
        rk = f"{(r.get('주소(도로명)') or '').strip()} {(r.get('주소(도로상세주소)') or '').strip()}".strip()
        if rk:
            by_road.setdefault(rk, (code, name))
    return len(rows), by_road


# -------------------------- 리포트 --------------------------
def report():
    rows = parse_raw()
    if not rows:
        sys.exit(f"raw/ 에 {LAWD_CD} 응답이 없습니다. 먼저 수집을 실행하세요:  python3 recon.py")

    n_kapt, by_road = load_kapt()

    # 단지(aptSeq) 단위로 집계
    agg = {}
    for r in rows:
        k = r["aptSeq"]
        a = agg.setdefault(k, {"aptSeq": k, "aptNm": r["aptNm"], "umdNm": r["umdNm"],
                               "road_key": road_key(r["roadNm"], r["roadNmBonbun"], r["roadNmBubun"]),
                               "jibun_key": jibun_key(r["umdNm"], r["bonbun"], r["bubun"]),
                               "거래건수": 0})
        a["거래건수"] += 1

    # 도로명 단독 매칭.
    for a in agg.values():
        hit = by_road.get(a["road_key"])
        a["kaptCode"], a["kaptName"] = hit if hit else ("", "")
        a["matched_by"] = "road" if hit else "none"

    per = sorted(agg.values(), key=lambda x: -x["거래건수"])
    matched = [a for a in per if a["kaptCode"]]
    unmatched = [a for a in per if not a["kaptCode"]]
    tot_d = sum(a["거래건수"] for a in per)
    wd = sum(a["거래건수"] for a in matched)
    dup = collections.Counter(a["kaptCode"] for a in matched)
    n_cancel = sum(1 for r in rows if r["cdealType"] == "O") # 알파벳 O = 해제 신고가 발생함
    n_direct = sum(1 for r in rows if r["dealingGbn"] == "직거래")

    print("=" * 72)
    print(f"기간 {START_YM}~{END_YM} (36개월)   지역 {LAWD_CD} {SIGUNGU}   공동주택 정보 {n_kapt}단지")
    print(f"1) 거래 총건수      : {tot_d:,}   (해제/직거래 미정제 원시 건수)")
    print(f"2) aptSeq 고유값    : {len(per):,}")
    # 단지 수 기준과 거래건수 가중 기준을 함께 봄.
    # 단지 수만 보면 소규모 단지의 영향이 크고, 거래건수만 보면 대단지에 유리할 수 있기 때문.
    print(f"3) 단지 기준 매칭률 : {len(matched)}/{len(per)} = {len(matched)/len(per)*100:.1f}%")
    print(f"4) 건수 가중 매칭률 : {wd:,}/{tot_d:,} = {wd/tot_d*100:.1f}%"
          f"    <- 70% 미만이면 README 숫자를 실측 구 범위로 표기")
    print(f"5) 1:N 케이스       : {sum(1 for v in dup.values() if v > 1)}건"
          f"  (실거래 여러 단지 -> csv 한 단지)")
    print(f"6) 해제 / 직거래    : {n_cancel:,} ({n_cancel/tot_d*100:.1f}%)"
          f" / {n_direct:,} ({n_direct/tot_d*100:.1f}%)    <- 구별 편차 확인용")
    print("=" * 72)

    print("\n[미매칭 상위 30] 거래가 많은데 안 붙었다 = 실질 손실")
    print(f"{'aptSeq':<14}{'단지명':<26}{'법정동':<10}{'도로명주소':<24}건수")
    print("-" * 84)
    for a in unmatched[:30]:
        print(f"{a['aptSeq']:<14}{a['aptNm'][:24]:<26}{a['umdNm']:<10}{a['road_key'][:22]:<24}{a['거래건수']}")

    cols = ["aptSeq", "aptNm", "umdNm", "road_key", "jibun_key", "거래건수",
            "kaptCode", "kaptName", "matched_by"]
    for stem, data in (("matching-failures", unmatched), ("complex-coverage", per)):
        path = DATA / f"{stem}-{LAWD_CD}.csv"
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(data)
        print(f"\n-> {path}")

# -----------------------------------------------------------------------------
# 자연키 감사 정찰 스크립트
#
# 최근 3개월을 반복 수집하므로 DB 는 재수집에 멱등적이어야 한다.
# 자연키 upsert 가 가능한지, 값 변경을 이력으로 추적할 후보가 있는지 검증한다.
#   - 컬럼 조합만으로 거래가 유일하게 구분되는지  -> audit_natural_key
#   - 금액 변경을 같은 identity 로 추적 가능한지  -> audit_amount_tracking
#   - 컬럼별 실제 형식과 채움 상태                -> audit_fields
# -----------------------------------------------------------------------------

# 후보 A: 자연키 후보. 강남구 36개월 전수 확인 -> 매매 자연키 unique 인덱스의 근거
# 후보 B: 감사 실험. 자연키 후보 아님
#   - 목적: 금액만 정정된 같은 거래를 같은 identity 로 추적 가능한지
#   - 이 키로 묶였는데 금액이 다른 그룹 = 정정 후보
KEY_TRADE_BASE        = ("aptSeq", "dealYear", "dealMonth", "dealDay",
                         "excluUseAr", "floor")
KEY_TRADE_WITH_AMOUNT = KEY_TRADE_BASE + ("dealAmount",)

KEY_RENT = ("aptSeq", "dealYear", "dealMonth", "dealDay",
            "excluUseAr", "floor", "deposit", "monthlyRent")

_NUMERIC = ("dealAmount", "dealYear", "dealMonth", "dealDay",
            "floor", "deposit", "monthlyRent")

_AREA_SCALE = Decimal("0.0001")     # exclu_use_ar 컬럼 타입 numeric(9,4)와 동일

_RAW_DIR = {"trade": RAW, "rent": RAW_RENT}


def _items(path):
    """XML 파일 하나 -> dict 리스트. 정상 응답이 아니면 빈 리스트.

    parse_raw 와 따로 두는 이유
      - parse_raw 는 12개 필드 화이트리스트. 응답에 없는 태그와 안 읽는 태그를 구분 못 함
      - 감사는 실제로 온 태그만 담아야 '태그없음' 집계가 성립

    totalCount 검사
      - 인증 실패 응답도 유효한 XML. item 이 0개라 '거래 0건' 과 구분되지 않음
      - collect() 가 기존 파일을 지울 때 쓰는 기준과 동일

    _source_file: 원본에 없는 칸. API 필드와 구분되도록 밑줄 접두사
      - 파일명 형식 {구}_{계약월}_{페이지}.xml
      - 샘플 출력의 출처 표시, audit_fields 의 파일명 계약월 대조에 사용
    """
    text = path.read_text(encoding="utf-8")
    if "<totalCount>" not in text:
        print(f"[건너뜀] 정상 응답 아님: {path.name}")
        return []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        print(f"[건너뜀] 파싱 실패: {path.name}")
        return []
    rows = []
    for item in root.iter("item"):
        row = {c.tag: (c.text or "").strip() for c in item}
        row["_source_file"] = path.name
        rows.append(row)
    return rows


def _norm_one(field, value):
    """키 한 칸 -> DB 저장 표기. 변환 불가 시 ValueError.

    면적 반올림
      - numeric(9,4)와 같은 자리에서 끊음
      - Postgres numeric = 사사오입 -> ROUND_HALF_UP 명시
      - 파이썬 Decimal 기본값은 은행가 반올림. 그대로 두면 84.12345 에서 불일치
      - float 경유 금지. 십진 소수가 근사값이 됨
    """
    if field == "excluUseAr":
        try:
            return str(Decimal(value).quantize(_AREA_SCALE, rounding=ROUND_HALF_UP))
        except InvalidOperation:
            raise ValueError(f"{field}={value!r}") from None
    if field in _NUMERIC:
        try:
            return str(int(value.replace(",", "")))
        except ValueError:
            raise ValueError(f"{field}={value!r}") from None
    return value


# 빈 값을 기본값으로 바꾸는 유일한 칸
#   - 전세는 monthlyRent가 빈 값. 버리면 감사에서 전세 전량이 빠짐
#   - 수집 스크립트의 결측 처리와 같은 규칙 필수. 감사가 DB 보다 엄격해도 느슨해도 안 됨
#   - 다른 칸에는 적용하지 않음. deposit 결측과 0은 다른 사실
_BLANK_DEFAULT = {"monthlyRent": "0"}


def _key_of(row, fields):
    """행 하나 -> 자연키 튜플. 실패 시 (None, 사유)

    사유는 둘뿐
      - 결측: 키 칸이 빔. 수집 단계도 동일 기준으로 버림
      - 형식: 값은 있으나 컬럼 타입 변환 불가. DB 가 22P02 로 거부
    """
    out = []
    for f in fields:
        v = (row.get(f) or "").strip()
        if not v:
            v = _BLANK_DEFAULT.get(f, "")
        if not v:
            return None, f"결측 {f}"
        try:
            out.append(_norm_one(f, v))
        except ValueError as e:
            return None, f"형식 {e}"
    return tuple(out), None


def _bucket(rows, fields):
    """자연키별 그룹핑. 키 생성 실패 행은 사유 목록으로 분리."""
    buckets = collections.defaultdict(list)
    dropped = []
    for r in rows:
        key, reason = _key_of(r, fields)
        if key is None:
            dropped.append(reason)
        else:
            buckets[key].append(r)
    return buckets, dropped


def _print_dropped(dropped, total):
    """제외 행을 사유별 출력. 조용한 누락 방지용."""
    print(f"  키를 만들 수 있는 행         : {total - len(dropped):,}")
    print(f"  키를 못 만든 행              : {len(dropped):,}   "
          f"<- 수집이 버리거나 DB 가 거부하는 행")
    for reason, n in collections.Counter(dropped).most_common(6):
        print(f"      {reason:26s} {n:,}")


def summarize(group):
    """자연키 하나에 묶인 행들의 성격 집계.

    cdealType 분류
      - 태그 결손과 빈 값은 동일 취급 = 정상. 이 API 에서 둘 다 "해제 아님"
      - unknown = 'O'도 빈 값도 아닌 값. 0이 아니면 판정 규칙 재검토 대상
      - 전월세 응답에는 cdealType 자체가 없어 전 행이 정상으로 잡힘

    trade_count: 자연키가 나타내는 거래 건수 추정치 (원본에 거래 ID 없음)
      - 정상 n건        -> n
      - 정상 0 + 해제만  -> 1   (해제할 거래가 없으면 해제 통지도 없음)
      - 전 행 unknown  -> 0   (판정 불가. 합계 지표에서 제외)

    ambiguous_cancel: 정상 2건 이상 + 해제 존재. 해제 대상 식별 불가 표시
    """
    normal  = sum(1 for r in group if (r.get("cdealType") or "") == "")
    cancel  = sum(1 for r in group if (r.get("cdealType") or "") == "O")
    unknown = len(group) - normal - cancel
    return {
        "rows": len(group),
        "normal": normal,
        "cancel": cancel,
        "unknown": unknown,
        "trade_count": normal if normal else (1 if cancel else 0),
        "ambiguous_cancel": normal > 1 and cancel > 0,
    }


def _norm_row(row):
    """_source_file 을 뺀 전 칸. 행 동일성 비교용."""
    return tuple(sorted((f, v) for f, v in row.items() if f != "_source_file"))


def _classify(group):
    """중복 그룹의 성격 -> (성격, 충돌 칸 목록).

    unique 인덱스가 지우는 행이 실제 정보 손실인지 가르는 기준

    identical: 전 칸 동일. 같은 행이 두 번 온 것. 지워도 손실 없음
    subset:    갈리는 칸이 전부 '빈 값 vs 채워진 값'. 채워진 행을 남기면 손실 없음
    conflict:  비어 있지 않은 값이 둘 이상인 칸이 있음. 별개 거래인지 정정인지 판정 불가

    subset 이 최대 유형이면 적재는 빈 값이 채워진 값을 덮어쓰지 않아야 한다
    """
    if len({_norm_row(r) for r in group}) == 1:
        return "identical", []
    conflict = [f for f in set().union(*(set(r) for r in group))
                if f != "_source_file"
                and len({(r.get(f) or "").strip() for r in group} - {""}) > 1]
    return ("conflict" if conflict else "subset"), conflict


def _load_rows(kind):
    src = _RAW_DIR[kind]
    files = sorted(src.glob(f"{LAWD_CD}_*.xml"))
    if not files:
        print(f"[중단] {src}/{LAWD_CD}_*.xml 에 파일이 없다.")
        return None, 0
    rows = []
    for f in files:
        rows.extend(_items(f))
    return rows, len(files)


def _amount_sort_key(v):
    """숫자 우선 정렬. 이상값은 뒤로 보내고 예외는 내지 않음."""
    return (0, int(v), "") if v.isdigit() else (1, 0, v)


def audit_natural_key(kind="trade"):
    """후보 A(금액 포함) 유일성 감사. 행 하나짜리 키까지 포함한 전수.

    전수인 이유: 해제 단독건이 trade_count=1 규칙의 대표 사례
    """
    rows, n_files = _load_rows(kind)
    if rows is None:
        return
    key = KEY_TRADE_WITH_AMOUNT if kind == "trade" else KEY_RENT

    buckets, dropped = _bucket(rows, key)
    stats = {k: summarize(g) for k, g in buckets.items()}

    single = sum(1 for s in stats.values() if s["rows"] == 1)
    multi  = len(stats) - single

    # 아래 4개는 '행 둘 이상인 키'의 분할. 각각 독립 집계
    normal_only = sum(1 for s in stats.values()
                      if s["rows"] > 1 and s["cancel"] == 0 and s["unknown"] == 0)
    mixed       = sum(1 for s in stats.values()
                      if s["rows"] > 1 and s["normal"] > 0 and s["cancel"] > 0
                      and s["unknown"] == 0)
    cancel_only = sum(1 for s in stats.values()
                      if s["rows"] > 1 and s["cancel"] == s["rows"])
    multi_other = sum(1 for s in stats.values()
                      if s["rows"] > 1 and s["unknown"] > 0)
    parts = normal_only + mixed + cancel_only + multi_other

    # 지워지는 행이 실제 손실인지 판정
    #   - 해제 행 제외. 해제는 정제 단계에서 걸러지므로 유일성 판단 대상이 아님
    #   - 전월세는 cdealType 이 없어 전 행이 남음
    # 대상은 '행 둘 이상인 키'가 아니라 '정상 행이 2개 이상인 키'.
    # 위 4분할과 모수가 다르므로 shape_base 를 따로 세워 출력에 명시
    amb = sum(1 for s in stats.values() if s["ambiguous_cancel"])
    shape_base = normal_only + amb
    shape, shape_rows = collections.Counter(), collections.Counter()
    conflict_fields = collections.Counter()
    for g in buckets.values():
        alive = [r for r in g if (r.get("cdealType") or "") == ""]
        if len(alive) <= 1:
            continue
        c, fields = _classify(alive)
        shape[c] += 1
        shape_rows[c] += len(alive)
        for f in fields:
            conflict_fields[f] += 1

    # unknown은 정상/해제와 다른 축. multi의 하위 항목이 아니므로 분리 출력
    unknown_any = sum(1 for s in stats.values() if s["unknown"] > 0)

    cancel_single = sum(1 for s in stats.values()
                        if s["rows"] == 1 and s["cancel"] == 1)

    print(f"\n[{kind} / 금액 포함] 파일 {n_files}개 / 총 {len(rows):,}행")
    _print_dropped(dropped, len(rows))
    print(f"  고유 키                      : {len(stats):,}\n")
    print(f"  행 하나짜리 키              : {single:,}")
    print(f"  행 둘 이상인 키             : {multi:,}")
    print(f"    - 정상만 여러 건           : {normal_only:,}   (trade_count > 1 대상)")
    if kind == "trade":
        print(f"    - 정상 + 해제 혼재         : {mixed:,}")
        print(f"    - 해제만 여러 건           : {cancel_only:,}")
    print(f"    - 그 밖 (unknown 섞임)     : {multi_other:,}")
    print(f"    검산 {normal_only}+{mixed}+{cancel_only}+{multi_other} = {parts:,}"
          f" (행 둘 이상인 키 {multi:,})"
          + ("" if parts == multi else "   <- 불일치. 분류 조건에 구멍"))
    print()

    print(f"  지워지는 행의 성격 (자연키 밖 칸까지 비교)")
    print(f"    대상: 정상 행이 2개 이상인 키 {shape_base:,}"
          f"   (정상만 여러 건 {normal_only:,} + 정상2이상+해제 {amb:,})")
    for c, label, note in (("identical", "전 칸이 동일",       "손실 없음"),
                           ("subset",    "빈 값 vs 채워진 값", "채워진 행을 남기면 손실 없음"),
                           ("conflict",  "값이 서로 충돌",     "실제 손실 후보")):
        print(f"    - {label:18s} : {shape[c]:,}키 / 지워지는 행 {shape_rows[c] - shape[c]:,}"
              f"   <- {note}")
    if conflict_fields:
        print(f"      충돌 칸: {dict(conflict_fields.most_common())}")
    shape_sum = sum(shape.values())
    print(f"    검산 {shape['identical']}+{shape['subset']}+{shape['conflict']} = {shape_sum:,}"
          f" (대상 {shape_base:,})"
          + ("" if shape_sum == shape_base else "   <- 불일치. 분류 조건에 구멍"))
    print()

    # 전월세는 cdealType 필드 자체가 없음. 0을 찍으면 '해제가 없다'로 잘못 읽힘
    if kind == "trade":
        print(f"  해제 단독건 (정상0 + 해제1)  : {cancel_single:,}")
        print(f"  정상 2 이상 + 해제 있음      : {amb:,}   <- 해제 대상 식별 불가")
        print(f"  cdealType 이 O 도 빈 값도 아닌 행이 낀 키 : {unknown_any:,}   "
              f"<- 0 이 아니면 판정 규칙 재검토")
        print()

    # trade_count=0 (전 행 unknown)인 키는 합계와 분모 양쪽에서 제외
    counted  = sum(1 for s in stats.values() if s["trade_count"] > 0)
    total_tc = sum(s["trade_count"] for s in stats.values())
    label = "trade_count" if kind == "trade" else "계약건수"
    print(f"  {label} 합계            : {total_tc:,}   (판정 가능한 키 {counted:,}개 기준)")
    print(f"  {label} 분포            : "
          f"{dict(sorted(collections.Counter(s['trade_count'] for s in stats.values()).items()))}")
    # 이 설계의 실익 측정. 작으면 설계 반대 근거로도 사용
    print(f"  중복을 버렸다면 사라질 건수  : {total_tc - counted:,}건")

    if kind != "trade":
        return
    shown = 0
    for k, g in buckets.items():
        if shown >= 5:
            break
        if not stats[k]["ambiguous_cancel"]:
            continue
        print(f"\n  [식별 불가 샘플] 키={k}")
        for r in g:
            print(f"    해제={r.get('cdealType','')!r} 동={r.get('aptDong','')!r} "
                  f"등기={r.get('rgstDate','')!r} 해제일={r.get('cdealDay','')!r} "
                  f"금액={r.get('dealAmount','')!r} 출처={r.get('_source_file','')!r}")
        shown += 1


def audit_amount_tracking():
    """후보 B(금액 제외) 감사. 목적은 채택 여부가 아니라 정정 추적 가능성.

    측정 대상: 같은 base 키에 묶였으나 금액이 서로 다른 그룹

    해석 주의
      - '정정'으로 단정 불가. '금액이 다른 복수 거래'와 구분할 근거가 원본에 없음
      - 개수만 측정, 판단은 사람

    그룹 성격 분해
      - 정상만     -> 신고 금액 정정 가능성
      - 해제 섞임   -> 해제 통지의 금액 불일치 가능성
      - 다수 쪽이 금액 변경 이력 설계를 좌우

    위 [금액 포함] 절과는 키가 달라 숫자 비교 불가
    """
    rows, _ = _load_rows("trade")
    if rows is None:
        return

    buckets, dropped = _bucket(rows, KEY_TRADE_BASE)

    diff_amount, missing_amount = [], 0
    for k, g in buckets.items():
        amounts, blank = set(), 0
        for r in g:
            a = (r.get("dealAmount") or "").replace(",", "").strip()
            if a:
                amounts.add(a)
            else:
                blank += 1
        if len(amounts) > 1:
            diff_amount.append((k, g, amounts))
        if amounts and blank:
            missing_amount += 1

    dup = sum(1 for g in buckets.values() if len(g) > 1)

    # if/elif/else 3갈래 분류. 합은 구조상 diff_amount와 항상 같아 검산 대상 아님
    pat_normal = pat_cancel = pat_unknown = pat_amb = 0
    for _, g, _ in diff_amount:
        s = summarize(g)
        if s["unknown"]:
            pat_unknown += 1
        elif s["cancel"]:
            pat_cancel += 1
            if s["ambiguous_cancel"]:
                pat_amb += 1
        else:
            pat_normal += 1

    print(f"\n[trade / 금액 제외] 총 {len(rows):,}행")
    _print_dropped(dropped, len(rows))
    print(f"  고유 키                      : {len(buckets):,}")
    print(f"  중복 그룹                    : {dup:,}")
    print(f"  금액이 실제로 다른 그룹      : {len(diff_amount):,}   <- 정정 후보")
    print(f"    이 그룹들의 성격 (금액 제외 키 기준. 위 절과 비교 불가)")
    print(f"      - 해제 없이 정상만       : {pat_normal:,}   <- 신고 금액 정정 쪽")
    print(f"      - 해제가 섞여 있음       : {pat_cancel:,}   <- 해제 통지 금액 불일치 쪽")
    print(f"          그중 정상 2 이상     : {pat_amb:,}")
    print(f"      - unknown 섞임           : {pat_unknown:,}")
    print(f"  금액 결측과 값이 함께 있는 그룹: {missing_amount:,}   "
          f"<- 정정 아님. 데이터 품질 이상")

    for k, g, amounts in diff_amount[:5]:
        print(f"\n  [금액 상이 샘플] 키={k}")
        print(f"    금액 집합={sorted(amounts, key=_amount_sort_key)}")
        for r in g:
            print(f"    금액={r.get('dealAmount','')!r} 해제={r.get('cdealType','')!r} "
                  f"해제일={r.get('cdealDay','')!r} 동={r.get('aptDong','')!r} "
                  f"등기={r.get('rgstDate','')!r} 출처={r.get('_source_file','')!r}")


def audit_fields(kind="trade"):
    """컬럼별 실제 형식과 채움 상태 측정. 스키마 타입 결정 근거."""
    rows, _ = _load_rows(kind)
    if rows is None:
        return

    fields = (("cdealType", "cdealDay", "rgstDate", "dealingGbn",
               "aptDong", "umdCd", "landLeaseholdGbn")
              if kind == "trade" else
              # 전월세 응답은 일부 필드가 소문자. roadNm 아님
              # jibun: 매매의 bonbun/bubun 과 다른 규칙. 단일 필드 + zero-pad 없음
              #   - 미매칭 단지 지오코딩에서 매매/전월세 정규화를 따로 써야 하는 근거
              # monthlyRent: 자연키 구성 칸. _BLANK_DEFAULT 가 걸린 유일한 칸이라 빈값 수를 본다
              ("contractType", "contractTerm", "preDeposit",
               "preMonthlyRent", "useRRRight", "roadnm", "umdNm", "jibun",
               "monthlyRent"))

    print(f"\n[{kind}] 필드 실제 형식")
    print(f"  {'필드':20s} {'태그없음':>8s} {'빈값':>8s} {'채워짐':>8s}   예시")
    for f in fields:
        # 태그없음 > 0 -> 원본 스키마 변경. 결측 해석 규칙 전체 재검토 대상
        absent = sum(1 for r in rows if f not in r)
        blank  = sum(1 for r in rows if f in r and not r[f])
        filled = [r[f] for r in rows if r.get(f)]
        print(f"  {f:20s} {absent:8,d} {blank:8,d} {len(filled):8,d}   {filled[:3]}")

    floors, bad_floor = [], []
    for r in rows:
        v = (r.get("floor") or "").strip()
        if not v:
            continue
        try:
            floors.append(int(v))
        except ValueError:
            bad_floor.append(v)
    print(f"  floor 범위           : {min(floors) if floors else 'N/A'} ~ "
          f"{max(floors) if floors else 'N/A'} / 파싱 실패 {len(bad_floor):,}건 {bad_floor[:5]}")

    # 결측과 형식 오류를 나누는 기준은 _key_of와 동일. 합치면 원인별 대응이 갈리지 않음
    amt_field = "dealAmount" if kind == "trade" else "deposit"
    amt_blank = sum(1 for r in rows if not (r.get(amt_field) or "").strip())
    amt_bad   = [r[amt_field] for r in rows
                 if (r.get(amt_field) or "").strip()
                 and not r[amt_field].replace(",", "").strip().isdigit()]
    print(f"  {amt_field:12s} 결측 : {amt_blank:,}건 / 형식 오류 {len(amt_bad):,}건 {amt_bad[:5]}")

    # 소수 5자리 이상 출현 -> numeric(9,4)로 원본 보존 불가
    scales = collections.Counter()
    for r in rows:
        v = (r.get("excluUseAr") or "").strip()
        if "." not in v:
            continue
        try:
            Decimal(v)
        except InvalidOperation:
            continue
        scales[len(v.split(".", 1)[1])] += 1
    print(f"  excluUseAr 소수 자리수 분포: {dict(sorted(scales.items()))}   "
          f"<- 5 이상이면 numeric(9,4) 재검토")

    if kind == "trade":
        # 불변식 검산: 한 파일 = 한 (구, 계약월)
        #   - 불일치 > 0 -> DEAL_YMD = 계약월 전제 붕괴
        #   - 1콜 = 1개월, 3개월 롤링 윈도우가 이 전제 위에 있음
        # 계약월을 못 읽은 행은 불일치와 분리. 이상값 탐지 함수가 이상값에 죽으면 안됨
        mismatch, bad_ym = 0, 0
        for r in rows:
            src = (r.get("_source_file") or "").split("_")
            if len(src) < 2:
                continue
            try:
                ym = f"{int(r['dealYear'])}{int(r['dealMonth']):02d}"
            except (KeyError, ValueError):
                bad_ym += 1
                continue
            if src[1] != ym:
                mismatch += 1
        print(f"  파일명 계약월과 행 계약월 불일치: {mismatch:,}건 "
              f"/ 계약월 판독 불가 {bad_ym:,}건   <- 0 이 아니면 DEAL_YMD 전제 재검토")

        # 두 칸의 동반 채움률. 동 정보 결측을 '등기 전'으로 설명할 수 있는지 판단 근거
        both = sum(1 for r in rows if r.get("aptDong") and r.get("rgstDate"))
        dong = sum(1 for r in rows if r.get("aptDong"))
        rgst = sum(1 for r in rows if r.get("rgstDate"))
        print(f"  aptDong 과 rgstDate 동반 채움: {both:,} / 동 {dong:,} / 등기 {rgst:,}")


if __name__ == "__main__":

    # 매매:   python3 scripts/recon.py --audit-key
    # 전월세: python3 scripts/recon.py --audit-key --rent
    if "--audit-key" in sys.argv:
        kind = "rent" if "--rent" in sys.argv else "trade"
        audit_natural_key(kind)
        if kind == "trade":
            audit_amount_tracking()   # 금액 제외 키. 전월세는 dealAmount 자체가 없음
        audit_fields(kind)
        sys.exit(0)

    if "--report-only" in sys.argv:
        preflight(need_key=False)
    else:
        print(
            f"수집 시작: {SIGUNGU}({LAWD_CD}) "
            f"{START_YM}~{END_YM}\n"
        )
        collect(preflight())
        print()

    report()