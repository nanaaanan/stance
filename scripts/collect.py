"""국토교통부 아파트 실거래 수집기.

응답을 받아 파싱, 같은 응답 안의 중복 합치기, Supabase 적재, 이어하기,
응답에서 빠진 행 표시(is_current), 넣은/고친/안바뀐 수 기록까지

전제
    자연키    trade (apt_seq, deal_date, exclu_use_ar, floor, deal_amount)
              rent  (apt_seq, deal_date, exclu_use_ar, floor, deposit, monthly_rent)
    정규화    면적 반올림과 월세 결측 처리는 scripts/recon.py 와 같은 규칙이어야 함
              두 파일의 규칙이 달라지면 recon.py 로 센 숫자와 DB 행 수가 어긋남
    payload   docs/data/schema.md 의 컬럼 목록과 정확히 같아야 함
    DB 준비   빈 값 보호 트리거(trg_trade_keep_filled, trg_rent_keep_filled)가
              먼저 적용돼 있어야 함. 없으면 빈 값이 이미 채워진 값을 지움
              deal_change_log.run_id 연결(supabase/migrations/*_run_id_from_header.sql)은
              없어도 나머지 기능은 그대로 동작함

실행
    export DATA_GO_KR_KEY='디코딩_서비스키'

    DB 없이 건수만 확인
        python3 scripts/collect.py --kind trade --districts 11680 --months 202607 --dry-run

    실제 적재 (Supabase 접속 정보가 추가로 필요)
        set -a; source .env; set +a
        python3 scripts/collect.py --kind trade --districts 11680 --months 202607

    원본 응답을 남기며 확인 (파서를 의심할 때만)
        python3 scripts/collect.py --kind trade --districts 11680 --months 202607 --save-raw

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
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
DISTRICTS_PATH = ROOT / "config" / "districts.json"
RAW_DIR        = HERE / "raw"          # gitignore 대상. --save-raw 일 때만 씀
RAW_PREFIX     = "collect_"            # recon.py 가 만든 같은 이름 파일을 덮지 않기 위함


# ============================== 상수 ==============================
API = {
    "trade": "https://apis.data.go.kr/1613000/RTMSDataSvcAptTradeDev/getRTMSDataSvcAptTradeDev",
    "rent":  "https://apis.data.go.kr/1613000/RTMSDataSvcAptRent/getRTMSDataSvcAptRent",
}

NUM_OF_ROWS  = 1000          # 지정하지 않으면 10건씩만 옴
THROTTLE_SEC = 0.2           # 초당 30건까지 허용. 5건으로 여유를 둠
MAX_RETRY    = 3
TIMEOUT_SEC  = 30
# 한 번에 보낼 행 수
#   - 너무 크면 요청 본문이 커짐
#   - 한 묶음이 실패했을 때 다시 보내야 할 양도 커짐
UPSERT_CHUNK = 500

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


class SupabaseError(RuntimeError):
    """Supabase(PostgREST) 요청이 실패함. status 로 전체 중단 여부를 정함.

    401, 403 이면 키나 권한이 잘못된 것
      - 남은 요청도 같은 이유로 다 막히니 전체 중단
      - 그 밖의 status 는 이 (구, 계약월) 하나만 실패로 두고 다음으로 넘어감
    """

    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"status={status} {detail}")


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
def fetch(kind: str, lawd_cd: str, deal_ym: str, page: int, key: str) -> tuple:
    """API 를 한 번 호출해 (본문, 상태코드, Content-Type) 을 돌려줌. 재시도는 부르는 쪽이 담당.

    본문만 돌려주지 않는 이유
      - 파싱 실패 시 본문 대신 남길 진단 정보가 필요 (_fetch_page 참조)
    """
    q = urlencode({
        "serviceKey": key,
        "LAWD_CD": lawd_cd,
        "DEAL_YMD": deal_ym,
        "pageNo": page,
        "numOfRows": NUM_OF_ROWS,
    })
    with urlopen(f"{API[kind]}?{q}", timeout=TIMEOUT_SEC) as resp:
        return (resp.read().decode("utf-8"),
                resp.status,
                resp.headers.get("Content-Type", ""))


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
    key: str                 # data.go.kr 서비스키
    dry_run: bool = True
    sb_url: str = ""
    sb_key: str = ""
    save_raw: bool = False   # 기본 꺼짐. 36개월 전량에 켜면 약 230MB""


def _sb_config() -> tuple:
    """환경변수에서 Supabase 접속 정보를 읽음. dry-run 이면 부르지 않음.

    SUPABASE_URL 이 없으면 NEXT_PUBLIC_SUPABASE_URL 을 대신 봄
      - 지금 .env 에는 NEXT_PUBLIC_ 쪽만 있음
    ANON_KEY 가 아니라 SERVICE_ROLE_KEY 만 쓰는 이유
      - ANON_KEY 로는 RLS 에 막혀 쓰기가 안 됨
    """
    url = (os.environ.get("SUPABASE_URL") or os.environ.get("NEXT_PUBLIC_SUPABASE_URL") or "").strip()
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url or not key:
        sys.exit("[중단] Supabase 접속 정보가 없습니다. SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY 를 확인하세요.\n"
                 "       set -a; source .env; set +a")
    return url.rstrip("/"), key


def _sb_request(method: str, path: str, sb_url: str, sb_key: str, body=None, prefer=None,
                 extra_headers=None):
    """PostgREST 요청 한 번. 응답 본문이 있으면 그 값을, 없으면 None 을 돌려줌.

    실패한 응답의 본문까지 읽어서 SupabaseError 에 담는 이유
      - PostgREST 는 왜 막혔는지를 본문에 적어줌 (권한 문제면 필요한 GRANT 문까지)
      - 본문을 버리면 원인을 알 방법이 없음

    extra_headers 는 X-Run-Id 처럼 요청마다 달라지는 헤더용
    """
    headers = {
        "apikey": sb_key,
        "Authorization": f"Bearer {sb_key}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    if extra_headers:
        headers.update(extra_headers)
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = Request(f"{sb_url}/rest/v1/{path}", data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=TIMEOUT_SEC) as resp:
            raw = resp.read()
    except HTTPError as e:
        raise SupabaseError(e.code, e.read().decode("utf-8", errors="replace")) from None
    except (URLError, TimeoutError, OSError) as e:
        # HTTPError 가 아닌 통신 오류(타임아웃, DNS 실패 등)도 SupabaseError 로 통일
        #   - start_run / finish_run / done_today 가 "예외를 밖으로 안 낸다"고 약속하는데
        #     여기서 새어나가면 그 약속이 깨짐
        raise SupabaseError(0, str(e)) from None
    return json.loads(raw) if raw else None


def upsert(table: str, rows: list, key_fields: tuple, cfg: CollectConfig, run_id=None) -> int:
    """자연키 기준으로 넣거나 고침. UPSERT_CHUNK 개씩 나눠 보냄.

    on_conflict 값이 unique 인덱스의 컬럼 목록과 글자까지 같아야 하는 이유
      - 다르면 PostgREST 가 42P10 으로 거부
    return=minimal 을 쓰는 이유
      - 넣은 행을 그대로 돌려받을 필요가 없음. 응답만 커짐
    X-Run-Id 헤더를 붙이는 이유
      - log_deal_change 트리거가 이 헤더를 읽어 deal_change_log.run_id 를 채움
      - run_id 가 없으면(=start_run 실패) 헤더도 안 붙임. 트리거가 빈 값으로 처리함
    """
    if not rows:
        return 0
    if cfg.dry_run:
        return len(rows)

    path = f"{table}?on_conflict={','.join(key_fields)}"
    headers = {"X-Run-Id": str(run_id)} if run_id is not None else None
    sent = 0
    for i in range(0, len(rows), UPSERT_CHUNK):
        chunk = rows[i:i + UPSERT_CHUNK]
        _sb_request("POST", path, cfg.sb_url, cfg.sb_key, body=chunk,
                    prefer="resolution=merge-duplicates,return=minimal", extra_headers=headers)
        sent += len(chunk)
    return sent


def start_run(kind: str, lawd_cd: str, deal_ym: str, cfg: CollectConfig):
    """수집을 시작할 때 기록을 먼저 남김. 실패해도 예외 없이 None 을 돌려줌.

    끝난 뒤에만 기록하면, 중간에 죽었을 때 시도했다는 흔적조차 안 남음
      - status='running' 인 채로 남은 행이 '여기서 죽었다' 는 표시가 됨
    """
    if cfg.dry_run:
        return None
    body = [{
        "kind": kind, "lawd_cd": lawd_cd, "deal_ym": deal_ym,
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
    }]
    try:
        resp = _sb_request("POST", "collect_run", cfg.sb_url, cfg.sb_key,
                            body=body, prefer="return=representation")
        return resp[0]["id"]
    except SupabaseError as e:
        print(f"  [경고] collect_run 시작 기록 실패: {e}")
        return None


def finish_run(run_id, status: str, cfg: CollectConfig, **fields):
    """collect_run 행 하나를 마무리 상태로 고침.

    run_id 가 없으면(=start_run 이 실패했으면) 새 행을 만들어서라도 남김
      - 이때 kind, lawd_cd, deal_ym 이 fields 에 없으면 NOT NULL 위반으로 이 기록마저 사라짐
      - 부르는 쪽(main)이 실패를 대비해 항상 같이 넘겨줌
    실패해도 예외를 밖으로 내지 않음. 기록이 실패했다고 수집 결과까지 버릴 이유는 없음
    """
    if cfg.dry_run:
        return
    if fields.get("error_msg"):
        fields["error_msg"] = fields["error_msg"][:500]
    body = {"status": status, "finished_at": datetime.now(timezone.utc).isoformat(), **fields}
    try:
        if run_id is None:
            _sb_request("POST", "collect_run", cfg.sb_url, cfg.sb_key,
                        body=[body], prefer="return=minimal")
        else:
            _sb_request("PATCH", f"collect_run?id=eq.{run_id}", cfg.sb_url, cfg.sb_key,
                        body=body, prefer="return=minimal")
    except SupabaseError as e:
        print(f"  [경고] collect_run 종료 기록 실패: {e}")


def done_today(kind: str, cfg: CollectConfig) -> set:
    """오늘 이미 성공한 (구, 계약월) 조합. dry-run 이거나 --no-resume 이면 부르지 않음.

    읽기에 실패하면 빈 set (건너뛰는 것보다 다시 받는 편이 안전)
    '오늘' 로 자르는 이유
      - 어제 성공했어도 오늘 새로 신고된 거래가 있을 수 있음
    """
    if cfg.dry_run:
        return set()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00+00:00")
    path = (f"collect_run?select=lawd_cd,deal_ym&kind=eq.{kind}"
            f"&status=eq.ok&started_at=gte.{quote(today, safe='')}")
    try:
        rows = _sb_request("GET", path, cfg.sb_url, cfg.sb_key) or []
    except SupabaseError as e:
        print(f"  [경고] {kind} 완료 목록 조회 실패: {e}")
        return set()
    return {(r["lawd_cd"], r["deal_ym"]) for r in rows}


def _sb_count(path: str, cfg: CollectConfig) -> int:
    """HEAD 로 물어서 행 수만 얻음. 본문이 아니라 Content-Range 헤더를 읽음.

    _sb_request 를 그대로 못 쓰는 이유
      - _sb_request 는 본문을 읽는데, HEAD 응답은 본문이 없고 헤더에만 개수가 있음
    """
    req = Request(f"{cfg.sb_url}/rest/v1/{path}", headers={
        "apikey": cfg.sb_key,
        "Authorization": f"Bearer {cfg.sb_key}",
        "Prefer": "count=exact",
    }, method="HEAD")
    try:
        with urlopen(req, timeout=TIMEOUT_SEC) as resp:
            header = resp.headers.get("Content-Range", "")
    except HTTPError as e:
        raise SupabaseError(e.code, e.read().decode("utf-8", errors="replace")) from None
    except (URLError, TimeoutError, OSError) as e:
        raise SupabaseError(0, str(e)) from None
    # 모양은 '0-24/148'. 0건이면 '*/0'
    total = header.rsplit("/", 1)[-1] if "/" in header else ""
    if not total.isdigit():
        # 읽지 못하면 0 이 아니라 예외
        #   - 0 을 돌려주면 '표가 비어 있음' 과 구분이 안 됨
        #   - 그러면 넣은/고친 수가 전부 0 으로 나와 '변화 없음' 처럼 보임
        #   - 세는 기능이 통째로 죽어도 로그는 멀쩡해 보이게 됨
        raise SupabaseError(0, f"Content-Range 를 읽지 못함: {header!r}")
    return int(total)


def _max_log_id(cfg: CollectConfig) -> int:
    """deal_change_log 의 가장 큰 id. 비어 있으면 0."""
    rows = _sb_request("GET", "deal_change_log?select=id&order=id.desc&limit=1",
                        cfg.sb_url, cfg.sb_key) or []
    return rows[0]["id"] if rows else 0


def mark_stale(kind: str, lawd_cd: str, deal_ym: str, seen_at: str, cfg: CollectConfig,
                run_id=None) -> int:
    """이번 응답에 없던 행을 is_current=false 로 표시. 지우지 않음.

    last_seen_at 이 seen_at 보다 이른 행만 고르는 이유
      - seen_at 은 collect_one 이 이번 응답을 받기 전에 만든 값
      - 이번에 upsert 한 행은 last_seen_at 이 정확히 seen_at 이라 lt 조건에 안 걸림
      - 즉 이번에 안 들어온 행만 골라짐

    seen_at 을 quote() 로 감싸는 이유
      - '2026-08-20T05:12:33+00:00' 의 + 를 그대로 URL 에 넣으면 공백으로 읽힘
      - done_today 가 오늘 날짜를 넣을 때도 같은 문제라 같은 방식으로 감쌈

    예외를 여기서 잡지 않는 이유
      - 적재는 됐는데 마킹만 실패한 것도 실패로 봐야 함. main 이 판단하게 그대로 올림
    """
    if cfg.dry_run:
        return 0
    path = (f"{kind}?sgg_cd=eq.{lawd_cd}&deal_ym=eq.{deal_ym}"
            f"&last_seen_at=lt.{quote(seen_at, safe='')}&is_current=is.true&select=id")
    headers = {"X-Run-Id": str(run_id)} if run_id is not None else None
    resp = _sb_request("PATCH", path, cfg.sb_url, cfg.sb_key, body={"is_current": False},
                        prefer="return=representation", extra_headers=headers)
    return len(resp) if resp else 0


def _save_raw(kind: str, lawd_cd: str, deal_ym: str, page: int, body: str):
    """원본 응답을 파일로 남김. 실패해도 예외를 올리지 않음.

    파일명에 페이지를 넣는 이유
      - 한 (구, 계약월) 이 여러 페이지일 수 있음 (강남구 202607 전월세는 2페이지)
      - 페이지가 빠지면 뒤 페이지가 앞 페이지를 덮어써 조용히 사라짐
    타임스탬프를 넣지 않는 이유
      - 같은 (구, 계약월, 페이지) 는 덮어씀. 매번 새 파일이 쌓이면 관리 기능이 필요해짐
    collect_ 접두사를 붙이는 이유
      - recon.py 가 같은 규칙으로 만든 정찰 원본이 이미 있음. 덮으면 그 수치의 근거가 사라짐
    저장 실패로 수집을 멈추지 않는 이유
      - 원본은 진단 자료이지 수집 성공 조건이 아님
      - 다만 조용히 넘어가지는 않음. 켰는데 안 남은 것을 알 수 있어야 함
    """
    path = RAW_DIR / kind / f"{RAW_PREFIX}{lawd_cd}_{deal_ym}_{page}.xml"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    except OSError as e:
        print(f"  [경고] 원본 저장 실패 {path.name}: {e}")


def _fetch_page(kind: str, lawd_cd: str, deal_ym: str, page: int, key: str,
                 save_raw: bool = False) -> ET.Element:
    """한 페이지를 받아옴. 네트워크 오류만 다시 시도.

    다시 시도할 때마다 대기 시간을 두 배로 늘림 (0.2초, 0.4초, 0.8초)
    응답 코드 오류는 check_error 가 바로 예외를 내므로 이 재시도를 타지 않음

    재시도를 다 쓰면 ApiError 로 바꿔서 던지는 이유
      - 통신 오류를 그대로 올리면 main 이 못 잡아 전체 실행이 멈춤
      - 그러면 collect_run 에 running 인 행이 그대로 남고 실패 목록에도 안 잡힘
      - 이 (구, 계약월) 하나만 실패로 두고 나머지는 계속 받게 함
    """
    last_err = None
    for attempt in range(MAX_RETRY):
        try:
            body, status, ctype = fetch(kind, lawd_cd, deal_ym, page, key)
        except (URLError, TimeoutError, OSError) as e:
            last_err = e
            time.sleep(THROTTLE_SEC * (2 ** attempt))
            continue
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            # 인증 실패나 서버 오류일 때 XML 이 아닌 본문이 오는 경우가 있음
            # 본문을 로그에 남기지 않음
            #   - serviceKey 가 URL 쿼리에 포함 (fetch)
            #   - 차단 페이지가 요청 URL 을 되비추면 키가 그대로 로그에 남음
            #   - GitHub 시크릿 마스킹은 등록된 원문만 가림. %2F 등 인코딩 변형은 통과
            #   - 공개 레포의 Actions 로그는 누구나 열람 가능
            raise ApiError("", f"XML 이 아닌 응답 status={status} "
                               f"content-type={ctype!r} length={len(body)}") from None
        check_error(root)
        # 저장은 반드시 check_error 뒤에
        #   - 인증 실패 응답은 바깥 태그가 <OpenAPI_ServiceResponse> 로 바뀌지만 유효한 XML
        #   - 파싱만으로는 안 걸러짐. 저장되면 나중에 정상 응답으로 오인해 파싱함
        if save_raw:
            _save_raw(kind, lawd_cd, deal_ym, page, body)
        return root
    raise ApiError("", f"{MAX_RETRY}회 다시 시도했지만 실패: {last_err}") from last_err


def collect_one(kind: str, lawd_cd: str, deal_ym: str, cfg: CollectConfig,
                 run_id=None, progress: str = "") -> dict:
    """구 하나의 계약월 하나를 받아 파싱, 합치기, 적재, 넣은/고친/안바뀐 수 계산, 마킹까지.

    전체 건수(totalCount)로 페이지 수를 먼저 계산하는 이유
      - 파서가 자연키를 못 만든 행을 걸러내서, 받은 행 수와 전체 건수가 다를 수 있음
      - 받은 행 수로 끝을 판단하면 마지막 페이지를 놓침
    """
    seen_at = datetime.now(timezone.utc).isoformat()
    parser     = parse_trade if kind == "trade" else parse_rent
    key_fields = TRADE_KEY if kind == "trade" else RENT_KEY

    root  = _fetch_page(kind, lawd_cd, deal_ym, 1, cfg.key, cfg.save_raw)
    total = int(root.findtext(".//totalCount") or 0)
    pages = max(1, math.ceil(total / NUM_OF_ROWS)) if total else 0

    all_rows = list(parser(root, lawd_cd, seen_at))
    for page in range(2, pages + 1):
        time.sleep(THROTTLE_SEC)
        p_root = _fetch_page(kind, lawd_cd, deal_ym, page, cfg.key, cfg.save_raw)
        all_rows.extend(parser(p_root, lawd_cd, seen_at))

    merged, collapsed, amb, conflicts = merge_batch(all_rows, key_fields)

    # 넣은/고친 수는 PostgREST 가 안 알려줘서 앞뒤로 재서 계산
    #   - 이 구간에 같은 (kind, 구, 계약월) 을 두 번째로 돌리는 프로세스가 없다는 게 전제
    #   - 동시 실행 차단은 GitHub Actions concurrency 그룹이 맡음(이 파일의 일이 아님)
    count_path = f"{kind}?sgg_cd=eq.{lawd_cd}&deal_ym=eq.{deal_ym}&select=id"
    inserted = updated = unchanged = marked = 0
    before_rows = before_log_id = 0
    if not cfg.dry_run:
        before_rows = _sb_count(count_path, cfg)
        before_log_id = _max_log_id(cfg)

    sent = upsert(kind, merged, key_fields, cfg, run_id=run_id)   # kind 가 곧 테이블 이름

    if not cfg.dry_run:
        after_rows = _sb_count(count_path, cfg)
        changed = _sb_count(f"deal_change_log?id=gt.{before_log_id}&select=id", cfg)
        inserted, updated = after_rows - before_rows, changed
        # 음수 보정을 unchanged 계산보다 먼저
        #   - 나중에 하면 unchanged 가 음수 inserted 로 계산돼 셋의 합이 안 맞음
        if inserted < 0:
            print(f"  [경고] {kind} {lawd_cd} {deal_ym}: 넣은 수가 음수({inserted}) -> 0 처리. "
                  f"누군가 행을 지웠을 수 있음")
            inserted = 0
        unchanged = len(merged) - inserted - updated
        if unchanged < 0:
            print(f"  [경고] {kind} {lawd_cd} {deal_ym}: 안 바뀐 수가 음수({unchanged}) -> 0 처리")
            unchanged = 0

        # 마킹은 카운트 계산 뒤에
        #   - 마킹도 이력을 만듦
        #   - 먼저 하면 사라진 행이 '고친 수' 에 섞임
        # 합친 행이 0개면 마킹 안 함
        #   - API 가 일시적으로 0건을 주면 그 달 전체가 한 번에 false 로 뒤집힘
        if merged:
            marked = mark_stale(kind, lawd_cd, deal_ym, seen_at, cfg, run_id=run_id)

    # dry-run 은 실제로 보내지 않았으므로 sent 를 숫자로 찍지 않음
    if cfg.dry_run:
        sent_txt = "적재 안함"
    else:
        sent_txt = f"sent={sent:>5} in={inserted} up={updated} same={unchanged} stale={marked}"
    line = (f"  {progress}{kind:5s} {lawd_cd} {deal_ym}: totalCount={total:>5} page={pages:>2}  "
            f"parsed={len(all_rows):>5} merged={len(merged):>5} {sent_txt} "
            f"collapsed={collapsed:>3} ambiguous_cancel={amb}")
    if conflicts:
        line += f"  conflicts={conflicts}"
    print(line)

    return {
        "kind": kind, "lawd_cd": lawd_cd, "deal_ym": deal_ym,
        "total_count": total, "page_count": pages,
        "parsed_rows": len(all_rows), "merged_rows": len(merged), "fetched_rows": sent,
        "collapsed": collapsed, "ambiguous_cancel": amb, "conflicts": conflicts,
        "inserted_count": inserted, "updated_count": updated, "unchanged_count": unchanged,
        "marked_stale": marked,
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
        months = PRESETS[args.preset]()
    elif args.recent_months:
        months = months_back(args.recent_months, skip=1)
    elif args.months:
        months = month_range(args.months)
    else:
        sys.exit("[중단] --months / --recent-months / --preset 중 하나가 필요합니다.")
    # preset 처럼 두 구간을 이어붙이면 겹칠 수 있음
    #   - 겹친 채로 두면 같은 달을 두 번 호출해 API 한도만 씀
    return sorted(set(months))


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
    ap.add_argument("--no-resume", action="store_true", help="오늘 이미 성공한 것도 다시 받기")
    ap.add_argument("--save-raw", action="store_true",
                    help="검사를 통과한 원본 응답을 scripts/raw/{kind}/ 에 저장")
    args = ap.parse_args()

    key       = _get_key()
    months    = _resolve_months(args)
    districts = _resolve_districts(args.districts)
    kinds     = ("trade", "rent") if args.kind == "both" else (args.kind,)

    cfg = CollectConfig(key=key, dry_run=args.dry_run, save_raw=args.save_raw)
    if not args.dry_run:
        cfg.sb_url, cfg.sb_key = _sb_config()

    n_calls = len(districts) * len(months) * len(kinds)
    print(f"수집 대상: kind={list(kinds)}  구 {len(districts)}개  월 {len(months)}개  "
          f"(최소 {n_calls}콜, 페이지 2개 이상인 달은 더 많음)\n")

    parsed_total = merged_total = 0
    failed = []
    printed_keys = set()
    idx = 0   # 건너뛴 것도 세야 [12/150] 같은 진행률이 끝까지 맞음
    try:
        for kind in kinds:
            skip = set()
            if not cfg.dry_run and not args.no_resume:
                skip = done_today(kind, cfg)
                print(f"  {kind}: 오늘 이미 성공 {len(skip)}건 건너뜀")

            for lawd_cd in districts:
                for deal_ym in months:
                    idx += 1
                    if (lawd_cd, deal_ym) in skip:
                        continue
                    progress = f"[{idx:>3}/{n_calls:>3}] "

                    run_id = start_run(kind, lawd_cd, deal_ym, cfg)
                    try:
                        result = collect_one(kind, lawd_cd, deal_ym, cfg,
                                              run_id=run_id, progress=progress)
                    except FatalApiError as e:
                        finish_run(run_id, "error", cfg, kind=kind, lawd_cd=lawd_cd, deal_ym=deal_ym,
                                   error_code=e.code, error_msg=str(e))
                        raise
                    except ApiError as e:
                        # 한 구가 실패해도 나머지는 계속 받고, 끝에 모아서 알려줌
                        finish_run(run_id, "error", cfg, kind=kind, lawd_cd=lawd_cd, deal_ym=deal_ym,
                                   error_code=e.code, error_msg=str(e))
                        failed.append((kind, lawd_cd, deal_ym, str(e)))
                        print(f"  {progress}{kind:5s} {lawd_cd} {deal_ym}: 실패 {e}")
                        continue
                    except SupabaseError as e:
                        finish_run(run_id, "error", cfg, kind=kind, lawd_cd=lawd_cd, deal_ym=deal_ym,
                                   error_code=str(e.status), error_msg=e.detail)
                        if e.status in (401, 403):
                            # 키나 권한이 잘못된 것. 남은 요청도 다 같은 이유로 막히니 전체 중단
                            raise FatalApiError(str(e.status), "Supabase 인증/권한 오류") from e
                        failed.append((kind, lawd_cd, deal_ym, str(e)))
                        print(f"  {progress}{kind:5s} {lawd_cd} {deal_ym}: 실패 {e}")
                        continue

                    finish_run(run_id, "ok", cfg, kind=kind, lawd_cd=lawd_cd, deal_ym=deal_ym,
                               total_count=result["total_count"],
                               fetched_rows=result["fetched_rows"],
                               page_count=result["page_count"],
                               inserted_count=result["inserted_count"],
                               updated_count=result["updated_count"],
                               unchanged_count=result["unchanged_count"])
                    parsed_total += result["parsed_rows"]
                    merged_total += result["merged_rows"]
                    if kind not in printed_keys and result["rows"]:
                        print(f"  [{kind} payload {len(result['rows'][0])}칸] "
                              f"{sorted(result['rows'][0].keys())}")
                        printed_keys.add(kind)
                    time.sleep(THROTTLE_SEC)
    except FatalApiError as e:
        sys.exit(f"[중단] {e}")

    print(f"\n총 파싱 {parsed_total:,}건 / 병합 {merged_total:,}건 "
          f"(병합분 {parsed_total - merged_total:,}건)")

    if failed:
        print(f"\n실패 {len(failed)}건")
        for kind, lawd_cd, deal_ym, msg in failed:
            print(f"  {kind:5s} {lawd_cd} {deal_ym}  {msg}")
        sys.exit(1)


if __name__ == "__main__":
    main()
