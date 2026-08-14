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
    n_cancel = sum(1 for r in rows if r["cdealType"] == "O")
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


if __name__ == "__main__":
    if "--report-only" in sys.argv:
        preflight(need_key=False)
    else:
        print(f"수집 시작: {SIGUNGU}({LAWD_CD})  {START_YM}~{END_YM}\n")
        collect(preflight())
        print()
    report()