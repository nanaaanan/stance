"""국토교통부 아파트 실거래 수집기.

응답을 받아 파싱하고, 같은 응답 안의 중복을 합치는 데까지. DB 적재는 아직 없음

전제
    자연키    trade (apt_seq, deal_date, exclu_use_ar, floor, deal_amount)
              rent  (apt_seq, deal_date, exclu_use_ar, floor, deposit, monthly_rent)
    정규화    면적 반올림과 월세 결측 처리는 scripts/recon.py 와 같은 규칙이어야 함
              두 파일의 규칙이 달라지면 recon.py 로 센 숫자와 DB 행 수가 어긋남
    payload   docs/data/schema.md 의 컬럼 목록과 정확히 같아야 함

실행
    export DATA_GO_KR_KEY='디코딩_서비스키'
    python3 scripts/collect.py --kind trade --districts 11680 --months 202607 --dry-run
    python3 scripts/collect.py --kind trade --months 202607 --dry-run

    서비스키는 디코딩 키를 쓸 것
      - urlencode() 가 키를 한 번 더 인코딩함
      - 인코딩 키를 넣으면 두 번 인코딩되어 에러 30
"""

import argparse
import collections
import json
import math
import os
import pathlib
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen
import xml.etree.ElementTree as ET

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
DISTRICTS_PATH = ROOT / "config" / "districts.json"


# ============================== 상수 ==============================
API = {
    "trade": "https://apis.data.go.kr/1613000/RTMSDataSvcAptTradeDev/getRTMSDataSvcAptTradeDev",
    "rent":  "https://apis.data.go.kr/1613000/RTMSDataSvcAptRent/getRTMSDataSvcAptRent",
}

NUM_OF_ROWS  = 1000          # 지정하지 않으면 10건씩만 옴
THROTTLE_SEC = 0.2           # 초당 30건까지 허용. 5건으로 여유를 둠
MAX_RETRY    = 3
TIMEOUT_SEC  = 30

AREA_SCALE = Decimal("0.0001")   # exclu_use_ar 컬럼이 numeric(9,4) 라 소수 넷째 자리까지

# 정상 응답 코드. raw 응답 38개 전부 '000' 이었음
OK_CODES = ("000", "00")

# 다시 요청해도 결과가 같은 코드. 기다렸다 재시도하면 시간만 버림
FATAL_CODES = {
    "20": "활용승인 대기. 승인 전에는 몇 번을 불러도 같은 응답",
    "22": "일일 요청 한도 초과. 내일 다시 실행",
    "30": "서비스키 오류. 인코딩 키를 넣었을 가능성이 큼",
    "31": "서비스키 사용기한 만료",
}


class ApiError(RuntimeError):
    """API 가 정상 코드를 주지 않음. 이 (구, 계약월) 하나만 실패로 처리.

    code 를 들고 있는 이유
      - collect_run 테이블의 error_code 칸에 그대로 들어갈 값
    """

    def __init__(self, code: str, msg: str):
        self.code = code
        super().__init__(f"code={code!r} {msg}")


class FatalApiError(ApiError):
    """남은 요청도 전부 같은 이유로 실패. 바로 전체 중단.

    예외 메시지 대신 클래스로 구분하는 이유
      - 메시지 글자를 보고 재시도 여부를 정하면, 문구를 고치는 순간 판단이 틀어짐
    """


# ============================== 기간 ==============================
def month_range(spec: str) -> list:
    """'202308-202607' 또는 '202607' -> YYYYMM 리스트.

    DEAL_YMD 는 계약년월 하나만 받음. 기간을 한 번에 넘길 수 없어 1콜에 1개월
    """
    start, end = (spec.split("-", 1) if "-" in spec else (spec, spec))
    y, m = int(start[:4]), int(start[4:])
    ey, em = int(end[:4]), int(end[4:])
    if (y, m) > (ey, em):
        raise ValueError(f"시작월이 종료월보다 뒤: {spec!r}")
    out = []
    while (y, m) <= (ey, em):
        out.append(f"{y}{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def months_back(count: int, skip: int = 1) -> list:
    """이번 달에서 skip개월 거슬러 올라간 달부터 과거로 count개월.

    기본값 skip=1 로 이번 달을 빼는 이유
      - 거래 후 30일 안에 신고하면 되므로 최근 1~2개월은 아직 덜 들어옴
      - 그대로 쓰면 거래가 갑자기 줄어든 것처럼 보임
    """
    today = date.today()
    y, m = today.year, today.month
    for _ in range(skip):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    out = []
    for _ in range(count):
        out.append(f"{y}{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


# 값을 함수로 둔 이유: 실행하는 날짜에 따라 결과가 달라져야 함
PRESETS = {
    # 최근 12개월 + 24~26개월 전 3개월. 역전세를 보려면 2년 전 시점이 필요
    "rent-cut": lambda: months_back(12, skip=1) + months_back(3, skip=24),
}


# ============================== HTTP ==============================
def fetch(kind: str, lawd_cd: str, deal_ym: str, page: int, key: str) -> str:
    """API 를 한 번 호출해 XML 원문을 돌려줌. 재시도는 부르는 쪽이 담당."""
    q = urlencode({
        "serviceKey": key,
        "LAWD_CD": lawd_cd,
        "DEAL_YMD": deal_ym,
        "pageNo": page,
        "numOfRows": NUM_OF_ROWS,
    })
    with urlopen(f"{API[kind]}?{q}", timeout=TIMEOUT_SEC) as resp:
        return resp.read().decode("utf-8")


def check_error(root: ET.Element) -> str:
    """정상 코드가 아니면 예외. 정상이면 그 코드를 돌려줌.

    코드를 두 군데서 찾는 이유
      - 인증에 실패하면 응답 모양이 통째로 바뀜
      - 바깥 태그가 <response> 에서 <OpenAPI_ServiceResponse> 로, 코드 위치도 함께 이동

    정상 코드 목록을 통과 조건으로 쓰는 이유
      - 치명 코드만 막으면 나머지 에러가 '거래 0건' 처럼 보임
      - 실패가 성공으로 기록되는 길을 막음
    """
    code = (root.findtext(".//resultCode") or root.findtext(".//returnReasonCode") or "").strip()
    msg  = (root.findtext(".//resultMsg")  or root.findtext(".//returnAuthMsg")  or "").strip()
    if code in FATAL_CODES:
        raise FatalApiError(code, f"{FATAL_CODES[code]} (msg={msg!r})")
    if code not in OK_CODES:
        raise ApiError(code, f"알 수 없는 응답 코드 (msg={msg!r})")
    return code


# ============================== 변환 ==============================
def _row(item: ET.Element) -> dict:
    """item 안의 태그를 전부 소문자 키 dict 로.

    전월세는 필드명이 일부 소문자로 옴 (roadNm / roadnm). 여기서 한 가지로 맞춤
    """
    return {c.tag.lower(): (c.text or "").strip() for c in item}


def _int(v, default=None):
    """'110,000' -> 110000. 빈 값이면 default, 숫자가 아니면 ValueError."""
    v = (v or "").strip()
    if not v:
        return default
    try:
        return int(v.replace(",", ""))
    except ValueError:
        raise ValueError(f"정수 변환 실패 {v!r}") from None


def _int_soft(v, bad: collections.Counter, label: str):
    """자연키가 아닌 숫자 칸. 숫자가 아니면 None 으로 두고 넘어감.

    자연키 칸과 다르게 처리하는 이유
      - 준공년도 하나가 이상하다고 멀쩡한 거래를 통째로 버릴 이유가 없음
      - 대신 몇 건을 비웠는지 세어서 출력. 조용히 사라지지 않게
    """
    try:
        return _int(v)
    except ValueError:
        bad[f"형식 {label}"] += 1
        return None


def _area(v) -> str:
    """'84.98' -> '84.9800'. 소수 넷째 자리에서 반올림.

    float 를 거치면 안 되는 이유
      - float 는 근사값이라 84.98 이 84.98000000000001 로 저장될 수 있음
      - 자연키 값이 달라져서 같은 거래를 다른 거래로 보게 됨

    반올림 방식을 명시하는 이유
      - Postgres 는 0.5 를 항상 올림
      - 파이썬 Decimal 은 기본값이 0.5 를 짝수 쪽으로 보냄 (은행가 반올림)
      - 그대로 두면 84.12345 같은 값에서 둘의 결과가 갈림
    """
    try:
        return str(Decimal(v).quantize(AREA_SCALE, rounding=ROUND_HALF_UP))
    except InvalidOperation:
        raise ValueError(f"면적 변환 실패 {v!r}") from None


def _deal_date(r: dict) -> str:
    """연/월/일 세 칸을 'YYYY-MM-DD' 로. 월과 일은 앞자리 0 없이 옴 (7, 19)."""
    try:
        y, m, d = int(r["dealyear"]), int(r["dealmonth"]), int(r["dealday"])
    except (KeyError, ValueError):
        raise ValueError("계약일자 변환 실패") from None
    return f"{y:04d}-{m:02d}-{d:02d}"


def _ymd_dot(v):
    """'24.02.22' -> '2024-02-22'. 이 모양이 아니면 None.

    앞에 '20' 을 붙여도 되는 이유
      - 등기일이 거래일보다 앞설 수 없음
      - 실거래는 2006년부터 공개 -> 1900년대로 읽힐 일이 없음

    'YYYY-MM-DD' 로 바꿔두면 글자 순서대로 정렬해도 날짜 순서와 같아짐
      - 아래 병합에서 '가장 이른 해제일' 을 고를 때 이 성질을 씀
    """
    v = (v or "").strip()
    parts = v.split(".")
    if len(parts) != 3 or not all(p.isdigit() and 1 <= len(p) <= 2 for p in parts):
        return None
    yy, mm, dd = parts
    return f"20{int(yy):02d}-{int(mm):02d}-{int(dd):02d}"


def _blank_none(v):
    """빈 문자열은 None 으로. 값이 없는 것과 빈 값을 DB 에서 구분하기 위함."""
    v = (v or "").strip()
    return v if v else None


def _require(v, reason: str):
    """빈 값이면 ValueError."""
    if not v:
        raise ValueError(reason)
    return v


# ============================== 파서 ==============================
def parse_trade(root: ET.Element, lawd_cd: str, seen_at: str) -> list:
    """item 목록을 trade 테이블에 넣을 dict 리스트로.

    sgg_cd 에 응답의 sggCd 대신 호출에 쓴 구 코드를 넣는 이유
      - 어차피 같은 값
      - 태그가 빠져 있어도 값이 비지 않음

    값이 없을 때 두 갈래로 나눠 처리
      - 자연키와 NOT NULL 칸 : 그 행을 버리고 사유를 셈
      - 나머지 칸           : None 으로 두고 사유를 셈
    """
    rows = []
    dropped, soft = collections.Counter(), collections.Counter()
    for item in root.iter("item"):
        r = _row(item)
        try:
            apt_seq = _require(r.get("aptseq", ""), "결측 aptSeq")
            apt_nm  = _require(r.get("aptnm", ""), "결측 aptNm")
            deal_date    = _deal_date(r)
            exclu_use_ar = _area(r.get("excluusear", ""))
            floor = _int(r.get("floor"))
            if floor is None:
                raise ValueError("결측 floor")
            deal_amount = _int(r.get("dealamount"))
            if deal_amount is None:
                raise ValueError("결측 dealAmount")
        except ValueError as e:
            dropped[str(e)] += 1
            continue

        rows.append({
            "apt_seq": apt_seq,
            "deal_date": deal_date,
            "exclu_use_ar": exclu_use_ar,
            "floor": floor,
            "deal_amount": deal_amount,
            "trade_count": 1,            # 아래 merge_group 에서 다시 계산
            "ambiguous_cancel": False,   # 위와 같음
            "is_current": True,
            "sgg_cd": lawd_cd,
            "umd_cd": _blank_none(r.get("umdcd")),
            "umd_nm": _blank_none(r.get("umdnm")),
            "apt_nm": apt_nm,
            "apt_dong": _blank_none(r.get("aptdong")),
            "jibun": _blank_none(r.get("jibun")),
            "road_nm": _blank_none(r.get("roadnm")),
            "road_nm_bonbun": _blank_none(r.get("roadnmbonbun")),
            "road_nm_bubun": _blank_none(r.get("roadnmbubun")),
            "build_year": _int_soft(r.get("buildyear"), soft, "buildYear"),
            "dealing_gbn": _blank_none(r.get("dealinggbn")),
            "cdeal_type": _blank_none(r.get("cdealtype")),
            "cdeal_day": _ymd_dot(r.get("cdealday")),
            "rgst_date": _ymd_dot(r.get("rgstdate")),
            "sler_gbn": _blank_none(r.get("slergbn")),
            "buyer_gbn": _blank_none(r.get("buyergbn")),
            "estate_agent_sgg_nm": _blank_none(r.get("estateagentsggnm")),
            "land_leasehold_gbn": _blank_none(r.get("landleaseholdgbn")),
            "last_seen_at": seen_at,
        })
    if dropped:
        print(f"    [trade 파싱] 행 버림 {sum(dropped.values())}건 {dict(dropped)}")
    if soft:
        print(f"    [trade 파싱] 칸 비움 {sum(soft.values())}건 {dict(soft)}")
    return rows


def parse_rent(root: ET.Element, lawd_cd: str, seen_at: str) -> list:
    """item 목록을 rent 테이블에 넣을 dict 리스트로.

    매매 파서를 같이 쓸 수 없는 이유
      - 해제(cdealType), 직거래(dealingGbn) 필드가 아예 없음
      - 법정동 코드(umdCd)가 없음
      - roadnm 에 번지가 이미 붙어 있어서 따로 조립하면 안 됨

    보증금은 자연키라 비면 행을 버림. 월세만 빈 값을 0 으로 채움
    """
    rows = []
    dropped, soft = collections.Counter(), collections.Counter()
    for item in root.iter("item"):
        r = _row(item)
        try:
            apt_seq = _require(r.get("aptseq", ""), "결측 aptSeq")
            apt_nm  = _require(r.get("aptnm", ""), "결측 aptNm")
            deal_date    = _deal_date(r)
            exclu_use_ar = _area(r.get("excluusear", ""))
            floor = _int(r.get("floor"))
            if floor is None:
                raise ValueError("결측 floor")
            deposit = _int(_require(r.get("deposit", ""), "결측 deposit"))
            # 전세는 monthlyRent 가 빈 값으로 옴. recon.py 와 같은 규칙으로 0 처리
            monthly_rent = _int(r.get("monthlyrent"), default=0)
        except ValueError as e:
            dropped[str(e)] += 1
            continue

        rows.append({
            "apt_seq": apt_seq,
            "deal_date": deal_date,
            "exclu_use_ar": exclu_use_ar,
            "floor": floor,
            "deposit": deposit,
            "monthly_rent": monthly_rent,
            "trade_count": 1,            # 아래 merge_group 에서 다시 계산
            "is_current": True,
            "sgg_cd": lawd_cd,
            "umd_nm": _blank_none(r.get("umdnm")),
            "apt_nm": apt_nm,
            "jibun": _blank_none(r.get("jibun")),
            "road_nm_full": _blank_none(r.get("roadnm")),   # 번지 포함. 조립 금지
            "build_year": _int_soft(r.get("buildyear"), soft, "buildYear"),
            "contract_type": _blank_none(r.get("contracttype")),
            "contract_term": _blank_none(r.get("contractterm")),
            "pre_deposit": _int_soft(r.get("predeposit"), soft, "preDeposit"),
            "pre_monthly_rent": _int_soft(r.get("premonthlyrent"), soft, "preMonthlyRent"),
            "use_rr_right": _blank_none(r.get("userrright")),
            "last_seen_at": seen_at,
        })
    if dropped:
        print(f"    [rent 파싱] 행 버림 {sum(dropped.values())}건 {dict(dropped)}")
    if soft:
        print(f"    [rent 파싱] 칸 비움 {sum(soft.values())}건 {dict(soft)}")
    return rows


# ============================== 같은 응답 안의 중복 합치기 ==============================
# 해제된 거래는 정상 행 1개와 해제 행 1개 이상이 같은 응답에 함께 온다.
# 자연키가 같은 행이 둘 이상인 채로 한 번에 넣으면 Postgres 가 21000 으로 거부한다
#   (같은 행을 한 번의 INSERT 에서 두 번 고칠 수 없다는 뜻)
# 그래서 넣기 전에 여기서 먼저 합친다
TRADE_KEY = ("apt_seq", "deal_date", "exclu_use_ar", "floor", "deal_amount")
RENT_KEY  = ("apt_seq", "deal_date", "exclu_use_ar", "floor", "deposit", "monthly_rent")

CANCEL_FLAG = "cdeal_type"
EARLIEST    = "cdeal_day"

# 값이 서로 달라도 비워둘 수 없는 칸
#   - 비워서 보내면 NOT NULL 위반(23502)으로 500행 묶음 전체가 실패
#   - 실제로 갈리는 사례가 있음. 같은 단지인데 응답마다 이름 표기가 다름
#     (래미안블레스티지 / 래미안 블레스티지)
NEVER_NULL = frozenset({
    "apt_seq", "deal_date", "exclu_use_ar", "floor", "deal_amount",
    "deposit", "monthly_rent", "apt_nm", "sgg_cd",
    "trade_count", "ambiguous_cancel", "is_current", "last_seen_at",
})


def _pick(field: str, values: list):
    """한 칸의 후보 값들 중 하나를 고름. 어떤 순서로 들어와도 결과가 같아야 함.

    고르는 규칙
      - 해제 표시   : 'O' 가 하나라도 있으면 'O'
      - 해제일     : 자리수가 같으면 가장 이른 날. 다르면 정렬해서 첫 값
      - 비울 수 없는 칸 : 정렬해서 첫 값
      - 나머지     : 값이 서로 다르면 비움. 어느 쪽이 맞는지 모르는 것을 찍어 넣지 않음

    해제 표시의 마지막 줄에서 정렬 값을 쓰는 이유
      - 실제로는 'O' 아니면 빈 값뿐이라 지금은 차이가 안 남
      - 다만 '들어온 순서의 첫 값' 을 쓰면 순서에 따라 결과가 달라질 여지가 남음
    """
    uniq = sorted({v for v in values if v is not None}, key=str)

    if field == CANCEL_FLAG:
        if "O" in uniq:
            return "O"
        return sorted(values, key=str)[0] if values else None

    if len(uniq) <= 1:
        return uniq[0] if uniq else None

    if field == EARLIEST:
        if len({len(v) for v in uniq}) == 1:
            return min(uniq, key=str)
        return uniq[0]

    if field in NEVER_NULL:
        return uniq[0]

    return None


def merge_group(group: list) -> tuple:
    """자연키가 같은 행들을 1행으로. (합친 행, 값이 서로 달랐던 칸 목록) 을 돌려줌.

    받은 dict 를 고치지 않고 읽기만 함

    거래 건수와 해제 식별 여부를 _pick 에 맡기지 않는 이유
      - 파서가 모든 행에 1 과 False 를 넣어놔서, 값만 봐서는 그룹 구성을 알 수 없음
      - 정상 행과 해제 행이 몇 개인지를 직접 세야 함

    합친 결과는 해제 행이 아니라 '정상 행에 해제 표시를 얹은 것'
      - 해제 행에는 동과 등기일이 비어 있음
      - 그쪽을 남기면 단지 상세 화면에 보여줄 정보가 사라짐
    """
    fields = set()
    for row in group:
        fields.update(row.keys())

    merged, conflicts = {}, []
    for field in fields:
        values = [row.get(field) for row in group]
        merged[field] = _pick(field, values)
        # 비울 수 없어서 하나를 골랐더라도 갈렸다는 사실은 따로 남김
        # 고른 것과 갈린 것은 다른 이야기
        if len({v for v in values if v is not None}) > 1:
            conflicts.append(field)

    normal = [row for row in group if not row.get(CANCEL_FLAG)]
    cancel = [row for row in group if row.get(CANCEL_FLAG)]
    # 정상 행이 있으면 그 개수, 해제 통지만 왔으면 1건으로 봄
    # 해제할 거래가 없으면 해제 통지도 오지 않기 때문
    merged["trade_count"] = len(normal) if normal else (1 if cancel else 0)
    if "ambiguous_cancel" in fields:   # rent 에는 이 칸이 없음
        merged["ambiguous_cancel"] = len(normal) >= 2 and len(cancel) > 0

    return merged, sorted(conflicts)


def merge_batch(rows: list, key_fields: tuple) -> tuple:
    """받은 행 전체를 자연키별로 합침. (합친 행, 줄어든 행 수, 식별 불가 수, 갈린 칸)."""
    groups = collections.defaultdict(list)
    for row in rows:
        groups[tuple(row[f] for f in key_fields)].append(row)

    merged, amb = [], 0
    conflict_counter = collections.Counter()
    for group in groups.values():
        row, conflicts = merge_group(group)
        merged.append(row)
        if row.get("ambiguous_cancel"):
            amb += 1
        for f in conflicts:
            conflict_counter[f] += 1

    return merged, len(rows) - len(merged), amb, dict(conflict_counter)


# ============================== 수집 ==============================
@dataclass
class CollectConfig:
    key: str
    dry_run: bool = True


def _fetch_page(kind: str, lawd_cd: str, deal_ym: str, page: int, key: str) -> ET.Element:
    """한 페이지를 받아옴. 네트워크 오류만 다시 시도.

    다시 시도할 때마다 대기 시간을 두 배로 늘림 (0.2초, 0.4초, 0.8초)
    응답 코드 오류는 check_error 가 바로 예외를 내므로 이 재시도를 타지 않음
    """
    last_err = None
    for attempt in range(MAX_RETRY):
        try:
            body = fetch(kind, lawd_cd, deal_ym, page, key)
        except (URLError, TimeoutError, OSError) as e:
            last_err = e
            time.sleep(THROTTLE_SEC * (2 ** attempt))
            continue
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            # 인증 실패나 서버 오류일 때 XML 이 아닌 본문이 오는 경우가 있음
            raise ApiError("", f"XML 이 아닌 응답 {body[:200]!r}") from None
        check_error(root)
        return root
    raise last_err


def collect_one(kind: str, lawd_cd: str, deal_ym: str, cfg: CollectConfig) -> dict:
    """구 하나의 계약월 하나를 받아 파싱하고 합치는 데까지.

    전체 건수(totalCount)로 페이지 수를 먼저 계산하는 이유
      - 파서가 자연키를 못 만든 행을 걸러내서, 받은 행 수와 전체 건수가 다를 수 있음
      - 받은 행 수로 끝을 판단하면 마지막 페이지를 놓침
    """
    seen_at = datetime.now(timezone.utc).isoformat()
    parser     = parse_trade if kind == "trade" else parse_rent
    key_fields = TRADE_KEY if kind == "trade" else RENT_KEY

    root  = _fetch_page(kind, lawd_cd, deal_ym, 1, cfg.key)
    total = int(root.findtext(".//totalCount") or 0)
    pages = max(1, math.ceil(total / NUM_OF_ROWS)) if total else 0

    all_rows = list(parser(root, lawd_cd, seen_at))
    for page in range(2, pages + 1):
        time.sleep(THROTTLE_SEC)
        p_root = _fetch_page(kind, lawd_cd, deal_ym, page, cfg.key)
        all_rows.extend(parser(p_root, lawd_cd, seen_at))

    merged, collapsed, amb, conflicts = merge_batch(all_rows, key_fields)

    if cfg.dry_run:
        line = (f"  {kind:5s} {lawd_cd} {deal_ym}: totalCount={total:>5} page={pages:>2}  "
                f"parsed={len(all_rows):>5} merged={len(merged):>5} collapsed={collapsed:>3} "
                f"ambiguous_cancel={amb}")
        if conflicts:
            line += f"  conflicts={conflicts}"
        print(line)

    return {
        "kind": kind, "lawd_cd": lawd_cd, "deal_ym": deal_ym,
        "total_count": total, "page_count": pages,
        "parsed_rows": len(all_rows), "merged_rows": len(merged),
        "collapsed": collapsed, "ambiguous_cancel": amb, "conflicts": conflicts,
        "rows": merged,
    }


# ============================== CLI ==============================
def _load_districts() -> dict:
    data = json.loads(DISTRICTS_PATH.read_text(encoding="utf-8"))
    return {d["code"]: d["name"] for d in data["districts"]}


def _resolve_districts(spec: str) -> list:
    known = _load_districts()
    if spec == "all":
        return list(known)
    codes = [c.strip() for c in spec.split(",") if c.strip()]
    unknown = [c for c in codes if c not in known]
    if unknown:
        sys.exit(f"[중단] districts.json 에 없는 구 코드: {unknown}")
    return codes


def _resolve_months(args) -> list:
    if args.preset:
        return PRESETS[args.preset]()
    if args.recent_months:
        return months_back(args.recent_months, skip=1)
    if args.months:
        return month_range(args.months)
    sys.exit("[중단] --months / --recent-months / --preset 중 하나가 필요합니다.")


def _get_key() -> str:
    key = os.environ.get("DATA_GO_KR_KEY", "").strip()
    if not key:
        sys.exit("[중단] 서비스키가 없습니다.   export DATA_GO_KR_KEY='디코딩키'")
    if "%" in key:
        sys.exit("[중단] 서비스키에 '%'가 있습니다 -> 인코딩 키로 보입니다. 디코딩 키를 쓰세요.")
    return key


def main():
    ap = argparse.ArgumentParser(description="국토교통부 아파트 실거래 수집기")
    ap.add_argument("--kind", choices=["trade", "rent", "both"], default="both")
    ap.add_argument("--months", help="'202308-202607' 또는 '202607'")
    ap.add_argument("--recent-months", type=int, help="지난달부터 N개월 (이번 달 제외)")
    ap.add_argument("--preset", choices=list(PRESETS))
    ap.add_argument("--districts", default="all", help="'all' 또는 '11680,11590'")
    ap.add_argument("--dry-run", action="store_true", help="DB 에 쓰지 않고 건수만 확인")
    args = ap.parse_args()

    if not args.dry_run:
        sys.exit("[중단] DB 적재는 아직 구현되지 않았습니다. --dry-run 을 붙이세요.")

    key       = _get_key()
    months    = _resolve_months(args)
    districts = _resolve_districts(args.districts)
    kinds     = ("trade", "rent") if args.kind == "both" else (args.kind,)
    cfg       = CollectConfig(key=key, dry_run=True)

    n_calls = len(districts) * len(months) * len(kinds)
    print(f"수집 대상: kind={list(kinds)}  구 {len(districts)}개  월 {len(months)}개  "
          f"(최소 {n_calls}콜, 페이지 2개 이상인 달은 더 많음)\n")

    parsed_total = merged_total = 0
    failed = []
    printed_keys = set()
    try:
        for kind in kinds:
            for lawd_cd in districts:
                for deal_ym in months:
                    try:
                        result = collect_one(kind, lawd_cd, deal_ym, cfg)
                    except FatalApiError:
                        raise
                    except ApiError as e:
                        # 한 구가 실패해도 나머지는 계속 받고, 끝에 모아서 알려줌
                        failed.append((kind, lawd_cd, deal_ym, str(e)))
                        print(f"  {kind:5s} {lawd_cd} {deal_ym}: 실패 {e}")
                        continue
                    parsed_total += result["parsed_rows"]
                    merged_total += result["merged_rows"]
                    if kind not in printed_keys and result["rows"]:
                        print(f"  [{kind} payload {len(result['rows'][0])}칸] "
                              f"{sorted(result['rows'][0].keys())}")
                        printed_keys.add(kind)
                    time.sleep(THROTTLE_SEC)
    except FatalApiError as e:
        sys.exit(f"[중단] {e}")

    print(f"\ndry-run 총 파싱 {parsed_total:,}건 / 병합 {merged_total:,}건 "
          f"(병합분 {parsed_total - merged_total:,}건)")

    if failed:
        print(f"\n실패 {len(failed)}건")
        for kind, lawd_cd, deal_ym, msg in failed:
            print(f"  {kind:5s} {lawd_cd} {deal_ym}  {msg}")
        sys.exit(1)


if __name__ == "__main__":
    main()
